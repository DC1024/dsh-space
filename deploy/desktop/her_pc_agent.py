"""Fixed-action desktop agent for the isolated X11 container.

Desktop: screen/click/type/key/scroll.
Files:   /fs/list, /fs/read, /fs/write confined to /home/her.
Exec:    /exec with an argv array (no shell), whitelisted environment.

The shipped container starts this agent as root (GUI session stays uid 1000),
so /exec can install packages or change system settings inside the isolated
container. Files written as root are chowned back to uid 1000.

No shell strings, no absolute paths, no host paths.
"""
import base64
import hmac
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DISPLAY = os.environ.get('DISPLAY', ':99')
TOKEN = os.environ.get('HER_AGENT_TOKEN', '')
ALLOWED_IP = os.environ.get('HER_ALLOWED_IP', '127.0.0.1')
PORT = int(os.environ.get('HER_AGENT_PORT', '19990'))
MAX_BODY = 8 * 1024 * 1024
VERSION = '1.1'
STARTED = time.time()
ENV = {**os.environ, 'DISPLAY': DISPLAY}
KEYS = {
    'ENTER': 'Return', 'ESC': 'Escape', 'TAB': 'Tab', 'BACKSPACE': 'BackSpace',
    'CTRL+L': 'ctrl+l', 'CTRL+A': 'ctrl+a', 'CTRL+C': 'ctrl+c', 'CTRL+V': 'ctrl+v',
    'CTRL+T': 'ctrl+t', 'ALT+LEFT': 'alt+Left', 'WIN': 'super',
}
ROOT = '/home/her'
HISTORY = os.path.join(ROOT, '.her-edit-history')
MAX_FILE = 256 * 1024
MAX_OUTPUT = 64 * 1024


def _run(cmd, timeout=10):
    return subprocess.run(cmd, env=ENV, capture_output=True, timeout=timeout,
                          creationflags=0)


def _chown_her(path):
    if os.geteuid() != 0:
        return
    try:
        os.chown(path, 1000, 1000)
    except OSError:
        pass


def geometry():
    try:
        out = _run(['xdotool', 'getdisplaygeometry']).stdout.decode('ascii', 'replace').split()
        return int(out[0]), int(out[1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return 1280, 800


def capture():
    path = '/tmp/her-frame.jpg'
    _run(['scrot', '-o', '-q', '70', path])
    with open(path, 'rb') as handle:
        data = handle.read()
    try:
        os.unlink(path)
    except OSError:
        pass
    if not data.startswith(b'\xff\xd8'):
        raise RuntimeError('frame format invalid')
    width, height = geometry()
    return {'image': base64.b64encode(data).decode('ascii'), 'width': width, 'height': height,
            'capturedAt': datetime.now().astimezone().isoformat()}


def active_class():
    try:
        return _run(['xdotool', 'getactivewindow', 'getwindowclassname']).stdout.decode(
            'utf-8', 'replace').strip().lower()
    except (OSError, subprocess.TimeoutExpired):
        return ''


def act(body):
    action = body.get('action')
    if action == 'screen':
        return {'ok': True, 'action': action, **capture()}
    if action == 'click':
        x, y = int(body['x']), int(body['y'])
        width, height = geometry()
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError('coordinate out of range')
        _run(['xdotool', 'mousemove', str(x), str(y)], timeout=5)
        _run(['xdotool', 'click', '--repeat', '1', '1'], timeout=5)
    elif action == 'type':
        text = body.get('text')
        if not isinstance(text, str) or len(text) > 12000:
            raise ValueError('text invalid')
        subprocess.run(['xclip', '-selection', 'clipboard', '-i'], input=text.encode('utf-8'),
                       env=ENV, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0)
        combo = 'ctrl+shift+v' if 'term' in active_class() else 'ctrl+v'
        _run(['xdotool', 'key', '--clearmodifiers', combo])
    elif action == 'key':
        key = body.get('key')
        if key not in KEYS:
            raise ValueError('key not allowed')
        _run(['xdotool', 'key', '--clearmodifiers', KEYS[key]])
    elif action == 'scroll':
        amount = int(body.get('amount', 0))
        if not -10 <= amount <= 10:
            raise ValueError('scroll out of range')
        button = '4' if amount > 0 else '5'
        for _ in range(abs(amount)):
            _run(['xdotool', 'click', '--repeat', '1', button], timeout=5)
    else:
        raise ValueError('unknown action')
    wait = body.get('screen_after_ms') or 0
    if isinstance(wait, (int, float)) and wait > 0:
        time.sleep(min(3.0, wait / 1000.0))
        frame = capture()
        return {'ok': True, 'action': action, 'width': frame['width'], 'height': frame['height'],
                'capturedAt': frame['capturedAt'], 'frameImage': frame['image'],
                'frameWidth': frame['width'], 'frameHeight': frame['height'],
                'frameCapturedAt': frame['capturedAt']}
    return {'ok': True, 'action': action}


def _safe_path(raw, directory=False):
    if not isinstance(raw, str) or not raw or len(raw) > 500:
        raise ValueError('path invalid')
    if raw.startswith('~') or any(ord(c) < 32 for c in raw):
        raise ValueError('path invalid')
    # Absolute paths are accepted only when they resolve inside her home.
    candidate = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(ROOT, raw))
    if candidate == ROOT:
        if directory:
            return candidate
        raise ValueError('path invalid')
    if not candidate.startswith(ROOT + os.sep):
        raise ValueError('path outside her home')
    return candidate


def fs_list(body):
    path = _safe_path(body.get('path') or '.', directory=True)
    if not os.path.isdir(path):
        raise ValueError('not a directory')
    entries = []
    for name in sorted(os.listdir(path))[:200]:
        full = os.path.join(path, name)
        if os.path.islink(full):
            continue
        is_dir = os.path.isdir(full)
        entries.append({'path': os.path.relpath(full, ROOT),
                        'type': 'dir' if is_dir else 'file',
                        'size': None if is_dir else os.path.getsize(full)})
    return {'ok': True, 'path': os.path.relpath(path, ROOT), 'entries': entries}


def fs_read(body):
    path = _safe_path(body.get('path'))
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError('not a regular file')
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError('file too large')
    offset = int(body.get('offset', 0))
    limit = int(body.get('limit', 20000))
    if offset < 0 or not 1 <= limit <= 20000:
        raise ValueError('pagination invalid')
    with open(path, 'r', encoding='utf-8-sig') as handle:
        text = handle.read()
    end = min(offset + limit, len(text))
    return {'ok': True, 'path': os.path.relpath(path, ROOT), 'text': text[offset:end],
            'offset': offset, 'total_chars': len(text),
            'next_offset': end if end < len(text) else None, 'truncated': end < len(text)}


def fs_write(body):
    path = _safe_path(body.get('path'))
    text = body.get('text')
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_FILE:
        raise ValueError('text invalid or too large')
    if os.path.islink(path):
        raise ValueError('symlink target rejected')
    previous = None
    if os.path.exists(path):
        if os.path.getsize(path) > MAX_FILE:
            raise ValueError('existing file too large')
        with open(path, 'r', encoding='utf-8-sig') as handle:
            previous = handle.read()
        os.makedirs(HISTORY, exist_ok=True)
        _chown_her(HISTORY)
        history = os.path.join(HISTORY, uuid.uuid4().hex + '-' + os.path.basename(path))
        with open(history, 'w', encoding='utf-8') as handle:
            handle.write(previous)
        _chown_her(history)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    _chown_her(parent)
    temp = path + '.tmp-' + uuid.uuid4().hex
    with open(temp, 'w', encoding='utf-8') as handle:
        handle.write(text)
    os.replace(temp, path)
    _chown_her(path)
    return {'ok': True, 'path': os.path.relpath(path, ROOT), 'chars': len(text),
            'previous_saved': previous is not None}


def exec_run(body):
    argv = body.get('argv')
    if (not isinstance(argv, list) or not argv or len(argv) > 32
            or any(not isinstance(item, str) or not item or len(item) > 2000 for item in argv)):
        raise ValueError('argv invalid')
    try:
        timeout = int(body.get('timeout_sec', 30))
    except (TypeError, ValueError):
        timeout = 30
    timeout = max(1, min(60, timeout))
    cwd = _safe_path(body.get('cwd') or '.', directory=True)
    if not os.path.isdir(cwd):
        raise ValueError('cwd not a directory')
    env = {k: os.environ[k] for k in ('PATH', 'DISPLAY', 'LANG', 'TZ', 'USER') if k in os.environ}
    env['HOME'] = ROOT
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout,
                              creationflags=0)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'command timed out after ' + str(timeout) + 's',
                'timed_out': True, 'executed': True}
    except FileNotFoundError:
        return {'ok': False, 'error': 'command not found', 'executed': False}
    out = proc.stdout[:MAX_OUTPUT].decode('utf-8', 'replace')
    err = proc.stderr[:MAX_OUTPUT].decode('utf-8', 'replace')
    return {'ok': True, 'argv': argv, 'exit': proc.returncode, 'stdout': out, 'stderr': err,
            'uid': os.geteuid(),
            'truncated': len(proc.stdout) > MAX_OUTPUT or len(proc.stderr) > MAX_OUTPUT}


def health():
    width, height = geometry()
    return {'ok': True, 'agent': 'dsh-her-pc', 'version': VERSION,
            'display': {'width': width, 'height': height},
            'uid': os.geteuid(),
            'uptimeSeconds': round(time.time() - STARTED, 1)}


class Handler(BaseHTTPRequestHandler):
    server_version = 'dsh-her-pc/' + VERSION

    def log_message(self, *args):
        pass

    def _reply(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        ip = self.client_address[0]
        if not TOKEN:
            print('deny: token not configured', flush=True)
            return False
        if ip != ALLOWED_IP:
            print('deny ip=' + ip, flush=True)
            return False
        header = self.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            print('deny: missing bearer', flush=True)
            return False
        if not hmac.compare_digest(header[7:].strip(), TOKEN):
            print('deny token len=' + str(len(header[7:].strip())) + ' expected=' + str(len(TOKEN)),
                  flush=True)
            return False
        return True

    def do_GET(self):
        if self.path != '/health':
            return self._reply(404, {'ok': False, 'error': 'unknown endpoint'})
        if not self._authorized():
            return self._reply(403, {'ok': False, 'error': 'forbidden'})
        try:
            return self._reply(200, health())
        except BaseException as error:
            return self._reply(200, {'ok': False, 'error': str(error)[:200]})

    def do_POST(self):
        routes = {'/action': act, '/fs/list': fs_list, '/fs/read': fs_read,
                  '/fs/write': fs_write, '/exec': exec_run}
        handler = routes.get(self.path)
        if handler is None:
            return self._reply(404, {'ok': False, 'error': 'unknown endpoint'})
        if not self._authorized():
            return self._reply(403, {'ok': False, 'error': 'forbidden'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._reply(200, {'ok': False, 'error': 'body length invalid'})
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return self._reply(200, {'ok': False, 'error': 'body invalid'})
        if not isinstance(body, dict):
            return self._reply(200, {'ok': False, 'error': 'body invalid'})
        try:
            return self._reply(200, handler(body))
        except (ValueError, KeyError, TypeError) as error:
            # Rejected before doing anything: the caller can retry safely.
            return self._reply(200, {'ok': False, 'error': str(error)[:300], 'executed': False})
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            return self._reply(200, {'ok': False, 'error': str(error)[:300], 'executed': True})
        except BaseException as error:
            return self._reply(200, {'ok': False, 'error': 'action failed: ' + type(error).__name__,
                                     'executed': True})


if __name__ == '__main__':
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
