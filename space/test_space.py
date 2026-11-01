import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
from space import Store, Rejected, ROOT


class SpaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copyfile(ROOT / 'config.json', self.root / 'config.json')
        fixture=json.loads((self.root/'config.json').read_text(encoding='utf-8'))
        fixture['runtime'].update(ready=False,chat_ready=False,session_id=None,budget_source_ready=False)
        fixture['runtime'].pop('persona_bindings',None)
        (self.root/'config.json').write_text(json.dumps(fixture),encoding='utf-8')
        self.store = Store(self.root)
        self.store.config['min_free_gb'] = 0

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    @unittest.skipUnless(__import__('os').name == 'nt', 'GetSystemTimes sampling is Windows-only')
    def test_back_to_back_cpu_readings_are_not_samples(self):
        import threading, time
        s = self.store
        for _ in range(3):
            s.resources()
        self.assertEqual(len(s.cpu_samples), 0)
        time.sleep(0.6)
        s.resources()
        self.assertEqual(len(s.cpu_samples), 1)
        time.sleep(0.6)
        threads = [threading.Thread(target=s.resources) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(s.cpu_samples), 2)

    def test_queue_retry_and_restart_do_not_duplicate_or_resume(self):
        s = self.store
        first = s.command('chat', {'text': '你好', 'request_key': 'stable-key'})
        second = s.command('chat', {'text': '你好', 'request_key': 'stable-key'})
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second['duplicate'])
        with s.db:
            s.db.execute("UPDATE tasks SET status='running'")
            s.set('paused', False)
        s.db.close()
        self.store = s = Store(self.root)
        self.assertTrue(s.get('paused'))
        self.assertEqual(s.status()['tasks'][0]['status'], 'uncertain')

    def test_memory_revision_and_reload(self):
        s = self.store
        old = s.add_memory({'kind': 'preference', 'text': '旧偏好'})
        s.add_memory({'kind': 'preference', 'text': '新偏好', 'supersedes': old['id']})
        self.assertEqual([x['text'] for x in s.memories()], ['新偏好'])
        self.assertEqual(s.db.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 2)
        s.db.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.memories()[0]['text'], '新偏好')

    def test_correction_pauses_and_cannot_resume_without_ack_and_runtime(self):
        s = self.store
        s.command('correct', {'text': '先问清楚安装用途'})
        self.assertTrue(s.get('paused'))
        self.assertEqual(s.get('revision'), 1)
        with self.assertRaises(Rejected):
            s.command('resume', {})
        self.assertTrue(s.get('paused'))
        self.assertEqual(s.memories()[0]['kind'], 'correction')

    def test_install_decision_is_record_only(self):
        s = self.store
        aid = s.install_request('测试安装', {key: 'test' for key in ('purpose','source','version','download','disk','scope')})
        s.command('approval', {'id': aid, 'decision': 'denied'})
        with self.assertRaises(Rejected):
            s.command('approval', {'id': aid, 'decision': 'approved'})
        self.assertEqual(s.status()['approvals'][0]['status'], 'denied')
        self.assertFalse(s.config['runtime']['ready'])

    def test_event_binding_flash_dedup_and_replay(self):
        s = self.store
        body = {'session_id': 'formal-test', 'model': 'deepseek-flash', 'kind': 'reasoning.delta',
                'source_key': 'chunk-1', 'payload': {'text': '测试夹具，非真实模型输出'}}
        with self.assertRaises(Rejected):
            s.ingest(body)
        s.config['runtime']['session_id'] = 'formal-test'
        s.ingest(body); s.ingest(body)
        rows = [r for r in s.events() if r['kind'] == 'reasoning.delta']
        self.assertEqual(len(rows), 1)
        self.assertTrue(s.get('stream_seen'))
        self.assertEqual(s.events(rows[0]['seq']), [])
        body['model'] = 'deepseek-v4-pro'
        with self.assertRaises(Rejected):
            s.ingest(body)

    def test_storage_and_budget_fail_closed_without_deleting(self):
        s = self.store
        s.add_memory({'kind': 'experience', 'text': '保留我'})
        with s.db:
            s.set('paused', False)
        s.config['history_quota_gb'] = 0
        with self.assertRaises(Rejected):
            s.command('chat', {'text': '不应写入', 'request_key': 'quota'})
        self.assertTrue(s.get('paused'))
        self.assertEqual(s.memories()[0]['text'], '保留我')
        self.assertFalse(s.budget()['shared_ledger_verified'])

    def test_export_is_consistent_and_hashes_match_no_secret(self):
        s = self.store
        s.add_memory({'kind': 'interest', 'text': '星星与中文'})
        target = s.export()
        with zipfile.ZipFile(target) as z:
            checks = json.loads(z.read('SHA256.json'))
            for name, expected in checks.items():
                self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), expected)
            self.assertNotIn('bridge.key', z.namelist())
            self.assertIn('星星与中文', z.read('memory.jsonl').decode())
        self.assertTrue(s.memories())


if __name__ == '__main__':
    unittest.main(verbosity=2)
