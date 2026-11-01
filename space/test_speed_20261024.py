"""2026-10-24 提速改动：动作后附画面、手机身份缓存、常驻电脑桥接、主人消息优先于自主。"""
from contextlib import nullcontext
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import desktop
import phone
import phone_tools
import worker
from tests_phone import FakeDevice, png


class PhoneSpeedTests(unittest.TestCase):
    def setUp(self):
        phone._forget()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.device = FakeDevice()
        self.patches = [
            patch.object(phone, '_environment', return_value=(Path('trusted-adb.exe'), self.root)),
            patch.object(phone, '_device_lock', side_effect=lambda _: nullcontext()),
            patch.object(phone, '_adb', side_effect=self.device),
            patch.object(phone.time, 'sleep'),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()
        phone._forget()

    def probes(self):
        return [a for a, _ in self.device.commands if a in (('emu', 'avd', 'name'), ('shell', 'getprop', 'ro.build.version.sdk'))]

    def test_identity_is_checked_once_then_cached_but_connection_every_time(self):
        phone.call(self.root, 'key', key='HOME')
        phone.call(self.root, 'key', key='BACK')
        self.assertEqual(len(self.probes()), 2)
        self.assertEqual(sum(1 for a, _ in self.device.commands if a == ('get-state',)), 2)

    def test_cache_expires_and_any_error_forces_a_full_recheck(self):
        phone.call(self.root, 'key', key='HOME')
        phone._PROBED['at'] -= phone.PROBE_TTL + 1
        phone.call(self.root, 'key', key='HOME')
        self.assertEqual(len(self.probes()), 4)
        self.device.name = 'Other_AVD'
        phone._PROBED['at'] -= phone.PROBE_TTL + 1
        with self.assertRaises(phone.PhoneError):
            phone.call(self.root, 'key', key='HOME')
        self.assertIsNone(phone._PROBED['at'])

    def test_click_reuses_recent_frame_size_and_attaches_after_frame(self):
        phone.call(self.root, 'screen')
        before = sum(1 for a, _ in self.device.commands if a == ('exec-out', 'screencap', '-p'))
        result = phone.call(self.root, 'click', x=10, y=20, screen_after=0.5)
        after = sum(1 for a, _ in self.device.commands if a == ('exec-out', 'screencap', '-p'))
        self.assertEqual(after - before, 1)  # only the after-action frame, no pre-click frame
        self.assertTrue(result['command_acknowledged'])
        self.assertTrue(result['screenAfterAction']['ok'])
        self.assertEqual((result['width'], result['height']), (480, 800))
        self.assertTrue(result['image'])

    def test_failed_after_frame_keeps_the_action_receipt(self):
        phone.call(self.root, 'screen')
        self.device.frame = b'broken'
        result = phone.call(self.root, 'key', key='HOME', screen_after=0.5)
        self.assertTrue(result['command_acknowledged'])
        self.assertFalse(result['screenAfterAction']['ok'])
        self.assertNotIn('image', result)

    def test_without_screen_after_nothing_extra_is_captured(self):
        result = phone.call(self.root, 'key', key='HOME')
        self.assertNotIn('image', result)
        self.assertFalse([a for a, _ in self.device.commands if a == ('exec-out', 'screencap', '-p')])

    def test_rotation_seen_in_last_frame_is_used_for_bounds(self):
        self.device.frame = png(800, 480)
        phone.call(self.root, 'screen')
        with self.assertRaises(phone.PhoneError) as ctx:
            phone.call(self.root, 'click', x=100, y=600)
        self.assertEqual(ctx.exception.code, 'PHONE_COORDINATE_OUTSIDE')


class ScreenAfterConfigTests(unittest.TestCase):
    def test_defaults_and_owner_switch(self):
        self.assertEqual(phone_tools.screen_after_seconds({}, 'phone'), 0.5)
        self.assertEqual(phone_tools.screen_after_seconds({}, 'desktop'), 0.4)
        self.assertEqual(phone_tools.screen_after_seconds({'speed': {'phone_settle_ms': 900}}, 'phone'), 0.9)
        self.assertEqual(phone_tools.screen_after_seconds({'speed': {'phone_settle_ms': 'x'}}, 'phone'), 0.5)
        self.assertIsNone(phone_tools.screen_after_seconds({'speed': {'screen_after_action': False}}, 'phone'))

    def test_background_health_skips_while_she_uses_the_phone(self):
        with patch.dict(phone_tools.LAST_ACTION, {'biao': phone_tools.time.monotonic()}, clear=True), \
             patch.object(phone, 'health') as health:
            self.assertFalse(phone_tools.poll_once(SimpleNamespace()))
            health.assert_not_called()


class FakeServe:
    """Stands in for `powershell guest_bridge.ps1 -Serve`."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        out = self
        class In:
            def write(self, text): out.sent.append(text)
            def flush(self): out.flushed = True
        self.stdin = In()
        self.stdout = iter(self.replies)
        self.killed = False

    def poll(self):
        return 1 if self.killed else None

    def kill(self):
        self.killed = True


class DesktopServeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        desktop._serve_stop()
        desktop.PERSISTENT['enabled'] = True

    def tearDown(self):
        desktop.PERSISTENT['enabled'] = False
        desktop._serve_stop()
        self.temp.cleanup()

    def serve(self, *replies):
        fake = FakeServe(replies)
        return fake, patch.object(desktop.subprocess, 'Popen', return_value=fake)

    def header(self, code=0):
        stages = [{'phase': 'host.request', 'elapsedMs': 0}, {'phase': 'host.session_reused', 'elapsedMs': 40},
                  {'phase': 'guest.screen_after', 'elapsedMs': 300, 'guestElapsedMs': 280}]
        return desktop.SERVE_HEADER + json.dumps({'exitCode': code, 'stages': stages}) + '\n'

    def timing(self):
        return [json.loads(x) for x in (self.root / 'data' / 'desktop-bridge-timing.jsonl').read_text().splitlines()]

    def test_one_process_serves_requests_and_returns_after_action_frame(self):
        receipt = {'ok': True, 'kind': 'click', 'frameImage': 'QUJD', 'frameWidth': 1280, 'frameHeight': 800,
                   'frameCapturedAt': '2026-10-24T00:00:00Z'}
        fake, popen = self.serve(self.header(), json.dumps(receipt) + '\n',
                                 self.header(), json.dumps({'ok': True, 'image': 'eA=='}) + '\n')
        with popen as started, patch.object(desktop.subprocess, 'run') as run:
            response = desktop.call(self.root, 'click', x=5, y=6, screen_after=0.4)
            desktop.call(self.root, 'screen')
        started.assert_called_once()
        run.assert_not_called()
        self.assertEqual(json.loads(fake.sent[0]), {'action': 'click', 'x': 5, 'y': 6, 'screen_after_ms': 400})
        self.assertNotIn('screen_after_ms', json.loads(fake.sent[1]))
        self.assertEqual(response['frame'], {'image': 'QUJD', 'width': 1280, 'height': 800, 'capturedAt': '2026-10-24T00:00:00Z'})
        self.assertNotIn('frameImage', response)
        rows = self.timing()
        self.assertEqual([r['bridge'] for r in rows], ['persistent', 'persistent'])
        self.assertIn('guest.screen_after', [s['phase'] for s in rows[0]['stages']])

    def test_bridge_dying_after_a_mutation_is_uncertain_and_never_replayed(self):
        fake, popen = self.serve()  # EOF right away
        with popen, patch.object(desktop.subprocess, 'run') as run:
            with self.assertRaises(desktop.DesktopError) as ctx:
                desktop.call(self.root, 'type', text='secret')
        run.assert_not_called()
        self.assertTrue(ctx.exception.result['uncertain'])
        self.assertTrue(fake.killed)
        self.assertNotIn('secret', (self.root / 'data' / 'desktop-bridge-timing.jsonl').read_text())

    def test_read_only_falls_back_to_one_shot_when_the_bridge_breaks(self):
        fake, popen = self.serve()
        done = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'image': 'eA=='}), '')
        with popen, patch.object(desktop.subprocess, 'run', return_value=done) as run:
            self.assertTrue(desktop.call(self.root, 'screen')['ok'])
        run.assert_called_once()
        self.assertEqual(self.timing()[-1]['bridge'], 'one-shot')

    def test_unsendable_request_uses_one_shot_even_for_mutations(self):
        with patch.object(desktop.subprocess, 'Popen', side_effect=OSError), \
             patch.object(desktop.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', '')) as run:
            self.assertTrue(desktop.call(self.root, 'key', key='ESC')['ok'])
        run.assert_called_once()

    def test_timeout_kills_the_bridge_and_reports_uncertain_mutation(self):
        class Silent(FakeServe):
            pass
        fake = Silent([])
        fake.stdout = iter(())  # reader ends; patched queue never yields
        with patch.object(desktop.subprocess, 'Popen', return_value=fake), \
             patch.object(desktop.queue.Queue, 'get', side_effect=queue.Empty), \
             patch.object(desktop.subprocess, 'run') as run:
            with self.assertRaises(desktop.DesktopError) as ctx:
                desktop.call(self.root, 'click', x=1, y=1)
        run.assert_not_called()
        self.assertTrue(ctx.exception.result['uncertain'])
        self.assertTrue(fake.killed)
        self.assertEqual(self.timing()[-1]['outcome'], 'timeout')

    def test_cli_processes_keep_one_shot_by_default(self):
        desktop.PERSISTENT['enabled'] = False
        with patch.object(desktop.subprocess, 'Popen') as popen, \
             patch.object(desktop.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', '')):
            desktop.call(self.root, 'health')
        popen.assert_not_called()


class OwnerMessageFirstTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE tasks(id TEXT, request_key TEXT, status TEXT, persona TEXT)')
        self.db = db
        self.store = SimpleNamespace(db=db, lock=threading.RLock(),
                                     identity=SimpleNamespace(actor=lambda: 'biao'))

    def tearDown(self):
        self.db.close()

    def test_only_a_queued_owner_message_for_this_persona_preempts(self):
        self.db.execute("INSERT INTO tasks VALUES('a','biao:autonomy-1','running','biao')")
        self.assertFalse(worker.owner_message_waiting(self.store))
        self.db.execute("INSERT INTO tasks VALUES('b','li:pane-1','queued','li')")
        self.db.execute("INSERT INTO tasks VALUES('c','biao:autonomy-2','queued','biao')")
        self.assertFalse(worker.owner_message_waiting(self.store))
        self.db.execute("INSERT INTO tasks VALUES('d','biao:pane-2','queued','biao')")
        self.assertTrue(worker.owner_message_waiting(self.store))

    def test_preempt_reason_is_explained(self):
        from failure_reason import classify
        failure = classify({'kind': 'cancelled'}, 'owner-message-preempt')
        self.assertEqual(failure['code'], 'OWNER_MESSAGE_FIRST')


class StorageScanCacheTests(unittest.TestCase):
    def make(self, free_gib=500, used_gib=88):
        import space
        GIB = 2**30
        class Probe:
            STORAGE_CACHE_SECONDS = space.Store.STORAGE_CACHE_SECONDS
            STORAGE_CACHE_MARGIN = space.Store.STORAGE_CACHE_MARGIN
            _storage_for_check = space.Store._storage_for_check
            check_storage = space.Store.check_storage
            scans = 0
            usage = {'scan_complete': True, 'free_bytes': free_gib * GIB, 'used_bytes': used_gib * GIB, 'history_bytes': GIB}
            def storage(self):
                type(self).scans += 1
                return dict(self.usage)
        probe = Probe()
        probe.config = {'min_free_gb': 50, 'max_space_gb': 470, 'history_quota_gb': 8}
        return probe

    def test_far_from_limits_reuses_a_recent_scan(self):
        probe = self.make()
        for _ in range(5):
            probe.check_storage()
        self.assertEqual(type(probe).scans, 1)

    def test_expired_scan_is_repeated(self):
        probe = self.make()
        probe.check_storage()
        probe._storage_cache = (probe._storage_cache[0] - probe.STORAGE_CACHE_SECONDS - 1, probe._storage_cache[1])
        probe.check_storage()
        self.assertEqual(type(probe).scans, 2)

    def test_near_any_limit_always_rescans(self):
        for kwargs in ({'free_gib': 51}, {'used_gib': 469}):
            probe = self.make(**kwargs)
            probe.check_storage(); probe.check_storage(); probe.check_storage()
            self.assertEqual(type(probe).scans, 3, kwargs)

    def test_instance_override_is_never_cached(self):
        probe = self.make()
        calls = []
        probe.storage = lambda: calls.append(1) or dict(probe.usage)
        probe.check_storage(); probe.check_storage()
        self.assertEqual(len(calls), 2)


class MessageWakeTests(unittest.TestCase):
    def test_new_message_wakes_an_idle_worker_at_once(self):
        woke = threading.Event()
        def idle():
            worker._idle(5)
            woke.set()
        thread = threading.Thread(target=idle, daemon=True)
        thread.start()
        deadline = __import__('time').monotonic() + 2
        while not woke.is_set() and __import__('time').monotonic() < deadline:
            worker.wake()
            woke.wait(0.05)
        self.assertTrue(woke.is_set())
        thread.join(1)

    def test_idle_without_message_still_returns_after_the_timeout(self):
        import time
        started = time.monotonic()
        worker._idle(0.05)
        self.assertLess(time.monotonic() - started, 1)


class ObserverDiscoveryTests(unittest.TestCase):
    def test_new_sessions_are_discovered_fast_while_followers_keep_their_interval(self):
        import time
        from thinking_observer import ThinkingObserverManager
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ThinkingObserverManager(project_root=root, binding_file=root / 'binding.json',
                                              archive_root=root / 'archive', autostart=False)
            self.assertEqual(manager.poll_seconds, 2)
            self.assertLessEqual(manager.discover_seconds, 0.25)
            calls = []
            def refresh():
                calls.append(time.monotonic())
                if len(calls) >= 4:
                    manager.stop_event.set()
            manager.refresh_binding = refresh
            started = time.monotonic()
            manager._run()
            self.assertEqual(len(calls), 4)
            self.assertLess(time.monotonic() - started, 1.5)  # was 3 x 2 s


if __name__ == '__main__':
    unittest.main()
