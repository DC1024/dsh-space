"""Independent loopback observer; read-only and no dependency on phone or QQ services."""
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from thinking_observer import ThinkingObserverManager
from thinking_http import handle_formal_thinking
from owner_http_auth import ObserverHttpAuth

def handler(observer,auth):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,status,data):
            raw=json.dumps(data,ensure_ascii=True).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)));self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if not auth.authenticated(self.headers) or self.headers.get('Sec-Fetch-Site')=='cross-site':
                return self.reply(403,{'error':'Host service credential required','code':'AUTH_REJECTED'})
            if not handle_formal_thinking(self,observer): self.reply(404,{'error':'unknown endpoint'})
    return Handler

def main():
    auth=ObserverHttpAuth(Path(__file__).resolve().parent)
    observer=ThinkingObserverManager();observer.start()
    try: ThreadingHTTPServer(('127.0.0.1',19993),handler(observer,auth)).serve_forever()
    finally: observer.stop()

if __name__=='__main__':main()
