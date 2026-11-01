"""Durability and idle-only checkpoint scheduling; no runtime/device access."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from thinking_collectors import SessionCollector
from thinking_store import ThinkingStore


class ThinkingCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=ThinkingStore(self.tmp.name,max_bytes=128*1024**2,journal_mode='WAL')

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def test_full_commit_is_visible_without_checkpoint_and_failure_rolls_back_batch(self):
        s=self.store
        with s._connection() as con:
            self.assertEqual(con.execute('PRAGMA synchronous').fetchone()[0],2)
            self.assertEqual(con.execute('PRAGMA wal_autocheckpoint').fetchone()[0],0)
        with s.transaction('s') as tx:
            tx.record_raw('original',{'complete':'raw'})
            tx.set_state('cursor',1);tx.emit({'kind':'status','text':'committed'})
        self.assertEqual(s.get_state('s','cursor'),1)
        self.assertEqual(s.page('s')['events'][0]['text'],'committed')
        with self.assertRaises(RuntimeError):
            with s.transaction('s') as tx:
                tx.record_raw('failed',{'complete':'failed'})
                tx.set_state('cursor',2);tx.emit({'kind':'status','text':'must rollback'})
                raise RuntimeError('injected post-write failure')
        self.assertEqual(s.get_state('s','cursor'),1)
        self.assertEqual(s.info('s')['rawCount'],1)
        self.assertEqual(s.info('s')['eventCount'],1)

    def test_passive_checkpoint_can_leave_reader_blocked_pages_then_finish_later(self):
        s=self.store
        reader=sqlite3.connect(s.path,isolation_level=None)
        try:
            reader.execute('BEGIN');reader.execute('SELECT COUNT(*) FROM sessions').fetchone()
            for index in range(8):
                with s.transaction('s') as tx:
                    tx.record_raw(str(index),{'text':'x'*(512*1024)})
            self.assertGreater(Path(str(s.path)+'-wal').stat().st_size,4*1024**2)
            with patch('thinking_store.time.monotonic',return_value=100):first=s.checkpoint_idle()
            self.assertIsNotNone(first)
            self.assertGreater(first['walFrames'],first['checkpointedFrames'])
            reader.commit();reader.close();reader=None
            with patch('thinking_store.time.monotonic',return_value=102):second=s.checkpoint_idle()
            self.assertEqual(second['walFrames'],second['checkpointedFrames'])
            self.assertEqual(s.info('s')['rawCount'],8)
            self.assertFalse(s.info('s')['paused'])
        finally:
            if reader is not None:reader.close()


class IdleCollectorCheckpointTests(unittest.TestCase):
    def manager(self):
        manager=SimpleNamespace(buffer_options={'batch_wait':0},lock=threading.RLock(),
            ingest_lock=threading.RLock(),collectors={},store=Mock(),backlog_limit_ms=5000)
        a=SessionCollector(manager,{'sessionId':'a','fingerprint':'a'})
        b=SessionCollector(manager,{'sessionId':'b','fingerprint':'b'})
        manager.collectors={'a':a,'b':b}
        return manager,a,b

    def test_other_session_and_inflight_frames_prevent_checkpoint_and_keep_five_second_age(self):
        manager,a,b=self.manager()
        a.buffer.put({'type':'native','value':{}},received_mono=time.monotonic()-5.1)
        batch=a.buffer.get_batch()
        self.assertEqual(a.buffer.snapshot()['inFlightFrames'],1)
        self.assertGreaterEqual(a.track_freshness()['backlogAgeMs'],5000)
        self.assertIsNone(a.checkpoint_if_idle());manager.store.checkpoint_idle.assert_not_called()
        a.buffer.committed(batch)
        b.buffer.put({'type':'native','value':{}})
        self.assertIsNone(a.checkpoint_if_idle());manager.store.checkpoint_idle.assert_not_called()
        b.buffer.committed(b.buffer.get_batch())
        a.checkpoint_if_idle();manager.store.checkpoint_idle.assert_called_once()

    def test_checkpoint_does_not_wait_for_another_writer(self):
        manager,a,_=self.manager();held=threading.Event();release=threading.Event()
        def writer():
            with manager.ingest_lock:held.set();release.wait(3)
        thread=threading.Thread(target=writer);thread.start();self.assertTrue(held.wait(2))
        try:
            self.assertIsNone(a.checkpoint_if_idle())
            manager.store.checkpoint_idle.assert_not_called()
        finally:release.set();thread.join()

    def test_checkpoint_error_propagates_without_acknowledging_or_changing_cache(self):
        manager,a,_=self.manager();marker={'archive':{'lastSeq':9},'native':{'code':'READY'}}
        a.cache=marker;manager.store.checkpoint_idle.side_effect=sqlite3.OperationalError('injected I/O error')
        with self.assertRaises(sqlite3.OperationalError):a.checkpoint_if_idle()
        self.assertIs(a.cache,marker)
        self.assertIsNone(a.last_ingest_time)
        self.assertIsNone(a.last_batch_metrics)


if __name__=='__main__':unittest.main(verbosity=2)
