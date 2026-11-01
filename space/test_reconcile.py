import json,unittest
from test_space import SpaceTests
from reconcile import reconcile
from space import Rejected

class ReconciliationTests(unittest.TestCase):
    setUp=SpaceTests.setUp
    tearDown=SpaceTests.tearDown
    def seed(self):
        s=self.store;t=s.command('chat',{'text':'test','request_key':'receipt-test','target':'phone'})['id']
        with s.db:s.db.execute("UPDATE tasks SET status='uncertain' WHERE id=?",(t,))
        s.event('task.dispatch',{'start_seq':4,'request_id':'space-task-'+t,'run_id':s.run_id},t,'sid')
        return t
    def runtime(self,t,ended=True,tool_result=True):
        events=[{'seq':5,'type':'turn/start','data':{'turn':2}},
                {'seq':6,'type':'user/message','data':{'source':{'rpcId':'space-task-'+t}}},
                {'seq':7,'type':'tool/call','data':{'turn':2,'callId':'c'}}]
        if tool_result:events.append({'seq':8,'type':'tool/result','data':{'turn':2,'message':{'toolCallId':'c','content':[]}}})
        if ended:events.append({'seq':9,'type':'turn/end','data':{'turn':2,'reason':{'kind':'cancelled'}}})
        return lambda method,args: {'asOfSeq':9} if method=='session/projections' else {'records':[{'event':e} for e in events]}
    def test_end_receipt_releases_without_replay_or_resume(self):
        t=self.seed();r=reconcile(self.store,t,self.runtime(t))
        self.assertEqual(r['status'],'needs-review');self.assertFalse(r['resent']);self.assertTrue(self.store.get('paused'))
    def test_missing_end_or_tool_receipt_stays_uncertain(self):
        t=self.seed()
        for fn in (self.runtime(t,ended=False),self.runtime(t,tool_result=False),self.runtime('wrong-task')):
            with self.assertRaises(Rejected):reconcile(self.store,t,fn)
        self.assertEqual(self.store.db.execute('SELECT status FROM tasks WHERE id=?',(t,)).fetchone()[0],'uncertain')
    def test_inflight_and_previous_process_never_unlock(self):
        t=self.seed();self.store.device_inflight[t]=1
        with self.assertRaises(Rejected):reconcile(self.store,t,self.runtime(t))
        self.store.device_inflight.clear();self.store.run_id='new-process'
        with self.assertRaises(Rejected):reconcile(self.store,t,self.runtime(t))

if __name__=='__main__':unittest.main()
