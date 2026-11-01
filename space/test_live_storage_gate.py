"""Quota rechecks and opt-in desktop observation; no real devices or services."""
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
import desktop
from device_lease import lease
from space import Rejected
from space_tools import GIB
import test_storage_expansion as fixtures

class EndPoll(BaseException):pass

class LiveStorageGateTests(unittest.TestCase):
    setUp=fixtures.StorageExpansionTests.setUp
    tearDown=fixtures.StorageExpansionTests.tearDown
    usage=fixtures.StorageExpansionTests.usage
    active=fixtures.StorageExpansionTests.active
    tool=fixtures.StorageExpansionTests.tool

    def normal_task(self,target='phone'):
        row=self.s.command('chat',{'text':'fixture owner task','request_key':'fixture','target':target})
        return self.active(row['id'],target)

    def test_device_lease_rejects_quota_reached_after_model_was_reserved(self):
        sid=self.normal_task();self.used=121*GIB
        with self.assertRaises(Rejected) as error:
            with lease(self.s,'phone'):self.fail('device action must not begin')
        self.assertEqual(error.exception.code,'STORAGE_LIMIT')
        self.assertEqual(self.s.device_inflight,{})
        self.assertTrue(self.s.get('paused'))

    def test_no_phone_or_desktop_provider_is_called_when_live_quota_is_full(self):
        self.normal_task('assistant');self.used=121*GIB;self.s.desktop_live={'ready':True}
        with patch('phone_tools.invoke') as phone,patch('desktop.call') as guest:
            self.assertTrue(self.tool('phone_click',{'x':0,'y':0})['isError'])
            self.assertTrue(self.tool('desktop_click',{'x':0,'y':0})['isError'])
            phone.assert_not_called();guest.assert_not_called()

    def test_ordinary_workspace_write_is_rejected_but_storage_proposal_remains_possible(self):
        self.normal_task();self.used=121*GIB
        with patch('space_tools.call') as writer:
            self.assertTrue(self.tool('workspace_write',{'path':'no.txt','text':'no'})['isError'])
            writer.assert_not_called()
        result=self.tool('storage_request_expansion',{'target_gib':140,'reason':'fixture full quota','request_key':'quota-appeal'})
        self.assertFalse(result.get('isError'));self.assertEqual(self.s.config['max_space_gb'],120)
        self.assertTrue(self.s.get('paused'))

    def configure(self,enabled):
        p=self.root/'config.json';cfg=json.loads(p.read_text('utf-8'));cfg['desktop']['auto_poll']=enabled
        # These cases explicitly test opt-in polling, independent of the live
        # installation's new on-demand default.
        cfg['desktop']['on_demand']=False
        p.write_text(json.dumps(cfg),'utf-8');(self.root/'data/guest-secrets.json').write_text('{}','utf-8')

    def poll_once(self,provider,quota=None):
        targets=[]
        def thread(**kwargs):targets.append(kwargs['target']);return SimpleNamespace(start=lambda:None)
        with patch.object(desktop.threading,'Thread',side_effect=thread),patch.object(desktop,'call',side_effect=provider) as calls,patch.object(desktop.time,'sleep',side_effect=EndPoll):
            desktop.start(self.s)
            if quota is None:
                with self.assertRaises(EndPoll):targets[0]()
            else:
                with patch.object(self.s,'check_storage',side_effect=quota):
                    with self.assertRaises(EndPoll):targets[0]()
            return calls

    def test_disabled_auto_poll_never_connects_to_vm_and_preserves_old_frame(self):
        self.configure(False);frame=self.s.data/'desktop.jpg';frame.write_bytes(b'old')
        calls=self.poll_once(lambda *a,**k:self.fail('no VM health/screen while disabled'))
        calls.assert_not_called();self.assertEqual(frame.read_bytes(),b'old')
        self.assertFalse(self.s.desktop_live['auto_poll'])

    def test_enabled_auto_poll_checks_quota_before_health(self):
        self.configure(True)
        calls=self.poll_once(lambda *a,**k:self.fail('quota full before VM call'),quota=Rejected('fixture quota full','STORAGE_LIMIT'))
        calls.assert_not_called();self.assertEqual(self.s.desktop_live['code'],'STORAGE_LIMIT')

    def test_mid_poll_disable_does_not_start_a_new_screen_call(self):
        self.configure(True)
        def health(*args,**kwargs):
            self.configure(False);return {'guestPolicyReady':True,'guestOnly':True,'user':'DSH'}
        calls=self.poll_once(health)
        self.assertEqual(calls.call_count,1);self.assertEqual(calls.call_args.args[1],'health')

    def test_quota_is_rechecked_again_before_frame_write(self):
        self.configure(True);frame=self.s.data/'desktop.jpg';frame.write_bytes(b'old')
        def provider(root,action):
            if action=='health':return {'guestPolicyReady':True,'guestOnly':True,'user':'DSH'}
            return {'image':base64.b64encode(b'\xff\xd8fixture').decode(),'width':1,'height':1,'capturedAt':'fixture'}
        calls=self.poll_once(provider,quota=[None,None,Rejected('fixture quota changed before write','STORAGE_LIMIT')])
        self.assertEqual(calls.call_count,2);self.assertEqual(frame.read_bytes(),b'old')
        self.assertFalse((self.s.data/'desktop-frame.tmp').exists())
        self.assertEqual(self.s.desktop_live['code'],'STORAGE_LIMIT')

if __name__=='__main__':unittest.main()
