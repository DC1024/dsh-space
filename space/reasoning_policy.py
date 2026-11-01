"""Task-scoped effort selection. No model calls, retries, devices or global config."""
import datetime as dt
import json
import re

LEVELS=('low','high','max')
# Owner 2026-10-24: no total model-step cap; repeated tool actions are counted by the runtime.


def _check_step(step):
    from space import Rejected
    if type(step) is not int or step<0:raise Rejected('请求步骤无效')


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _schema(store):
    store.db.execute('''CREATE TABLE IF NOT EXISTS reasoning_tasks(
        task_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,persona TEXT NOT NULL,
        cancel_epoch INTEGER NOT NULL,revision INTEGER NOT NULL,
        level TEXT NOT NULL,mode TEXT NOT NULL,reason TEXT NOT NULL,
        problem_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)''')


def _context(store,sid,gate=None):
    from personas import PERSONAS
    from space import Rejected
    persona=next((p for p in PERSONAS if store.identity.runtime(p).get('session_id')==sid),None)
    if persona is None:raise Rejected('未知推理会话','REASONING_SESSION_UNKNOWN')
    with store.identity.bind(persona):
        active=store.get('active_request')
        if gate and gate.get('acceptance_only'):
            return dict(task_id='acceptance:'+gate['grant_id']+':'+sid,session_id=sid,
                        persona=persona,cancel_epoch=store.get('cancel_epoch'),revision=store.get('revision'))
        if (not active or active.get('session_id')!=sid or active.get('persona')!=persona
            or active.get('cancel_epoch')!=store.get('cancel_epoch')
            or active.get('revision')!=store.get('revision')
            or gate and gate.get('task_id')!=active.get('task_id')):
            raise Rejected('推理设置只属于当前有效任务','REASONING_TASK_EXPIRED')
        return {k:active[k] for k in ('task_id','session_id','persona','cancel_epoch','revision')}


def _read(store,context):
    from space import Rejected
    _schema(store)
    row=store.db.execute('SELECT * FROM reasoning_tasks WHERE task_id=?',(context['task_id'],)).fetchone()
    if row:
        row=dict(row)
        if any(row[k]!=context[k] for k in context):
            raise Rejected('旧任务推理设置不能转借新授权','REASONING_TASK_EXPIRED')
        return row
    row=dict(context,level='low',mode='auto',reason='新任务默认low；遇到问题再逐级提高',problem_count=0,updated_at=_now())
    store.db.execute('''INSERT INTO reasoning_tasks(task_id,session_id,persona,cancel_epoch,
        revision,level,mode,reason,problem_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        tuple(row[k] for k in ('task_id','session_id','persona','cancel_epoch','revision',
                               'level','mode','reason','problem_count','updated_at')))
    store.event('reasoning.selected',_public(row),context['task_id'],context['session_id'])
    return row


def _public(row):
    return {k:row[k] for k in ('task_id','session_id','persona','level','mode','reason','problem_count','updated_at')}


def _save(store,row):
    row['updated_at']=_now()
    store.db.execute('UPDATE reasoning_tasks SET level=?,mode=?,reason=?,problem_count=?,updated_at=? WHERE task_id=?',
                     tuple(row[k] for k in ('level','mode','reason','problem_count','updated_at','task_id')))
    store.event('reasoning.selected',_public(row),row['task_id'],row['session_id'])


def resolve(store,body):
    """Choose before prepareCall/header logging; the returned request is immutable."""
    from space import Rejected
    step=body.get('step')
    _check_step(step)
    sid=body.get('session_id')
    gate=store.bridge_action('gate',{'session_id':sid,'phase':'model_step','step':step})
    with store.lock,store.db:
        context=_context(store,sid,gate)
        if body.get('task_id',context['task_id'])!=context['task_id']:
            raise Rejected('请求属于旧任务','REASONING_TASK_EXPIRED')
        with store.identity.bind(context['persona']):
            value=_public(_read(store,context))
        return dict(value,step=step,applies_to='next-request-only',allowed=list(LEVELS),
                    provider='deepseek-official',model='deepseek-flash')


def tool(store,args):
    from space import Rejected
    operation=args.get('operation','status')
    if operation not in ('status','select'):raise Rejected('未知推理强度操作')
    sid=store.identity.runtime().get('session_id')
    if operation=='select':
        store.bridge_action('gate',{'session_id':sid,'phase':'reasoning_change'})
    with store.lock,store.db:
        context=_context(store,sid)
        row=_read(store,context)
        if operation=='select':
            level=args.get('level')
            reason=args.get('reason')
            if level not in (*LEVELS,'auto') or not isinstance(reason,str) or not 1<=len(reason.strip())<=400:
                raise Rejected('请选择auto/low/high/max并给出具体原因')
            if level=='auto':
                row['mode']='auto';row['level']=LEVELS[min(row['problem_count'],2)]
            else:
                row['mode']='model-choice';row['level']=level
            row['reason']=reason.strip()
            _save(store,row)
        requests=store.db.execute("SELECT payload FROM events WHERE task_id=? AND kind='reasoning.request' ORDER BY seq DESC LIMIT 5",(context['task_id'],)).fetchall()
        return dict(_public(row),allowed=list(LEVELS),default='low',
                    applies_to='next-request-only',requests=[json.loads(r[0]) for r in requests],
                    note='QQ机器人已退役，不在本空间启用QQ/off渠道；不修改已经开始的请求。')


def record_problem(store,tool_name):
    """A genuine execution error raises the following request, never retries it."""
    with store.lock,store.db:
        sid=store.identity.runtime().get('session_id')
        try:context=_context(store,sid)
        except ValueError:return
        except Exception as error:
            if getattr(error,'code',None) in ('REASONING_SESSION_UNKNOWN','REASONING_TASK_EXPIRED'):return
            raise
        row=_read(store,context)
        row['problem_count']+=1
        automatic=LEVELS[min(row['problem_count'],2)]
        if LEVELS.index(automatic)>LEVELS.index(row['level']):row['level']=automatic
        row['mode']='auto-escalated'
        # No private tool arguments or raw provider errors enter this public reason.
        row['reason']='当前任务第'+str(row['problem_count'])+'次工具执行故障（'+str(tool_name)[:80]+'），后续请求提高强度；不自动重试动作'
        _save(store,row)


def record_request(store,body):
    """Record actual prepared options; a selection preference alone is not proof."""
    from space import Rejected
    rid=body.get('request_id');level=body.get('level');step=body.get('step')
    if (not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',rid)
        or level not in LEVELS or type(step) is not int or step<0):
        raise Rejected('实际请求强度证据无效')
    gate=store.bridge_action('gate',{'session_id':body.get('session_id'),'phase':'model_step','step':step})
    with store.lock,store.db:
        context=_context(store,body.get('session_id'),gate)
        if body.get('task_id')!=context['task_id']:
            raise Rejected('请求证据属于旧任务','REASONING_TASK_EXPIRED')
        evidence=dict(request_id=rid,task_id=context['task_id'],session_id=context['session_id'],
                      level=level,step=step,provider='deepseek-official',model='deepseek-flash',
                      phase='prepared-for-dispatch',provider_response_verified=False)
        old=store.db.execute('SELECT payload FROM events WHERE source_key=?',('reasoning-request:'+rid,)).fetchone()
        if old and json.loads(old[0])!=evidence:raise Rejected('同一请求的强度证据冲突')
        with store.identity.bind(context['persona']):
            store.event('reasoning.request',evidence,context['task_id'],context['session_id'],'reasoning-request:'+rid)
        return {'ok':True,**evidence}
