import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {excludedRuntimeTool,filterRuntimeTools,ownerDecisionSection} from './runtime-tool-policy.mjs';

test('excluded families cover native and MCP names without hiding unrelated tools',()=>{
 for(const prefix of ['bili_','vision_'])for(const wrapper of ['', 'mcp__space__', 'mcp__other-server__']){
  assert.equal(excludedRuntimeTool(wrapper+prefix+'read'),true);
 }
 for(const name of ['read','pwsh','mcp__space__workspace_write','mcp__vision_server__screen','revision_read','bilibili'])assert.equal(excludedRuntimeTool(name),false);
 assert.deepEqual(filterRuntimeTools([{name:'bili_read'},{name:'vision_read'},{name:'pwsh'}]),[{name:'pwsh'}]);
 assert.ok(ownerDecisionSection().includes('Apache-2.0 + 薄边界 + 完全权限零限制'));
});

test('real runtime hooks remove catalog entries and deny direct dispatch in every preset',async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-tool-policy-'));
 try{
  for(const f of ['runtime-policy.mjs','runtime-action-repeat.mjs','runtime-tool-policy.mjs','runtime-memory.mjs','runtime-reasoning.mjs','OWNER_DECISIONS.json'])fs.copyFileSync(new URL('./'+f,import.meta.url),path.join(dir,f));
  fs.mkdirSync(path.join(dir,'data'));
  const {apply}=await import(pathToFileURL(path.join(dir,'runtime-policy.mjs')));
  const hooks=new Map();let guard;
  apply({tools:{guard(fn){guard=fn;}},on(name,fn){hooks.set(name,fn);},inject(){}});
  const catalog=[{name:'bili_read'},{name:'mcp__example__vision_caption'},{name:'pwsh'},{name:'mcp__space__workspace_read'}];
  for(const preset of ['read-only','workspace-write','auto','full','danger-full-access']){
   fs.writeFileSync(path.join(dir,'config.json'),JSON.stringify({permission_preset:preset}));
   const result=await hooks.get('system-prompt/assemble')({}, {session:{id:'fixture-only'}},async()=>({tools:catalog}));
   assert.deepEqual(result.tools.map(t=>t.name),['pwsh','mcp__space__workspace_read']);
   assert.match(guard({name:'bili_read'}),/使用者已从运行时移除/);
   assert.match(guard({name:'mcp__example__vision_caption'}),/使用者已从运行时移除/);
   assert.equal(guard({name:'pwsh'}),undefined);
  }
 }finally{
  assert.equal(path.dirname(path.resolve(dir)),path.resolve(os.tmpdir()));
  assert.ok(path.basename(dir).startsWith('dsh-tool-policy-'));
  fs.rmSync(dir,{recursive:true,force:true});
 }
});

test('real runtime hooks permit step 151 and prevent the 31st repeated body and next model step',async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-repeat-hooks-'));
 const previousFetch=globalThis.fetch;
 try{
  for(const f of ['runtime-policy.mjs','runtime-action-repeat.mjs','runtime-tool-policy.mjs','runtime-memory.mjs','runtime-reasoning.mjs','OWNER_DECISIONS.json'])fs.copyFileSync(new URL('./'+f,import.meta.url),path.join(dir,f));
  fs.mkdirSync(path.join(dir,'data'));fs.writeFileSync(path.join(dir,'data','bridge.key'),'offline-fixture-only');
  fs.writeFileSync(path.join(dir,'config.json'),JSON.stringify({permission_preset:'danger-full-access',context:{max_steps:60}}));
  let taskId='task-one';
  globalThis.fetch=async()=>({ok:true,json:async()=>({ok:true,task_id:taskId})});
  const {apply}=await import(pathToFileURL(path.join(dir,'runtime-policy.mjs')));
  const hooks=new Map();apply({tools:{guard(){}},on(n,fn){hooks.set(n,fn);},inject(){}});
  const session={id:'session-test',deriveMessages:()=>[]},agent={session,ctx:{on(){}}};
  const step=()=>hooks.get('agent/pre-step')({agent,step:151},async()=>({continued:true}));
  assert.deepEqual(await step(),{continued:true});
  let bodies=0;
  for(let i=0;i<31;i++)await hooks.get('tools/pre-execute')({agent,name:'read',arguments:{path:'x'}},async()=>{bodies++;return {kind:'allow'};});
  assert.equal(bodies,30);
  await assert.rejects(step(),{code:'REPEATED_ACTION_LIMIT'});
  taskId='task-two';assert.deepEqual(await step(),{continued:true});
 }finally{
  globalThis.fetch=previousFetch;
  assert.equal(path.dirname(path.resolve(dir)),path.resolve(os.tmpdir()));
  assert.ok(path.basename(dir).startsWith('dsh-repeat-hooks-'));fs.rmSync(dir,{recursive:true,force:true});
 }
});
