import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function setup(){
 const nodes=new Map(),streams=[],messages=[];
 const element=()=>({value:'',dataset:{},style:{},children:[],replaceChildren(){this.children=[];},append(...x){this.children.push(...x);},prepend(x){this.children.unshift(x);},setAttribute(){},addEventListener(){}});
 const ctx=vm.createContext({console,setTimeout,clearTimeout,crypto:{randomUUID:()=> 'test-id'},
  localStorage:{getItem:()=>null,setItem(){},removeItem(){}},window:{addEventListener(){}},
  document:{getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},createElement:element},
  EventSource:class {constructor(url){this.url=url;streams.push(this);}close(){this.closed=true;}addEventListener(){}},
  fetch:async()=>({ok:true,json:async()=>ctx.next}), messages});
 const code=fs.readFileSync(new URL('./dist/app-v02.js',import.meta.url),'utf8').replace('refresh();setInterval(refresh,5000);','');
 vm.runInContext(code,ctx);
 vm.runInContext('renderStatus=()=>{};renderTaskReview=()=>{};renderApprovals=()=>{};loadActions=()=>{};loadMemories=()=>{};connect=()=>{};addMessage=(...x)=>messages.push(x);view="biao";state={runtime:{session_id:"session-old"}};connectThinking();',ctx);
 return {ctx,streams,messages};
}

test('same-persona continuity reconnects once and ignores late old-session frames',async()=>{
 const {ctx,streams}=setup();
 ctx.next={active_persona:'biao',csrf:'test',runtime:{session_id:'session-new'}};
 await vm.runInContext('refresh()',ctx);
 assert.equal(streams.length,2);assert.equal(streams[0].closed,true);
 assert.match(streams[1].url,/session-new/);
 streams[0].onmessage({data:JSON.stringify({sessionId:'session-old',model:'deepseek-flash',seq:999,kind:'reasoning.delta',text:'stale'})});
 assert.equal(vm.runInContext('thinkingSeq',ctx),0);
 await vm.runInContext('refresh()',ctx);assert.equal(streams.length,2);
});

test('classified failure is visible without fabricating an assistant reply',()=>{
 const {ctx,messages}=setup();
 ctx.e={persona:'biao',seq:1,time:new Date().toISOString(),kind:'task.result',payload:{status:'stopped',text:'',failure:{code:'PROVIDER_BALANCE_EMPTY',message:'DeepSeek 账户余额不足'}}};
 vm.runInContext('onEvent(e)',ctx);
 assert.equal(messages[0][0],'DeepSeek 账户余额不足');
 assert.match(messages[0][2],/停止/);
});
