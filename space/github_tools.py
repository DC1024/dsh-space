"""GitHub REST access for the owner-dedicated account.

The PAT is injected from data/github/token (0600) and must never be returned to
the model, written to notes, or logged. Both personas share this account by
owner decision; every call is recorded as a space event for later audit.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from space_tools import spec

METHODS = ('GET', 'POST', 'PATCH', 'PUT', 'DELETE')
TOKEN_PATH = 'data/github/token'
BASE = 'https://api.github.com'

TOOLS = [
    spec('github_request',
         '调用 GitHub REST API。path 必须是 / 开头的相对路径，例如 /repos/whale-duet/whale-notes/issues；method 支持 GET/POST/PATCH/PUT/DELETE。token 由服务端注入，绝不要把它放进参数、笔记、记忆或输出。写操作会记入空间审计事件。',
         {'method': {'type': 'string', 'enum': list(METHODS)},
          'path': {'type': 'string', 'maxLength': 2000},
          'query': {'type': 'object', 'additionalProperties': {'type': 'string'}, 'maxProperties': 20},
          'body': {'type': 'object'},
          'max_bytes': {'type': 'integer', 'minimum': 1000, 'maximum': 2000000}},
         ['method', 'path'])
]


def _token(store):
    path = store.root / TOKEN_PATH
    try:
        token = path.read_text(encoding='ascii').strip()
    except OSError as error:
        raise RuntimeError('GitHub token 尚未配置（data/github/token）') from error
    if not token:
        raise RuntimeError('GitHub token 为空')
    return token


def _read_response(response, max_bytes):
    raw = response.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    content_type = response.headers.get('Content-Type', '')
    if 'json' in content_type and raw:
        try:
            body = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            body = raw.decode('utf-8', 'replace')
    else:
        body = raw.decode('utf-8', 'replace') if raw else None
    headers = {key: response.headers.get(key) for key in (
        'x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset', 'link')
        if response.headers.get(key) is not None}
    return {'ok': True, 'status': response.status, 'body': body,
            'truncated': truncated, 'headers': headers}


def invoke(store, name, args):
    if name != 'github_request':
        raise ValueError('未知的 GitHub 工具')
    method = str(args.get('method') or 'GET').upper()
    if method not in METHODS:
        raise ValueError('不支持的 GitHub 方法')
    path = str(args.get('path') or '')
    if not path.startswith('/') or '://' in path or '..' in path or len(path) > 2000:
        raise ValueError('path 必须是 api.github.com 下的相对路径')
    query = args.get('query')
    if query is not None and not isinstance(query, dict):
        raise ValueError('query 必须是对象')
    if query:
        path = path + '?' + urllib.parse.urlencode({str(k): str(v) for k, v in query.items()})
    body = args.get('body')
    data = None
    headers = {'Authorization': 'Bearer ' + _token(store),
               'Accept': 'application/vnd.github+json',
               'User-Agent': 'dsh-space'}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    max_bytes = args.get('max_bytes') or 200000
    status = None
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = _read_response(response, int(max_bytes))
            status = result['status']
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read(int(max_bytes) + 1)[:int(max_bytes)]
        try:
            body = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            body = raw.decode('utf-8', 'replace')
        result = {'ok': False, 'status': status, 'body': body, 'truncated': False}
    except Exception as error:
        result = {'ok': False, 'status': None, 'error': type(error).__name__ + ': ' + str(error)[:300]}
    try:
        store.event('github.request', {
            'method': method, 'path': path, 'status': status,
            'write': method not in ('GET', 'HEAD'),
            'message': 'GitHub API 调用已记录；token 不写入事件'})
    except Exception:
        pass
    return result
