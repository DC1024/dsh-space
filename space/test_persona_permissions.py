"""表/里人格权限分档与事后审查的最小回归测试。

只使用临时目录和内存数据库，不接触真实的 data/space.sqlite，
也不启动控制台、不调用模型、不操作设备。
"""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import persona_policy
from space_tools import call

SCHEMA = '''
CREATE TABLE workspace_writes(id TEXT PRIMARY KEY,time TEXT NOT NULL,persona TEXT NOT NULL,
  scope TEXT NOT NULL,rel TEXT NOT NULL,chars INTEGER NOT NULL,sha256 TEXT NOT NULL,
  previous_sha256 TEXT,previous_text TEXT,task_id TEXT,session_id TEXT,
  review TEXT NOT NULL DEFAULT 'pending',review_time TEXT,reviewer TEXT,note TEXT,
  source_key TEXT UNIQUE);
'''


class Store:
    def __init__(self, root, actor):
        self.root = Path(root)
        self.actor = actor
        self.history = []
        self.identity = SimpleNamespace(actor=lambda: self.actor,
                                        log=self._log,
                                        runtime=lambda: {'session_id': 'session-test'})
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)
        self.check_storage = lambda: None

    def switch(self, actor):
        self.actor = actor

    def _log(self, *args, **kwargs):
        self.history.append((args, kwargs))

    def get(self, key):
        return {}


class PersonaPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'space'
        self.root.mkdir()
        self.store = Store(self.root, 'biao')

    def tearDown(self):
        self.tmp.cleanup()

    def test_biao_writes_own_workspace_and_interface(self):
        own = call(self.store, 'workspace_write', {'path': 'notes/a.txt', 'text': 'hi'})
        self.assertEqual(own['scope'], 'workspace')
        self.assertEqual((self.root / 'workdir/biao/notes/a.txt').read_text('utf-8'), 'hi')
        asset = call(self.store, 'workspace_write', {'path': 'dist/index.html', 'text': '<p>ok</p>'})
        self.assertEqual(asset['scope'], 'interface')
        self.assertEqual((self.root / 'dist/index.html').read_text('utf-8'), '<p>ok</p>')
        # 界面写入不产生 .versions 副本，避免在 dist 里留下旧静态文件。
        self.assertFalse((self.root / 'dist/.versions').exists())

    def test_biao_cannot_write_peer_or_unsupported_interface_file(self):
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_write', {'path': 'peer/x.txt', 'text': 'nope'})
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_write', {'path': 'dist/evil.py', 'text': 'nope'})

    def test_li_writes_only_own_workspace_but_reads_peer_and_interface(self):
        call(self.store, 'workspace_write', {'path': 'dist/index.html', 'text': 'ui'})
        self.store.switch('li')
        call(self.store, 'workspace_write', {'path': 'private.txt', 'text': 'inside'})
        self.assertTrue((self.root / 'workdir/li/private.txt').exists())
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_write', {'path': 'dist/index.html', 'text': 'li edit'})
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_write', {'path': 'peer/private.txt', 'text': 'li edit'})
        self.assertEqual(call(self.store, 'workspace_read', {'path': 'dist/index.html'})['text'], 'ui')

    def test_li_reads_biao_workspace(self):
        call(self.store, 'workspace_write', {'path': 'notes/a.txt', 'text': 'biao note'})
        self.store.switch('li')
        value = call(self.store, 'workspace_read', {'path': 'peer/notes/a.txt'})
        self.assertEqual(value['text'], 'biao note')
        self.assertEqual(value['scope'], 'peer')
        rows = call(self.store, 'workspace_list', {'path': 'peer/notes'})
        self.assertEqual([r['path'] for r in rows], ['notes/a.txt'])

    def test_interface_write_records_review_row_and_li_reviews_once(self):
        call(self.store, 'workspace_write', {'path': 'dist/app.js', 'text': 'v1'})
        call(self.store, 'workspace_write', {'path': 'dist/app.js', 'text': 'v2'})
        pending = self.store.db.execute("SELECT COUNT(*) FROM workspace_writes WHERE review='pending'").fetchone()[0]
        self.assertEqual(pending, 2)
        self.store.switch('li')
        listing = call(self.store, 'workspace_review_list', {'status': 'pending'})
        self.assertEqual(listing['pending'], 2)
        first = listing['rows'][0]
        self.assertEqual(first['rel'], 'app.js')
        self.assertEqual(first['scope'], 'interface')
        diff = call(self.store, 'workspace_read_diff', {'id': first['id']})
        self.assertEqual(diff['current_text'], 'v2')
        self.assertEqual(diff['previous_text'], 'v1')
        verdict = call(self.store, 'workspace_review_decide',
                       {'id': first['id'], 'decision': 'pass', 'note': '排版调整，可接受'})
        self.assertEqual(verdict['decision'], 'pass')
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_review_decide', {'id': first['id'], 'decision': 'flag'})
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_review_decide', {'id': first['id'], 'decision': 'maybe'})
        self.assertEqual(call(self.store, 'workspace_review_list', {'status': 'pass'})['rows'][0]['id'], first['id'])
        self.assertEqual(call(self.store, 'workspace_review_list', {})['pending'], 1)

    def test_workspace_writes_are_visible_to_li_only_via_review_tools(self):
        call(self.store, 'workspace_write', {'path': 'notes/a.txt', 'text': 'x'})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM workspace_writes').fetchone()[0], 1)
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_review_list', {})
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_read_diff', {'id': 'x'})
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_review_decide', {'id': 'x', 'decision': 'pass'})

    def test_path_escape_and_reserved_names_rejected(self):
        for bad in ('../x.txt', 'C:\\x.txt', '/etc/passwd', 'dist/../x.html', '.versions/x'):
            with self.assertRaises(ValueError, msg=bad):
                call(self.store, 'workspace_write', {'path': bad, 'text': 'x'})
        self.store.switch('li')
        with self.assertRaises(ValueError):
            call(self.store, 'workspace_read', {'path': '../config.json'})

    def test_describe_reports_split_permissions(self):
        info = persona_policy.describe()
        self.assertEqual(info['biao']['write'], ['workdir/biao', 'dist'])
        self.assertEqual(info['li']['write'], ['workdir/li'])
        self.assertIn('dist', info['biao']['read'])
        self.assertIn('workspace_review_decide', info['li']['tools'])
        self.assertNotIn('workspace_review_decide', info['biao']['tools'])


if __name__ == '__main__':
    unittest.main()
