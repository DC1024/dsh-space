"""Read runtime receipts before releasing an uncertain task; never resend it."""
import json


def reconcile(store, task_id, rpc_call=None, cross_restart=False, pause=True):
    """核对不确定任务：只读回执，绝不重发。

    cross_restart=True 专供「上一个控制台进程留下的不确定任务」（run_id 必然不符）。
    放行这一步不降低安全要求：仍然必须读到那一轮的 turn/end 且没有缺失的工具回执，
    否则照旧拒绝。pause=False 时不为自动收尾去暂停整个空间。"""
    from space import Rejected
    if rpc_call is None:
        from dsh_rpc import rpc as rpc_call
    with store.lock:
        row=store.db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not row or row['persona']!=store.identity.actor() or row['status']!='uncertain':
            raise Rejected('只能核对当前模式的不确定任务')
        if store.device_inflight.get(task_id):raise Rejected('设备操作仍在执行，暂不能解除占用')
        dispatch=store.db.execute("SELECT payload,session_id FROM events WHERE task_id=? AND kind='task.dispatch' ORDER BY seq DESC LIMIT 1",(task_id,)).fetchone()
        if not dispatch:raise Rejected('缺少原始发送边界，保留待人工核对；不会重发')
        data=json.loads(dispatch['payload']);sid=dispatch['session_id']
        # A previous process could still own a device relay. It requires an
        # explicit process/device inspection, not merely a missing Python lock.
        if data.get('run_id')!=store.run_id and not cross_restart:
            raise Rejected('该任务跨越控制台重启，须核对原设备执行进程后处理；不会自动解除')
    projection=rpc_call('session/projections',{'request':{'sessionId':sid}})
    through=projection['asOfSeq'];events={}
    for _ in range(20):
        page=rpc_call('session/page',{'request':{'address':{'kind':'session','sessionId':sid},'throughSeq':through,'maxMessages':100}})
        batch=[r.get('event',{}) for r in page.get('records',[])]
        batch=[e for e in batch if isinstance(e.get('seq'),int)]
        if not batch:break
        for e in batch:
            if e['seq']>data['start_seq']:events[e['seq']]=e
        low=min(e['seq'] for e in batch)
        if low<=data['start_seq'] or low>=through:break
        through=low-1
    turn=None;matched=False;queued_match=False;end=None;open_calls=set()
    for e in (events[k] for k in sorted(events)):
        d=e.get('data',{});kind=e.get('type')
        if kind=='agent/inbox/spliced':
            queued_match = queued_match or any(x.get('source',{}).get('rpcId')==data['request_id'] for x in d.get('inserted',[]))
        if kind=='turn/start':
            if matched:break
            turn=d.get('turn')
            matched=queued_match
        if kind=='user/message' and d.get('source',{}).get('rpcId')==data['request_id']:
            matched=True
        if matched and d.get('turn')==turn:
            if kind=='tool/call':open_calls.add(d.get('callId'))
            if kind=='tool/result':
                msg=d.get('message',{})
                for b in msg.get('content',[]):
                    open_calls.discard(b.get('toolCallId',b.get('callId')))
                open_calls.discard(msg.get('toolCallId',msg.get('callId')))
            if kind=='turn/end':end=e;break
    if not matched or end is None:
        # A dedicated new session may close during prompt transport, before
        # the request is accepted. Only accept an entirely empty closed tail,
        # with the task-to-session creation receipt and both inboxes empty.
        ordered=[events[k] for k in sorted(events)]
        inbox=projection.get('values',{}).get('inbox')
        binding=store.db.execute("SELECT 1 FROM events WHERE task_id=? AND session_id=? AND kind='context.continued'",(task_id,sid)).fetchone()
        closed_empty=(binding and ordered and ordered[-1].get('type')=='session/end-seed'
            and all(e.get('type')=='session/end-seed' for e in ordered)
            and isinstance(inbox,dict) and inbox.get('next-turn')==[] and inbox.get('next-step')==[])
        if not closed_empty:
            raise Rejected('尚未读到这项任务的停止回执，继续保留占用')
        end={'seq':ordered[-1]['seq'],'data':{'reason':{'kind':'not-started','evidence':'dedicated-session-closed-empty'}}}

    if open_calls:raise Rejected('原任务仍有缺失的工具回执，保留占用')
    with store.lock,store.db:
        row=store.db.execute('SELECT status FROM tasks WHERE id=?',(task_id,)).fetchone()
        if row['status']!='uncertain' or store.device_inflight.get(task_id):
            raise Rejected('任务状态已变化，请重新核对')
        store.db.execute("UPDATE tasks SET status='needs-review' WHERE id=?",(task_id,))
        if pause:
            store.set('paused',True)
        result={'id':task_id,'status':'needs-review','runtime_end_seq':end['seq'],
                'reason':end['data'].get('reason'),'resent':False,'resumed':False,'auto':bool(cross_restart),
                'message':'已核对原任务结束；执行效果仍待查看，旧任务不会重发。'}
        store.event('task.reconciled',result,task_id,sid)
        return result
