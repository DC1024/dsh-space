"""Offline HTTP authorization checks; no production/device/control requests."""
import http.client
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from owner_http_auth import OwnerHttpAuth,ObserverHttpAuth,host_capability,entry_url,owner_opener,observer_open,BOOTSTRAP_JS
from space import Server,handler


class OwnerSessions(unittest.TestCase):
    def test_session_needs_independent_capability_and_expires(self):
        now=[10.0];auth=OwnerHttpAuth(secret=secrets.token_urlsafe(32),clock=lambda:now[0],lifetime=2)
        self.assertIsNone(auth.issue('Bearer model-key'))
        self.assertIsNone(auth.issue({'capability':'anything'}))
        self.assertFalse(auth.authenticated('DSHOwnerSession=guessed'))
        cookie=auth.issue(auth._secret)
        self.assertTrue(auth.authenticated(cookie))
        self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
        now[0]=12.0
        self.assertFalse(auth.authenticated(cookie))

    def test_key_is_stable_local_and_fragment_is_not_served(self):
        with tempfile.TemporaryDirectory() as folder:
            secret=host_capability(folder)
            self.assertEqual(secret,host_capability(folder))
            parsed=urlsplit(entry_url(folder))
            self.assertEqual(parsed.query,'');self.assertEqual(parsed.path,'/owner-login')
            self.assertEqual(parsed.fragment,'owner='+secret)
            self.assertIn('history.replaceState',BOOTSTRAP_JS)
            with self.assertRaises(ValueError):entry_url(folder,'http://10.0.2.2:19991')


class OwnerHttpBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        outer=self
        class FakeStore:
            root=Path(outer.tmp.name)
            config={'port':0}
            bridge_key=secrets.token_urlsafe(32)
            writes=0
            def get(self,key,default=None):return default
            def status(self):return {'version':'test','instance_id':'fixture'}
            def command(self,*_):self.writes+=1;return {'ok':True}
            def mcp(self,body):return {'jsonrpc':'2.0','id':body.get('id'),'result':{'safe':True}}
            class identity:
                @staticmethod
                def validate(persona):return persona
                @staticmethod
                def bind(persona):
                    from contextlib import nullcontext
                    return nullcontext()
        self.store=FakeStore()
        self.server=Server(('127.0.0.1',0),http.client.HTTPConnection)
        self.store.config['port']=self.server.server_port
        self.server.RequestHandlerClass=handler(self.store)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.host=urlsplit(self.base).netloc
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()

    def request(self,path='/api/status',method='GET',headers=None,body=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        hdr={'Host':self.host,'Origin':self.base};hdr.update(headers or {})
        if body is not None:body=json.dumps(body).encode();hdr['Content-Type']='application/json'
        try:
            conn.request(method,path,body=body,headers=hdr)
            result=conn.getresponse();return result.status,dict(result.getheaders()),result.read()
        finally:conn.close()

    def test_spoofed_host_origin_and_model_bearer_cannot_read_private_get(self):
        for path in ('/api/status','/api/memory','/api/task-result?id=x','/api/events','/api/thinking',
                     '/api/phone-frame','/api/frame','/exports/memory.zip','/permissions','/app-v02.js'):
            for headers in ({},{'Authorization':'Bearer '+self.store.bridge_key}):
                status,_,body=self.request(path,headers=headers)
                self.assertEqual(status,403,path);self.assertNotIn(b'csrf',body)
                self.assertNotIn(host_capability(self.store.root).encode(),body)
        self.assertEqual(self.request(headers={'Host':'10.0.2.2:'+str(self.server.server_port)})[0],403)

    def test_owner_bootstrap_then_status_and_control_require_cookie_plus_csrf(self):
        for headers,body in (({}, {'capability':self.store.bridge_key}),
                             ({'Origin':'http://external.invalid'}, {'capability':host_capability(self.store.root)}),
                             ({'Authorization':'Bearer '+self.store.bridge_key},{})):
            status,hdr,_=self.request('/owner/session','POST',headers,body)
            self.assertEqual(status,403);self.assertNotIn('Set-Cookie',hdr)
        status,hdr,raw=self.request('/owner/session','POST',body={'capability':host_capability(self.store.root)})
        self.assertEqual(status,200);self.assertEqual(json.loads(raw),{'ok':True})
        cookie=hdr['Set-Cookie'].split(';',1)[0]
        status,_,raw=self.request(headers={'Cookie':cookie})
        self.assertEqual(status,200);csrf=json.loads(raw)['csrf']
        # These writes affect this temporary fake store only.
        status,_,_=self.request('/api/control/pause','POST',body={})
        self.assertEqual(status,403);self.assertEqual(self.store.writes,0)
        status,_,_=self.request('/api/control/pause','POST',{'X-CSRF-Token':csrf},body={})
        self.assertEqual(status,403);self.assertEqual(self.store.writes,0)
        status,_,_=self.request('/api/control/pause','POST',{'Cookie':cookie},body={})
        self.assertEqual(status,403);self.assertEqual(self.store.writes,0)
        status,_,_=self.request('/api/control/pause','POST',{'Cookie':cookie,'X-CSRF-Token':csrf},body={})
        self.assertEqual(status,200);self.assertEqual(self.store.writes,1)

    def test_bearer_works_only_for_model_bridge_and_never_grants_owner_session(self):
        body={'jsonrpc':'2.0','id':1,'method':'tools/list'}
        self.assertEqual(self.request('/mcp/biao','POST',body=body)[0],403)
        self.assertEqual(self.request('/mcp/biao','POST',{'Authorization':'Bearer '+self.store.bridge_key},body)[0],200)
        self.assertEqual(self.request('/api/control/pause','POST',{'Authorization':'Bearer '+self.store.bridge_key}, {})[0],403)
        self.assertEqual(self.store.writes,0)

    def test_public_bootstrap_does_not_contain_credential_and_host_cli_can_read_status(self):
        for path in ('/','/owner-login','/owner-bootstrap.js'):
            status,_,body=self.request(path)
            self.assertEqual(status,200);self.assertNotIn(host_capability(self.store.root).encode(),body)
            self.assertNotIn(b'csrf',body)
        opener=owner_opener(self.store.root,self.base)
        with opener.open(self.base+'/api/status',timeout=5) as response:
            value=json.load(response)
        self.assertEqual(value['instance_id'],'fixture');self.assertTrue(value['csrf'])


class InternalObserverBoundary(unittest.TestCase):
    def test_spoofed_headers_model_and_owner_credentials_are_rejected(self):
        from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
        from observer_server import handler as observer_handler
        with tempfile.TemporaryDirectory() as root:
            server=ThreadingHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
            base='http://127.0.0.1:'+str(server.server_port)
            auth=ObserverHttpAuth(root,base)
            server.RequestHandlerClass=observer_handler(None,auth)
            def fixture(h,_):h.reply(200,{'sessionId':'fixture','ready':True});return True
            with patch('observer_server.handle_formal_thinking',fixture):
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    for extras in ({},{'Authorization':'Bearer model-key'},
                                   {auth.header:host_capability(root)}, {'Cookie':'DSHOwnerSession=guessed'}):
                        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                        try:
                            conn.request('GET','/api/thinking/status',headers={'Host':urlsplit(base).netloc,'Origin':base,**extras})
                            response=conn.getresponse();raw=response.read()
                            self.assertEqual(response.status,403);self.assertNotIn(b'fixture',raw)
                        finally:conn.close()
                    with observer_open(root,'/api/thinking/status',base=base) as response:
                        self.assertEqual(json.load(response),{'sessionId':'fixture','ready':True})
                    owner=OwnerHttpAuth(root)
                    self.assertIsNone(owner.issue(auth._secret))
                finally:server.shutdown();server.server_close();thread.join()

    def test_internal_credential_cannot_be_redirected_to_another_origin(self):
        import urllib.error
        from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                self.send_response(302);self.send_header('Location','http://127.0.0.1:1/collect');self.end_headers()
        with tempfile.TemporaryDirectory() as root:
            server=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                with self.assertRaises(urllib.error.HTTPError) as error:observer_open(root,'/api/thinking/status',base=base)
                self.assertEqual(error.exception.code,302)
                with self.assertRaises(ValueError):observer_open(root,'http://external.invalid/collect')
            finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main(verbosity=2)
