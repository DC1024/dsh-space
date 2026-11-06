"""Exclusive runtime RPC, using existing credential-aware helper without copying secrets."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from datetime import datetime, timezone
from space import ROOT

NODE = os.environ.get('DSH_NODE') or shutil.which('node') or 'node'

HELPER = ROOT/'integration/dsh-rpc.mjs'
READ_METHODS = frozenset(('session/projections', 'session/page'))


def _diagnostic(method, attempt, code):
    # Do not persist arguments, provider responses, stderr, or credentials.
    try:
        with (ROOT/'data/rpc-transport-errors.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                'method': method, 'attempt': attempt, 'code': code})+'\n')
    except OSError:
        pass


def rpc(method, args=None, timeout=30):
    request = {'base':'http://127.0.0.1:19992','home':str(ROOT/'data/runtime-home'),
               'method':method,'args':args or {},'timeoutMs':timeout*1000}
    # The runtime composes its host generation just after the port opens. The
    # first read can race that composition and report sessionController as
    # unavailable; retry reads briefly instead of failing the owner's task.
    attempts = 8 if method in READ_METHODS else 1
    for attempt in range(1, attempts+1):
        try:
            result = subprocess.run([NODE,str(HELPER)],
                input=json.dumps(request),capture_output=True,text=True,encoding='utf-8',timeout=timeout+10,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:
                raise RuntimeError('RPC_PROCESS_EXIT_'+str(result.returncode))
            envelope=json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError, RuntimeError) as error:
            code = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            _diagnostic(method, attempt, code)
            if attempt == attempts:
                raise RuntimeError('独立 DSH RPC 传输失败：'+code+'；诊断已保存，任务未自动重发') from None
            time.sleep(0.25 * attempt)
            continue
        value=envelope.get('result',{})
        if value.get('ok') is not True:
            err=value.get('error',{})
            code=str(err.get('code'))
            message=str(err.get('message'))[:500]
            retryable=code.startswith(('gateway/service-unavailable','gateway/timeout','gateway/internal'))
            _diagnostic(method, attempt, code)
            if method in READ_METHODS and retryable and attempt < attempts:
                time.sleep(1.0)
                continue
            raise RuntimeError(code+': '+message)
        return value.get('value')
    raise RuntimeError('独立 DSH RPC 传输失败：未取得有效回执；任务未自动重发')
