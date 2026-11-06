"""她的安卓手机：固定动作 API（看屏、点、滑、打字、按键、开应用）。

只调用 `.204` 上的 her-android 服务；不提供任意 adb shell 透传。
失败语义与桌面一致：未发出的动作可安全失败，已发出无回执的标 uncertain 且不自动重试。
"""
import json
import socket
import urllib.error
import urllib.request

from space_tools import spec

BASE = 'http://192.168.43.204:19992'
TOKEN_FILE = 'data/android.token'
TIMEOUT = 45
TEXT = {'type': 'string'}
COORD = {'type': 'integer', 'minimum': 0, 'maximum': 5000}

TOOLS = [
    spec('android_view', '查看她手机的状态、当前画面（保存为 /api/phone-shot）和界面上的文字按钮；只读，会先唤醒屏幕。只想查型号/安卓版本时用 android_info 更快；只想要界面文字时用 android_ui。', {}),
    spec('android_info', '读取她手机的型号、安卓版本、屏幕尺寸、电量和亮屏状态；只读、不截图、不操作界面，最快。', {}),
    spec('android_ui', '读取她手机当前界面的文字和控件位置（不截图）；适合读标题、按钮和定位，界面一直在动画时可能读不到。', {}),
    spec('android_tap', '在她手机上点按：给 x/y 坐标，或给 text 按界面文字定位（按文字会先读一次界面，约 2 秒）。', {'x': COORD, 'y': COORD, 'text': TEXT}),
    spec('android_swipe', '在她手机上滑动。', {'x1': COORD, 'y1': COORD, 'x2': COORD, 'y2': COORD,
                                             'ms': {'type': 'integer', 'minimum': 50, 'maximum': 3000}},
         ['x1', 'y1', 'x2', 'y2']),
    spec('android_type', '在她手机上输入 ASCII 文字（中文需要先装 ADBKeyboard）。',
         {'text': {'type': 'string', 'maxLength': 200}}, ['text']),
    spec('android_key', '按她手机的按键：back / home / enter / recents / up / down / left / right / del。',
         {'key': TEXT}, ['key']),
    spec('android_app', '列出她手机已装应用（action=list），或打开某个应用（action=start + package）。',
         {'action': {'type': 'string', 'enum': ['list', 'start']}, 'package': TEXT}, ['action']),
]


class AndroidError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain
        self.result = {'uncertain': uncertain}


def _token(root):
    return (root / TOKEN_FILE).read_text(encoding='ascii').strip()


def _request(store, endpoint, payload=None, *, mutating=False, raw=False):
    headers = {'Authorization': 'Bearer ' + _token(store.root)}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(BASE + endpoint, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get('error') or '她的手机拒绝了这次操作'
        except ValueError:
            message = '她的手机接口错误（HTTP ' + str(error.code) + '）'
        raise AndroidError(str(message), False) from None
    except TimeoutError:
        raise AndroidError('她的手机操作超时；结果待核对，不自动重试', mutating) from None
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror, TimeoutError)):
            raise AndroidError('她的手机暂不可用，操作未发出', False) from None
        raise AndroidError('她的手机连接失败，操作未发出', False) from None
    except OSError:
        raise AndroidError('她的手机暂不可用，操作未发出', False) from None
    if raw:
        return body
    try:
        value = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise AndroidError('她的手机回执无法解析；结果待核对', True) from None
    if value.get('ok') is not True:
        raise AndroidError(str(value.get('error') or '她的手机未能完成'),
                           bool(value.get('uncertain')) or bool(value.get('executed')) and mutating)
    return value


def health(store):
    """Read-only phone status from the her-android service."""
    value = _request(store, '/health')
    value.setdefault('ready', value.get('ok') is True)
    return value


def invoke(store, name, args):
    if name == 'android_view':
        phone = _request(store, '/health')
        screenshot = {}
        try:
            image = _request(store, '/shot', {}, raw=True)
            target = store.root / 'data' / 'phone-shot.png'
            target.write_bytes(image)
            screenshot = {'url': '/api/phone-shot', 'bytes': len(image)}
        except AndroidError as error:
            screenshot = {'error': str(error)}
        screen_items = None
        try:
            screen_items = _request(store, '/ui').get('items', [])[:30]
        except AndroidError:
            pass
        return {'phone': phone, 'screenshot': screenshot, 'screen_items': screen_items}
    if name == 'android_info':
        return _request(store, '/health')
    if name == 'android_ui':
        return _request(store, '/ui')
    if name == 'android_tap':
        payload = {key: args[key] for key in ('x', 'y', 'text') if key in args}
        if not payload:
            raise ValueError('需要坐标或文字')
        return _request(store, '/tap', payload, mutating=True)
    if name == 'android_swipe':
        return _request(store, '/swipe', {key: args[key] for key in ('x1', 'y1', 'x2', 'y2', 'ms') if key in args},
                        mutating=True)
    if name == 'android_type':
        return _request(store, '/text', {'text': args.get('text', '')}, mutating=True)
    if name == 'android_key':
        return _request(store, '/key', {'key': args.get('key', '')}, mutating=True)
    if name == 'android_app':
        action = args.get('action')
        if action == 'list':
            return _request(store, '/apps')
        if action == 'start':
            if not args.get('package'):
                raise ValueError('缺少 package')
            return _request(store, '/start', {'package': args['package']}, mutating=True)
        raise ValueError('action 只能是 list 或 start')
    raise ValueError('未知的手机工具')
