"""方案制自主运作：先制定方案并邮件征求主人意见，再按指令实施。

状态机：
- draft: 任务正在起草（或尚未提交完整方案）
- pending_review: 已发送给主人，等待实施/不实施/修改意见
- revising: 主人给出修改意见，等待空间分析并提交修订版
- approved: 主人同意实施，等待执行/执行中作为当前方案
- rejected: 主人选择不实施（终态，可新建后续方案）
- executing: 已作为当前任务推进（由 worker 标记）
- completed: 当前方案对应任务完成（终态，可新建后续方案）
- stalled: 连续失败/停滞达到阈值，已邮件提醒主人手动放行或终止

不把邮件正文视为授权输入；主人决策只通过控制台 API（及已验证的 QQ #指令转发）。
"""
import json
import sqlite3
import uuid
from datetime import datetime

STATUSES = ('draft', 'pending_review', 'revising', 'approved', 'rejected', 'executing', 'completed', 'stalled')
TERMINAL = ('rejected', 'completed')
WAITING_OWNER = ('pending_review', 'revising', 'stalled')
ACTIVE_EXEC = ('approved', 'executing')
LIMIT_TITLE = 120
LIMIT_SECTION = 12000
LIMIT_NOTE = 4000
DEFAULT_STALL_THRESHOLD = 3


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def ensure_table(db):
    db.execute('''
      CREATE TABLE IF NOT EXISTS proposals(
        id TEXT PRIMARY KEY,
        persona TEXT NOT NULL,
        target TEXT NOT NULL,
        title TEXT NOT NULL,
        why TEXT NOT NULL,
        how TEXT NOT NULL,
        benefit TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        owner_note TEXT,
        mail_sent_at TEXT,
        mail_error TEXT,
        last_task_id TEXT,
        failure_streak INTEGER NOT NULL DEFAULT 0,
        stall_threshold INTEGER NOT NULL DEFAULT 3,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      )
    ''')
    db.execute('CREATE INDEX IF NOT EXISTS proposals_status_idx ON proposals(status, updated_at DESC)')
    db.execute('CREATE INDEX IF NOT EXISTS proposals_persona_idx ON proposals(persona, updated_at DESC)')


def _clean(value, limit, name, required=True):
    text = '' if value is None else str(value).strip()
    if not text:
        if required:
            raise ValueError(name + '不能为空')
        return ''
    if len(text) > limit:
        raise ValueError(name + '最多 ' + str(limit) + ' 字')
    return text


def _persona(value, default='biao'):
    persona = str(value or default)
    if persona not in ('biao', 'li'):
        raise ValueError('人格无效')
    return persona


def _target(value):
    target = str(value or 'assistant')
    if target not in ('assistant', 'dialogue', 'phone', 'desktop'):
        raise ValueError('执行端无效')
    return target


def _row(store, proposal_id):
    row = store.db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
    if not row:
        raise ValueError('方案不存在')
    return dict(row)


def _with_task(store, proposal):
    task_id = proposal.get('last_task_id')
    proposal['last_task'] = None
    if not task_id:
        return proposal
    row = store.db.execute('SELECT id,status,time,persona,target FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not row:
        return proposal
    task = dict(row)
    result = store.db.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='task.result' ORDER BY seq DESC LIMIT 1",
        (task_id,)).fetchone()
    if result:
        try:
            data = json.loads(result['payload'])
        except Exception:
            data = {}
        task['failure'] = data.get('failure')
        task['text'] = str(data.get('text') or '')[:1200]
        task['stop_source'] = data.get('stop_source')
    proposal['last_task'] = task
    return proposal


def _stall_threshold(store, body=None):
    value = None
    if isinstance(body, dict):
        value = body.get('stall_threshold')
    if value is None:
        value = (store.config.get('proposal') or {}).get('stall_threshold', DEFAULT_STALL_THRESHOLD)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_STALL_THRESHOLD
    if value < 1:
        value = 1
    if value > 20:
        value = 20
    return value


def current(store, persona=None):
    """返回当前需要门控关注的方案（执行中优先，其次等待主人，其次草稿/修订）。"""
    personas = (persona,) if persona in ('biao', 'li') else ('biao', 'li')
    with store.lock:
        for status_group in (ACTIVE_EXEC, WAITING_OWNER, ('draft', 'revising')):
            placeholders = ','.join('?' * len(status_group))
            row = store.db.execute(
                'SELECT * FROM proposals WHERE persona IN (%s) AND status IN (%s) ORDER BY updated_at DESC LIMIT 1'
                % (','.join('?' * len(personas)), placeholders),
                (*personas, *status_group)).fetchone()
            if row:
                return _with_task(store, dict(row))
    return None


def pending_for_owner(store):
    with store.lock:
        rows = [dict(r) for r in store.db.execute(
            "SELECT * FROM proposals WHERE status IN ('pending_review','revising','stalled','draft') ORDER BY updated_at DESC")]
    return [_with_task(store, r) for r in rows]


def active_for_execution(store, persona):
    with store.lock:
        row = store.db.execute(
            "SELECT * FROM proposals WHERE persona=? AND status IN ('approved','executing') ORDER BY updated_at DESC LIMIT 1",
            (persona,)).fetchone()
    return _with_task(store, dict(row)) if row else None


def payload(store):
    with store.lock:
        rows = [dict(r) for r in store.db.execute('SELECT * FROM proposals ORDER BY updated_at DESC LIMIT 50')]
    proposals = [_with_task(store, r) for r in rows]
    return {'ok': True, 'proposals': proposals, 'statuses': list(STATUSES),
            'current': current(store, store.identity.actor() if hasattr(store, 'identity') else None),
            'default_stall_threshold': DEFAULT_STALL_THRESHOLD}


def _mail_proposal(store, proposal, kind='proposal'):
    try:
        import mail_report
        subject, text, html = mail_report.compose_proposal(store, proposal, kind=kind)
        mail = dict(store.reports.config['mail']) if hasattr(store, 'reports') and store.reports else {}
        mail_report.deliver(mail, subject, text, html)
        return _now(), ''
    except Exception as e:
        return None, str(e)[:500]


def submit(store, body, actor_task_id=None):
    """提交/修订方案并发邮件给主人。由 MCP 工具或控制台调用。"""
    with store.lock, store.db:
        persona = _persona(body.get('persona'), store.identity.actor())
        # 若已有同 persona 的待修订方案且未指定 id，则作为该方案的新版本
        proposal_id = str(body.get('id') or '').strip()
        existing = None
        if proposal_id:
            existing = _row(store, proposal_id)
        else:
            row = store.db.execute(
                "SELECT * FROM proposals WHERE persona=? AND status IN ('draft','revising') ORDER BY updated_at DESC LIMIT 1",
                (persona,)).fetchone()
            existing = dict(row) if row else None
        title = _clean(body.get('title'), LIMIT_TITLE, '标题')
        why = _clean(body.get('why'), LIMIT_SECTION, '为什么')
        how = _clean(body.get('how'), LIMIT_SECTION, '怎么做')
        benefit = _clean(body.get('benefit'), LIMIT_SECTION, '有什么用')
        target = _target(body.get('target'))
        threshold = _stall_threshold(store, body)
        stamp = _now()
        if existing and existing['status'] in ('draft', 'revising'):
            version = int(existing.get('version') or 1) + (1 if existing['status'] == 'revising' or existing.get('mail_sent_at') else 0)
            store.db.execute('''UPDATE proposals SET title=?,why=?,how=?,benefit=?,target=?,version=?,status='pending_review',
                mail_sent_at=NULL,mail_error=NULL,updated_at=?,last_task_id=COALESCE(?,last_task_id),stall_threshold=?
                WHERE id=?''',
                (title, why, how, benefit, target, version, stamp, actor_task_id, threshold, existing['id']))
            proposal = _row(store, existing['id'])
            kind = 'revision' if existing.get('version') else 'proposal'
        else:
            proposal_id = str(uuid.uuid4())
            version = 1
            store.db.execute('''INSERT INTO proposals(id,persona,target,title,why,how,benefit,version,status,owner_note,
                mail_sent_at,mail_error,last_task_id,failure_streak,stall_threshold,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?, 'pending_review', NULL, NULL, NULL, ?, 0, ?, ?, ?)''',
                (proposal_id, persona, target, title, why, how, benefit, version, actor_task_id, threshold, stamp, stamp))
            proposal = _row(store, proposal_id)
            kind = 'proposal'
        proposal['status'] = 'pending_review'
    sent_at, mail_error = _mail_proposal(store, proposal, kind=kind)
    with store.lock, store.db:
        store.db.execute('UPDATE proposals SET mail_sent_at=?, mail_error=?, updated_at=? WHERE id=?',
                         (sent_at, mail_error, _now(), proposal['id']))
        proposal = _row(store, proposal['id'])
        store.event('proposal.submitted', {
            'id': proposal['id'], 'version': proposal['version'], 'status': proposal['status'],
            'mail_sent': bool(sent_at), 'mail_error': mail_error or None,
            'message': '方案已提交，等待主人决定' if sent_at else '方案已保存，但邮件发送失败：' + (mail_error or '未知原因')})
    return {'ok': True, 'proposal': _with_task(store, proposal), 'mail_sent': bool(sent_at), 'mail_error': mail_error or None}


def mutate(store, body):
    action = str(body.get('action') or '')
    proposal_id = str(body.get('id') or '').strip()
    if action == 'submit':
        return submit(store, body)
    if not proposal_id:
        raise ValueError('缺少方案编号')
    with store.lock, store.db:
        proposal = _row(store, proposal_id)
        stamp = _now()
        if action == 'approve':
            if proposal['status'] not in ('pending_review', 'revising', 'stalled'):
                raise ValueError('当前状态不可标记为实施')
            store.db.execute("UPDATE proposals SET status='approved', owner_note=NULL, failure_streak=0, updated_at=? WHERE id=?",
                             (stamp, proposal['id']))
            store.event('proposal.approved', {'id': proposal_id, 'message': '主人同意实施方案'})
        elif action == 'reject':
            if proposal['status'] not in ('pending_review', 'revising', 'stalled', 'draft', 'approved', 'executing'):
                raise ValueError('当前状态不可标记为不实施')
            store.db.execute("UPDATE proposals SET status='rejected', owner_note=?, updated_at=? WHERE id=?",
                             (_clean(body.get('note'), LIMIT_NOTE, '备注', required=False) or None, stamp, proposal_id))
            store.event('proposal.rejected', {'id': proposal_id, 'message': '主人选择不实施该方案'})
        elif action == 'revise':
            note = _clean(body.get('note') or body.get('feedback') or body.get('owner_note'), LIMIT_NOTE, '修改意见')
            if proposal['status'] not in ('pending_review', 'stalled', 'draft', 'approved', 'executing'):
                raise ValueError('当前状态不可提交修改意见')
            store.db.execute("UPDATE proposals SET status='revising', owner_note=?, updated_at=? WHERE id=?",
                             (note, stamp, proposal_id))
            store.event('proposal.revise_requested', {'id': proposal_id, 'message': '主人已提交修改意见，等待修订方案'})
        elif action == 'release':
            if proposal['status'] != 'stalled':
                raise ValueError('只有停滞方案可以手动放行继续')
            store.db.execute("UPDATE proposals SET status='approved', failure_streak=0, updated_at=? WHERE id=?",
                             (stamp, proposal_id))
            store.event('proposal.released', {'id': proposal_id, 'message': '主人已手动放行，方案可继续执行'})
        elif action == 'terminate':
            if proposal['status'] not in ('stalled', 'approved', 'executing', 'pending_review', 'revising'):
                raise ValueError('当前状态不可终止')
            note = _clean(body.get('note'), LIMIT_NOTE, '备注', required=False) or proposal.get('owner_note')
            store.db.execute("UPDATE proposals SET status='rejected', owner_note=?, updated_at=? WHERE id=?",
                             (note, stamp, proposal_id))
            store.event('proposal.terminated', {'id': proposal_id, 'message': '主人已终止该方案'})
        else:
            raise ValueError('未知的方案操作')
        result = _row(store, proposal_id)
    return {'ok': True, 'proposal': _with_task(store, result)}


def mark_executing(store, proposal_id, task_id):
    with store.lock, store.db:
        proposal = _row(store, proposal_id)
        if proposal['status'] in TERMINAL:
            return proposal
        store.db.execute("UPDATE proposals SET status='executing', last_task_id=?, updated_at=? WHERE id=?",
                         (task_id, _now(), proposal_id))
        return _row(store, proposal_id)


def _is_failure(status, failure, stop_source):
    if status in ('failed', 'uncertain'):
        return True
    if status != 'stopped':
        return False
    code = ((failure or {}).get('code') or '').upper()
    benign = {'CONTEXT_ROTATED', 'AUTONOMY_WINDOW_ENDED', 'OWNER_MESSAGE_PREEMPT', 'HOST_BECAME_ACTIVE'}
    if code in benign:
        return False
    if stop_source in ('autonomy-window-ended', 'owner-message-preempt', 'host-became-active', 'context-rotation'):
        return False
    return True


def record_task_result(store, proposal_id, task_id, status, failure=None, stop_source=None):
    """记录方案任务结果，更新 streak/状态；达到阈值时发停滞邮件并返回 stalled。"""
    stalled_now = False
    with store.lock, store.db:
        proposal = _row(store, proposal_id)
        if proposal['status'] in TERMINAL:
            return _with_task(store, proposal), False
        threshold = int(proposal.get('stall_threshold') or DEFAULT_STALL_THRESHOLD)
        streak = int(proposal.get('failure_streak') or 0)
        now = _now()
        if status == 'completed':
            store.db.execute("UPDATE proposals SET status='completed', last_task_id=?, failure_streak=0, updated_at=? WHERE id=?",
                             (task_id, now, proposal_id))
            store.event('proposal.completed', {'id': proposal_id, 'task_id': task_id, 'message': '方案对应任务已完成'})
        else:
            failed = _is_failure(status, failure, stop_source)
            if failed:
                streak += 1
            # needs-review 等未明确失败不强制清零，但不累计
            new_status = 'stalled' if failed and streak >= threshold else 'approved'
            store.db.execute("UPDATE proposals SET status=?, last_task_id=?, failure_streak=?, updated_at=? WHERE id=?",
                             (new_status, task_id, streak, now, proposal_id))
            if failed and streak >= threshold:
                stalled_now = True
                store.event('proposal.stalled', {'id': proposal_id, 'task_id': task_id, 'streak': streak,
                                                 'message': '方案连续失败/停滞，已等待主人手动放行或终止'})
            elif failed:
                store.event('proposal.progress', {'id': proposal_id, 'task_id': task_id, 'streak': streak,
                                                  'status': status, 'message': '方案本轮未成功，等待继续推进'})
        proposal = _row(store, proposal_id)
    if stalled_now:
        sent_at, mail_error = _mail_proposal(store, proposal, kind='stalled')
        with store.lock, store.db:
            store.db.execute('UPDATE proposals SET mail_sent_at=?, mail_error=?, updated_at=? WHERE id=?',
                             (sent_at, mail_error, _now(), proposal_id))
            store.event('proposal.stalled_mail', {'id': proposal_id, 'mail_sent': bool(sent_at), 'mail_error': mail_error or None})
            proposal = _row(store, proposal_id)
    return _with_task(store, proposal), stalled_now


def envelope_context(store, persona):
    """给任务信封注入当前方案上下文。"""
    with store.lock:
        rows = [dict(r) for r in store.db.execute(
            "SELECT * FROM proposals WHERE persona=? AND status NOT IN ('rejected','completed') ORDER BY updated_at DESC LIMIT 3",
            (persona,))]
    items = []
    for p in rows:
        items.append({
            'id': p['id'], 'version': p['version'], 'status': p['status'],
            'title': p['title'], 'why': p['why'], 'how': p['how'], 'benefit': p['benefit'],
            'target': p['target'], 'owner_note': p.get('owner_note'),
            'failure_streak': p.get('failure_streak', 0), 'stall_threshold': p.get('stall_threshold', DEFAULT_STALL_THRESHOLD),
        })
    return items
