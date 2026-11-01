"""Read QQ's OS notification routing metadata; never authorize by display name.

Uses the documented Android notification shell diagnostics, not app databases,
tokens, accessibility permissions, or a model-supplied sender field.
"""
import hashlib
import json
import re
import shlex
import time
from urllib.parse import urlsplit, parse_qs

PACKAGE = 'com.tencent.mobileqq'
KEY = re.compile(r'0\|com\.tencent\.mobileqq\|([0-9]+)\|null\|([0-9]+)')
PEER = re.compile(r'u_[A-Za-z0-9_-]{10,64}')


def parse_routes(raw):
    # Intent URI fields are OS-generated and URI escaped, unlike notification
    # bodies. The section and component must both belong to QQ itself.
    raw = raw.replace('\r', '')
    section = re.search(r'^  \* com\.tencent\.mobileqq:.*?(?=^  \* |\Z)', raw, re.M | re.S)
    if not section:
        return []
    routes = []
    for block in re.split(r'(?=^    #[0-9]+: PendingIntentRecord)', section[0], flags=re.M):
        if 'packageName=com.tencent.mobileqq ' not in block:
            continue
        lines = re.findall(r'^      requestIntent=(.*)$', block, re.M)
        if len(lines) != 1:
            continue
        line = lines[0]
        if not line.startswith('act=com.tencent.mobileqq.notification.action.reply '):
            continue
        if 'cmp=com.tencent.mobileqq/com.tencent.qqnt.notification.logic.NotificationBroadcastReceiver' not in line:
            continue
        found = re.findall(r'(?:^| )dat=(reply:/[^ ]+)(?: |$)', line)
        if len(found) != 1:
            continue
        uri = urlsplit(found[0])
        parts = uri.path.split('/')
        if len(parts) != 3 or parts[1] != '1' or not PEER.fullmatch(parts[2]):
            continue  # no group/channel/system sender may be promoted
        try:
            values = parse_qs(uri.query, strict_parsing=True)
            when = values['key_notification_time']
            notification_id = values['notification_reply_notify_id']
            if len(when) != 1 or len(notification_id) != 1:
                continue
            routes.append({'peer_uid': parts[2], 'when': int(when[0]),
                           'notification_id': int(notification_id[0])})
        except (KeyError, ValueError):
            continue
    return routes


def parse_notification(key, raw, routes, *, allow_unbound=False):
    match = KEY.fullmatch(key)
    if not match:
        return None  # skip summaries and other applications
    raw = raw.replace('\r', '')
    if len(raw) > 65536 or not raw.startswith('NotificationRecord('):
        return None
    # Read identity/time only BEFORE tickerText, the first attacker-controlled
    # free-text field. A forged diagnostic block inside chat text cannot alter it.
    header = raw.split('    tickerText=', 1)[0]
    expected = f'pkg={PACKAGE} user=UserHandle{{0}} id={match[1]} tag=null '
    if expected not in header.split('\n', 1)[0]:
        return None
    if re.findall(r'^  key=(.*)$', header, re.M) != [key]:
        return None
    uid = re.findall(r'^  uid=([0-9]+) userId=0$', header, re.M)
    when = re.findall(r'^    when=([0-9]+)/[0-9]+$', header, re.M)
    if uid != [match[2]] or len(when) != 1:
        return None
    when = int(when[0])
    peers = {r['peer_uid'] for r in routes
             if r['when'] == when and r['notification_id'] == int(match[1])}
    if len(peers) > 1 or (not peers and not allow_unbound):
        return None
    tokens = re.findall(r'^    contentIntent=PendingIntent\{[^\n]*PendingIntentRecord\{([0-9a-f]+) com\.tencent\.mobileqq startActivity(?: |\})', header, re.M)
    # Count even malformed/multiline occurrences: otherwise an injected second
    # line inside a multiline value could masquerade as the sole valid value.
    if raw.count('android.text=') != 1:
        return None
    texts = re.findall(r'^        android.text=String \(([^\r\n]*)\)$', raw, re.M)
    if len(texts) != 1 or len(texts[0]) > 12000:
        return None  # ambiguous/multiline/truncated diagnostics fail closed
    titles = re.findall(r'^        android.title=String \(([^\r\n]*)\)$', raw, re.M)
    title = titles[0] if len(titles)==1 and raw.count('android.title=')==1 else None
    return {'peer_uid': peers.pop() if peers else None, 'when': when, 'notification_key': key,
            'title': title, 'content_intent_record': tokens[0] if len(tokens)==1 else None,
            'package_uid': int(match[2]), 'package': PACKAGE,
            'chat_type': 'private', 'direction': 'incoming',
            'message_type': 'plain_text', 'text': texts[0],
            'notification_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()}


def process_generation(adb):
    import phone
    pid = phone._read(adb, 'shell', 'pidof', PACKAGE).strip()
    if not pid.isdigit():
        raise RuntimeError('QQ_PROCESS_UNAVAILABLE')
    stat = phone._read(adb, 'shell', 'cat', '/proc/'+pid+'/stat')
    start = stat[stat.rfind(')')+2:].split()[19]
    boot = phone._read(adb, 'shell', 'cat', '/proc/sys/kernel/random/boot_id').strip()
    return boot+':'+pid+':'+start


def read_notifications(root):
    import phone
    adb, data = phone._environment(root)
    if (data / 'PHONE_MAINTENANCE_HOLD.json').exists():
        raise RuntimeError('PHONE_MAINTENANCE_HOLD')
    # Read-only calls do not navigate the phone or steal another task's focus.
    phone._probe(adb)
    generation = process_generation(adb)
    keys = phone._read(adb, 'shell', 'cmd', 'notification', 'list').splitlines()
    keys = [k.strip() for k in keys if KEY.fullmatch(k.strip())]
    if len(keys) > 32:
        raise RuntimeError('Too many QQ notifications; bounded read refused')
    rows = {}
    for key in keys:
        raw = phone._adb(adb, 'shell', 'cmd', 'notification', 'get', shlex.quote(key)).decode('utf-8', 'strict')
        rows[key] = raw
    routes_raw = phone._adb(adb, 'shell', 'dumpsys', 'activity', 'intents').decode('utf-8', 'strict')
    routes = parse_routes(routes_raw)
    results = []
    for key, raw in rows.items():
        row = parse_notification(key, raw, routes, allow_unbound=True)
        if row:
            row['process_generation'] = generation
            results.append(row)
    if generation != process_generation(adb):
        raise RuntimeError('QQ_PROCESS_CHANGED_DURING_READ')
    return results


def authenticate(row, cfg, pin):
    # QQ's real special-concern decoration is required, but never sufficient:
    # the OS conversation token must also match the previously paired sender.
    if not isinstance(row.get('title'), str) or not re.fullmatch(
            r'\[特别关心\]爹(?:\([1-9][0-9]*条新消息\))?', row['title']):
        return None
    if row.get('peer_uid') is not None:
        return 'current-qq-private-route' if row['peer_uid']==cfg['peer_uid'] else None
    keys = ('notification_key', 'content_intent_record', 'process_generation')
    if (pin.get('peer_uid')==cfg['peer_uid'] and pin.get('owner_uin')==cfg['owner_uin']
            and all(row.get(k) and row.get(k)==pin.get(k) for k in keys)):
        return 'paired-qq-conversation-and-sole-special-concern'
    return None


def collect(store, cfg):
    path = store.root / 'data/qq-owner-notification-route.json'
    try:
        pin = json.loads(path.read_text('utf-8-sig'))
    except FileNotFoundError:
        pin = {}
    for row in read_notifications(store.root):
        # Pairing pins an immutable QQ route identity to the owner's verified
        # numeric account. Nicknames and the text of messages are not consulted.
        method = authenticate(row, cfg, pin)
        if not method:
            continue
        if row['when'] <= cfg['activated_after_ms']:
            continue
        if row['when'] > int(time.time() * 1000) + 60000:
            continue
        if method=='current-qq-private-route' and row.get('content_intent_record') and row.get('process_generation'):
            pin = {k:row[k] for k in ('peer_uid','notification_key','content_intent_record','process_generation')}
            pin['owner_uin'] = cfg['owner_uin']
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(pin,ensure_ascii=False,indent=2),encoding='utf8')
            temp.replace(path)
        text = row['text']
        proof = {**{k:v for k,v in row.items() if k!='text'}, 'peer_uid':cfg['peer_uid'],
                 'owner_uin': cfg['owner_uin'], 'self_uin': cfg['self_uin'],
                 'verified': True, 'owner_input_changed': False,
                 'method': method}
        yield proof, text
