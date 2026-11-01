// Read-only, one-session native follower. No Host-wide events or action RPCs.
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';

const BRIDGE=fileURLToPath(new URL('./dsh-client.mjs',import.meta.url));
const INSTALLED_RUNTIME='C:\\dsh-deps\\DSH_Official\\official-runtime-0.2.1-rc.2\\dsh\\node_modules\\@deepseek-ai\\dsh-desktop-host\\lib\\cli.js';
const SID=/^session-[A-Za-z0-9][A-Za-z0-9_-]{0,179}$/;
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const fail=code=>{throw Object.assign(Error(code),{code});};
const check=(condition,code)=>{if(!condition)fail(code);};

export function validateThinkingConfig(value){
 check(object(value),'CONFIG_INVALID');
 check(Object.keys(value).every(key=>['home','base','sessionId','maxMessages'].includes(key)),'CONFIG_UNKNOWN_FIELD');
 check(typeof value.home==='string'&&value.home.length>0&&value.home.length<=2000&&!/[\x00-\x1f]/.test(value.home)&&
   (path.isAbsolute(value.home)||path.win32.isAbsolute(value.home))&&!/^(?:\\\\|\/\/)/.test(value.home),'CONFIG_HOME_INVALID');
 check(typeof value.base==='string'&&/^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::\d{1,5})?\/?$/i.test(value.base),'CONFIG_BASE_INVALID');
 let base;try{base=new URL(value.base);}catch{fail('CONFIG_BASE_INVALID');}
 check(['http:','https:'].includes(base.protocol)&&!base.username&&!base.password&&base.pathname==='/'&&!base.search&&!base.hash,'CONFIG_BASE_INVALID');
 check(typeof value.sessionId==='string'&&SID.test(value.sessionId),'CONFIG_SESSION_INVALID');
 check(value.maxMessages===undefined||value.maxMessages===40,'CONFIG_HISTORY_LIMIT_INVALID');
 return Object.freeze({home:path.resolve(value.home),base:base.origin,sessionId:value.sessionId,maxMessages:40});
}

export function followOpenPayload(config,streamId){
 const valid=validateThinkingConfig(config);check(typeof streamId==='string'&&UUID.test(streamId),'STREAM_ID_INVALID');
 return {type:'open',streamId,endpoint:'session/follow',payload:{args:{request:{address:{kind:'session',sessionId:valid.sessionId},maxMessages:40,assistantStream:true}}}};
}

export function hasForeignSession(value,sid){
 const ids=[value?.sessionId,value?.header?.id,value?.header?.sessionId,value?.address?.sessionId,value?.frame?.sessionId,
   value?.event?.sessionId,value?.event?.data?.sessionId];
 for(const row of Array.isArray(value?.records)?value.records:[])ids.push(row?.sessionId,row?.event?.sessionId,row?.event?.data?.sessionId);
 return ids.some(id=>id!==undefined&&id!==sid);
}

export async function loadThinkingDependencies(){
 // Resolve the installed bridge's dependency first. This installation's bridge
 // has no direct ws dependency, so reuse the already-installed official runtime
 // copy as an explicit fallback. Never install a package or use global WebSocket.
 const bridgeRequire=createRequire(pathToFileURL(BRIDGE));
 let wsPath;try{wsPath=bridgeRequire.resolve('ws');}
 catch{wsPath=createRequire(pathToFileURL(INSTALLED_RUNTIME)).resolve('ws');}
 const [{NodeApiClient},ws]=await Promise.all([import(pathToFileURL(BRIDGE).href),import(pathToFileURL(wsPath).href)]);
 const WebSocket=ws.WebSocket||ws.default;
 return {createClient:(base,timeout)=>new NodeApiClient(base,timeout,{}),createSocket:(url,options)=>new WebSocket(url,options)};
}

/**
 * All I/O is injectable. A run authenticates once and creates at most one WS.
 * It never reconnects; the manager owns snapshot recovery after a new run.
 */
export async function runThinkingRawMux(rawConfig,deps={}){
 const config=validateThinkingConfig(rawConfig);
 process.env.DSH_HOME=config.home;
 const emit=deps.emit||((value)=>process.stdout.write(JSON.stringify(value)+'\n'));
 const now=deps.now||(()=>new Date().toISOString());
 const uuid=deps.uuid||randomUUID;
 const timeoutMs=deps.timeoutMs??15*60*1000,authTimeoutMs=deps.authTimeoutMs??15000;
 check(Number.isSafeInteger(timeoutMs)&&timeoutMs>0&&Number.isSafeInteger(authTimeoutMs)&&authTimeoutMs>0,'TIMEOUT_INVALID');
 const abort=new AbortController();let socket,client,finished=false,opened=false,snapshotSeen=false,listenerCleanup=()=>{},timer,authTimer;
 let resolveDone;
 const done=new Promise(resolve=>{resolveDone=resolve;});
 const status=(state,code)=>{try{emit({type:'transport',state,code,time:now()});}catch{finish('OUTPUT_CLOSED',false);}};
 const stopSocket=()=>{
  if(!socket)return;
  const current=socket;listenerCleanup();
  const swallow=()=>{};current.on?.('error',swallow);
  const forced=setTimeout(()=>{try{current.terminate?.();}catch{}},1000);forced.unref?.();
  const closed=()=>{clearTimeout(forced);current.off?.('error',swallow);current.off?.('close',closed);};
  current.on?.('close',closed);
  try{if(current.readyState===3)closed();else if(current.readyState===0)current.terminate?.();else current.close(1000);}catch{try{current.terminate?.();}catch{}closed();}
 };
 const finish=(code,error=false)=>{
  if(finished)return;finished=true;clearTimeout(timer);clearTimeout(authTimer);deps.signal?.removeEventListener('abort',externalAbort);
  abort.abort();stopSocket();if(client)client.cookie=null;
  if(error)status('error',code);status('disconnected',code);resolveDone({code,opened});
 };
 const externalAbort=()=>finish('STOP_REQUESTED');
 deps.signal?.addEventListener('abort',externalAbort,{once:true});
 if(deps.signal?.aborted){finish('STOP_REQUESTED');return done;}
 timer=setTimeout(()=>finish('TRANSPORT_TIMEOUT',true),timeoutMs);
 authTimer=setTimeout(()=>finish('AUTH_TIMEOUT',true),Math.min(timeoutMs,authTimeoutMs));
 void(async()=>{
  let createClient=deps.createClient,createSocket=deps.createSocket;
  try{
   if(!createClient||!createSocket){const installed=await loadThinkingDependencies();createClient??=installed.createClient;createSocket??=installed.createSocket;}
  }catch{finish('DEPENDENCIES_UNAVAILABLE',true);return;}
  if(finished)return;
  let cookie;
  try{client=createClient(config.base,authTimeoutMs);await client.ensureAuth(abort.signal);cookie=client.cookie;
   check(typeof cookie==='string'&&cookie.length>0&&!/[\r\n]/.test(cookie),'AUTH_UNAVAILABLE');}
  catch{if(!finished)finish('AUTH_FAILED',true);return;}
  clearTimeout(authTimer);if(finished)return;
  const url=new URL('/api/remote.mux',config.base);url.protocol=url.protocol==='https:'?'wss:':'ws:';
  const streamId=uuid();let payload;
  try{payload=followOpenPayload(config,streamId);socket=createSocket(url,{headers:{cookie},followRedirects:false,perMessageDeflate:false,handshakeTimeout:authTimeoutMs,maxPayload:64*1024*1024});}
  catch{finish('SOCKET_CREATE_FAILED',true);return;}finally{cookie=undefined;}
  const open=()=>{
   if(finished||opened)return;opened=true;
   try{socket.send(JSON.stringify(payload));status('connected','FOLLOW_OPEN_SENT');}
   catch{finish('FOLLOW_OPEN_FAILED',true);}
  };
  const message=(data,isBinary)=>{
   if(finished)return;
   if(isBinary===true){finish('BINARY_FRAME_REJECTED',true);return;}
   let frame;
   try{
    const text=typeof data==='string'?data:new TextDecoder('utf-8',{fatal:true}).decode(data);
    frame=JSON.parse(text);check(object(frame)&&typeof frame.type==='string'&&typeof frame.streamId==='string','FRAME_INVALID');
   }catch{finish('FRAME_INVALID',true);return;}
   if(frame.streamId!==streamId)return;
   if(frame.type==='end'){finish('FOLLOW_ENDED');return;}
   if(frame.type==='error'){finish('FOLLOW_ERROR',true);return;}
   if(frame.type!=='item'||!object(frame.value)||!['snapshot','event','assistant-stream'].includes(frame.value.type)){finish('FOLLOW_ITEM_INVALID',true);return;}
   const value=frame.value;
   if(hasForeignSession(value,config.sessionId)||value.type==='snapshot'&&value.header?.id!==config.sessionId){finish('FOREIGN_SESSION_REJECTED',true);return;}
   if(!snapshotSeen&&value.type!=='snapshot'){finish('SNAPSHOT_REQUIRED',true);return;}
   if(value.type==='snapshot')snapshotSeen=true;
   try{emit({type:'native',value});}catch{finish('OUTPUT_CLOSED',true);}
  };
  const error=()=>finish('SOCKET_ERROR',true),close=()=>finish('SOCKET_CLOSED');
  listenerCleanup=()=>{socket.off('open',open);socket.off('message',message);socket.off('error',error);socket.off('close',close);};
  try{socket.on('open',open);socket.on('message',message);socket.on('error',error);socket.on('close',close);}
  catch{finish('SOCKET_API_INVALID',true);}
 })().catch(()=>finish('TRANSPORT_INTERNAL_ERROR',true));
 return done;
}

/** One newline-terminated config; EOF/TERM/INT always closes this observer. */
export async function runThinkingCli({input=process.stdin,output=process.stdout,signals=process,deps={}}={}){
 const abort=new AbortController();let buffered='',started=false,run,ended=false;
 input.setEncoding?.('utf8');
 const emit=value=>output.write(JSON.stringify(value)+'\n');
 const invalid=code=>{emit({type:'transport',state:'error',code,time:new Date().toISOString()});abort.abort();};
 const stop=()=>abort.abort();
 const onData=chunk=>{
  if(ended)return;const text=typeof chunk==='string'?chunk:chunk.toString('utf8');
  if(started){if(text.trim())invalid('CONFIG_EXTRA_INPUT');return;}
  buffered+=text;if(Buffer.byteLength(buffered)>8192){invalid('CONFIG_TOO_LARGE');ended=true;return;}
  const newline=buffered.indexOf('\n');if(newline<0)return;
  const first=buffered.slice(0,newline);if(buffered.slice(newline+1).trim()){invalid('CONFIG_EXTRA_INPUT');ended=true;return;}
  let config;try{config=validateThinkingConfig(JSON.parse(first));}catch(error){invalid(/^CONFIG_[A-Z_]+$/.test(error.code||'')?error.code:'CONFIG_INVALID');ended=true;return;}
  started=true;buffered='';run=runThinkingRawMux(config,{...deps,emit,signal:abort.signal});
  const cleanupInput=()=>{ended=true;input.pause?.();input.destroy?.();};
  // Observe both settlements without leaving an ignored rejected finally-promise.
  run.then(cleanupInput,cleanupInput);
 };
 let closeInput;
 const inputDone=new Promise(resolve=>{closeInput=resolve;});
 const end=()=>{if(!started&&!ended)invalid('CONFIG_LINE_REQUIRED');ended=true;stop();closeInput();};
 input.on('data',onData);input.on('end',end);input.on('close',end);input.on('error',end);
 signals.on('SIGTERM',stop);signals.on('SIGINT',stop);
 try{
  await Promise.race([inputDone,new Promise(resolve=>abort.signal.addEventListener('abort',resolve,{once:true}))]);
  if(run)await run;
 }finally{
  stop();input.off('data',onData);input.off('end',end);input.off('close',end);input.off('error',end);
  signals.off('SIGTERM',stop);signals.off('SIGINT',stop);input.pause?.();
 }
}

if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 runThinkingCli().catch(()=>{process.stdout.write(JSON.stringify({type:'transport',state:'error',code:'CLI_FAILED',time:new Date().toISOString()})+'\n');process.exitCode=1;});
}
