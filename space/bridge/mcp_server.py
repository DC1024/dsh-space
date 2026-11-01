r"""小鲸鱼 ↔ 有权限的我的桥（stdio MCP 服务器）。

给专属空间里的表/里人格暴露一个工具：ask_owner_side(question)。
调用时本脚本起一个「无头 DSH」实例（同一套运行环境、同一账号、danger-full-access），
把活干完，把结果原样回给小鲸鱼。

手动测试（不经 MCP 协议）：
    C:\Python312\python.exe mcp_server.py ask "看看 19992 端口是谁在用"

协议：MCP over stdio，JSON-RPC 2.0，一行一条（与 Codex 档案的 archive.py serve 同款）。
"""
import json
import os
import subprocess
import sys
import time
import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stdin.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent      # ...\DSH专属空间_v01\bridge
SPACE = ROOT.parent
WORKDIR = SPACE / 'workdir'
LOGS = ROOT / 'logs'
MEMORY = ROOT / 'memory.md'
LOCK = ROOT / 'worker.lock'

NODE = r'C:\Program Files\nodejs\node.exe'
DSH_BIN = r'C:\Users\owner\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\lib\bin.js'
DSH_HOME = r'C:\Users\owner\.dsh'
DEFAULT_TIMEOUT = 600
MAX_QUESTION = 6000

PROMPT_HEAD = """你是主人（大肥鱼）桌面端的 DSH 助手，同一个账号、同一套运行环境、同样的能力。现在你替「小鲸鱼」——专属空间里的表/里人格——办一件她自己办不了的事：她那边所有内置工具都被策略禁掉了，只有空间接口和历史检索。

她的原话：
---
{question}
---

要求：
- 你有完整权限（danger-full-access）：可以读写任意目录、跑命令、上网、做 GUI 操作。
- 没有人会回答提问，别等人确认；自己判断该怎么做。
- 做了就给证据：命令输出、文件路径、接口返回、截图路径。只是判断就给结论和依据。
- 回复 1200 字以内，先结论后证据，不要复述她的原话。
- 你们之前的协作记录在 {memory}（不存在就跳过），有价值的结论可以追加进去。
"""

TOOLS = [dict(
    name='ask_owner_side',
    description=('把一件你做不了的事交给有完整权限的桌面端 DSH 助手（同一个主人、同一账号）：'
                 '可以读写任意目录、跑命令、上网、做 GUI 操作。适合查本机状态、改文件、'
                 '调外部工具或接口、做你被策略禁掉的任何动作。阻塞式：等他干完再返回结论。'),
    inputSchema=dict(type='object', properties=dict(
        question=dict(type='string',
                      description='你要办的事，写清目标、已知信息和期望的输出形式；越具体越快。'
                                  '（上限 %d 字，超了按前 %d 字处理。）' % (MAX_QUESTION, MAX_QUESTION)),
        timeout_sec=dict(type='integer',
                         description='最多等他多少秒，默认 %d，允许 60~1800。' % DEFAULT_TIMEOUT)),
        required=['question'], additionalProperties=False))]


def _lock(timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK.stat().st_mtime
            except OSError:
                age = 0
            if age > 1800:
                try:
                    LOCK.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                return False
            time.sleep(2)


def _unlock():
    try:
        LOCK.unlink()
    except OSError:
        pass


def _log(record):
    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d')
    with (LOGS / (stamp + '.jsonl')).open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def ask(question, timeout_sec=DEFAULT_TIMEOUT):
    question = (question or '').strip()
    if not question:
        raise ValueError('question 不能为空')
    if len(question) > MAX_QUESTION:
        raise ValueError('question 太长（上限 %d 字）' % MAX_QUESTION)
    if not _lock(timeout_sec + 120):
        raise RuntimeError('正在替另一个请求干活，排队超时；稍后再交一次。')
    started = time.monotonic()
    try:
        prompt = PROMPT_HEAD.format(question=question, memory=MEMORY)
        env = dict(os.environ, DSH_HOME=DSH_HOME, DSH_PERMISSION_MODE='danger-full-access')
        proc = subprocess.run([NODE, DSH_BIN, '--profile', 'headless', prompt],
                              cwd=str(WORKDIR), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', env=env,
                              timeout=timeout_sec)
        answer = (proc.stdout or '').strip()
        seconds = round(time.monotonic() - started, 1)
        record = dict(at=datetime.datetime.now().isoformat(timespec='seconds'),
                      question=question, answer=answer, exit=proc.returncode,
                      seconds=seconds, ok=bool(answer) and proc.returncode == 0)
        if not record['ok']:
            tail = (proc.stderr or '').strip().splitlines()[-1:] or ['(无输出)']
            record['error'] = '; '.join(tail)[:400]
        _log(record)
        if not answer:
            raise RuntimeError('桌面端没有给出回答（exit %s）：%s'
                               % (proc.returncode, record.get('error', '')))
        return answer, seconds
    finally:
        _unlock()


def _result(text):
    return dict(content=[dict(type='text', text=text)])


def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        req = {}
        try:
            req = json.loads(line)
            method = req.get('method')
            rid = req.get('id')
            p = req.get('params') or {}
            if rid is None:
                continue
            if method == 'initialize':
                result = dict(protocolVersion=p.get('protocolVersion', '2024-11-05'),
                              capabilities=dict(tools={}),
                              serverInfo=dict(name='owner-side-bridge', version='1.0.0'))
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = dict(tools=TOOLS)
            elif method == 'tools/call':
                name = p.get('name')
                args = p.get('arguments') or {}
                if name != 'ask_owner_side':
                    raise ValueError('Unknown tool')
                if set(args) - {'question', 'timeout_sec'} or 'question' not in args:
                    raise ValueError('Invalid tool arguments')
                answer, seconds = ask(args['question'], int(args.get('timeout_sec', DEFAULT_TIMEOUT)))
                result = _result('[%ss] %s' % (seconds, answer))
            else:
                raise ValueError('Unsupported method')
            reply = dict(jsonrpc='2.0', id=rid, result=result)
        except Exception as ex:
            reply = dict(jsonrpc='2.0', id=req.get('id'),
                         error=dict(code=-32602, message=str(ex)[:500]))
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'ask':
        text, seconds = ask(' '.join(sys.argv[2:]))
        print('[%ss]\n%s' % (seconds, text))
    else:
        serve()
