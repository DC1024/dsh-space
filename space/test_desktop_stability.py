import json
import subprocess
import tempfile
import sqlite3
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

import desktop
import device_lease


class DesktopTimingTests(unittest.TestCase):
    def root(self, folder):
        root=Path(folder)
        (root/'data').mkdir()
        return root

    def test_partial_timeout_timings_survive_without_action_replay(self):
        for action in ('click','type','key','scroll'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as folder:
                root=self.root(folder)
                error=subprocess.TimeoutExpired('guest',35,stderr=b'DSH_BRIDGE_TIMING {"phase":"host.direct_connect","elapsedMs":1500}\n')
                with patch.object(desktop.subprocess,'run',side_effect=error) as run:
                    with self.assertRaises(desktop.DesktopError) as caught:
                        desktop.call(root,action,text='private-input')
                self.assertEqual(run.call_count,1)
                self.assertTrue(caught.exception.result['uncertain'])
                self.assertFalse(caught.exception.result['retry_safe'])
                record=json.loads((root/'data/desktop-bridge-timing.jsonl').read_text())
                self.assertEqual(record['outcome'],'timeout')
                self.assertEqual(record['stages'][-1]['phase'],'host.direct_connect')
                self.assertFalse(record['automaticRetry'])

    def test_readonly_timeout_is_separate_from_uncertain_input(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.root(folder)
            with patch.object(desktop.subprocess,'run',side_effect=subprocess.TimeoutExpired('guest',35)) as run:
                with self.assertRaises(desktop.DesktopError) as caught:
                    desktop.call(root,'screen')
            self.assertFalse(caught.exception.result['uncertain'])
            self.assertEqual(run.call_count,1)

    def test_receipt_and_stderr_cannot_leak_payload_to_timing_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.root(folder)
            stderr='\n'.join([
                'password=private-secret',
                'DSH_BRIDGE_TIMING {"phase":"private-secret","elapsedMs":5}',
                'DSH_BRIDGE_TIMING {"phase":"guest.agent_response","elapsedMs":3000,"text":"private-secret","token":"private-secret"}',
                'DSH_BRIDGE_TIMING invalid',
                'DSH_BRIDGE_TIMING []',
            ])
            result=SimpleNamespace(stdout='{"ok":true,"image":"private-secret"}',stderr=stderr,returncode=0)
            with patch.object(desktop.subprocess,'run',return_value=result):
                response=desktop.call(root,'type',text='private-secret')
            self.assertEqual(response['image'],'private-secret')
            text=(root/'data/desktop-bridge-timing.jsonl').read_text()
            self.assertNotIn('private-secret',text)
            self.assertEqual(json.loads(text)['stages'],[{'phase':'guest.agent_response','elapsedMs':3000}])

    def test_invalid_receipt_for_mutation_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.root(folder)
            result=SimpleNamespace(stdout='interrupted',stderr='',returncode=1)
            with patch.object(desktop.subprocess,'run',return_value=result) as run:
                with self.assertRaises(desktop.DesktopError) as caught:
                    desktop.call(root,'click',x=10,y=20)
            self.assertTrue(caught.exception.result['uncertain'])
            self.assertEqual(run.call_count,1)
            self.assertEqual(json.loads((root/'data/desktop-bridge-timing.jsonl').read_text())['outcome'],'invalid_receipt')

    def test_log_failure_does_not_turn_a_receipt_into_a_retry(self):
        result=SimpleNamespace(stdout='{"ok":true}',stderr='',returncode=0)
        with tempfile.TemporaryDirectory() as folder:
            root=self.root(folder)
            with patch.object(desktop.subprocess,'run',return_value=result) as run,patch.object(Path,'open',side_effect=OSError('full')):
                self.assertTrue(desktop.call(root,'key',key='ESC')['ok'])
            self.assertEqual(run.call_count,1)

    def test_diagnostic_log_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.root(folder)
            path=root/'data/desktop-bridge-timing.jsonl'
            path.write_text('x'*262145)
            desktop._record_timing(root,{'outcome':'receipt'})
            self.assertEqual(json.loads(path.read_text()),{'outcome':'receipt'})
            self.assertEqual((root/'data/desktop-bridge-timing.previous.jsonl').stat().st_size,262145)


class EndPoll(BaseException):pass


class DesktopPreviewPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        data=root/'data';data.mkdir()
        (data/'guest-secrets.json').write_text('{}')
        (data/'desktop.jpg').write_bytes(b'old-frame')
        (root/'config.json').write_text(json.dumps({'desktop':{'auto_poll':True,'ready':True}}))
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE tasks(status TEXT)')
        self.store=SimpleNamespace(root=root,data=data,db=db,lock=threading.RLock(),
            desktop_live={'ready':True,'capturedAt':'old'},check_storage=Mock(),event=Mock())

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def once(self):
        targets=[]
        def thread(**kwargs):
            targets.append(kwargs['target'])
            return SimpleNamespace(start=lambda:None)
        with patch.object(desktop.threading,'Thread',side_effect=thread),patch.object(desktop.time,'sleep',side_effect=EndPoll):
            desktop.start(self.store)
            with self.assertRaises(EndPoll):targets[0]()

    def unchanged(self):
        self.assertEqual(self.store.desktop_live,{'ready':True,'capturedAt':'old'})
        self.assertEqual((self.store.data/'desktop.jpg').read_bytes(),b'old-frame')
        self.assertTrue(json.loads((self.store.root/'config.json').read_text())['desktop']['ready'])
        self.store.event.assert_not_called()

    def test_phone_occupancy_skips_preview_without_false_disconnect(self):
        with device_lease.background_lease(self.store,'phone') as admitted:
            self.assertTrue(admitted)
            with patch.object(desktop,'call') as provider:
                self.once()
                provider.assert_not_called()
        self.unchanged()

    def test_waiting_model_prevents_new_background_connection(self):
        entry={'scope':str(self.store.root.resolve()),'device':'phone','owner':'model','task_id':'fixture'}
        with patch.dict(device_lease._waiting,{'fixture':entry}),patch.object(desktop,'call') as provider:
            with self.assertRaises(desktop.DesktopPreviewDeferred):
                desktop._background_call(self.store,'health')
            provider.assert_not_called()

    def test_admission_is_rechecked_between_health_and_screen(self):
        admissions=iter((True,False))
        inside=[]
        @contextmanager
        def admission(store,device):
            inside.append(True)
            try:yield next(admissions)
            finally:inside.pop()
        def health(root,action):
            self.assertTrue(inside)
            self.assertEqual(action,'health')
            return {'guestPolicyReady':True,'guestOnly':True,'user':'DSH'}
        with patch.object(device_lease,'background_lease',side_effect=admission) as gate,patch.object(desktop,'call',side_effect=health) as provider:
            self.once()
        self.assertEqual(gate.call_count,2)
        self.assertEqual(provider.call_count,1)
        self.assertFalse(inside)
        self.unchanged()

    def test_background_cannot_invoke_input(self):
        with patch.object(desktop,'call') as provider:
            for action in ('click','type','key','scroll'):
                with self.assertRaises(ValueError):desktop._background_call(self.store,action)
            provider.assert_not_called()


if __name__=='__main__':unittest.main()
