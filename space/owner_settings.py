from install_paths import node_executable
"""主人设置面板的唯一后端：权限分档 / API 接口 / 人设提示词。

只被控制台 space.py 的 handler 调用，且已经过 owner 会话守卫、Host/Origin 校验
与 CSRF 令牌；本模块自身不读取任何凭据明文，也不扩大模型权限。

写入范围只有三处：
- data/owner-settings.json（权限栏位与接口备注，原子替换并留 .bak）
- config.json 的 autonomy_enabled（自主探索开关，已接线）
- 人设快照包 + CURRENT_xiaojingyu.json + config.json 的 persona_* 固定哈希，
  一次事务写入，随后调用 prepare_runtime.mjs 重新生成运行时补丁；任一步失败
  就整体回滚，不留下“哈希对不上”的半成品。

人设提示词改动在专属运行时（19992）重启后才载入，这里用
data/persona-pending-restart.json 记录保存时的监听进程号，重启后自动消解。
"""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

SETTINGS_FILE = 'data/owner-settings.json'
MODEL_FILE = 'data/model-settings.json'
PENDING_FILE = 'data/persona-pending-restart.json'
PERMISSION_PENDING_FILE = 'data/permission-pending.json'
ARCHIVE_DIR = 'data/persona-archive'
NODE = node_executable()

# 权限栏位：id / 分组 / 名称 / 说明 / 规则出处 / 形态 / 是否可改 / 是否已接线
RESTRICTIONS = [
    {'id': 'device_phone', 'group': '设备', 'kind': 'switch', 'editable': True, 'wired': False,
     'label': '使用她的 Android 模拟器（emulator-5562）',
     'desc': '观察屏幕、读界面、点击、输入、按键与滑动，只在她自己的模拟器里。这个开关只记录偏好，当前没有执行路径读它；实际能否操作由手机侧独占状态与权限档决定。',
     'source': '权限约束.md', 'default': True},
    {'id': 'device_desktop', 'group': '设备', 'kind': 'switch', 'editable': True, 'wired': False,
     'label': '使用专属 Windows 虚拟机（DSH-Home-v01）',
     'desc': '观察、点击、输入、按键与滚动；故障时报错，不改用主人的宿主桌面。这个开关只记录偏好，当前没有执行路径读它。',
     'source': '权限约束.md', 'default': True},
    {'id': 'device_exclusive', 'group': '设备', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '手机与电脑全局互斥',
     'desc': '同一时间只能用一台设备；两端任务按顺序执行，不自动关闭任一设备。',
     'source': '权限约束.md', 'value': '强制互斥（不可关闭）'},
    {'id': 'device_lease', 'group': '设备', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '设备操作串行与操作锁',
     'desc': '表、里与后台观察共用操作锁；收到当前动作完整回执后才可切换。',
     'source': '权限约束.md', 'value': '顺序执行'},

    {'id': 'autonomy', 'group': '自主与执行', 'kind': 'switch', 'editable': True, 'wired': True,
     'label': '自主探索开关', 'desc': '关闭时只执行主人提交的新任务，每轮最多 30 分钟；打开时闲置 10 分钟自主开工，不限时长。两种模式均不限总步数；同一行动连续执行 30 次后，阻止第 31 次并停止本轮。',
     'source': '权限约束.md', 'config_key': 'autonomy_enabled'},
    {'id': 'dispatch', 'group': '自主与执行', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '任务派发（控制台任务可执行）',
     'desc': '由空间启动流程与暂停状态共同决定；从这里改不会生效，避免绕过启动流程。',
     'source': '权限约束.md', 'value': '看运行状态'},
    {'id': 'pause', 'group': '自主与执行', 'kind': 'state', 'editable': True, 'wired': True,
     'label': '当前暂停状态',
     'desc': '暂停立即让当前授权失效，未执行的旧队列保留待核对；恢复只接受新任务。',
     'source': '权限约束.md'},
    {'id': 'parallel', 'group': '自主与执行', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '表里并发',
     'desc': '并发由页头的人格栏切换（需要二次确认），不在这里重复一个开关。',
     'source': '权限约束.md'},

    {'id': 'publish', 'group': '对外动作与不可逆', 'kind': 'switch', 'editable': True, 'wired': False,
     'label': '公开发布',
     'desc': '发布到公开平台或对不特定人可见的内容。这个开关只记录偏好，未接线；实际是否拦下由当前权限档与自动审查决定。',
     'source': '权限约束.md', 'default': False, 'risk': True},
    {'id': 'outbound_message', 'group': '对外动作与不可逆', 'kind': 'switch', 'editable': True, 'wired': False,
     'label': '对他人发送消息',
     'desc': '以任何身份对第三方发送消息或评论。这个开关只记录偏好，未接线；实际是否拦下由当前权限档与自动审查决定。',
     'source': '权限约束.md', 'default': False, 'risk': True},
    {'id': 'purchase', 'group': '对外动作与不可逆', 'kind': 'switch', 'editable': True, 'wired': False,
     'label': '购买与账户登录',
     'desc': '涉及金钱、账号登录或授权第三方服务的动作。这个开关只记录偏好，未接线；实际是否拦下由当前权限档与自动审查决定。',
     'source': '权限约束.md', 'default': False, 'risk': True},
    {'id': 'delete_irreversible', 'group': '对外动作与不可逆', 'kind': 'switch', 'editable': True, 'wired': False,
     'label': '删除不可恢复资料',
     'desc': '清空、覆盖或删除后无法还原的资料。这个开关只记录偏好，未接线；实际是否拦下由当前权限档与自动审查决定。',
     'source': '权限约束.md', 'default': False, 'risk': True},

    {'id': 'host_files', 'group': '宿主与凭据', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '读取宿主任意文件',
     'desc': '模型侧有文件工具；能碰到哪些路径由权限档决定：只读/工作区写档只限工作区，完全权限档可读宿主文件。',
     'source': '权限约束.md', 'value': '按权限档'},
    {'id': 'host_terminal', 'group': '宿主与凭据', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '宿主终端',
     'desc': '模型侧有终端工具（pwsh/命令）；能碰到宿主还是只碰工作区由权限档决定，完全权限档不拦。',
     'source': '权限约束.md', 'value': '按权限档'},
    {'id': 'credentials', 'group': '宿主与凭据', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '读取密码凭据与私有数据库',
     'desc': '主人凭据只允许宿主用户、SYSTEM 与管理员读取，模型工具读不到。',
     'source': '权限约束.md', 'value': '不开放'},
    {'id': 'host_access_expand', 'group': '宿主与凭据', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '扩大宿主或设备访问范围',
     'desc': '由本安装使用者选择权限档位；实际设备需已连接并完成配置。',
     'source': '权限约束.md', 'value': '由所选档位决定'},
    {'id': 'qq_channel', 'group': '宿主与凭据', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': 'QQ 聊天通道',
     'desc': '当前空间没有接入 QQ 聊天通道。',
     'source': '权限约束.md', 'value': '未接入'},

    {'id': 'model', 'group': '模型与额度', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '模型', 'desc': '只使用 deepseek-official/deepseek-flash。',
     'source': '权限约束.md', 'value_from': 'model'},
    {'id': 'daily_budget', 'group': '模型与额度', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '每日额度与停止线',
     'desc': '表、里共用空间账本：先预留后结算，未知费用保留预留。',
     'source': '权限约束.md', 'value_from': 'budget'},
    {'id': 'storage_quota', 'group': '模型与额度', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '空间总额度与最低保留余量',
     'desc': '扩容只由主人确认具体申请；额度更新不等于已预留物理空间。',
     'source': '权限约束.md', 'value_from': 'storage'},
    {'id': 'observation_gate', 'group': '模型与额度', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '观察门（积压 5 秒停止）',
     'desc': '观察链路必须绑定当前会话，积压达到 5 秒即停止后续操作。',
     'source': '权限约束.md', 'value': '5 秒'},

    {'id': 'personas', 'group': '身份与审查', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '双人格（表 / 里）与工作区范围',
     'desc': '表可写 workdir/biao 与 dist 静态资产，里只写 workdir/li；双方只读对方工作区。',
     'source': 'persona_policy.py:25', 'value_from': 'persona_policy'},
    {'id': 'inner_persona', 'group': '身份与审查', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '内人格',
     'desc': '暂不启用；表、里二分只用于解释，不是新增运行身份。',
     'source': '权限约束.md', 'value': '未启用'},
    {'id': 'review', 'group': '身份与审查', 'kind': 'fact', 'editable': False, 'wired': True,
     'label': '作品事后复核',
     'desc': '事后核对：不阻塞、不撤销已发生的写入，也不改变暂停与自主开关。',
     'source': '权限约束.md', 'value': '事后核对'},
]

# 运行能力取自安装配置；设置页不能把旧存档误显示为当前执行档。
PRESETS = [
    {'id':'read-only','label':'仅可查看','available':True,'desc':'查看与分析，不修改文件或执行设备动作。'},
    {'id':'workspace-write','label':'工作区内修改','available':True,'desc':'在工作区内完成读写、测试和构建。'},
    {'id':'danger-full-access','label':'完全权限','available':True,'desc':'零行为限制：不分类、不询问、不拦截（自动审查插件仍加载，但对本档会话不生效；预算、暂停等运行保护仍在）。'},
    {'id':'auto','label':'自动审查','exp':True,'available':True,'desc':'默认放行，只在不可恢复删除外部数据、凭据外发、操作他人设备、不可逆付款时拦下；本地只读工具不消耗审查。'},
]

# config.json 里手写的 'full' 是本地别名，规范档名是内置的 danger-full-access。
PRESET_ALIAS = {'full': 'danger-full-access'}


def preset_id(value):
    """把配置里的档名规范成 PRESETS 里的 id；认不出就按工作区写处理（与运行时一致）。"""
    name = PRESET_ALIAS.get(value, value)
    return name if name in {row['id'] for row in PRESETS} else 'workspace-write'

# API 面板的端点清单：全部来自 space.py 的真实路由，前端只做展示与自测。
ENDPOINTS = [
    {'group': '主人会话', 'method': 'GET', 'path': '/api/status', 'auth': '主人 Cookie + CSRF 返回',
     'purpose': '状态总览（含 csrf、人格、预算、设备、存储）', 'test': True},
    {'group': '主人会话', 'method': 'GET', 'path': '/api/events', 'auth': '主人 Cookie',
     'purpose': '空间事件 SSE（按人格与 after 游标）', 'test': False},
    {'group': '主人会话', 'method': 'GET', 'path': '/api/memory', 'auth': '主人 Cookie',
     'purpose': '长期记忆检索（?q=关键词）', 'test': True},
    {'group': '主人会话', 'method': 'POST', 'path': '/api/memory', 'auth': '主人 Cookie + CSRF',
     'purpose': '新增一条记忆（带执行者与记录者）', 'test': False},
    {'group': '主人会话', 'method': 'GET', 'path': '/api/actions', 'auth': '主人 Cookie',
     'purpose': '共用行动回执', 'test': True},
    {'group': '主人会话', 'method': 'GET', 'path': '/api/task-result', 'auth': '主人 Cookie',
     'purpose': '单个任务的结果与失败原因（?id=）', 'test': False},
    {'group': '主人会话', 'method': 'GET', 'path': '/api/thinking/stream', 'auth': '主人 Cookie',
     'purpose': '实时思考 SSE（经 19993 观察服务同源透传）', 'test': False},
    {'group': '主人会话', 'method': 'GET', 'path': '/api/phone-frame', 'auth': '主人 Cookie',
     'purpose': '最近一张手机画面', 'test': True},
    {'group': '主人会话', 'method': 'GET', 'path': '/api/frame', 'auth': '主人 Cookie',
     'purpose': '最近一张电脑画面', 'test': True},
    {'group': '主人会话', 'method': 'GET', 'path': '/permissions', 'auth': '主人 Cookie',
     'purpose': '权限约束.md 全文（纯文本）', 'test': True},
    {'group': '控制', 'method': 'POST', 'path': '/api/control/pause', 'auth': '主人 Cookie + CSRF',
     'purpose': '暂停活动', 'test': False},
    {'group': '控制', 'method': 'POST', 'path': '/api/control/resume', 'auth': '主人 Cookie + CSRF',
     'purpose': '恢复（只接受新任务）', 'test': False},
    {'group': '控制', 'method': 'POST', 'path': '/api/control/persona', 'auth': '主人 Cookie + CSRF',
     'purpose': '切换表 / 里', 'test': False},
    {'group': '控制', 'method': 'POST', 'path': '/api/control/chat', 'auth': '主人 Cookie + CSRF',
     'purpose': '提交新任务（走预算预留与设备门槛）', 'test': False},
    {'group': '控制', 'method': 'POST', 'path': '/api/control/parallel', 'auth': '主人 Cookie + CSRF',
     'purpose': '表里并发（需二次确认）', 'test': False},
    {'group': '控制', 'method': 'POST', 'path': '/api/control/approval', 'auth': '主人 Cookie + CSRF',
     'purpose': '一次性批准', 'test': False},
    {'group': '模型侧', 'method': 'POST', 'path': '/mcp/biao', 'auth': 'Authorization: Bearer（桥凭据）',
     'purpose': '表人格的 MCP 工具入口（streamable HTTP）', 'test': False},
    {'group': '模型侧', 'method': 'POST', 'path': '/mcp/li', 'auth': 'Authorization: Bearer（桥凭据）',
     'purpose': '里人格的 MCP 工具入口', 'test': False},
    {'group': '模型侧', 'method': 'POST', 'path': '/bridge/*', 'auth': 'Authorization: Bearer（桥凭据）',
     'purpose': '设备与观察服务回报事件与动作', 'test': False},
    {'group': '主人设置（新增）', 'method': 'GET', 'path': '/api/owner/settings', 'auth': '主人 Cookie',
     'purpose': '本页数据：权限栏位、接口清单、人设摘要', 'test': True},
    {'group': '主人设置（新增）', 'method': 'POST', 'path': '/api/owner/settings', 'auth': '主人 Cookie + CSRF',
     'purpose': '保存权限栏位与接口备注', 'test': False},
    {'group': '主人设置（新增）', 'method': 'GET', 'path': '/api/owner/persona', 'auth': '主人 Cookie',
     'purpose': '读取人设提示词全文与版本', 'test': True},
    {'group': '主人设置（新增）', 'method': 'POST', 'path': '/api/owner/persona', 'auth': '主人 Cookie + CSRF',
     'purpose': '保存为新版本并重新生成运行时补丁', 'test': False},
]

LIVE_ONLY = ('device_phone', 'device_desktop')

# 已接线的开关：生效值在运行状态里（config.json），不在设置存档里。
# 若拿存档/default 覆盖显示，界面会一直显示关闭，拨上去也会被读回关闭。
LIVE_SWITCH = {'autonomy': 'autonomy_enabled'}

# Owner 2026-10-24: both autonomy settings have no total step cap.
# The existing off-mode task duration is unchanged; repeated actions stop at 30.
AUTONOMY_OFF_LIMITS = {'max_steps': 0, 'task_timeout_seconds': 1800}


def _apply_autonomy_limits(config, enabled):
    context = config.setdefault('context', {})
    if enabled:
        context['max_steps'] = 0
        config.pop('task_timeout_seconds', None)
    else:
        context['max_steps'] = AUTONOMY_OFF_LIMITS['max_steps']
        config['task_timeout_seconds'] = AUTONOMY_OFF_LIMITS['task_timeout_seconds']


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text('utf-8-sig'))
    except (OSError, ValueError):
        return default


def _write_json(path, value, backup=True):
    """原子替换；有旧文件时先留一份 .bak-<时间戳>。返回写入后的 sha256。"""
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    original = path.read_bytes() if path.exists() else None
    had_bom = bool(original and original.startswith(b'\xef\xbb\xbf'))
    data = (('\ufeff' if had_bom else '') + payload).encode('utf-8')
    if original is not None and backup:
        stamp = time.strftime('%Y%m%dT%H%M%S') + '-' + str(time.time_ns())
        path.with_name(path.name + '.bak-' + stamp).write_bytes(original)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return hashlib.sha256(data).hexdigest()


def _defaults():
    return {'version': 1,
            'permissions': {'preset': 'workspace-write', 'restrictions': {}},
            'api': {'notes': ''}}


def model_values(store):
    """Read model-service settings; the key file is 0600 and never sent to the UI.

    config.json carries the non-secret copy so prepare_runtime and the worker
    can read provider/model/base URL without touching the credential file.
    """
    saved = _read_json(store.root / MODEL_FILE, {}) or {}
    config = store.config or {}
    llm = config.get('llm') if isinstance(config.get('llm'), dict) else {}
    provider = saved.get('provider') or llm.get('provider') or 'deepseek-official'
    model = saved.get('model') or llm.get('model') or config.get('model') or 'deepseek-flash'
    effort = saved.get('reasoning_effort') or llm.get('reasoning_effort') or 'low'
    base_url = saved.get('base_url') or llm.get('base_url') or ''
    api_key = saved.get('api_key') if isinstance(saved.get('api_key'), str) else ''
    return {'provider': str(provider), 'model': str(model),
            'reasoning_effort': str(effort), 'base_url': str(base_url),
            'api_key': api_key, 'updated_at': saved.get('updated_at')}


def _patch_applied(store):
    """Whether the running runtime booted from the patch currently on disk."""
    patch = store.root / 'data' / 'runtime-home' / 'profiles' / 'space' / 'cordis.patch.yml'
    applied = _read_json(store.root / 'data' / 'runtime-applied.json', {}) or {}
    try:
        current = hashlib.sha256(patch.read_bytes()).hexdigest()
    except OSError:
        return False
    return bool(current) and applied.get('patch_sha256') == current


def model_public(store):
    value = model_values(store)
    key = value['api_key'] or os.environ.get('DEEPSEEK_API_KEY', '')
    hint = ('••••' + key[-4:]) if key else ''
    return {'provider': value['provider'], 'model': value['model'],
            'reasoning_effort': value['reasoning_effort'],
            'base_url': value['base_url'], 'api_key_set': bool(key),
            'api_key_hint': hint,
            'api_key_source': 'data/model-settings.json' if value['api_key'] else '部署 env',
            'updated_at': value['updated_at'],
            'restart_required': not _patch_applied(store)}


def save_model(store, values):
    """Validate and persist model-service settings without echoing the key."""
    current = model_values(store)
    provider = str(values.get('provider', current['provider'])).strip()
    model = str(values.get('model', current['model'])).strip()
    effort = str(values.get('reasoning_effort', current['reasoning_effort'])).strip()
    base_url = str(values.get('base_url', current['base_url'])).strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]{2,80}', provider):
        raise ValueError('服务提供方名称无效')
    if not re.fullmatch(r'[A-Za-z0-9._:/-]{1,160}', model):
        raise ValueError('模型名称无效')
    if effort not in ('low', 'high', 'max'):
        raise ValueError('推理档位无效')
    if base_url:
        if len(base_url) > 500 or not re.fullmatch(r'https?://[^\s/]+(?:/[^\s]*)?', base_url):
            raise ValueError('API 地址必须是 http(s) URL，且不能包含空格')
        if '@' in base_url.split('://', 1)[1].split('/', 1)[0]:
            raise ValueError('API 地址不能内嵌用户名或密码')
    raw_key = values.get('api_key')
    if raw_key is None or not str(raw_key).strip():
        api_key = current['api_key']  # 留空 = 不修改
    else:
        api_key = str(raw_key).strip()
        if len(api_key) > 4000 or any(ord(ch) < 32 for ch in api_key):
            raise ValueError('API Key 无效或过长')

    record = {'version': 1, 'provider': provider, 'model': model,
              'reasoning_effort': effort, 'base_url': base_url,
              'api_key': api_key, 'updated_at': _now()}
    path = store.root / MODEL_FILE
    old_model = path.read_bytes() if path.exists() else None
    old_config = (store.root / 'config.json').read_bytes()
    _write_json(path, record)
    os.chmod(path, 0o600)
    config = dict(store.config)
    llm = dict(config.get('llm') or {})
    llm.update({'provider': provider, 'model': model,
                'reasoning_effort': effort, 'base_url': base_url})
    config['llm'] = llm
    config['model'] = model
    _write_json(store.root / 'config.json', config)
    store.config = config
    try:
        publish = getattr(getattr(store, 'identity', None), 'publish_binding', None)
        if callable(publish):
            publish()
        patch_sha = _regenerate_patch(store)
    except Exception as exc:
        (store.root / 'config.json').write_bytes(old_config)
        store.config = json.loads(old_config.decode('utf-8-sig'))
        if old_model is None:
            try:
                path.unlink()
            except OSError:
                pass
        else:
            path.write_bytes(old_model)
        try:
            store.identity.publish_binding()
        except Exception:
            pass
        raise RuntimeError('大模型设置未生效，已回滚：' + str(exc)[:300]) from None
    store.event('model.settings.saved', {
        'provider': provider, 'model': model, 'reasoning_effort': effort,
        'base_url': base_url, 'api_key_set': bool(api_key),
        'patch_sha256': patch_sha,
        'message': '大模型服务设置已保存；重启专属运行时后生效'})
    return {'ok': True, 'changed': True, 'restart_required': True,
            'message': '已保存大模型服务设置；重启专属运行时后生效。',
            'model': model_public(store)}


def load(store):
    value = _read_json(store.root / SETTINGS_FILE)
    base = _defaults()
    if isinstance(value, dict):
        for section in ('permissions', 'api'):
            saved = value.get(section)
            if isinstance(saved, dict):
                merged = dict(base[section])
                merged.update(saved)
                base[section] = merged
        base['version'] = value.get('version', 1)
        base['updated_at'] = value.get('updated_at')
    restrictions = base['permissions'].setdefault('restrictions', {})
    for item in RESTRICTIONS:
        if item.get('kind') == 'switch' and item['editable'] and item['id'] not in restrictions:
            restrictions[item['id']] = bool(item.get('default', False))
    return base


def _listener_pid(port):
    """19992 上监听进程的 PID；拿不到就返回 None（不猜）。"""
    try:
        done = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                              timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if not done.stdout:
        return None
    # 中文 Windows 上 netstat 输出是 cp936 字节，别用 text=True 硬按 utf-8 解（会崩在读线程里）。
    out = done.stdout.decode('utf-8', 'replace')
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(':' + str(port)) and parts[3].upper() == 'LISTENING':
            if parts[4].isdigit():
                return int(parts[4])
    return None


def _load_pending(store):
    value = _read_json(store.root / PENDING_FILE)
    return value if isinstance(value, dict) else None


def _clear_pending(store):
    try:
        (store.root / PENDING_FILE).unlink()
    except OSError:
        pass


def pending_restart(store):
    """保存后是否还没载入：只有监听进程号变了才算重启过。"""
    flag = _load_pending(store)
    if not flag:
        return None
    current = _listener_pid(19992)
    if current is not None and current != flag.get('runtime_pid'):
        _clear_pending(store)
        return None
    return flag


def permission_pending(store):
    """档位改动后是否还没载入：只有 19992 的监听进程号变了才算重启过。

    与人格的 pending 分开记，免得换一次档就把「人格已载入」误显示成未载入。
    """
    value = _read_json(store.root / PERMISSION_PENDING_FILE)
    if not isinstance(value, dict):
        return None
    current = _listener_pid(19992)
    if current is not None and current != value.get('runtime_pid'):
        try:
            (store.root / PERMISSION_PENDING_FILE).unlink()
        except OSError:
            pass
        return None
    return value


def _regenerate_patch(store):
    """按 config.json 里的档位重算运行时补丁，返回新补丁 sha256。

    档位决定两件事：组合阶段写入的 sandbox+approval 启动对，以及是否挂审查插件
    （自动审查挂 runtime-auto-review.mjs；其余档位挂空壳 runtime-auto-preset-shim.mjs，
    只为让已存的 auto 会话还能被恢复）。两者都只在启动时读取，所以换档必须重算补丁。
    """
    patch_path = store.root / 'data' / 'runtime-home' / 'profiles' / 'space' / 'cordis.patch.yml'
    done = subprocess.run([NODE, str(store.root / 'prepare_runtime.mjs')], cwd=str(store.root),
                          capture_output=True, text=True, errors='replace', timeout=180,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if done.returncode != 0:
        raise RuntimeError('重新生成运行时补丁失败：' + (done.stderr or done.stdout or '')[-800:])
    try:
        return hashlib.sha256(patch_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError('运行时补丁没有生成：%s' % exc)


def _live(store):
    status = store.status()
    budget = status.get('budget') or {}
    storage = status.get('storage') or {}
    runtime = status.get('runtime') or {}
    return {'paused': bool(status.get('paused')),
            'autonomy_enabled': bool(status.get('autonomy_enabled')),
            'dispatch_enabled': bool(status.get('dispatch_enabled')),
            'parallel_enabled': bool(status.get('parallel_enabled')),
            'active_persona': status.get('active_persona'),
            'model': status.get('model'),
            'runtime': {'ready': runtime.get('ready'), 'chat_ready': runtime.get('chat_ready'),
                        'session_id': runtime.get('session_id'),
                        'persona_version': runtime.get('persona_version')},
            'budget': {'day': budget.get('day'), 'local_recorded_cny': budget.get('local_recorded_cny'),
                       'daily_budget_cny': budget.get('daily_budget_cny'),
                       'stop_at_cny': budget.get('stop_at_cny'),
                       'limit_enabled': budget.get('limit_enabled', True),
                       'blockers': status.get('blockers') or []},
            'storage': {'used_bytes': storage.get('used_bytes'), 'quota_gib': storage.get('quota_gib'),
                        'free_bytes': storage.get('free_bytes'),
                        'minimum_free_gib': storage.get('minimum_free_gib')},
            'phone': {'ready': (status.get('phone') or {}).get('ready'),
                      'message': (status.get('phone') or {}).get('message'),
                      'serial': (status.get('phone') or {}).get('serial')},
            'desktop': {'ready': (status.get('desktop') or {}).get('ready'),
                        'message': (status.get('desktop') or {}).get('message')}}


def _restrictions(store, saved, live):
    out = []
    for item in RESTRICTIONS:
        row = {key: item[key] for key in ('id', 'group', 'kind', 'label', 'desc', 'source', 'editable', 'wired')}
        ident = item['id']
        if ident in ('host_files', 'host_terminal'):
            enabled = preset_id(store.config.get('permission_preset'))
            row['value_text'] = '已配置普通任务工具；范围由当前档位决定' if enabled else '当前档位未开放'
            row['desc'] = '文件与命令用于完成已交办目标；普通读取、修改、测试和构建不逐步申请。实际目录以运行时为准。'
        elif ident == 'credentials':
            row['value_text'] = '私人数据按任务范围处理'
            row['desc'] = '不因具备文件工具而读取无关凭据或向外发送私人信息；本项目不向原作者回传数据。'
        elif ident == 'pause':
            row['value'] = bool(live['paused'])
            row['value_text'] = '已暂停' if live['paused'] else '运行中'
        elif ident == 'parallel':
            row['value_text'] = '已开启' if live['parallel_enabled'] else '未开启'
        elif ident == 'dispatch':
            row['value_text'] = '可执行' if (live['dispatch_enabled'] and not live['paused']) else '不可执行'
        elif ident == 'autonomy':
            row['value'] = bool(live['autonomy_enabled'])
            row['value_text'] = '已开启' if live['autonomy_enabled'] else '已关闭'
        elif item.get('value_from') == 'model':
            row['value_text'] = str(live['model'])
        elif item.get('value_from') == 'budget':
            budget = live['budget']
            if budget.get('limit_enabled', True):
                row['value_text'] = '日额度 %s 元 · 停止线 %s 元 · 今日已记 %s 元' % (
                    budget.get('daily_budget_cny'), budget.get('stop_at_cny'), budget.get('local_recorded_cny'))
            else:
                row['value_text'] = '无上限 · 今日已记 %s 元' % budget.get('local_recorded_cny')
        elif item.get('value_from') == 'storage':
            storage = live['storage']
            gib = (lambda value: '—' if value is None else round(value / 2 ** 30, 1))
            row['value_text'] = '总额度 %s GiB · 已用 %s GiB · 所在磁盘可用 %s GiB · 最低保留 %s GiB' % (
                storage.get('quota_gib'), gib(storage.get('used_bytes')), gib(storage.get('free_bytes')),
                storage.get('minimum_free_gib'))
        elif item.get('value_from') == 'persona_policy':
            import persona_policy
            described = persona_policy.describe()
            row['value_text'] = '表可写 %s；里可写 %s' % ('、'.join(described['biao']['write']),
                                                       '、'.join(described['li']['write']))
            row['detail'] = described['notes']
        elif 'value' in item:
            row['value_text'] = item['value']
        if row['kind'] == 'switch':
            row['default'] = bool(item.get('default', False))
            if ident not in LIVE_SWITCH:
                row['value'] = bool(saved['permissions']['restrictions'].get(ident, item.get('default', False)))
            # ident in LIVE_SWITCH 时 value 已由上面的实时状态给出，不再用存档覆盖
            if ident in LIVE_ONLY:
                row['note'] = '策略记录；实际连接仍由设备清单与操作锁控制。'
        row['risk'] = bool(item.get('risk'))
        out.append(row)
    return out


def settings_payload(store, csrf=''):
    live = _live(store)
    saved = load(store)
    persona = persona_summary(store)
    bridge_fingerprint = hashlib.sha256(store.bridge_key.encode('utf-8')).hexdigest()[:12]
    config = store.config
    pending = pending_restart(store)
    return {
        'ok': True,
        'generated_at': _now(),
        'csrf': csrf,
        'live': live,
        'permissions': {
            'preset': preset_id(config.get('permission_preset')),
            'presets': PRESETS,
            'restrictions': _restrictions(store, saved, live),
            'pending_restart': permission_pending(store),
            'updated_at': saved.get('updated_at'),
        },
        'api': {
            'endpoints': ENDPOINTS,
            'notes': saved['api'].get('notes', ''),
            'auth': {
                'owner_session': 'HttpOnly · SameSite=Strict Cookie（由启动入口用受限凭据建立）',
                'csrf': '写入需要 X-CSRF-Token，值见 /api/status 的 csrf 字段',
                'origin': '写入还要求与本机控制台同源',
                'bridge': '模型侧与设备侧用 Authorization: Bearer（桥凭据指纹 %s）' % bridge_fingerprint,
                'capability': '引导凭据只在启动入口一次性使用，不经过浏览器、不写入页面',
            },
            'server': {'base': 'http://127.0.0.1:%s' % config.get('port'),
                       'control_console': config.get('port'),
                       'runtime': 19992, 'observer': 19993,
                       'timezone': config.get('timezone')},
            'model': {'current': model_public(store)['model'],
                      'allowed': [model_public(store)['model']],
                      'provider': model_public(store)['provider'],
                      'base_url': model_public(store)['base_url']},
            'mcp': {'biao': '/mcp/biao', 'li': '/mcp/li',
                    'legacy': '旧 /mcp 只返回 400 提示，不提供服务'},
            'budget': live['budget'],
            'runtime': live['runtime'],
        },
        'model': model_public(store),
        'persona': persona,
        'pending_restart': pending,
        'defaults': _defaults(),
    }


def _validate_switch(ident):
    for item in RESTRICTIONS:
        if item['id'] == ident:
            return item
    return None


def save(store, body):
    section = body.get('section')
    values = body.get('values')
    if section not in ('permissions', 'api', 'model'):
        raise ValueError('只接受 permissions、api 或 model 三段')
    if not isinstance(values, dict):
        raise ValueError('缺少要保存的值')
    if section == 'model':
        return save_model(store, values)
    saved = load(store)
    applied, stored = [], []
    if section == 'permissions':
        # Validate all switches before changing the execution preset or any live value.
        incoming = values.get('restrictions')
        if incoming is not None:
            if not isinstance(incoming, dict):
                raise ValueError('restrictions 须为对象')
            for ident, raw in incoming.items():
                item = _validate_switch(ident)
                if item is None or item.get('kind') != 'switch' or not item.get('editable') or type(raw) is not bool:
                    raise ValueError('权限开关无效：' + str(ident))
        preset = values.get('preset')
        if preset is not None:
            preset = PRESET_ALIAS.get(preset, preset)
            if preset not in {row['id'] for row in PRESETS}:
                raise ValueError('未知权限档位')
            if preset != preset_id(store.config.get('permission_preset')):
                previous = preset_id(store.config.get('permission_preset'))
                patch_path = store.root / 'data' / 'runtime-home' / 'profiles' / 'space' / 'cordis.patch.yml'
                old_patch = patch_path.read_bytes() if patch_path.exists() else None
                old_config = (store.root / 'config.json').read_bytes()
                config = dict(store.config)
                config['permission_preset'] = preset
                _write_json(store.root / 'config.json', config)
                store.config = config
                try:
                    patch_sha = _regenerate_patch(store)
                except Exception:
                    # 补丁没生成就等于档位没换成：把 config 和旧补丁一起还原。
                    (store.root / 'config.json').write_bytes(old_config)
                    store.config = json.loads(old_config.decode('utf-8-sig'))
                    if old_patch is not None:
                        patch_path.write_bytes(old_patch)
                    raise
                _write_json(store.root / PERMISSION_PENDING_FILE,
                            {'preset': preset, 'previous_preset': previous,
                             'patch_sha256': patch_sha, 'saved_at': _now(),
                             'runtime_pid': _listener_pid(19992)})
                store.event('permission.preset.configured',
                            {'preset': preset, 'previous_preset': previous,
                             'patch_sha256': patch_sha,
                             'reviewer_mounted': preset == 'auto'})
                applied.append('permission_preset_configured')
                applied.append('runtime_patch_regenerated')
            saved['permissions']['preset'] = preset
            stored.append('preset')
        incoming = values.get('restrictions')
        if incoming is not None:
            if not isinstance(incoming, dict):
                raise ValueError('restrictions 须为对象')
            for ident, raw in incoming.items():
                item = _validate_switch(ident)
                if item is None or item.get('kind') != 'switch' or not item.get('editable'):
                    raise ValueError('这项权限栏位不可修改：' + str(ident))
                if not isinstance(raw, bool):
                    raise ValueError('开关值须为 true 或 false')
            # Validate the entire request before applying any live switch.
            for ident, raw in incoming.items():
                item = _validate_switch(ident)
                if item.get('wired') and item.get('config_key'):
                    config = store.config
                    if bool(config.get(item['config_key'])) != raw:
                        config[item['config_key']] = raw
                        if item['config_key'] == 'autonomy_enabled':
                            _apply_autonomy_limits(config, raw)
                        _write_json(store.root / 'config.json', config)
                        applied.append(item['config_key'])
                else:
                    saved['permissions']['restrictions'][ident] = raw
                    stored.append(ident)
    else:
        notes = values.get('notes')
        if notes is not None:
            if not isinstance(notes, str):
                raise ValueError('接口备注须为文本')
            saved['api']['notes'] = notes.strip()[:2000]
            stored.append('notes')
    saved['updated_at'] = _now()
    _write_json(store.root / SETTINGS_FILE, saved)
    permission_pending_now = permission_pending(store)
    message = ''
    if permission_pending_now:
        message = ('档位已写进 config.json 并重新生成了运行时补丁（%s）。'
                   '正在跑的 19992 还是旧档位：请先暂停、再重启专属运行时，之后才按新档位执行。'
                   % (preset_id(store.config.get('permission_preset'))))
    return {'ok': True, 'applied': applied, 'stored': stored,
            'restart_required': permission_pending_now is not None,
            'pending_restart': permission_pending_now, 'message': message,
            'settings': settings_payload(store, body.get('csrf', '') or '')}


def _patch_prefixes(store):
    """从磁盘 patch 里取表/里两份系统提示的 sha256；读不到就返回 None（不猜）。"""
    patch = store.root / 'data' / 'runtime-home' / 'profiles' / 'space' / 'cordis.patch.yml'
    try:
        text = patch.read_text('utf-8-sig')
    except OSError:
        return None
    found = {}
    for line in text.splitlines():
        marker = 'prefix:'
        if marker in line:
            value = line.split(marker, 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                raw = value[1:-1]
                try:
                    raw = (raw.replace('\\n', '\n').replace('\\r', '\r').replace('\\t', '\t')
                              .replace('\\"', '"').replace('\\\\', '\\'))
                except ValueError:
                    pass
                found[hashlib.sha256(raw.encode('utf-8')).hexdigest()] = len(raw)
    return found or None


def persona_summary(store):
    info = store.persona()
    pending = pending_restart(store)
    if not info.get('available'):
        return {'available': False, 'message': info.get('message', '人格入口不可用')}
    try:
        raw = Path(info['package']).read_bytes()
        pack = json.loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError, KeyError):
        return {'available': False, 'message': '人格快照无法解析'}
    personas = {}
    for label, item in (pack.get('personas') or {}).items():
        prompt = (item or {}).get('system_prompt') or ''
        personas[label] = {'chars': len(prompt),
                           'sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:12]}
    loaded = bool(info.get('loaded_into_runtime')) and not pending
    return {'available': True, 'version': info.get('version'),
            'active_version': info.get('active_version'),
            'loaded_into_runtime': loaded,
            'package_sha256': (info.get('package_sha256') or '')[:12],
            'pointer': info.get('pointer'), 'package': info.get('package'),
            'default': info.get('default'), 'inner_enabled': bool(info.get('inner_enabled')),
            'personas': personas, 'pending_restart': pending,
            'restart_required': bool(pending),
            'editable': True, 'min_chars': 200, 'max_chars': 12000}


def persona_payload(store):
    info = store.persona()
    if not info.get('available'):
        return {'ok': False, 'message': info.get('message', '人格入口不可用')}
    pack_path = Path(info['package'])
    raw = pack_path.read_bytes()
    pack = json.loads(raw.decode('utf-8-sig'))
    prompts = {}
    for label, item in (pack.get('personas') or {}).items():
        prompts[label] = (item or {}).get('system_prompt') or ''
    archive = store.root / ARCHIVE_DIR
    history = []
    try:
        for item in sorted(archive.glob('persona-*.json.bak-*'), key=lambda p: p.name, reverse=True)[:8]:
            history.append({'file': item.name, 'bytes': item.stat().st_size,
                            'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(item.stat().st_mtime))})
    except OSError:
        pass
    pending = pending_restart(store)
    prefixes = _patch_prefixes(store)
    loaded = bool(info.get('loaded_into_runtime')) and not pending
    if prefixes is not None:
        loaded = loaded and any(hashlib.sha256(text.encode('utf-8')).hexdigest() in prefixes
                                for text in prompts.values())
    return {'ok': True, 'version': info.get('version'), 'active_version': info.get('active_version'),
            'package': str(pack_path), 'pointer': info.get('pointer'),
            'package_sha256': info.get('package_sha256'),
            'personas': prompts, 'chars': {label: len(text) for label, text in prompts.items()},
            'loaded_into_runtime': loaded, 'patch_matches': prefixes is not None,
            'pending_restart': pending, 'restart_required': bool(pending),
            'editable': True, 'min_chars': 200, 'max_chars': 12000,
            'raw_bytes': len(raw), 'bom': raw.startswith(b'\xef\xbb\xbf'),
            'crlf': b'\r\n' in raw, 'note': '真源就是这份 JSON 的 system_prompt；'
            '保存会重算固定哈希并重新生成运行时补丁，但不改版本号，也不碰 CURRENT_xiaojingyu.json。',
            'history': history}


def save_persona(store, body):
    """原地改写人设正文：版本号、指针、文件名全部不动，只换字节与固定哈希。"""
    if body.get('action') == 'ack_restart':
        _clear_pending(store)
        return {'ok': True, 'restart_required': False, 'pending_restart': None,
                'persona': persona_summary(store)}
    edits = body.get('personas')
    if not isinstance(edits, dict) or not edits:
        raise ValueError('没有收到要保存的人设文本')
    info = store.persona()
    if not info.get('available'):
        raise ValueError(info.get('message', '人格入口不可用'))
    pack_path = Path(info['package'])
    raw = pack_path.read_bytes()
    old_sha = hashlib.sha256(raw).hexdigest()
    if body.get('expected_sha256') is not None and body['expected_sha256'] != old_sha:
        raise ValueError('人设在读取后已被修改，未覆盖他人的更新；请重新读取并核对草稿')
    if old_sha != (store.config['runtime'] or {}).get('persona_sha256'):
        raise ValueError('当前人格快照与固定哈希不一致（可能有人改过文件），未做任何修改；'
                         '请让宿主修好哈希对不上的一致性再回到这里')
    store.check_storage()
    pack = json.loads(raw.decode('utf-8-sig'))
    personas = pack.get('personas') or {}
    changed = []
    for label, text in edits.items():
        if label not in personas:
            raise ValueError('未知人格：' + str(label))
        if not isinstance(text, str):
            raise ValueError('人设文本须为字符串')
        text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
        if '\x00' in text:
            raise ValueError('人设文本含非法字符')
        if len(text) < 200:
            raise ValueError('「%s」人设太短（%d 字，至少 200 字），像是没填完整' % (label, len(text)))
        if len(text) > 12000:
            raise ValueError('「%s」人设 %d 字，超过 12000 字上限' % (label, len(text)))
        item = personas[label]
        if not isinstance(item, dict):
            raise ValueError('「%s」的人设结构异常，未做任何修改' % label)
        if text != item.get('system_prompt'):
            item['system_prompt'] = text
            changed.append(label)
    if not changed:
        raise ValueError('内容没有变化，未写入')
    new_bytes = json.dumps(pack, ensure_ascii=False, indent=2).encode('utf-8')
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    patch_path = store.root / 'data' / 'runtime-home' / 'profiles' / 'space' / 'cordis.patch.yml'
    archive = store.root / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%S') + '-' + str(time.time_ns())
    backups = {}
    old_patch = patch_path.read_bytes() if patch_path.exists() else None
    backup = archive / ('%s.bak-%s' % (pack_path.name, stamp))
    backup.write_bytes(raw)
    old_config = (store.root / 'config.json').read_bytes()
    runtime_pid = _listener_pid(19992)
    try:
        tmp = pack_path.with_name(pack_path.name + '.tmp')
        tmp.write_bytes(new_bytes)
        os.replace(tmp, pack_path)
        config = store.config
        config.setdefault('runtime', {})['persona_sha256'] = new_sha
        _write_json(store.root / 'config.json', config)
        prepare = subprocess.run([NODE, str(store.root / 'prepare_runtime.mjs')], cwd=str(store.root),
                                 capture_output=True, text=True, errors='replace', timeout=180,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if prepare.returncode != 0:
            raise RuntimeError('重新生成运行时补丁失败：' + (prepare.stderr or prepare.stdout or '')[-800:])
        if not patch_path.exists() or hashlib.sha256(patch_path.read_bytes()).hexdigest() == \
                (hashlib.sha256(old_patch).hexdigest() if old_patch else ''):
            raise RuntimeError('运行时补丁没有更新，已回滚')
    except Exception:
        try:
            pack_path.write_bytes(raw)
            (store.root / 'config.json').write_bytes(old_config)
            store.config = json.loads(old_config.decode('utf-8-sig'))
            if old_patch is not None:
                patch_path.write_bytes(old_patch)
        except OSError:
            pass
        try:
            backup.unlink()
        except OSError:
            pass
        raise
    _write_json(store.root / PENDING_FILE, {'version': info.get('version'), 'sha256': new_sha,
                                            'previous_sha256': old_sha, 'saved_at': _now(),
                                            'runtime_pid': runtime_pid, 'changed': changed,
                                            'archive': str(backup)})
    store.event('persona.prompt.saved', {'sha256': new_sha, 'previous_sha256': old_sha,
                                        'changed': changed, 'archive': backup.name,
                                        'runtime_pid': runtime_pid})
    chars = {label: len((personas[label] or {}).get('system_prompt') or '') for label in personas}
    return {'ok': True, 'version': info.get('version'), 'package': str(pack_path),
            'sha256': new_sha, 'previous_sha256': old_sha, 'changed': changed, 'chars': chars,
            'archive': backup.name, 'bytes': len(new_bytes),
            'prepare': {'ok': True, 'output': (prepare.stdout or '').strip()},
            'runtime_applied': False, 'restart_required': True,
            'persona': persona_summary(store),
            'message': '正文已写入 %s（%s），固定哈希与运行时补丁同步更新，旧版留档 %s。'
                       '运行的 19992 仍是旧提示词：请先暂停、再重启专属运行时，之后这里的「已载入」才会变真。'
                       % (pack_path.name, '/'.join(changed), backup.name)}
