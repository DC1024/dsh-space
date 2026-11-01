import concurrent.futures
import json
import time
import unittest
import test_personas
from acceptance import AcceptanceDenied
from space import Rejected


class AcceptanceParallelTests(unittest.TestCase):
    setUp = test_personas.PersonaTests.setUp
    tearDown = test_personas.PersonaTests.tearDown
    def authorize(self, consumer='phone:session-test', **changes):
        item={'session_id':'session-test','max_requests':2,'max_upper_cny':0.2,'max_total_cny':0.3}
        item.update(changes)
        doc={'enabled':True,'created_at':time.time(),'expires_at':time.time()+60,'id':'test-grant','consumers':{consumer:item}}
        (self.s.data/'acceptance-grants-v03.json').write_text(json.dumps(doc))
        return doc

    def test_finite_grant_does_not_open_dispatch_and_caps_concurrent_cost(self):
        s=self.s;self.authorize()
        def reserve(i):
            try:
                return s.bridge_action('budget/reserve',{'id':'r'+str(i),'consumer':'phone:session-test','upper_cny':0.2})
            except AcceptanceDenied:
                return None
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results=list(pool.map(reserve,range(4)))
        self.assertEqual(sum(x is not None for x in results),1)
        self.assertTrue(s.config['owner_hold']['paused'])
        self.assertFalse(s.config['dispatch_enabled'])
        with self.assertRaises(Rejected):
            s.bridge_action('budget/reserve',{'id':'other','consumer':'unlisted','upper_cny':0.01})

    def test_expired_and_cancelled_grant_are_closed(self):
        doc=self.authorize();doc['enabled']=False
        (self.s.data/'acceptance-grants-v03.json').write_text(json.dumps(doc))
        with self.assertRaises(Rejected):
            self.s.bridge_action('budget/reserve',{'id':'a','consumer':'phone:session-test','upper_cny':0.1})

    def test_parallel_confirmation_is_required_and_one_use(self):
        s=self.s
        with self.assertRaises(Rejected):s.command('parallel',{'enabled':True,'nonce':'fake'})
        c=s.command('parallel-prepare',{'enabled':True})
        s.command('parallel',{'enabled':True,'nonce':c['nonce']})
        self.assertTrue(s.get('parallel_enabled'))
        self.assertTrue(s.get('paused'));self.assertFalse(s.config['autonomy_enabled'])
        with self.assertRaises(Rejected):s.command('parallel',{'enabled':True,'nonce':c['nonce']})

    def test_parallel_view_switch_keeps_separate_active_requests(self):
        s=self.s;c=s.command('parallel-prepare',{'enabled':True});s.command('parallel',{'enabled':True,'nonce':c['nonce']})
        with s.db:s.set('active_request',{'task_id':'biao-task'})
        epoch=s.get('cancel_epoch')
        s.command('persona',{'persona':'li'})
        self.assertEqual(s.get('active_request'),{})
        with s.db:s.set('active_request',{'task_id':'li-task'})
        self.assertEqual(s.get('cancel_epoch'),epoch)
        with s.identity.bind('biao'):self.assertEqual(s.get('active_request')['task_id'],'biao-task')
        self.assertEqual(s.get('active_request')['task_id'],'li-task')

    def test_dialogue_tasks_can_claim_together_but_device_tasks_do_not(self):
        from worker import claim_task
        s=self.s
        for target,expected in (('dialogue',2),('assistant',2),('desktop',1),('phone',1)):
            with s.db:s.db.execute('DELETE FROM tasks')
            for p in ('biao','li'):
                s.command('persona',{'persona':p})
                s.command('chat',{'text':'isolated test','request_key':target+p,'target':target})
            with s.db:
                s.db.execute("UPDATE tasks SET status='queued'")
                s.set('paused',False)
            def claim(p):
                with s.identity.bind(p):return claim_task(s)
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                claimed=list(pool.map(claim,('biao','li')))
            self.assertEqual(sum(t is not None for t in claimed),expected,target)
