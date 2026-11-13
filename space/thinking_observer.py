"""Read-only formal-session observation. Never prompts, pays, or controls a device."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from thinking_store import ThinkingStore, ThinkingCapacityError
from thinking_native import NativeThinkingNormalizer
from thinking_bindings import binding_entries, BindingsFormatError
from thinking_collectors import SessionCollector

ROOT = Path(__file__).resolve().parent
BINDING_FILE = ROOT / 'data/runtime-binding.public.json'
ARCHIVE_ROOT = ROOT / 'data/observations'
NODE = os.environ.get('DSH_NODE') or shutil.which('node') or 'node'
MAX_ARCHIVE_BYTES = int(os.environ.get('DSH_THINKING_MAX_BYTES', str(1 * 1024 ** 3)))
SID = re.compile(r'^session-[A-Za-z0-9][A-Za-z0-9_-]{0,179}$')


class ThinkingBindingError(ValueError):
    def __init__(self, code, message='正式会话尚未登记，保留现有档案并等待接入'):
        super().__init__(message)
        self.code = code


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _read_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.observer.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def validate_binding(value, *, legacy_sids=(), allowed_ports=(19992,)):
    if not isinstance(value, dict):
        raise ThinkingBindingError('BINDING_INVALID')
    identities = [value[key] for key in ('sessionId', 'sid', 'session_id') if key in value]
    sid = identities[0] if identities else None
    if any(identity != sid for identity in identities):
        raise ThinkingBindingError('BINDING_SESSION_CONFLICT')
    if not isinstance(sid, str) or not SID.fullmatch(sid) or sid in legacy_sids:
        raise ThinkingBindingError('FORMAL_SESSION_REQUIRED')
    home = value.get('home')
    if (not isinstance(home, str) or not home or len(home) > 2000 or re.search(r'[\x00-\x1f]', home)
            or home.startswith(('\\\\', '//')) or not Path(home).is_absolute() or not Path(home).is_dir()):
        raise ThinkingBindingError('BINDING_HOME_INVALID')
    # A new profile in the legacy Home is not an independent formal identity.
    if os.path.normcase(str(Path(home).resolve())) == os.path.normcase(str(Path(r'C:\dsh-deps\DSH_Official\home').resolve())):
        raise ThinkingBindingError('LEGACY_HOME_NOT_FORMAL')
    profile = value.get('profile')
    if not isinstance(profile, str) or not profile.strip() or len(profile) > 120 or any(c in profile for c in '\r\n\x00'):
        raise ThinkingBindingError('BINDING_PROFILE_INVALID')
    model = value.get('model')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,160}', model):
        raise ThinkingBindingError('BINDING_MODEL_INVALID', '观察绑定的模型名无效')
    base = value.get('base')
    try:
        url = urlsplit(base)
        port = url.port
    except (TypeError, ValueError):
        raise ThinkingBindingError('BINDING_BASE_INVALID') from None
    if (url.scheme not in ('http', 'https') or url.hostname not in ('127.0.0.1', 'localhost', '::1')
            or port not in allowed_ports or url.username or url.password or url.path not in ('', '/')
            or url.query or url.fragment):
        raise ThinkingBindingError('BINDING_BASE_INVALID')
    safe = {'sessionId': sid, 'home': str(Path(home).resolve()), 'profile': profile,
            'base': base.rstrip('/'), 'model': model}
    if value.get('persona') is not None:
        if value['persona'] not in ('biao', 'li', 'surface', 'inner'):
            raise ThinkingBindingError('BINDING_PERSONA_INVALID')
        safe['persona'] = value['persona']
    if isinstance(value.get('provider'), str):
        if value['provider'] != 'deepseek-official':
            raise ThinkingBindingError('BINDING_PROVIDER_MISMATCH')
        safe['provider'] = value['provider']
    fingerprint = hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()
    return {**safe, 'fingerprint': fingerprint}


class ThinkingObserverManager:
    def __init__(self, *, project_root=ROOT, binding_file=BINDING_FILE, archive_root=ARCHIVE_ROOT,
                 max_bytes=MAX_ARCHIVE_BYTES, allowed_ports=(19992,), poll_seconds=2,
                 process_factory=subprocess.Popen, autostart=True, backlog_limit_ms=8000,
                 buffer_options=None, discover_seconds=0.25):
        self.project_root, self.binding_file, self.archive_root = Path(project_root), Path(binding_file), Path(archive_root)
        self.max_bytes, self.allowed_ports, self.poll_seconds = max_bytes, tuple(allowed_ports), poll_seconds
        # 主人 2026-10-24 提速：每条主人消息都开新会话，空间要等这里开始跟踪才派发。
        # 只缩短"发现新会话"的间隔；跟踪器自身的重连/检查间隔仍用 poll_seconds。
        self.discover_seconds = discover_seconds
        self.process_factory, self.autostart, self.node = process_factory, autostart, NODE
        self.backlog_limit_ms = backlog_limit_ms
        self.buffer_options = dict(buffer_options or {})
        self.lock, self.binding_lock, self.ingest_lock = threading.RLock(), threading.RLock(), threading.RLock()
        self.stop_event, self.thread = threading.Event(), None
        self.bindings, self.collectors, self.normalizers = {}, {}, {}
        self.binding_error = 'FORMAL_SESSION_NOT_REGISTERED'
        self._store = None
        self.archive_paused = False
        self.registry_file, self.registry = self.archive_root / 'registered-sessions.json', {}
        if self.registry_file.exists():
            data = _read_json(self.registry_file)
            if not isinstance(data, dict) or data.get('schemaVersion') != 1 or not isinstance(data.get('sessions'), dict):
                raise ThinkingBindingError('REGISTRY_INVALID')
            self.registry = data['sessions']

    @property
    def store(self):
        with self.lock:
            if self._store is None:
                self._store = ThinkingStore(self.archive_root, max_bytes=self.max_bytes,
                                            journal_mode='WAL' if self.max_bytes >= 16 * 1024**2 else 'DELETE')
            return self._store

    @property
    def binding(self):
        # Compatibility for old one-session internal callers; never select a
        # default when two identities are registered.
        with self.lock:
            return next(iter(self.bindings.values())) if len(self.bindings) == 1 else None

    def _sole_collector(self):
        binding = self.binding
        return self._collector(binding) if binding else None

    @property
    def transport_state(self):
        collector = self._sole_collector()
        return collector.transport_state if collector else 'waiting'

    @transport_state.setter
    def transport_state(self, value):
        collector = self._sole_collector()
        if collector:
            with collector.lock:
                collector.transport_state = value

    @property
    def follow_ready(self):
        collector = self._sole_collector()
        return bool(collector and collector.follow_ready)

    @property
    def child(self):
        collector = self._sole_collector()
        return collector.child if collector else None

    def legacy_sids(self):
        result = set()
        for name in ['control.json', 'dev-control.json']:
            path = self.project_root / name
            if path.exists():
                try:
                    sid = _read_json(path).get('sessionId')
                except (ValueError, OSError, AttributeError):
                    raise ThinkingBindingError('LEGACY_IDENTITY_UNREADABLE') from None
                if isinstance(sid, str):
                    result.add(sid)
        return result

    def _collector(self, binding):
        sid = binding['sessionId']
        with self.lock:
            collector = self.collectors.get(sid)
            if (collector is None or collector.binding['fingerprint'] != binding['fingerprint'] or
                    collector.stop_event.is_set() and self.is_active(sid, binding['fingerprint']) and not self.archive_paused):
                if collector:
                    collector.stop()
                collector = SessionCollector(self, binding)
                self.collectors[sid] = collector
            return collector

    def note_archive_info(self, info):
        if not info.get('paused'):
            return
        with self.lock:
            self.archive_paused = True
            collectors = list(self.collectors.values())
        for collector in collectors:
            collector.stop()

    def refresh_binding(self):
        # Only registry I/O is serialized here. The high-volume writer never
        # holds this lock, and steady-state status never scans the event table.
        with self.binding_lock:
            try:
                entries = binding_entries(_read_json(self.binding_file))
                legacy = self.legacy_sids()
                bindings = {}
                for entry in entries:
                    binding = validate_binding(entry, legacy_sids=legacy, allowed_ports=self.allowed_ports)
                    sid = binding['sessionId']
                    old = self.registry.get(sid)
                    if old and (any(old.get(k) != binding[k] for k in ('home', 'profile')) or
                                old.get('persona') is not None and old.get('persona') != binding.get('persona')):
                        raise ThinkingBindingError('BINDING_IDENTITY_CHANGED')
                    bindings[sid] = binding
                updated = {**self.registry, **bindings}
                if updated != self.registry:
                    _atomic_json(self.registry_file, {'schemaVersion': 1, 'sessions': updated})
                with self.lock:
                    self.registry, self.bindings, self.binding_error = updated, bindings, None
                    inactive = [c for sid, c in self.collectors.items()
                                if sid not in bindings or c.binding['fingerprint'] != bindings[sid]['fingerprint']]
                for collector in inactive:
                    collector.stop()
                for binding in bindings.values():
                    self._collector(binding)
                return self.binding or dict(bindings)
            except FileNotFoundError:
                code = 'FORMAL_SESSION_NOT_REGISTERED'
            except (ThinkingBindingError, BindingsFormatError) as error:
                code = error.code
            except (OSError, ValueError, TypeError, AttributeError):
                code = 'BINDING_UNREADABLE'
            with self.lock:
                self.bindings, self.binding_error = {}, code
                collectors = list(self.collectors.values())
            for collector in collectors:
                collector.stop()
            return None

    def is_active(self, sid, fingerprint):
        with self.lock:
            return bool(self.bindings.get(sid, {}).get('fingerprint') == fingerprint)

    def check_active(self, sid, fingerprint, *, refresh=True):
        if refresh:
            self.refresh_binding()
        if not self.is_active(sid, fingerprint):
            raise ThinkingBindingError('BINDING_NOT_ACTIVE')

    def require_session(self, session_id):
        if not isinstance(session_id, str) or not SID.fullmatch(session_id):
            raise ThinkingBindingError('FORMAL_SESSION_REQUIRED')
        self.refresh_binding()
        with self.lock:
            if session_id in self.legacy_sids() or session_id not in self.registry:
                raise ThinkingBindingError('FORMAL_SESSION_NOT_REGISTERED')
            return dict(self.registry[session_id])

    def normalizer(self, session_id):
        with self.lock:
            if session_id not in self.normalizers:
                registration = self.registry[session_id]
                self.normalizers[session_id] = NativeThinkingNormalizer(self.store, session_id,
                    expected_model=registration['model'], expected_provider=registration.get('provider'))
            return self.normalizers[session_id]

    def status(self, session_id):
        registration = self.require_session(session_id)
        collector = self._collector(registration)
        snapshot = collector.snapshot()
        if snapshot['cache'] is None and not snapshot['freshness']['pendingFrames']:
            # One cold read restores archived metadata. Once live ingestion has
            # started, status uses only the last fully committed compact cache.
            try:
                collector.load_cache()
                snapshot = collector.snapshot()
            except Exception:
                snapshot['cache_error'] = 'NATIVE_ARCHIVE_UNAVAILABLE'
        cache = snapshot['cache'] or {'archive': {}, 'native': {'requiresPause': True, 'code': 'MODEL_UNKNOWN'}}
        info, native = cache['archive'], cache['native']
        with self.lock:
            active = self.is_active(session_id, registration['fingerprint'])
            binding_error = self.binding_error
        connected = bool(active and snapshot['follow_ready'] and snapshot['transport'] == 'connected'
                         and snapshot['ready_fingerprint'] == registration['fingerprint'])
        blocked = bool(info.get('paused') or self.archive_paused)
        lagged = snapshot['freshness']['backlogAgeMs'] >= self.backlog_limit_ms
        ready = bool(connected and not lagged and not blocked and not snapshot['cache_error'] and
                     snapshot['cache'] and not native.get('requiresPause'))
        freshness = {**snapshot['freshness'], 'ready': ready, 'thresholdMs': self.backlog_limit_ms,
                     'clock': 'local-monotonic-receive-through-commit'}
        if blocked:
            code = 'ARCHIVE_CAPACITY_REACHED'
        elif binding_error or snapshot['cache_error']:
            code = binding_error or snapshot['cache_error']
        elif lagged:
            code = 'NATIVE_BACKLOG'
        else:
            code = 'WAITING_FOR_NATIVE_STREAM' if not connected else native.get('code') or 'READY'
        return {'ok': True, 'schemaVersion': 1, 'sessionId': session_id, 'persona': registration.get('persona'),
                'sourceInstanceId': info.get('sourceInstanceId'), 'sequenceScope': 'per-session',
                'registered': True, 'activeBinding': active, 'transport': snapshot['transport'] if active else 'inactive',
                'canObserve': ready, 'requiresPause': not ready or bool(native.get('requiresPause')),
                'code': code, 'nativeStatus': native.get('code'), 'expectedModel': registration['model'],
                'model': native.get('model'), 'provider': native.get('provider'), 'nativeCursor': native.get('nativeCursor'),
                'pendingSelection': native.get('pendingSelection'), 'pendingModel': native.get('pendingModel'),
                'pendingProvider': native.get('pendingProvider'), 'archive': info,
                'freshness': freshness, 'lastNativeTime': freshness['lastNativeTime'],
                'ingest': snapshot.get('ingest'),
                'lastIngestTime': freshness['lastIngestTime'], 'backlogAgeMs': freshness['backlogAgeMs'],
                'updatedAt': utc_now(), 'observationOnly': True}

    def page(self, session_id, after=0, limit=200):
        self.require_session(session_id)
        page = self.store.page(session_id, after=after, limit=limit)
        return {**page, 'ok': True, 'schemaVersion': 1, 'sessionId': session_id,
                'entries': page.get('events', []), 'sequenceScope': 'per-session'}

    def export(self, session_id):
        self.require_session(session_id)
        return self.store.export(session_id, include_raw=True)

    def record_status(self, session_id, code, *, state=None, requires_pause=False):
        registration = self.require_session(session_id)
        return self._collector(registration).record_status(code, state=state, requires_pause=requires_pause)

    def accept_native(self, session_id, value, *, binding_fingerprint=None):
        return self.accept_native_batch(session_id, [{'value': value, 'receivedAt': utc_now()}],
                                        binding_fingerprint=binding_fingerprint)

    def accept_native_batch(self, session_id, items, *, binding_fingerprint=None):
        registration = self.require_session(session_id)
        self.check_active(session_id, binding_fingerprint or registration['fingerprint'], refresh=False)
        return self._collector(registration).accept_batch(items, fingerprint=binding_fingerprint)

    def _observe(self, binding):
        return self._collector(binding).observe()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name='formal-thinking-manager', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            collectors = list(self.collectors.values())
        for collector in collectors:
            collector.stop()
        for collector in collectors:
            if collector.thread and collector.thread is not threading.current_thread():
                collector.thread.join(timeout=3)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def _run(self):
        while not self.stop_event.is_set():
            self.refresh_binding()
            with self.lock:
                bindings = list(self.bindings.values())
            for binding in bindings:
                collector = self._collector(binding)
                if (self.autostart and not self.archive_paused and
                        (not collector.thread or not collector.thread.is_alive()) and not collector.stop_event.is_set()):
                    collector.thread = threading.Thread(target=collector.run, name='thinking-' + binding['sessionId'], daemon=True)
                    collector.thread.start()
            self.stop_event.wait(max(.1, self.discover_seconds))
