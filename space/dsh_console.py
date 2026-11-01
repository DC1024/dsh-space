"""Owner's direct local console; no shell tools or automatic execution enabling."""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from owner_http_auth import owner_opener

BASE='http://127.0.0.1:19991'
sys.stdout.reconfigure(encoding='utf-8')
OPENER=None


def request(path, body=None, csrf=None):
    global OPENER
    if OPENER is None:OPENER=owner_opener(Path(__file__).resolve().parent,BASE)
    headers={}
    if body is not None:
        headers={'Content-Type':'application/json','Origin':BASE,'X-CSRF-Token':csrf or ''}
    req=urllib.request.Request(BASE+path,data=None if body is None else json.dumps(body,ensure_ascii=False).encode('utf-8'),headers=headers)
    try:
        with OPENER.open(req,timeout=20) as response:return json.load(response)
    except urllib.error.HTTPError as e:
        try:message=json.load(e).get('error')
        except ValueError:message='本机接口拒绝请求'
        raise RuntimeError(message) from None


def state_line(s):
    return '小鲸鱼 · '+('表人格' if s['active_persona']=='biao' else '里人格')+'；'+('自主活动关闭' if not s['autonomy_enabled'] else '自主活动暂停' if s['paused'] else '已允许自主活动')+'；'+('只保存留言，未发送模型' if not s['dispatch_enabled'] else '可接收主人消息')


def main():
    parser=argparse.ArgumentParser(description='与本机专属DSH交流；不会启用自主活动')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--target',choices=['assistant','phone','desktop','dialogue'],default='assistant')
    args=parser.parse_args();target=args.target
    s=request('/api/status');print(state_line(s))
    if args.status:return
    print('输入任务；/表 /里 切换，/手机 /电脑 /对话 /两者 选择执行端，/状态 查看，/暂停 停止，/退出 关闭终端。')
    while True:
        try:text=input('你 > ').strip()
        except (EOFError,KeyboardInterrupt):print('\n终端已关闭；没有恢复活动。');return
        if not text:continue
        if text in ('/退出','/exit'):return
        try:
            s=request('/api/status');persona=s['active_persona']
            if text=='/状态':print(state_line(s)+'；执行端：'+target);continue
            targets={'/手机':'phone','/电脑':'desktop','/对话':'dialogue','/两者':'assistant'}
            if text in targets:
                target=targets[text];print('新任务执行端：'+target);continue
            if text in ('/表','/里'):
                request('/api/control/persona',{'persona':'biao' if text=='/表' else 'li'},s['csrf'])
                print(state_line(request('/api/status')));continue
            if text=='/暂停':
                request('/api/control/pause',{'persona':persona},s['csrf']);print('已暂停；不会重发旧留言。');continue
            if text.startswith('/'):
                print('没有这个命令。终端不能开启自主活动或执行电脑命令。');continue
            result=request('/api/control/chat',{'persona':persona,'target':target,'text':text,'request_key':__import__('uuid').uuid4().hex},s['csrf'])
            if result['status']=='needs-review':print('已保存给当前人格；执行关闭，未向模型发送。');continue
            tid=result['id'];deadline=time.monotonic()+620
            while time.monotonic()<deadline:
                s=request('/api/status')
                task=next((t for t in s['tasks'] if t['id']==tid),None)
                if s['active_persona']!=persona:print('人格已切换，请在控制台查看原任务。');break
                if task and task['status'] in ('completed','stopped','needs-review','uncertain'):
                    receipt=request('/api/task-result?id='+tid)
                    text=(receipt.get('result') or {}).get('text')
                    if text:print('小鲸鱼 · '+('表人格' if persona=='biao' else '里人格')+' > '+text)
                    else:print(((receipt.get('result') or {}).get('failure') or {}).get('message') or '本轮状态：'+task['status']+'；尚无回答原文，不自动重发。')
                    break
                time.sleep(1)
            else:print('等待结束；以控制台回执为准，不自动重发。')
        except KeyboardInterrupt:
            s=request('/api/status');request('/api/control/pause',{'persona':s['active_persona']},s['csrf']);print('\n已请求暂停。')
        except (OSError,RuntimeError) as e:print('未完成：'+str(e))


if __name__=='__main__':main()
