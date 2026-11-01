"""Owner-on-emulator attribution; no emulator, window or provider calls."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import phone
import phone_tools


class FakeIdentity:
    def __init__(self):self.persona='biao';self.logs=[]
    def actor(self):return self.persona
    def runtime(self):return {'session_id':'session-test'}
    def log(self,*args):self.logs.append(args)


class FakeStore:
    def __init__(self,root):
        self.root=Path(root);self.data=self.root/'data';self.data.mkdir();self.identity=FakeIdentity();self.events=[]
    def get(self,key):return {'task_id':'task-test'} if key=='active_request' else None
    def event(self,kind,payload,*rest):self.events.append((kind,payload))


class Clock:
    """Windows monotonic ticks are ~15 ms; tests advance time explicitly."""
    def __init__(self):self.now=1000.0
    def __call__(self):self.now+=0.5;return self.now


class OwnerActivityTests(unittest.TestCase):
    def setUp(self):
        phone_tools.OWNER.update(last=None,samples=0,watching=True);phone_tools.LAST_ACTION.clear()
        self.clock=Clock();self.patch_clock=patch.object(phone_tools.time,'monotonic',self.clock);self.patch_clock.start()

    def tearDown(self):
        self.patch_clock.stop()

    def test_only_new_input_with_emulator_in_front_counts_as_owner(self):
        cases=[(101,'Android Emulator - DSH_Phone_CN:5562',True),(101,'小鲸鱼 · 专属空间',False),(100,'Android Emulator - DSH_Phone_CN:5562',False)]
        for tick,title,owner in cases:
            phone_tools.OWNER.update(last=None,samples=0)
            with patch.object(phone_tools,'_host_input_tick',return_value=tick),patch.object(phone_tools,'_foreground_title',return_value=title):
                self.assertEqual(phone_tools.sample_owner_input(100),tick)
            self.assertEqual(phone_tools.OWNER['last'] is not None,owner,(tick,title))

    def test_unknown_input_state_never_counts(self):
        with patch.object(phone_tools,'_host_input_tick',return_value=None),patch.object(phone_tools,'_foreground_title',return_value='Android Emulator - DSH_Phone_CN:5562'):
            phone_tools.sample_owner_input(100)
        self.assertIsNone(phone_tools.OWNER['last'])

    def test_activity_is_relative_to_her_previous_action(self):
        now=time.monotonic();phone_tools.OWNER['last']=now-5
        flagged=phone_tools.owner_activity(now-30)
        self.assertTrue(flagged['ownerInputOnEmulator']);self.assertIn('不是你的操作结果',flagged['note'])
        self.assertFalse(phone_tools.owner_activity(now-1)['ownerInputOnEmulator'])
        self.assertNotIn('note',phone_tools.owner_activity(now-1))

    def test_first_action_only_counts_recent_owner_input(self):
        now=time.monotonic()
        phone_tools.OWNER['last']=now-30;self.assertTrue(phone_tools.owner_activity(None)['ownerInputOnEmulator'])
        phone_tools.OWNER['last']=now-3600;self.assertFalse(phone_tools.owner_activity(None)['ownerInputOnEmulator'])
        phone_tools.OWNER['last']=None;self.assertEqual(phone_tools.owner_activity(None),{'watching':True,'ownerInputOnEmulator':False})

    def test_invoke_attaches_hint_records_event_and_tracks_her_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=FakeStore(tmp)
            with patch.object(phone,'call',return_value={'ok':True,'action':'click'}):
                first=phone_tools.invoke(store,'phone_click',{'x':1,'y':2})
                self.assertFalse(first['ownerActivity']['ownerInputOnEmulator'])
                self.assertEqual(store.events,[])
                phone_tools.OWNER['last']=self.clock()
                second=phone_tools.invoke(store,'phone_click',{'x':1,'y':2})
                self.assertTrue(second['ownerActivity']['ownerInputOnEmulator'])
                self.assertEqual([k for k,_ in store.events],['phone.owner_input'])
                third=phone_tools.invoke(store,'phone_click',{'x':1,'y':2})
                self.assertFalse(third['ownerActivity']['ownerInputOnEmulator'])

    def test_failed_step_still_counts_as_her_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=FakeStore(tmp);error=phone.PhoneError('PHONE_UI_UNAVAILABLE','x')
            with patch.object(phone,'call',side_effect=error):
                with self.assertRaises(phone.PhoneError):phone_tools.invoke(store,'phone_ui',{})
            self.assertIn('biao',phone_tools.LAST_ACTION)

    def test_personas_are_tracked_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=FakeStore(tmp)
            with patch.object(phone,'call',return_value={'ok':True}):
                phone_tools.invoke(store,'phone_key',{'key':'HOME'})
                phone_tools.OWNER['last']=self.clock()
                store.identity.persona='li'
                self.assertTrue(phone_tools.invoke(store,'phone_key',{'key':'HOME'})['ownerActivity']['ownerInputOnEmulator'])
                store.identity.persona='biao'
                self.assertTrue(phone_tools.invoke(store,'phone_key',{'key':'HOME'})['ownerActivity']['ownerInputOnEmulator'])


if __name__=='__main__':
    unittest.main()
