"""One explicit SID per observer; no model, action or device RPCs."""
import datetime
import collections
import json
import os
import re
import subprocess
import threading
import time

from thinking_queue import PendingFrames, NativeFrameLimitError
from thinking_store import ThinkingCapacityError


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def native_time(value):
    candidates = []
    if isinstance(value, dict):
        candidates.extend([value.get('time'), (value.get('frame') or {}).get('time'),
                           (value.get('event') or {}).get('time')])
        for record in value.get('records', []) if isinstance(value.get('records'), list) else []:
            if isinstance(record, dict):
                candidates.append((record.get('event') or record).get('time'))
    return max((v for v in candidates if isinstance(v, (int, float)) and not isinstance(v, bool)), default=None)


class SessionCollector:
    def __init__(self, manager, binding):
        self.manager, self.binding = manager, dict(binding)
        self.sid = binding['sessionId']
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.child = None
        self.transport_state = 'waiting'
        self.follow_ready = False
        self.ready_fingerprint = None
        # 用于重连宽限：mux 正常续连只需一两秒，不能把这段时间当成观察失效。
        self.last_connected_mono = None
        self.buffer = PendingFrames(**manager.buffer_options)
        self.cache = None
        self.cache_error = None
        self.last_ingest_time = None
        self.last_batch_metrics = None
        self._lagged = False
        self._lag_notices = collections.deque()

    def track_freshness(self):
        freshness = self.buffer.snapshot()
        lagged = freshness['backlogAgeMs'] >= self.manager.backlog_limit_ms
        with self.lock:
            if lagged != self._lagged:
                self._lagged = lagged
                self._lag_notices.append({'code': 'NATIVE_BACKLOG' if lagged else 'NATIVE_BACKLOG_RECOVERED',
                    'receivedAt': utc_now(), 'requiresPause': lagged, 'backlogAgeMs': freshness['backlogAgeMs'],
                    'pendingFrames': freshness['pendingFrames'], 'inFlightFrames': freshness['inFlightFrames'],
                    'thresholdMs': self.manager.backlog_limit_ms})
        return freshness

    def flush_freshness_notices(self):
        with self.lock:
            notices = list(self._lag_notices)
        if not notices:
            return
        with self.manager.ingest_lock:
            with self.manager.store.transaction(self.sid) as tx:
                for notice in notices:
                    tx.emit({'schemaVersion': 1, 'kind': 'status', 'text': '', 'stream': False, 'complete': True,
                             'origin': 'native-live', 'time': None, 'model': None, 'provider': None,
                             'attemptId': None, 'turn': None, 'step': None, 'revision': None,
                             'chunkIndex': None, 'blockIndex': None, 'source': 'observer-freshness', **notice})
            with self.lock:
                for _ in notices:
                    self._lag_notices.popleft()
            self.load_cache()

    def load_cache(self, *, clear_error=False):
        # Called by the writer after commit, never under the manager/status lock.
        info = self.manager.store.info(self.sid, include_counts=False)
        self.manager.note_archive_info(info)
        native = self.manager.normalizer(self.sid).status(archive_info=info)
        with self.lock:
            previous = self.cache['archive'] if self.cache else None
            if previous and (info.get('lastRawSeq', 0), info.get('lastSeq', 0)) < (
                    previous.get('lastRawSeq', 0), previous.get('lastSeq', 0)):
                return  # A slow cold reader must not replace a newer committed gate.
            self.cache = {'archive': info, 'native': native}
            if clear_error:
                self.cache_error = None

    def snapshot(self):
        freshness = self.track_freshness()
        with self.lock:
            return {'transport': self.transport_state, 'follow_ready': self.follow_ready,
                    'ready_fingerprint': self.ready_fingerprint,
                    'last_connected_mono': self.last_connected_mono,
                    'cache': self.cache,
                    'cache_error': self.cache_error,
                    'ingest': dict(self.last_batch_metrics) if self.last_batch_metrics else None,
                    'freshness': {**freshness, 'lastIngestTime': self.last_ingest_time or freshness['lastIngestTime']}}

    def accept_batch(self, items, *, fingerprint=None):
        started = time.monotonic()
        fingerprint = fingerprint or self.binding['fingerprint']
        self.check_current()
        self.manager.check_active(self.sid, fingerprint)
        for item in items:
            value = item.get('value')
            if not isinstance(value, dict):
                raise ValueError('NATIVE_VALUE_INVALID')
            if value.get('type') == 'snapshot':
                header = value.get('header') or {}
                if any(header.get(k) not in (None, self.sid) for k in ('id', 'sessionId')):
                    raise ValueError('NATIVE_IDENTITY_MISMATCH')
        waiting = time.monotonic()
        with self.manager.ingest_lock:
            acquired = time.monotonic()
            self.check_current()
            self.manager.check_active(self.sid, fingerprint, refresh=False)
            try:
                events = self.manager.normalizer(self.sid).accept_many(items)
                committed = time.monotonic()
                committed_at = utc_now()
                self.load_cache(clear_error=True)
                cached = time.monotonic()
            except Exception:
                with self.lock:
                    self.cache_error = 'NATIVE_INGEST_UNAVAILABLE'
                try:
                    self.load_cache()  # Includes the store's sticky capacity pause.
                except Exception:
                    pass
                raise
        with self.lock:
            self.last_ingest_time = committed_at
            self.last_batch_metrics = {'frames':len(items),
                'validationMs':round((waiting-started)*1000,3),
                'writerWaitMs':round((acquired-waiting)*1000,3),
                'normalizeAndCommitMs':round((committed-acquired)*1000,3),
                'cacheRefreshMs':round((cached-committed)*1000,3),
                'totalMs':round((cached-started)*1000,3)}
            if any(item['value'].get('type') == 'snapshot' for item in items):
                self.follow_ready = True
                self.ready_fingerprint = fingerprint
        return events

    def checkpoint_if_idle(self):
        # Serialize with all collector writes, then recheck every session.
        # Newly received frames stay pending and count toward the unchanged
        # backlog gate even if they arrive during this PASSIVE checkpoint.
        if not self.manager.ingest_lock.acquire(blocking=False):
            return None
        try:
            with self.manager.lock:
                collectors = list(self.manager.collectors.values())
            if any(c.buffer.snapshot()['pendingFrames'] for c in collectors):
                return None
            return self.manager.store.checkpoint_idle()
        finally:
            self.manager.ingest_lock.release()

    def check_current(self):
        if (self.stop_event.is_set() or self.manager.stop_event.is_set() or
                self.manager.collectors.get(self.sid) is not self):
            raise RuntimeError('NATIVE_COLLECTOR_STOPPED')

    def record_status(self, code, *, state=None, requires_pause=False, transport_code=None):
        event = {'schemaVersion': 1, 'kind': 'status', 'text': '', 'stream': False, 'complete': True,
                 'origin': 'native-live', 'time': int(time.time() * 1000), 'receivedAt': utc_now(),
                 'model': None, 'provider': None, 'attemptId': None, 'turn': None, 'step': None,
                 'revision': None, 'chunkIndex': None, 'blockIndex': None, 'code': code,
                 'transport': state, 'requiresPause': requires_pause, 'source': 'observer-lifecycle'}
        if isinstance(transport_code, str) and re.fullmatch(r'[A-Z0-9_]{1,80}', transport_code):
            event['transportCode'] = transport_code
        try:
            with self.manager.ingest_lock:
                with self.manager.store.transaction(self.sid) as tx:
                    key = {'code': code, 'state': state, 'transportCode': event.get('transportCode')}
                    if tx.get_state('observer-lifecycle') != key:
                        tx.set_state('observer-lifecycle', key)
                        tx.emit(event)
                self.load_cache()
        except ThinkingCapacityError:
            with self.lock:
                self.transport_state, self.follow_ready = 'capacity-paused', False
            try:
                self.load_cache()
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()
        self.buffer.close()
        with self.lock:
            child = self.child
            self.follow_ready = False
        if child and child.poll() is None:
            child.terminate()

    def _watch_child(self, child):
        while not self.stop_event.wait(self.manager.poll_seconds):
            self.track_freshness()
            self.manager.refresh_binding()
            if not self.manager.is_active(self.sid, self.binding['fingerprint']) or child.poll() is not None:
                break
        if child.poll() is None:
            child.terminate()

    def _reader(self, child, buffer):
        try:
            for line in child.stdout:
                if self.stop_event.is_set() or self.manager.stop_event.is_set():
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError('NATIVE_MESSAGE_INVALID')
                kind = message.get('type')
                if kind == 'native':
                    now, mono = utc_now(), time.monotonic()
                    if not buffer.put(message, size=len(line.encode('utf-8')), received_at=now,
                                      received_mono=mono, native_time=native_time(message.get('value')), stop=self.stop_event):
                        break
                elif kind == 'transport' and message.get('state') in ('connected', 'disconnected', 'error'):
                    with self.lock:
                        self.transport_state = message['state']
                        if message['state'] != 'connected':
                            self.follow_ready = False
                        else:
                            self.last_connected_mono = time.monotonic()
                    if not buffer.put(message, stop=self.stop_event):
                        break
                else:
                    raise ValueError('NATIVE_MESSAGE_INVALID')
        except (ValueError, KeyError, TypeError, NativeFrameLimitError, UnicodeError):
            with self.lock:
                self.transport_state, self.follow_ready = 'error', False
                self.cache_error = 'NATIVE_FRAME_REJECTED'
            buffer.put({'type': 'transport', 'state': 'error', 'code': 'NATIVE_FRAME_REJECTED'}, stop=self.stop_event)
        finally:
            with self.lock:
                if self.transport_state != 'error':
                    self.transport_state = 'disconnected'
                self.follow_ready = False
            buffer.close()

    def observe(self):
        binding, manager = self.binding, self.manager
        self.buffer = buffer = PendingFrames(**manager.buffer_options)
        kwargs = {'cwd': manager.project_root, 'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE,
                  'stderr': subprocess.DEVNULL, 'text': True, 'encoding': 'utf-8', 'errors': 'strict', 'bufsize': 1}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        child = manager.process_factory([manager.node, str(manager.project_root / 'integration/thinking-raw-mux.mjs')], **kwargs)
        with self.lock:
            self.child, self.follow_ready, self.transport_state = child, False, 'connecting'
        try:
            wire = {k: binding[k] for k in ['home', 'base', 'sessionId']}
            child.stdin.write(json.dumps({**wire, 'maxMessages': 40}) + '\n')
            child.stdin.flush()
            threading.Thread(target=self._watch_child, args=(child,), daemon=True).start()
            self.record_status('NATIVE_CONNECTING', state='connecting', requires_pause=True)
            reader = threading.Thread(target=self._reader, args=(child, buffer), daemon=True)
            reader.start()
            while not self.stop_event.is_set() and not manager.stop_event.is_set():
                batch = buffer.get_batch()
                if not batch:
                    break
                if batch[0].get('type') == 'native':
                    items = [{'value': item['value'], 'receivedAt': item['receivedAt']} for item in batch]
                    self.accept_batch(items)
                    buffer.committed(batch, ingested_at=self.last_ingest_time)
                    self.track_freshness()
                    self.flush_freshness_notices()
                    self.checkpoint_if_idle()
                else:
                    message = batch[0]
                    self.record_status('NATIVE_' + message['state'].upper(), state=message['state'],
                                       requires_pause=True, transport_code=message.get('code'))
        except ThinkingCapacityError:
            with self.lock:
                self.transport_state, self.follow_ready = 'capacity-paused', False
        finally:
            buffer.close()
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            for stream in (child.stdin, child.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            with self.lock:
                self.child, self.follow_ready = None, False
                if self.transport_state != 'capacity-paused':
                    self.transport_state = 'disconnected'

    def run(self):
        try:
            self.load_cache()
        except Exception:
            self.cache_error = 'NATIVE_ARCHIVE_UNAVAILABLE'
        while not self.stop_event.is_set() and not self.manager.stop_event.is_set():
            if not self.manager.is_active(self.sid, self.binding['fingerprint']):
                break
            try:
                if not self.manager.store.info(self.sid, include_counts=False).get('paused'):
                    self.observe()
            except Exception:
                with self.lock:
                    self.transport_state, self.follow_ready = 'error', False
                try:
                    self.record_status('NATIVE_OBSERVER_UNAVAILABLE', state='error', requires_pause=True)
                except Exception:
                    self.cache_error = 'NATIVE_ARCHIVE_UNAVAILABLE'
            self.stop_event.wait(max(.2, self.manager.poll_seconds))
