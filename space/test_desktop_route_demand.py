import json
import unittest
from unittest.mock import patch
from test_personas import PersonaTests
from device_lease import device_lease_status
import desktop


class DesktopRouteDemandTests(unittest.TestCase):
    setUp_base=PersonaTests.setUp
    tearDown=PersonaTests.tearDown

    def setUp(self):
        self.setUp_base()
        self.s.config.update(dispatch_enabled=True,owner_hold={'paused':False})
        self.s.set('paused',False)
        self.s.db.execute('INSERT INTO tasks(id,request_key,text,status,time,revision,persona,target) VALUES(?,?,?,?,?,?,?,?)',
                          ('demand','demand','test','running','2026-10-22',0,'biao','assistant'))
        self.s.db.commit()
        self.s.set('active_request',{'task_id':'demand','session_id':'test-biao','persona':'biao',
                   'revision':self.s.get('revision'),'cancel_epoch':self.s.get('cancel_epoch'),'target':'assistant'})
        self.s.observer_status=lambda *a:{'canObserve':True,'requiresPause':False,'freshness':{'ready':True}}
        self.s.check_storage=lambda:None
        self.s.budget=lambda:{'shared_ledger_verified':True,'local_recorded_cny':.5,'held_cny':1,'stop_at_cny':19.5}
        self.s.desktop_live={'ready':False}

    def invoke(self):
        return self.s.mcp({'jsonrpc':'2.0','id':1,'method':'tools/call',
                          'params':{'name':'desktop_screen','arguments':{}}})['result']

    def prepared(self,store,*,allow_start=False):
        self.assertTrue(allow_start)
        state=device_lease_status(store)
        self.assertEqual((state['current_device'],state['owner']),('desktop','model'))
        store.desktop_live={'ready':True}

    def test_cold_desktop_is_prepared_under_lease_then_action_runs(self):
        with patch.object(desktop,'prepare',side_effect=self.prepared) as prepare,patch.object(desktop,'call',return_value={'ok':True,'image':'AA=='}) as call:
            self.assertFalse(self.invoke().get('isError',False))
            prepare.assert_called_once_with(self.s,allow_start=True)
            call.assert_called_once_with(self.s.root,'screen')
            self.assertFalse(self.invoke().get('isError',False))
            self.assertEqual(prepare.call_count,1)

    def test_scope_pause_observer_and_budget_block_before_prepare(self):
        original=dict(self.s.get('active_request'))
        changes=[lambda:self.s.set('active_request',{**original,'target':'phone'}),
                 lambda:self.s.set('paused',True),
                 lambda:setattr(self.s,'observer_status',lambda *a:{'canObserve':False,'requiresPause':True}),
                 lambda:self.s.set('budget_hold',True)]
        for change in changes:
            self.s.set('active_request',dict(original));self.s.set('paused',False);self.s.set('budget_hold',False)
            self.s.observer_status=lambda *a:{'canObserve':True,'requiresPause':False,'freshness':{'ready':True}}
            change()
            with patch.object(desktop,'prepare') as prepare,patch.object(desktop,'call') as call:
                self.assertTrue(self.invoke()['isError'])
                prepare.assert_not_called();call.assert_not_called()
            self.assertFalse(device_lease_status(self.s)['blocked_unknown'])

    def test_pause_during_preparation_blocks_requested_action(self):
        def prepare(store,**kwargs):
            self.prepared(store,**kwargs)
            store.set('paused',True);store.set('cancel_epoch',store.get('cancel_epoch')+1)
        with patch.object(desktop,'prepare',side_effect=prepare),patch.object(desktop,'call') as call:
            self.assertTrue(self.invoke()['isError'])
            call.assert_not_called()
        self.assertFalse(device_lease_status(self.s)['blocked_unknown'])

    def test_unknown_wake_effect_blocks_both_even_for_screen_request(self):
        with patch.object(desktop,'prepare',side_effect=desktop.DesktopError('wake receipt missing',True)),patch.object(desktop,'call') as call:
            self.assertTrue(self.invoke()['isError']);call.assert_not_called()
        self.assertTrue(device_lease_status(self.s)['blocked_unknown'])
        self.assertTrue(self.s.get('paused'))
        self.assertEqual(self.s.device_inflight,{})


if __name__=='__main__':unittest.main()
