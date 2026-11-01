// Select before prepareCall freezes the request and logs its real header.
export function createReasoningRouter(rpc){
 const attached=new WeakSet(),prepared=new Map();
 async function call(action,body){
  const deadline=Date.now()+3000;
  while(true){try{return await rpc(action,body);}catch(e){
   if(e.code!=='OBSERVATION_STARTING'||Date.now()>=deadline)throw e;
   await new Promise(r=>setTimeout(r,100));
  }}
 }
 return {
  bind(agent){
   if(attached.has(agent))return;
   if(!agent?.ctx?.on||typeof agent.session?.id!=='string')throw Error('AGENT_SCOPE_REQUIRED');
   attached.add(agent);
   agent.ctx.on('agent/request',async(payload,next)=>{
    const base=await next();
    const selection=await call('reasoning/resolve',{session_id:agent.session.id,step:Number(payload.step)});
    if(!['low','high','max'].includes(selection.level))throw Error('INVALID_REASONING_SELECTION');
    const snapshot=Object.freeze({...selection});
    prepared.set(agent.session.id,snapshot);
    return {...base,provider:'deepseek-official',model:'deepseek-flash',
            reasoningEffort:snapshot.level,maxTokens:8192};
   },{prepend:true});
  },
  snapshot(sessionId,level){
   const value=prepared.get(sessionId);
   if(!value||value.level!==level)throw Error('REASONING_REQUEST_NOT_PREPARED');
   return value;
  },
  record(body){return call('reasoning/request',body);}
 };
}
