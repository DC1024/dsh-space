"""Her container's files and argv-only exec, behind the same space gate."""
import json
from space_tools import spec

TEXT = {'type': 'string'}
TOOLS = [
    spec('her_list', '列出她电脑上 /home/her 内的目录（最多200项，只返回相对路径）。', {'path': TEXT}),
    spec('her_read', '读取她电脑 /home/her 内的 UTF-8 文件，每页最多20000字；truncated=true 时用 next_offset 继续。',
         {'path': TEXT, 'offset': {'type': 'integer', 'minimum': 0},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20000}}, ['path']),
    spec('her_write', '写入她电脑 /home/her 内的文件（单文件≤256KiB），覆盖前保留旧版本；不能写符号链接。',
         {'path': TEXT, 'text': {'type': 'string', 'maxLength': 100000}}, ['path', 'text']),
    spec('her_run', '在她电脑的容器内按 argv 直接执行（不经 shell），以 root 运行，可安装软件或改系统设置；默认30秒、上限60秒；cwd 只能是 /home/her 内的目录。超过30秒的测试/构建/安装请改用 her_start，不要在前台等到超时。',
         {'argv': {'type': 'array', 'items': {'type': 'string', 'maxLength': 2000},
                   'minItems': 1, 'maxItems': 32},
          'timeout_sec': {'type': 'integer', 'minimum': 1, 'maximum': 60},
          'cwd': TEXT}, ['argv']),
    spec('her_start', '在她电脑上后台启动长命令并立即返回 pid 和日志路径，不等待执行结束；之后用 her_read 轮询日志。适用于 pnpm test、构建、依赖安装等超过30秒的命令。',
         {'argv': {'type': 'array', 'items': {'type': 'string', 'maxLength': 2000},
                   'minItems': 1, 'maxItems': 32},
          'log': TEXT, 'cwd': TEXT}, ['argv']),
]

_ENDPOINTS = {'her_list': '/fs/list', 'her_read': '/fs/read', 'her_write': '/fs/write', 'her_run': '/exec'}


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
    return desktop.remote_request(store.root, endpoint, args,
                                  mutating=name in ('her_write', 'her_run'))
