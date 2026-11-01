r"""跟专属空间里的小鲸鱼说话：一条命令问一句，或者进终端连续聊。

用法（在 C:\dsh-space\DSH专属空间_v01 下）：
    C:\Python312\python.exe talk_to_dsh.py --status
    C:\Python312\python.exe talk_to_dsh.py "只回答两个字：在的"
    C:\Python312\python.exe talk_to_dsh.py            # 进交互终端
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = 'http://127.0.0.1:19991'
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stdin.reconfigure(encoding='utf-8', errors='replace')

OPENER = None
TERMINAL = ('completed', 'stopped', 'needs-review', 'uncertain')
TARGETS = {'/手机': 'phone', '/电脑': 'desktop', '/对话': 'dialogue', '/两者': 'assistant'}


def opener():
    global OPENER
    if OPENER is None:
        from owner_http_auth import owner_opener
        OPENER = owner_opener(ROOT, BASE)
    return OPENER


def request(path, body=None, csrf=None, timeout=30):
    headers = {}
    data = None
    if body is not None:
        headers = {'Content-Type': 'application/json', 'Origin': BASE, 'X-CSRF-Token': csrf or ''}
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with opener().open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            message = json.load(e).get('error')
        except ValueError:
            message = '本机接口拒绝请求（HTTP %s）' % e.code
        raise RuntimeError(message) from None
    except urllib.error.URLError as e:
        raise RuntimeError('连不上空间控制台（%s）；先运行 启动专属空间.cmd 或 start_space.pyw' % e.reason) from None


def ensure_server(timeout=90):
    try:
        request('/api/status', timeout=10)
        return True
    except RuntimeError:
        pass
    launcher = ROOT / 'start_space.pyw'
    if not launcher.exists():
        return False
    print('空间控制台没起来，正在启动…', flush=True)
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, str(launcher)], cwd=str(ROOT), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            request('/api/status', timeout=5)
            print('已起来。', flush=True)
            return True
        except RuntimeError:
            continue
    return False


def state_line(s, target):
    persona = '表人格' if s['active_persona'] == 'biao' else '里人格'
    if not s['autonomy_enabled']:
        activity = '自主活动关闭'
    elif s['paused']:
        activity = '自主活动暂停'
    else:
        activity = '已允许自主活动'
    dispatch = '只保存留言，未发送模型' if not s['dispatch_enabled'] else '可接收主人消息'
    return '小鲸鱼 · %s；%s；%s；执行端：%s' % (persona, activity, dispatch, target)


def wait_reply(task_id, persona, limit=620):
    deadline = time.monotonic() + limit
    started = time.monotonic()
    while time.monotonic() < deadline:
        s = request('/api/status')
        if s['active_persona'] != persona:
            return None, '人格已切换，请在控制台查看原任务。'
        task = next((t for t in s['tasks'] if t['id'] == task_id), None)
        if task and task['status'] in TERMINAL:
            receipt = request('/api/task-result?id=' + task_id)
            result = receipt.get('result') or {}
            text = result.get('text')
            if text:
                return text, '%.1f 秒' % (time.monotonic() - started)
            failure = result.get('failure') or {}
            return None, failure.get('message') or ('本轮状态：%s；尚无回答原文，不自动重发。' % task['status'])
        time.sleep(1)
    return None, '等待结束；以控制台回执为准，不自动重发。'


def ask(text, persona, target, echo_sent=True):
    s = request('/api/status')
    if not s['dispatch_enabled']:
        return None, '这轮没发出：控制台当前只保存留言（dispatch 关闭）。'
    result = request('/api/control/chat', {'persona': persona, 'target': target, 'text': text,
                                          'request_key': uuid.uuid4().hex}, s['csrf'])
    if result['status'] == 'needs-review':
        return None, '已保存给当前人格；执行关闭，未向模型发送。'
    tid = result['id']
    if echo_sent: print('（已发出 %s，等小鲸鱼回话…）' % tid[:8], flush=True)
    return wait_reply(tid, persona)


def main():
    parser = argparse.ArgumentParser(description='与专属空间里的小鲸鱼对话；不会启用或恢复自主活动')
    parser.add_argument('text', nargs='*', help='要问的一句话；不给就进交互终端')
    parser.add_argument('--status', action='store_true', help='只看状态')
    parser.add_argument('--persona', choices=['biao', 'li'], help='这一次用表人格还是里人格')
    parser.add_argument('--target', choices=['assistant', 'phone', 'desktop', 'dialogue'], help='这一次的执行端')
    parser.add_argument('--timeout', type=int, default=620, help='等回答的秒数，默认 620')
    args = parser.parse_args()

    if not ensure_server():
        print('空间控制台起不来；没有发送任何内容。')
        return 2

    s = request('/api/status')
    persona = args.persona or s['active_persona']
    target = args.target or 'assistant'

    if args.status:
        print(state_line(s, target))
        return 0

    if args.text:
        text = ' '.join(args.text).strip()
        if not text:
            return 2
        if args.persona and args.persona != s['active_persona']:
            request('/api/control/persona', {'persona': persona}, s['csrf'])
        reply, note = ask(text, persona, target, echo_sent=False)
        if reply:
            print('小鲸鱼 > ' + reply)
            print('（用时 %s）' % note)
            return 0
        print('未完成：' + note)
        return 1

    if args.status:
        return 0

    print(state_line(s, target))
    print('直接打字就是跟小鲸鱼说话；/帮助 看命令，/退出 关闭。')
    current = s['active_persona']
    while True:
        try:
            line = input('你 > ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\n终端已关闭；没有恢复活动。')
            return 0
        if not line:
            continue
        if line in ('/退出', '/exit', '/quit'):
            return 0
        if line in ('/帮助', '/h', '/?'):
            print('直接打字＝发任务；/表 /里＝换人格；/手机 /电脑 /对话 /两者＝换执行端；'
                  '/状态 /暂停 /帮助 /退出。')
            continue
        try:
            s = request('/api/status')
            if line == '/状态':
                print(state_line(s, target))
                continue
            if line in TARGETS:
                target = TARGETS[line]
                print('新任务执行端：' + target)
                continue
            if line in ('/表', '/里'):
                request('/api/control/persona', {'persona': 'biao' if line == '/表' else 'li'}, s['csrf'])
                s = request('/api/status')
                current = s['active_persona']
                print(state_line(s, target))
                continue
            if line == '/暂停':
                request('/api/control/pause', {'persona': current}, s['csrf'])
                print('已暂停；不会重发旧留言。')
                continue
            if line.startswith('/'):
                print('没有这个命令；终端不能开启自主活动或执行电脑命令。')
                continue
            reply, note = ask(line, current, target)
            if reply:
                print(('表人格' if current == 'biao' else '里人格') + ' > ' + reply)
            else:
                print('未完成：' + note)
        except KeyboardInterrupt:
            try:
                s = request('/api/status')
                request('/api/control/pause', {'persona': s['active_persona']}, s['csrf'])
                print('\n已请求暂停。')
            except RuntimeError as e:
                print('\n已请求停止；' + str(e))
        except (OSError, RuntimeError) as e:
            print('未完成：' + str(e))


if __name__ == '__main__':
    sys.exit(main())
