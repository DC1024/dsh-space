// Select before prepareCall freezes the request and logs its real header.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url));
function modelService(){
 let provider='deepseek-official',model='deepseek-flash';
 try{
  const config=JSON.parse(fs.readFileSync(path.join(root,'config.json'),'utf8').replace(/^\uFEFF/,''));
  const llm=config.llm&&typeof config.llm==='object'?config.llm:{};
  if(typeof llm.provider==='string'&&llm.provider)provider=llm.provider;
  if(typeof llm.model==='string'&&llm.model)model=llm.model;
  else if(typeof config.model==='string'&&config.model)model=config.model;
 }catch{}
 return {provider,model};
}
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
    const expected=modelService();
    return {...base,provider:expected.provider,model:expected.model,
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
