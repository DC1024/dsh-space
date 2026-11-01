import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import {createReasoningRouter} from './runtime-reasoning.mjs';
import {createActionRepeatGuard} from './runtime-action-repeat.mjs';
import {releaseConsumedHistory} from './runtime-memory.mjs';
import {excludedRuntimeTool, filterRuntimeTools, ownerDecisions} from './runtime-tool-policy.mjs';
const root=path.dirname(fileURLToPath(import.meta.url));

export const name='space-exclusive-tools-and-budget';
export const inject=['tools','llm','sessionQuery'];
const readonlyAcceptance=new Set(['memory_recall','space_status','action_log_read'].map(n=>'mcp__space__'+n));
function controlled(){try{const g=JSON.parse(fs.readFileSync(path.join(root,'data','acceptance-grants-v03.json'),'utf8'));return g.enabled===true&&Date.now()/1000<g.expires_at&&Object.keys(g.consumers||{}).some(k=>k.startsWith('space-runtime:'));}catch{return false;}}
// In 自动审查 mode the owner granted the full built-in toolset (fs/command/web/...):
// the per-call auto-review layer judges each action, so the space no longer hides
// host tools from the model. The allowlist below only applies to the confined
// presets (read-only / workspace-write).
let presetCache={at:0,value:'workspace-write'};
function permissionPreset(){
 const now=Date.now();
 // Owner changes are read at the next step; do not reuse a stale permission cache.
 let value='workspace-write';
 try{
  const doc=JSON.parse(fs.readFileSync(path.join(root,'config.json'),'utf8').replace(/^\uFEFF/,''));
  if(typeof doc.permission_preset==='string')value=doc.permission_preset;
 }catch{}
 presetCache={at:now,value};
 return value;
}
// 主人可见的权限档名（config.json -> permission_preset）：
//   read-only | workspace-write | auto | full
// "full" = 完全权限档：钉到内置的 danger-full-access 身份，于是自动审查器
// （只审 AUTO_PRESET 会话）根本不会挂上来，行为层面零限制。预算、设备互斥、
// 暂停这类运行时保护不在权限层，照旧生效。
const PRESET_ALIAS={full:'danger-full-access'};
export function permissionPresetName(){
 const v=permissionPreset();
 return PRESET_ALIAS[v]||v;
}
const hostToolsAllowed=()=>!controlled();
const readOnlySpace=new Set(['memory_recall','context_recall','context_read','space_status','storage_status','action_log_read','phone_health','phone_screen','phone_ui','desktop_screen','workspace_list','workspace_read','workspace_review_list','workspace_read_diff','web_fetch','web_search','image_capabilities','her_list','her_read','android_view','android_info','android_ui'].map(n=>'mcp__space__'+n));
const mayExpose=name=>!excludedRuntimeTool(name)&&(controlled()?readonlyAcceptance.has(name):permissionPreset()==='read-only'&&name.startsWith('mcp__')?readOnlySpace.has(name):true);
const allowed=new Set(['memory_recall','context_recall','context_read','memory_remember','memory_revise','correction_ack','space_status','storage_status','storage_request_expansion','action_log_read','desktop_screen','desktop_click','desktop_type','desktop_key','desktop_scroll','phone_health','phone_screen','phone_ui','phone_click','phone_type','phone_key','phone_swipe','workspace_list','workspace_read','workspace_write','workspace_review_list','workspace_read_diff','workspace_review_decide','pet','web_fetch','web_search','image_capabilities','image_create_svg','reasoning_level','her_list','her_read','her_write','her_run','android_view','android_info','android_ui','android_tap','android_swipe','android_type','android_key','android_app','owner_ask'].map(n=>'mcp__space__'+n));
// Fixed, explicitly authorized read/refresh of this workspace archive only.
for(const n of ['status','search','read','refresh'])allowed.add('mcp__codex-history__codex_history_'+n);
async function rpc(action,body){
 const key=fs.readFileSync(path.join(root,'data','bridge.key'),'utf8').trim();
 const r=await fetch('http://127.0.0.1:19991/bridge/'+action,{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+key},body:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
 const v=await r.json();if(!r.ok){const e=Error(v.error||'SPACE_GATE_REJECTED');e.code=v.code;throw e;}return v;
}
// Limits are owner-tunable in config.json -> "context"; the values below apply
// only when that block is missing or unreadable. 180000 bytes was just under
// ~35k tokens of real prompt, far below the model window (1e6 tokens declared,
// provider accepts far more), so long file-heavy tasks hit our own guard first.
const FALLBACK={inputBytes:420000,softRatio:.7};
let policyCache={at:0,value:null};
function policy(){
  const now=Date.now();
  if(policyCache.value&&now-policyCache.at<5000)return policyCache.value;
  let value=FALLBACK;
  try{
    const doc=JSON.parse(fs.readFileSync(path.join(root,'config.json'),'utf8').replace(/^\uFEFF/,''));
    const c=doc.context||{};
    value={inputBytes:Number(c.input_byte_limit)>0?Math.floor(Number(c.input_byte_limit)):FALLBACK.inputBytes,
      softRatio:Number(c.soft_ratio)>0&&Number(c.soft_ratio)<1?Number(c.soft_ratio):FALLBACK.softRatio};
  }catch{}
  policyCache={at:now,value};
  return value;
}
export function apply(ctx){
 const repeats=createActionRepeatGuard({onBlocked(row){
  try{fs.appendFileSync(path.join(root,'data','action-repeat-audit.jsonl'),JSON.stringify({time:new Date().toISOString(),...row})+'\n');}catch{}
 }});
 ctx.on('tools/pre-execute',(exec,next)=>repeats.before(exec)||next(),{prepend:true});
 const reasoning=createReasoningRouter(rpc); ctx.on('agent/pre-step',async({agent,step,signal},next)=>{
  const deadline=Date.now()+3000;
  while(true){try{const gate=await rpc('gate',{session_id:agent.session.id,step:Number(step),phase:'model_step'});repeats.bind(agent.session,gate.task_id||gate.grant_id);break;}catch(e){if(e.code!=='OBSERVATION_STARTING'||Date.now()>=deadline)throw e;await new Promise(r=>setTimeout(r,100));}}
  repeats.assertCanContinue(agent.session);
  await releaseConsumedHistory(agent.session,ctx.sessionQuery,signal);
   reasoning.bind(agent);
  return next();
 },{prepend:true});
 ctx.tools.guard(ex=>excludedRuntimeTool(ex?.name||'')?'本安装使用者已从运行时移除此工具族。':mayExpose(ex?.name||'')?undefined:'当前为只读或受限验收状态，此工具会产生修改。');
 ctx.on('system-prompt/assemble',async(_a,_c,next)=>{
  const a=await next();const tools=filterRuntimeTools(a.tools).filter(t=>mayExpose(t.name));
  // Metadata only: prove the final assembled catalog, never persist prompt bodies.
  try{fs.appendFileSync(path.join(root,'data','tool-catalog-audit.jsonl'),JSON.stringify({time:new Date().toISOString(),session_id:_c?.session?.id,policy_version:ownerDecisions.version,offered:tools.map(t=>t.name),removed:a.tools.filter(t=>excludedRuntimeTool(t.name)).map(t=>t.name)})+'\n');}catch{}
  return {...a,tools};
 },{prepend:true});
 // 自动审查 is a per-session permission identity, not a composed default: the
 // permission layer only offers "auto" while the experimental reviewer is
 // mounted, and the reviewer only inspects calls whose session is on "auto".
 // The runtime starts on the configured workspace-write+ask pair, so each space
 // session is re-pinned to "auto" here (once the reviewer is registered).
 const pinLog=(row)=>{try{fs.appendFileSync(path.join(root,'data','permission-auto.jsonl'),JSON.stringify({time:new Date().toISOString(),...row})+'\n');}catch{}};
 ctx.inject(['permissionPresets','sessions'],pctx=>{
  const presets=pctx.permissionPresets;
  const pin=session=>{
   try{
    if(!hostToolsAllowed())return;
    const desired=permissionPresetName();
    if(!presets.names.includes(desired))return false;
    if(presets.current(session)===desired)return true;
    presets.set(session,desired);
    pinLog({session_id:session?.id,event:'pinned_owner_preset',preset:desired,config:permissionPreset()});
    return true;
   }catch(e){pinLog({session_id:session?.id,event:'pin_failed',error:String(e?.message||e)});return false;}
  };
  const ensure=session=>{
   if(pin(session))return true;
   let tries=0;
   const timer=setInterval(()=>{tries+=1;if(pin(session)!==false||tries>=40)clearInterval(timer);},250);
   timer.unref?.();
   return false;
  };
  pctx.on('agent/pre-step',({agent},next)=>{if(!controlled()&&pin(agent.session)!==true)throw Error('配置的权限档尚不可用，未开始执行');return next();},{prepend:true});
  pctx.on('session/created',session=>{ensure(session);});
  for(const session of pctx.sessions.list())ensure(session);
 });
 ctx.on('llm/stream',(options,next)=>(async function*(){
  if(options.model!=='deepseek-flash'||options.provider!=='deepseek-official')throw Error('FLASH_ONLY');
  // 内部请求：@deepseek-ai/dsh-experimental-auto-review 的审查器直接调 ctx.llm.stream，
  // 不带 sessionId（它是授权判定，不是主人任务的回合，也不该记进会话思考档或受会话
  // 上下文闸限制）。早先这里无条件要求 sessionId，导致审查器每次都抛
  // SESSION_ID_REQUIRED，所有工具调用都被 "Auto review of tool ... failed" 拦掉。
  // 现在：无 sessionId 的请求走独立 consumer 计费；预留失败记录诊断并停止请求，
  // 审查请求也受主人预算约束。
  const internalId=typeof options.sessionId==='string'?null:randomUUID();
  if(internalId){
   const internalBytes=Buffer.byteLength(JSON.stringify(options));
   let reserved=false;
   try{
    await rpc('budget/reserve',{id:internalId,upper_cny:(internalBytes+24000)*2/1e6+8192*8/1e6,consumer:'space-internal'});
    reserved=true;
   }catch(e){
    try { fs.appendFileSync(path.join(root,'data','internal-reserve-failed.jsonl'),
      JSON.stringify({time:new Date().toISOString(),error:String(e?.message||e),bytes:internalBytes})+'\n'); } catch {}
    throw e;
   }
   let internalUsage=null;
   try{for await(const chunk of next()){
    if(chunk.type==='usage')internalUsage=structuredClone(chunk.usage);
    yield chunk;
   }}finally{
    if(reserved&&internalUsage&&Number.isSafeInteger(internalUsage.inputTokens)&&Number.isSafeInteger(internalUsage.outputTokens)){
     const cost=((internalUsage.inputTokens+(internalUsage.cacheWriteTokens||0))*2+(internalUsage.cacheReadTokens||0)*.04+internalUsage.outputTokens*8)/1e6;
     const row={id:internalId,actual_cny:cost,usage:internalUsage,reasoning_effort:'internal',
       price_basis:'2026-10-21 official peak conservative estimate',time:new Date().toISOString()};
     try{
      fs.appendFileSync(path.join(root,'data','usage-recovery.jsonl'),JSON.stringify(row)+'\n');
      await rpc('budget/settle',row);
     }catch(e){
      fs.appendFileSync(path.join(root,'data','internal-settle-failed.jsonl'),
        JSON.stringify({time:new Date().toISOString(),error:String(e?.message||e)})+'\n');
     }
    }
   }
   return;
  }
  const currentSession=options.sessionId;
  if(typeof currentSession!=='string'||!/^session-[A-Za-z0-9_-]+$/.test(currentSession))throw Error('SESSION_ID_REQUIRED');
  const id=randomUUID();
   const selection=reasoning.snapshot(currentSession,options.reasoningEffort);
  const inputBytes=Buffer.byteLength(JSON.stringify(options));
  // Byte-only diagnostics: never persist credentials, prompts or image payloads here.
  fs.appendFileSync(path.join(root,'data','context-metrics.jsonl'),JSON.stringify({
    session_id:currentSession,time:new Date().toISOString(),input_bytes:inputBytes,
    parts:Object.fromEntries(Object.entries(options).map(([k,v])=>[k,Buffer.byteLength(JSON.stringify(v)??'')]))
  })+'\n');
  const limits=policy();
  if(inputBytes>limits.inputBytes)throw Error('工作上下文达到上限，请先按需回忆并整理；原记录保留。');
  if(inputBytes>limits.inputBytes*limits.softRatio)fs.appendFileSync(path.join(root,'data','context-pressure.jsonl'),JSON.stringify({
    session_id:currentSession,time:new Date().toISOString(),input_bytes:inputBytes,limit:limits.inputBytes,
    ratio:Number((inputBytes/limits.inputBytes).toFixed(3))
  })+'\n');
  const upper=(inputBytes+24000)*2/1e6+8192*8/1e6;
  await rpc('budget/reserve',{id,upper_cny:upper,consumer:'space-runtime:'+currentSession});
   await reasoning.record({session_id:currentSession,request_id:id,
       task_id:selection.task_id,step:selection.step,level:options.reasoningEffort});
  let usage=null;
  try{for await(const chunk of next()){
   if(chunk.type==='usage')usage=structuredClone(chunk.usage);
   yield chunk;
  }}finally{
   if(usage&&Number.isSafeInteger(usage.inputTokens)&&Number.isSafeInteger(usage.outputTokens)){
    const cost=((usage.inputTokens+(usage.cacheWriteTokens||0))*2+(usage.cacheReadTokens||0)*.04+usage.outputTokens*8)/1e6;
     const row={id,actual_cny:cost,usage,reasoning_effort:options.reasoningEffort,
       price_basis:'2026-10-21 official peak conservative estimate',time:new Date().toISOString()};
    fs.appendFileSync(path.join(root,'data','usage-recovery.jsonl'),JSON.stringify(row)+'\n');
    await rpc('budget/settle',row);
   }
  }
 })(),{prepend:true});
}
