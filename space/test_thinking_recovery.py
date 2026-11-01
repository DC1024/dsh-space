"""No model/device I/O: reused native IDs and evidence-only legacy recovery."""
import contextlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thinking_native import NativeThinkingNormalizer, _Reducer
from thinking_store import ThinkingStore

SID = 'session-restart-regression'
ATTEMPT = SID + ':1'
CONFIG = {'model': 'deepseek-flash', 'provider': 'deepseek-official'}

def event(seq, kind, data):
    return {'type': 'event', 'event': {'seq': seq, 'type': kind, 'time': seq * 100, 'data': data}}

def stream(**fields):
    return {'type': 'assistant-stream', 'frame': {'attemptId': ATTEMPT, **fields}}

def turn(baseline, number, text):
    start = event(baseline, 'step/start', {'turn': number, 'step': 1})
    head = stream(type='start', startedAfterSeq=baseline, turn=number, step=1, revision=1)
    chunks = [
        stream(type='chunk', index=0, revision=2, time=baseline*100+1,
               chunk={'type': 'block-start', 'index': 0, 'blockType': 'reasoning'}),
        stream(type='chunk', index=1, revision=3, time=baseline*100+2,
               chunk={'type': 'reasoning-delta', 'index': 0, 'text': text}),
        stream(type='chunk', index=2, revision=4, time=baseline*100+3,
               chunk={'type': 'finish', 'reason': {'kind': 'stop'}}),
    ]
    committed = event(baseline+1, 'assistant/message', {'turn': number, 'step': 1,
        'message': {'id': f'm-{number}', 'source': CONFIG, 'content': [{'type': 'reasoning', 'text': text}]}})
    end = stream(type='end', index=3, revision=5,
                 outcome={'kind': 'committed', 'eventType': 'assistant/message', 'seq': baseline+1})
    tail = event(baseline+2, 'turn/end', {'turn': number})
    return [start, head, *chunks, committed, end, tail]

INITIAL = {'type': 'snapshot', 'cursor': 0, 'hasMore': False,
           'header': {'id': SID, 'config': CONFIG},
           'records': [event(0, 'request/header', {'header': {'config': CONFIG}})['event']],
           'assistantStream': {'revision': 0}}


class ThinkingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ThinkingStore(self.tmp.name, journal_mode='WAL')
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.normalizer = NativeThinkingNormalizer(self.store, SID, expected_provider='deepseek-official')
        self.normalizer.accept(INITIAL, 'initial')
        self.first = turn(1, 1, 'first-history')
        self.second = turn(4, 2, 'second-history')

    def feed(self, values):
        for index, value in enumerate(values):
            self.normalizer.accept(value, 'time-' + str(index))

    def state(self):
        return self.store.get_state(SID, 'normalizer')

    def payloads(self, kind):
        with contextlib.closing(sqlite3.connect(self.store.path)) as con:
            return con.execute('SELECT payload FROM records WHERE sid=? AND kind=? ORDER BY seq', (SID, kind)).fetchall()

    def legacy_failure(self):
        self.feed(self.first)
        # Emulate the released old resolver: it could only use active or a
        # single ID reference, and did not recover a persisted false notice.
        with patch.object(_Reducer, 'committed_end_key', return_value=None), \
             patch.object(_Reducer, 'recover_missing_end_identity'):
            self.feed(self.second)
        self.assertEqual(self.state()['gaps'], {'ATTEMPT_START_MISSING': 'ATTEMPT_START_MISSING'})
        self.assertTrue(self.normalizer.status()['requiresPause'])

    def test_reused_id_end_after_durable_message_resolves_new_baseline(self):
        self.feed(self.first)
        self.normalizer = NativeThinkingNormalizer(self.store, SID, expected_provider='deepseek-official')
        self.feed(self.second)
        state = self.state()
        keys = state['attemptRefs'][ATTEMPT]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(state['byDurableSeq']['2'], keys[0])
        self.assertEqual(state['byDurableSeq']['5'], keys[1])
        self.assertEqual(self.store.get_state(SID, keys[0])['blocks']['0']['text'], 'first-history')
        self.assertEqual(self.store.get_state(SID, keys[1])['blocks']['0']['text'], 'second-history')
        self.assertFalse(self.normalizer.status()['requiresPause'])

    def test_end_before_durable_message_also_remains_valid(self):
        self.feed(self.first)
        self.feed(self.second[:5] + [self.second[6], self.second[5], self.second[7]])
        self.assertFalse(self.normalizer.status()['requiresPause'])

    def test_late_old_end_cannot_end_new_active_attempt(self):
        self.feed(self.first)
        self.feed(self.second[:2])
        active = self.state()['activeAttempt']
        self.normalizer.accept(self.first[6], 'old-end-duplicate')
        self.assertEqual(self.state()['activeAttempt'], active)
        self.assertFalse(self.normalizer.status()['requiresPause'])

    def test_legacy_recovery_preserves_raw_and_error_and_adds_receipt_once(self):
        self.legacy_failure()
        original_raw = self.payloads('raw')
        original_events = self.payloads('event')
        self.normalizer = NativeThinkingNormalizer(self.store, SID, expected_provider='deepseek-official')
        # Exact already-archived snapshot: no new carrier or synthetic replay.
        self.normalizer.accept(INITIAL, 'reconnect')
        self.assertFalse(self.normalizer.status()['requiresPause'])
        self.assertEqual(original_raw, self.payloads('raw'))
        repaired = self.payloads('event')
        self.assertEqual(repaired[:len(original_events)], original_events)
        receipt = json.loads(repaired[-1][0])
        self.assertEqual(receipt['code'], 'ATTEMPT_START_RECOVERED')
        self.assertEqual(receipt['verifiedEvidence'][0]['verifiedChunkCount'], 3)
        self.assertEqual(receipt['verifiedEvidence'][0]['startedAfterSeq'], 4)
        self.normalizer.accept(INITIAL, 'reconnect-again')
        self.assertEqual(self.payloads('event'), repaired)

    def test_real_missing_start_remains_paused(self):
        self.feed(self.first)
        self.feed([self.second[0], *self.second[2:]])
        self.normalizer.accept(INITIAL, 'reconnect')
        self.assertTrue(self.normalizer.status()['requiresPause'])
        self.assertTrue(self.state()['gaps'])

    def test_real_missing_chunk_remains_paused(self):
        self.feed(self.first)
        self.feed(self.second[:3] + self.second[4:])  # no chunk index 1
        self.normalizer.accept(INITIAL, 'reconnect')
        self.assertTrue(self.normalizer.status()['requiresPause'])
        self.assertTrue(self.state()['gaps'])

    def test_recovery_refuses_complete_state_without_actual_start_raw(self):
        self.check_missing_raw('start')

    def test_recovery_refuses_complete_state_without_actual_chunk_raw(self):
        self.check_missing_raw('chunk')

    def test_recovery_refuses_complete_state_without_actual_end_raw(self):
        self.check_missing_raw('end')

    def check_missing_raw(self, kind):
        self.legacy_failure()
        # Deliberately damage only this disposable fixture to prove the saved
        # counters/outcome alone cannot authorize recovery.
        for payload, in self.payloads('raw'):
            raw = json.loads(payload)
            value = raw['frame']
            frame = value.get('frame') or {}
            if value.get('type') == 'assistant-stream' and frame.get('type') == kind:
                if (kind == 'start' and frame.get('startedAfterSeq') == 4
                        or kind == 'chunk' and frame.get('time') == 402
                        or kind == 'end' and (frame.get('outcome') or {}).get('seq') == 5):
                    with contextlib.closing(sqlite3.connect(self.store.path)) as con:
                        con.execute('DELETE FROM records WHERE sid=? AND raw_key=?', (SID, raw['rawKey']))
                        con.commit()
                    break
        else:
            self.fail('fixture raw frame not found')
        self.normalizer.accept(INITIAL, 'reconnect')
        self.assertTrue(self.normalizer.status()['requiresPause'])
        self.assertIn('ATTEMPT_START_MISSING', self.state()['gaps'])

    def test_recovery_requires_contiguous_native_durable_events(self):
        self.legacy_failure()
        with contextlib.closing(sqlite3.connect(self.store.path)) as con:
            con.execute('DELETE FROM states WHERE sid=? AND key=?', (SID, 'native-event:4'))
            con.commit()
        self.normalizer.accept(INITIAL, 'reconnect')
        self.assertTrue(self.normalizer.status()['requiresPause'])

    def test_recovery_requires_actual_raw_durable_events_not_just_hash_state(self):
        self.legacy_failure()
        for payload, in self.payloads('raw'):
            raw = json.loads(payload)
            if raw['frame'].get('type') == 'event' and raw['frame']['event']['seq'] == 4:
                with contextlib.closing(sqlite3.connect(self.store.path)) as con:
                    con.execute('DELETE FROM records WHERE sid=? AND raw_key=?', (SID, raw['rawKey']))
                    con.commit()
                break
        else:
            self.fail('fixture event not found')
        self.normalizer.accept(INITIAL, 'reconnect')
        self.assertTrue(self.normalizer.status()['requiresPause'])

    def test_recovery_preserves_other_real_gap(self):
        self.legacy_failure()
        with self.store.transaction(SID) as tx:
            state = tx.get_state('normalizer')
            state['gaps']['another-attempt'] = 'STREAM_GAP'
            tx.set_state('normalizer', state)
        self.normalizer.accept(INITIAL, 'reconnect')
        self.assertEqual(self.state()['gaps'], {'another-attempt': 'STREAM_GAP'})
        self.assertTrue(self.normalizer.status()['requiresPause'])

    def test_recovery_receipt_failure_rolls_back_recovered_state(self):
        self.legacy_failure()
        original_raw, original_events = self.payloads('raw'), self.payloads('event')
        from thinking_store import _ThinkingTransaction
        emit = _ThinkingTransaction.emit
        def fail_recovery(tx, entry, dedupe_key=None):
            if entry.get('code') == 'ATTEMPT_START_RECOVERED':
                raise OSError('fixture persistence failure')
            return emit(tx, entry, dedupe_key)
        with patch.object(_ThinkingTransaction, 'emit', fail_recovery):
            with self.assertRaises(OSError):
                self.normalizer.accept(INITIAL, 'reconnect')
        self.assertIn('ATTEMPT_START_MISSING', self.state()['gaps'])
        self.assertEqual(self.payloads('raw'), original_raw)
        self.assertEqual(self.payloads('event'), original_events)


if __name__ == '__main__':
    unittest.main()
