"""Host owner sessions, independent of spoofable Host/Origin and model bearer.

Integration: call auth.guard(handler) after valid_host(), at the start of both
GET and POST. False means this module already replied. The existing model
Bearer checks and owner Origin/CSRF checks MUST remain in the caller.
The host launcher opens entry_url(); never print or serve its capability.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import hmac
from http.cookies import SimpleCookie,CookieError
import json
import os
from pathlib import Path
import secrets
import stat
import time
from urllib.parse import urlsplit

COOKIE='DSHOwnerSession'
BOOTSTRAP_PATH='/owner/session'
BOOTSTRAP_JS='''"use strict";
async function establishOwnerSession(){
 const state=document.getElementById('ownerLoginState');
 const capability=new URLSearchParams(location.hash.slice(1)).get('owner');
 history.replaceState(null,'','/owner-login');
 if(!capability){state.textContent='请从这台电脑的小鲸鱼启动入口打开主人控制台。';return;}
 try{
  const r=await fetch('/owner/session',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({capability})});
  if(!r.ok)throw Error();
  location.replace('/');
 }catch{state.textContent='主人会话未建立，请重新从本机启动入口打开。';}
}
window.addEventListener('hashchange',establishOwnerSession);
establishOwnerSession();'''
BOOTSTRAP_HTML='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>小鲸鱼 · 主人控制台</title><script defer src="/owner-bootstrap.js"></script></head><body><p id="ownerLoginState">正在核对本地主人会话…</p></body></html>'''

def _protect_file(path):
    if os.name!='nt':os.chmod(path,0o600);return
    adv=ctypes.WinDLL('advapi32',use_last_error=True)
    ker=ctypes.WinDLL('kernel32',use_last_error=True)
    ker.GetCurrentProcess.restype=wintypes.HANDLE
    ker.CloseHandle.argtypes=[wintypes.HANDLE]
    ker.LocalFree.argtypes=[ctypes.c_void_p]
    adv.OpenProcessToken.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
    adv.SetFileSecurityW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p]
    token=wintypes.HANDLE();sid_text=ctypes.c_void_p();descriptor=ctypes.c_void_p()
    try:
        if not adv.OpenProcessToken(ker.GetCurrentProcess(),0x8,ctypes.byref(token)):raise ctypes.WinError(ctypes.get_last_error())
        needed=wintypes.DWORD()
        adv.GetTokenInformation(token,1,None,0,ctypes.byref(needed))
        buf=ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token,1,buf,needed,ctypes.byref(needed)):raise ctypes.WinError(ctypes.get_last_error())
        sid=ctypes.cast(buf,ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid,ctypes.byref(sid_text)):raise ctypes.WinError(ctypes.get_last_error())
        sddl='D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;'+ctypes.wstring_at(sid_text)+')'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,ctypes.byref(descriptor),None):raise ctypes.WinError(ctypes.get_last_error())
        if not adv.SetFileSecurityW(str(path),0x80000004,descriptor):raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if sid_text.value:ker.LocalFree(sid_text)
        if descriptor.value:ker.LocalFree(descriptor)
        if token:ker.CloseHandle(token)

def _protected_capability(root,name):
    if name not in ('owner-http.key','observer-http.key'):raise ValueError('Unknown host credential')
    path=Path(root).absolute()/'data'/name
    for p in (*reversed(path.parents),path):
        try:info=p.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise RuntimeError('Owner credential path cannot be a link')
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        if path.stat().st_nlink!=1:raise RuntimeError('Owner credential cannot be a hardlink')
        _protect_file(path)
        secret=path.read_text('ascii').strip()
    else:
        try:
            # Protect the newly created empty file before writing any secret.
            _protect_file(path)
            with os.fdopen(fd,'w',encoding='ascii') as handle:
                secret=secrets.token_urlsafe(32);handle.write(secret+'\n');handle.flush();os.fsync(handle.fileno())
        except BaseException:
            try:os.close(fd)
            except OSError:pass
            raise
    if len(secret)!=43 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in secret):
        raise RuntimeError('Owner credential is invalid; preserve and repair locally')
    return secret

def host_capability(root):
    return _protected_capability(root,'owner-http.key')

class ObserverHttpAuth:
    """Host service-to-service credential; never accepted as an owner session."""
    header='X-DSH-Observer-Capability'
    def __init__(self,root,base='http://127.0.0.1:19993'):
        self.host=urlsplit(_base_url(base)).netloc
        self._secret=_protected_capability(root,'observer-http.key')
    def authenticated(self,headers):
        value=headers.get(self.header,'')
        try:return headers.get('Host')==self.host and hmac.compare_digest(value,self._secret)
        except TypeError:return False

def observer_open(root,path,*,headers=None,timeout=8,base='http://127.0.0.1:19993'):
    """Only the host console calls this; never forward this credential to a browser."""
    import urllib.request,urllib.error
    base=_base_url(base);parsed=urlsplit(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith('/api/thinking') or parsed.fragment:
        raise ValueError('Invalid internal observer endpoint')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*_):return None
    fields=dict(headers or {})
    fields[ObserverHttpAuth.header]=_protected_capability(root,'observer-http.key')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    return opener.open(urllib.request.Request(base+path,headers=fields),timeout=timeout)

def _base_url(base):
    parsed=urlsplit(base)
    if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password
        or parsed.path not in ('','/') or parsed.query or parsed.fragment or not parsed.port):
        raise ValueError('Owner session bootstrap is restricted to host loopback')
    return 'http://127.0.0.1:'+str(parsed.port)

def entry_url(root,base='http://127.0.0.1:19991'):
    return _base_url(base)+'/owner-login#owner='+host_capability(root)

def _sign(secret,text):
    return hmac.new(secret.encode(),text.encode('ascii'),hashlib.sha256).hexdigest()

def _token(secret,expires):
    text=format(int(expires),'x')
    return text+'.'+_sign(secret,text)

class OwnerHttpAuth:
    """主人会话：无状态签名票据。

    票据内容就是「到期时间 + 用 data/owner-http.key 签的 HMAC」。不依赖进程内存，
    所以 space.py 重启（改前端、重启电脑）不会把主人踢下线；12 小时自然过期照旧。
    凭据本身仍只在本机文件里、只本机主人可读，伪造票据需要这个凭据。
    """
    def __init__(self,root=None,*,secret=None,base='http://127.0.0.1:19991',lifetime=12*3600,clock=time.time):
        self.base=_base_url(base);self.host=urlsplit(self.base).netloc
        self._secret=secret if secret is not None else host_capability(root)
        if not isinstance(self._secret,str) or len(self._secret)<32:raise ValueError('Owner capability must have sufficient entropy')
        self._clock=clock;self.lifetime=lifetime
        # Optional trusted gateway: when it presents this shared secret after its
        # own account/password check, a browser session is opened without the
        # capability URL. Empty means the gateway auto-login is disabled.
        self.proxy_secret=os.environ.get('DSH_PROXY_SECRET','')

    def issue(self,capability):
        if not isinstance(capability,str) or len(capability)>128:return None
        try:valid=hmac.compare_digest(capability,self._secret)
        except TypeError:return None
        if not valid:return None
        token=_token(self._secret,self._clock()+self.lifetime)
        return COOKIE+'='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+str(int(self.lifetime))

    def authenticated(self,cookie_header):
        if not isinstance(cookie_header,str) or len(cookie_header)>8192:return False
        try:
            cookie=SimpleCookie();cookie.load(cookie_header)
            value=cookie[COOKIE].value
            if not value or len(value)>96:return False
        except (CookieError,KeyError,ValueError):return False
        text,_,signature=value.partition('.')
        try:expires=int(text,16)
        except ValueError:return False
        if not hmac.compare_digest(signature,_sign(self._secret,text)):return False
        return self._clock()<expires

    @staticmethod
    def _reply(handler,status,value,*,mime='application/json; charset=utf-8',cookie=None):
        raw=(json.dumps(value,ensure_ascii=False).encode('utf-8') if isinstance(value,dict) else value.encode('utf-8'))
        handler.send_response(status);handler.send_header('Content-Type',mime);handler.send_header('Content-Length',str(len(raw)))
        handler.send_header('Cache-Control','no-store');handler.send_header('X-Content-Type-Options','nosniff')
        handler.send_header('Referrer-Policy','no-referrer')
        handler.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; frame-ancestors 'self' http://127.0.0.1:19981; base-uri 'none'")
        if cookie:handler.send_header('Set-Cookie',cookie)
        handler.end_headers();handler.wfile.write(raw)

    def guard(self,handler):
        """False = response sent. True = continue existing authorization/router."""
        path=urlsplit(handler.path).path;method=handler.command
        if handler.headers.get('Host')!=self.host:
            self._reply(handler,403,{'error':'Host rejected','code':'AUTH_REJECTED'});return False
        if method=='POST' and (path=='/mcp' or path.startswith(('/mcp/','/bridge/'))):
            return True  # Existing caller MUST still verify the model bearer.
        if method=='GET' and path=='/owner-bootstrap.js':
            self._reply(handler,200,BOOTSTRAP_JS,mime='text/javascript; charset=utf-8');return False
        if method=='POST' and path==BOOTSTRAP_PATH:
            cookie=None
            if handler.headers.get('Origin')==self.base:
                try:
                    length=int(handler.headers.get('Content-Length','0'))
                    if not 0<length<=4096:raise ValueError()
                    value=json.loads(handler.rfile.read(length))
                    if not isinstance(value,dict) or set(value)!={'capability'}:raise ValueError()
                    cookie=self.issue(value['capability'])
                except (ValueError,TypeError,KeyError):pass
            self._reply(handler,200 if cookie else 403,{'ok':bool(cookie)},cookie=cookie);return False
        authenticated=self.authenticated(handler.headers.get('Cookie',''))
        proxy_authorized=(bool(self.proxy_secret)
                          and hmac.compare_digest(handler.headers.get('X-DSH-Proxy-Authorized','') or '',
                                                  self.proxy_secret))
        if not authenticated and proxy_authorized and method=='GET' and path in ('/','/owner-login'):
            cookie=self.issue(self._secret)
            handler.send_response(302)
            handler.send_header('Location','/')
            handler.send_header('Set-Cookie',cookie)
            handler.send_header('Content-Length','0')
            handler.send_header('Cache-Control','no-store')
            handler.end_headers()
            return False
        if method=='GET' and (path=='/owner-login' or path=='/' and not authenticated):
            self._reply(handler,200,BOOTSTRAP_HTML,mime='text/html; charset=utf-8');return False
        if not authenticated:
            self._reply(handler,403,{'error':'请从本机启动入口建立主人会话','code':'OWNER_SESSION_REQUIRED'});return False
        return True

def owner_opener(root,base='http://127.0.0.1:19991'):
    """For authorized HOST owner scripts only; never expose via model tools."""
    import http.cookiejar,urllib.request
    base=_base_url(base)
    jar=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
    request=urllib.request.Request(base+BOOTSTRAP_PATH,data=json.dumps({'capability':host_capability(root)}).encode(),
        headers={'Origin':base,'Content-Type':'application/json'})
    with opener.open(request,timeout=5) as response:
        if not json.load(response).get('ok'):raise RuntimeError('Host owner session was not established')
    return opener

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(Path(__file__).parent));parser.add_argument('--open',action='store_true')
    args=parser.parse_args()
    if not args.open:parser.error('Use --open from the host owner launcher; the capability is never printed.')
    import webbrowser
    webbrowser.open(entry_url(args.root))
