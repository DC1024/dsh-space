"""Local owner console. Standard library only; no host execution tools.

The runtime bridge is intentionally closed until a guest-only executor and a
shared budget authority are verified. An approval record never runs an installer.
"""
import argparse
import ctypes
import datetime as dt
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import traceback
import uuid
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo
from budget import BudgetAuthority, BudgetDenied
from personas import PersonaBoundary, PERSONAS, PERFORMERS
from owner_http_auth import OwnerHttpAuth,observer_open
import persona_policy
import owner_settings
import owner_goals
from task_authority import data_authority, web_source_note

ROOT = Path(__file__).resolve().parent
# `python space.py` runs this file as __main__ while helper modules do
# `from space import Rejected`: that used to create a second copy of this module
# with a *different* Rejected class, so their rejections slipped past the handler
# tuples and killed the socket (the client only saw "fetch failed"). Alias the
# running module so every importer shares one class identity.
if __name__ == '__main__':
    sys.modules.setdefault('space', sys.modules[__name__])
VERSION = '0.2.0'
KINDS = {'preference', 'experience', 'relationship', 'project', 'correction', 'interest'}
EVENT_TYPES = {'reasoning.delta', 'reply.delta', 'turn.start', 'turn.end', 'tool.start', 'tool.end', 'runtime.error'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _internal_error_log(where, error):
    """Never let one unexpected exception close the bridge socket silently.

    Anything outside the explicit handler tuples used to propagate out of the
    request thread, so the client only saw "fetch failed". Record the traceback
    locally and answer with an honest isError instead."""
    try:
        log = ROOT / 'data' / 'console-error.log'
        try:
            if log.exists() and log.stat().st_size > 512 * 1024:
                log.replace(log.with_suffix('.log.1'))
        except OSError:
            pass
        with log.open('a', encoding='utf-8') as handle:
            handle.write('%s [%s] %s: %s\n%s\n' % (now(), where, type(error).__name__, error,
                                                   traceback.format_exc()))
    except BaseException:
        pass


class Rejected(Exception):
    def __init__(self, message, code='SPACE_REJECTED'):
        super().__init__(message)
        self.code=code


class Store:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.data = self.root / 'data'
        self.data.mkdir(parents=True, exist_ok=True)
        (self.root / 'exports').mkdir(exist_ok=True)
        self.config = json.loads((self.root / 'config.json').read_text(encoding='utf-8-sig'))
        self.path = self.data / 'space.sqlite'
        self.lock = threading.RLock()
        self.device_inflight = {}
        self.run_id = str(uuid.uuid4())
        self.phone_live = {"ready":False,"error":"等待手机状态"}
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,time TEXT NOT NULL,
            kind TEXT NOT NULL,task_id TEXT,session_id TEXT,payload TEXT NOT NULL,source_key TEXT UNIQUE);
          CREATE TABLE IF NOT EXISTS memory(id TEXT PRIMARY KEY,kind TEXT NOT NULL,text TEXT NOT NULL,
            source TEXT NOT NULL,time TEXT NOT NULL,supersedes TEXT REFERENCES memory(id));
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,
            text TEXT NOT NULL,status TEXT NOT NULL,time TEXT NOT NULL,revision INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY,text TEXT NOT NULL,persona TEXT NOT NULL,
            target TEXT NOT NULL,status TEXT NOT NULL,position INTEGER NOT NULL,created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,last_task_id TEXT);
          CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,
            details TEXT NOT NULL,status TEXT NOT NULL,time TEXT NOT NULL,decision_time TEXT);
          CREATE TABLE IF NOT EXISTS costs(id TEXT PRIMARY KEY,day TEXT NOT NULL,cny REAL NOT NULL,
            source TEXT NOT NULL,time TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS workspace_writes(id TEXT PRIMARY KEY,time TEXT NOT NULL,
            persona TEXT NOT NULL,scope TEXT NOT NULL,rel TEXT NOT NULL,chars INTEGER NOT NULL,
            sha256 TEXT NOT NULL,previous_sha256 TEXT,previous_text TEXT,task_id TEXT,session_id TEXT,
            review TEXT NOT NULL DEFAULT 'pending',review_time TEXT,reviewer TEXT,note TEXT,source_key TEXT UNIQUE);
        ''')
        defaults = {'instance_id': str(uuid.uuid4()), 'paused': True, 'revision': 0,
                    'correction': '', 'correction_ack_revision': -1, 'stream_seen': False,
                    'cancel_epoch':0,'active_request':{},'correction_response':{},'budget_hold':False,
                    'parallel_enabled':False,'parallel_confirmation':{},'owner_question':{}}
        for key, value in defaults.items():
            self.db.execute('INSERT OR IGNORE INTO state VALUES(?,?)', (key, json.dumps(value)))
        self.db.commit()
        self.identity = PersonaBoundary(self)
        if 'target' not in {r[1] for r in self.db.execute('PRAGMA table_info(tasks)')}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN target TEXT NOT NULL DEFAULT 'desktop'")
            self.db.commit()
        keyfile = self.data / 'bridge.key'
        if not keyfile.exists():
            with keyfile.open('x', encoding='ascii') as f:
                f.write(secrets.token_urlsafe(48))
        self.bridge_key = keyfile.read_text(encoding='ascii').strip()
        self.authority = BudgetAuthority(self.data / 'budget.sqlite', self.config['timezone'], self.config['stop_at_cny'],
                                        limit_enabled=self.config.get('budget_limit_enabled', True))
        self.desktop_live={'ready':False,'message':'等待专属虚拟机连接'}
        self.cpu_samples=deque()
        self.cpu_previous=None
        # Restart never silently resumes execution or redelivers an uncertain request.
        with self.lock, self.db:
            self.set('paused', True)
            for persona in PERSONAS:
                with self.identity.bind(persona):
                    self.set('active_request', {})
            self.db.execute("UPDATE tasks SET status='uncertain' WHERE status='running'")
            self.db.execute("UPDATE tasks SET status='needs-review' WHERE status IN ('queued','claimed')")
        # A container restart interrupts every in-flight model call, so their
        # reservations never receive a usage receipt. Reconcile them at boot
        # instead of letting the upper bounds stay frozen forever.
        reconciled = self.authority.reconcile()
        if reconciled['count']:
            self.event('budget.reconciled', {
                'count': reconciled['count'], 'released_cny': reconciled['released_cny'],
                'days': reconciled['days'],
                'message': '启动对账：没有用量回执的预留已释放，记录保留'})
        self.event('console.start', {'version': VERSION, 'message': '控制台已启动；执行保持暂停'})
        from space_tools import StorageQuota
        self.storage_quota=StorageQuota(self)
        self.storage_quota.initialize()

    def get(self, key):
        with self.lock:
            if hasattr(self, 'identity'):
                key = self.identity.key(key)
            return json.loads(self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()[0])

    def set(self, key, value):
        with self.lock:
            if hasattr(self, 'identity'):
                key = self.identity.key(key)
            self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def event(self, kind, payload, task_id=None, session_id=None, source_key=None):
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO events(time,kind,task_id,session_id,payload,source_key,persona) VALUES(?,?,?,?,?,?,?)',
                            (now(), kind, task_id, session_id, json.dumps(payload, ensure_ascii=False), source_key, self.identity.actor()))

    def events(self, after=0, limit=300):
        with self.lock:
            rows = self.db.execute('SELECT * FROM events WHERE seq>? AND persona=? ORDER BY seq LIMIT ?', (after, self.identity.actor(), min(limit, 1000))).fetchall()
            return [{**dict(r), 'payload': json.loads(r['payload']), 'instance_id': self.get('instance_id')} for r in rows]

    def task_result(self, task_id):
        with self.lock:
            task=self.db.execute('SELECT persona,status FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task or task['persona']!=self.identity.actor():
                raise Rejected('当前人格没有该任务','TASK_NOT_FOUND')
            row=self.db.execute("SELECT payload FROM events WHERE task_id=? AND persona=? AND kind='task.result' ORDER BY seq DESC LIMIT 1",
                                (task_id,self.identity.actor())).fetchone()
            result=json.loads(row[0]) if row else None
            notice=self.db.execute("SELECT payload FROM events WHERE task_id=? AND persona=? AND kind='task.failure' ORDER BY seq DESC LIMIT 1",(task_id,self.identity.actor())).fetchone()
            if result is not None and notice:
                result={**result,'failure':json.loads(notice[0])['failure']}
            return {'persona':self.identity.actor(),'assistant_name':self.config.get('assistant_name','小鲸鱼'),'status':task['status'],
                    'result':result}

    def memories(self, query='', *, performed_by=None, recorded_by=None, kind=None,
                 source_query='', limit=20, max_chars=12000, include_history=False,
                 memory_id=None):
        """Select across the common pool; provenance filters never grant access."""
        import re
        if not isinstance(query, str) or len(query)>2000 or not isinstance(source_query,str) or len(source_query)>500:
            raise Rejected('记忆检索文字无效或过长')
        if type(limit) is not int or not 1<=limit<=100 or type(max_chars) is not int or not 1<=max_chars<=24000:
            raise Rejected('记忆检索数量或字数超界')
        if type(include_history) is not bool:
            raise Rejected('记忆历史筛选无效')
        if performed_by is not None and performed_by not in PERFORMERS:
            raise Rejected('实际执行者标记无效')
        if recorded_by is not None and recorded_by not in (*PERSONAS, 'unknown'):
            raise Rejected('记录模式标记无效')
        if kind is not None and kind not in KINDS:
            raise Rejected('记忆类型无效')
        if memory_id is not None and (not isinstance(memory_id,str) or not 1<=len(memory_id)<=200):
            raise Rejected('记忆编号无效')
        def pattern(value):
            return '%'+value.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        where=[]; params=[]
        if not include_history and memory_id is None:
            where.append('NOT EXISTS (SELECT 1 FROM memory n WHERE n.supersedes=m.id)')
        for column,value in (('performed_by',performed_by),('recorded_by',recorded_by),('kind',kind),('id',memory_id)):
            if value is not None:
                where.append('m.'+column+'=?');params.append(value)
        if source_query:
            where.append("m.source LIKE ? ESCAPE '\\'");params.append(pattern(source_query))
        tokens=list(dict.fromkeys(re.findall(r'[A-Za-z0-9_]{2,}',query.lower())))
        for word in re.findall(r'[\u4e00-\u9fff]+',query):
            tokens.extend(word[i:i+2] for i in range(len(word)-1))
        tokens=list(dict.fromkeys([query.strip(), *tokens]))[:32] if query.strip() else []
        score='0'; score_args=[]
        if tokens:
            clauses=["m.text LIKE ? ESCAPE '\\'" for _ in tokens]
            where.append('('+' OR '.join(clauses)+')')
            params.extend(pattern(t) for t in tokens)
            score=' + '.join('(CASE WHEN '+c+' THEN '+('8' if i==0 else '1')+' ELSE 0 END)'
                             for i,c in enumerate(clauses))
            score_args=[pattern(t) for t in tokens]
        with self.lock:
            rows=self.db.execute('SELECT m.*, ('+score+') AS relevance FROM memory m '+
                ('WHERE '+' AND '.join(where)+' ' if where else '')+
                'ORDER BY relevance DESC,m.time DESC,m.id DESC LIMIT ?',
                (*score_args,*params,limit)).fetchall()
            result=[]; remaining=max_chars
            for row in rows:
                if remaining<=0:break
                item=dict(row); full=item['text'];item['text']=full[:remaining]
                remaining-=len(item['text'])
                item['text_truncated']=len(item['text'])<len(full)
                item['text_total_chars']=len(full)
                item['performed_by_label']=PERFORMERS.get(item['performed_by'],'未知')
                item['recorded_by_label']=PERSONAS.get(item['recorded_by'],'未知')
                result.append(item)
            return result

    def add_memory(self, body, source='owner'):
        if source == 'owner' and body.get('persona', self.identity.active()) != self.identity.active():
            raise Rejected('人格已切换，请确认后重新保存')
        kind, text = body.get('kind'), str(body.get('text', '')).strip()
        if kind not in KINDS or not text or len(text) > 12000:
            raise Rejected('请选择记忆类型并填写内容（最多12000字）')
        mid = str(uuid.uuid4())
        recorder = self.identity.actor()
        with self.lock, self.db:
            self.check_storage()
            previous = body.get('supersedes')
            old = None
            if previous:
                old = self.db.execute('SELECT * FROM memory WHERE id=?', (previous,)).fetchone()
                if not old:
                    raise Rejected('待修订记忆不存在')
                if self.db.execute('SELECT 1 FROM memory WHERE supersedes=?', (previous,)).fetchone():
                    raise Rejected('该记忆已有修订，请先读取最新版本')
            performer=body.get('performed_by',old['performed_by'] if old else 'unknown')
            action_id=body.get('action_id',old['action_id'] if old else None)
            if performer not in PERFORMERS:
                raise Rejected('实际执行者须为表、里、主人、外部来源或未知')
            task_id=old['task_id'] if old else None
            performed_at=old['performed_at'] if old else None
            if old and performer!=old['performed_by'] and 'action_id' not in body:
                if source.startswith('assistant:'):
                    raise Rejected('更改实际执行者须引用新的匹配行动回执，或由主人明确更正')
                action_id=None;task_id=None;performed_at=None
            if action_id is not None:
                if not isinstance(action_id,str) or not 1<=len(action_id)<=200:
                    raise Rejected('行动回执编号无效')
                receipt=self.db.execute('SELECT persona,task_id,time FROM activities WHERE id=?',(action_id,)).fetchone()
                if not receipt or receipt['persona'] not in PERSONAS:
                    raise Rejected('实际执行者需有可核对的行动回执')
                if performer not in ('unknown',receipt['persona']):
                    raise Rejected('实际执行者与行动回执不一致；不能改写表里归属')
                performer=receipt['persona'];task_id=receipt['task_id'];performed_at=receipt['time']
            elif (source.startswith('assistant:') and performer in PERSONAS
                  and not (old and performer==old['performed_by'])):
                raise Rejected('标注表或里实际完成需提供action_id；仅记录或阅读请标unknown')
            if old and performer!=old['performed_by'] and 'action_id' not in body:
                task_id=None;performed_at=None
            created=now()
            self.db.execute('''INSERT INTO memory(id,kind,text,source,time,supersedes,persona,
                recorded_by,performed_by,action_id,task_id,performed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (mid,kind,text,source,created,previous,recorder,recorder,performer,action_id,task_id,performed_at))
            self.event('memory.saved', {'id':mid,'kind':kind,'source':source,
                       'recorded_by':recorder,'performed_by':performer,'action_id':action_id})
        return {'id':mid,'recorded_by':recorder,'performed_by':performer,'source':source,
                'time':created,'supersedes':previous,'action_id':action_id,'task_id':task_id,
                'performed_at':performed_at}

    def persona(self):
        pointer = Path(self.config['persona_pointer'])
        if not pointer.is_absolute():
            pointer = self.root / pointer
        try:
            raw = pointer.read_bytes()
            value = json.loads(raw.decode('utf-8-sig'))
            package = (pointer.parent / value['loadable_package']).resolve()
            package.relative_to(pointer.parent.resolve())
            data = package.read_bytes()
            return {'available': True, 'version': value['version'], 'default': value.get('default_persona'),
                    'inner_enabled': value.get('inner_persona_enabled', False), 'pointer': str(pointer),
                    'package': str(package), 'pointer_sha256': hashlib.sha256(raw).hexdigest(),
                    'package_sha256': hashlib.sha256(data).hexdigest(),
                    'active_version':self.config['runtime'].get('persona_version'),
                    'loaded_into_runtime': self.config['runtime'].get('persona_sha256')==hashlib.sha256(data).hexdigest()}
        except (OSError, ValueError, KeyError):
            return {'available': False, 'message': '人格入口缺失或格式有误，未替换原件'}

    def storage(self):
        from space_tools import storage_usage
        return storage_usage(self.root,self.data,self.config)

    # 主人 2026-10-24 提速：整棵目录（约33万个文件）每次都要遍历，机械盘忙时一次可达1秒以上，
    # 而每一步要查两三次。10秒内、且每项限额都还剩2GiB以上时复用上次结果；接近任何限额就每次重扫。
    STORAGE_CACHE_SECONDS = 10.0
    STORAGE_CACHE_MARGIN = 2 * 2**30

    def _storage_for_check(self):
        if 'storage' in vars(self):  # an instance override (test/diagnostic stub) is always asked fresh
            return self.storage()
        c = self.config
        cached = getattr(self, '_storage_cache', None)
        if cached and time.monotonic() - cached[0] < self.STORAGE_CACHE_SECONDS:
            s, margin = cached[1], self.STORAGE_CACHE_MARGIN
            if (s.get('scan_complete', True) and s['free_bytes'] - c['min_free_gb'] * 2**30 > margin
                    and c['max_space_gb'] * 2**30 - s['used_bytes'] > margin
                    and c['history_quota_gb'] * 2**30 - s['history_bytes'] > margin):
                return s
        s = self.storage()
        self._storage_cache = (time.monotonic(), s)
        return s

    def check_storage(self):
        s = self._storage_for_check()
        c = self.config
        if (not s.get('scan_complete',True) or s['free_bytes'] < c['min_free_gb'] * 2**30 or s['used_bytes'] >= c['max_space_gb'] * 2**30
                or s['history_bytes'] >= c['history_quota_gb'] * 2**30):
            self.set('paused', True)
            self.db.commit()
            raise Rejected('存储达到限制：已暂停写入与执行，请导出或扩容；不会清除历史','STORAGE_LIMIT')

    @staticmethod
    def _window_clock(value):
        try:
            hour, minute = str(value).split(':')[:2]
            return dt.time(int(hour), int(minute))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _window_inside(start, end, at):
        # A range whose end is earlier than its start wraps over midnight.
        return (start <= at < end) if start <= end else (at >= start or at < end)

    def autonomy_window_active(self):
        """True when the configured autonomy window includes the local time."""
        window = self.config.get('autonomy_window') or {}
        if window.get('enabled', True) is not True:
            return False
        windows = []
        ranges = window.get('ranges')
        if isinstance(ranges, list) and ranges:
            for item in ranges:
                if not isinstance(item, dict):
                    continue
                start, end = self._window_clock(item.get('start')), self._window_clock(item.get('end'))
                if start is not None and end is not None:
                    windows.append((start, end))
        else:
            start, end = self._window_clock(window.get('start')), self._window_clock(window.get('end'))
            if start is not None and end is not None:
                windows.append((start, end))
        if not windows:
            windows = [(dt.time(9, 0), dt.time(23, 0))]
        try:
            local = dt.datetime.now(ZoneInfo(self.config['timezone'])).time()
        except Exception:
            local = dt.datetime.now(dt.timezone.utc).time()
        return any(self._window_inside(start, end, local) for start, end in windows)

    def _owner_idle_seconds(self):
        """Linux 上“空闲”的含义：距主人上一次非自主任务的时间。

        服务器没有物理键鼠，Win32 的 GetLastInputInfo 没有对应信号；自主探索
        另由 config.autonomy_window 限定时段，窗口外返回 0，让既有门直接拒绝
        新的自主步骤。
        """
        if not self.autonomy_window_active():
            return 0
        with self.lock:
            row = self.db.execute(
                "SELECT MAX(time) FROM tasks WHERE request_key NOT LIKE '%:autonomy-%'").fetchone()
        try:
            last = dt.datetime.fromisoformat(row[0]).timestamp() if row and row[0] else 0
        except ValueError:
            last = 0
        return max(0, time.time() - last) if last else 10 ** 9

    def _linux_resources(self):
        """Linux counterpart of the Win32 idle/memory/CPU probe."""
        try:
            available = 0
            with open('/proc/meminfo', encoding='ascii') as handle:
                for line in handle:
                    if line.startswith('MemAvailable:'):
                        available = int(line.split()[1])
                        break
            with open('/proc/stat', encoding='ascii') as handle:
                row = handle.readline().split()
            idle, total = int(row[4]), sum(int(value) for value in row[1:])
            stamp = time.monotonic()
            with self.lock:
                if not self.cpu_previous or stamp - self.cpu_previous[0] >= 0.5:
                    if self.cpu_previous:
                        _, prev_idle, prev_total = self.cpu_previous
                        delta = total - prev_total
                        if delta > 0:
                            self.cpu_samples.append((stamp, 100 * (1 - (idle - prev_idle) / delta)))
                    self.cpu_previous = (stamp, idle, total)
                window = self.config['cpu_window_seconds']
                while self.cpu_samples and self.cpu_samples[0][0] < stamp - window - 3:
                    self.cpu_samples.popleft()
                stable = bool(self.cpu_samples and stamp - self.cpu_samples[0][0] >= window
                              and all(value < self.config['max_cpu_percent'] for _, value in self.cpu_samples))
                percent = round(self.cpu_samples[-1][1], 1) if self.cpu_samples else None
            return {'known': True, 'idle_seconds': self._owner_idle_seconds(),
                    'available_ram_gb': round(available / 2 ** 20, 2),
                    'cpu_percent': percent, 'cpu_window_ready': stable}
        except (OSError, ValueError, KeyError, IndexError):
            return {'known': False}

    def resources(self):
        if os.name != 'nt':
            return self._linux_resources()
        class LASTINPUT(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]
        class MEMORY(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        info = LASTINPUT(ctypes.sizeof(LASTINPUT), 0)
        mem = MEMORY(); mem.dwLength = ctypes.sizeof(mem)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)) or not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return {'known': False}
        idle = ((ctypes.windll.kernel32.GetTickCount() & 0xffffffff) - info.dwTime) & 0xffffffff
        idle_time=ctypes.c_ulonglong();kernel=ctypes.c_ulonglong();user=ctypes.c_ulonglong()
        stamp=time.monotonic()
        with self.lock:
            if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle_time),ctypes.byref(kernel),ctypes.byref(user)):
                current=(stamp,idle_time.value,kernel.value+user.value)
                # 多个线程几乎同时调用时，两次读数可能只隔几毫秒，算出的占用率是噪声（实测出现假的 50%），
                # 会让"整个窗口都低于门槛"永远不成立。间隔不足 0.5 秒的读数不记样本，也不替换基线。
                if not self.cpu_previous or stamp-self.cpu_previous[0]>=0.5:
                    if self.cpu_previous:
                        _,prev_idle,prev_total=self.cpu_previous
                        total=current[2]-prev_total
                        if total>0:self.cpu_samples.append((stamp,100*(1-(current[1]-prev_idle)/total)))
                    self.cpu_previous=current
            while self.cpu_samples and self.cpu_samples[0][0]<stamp-self.config['cpu_window_seconds']-3:
                self.cpu_samples.popleft()
            stable=bool(self.cpu_samples and stamp-self.cpu_samples[0][0]>=self.config['cpu_window_seconds'] and all(v<self.config['max_cpu_percent'] for _,v in self.cpu_samples))
            percent=round(self.cpu_samples[-1][1],1) if self.cpu_samples else None
        return {'known': True, 'idle_seconds': idle / 1000, 'available_ram_gb': round(mem.ullAvailPhys / 2**30, 2),
                'cpu_percent':percent,'cpu_window_ready':stable}

    def budget(self):
        day = dt.datetime.now(ZoneInfo(self.config['timezone'])).date().isoformat()
        with self.lock:
            with self.authority.connect() as ledger:
                spent = ledger.execute('SELECT COALESCE(SUM(cost),0) FROM entries WHERE day=?', (day,)).fetchone()[0] / 1e9
                held = ledger.execute("SELECT COALESCE(SUM(upper_bound),0) FROM reservations WHERE day=? AND state='held'", (day,)).fetchone()[0] / 1e9
        return {'day': day, 'local_recorded_cny': spent, 'daily_budget_cny': self.config['daily_budget_cny'],
                'stop_at_cny': self.config['stop_at_cny'], 'shared_ledger_verified': self.config['runtime']['budget_source_ready'],
                'held_cny': held, 'limit_enabled': bool(self.config.get('budget_limit_enabled', True)),
                'note': '本空间保守峰值估算与预留，不代表供应商账单或其他DSH消费'}

    def _active_for(self, persona):
        with self.identity.bind(persona):
            return self.get('active_request')

    def persona_panes(self):
        # Read-only per-persona view for the two conversation panes. Kept cheap:
        # no observer or device probing here (those own their own timeouts).
        panes = {}
        for p, label in PERSONAS.items():
            rows = [dict(r) for r in self.db.execute('SELECT * FROM tasks WHERE persona=? ORDER BY time DESC LIMIT 40', (p,))]
            latest = rows[0] if rows else None
            if latest and latest['status'] in ('stopped', 'uncertain', 'needs-review'):
                result = self.db.execute(
                    "SELECT payload FROM events WHERE task_id=? AND persona=? AND kind='task.result' ORDER BY seq DESC LIMIT 1",
                    (latest['id'], p)).fetchone()
                if result:
                    payload = json.loads(result['payload'])
                    latest['failure'] = payload.get('failure') or {}
                    latest['stop_source'] = payload.get('stop_source')
            panes[p] = {'id': p, 'label': label, 'session_id': self.identity.runtime(p).get('session_id'),
                        'latest_task': latest,
                        'pending': sum(1 for r in rows if r['status'] in ('queued', 'claimed', 'running', 'uncertain')),
                        'tasks': rows}
        return panes

    def bridge_action(self, action, body):
        if action in ('reasoning/resolve','reasoning/request'):
            from reasoning_policy import resolve, record_request
            return (resolve if action=='reasoning/resolve' else record_request)(self,body)
        if action == 'gate':
            from acceptance import model_gate
            controlled = model_gate(self, body)
            if controlled is not None:
                return controlled
            persona=next((p for p in PERSONAS if self.identity.runtime(p).get('session_id')==body.get('session_id')), None)
            if persona is None:
                raise Rejected('Unknown session')
            # 主人 2026-10-24 提速：观察状态是一次本机 HTTP 往返（超时 8 秒），先在全局锁外取到，
            # 不让每一步都拿着全局锁等它；下面的判定逻辑与原来完全相同。
            with self.identity.bind(persona):
                observe=self.observer_status()
            with self.lock, self.identity.bind(persona):
                active=self.get('active_request')
                sid=self.identity.runtime()['session_id']
                if self.config.get('owner_hold',{}).get('paused') or not self.config.get('dispatch_enabled',False):
                    raise Rejected('主人要求保持执行关闭','OWNER_HOLD')
                if not sid or body.get('session_id')!=sid or not active or active.get('persona')!=persona or (not self.get('parallel_enabled') and persona!=self.identity.active()) or active.get('session_id')!=sid or active.get('revision')!=self.get('revision') or active.get('cancel_epoch')!=self.get('cancel_epoch'):
                    raise Rejected('活动未授权或已暂停/纠正；不得开始新步骤')
                if active.get('autonomy') and (self.get('paused') or not self.config.get('autonomy_enabled',False)):
                    raise Rejected('自主活动保持关闭')
                if active.get('autonomy') and self.resources().get('idle_seconds',0)<self.config['idle_seconds']:
                    if not self.autonomy_window_active():
                        raise Rejected('已到设定的自主时段结束时间，本轮停止新步骤','AUTONOMY_WINDOW_CLOSED')
                    raise Rejected('主人已重新使用电脑，停止新的自主步骤')
                if body.get('phase') == 'device_step' and active.get('target','desktop') not in (body.get('device','desktop'),'assistant'):
                    raise Rejected('This dialogue task has no device lease')
                if active.get('target')=='correction' and body.get('phase') not in ('model_step','correction_ack'):
                    raise Rejected('Correction acknowledgement cannot run device or mutation tools')
                if active.get('target')=='storage':
                    self.storage_quota.check_consult(active)
                    if body.get('phase') not in ('model_step','storage_request') or int(body.get('step',0))>2:
                        raise Rejected('容量专用任务只允许两次模型请求及容量查询/提案，不能写作品或操作设备')
                # 主人 2026-10-24 决定：自主轮次与主人任务一样可用第一步启动通道。每个任务都开新会话，
                # 新会话在首次模型请求前观察必然是 MODEL_UNKNOWN，否则自主永远起不来。只放行第 0/1 步
                # 的 model_step；设备、记忆和工作区动作仍须真实的新鲜观察。
                first_model_step=(body.get('phase')=='model_step'
                                  and 0<=int(body.get('step',99))<=1 and self.initial_model_observation(observe))
                if not first_model_step and body.get('phase')=='model_step' and int(body.get('step',99))<=1 and observe.get('code')=='MODEL_UNKNOWN' and observe.get('freshness',{}).get('pendingFrames',0)>0 and observe.get('freshness',{}).get('backlogAgeMs',5000)<5000:
                    raise Rejected('首轮观察事件正在提交','OBSERVATION_STARTING')
                if not self.observation_fresh(observe) and not first_model_step:
                    raise Rejected('真实思考观察链路不可用或请求暂停，已阻止后续步骤')
                # Recheck after observation and after a device lease has waited
                # for its lock. A prior model reservation is not a live quota
                # check. Only the two explicit storage proposal tools use the
                # separate storage_request phase; no general tool is exempt.
                if body.get('phase') in ('device_step','space_tool','memory_write'):
                    self.check_storage()
                return {'ok':True,'revision':self.get('revision'),'task_id':active['task_id']}
        if action == 'budget/reserve':
            consult=self.storage_quota.consult_for_consumer(body.get('consumer'))
            if consult:
                self.bridge_action('gate',{'session_id':consult['session_id'],'phase':'model_step','step':1})
            else:self.check_storage()
            if self.get('budget_hold'):
                raise Rejected('历史请求超出预留上界，等待人工核对','BUDGET_BOUND_HOLD')
            from acceptance import reserve
            controlled = reserve(self, body)
            if controlled is not None:
                return controlled
            if self.config.get('owner_hold',{}).get('paused'):
                raise Rejected('主人要求保持执行关闭','OWNER_HOLD')
            if not self.config.get('dispatch_enabled',False):
                raise Rejected('任务派发关闭','DISPATCH_DISABLED')
            if not self.config['runtime']['budget_source_ready']:
                raise Rejected('本空间预算权威未启用','BUDGET_NOT_READY')
            if consult:self.storage_quota.reserve_consult_slot(consult,body.get('id'))
            return self.authority.reserve(body['id'], body['upper_cny'], body['consumer'])
        if action == 'budget/reconcile':
            result=self.authority.reconcile(body.get('id'))
            if result.get('released_cny'):
                self.event('budget.reconciled',{'request_id':body.get('id'),'released_cny':result['released_cny'],
                    'message':'本轮请求没有用量回执，预留已对账释放，记录保留'})
            return result
        if action == 'budget/cancel_unstarted':
            result=self.authority.cancel_unstarted(body['id'], body.get('confirmed_not_sent'))
            self.event('budget.cancelled',{'request_id':body['id'],'confirmed_not_sent':True})
            return result
        if action == 'budget/settle':
            result = self.authority.settle(body['id'], body['actual_cny'])
            if result.get('bound_violation'):
                with self.lock,self.db:
                    self.set('budget_hold',True)
                    self.set('paused',True)
                    self.set('cancel_epoch',self.get('cancel_epoch')+1)
                    self.event('budget.bound_violation',{'request_id':body['id'],'message':'实际估算超过预留，已如实结算并停止后续消费，等待人工核对'})
            self.event('budget.settled', {'request_id': body['id'], 'estimated_cny': body['actual_cny'], 'usage': body.get('usage')}, source_key='cost:'+body['id'])
            return result
        raise Rejected('未知运行桥操作')

    def observer_status(self, sid=None):
        sid=sid or self.identity.runtime().get('session_id')
        if not sid:
            return {'canObserve':False,'requiresPause':True}
        try:
            with observer_open(self.root,'/api/thinking/status?sessionId='+sid,timeout=8) as r:
                value=json.load(r)
            if value.get('sessionId')!=sid:
                raise ValueError('wrong observer session')
            return value
        except (OSError,ValueError):
            return {'canObserve':False,'requiresPause':True,'error':'思考观察接口暂不可用'}

    @staticmethod
    def observation_fresh(value):
        # Missing freshness evidence must not silently mean fresh. The sole
        # collector owns clock/skew/backlog interpretation and raw frame parsing.
        return (value.get('canObserve') is True and value.get('requiresPause') is False
                and value.get('freshness', {}).get('ready') is True)

    def initial_model_observation(self, value):
        # A fresh session cannot report lastUsed until its first model request.
        # Only the first model step of a dispatched turn (owner task or, since
        # 2026-10-24 by owner decision, an autonomy turn) may use this bootstrap.
        # Device/memory mutations still require actual model evidence and a
        # fresh committed stream.
        llm=owner_settings.model_values(self)
        fresh=value.get('freshness',{})
        return (value.get('code')=='MODEL_UNKNOWN' and value.get('registered') is True
                and value.get('activeBinding') is True and value.get('transport')=='connected'
                and value.get('expectedModel')==llm['model']
                and value.get('pendingModel')==llm['model']
                and value.get('pendingProvider')==llm['provider']
                and fresh.get('pendingFrames')==0 and fresh.get('backlogAgeMs')==0)

    def mcp(self, request):
        method, params, rid = request.get('method'), request.get('params', {}), request.get('id')
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'dsh-exclusive-space', 'version': VERSION}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            def tool(name, desc, properties, required=()):
                return {'name': name, 'description': desc, 'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(required)}}
            result = {'tools': [
                tool('memory_recall', '按任务选择读取表里共用记忆；performed_by是实际执行者，recorded_by是记录模式。来源筛选不是读取权限；读取不改变归属。默认最多20条/12000字，可按id读取原文及选择历史版本。', {
                    'query':{'type':'string','maxLength':2000},
                    'performed_by':{'type':'string','enum':list(PERFORMERS)},
                    'recorded_by':{'type':'string','enum':[*PERSONAS,'unknown']},
                    'kind':{'type':'string','enum':sorted(KINDS)},
                    'source_query':{'type':'string','maxLength':500},
                    'limit':{'type':'integer','minimum':1,'maximum':100},
                    'max_chars':{'type':'integer','minimum':1,'maximum':24000},
                    'include_history':{'type':'boolean'},'memory_id':{'type':'string','maxLength':200}}),
                tool('memory_remember', '保存到表里共用记忆；记录者由服务器绑定，不能指定。实际执行者默认unknown；标注表/里实际完成需引用action_log_read返回的action_id，不能把读到或记录的事冒充自己完成。', {
                    'kind':{'type':'string','enum':sorted(KINDS)},'text':{'type':'string','maxLength':12000},
                    'source_note':{'type':'string','maxLength':1000},
                    'performed_by':{'type':'string','enum':list(PERFORMERS)},
                    'action_id':{'type':'string','maxLength':200}}, ('kind','text','source_note')),
                tool('memory_revise', '修订共用记忆，保留旧原文/来源/时间及修订链；新记录者为当前模式，默认继承原实际执行者。更改执行归属必须提供相符行动回执。', {
                    'id':{'type':'string','maxLength':200},'kind':{'type':'string','enum':sorted(KINDS)},
                    'text':{'type':'string','maxLength':12000},'source_note':{'type':'string','maxLength':1000},
                    'performed_by':{'type':'string','enum':list(PERFORMERS)},
                    'action_id':{'type':'string','maxLength':200}}, ('id','kind','text','source_note')),
                tool('space_status', '读取本空间实际桌面、预算、暂停状态；未连接不代表已操作。', {})
                ,tool('action_log_read','读取双方简短实际行动回执，保留执行模式、任务和时间；回执id可用于memory_remember的action_id。共用长期记忆用memory_recall按需读取，完整对话/思考仍按会话归档。',{'other_only':{'type':'boolean'},'limit':{'type':'integer','minimum':1,'maximum':100}})
                ,tool('reasoning_level','查看或选择当前任务后续请求的思考强度。新任务默认low；工具故障自动升high，再有问题升max。主动升降须说明具体原因；不会改变正在进行的请求，不切模型、不绕过预算或观察门。QQ已退役，不提供off渠道。',{
                    'operation':{'type':'string','enum':['status','select']},
                    'level':{'type':'string','enum':['auto','low','high','max']},
                    'reason':{'type':'string','maxLength':400}})
                ,tool('correction_ack','确认主人当前纠正后的新理解与下一步；不会恢复活动，仍等主人允许。',{'revision':{'type':'integer'},'understanding':{'type':'string'},'next_step':{'type':'string'}},('revision','understanding','next_step'))
                ,tool('desktop_screen','读取专属Windows的真实画面；绝不读取宿主桌面。',{})
                ,tool('desktop_click','在最近画面的原始像素坐标点击一次；只能操作专属虚拟机。',{'x':{'type':'integer','minimum':0},'y':{'type':'integer','minimum':0}},('x','y'))
                ,tool('desktop_type','向专属虚拟机当前输入框粘贴文字；禁止未经允许安装或向他人发送。',{'text':{'type':'string','maxLength':12000}},('text',))
                ,tool('desktop_key','按专属虚拟机允许的快捷键。',{'key':{'type':'string','enum':['ENTER','ESC','TAB','BACKSPACE','CTRL+L','CTRL+A','CTRL+C','CTRL+V','CTRL+T','ALT+LEFT','WIN']}},('key',))
                ,tool('desktop_scroll','在专属Windows滚动，正数向上、负数向下。',{'amount':{'type':'integer','minimum':-10,'maximum':10}},('amount',))
                ,tool('owner_ask','需要主人提供手机号、验证码、账号密码，或需要主人做一个明确决定时，把问题提交到控制台「需要你决定」。提交后本轮即可结束；不要假设答案，也不要自己编造凭据。',
                    {'question':{'type':'string','maxLength':2000},
                     'options':{'type':'array','items':{'type':'string','maxLength':200},'maxItems':6},
                     'kind':{'type':'string','enum':['input','choice']}},('question',))
            ]}
            from space_tools import TOOLS
            result['tools'].extend(TOOLS)
            if self.identity.actor()=='li':
                # 自动审查只对里人格开放；表人格的工具列表里不出现这些名字。
                from space_tools import REVIEW_TOOLS
                result['tools'].extend(REVIEW_TOOLS)
            from context_archive_tools import TOOLS as ARCHIVE_TOOLS
            result['tools'].extend(ARCHIVE_TOOLS)
            if os.name == 'nt':
                # phone_* 是 Windows 模拟器的旧适配器；Linux 空间实际走
                # her-android 的 android_*。不要在 Linux 工具列表里暴露
                # phone_*，否则模型会反复调用并卡在 ModuleNotFoundError。
                try:
                    from phone_tools import TOOLS as PHONE_TOOLS
                    result['tools'].extend(PHONE_TOOLS)
                except ImportError: pass
            from her_tools import TOOLS as HER_TOOLS
            result['tools'].extend(HER_TOOLS)
            from her_android import TOOLS as ANDROID_TOOLS
            result['tools'].extend(ANDROID_TOOLS)
            from github_tools import TOOLS as GITHUB_TOOLS
            result['tools'].extend(GITHUB_TOOLS)
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            try:
                if name.startswith('phone_') and os.name != 'nt':
                    raise Rejected('Linux 空间没有 phone_* 工具；请使用 android_*（android_view/android_info/android_ui/android_tap/android_swipe/android_type/android_key/android_app）','PHONE_TOOL_UNAVAILABLE')
                from space_tools import validate_arguments
                known=self.mcp({'method':'tools/list','id':'validate'})['result']['tools']
                definition=next((item for item in known if item['name']==name),None)
                if not definition:raise Rejected('Unknown tool')
                validate_arguments(definition['inputSchema'],args)
                active=self.get('active_request') or {}
                if active.get('target')=='storage' and name not in ('storage_status','storage_request_expansion'):
                    raise Rejected('当前是受限容量评估任务，只能查询空间或提出扩容申请')
                if name == 'memory_recall':
                    value = self.memories(**args)
                elif name in ('context_recall','context_read'):
                    self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'space_tool'})
                    from context_archive_tools import call as archive_call
                    value=archive_call(self,name,args)
                elif name == 'reasoning_level':
                    from reasoning_policy import tool
                    value = tool(self,args)
                elif name in ('memory_remember', 'memory_revise'):
                    self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'memory_write'})
                    note = str(args.get('source_note','')).strip()
                    if not note or len(note) > 1000:
                        raise Rejected('记忆必须附来源说明')
                    body = {'kind':args.get('kind'),'text':args.get('text')}
                    for field in ('performed_by','action_id'):
                        if field in args:body[field]=args[field]
                    if not args.get('action_id') and web_source_note(note):
                        body['performed_by'] = 'external'
                    if name == 'memory_revise':
                        body['supersedes'] = args['id']
                    value = self.add_memory(body, source='assistant:'+note)
                elif name == 'space_status':
                    value = self.model_status()
                elif name in ('storage_status','storage_request_expansion'):
                    # Only the small proposal/status route bypasses quota-write
                    # checks. Owner hold, active task/persona/epoch and fresh
                    # native observation remain required by the normal gate.
                    self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'storage_request'})
                    value=(self.storage_quota.status() if name=='storage_status' else self.storage_quota.request(args))
                elif name == 'action_log_read':
                    value = self.identity.actions(args.get('other_only', False), args.get('limit', 50))
                elif name == 'correction_ack':
                    self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'correction_ack'})
                    with self.lock,self.db:
                        if not self.get('correction') or args.get('revision')!=self.get('revision'):
                            raise Rejected('纠正版本不匹配，不能确认旧认识')
                        understanding=str(args.get('understanding','')).strip(); next_step=str(args.get('next_step','')).strip()
                        if not understanding or not next_step or len(understanding)+len(next_step)>12000:
                            raise Rejected('请说明新的理解与下一步')
                        self.set('correction_ack_revision',self.get('revision'))
                        value={'understanding':understanding,'next_step':next_step,'revision':self.get('revision'),'resumed':False}
                        self.set('correction_response',value)
                        self.event('correction.acknowledged',value)
                elif name in ('workspace_list','workspace_read','workspace_write','workspace_review_list','workspace_read_diff','workspace_review_decide','pet','web_fetch','web_search','image_capabilities','image_create_svg'):
                    if (name.startswith('workspace_review_') or name=='workspace_read_diff') and self.identity.actor()!='li':
                        raise Rejected('审查工具只对里人格开放')
                    self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'space_tool'})
                    from space_tools import call
                    value=call(self,name,args)
                elif name.startswith('github_'):
                    self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'space_tool'})
                    from github_tools import invoke as github_invoke
                    value=github_invoke(self, name, args)
                elif name.startswith('phone_'):
                    self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'device_step','device':'phone'})
                    from phone_tools import invoke
                    from device_lease import lease
                    with lease(self,'phone',name): value=invoke(self,name,args)
                    if value.get('image'):
                        return {'jsonrpc':'2.0','id':rid,'result':self.tool_data_result({'content':[
                            {'type':'text','text':json.dumps({k:v for k,v in value.items() if k!='image'},ensure_ascii=False)},
                            {'type':'image','data':value['image'],'mimeType':value.get('mimeType','image/png')}]})}
                elif name.startswith('desktop_'):
                    if self.get('correction') and self.get('paused'):
                        raise Rejected('纠正后的活动尚待主人允许继续')
                    self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'device_step','device':'desktop'})
                    import desktop
                    action={'desktop_screen':'screen','desktop_click':'click','desktop_type':'type','desktop_key':'key','desktop_scroll':'scroll'}.get(name)
                    if not action:
                        raise Rejected('Unknown desktop action')
                    from device_lease import lease
                    with lease(self,'desktop',name):
                        try:
                            budget=self.budget()
                            if (self.get('budget_hold') or not budget['shared_ledger_verified']
                                    or (budget['limit_enabled']
                                        and budget['local_recorded_cny']+budget['held_cny']>=budget['stop_at_cny'])):
                                raise Rejected('预算检查未通过，专属电脑尚未按需启动','BUDGET_BLOCKED')
                            task_id=self.get('active_request')['task_id']
                            if not self.desktop_live.get('ready') or getattr(self,'_desktop_prepared_task_id',None)!=task_id:
                                desktop.prepare(self,allow_start=True)
                                self._desktop_prepared_task_id=task_id
                            # Preparation may wait for a guest connection. Recheck
                            # cancellation/observation before the requested action.
                            self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],'phase':'device_step','device':'desktop'})
                            after=desktop.screen_after_seconds(self.config,'desktop') if action!='screen' else None
                            response=desktop.call(self.root,action,**args,**({'screen_after':after} if after is not None else {}))
                        except Rejected as error:
                            # These gates run before the requested action. Any
                            # completed preparation already has full receipts.
                            error.result={'uncertain':False}
                            raise
                        except desktop.DesktopError as error:
                            if error.result.get('uncertain'):
                                with self.lock,self.db:
                                    self.set('paused',True);self.set('cancel_epoch',self.get('cancel_epoch')+1)
                                    self.db.execute("UPDATE tasks SET status='uncertain' WHERE id=?",(self.get('active_request').get('task_id'),))
                            raise
                    self.identity.log('desktop.'+action, '已完成专属桌面'+{'screen':'截图','click':'点击','type':'文字输入','key':'按键','scroll':'滚动'}[action]+'，内容留在当前人格私有记录。',
                        self.get('active_request').get('task_id'), self.identity.runtime()['session_id'])
                    if action=='screen':
                        return {'jsonrpc':'2.0','id':rid,'result':self.tool_data_result({'content':[
                            {'type':'text','text':json.dumps({k:v for k,v in response.items() if k!='image'},ensure_ascii=False)},
                            {'type':'image','data':response['image'],'mimeType':'image/jpeg'}]})}
                    frame=response.get('frame') if isinstance(response.get('frame'),dict) else None
                    if frame and isinstance(frame.get('image'),str):
                        receipt={k:v for k,v in response.items() if k!='frame'}
                        receipt['screenAfterAction']={'ok':True,'width':frame.get('width'),'height':frame.get('height'),
                            'capturedAt':frame.get('capturedAt'),'note':'附图是动作后的真实画面，不必再单独调用desktop_screen。'}
                        return {'jsonrpc':'2.0','id':rid,'result':self.tool_data_result({'content':[
                            {'type':'text','text':json.dumps(receipt,ensure_ascii=False)},
                            {'type':'image','data':frame['image'],'mimeType':'image/jpeg'}]})}
                    value=response
                elif name == 'owner_ask':
                    value = self.owner_ask(args)
                elif name.startswith('her_'):
                    try:
                        self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],
                            'phase':'device_step','device':'desktop'})
                        from her_tools import invoke
                        from device_lease import lease
                        with lease(self,'desktop',name):
                            value=invoke(self,name,args)
                    except Rejected as error:
                        error.result={'uncertain':False}
                        raise
                elif name.startswith('android_'):
                    try:
                        from her_android import invoke as android_invoke, AndroidError
                        self.bridge_action('gate',{'session_id':self.identity.runtime()['session_id'],
                            'phase':'device_step','device':'phone'})
                        from device_lease import lease
                        with lease(self,'phone',name):
                            value=android_invoke(self,name,args)
                    except AndroidError as error:
                        rejected = Rejected(str(error), 'ANDROID_FAILED')
                        rejected.result = {'uncertain': error.uncertain}
                        raise rejected from None
                else:
                    raise Rejected('工具不在专属空间允许范围')
                result = {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
            except (Rejected, RuntimeError, ValueError, KeyError, OSError, __import__('subprocess').TimeoutExpired, sqlite3.IntegrityError) as e:
                if (name!='reasoning_level' and not isinstance(e,Rejected)
                    and isinstance(e,(RuntimeError,OSError,__import__('subprocess').TimeoutExpired))):
                    try:
                        from reasoning_policy import record_problem
                        record_problem(self,name)
                    except BaseException as bookkeeping:
                        _internal_error_log('reasoning_problem:'+str(name), bookkeeping)
                result = {'isError':True,'content':[{'type':'text','text':str(e)}]}
            except BaseException as e:
                _internal_error_log('tools/call:'+str(name), e)
                result = {'isError':True,'content':[{'type':'text','text':
                    '专属空间控制台内部错误（'+type(e).__name__+'），已记录到本地控制台日志；'
                    '该工具未完成，效果未确认，不要按成功处理'}]}
            result=self.tool_data_result(result)
        elif method and method.startswith('notifications/'):
            return None
        else:
            return {'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'Method not found'}}
        return {'jsonrpc':'2.0','id':rid,'result':result}

    @staticmethod
    def tool_data_result(result):
        authority=data_authority()
        result={**result,**authority,'_meta':{**result.get('_meta',{}),**authority}}
        blocks=[];authority_visible=False
        for original in result.get('content',[]):
            block={**original,'_meta':{**original.get('_meta',{}),**authority}}
            if block.get('type')=='text':
                try:
                    value=json.loads(block['text'])
                    if isinstance(value,dict):
                        block['text']=json.dumps({**value,**authority},ensure_ascii=False);authority_visible=True
                except (ValueError,TypeError,KeyError):pass
            blocks.append(block)
        # Keep list/plain-text business values intact and image attachments in
        # place; give the model a separate explicit, server-generated notice.
        if not authority_visible:
            blocks.append({'type':'text','text':json.dumps(authority,ensure_ascii=False),'_meta':dict(authority)})
        result['content']=blocks
        return result

    def model_status(self):
        """Explicit model-safe view; owner CSRF/nonces never become tool data."""
        value=self.status()
        safe={k:value.get(k) for k in ('version','paused','active_persona','model','autonomy_enabled',
             'dispatch_enabled','parallel_enabled','memory_count','blockers','context_policy')}
        fields={'runtime':('ready','chat_ready','budget_source_ready','session_id','persona_version'),
                'desktop':('ready','error','message'),'phone':('ready','error','code','serial','avdName'),
                'budget':('day','local_recorded_cny','daily_budget_cny','stop_at_cny','shared_ledger_verified','held_cny','note'),
                'thinking':('canObserve','requiresPause','code','model','provider','nativeCursor','backlogAgeMs'),
                'storage':('used_bytes','space_root_bytes','phone_data_bytes','phone_data_status','history_bytes','free_bytes',
                     'quota_gib','quota_bytes','minimum_free_gib','minimum_free_bytes','usable_remaining_bytes','volume','unit',
                     'physically_reserved','scan_complete','scan_errors','skipped_links','scope','vm_system_disk_logical_gib',
                     'vm_disk_live_verified','vm_expansion_executed')}
        for name,keys in fields.items():
            item=value.get(name) or {}
            if name=='budget' and item.get('limit_enabled') is False:
                # 预算无上限时不再把日额度和停止线交给模型；否则她每轮都会
                # 按“已越过 19.5”自我限制，只做核对却继续消耗额度。
                item={**item,'note':'预算无上限：只记录实际用量和预留，不据此停止、少做或只做状态核对。'}
                keys=('day','local_recorded_cny','held_cny','shared_ledger_verified','limit_enabled','note')
            safe[name]={key:item[key] for key in keys if key in item}
        return safe

    def blockers(self):
        c = self.config
        items = []
        if c.get('owner_hold',{}).get('paused'):
            items.append('主人要求保持执行关闭')
        if self.get('budget_hold'):
            items.append('费用超出预留上界，等待人工核对')
        # Device readiness is checked by the requested device tool under its lease.
        if not c['runtime']['ready']:
            items.append('模型运行时尚未接入')
        if not c['runtime']['budget_source_ready']:
            items.append('共享预算账本尚未核实')
        if not owner_settings.model_values(self)['model']:
            items.append('模型尚未配置')
        if not self.persona()['available']:
            items.append('人格来源不可用')
        if self.budget()['limit_enabled'] and self.budget()['local_recorded_cny'] >= c['stop_at_cny']:
            items.append('当日费用达到停止线')
        return items

    def status(self):
        with self.lock:
            self.config = json.loads((self.root / 'config.json').read_text(encoding='utf-8-sig'))
            pending = [dict(r) | {'details': json.loads(r['details'])} for r in self.db.execute('SELECT * FROM approvals ORDER BY time DESC')]
            for item in pending:
                if item['kind']=='storage_expand':
                    execution=self.storage_quota._row(item['id'])
                    item.update({k:execution[k] for k in ('phase','error','approved_at','applied_at','applied_from_gib')})
            tasks = [dict(r) for r in self.db.execute('SELECT * FROM tasks WHERE persona=? ORDER BY time DESC LIMIT 100',(self.identity.actor(),))]
            value = {'version': VERSION, 'instance_id': self.get('instance_id'), 'paused': self.get('paused'),
                    'revision': self.get('revision'), 'correction': self.get('correction'),
                    'correction_ack_revision': self.get('correction_ack_revision'),
                    'correction_response': self.get('correction_response'),
                    'reasoning_stream_seen': self.get('stream_seen'), 'runtime': self.identity.runtime(),
                    'active_persona':self.identity.active(), 'view_persona':self.identity.actor(),
                    'personas':[{ 'id':p,'label':label,'session_id':self.identity.runtime(p).get('session_id')} for p,label in PERSONAS.items()],
                    'autonomy_enabled':self.config.get('autonomy_enabled',False),
                    'dispatch_enabled':self.config.get('dispatch_enabled',False),
                    'parallel_enabled':self.get('parallel_enabled'),
                    'parallel_tasks':{p:self._active_for(p) for p in PERSONAS},
                    'persona_panes':self.persona_panes(),
                    'model': owner_settings.model_values(self)['model'], 'desktop': self.config['desktop']|self.desktop_live, 'phone': self.config['phone']|self.phone_live,
                    'persona': self.persona(), 'storage': self.storage(), 'budget': self.budget(),
                    'resources': self.resources(), 'blockers': self.blockers(), 'tasks': tasks, 'approvals': pending,
                    'thinking':None,
                    'memory_count': self.db.execute('SELECT COUNT(*) FROM memory m WHERE NOT EXISTS (SELECT 1 FROM memory n WHERE n.supersedes=m.id)').fetchone()[0],
                    'latest_seq': self.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0],
                    'persona_permissions': persona_policy.describe()}
            if self.identity.actor()=='li':
                try:
                    value['workspace_review']={'pending':self.db.execute("SELECT COUNT(*) FROM workspace_writes WHERE review='pending'").fetchone()[0],
                                               'total':self.db.execute('SELECT COUNT(*) FROM workspace_writes').fetchone()[0]}
                except sqlite3.Error:
                    value['workspace_review']={'pending':0,'total':0}
        # A slow/offline observer must not hold the pause, persona or memory lock.
        from device_lease import device_lease_status
        value['device_lease']=device_lease_status(self)
        value['context_policy']={'new_owner_task':'fresh_native_session',
                                 'current_task':'retain_goal_and_receipts',
                                 'history':'short_index_then_explicit_retrieval',
                                 'mid_task_rotation':'not_implemented',
                                 'default_device':'phone_first_computer_on_demand'}
        value['thinking']=self.observer_status(value['runtime'].get('session_id'))
        return value

    def command(self, action, body):
        if action in ('storage-prepare','storage-approve','storage-deny','storage-retry','storage-consult'):
            with self.lock:
                if body.get('persona',self.identity.actor())!=self.identity.actor():
                    raise Rejected('人格已切换，请重新查看扩容申请')
                if action=='storage-prepare':return self.storage_quota.prepare(body)
                if action=='storage-consult':return self.storage_quota.consult(body)
                if action=='storage-approve':return self.storage_quota.decide(body,True)
                if action=='storage-deny':return self.storage_quota.decide(body,False)
                return self.storage_quota.retry(body)
        if action == 'reconcile':
            if body.get('persona',self.identity.actor())!=self.identity.actor():
                raise Rejected('模式已切换，请重新核对')
            from reconcile import reconcile
            cross_restart=body.get('cross_restart',False)
            if type(cross_restart) is not bool:raise Rejected('跨重启核对参数无效')
            # Host-owner endpoint only. Caller must inspect the former executor;
            # reconciliation still requires matching native receipts and no inflight device.
            return reconcile(self,body.get('id'),cross_restart=cross_restart)
        with self.lock, self.db:
            if action not in ('persona','parallel-prepare','parallel') and body.get('persona', self.identity.actor()) != self.identity.actor():
                raise Rejected('人格已切换，请确认后重新发送')
            if action == 'review_task':
                tid = str(body.get('id') or '')
                row = self.db.execute('SELECT persona,status FROM tasks WHERE id=?', (tid,)).fetchone()
                if not row or row['persona'] != self.identity.actor():
                    raise Rejected('任务不存在或不属于当前人格','TASK_NOT_FOUND')
                if row['status'] not in ('needs-review', 'uncertain'):
                    raise Rejected('只有待复核或待核对的任务可以关闭')
                dispatch = self.db.execute(
                    "SELECT session_id FROM events WHERE task_id=? AND kind='task.dispatch' ORDER BY seq DESC LIMIT 1",
                    (tid,)).fetchone()
                self.db.execute("UPDATE tasks SET status='reviewed' WHERE id=?", (tid,))
                self.event('task.reviewed', {'id': tid, 'from': row['status'],
                    'message': '主人已复核并关闭此任务；不重发'}, tid,
                    dispatch['session_id'] if dispatch else None)
                return {'ok': True, 'id': tid, 'status': 'reviewed'}
            if action == 'parallel-prepare':
                enabled=body.get('enabled')
                if type(enabled) is not bool:
                    raise Rejected('并发模式参数无效')
                challenge={'nonce':secrets.token_urlsafe(24),'enabled':enabled,'expires':time.time()+60}
                self.set('parallel_confirmation',challenge)
                return challenge
            elif action == 'parallel':
                challenge=self.get('parallel_confirmation')
                if (not challenge or challenge.get('expires',0)<time.time()
                    or type(body.get('enabled')) is not bool
                    or body['enabled']!=challenge.get('enabled')
                    or not hmac.compare_digest(str(body.get('nonce','')),challenge.get('nonce',''))):
                    raise Rejected('请重新点击按钮并确认并发模式')
                self.set('parallel_confirmation',{})
                self.set('parallel_enabled',body['enabled'])
                if not body['enabled']:
                    self.set('paused',True)
                    self.set('cancel_epoch',self.get('cancel_epoch')+1)
                self.event('parallel.changed',{'enabled':body['enabled'],'message':'表里并发模式已'+('启用' if body['enabled'] else '关闭')+'；活动仍需单独授权'})
                return {'ok':True,'parallel_enabled':body['enabled'],'autonomy_resumed':False}
            elif action == 'persona':
                target = self.identity.validate(body.get('persona'))
                if not self.get('parallel_enabled') and (self.get('active_request') or self.db.execute("SELECT 1 FROM tasks WHERE status IN ('claimed','running')").fetchone()):
                    raise Rejected('当前一轮尚未确认停止，请先暂停并等停止回执后切换')
                if target == self.identity.active():
                    return {'ok':True,'persona':target,'paused':self.get('paused')}
                parallel=self.get('parallel_enabled')
                if not parallel:
                    self.set('paused',True)
                    self.set('cancel_epoch',self.get('cancel_epoch')+1)
                    self.db.execute("UPDATE tasks SET status='needs-review' WHERE status='queued'")
                self.set('active_persona',target)
                self.identity.publish_binding()
                with self.identity.bind(target):
                    self.event('persona.switched',{'persona':target,'label':PERSONAS[target],
                                                   'message':'已切换到'+PERSONAS[target]+'；私有上下文独立'+('，并发运行不受影响' if parallel else '，活动保持暂停')})
                return {'ok':True,'persona':target,'paused':self.get('paused'),'resumed':not self.get('paused')}
            elif action == 'pause':
                self.db.execute("UPDATE tasks SET status='needs-review' WHERE status='queued'")
                self.set('auto_recover_at', None)
                self.set('paused', True)
                self.set('cancel_epoch',self.get('cancel_epoch')+1)
                self.event('control.pause', {'message': '主人暂停；不再领取新动作，执行器须确认停止'})
            elif action == 'correct':
                text = str(body.get('text', '')).strip()
                if not text or len(text) > 12000:
                    raise Rejected('请填写纠正内容（最多12000字）')
                self.check_storage()
                self.set('auto_recover_at', None)
                self.set('paused', True)
                self.set('cancel_epoch',self.get('cancel_epoch')+1)
                self.db.execute("UPDATE tasks SET status='needs-review' WHERE status='queued'")
                revision = self.get('revision') + 1
                self.set('revision', revision)
                self.set('correction', text)
                self.set('correction_response',{})
                self.add_memory({'kind': 'correction', 'text': text})
                self.event('control.correct', {'text': text, 'revision': revision, 'ack_required': True})
                tid=str(uuid.uuid4())
                prompt='主人提出纠正：'+text+'。当前纠正版本revision='+str(revision)+'。请简要说明纠正后的新理解与下一步，并调用correction_ack确认这个revision。不操作桌面，不安装，不联系他人。确认只记录理解，活动仍保持暂停，等主人允许继续。'
                status = 'queued' if self.config.get('dispatch_enabled',False) and not self.config.get('owner_hold',{}).get('paused') else 'needs-review'
                self.db.execute('INSERT INTO tasks(id,request_key,text,status,time,revision,persona) VALUES(?,?,?,?,?,?,?)',(tid,'correction-'+self.identity.actor()+'-'+str(revision),prompt,status,now(),revision,self.identity.actor()))
                self.db.execute("UPDATE tasks SET target='correction' WHERE id=?",(tid,))
            elif action == 'resume':
                blockers = self.blockers()
                if self.get('correction') and self.get('correction_ack_revision') != self.get('revision'):
                    blockers.append('等待助手确认纠正后的理解和下一步')
                if blockers:
                    raise Rejected('；'.join(blockers))
                # 主人 2026-10-24 决定：空闲时“允许继续”只解除暂停，不要求观察链路新鲜——空闲时观察本来
                # 就不可能新鲜，按钮会永远被拒。之后每一步仍由 gate 按原样检查（第一步走启动通道）。
                # 这个人格还有任务在领取/运行时，仍要求观察可用。
                active=self.get('active_request') or {}
                busy=active.get('task_id') or self.db.execute("SELECT 1 FROM tasks WHERE persona=? AND status IN ('claimed','running')",(self.identity.actor(),)).fetchone()
                if busy:
                    observe=self.observer_status()
                    if not (self.observation_fresh(observe) or self.initial_model_observation(observe)):
                        raise Rejected('思考观察链路未就绪，保持暂停')
                self.check_storage()
                self.set('auto_recover_at', None)
                self.set('paused', False)
                self.event('control.resume', {'revision': self.get('revision')})
            elif action == 'chat':
                text = str(body.get('text', '')).strip()
                key = str(body.get('request_key', ''))
                if not text or len(text) > 24000 or not key or len(key) > 100:
                    raise Rejected('消息无效或缺少请求编号')
                self.check_storage()
                key = self.identity.actor()+':'+key
                old = self.db.execute('SELECT id FROM tasks WHERE request_key=?', (key,)).fetchone()
                if old:
                    return {'id': old[0], 'duplicate': True}
                target=body.get('target','assistant')
                if target not in ('dialogue','desktop','phone','assistant'):
                    raise Rejected('任务执行端无效')
                tid = str(uuid.uuid4())
                status = 'queued' if self.config.get('dispatch_enabled',False) and not self.config.get('owner_hold',{}).get('paused') else 'needs-review'
                self.db.execute('INSERT INTO tasks(id,request_key,text,status,time,revision,persona) VALUES(?,?,?,?,?,?,?)',
                                (tid, key, text, status, now(), self.get('revision'),self.identity.actor()))
                self.db.execute('UPDATE tasks SET target=? WHERE id=?',(target,tid))
                if status=='queued': self.set('paused',False)
                self.event('owner.message', {'text': text, 'message': '已保存，尚未执行'}, task_id=tid)
                if status=='queued':
                    # 执行器拿全局锁才能领取，会在本事务提交后才读到这条任务。
                    import worker; worker.wake()
                return {'id': tid, 'status': status, 'persona':self.identity.actor()}
            elif action == 'approval':
                aid, decision = body.get('id'), body.get('decision')
                if decision not in ('approved', 'denied'):
                    raise Rejected('申请决定无效')
                row = self.db.execute('SELECT status,kind FROM approvals WHERE id=?', (aid,)).fetchone()
                if row and row['kind']=='storage_expand':
                    raise Rejected('扩容只能在专用界面确认具体申请和目标，普通文字或安装批准无效')
                if not row or row[0] != 'pending':
                    raise Rejected('该申请不存在或已有决定')
                self.db.execute('UPDATE approvals SET status=?,decision_time=? WHERE id=?', (decision, now(), aid))
                self.event('approval.decided', {'id': aid, 'decision': decision, 'executed': False})
            elif action == 'answer':
                aid = str(body.get('id', ''))
                answer = str(body.get('answer', '')).strip()
                if not answer or len(answer) > 4000:
                    raise Rejected('回答为空或过长')
                row = self.db.execute('SELECT kind,status,details FROM approvals WHERE id=?', (aid,)).fetchone()
                if not row or row['kind'] != 'question' or row['status'] != 'pending':
                    raise Rejected('该问题不存在或已回答')
                details = json.loads(row['details'])
                details['answer'] = answer
                self.db.execute('UPDATE approvals SET status=?,details=?,decision_time=? WHERE id=?',
                                ('answered', json.dumps(details, ensure_ascii=False), now(), aid))
                self.event('approval.answered', {'id': aid, 'answer': answer})
                text = '主人的回答（对应你之前的问题「' + str(details.get('question', ''))[:80] + '」）：' + answer
                key = self.identity.actor() + ':answer-' + aid
                old = self.db.execute('SELECT id FROM tasks WHERE request_key=?', (key,)).fetchone()
                if old:
                    return {'ok': True, 'paused': self.get('paused'), 'task_id': old[0]}
                self.check_storage()
                tid = str(uuid.uuid4())
                status = 'queued' if self.config.get('dispatch_enabled', False) and not self.config.get('owner_hold', {}).get('paused') else 'needs-review'
                self.db.execute('INSERT INTO tasks(id,request_key,text,status,time,revision,persona) VALUES(?,?,?,?,?,?,?)',
                                (tid, key, text, status, now(), self.get('revision'), self.identity.actor()))
                self.db.execute("UPDATE tasks SET target='assistant' WHERE id=?", (tid,))
                if status == 'queued':
                    self.set('paused', False)
                self.event('owner.message', {'text': text, 'message': '主人已回答这个问题'}, task_id=tid)
                import worker
                worker.wake()
                return {'ok': True, 'paused': self.get('paused'), 'task_id': tid}
            elif action == 'dismiss':
                aid = str(body.get('id', ''))
                row = self.db.execute('SELECT kind,status FROM approvals WHERE id=?', (aid,)).fetchone()
                if not row or row['status'] not in ('pending', 'answered', 'denied', 'expired'):
                    raise Rejected('这条决定不存在或已经关闭')
                if row['kind'] == 'storage_expand':
                    raise Rejected('扩容申请只能在扩容界面处理')
                self.db.execute('UPDATE approvals SET status=?,decision_time=? WHERE id=?',
                                ('closed', now(), aid))
                self.event('approval.dismissed', {'id': aid, 'message': '主人关闭了这条决定'})
            else:
                raise Rejected('未知操作')
        if action in ('correct', 'resume'):
            import worker; worker.wake()
        return {'ok': True, 'paused': self.get('paused')}

    def owner_ask(self, args):
        """把需要主人提供的信息或决定放进「需要你决定」，提交后本轮可结束等回答。"""
        question = str(args.get('question', '')).strip()
        if not question or len(question) > 2000:
            raise Rejected('问题为空或过长')
        options = [str(item).strip()[:200] for item in (args.get('options') or []) if str(item).strip()][:6]
        active = self.get('active_request') or {}
        runtime = self.identity.runtime()
        details = {'question': question, 'options': options, 'kind': args.get('kind', 'input'),
                   'task_id': active.get('task_id'), 'session_id': runtime.get('session_id'),
                   'persona': self.identity.actor()}
        aid = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute('INSERT INTO approvals(id,kind,title,details,status,time) VALUES(?,?,?,?,?,?)',
                            (aid, 'question', question[:60], json.dumps(details, ensure_ascii=False),
                             'pending', now()))
            self.event('approval.requested', {'id': aid, 'question': question, 'options': options},
                       task_id=details['task_id'], session_id=details['session_id'])
            # 本轮最多等主人 30 秒；到点由执行器收尾并给出明确结果。
            self.set('owner_question', {'id': aid, 'task_id': details['task_id'],
                                        'deadline': time.time() + 30, 'question': question[:200]})
        return {'ok': True, 'id': aid,
                'message': '问题已提交给主人；本轮最多等 30 秒，超时会先结束并保留问题。'}

    def owner_question_state(self, task_id):
        """当前任务是否在等主人回答；回答或超时由执行器据此收尾。"""
        try:
            question = self.get('owner_question') or {}
        except (TypeError, ValueError):
            return None
        if not isinstance(question, dict) or question.get('task_id') != task_id:
            return None
        row = self.db.execute('SELECT status FROM approvals WHERE id=?', (question['id'],)).fetchone()
        return {**question, 'answered': bool(row and row[0] == 'answered')}

    def install_request(self, title, details, aid=None):
        required = {'purpose', 'source', 'version', 'download', 'disk', 'scope'}
        if not required.issubset(details):
            raise Rejected('安装申请缺少用途、来源、版本、下载量、占用或范围')
        aid = aid or str(uuid.uuid4())
        with self.lock, self.db:
            self.check_storage()
            self.db.execute('INSERT OR IGNORE INTO approvals VALUES(?,?,?,?,?,?,NULL)',
                            (aid, 'install', title, json.dumps(details, ensure_ascii=False), 'pending', now()))
        return aid

    def ingest(self, body):
        """Bridge accepts observations only. It cannot execute host commands."""
        kind = body.get('kind')
        if kind not in EVENT_TYPES:
            raise Rejected('不支持的运行事件')
        runtime = self.identity.runtime()
        if not runtime['session_id'] or body.get('session_id') != runtime['session_id']:
            raise Rejected('只接受显式绑定的专属会话；旧试验记录不接入')
        if body.get('model') != owner_settings.model_values(self)['model']:
            raise Rejected('非当前模型事件已拒绝')
        source_key = str(body.get('source_key', ''))
        if not source_key or len(source_key) > 500:
            raise Rejected('事件须有稳定去重编号')
        payload = body.get('payload')
        if not isinstance(payload, dict):
            raise Rejected('事件内容无效')
        if kind.endswith('.delta') and not isinstance(payload.get('text'), str):
            raise Rejected('增量必须包含模型原始文本')
        with self.lock, self.db:
            self.check_storage()
            if kind == 'reasoning.delta':
                self.set('stream_seen', True)
            self.event(kind, payload, body.get('task_id'), runtime['session_id'], source_key)
        return {'ok': True}

    def export(self):
        """Consistent SQLite backup plus readable JSONL, hashes; never deletes data."""
        with self.lock:
            out = self.root / 'exports' / (dt.datetime.now(ZoneInfo(self.config['timezone'])).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3) + '.zip')
            backup = sqlite3.connect(':memory:')
            self.db.backup(backup)
            dump = '\n'.join(backup.iterdump()).encode('utf-8')
            files = {'database.sql': dump, 'config.json': json.dumps(self.config, ensure_ascii=False, indent=2).encode('utf-8'),
                     'persona-reference.json': json.dumps(self.persona(), ensure_ascii=False, indent=2).encode('utf-8')}
            for table in ('events', 'memory', 'tasks', 'approvals', 'costs', 'state','activities'):
                files[table + '.jsonl'] = ''.join(json.dumps(dict(zip([c[0] for c in cursor.description], row)), ensure_ascii=False) + '\n'
                    for cursor in [backup.execute('SELECT * FROM ' + table)] for row in cursor).encode('utf-8')
            backup.close()
            sessions={p:self.identity.runtime(p).get('session_id') for p in PERSONAS}
        # Exporting archives does not hold the live memory/action lock.
        with self.authority.connect() as ledger:
            snap=sqlite3.connect(':memory:');ledger.backup(snap)
            files['budget.sql']='\n'.join(snap.iterdump()).encode('utf-8');snap.close()
        checks={k:hashlib.sha256(v).hexdigest() for k,v in files.items()}
        partial=out.with_suffix('.zip.partial')
        with zipfile.ZipFile(partial,'w',zipfile.ZIP_DEFLATED) as z:
            for name,content in files.items():z.writestr(name,content)
            legacy=self.config['runtime'].get('legacy_session_ids',[])
            manifest={'thinking':{},'complete':not bool(legacy),'owner_database_complete':True,
                      'legacy_thinking':{'sessionIds':legacy,'status':'external_archive_preserved_not_included' if legacy else 'none'},
                      'scope':'owner database, budget and current registered persona sessions; not a full directory archive; never shared with the other persona'}
            for persona,sid in sessions.items():
                if not sid:
                    manifest['thinking'][persona]={'status':'unbound'}
                    manifest['complete']=False
                    continue
                try:
                    stream=observer_open(self.root,'/api/thinking/export?sessionId='+sid,timeout=30)
                except OSError:
                    manifest['thinking'][persona]={'sessionId':sid,'status':'unavailable','note':'思考原件仍由手机工程保留；此包不完整'}
                    manifest['complete']=False
                    continue
                digest=hashlib.sha256()
                filename='personas/'+persona+'/thinking-original.jsonl'
                with stream,z.open(filename,'w') as target:
                    while chunk:=stream.read(2**20):
                        target.write(chunk);digest.update(chunk)
                checks[filename]=digest.hexdigest()
                manifest['thinking'][persona]={'sessionId':sid,'status':'included','file':filename}
            content=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
            z.writestr('export-status.json',content)
            checks['export-status.json']=hashlib.sha256(content).hexdigest()
            z.writestr('SHA256.json',json.dumps(checks,indent=2).encode('ascii'))
        partial.replace(out)
        self.event('export.saved', {'filename': out.name,'complete':manifest['complete'],'thinking':manifest['thinking']})
        return out


class Server(ThreadingHTTPServer):
    daemon_threads = True


_TIMING_LOCK=threading.Lock()


def _bridge_timing(folder, path, elapsed_ms, tool=None):
    """诊断用：运行时每次回调空间花了多久。只记路径、工具名和毫秒数；不 fsync，失败不影响请求。"""
    try:
        target=folder/'bridge-timing.jsonl'
        with _TIMING_LOCK:
            if target.exists() and target.stat().st_size>1048576:
                target.replace(folder/'bridge-timing.previous.jsonl')
            with target.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'at':now(),'path':path.split('?')[0][:40],'tool':tool,'ms':elapsed_ms})+'\n')
    except OSError:
        pass


def handler(store):
    csrf = secrets.token_urlsafe(32)
    expected_host = '127.0.0.1:' + str(store.config['port'])
    origin = 'http://' + expected_host
    owner_auth = OwnerHttpAuth(store.root,base=origin)
    # 外部委托入口（QQ Agent 等）用自己的密钥，不复用主人 Cookie。
    ext_file = store.data / 'ext.key'
    if not ext_file.exists():
        ext_file.write_text(secrets.token_urlsafe(32), encoding='ascii')
        try:
            ext_file.chmod(0o600)
        except OSError:
            pass
    ext_key = ext_file.read_text(encoding='ascii').strip()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def valid_host(self):
            return self.headers.get('Host') == expected_host

        def ext_ok(self):
            header = self.headers.get('Authorization', '')
            return bool(ext_key) and header.startswith('Bearer ') and hmac.compare_digest(header[7:].strip(), ext_key)

        def _ext_get(self, path, query):
            if not self.ext_ok():
                return self.send({'error': '外部调用密钥不匹配', 'code': 'EXT_AUTH_REQUIRED'}, 403)
            if path == '/api/ext/info':
                with store.identity.bind('li'):
                    tools = store.mcp({'method': 'tools/list', 'id': 'ext'})['result']['tools']
                return self.send({'ok': True, 'name': store.config.get('assistant_name', '小鲸鱼'),
                                  'paused': store.get('paused'), 'tool_count': len(tools),
                                  'tools': [item['name'] for item in tools]})
            if path.startswith('/api/ext/task/'):
                tid = path.removeprefix('/api/ext/task/')
                with store.lock:
                    row = store.db.execute('SELECT id,status,text,time,persona FROM tasks WHERE id=?', (tid,)).fetchone()
                    result = store.db.execute(
                        "SELECT payload FROM events WHERE task_id=? AND kind='task.result' ORDER BY seq DESC LIMIT 1",
                        (tid,)).fetchone()
                if not row:
                    return self.send({'error': '没有这个任务'}, 404)
                return self.send({'ok': True, 'id': tid, 'status': row['status'], 'text': row['text'],
                                  'time': row['time'], 'persona': row['persona'],
                                  'result': json.loads(result[0]) if result else None})
            return self.send({'error': '不存在'}, 404)

        def _ext_post(self, path):
            if not self.valid_host():
                return self.send({'error': 'Host rejected'}, 403)
            if not self.ext_ok():
                return self.send({'error': '外部调用密钥不匹配', 'code': 'EXT_AUTH_REQUIRED'}, 403)
            if path != '/api/ext/task':
                return self.send({'error': '不存在'}, 404)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise ValueError('size')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('shape')
            except (ValueError, TypeError):
                return self.send({'error': '请求体无效'}, 400)
            text = str(body.get('text', '')).strip()
            if not text or len(text) > 24000:
                return self.send({'error': '指令为空或过长'}, 400)
            persona = body.get('persona', 'li')
            if persona not in PERSONAS:
                persona = 'li'
            request_key = 'ext-' + secrets.token_hex(8)
            try:
                with store.identity.bind(persona):
                    result = store.command('chat', {'text': text, 'request_key': request_key,
                                                    'target': 'assistant'})
                    store.event('external.task', {'tool': 'chat', 'request_key': request_key,
                                                  'source': str(body.get('source', 'external'))[:40],
                                                  'persona': persona}, result.get('id'))
            except (Rejected, ValueError, RuntimeError) as error:
                return self.send({'error': str(error), 'code': getattr(error, 'code', 'EXT_REJECTED')}, 409)
            return self.send({'ok': True, 'id': result.get('id'), 'status': result.get('status'),
                              'persona': persona})

        def send(self, data, status=200, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            # The host loopback hub page (phone bridge) may frame this console in one
            # shared window; no other origin can, and no framing header conflicts.
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'self' http://127.0.0.1:19981; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):
                pass

        def do_GET(self):
            if not self.valid_host() or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                return self.send({'error': '仅限本机同源页面'}, 403)
            parsed = urlparse(self.path)
            if parsed.path.startswith('/api/ext/'):
                return self._ext_get(parsed.path, parse_qs(parsed.query))
            if not owner_auth.guard(self):return
            path = parsed
            q = parse_qs(path.query)
            if path.path == '/permissions':
                return self.send((store.root/'权限约束.md').read_bytes(),content_type='text/plain; charset=utf-8')
            if path.path.startswith('/assets/pet/'):
                from urllib.parse import unquote
                from space_tools import PET
                name=unquote(path.path.removeprefix('/assets/pet/'))
                file=store.root/'assets/pet'/name
                if name not in PET.values() or not file.is_file():return self.send({'error':'not found'},404)
                return self.send(file.read_bytes(),content_type='video/webm')
            if path.path.startswith('/artifacts/'):
                import re
                name=path.path.removeprefix('/artifacts/')
                if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.(png|svg)',name):
                    return self.send({'error':'not found'},404)
                file=store.root/'image-studio/outputs'/name
                if not file.is_file() or file.is_symlink():return self.send({'error':'not found'},404)
                return self.send(file.read_bytes(),content_type='image/png' if file.suffix=='.png' else 'image/svg+xml')
            if path.path == '/api/status':
                return self.send(store.status() | {'csrf': csrf})
            if path.path == '/api/schedule':
                return self.send(store.reports.payload())
            if path.path == '/api/owner/settings':
                return self.send(owner_settings.settings_payload(store, csrf))
            if path.path == '/api/goals':
                return self.send(owner_goals.payload(store) | {'csrf': csrf})
            if path.path == '/api/owner/persona':
                return self.send(owner_settings.persona_payload(store))
            if path.path == '/api/memory':
                return self.send(store.memories(q.get('q', [''])[0]))
            if path.path == '/api/actions':
                return self.send(store.identity.actions(q.get('other_only',['false'])[0]=='true'))
            if path.path == '/api/task-result':
                try:
                    return self.send(store.task_result(q.get('id',[''])[0]))
                except Rejected as error:
                    return self.send({'error':str(error),'code':error.code},404)
            if path.path in ('/api/thinking','/api/thinking/stream'):
                sid=store.identity.runtime().get('session_id')
                owner=store.identity.active()
                requested=q.get('sessionId',[sid])[0]
                if requested!=sid:
                    if not store.get('parallel_enabled'):
                        return self.send({'ok':False,'error':'当前人格会话已切换，请重新连接'},409)
                    owner=next((p for p in PERSONAS if store.identity.runtime(p).get('session_id')==requested),None)
                    if owner is None:
                        return self.send({'ok':False,'error':'当前人格会话已切换，请重新连接'},409)
                    sid=requested
                if not sid:
                    return self.send({'ok':False,'error':'正式会话尚未绑定'},503)
                after=q.get('after',['0'])[0]
                if not after.isdigit():
                    return self.send({'error':'游标无效'},400)
                last_id=self.headers.get('Last-Event-ID')
                if last_id and (len(last_id)>256 or ':'+sid+':' not in last_id):
                    return self.send({'error':'思考来源或会话游标不匹配'},409)
                url=path.path+'?sessionId='+sid+('' if last_id else '&after='+after)
                try:
                    upstream=observer_open(store.root,url,headers={'Last-Event-ID':last_id} if last_id else {},timeout=20)
                except (OSError,urllib.error.HTTPError):
                    return self.send({'ok':False,'error':'思考采集接口暂未连接，原记录保留'},503)
                with upstream:
                    if path.path.endswith('/stream'):
                        if 'text/event-stream' not in upstream.headers.get('Content-Type',''):
                            return self.send({'ok':False,'error':'思考接口尚未提供真实流'},503)
                        self.send_response(200)
                        self.send_header('Content-Type','text/event-stream; charset=utf-8')
                        self.send_header('Cache-Control','no-store')
                        self.send_header('Connection','close'); self.end_headers()
                        try:
                            # Relay normalized SSE verbatim. Parsing, IDs and durable
                            # archives are owned by this space's independent collector.
                            while line:=upstream.readline():
                                if store.identity.runtime(owner).get('session_id')!=sid:
                                    break
                                self.wfile.write(line); self.wfile.flush()
                        except (OSError,ConnectionError):
                            pass
                        return
                    raw=upstream.read(2**21)
                    try:
                        view=json.loads(raw)
                        if view.get('sessionId')!=sid:
                            raise ValueError('wrong session')
                    except ValueError:
                        return self.send({'ok':False,'error':'思考接口会话不匹配，未展示'},503)
                    return self.send(raw)
            if path.path == '/api/events':
                persona=store.identity.validate(q.get('persona',[store.identity.active()])[0])
                try:
                    after = max(0, int(q.get('after', [self.headers.get('Last-Event-ID', '0')])[0]))
                except ValueError:
                    return self.send({'error': '游标无效'}, 400)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        # In parallel mode both panes keep their own event stream;
                        # the front persona no longer cancels the other pane.
                        if not store.get('parallel_enabled') and store.identity.active()!=persona:
                            break
                        with store.identity.bind(persona):
                            rows = store.events(after)
                        for event in rows:
                            self.wfile.write(('id: ' + str(event['seq']) + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode('utf-8'))
                            after = event['seq']
                        if not rows:
                            self.wfile.write(b': keepalive\n\n')
                            time.sleep(.4)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return
            if path.path == '/api/phone-frame':
                frame=store.data/'phone-frames'/store.identity.actor()/'latest.png'
                if not frame.exists():return self.send({'error':'尚无当前模式截图'},404)
                return self.send(frame.read_bytes(),content_type='image/png')
            if path.path == '/api/phone-shot':
                shot=store.root/'data'/'phone-shot.png'
                if not shot.exists():return self.send({'error':'还没有手机画面'},404)
                return self.send(shot.read_bytes(),content_type='image/png')
            if path.path == '/api/frame':
                # The verified guest bridge writes this image atomically. Never capture the owner's host.
                frame = store.root / store.config['desktop']['frame_file']
                if not store.config['desktop']['ready'] or not frame.exists():
                    return self.send({'error': '专属桌面尚未连接'}, 503)
                return self.send(frame.read_bytes(), content_type='image/jpeg')
            if path.path.startswith('/exports/'):
                name = path.path.removeprefix('/exports/')
                if '/' in name or '\\' in name or not name.endswith('.zip'):
                    return self.send({'error': '路径无效'}, 400)
                file = store.root / 'exports' / name
                if file.exists():
                    return self.send(file.read_bytes(), content_type='application/zip')
                return self.send({'error': '导出不存在'}, 404)
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/app-v02.js': ('app-v02.js', 'text/javascript; charset=utf-8'),
                     '/panes.js': ('panes.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8'),
                     '/panes.css': ('panes.css', 'text/css; charset=utf-8'),
                     '/settings.js': ('settings.js', 'text/javascript; charset=utf-8'),
                     '/settings.css': ('settings.css', 'text/css; charset=utf-8'),
                     '/dual-layout.css': ('dual-layout.css', 'text/css; charset=utf-8'),
                     '/persona-switch.css': ('persona-switch.css', 'text/css; charset=utf-8'),
                     '/nav-pages.css': ('nav-pages.css', 'text/css; charset=utf-8'),
                     '/nav-pages.js': ('nav-pages.js', 'text/javascript; charset=utf-8'),
                     '/goals.js': ('goals.js', 'text/javascript; charset=utf-8'),
                     '/schedule.js': ('schedule.js', 'text/javascript; charset=utf-8'),
                     '/phone-panel.js': ('phone-panel.js', 'text/javascript; charset=utf-8'),
                     '/uuid-polyfill.js': ('uuid-polyfill.js', 'text/javascript; charset=utf-8')}
            if path.path in files:
                name, mime = files[path.path]
                return self.send((store.root / 'dist' / name).read_bytes(), content_type=mime)
            return self.send({'error': '不存在'}, 404)

        def do_POST(self):
            started=time.monotonic()
            try:
                if self.path.startswith('/api/ext/'):
                    return self._ext_post(urlparse(self.path).path)
                return self._post()
            finally:
                if self.path.startswith('/bridge/') or self.path.startswith('/mcp/'):
                    _bridge_timing(store.data,self.path,round((time.monotonic()-started)*1000),getattr(self,'_tool_name',None))

        def _post(self):
            if not self.valid_host():
                return self.send({'error': 'Host rejected'}, 403)
            if not owner_auth.guard(self):return
            bridge = self.path.startswith('/bridge/') or self.path == '/mcp' or self.path.startswith('/mcp/')
            if bridge:
                valid = hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + store.bridge_key)
            else:
                valid = self.headers.get('Origin') == origin and hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf)
            if not valid:
                return self.send({'error': '请求来源未通过验证','code':'AUTH_REJECTED'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 131072:
                    raise Rejected('请求大小无效')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise Rejected('请求须为对象')
                if body.get('method')=='tools/call' and isinstance(body.get('params'),dict):
                    self._tool_name=str(body['params'].get('name',''))[:60]
                if self.path == '/mcp' or self.path.startswith('/mcp/'):
                    if self.path == '/mcp':
                        raise Rejected('旧MCP入口已停用，请使用绑定人格的/mcp/biao或/mcp/li')
                    persona=store.identity.validate(self.path.removeprefix('/mcp/'))
                    with store.identity.bind(persona):
                        result = store.mcp(body)
                    if result is None:
                        self.send_response(202); self.send_header('Content-Length','0'); self.end_headers(); return
                elif self.path == '/bridge/event':
                    result = store.ingest(body)
                elif bridge:
                    result = store.bridge_action(self.path.removeprefix('/bridge/'), body)
                elif self.path == '/api/owner/settings':
                    result = owner_settings.save(store, body)
                elif self.path == '/api/goals':
                    result = owner_goals.mutate(store, body)
                elif self.path == '/api/owner/persona':
                    result = owner_settings.save_persona(store, body)
                elif self.path == '/api/memory':
                    pane=body.get('persona')
                    if store.get('parallel_enabled') and pane in PERSONAS:
                        with store.identity.bind(pane):
                            result = store.add_memory(body)
                    else:
                        result = store.add_memory(body)
                elif self.path == '/api/export':
                    file = store.export()
                    result = {'url': '/exports/' + file.name, 'path': str(file)}
                elif self.path.startswith('/api/schedule/'):
                    action = self.path.removeprefix('/api/schedule/')
                    if action == 'save':
                        result = store.reports.save(body)
                    elif action == 'test':
                        result = store.reports.send_test()
                    elif action == 'run':
                        segment = body.get('segment')
                        if isinstance(segment, dict):
                            result = store.reports.run_segment(segment)
                        else:
                            result = store.reports.run_now()
                    else:
                        raise Rejected('定时任务操作不存在')
                elif self.path.startswith('/api/control/'):
                    action = self.path.removeprefix('/api/control/')
                    pane = body.get('persona')
                    if action != 'persona' and store.get('parallel_enabled') and pane in PERSONAS:
                        # A pane may act for the persona that is not in front.
                        with store.identity.bind(pane):
                            result = store.command(action, body)
                    else:
                        result = store.command(action, body)
                else:
                    return self.send({'error': '不存在'}, 404)
                return self.send(result)
            except (Rejected, BudgetDenied, ValueError, RuntimeError, KeyError, sqlite3.IntegrityError) as error:
                return self.send({'error': str(error),'code':getattr(error,'code','INVALID_REQUEST')}, 409)
            except BaseException as error:
                _internal_error_log('POST '+str(self.path), error)
                bridge_path = (self.path == '/mcp' or self.path.startswith('/mcp/'))
                if bridge_path:
                    payload = {'jsonrpc':'2.0','id':None,'result':store.tool_data_result({'isError':True,'content':[{'type':'text','text':
                        '专属空间控制台内部错误（'+type(error).__name__+'），已记录到本地控制台日志；请求未完成'}]})}
                else:
                    payload = {'error': '控制台内部错误（'+type(error).__name__+'），已记录到本地控制台日志',
                               'code': 'CONSOLE_INTERNAL_ERROR'}
                return self.send(payload, 500)

    return Handler


def _auto_recovery(store):
    """Owner choice 2026-11-05: after an unconfirmed device action, wait 10 minutes,
    verify the device is reachable, turn the uncertain tasks into needs-review and
    resume. Manual pause/correction clears this deadline."""
    while True:
        time.sleep(10)
        try:
            if not store.get('paused'):
                if store.get('auto_recover_at') is not None:
                    store.set('auto_recover_at', None)
                continue
            due = store.get('auto_recover_at')
            if not isinstance(due, (int, float)) or time.time() < due:
                continue
            running = store.db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('claimed','running') LIMIT 1").fetchone()
            active = running or any((store._active_for(p) or {}).get('task_id') for p in PERSONAS)
            if active:
                store.set('auto_recover_at', time.time() + 30)
                continue
            from device_lease import device_lease_status
            if device_lease_status(store).get('phase') == 'busy':
                store.set('auto_recover_at', time.time() + 30)
                continue
            blockers = store.blockers()
            if blockers:
                store.set('auto_recover_at', time.time() + 300)
                store.event('control.auto-resume-blocked',
                            {'message': '自动恢复被阻塞，5 分钟后再试', 'blockers': blockers})
                continue
            device = str(store.get('auto_recover_device') or 'desktop')
            try:
                if device == 'phone':
                    if not store.phone_live.get('ready'):
                        raise RuntimeError('手机未连接')
                elif not store.desktop_live.get('ready'):
                    raise RuntimeError('电脑未连接')
            except Exception as error:
                store.set('auto_recover_at', time.time() + 300)
                store.event('control.auto-resume-blocked',
                            {'device': device, 'message': '设备健康检查失败，保持暂停，5 分钟后再试',
                             'error': str(error)[:200]})
                continue
            with store.lock, store.db:
                for row in store.db.execute("SELECT id FROM tasks WHERE status='uncertain'").fetchall():
                    tid = row[0]
                    dispatch = store.db.execute(
                        "SELECT session_id FROM events WHERE task_id=? AND kind='task.dispatch' "
                        "ORDER BY seq DESC LIMIT 1", (tid,)).fetchone()
                    store.db.execute("UPDATE tasks SET status='needs-review' WHERE id=?", (tid,))
                    store.event('task.auto-reviewed',
                                {'id': tid, 'message': '设备结果不确定，10 分钟未处理；已自动转为待复核，不重发'},
                                tid, dispatch[0] if dispatch else None)
                store.set('paused', False)
                store.set('active_request', None)
                store.set('auto_recover_at', None)
                store.event('control.auto-resumed',
                            {'device': device, 'message': '设备已恢复可达；不确定任务转为待复核，空间自动恢复'})
            try:
                import worker
                worker.wake()
            except Exception:
                pass
        except Exception as error:
            try:
                store.event('control.auto-resume-error', {'message': str(error)[:300]})
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(ROOT))
    args = parser.parse_args()
    store = Store(args.root)
    def _thread_error(report):
        # A background poller that dies leaves stale status (e.g. phone never
        # ready) with no visible trace; keep the reason on disk.
        name = report.thread.name if report.thread is not None else 'unknown'
        _internal_error_log('thread:' + name, report.exc_value or RuntimeError('missing exception'))
    threading.excepthook = _thread_error
    import qq_owner
    qq_owner.start(store)
    import worker
    worker.start(store)
    threading.Thread(target=_auto_recovery, args=(store,), name='auto-recovery', daemon=True).start()
    import desktop
    # 主人 2026-10-24 提速：只有空间服务进程复用常驻桥接；config.speed 可关闭。
    desktop.PERSISTENT['enabled']=(store.config.get('speed') or {}).get('desktop_persistent_bridge',True) is True
    desktop.start(store)
    import mail_report
    mail_report.start(store)
    try:
        import phone_tools
        phone_tools.start(store)
    except ImportError:
        pass
    bind=os.environ.get('DSH_CONSOLE_BIND','127.0.0.1')
    print('DSH 专属空间控制台 http://'+bind+':' + str(store.config['port']), flush=True)
    Server((bind, store.config['port']), handler(store)).serve_forever()


if __name__ == '__main__':
    main()
