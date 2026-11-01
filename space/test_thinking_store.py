"""Offline archive regression checks: full frames, atomicity and capacity pause."""
import contextlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from thinking_store import ThinkingStore, ThinkingCapacityError


class ThinkingArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ThinkingStore(self.root, max_bytes=1024 * 1024, journal_mode='WAL')
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def rows(self, kind=None):
        with contextlib.closing(sqlite3.connect(self.store.path)) as con:
            return [json.loads(row[0]) for row in con.execute(
                'SELECT payload FROM records WHERE (? IS NULL OR kind=?) ORDER BY record_id', (kind, kind))]

    def test_reader_sees_raw_state_and_events_only_after_commit(self):
        with self.store.transaction('s') as tx:
            tx.record_raw('r', {'native': ['unchanged', '\uD800', '完整原文' * 1000]})
            tx.set_state('s', {'cursor': 1})
            event = tx.emit({'kind': 'reply.delta', 'text': '真实原文'})
            self.assertEqual(self.rows(), [])
            self.assertIsNone(self.store.get_state('s', 's'))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.store.get_state('s', 's'), {'cursor': 1})
        self.assertEqual(self.store.page('s')['events'], [event])
        self.assertEqual(self.rows('raw')[0]['frame']['native'][1], '\uD800')

    def test_failure_rolls_back_all_rows_state_and_sequence(self):
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            with self.store.transaction('s') as tx:
                tx.record_raw('r', {'frame': 1})
                tx.set_state('cursor', 1)
                tx.emit({'kind': 'reply.delta', 'text': 'not published'})
                raise RuntimeError('failure before commit')
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.store.get_state('s', 'cursor'))
        with self.store.transaction('s') as tx:
            tx.record_raw('r', {'frame': 1})
            event = tx.emit({'kind': 'reply.delta', 'text': 'committed'})
        self.assertEqual(event['seq'], 1)
        self.assertEqual(self.rows('raw')[0]['rawSeq'], 1)

    def test_cumulative_allowance_refreshes_without_false_pause(self):
        self.store.max_bytes = 512 * 1024
        with self.store.transaction('s') as tx:
            for i in range(100):
                tx.record_raw(str(i), {'frame': i, 'text': '保留完整内容'})
                tx.emit({'kind': 'reply.delta', 'text': str(i)})
        info = self.store.info('s')
        self.assertFalse(info['paused'])
        self.assertEqual(info['rawCount'], 100)
        self.assertEqual(info['eventCount'], 100)
        self.assertLess(info['bytesUsed'], info['maxBytes'] - info['reserveBytes'])

    def test_large_payload_preserves_prior_history_and_sticky_pause(self):
        with self.store.transaction('s') as tx:
            tx.record_raw('first', {'full': 'original'})
            tx.set_state('cursor', 1)
            tx.emit({'kind': 'reply.delta', 'text': 'original'})
        self.store.max_bytes = 256 * 1024
        with self.assertRaises(ThinkingCapacityError):
            with self.store.transaction('s') as tx:
                tx.record_raw('second', {'full': '原' * 100_000})
                tx.set_state('cursor', 2)
        self.assertEqual([r['rawKey'] for r in self.rows('raw')], ['first'])
        self.assertEqual(self.store.get_state('s', 'cursor'), 1)
        self.assertEqual(self.rows('event')[-1]['code'], 'ARCHIVE_CAPACITY_REACHED')
        self.assertTrue(self.store.info('s')['paused'])
        with self.assertRaises(ThinkingCapacityError):
            with self.store.transaction('other'):
                self.fail('capacity pause must remain sticky for every session')
        self.assertEqual(len(self.rows('event')), 2)

    def test_growth_elsewhere_in_directory_is_checked_before_commit(self):
        self.store.max_bytes = 512 * 1024
        with self.assertRaises(ThinkingCapacityError):
            with self.store.transaction('s') as tx:
                tx.record_raw('r', {'native': 'must roll back'})
                tx.set_state('cursor', 5)
                tx.emit({'kind': 'reply.delta', 'text': 'must roll back'})
                subdir = self.root / 'nested'
                subdir.mkdir()
                (subdir / 'unrelated.dat').write_bytes(b'x' * self.store.max_bytes)
        self.assertEqual(self.rows('raw'), [])
        self.assertIsNone(self.store.get_state('s', 'cursor'))
        self.assertEqual([r.get('code') for r in self.rows('event')], ['ARCHIVE_CAPACITY_REACHED'])

    def test_duplicate_raw_and_events_do_not_advance_sequences(self):
        with self.store.transaction('s') as tx:
            self.assertTrue(tx.record_raw('r', {'native': 1}))
            tx.emit({'kind': 'reply.delta', 'text': 'one'}, 'event-one')
        with self.store.transaction('s') as tx:
            self.assertFalse(tx.record_raw('r', {'native': 1}))
            self.assertIsNone(tx.emit({'kind': 'reply.delta', 'text': 'one'}, 'event-one'))
        with self.assertRaisesRegex(ValueError, 'different native data'):
            with self.store.transaction('s') as tx:
                tx.record_raw('r', {'native': 2})
        self.assertEqual(self.store.info('s')['lastRawSeq'], 1)
        self.assertEqual(self.store.info('s')['lastSeq'], 1)

    def test_quota_budget_is_not_shared_between_transactions_or_sessions(self):
        with self.store.transaction('s') as tx:
            tx.record_raw('same-key', {'session': 's'})
        with self.store.transaction('other') as tx:
            tx.record_raw('same-key', {'session': 'other'})
        # An unrelated file added between transactions must be counted afresh.
        self.store.max_bytes = 512 * 1024
        (self.root / 'other.dat').write_bytes(b'x' * self.store.max_bytes)
        with self.assertRaises(ThinkingCapacityError):
            with self.store.transaction('s') as tx:
                tx.record_raw('blocked', {'native': 3})
        self.assertEqual(len(self.rows('raw')), 2)
        self.assertEqual(self.store.info('s')['lastRawSeq'], 1)
        self.assertEqual(self.store.info('other')['lastRawSeq'], 1)

    def test_sqlite_full_rolls_back_ingest_and_commits_pause(self):
        error = sqlite3.OperationalError('database or disk is full')
        error.sqlite_errorcode = sqlite3.SQLITE_FULL
        with self.assertRaises(ThinkingCapacityError):
            with self.store.transaction('s') as tx:
                tx.record_raw('r', {'native': 1})
                raise error
        self.assertEqual(self.rows('raw'), [])
        self.assertEqual(self.rows('event')[-1]['code'], 'ARCHIVE_CAPACITY_REACHED')

    def test_process_exit_after_commit_retains_full_wal_transaction(self):
        child_root = self.root / 'child'
        script = '''import os,sys
from thinking_store import ThinkingStore
s=ThinkingStore(sys.argv[1],journal_mode='WAL')
with s.transaction('s') as tx:
    tx.record_raw('r', {'native':'原文'*1000})
    tx.set_state('cursor', {'seq':1})
    tx.emit({'kind':'reply.delta','text':'committed'})
os._exit(0)
'''
        subprocess.run([sys.executable, '-c', script, str(child_root)],
                       cwd=Path(__file__).parent, check=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        with contextlib.closing(sqlite3.connect((child_root / 'thinking.sqlite3').as_uri() + '?mode=ro', uri=True)) as con:
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM records').fetchone()[0], 2)
            frame = json.loads(con.execute("SELECT payload FROM records WHERE kind='raw'").fetchone()[0])
            self.assertEqual(frame['frame']['native'], '原文' * 1000)
            self.assertEqual(json.loads(con.execute('SELECT payload FROM states').fetchone()[0]), {'seq': 1})

    def test_default_rollback_journal_has_same_atomic_capacity_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ThinkingStore(directory, max_bytes=512 * 1024)
            try:
                with store.transaction('s') as tx:
                    for i in range(32):
                        tx.record_raw(str(i), {'native': '原文' * 10, 'i': i})
                        tx.emit({'kind': 'reply.delta', 'text': str(i)})
                self.assertEqual(store.info('s')['rawCount'], 32)
                with self.assertRaises(ThinkingCapacityError):
                    with store.transaction('s') as tx:
                        tx.record_raw('large', {'native': '原文' * 100_000})
                info = store.info('s')
                self.assertEqual(info['rawCount'], 32)
                self.assertEqual(info['eventCount'], 33)
                self.assertTrue(info['paused'])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
