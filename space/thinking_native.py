"""Lossless native session/follow observation normalizer; never drives a session.

Raw carrier values, reducer state and normalized events share one store transaction.
Native event seq, process-local revision and local persisted event seq stay separate.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from typing import Any


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    # JSON escapes preserve even an isolated UTF-16 surrogate received over the
    # native carrier. Hashing must not reject a raw value the archive can retain.
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _time(value=None):
    if value is None:
        return dt.datetime.now(dt.timezone.utc).isoformat()
    if isinstance(value, (str, int, float)):
        return value
    raise ValueError('received_at must be a timestamp or string')


def _expand(stream):
    """Expand the SDK's lossless AssistantStreamRecord and TimedChunk forms."""
    if not isinstance(stream, list):
        raise ValueError('native stream is not an array')
    output = []
    for record in stream:
        if not isinstance(record, dict):
            raise ValueError('invalid timed chunk')
        kind = record.get('type')
        if isinstance(record.get('chunk'), dict):
            output.append({'time': record.get('time'), 'chunk': copy.deepcopy(record['chunk'])})
            continue
        if kind not in ('reasoning-chunks', 'text-chunks', 'tool-call-chunks'):
            raise ValueError('unsupported compact stream record')
        members = record.get('args' if kind == 'tool-call-chunks' else 'texts')
        gaps, at = record.get('dt'), record.get('time0')
        if (not isinstance(members, list) or not members or not all(isinstance(x, str) for x in members)
                or not isinstance(gaps, list) or len(gaps) != len(members) - 1
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in gaps)
                or not isinstance(at, int) or isinstance(at, bool) or not _integer(record.get('index'))):
            raise ValueError('invalid compact stream run')
        for index, text in enumerate(members):
            if index:
                at += gaps[index - 1]
            chunk = {'type': {'reasoning-chunks': 'reasoning-delta', 'text-chunks': 'text-delta',
                              'tool-call-chunks': 'tool-call-delta'}[kind], 'index': record['index']}
            if kind == 'tool-call-chunks':
                if not isinstance(record.get('id'), str):
                    raise ValueError('tool run missing actual call id')
                chunk.update(id=record['id'], argumentsDelta=text)
                if 'name' in record:
                    chunk['name'] = record['name']
            else:
                chunk['text'] = text
            output.append({'time': at, 'chunk': chunk})
    return output


class NativeThinkingPaused(RuntimeError):
    requiresPause = True


_MISSING_STATE = object()


class _BatchStateTransaction:
    """Transaction-local state cache; raw/event rows still use the real tx.

    Normal reads/writes preserve the store's detached-value semantics. Only the
    live reducer borrows an owned mutable value, and that path always saves it.
    Consequently stale snapshot validation cannot modify a saved newer attempt.
    """
    def __init__(self, transaction):
        self.transaction = transaction
        self.values = {}
        self.dirty = {}

    def _load(self, key):
        if key not in self.values:
            self.values[key] = self.transaction.get_state(key, _MISSING_STATE)
        return self.values[key]

    def get_state(self, key, default=None):
        value = self._load(key)
        return copy.deepcopy(default if value is _MISSING_STATE else value)

    def borrow_state(self, key, default=None):
        value = self._load(key)
        return copy.deepcopy(default) if value is _MISSING_STATE else value

    def set_state(self, key, value):
        if self.values.get(key, _MISSING_STATE) is not value:
            self.values[key] = copy.deepcopy(value)
        self.dirty[key] = None

    def record_raw(self, raw_key, value):
        return self.transaction.record_raw(raw_key, value)

    def emit(self, event, dedupe_key=None):
        return self.transaction.emit(event, dedupe_key)

    def gap_frames(self, code):
        return self.transaction.gap_frames(code)

    def attempt_frames(self, attempt_id):
        return self.transaction.attempt_frames(attempt_id)

    def native_events(self, first, last):
        return self.transaction.native_events(first, last)

    def flush(self):
        for key in self.dirty:
            self.transaction.set_state(key, self.values[key])


class NativeThinkingNormalizer:
    def __init__(self, store, session_id, expected_model='deepseek-flash', expected_provider=None):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError('explicit native session id required')
        self.store, self.session_id = store, session_id
        self.expected_model, self.expected_provider = expected_model, expected_provider
        self._failed = None

    def status(self, archive_info=None):
        state = self.store.get_state(self.session_id, 'normalizer', {}) or {}
        info = self.store.info(self.session_id) if archive_info is None else archive_info
        mismatch = bool(state.get('model') is not None and self.expected_model is not None
                        and state['model'] != self.expected_model)
        mismatch |= bool(state.get('provider') is not None and self.expected_provider is not None
                         and state['provider'] != self.expected_provider)
        pending = state.get('pendingSelection')
        pending = pending if isinstance(pending, dict) else None
        pending_mismatch = bool(pending is not None and (
            pending.get('model') is not None and self.expected_model is not None and pending['model'] != self.expected_model
            or pending.get('provider') is not None and self.expected_provider is not None and pending['provider'] != self.expected_provider))
        pending_unknown = bool(pending is not None and (
            self.expected_model is not None and pending.get('model') is None
            or self.expected_provider is not None and pending.get('provider') is None))
        unknown = bool(self.expected_model is not None and state.get('model') is None
                       or self.expected_provider is not None and state.get('provider') is None)
        paused = bool(info.get('requiresPause') or info.get('paused'))
        return {'model': state.get('model'), 'provider': state.get('provider'),
                'pendingSelection': copy.deepcopy(pending), 'pendingModel': pending.get('model') if pending else None,
                'pendingProvider': pending.get('provider') if pending else None,
                'requiresPause': paused or mismatch or pending_mismatch or unknown or pending_unknown or bool(state.get('gaps')) or self._failed is not None,
                'code': info.get('pauseReason') if paused else 'MODEL_MISMATCH' if mismatch or pending_mismatch
                        else 'NATIVE_GAP' if state.get('gaps') else 'MODEL_UNKNOWN' if state.get('model') is None
                        else 'PROVIDER_UNKNOWN' if self.expected_provider is not None and state.get('provider') is None
                        else 'PENDING_MODEL_UNKNOWN' if pending_unknown
                        else 'NORMALIZER_UNAVAILABLE' if self._failed else 'READY',
                'nativeCursor': state.get('nativeCursor'), 'activeAttempt': state.get('activeAttempt'),
                'sourceInstanceId': info.get('sourceInstanceId'), 'paused': paused}

    def accept(self, value, received_at=None):
        """Compatibility entry point; commits the same one-item batch protocol."""
        return self.accept_many([{'value': value, 'receivedAt': received_at}])

    def accept_many(self, items):
        """Atomically ingest at most 128 ``{'value': dict, 'receivedAt': ...}`` items.

        ``receivedAt`` is optional and uses the same timestamp rules as accept.
        Returned events become visible only after the complete batch commits.
        A reducer/storage failure rolls back all of this batch's raw values,
        state and events; the store alone may retain its durable capacity pause.
        """
        if self._failed is not None:
            raise NativeThinkingPaused('Native observation ingestion is stopped after a persistence failure')
        if not isinstance(items, list) or len(items) > 128:
            raise ValueError('native batch must be a list of at most 128 items')
        prepared = []
        for item in items:
            if (not isinstance(item, dict) or 'value' not in item
                    or any(key not in ('value', 'receivedAt') for key in item)
                    or not isinstance(item['value'], dict)):
                raise ValueError('native batch item requires a follow value object')
            raw = copy.deepcopy(item['value'])
            prepared.append((raw, 'native:' + _hash(raw), _time(item.get('receivedAt'))))
        if not prepared:
            return []
        emitted = []
        try:
            with self.store.transaction(self.session_id) as transaction:
                tx = _BatchStateTransaction(transaction)
                state = None
                for raw, raw_key, received in prepared:
                    if not tx.record_raw(raw_key, raw):
                        continue
                    if state is None:
                        state = tx.get_state('normalizer', None) or {'version': 1, 'nativeCursor': None,
                            'model': None, 'provider': None, 'activeAttempt': None, 'attemptRefs': {}, 'byStep': {},
                            'byDurableSeq': {}, 'gaps': {}}
                    reducer = _Reducer(self, tx, state, emitted, received, raw_key)
                    reducer.accept(raw)
                if state is None:
                    # A reconnect snapshot can already be archived verbatim.
                    # Still allow evidence-based recovery of an older reducer
                    # error; raw deduplication must not suppress that check.
                    state = tx.get_state('normalizer', None)
                if state is not None:
                    _Reducer(self, tx, state, emitted, prepared[-1][2], prepared[-1][1]).recover_missing_end_identity()
                    tx.set_state('normalizer', state)
                tx.flush()
            return emitted
        except Exception as error:
            # The store rolls back raw + state + output together, and reserves
            # room for its own durable capacity-pause event. Never forge one.
            self._failed = type(error).__name__
            raise


class _Reducer:
    def __init__(self, owner, tx, state, emitted, received, raw_key):
        self.owner, self.tx, self.state, self.emitted = owner, tx, state, emitted
        self.received, self.raw_key = received, raw_key

    def emit(self, kind, text='', *, attempt=None, native_seq=None, origin='native-live',
             stream=False, complete=False, chunk_index=None, block_index=None, time=None,
             dedupe=None, model=None, provider=None, **extra):
        attempt = attempt or {}
        event = {'schemaVersion': 1, 'sessionId': self.owner.session_id,
                 'model': attempt.get('model', self.state.get('model')) if model is None else model,
                 'provider': attempt.get('provider', self.state.get('provider')) if provider is None else provider,
                 'attemptId': attempt.get('attemptId'), 'startedAfterSeq': attempt.get('startedAfterSeq'),
                 'turn': attempt.get('turn'), 'step': attempt.get('step'),
                 'revision': attempt.get('revision'), 'chunkIndex': chunk_index,
                 'blockIndex': block_index, 'time': time, 'receivedAt': self.received,
                 'kind': kind, 'text': text, 'stream': bool(stream), 'complete': bool(complete),
                 'origin': origin, 'nativeSeq': native_seq, 'rawKey': self.raw_key, **extra}
        event['pendingSelection'] = copy.deepcopy(self.state.get('pendingSelection'))
        row = self.tx.emit(event, dedupe_key=dedupe)
        if row is not None:
            self.emitted.append(row)
        return row

    def notice(self, code, *, gap=False, attempt=None, key=None, **details):
        if gap:
            self.state['gaps'][key or code] = code
        requires_pause = self._gate(self.state.get('model'), self.state.get('provider'))
        return self.emit('status', attempt=attempt, code=code, requiresPause=bool(gap or requires_pause or self.state['gaps']),
                         gap=gap, dedupe='status:' + _hash([code, key, details, self.raw_key]), **details)

    def _mismatch(self, model, provider):
        return bool(model is not None and self.owner.expected_model is not None and model != self.owner.expected_model
                    or provider is not None and self.owner.expected_provider is not None
                    and provider != self.owner.expected_provider)

    def _gate(self, model, provider):
        pending = self.state.get('pendingSelection')
        pending_gate = isinstance(pending, dict) and (
            self._mismatch(pending.get('model'), pending.get('provider'))
            or self.owner.expected_model is not None and pending.get('model') is None
            or self.owner.expected_provider is not None and pending.get('provider') is None)
        return bool(pending_gate or self._mismatch(model, provider)
                    or self.owner.expected_model is not None and model is None
                    or self.owner.expected_provider is not None and provider is None)

    def pending(self, value, *, source, native_seq=None):
        if value is not None and not isinstance(value, dict):
            self.notice('PENDING_SELECTION_INVALID', gap=True)
            return
        selected = None if value is None else {key: value.get(key) for key in ('provider', 'model', 'reasoningEffort')
                                                if isinstance(value.get(key), str)}
        if selected == self.state.get('pendingSelection'):
            return
        self.state['pendingSelection'] = selected
        mismatch = isinstance(selected, dict) and self._mismatch(selected.get('model'), selected.get('provider'))
        self.emit('status', native_seq=native_seq, code='MODEL_MISMATCH' if mismatch else 'PENDING_SELECTION',
                  requiresPause=self._gate(self.state.get('model'), self.state.get('provider')) or bool(self.state['gaps']),
                  modelSource=source, mismatchSource='pending-selection' if mismatch else None,
                  pendingSelectionIsActual=False,
                  dedupe='pending:' + _hash([selected, source, native_seq, self.raw_key]))

    def metadata(self, value, *, source, native_seq=None):
        if not isinstance(value, dict):
            return
        model, provider = value.get('model'), value.get('provider')
        if isinstance(model, dict):
            provider, model = model.get('provider', provider), model.get('model') or model.get('id')
        if not isinstance(model, str):
            model = None
        if not isinstance(provider, str):
            provider = None
        if model is None and provider is None:
            return
        old = (self.state.get('model'), self.state.get('provider'))
        if model is not None:
            self.state['model'] = model
        if provider is not None:
            self.state['provider'] = provider
        current = (self.state.get('model'), self.state.get('provider'))
        if current != old:
            code = 'MODEL_MISMATCH' if self._mismatch(*current) else 'MODEL_UNKNOWN' if current[0] is None else 'MODEL_SELECTED'
            self.emit('status', native_seq=native_seq, code=code, requiresPause=self._gate(*current)
                      or bool(self.state['gaps']), modelSource=source,
                      dedupe='model:' + _hash([current, source, native_seq, self.raw_key]))

    def attempt(self, frame, *, create=False, borrow=False):
        attempt_id = frame.get('attemptId')
        if not isinstance(attempt_id, str) or not attempt_id:
            self.notice('ATTEMPT_ID_MISSING', gap=True)
            return None, None
        started = frame.get('startedAfterSeq')
        refs = self.state['attemptRefs'].get(attempt_id, [])
        if started is not None and not _integer(started):
            self.notice('ATTEMPT_BASELINE_INVALID', gap=True)
            return None, None
        if started is None:
            active = self.state.get('activeAttempt')
            committed = self.committed_end_key(frame, refs)
            if committed is not None:
                key = committed
            elif active in refs:
                key = active
            elif len(refs) == 1:
                key = refs[0]
            else:
                self.notice('ATTEMPT_START_MISSING', gap=True, attemptIdObserved=attempt_id)
                return None, None
        else:
            key = 'native-attempt:' + _hash([attempt_id, started])
        saved = self.tx.borrow_state(key, None) if borrow else self.tx.get_state(key, None)
        if saved is None:
            if not create or started is None:
                self.notice('ATTEMPT_START_MISSING', gap=True, attemptIdObserved=attempt_id)
                return None, None
            saved = {'key': key, 'attemptId': attempt_id, 'startedAfterSeq': started,
                     'turn': frame.get('turn'), 'step': frame.get('step'), 'revision': frame.get('revision'),
                     'model': self.state.get('model'), 'provider': self.state.get('provider'),
                     'nextIndex': 0, 'chunks': {}, 'blocks': {}, 'terminal': False,
                     'endEmitted': False, 'finishReason': None, 'outcome': None}
            if refs:
                self.notice('ATTEMPT_RESTART', attempt=saved, previousAttemptKeys=refs,
                            identity='attemptId+startedAfterSeq', key=key)
            self.state['attemptRefs'].setdefault(attempt_id, []).append(key)
        if frame.get('revision') is not None:
            saved['revision'] = frame['revision']
        for field in ('turn', 'step'):
            if field in frame:
                saved[field] = frame[field]
        return key, saved

    def committed_end_key(self, frame, refs):
        """An end outcome's durable seq survives process-local ID reuse."""
        outcome = frame.get('outcome') or {}
        if (frame.get('type') != 'end' or not isinstance(outcome, dict)
                or outcome.get('kind') != 'committed' or not _integer(outcome.get('seq'))):
            return None
        seq = str(outcome['seq'])
        mapped = self.state['byDurableSeq'].get(seq)
        if mapped in refs:
            return mapped
        # Older archives did not save this correlation until the live end.
        # Require the actual durable event AND one uniquely matching saved
        # outcome, never the most recent/first reused attempt ID by guesswork.
        if self.tx.get_state('native-event:' + seq, None) is None:
            return None
        matches = []
        for key in refs:
            attempt = self.tx.get_state(key, None) or {}
            saved = attempt.get('outcome') or {}
            if (saved.get('kind') == 'committed' and saved.get('seq') == outcome['seq']
                    and saved.get('eventType') == outcome.get('eventType')):
                matches.append(key)
        return matches[0] if len(matches) == 1 else None

    def recover_missing_end_identity(self):
        """Repair only archived ends whose start and complete chunks exist.

        The historical error and raw frame remain append-only. A genuine
        missing start/chunk, conflicting event or ambiguous identity keeps its
        gate. This also repairs the old false error after an observer reload.
        """
        code = 'ATTEMPT_START_MISSING'
        if code not in self.state.get('gaps', {}):
            return
        frames = self.tx.gap_frames(code)
        if not frames:
            return
        verified = []
        for raw_key, value in frames.items():
            if value.get('type') != 'assistant-stream':
                return
            frame = value.get('frame') or {}
            refs = self.state['attemptRefs'].get(frame.get('attemptId'), [])
            key = self.committed_end_key(frame, refs)
            attempt = self.tx.get_state(key, None) if key else None
            if (not attempt or not _integer(attempt.get('startedAfterSeq'))
                    or not attempt.get('terminal') or not _integer(frame.get('index'))
                    or frame['index'] != attempt.get('nextIndex')
                    or set(attempt.get('chunks', {})) != {str(i) for i in range(frame['index'])}
                    or key in self.state['gaps']):
                return
            outcome = attempt.get('outcome') or {}
            if (outcome.get('kind') != 'committed' or outcome.get('seq') != frame['outcome']['seq']
                    or outcome.get('eventType') != frame['outcome'].get('eventType')
                    or outcome['seq'] < attempt['startedAfterSeq']):
                return
            # Every native durable event between the explicit start baseline
            # and committed end must have actually been archived/reduced.
            if any(self.tx.get_state('native-event:' + str(seq), None) is None
                   for seq in range(attempt['startedAfterSeq'], outcome['seq'] + 1)):
                return
            proof = self.archived_attempt_proof(frame, attempt)
            if proof is None:
                return
            verified.append((raw_key, key, frame['outcome']['seq'], proof))
        # All recorded occurrences of this legacy, unscoped error must have
        # direct complete evidence. Never clear another gap as a side effect.
        for _raw_key, key, seq, _proof in verified:
            self.state['byDurableSeq'][str(seq)] = key
        del self.state['gaps'][code]
        self.notice('ATTEMPT_START_RECOVERED',
                    recoverySource='archived-end+durable-event+complete-start/chunks',
                    recoveredRawKeys=[row[0] for row in verified],
                    recoveredNativeSeqs=[row[2] for row in verified],
                    verifiedEvidence=[row[3] for row in verified])

    def archived_attempt_proof(self, end_frame, attempt):
        """Check actual raw start/snapshot, every timed chunk and exact end."""
        baseline = attempt['startedAfterSeq']
        current, start_key, end_seen, hashes = None, None, False, {}
        for raw_key, value in self.tx.attempt_frames(end_frame['attemptId']):
            if value.get('type') == 'snapshot':
                active = (value.get('assistantStream') or {}).get('activeAttempt') or {}
                if active.get('attemptId') != end_frame['attemptId']:
                    continue
                current = active.get('startedAfterSeq')
                if current != baseline:
                    continue
                try:
                    timed = _expand(active.get('stream', []))
                except ValueError:
                    return None
                if active.get('nextIndex', len(timed)) != len(timed):
                    return None
                start_key = start_key or raw_key
                for index, chunk in enumerate(timed):
                    hashed = _hash(chunk)
                    if str(index) in hashes and hashes[str(index)] != hashed:
                        return None
                    hashes[str(index)] = hashed
            elif value.get('type') == 'assistant-stream':
                frame = value.get('frame') or {}
                if frame.get('attemptId') != end_frame['attemptId']:
                    continue
                if frame.get('type') == 'start':
                    current = frame.get('startedAfterSeq')
                    if current == baseline:
                        start_key = start_key or raw_key
                elif current == baseline and frame.get('type') == 'chunk':
                    index = frame.get('index')
                    if not _integer(index) or not isinstance(frame.get('chunk'), dict):
                        return None
                    hashed = _hash({'time': frame.get('time'), 'chunk': frame['chunk']})
                    if str(index) in hashes and hashes[str(index)] != hashed:
                        return None
                    hashes[str(index)] = hashed
                elif current == baseline and frame == end_frame:
                    end_seen = True
        saved_hashes = {i: chunk.get('hash') for i, chunk in attempt['chunks'].items()}
        if not start_key or not end_seen or hashes != saved_hashes:
            return None
        durable_seen = set()
        through = end_frame['outcome']['seq']
        for event in self.tx.native_events(baseline, through):
            if self.tx.get_state('native-event:' + str(event['seq']), None) != _hash(event):
                return None
            durable_seen.add(event['seq'])
        if len(durable_seen) != through - baseline + 1:
            return None
        return {'attemptId': end_frame['attemptId'], 'startedAfterSeq': baseline,
                'startRawKey': start_key, 'verifiedChunkCount': len(hashes),
                'durableFromSeq': baseline, 'durableThroughSeq': through,
                'verifiedDurableEventCount': len(durable_seen), 'endRawArchived': True}

    def save_attempt(self, key, attempt):
        self.tx.set_state(key, attempt)
        if attempt.get('turn') is not None or attempt.get('step') is not None:
            self.state['byStep'][_json([attempt.get('turn'), attempt.get('step')])] = key

    def block(self, attempt, block_index, kind):
        key = str(block_index) if block_index is not None else 'unknown:' + kind
        block = attempt['blocks'].setdefault(key, {'blockIndex': block_index, 'kind': kind, 'text': '', 'blockComplete': False})
        if block['kind'] != kind:
            self.notice('BLOCK_KIND_CONFLICT', attempt=attempt, gap=True, key=attempt['key'])
        return block

    def apply_chunk(self, attempt, timed, index, *, emit=True, origin='native-live'):
        chunk, at = timed['chunk'], timed.get('time')
        kind, block_index = chunk.get('type'), chunk.get('index')
        block_index = block_index if _integer(block_index) else None
        if kind == 'block-start':
            self.block(attempt, block_index, chunk.get('blockType', 'unknown'))
        elif kind in ('reasoning-delta', 'text-delta'):
            if not isinstance(chunk.get('text'), str):
                self.notice('TEXT_CHUNK_INVALID', attempt=attempt, gap=True, key=attempt['key'])
                return
            channel = 'reasoning' if kind == 'reasoning-delta' else 'text'
            block = self.block(attempt, block_index, channel)
            block['text'] += chunk['text']
            if emit:
                self.emit('reasoning.delta' if channel == 'reasoning' else 'reply.delta', chunk['text'],
                          attempt=attempt, origin=origin, stream=True, chunk_index=index,
                          block_index=block_index, time=at,
                          dedupe=f"delta:{attempt['key']}:{index}:{channel}")
        elif kind == 'block-end':
            final = chunk.get('block', {})
            channel = final.get('type')
            if channel in ('reasoning', 'text') and isinstance(final.get('text'), str):
                block = self.block(attempt, block_index, channel)
                previous, text = block['text'], final['text']
                block['text'], block['blockComplete'] = text, True
                if emit and text != previous:
                    if channel == 'reasoning':
                        self.emit('reasoning.snapshot', text, attempt=attempt, stream=True,
                                  chunk_index=index, block_index=block_index, time=at, blockComplete=True,
                                  dedupe=f"block-snapshot:{attempt['key']}:{block_index}:{_hash(text)}")
                    else:
                        self.reply_suffix(previous, text, attempt, block_index, index, at, origin)
            elif emit and channel == 'tool-call':
                self.emit('status', attempt=attempt, stream=True, code='TOOL_STREAM', channel='tool',
                          chunk_index=index, block_index=block_index, time=at,
                          requiresPause=self._gate(attempt.get('model'), attempt.get('provider')) or bool(self.state['gaps']),
                          dedupe=f"tool:{attempt['key']}:{index}")
        elif kind == 'tool-call-delta':
            if emit:
                self.emit('status', attempt=attempt, stream=True, code='TOOL_STREAM', channel='tool',
                          chunk_index=index, block_index=block_index, time=at,
                          requiresPause=self._gate(attempt.get('model'), attempt.get('provider')) or bool(self.state['gaps']),
                          dedupe=f"tool:{attempt['key']}:{index}")
        elif kind == 'finish':
            attempt['finishReason'] = copy.deepcopy(chunk.get('reason'))

    def reply_suffix(self, previous, text, attempt, block_index, chunk_index, at, origin, complete=False, native_seq=None):
        if text == previous:
            return
        if text.startswith(previous):
            self.emit('reply.delta', text[len(previous):], attempt=attempt, block_index=block_index,
                      chunk_index=chunk_index, time=at, origin=origin, stream=not complete,
                      complete=complete, native_seq=native_seq,
                      dedupe='reply-suffix:' + _hash([attempt['key'], block_index, text, complete]))
        else:
            self.notice('REPLY_RESYNC_REQUIRED', attempt=attempt, gap=True, key=attempt['key'],
                        channel='reply', replySnapshotAvailableInRaw=True, native_seq=native_seq)

    def live(self, frame):
        if not isinstance(frame, dict):
            self.notice('STREAM_FRAME_INVALID', gap=True)
            return
        kind = frame.get('type')
        self.metadata(frame, source='assistant-stream metadata')
        if isinstance(frame.get('chunk'), dict):
            self.metadata(frame['chunk'], source='SDK chunk metadata')
            self.metadata(frame['chunk'].get('metadata'), source='SDK chunk metadata')
        key, attempt = self.attempt(frame, create=kind == 'start', borrow=True)
        if attempt is None:
            return
        if self.state.get('model') is not None:
            attempt['model'] = self.state['model']
        if self.state.get('provider') is not None:
            attempt['provider'] = self.state['provider']
        if kind == 'start':
            self.state['activeAttempt'] = key
            self.emit('status', attempt=attempt, code='ATTEMPT_STARTED', stream=True,
                      requiresPause=self._gate(attempt.get('model'), attempt.get('provider')) or bool(self.state['gaps']),
                      dedupe='start:' + key)
        elif kind == 'chunk':
            index, chunk = frame.get('index'), frame.get('chunk')
            if not _integer(index) or not isinstance(chunk, dict):
                self.notice('STREAM_CHUNK_INVALID', attempt=attempt, gap=True, key=key)
            else:
                timed = {'time': frame.get('time'), 'chunk': chunk}
                chunk_hash = _hash(timed)
                old = attempt['chunks'].get(str(index))
                if old is not None and old['hash'] != chunk_hash:
                    self.notice('ATTEMPT_CHUNK_CONFLICT', attempt=attempt, gap=True, key=key,
                                chunkIndex=index, restart=True)
                elif old is None and attempt['terminal']:
                    self.notice('CHUNK_AFTER_TERMINAL', attempt=attempt, gap=True, key=key, chunkIndex=index)
                elif old is None:
                    attempt['chunks'][str(index)] = {'hash': chunk_hash, 'timed': timed}
                    if index > attempt['nextIndex']:
                        self.notice('STREAM_GAP', attempt=attempt, gap=True, key=key,
                                    expectedChunkIndex=attempt['nextIndex'], observedChunkIndex=index)
                    while str(attempt['nextIndex']) in attempt['chunks']:
                        current = attempt['nextIndex']
                        self.apply_chunk(attempt, attempt['chunks'][str(current)]['timed'], current)
                        attempt['nextIndex'] += 1
                    if index <= attempt['nextIndex'] and key in self.state['gaps'] and all(int(i) < attempt['nextIndex'] for i in attempt['chunks']):
                        if self.state['gaps'][key] == 'STREAM_GAP':
                            del self.state['gaps'][key]
                            self.notice('STREAM_GAP_RECOVERED', attempt=attempt)
        elif kind == 'end':
            if _integer(frame.get('index')) and frame['index'] != attempt['nextIndex']:
                self.notice('STREAM_END_GAP', attempt=attempt, gap=True, key=key,
                            expectedChunkIndex=attempt['nextIndex'], observedEndIndex=frame['index'])
            attempt['terminal'], attempt['outcome'] = True, copy.deepcopy(frame.get('outcome'))
            outcome = attempt['outcome'] or {}
            if _integer(outcome.get('seq')):
                self.state['byDurableSeq'][str(outcome['seq'])] = key
            self.end_reasoning(attempt, origin='native-live', native_seq=outcome.get('seq'))
            if self.state.get('activeAttempt') == key:
                self.state['activeAttempt'] = None
        self.save_attempt(key, attempt)

    def end_reasoning(self, attempt, *, origin, native_seq=None, time=None):
        outcome = attempt.get('outcome') or {'kind': 'committed'}
        for block in attempt['blocks'].values():
            if block['kind'] == 'reasoning':
                self.emit('reasoning.end', attempt=attempt, block_index=block['blockIndex'], time=time,
                          chunk_index=attempt.get('nextIndex'), native_seq=native_seq, origin=origin,
                          stream=False, complete=True, outcome=outcome, finishReason=attempt.get('finishReason'),
                          interrupted=attempt.get('interrupted', False),
                          contentComplete=outcome.get('kind') == 'committed'
                          and outcome.get('eventType') != 'assistant/attempt' and not attempt.get('interrupted')
                          and (attempt.get('finishReason') or {}).get('kind') not in ('aborted', 'error')
                          and attempt['key'] not in self.state['gaps'],
                          dedupe=f"reasoning-end:{attempt['key']}:{block['blockIndex']}")
        self.emit('status', attempt=attempt, origin=origin, native_seq=native_seq,
                  stream=False, complete=True, code='ATTEMPT_ENDED', outcome=outcome,
                  finishReason=attempt.get('finishReason'), interrupted=attempt.get('interrupted', False),
                  requiresPause=self._gate(attempt.get('model'), attempt.get('provider')) or bool(self.state['gaps']),
                  dedupe='attempt-end:' + attempt['key'])
        attempt['endEmitted'] = True

    def snapshot_attempt(self, snapshot):
        if not isinstance(snapshot, dict):
            self.notice('SNAPSHOT_STREAM_INVALID', gap=True)
            return
        active = snapshot.get('activeAttempt')
        if not isinstance(active, dict):
            return
        frame = {**active, 'revision': snapshot.get('revision')}
        key, saved = self.attempt(frame, create=True)
        if saved is None:
            return
        try:
            timed = _expand(active.get('stream', []))
        except ValueError:
            self.notice('SNAPSHOT_STREAM_INVALID', attempt=saved, gap=True, key=key)
            return
        if active.get('nextIndex', len(timed)) != len(timed):
            self.notice('SNAPSHOT_CHUNK_GAP', attempt=saved, gap=True, key=key,
                        snapshotChunkCount=len(timed), nativeNextIndex=active.get('nextIndex'))
            return
        for index, item in enumerate(timed):
            old = saved['chunks'].get(str(index))
            if old and old['hash'] != _hash(item):
                self.notice('ATTEMPT_CHUNK_CONFLICT', attempt=saved, gap=True, key=key,
                            chunkIndex=index, restart=True)
                return
        if len(timed) < saved['nextIndex'] or saved['terminal']:
            self.notice('STALE_ATTEMPT_SNAPSHOT', attempt=saved)
            return
        rebuilt = copy.deepcopy(saved)
        rebuilt.update(blocks={}, chunks={}, nextIndex=len(timed), revision=snapshot.get('revision'))
        for index, item in enumerate(timed):
            rebuilt['chunks'][str(index)] = {'hash': _hash(item), 'timed': item}
            self.apply_chunk(rebuilt, item, index, emit=False)
        for block_key, block in rebuilt['blocks'].items():
            previous = saved['blocks'].get(block_key, {}).get('text', '')
            if block['kind'] == 'reasoning':
                self.emit('reasoning.snapshot', block['text'], attempt=rebuilt, block_index=block['blockIndex'],
                          origin='reconnect-snapshot', stream=True, complete=False,
                          chunk_index=len(timed) - 1 if timed else None,
                          dedupe='reconnect-snapshot:' + _hash([key, block_key, block['text']]))
            elif block['kind'] == 'text':
                self.reply_suffix(previous, block['text'], rebuilt, block['blockIndex'],
                                  len(timed) - 1 if timed else None, None, 'reconnect-snapshot')
        if self.state['gaps'].get(key) == 'STREAM_GAP':
            del self.state['gaps'][key]
        self.state['activeAttempt'] = key
        self.save_attempt(key, rebuilt)

    def durable(self, event):
        if not isinstance(event, dict) or not _integer(event.get('seq')):
            self.notice('DURABLE_EVENT_INVALID', gap=True)
            return
        seq, kind, data = event['seq'], event.get('type'), event.get('data', {})
        data = data if isinstance(data, dict) else {}
        key, value_hash = 'native-event:' + str(seq), _hash(event)
        old = self.tx.get_state(key, None)
        if old is not None:
            if old != value_hash:
                self.notice('PERSISTENT_EVENT_CONFLICT', gap=True, native_seq=seq)
            return
        self.tx.set_state(key, value_hash)
        if kind == 'request/header':
            header = data.get('header', {})
            self.metadata(header.get('config', {}), source='request/header', native_seq=seq)
        elif kind == 'model/selection':
            # Selecting a future model is not proof that a physical request used
            # it. Native request/header or assistant source proves actual usage.
            self.pending(data, source='model/selection pending', native_seq=seq)
        elif kind in ('assistant/message', 'assistant/attempt'):
            message = data.get('message', {})
            message = message if isinstance(message, dict) else {}
            self.metadata(message.get('source', {}), source='assistant source', native_seq=seq)
            attempt_key = self.state['byDurableSeq'].get(str(seq)) or self.state['byStep'].get(_json([data.get('turn'), data.get('step')]))
            attempt = self.tx.get_state(attempt_key, None) if attempt_key else None
            if attempt is None:
                attempt_key = 'durable-attempt:' + _hash([seq, message.get('id')])
                attempt = {'key': attempt_key, 'attemptId': data.get('attemptId'), 'startedAfterSeq': None,
                    'turn': data.get('turn'), 'step': data.get('step'), 'revision': None,
                    'model': message.get('source', {}).get('model', self.state.get('model')),
                    'provider': message.get('source', {}).get('provider', self.state.get('provider')),
                    'blocks': {}, 'chunks': {}, 'nextIndex': None, 'terminal': True,
                    'finishReason': data.get('finishReason'), 'outcome': {'kind': 'committed', 'eventType': kind, 'seq': seq}}
            else:
                attempt = copy.deepcopy(attempt)
                attempt['model'], attempt['provider'] = self.state.get('model'), self.state.get('provider')
                attempt['terminal'], attempt['outcome'] = True, {'kind': 'committed', 'eventType': kind, 'seq': seq}
            attempt['interrupted'] = data.get('interrupted') is True
            content = message.get('content', [])
            content = content if isinstance(content, list) else []
            if isinstance(data.get('stream'), list):
                try:
                    timed = _expand(data['stream'])
                    for item in timed:
                        if item['chunk'].get('type') == 'finish':
                            attempt['finishReason'] = copy.deepcopy(item['chunk'].get('reason'))
                    if kind == 'assistant/attempt':
                        materialized = copy.deepcopy(attempt)
                        materialized['blocks'] = {}
                        for ordinal, item in enumerate(timed):
                            self.apply_chunk(materialized, item, ordinal, emit=False)
                        # This is a reduction of recorded raw chunks, not a
                        # manufactured assistant/message in native history.
                        content = [{'type': block['kind'], 'text': block['text'], '_nativeBlockIndex': block['blockIndex']}
                                   for block in materialized['blocks'].values() if block['kind'] in ('reasoning', 'text')]
                except ValueError:
                    self.notice('DURABLE_STREAM_INVALID', gap=True, key=attempt_key, native_seq=seq)
            for position, block in enumerate(content):
                if not isinstance(block, dict) or block.get('type') not in ('reasoning', 'text') or not isinstance(block.get('text'), str):
                    continue
                index = block.get('_nativeBlockIndex', position)
                current = self.block(attempt, index, block['type'])
                previous, text = current['text'], block['text']
                current['text'], current['blockComplete'] = text, True
                if block['type'] == 'reasoning':
                    if text != previous:
                        self.emit('reasoning.snapshot', text, attempt=attempt, block_index=index,
                                  origin='durable-backfill', stream=False, complete=True, native_seq=seq,
                                  time=event.get('time'), messageId=message.get('id'),
                                  dedupe='durable-snapshot:' + _hash([attempt_key, index, text]))
                else:
                    self.reply_suffix(previous, text, attempt, index, None, event.get('time'),
                                      'durable-backfill', complete=True, native_seq=seq)
            self.end_reasoning(attempt, origin='durable-backfill', native_seq=seq, time=event.get('time'))
            self.save_attempt(attempt_key, attempt)
            self.state['byDurableSeq'][str(seq)] = attempt_key
            if self.state.get('activeAttempt') == attempt_key:
                self.state['activeAttempt'] = None
        elif kind == 'turn/end':
            self.emit('turn.end', native_seq=seq, origin='durable-backfill', stream=False, complete=True,
                      time=event.get('time'), turn=data.get('turn'), outcome=copy.deepcopy(data.get('outcome')),
                      finishReason=copy.deepcopy(data.get('reason') or data.get('finishReason')),
                      dedupe='turn-end:' + str(seq))

    def accept(self, value):
        address = value.get('address')
        explicit_sid = value.get('sessionId') or (address.get('sessionId') if isinstance(address, dict) else None)
        if explicit_sid is not None and explicit_sid != self.owner.session_id:
            self.notice('SESSION_MISMATCH', gap=True)
            return
        kind = value.get('type')
        if kind == 'snapshot':
            cursor_before = self.state.get('nativeCursor')
            records = value.get('records', [])
            events = []
            if isinstance(records, list):
                events = [record.get('event', record) for record in records if isinstance(record, dict)]
                for event in sorted(events, key=lambda row: row.get('seq', -1)):
                    self.durable(event)
            cursor = value.get('cursor')
            if _integer(cursor):
                previous = self.state.get('nativeCursor')
                self.state['nativeCursor'] = max(previous, cursor) if _integer(previous) else cursor
            available = sorted({event.get('seq') for event in events if _integer(event.get('seq'))})
            new_available = [seq for seq in available if _integer(cursor_before) and seq > cursor_before
                             and _integer(cursor) and seq <= cursor]
            missing_window = (not _integer(cursor_before) or
                _integer(cursor) and cursor > cursor_before and (
                    not new_available or new_available[0] != cursor_before + 1
                    or new_available[-1] != cursor or len(new_available) != cursor - cursor_before))
            if value.get('hasMore') is True and missing_window:
                self.notice('NATIVE_HISTORY_WINDOW', gap=True, oldestAvailableNativeSeq=min(
                    (event.get('seq') for event in events if _integer(event.get('seq'))), default=None),
                    nativeHistoryHasMore=True, previouslyObservedNativeCursor=cursor_before)
            elif value.get('hasMore') is False and 'NATIVE_HISTORY_WINDOW' in self.state['gaps']:
                del self.state['gaps']['NATIVE_HISTORY_WINDOW']
                self.notice('NATIVE_HISTORY_WINDOW_RECOVERED', nativeHistoryHasMore=False)
            header = value.get('header', {})
            header = header if isinstance(header, dict) else {}
            self.metadata(header.get('config', header), source='snapshot/header', native_seq=cursor)
            projections = value.get('projections', {})
            values = projections.get('values', projections) if isinstance(projections, dict) else {}
            if isinstance(values, dict):
                selection = values.get('modelSelection', values.get('model', {}))
                if isinstance(selection, dict) and ('lastUsed' in selection or 'next' in selection):
                    if isinstance(selection.get('lastUsed'), dict):
                        self.metadata(selection['lastUsed'], source='snapshot/projections.lastUsed actual', native_seq=cursor)
                    elif 'lastUsed' in selection and selection['lastUsed'] is None:
                        self.state['model'], self.state['provider'] = None, None
                        self.notice('MODEL_UNKNOWN', native_seq=cursor, modelSource='snapshot/projections.lastUsed null')
                    if 'next' in selection:
                        self.pending(selection['next'], source='snapshot/projections.next pending', native_seq=cursor)
                else:
                    self.metadata(selection, source='snapshot/projections legacy-flat', native_seq=cursor)
            self.snapshot_attempt(value.get('assistantStream', {}))
            self.notice('SNAPSHOT_RECEIVED', native_seq=cursor, origin='reconnect-snapshot',
                        modelKnown=self.state.get('model') is not None)
        elif kind == 'event':
            event = value.get('event', {})
            seq, previous = event.get('seq'), self.state.get('nativeCursor')
            if _integer(seq) and _integer(previous) and seq > previous + 1:
                self.notice('PERSISTENT_GAP', gap=True, expectedNativeSeq=previous + 1, observedNativeSeq=seq)
            self.durable(event)
            if _integer(seq):
                self.state['nativeCursor'] = max(previous, seq) if _integer(previous) else seq
        elif kind == 'assistant-stream':
            self.live(value.get('frame'))
        elif kind in ('error', 'end'):
            self.notice('CARRIER_ERROR' if kind == 'error' else 'CARRIER_END', reconnectRequired=True)
        else:
            self.notice('NATIVE_VALUE_UNKNOWN', nativeType=kind)
