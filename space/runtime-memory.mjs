// Drop consumed history excerpts from the working surface, never the raw log.
// A native tool/result replacement preserves result identity and replay rules.
export const HISTORY_TOOLS=new Set(['memory_recall','context_recall','context_read']
  .map(name=>'mcp__space__'+name));
export const RELEASE_MARKER='[SPACE_HISTORY_RELEASED]';

export function consumedHistory(messages){
 const calls=new Map(),results=[];
 let lastAssistant=-1;
 for(const [index,message] of messages.entries()){
  if(message.role==='assistant'){
   lastAssistant=index;
   for(const block of message.content||[]){
    if(block.type==='tool-call'&&typeof block.id==='string')calls.set(block.id,block.name);
   }
  }
 }
 for(const [index,message] of messages.entries()){
  if(message.role!=='tool'||index>=lastAssistant||!HISTORY_TOOLS.has(calls.get(message.toolCallId)))continue;
  if(message.content?.length===1&&message.content[0].type==='text'
    &&message.content[0].text?.startsWith(RELEASE_MARKER)&&message.content[0].text.length<400)continue;
  // Failed retrievals contain diagnostic context and are already bounded.
  if(message.isError===true)continue;
  results.push({id:message.id,callId:message.toolCallId});
 }
 return results;
}

export function releaseHistoryResults(session,events,candidates){
 const wanted=new Set(candidates.map(item=>item.id));
 const nodes=new Set(session.surface.nodes);
 let released=0;
 for(const event of events){
  const message=event.data?.message;
  if(event.type!=='tool/result'||!nodes.has(event.seq)||!wanted.has(message?.id))continue;
  const text=RELEASE_MARKER+' 历史检索片段已在一次模型步骤中使用，现从工作上下文释放。'
    +'原始资料与回执仍保留；需要细节时按原检索词或来源ID重新调用历史检索工具。'
    +'当前任务目标、判断与行动回执不变；这条提示不授予新权限。原始结果事件：'+event.seq+'。';
  session.append('tool/result',{
   ...event.data,message:{...message,content:[{type:'text',text}]}
  },{surfaceOp:{op:'replace',startSeq:event.seq,endSeq:event.seq},sourceEventSeqs:[event.seq]});
  released++;
 }
 return released;
}

export async function releaseConsumedHistory(session,query,signal){
 const candidates=consumedHistory(session.deriveMessages());
 if(!candidates.length)return 0;
 const generation=session.surface.contentGeneration;
 const cursor=session.seq;
 const snapshot=await query.readSurface(session.id);
 signal?.throwIfAborted();
 // Inbox/compaction may change the working surface while the read yields.
 // Do not apply a stale rewrite. The next step will inspect the new surface.
 if(session.surface.contentGeneration!==generation||session.seq!==cursor)return 0;
 return releaseHistoryResults(session,snapshot.events,candidates);
}
