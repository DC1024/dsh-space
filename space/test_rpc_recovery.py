import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import dsh_rpc
import worker
from test_reconcile import ReconciliationTests


class TransportRecoveryTests(unittest.TestCase):
    def test_read_recovers_after_transient_exit(self):
        bad=SimpleNamespace(returncode=1,stdout='',stderr='secret-not-logged')
        good=SimpleNamespace(returncode=0,stdout=json.dumps({'result':{'ok':True,'value':{'asOfSeq':9}}}))
        with patch.object(dsh_rpc.subprocess,'run',side_effect=[bad,good]) as run, patch.object(dsh_rpc,'_diagnostic') as log, patch.object(dsh_rpc.time,'sleep'):
            self.assertEqual(dsh_rpc.rpc('session/projections'),{'asOfSeq':9})
            self.assertEqual(run.call_count,2)
            self.assertNotIn('secret',str(log.call_args))

    def test_prompt_is_never_replayed(self):
        bad=SimpleNamespace(returncode=1,stdout='',stderr='secret')
        with patch.object(dsh_rpc.subprocess,'run',return_value=bad) as run, patch.object(dsh_rpc,'_diagnostic'):
            with self.assertRaises(RuntimeError):dsh_rpc.rpc('session/prompt')
            self.assertEqual(run.call_count,1)

    def test_application_error_is_not_retried(self):
        bad=SimpleNamespace(returncode=0,stdout=json.dumps({'result':{'ok':False,'error':{'code':'DENIED','message':'denied'}}}))
        with patch.object(dsh_rpc.subprocess,'run',return_value=bad) as run:
            with self.assertRaises(RuntimeError):dsh_rpc.rpc('session/page')
            self.assertEqual(run.call_count,1)


class SameProcessRecoveryTests(ReconciliationTests):
    def test_resume_bootstraps_bound_empty_session_but_rejects_disconnected_while_busy(self):
        from space import Rejected
        observer={'code':'MODEL_UNKNOWN','registered':True,'activeBinding':True,'transport':'connected',
            'expectedModel':'deepseek-flash','pendingModel':'deepseek-flash','pendingProvider':'deepseek-official',
            'freshness':{'pendingFrames':0,'backlogAgeMs':0}}
        with patch.object(self.store,'blockers',return_value=[]), patch.object(self.store,'check_storage'), patch.object(self.store,'observer_status',return_value=observer):
            with self.store.db:self.store.set('active_request',{'task_id':'busy-task'})
            self.store.command('resume',{})
            self.assertFalse(self.store.get('paused'))
            with self.store.db:self.store.set('paused',True)
            observer['transport']='disconnected'
            with self.assertRaises(Rejected):self.store.command('resume',{})
            self.assertTrue(self.store.get('paused'))

    def test_idle_resume_does_not_need_fresh_observation_but_blockers_still_apply(self):
        from space import Rejected
        observer={'canObserve':False,'requiresPause':True,'transport':'disconnected'}
        with patch.object(self.store,'check_storage'), patch.object(self.store,'observer_status',return_value=observer) as status:
            with self.store.db:self.store.set('active_request',{})
            with patch.object(self.store,'blockers',return_value=['当日费用达到停止线']):
                with self.assertRaises(Rejected):self.store.command('resume',{})
            self.assertTrue(self.store.get('paused'))
            with patch.object(self.store,'blockers',return_value=[]):
                self.store.command('resume',{})
            self.assertFalse(self.store.get('paused'))
            status.assert_not_called()

    def test_interrupted_before_user_message_matches_inbox_receipt(self):
        from reconcile import reconcile
        t=self.seed()
        events=[{'seq':5,'type':'agent/inbox/spliced','data':{'inserted':[{'source':{'rpcId':'space-task-'+t}}]}},
                {'seq':6,'type':'turn/start','data':{'turn':1}},
                {'seq':7,'type':'turn/end','data':{'turn':1,'reason':{'kind':'interrupted'}}}]
        def rpc(method,args):
            return {'asOfSeq':7} if method=='session/projections' else {'records':[{'event':e}for e in events]}
        self.assertEqual(reconcile(self.store,t,rpc)['status'],'needs-review')

    def test_empty_closed_dedicated_session_requires_creation_and_empty_inbox(self):
        from reconcile import reconcile
        from space import Rejected
        t=self.seed()
        def rpc(method,args):
            return {'asOfSeq':5,'values':{'inbox':{'next-turn':[],'next-step':[]}}} if method=='session/projections' else {'records':[{'event':{'seq':5,'type':'session/end-seed','data':{}}}]}
        with self.assertRaises(Rejected):reconcile(self.store,t,rpc)
        self.store.event('context.continued',{'new_task_id':t},t,'sid')
        self.assertEqual(reconcile(self.store,t,rpc)['reason']['kind'],'not-started')

    def test_autonomy_off_does_not_block_explicit_owner_tasks(self):
        self.store.config['autonomy_enabled']=False
        self.store.config['owner_hold']={'paused':False}
        self.assertFalse(any('主人要求' in x for x in self.store.blockers()))
        self.store.config['owner_hold']={'paused':True}
        self.assertTrue(any('主人要求' in x for x in self.store.blockers()))

    def test_same_process_finished_task_reconciles_without_replay(self):
        t=self.seed();worker._HEAL_ATTEMPT.clear()
        with patch.object(dsh_rpc,'rpc',side_effect=self.runtime(t)):
            self.assertTrue(worker.heal_cross_restart_uncertain(self.store))
        self.assertEqual(self.store.db.execute('SELECT status FROM tasks WHERE id=?',(t,)).fetchone()[0],'needs-review')

    def test_same_process_missing_receipt_keeps_block(self):
        t=self.seed();worker._HEAL_ATTEMPT.clear()
        with patch.object(dsh_rpc,'rpc',side_effect=self.runtime(t,tool_result=False)):
            self.assertFalse(worker.heal_cross_restart_uncertain(self.store))
        self.assertEqual(self.store.db.execute('SELECT status FROM tasks WHERE id=?',(t,)).fetchone()[0],'uncertain')


if __name__=='__main__':unittest.main()
