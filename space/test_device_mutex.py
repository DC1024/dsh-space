import json
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from test_personas import PersonaTests
from device_lease import lease, background_lease, device_lease_status


class DeviceMutexTests(unittest.TestCase):
    setUp_base=PersonaTests.setUp
    tearDown=PersonaTests.tearDown

    def setUp(self):
        self.setUp_base()
        self.s.set('paused',False)
        self.errors=[];self.entered=[]
        self.s.bridge_action=lambda *args,**kwargs: {'ok':True}
        for persona in ('biao','li'):
            tid='task-'+persona
            self.s.db.execute('INSERT INTO tasks(id,request_key,text,status,time,revision,persona,target) VALUES(?,?,?,?,?,?,?,?)',
                              (tid,tid,'test','running','2026-10-22',0,persona,'assistant'))
            with self.s.identity.bind(persona):
                self.s.set('active_request',{'task_id':tid,'session_id':'test-'+persona,'revision':0,'cancel_epoch':self.s.get('cancel_epoch')})
        self.s.db.commit()

    def run_thread(self,persona,device,body,action=None):
        def execute():
            try:
                with self.s.identity.bind(persona),lease(self.s,device,action or device+'_screen'):
                    self.entered.append(device)
                    body()
            except BaseException as error:self.errors.append(error)
        thread=threading.Thread(target=execute,daemon=True);thread.start();return thread

    def wait_waiter(self):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if device_lease_status(self.s)['waiting_count']:return
            time.sleep(.005)
        self.fail('Second device never reached the shared lease queue')

    def competition(self,change=None,unknown=False):
        begun=threading.Event();release=threading.Event()
        def first():
            begun.set();self.assertTrue(release.wait(3))
            if unknown:raise TimeoutError('receipt missing')
        a=self.run_thread('biao','phone',first,'phone_type' if unknown else 'phone_screen')
        self.assertTrue(begun.wait(2))
        b=self.run_thread('li','desktop',lambda:None)
        try:
            self.wait_waiter()
            state=device_lease_status(self.s)
            self.assertEqual(state['current_device'],'phone')
            self.assertEqual(state['waiting_devices'],['desktop'])
            self.assertEqual(self.entered,['phone'])
            if change:change()
        finally:
            release.set();a.join(3);b.join(3)
        self.assertFalse(a.is_alive() or b.is_alive())

    def test_real_competing_threads_serialize_phone_and_desktop(self):
        self.competition()
        self.assertEqual(self.entered,['phone','desktop'])
        self.assertEqual(self.errors,[])
        self.assertEqual(device_lease_status(self.s)['phase'],'idle')
        self.assertEqual(self.s.device_inflight,{})
        rows=[json.loads(r[0]) for r in self.s.db.execute("SELECT payload FROM events WHERE kind='device.stage' ORDER BY seq")]
        self.assertEqual([(r['device'],r['phase']) for r in rows],
                         [('phone','started'),('phone','receipt'),('desktop','started'),('desktop','receipt')])

    def test_pause_invalidates_waiting_device(self):
        def pause():
            with self.s.lock,self.s.db:
                self.s.set('paused',True);self.s.set('cancel_epoch',self.s.get('cancel_epoch')+1)
        self.competition(change=pause)
        self.assertEqual(self.entered,['phone'])
        self.assertEqual(self.errors[0].code,'DEVICE_LEASE_EXPIRED')

    def test_session_change_invalidates_waiting_device(self):
        self.competition(change=lambda:self.s.config['runtime']['persona_bindings']['li'].update(session_id='new-session'))
        self.assertEqual(self.entered,['phone'])
        self.assertEqual(self.errors[0].code,'DEVICE_LEASE_EXPIRED')

    def test_unknown_effect_releases_lock_but_blocks_both_devices(self):
        self.competition(unknown=True)
        self.assertEqual(self.entered,['phone'])
        self.assertTrue(device_lease_status(self.s)['blocked_unknown'])
        self.assertEqual(self.s.db.execute("SELECT status FROM tasks WHERE id='task-biao'").fetchone()[0],'uncertain')
        # Even a cleared pause alone does not release the unknown-effect gate.
        self.s.set('paused',False)
        for persona,device in [('biao','phone'),('li','desktop')]:
            with self.s.identity.bind(persona):
                active=self.s.get('active_request');active['cancel_epoch']=self.s.get('cancel_epoch');self.s.set('active_request',active)
                with self.assertRaises(Exception) as caught:
                    with lease(self.s,device,device+'_screen'):self.fail('Unknown effects must block admission')
                self.assertEqual(caught.exception.code,'DEVICE_RESULT_UNCERTAIN')
        with background_lease(self.s,'desktop') as admitted:self.assertFalse(admitted)
        self.assertEqual(self.s.device_inflight,{})

    def test_background_skips_queued_model_and_yields_at_receipt(self):
        begun=threading.Event();release=threading.Event()
        with background_lease(self.s,'desktop') as admitted:
            self.assertTrue(admitted)
            t=self.run_thread('biao','phone',lambda:(begun.set(),release.wait(3)))
            self.wait_waiter()
            with background_lease(self.s,'desktop') as second:self.assertFalse(second)
            self.assertFalse(begun.is_set())
        try:
            self.assertTrue(begun.wait(2))
            with background_lease(self.s,'desktop') as admitted:self.assertFalse(admitted)
        finally:
            release.set();t.join(3)
        self.assertEqual(self.errors,[])
        with background_lease(self.s,'desktop') as admitted:self.assertTrue(admitted)

    def test_known_rejection_releases_without_inventing_unknown_side_effects(self):
        class RejectedBeforeSend(RuntimeError):result={'uncertain':False}
        t=self.run_thread('biao','phone',lambda:(_ for _ in ()).throw(RejectedBeforeSend()),'phone_click')
        t.join(3)
        self.assertEqual(len(self.errors),1)
        self.assertFalse(device_lease_status(self.s)['blocked_unknown'])
        t=self.run_thread('li','desktop',lambda:None);t.join(3)
        self.assertEqual(self.entered,['phone','desktop'])

    def test_phone_poll_skips_busy_device_and_preserves_last_health(self):
        from phone_tools import poll_once
        previous={'ready':True,'at':'previous-receipt'}
        self.s.phone_live=previous
        with patch('phone.health',return_value={'ready':True,'at':'new-receipt'}) as health:
            with self.s.identity.bind('biao'),lease(self.s,'desktop','desktop_screen'):
                self.assertFalse(poll_once(self.s))
                health.assert_not_called()
                self.assertIs(self.s.phone_live,previous)
            self.assertTrue(poll_once(self.s))
            health.assert_called_once_with(self.s.root)
            self.assertEqual(self.s.phone_live['at'],'new-receipt')

    def test_phone_poll_holds_lock_until_health_receipt(self):
        from phone_tools import poll_once
        begun=threading.Event();release=threading.Event();entered=threading.Event()
        def health(_):
            begun.set()
            if not release.wait(3):raise TimeoutError('test health timed out')
            return {'ready':True}
        with patch('phone.health',side_effect=health):
            poll=threading.Thread(target=lambda:poll_once(self.s),daemon=True);poll.start()
            self.assertTrue(begun.wait(2))
            model=self.run_thread('biao','desktop',entered.set)
            try:
                self.wait_waiter()
                self.assertFalse(entered.is_set())
                self.assertEqual(device_lease_status(self.s)['current_device'],'phone')
            finally:
                release.set();poll.join(3);model.join(3)
            self.assertTrue(entered.is_set())
            self.assertFalse(poll.is_alive() or model.is_alive())
            self.assertEqual(self.errors,[])

    def test_phone_poll_failure_releases_without_unknown_write(self):
        from phone_tools import poll_once
        with patch('phone.health',side_effect=TimeoutError('health read timeout')):
            self.assertTrue(poll_once(self.s))
        self.assertFalse(self.s.phone_live['ready'])
        self.assertFalse(device_lease_status(self.s)['blocked_unknown'])
        self.assertEqual(device_lease_status(self.s)['phase'],'idle')
        with background_lease(self.s,'desktop') as admitted:self.assertTrue(admitted)


if __name__=='__main__':unittest.main()
