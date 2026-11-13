"""时段汇总与邮件汇报。

只读本空间的账本与事件，不派发任务、不碰设备。每个自主时段结束后，把这段时间
发生的任务、事件、记忆与费用汇总成一封邮件发给主人。配置在 data/schedule.json。
"""
import datetime as dt
import json
import re
import smtplib
import ssl
import threading
import time as _time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from zoneinfo import ZoneInfo

FILE = 'schedule.json'
MAX_RUNS = 30
CATCH_UP_SECONDS = 900
SKIP_KINDS = {'reasoning.delta', 'reply.delta', 'turn.start', 'turn.end', 'tool.start', 'tool.end'}
# 汇报只讲有内容的事；心跳、开关、记账这类流水留给活动记录页，不占邮件篇幅。
QUIET_KINDS = SKIP_KINDS | {
    'console.start', 'control.pause', 'control.resume', 'control.hold', 'control.correct',
    'budget.settled', 'schedule.report', 'schedule.mail_test', 'desktop.connected', 'desktop.disconnected',
    'context.archived', 'context.continued', 'task.accepted', 'task.dispatch', 'persona.switched',
    'parallel.changed', 'reasoning.request', 'reasoning.selected', 'budget.cancelled',
}

KIND_LABEL = {
    'console.start': '控制台启动', 'owner.message': '主人留言', 'memory.saved': '记忆已保存',
    'task.result': '本轮结果', 'task.failure': '本轮停止原因', 'task.accepted': '消息已接收',
    'task.dispatch': '已派发给她', 'task.reconciled': '任务已核对', 'task.reclassified': '停止原因已补注', 'device.stage': '设备回执',
    'persona.switched': '人格已切换', 'control.pause': '活动已暂停', 'control.resume': '已记录继续',
    'control.hold': '本轮已停止', 'control.correct': '纠正已保存', 'runtime.error': '运行时出错',
    'budget.settled': '费用已记账', 'budget.bound_violation': '费用待核对', 'budget.cancelled': '费用预留已撤销',
    'desktop.connected': '桌面已连接', 'desktop.disconnected': '桌面已断开',
    'context.archived': '本轮记录已归档', 'context.continued': '新上下文已就绪',
    'storage.quota_applied': '空间额度已更新', 'storage.expansion_requested': '扩容提案已保存',
    'phone.failed': '手机动作未完成', 'parallel.changed': '并发模式已变更',
    'schedule.report': '时段汇报', 'schedule.mail_test': '测试邮件',
    'reasoning.request': '开始思考', 'reasoning.selected': '思考模型已选定',
}
TASK_STATUS = {
    'completed': '完成', 'stopped': '停止', 'failed': '失败', 'running': '进行中',
    'queued': '排队', 'claimed': '已领取', 'uncertain': '待核对', 'needs-review': '待审阅', 'reviewed': '已复核',
}


def _moment(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


def _clock(value, tz):
    stamp = _moment(value)
    return stamp.astimezone(tz).strftime('%H:%M') if stamp else ''


AUTONOMY_PREFIX = '现在电脑符合主人设置的空闲条件'
# 没有实际内容就不值得占一行的类型。
NEED_BRIEF = {'device.stage', 'task.result', 'task.failure', 'memory.saved', 'owner.message'}


def _clean(text, limit=0):
    """去掉模型回复里的 markdown 记号，压成一行，邮件里读得顺。"""
    value = '' if text is None else str(text)
    value = re.sub(r'(?m)^\s*#+\s*', '', value)
    value = value.replace('**', '').replace('`', '')
    value = re.sub(r'\s+', ' ', value).strip()
    if limit and len(value) > limit:
        return value[:limit].rstrip() + '…'
    return value


def _task_title(text):
    value = _clean(text)
    if value.startswith(AUTONOMY_PREFIX):
        return '自主探索'
    return _clean(value, 240)


def _payload(event):
    try:
        return json.loads(event['payload'])
    except (KeyError, TypeError, ValueError):
        return {}


def _brief(payload):
    if not isinstance(payload, dict):
        return ''
    failure = payload.get('failure') or {}
    if isinstance(failure, dict) and failure.get('message'):
        return str(failure['message']).strip().replace('\n', ' ')[:400]
    for key in ('text', 'message', 'decision', 'reason', 'filename', 'summary', 'note'):
        if payload.get(key):
            return str(payload[key]).strip().replace('\n', ' ')[:400]
    return ''


def collect(store, start, end):
    """取 [start, end) 里的事实；时间边界统一换成 UTC 与库里的 ISO 字符串比较。"""
    tz = ZoneInfo(store.config['timezone'])
    begin = start.astimezone(dt.timezone.utc).isoformat()
    finish = end.astimezone(dt.timezone.utc).isoformat()
    with store.lock:
        events = [dict(row) for row in store.db.execute(
            'SELECT seq,time,kind,task_id,persona,payload FROM events WHERE time>=? AND time<=? ORDER BY seq',
            (begin, finish))]
        tasks = [dict(row) for row in store.db.execute(
            'SELECT id,text,status,time,target,persona FROM tasks WHERE time>=? AND time<=? ORDER BY time',
            (begin, finish))]
        # 任务结果可能落在时段边界之后（例如 12:00 整点收尾），按任务再补一次，
        # 否则汇总只能看到“停止”，看不到归宿。
        if tasks:
            placeholders = ','.join('?' * len(tasks))
            seen = {event['seq'] for event in events}
            for row in store.db.execute(
                    'SELECT seq,time,kind,task_id,persona,payload FROM events WHERE task_id IN (%s) '
                    "AND kind IN ('task.result','task.failure','control.hold') ORDER BY seq"
                    % placeholders, [task['id'] for task in tasks]):
                if row['seq'] not in seen:
                    events.append(dict(row))
            events.sort(key=lambda item: item['seq'])
        memories = [dict(row) for row in store.db.execute(
            'SELECT kind,text,time,persona FROM memory WHERE time>=? AND time<=? ORDER BY time',
            (begin, finish))]
        cost = store.db.execute('SELECT COALESCE(SUM(cny),0) FROM costs WHERE time>=? AND time<=?',
                                (begin, finish)).fetchone()[0] or 0.0
    counts = {}
    for task in tasks:
        label = TASK_STATUS.get(task['status'], task['status'])
        counts[label] = counts.get(label, 0) + 1
    ordered = ['完成', '停止', '失败', '待审阅', '待核对', '进行中', '排队', '已领取']
    parts = [f'{key} {counts[key]}' for key in ordered if key in counts]
    parts += [f'{key} {value}' for key, value in counts.items() if key not in ordered]
    task_text = '、'.join(parts) or '没有新任务'
    highlights = []
    for event in events:
        if event['kind'] in QUIET_KINDS or event['kind'] in ('task.result', 'task.failure'):
            continue
        try:
            payload = json.loads(event['payload'])
        except ValueError:
            payload = {}
        label = KIND_LABEL.get(event['kind'], event['kind'])
        brief = _brief(payload)
        if event['kind'] == 'memory.saved' and not brief and payload.get('items'):
            brief = str(payload['items'][0].get('text', ''))[:400]
        brief = _clean(brief, 400)
        if event['kind'] == 'owner.message' and brief.startswith(AUTONOMY_PREFIX):
            continue
        if not brief and event['kind'] in NEED_BRIEF:
            continue
        if (highlights and not brief and highlights[-1]['label'] == label
                and highlights[-1]['persona'] == event['persona']):
            continue
        highlights.append({'time': _clock(event['time'], tz), 'label': label,
                           'persona': event['persona'], 'brief': brief})
    return {'start': start, 'end': end, 'tz': tz, 'events': events, 'tasks': tasks,
            'task_counts': counts, 'task_text': task_text, 'memories': memories, 'cost': float(cost),
            'highlights': highlights[-8:]}


def _past_lines(facts):
    """过去这个时段做了什么：先列完成，再按原因汇总未完成。"""
    tz = facts['tz']
    results = {}
    holds = {}
    for event in facts['events']:
        payload = _payload(event)
        if event['kind'] in ('task.result', 'task.failure'):
            results[event['task_id']] = payload
        elif event['kind'] == 'control.hold':
            holds[event['task_id']] = payload

    lines = []
    completed = [item for item in facts['tasks'] if item['status'] == 'completed']
    for task in completed[:6]:
        detail = _clean((results.get(task['id']) or {}).get('text') or '', 160)
        line = f"· {_clock(task['time'], tz)}　完成：{_task_title(task.get('text'))}"
        if detail:
            line += '　—— ' + detail
        lines.append(line)
    if len(completed) > 6:
        lines.append(f"· 另有 {len(completed) - 6} 个任务完成，未逐条列出。")

    # 没有完成态任务时，任务列表只剩停止原因，看不出实际做了什么；
    # 这里从记忆和结果摘要里补一段“留下的实际记录”。
    if not completed:
        actions = []
        seen = set()

        def add(when, text, label, fallback):
            value = _clean(text, 240)
            key = value[:36]
            if not value or key in seen:
                return
            seen.add(key)
            stamp = _moment(when)
            actions.append((stamp.timestamp() if stamp else fallback, when, label, value))

        for item in facts['memories']:
            add(item['time'], item['text'], '记忆', 0.0)
        for task in facts['tasks']:
            payload = results.get(task['id']) or {}
            text = str(payload.get('text') or payload.get('summary') or '')
            if '主动压缩收尾' in text or '原目标' in text:
                continue
            add(task['time'], text, '结果', 0.0)
        actions.sort(key=lambda entry: entry[0])
        if not actions:
            # 连记忆和结果都没有时，才退回事件摘要；跳过压缩、运行时这类过程噪音。
            for item in facts['highlights']:
                if item['label'] in ('本轮结果', '本轮停止原因', '上下文压缩', '运行时出错'):
                    continue
                add(item['time'], item['brief'], item['label'], 1e12)
        if actions:
            lines.append('· 没有完成态任务；以下是期间留下的实际记录：')
            for _stamp, when, label, value in actions[:5]:
                clock = _clock(when, tz) or str(when or '')
                lines.append('  · ' + (clock + '　' if clock else '')
                             + (label + '：' if label else '') + value)
        else:
            lines.append('· 这一时段没有完成态任务，也没有留下可读的工作记录。')

    groups = {}
    for task in facts['tasks']:
        if task['status'] not in ('failed', 'stopped', 'needs-review', 'uncertain'):
            continue
        payload = results.get(task['id']) or {}
        failure = payload.get('failure') or {}
        code = str(failure.get('code') or payload.get('reason') or '')
        hold = holds.get(task['id']) or {}
        source = str(hold.get('source') or '')
        if code == 'CANCELLED' or source == 'owner-correction-or-pause':
            key = '被暂停/取消（设备结果不确定或主人暂停）'
        elif code == 'OBSERVATION_UNAVAILABLE' or source == 'observation-unavailable':
            key = '思考观察链路不可用'
        elif code == 'CONTEXT_ROTATED' or source == 'context-rotation':
            key = '上下文压缩收尾'
        elif code == 'RUNTIME_STOPPED':
            key = '运行时停止'
        elif code == 'AUTONOMY_WINDOW_ENDED' or source == 'autonomy-window-ended':
            key = '自主时段结束（正常收尾）'
        elif code == 'SUPERSEDED_BY_RESTART':
            key = '重启时已过期（正常收尾）'
        elif task['status'] == 'needs-review':
            key = '待审阅'
        elif task['status'] == 'uncertain':
            key = '待核对'
        else:
            key = '其他原因停止'
        groups.setdefault(key, []).append(task)

    for key, items in sorted(groups.items(), key=lambda pair: -len(pair[1])):
        lines.append(f"· {key}：{len(items)} 次（{_clock(items[0]['time'], tz)}–"
                     f"{_clock(items[-1]['time'], tz)}）")

    if not lines:
        for item in facts['highlights'][-6:]:
            suffix = ('：' + item['brief']) if item['brief'] else ''
            lines.append(f"· {item['time']}　{item['label']}{suffix}")
    if not lines:
        lines.append('· 这段时间没有任务或可记录的动作。')
    return lines


def _now_lines(store, tz):
    """现在正在做什么：活动开关、手上的任务、最近一次动作。"""
    with store.lock:
        running = [dict(row) for row in store.db.execute(
            "SELECT text,status,time FROM tasks WHERE status IN ('running','queued','claimed') "
            "ORDER BY time DESC LIMIT 5")]
        last = store.db.execute('SELECT time,kind,payload FROM events ORDER BY seq DESC LIMIT 1').fetchone()
    lines = ['· 当前' + ('已暂停，不会接手新任务' if store.get('paused') else '已唤醒，可以接手任务')
             + '；' + ('自主探索开放' if store.config.get('autonomy_enabled') else '自主探索关闭')]
    if running:
        for item in running:
            label = TASK_STATUS.get(item['status'], item['status'])
            lines.append(f"· {label}：{_clean(item['text'], 240)}（{_clock(item['time'], tz)} 开始）")
    elif last is not None:
        lines.append(f"· 手上没有正在执行的任务；最近一次动作是 {_clock(last['time'], tz)} 的"
                     + KIND_LABEL.get(last['kind'], last['kind']))
    else:
        lines.append('· 手上没有正在执行的任务。')
    return lines


def _next_lines(store, at):
    """下一个时段准备做什么：下一个窗口、待处理事项、到点后的安排。"""
    lines = []
    upcoming = _next_window(store, at)
    if upcoming:
        start, end, segment = upcoming
        lines.append(f"· 下一个时段：{start.strftime('%m-%d %H:%M')} – {end.strftime('%m-%d %H:%M')}"
                     f"（{segment['start']}–{segment['end']}）")
    else:
        lines.append('· 定时任务里还没有可用的时段')
    since = (at - dt.timedelta(hours=24)).astimezone(dt.timezone.utc).isoformat()
    with store.lock:
        unfinished = [dict(row) for row in store.db.execute(
            "SELECT text,status,time FROM tasks WHERE status IN ('needs-review','uncertain') "
            "AND time>=? ORDER BY time DESC LIMIT 5", (since,))]
        halted = store.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('stopped','failed') AND time>=?",
            (since,)).fetchone()[0]
    if unfinished:
        lines.append('· 待你决定（不会自动重发）：' + '、'.join(
            f"{TASK_STATUS.get(item['status'], item['status'])}「{_clean(item['text'], 120)}」"
            for item in unfinished[:3]))
    else:
        lines.append('· 没有等你决定的任务')
    if halted:
        lines.append(f'· 过去 24 小时另有 {halted} 条中途停止，需要重新安排才会继续')
    lines.append('· 到点后按设定自主探索；不扩大权限，不碰你的文件与设备')
    return lines


def _task_line(item):
    text = (item.get('text') or '').strip().replace('\n', ' ')
    return f"{text[:60]}（{TASK_STATUS.get(item['status'], item['status'])}）" if text else ''


def _window(segment, at):
    """把 HH:MM 时段落到具体日期上；结束时间早于开始时间时按跨夜处理。"""
    hour, minute = _hhmm(segment['end'])
    end = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    hour, minute = _hhmm(segment['start'])
    start = end.replace(hour=hour, minute=minute)
    if start > end:
        start -= dt.timedelta(days=1)
    return start, end


def _next_window(store, at):
    """返回 (start, end, segment)：按当前时间算出最近一个还没开始的时段。"""
    reports = getattr(store, 'reports', None)
    segments = (reports.task().get('segments') if reports else None) or [
        {'start': '06:00', 'end': '12:00'}, {'start': '15:00', 'end': '18:00'},
        {'start': '21:00', 'end': '04:00'}]
    best = None
    for segment in segments:
        start, end = _window(segment, at)
        if end <= at:
            start += dt.timedelta(days=1)
            end += dt.timedelta(days=1)
        if best is None or end < best[1]:
            best = (start, end, segment)
    return best


def compose(store, segment, start, end, *, manual=False):
    facts = collect(store, start, end)
    tz = facts['tz']
    total = len(facts['events'])
    task_text = facts['task_text']
    memory_text = f"{len(facts['memories'])} 条"
    pause_events = [item for item in facts['events'] if item['kind'] in ('control.pause', 'control.hold')]
    pending = [item for item in facts['events'] if item['kind'] == 'budget.bound_violation']

    head = '手动汇总' if manual else f"{segment['start']}–{segment['end']} 时段汇总"
    subject = f"[小鲸鱼] {head}（{end.strftime('%m-%d')}）"
    window = f"{start.strftime('%m-%d %H:%M')} – {end.strftime('%m-%d %H:%M')}"
    past = _past_lines(facts)
    now = _now_lines(store, tz)
    ahead = _next_lines(store, end)

    lines = [f'时段：{window}（{store.config["timezone"]}）', '',
             '【过去这个时段做了什么】'] + past
    lines.append(f'　合计：{task_text}；新增记忆 {memory_text}；费用 ¥{facts["cost"]:.4f}；'
                 f'记录 {total} 条事件')
    if pause_events:
        lines.append(f'　其中暂停或中止 {len(pause_events)} 次')
    if pending:
        lines.append(f'　待核对费用 {len(pending)} 条，需要你确认')
    lines += ['', '【现在正在做什么】'] + now
    lines += ['', '【下一个时段准备做什么】'] + ahead
    if facts['memories']:
        lines += ['', '新增记忆：']
        for item in facts['memories'][-4:]:
            lines.append('· ' + _clean(item['text'], 400))
    lines += ['', '这封邮件只汇总本空间记录，不包含设备截图，也不会触发新任务。']

    memory_html = _block('新增记忆', [_clean(item['text'], 400)
                                      for item in facts['memories'][-4:]]) if facts['memories'] else ''
    html = f"""<!doctype html><html><body style="margin:0;padding:20px;background:#f5f6f8;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#20242b">
<div style="max-width:660px;margin:0 auto;background:#fff;border-radius:12px;padding:22px 24px">
<p style="margin:0 0 4px;font-size:12px;letter-spacing:.14em;color:#8a919c">DSH PERSONAL SPACE</p>
<h2 style="margin:0 0 10px;font-size:19px">小鲸鱼 · {_html(head)}</h2>
<p style="margin:0 0 6px;color:#5a6270;font-size:13px">{_html(window)} · {_html(store.config['timezone'])}</p>
{_block('过去这个时段做了什么', past)}
{_block('现在正在做什么', now)}
{_block('下一个时段准备做什么', ahead)}
<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:16px;border-top:1px solid #eceff3">
<tr><td style="padding:8px 0;color:#6b7280;width:112px">合计</td><td style="padding:8px 0">{_html(task_text)}</td></tr>
<tr><td style="padding:8px 0;color:#6b7280">费用 / 记忆</td><td style="padding:8px 0">¥{facts['cost']:.4f} · 新增 {_html(memory_text)}</td></tr>
<tr><td style="padding:8px 0;color:#6b7280">事件</td><td style="padding:8px 0">{total} 条 · 暂停或中止 {len(pause_events)} 次</td></tr>
</table>
{memory_html}
<p style="margin:18px 0 0;color:#8a919c;font-size:12px">只汇总本空间记录，不含设备截图，也不会触发新任务。</p>
</div></body></html>"""
    return subject, '\n'.join(lines), html


def _block(title, items):
    body = ''.join(f'<li style="margin:5px 0;line-height:1.65">{_html(str(x).lstrip("· ").strip())}</li>'
                   for x in items)
    return ('<h3 style="margin:18px 0 6px;font-size:14px">' + _html(title) + '</h3>'
            '<ul style="margin:0;padding-left:18px;font-size:13px;color:#333a45">' + body + '</ul>')


def _html(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def deliver(mail, subject, text, html):
    if not (mail.get('host') and mail.get('user') and mail.get('password') and mail.get('to')):
        raise RuntimeError('邮箱还没配置完整：需要 SMTP 服务器、账号、授权码和收件地址')
    message = MIMEText(html, 'html', 'utf-8')
    message['Subject'] = Header(subject, 'utf-8')
    message['From'] = formataddr((mail.get('from_name') or '小鲸鱼', mail['user']))
    message['To'] = mail['to']
    message['Date'] = formatdate(localtime=True)
    context = ssl.create_default_context()
    host, port = mail['host'], int(mail['port'])
    if mail.get('ssl', True):
        with smtplib.SMTP_SSL(host, port, timeout=25, context=context) as server:
            server.login(mail['user'], mail['password'])
            server.sendmail(mail['user'], [mail['to']], message.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=25) as server:
            server.starttls(context=context)
            server.login(mail['user'], mail['password'])
            server.sendmail(mail['user'], [mail['to']], message.as_string())


class Reports:
    def __init__(self, store):
        self.store = store
        self.path = store.data / FILE
        self.lock = threading.RLock()
        self.thread = None
        self.config = self._load()
        if not self.path.exists():
            self._save()

    # ---------- 配置 ----------
    def _defaults(self):
        ranges = ((self.store.config.get('autonomy_window') or {}).get('ranges') or [])
        segments = [{'start': r['start'], 'end': r['end']} for r in ranges
                    if isinstance(r, dict) and r.get('start') and r.get('end')]
        return segments or [{'start': '06:00', 'end': '12:00'},
                            {'start': '15:00', 'end': '18:00'},
                            {'start': '21:00', 'end': '04:00'}]

    def _load(self):
        empty_mail = {'host': '', 'port': 465, 'ssl': True, 'user': '', 'password': '',
                      'to': '', 'from_name': '小鲸鱼'}
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}
        mail = {**empty_mail, **(raw.get('mail') or {})}
        tasks = [t for t in (raw.get('tasks') or []) if isinstance(t, dict)]
        if not tasks:
            tasks = [{'id': 'segment-summary', 'name': '时段汇总', 'enabled': True,
                      'segments': self._defaults(), 'last_fired': {}}]
        return {'mail': mail, 'tasks': tasks, 'runs': raw.get('runs') or []}

    def _save(self):
        payload = json.dumps(self.config, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(FILE + '.tmp')
        with tmp.open('w', encoding='utf-8') as handle:
            handle.write(payload)
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def task(self):
        return self.config['tasks'][0]

    def payload(self):
        with self.lock:
            mail = dict(self.config['mail'])
            mail['password_set'] = bool(mail.pop('password', ''))
            task = dict(self.task())
            task.setdefault('segments', self._defaults())
            return {'timezone': self.store.config['timezone'], 'mail': mail,
                    'task': task, 'runs': self.config['runs'][:MAX_RUNS],
                    'default_segments': self._defaults()}

    def save(self, body):
        mail = body.get('mail') or {}
        target = self.config['mail']
        host = str(mail.get('host', target['host']) or '').strip()
        if host and not (1 <= len(host) <= 200):
            raise ValueError('SMTP 服务器地址无效')
        port = int(mail.get('port', target['port']) or 465)
        if not 1 <= port <= 65535:
            raise ValueError('端口需在 1-65535 之间')
        user = str(mail.get('user', target['user']) or '').strip()
        to = str(mail.get('to', target['to']) or '').strip()
        for value in (user, to):
            if value and '@' not in value:
                raise ValueError('邮箱地址缺少 @')
        with self.lock:
            target.update({'host': host, 'port': port,
                           'ssl': bool(mail.get('ssl', target.get('ssl', True))),
                           'user': user, 'to': to,
                           'from_name': str(mail.get('from_name', target.get('from_name')) or '小鲸鱼')[:40]})
            if mail.get('password'):
                target['password'] = str(mail['password']).strip()
            task = body.get('task')
            if isinstance(task, dict):
                current = self.task()
                if 'enabled' in task:
                    current['enabled'] = bool(task['enabled'])
                segments = task.get('segments')
                if isinstance(segments, list) and segments:
                    clean = []
                    for item in segments:
                        if not isinstance(item, dict):
                            continue
                        start, end = str(item.get('start', '')), str(item.get('end', ''))
                        if not (_hhmm(start) and _hhmm(end)):
                            raise ValueError('时段格式应为 HH:MM')
                        clean.append({'start': start, 'end': end})
                    if not clean:
                        raise ValueError('至少保留一个时段')
                    current['segments'] = clean
            self._save()
        return self.payload()

    # ---------- 汇报 ----------
    def _window(self, segment, at):
        return _window(segment, at)

    def _record(self, task, segment, end, ok, error, subject, mark=True):
        label = f"{segment['start']}–{segment['end']}"
        with self.lock:
            if mark:
                fired = task.setdefault('last_fired', {})
                fired[f"{task['id']}:{end.isoformat()}"] = dt.datetime.now(dt.timezone.utc).isoformat()
                for key in sorted(fired, key=fired.get)[:-12]:
                    fired.pop(key, None)
            self.config['runs'] = ([{'time': dt.datetime.now(dt.timezone.utc).isoformat(),
                                     'task': task.get('name', '时段汇总'), 'segment': label,
                                     'ok': ok, 'error': error, 'subject': subject}]
                                   + self.config['runs'])[:MAX_RUNS]
            self._save()
        with self.store.identity.bind('biao'):
            self.store.event('schedule.report', {'segment': label, 'ok': ok, 'error': error,
                                                 'subject': subject})

    def fire(self, segment, start, end, *, task=None, manual=False, mark=True):
        task = task or self.task()
        subject, text, html = compose(self.store, segment, start, end, manual=manual)
        ok, error = False, ''
        try:
            deliver(self.config['mail'], subject, text, html)
            ok = True
        except Exception as problem:  # 网络、认证、配置缺失都要如实落到记录里
            error = str(problem)
        self._record(task, segment, end, ok, error, subject, mark=mark)
        return {'ok': ok, 'error': error, 'subject': subject}

    def send_test(self):
        subject = f"[小鲸鱼] 测试邮件（{dt.datetime.now(ZoneInfo(self.store.config['timezone'])).strftime('%m-%d %H:%M')}）"
        text = '这是一封测试邮件。收到它说明时段汇总的邮箱配置可用。'
        html = ('<div style="font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;padding:18px">'
                '<h3>小鲸鱼 · 测试邮件</h3><p>收到这封邮件说明时段汇总的邮箱配置可用。</p></div>')
        try:
            deliver(self.config['mail'], subject, text, html)
        except Exception as problem:
            with self.store.identity.bind('biao'):
                self.store.event('schedule.mail_test', {'ok': False, 'error': str(problem)})
            return {'ok': False, 'error': str(problem)}
        with self.store.identity.bind('biao'):
            self.store.event('schedule.mail_test', {'ok': True, 'error': '', 'subject': subject})
        return {'ok': True, 'error': '', 'subject': subject}

    def run_now(self):
        tz = ZoneInfo(self.store.config['timezone'])
        end = dt.datetime.now(tz)
        return self.fire({'start': (end - dt.timedelta(hours=12)).strftime('%H:%M'),
                          'end': end.strftime('%H:%M')}, end - dt.timedelta(hours=12), end,
                         manual=True, mark=False)

    def run_segment(self, segment):
        """手动重发一个已结束时段的汇总（不记为自动触发）。"""
        start, end = _hhmm(segment['start']), _hhmm(segment['end'])
        if start is None or end is None:
            raise ValueError('时段格式应为 HH:MM')
        at = dt.datetime.now(ZoneInfo(self.store.config['timezone']))
        window_start, window_end = _window(segment, at)
        if window_end > at:
            raise ValueError('该时段还没结束，不能提前汇总')
        return self.fire({'start': f'{start[0]:02d}:{start[1]:02d}',
                          'end': f'{end[0]:02d}:{end[1]:02d}'},
                         window_start, window_end, manual=True, mark=False)

    def tick(self):
        tz = ZoneInfo(self.store.config['timezone'])
        at = dt.datetime.now(tz)
        due = []
        with self.lock:
            for task in self.config['tasks']:
                if not task.get('enabled'):
                    continue
                for segment in (task.get('segments') or self._defaults()):
                    start, end = self._window(segment, at)
                    if 0 <= (at - end).total_seconds() <= CATCH_UP_SECONDS:
                        key = f"{task['id']}:{end.isoformat()}"
                        if key not in (task.get('last_fired') or {}):
                            due.append((task, segment, start, end))
        for task, segment, start, end in due:
            self.fire(segment, start, end, task=task)

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._loop, name='schedule-reports', daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:
                pass
            _time.sleep(30)


def _hhmm(value):
    try:
        hour, minute = str(value).split(':')
        hour, minute = int(hour), int(minute)
    except (ValueError, TypeError):
        return None
    if 0 <= hour < 24 and 0 <= minute < 60:
        return hour, minute
    return None


def start(store):
    store.reports = Reports(store)
    store.reports.start()
    return store.reports
