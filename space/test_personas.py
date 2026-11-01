import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from space import ROOT, Store, Rejected


class PersonaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        cfg=json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))
        cfg.update(min_free_gb=0,autonomy_enabled=False,dispatch_enabled=False,owner_hold={'paused':True})
        cfg['runtime']['persona_bindings']={p:{'session_id':'test-'+p} for p in ('biao','li')}
        (self.root/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
        self.s=Store(self.root)

    def tearDown(self):
        self.s.db.close();self.tmp.cleanup()

    def test_shared_memory_is_readable_and_revisable_with_recorders_preserved(self):
        s=self.s
        first=s.add_memory({'kind':'experience','text':'表记录','performed_by':'owner'})
        s.command('persona',{'persona':'li'})
        self.assertEqual(s.memories()[0]['recorded_by'],'biao')
        revised=s.add_memory({'kind':'experience','text':'里补充记录','supersedes':first['id']})
        self.assertEqual((revised['recorded_by'],revised['performed_by']),('li','owner'))
        s.command('persona',{'persona':'biao'})
        self.assertEqual([m['text'] for m in s.memories()],['里补充记录'])
        self.assertEqual(s.memories(memory_id=first['id'])[0]['text'],'表记录')

    def test_mcp_route_binding_does_not_follow_owner_switch(self):
        s=self.s
        s.add_memory({'kind':'preference','text':'表记录'})
        s.command('persona',{'persona':'li'})
        s.add_memory({'kind':'preference','text':'里记录'})
        request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'memory_recall','arguments':{'query':'','recorded_by':'li'}}}
        with s.identity.bind('biao'):
            result=json.dumps(s.mcp(request),ensure_ascii=False)
        self.assertIn('里记录',result);self.assertNotIn('表记录',result)
        self.assertEqual(s.identity.active(),'li')

    def test_all_memory_is_shared_by_default_with_legacy_visibility_compatible(self):
        s=self.s
        shared=s.add_memory({'kind':'relationship','text':'主人明确共同约定','visibility':'shared'})
        s.command('persona',{'persona':'li'})
        self.assertEqual(s.memories()[0]['id'],shared['id'])
        s.add_memory({'kind':'preference','text':'共用笔记'},'assistant:test')
        revised=s.add_memory({'kind':'relationship','text':'补充共同约定','supersedes':shared['id']},'assistant:test')
        self.assertEqual(revised['recorded_by'],'li')
        with s.identity.bind('biao'):
            self.assertEqual(len(s.memories()),2)

    def test_shared_log_does_not_copy_private_conversation(self):
        s=self.s
        s.command('chat',{'text':'私有对话暗号','request_key':'same'})
        s.identity.log('test.fixture','离线测试：已完成截图',source_key='fixture-1')
        s.command('persona',{'persona':'li'})
        actions=s.identity.actions(other_only=True)
        self.assertEqual(len(actions),1)
        self.assertNotIn('私有对话暗号',json.dumps(actions,ensure_ascii=False))
        self.assertFalse(any(e['kind']=='owner.message' for e in s.events()))

    def test_terminal_result_requires_the_same_persona_and_names_the_assistant(self):
        s=self.s
        task=s.command('chat',{'text':'离线测试留言','request_key':'result-fixture'})
        s.event('task.result',{'text':'离线测试回复'},task_id=task['id'])
        result=s.task_result(task['id'])
        self.assertEqual(result['assistant_name'],self.s.config.get('assistant_name','小鲸鱼'))
        self.assertEqual(result['result']['text'],'离线测试回复')
        s.command('persona',{'persona':'li'})
        with self.assertRaises(Rejected):s.task_result(task['id'])

    def test_queued_messages_are_persona_bound_and_never_auto_replayed(self):
        s=self.s
        b=s.command('chat',{'text':'表任务','request_key':'same'})
        s.command('persona',{'persona':'li'})
        li=s.command('chat',{'text':'里任务','request_key':'same'})
        self.assertNotEqual(b['id'],li['id'])
        self.assertEqual(li['status'],'needs-review')
        self.assertTrue(s.get('paused'))
        self.assertEqual(s.status()['tasks'][0]['persona'],'li')

    def test_switch_does_not_resume_and_survives_restart(self):
        s=self.s
        s.command('persona',{'persona':'li'})
        with self.assertRaises(Rejected):s.command('resume',{})
        s.db.close();self.s=s=Store(self.root)
        self.assertEqual(s.identity.active(),'li');self.assertTrue(s.get('paused'))
        self.assertEqual(s.identity.runtime()['session_id'],'test-li')

    def test_corrections_are_private_and_ack_versions_are_independent(self):
        s=self.s
        s.command('correct',{'text':'表纠正'})
        self.assertEqual(s.get('revision'),1)
        s.command('persona',{'persona':'li'})
        self.assertEqual(s.get('correction'),'');self.assertEqual(s.get('revision'),0)
        s.command('correct',{'text':'里纠正'})
        s.command('persona',{'persona':'biao'})
        self.assertEqual(s.get('correction'),'表纠正')

    def test_busy_switch_and_stale_owner_form_are_rejected(self):
        s=self.s
        with s.db:s.set('active_request',{'task_id':'unfinished'})
        with self.assertRaises(Rejected):s.command('persona',{'persona':'li'})
        with s.db:s.set('active_request',{})
        s.command('persona',{'persona':'li'})
        with self.assertRaises(Rejected):s.command('chat',{'persona':'biao','text':'旧窗口','request_key':'x'})
        with self.assertRaises(Rejected):s.add_memory({'persona':'biao','kind':'preference','text':'旧窗口'})

    def test_missing_or_stale_freshness_fails_closed(self):
        base={'canObserve':True,'requiresPause':False}
        self.assertFalse(Store.observation_fresh(base))
        self.assertFalse(Store.observation_fresh(base|{'freshness':{'ready':False}}))
        self.assertTrue(Store.observation_fresh(base|{'freshness':{'ready':True}}))

    def test_hold_blocks_even_a_stale_ready_configuration(self):
        s=self.s
        s.config['runtime']['budget_source_ready']=True
        with self.assertRaises(Rejected):s.bridge_action('budget/reserve',{'id':'x','upper_cny':1,'consumer':'test'})
        with self.assertRaises(Rejected):s.bridge_action('gate',{'session_id':'test-biao'})

    def test_bound_violation_is_recorded_and_blocks_future_consumption(self):
        s=self.s
        s.authority.reserve('bound-fixture',.1,'test')
        result=s.bridge_action('budget/settle',{'id':'bound-fixture','actual_cny':.2})
        self.assertTrue(result['bound_violation']);self.assertTrue(s.get('budget_hold'))
        with self.assertRaises(Rejected) as denied:
            s.bridge_action('budget/reserve',{'id':'next','upper_cny':.1,'consumer':'test'})
        self.assertEqual(denied.exception.code,'BUDGET_BOUND_HOLD')
        self.assertTrue(s.bridge_action('budget/settle',{'id':'bound-fixture','actual_cny':.2})['bound_violation'])

    def test_model_bootstrap_never_authorizes_device_or_memory_actions(self):
        s=self.s
        fixture={'code':'MODEL_UNKNOWN','registered':True,'activeBinding':True,'transport':'connected',
                 'expectedModel':'deepseek-flash','pendingModel':'deepseek-flash','pendingProvider':'deepseek-official',
                 'canObserve':False,'requiresPause':True,'freshness':{'ready':False,'pendingFrames':0,'backlogAgeMs':0}}
        s.observer_status=lambda:fixture
        s.config['owner_hold']={'paused':False};s.config['dispatch_enabled']=True
        with s.lock,s.db:
            s.set('active_request',{'persona':'biao','session_id':'test-biao','revision':s.get('revision'),
                'cancel_epoch':s.get('cancel_epoch'),'task_id':'test','autonomy':False})
        self.assertTrue(s.bridge_action('gate',{'session_id':'test-biao','step':1,'phase':'model_step'})['ok'])
        with self.assertRaises(Rejected):s.bridge_action('gate',{'session_id':'test-biao','step':1})
        with self.assertRaises(Rejected):s.bridge_action('gate',{'session_id':'test-biao','step':2,'phase':'model_step'})
        fixture['freshness']['pendingFrames']=1
        with self.assertRaises(Rejected):s.bridge_action('gate',{'session_id':'test-biao','step':1,'phase':'model_step'})

    def test_autonomy_turn_may_bootstrap_first_model_step_but_not_devices(self):
        s=self.s
        fixture={'code':'MODEL_UNKNOWN','registered':True,'activeBinding':True,'transport':'connected',
                 'expectedModel':'deepseek-flash','pendingModel':'deepseek-flash','pendingProvider':'deepseek-official',
                 'canObserve':False,'requiresPause':True,'freshness':{'ready':False,'pendingFrames':0,'backlogAgeMs':0}}
        s.observer_status=lambda:fixture
        s.resources=lambda:{'known':True,'idle_seconds':10**6}
        s.config['owner_hold']={'paused':False};s.config['dispatch_enabled']=True;s.config['autonomy_enabled']=True
        with s.lock,s.db:
            s.set('paused',False)
            s.set('active_request',{'persona':'biao','session_id':'test-biao','revision':s.get('revision'),
                'cancel_epoch':s.get('cancel_epoch'),'task_id':'test','autonomy':True,'target':'assistant'})
        self.assertTrue(s.bridge_action('gate',{'session_id':'test-biao','step':1,'phase':'model_step'})['ok'])
        for body in ({'step':1,'phase':'device_step','device':'phone'},{'step':1,'phase':'memory_write'},
                     {'step':2,'phase':'model_step'}):
            with self.assertRaises(Rejected):s.bridge_action('gate',{'session_id':'test-biao',**body})
        fixture.update(registered=False)
        with self.assertRaises(Rejected):s.bridge_action('gate',{'session_id':'test-biao','step':1,'phase':'model_step'})

    def test_thread_local_model_scopes_do_not_leak(self):
        s=self.s
        for p in ('biao','li'):
            with s.identity.bind(p):s.add_memory({'kind':'experience','text':p})
        failures=[]
        def read(p):
            try:
                with s.identity.bind(p):
                    for _ in range(30):
                        if s.identity.actor()!=p:failures.append(p)
                        if [m['text'] for m in s.memories(recorded_by=p)]!=[p]:failures.append(p)
            except Exception as e:failures.append(str(e))
        def switch():
            try:
                for _ in range(30):
                    for p in ('li','biao'):
                        with s.lock,s.db:s.set('active_persona',p)
                        s.identity.active()
            except Exception as e:failures.append(str(e))
        threads=[threading.Thread(target=read,args=(p,)) for p in ('biao','li')]
        threads.append(threading.Thread(target=switch))
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(failures,[])


if __name__=='__main__':unittest.main(verbosity=2)
