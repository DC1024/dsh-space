import test from 'node:test';
import assert from 'node:assert/strict';
import {createReasoningRouter} from './runtime-reasoning.mjs';

function fixture(){
 const levels=new Map(),handlers=new Map(),calls=[];
 const router=createReasoningRouter(async(action,body)=>{
  calls.push({action,...body});
  return {task_id:'task-'+body.session_id,level:levels.get(body.session_id)||'low',step:body.step};
 });
 const agent=id=>({session:{id},ctx:{on(name,fn){handlers.set(id,fn);}}});
 const request=async(id)=>handlers.get(id)({step:1},async()=>Object.freeze({
  provider:'unrelated-provider',model:'other-model',reasoningEffort:'high',maxTokens:1}));
 return {router,agent,request,levels,calls};
}

test('actual returned request overrides old high with selected low before it is frozen',async()=>{
 const f=fixture();f.router.bind(f.agent('session-a'));
 const options=await f.request('session-a');
 assert.equal(options.reasoningEffort,'low');
 assert.equal(options.provider,'deepseek-official');assert.equal(options.model,'deepseek-flash');
 assert.equal(options.maxTokens,8192);
 assert.equal(f.router.snapshot('session-a',options.reasoningEffort).level,'low');
});

test('later raise or lower does not mutate an already prepared request',async()=>{
 const f=fixture();f.router.bind(f.agent('session-a'));
 const first=Object.freeze(await f.request('session-a'));
 const snapshot=f.router.snapshot('session-a','low');
 f.levels.set('session-a','max');const second=await f.request('session-a');
 assert.equal(second.reasoningEffort,'max');assert.equal(first.reasoningEffort,'low');
 assert.equal(snapshot.level,'low');assert.ok(Object.isFrozen(snapshot));
 f.levels.set('session-a','low');assert.equal((await f.request('session-a')).reasoningEffort,'low');
});

test('two agent-scoped request hooks cannot lend levels to one another',async()=>{
 const f=fixture();f.levels.set('session-a','max');
 f.router.bind(f.agent('session-a'));f.router.bind(f.agent('session-b'));
 const [a,b]=await Promise.all([f.request('session-a'),f.request('session-b')]);
 assert.equal(a.reasoningEffort,'max');assert.equal(b.reasoningEffort,'low');
 assert.equal(f.calls.length,2);
 assert.throws(()=>f.router.snapshot('session-b','max'),/NOT_PREPARED/);
});

test('an invalid selection fails before request dispatch',async()=>{
 const f=fixture();f.levels.set('session-a','off');f.router.bind(f.agent('session-a'));
 await assert.rejects(f.request('session-a'),/INVALID_REASONING_SELECTION/);
 assert.throws(()=>f.router.snapshot('session-a','off'),/NOT_PREPARED/);
});
