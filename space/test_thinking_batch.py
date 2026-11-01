"""Burst throughput must not publish unfinished writes or lose original frames."""
import contextlib, json, sqlite3, tempfile, unittest
from unittest.mock import patch
from thinking_queue import PendingFrames
from thinking_native import NativeThinkingNormalizer
from thinking_store import ThinkingStore, _ThinkingTransaction

class ThinkingBatchTests(unittest.TestCase):
    def test_inflight_burst_stays_stale_until_complete_commit(self):
        now=[0.0]; q=PendingFrames(clock=lambda:now[0],batch_wait=0)
        for i in range(128):q.put({'type':'native','value':{'i':i}},size=10,received_mono=0)
        batch=q.get_batch();self.assertEqual(len(batch),128)
        now[0]=5.1;s=q.snapshot()
        self.assertEqual(s['pendingFrames'],128);self.assertEqual(s['inFlightFrames'],128)
        self.assertGreaterEqual(s['backlogAgeMs'],5000)
        q.committed(batch);self.assertEqual(q.snapshot()['pendingFrames'],0)

    def test_byte_limit_and_order_remain_bounded(self):
        q=PendingFrames(batch_bytes=100,batch_wait=0)
        for i in range(10):q.put({'type':'native','value':{'i':i}},size=30)
        batch=q.get_batch();self.assertEqual([v['value']['i'] for v in batch],[0,1,2])
        self.assertEqual(q.snapshot()['pendingFrames'],10)

    def test_large_batch_is_atomic_on_last_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            s=ThinkingStore(directory,journal_mode='WAL');self.addCleanup(s.close)
            n=NativeThinkingNormalizer(s,'session-fixture')
            items=[{'value':{'type':'event','event':{'seq':i,'type':'fixture','data':{'n':i}}}} for i in range(128)]
            original=_ThinkingTransaction.record_raw;count=[0]
            def fail(tx,*a,**kw):
                count[0]+=1
                if count[0]==128:raise OSError('simulated final raw failure')
                return original(tx,*a,**kw)
            try:
                with patch.object(_ThinkingTransaction,'record_raw',fail):
                    with self.assertRaises(OSError):n.accept_many(items)
                self.assertEqual(s.info('session-fixture')['rawCount'],0)
                self.assertEqual(s.info('session-fixture')['eventCount'],0)
                n=NativeThinkingNormalizer(s,'session-fixture');n.accept_many(items)
                with contextlib.closing(sqlite3.connect(s.path)) as c:
                    rows=[json.loads(r[0])['frame'] for r in c.execute("SELECT payload FROM records WHERE kind='raw' ORDER BY seq")]
                self.assertEqual(rows,[i['value'] for i in items])
                with self.assertRaises(ValueError):n.accept_many(items+[items[0]])
            finally:s.close()

if __name__=='__main__':unittest.main()
