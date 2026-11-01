"""Single reservation authority, integer nano-CNY, cross-process SQLite locks.

Not connected to existing DSH consumers. No shared-limit claim until all paid
consumers use this authority and tariff/max-output bounds have been verified.
Uncertain requests retain reservations until explicit reconciliation.
"""
import datetime as dt
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


class BudgetDenied(Exception):
    def __init__(self, message, code='BUDGET_REJECTED'):
        super().__init__(message)
        self.code=code


def nano(amount):
    v = Decimal(str(amount))
    if not v.is_finite() or v < 0:
        raise BudgetDenied('费用必须为非负有限数')
    return int((v * 1000000000).to_integral_value(rounding=ROUND_CEILING))


class BudgetAuthority:
    def __init__(self, path, timezone='America/New_York', stop_at='19.5'):
        self.path = Path(path)
        self.timezone = timezone
        self.stop_at = nano(stop_at)
        self.path.parent.mkdir(exist_ok=True, parents=True)
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
              CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,day TEXT NOT NULL,
                cost INTEGER NOT NULL CHECK(cost>=0),source TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,day TEXT NOT NULL,
                upper_bound INTEGER NOT NULL CHECK(upper_bound>0),state TEXT NOT NULL,
                actual INTEGER,consumer TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS imports(source TEXT PRIMARY KEY,sha256 TEXT NOT NULL,time TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level='DEFERRED')
        try:
            with db:
                yield db
        finally:
            db.close()

    def day(self):
        return dt.datetime.now(ZoneInfo(self.timezone)).date().isoformat()

    def reserve(self, request_id, upper_cny, consumer, day=None):
        upper, day = nano(upper_cny), day or self.day()
        if not request_id or not consumer or upper <= 0:
            raise BudgetDenied('需要请求编号、消费者和大于零的费用上界')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT day,upper_bound,state,consumer FROM reservations WHERE id=?', (request_id,)).fetchone()
            if old:
                if old[0] != day or old[1] != upper or old[3] != consumer:
                    raise BudgetDenied('相同请求编号的参数发生变化','REQUEST_CONFLICT')
                return {'id': request_id, 'state': old[2], 'duplicate': True}
            cost = db.execute('SELECT COALESCE(SUM(cost),0) FROM entries WHERE day=?', (day,)).fetchone()[0]
            held = db.execute("SELECT COALESCE(SUM(upper_bound),0) FROM reservations WHERE day=? AND state='held'", (day,)).fetchone()[0]
            if cost + held + upper > self.stop_at:
                raise BudgetDenied('预算不足，保留记录并停止；不回退免费模型','BUDGET_EXHAUSTED')
            db.execute('INSERT INTO reservations VALUES(?,?,?,?,NULL,?)', (request_id, day, upper, 'held', consumer))
        return {'id': request_id, 'state': 'held', 'duplicate': False}

    def settle(self, request_id, actual_cny):
        actual = nano(actual_cny)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT day,upper_bound,state,actual,consumer FROM reservations WHERE id=?', (request_id,)).fetchone()
            if not row:
                raise BudgetDenied('请求没有费用预留，拒绝结算','RESERVATION_NOT_FOUND')
            day, upper, state, previous, consumer = row
            if state == 'settled':
                if previous != actual:
                    raise BudgetDenied('重复结算金额不一致，须人工核对','SETTLEMENT_CONFLICT')
                return {'duplicate': True, 'bound_violation': actual > upper}
            if state != 'held':
                raise BudgetDenied('请求已取消，不能再次结算')
            # Even a bound violation must record actual spend; never hide an overrun.
            db.execute('INSERT INTO entries VALUES(?,?,?,?)', ('request:' + request_id, day, actual, consumer))
            db.execute("UPDATE reservations SET state='settled',actual=? WHERE id=?", (actual, request_id))
        return {'duplicate': False, 'bound_violation': actual > upper}

    def cancel_unstarted(self, request_id, confirmed_not_sent=False):
        if confirmed_not_sent is not True:
            raise BudgetDenied('不确定是否发出的请求继续占用预留，等待对账','SEND_STATE_UNKNOWN')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state FROM reservations WHERE id=?',(request_id,)).fetchone()
            if not row:
                raise BudgetDenied('预留不存在','RESERVATION_NOT_FOUND')
            if row[0]=='settled':
                raise BudgetDenied('已结算请求不能取消','ALREADY_SETTLED')
            if row[0]=='cancelled':
                return {'id':request_id,'duplicate':True,'state':'cancelled'}
            db.execute("UPDATE reservations SET state='cancelled' WHERE id=? AND state='held'", (request_id,))
        return {'id':request_id,'duplicate':False,'state':'cancelled'}

    def import_legacy_snapshot(self, source_path):
        """Immutable one-time baseline per source; never rewrite/reprice old files."""
        path = Path(source_path).resolve()
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        meter = json.loads(raw.decode('utf-8-sig'))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT sha256 FROM imports WHERE source=?', (str(path),)).fetchone()
            if previous:
                if previous[0] != sha:
                    raise BudgetDenied('旧账本已变化：须先协调停写及增量迁移，不能重复导入')
                return {'duplicate': True, 'sha256': sha}
            for day, value in meter.get('days', {}).items():
                dt.date.fromisoformat(day)
                if 'estimatedCny' not in value:
                    raise BudgetDenied('旧账本缺少估算金额，拒绝将未知值当成零')
                db.execute('INSERT INTO entries VALUES(?,?,?,?)', ('legacy:' + hashlib.sha256((str(path)+day).encode()).hexdigest(),
                           day, nano(value['estimatedCny']), str(path)))
            db.execute('INSERT INTO imports VALUES(?,?,?)', (str(path), sha, dt.datetime.now(dt.timezone.utc).isoformat()))
        return {'duplicate': False, 'sha256': sha}
