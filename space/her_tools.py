"""Her container's files and argv-only exec, behind the same space gate."""
from space_tools import spec

TEXT = {'type': 'string'}
TOOLS = [
    spec('her_list', '列出她电脑上 /home/her 内的目录（最多200项，只返回相对路径）。', {'path': TEXT}),
    spec('her_read', '读取她电脑 /home/her 内的 UTF-8 文件，每页最多20000字；truncated=true 时用 next_offset 继续。',
         {'path': TEXT, 'offset': {'type': 'integer', 'minimum': 0},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20000}}, ['path']),
    spec('her_write', '写入她电脑 /home/her 内的文件（单文件≤256KiB），覆盖前保留旧版本；不能写符号链接。',
         {'path': TEXT, 'text': {'type': 'string', 'maxLength': 100000}}, ['path', 'text']),
    spec('her_run', '在她电脑的容器内按 argv 直接执行（不经 shell），默认30秒、上限60秒；cwd 只能是 /home/her 内的目录。',
         {'argv': {'type': 'array', 'items': {'type': 'string', 'maxLength': 2000},
                   'minItems': 1, 'maxItems': 32},
          'timeout_sec': {'type': 'integer', 'minimum': 1, 'maximum': 60},
          'cwd': TEXT}, ['argv']),
]

_ENDPOINTS = {'her_list': '/fs/list', 'her_read': '/fs/read', 'her_write': '/fs/write', 'her_run': '/exec'}


def invoke(store, name, args):
    import desktop
    endpoint = _ENDPOINTS.get(name)
    if not endpoint:
        raise ValueError('未知的她电脑工具')
    return desktop.remote_request(store.root, endpoint, args,
                                  mutating=name in ('her_write', 'her_run'))
