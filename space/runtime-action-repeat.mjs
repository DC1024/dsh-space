import {createHash} from 'node:crypto';

export const REPEAT_LIMIT=30;
export const REPEAT_CODE='REPEATED_ACTION_LIMIT';
const message=REPEAT_CODE+': 同一行动已连续执行30次，第31次未执行；保留进度并停止本轮。';

function canonical(value){
 if(Array.isArray(value))return value.map(canonical);
 if(value!==null&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])]));
 return value;
}

export function actionFingerprint(name,args){
 // The pwsh description is a UI label, not part of the command being executed.
 const value=name==='pwsh'&&args&&typeof args==='object'&&!Array.isArray(args)
  ? Object.fromEntries(Object.entries(args).filter(([k])=>k!=='description')) : args;
 return createHash('sha256').update(JSON.stringify([name,canonical(value??{})])).digest('hex');
}

export function createActionRepeatGuard({onBlocked=()=>{}}={}){
 // Session objects are owned by the runtime. Weak keys do not retain old chats.
 const states=new WeakMap();
 return {
  bind(session,taskId){
   if(!session||typeof taskId!=='string'||!taskId)throw Error('ACTION_TASK_ID_REQUIRED');
   if(states.get(session)?.taskId!==taskId)states.set(session,{taskId,last:null,count:0,blocked:false});
  },
  assertCanContinue(session){
   if(states.get(session)?.blocked)throw Object.assign(Error(message),{code:REPEAT_CODE});
  },
  before(exec){
   if(!exec.agent)return;
   // Count started inner actions, not the run_code presentation envelope.
   if(exec.name==='run_code'&&exec.parent===undefined)return;
   const session=exec.agent.session,state=states.get(session);
   if(!state)throw Error('ACTION_TASK_ID_REQUIRED');
   const deny=()=>({kind:'deny',reason:message,info:{code:REPEAT_CODE,name:'RepeatedActionLimitError'}});
   if(state.blocked)return deny();
   const fingerprint=actionFingerprint(exec.name,exec.arguments);
   if(state.last===fingerprint&&state.count>=REPEAT_LIMIT){
    state.blocked=true;
    onBlocked({session_id:session.id,task_id:state.taskId,tool:exec.name,count:state.count,blocked_attempt:31,code:REPEAT_CODE});
    return deny();
   }
   state.count=state.last===fingerprint?state.count+1:1;
   state.last=fingerprint;
  }
 };
}
