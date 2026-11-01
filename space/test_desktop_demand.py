"""On-demand bridge tests; all VM and guest operations are mocked."""
import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import desktop


class DesktopDemandTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        data=root/'data';data.mkdir()
        self.config={'desktop':{'auto_poll':True,'on_demand':True,'ready':False}}
        (root/'config.json').write_text(json.dumps(self.config))
        (data/'desktop.jpg').write_bytes(b'old')
        self.store=SimpleNamespace(root=root,data=data,lock=threading.RLock(),
            desktop_live={'ready':False},check_storage=Mock())
        self.health={'ok':True,'guestPolicyReady':True,'guestOnly':True,'user':'DSH'}
        self.frame={'ok':True,'image':base64.b64encode(b'\xff\xd8new').decode(),
                    'width':1024,'height':768,'capturedAt':'fresh'}

    def tearDown(self):self.tmp.cleanup()

    def test_demand_mode_overrides_old_auto_poll_true(self):
        self.assertFalse(desktop._auto_poll_enabled(self.store))
        self.assertTrue(self.store.desktop_live['on_demand'])
        self.assertEqual((self.store.data/'desktop.jpg').read_bytes(),b'old')

    def test_prepare_without_start_checks_health_then_screen_and_preserves_config(self):
        before=(self.store.root/'config.json').read_bytes()
        with patch.object(desktop,'call',side_effect=[self.health,self.frame]) as provider:
            status=desktop.prepare(self.store)
        self.assertEqual([c.args[1] for c in provider.call_args_list],['health','screen'])
        self.assertTrue(status['ready'])
        self.assertEqual(status['capturedAt'],'fresh')
        self.assertEqual((self.store.root/'config.json').read_bytes(),before)
        self.assertEqual((self.store.data/'desktop.jpg').read_bytes(),b'\xff\xd8new')
        self.assertFalse(desktop._auto_poll_enabled(self.store))
        self.assertTrue(self.store.desktop_live['ready'])

    def test_explicit_start_uses_parameterless_fixed_action_once(self):
        with patch.object(desktop,'call',side_effect=[{'ok':True},self.health,self.frame]) as provider:
            desktop.prepare(self.store,allow_start=True)
        self.assertEqual([c.args[1] for c in provider.call_args_list],['ensure_running','health','screen'])
        self.assertEqual(provider.call_args_list[0].kwargs,{})

    def test_failed_policy_cannot_capture_or_mark_desktop_ready(self):
        for key,value in [('guestPolicyReady',False),('guestOnly',False),('user','DSHOwner')]:
            with self.subTest(key=key),patch.object(desktop,'call',return_value={**self.health,key:value}) as provider:
                with self.assertRaises(desktop.DesktopError):desktop.prepare(self.store)
                self.assertEqual(provider.call_count,1)
                self.assertFalse(self.store.desktop_live['ready'])
                self.assertEqual((self.store.data/'desktop.jpg').read_bytes(),b'old')

    def test_start_timeout_is_not_replayed_or_followed_by_guest_actions(self):
        with patch.object(desktop,'call',side_effect=desktop.DesktopError('start timeout',True)) as provider:
            with self.assertRaises(desktop.DesktopError) as caught:
                desktop.prepare(self.store,allow_start=True)
        self.assertTrue(caught.exception.result['uncertain'])
        self.assertEqual(provider.call_count,1)
        self.assertFalse(self.store.desktop_live['ready'])

    def test_invalid_frame_preserves_last_frame_and_not_ready(self):
        frame={**self.frame,'image':base64.b64encode(b'invalid').decode()}
        with patch.object(desktop,'call',side_effect=[self.health,frame]):
            with self.assertRaises(desktop.DesktopError):desktop.prepare(self.store)
        self.assertFalse(self.store.desktop_live['ready'])
        self.assertEqual((self.store.data/'desktop.jpg').read_bytes(),b'old')

    def test_start_does_not_accept_vm_parameters(self):
        with patch.object(desktop.subprocess,'run') as process:
            with self.assertRaises(ValueError):
                desktop.call(self.store.root,'ensure_running',vm='other')
            process.assert_not_called()

    def test_truthy_text_cannot_authorize_start(self):
        with patch.object(desktop,'call') as provider:
            with self.assertRaises(desktop.DesktopError):
                desktop.prepare(self.store,allow_start='false')
            provider.assert_not_called()

    def test_malformed_frame_is_readonly_failure_not_unknown_input(self):
        with patch.object(desktop,'call',side_effect=[self.health,{**self.frame,'image':'bad!'}]):
            with self.assertRaises(desktop.DesktopError) as caught:desktop.prepare(self.store)
        self.assertFalse(caught.exception.result['uncertain'])
        self.assertEqual((self.store.data/'desktop.jpg').read_bytes(),b'old')


@unittest.skipUnless(os.name=='nt','Windows PowerShell stubbed fixed-VM branch')
class FixedVMStartTests(unittest.TestCase):
    def invoke(self,state='Off',identity='e0fea15b-c2bb-4a06-8849-e0571606e08f',media=False,request=None):
        # Shadow every Hyper-V/remote command. This script cannot reach a real VM.
        source=(Path(__file__).parent/'guest_bridge.ps1').read_text(encoding='utf-8-sig')
        prelude=r'''
$env:PSModulePath=($env:SystemRoot+'\System32\WindowsPowerShell\v1.0\Modules;'+$env:ProgramFiles+'\WindowsPowerShell\Modules')
$global:fakeState=$env:DSH_FAKE_STATE
function Get-VM { param($Name,$Id) [pscustomobject]@{Name='DSH-Home-v01';Id=[guid]$env:DSH_FAKE_ID;State=$global:fakeState} }
function Get-VMDvdDrive { param($VMName) if($env:DSH_FAKE_MEDIA -eq 'true'){[pscustomobject]@{Path='test.iso'}} }
function Start-VM { param($VM,$Confirm) [IO.File]::AppendAllText($env:DSH_START_MARKER,$VM.Id.ToString()+"`n");$global:fakeState='Running' }
function Invoke-Command { throw 'Unexpected guest execution in host-only start branch' }
'''
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            (root/'data/guest-secrets.json').write_text('{}')
            path=root/'probe.ps1';path.write_text(prelude+source,encoding='utf-8-sig')
            marker=root/'started.txt'
            env=dict(os.environ,DSH_FAKE_STATE=state,DSH_FAKE_ID=identity,
                DSH_FAKE_MEDIA=str(media).lower(),DSH_START_MARKER=str(marker))
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path)],
                input=json.dumps(request or {'action':'ensure_running'}),capture_output=True,text=True,encoding='utf-8',env=env,timeout=10)
            return json.loads(result.stdout.lstrip('\ufeff')),marker.read_text().splitlines() if marker.exists() else []

    def test_off_starts_only_existing_fixed_identity_once(self):
        result,starts=self.invoke()
        self.assertTrue(result['ok']);self.assertTrue(result['started'])
        self.assertEqual(result['state'],'Running')
        self.assertEqual(starts,['e0fea15b-c2bb-4a06-8849-e0571606e08f'])

    def test_running_is_noop(self):
        result,starts=self.invoke(state='Running')
        self.assertTrue(result['ok']);self.assertFalse(result['started'])
        self.assertEqual(starts,[])

    def test_mismatch_media_saved_and_arbitrary_parameters_never_start(self):
        cases=[{'identity':'00000000-0000-0000-0000-000000000001'},
               {'media':True},{'state':'Saved'},{'state':'Starting'},
               {'request':{'action':'ensure_running','vm':'other'}},
               {'request':{'action':'ensure_running','force':True}}]
        for case in cases:
            with self.subTest(case=case):
                result,starts=self.invoke(**case)
                self.assertFalse(result['ok']);self.assertEqual(starts,[])


if __name__=='__main__':unittest.main()
