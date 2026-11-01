"""Offline effort decisions and immutable request evidence; no provider calls."""
import json
import unittest
from unittest.mock import patch
import test_memory_shared as memory_fixture
from space import Rejected
from reasoning_policy import resolve,tool,record_problem,record_request


class ReasoningPolicyTests(unittest.TestCase):
    setUpFixture=memory_fixture.SharedMemoryTests.setUp
    tearDown=memory_fixture.SharedMemoryTests.tearDown

    def setUp(self):
        self.setUpFixture()
        self.s.config.update(owner_hold={'paused':False},dispatch_enabled=True)
        self.s.observer_status=lambda sid=None:{'canObserve':True,'requiresPause':False,'freshness':{'ready':True}}
        self.activate('fixture-task-biao')

    def activate(self,tid,persona='biao'):
        with self.s.identity.bind(persona),self.s.db:
            self.s.set('paused',False)
            self.s.set('active_request',dict(task_id=tid,session_id='session-fixture-'+persona,
                persona=persona,cancel_epoch=self.s.get('cancel_epoch'),revision=self.s.get('revision'),
                target='assistant',autonomy=False))

    def decision(self,persona='biao',step=1):
        return resolve(self.s,{'session_id':'session-fixture-'+persona,'step':step})

    def test_new_task_defaults_low_and_starts_fresh_after_prior_max(self):
        self.assertEqual(self.decision()['level'],'low')
        tool(self.s,{'operation':'select','level':'max','reason':'合成复杂排错'})
        self.assertEqual(self.decision()['level'],'max')
        self.activate('fixture-new-task')
        self.assertEqual(self.decision()['level'],'low')

    def test_long_task_has_no_sixty_step_cap_in_selection_or_receipts(self):
        for step in (1,30,31,60,61,150,151,1001):
            value=self.decision(step=step)
            self.assertEqual(value['step'],step)
            record_request(self.s,{'session_id':value['session_id'],'task_id':value['task_id'],
                'step':step,'request_id':'fixture-step-'+str(step),'level':value['level']})
        for bad in (-1,'7',True,None):
            with self.assertRaises(Rejected):self.decision(step=bad)
        self.activate('fixture-next-task')
        self.assertEqual(self.decision(step=1)['step'],1)

    def test_repeat_stop_has_its_own_reason(self):
        from failure_reason import classify
        result=classify({'kind':'error','error':{'message':'REPEATED_ACTION_LIMIT: same action 30 times'}})
        self.assertEqual(result['code'],'REPEATED_ACTION_LIMIT')

    def test_autonomy_switch_does_not_restore_total_step_cap(self):
        from owner_settings import _apply_autonomy_limits
        cfg={'context':{'max_steps':60},'task_timeout_seconds':1800}
        for enabled in (True,False,True):
            _apply_autonomy_limits(cfg,enabled)
            self.assertEqual(cfg['context']['max_steps'],0)
        self.assertNotIn('task_timeout_seconds',cfg)

    def test_real_tool_errors_raise_next_request_without_running_or_retrying_tools(self):
        self.assertEqual(self.decision()['level'],'low')
        record_problem(self.s,'web_fetch')
        self.assertEqual(self.decision()['level'],'high')
        record_problem(self.s,'phone_click')
        self.assertEqual(self.decision()['level'],'max')
        record_problem(self.s,'phone_click')
        self.assertEqual(self.decision()['level'],'max')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)

    def test_mcp_runtime_error_escalates_but_permission_denial_does_not(self):
        with patch('space_tools.call',side_effect=RuntimeError('synthetic network failure')):
            result=self.s.mcp({'id':1,'method':'tools/call','params':{'name':'web_fetch',
                'arguments':{'url':'https://example.com'}}})['result']
        self.assertTrue(result.get('isError'))
        self.assertEqual(self.decision()['level'],'high')
        with patch('space_tools.call',side_effect=Rejected('permission denial')):
            result=self.s.mcp({'id':1,'method':'tools/call','params':{'name':'web_fetch',
                'arguments':{'url':'https://example.com'}}})['result']
        self.assertTrue(result.get('isError'))
        self.assertEqual(tool(self.s,{'operation':'status'})['problem_count'],1)

    def test_model_can_raise_and_lower_for_this_task_only_and_must_explain(self):
        self.decision()
        for level in ('high','max','low'):
            row=tool(self.s,{'operation':'select','level':level,'reason':'当前步骤需要'+level})
            self.assertEqual(row['level'],level)
            self.assertEqual(row['applies_to'],'next-request-only')
        for args in ({'operation':'select','level':'high'},
                     {'operation':'select','level':'off','reason':'QQ已退役'},
                     {'operation':'select','level':'high','reason':'   '}):
            with self.assertRaises(Rejected):tool(self.s,args)

    def test_later_change_does_not_rewrite_prepared_request_level(self):
        first=self.decision()
        body={'session_id':first['session_id'],'task_id':first['task_id'],'step':1,
              'request_id':'fixture-request-one','level':first['level']}
        record_request(self.s,body)
        tool(self.s,{'operation':'select','level':'max','reason':'后续复杂步骤'})
        self.assertEqual(first['level'],'low')
        self.assertEqual(self.decision(step=2)['level'],'max')
        observed=tool(self.s,{'operation':'status'})['requests'][0]
        self.assertEqual(observed['level'],'low')
        self.assertFalse(observed['provider_response_verified'])
        record_request(self.s,body)
        with self.assertRaises(Rejected):record_request(self.s,{**body,'level':'max'})

    def test_pause_stale_task_and_stale_epoch_remain_rejected(self):
        selected=self.decision()
        self.s.command('pause',{})
        with self.assertRaises(Rejected):self.decision()
        with self.assertRaises(Rejected):tool(self.s,{'operation':'select','level':'max','reason':'不能绕过暂停'})
        self.activate('fixture-replacement')
        with self.assertRaises(Rejected):record_request(self.s,{'session_id':selected['session_id'],
            'task_id':selected['task_id'],'step':1,'request_id':'fixture-stale','level':'low'})

    def test_observation_and_correction_limits_are_not_weakened(self):
        self.s.observer_status=lambda sid=None:{'canObserve':True,'requiresPause':True,'freshness':{'ready':False}}
        with self.assertRaises(Rejected):self.decision()
        self.s.observer_status=lambda sid=None:{'canObserve':True,'requiresPause':False,'freshness':{'ready':True}}
        with self.s.db:
            active=self.s.get('active_request');active['target']='correction';self.s.set('active_request',active)
        self.assertEqual(self.decision()['level'],'low')
        with self.assertRaises(Rejected):tool(self.s,{'operation':'select','level':'high','reason':'纠正不扩权'})

    def test_personas_keep_distinct_task_selections_while_sharing_memory(self):
        with self.s.db:self.s.set('parallel_enabled',True)
        self.activate('fixture-task-li','li')
        tool(self.s,{'operation':'select','level':'max','reason':'表的复杂步骤'})
        with self.s.identity.bind('li'):
            self.assertEqual(self.decision('li')['level'],'low')
            tool(self.s,{'operation':'select','level':'high','reason':'里的排错步骤'})
        self.assertEqual(self.decision()['level'],'max')

    def test_mcp_tool_result_records_selection_reason_and_rejects_undeclared_fields(self):
        def call(args):
            return self.s.mcp({'id':1,'method':'tools/call','params':{'name':'reasoning_level','arguments':args}})['result']
        result=call({'operation':'select','level':'high','reason':'需要核对两个矛盾条件'})
        self.assertFalse(result.get('isError',False))
        self.assertEqual(json.loads(result['content'][0]['text'])['reason'],'需要核对两个矛盾条件')
        self.assertTrue(call({'operation':'select','level':'high','reason':'不能指定其他任务','task_id':'other'}).get('isError'))


if __name__=='__main__':unittest.main(verbosity=2)
