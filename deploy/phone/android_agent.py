#!/usr/bin/env python3
"""她的手机：固定动作 API。

只做白名单动作（看屏、点、滑、打字、按键、查应用、开应用），
不提供任意 adb shell 透传；每次动作前唤醒、空闲后主动熄屏。
鉴权：Bearer token；来源限制由宿主防火墙负责。
"""
import base64
import hmac
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADB = '/usr/bin/adb'
BIND = os.environ.get('HER_ANDROID_BIND', '0.0.0.0')
PORT = int(os.environ.get('HER_ANDROID_PORT', '19992'))
TOKEN_FILE = os.environ.get('HER_ANDROID_TOKEN_FILE', '/etc/her-android/token')
IDLE_SLEEP_SECONDS = 30
KEY_WAKE = '224'
KEY_SLEEP = '223'

state = {'last_action': 0.0, 'screen_on': False, 'ui_raw': None, 'ui_at': 0.0}
lock = threading.Lock()


def token():
    try:
        return open(TOKEN_FILE, encoding='ascii').read().strip()
    except OSError:
        return ''


def adb(args, timeout=20, binary=False):
    return subprocess.run([ADB, *args], capture_output=True, timeout=timeout, text=not binary)


def serial():
    out = adb(['devices'])
    for line in (out.stdout or '').splitlines()[1:]:
        if '\tdevice' in line:
            return line.split('\t')[0].strip()
    return None


def shell(*args, timeout=20):
    return adb(['shell', *args], timeout=timeout)


def wake():
    """息屏时触摸不会生效，所以动作前统一唤醒并收起锁屏层。"""
    with lock:
        adb(['shell', 'input', 'keyevent', KEY_WAKE], timeout=10)
        adb(['shell', 'wm', 'dismiss-keyguard'], timeout=10)
        state['screen_on'] = True
        state['last_action'] = time.time()


def idle_loop():
    while True:
        time.sleep(5)
        with lock:
            if state['screen_on'] and time.time() - state['last_action'] > IDLE_SLEEP_SECONDS:
                adb(['shell', 'input', 'keyevent', KEY_SLEEP], timeout=10)
                state['screen_on'] = False


def wakefulness():
    out = shell('dumpsys', 'power')
    for line in (out.stdout or '').splitlines():
        if 'mWakefulness=' in line:
            return line.split('=')[1].strip()
    return 'unknown'


def health():
    if not serial():
        return {'ok': False, 'code': 'DEVICE_OFFLINE', 'executed': False,
                'error': '手机未连接或未授权'}
    model = shell('getprop', 'ro.product.model').stdout.strip()
    version = shell('getprop', 'ro.build.version.release').stdout.strip()
    sdk = shell('getprop', 'ro.build.version.sdk').stdout.strip()
    size = shell('wm', 'size').stdout.strip()
    density = shell('wm', 'density').stdout.strip()
    battery = shell('dumpsys', 'battery').stdout
    level = ''
    for line in battery.splitlines():
        if 'level:' in line:
            level = line.split(':')[-1].strip()
            break
    return {'ok': True, 'serial': serial(), 'model': model, 'android': version, 'sdk': sdk,
            'size': size.replace('Physical size: ', ''), 'density': density.replace('Physical density: ', ''),
            'battery': level, 'awake': wakefulness(), 'executed': False}


def shot():
    if not serial():
        return None, {'ok': False, 'code': 'DEVICE_OFFLINE', 'executed': False,
                      'error': '手机未连接或未授权'}
    wake()
    try:
        out = adb(['exec-out', 'screencap', '-p'], timeout=20, binary=True)
    except subprocess.TimeoutExpired:
        return None, {'ok': False, 'code': 'ADB_TIMEOUT', 'executed': True, 'uncertain': True,
                      'error': '截图超时'}
    if out.returncode != 0 or not out.stdout.startswith(b'\x89PNG'):
        return None, {'ok': False, 'code': 'SCREENSHOT_FAILED', 'executed': True, 'uncertain': True,
                      'error': (out.stderr or b'')[:200].decode('utf-8', 'replace')}
    with lock:
        state['last_action'] = time.time()
    return out.stdout, None


def invalidate_ui():
    with lock:
        state['ui_raw'] = None
        state['ui_at'] = 0.0


def dump_ui(force=False):
    """取当前界面的控件树（uiautomator），用于按文字定位。

    --compressed 明显更快（约 2 秒，普通模式在动画界面会等 idle 十几秒后失败）。
    3 秒内复用最近一次结果，避免同一步里 view / tap(text) 重复 dump。
    """
    now = time.time()
    with lock:
        cached, cached_at = state['ui_raw'], state['ui_at']
    if not force and cached and now - cached_at < 3:
        return cached, None
    wake()
    try:
        out = shell('uiautomator', 'dump', '--compressed', '/sdcard/her-ui.xml', timeout=15)
    except subprocess.TimeoutExpired:
        return None, {'ok': False, 'executed': True, 'code': 'UI_DUMP_TIMEOUT',
                      'error': '读取界面文字超时（界面可能一直在动画）'}
    text = (out.stdout or '') + (out.stderr or '')
    if 'ERROR' in text or 'Exception' in text:
        return None, {'ok': False, 'executed': True, 'code': 'UI_DUMP_FAILED', 'error': text[:200]}
    raw = shell('cat', '/sdcard/her-ui.xml', timeout=20).stdout or ''
    with lock:
        state['ui_raw'] = raw
        state['ui_at'] = time.time()
        state['last_action'] = time.time()
    return raw, None


def ui_items(raw, limit=60):
    items = []
    for tag in re.findall(r'<node[^>]*>', raw):
        bounds = re.search(r'bounds="([^"]*)"', tag)
        text = re.search(r'text="([^"]*)"', tag)
        desc = re.search(r'content-desc="([^"]*)"', tag)
        label = ((text.group(1) if text else '') or (desc.group(1) if desc else '')).strip()
        if not label or not bounds:
            continue
        if items and items[-1]['text'] == label:
            continue
        items.append({'text': label[:40], 'bounds': bounds.group(1)})
        if len(items) >= limit:
            break
    return items


def ui():
    if not serial():
        return {'ok': False, 'code': 'DEVICE_OFFLINE', 'executed': False, 'error': '手机未连接'}
    raw, failure = dump_ui()
    if failure:
        return failure
    items = ui_items(raw)
    return {'ok': True, 'executed': False, 'count': len(items), 'items': items}


def text_bounds(label):
    raw, failure = dump_ui()
    if failure:
        return None, failure
    for item in ui_items(raw, limit=400):
        if label in item['text']:
            return item['bounds'], None
    return None, {'ok': False, 'executed': False, 'code': 'TEXT_NOT_FOUND',
                  'error': '当前界面没有找到这段文字'}


def center(bounds):
    match = re.findall(r'\d+', bounds)
    if len(match) < 4:
        raise ValueError('坐标无效')
    x1, y1, x2, y2 = (int(value) for value in match[:4])
    return (x1 + x2) // 2, (y1 + y2) // 2


def tap(body):
    label = str(body.get('text', '') or '').strip()
    if label:
        bounds, failure = text_bounds(label)
        if failure:
            return failure
        x, y = center(bounds)
    else:
        try:
            x, y = int(body.get('x', -1)), int(body.get('y', -1))
        except (TypeError, ValueError):
            raise ValueError('坐标无效')
    if not (0 <= x <= 5000 and 0 <= y <= 5000):
        raise ValueError('坐标无效')
    wake()
    shell('input', 'tap', str(x), str(y))
    invalidate_ui()
    with lock:
        state['last_action'] = time.time()
    return {'ok': True, 'executed': True, 'action': 'tap', 'x': x, 'y': y}


def swipe(body):
    values = [int(body.get(name, -1)) for name in ('x1', 'y1', 'x2', 'y2')]
    if any(not 0 <= value <= 5000 for value in values):
        raise ValueError('坐标无效')
    duration = max(50, min(3000, int(body.get('ms', 400))))
    wake()
    shell('input', 'swipe', *[str(value) for value in values], str(duration))
    invalidate_ui()
    with lock:
        state['last_action'] = time.time()
    return {'ok': True, 'executed': True, 'action': 'swipe', 'ms': duration}


def text(body):
    value = str(body.get('text', ''))
    if not value or len(value) > 200:
        raise ValueError('文字为空或过长')
    wake()
    method = 'ascii'
    switched = False
    if value.isascii():
        shell('input', 'text', value.replace(' ', '%s'))
    else:
        enabled = shell('ime', 'list', '-s').stdout or ''
        if 'com.android.adbkeyboard/.AdbIME' not in enabled:
            return {'ok': False, 'executed': False, 'code': 'NEEDS_IME',
                    'error': '中文输入需要 ADBKeyboard（未安装或未启用）'}
        current = (shell('settings', 'get', 'secure', 'default_input_method').stdout or '').strip()
        if 'adbkeyboard' not in current:
            shell('ime', 'set', 'com.android.adbkeyboard/.AdbIME')
            time.sleep(0.8)
            switched = True
        encoded = base64.b64encode(value.encode('utf-8')).decode('ascii')
        out = shell('am', 'broadcast', '-a', 'ADB_INPUT_B64', '--es', 'msg', encoded, timeout=30)
        if 'Broadcast completed' not in ((out.stdout or '') + (out.stderr or '')):
            return {'ok': False, 'executed': True, 'code': 'IME_BROADCAST_FAILED',
                    'error': ((out.stdout or out.stderr or '')[:160])}
        method = 'adbkeyboard'
    invalidate_ui()
    with lock:
        state['last_action'] = time.time()
    return {'ok': True, 'executed': True, 'action': 'text', 'chars': len(value),
            'method': method, 'ime_switched': switched}


def key(body):
    code = str(body.get('key', ''))
    mapping = {'back': '4', 'home': '3', 'enter': '66', 'recents': '187', 'menu': '82',
               'up': '19', 'down': '20', 'left': '21', 'right': '22', 'del': '67',
               'wake': '224', 'sleep': '223', 'volume_up': '24', 'volume_down': '25'}
    code = mapping.get(code, code)
    if not re.fullmatch(r'\d{1,4}', code) and not re.fullmatch(r'KEYCODE_[A-Z_]{1,20}', code):
        raise ValueError('按键无效')
    wake()
    shell('input', 'keyevent', code)
    invalidate_ui()
    with lock:
        state['last_action'] = time.time()
    return {'ok': True, 'executed': True, 'action': 'key', 'key': code}


def apps():
    if not serial():
        return {'ok': False, 'code': 'DEVICE_OFFLINE', 'executed': False, 'error': '手机未连接'}
    out = shell('pm', 'list', 'packages', '-3')
    packages = [line.split(':', 1)[1].strip() for line in (out.stdout or '').splitlines() if ':' in line]
    return {'ok': True, 'executed': False, 'count': len(packages), 'packages': packages[:200]}


def start(body):
    package = str(body.get('package', ''))
    if not re.fullmatch(r'[a-zA-Z0-9._-]{3,80}', package):
        raise ValueError('包名无效')
    wake()
    out = shell('monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER', '1')
    if 'No activities found' in (out.stdout or '') or 'aborted' in (out.stdout or '').lower():
        return {'ok': False, 'executed': True, 'code': 'LAUNCH_FAILED',
                'error': (out.stdout or out.stderr or '')[:200]}
    invalidate_ui()
    with lock:
        state['last_action'] = time.time()
    return {'ok': True, 'executed': True, 'action': 'start', 'package': package}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = token()
        if not expected:
            return False
        header = self.headers.get('Authorization', '')
        return header.startswith('Bearer ') and hmac.compare_digest(header[7:].strip(), expected)

    def do_GET(self):
        if not self.authorized():
            return self.send_json({'error': 'token rejected'}, 403)
        if self.path == '/health':
            return self.send_json(health())
        if self.path == '/apps':
            return self.send_json(apps())
        if self.path == '/ui':
            return self.send_json(ui())
        return self.send_json({'error': 'not found'}, 404)

    def do_POST(self):
        if not self.authorized():
            return self.send_json({'error': 'token rejected'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length)) if 0 < length <= 65536 else {}
        except (ValueError, TypeError):
            return self.send_json({'error': '请求体无效'}, 400)
        try:
            if self.path == '/shot':
                image, failure = shot()
                if failure:
                    return self.send_json(failure, 503)
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(image)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(image)
                return None
            if self.path == '/tap':
                return self.send_json(tap(body))
            if self.path == '/swipe':
                return self.send_json(swipe(body))
            if self.path == '/text':
                return self.send_json(text(body))
            if self.path == '/key':
                return self.send_json(key(body))
            if self.path == '/start':
                return self.send_json(start(body))
            return self.send_json({'error': 'not found'}, 404)
        except ValueError as error:
            return self.send_json({'error': str(error), 'code': 'INVALID_ARGUMENT', 'executed': False}, 400)
        except subprocess.TimeoutExpired:
            return self.send_json({'error': 'adb 超时，动作可能已发出', 'code': 'ADB_TIMEOUT',
                                   'executed': True, 'uncertain': True}, 503)
        except Exception as error:  # noqa: BLE001 动作接口不把异常甩给调用方
            return self.send_json({'error': str(error), 'code': 'ANDROID_FAILED', 'executed': True}, 503)


if __name__ == '__main__':
    threading.Thread(target=idle_loop, daemon=True).start()
    print('her-android listening on %s:%s' % (BIND, PORT), flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
