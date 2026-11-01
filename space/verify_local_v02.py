"""Protocol-only checks against the live owner console; never prompt a model."""
import json
from pathlib import Path
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parent
base='http://127.0.0.1:19991'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

def get(path):
    with opener.open(base+path,timeout=20) as response:return json.load(response)

def main():
    status=get('/api/status')
    assert status['paused'] is True and not status['dispatch_enabled'] and not status['autonomy_enabled']
    assert len({p['session_id'] for p in status['personas'] if p['session_id']})==2
    key=(ROOT/'data/bridge.key').read_text(encoding='ascii').strip()
    seen={}
    for persona in ('biao','li'):
        body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'memory_recall','arguments':{'query':''}}}
        request=urllib.request.Request(base+'/mcp/'+persona,data=json.dumps(body).encode(),headers={
            'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        with opener.open(request,timeout=20) as response:result=json.load(response)
        rows=json.loads(result['result']['content'][0]['text'])
        assert all(r['persona'] in (persona,'shared') for r in rows)
        seen[persona]={r['id'] for r in rows if r['persona']!='shared'}
    assert not (seen['biao']&seen['li'])
    # Owner hold rejects both dispatch and a forged budget reservation, even with bridge auth.
    body={'session_id':status['runtime']['session_id']}
    request=urllib.request.Request(base+'/bridge/gate',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    try:opener.open(request,timeout=10);raise AssertionError('hold bypassed')
    except urllib.error.HTTPError as e:assert e.code==409
    result={'passed':True,'model_prompts_sent':0,'device_actions_sent':0,
            'active_persona':status['active_persona'],'session_ids_distinct':True,
            'private_memory_counts':{p:len(v) for p,v in seen.items()},
            'private_ids_overlap':False,'owner_hold_gate_rejected':True,
            'dispatch_enabled':False,'autonomy_enabled':False}
    target=ROOT/'evidence/local-repair-v02-protocol.json'
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True))

if __name__=='__main__':main()
