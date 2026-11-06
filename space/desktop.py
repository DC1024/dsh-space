"""Hyper-V Direct relay. Never invokes arbitrary guest or host commands from model text."""
import base64
import json
import queue
import socket
import subprocess
import threading
import time
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

LOCK=threading.Lock()
TIMING_LOCK=threading.Lock()
TIMING_PHASES=frozenset(('host.request','host.vm_query','host.media_query',
    'host.vm_start','host.direct_connect','host.session_open','host.session_reused',
    'guest.enter','guest.agent_request','guest.agent_response','guest.screen_after',
    'guest.identity_policy','guest.applocker_policy','guest.firewall_policy',
    'guest.markers_acl','guest.complete','host.direct_complete','host.error'))
# 主人 2026-10-24 提速：空间进程里复用同一个 PowerShell 进程和 PowerShell Direct 会话
# （guest_bridge.ps1 -Serve）。命令行工具（guest_admin.py 等）仍按原样一次一进程。
PERSISTENT={'enabled':False}
_SERVE={'proc':None,'lines':None}
SERVE_HEADER='DSH_BRIDGE_SERVE '
# Linux deployments drive an isolated X11 container over LAN instead of Hyper-V.
REMOTE = os.name != 'nt'
REMOTE_BASE = os.environ.get('DSH_DESKTOP_BASE', 'http://192.168.43.204:19990')
REMOTE_TIMEOUT = float(os.environ.get('DSH_DESKTOP_TIMEOUT', '75'))


class _NotSent(Exception):
    """The persistent bridge never received the request; the one-shot path is safe."""


class _Broken(Exception):
    """The persistent bridge ended after the request was sent; the result is unknown."""


def bridge_stages(stderr):
    """Accept fixed timing labels only; never persist input, credentials or images."""
    if isinstance(stderr,bytes):
        stderr=stderr.decode('utf-8','replace')
    stages=[]
    for line in (stderr or '').splitlines():
        if not line.startswith('DSH_BRIDGE_TIMING '):continue
        try:
            item=json.loads(line[len('DSH_BRIDGE_TIMING '):])
            if item.get('phase') not in TIMING_PHASES:continue
            stage={'phase':item['phase']}
            for field in ('elapsedMs','guestElapsedMs'):
                value=item.get(field)
                if type(value) is int and value>=0:stage[field]=value
            stages.append(stage)
        except (ValueError,AttributeError,TypeError):continue
    return stages[-32:]


def _record_timing(root,record):
    # Diagnostic failure cannot change an acknowledged device result.
    try:
        folder=root/'data'
        if not folder.is_dir():return
        path=folder/'desktop-bridge-timing.jsonl'
        with TIMING_LOCK:
            if path.exists() and path.stat().st_size>262144:
                path.replace(folder/'desktop-bridge-timing.previous.jsonl')
            with path.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(record,ensure_ascii=True)+'\n')
    except OSError:pass

class DesktopError(RuntimeError):
    def __init__(self,message,uncertain=False):
        super().__init__(message)
        self.result={'ok':False,'error':message,'uncertain':uncertain,'retry_safe':not uncertain}


class DesktopPreviewDeferred(Exception):
    """Another device operation has priority; keep the previous frame/status."""


def _background_call(store,action):
    if action not in ('health','screen'):
        raise ValueError('Background preview is read-only')
    from device_lease import background_lease
    # Use the same global admission as model phone/desktop operations. Do not
    # queue a preview or retain the lease between health and the next screen.
    with background_lease(store,'desktop') as admitted:
        if not admitted:raise DesktopPreviewDeferred()
        return call(store.root,action)


def _serve_stop():
    proc=_SERVE['proc']
    _SERVE.update(proc=None,lines=None)
    if proc is not None:
        try:proc.kill()
        except OSError:pass


def _serve_call(root,text,env,timeout):
    """One request through the persistent bridge; same receipt shape as subprocess.run."""
    proc=_SERVE['proc']
    if proc is None or proc.poll() is not None:
        _serve_stop()
        try:
            proc=subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                '-File',str(root/'guest_bridge.ps1'),'-Serve'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW,env=env)
        except OSError:
            raise _NotSent() from None
        lines=queue.Queue()
        def pump(stream=proc.stdout,sink=lines):
            try:
                for line in stream:sink.put(line)
            except (OSError,ValueError):pass
            sink.put(None)
        threading.Thread(target=pump,daemon=True,name='desktop-bridge-serve').start()
        _SERVE.update(proc=proc,lines=lines)
    lines=_SERVE['lines']
    try:
        proc.stdin.write(text);proc.stdin.flush()
    except (OSError,ValueError):
        _serve_stop()
        raise _NotSent() from None
    deadline=time.monotonic()+timeout
    def next_line():
        remaining=deadline-time.monotonic()
        try:
            if remaining<=0:raise queue.Empty
            line=lines.get(timeout=remaining)
        except queue.Empty:
            raise subprocess.TimeoutExpired('guest_bridge.ps1 -Serve',timeout) from None
        if line is None:raise _Broken()
        return line.rstrip('\r\n').lstrip('\ufeff')
    try:
        header=next_line()
        while not header.startswith(SERVE_HEADER):header=next_line()
        meta=json.loads(header[len(SERVE_HEADER):])
        body=next_line()
    except (subprocess.TimeoutExpired,_Broken):
        _serve_stop()
        raise
    except ValueError:
        _serve_stop()
        raise _Broken() from None
    stages=meta.get('stages') or []
    if isinstance(stages,dict):stages=[stages]
    stderr='\n'.join('DSH_BRIDGE_TIMING '+json.dumps(s) for s in stages if isinstance(s,dict))
    code=meta.get('exitCode')
    return subprocess.CompletedProcess(['guest_bridge.ps1','-Serve'],code if type(code) is int else 1,body,stderr)


def _frame_from_flat(response):
    """The bridge returns the after-action frame as flat fields; expose one frame dict."""
    image=response.pop('frameImage',None)
    fields={k:response.pop(k,None) for k in ('frameWidth','frameHeight','frameCapturedAt')}
    if isinstance(image,str) and image:
        response['frame']={'image':image,'width':fields['frameWidth'],'height':fields['frameHeight'],
                           'capturedAt':fields['frameCapturedAt']}
    return response


def _remote_token(root):
    return (root/'data'/'her-agent.token').read_text(encoding='utf-8').strip()


def _remote_call(root,action,screen_after=None,**args):
    """Same receipt contract as the Windows bridge, over the LAN agent."""
    mutating = action not in ('health','screen','admin_result')
    request={'action':action,**args}
    if screen_after is not None and action in ('click','type','key','scroll'):
        request['screen_after_ms']=max(0,min(3000,round(float(screen_after)*1000)))
    headers={'Authorization':'Bearer '+_remote_token(root)}
    data=None
    url=REMOTE_BASE+('/health' if action=='health' else '/action')
    if action!='health':
        data=json.dumps(request,ensure_ascii=False).encode('utf-8')
        headers['Content-Type']='application/json'
    req=urllib.request.Request(url,data=data,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=REMOTE_TIMEOUT) as response:
            raw=response.read()
    except urllib.error.HTTPError as error:
        try:
            message=json.loads(error.read()).get('error') or '专属桌面拒绝了这次操作'
        except ValueError:
            message='专属桌面接口错误（HTTP '+str(error.code)+'）'
        raise DesktopError(str(message),False) from None
    except TimeoutError:
        raise DesktopError('专属桌面操作超时；结果待核对，不自动重试',mutating) from None
    except urllib.error.URLError as error:
        reason=error.reason
        if isinstance(reason,(ConnectionRefusedError,socket.gaierror)):
            raise DesktopError('专属桌面暂不可用，动作未发出',False) from None
        if isinstance(reason,TimeoutError):
            raise DesktopError('专属桌面操作超时；结果待核对，不自动重试',mutating) from None
        raise DesktopError('专属桌面连接失败，动作未发出',False) from None
    except OSError:
        raise DesktopError('专属桌面暂不可用，动作未发出',False) from None
    try:
        value=json.loads(raw.decode('utf-8'))
    except (ValueError,UnicodeDecodeError):
        raise DesktopError('专属桌面回执无法解析；结果待核对',True) from None
    if value.get('ok') is not True:
        raise DesktopError(str(value.get('error') or '专属桌面操作未完成'),mutating)
    if action=='screen' and isinstance(value.get('image'),str) and 'frameImage' not in value:
        value['frameImage']=value['image']
        value['frameWidth']=value.get('width')
        value['frameHeight']=value.get('height')
        value['frameCapturedAt']=value.get('capturedAt')
    return _frame_from_flat(value)


def remote_request(root,endpoint,payload,*,mutating=True):
    """POST a non-action request to her container and return the JSON receipt."""
    headers={'Authorization':'Bearer '+_remote_token(root),'Content-Type':'application/json'}
    data=json.dumps(payload,ensure_ascii=False).encode('utf-8')
    req=urllib.request.Request(REMOTE_BASE+endpoint,data=data,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=REMOTE_TIMEOUT) as response:
            raw=response.read()
    except urllib.error.HTTPError as error:
        try:
            message=json.loads(error.read()).get('error') or '她的电脑拒绝了这次操作'
        except ValueError:
            message='她的电脑接口错误（HTTP '+str(error.code)+'）'
        raise DesktopError(str(message),False) from None
    except TimeoutError:
        raise DesktopError('她的电脑操作超时；结果待核对，不自动重试',mutating) from None
    except urllib.error.URLError as error:
        reason=error.reason
        if isinstance(reason,(ConnectionRefusedError,socket.gaierror)):
            raise DesktopError('她的电脑暂不可用，操作未发出',False) from None
        if isinstance(reason,TimeoutError):
            raise DesktopError('她的电脑操作超时；结果待核对，不自动重试',mutating) from None
        raise DesktopError('她的电脑连接失败，操作未发出',False) from None
    except OSError:
        raise DesktopError('她的电脑暂不可用，操作未发出',False) from None
    try:
        value=json.loads(raw.decode('utf-8'))
    except (ValueError,UnicodeDecodeError):
        raise DesktopError('她的电脑回执无法解析；结果待核对',True) from None
    if value.get('ok') is not True:
        uncertain=bool(value.get('timed_out')) or (mutating and value.get('executed',True) is not False)
        raise DesktopError(str(value.get('error') or '操作未完成'),uncertain)
    return value


def call(root,action,screen_after=None,**args):
    if action not in ('health','screen','click','type','key','scroll','ensure_running','admin_submit','admin_result'):
        raise ValueError('Unknown guest action')
    if action=='ensure_running' and args:
        raise ValueError('Fixed VM startup accepts no parameters')
    if REMOTE:
        return _remote_call(root,action,screen_after,**args)
    request={'action':action,**args}
    if screen_after is not None and action in ('click','type','key','scroll'):
        request['screen_after_ms']=max(0,min(3000,round(float(screen_after)*1000)))
    queued=time.monotonic()
    with LOCK:
        started=time.monotonic()
        mutating=action not in ('health','screen','admin_result')
        record={'at':datetime.now(timezone.utc).isoformat(),'action':action,
                'queueWaitMs':round((started-queued)*1000),'automaticRetry':False}
        stderr=''
        env=os.environ.copy()
        # A Python child does not receive pwsh's automatic Windows PowerShell
        # PSModulePath cleanup. Do not load PS7 core modules in Windows PS5.
        env['PSModulePath']=r'C:\Windows\System32\WindowsPowerShell\v1.0\Modules;C:\Program Files\WindowsPowerShell\Modules'
        text=json.dumps(request,ensure_ascii=False)+'\n'
        try:
            result=None
            if PERSISTENT['enabled']:
                try:
                    result=_serve_call(root,text,env,35)
                    record['bridge']='persistent'
                except _NotSent:
                    result=None
                except _Broken:
                    # Sent but no receipt: never replay a mutating action.
                    if mutating:
                        record.update(bridge='persistent',outcome='invalid_receipt',uncertain=True,
                                      processMs=round((time.monotonic()-started)*1000),stages=[])
                        _record_timing(root,record)
                        raise DesktopError('Guest connection unavailable; operation result unconfirmed',True) from None
                    result=None
            if result is None:
                record['bridge']='one-shot'
                result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                    '-File',str(root/'guest_bridge.ps1')],input=text,
                    capture_output=True,text=True,encoding='utf-8',timeout=35,creationflags=subprocess.CREATE_NO_WINDOW,env=env)
        except subprocess.TimeoutExpired as error:
            stderr=error.stderr
            record.update(outcome='timeout',uncertain=mutating)
            record.update(processMs=round((time.monotonic()-started)*1000),stages=bridge_stages(stderr))
            _record_timing(root,record)
            raise DesktopError('专属桌面操作超时；结果待核对，不自动重试',mutating) from None
        stderr=result.stderr if hasattr(result,'stderr') else ''
        record.update(processMs=round((time.monotonic()-started)*1000),stages=bridge_stages(stderr))
        try:
            response=json.loads(result.stdout.lstrip('\ufeff'))
        except ValueError:
            record.update(outcome='invalid_receipt',uncertain=mutating)
            _record_timing(root,record)
            raise DesktopError('Guest connection unavailable; operation result unconfirmed',mutating) from None
        if result.returncode or response.get('ok') is not True:
            record.update(outcome='rejected',uncertain=mutating)
            _record_timing(root,record)
            raise DesktopError(response.get('error','Guest operation unavailable'),mutating)
        record.update(outcome='receipt',uncertain=False)
        _record_timing(root,record)
        return _frame_from_flat(response)


def _auto_poll_enabled(store):
    with store.lock:
        config=json.loads((store.root/'config.json').read_text(encoding='utf-8-sig'))
        store.config=config
    settings=config.get('desktop',{})
    on_demand=settings.get('on_demand') is True
    enabled=settings.get('auto_poll') is True and not on_demand
    if not enabled:
        status=dict(store.desktop_live)
        status.setdefault('ready',False)
        status.update(auto_poll=False,on_demand=on_demand,
            message='电脑按需连接，保留最后画面' if on_demand else '自动桌面观察已停用，保留最后画面')
        store.desktop_live=status
    return enabled


def screen_after_seconds(config,device='desktop'):
    """动作回执直接附动作后的画面；config.speed 可关闭或调等待时间。"""
    speed=config.get('speed') or {}
    if speed.get('screen_after_action',True) is not True:return None
    value=speed.get(device+'_settle_ms',500 if device=='phone' else 400)
    if type(value) not in (int,float) or not 0<=value<=3000:value=500 if device=='phone' else 400
    return value/1000


def _check_health(health):
    if health.get('ok') is True and health.get('agent')=='dsh-her-pc':
        return
    if not (health.get('guestPolicyReady') is True and health.get('guestOnly') is True and health.get('user')=='DSH'):
        raise DesktopError('Guest desktop policy or standard account not confirmed')


def _publish_frame(store,frame,health):
    try:
        image=base64.b64decode(frame['image'],validate=True)
        status={k:frame[k] for k in ('width','height','capturedAt')}
    except (ValueError,KeyError,TypeError):
        raise DesktopError('Guest frame receipt invalid') from None
    if not image.startswith(b'\xff\xd8'):
        raise DesktopError('Guest frame format invalid')
    store.check_storage()
    temp=store.data/'desktop-frame.tmp'
    temp.write_bytes(image);temp.replace(store.data/'desktop.jpg')
    status.update(ready=True,health=health,scope='DSH-Home-v01')
    store.desktop_live=status
    return status


def prepare(store,*,allow_start=False):
    """Prepare on actual desktop demand, INSIDE the caller's global device lease.

    No nested lease, config write, action replay, background activation or wait
    loop. A booting guest can return a read-only health error for a later probe.
    """
    if type(allow_start) is not bool:
        raise DesktopError('Explicit boolean startup permission required')
    store.desktop_live={**store.desktop_live,'ready':False,'on_demand':True,
                        'message':'正在按需连接专属电脑'}
    try:
        if allow_start and not REMOTE:
            store.check_storage()
            call(store.root,'ensure_running')
        store.check_storage()
        health=call(store.root,'health')
        _check_health(health)
        store.check_storage()
        frame=call(store.root,'screen')
        return _publish_frame(store,frame,health)
    except Exception as error:
        store.desktop_live={**store.desktop_live,'ready':False,'on_demand':True,
            'error':str(error)[:500],'code':getattr(error,'code','DESKTOP_UNAVAILABLE')}
        raise


def start(store):
    if REMOTE:
        def remote_poll():
            while True:
                try:
                    health=call(store.root,'health')
                    _check_health(health)
                    display=health.get('display') or {}
                    store.desktop_live={'ready':True,'scope':'dsh-her-pc',
                        'width':display.get('width'),'height':display.get('height'),'health':health}
                except Exception as error:
                    store.desktop_live={'ready':False,'error':str(error)[:200],
                        'code':getattr(error,'code','DESKTOP_UNAVAILABLE')}
                time.sleep(15)
        threading.Thread(target=remote_poll,daemon=True,name='space-desktop-health').start()
        return True

    def poll():
        previous=None
        while True:
            try:
                if not _auto_poll_enabled(store):
                    previous=False;time.sleep(5);continue
                if not (store.root/'data/guest-secrets.json').exists():
                    time.sleep(5);continue
                store.check_storage()
                health=_background_call(store,'health')
                _check_health(health)
                if not _auto_poll_enabled(store):
                    previous=False;time.sleep(5);continue
                store.check_storage()
                frame=_background_call(store,'screen')
                if not _auto_poll_enabled(store):
                    previous=False;time.sleep(5);continue
                _publish_frame(store,frame,health)
                if previous is not True:
                    with store.lock:
                        config=json.loads((store.root/'config.json').read_text(encoding='utf-8-sig'))
                        # A connected device is not permission to dispatch tasks.
                        config['desktop']['ready']=True
                        tmp=store.root/'config.desktop.tmp';tmp.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(store.root/'config.json')
                        store.config=config
                    store.event('desktop.connected',{'message':'专属虚拟机真实桌面已连接，使用标准用户DSH'})
                previous=True
            except DesktopPreviewDeferred:
                # Busy is not disconnected. No readiness/config/event change,
                # no immediate retry; resume at the ordinary next poll.
                pass
            except Exception as e:
                store.desktop_live={'ready':False,'error':str(e)[:500],'code':getattr(e,'code','DESKTOP_UNAVAILABLE')}
                if previous is True:
                    with store.lock:
                        config=json.loads((store.root/'config.json').read_text(encoding='utf-8-sig'))
                        config['desktop']['ready']=False
                        tmp=store.root/'config.desktop.tmp';tmp.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(store.root/'config.json')
                        store.config=config
                    store.event('desktop.disconnected',{'message':'桌面连接中断，保留最后画面并停止新桌面操作'})
                previous=False
            time.sleep(5)
    threading.Thread(target=poll,daemon=True,name='exclusive-guest-frame-relay').start()
