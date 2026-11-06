"""Owner-managed goals for the space.

Goals are first-class owner intentions, separate from one-off chat tasks. The
autonomy envelope reads active goals in position order; the WebUI can add,
edit, pause, finish, reorder and dispatch them.
"""
import json
import uuid
from datetime import datetime

STATUSES = ('active', 'paused', 'done')
PERSONAS = ('biao', 'li', 'both')
TARGETS = ('assistant', 'dialogue', 'phone', 'desktop')
LIMIT_TEXT = 12000


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _clean_text(value):
    text = str(value or '').strip()
    if not text:
        raise ValueError('目标内容不能为空')
    if len(text) > LIMIT_TEXT:
        raise ValueError('目标内容最多 %d 字' % LIMIT_TEXT)
    return text


def _persona(value):
    value = str(value or 'biao')
    if value not in PERSONAS:
        raise ValueError('执行人格无效')
    return value


def _target(value):
    value = str(value or 'assistant')
    if value not in TARGETS:
        raise ValueError('执行端无效')
    return value


def _status(value):
    value = str(value or '')
    if value not in STATUSES:
        raise ValueError('目标状态无效')
    return value


def _goal(store, goal_id):
    row = store.db.execute('SELECT * FROM goals WHERE id=?', (goal_id,)).fetchone()
    if not row:
        raise ValueError('目标不存在')
    return dict(row)


def _last_task(store, task_id):
    if not task_id:
        return None
    row = store.db.execute('SELECT id,status,time,persona FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not row:
        return None
    task = dict(row)
    result = store.db.execute(
        "SELECT payload FROM events WHERE task_id=? AND kind='task.result' ORDER BY seq DESC LIMIT 1",
        (task_id,)).fetchone()
    if result:
        data = json.loads(result['payload'])
        task['failure'] = data.get('failure')
        task['text'] = str(data.get('text') or '')[:1200]
    return task


def payload(store):
    rows = [dict(row) for row in store.db.execute('SELECT * FROM goals ORDER BY position, created_at')]
    for goal in rows:
        goal['last_task'] = _last_task(store, goal.get('last_task_id'))
    return {'ok': True, 'goals': rows, 'statuses': list(STATUSES),
            'personas': list(PERSONAS), 'targets': list(TARGETS)}


def mutate(store, body):
    action = str(body.get('action') or '')
    if action == 'reorder':
        return _reorder(store, body)
    if action == 'dispatch':
        return _dispatch(store, body)
    with store.lock, store.db:
        if action == 'add':
            text, persona, target = _clean_text(body.get('text')), _persona(body.get('persona')), _target(body.get('target'))
            goal_id = str(uuid.uuid4())
            position = store.db.execute('SELECT COALESCE(MAX(position), -1) + 1 FROM goals').fetchone()[0]
            stamp = _now()
            store.db.execute('INSERT INTO goals VALUES(?,?,?,?,?,?,?,?,NULL)',
                             (goal_id, text, persona, target, 'active', position, stamp, stamp))
            store.event('goal.added', {'id': goal_id, 'persona': persona, 'target': target,
                                       'message': '主人新增了一个目标'})
            return {'ok': True, 'goal': _with_task(store, goal_id)}
        goal = _goal(store, str(body.get('id') or ''))
        if action == 'edit':
            text = _clean_text(body.get('text')) if body.get('text') is not None else goal['text']
            persona = _persona(body.get('persona')) if body.get('persona') is not None else goal['persona']
            target = _target(body.get('target')) if body.get('target') is not None else goal['target']
            store.db.execute('UPDATE goals SET text=?,persona=?,target=?,updated_at=? WHERE id=?',
                             (text, persona, target, _now(), goal['id']))
            store.event('goal.edited', {'id': goal['id'], 'message': '主人修改了目标'})
            return {'ok': True, 'goal': _with_task(store, goal['id'])}
        if action == 'status':
            status = _status(body.get('status'))
            store.db.execute('UPDATE goals SET status=?,updated_at=? WHERE id=?',
                             (status, _now(), goal['id']))
            store.event('goal.status', {'id': goal['id'], 'status': status,
                                        'message': {'active': '目标已启用', 'paused': '目标已暂停',
                                                    'done': '目标已完成'}[status]})
            return {'ok': True, 'goal': _with_task(store, goal['id'])}
    raise ValueError('未知的目标操作')


def _with_task(store, goal_id):
    goal = _goal(store, goal_id)
    goal['last_task'] = _last_task(store, goal.get('last_task_id'))
    return goal


def _reorder(store, body):
    ids = body.get('ids')
    if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
        raise ValueError('排序参数无效')
    with store.lock, store.db:
        existing = [row[0] for row in store.db.execute('SELECT id FROM goals')]
        if sorted(ids) != sorted(existing):
            raise ValueError('排序列表与现有目标不一致，请刷新后重试')
        for position, goal_id in enumerate(ids):
            store.db.execute('UPDATE goals SET position=? WHERE id=?', (position, goal_id))
        store.event('goal.reordered', {'ids': ids, 'message': '主人调整了目标顺序'})
    return {'ok': True, 'goals': payload(store)['goals']}


def _dispatch(store, body):
    goal = _goal(store, str(body.get('id') or ''))
    if goal['status'] == 'done':
        raise ValueError('已完成的目标不能再次交办')
    persona = store.identity.active() if goal['persona'] == 'both' else goal['persona']
    with store.identity.bind(persona):
        result = store.command('chat', {
            'text': goal['text'],
            'request_key': 'goal-' + goal['id'] + '-' + uuid.uuid4().hex[:8],
            'target': goal['target'],
        })
    with store.lock, store.db:
        store.db.execute('UPDATE goals SET last_task_id=?,updated_at=? WHERE id=?',
                         (result['id'], _now(), goal['id']))
        store.event('goal.dispatched', {'id': goal['id'], 'task_id': result['id'],
                                        'status': result.get('status'),
                                        'message': '目标已交办给' + ('表' if persona == 'biao' else '里') + '人格'})
    return {'ok': True, 'goal': _with_task(store, goal['id']), 'task': result}
