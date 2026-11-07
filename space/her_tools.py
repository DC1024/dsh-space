"""Her container's files and argv-only exec, behind the same space gate."""
import json
import os
import re
from space_tools import spec

TEXT = {'type': 'string'}
READ_LIMIT = 12000
RUN_TIMEOUT = 30
OUTPUT_CLIP = 12000

# 这些命令在前台通常超过 30 秒；直接拒绝并引导到 her_start，
# 避免模型反复等超时、把输出塞满上下文。
_LONG_COMMAND = re.compile(
    r'(^|\s)(?:sleep\s+\d+|pnpm\s+(?:test|install|add|build|run)|npm\s+(?:test|install|ci|run)|'
    r'yarn\s+(?:test|install|build|run)|npx|pytest|go\s+(?:test|build)|cargo\s+(?:build|test|run)|'
    r'make|cmake|gradle|\.\/gradlew|mvn|apt(?:-get)?\s+.*install|pip3?\s+install|'
    r'git\s+clone|docker\s+build|podman\s+build|uv\s+(?:sync|pip)|python3?\s+-m\s+pytest)(\s|$)', re.I)

TOOLS = [
    spec('her_list', '列出她电脑上 /home/her 内的目录（最多200项，只返回相对路径）。', {'path': TEXT}),
    spec('her_read', '读取她电脑 /home/her 内的 UTF-8 文件，默认每页12000字（最多20000字）；truncated=true 时用 next_offset 继续。读日志优先从尾部读取，不要反复全量读同一文件。',
         {'path': TEXT, 'offset': {'type': 'integer', 'minimum': 0},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20000}}, ['path']),
    spec('her_write', '写入她电脑 /home/her 内的文件（单文件≤256KiB），覆盖前保留旧版本；不能写符号链接。',
         {'path': TEXT, 'text': {'type': 'string', 'maxLength': 100000}}, ['path', 'text']),
    spec('her_run', '在她电脑的容器内按 argv 直接执行（不经 shell），以 root 运行，可安装软件或改系统设置；默认20秒、上限30秒；cwd 只能是 /home/her 内的目录。超过30秒的测试/构建/安装会被拒绝，请改用 her_start 后台启动；输出只保留最后12000字。',
         {'argv': {'type': 'array', 'items': {'type': 'string', 'maxLength': 2000},
                   'minItems': 1, 'maxItems': 32},
          'timeout_sec': {'type': 'integer', 'minimum': 1, 'maximum': 30},
          'cwd': TEXT}, ['argv']),
    spec('her_start', '在她电脑上后台启动长命令并立即返回 pid 和日志路径，不等待执行结束；之后用 her_read 轮询日志尾部。适用于 pnpm test、构建、依赖安装等超过30秒的命令。',
         {'argv': {'type': 'array', 'items': {'type': 'string', 'maxLength': 2000},
                   'minItems': 1, 'maxItems': 32},
          'log': TEXT, 'cwd': TEXT}, ['argv']),
]

_ENDPOINTS = {'her_list': '/fs/list', 'her_read': '/fs/read', 'her_write': '/fs/write', 'her_run': '/exec'}


def _long_command(argv):
    joined = ' '.join([os.path.basename(str(argv[0]))] + [str(x) for x in argv[1:]])
    return bool(_LONG_COMMAND.search(joined))


def _clip_tail(text, limit=OUTPUT_CLIP):
    text = str(text or '')
    if len(text) <= limit:
        return text, False
    return '…（前略，仅保留最后 %d 字）\n' % limit + text[-limit:], True


def _start(store, args):
    import desktop
    argv = args.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError('argv 必须是非空字符串数组')
    cwd = str(args.get('cwd') or '/home/her')
    log = str(args.get('log') or 'lab/her-start.log')
    if not cwd.startswith('/home/her'):
        raise ValueError('cwd 必须位于 /home/her 内')
    if not log.startswith('/'):
        log = '/home/her/' + log
    if not log.startswith('/home/her/'):
        raise ValueError('日志路径必须位于 /home/her 内')
    payload = json.dumps({'argv': argv, 'log': log, 'cwd': cwd}, ensure_ascii=False)
    code = ("import json,subprocess; d=json.loads(%r); "
            "f=open(d['log'],'w',encoding='utf-8'); "
            "p=subprocess.Popen(d['argv'],cwd=d['cwd'],stdin=subprocess.DEVNULL,"
            "stdout=f,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True); "
            "print(p.pid)" % payload)
    value = desktop.remote_request(store.root, '/exec',
        {'argv': ['python3', '-c', code], 'cwd': '/home/her', 'timeout_sec': 10},
        mutating=True)
    pid = (value.get('stdout') or '').strip()
    return {'ok': True, 'pid': int(pid) if pid.isdigit() else None, 'log': log}


def invoke(store, name, args):
    import desktop
    if name == 'her_start':
        return _start(store, args)
    endpoint = _ENDPOINTS.get(name)
    if not endpoint:
        raise ValueError('未知的她电脑工具')
    if name == 'her_read':
        args = dict(args)
        args.setdefault('limit', READ_LIMIT)
        value = desktop.remote_request(store.root, endpoint, args, mutating=False)
        if isinstance(value.get('text'), str):
            clipped, cut = _clip_tail(value['text'])
            if cut:
                value = {**value, 'text': clipped, 'truncated': True,
                         'note': '单页输出过长，已只保留最后 %d 字；用 offset/limit 读取更早内容。' % OUTPUT_CLIP}
        return value
    if name == 'her_run':
        args = dict(args)
        timeout = args.get('timeout_sec', 20)
        if type(timeout) is not int or not 1 <= timeout <= RUN_TIMEOUT:
            raise ValueError('her_run 前台最多 %d 秒；长命令请改用 her_start 后台运行，再用 her_read 轮询日志。' % RUN_TIMEOUT)
        args['timeout_sec'] = timeout
        argv = args.get('argv')
        if isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv) and _long_command(argv):
            raise ValueError('检测到可能超过30秒的命令；请改用 her_start 在后台启动，再用 her_read 读日志尾部，避免前台超时和上下文膨胀。')
        value = desktop.remote_request(store.root, endpoint, args, mutating=True)
        out = dict(value); cut_any = False
        for key in ('stdout', 'stderr'):
            if isinstance(out.get(key), str):
                clipped, cut = _clip_tail(out[key])
                if cut:
                    out[key] = clipped; cut_any = True
        if cut_any:
            out['truncated'] = True
            out['note'] = '输出过长，只保留最后 %d 字；需要完整结果请用 her_start 写日志，再用 her_read 分段读取。' % OUTPUT_CLIP
        return out
    return desktop.remote_request(store.root, endpoint, args,
                                  mutating=name == 'her_write')
