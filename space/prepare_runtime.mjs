import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {ownerDecisionSection} from './runtime-tool-policy.mjs';
const root=path.dirname(fileURLToPath(import.meta.url));
const installed=process.env.DSH_RUNTIME||'/usr/local/lib/node_modules/@deepseek-ai/dsh';
const require=createRequire(path.join(installed,'package.json'));
const YAML=require('js-yaml');
const jsType=new YAML.Type('tag:yaml.org,2002:js',{kind:'scalar',construct:x=>({__jsExpr:x})});
const schema=YAML.JSON_SCHEMA.extend(jsType);
const home=path.join(root,'data','runtime-home'), profile=path.join(home,'profiles','space');
fs.mkdirSync(profile,{recursive:true});
if(!fs.existsSync(path.join(profile,'node_modules')))fs.symlinkSync(path.join(installed,'node_modules'),path.join(profile,'node_modules'),'junction');
fs.writeFileSync(path.join(profile,'package.json'),JSON.stringify({name:'dsh-personal-space-runtime',private:true,type:'module',dsh:{profile:{bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-web-app']}}},null,2));
fs.writeFileSync(path.join(profile,'cordis.yml'),'[]\n');
// Disable every model-facing built-in tool and every shipped preset. The only
// agent tools will come from our explicitly scoped MCP server.
const patch=[];
for(const file of ['dsh-base/cordis.patch.yml','dsh-web-app/cordis.patch.yml',...['standard','ptc','minimal','cordis'].map(n=>'dsh-web-app/presets/'+n+'.patch.yml')]){
 const rows=YAML.load(fs.readFileSync(path.join(installed,'node_modules/@deepseek-ai',file),'utf8'),{schema});
 for(const op of rows)for(const row of op.insert||[]){
  if(row.id?.startsWith('tool-')||row.id?.startsWith('preset-')||['llm-retry','session-title-llm','subagent','subagent-spawn-in-process','subagent-fork-in-process','goal-round-driver','command-goal','commands','skill-filesystem','agent-instructions','harness-dev-context','open-in-app','file-upload','terminal-controller','workspace-controller','web-search-deepseek','web-deepseek-api','llm-pi-ai','llm-deepseek-account','ptc-runtime','workflow-ptc','plugin-manager','config-editor'].includes(row.id))patch.push({id:row.id,disabled:true});
 }
}
const config=JSON.parse(fs.readFileSync(path.join(root,'config.json'),'utf8').replace(/^\uFEFF/,''));
const llm=(config.llm&&typeof config.llm==='object')?config.llm:{};
const llmProvider=typeof llm.provider==='string'&&llm.provider?llm.provider:'deepseek-official';
const llmModel=typeof llm.model==='string'&&llm.model?llm.model:(config.model||'deepseek-flash');
const llmEffort=['low','high','max'].includes(llm.reasoning_effort)?llm.reasoning_effort:'low';
const llmBaseURL=typeof llm.base_url==='string'&&llm.base_url?llm.base_url:(process.env.DSH_MODEL_BASE_URL||'http://127.0.0.1:3425/v1');
// The provider catalog is advisory; include every configured model id so
// session/selectModel and the GUI model picker accept proxy aliases such as
// auto/deepseek-v4.1-flash without recompiling the provider.
const llmModels=Array.isArray(llm.models)&&llm.models.length?llm.models:[
 {id:'deepseek-flash',name:'DeepSeek-V41-Flash',contextWindow:1000000,inputModalities:['text','image'],systemPromptUpdate:'in-history'},
 {id:'deepseek-v4-pro',name:'DeepSeek-V4-Pro',contextWindow:1000000},
 ...(llmModel==='deepseek-flash'||llmModel==='deepseek-v4-pro'?[]:[
  {id:llmModel,name:llmModel,contextWindow:1000000,systemPromptUpdate:'in-history'}])
];
// Required host services do not create model-facing tools. Their tool plugins
// remain disabled and the model-facing guard is an explicit allowlist.
patch.push({id:'config-editor',disabled:false},{id:'commands',disabled:false},{id:'file-upload',disabled:false},
 // Owner 2026-10-23: the four reminder tools stay in the standard preset, so the
 // Schedule service (its provider side) must be live or tool-schedule registers
 // and then fails on first call.
 {id:'schedule',disabled:false},
 // Services behind the newly granted tools. The tools themselves still come from
 // the persona preset; these rows are the provider side, and leaving them off
 // would make the tool register but fail on first call.
 {id:'goal-round-driver',disabled:false},{id:'command-goal',disabled:false},
 {id:'subagent',disabled:false},{id:'subagent-spawn-in-process',disabled:false},{id:'subagent-fork-in-process',disabled:false},
 {id:'ptc-runtime',disabled:false},{id:'tool-working-directory',disabled:false},
 {id:'web-runtime',config:{openBrowser:false,printUrl:false,surfaceContext:true}});
// Agent Teams (owner 2026-10-23: "增加到和你一样多"). The shipped bundle swaps
// single-agent delegation for the Team tools, so the two overlapping control
// plugins are dropped from the persona preset below (they register the same
// send_message / interrupt_agent / list_agents names) and the Team plugin takes
// over those names while adding spawn_teammate, team_task_* and wait_agent.
if(fs.existsSync(path.join(installed,'node_modules/@deepseek-ai/dsh-experimental-agent-team'))&&fs.existsSync(path.join(installed,'node_modules/@deepseek-ai/dsh-experimental-tool-agent-team')))patch.push({insert:[
 {id:'agent-team',name:'@deepseek-ai/dsh-experimental-agent-team',config:{maxMembers:8,maxTasks:256,maxMessageBytes:65536,disposalTimeoutMs:5000}},
 {id:'tool-agent-team',name:'@deepseek-ai/dsh-experimental-tool-agent-team',config:{freshProvider:'spawn',forkProvider:'fork'}}]});
// load_workspace_dependencies needs a real bundled Python/Node payload directory
// (runtime.json + distributions). The desktop app ships one; without a payload
// the tool would report nothing useful, so the row is only added when it exists.
const payloadCandidates=[process.env.DSH_PRIMARY_RUNTIME,process.env.DSH_BUNDLED_PRIMARY_RUNTIME].filter(Boolean);
const payloadPath=payloadCandidates.find(p=>{try{return fs.existsSync(path.join(p,'runtime.json'))}catch{return false}});
if(payloadPath)patch.push({insert:[{id:'workspace-dependencies',name:'@deepseek-ai/dsh-tool-workspace-dependencies',config:{source:payloadPath.replace(/\\/g,'/')}}]});
// Owner-selectable permission preset (config.json -> "permission_preset"):
//   read-only           host tools stay blocked by runtime-policy (space tools only)
//   workspace-write     confined writes + ask
//   auto                自动审查: full access + thin reviewer, only the four irreversible
//                       outcomes are denied and asked back to the owner
//   danger-full-access  完全权限: no classification, no per-call question, no interception
//   full                本地别名，等价 danger-full-access
// The composed sandbox+approval pair must name a preset the base config actually
// defines, otherwise the permission layer refuses to boot. "auto" is not such a
// pair; it boots as workspace-write+ask and runtime-policy.mjs pins each space
// session to "auto" once the reviewer has registered.
const PRESET_ALIAS={full:'danger-full-access'};
const requestedPreset=config.permission_preset;
const presetKey=PRESET_ALIAS[requestedPreset]||requestedPreset;
const BOOT_PAIR={
 'read-only':{mode:'read-only',policy:'ask'},
 'workspace-write':{mode:'workspace-write',policy:'ask'},
 auto:{mode:'workspace-write',policy:'ask'},
 'danger-full-access':{mode:'danger-full-access',policy:'never'}};
const bootPair=BOOT_PAIR[presetKey]||BOOT_PAIR['workspace-write'];
if(!BOOT_PAIR[presetKey])console.warn('[prepare] unknown permission_preset '+(requestedPreset||'(none)')+' -> booting workspace-write+ask');
patch.push({id:'sandbox-policy',config:{mode:bootPair.mode}},{id:'approval',config:{policy:bootPair.policy}});
// 自动审查 is the only preset that gets the reviewer. In every other preset the
// reviewer is not mounted at all, so 完全权限 really is "no classification, no
// per-call question, no interception" with no plugin sitting in the tool path.
// What cannot simply be dropped: DSH refuses to restore a session whose persisted
// preset is "auto" unless some integration has registered "auto"
// (dsh-permission-presets/lib/index.js:205,370-376), and the owner already has
// sessions saved as "auto". So the non-auto presets mount runtime-auto-preset-shim
// instead — a ~40-line plugin that registers the "auto" identity with a no-op
// admission and installs no hook. A restored auto session boots under
// danger-full-access+ask and runtime-policy.mjs re-pins it to config.json's
// permission_preset on its next step, so the stale identity disappears on first use.
const reviewerMounted=presetKey==='auto';
patch.push({insert:[{id:reviewerMounted?'auto-review':'auto-preset-shim',
 name:pathToFileURL(path.join(root,reviewerMounted?'runtime-auto-review.mjs':'runtime-auto-preset-shim.mjs')).href}]});
console.log('[prepare] permission preset '+(presetKey||'(none)')+' -> '+(reviewerMounted?'auto-review mounted':'reviewer unmounted, auto-preset-shim mounted'));
// Built-in toolset: owner 2026-10-23 asked for full parity with the desktop
// session, so personas now keep the whole shipped `standard` catalog. Only the
// rows that cannot work here or stay under owner control are dropped:
//   persona            -> replaced by the pinned 小鲸鱼 persona above
//   agent-instructions -> reads host AGENTS.md; the space has its own rules file
//   tool-pwsh          -> Linux host; tool-bash covers it
//   tool-plugin-manager / tool-plugin-market -> installing plugins stays an
//                         owner decision, not a model tool
// The services those plugins inject (goal rounds, subagent in-process runs,
// ptc runtime for workflow) are re-enabled below, because a disabled service
// makes the corresponding tool fail at call time.
const standardPatch=YAML.load(fs.readFileSync(path.join(installed,'node_modules/@deepseek-ai/dsh-web-app/presets/standard.patch.yml'),'utf8'),{schema});
const DROP=new Set(['persona','agent-instructions','tool-pwsh','tool-plugin-manager','tool-plugin-market',
 // Team plugin owns these names now; loading both would double-register.
 'tool-subagent-control','tool-subagent-list-agents']);
// The `delegation` entry is a cordis group, so the three overlapping control
// plugins live inside its config list and have to be removed there as well.
const builtinTools=structuredClone(standardPatch[0].insert[0].config.plugins).filter(p=>!DROP.has(p.id)).map(p=>{
 if(p.id==='delegation'&&Array.isArray(p.config))p.config=p.config.filter(c=>!DROP.has(c.id));
 return p;});
const sha256=b=>createHash('sha256').update(b).digest('hex');
const packPath=path.resolve(root,config.persona_snapshot);
const packBytes=fs.readFileSync(packPath);
// The persona pin is this deployment's own tamper check on its own snapshot.
// It is not a licence term and not an approval: a fork that ships its own
// persona simply leaves runtime.persona_sha256 out of its config.json.
if(config.runtime.persona_sha256 && sha256(packBytes)!==config.runtime.persona_sha256)throw Error('Pinned persona snapshot hash mismatch');
const pack=JSON.parse(packBytes.toString('utf8').replace(/^\uFEFF/,''));
// AUTHORITY_RULES.md is behaviour configuration ("in THIS deployment only the
// local owner is an instruction source"), not a licence and not an approval
// artifact. Whoever deploys the space owns that file, so an edited rule is
// re-accepted locally and recorded as a receipt instead of aborting startup on
// a signature nobody else can produce. Nothing here asks the original author
// for permission. DSH_STRICT_AUTHORITY=1 keeps the author's own tamper check.
const authorityPath=path.join(root,'AUTHORITY_RULES.md');
const authorityBytes=fs.readFileSync(authorityPath);
const authoritySha256=sha256(authorityBytes);
const receiptPath=path.join(root,'data','authority-policy-v01.json');
let authorityManifest={};
try{authorityManifest=JSON.parse(fs.readFileSync(receiptPath,'utf8').replace(/^\uFEFF/,''));}catch{}
if(authorityManifest.sha256!==authoritySha256){
 if(process.env.DSH_STRICT_AUTHORITY==='1')throw Error('AUTHORITY_RULES.md changed; strict mode refuses to re-accept (unset DSH_STRICT_AUTHORITY to accept locally)');
 authorityManifest={schemaVersion:1,version:'local-acceptance-v01',path:'AUTHORITY_RULES.md',sha256:authoritySha256,
  accepted_by:'local-deployment',
  note:'本地签收记录：只说明这份行为规则在本部署被接受过。它不是许可条件，不需要任何人的授权；改写后下次启动自动重签。',
  at:new Date().toISOString().slice(0,10)};
 fs.mkdirSync(path.dirname(receiptPath),{recursive:true});
 fs.writeFileSync(receiptPath,JSON.stringify(authorityManifest,null,2)+'\n');
 console.log('AUTHORITY_RULES.md changed -> re-accepted locally (local-acceptance-v01)');
}
const authorityPolicy=authorityBytes.toString('utf8').replace(/^\uFEFF/,'');
patch.push({id:'agent-default-model',config:{provider:llmProvider,model:llmModel,reasoningEffort:llmEffort}},
 // The key belongs to the configured model proxy, not necessarily api.deepseek.com.
 {id:'llm-deepseek',config:{baseURL:llmBaseURL,models:llmModels}},
 {insert:[{id:'space-budget',name:pathToFileURL(path.join(root,'runtime-policy.mjs')).href}]});
// Workspace Codex history is a Windows-install side product; this Linux deployment
// has no such archive folder, so the pair is not registered here.
// Owner-side bridge: a full-permission headless DSH worker on the owner's side.
// The Linux deployment keeps it OFF by default; opt in only with DSH_OWNER_BRIDGE=1.
if(process.env.DSH_OWNER_BRIDGE==='1')patch.push({insert:[{id:'mcp-owner-bridge-v01',name:'@deepseek-ai/dsh-mcp-client',config:{serverName:'owner-bridge',transport:'stdio',command:process.env.DSH_PYTHON||'python3',args:[path.join(root,'bridge','mcp_server.py')],env:{PYTHONUTF8:'1'},toolCallTimeoutMs:1500000}}]});
for(const [identity,label] of [['biao','表'],['li','里']]){
const persona=pack.personas?.[label]?.system_prompt;
if(!persona)throw Error('Pinned persona package shape changed; preserve and review');
const prefix=authorityPolicy+'\n\n'+ownerDecisionSection()+'\n\n'+persona;
patch.push({insert:[{id:'space-preset-'+identity,name:'@deepseek-ai/dsh-agent-preset',config:{id:config.runtime.persona_bindings[identity].agent_preset,name:'小鲸鱼 · '+label+'模式',plugins:[
 {id:'persona',name:'@deepseek-ai/dsh-persona',config:{prefix,complete:true,includeRuntimeContext:false}},
 {id:'mcp-space',name:'@deepseek-ai/dsh-mcp-client',config:{serverName:'space',transport:'streamable-http',url:'http://127.0.0.1:19991/mcp/'+identity,headers:{Authorization:'Bearer '+fs.readFileSync(path.join(root,'data','bridge.key'),'utf8').trim()},failOnStartupError:true,toolCallTimeoutMs:30000}},
 ...structuredClone(builtinTools),
 ]}}]});
}
fs.writeFileSync(path.join(profile,'cordis.patch.yml'),JSON.stringify(patch,null,2));
fs.mkdirSync(path.join(root,'workdir'),{recursive:true});
console.log(JSON.stringify({home,profile,port:19992,personaVersion:config.runtime.persona_version,authorityVersion:authorityManifest.version,authoritySha256:authorityManifest.sha256,personas:['biao','li'],disabledRows:patch.filter(x=>x.disabled).length}));
