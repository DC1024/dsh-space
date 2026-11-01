import test from 'node:test';
import assert from 'node:assert/strict';
import {createActionRepeatGuard,actionFingerprint,REPEAT_CODE} from './runtime-action-repeat.mjs';

function fixture(){
 const audit=[],guard=createActionRepeatGuard({onBlocked:r=>audit.push(r)}),session={id:'session-biao'};
 guard.bind(session,'task-one');
 const call=(name='pwsh',args={command:'Write-Output hello'},extra={})=>({agent:{session},name,arguments:args,...extra});
 return {audit,guard,session,call};
}

test('thirty native actions execute; thirty-first and later batch actions do not',()=>{
 const f=fixture();let bodies=0;
 for(let i=0;i<35;i++)if(!f.guard.before(f.call()))bodies++;
 assert.equal(bodies,30);assert.equal(f.audit.length,1);
 assert.equal(f.audit[0].blocked_attempt,31);
 assert.throws(()=>f.guard.assertCanContinue(f.session),{code:REPEAT_CODE});
 assert.equal(f.guard.before(f.call('read',{file_path:'different'})).kind,'deny');
 assert.ok(!JSON.stringify(f.audit).includes('Write-Output'));
});

test('different parameters and different tools reset consecutive count; long work continues',()=>{
 const f=fixture();
 for(let i=0;i<300;i++)assert.equal(f.guard.before(f.call('read',{offset:i*100,path:'book'})),undefined);
 for(let i=0;i<29;i++)assert.equal(f.guard.before(f.call()),undefined);
 assert.equal(f.guard.before(f.call('read',{path:'progress'})),undefined);
 for(let i=0;i<30;i++)assert.equal(f.guard.before(f.call()),undefined);
 assert.equal(f.guard.before(f.call()).info.code,REPEAT_CODE);
});

test('new owner task resets a blocked session; independent personas do not share counts',()=>{
 const f=fixture(),other={id:'session-li'};f.guard.bind(other,'task-li');
 for(let i=0;i<31;i++)f.guard.before(f.call());
 assert.equal(f.guard.before({...f.call(),agent:{session:other}}),undefined);
 f.guard.bind(f.session,'task-two');f.guard.assertCanContinue(f.session);
 assert.equal(f.guard.before(f.call()),undefined);
});

test('MCP and PTC inner calls use the same boundary; wrapper is not double counted',()=>{
 const f=fixture();
 for(let i=0;i<30;i++){
  assert.equal(f.guard.before(f.call('run_code',{code:'await tools.read()'})),undefined);
  assert.equal(f.guard.before(f.call('mcp__space__desktop_screen',{}, {parent:{},callId:'call-'+i})),undefined);
 }
 assert.equal(f.guard.before(f.call('mcp__space__desktop_screen',{}, {parent:{}})).kind,'deny');
});

test('JSON key order and pwsh display descriptions do not evade matching',()=>{
 assert.equal(actionFingerprint('read',{a:1,b:{d:2,c:3}}),actionFingerprint('read',{b:{c:3,d:2},a:1}));
 assert.equal(actionFingerprint('pwsh',{command:'x',description:'first'}),actionFingerprint('pwsh',{description:'next',command:'x'}));
 assert.notEqual(actionFingerprint('pwsh',{command:'x'}),actionFingerprint('pwsh',{command:'y'}));
 assert.notEqual(actionFingerprint('read',{text:'a b'}),actionFingerprint('read',{text:'ab'}));
});

test('synchronous admission counts concurrent calls before bodies start',async()=>{
 const f=fixture();let bodies=0;
 await Promise.all(Array.from({length:45},async()=>{if(!f.guard.before(f.call())){await Promise.resolve();bodies++;}}));
 assert.equal(bodies,30);
});
