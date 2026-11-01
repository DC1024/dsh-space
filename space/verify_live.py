"""Consume the single normalized collector as a client; no native reasoning parser."""
import datetime as dt
import json
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BASE='http://127.0.0.1:19991'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path):
    with opener.open(BASE+path,timeout=10) as r:
        return json.load(r)


status=get('/api/status')
assert status['thinking']['canObserve'] and not status['thinking']['requiresPause']
sid=status['runtime']['session_id']
baseline=get('/api/thinking')
after=baseline.get('latestSeq',0)
ready=threading.Event(); records=[]; failures=[]; stop=threading.Event()


def listen():
    try:
        with opener.open(BASE+'/api/thinking/stream?after='+str(after),timeout=25) as r:
            ready.set()
            for line in r:
                if stop.is_set():break
                if not line.startswith(b'data:'):continue
                value=json.loads(line[5:].decode('utf-8'))
                if value.get('sessionId')!=sid:continue
                records.append({k:value.get(k) for k in ('seq','id','kind','origin','stream','complete','time','attemptId','chunkIndex')}
                    | {'received_monotonic':time.monotonic(),'text_chars':len(value.get('text',''))})
    except Exception as e:
        failures.append(str(e))


threading.Thread(target=listen,daemon=True).start()
if not ready.wait(12):raise RuntimeError('Normalized SSE did not open')
nonce='SPACE-'+uuid.uuid4().hex[:12]
message='这是已授权的接通验收。请先调用 memory_remember，kind=project，text="专属空间接通验收标记 '+nonce+'；这是系统测试标记，不是生活经历。"，source_note="主人授权的系统连接验收"。然后调用 memory_recall 查询 '+nonce+'，用一句中文报告查到的标记。不要安装、操作桌面或访问其他文件。'
request=urllib.request.Request(BASE+'/api/control/chat',data=json.dumps({'text':message,'request_key':'live-'+nonce}).encode(),
    headers={'Content-Type':'application/json','Origin':BASE,'X-CSRF-Token':status['csrf']})
with opener.open(request,timeout=10) as r:
    task_id=json.load(r)['id']
deadline=time.monotonic()+180
task=None
while time.monotonic()<deadline:
    state=get('/api/status')
    task=next((x for x in state['tasks'] if x['id']==task_id),None)
    if task and task['status'] in ('completed','stopped','needs-review'):break
    time.sleep(1)
completed_at=time.monotonic()
stop.set()
deltas=[e for e in records if e['kind']=='reasoning.delta' and e['origin']=='native-live' and e['text_chars']]
replies=[e for e in records if e['kind']=='reply.delta' and e['origin']=='native-live' and e['text_chars']]
memory=get('/api/memory?q='+nonce)
proof={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'session_id':sid,'task_id':task_id,'nonce':nonce,
       'task':task,'memory_ids':[m['id'] for m in memory], 'native_live_reasoning_deltas':len(deltas),
       'native_live_reply_deltas':len(replies),'first_reasoning_before_last_reply':bool(deltas and replies and deltas[0]['received_monotonic']<replies[-1]['received_monotonic']),
       'first_reasoning_before_task_completion':bool(deltas and deltas[0]['received_monotonic']<completed_at),
       'events_metadata':records,'stream_errors':failures,'budget':state['budget']}
out=ROOT/'evidence/live-flash-memory-stream.json'
out.write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in proof.items() if k not in ('events_metadata','task','budget')},ensure_ascii=True),flush=True)
if not(task and task['status']=='completed' and memory and proof['first_reasoning_before_last_reply']):
    raise SystemExit('Live test did not meet every acceptance gate; inspect local evidence')
