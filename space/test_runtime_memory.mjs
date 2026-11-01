import test from 'node:test';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {consumedHistory,releaseHistoryResults,releaseConsumedHistory,RELEASE_MARKER} from './runtime-memory.mjs';
const runtime='C:/dsh-deps/DSH_Official/official-runtime-0.2.1-rc.2/dsh/node_modules/@deepseek-ai/';
const {Session}=await import(pathToFileURL(runtime+'dsh-session/lib/index.js'));
const {createAssistantMessage,createToolResultMessage,createUserMessage}=await import(pathToFileURL(runtime+'dsh-llm/lib/index.js'));

function fixture(tool='mcp__space__context_read'){
 const session=Session.create('session-memory-test');
 const owner=createUserMessage({source:{kind:'direct'},content:[{type:'text',text:'当前目标保留'}]});
 session.append('user/message',owner,{surfaceOp:'append'});
 const call={type:'tool-call',id:'call-history',name:tool,arguments:'{"source_id":"task:archive"}'};
 session.append('assistant/message',{turn:1,step:1,stream:[],message:createAssistantMessage({source:{provider:'deepseek-official',model:'deepseek-flash'},content:[call]})},{surfaceOp:'append'});
 const callEvent=session.append('tool/call',{turn:1,step:1,callId:call.id,name:tool,arguments:call.arguments});
 const result=session.append('tool/result',{turn:1,step:1,message:createToolResultMessage({callId:call.id,isError:false,content:[{type:'text',text:'OLD_DETAIL_UNIQUE_MARKER'}]})},{surfaceOp:'append',sourceEventSeqs:[callEvent.seq]});
 const events=[result];
 function consumed(){session.append('assistant/message',{turn:1,step:2,stream:[],message:createAssistantMessage({source:{provider:'deepseek-official',model:'deepseek-flash'},content:[{type:'text',text:'当前任务判断与进度'}]})},{surfaceOp:'append'});}
 return {session,result,events,consumed};
}

test('history is present for the first model step, released only after consumption',()=>{
 const f=fixture();assert.equal(consumedHistory(f.session.deriveMessages()).length,0);
 f.consumed();const candidates=consumedHistory(f.session.deriveMessages());assert.equal(candidates.length,1);
 const rawBefore=JSON.stringify(f.result);
 assert.equal(releaseHistoryResults(f.session,f.events,candidates),1);
 const surface=JSON.stringify(f.session.deriveMessages());
 assert.ok(!surface.includes('OLD_DETAIL_UNIQUE_MARKER'));assert.ok(surface.includes(RELEASE_MARKER));
 assert.ok(surface.includes('当前目标保留'));assert.ok(surface.includes('当前任务判断与进度'));
 assert.equal(JSON.stringify(f.result),rawBefore);assert.ok(rawBefore.includes('OLD_DETAIL_UNIQUE_MARKER'));
 assert.equal(consumedHistory(f.session.deriveMessages()).length,0);
});

test('native replay retains released working surface and intact original events',()=>{
 const f=fixture();f.consumed();releaseHistoryResults(f.session,f.events,consumedHistory(f.session.deriveMessages()));
 // Native constructor validates the persisted replacement, including identity.
 const restored=Session.create(f.session.id,f.session.snapshotEvents());
 assert.ok(!JSON.stringify(restored.deriveMessages()).includes('OLD_DETAIL_UNIQUE_MARKER'));
 assert.ok(JSON.stringify(restored.snapshotEvents()).includes('OLD_DETAIL_UNIQUE_MARKER'));
 const old=f.result.data.message, current=restored.deriveMessages().find(m=>m.id===old.id);
 for(const key of ['id','role','toolCallId','isError'])assert.equal(current[key],old[key]);
 assert.deepEqual(current.source,old.source);
});

test('current device receipts and owner messages never become expiry targets',()=>{
 for(const name of ['mcp__space__phone_screen','mcp__space__desktop_click','mcp__space__action_log_read','untrusted_context_read']){
  const f=fixture(name);f.consumed();assert.deepEqual(consumedHistory(f.session.deriveMessages()),[]);
 }
});

test('stale surface and aborted reads cannot rewrite the native log',async()=>{
 const f=fixture();f.consumed();const before=f.session.seq;
 const query={async readSurface(){f.session.append('user/message',createUserMessage({source:{kind:'direct'},content:[{type:'text',text:'新指令'}]}),{surfaceOp:'append'});return {events:f.events};}};
 assert.equal(await releaseConsumedHistory(f.session,query),0);assert.equal(f.session.seq,before+1);
 const controller=new AbortController();controller.abort();
 await assert.rejects(releaseConsumedHistory(f.session,{async readSurface(){return {events:f.events};}},controller.signal));
 assert.ok(JSON.stringify(f.session.deriveMessages()).includes('OLD_DETAIL_UNIQUE_MARKER'));
});

test('surface reads are avoided when no consumed history exists',async()=>{
 const f=fixture();let reads=0;
 assert.equal(await releaseConsumedHistory(f.session,{readSurface(){reads++;throw Error('unneeded');}}),0);
 assert.equal(reads,0);
});
