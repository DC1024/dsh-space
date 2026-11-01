"""Loopback-only JSON, SSE and export endpoints for the formal observer."""
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

from thinking_observer import ThinkingBindingError
from thinking_store import ThinkingCapacityError


def request_fields(handler):
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True, max_num_fields=8)
    if any(len(values) != 1 for values in query.values()):
        raise ValueError('重复的查询字段')
    sid = query.get('sessionId', [None])[0]
    if not sid:
        raise ValueError('必须明确提供已登记的正式sessionId')
    after_text = query.get('after', ['0'])[0]
    if not re.fullmatch(r'\d{1,18}', after_text):
        raise ValueError('after必须是非负整数')
    limit_text = query.get('limit', ['200'])[0]
    if not re.fullmatch(r'\d{1,4}', limit_text) or not 1 <= int(limit_text) <= 1000:
        raise ValueError('limit应在1到1000之间')
    return sid, int(after_text), int(limit_text), 'after' in query


def _json_text(value):
    # Escaping preserves native text exactly, including Unicode code units.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'))


def _event(handler, event, value, event_id=None):
    text = (('id: ' + event_id + '\n') if event_id else '') + 'event: ' + event + '\n'
    text += 'data: ' + _json_text(value) + '\n\n'
    handler.wfile.write(text.encode('utf-8'))
    handler.wfile.flush()


def handle_formal_thinking(handler, observer):
    """Return True if this is an explicit formal API request, else False."""
    path = urlsplit(handler.path).path
    try:
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True, max_num_fields=8)
    except ValueError:
        handler.reply(400, {'ok': False, 'code': 'THINKING_REQUEST_INVALID', 'error': '正式观察请求参数无效'})
        return True
    if path == '/api/thinking' and 'sessionId' not in query:
        return False  # The legacy phone page retains its existing endpoint.
    if path not in ['/api/thinking', '/api/thinking/status', '/api/thinking/stream', '/api/thinking/export']:
        return False
    try:
        sid, after, limit, explicit_after = request_fields(handler)
        observer.require_session(sid)
        if path == '/api/thinking/status':
            handler.reply(200, observer.status(sid))
            return True
        if path == '/api/thinking':
            handler.reply(200, observer.page(sid, after=after, limit=limit))
            return True
        if path == '/api/thinking/export':
            lines = iter(observer.export(sid))
            first = next(lines, None)  # Detect archive failure before HTTP headers.
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.send_header('Content-Disposition', 'attachment; filename="thinking-' + sid + '.jsonl"')
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.close_connection = True
            try:
                if first is not None:
                    handler.wfile.write((first.rstrip('\n') + '\n').encode('utf-8'))
                for line in lines:
                    handler.wfile.write((line.rstrip('\n') + '\n').encode('utf-8'))
                handler.wfile.flush()
            except Exception:
                # Headers are committed; close the stream, never send a second
                # HTTP response or leak archive/dependency exception text.
                handler.close_connection = True
            return True
        info = observer.store.info(sid)
        last_id = handler.headers.get('Last-Event-ID')
        source_mismatch = False
        if last_id:
            if re.fullmatch(r'\d{1,18}', last_id):
                header_after = int(last_id)
            else:
                parts = last_id.rsplit(':', 2)
                if len(parts) != 3 or parts[1] != sid or not re.fullmatch(r'\d{1,18}', parts[2]):
                    raise ValueError('Last-Event-ID不属于该正式会话')
                source_mismatch = parts[0] != info['sourceInstanceId']
                header_after = int(parts[2])
            if explicit_after and after != header_after:
                raise ValueError('after与Last-Event-ID不一致')
            after = header_after
        handler.protocol_version = 'HTTP/1.1'
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store, no-transform')
        handler.send_header('X-Accel-Buffering', 'no')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('X-DSH-Source-Instance', info['sourceInstanceId'])
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        try:
            handler.wfile.write(b'retry: 2000\n: observation-only\n\n')
            handler.wfile.flush()
            if source_mismatch:
                _event(handler, 'gap', {'ok': False, 'sessionId': sid, 'sourceInstanceId': info['sourceInstanceId'],
                    'reason': 'source-instance-changed', 'requestedAfter': after,
                    'oldestAvailableSeq': observer.page(sid, limit=1).get('oldestAvailableSeq'),
                    'latestSeq': info.get('latestSeq', 0), 'requiresResync': True})
                return True
            began = time.monotonic()
            ping_at = began
            lifetime = getattr(handler.server, 'thinking_stream_lifetime', 300)
            poll = getattr(handler.server, 'thinking_stream_poll', .2)
            while time.monotonic() - began < lifetime:
                # A registry or identity change must not silently select another SID.
                observer.require_session(sid)
                page = observer.page(sid, after=after, limit=min(limit, 200))
                if page.get('gap'):
                    _event(handler, 'gap', {'sessionId': sid, 'requestedAfter': after,
                        'reason': page.get('gapReason'), 'ranges': page.get('gapRanges', []),
                        'oldestAvailableSeq': page.get('oldestAvailableSeq'), 'latestSeq': page.get('latestSeq'),
                        'sourceInstanceId': page.get('sourceInstanceId'), 'requiresResync': True})
                    break
                for entry in page.get('events', []):
                    _event(handler, 'reasoning', entry, entry['id'])
                    after = entry['seq']
                if page.get('hasMore'):
                    continue
                if time.monotonic() - ping_at >= 10:
                    handler.wfile.write(b': keepalive\n\n')
                    handler.wfile.flush()
                    ping_at = time.monotonic()
                time.sleep(poll)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass
        except Exception:
            try:
                _event(handler, 'status', {'ok': False, 'sessionId': sid,
                    'sourceInstanceId': info['sourceInstanceId'], 'requiresPause': True,
                    'code': 'FORMAL_STREAM_UNAVAILABLE'})
            except OSError:
                pass
        return True
    except ThinkingBindingError as error:
        handler.reply(409, {'ok': False, 'code': error.code, 'error': str(error), 'requiresPause': True})
    except ThinkingCapacityError:
        handler.reply(507, {'ok': False, 'code': 'ARCHIVE_CAPACITY_REACHED', 'requiresPause': True,
                            'error': '正式档案容量已到，保留历史并暂停后续活动'})
    except (ValueError, OverflowError):
        handler.reply(400, {'ok': False, 'code': 'THINKING_REQUEST_INVALID', 'error': '正式观察请求参数无效'})
    return True
