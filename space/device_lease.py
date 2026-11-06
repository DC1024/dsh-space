"""One device operation at a time; model calls take priority over previews."""
from contextlib import contextmanager
import os
import threading
import time
import uuid

AUTO_RECOVER_SECONDS = int(os.environ.get('DSH_AUTO_RECOVER_SECONDS', '600'))

_EXECUTOR_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_active = None
_waiting = {}
_emergency_hold = set()
READ_ONLY = {'phone_health', 'phone_screen', 'phone_ui', 'desktop_screen', 'desktop_health',
             'her_list', 'her_read', 'android_view', 'android_info', 'android_ui'}


def _scope(store):
    return str(store.root.resolve())


def _unknown(store):
    with store.lock:
        return bool(store.db.execute("SELECT 1 FROM tasks WHERE status='uncertain' LIMIT 1").fetchone())


def device_lease_status(store):
    scope = _scope(store)
    uncertain = _unknown(store)
    with _STATE_LOCK:
        active = dict(_active) if _active and _active['scope'] == scope else None
        waiting = [dict(v) for v in _waiting.values() if v['scope'] == scope]
        uncertain = uncertain or scope in _emergency_hold
    return {'policy': 'one-device-operation-at-a-time',
            'current_device': active['device'] if active else None,
            'owner': active['owner'] if active else None,
            'task_id': active.get('task_id') if active else None,
            'waiting_devices': list(dict.fromkeys(v['device'] for v in waiting)),
            'waiting_count': len(waiting), 'blocked_unknown': uncertain,
            'phase': 'blocked_unknown' if uncertain else ('busy' if active else 'idle')}


def _check_current(store, captured):
    from space import Rejected
    current = store.get('active_request')
    if (any(current.get(k) != captured.get(k) for k in ('task_id','session_id','cancel_epoch','revision'))
            or store.get('cancel_epoch') != captured.get('cancel_epoch')
            or store.get('revision') != captured.get('revision')
            or store.get('paused')
            or store.identity.runtime().get('session_id') != captured.get('session_id')):
        raise Rejected('设备等待期间任务已暂停、纠正或切换会话；未执行等待动作','DEVICE_LEASE_EXPIRED')
    with _STATE_LOCK:
        emergency = _scope(store) in _emergency_hold
    if _unknown(store) or emergency:
        raise Rejected('已有设备操作结果不确定，手机和电脑都保持停止；须核对原回执','DEVICE_RESULT_UNCERTAIN')


def _mark_unknown(store, captured, device=None):
    # Persist before releasing the global lock, so a waiter cannot race the
    # caller's outer error handling and start on the other device.
    try:
        with store.lock, store.db:
            row=store.db.execute('SELECT status FROM tasks WHERE id=?',(captured['task_id'],)).fetchone()
            store.set('auto_recover_at', time.time()+AUTO_RECOVER_SECONDS)
            store.set('auto_recover_device', device or 'desktop')
            store.set('auto_recover_task', captured['task_id'])
            if not row or row[0] != 'uncertain':
                store.db.execute("UPDATE tasks SET status='uncertain' WHERE id=?",(captured['task_id'],))
                store.set('paused',True)
                store.set('cancel_epoch',store.get('cancel_epoch')+1)
    except BaseException:
        with _STATE_LOCK:
            _emergency_hold.add(_scope(store))
        raise


@contextmanager
def lease(store, device, action=None):
    global _active
    from space import Rejected
    if device not in ('phone','desktop'):
        raise Rejected('Unknown device')
    with store.lock:
        captured=dict(store.get('active_request'))
        task_id=captured.get('task_id')
        if not task_id:raise Rejected('No active task')
        _check_current(store,captured)
        store.device_inflight[task_id]=store.device_inflight.get(task_id,0)+1
    stage_id=uuid.uuid4().hex
    entry={'scope':_scope(store),'device':device,'owner':'model','task_id':task_id}
    acquired=False
    with _STATE_LOCK:
        _waiting[stage_id]=entry
    try:
        _EXECUTOR_LOCK.acquire()
        acquired=True
        with _STATE_LOCK:
            _waiting.pop(stage_id,None)
            _active=entry
        with store.lock:
            _check_current(store,captured)
        store.bridge_action('gate',{'session_id':captured['session_id'],'phase':'device_step','device':device})
        metadata={'stage_id':stage_id,'device':device,'action':action,'run_id':store.run_id,
                  'cancel_epoch':captured['cancel_epoch'],'automatic_retry':False}
        store.event('device.stage',{**metadata,'phase':'started'},task_id,captured['session_id'])
        try:
            yield
        except BaseException as error:
            # A screen request can include an explicitly authorized VM wake.
            # Preserve an executor's explicit uncertain flag even for reads.
            unknown = (getattr(error,'result',None) or {}).get('uncertain',action not in READ_ONLY)
            if unknown:_mark_unknown(store,captured,device)
            store.event('device.stage',{**metadata,'phase':'failed',
                        'effect':'unconfirmed' if unknown else 'read_only_or_rejected'},task_id,captured['session_id'])
            raise
        else:
            store.event('device.stage',{**metadata,'phase':'receipt','effect':'requires_result_readback'},task_id,captured['session_id'])
    finally:
        with _STATE_LOCK:
            _waiting.pop(stage_id,None)
            if acquired:_active=None
        if acquired:_EXECUTOR_LOCK.release()
        with store.lock:
            remaining=store.device_inflight.get(task_id,1)-1
            if remaining:store.device_inflight[task_id]=remaining
            else:store.device_inflight.pop(task_id,None)


@contextmanager
def background_lease(store, device='desktop'):
    """Non-blocking read-only preview admission with model priority.

    Use `with background_lease(store, 'desktop') as admitted:`. Only when True,
    call the existing health/screen executor; keep the context open through the
    full receipt. False means skip this poll: no queue, sleep under this lock,
    or immediate retry. The helper grants no model authority or mutation access.
    """
    global _active
    if device not in ('phone','desktop'):
        raise ValueError('Unknown device')
    scope=_scope(store)
    admitted=False
    uncertain=_unknown(store)
    with _STATE_LOCK:
        if not uncertain and scope not in _emergency_hold and not _waiting:
            admitted=_EXECUTOR_LOCK.acquire(blocking=False)
            if admitted:_active={'scope':scope,'device':device,'owner':'background','task_id':None}
    # An operation may have failed while this caller was reading its first
    # status snapshot. Recheck after owning the execution lock, without taking
    # store.lock while holding _STATE_LOCK (model admission uses the reverse).
    if admitted and _unknown(store):
        with _STATE_LOCK:
            _active=None
        _EXECUTOR_LOCK.release()
        admitted=False
    try:
        yield admitted
    finally:
        if admitted:
            with _STATE_LOCK:
                _active=None
            _EXECUTOR_LOCK.release()
