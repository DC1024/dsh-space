'use strict';
// 嵌在总览窗口右栏时只留一份总览：欢迎标题与四张数据卡压成一行，避免和左侧页面重复。
if(window.self!==window.top)document.documentElement.classList.add('embedded');
const $=id=>document.getElementById(id), names={biao:'小鲸鱼 · 表人格',li:'小鲸鱼 · 里人格'};
let state,csrf='',view=null,epoch=0,lastSeq=0,source,thinkingSource,thinkingSeq=0,toastTimer;
// 共同记忆的“看谁的视角”是纯本地视图，不重绑人格、不发服务器请求。
let memView=null;
function memCurrentView(){return memView||view;}
function syncMemSwitch(){const cur=memCurrentView();for(const id of ['memViewBiao','memViewLi','memViewAll']){const b=$(id);if(b)b.setAttribute('aria-pressed',String(b.dataset.view===cur));}}
function setMemoryTitle(){const cur=memCurrentView();$('memoryTitle').textContent='共同记忆 · '+(cur==='all'?'表与里':names[cur])+'视角';syncMemSwitch();}
let lastEvent=null,liveThinkTail='';
const eventNames={'console.start':'控制台启动','owner.message':'主人留言','memory.saved':'记忆已保存','task.result':'本轮结果','task.accepted':'消息已接收','task.failure':'本轮停止原因','context.continued':'新任务上下文已就绪','context.archived':'本轮记录已归档','device.stage':'设备阶段回执','persona.switched':'人格已切换','control.pause':'活动已暂停','control.correct':'纠正已保存','runtime.error':'连接出现问题','budget.settled':'费用已记录','budget.bound_violation':'费用待核对','export.saved':'记录已导出','storage.owner_authorized':'主人授权空间额度','storage.expansion_requested':'扩容提案已保存','storage.expansion_decided':'主人已决定扩容申请','storage.quota_applied':'空间额度已更新','storage.expansion_blocked':'扩容受余量限制','storage.expansion_apply_failed':'额度更新失败，记录保留','task.dispatch':'已派发给她','control.hold':'本轮已停止','control.resume':'已记录继续','parallel.changed':'并发模式已变更','approval.decided':'审批已决定','approval.requested':'她在等你回答','approval.answered':'已回答','approval.dismissed':'决定已关闭','correction.acknowledged':'已确认纠正后的理解','phone.failed':'手机动作未完成','budget.cancelled':'费用预留已撤销','reasoning.request':'开始思考','reasoning.selected':'思考模型已选定','desktop.connected':'桌面端已连接','desktop.disconnected':'桌面端已断开','task.reconciled':'任务已核对','pet.action':'宠物动作'};
let thinkingSid=null;
const reasonBlocks=new Map(),labels={preference:'偏好',experience:'经历',relationship:'约定',project:'项目',correction:'纠正',interest:'兴趣'};
function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function toast(t){$('toast').textContent=t;$('toast').style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').style.display='none',6000);}
function stamp(t){return new Date(t).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});}
async function post(path,body={}){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});const v=await r.json();if(!r.ok)throw Error(v.error||'操作未完成');return v;}
function resetView(persona){epoch++;view=persona;memView=null;source?.close();thinkingSource?.close();lastSeq=0;thinkingSeq=0;reasonBlocks.clear();lastEvent=null;liveThinkTail='';for(const id of ['messages','eventList','memoryList','reasonText'])$(id).replaceChildren();$('reasonText').append(el('p','当前人格尚无可显示的模型原文。','muted'));for(const id of ['chat','correction','memoryText','search'])$(id).value='';$('memoryPerformed').value='unknown';delete $('chatForm').dataset.key;}
async function refresh(){try{const r=await fetch('/api/status');if(!r.ok)throw Error();const next=await r.json(),changed=view!==next.active_persona;state=next;csrf=next.csrf;if(changed)resetView(next.active_persona);renderStatus();renderLiveNow();renderTaskReview();renderApprovals();if(changed){loadMemories();connect();connectThinking();}else if(!window.dshDualStreamsReady&&thinkingSid!==next.runtime.session_id){thinkingSource?.close();thinkingSeq=0;reasonBlocks.clear();connectThinking();}loadActions();}catch{$('connection').textContent='控制台暂时断开';$('liveDot').className='';$('phoneState').textContent='手机设备：状态待确认 · 控制台暂时断开';$('phoneState').title='无法取得当前设备状态，不能将上次结果视为仍已连接。';}}
function renderStatus(){const s=state,label=names[view];renderDeviceLease(s.device_lease);const phoneStatus=describePhoneStatus(s.phone);$('phoneState').textContent=phoneStatus.text;$('phoneState').title=phoneStatus.title;$('deviceState').textContent='电脑：'+(s.desktop.on_demand?'按需使用':s.desktop.ready?'已连接 · 专属 Windows':'尚未连接');$('version').textContent='本地空间 · v'+s.version;$('state').textContent=s.dispatch_enabled?(s.paused?'当前活动已暂停':'已唤醒，可接收任务'):'准备中，任务仅保存';$('stateHint').textContent=!s.dispatch_enabled?'修复验收中；留言只保存，不发送模型或设备任务':'一般任务手机优先，需要电脑时顺序切换；自主探索关闭';if(s.dispatch_enabled&&s.thinking?.requiresPause&&s.thinking.code!=='MODEL_UNKNOWN'){$('state').textContent='观察连接待恢复，执行受限';$('stateHint').textContent='已有记录保留；新操作仍须通过观察检查。';}$('resume').disabled=!s.autonomy_enabled||s.blockers.length>0;$('resume').title=s.blockers.join('；');$('resume').textContent=s.autonomy_enabled?'允许继续':'自主活动未开放';$('parallelToggle').textContent='表里并发 · '+(s.parallel_enabled?'已开启':'关闭');$('parallelToggle').setAttribute('aria-pressed',String(Boolean(s.parallel_enabled)));$('activePersonaLabel').textContent=label;setMemoryTitle();$('personaBiao').setAttribute('aria-pressed',String(view==='biao'));$('personaLi').setAttribute('aria-pressed',String(view==='li'));$('personaBoundary').textContent=(s.parallel_enabled?'表里可同时处理已授权任务；点击人格只切换查看。':'表、里共用记忆，保留各自经历的归属；切换不会恢复活动。')+(s.runtime.session_id?'':'此人格的独立模型会话待连接。');$('desktopBadge').textContent=s.desktop.on_demand?(s.desktop.ready?'保留最近画面':'需要时连接'):(s.desktop.ready?'画面已连接':'待接入');$('desktopEmpty').hidden=s.desktop.ready;$('desktopFrame').hidden=!s.desktop.ready;if(s.desktop.ready)$('desktopFrame').src='/api/frame?t='+Date.now();$('persona').textContent=s.persona.active_version||s.persona.version||'来源待确认';$('personaHint').textContent=label+' · '+(s.runtime.session_id?'独立会话已登记':'独立会话待登记');$('memoryCount').textContent=s.memory_count;$('cost').textContent='¥ '+s.budget.local_recorded_cny.toFixed(4)+' / 20';$('costHint').textContent='本空间记录；未知预留 ¥'+s.budget.held_cny.toFixed(4)+'；共享于表、里模式';$('storage').textContent=asGiB(s.storage.used_bytes)+' / '+s.storage.quota_gib+' GiB';$('free').textContent='F盘可用 '+asGiB(s.storage.free_bytes)+' GiB · 额度按需使用，未预占磁盘';$('storageBreakdown').textContent='根目录 '+asGiB(s.storage.space_root_bytes)+' · 手机数据 '+asGiB(s.storage.phone_data_bytes)+' GiB';$('storageHeadroom').textContent='最低保留 '+s.storage.minimum_free_gib+' GiB · 真正可继续用 '+asGiB(s.storage.usable_remaining_bytes)+' GiB';$('storageVm').textContent='系统虚拟盘配置 '+s.storage.vm_system_disk_logical_gib+' GiB · 逻辑容量，实际扩展另验'+(s.storage.scan_complete?'':' · 占用统计未完整，写入受限');$('resource').textContent=s.resources.known?'空闲 '+Math.floor(s.resources.idle_seconds/60)+' 分钟 · 可用内存 '+s.resources.available_ram_gb+' GB':'空闲状态未知';const lag=s.thinking?.backlogAgeMs;$('reasonModel').textContent=label+' · Flash · '+(s.thinking?.code==='MODEL_UNKNOWN'?'已连接，实际模型待验证':s.thinking?.freshness?.ready===true?'观察数据新鲜':'观察未就绪')+(Number.isFinite(lag)?' · 落后 '+(lag/1000).toFixed(1)+'秒':'');$('correctionState').textContent=s.correction?(s.correction_ack_revision===s.revision?'已确认新理解；活动仍关闭':'纠正已保存，等待明确安排后确认'):'纠正只作用于当前人格；保存后保持暂停。';$('chatForm').querySelector('button').textContent=s.dispatch_enabled?'发送给她 ↑':'保存给当前人格 ↑';}
function asGiB(value){return Number.isFinite(value)?(value/2**30).toFixed(2):'未核实';}
let storageChallenge=null;
async function showStorageApproval(a){
 const v=await post('/api/control/storage-prepare',{id:a.id,target_gib:a.details.target_gib,persona:view});
 storageChallenge={id:v.id,target_gib:v.target_gib,nonce:v.nonce,persona:view};
 const u=v.safety.usage;
 $('storageConfirmTitle').textContent='确认允许总额度扩至 '+v.target_gib+' GiB？';
 $('storageConfirmId').textContent='申请 '+v.id;
 $('storageConfirmReason').textContent='模型提案：'+v.details.reason;
 $('storageConfirmUsage').textContent='当前额度 '+u.quota_gib+' GiB，已用 '+asGiB(u.used_bytes)+' GiB（根目录 '+asGiB(u.space_root_bytes)+'，手机 '+asGiB(u.phone_data_bytes)+'）。F盘可用 '+asGiB(u.free_bytes)+' GiB，最低须保留 '+u.minimum_free_gib+' GiB。';
 $('storageConfirmSafety').textContent=v.safety.message;
 $('storageConfirmVm').textContent=v.details.vm_disk_target_gib?'虚拟盘申请 '+v.details.vm_current_logical_gib+' → '+v.details.vm_disk_target_gib+' GiB，仍须设备执行和回读；本按钮不会执行磁盘或分区扩展。':'本次更新总额度。系统虚拟盘逻辑容量另行显示，不据此认定磁盘已扩展。';
 $('storageConfirm').disabled=!v.safety.eligible;
 $('storageConfirm').textContent='确认允许这项 '+v.target_gib+' GiB 申请';
 $('storageDialog').returnValue='';$('storageDialog').showModal();
}
function storageCard(a){
 const d=a.details,card=el('article',undefined,'approval'),head=el('div',undefined,'approval-heading');
 const labels={pending_owner:'待主人确认',applying:'已批准，正在更新',quota_applied:'额度已更新',quota_applied_vm_pending:'额度已更新 · 虚拟盘待执行',blocked_space:'已批准 · 余量不足未更新',apply_failed:'已批准 · 更新失败待重试',denied:'主人未允许'};
 head.append(el('h3',a.title),el('span',labels[a.phase]||'待主人确认','pill'));card.append(head);
 const dl=el('dl'),u=d.current_usage||{};
 for(const [label,text]of [['申请编号',a.id],['提案理由',d.reason],['申请时用量',asGiB(u.used_bytes)+' GiB'],['申请时总额度',d.current_quota_gib+' GiB'],['固定目标',d.target_gib+' GiB'],['申请者',({biao:'表人格',li:'里人格'})[d.requester_persona]||'模型提案']])dl.append(el('dt',label),el('dd',text));
 if(d.vm_disk_target_gib)dl.append(el('dt','虚拟盘提案'),el('dd',d.vm_current_logical_gib+' → '+d.vm_disk_target_gib+' GiB · 尚未执行'));
 card.append(dl,el('p','申请文字都是模型提案。普通文字或被引用的“同意”无效；只有你在这里确认这一申请及目标，才能更新额度。','muted'));
 if(a.error)card.append(el('p',a.error,'muted'));
 const acts=el('div',undefined,'approval-actions');
 if(a.status==='pending'){
  const yes=el('button','查看并确认 '+d.target_gib+' GiB');yes.onclick=async()=>{yes.disabled=true;try{await showStorageApproval(a);}catch(e){toast(e.message);}finally{yes.disabled=false;}};
  const no=el('button','不允许这项申请');no.onclick=async()=>{no.disabled=true;try{await post('/api/control/storage-deny',{id:a.id,target_gib:d.target_gib,persona:view});await refresh();toast('这项扩容申请已拒绝。');}catch(e){toast(e.message);}finally{no.disabled=false;}};
  acts.append(yes,no);
 }else if(a.status==='approved'&&['apply_failed','blocked_space','applying'].includes(a.phase)){
  const retry=el('button','重试已批准的 '+d.target_gib+' GiB');retry.onclick=async()=>{retry.disabled=true;try{const r=await post('/api/control/storage-retry',{id:a.id,target_gib:d.target_gib,persona:view});await refresh();toast(r.phase.startsWith('quota_applied')?'已更新额度；暂停状态保持。':'尚未更新，原批准与错误记录保留。');}catch(e){toast(e.message);}finally{retry.disabled=false;}};acts.append(retry);
 }
 card.append(acts);return card;
}
function questionCard(a){
 const d=a.details||{},card=el('article',undefined,'approval'),head=el('div',undefined,'approval-heading');
 const tools=el('div',undefined,'approval-tools');
 tools.append(el('span',({pending:'等你回答',answered:'已回答',denied:'已取消',expired:'已过期',closed:'已关闭'})[a.status]||a.status,'pill'));
 const close=el('button','×','approval-close');close.type='button';close.title='关闭这条决定';close.setAttribute('aria-label','关闭这条决定');
 close.onclick=async()=>{close.disabled=true;try{await post('/api/control/dismiss',{id:a.id,persona:view});await refresh();toast('这条决定已关闭。');}catch(e){toast(e.message);close.disabled=false;}};
 tools.append(close);
 head.append(el('h3',d.question||a.title),tools);
 card.append(head);
 if(a.status==='pending'){
  const input=el('input');input.className='question-input';input.placeholder='在这里回答（手机号、验证码或你的决定）';input.maxLength=4000;
  const submit=async(answer)=>{
   const value=String(answer||'').trim();
   if(!value){toast('回答不能为空');return;}
   try{await post('/api/control/answer',{id:a.id,answer:value,persona:view});await refresh();toast('回答已交给她，她会在下一轮继续。');}
   catch(e){toast(e.message);}
  };
  const acts=el('div',undefined,'approval-actions');
  for(const option of (d.options||[])){const b=el('button',option);b.onclick=()=>submit(option);acts.append(b);}
  const send=el('button','提交回答');send.className='primary';send.onclick=()=>submit(input.value);acts.append(send);
  card.append(input,acts);
 }else if(d.answer){card.append(el('p','你的回答：'+d.answer));}
 return card;
}
function renderApprovals(){const box=$('approvalList');box.replaceChildren();$('requestCount').textContent=state.approvals.filter(x=>x.status==='pending').length;for(const a of state.approvals){if(a.status==='closed')continue;if(a.kind==='storage_expand'){box.append(storageCard(a));continue;}if(a.kind==='question'){box.append(questionCard(a));continue;}const card=el('article',undefined,'approval'),heading=el('div',undefined,'approval-heading');heading.append(el('h3',a.title),el('span',({pending:'待你决定',approved:'已允许',denied:'已拒绝'})[a.status],'pill'));card.append(heading);if(a.status!=='pending')card.append(el('p','历史申请原文：以下为申请时的计划，不代表当前安装状态或容量；现行容量见页面上方。','muted'));const dl=el('dl');for(const [key,label]of [['purpose','用途'],['source','来源'],['version','版本'],['download','下载量'],['disk','空间'],['scope','范围'],['license','授权']])if(a.details[key])dl.append(el('dt',label),el('dd',a.details[key]));card.append(dl);if(a.status==='pending'){const acts=el('div',undefined,'approval-actions');for(const [decision,label]of [['approved','允许准备安装'],['denied','暂不允许']]){const b=el('button',label);b.onclick=async()=>{try{await post('/api/control/approval',{id:a.id,decision,persona:view});await refresh();toast('决定已保存。');}catch(e){toast(e.message);}};acts.append(b);}card.append(acts);}box.append(card);}if(!box.children.length)box.append(el('p','目前没有待决定的申请。','empty'));}
async function loadMemories(){const token=epoch;try{const r=await fetch('/api/memory?q='+encodeURIComponent($('search').value));if(!r.ok)throw Error();const rows=await r.json();if(token!==epoch)return;const box=$('memoryList');box.replaceChildren();const mv=memCurrentView();for(const m of rows){const row=el('div',undefined,'memory-row');if(mv!=='all')row.classList.add((m.performed_by===mv||m.recorded_by===mv)?'mem-mine':'mem-other');const body=el('p',m.text);const meta=el('small','执行：'+({biao:'表',li:'里',owner:'主人',unknown:'未明确'}[m.performed_by]||'未明确')+' · 记录：'+({biao:'表',li:'里',owner:'主人'}[m.recorded_by]||'历史未明确')+' · '+stamp(m.time));row.append(el('span',labels[m.kind]||m.kind,'tag'),body,meta);
    // 一条记忆常常有几百字，右栏只有 340 宽，原样铺开会把列表撑成"一条就是一屏"。
    // 默认折四行，点"展开全文"看完整原文，不删改任何字。
    if(m.text&&m.text.length>80){row.classList.add('folded');const more=el('button','展开全文','mem-more');more.type='button';more.addEventListener('click',()=>{const open=row.classList.toggle('open');more.textContent=open?'收起':'展开全文';});row.append(more);}
    box.append(row);}if(!rows.length)box.append(el('p','没有相关记忆，可以换一个关键词查找。','empty'));}catch{if(token===epoch)toast('记忆暂时无法读取');}}
async function loadActions(){const token=epoch;try{const r=await fetch('/api/actions');if(!r.ok)return;const rows=await r.json();if(token!==epoch)return;const box=$('sharedActions');box.replaceChildren();for(const a of rows){const row=el('div',undefined,'event-row'),text=el('div');row.append(el('time',stamp(a.time)));text.append(el('p',names[a.persona]+' · '+a.kind),el('small',a.summary));row.append(text);box.append(row);}if(!rows.length)box.append(el('p','还没有双方共享的实际行动回执；私有对话不会自动复制到这里。','empty'));}catch{}}
function displayReplyText(text){return String(text??'').split(/(```[\s\S]*?```|`[^`\n]+`)/g).map((part,i)=>i%2?part:part.replace(/\*\*([^\n]+?)\*\*/g,'$1')).join('');}
function addMessage(text,role,status){const box=$('messages'),m=el('div',undefined,'message '+role);if(role==='assistant')m.append(el('strong',names[view],'speaker'));m.append(el('p',role==='assistant'?displayReplyText(text):text));if(status)m.append(el('small',status));box.append(m);box.scrollTop=box.scrollHeight;}
function onEvent(e){if(e.persona!==view||e.seq<=lastSeq)return;lastSeq=e.seq;const p=e.payload;if(!/^(reasoning\.|reply\.)/.test(e.kind))lastEvent={kind:e.kind,payload:p,time:e.time};if(e.kind==='pet.action'&&p.file){$('petVideo').src='/assets/pet/'+encodeURIComponent(p.file);$('petVideo').play().catch(()=>{});}if(e.kind==='owner.message')addMessage(p.text,'user','已保存 · 以任务回执为准');if(e.kind==='task.failure')addMessage(p.failure?.message||'本轮停止原因已保存。','assistant','本轮已停止 · 旧任务不重发');if(e.kind==='task.result')addMessage(p.text||p.failure?.message||'本轮没有最终回复，已保留执行回执。旧任务不会自动重发。','assistant',p.status==='completed'?'本轮已结束 · 完成情况见回复':'本轮已停止 · 查看执行回执');if(e.kind==='reasoning.delta'||e.kind==='reply.delta')return;const row=el('div',undefined,'event-row'),t=el('div');row.append(el('time',stamp(e.time)));t.append(el('p',eventNames[e.kind]||'活动记录'),el('small',e.kind==='task.result'?displayReplyText(p.text||p.failure?.message||''):(p.failure?.message||p.message||p.filename||p.text||p.decision||(e.kind==='device.stage'?(p.device+' · '+p.action+' · '+({started:'已开始',receipt:'已取得回执',failed:'未完成'}[p.phase]||p.phase)):'')||'')));row.append(t);$('eventList').prepend(row);while($('eventList').children.length>150)$('eventList').lastChild.remove();if(e.kind==='memory.saved')loadMemories();renderLiveNow();}
function connect(){if(window.dshDualStreamsReady){replaySharedView();return;}const token=epoch;source=new EventSource('/api/events?persona='+view+'&after='+lastSeq);source.onopen=()=>{if(token!==epoch)return;$('connection').textContent='本地控制台已连接';$('liveDot').className='live';};source.onmessage=e=>{if(token!==epoch)return;try{onEvent(JSON.parse(e.data));}catch{toast('事件格式异常，未补写内容');}};source.onerror=()=>{if(token===epoch){$('connection').textContent='正在重连';$('liveDot').className='';}};}
function renderThinkingEvent(v,sid){if(thinkingSid!==sid){thinkingSid=sid;thinkingSeq=0;reasonBlocks.clear();liveThinkTail='';}try{if(v.sessionId!==sid||v.seq<=thinkingSeq)return;thinkingSeq=v.seq;if(v.kind!=='reasoning.delta'&&v.kind!=='reasoning.snapshot')return;const key=(v.attemptId||'unknown')+':'+(v.blockIndex??0);let block=reasonBlocks.get(key);if(!block){if(!reasonBlocks.size)$('reasonText').replaceChildren();const card=el('div',undefined,'reason-block');card.append(el('small',stamp(v.receivedAt||v.time)+' · '+(v.origin==='native-live'?'模型原文':'历史原文')),el('span',''));$('reasonText').append(card);block=card.lastChild;reasonBlocks.set(key,block);}if(v.kind==='reasoning.snapshot'){block.textContent=v.text||'';liveThinkTail=String(v.text||'').slice(-160);}else{block.textContent+=v.text||'';liveThinkTail=(liveThinkTail+String(v.text||'')).slice(-160);}$('reasonText').scrollTop=$('reasonText').scrollHeight;renderLiveNow();}catch{toast('思考事件格式异常，未补写内容');}}
function connectThinking(){if(window.dshDualStreamsReady||!state?.runtime.session_id)return;const token=epoch,sid=state.runtime.session_id;thinkingSid=sid;thinkingSource?.close();thinkingSource=new EventSource('/api/thinking/stream?sessionId='+encodeURIComponent(sid)+'&after='+thinkingSeq);const render=e=>{if(token!==epoch||state?.runtime.session_id!==sid)return;try{renderThinkingEvent(JSON.parse(e.data),sid);}catch{}};thinkingSource.onmessage=render;thinkingSource.addEventListener('reasoning',render);thinkingSource.onerror=()=>{if(token===epoch)$('reasonModel').textContent=names[view]+' · Flash · 观察连接待恢复';};}
// The dual panes own the four live sockets. Reuse their events for the legacy
// activity/summary views instead of occupying all six HTTP/1 connection slots.
window.addEventListener('dsh:dual-ready',()=>{source?.close();thinkingSource?.close();source=null;thinkingSource=null;});
const sharedEvents=new Map(),sharedThinking=new Map();
function clearSharedThinking(sid){thinkingSid=sid;thinkingSeq=0;reasonBlocks.clear();liveThinkTail='';$('reasonText').replaceChildren(el('p','当前人格尚无可显示的模型原文。','muted'));}
function replaySharedView(){
 for(const e of sharedEvents.get(view)?.values()||[])onEvent(e);
 const cache=sharedThinking.get(view);clearSharedThinking(cache?.sid||null);
 if(cache)for(const v of [...cache.blocks.values()].sort((a,b)=>a.seq-b.seq))renderThinkingEvent(v,cache.sid);
 renderLiveNow();
}
window.addEventListener('dsh:space-event',e=>{const v=e.detail;let rows=sharedEvents.get(v.persona);if(!rows){rows=new Map();sharedEvents.set(v.persona,rows);}rows.set(v.seq,v);while(rows.size>150)rows.delete(rows.keys().next().value);onEvent(v);});
window.addEventListener('dsh:thinking-session',e=>{const d=e.detail;if(sharedThinking.get(d.persona)?.sid!==d.sessionId){sharedThinking.set(d.persona,{sid:d.sessionId,seq:0,blocks:new Map()});if(d.persona===view)clearSharedThinking(d.sessionId);}});
window.addEventListener('dsh:thinking-event',e=>{
 const d=e.detail,v=d.value;
 let cache=sharedThinking.get(d.persona);if(!cache||cache.sid!==d.sessionId){cache={sid:d.sessionId,seq:0,blocks:new Map()};sharedThinking.set(d.persona,cache);}
 if(v.seq<=cache.seq)return;cache.seq=v.seq;
 if(v.kind==='reasoning.delta'||v.kind==='reasoning.snapshot'){
  const key=(v.attemptId||'unknown')+':'+(v.blockIndex??0),old=cache.blocks.get(key);
  cache.blocks.set(key,{...v,kind:'reasoning.snapshot',text:v.kind==='reasoning.snapshot'?String(v.text||''):String(old?.text||'')+String(v.text||'')});
 }
 if(d.persona===view)renderThinkingEvent(v,d.sessionId);
});
window.addEventListener('dsh:stream-open',e=>{if(e.detail.persona===view){$('connection').textContent='本地控制台已连接';$('liveDot').className='live';}});

async function guardedForm(e,work){e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{await work();await refresh();}catch(x){toast(x.message);}finally{b.disabled=false;}}
$('chatForm').onsubmit=e=>guardedForm(e,async()=>{const key=$('chatForm').dataset.key||crypto.randomUUID();$('chatForm').dataset.key=key;const v=await post('/api/control/chat',{persona:view,text:$('chat').value,request_key:key,target:$('taskTarget').value});$('chat').value='';delete $('chatForm').dataset.key;toast(v.status==='needs-review'?'已保存给当前人格，执行仍关闭，不会自动重发。':'已保存，等待处理回执。');});$('chat').oninput=()=>delete $('chatForm').dataset.key;
$('storageConsultForm').onsubmit=e=>guardedForm(e,async()=>{const form=$('storageConsultForm'),key=form.dataset.key||crypto.randomUUID();form.dataset.key=key;const r=await post('/api/control/storage-consult',{persona:view,target_gib:Number($('storageTarget').value),reason:$('storageReason').value,request_key:key});delete form.dataset.key;toast(r.status==='needs-review'?'受限评估已保存，执行仍关闭。':'已安排只评估容量；全局暂停保持，扩容仍等你明确确认。');});$('storageTarget').oninput=()=>delete $('storageConsultForm').dataset.key;$('storageReason').oninput=()=>delete $('storageConsultForm').dataset.key;
$('memoryForm').onsubmit=e=>guardedForm(e,async()=>{await post('/api/memory',{persona:view,kind:$('memoryKind').value,text:$('memoryText').value,performed_by:$('memoryPerformed').value});$('memoryText').value='';loadMemories();toast('记忆已保存，并保留执行者与记录来源。');});
$('correctionForm').onsubmit=e=>guardedForm(e,async()=>{await post('/api/control/correct',{persona:view,text:$('correction').value});$('correction').value='';toast('纠正已保存，活动保持暂停。');});
for(const action of ['pause','resume'])$(action).onclick=async()=>{try{await post('/api/control/'+action,{persona:view});await refresh();toast(action==='pause'?'已暂停':'已记录继续决定');}catch(e){toast(e.message);}};
for(const [id,p]of [['personaBiao','biao'],['personaLi','li']])$(id).onclick=async()=>{const b=$(id);b.disabled=true;memView=null;try{await post('/api/control/persona',{persona:p});await refresh();toast('已切到'+names[p]+'；自主活动保持关闭。');}catch(e){toast(e.message);}finally{b.disabled=false;}};
// 记忆面板里的表/里切换只改“看谁的视角”，不重绑人格、不影响正在运行的任务。
for(const id of ['memViewBiao','memViewLi','memViewAll']){const b=$(id);if(!b)continue;b.onclick=()=>{memView=b.dataset.view;setMemoryTitle();loadMemories();};}
let parallelChallenge=null;
$('parallelToggle').onclick=async()=>{try{const enabled=!state.parallel_enabled;parallelChallenge=await post('/api/control/parallel-prepare',{enabled});$('parallelTitle').textContent=enabled?'启用表里并发？':'关闭表里并发？';$('parallelConfirm').textContent=enabled?'确认启用':'确认关闭';$('parallelExplain').textContent=enabled?'小鲸鱼的表、里人格可以同时处理各自任务，记忆共用并标注归属，对话会话分别保存。':'两边当前任务将暂停。之后只由当前选中的人格处理新授权任务。';$('parallelDialog').showModal();}catch(e){toast(e.message);}};
$('parallelDialog').onclose=async()=>{const challenge=parallelChallenge;parallelChallenge=null;if($('parallelDialog').returnValue!=='confirm'||!challenge)return;try{await post('/api/control/parallel',{nonce:challenge.nonce,enabled:challenge.enabled});await refresh();toast(challenge.enabled?'表里并发已开启；自主活动保持关闭。':'表里并发已关闭，活动已暂停。');}catch(e){toast(e.message);}};
$('export').onclick=async()=>{try{const v=await post('/api/export');const a=el('a');a.href=v.url;a.download='';a.click();toast('导出已保存到本机。');}catch(e){toast(e.message);}};$('search').oninput=loadMemories;
$('reasonClose').onclick=()=>{$('reasonWindow').hidden=true;localStorage.setItem('reasonHidden','1');};$('reasonToggle').onclick=()=>{$('reasonWindow').hidden=false;localStorage.removeItem('reasonHidden');};if(localStorage.getItem('reasonHidden'))$('reasonWindow').hidden=true;
let drag;const win=$('reasonWindow'),handle=$('reasonHandle');handle.onpointerdown=e=>{if(e.target.closest('button'))return;const r=win.getBoundingClientRect();drag={dx:e.clientX-r.left,dy:e.clientY-r.top};handle.setPointerCapture(e.pointerId);};handle.onpointermove=e=>{if(!drag)return;win.style.left=Math.max(0,Math.min(innerWidth-win.offsetWidth,e.clientX-drag.dx))+'px';win.style.top=Math.max(0,Math.min(innerHeight-50,e.clientY-drag.dy))+'px';win.style.right='auto';win.style.bottom='auto';};handle.onpointerup=()=>{drag=null;};

$('storageDialog').onclose=async()=>{const c=storageChallenge;storageChallenge=null;if($('storageDialog').returnValue!=='confirm'||!c)return;try{const r=await post('/api/control/storage-approve',c);await refresh();toast(r.phase.startsWith('quota_applied')?'这项额度已更新；活动暂停状态保持。':'已保留批准，额度尚未更新，请查看申请回执。');}catch(e){toast(e.message);}};

refresh();setInterval(refresh,5000);

function renderTaskReview(){const box=$("uncertainTasks");box.replaceChildren();for(const t of state.tasks.filter(t=>t.status==="uncertain")){const card=el("div",undefined,"status-strip"),note=el("span","有一项操作结果待核对，旧任务不会自动重发。"),b=el("button","核对停止回执");b.onclick=async()=>{b.disabled=true;try{const r=await post("/api/control/reconcile",{id:t.id,persona:view});toast(r.message);await refresh();}catch(e){toast(e.message);}finally{b.disabled=false;}};card.append(note,b);box.append(card);}}

function renderDeviceLease(value){const target=$("deviceLeaseState");if(!target)return;const label={phone:"手机",desktop:"电脑"},v=value||{};target.textContent=v.blocked_unknown?"有操作结果待核对，两端暂不开始新动作":v.current_device?("当前占用："+label[v.current_device]+(v.owner==="background"?"（后台预览）":"")+(v.waiting_devices?.length?" · 等待完整回执后切换至"+v.waiting_devices.map(x=>label[x]).join("、"):"")):"当前无设备操作 · 手机和电脑顺序执行";}

function describePhoneStatus(phone,now=Date.now()){
  const at=Date.parse(phone?.at||'');
  const fresh=Number.isFinite(at)&&now-at<=30000&&at-now<=5000;
  const title='此处只表示小鲸鱼的手机设备通道，不代表旧手机桥或自主活动已经启动。'+(Number.isFinite(at)?' 最近设备检查：'+stamp(phone.at):' 尚无设备检查时间。');
  if(!fresh)return {text:'手机设备：状态待确认 · 等待最新检查',title};
  if(phone?.ready!==true)return {text:'手机设备：未就绪 · '+(phone?.error||'等待连接'),title};
  return {text:'手机设备：已就绪 · 固定模拟器',title};
}
/* --- 常驻「她在做什么」条：只显示真实数据，不补写内容 --- */
const TARGET_LABEL={phone:'手机优先',desktop:'电脑端',both:'手机与电脑'};
const TASK_STATE={queued:'排队中',claimed:'已接手',running:'进行中',completed:'已完成',reviewed:'已复核',stopped:'已停止',uncertain:'待核对','needs-review':'待复核'};
const ACTIVE_STATE=new Set(['queued','claimed','running']);
function clipText(value,limit){const text=String(value==null?'':value).replace(/\s+/g,' ').trim();return text.length>limit?text.slice(0,limit)+'…':text;}
function elapsedText(time){const sec=Math.max(0,Math.round((Date.now()-Number(time||Date.now()))/1000));if(sec<60)return sec+'秒';if(sec<3600)return Math.floor(sec/60)+'分'+String(sec%60).padStart(2,'0')+'秒';return Math.floor(sec/3600)+'小时'+String(Math.floor(sec%3600/60)).padStart(2,'0')+'分';}
function liveNowTask(){
  if(!state)return null;
  const tasks=Array.isArray(state.tasks)?state.tasks:[];
  const byId=new Map(tasks.map(t=>[t.id,t]));
  const live=state.parallel_tasks?Object.values(state.parallel_tasks):[];
  for(const slot of live){const req=slot&&slot.active_request;if(req&&byId.has(req.task_id))return byId.get(req.task_id);}
  const ordered=['running','claimed','queued'].map(s=>tasks.find(t=>t.status===s)).filter(Boolean);
  return ordered[0]||tasks[0]||null;
}
function liveEventLine(task){
  if(lastEvent){
    const label=eventNames[lastEvent.kind]||'活动记录';
    const pl=lastEvent.payload||{};
    const detail=pl.message||pl.text||pl.filename||pl.summary||'';
    return '最近动作：'+label+(detail?' · '+clipText(detail,60):'')+(lastEvent.time?' · '+stamp(lastEvent.time):'');
  }
  if(task)return '最近动作：任务记录已同步 · '+stamp(task.time);
  return '最近动作：尚未开始';
}
function renderLiveNow(){
  if(!state)return;
  const bar=$('liveNow');if(!bar)return;
  const task=liveNowTask();
  const active=task?ACTIVE_STATE.has(task.status):false;
  const open=Boolean(state.dispatch_enabled)&&!state.paused;
  bar.className='live-now '+(active&&open?'running':'idle');
  let headline='',meta='';
  if(!state.dispatch_enabled){headline='她还没开始新任务';meta='执行已关闭 · 留言只保存';}
  else if(state.paused){headline='活动已暂停，等你决定';meta='暂停期间不接手新任务';}
  else if(task&&ACTIVE_STATE.has(task.status)){
    headline=(task.status==='running'?'正在做：':'排队中：')+clipText(task.text,110);
    meta=[TARGET_LABEL[task.target]||'',task.status==='running'?'已进行 '+elapsedText(task.time):stamp(task.time)].filter(Boolean).join(' · ');
  }else if(task){
    headline='刚才这一轮：'+clipText(task.text,90);
    meta=(TASK_STATE[task.status]||task.status)+' · '+stamp(task.time);
  }else{
    headline='她还没开始新任务';
    meta='自主已开启 · 等你的第一条任务';
  }
  $('liveHeadline').textContent=headline;
  $('liveMeta').textContent=meta;
  $('liveAction').textContent=liveEventLine(task);
  $('liveThink').textContent=liveThinkTail?'思考：…'+clipText(liveThinkTail,150):'思考：尚未接通';
}
(function(){
  const list=$('messages'),jump=$('jumpLatest');
  if(!list||!jump)return;
  const sync=()=>{const far=list.scrollHeight-list.scrollTop-list.clientHeight>80;jump.hidden=!far||list.children.length<2;};
  list.addEventListener('scroll',sync,{passive:true});
  jump.addEventListener('click',()=>{list.scrollTop=list.scrollHeight;sync();});
  if(window.MutationObserver)new MutationObserver(sync).observe(list,{childList:true,subtree:true});
  sync();
})();
const reasonWindow=$('reasonWindow');
if(reasonWindow){
  const pref=localStorage.getItem('reasonHidden');
  // 思考观察窗是浮在右栏上面的，默认不收起来就会挡住"彼此做过什么 / 活动记录"。
  // 改成默认收起：想看思考原文时点顶栏的"思考小窗"，她关掉后也会记住。
  if(pref!=='0')reasonWindow.hidden=true;
  $('reasonToggle')?.addEventListener('click',()=>{if(!reasonWindow.hidden)return;reasonWindow.hidden=false;localStorage.setItem('reasonHidden','0');});
  $('reasonClose')?.addEventListener('click',()=>{reasonWindow.hidden=true;localStorage.setItem('reasonHidden','1');});
}
function fitConversation(){
  const box=document.querySelector('.center .conversation'),scroll=document.querySelector('.center .messages');
  if(!box)return;
  if(window.matchMedia('(min-width:1281px)').matches){box.style.height='';if(scroll)scroll.style.height='';return;}
  const top=box.getBoundingClientRect().top+window.scrollY;
  box.style.height=Math.max(340,window.innerHeight-top-14)+'px';
  if(scroll)scroll.style.height='';
}
window.addEventListener('resize',fitConversation,{passive:true});
window.addEventListener('load',fitConversation);
setInterval(fitConversation,700);
if(window.self!==window.top)setTimeout(fitConversation,120);
try{fitConversation();renderLiveNow();}catch{}
