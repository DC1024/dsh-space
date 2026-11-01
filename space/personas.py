"""Separate runtime sessions; a common memory pool with explicit provenance.

The owner console may inspect either persona. Model MCP routes bind a persona
on the server. Mode tags describe who recorded/performed something, not who
may read it. Old memory rows and their original source/time remain intact.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import uuid

PERSONAS = {'biao': '表', 'li': '里'}
PERFORMERS = {'biao': '表', 'li': '里', 'owner': '主人', 'external': '外部来源', 'unknown': '未知'}
SCOPED_STATE = {'revision', 'correction', 'correction_ack_revision',
                'correction_response', 'stream_seen', 'active_request'}
SCOPED_DEFAULTS = {'revision': 0, 'correction': '', 'correction_ack_revision': -1,
                   'correction_response': {}, 'stream_seen': False, 'active_request': {}}
ACTOR = ContextVar('space_persona', default=None)


class PersonaBoundary:
    def __init__(self, store):
        self.store = store
        db = store.db
        for table in ('memory', 'tasks', 'events'):
            names = {r[1] for r in db.execute('PRAGMA table_info('+table+')')}
            if 'persona' not in names:
                db.execute("ALTER TABLE "+table+" ADD COLUMN persona TEXT NOT NULL DEFAULT 'biao'")
        db.execute('''CREATE TABLE IF NOT EXISTS activities(
            id TEXT PRIMARY KEY, time TEXT NOT NULL, persona TEXT NOT NULL,
            kind TEXT NOT NULL, summary TEXT NOT NULL, task_id TEXT, session_id TEXT,
            source_key TEXT UNIQUE)''')
        self.migrate_memory_provenance()
        db.execute("INSERT OR IGNORE INTO state VALUES('active_persona','\"biao\"')")
        for persona in PERSONAS:
            for key, default in SCOPED_DEFAULTS.items():
                old = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
                value = old[0] if persona == 'biao' and old else json.dumps(default)
                db.execute('INSERT OR IGNORE INTO state VALUES(?,?)',
                           ('persona/'+persona+'/'+key, value))
        db.commit()

    def migrate_memory_provenance(self):
        """Add metadata without relabeling old text or inventing an executor."""
        db = self.store.db
        names = {r[1] for r in db.execute('PRAGMA table_info(memory)')}
        additions = {'recorded_by': "TEXT NOT NULL DEFAULT 'unknown'",
                     'performed_by': "TEXT NOT NULL DEFAULT 'unknown'",
                     'action_id': 'TEXT', 'task_id': 'TEXT', 'performed_at': 'TEXT'}
        for name, declaration in additions.items():
            if name not in names:
                db.execute('ALTER TABLE memory ADD COLUMN '+name+' '+declaration)
        if 'recorded_by' not in names:
            # Legacy persona is a creation/visibility tag, not proof of action.
            # A shared row has lost its creation mode; leave it explicitly unknown.
            db.execute("UPDATE memory SET recorded_by=persona WHERE persona IN ('biao','li')")

    def active(self):
        with self.store.lock:
            row = self.store.db.execute("SELECT value FROM state WHERE key='active_persona'").fetchone()
            return self.validate(json.loads(row[0]))

    def validate(self, persona):
        if persona not in PERSONAS:
            raise ValueError('人格必须为表或里')
        return persona

    def actor(self):
        return ACTOR.get() or self.active()

    @contextmanager
    def bind(self, persona):
        token = ACTOR.set(self.validate(persona))
        try:
            yield
        finally:
            ACTOR.reset(token)

    def key(self, key):
        return 'persona/'+self.actor()+'/'+key if key in SCOPED_STATE else key

    def runtime(self, persona=None):
        persona = self.validate(persona or self.actor())
        runtime = dict(self.store.config['runtime'])
        bindings = runtime.get('persona_bindings')
        if bindings:
            runtime.update(bindings.get(persona, {'session_id': None}))
        elif persona != 'biao':
            runtime['session_id'] = None
        runtime['active_persona'] = persona
        return runtime

    def log(self, kind, summary, task_id=None, session_id=None, source_key=None):
        # Only callers holding actual action/result evidence write this table.
        # Never copy task text, replies, reasoning, screenshots or private memories.
        s = self.store
        with s.lock, s.db:
            s.db.execute('INSERT OR IGNORE INTO activities VALUES(?,?,?,?,?,?,?,?)',
                         (str(uuid.uuid4()), __import__('datetime').datetime.now(
                             __import__('datetime').timezone.utc).isoformat(),
                          self.actor(), kind, summary[:1000], task_id, session_id, source_key))

    def actions(self, other_only=False, limit=50):
        with self.store.lock:
            actor = self.actor()
            rows = self.store.db.execute(
                'SELECT * FROM activities '+('WHERE persona<>? ' if other_only else '')+
                'ORDER BY time DESC,id DESC LIMIT ?',
                (actor, max(1, min(int(limit), 100))) if other_only else (max(1, min(int(limit), 100)),)).fetchall()
            return [dict(r) for r in rows]

    def publish_binding(self):
        """Atomic public registry; contains no credential or persona prompt."""
        s = self.store
        actor = self.active()
        runtime = self.runtime(actor)
        registry = []
        for persona in PERSONAS:
            binding = self.runtime(persona)
            if binding.get('session_id'):
                registry.append({'persona': persona, 'sessionId': binding['session_id'],
                                 'model': 'deepseek-flash', 'provider': 'deepseek-official'})
        public = {'schemaVersion': 2, 'home': str(s.data/'runtime-home'), 'profile': 'space',
                  'base': 'http://127.0.0.1:19992', 'activePersona': actor,
                  'sessionId': runtime.get('session_id'),
                  'model': 'deepseek-flash', 'provider': 'deepseek-official',
                  'agent_preset': runtime.get('agent_preset', 'space-'+actor+'-v02'),
                  'registrations': registry}
        target = s.data/'runtime-binding.public.json'
        tmp = target.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(target)
        return public
