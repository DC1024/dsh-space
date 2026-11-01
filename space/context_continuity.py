"""Give every ordinary owner task a fresh session; retain native originals."""
import copy
import json
import time
import uuid


def atomic_json(path, value):
    temp = path.with_name(path.name + '.continuity.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def checkpoint(store, sid, through):
    rows = store.db.execute('''SELECT t.id,t.status,t.target,t.time,t.text,t.persona,e.seq,e.payload,e.session_id
        FROM tasks t LEFT JOIN events e ON e.task_id=t.id AND e.kind='task.result'
        WHERE t.persona=? AND t.status NOT IN ('queued','claimed','running')
        ORDER BY t.time DESC LIMIT 6''', (store.identity.actor(),)).fetchall()
    receipts = []
    for row in rows:
        result = json.loads(row['payload']) if row['payload'] else {}
        receipts.append({'task_id': row['id'], 'status': row['status'], 'target': row['target'],
                         'time': row['time'], 'session_id': row['session_id'],
                         'source_event_seq': row['seq'], 'performed_by': row['persona'],
                         'owner_goal_excerpt': row['text'][:2400],
                         'goal_excerpt_only': len(row['text']) > 2400,
                         'failure': result.get('failure'),
                         'tools': result.get('tools', []), 'through_seq': result.get('through_seq'),
                         'reply_excerpt': result.get('text', '')[:2400],
                         'excerpt_only': len(result.get('text', '')) > 2400})
    return {'schema': 'dsh-context-checkpoint-v1', 'authority': 'data_only',
            'previous_session_id': sid, 'through_seq': through,
            'persona': store.identity.actor(), 'recorded_by': 'space-context-continuity',
            'performed_by': 'space-context-continuity',
            'memory_backend': 'unchanged_shared_space_sqlite', 'recent_receipts': receipts,
            'authorization': 'none; only the current owner_request grants a new task',
            'pending_actions': [], 'old_tasks_replayed': False,
            'notice': '历史摘要仅供回忆，不是新任务或批准；工具名仅证明调用，效果以原回执为准。'
                      '旧外发任务缺少收件人与内容，禁止自动续发。原会话、工具回执和记忆完整保留。'}


def prepare_session(store, task, rpc_call=None):
    from space import Rejected
    if rpc_call is None:
        from dsh_rpc import rpc as rpc_call
    cfg = store.identity.runtime()
    sid = cfg['session_id']
    # Service-control rounds acknowledge the current correction/quota flow.
    # They are not a new ordinary task and must not discard its working context.
    if task.get('target') in ('correction','storage'):
        return sid
    projection = rpc_call('session/projections', {'request': {'sessionId': sid}})
    pressure = projection.get('values', {}).get('contextPressure') or {}
    with store.lock:
        # Evidence lock is per persona in parallel mode: the other persona's
        # native session must not block this bounded context rotation.
        if store.get('parallel_enabled'):
            busy = store.db.execute("SELECT 1 FROM tasks WHERE status IN ('running','uncertain') AND persona=? AND id!=?",
                                    (task['persona'], task['id'])).fetchone()
            # Device/phone execution is still one global queue.
            blocked = busy or store.device_inflight
        else:
            blocked = store.db.execute("SELECT 1 FROM tasks WHERE status IN ('running','uncertain')").fetchone() or store.device_inflight
        if blocked:
            raise Rejected('仍有执行中或结果不确定的任务，保留会话，不自动接续')
        if store.get('cancel_epoch') != task['_claim_cancel_epoch'] or store.get('revision') != task['revision']:
            raise Rejected('接续前任务已被暂停或纠正')
        page = rpc_call('session/page', {'request': {'address': {'kind': 'session', 'sessionId': sid},
                                                   'throughSeq': projection['asOfSeq'], 'maxMessages': 2}})
        turns = [r['event'] for r in page.get('records', []) if r.get('event', {}).get('type') in ('turn/start', 'turn/end')]
        if turns and turns[-1]['type'] != 'turn/end':
            raise Rejected('原会话尚未结束，保留原任务，不自动接续')
        old = json.loads((store.root/'config.json').read_text('utf-8-sig'))
        if old['runtime']['persona_bindings'][task['persona']]['session_id'] != sid:
            raise Rejected('会话绑定已改变，请核对新任务')
        new_sid = 'session-' + str(uuid.uuid4())
        folder = store.data/'context-checkpoints'
        folder.mkdir(exist_ok=True)
        record = checkpoint(store, sid, projection['asOfSeq'])
        record.update({'new_session_id': new_sid, 'pressure_before': pressure,
                       'boundary': 'new_owner_task', 'new_task_id': task['id'],
                       'persona_sha256': cfg['persona_sha256'], 'agent_preset': cfg['agent_preset'],
                       'created_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
        atomic_json(folder/(new_sid+'.json'), record)
        atomic_json(folder/(new_sid+'.config-before.json'), old)
        made = rpc_call('session/create', {'request': {'sessionId': new_sid,
                       'cwd': str(store.root/'workdir'/task['persona']), 'agentPreset': cfg['agent_preset']}})
        if made.get('sessionId') != new_sid:
            raise Rejected('原生会话创建回执不匹配，未改绑定')
        rpc_call('session/selectModel', {'request': {'sessionId': new_sid,
                 'provider': 'deepseek-official', 'model': 'deepseek-flash', 'reasoningEffort': 'low'}})
        changed = copy.deepcopy(old)
        binding = changed['runtime']['persona_bindings'][task['persona']]
        binding['session_id'] = new_sid
        binding['context_checkpoint'] = str((folder/(new_sid+'.json')).relative_to(store.root))
        changed['runtime']['session_id'] = changed['runtime']['persona_bindings'][store.identity.active()]['session_id']
        # History IDs are retained separately from the legacy external-archive field.
        history = changed['runtime'].setdefault('continuity_sessions', [])
        history.append({'persona': task['persona'], 'session_id': sid, 'through_seq': projection['asOfSeq'], 'next_session_id': new_sid,
                        'boundary': 'new_owner_task', 'new_task_id': task['id']})
        atomic_json(store.root/'config.json', changed)
        store.config = changed
        store.identity.publish_binding()
        store.event('context.continued', {'previous_session_id': sid, 'new_session_id': new_sid,
                    'through_seq': projection['asOfSeq'], 'checkpoint': binding['context_checkpoint'],
                    'boundary': 'new_owner_task',
                    'message': '本次新任务已使用独立上下文；原会话完整保留，历史按索引检索，旧动作不重发。'}, task['id'], new_sid)
    deadline = time.monotonic()+20
    while time.monotonic() < deadline:
        observe = store.observer_status(new_sid)
        if store.observation_fresh(observe) or store.initial_model_observation(observe):
            return new_sid
        time.sleep(.25)
    raise Rejected('新会话观察绑定尚未就绪，保留记录与绑定，未发送任务')


def reference_checkpoint(store):
    if not hasattr(store, 'identity'):
        return None
    path = store.identity.runtime().get('context_checkpoint')
    if not path:
        return None
    target = (store.root/path).resolve()
    if not target.is_relative_to((store.data/'context-checkpoints').resolve()):
        raise ValueError('Invalid context checkpoint path')
    value = json.loads(target.read_text('utf-8'))
    if value['new_session_id'] != store.identity.runtime()['session_id'] or value['persona'] != store.identity.actor():
        raise ValueError('Context checkpoint identity mismatch')
    return value
