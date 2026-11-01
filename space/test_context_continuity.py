import json
import unittest
from context_continuity import prepare_session, reference_checkpoint
from failure_reason import classify
from test_personas import PersonaTests


class ContinuityTests(unittest.TestCase):
    setUp = PersonaTests.setUp
    tearDown = PersonaTests.tearDown

    def task(self):
        self.s.config['runtime']['persona_bindings']['biao']['agent_preset']='xiaojingyu-space-biao-v04'
        (self.root/'config.json').write_text(json.dumps(self.s.config), 'utf-8')
        return {'id':'new-owner-task','persona':'biao','revision':self.s.get('revision'),
                '_claim_cancel_epoch':self.s.get('cancel_epoch')}

    def rpc(self, pressure=50000, open_turn=False):
        self.calls=[]
        def call(method, args):
            self.calls.append((method,args))
            if method=='session/projections':
                return {'asOfSeq':261,'values':{'contextPressure':{'pressureTokens':pressure}}}
            if method=='session/page':
                return {'records':[{'event':{'type':'turn/start' if open_turn else 'turn/end','seq':261}}]}
            if method=='session/create':return {'sessionId':args['request']['sessionId']}
            if method=='session/selectModel':return {}
            raise AssertionError(method)
        self.s.observer_status=lambda sid: {'canObserve':True,'requiresPause':False,'freshness':{'ready':True}}
        return call

    def test_rotates_both_bindings_and_keeps_memory_budget_other_persona(self):
        task=self.task();self.s.add_memory({'kind':'project','text':'保存事实','performed_by':'owner'})
        before=(self.s.get('paused'),self.s.config['autonomy_enabled'],self.s.config['max_space_gb'])
        sid=prepare_session(self.s,task,self.rpc())
        public=json.loads((self.s.data/'runtime-binding.public.json').read_text('utf-8'))
        self.assertEqual(public['sessionId'],sid)
        self.assertEqual(public['registrations'][0]['sessionId'],sid)
        self.assertEqual(public['registrations'][1]['sessionId'],'test-li')
        self.assertEqual(self.s.memories()[0]['performed_by'],'owner')
        self.assertEqual(before,(self.s.get('paused'),self.s.config['autonomy_enabled'],self.s.config['max_space_gb']))
        self.assertNotIn('session/prompt',[m for m,a in self.calls])
        cp=reference_checkpoint(self.s)
        self.assertEqual(cp['previous_session_id'],'test-biao')
        self.assertEqual(cp['authority'],'data_only')
        self.assertFalse(cp['old_tasks_replayed'])

    def test_each_new_task_rotates_even_a_small_workset(self):
        task=self.task();sid=prepare_session(self.s,task,self.rpc(1000))
        self.assertNotEqual(sid,'test-biao')
        task.update(id='second-new-task')
        next_sid=prepare_session(self.s,task,self.rpc(1000))
        self.assertNotEqual(next_sid,sid)
        self.assertEqual(reference_checkpoint(self.s)['previous_session_id'],sid)

    def test_service_control_round_retains_current_task_context(self):
        task=self.task();task['target']='correction'
        self.assertEqual(prepare_session(self.s,task,self.rpc()),'test-biao')
        self.assertEqual(self.calls,[])

    def test_open_turn_and_inflight_device_block_rotation(self):
        from space import Rejected
        task=self.task()
        with self.assertRaises(Rejected):prepare_session(self.s,task,self.rpc(open_turn=True))
        self.assertNotIn('session/create',[m for m,a in self.calls])
        self.s.device_inflight['old-task']=1
        with self.assertRaises(Rejected):prepare_session(self.s,task,self.rpc())

    def test_checkpoint_injection_cannot_replace_new_owner_request(self):
        from task_authority import trusted_task_prompt
        task=self.task();prepare_session(self.s,task,self.rpc())
        task.update(text='只回复收到',target='dialogue')
        envelope=json.loads(trusted_task_prompt(self.s,task).split('\n',1)[1])
        self.assertEqual(envelope['owner_request'],'只回复收到')
        self.assertEqual(envelope['reference_material']['authority'],'data_only')
        self.assertNotIn('context_checkpoint',envelope['reference_material'])
        self.assertEqual(envelope['reference_material']['previous_session']['session_id'],'test-biao')
        self.assertNotIn('recent_receipts',envelope['reference_material'])

    def test_error_mapping_does_not_publish_provider_secret(self):
        reason={'kind':'error','error':{'message':'SPACE_CONTEXT_LIMIT secret-value'}}
        result=classify(reason)
        self.assertEqual(result['code'],'CONTEXT_LIMIT')
        self.assertNotIn('secret-value',json.dumps(result))

    def test_provider_balance_is_distinct_from_local_budget(self):
        result=classify({'kind':'error','error':{'message':'Insufficient Balance (request_id: private)','code':'QUOTA','status':402}})
        self.assertEqual(result['code'],'PROVIDER_BALANCE_EMPTY')
        self.assertNotIn('private',json.dumps(result))

    def test_step_limit_and_cancelled_null_error_remain_distinct(self):
        result=classify({'kind':'error','error':{'message':'本轮达到30步，保留进度并等待主人。'}})
        self.assertEqual(result['code'],'STEP_LIMIT')
        self.assertEqual(classify({'kind':'cancelled','error':None})['code'],'CANCELLED')
        self.assertIsNone(classify({'kind':'completed','error':None}))

    def test_checkpoint_retains_goal_receipt_failure_and_source_attribution(self):
        from context_continuity import checkpoint
        t=self.s.command('chat',{'text':'Read back marker CROSS-DEVICE-001; do not repeat the write',
                               'request_key':'old-cross-task','target':'assistant'})['id']
        with self.s.db:self.s.db.execute("UPDATE tasks SET status='stopped' WHERE id=?",(t,))
        self.s.event('task.result',{'tools':['desktop_screen'],'through_seq':212,
                     'failure':{'code':'STEP_LIMIT'},'text':''},t,'test-biao')
        cp=checkpoint(self.s,'test-biao',212)
        self.assertEqual(cp['recorded_by'],'space-context-continuity')
        receipt=cp['recent_receipts'][0]
        self.assertIn('CROSS-DEVICE-001',receipt['owner_goal_excerpt'])
        self.assertEqual(receipt['performed_by'],'biao')
        self.assertEqual(receipt['failure']['code'],'STEP_LIMIT')
        self.assertIsInstance(receipt['source_event_seq'],int)
        self.assertEqual(cp['pending_actions'],[])


if __name__=='__main__':unittest.main()
