"""Opt-in boot resume: clear the start-up pause once the console is ready.

Only runs when DSH_AUTO_RESUME=1. Refuses to resume while blockers exist.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, '/opt/dsh-space')
from owner_http_auth import owner_opener

BASE = 'http://127.0.0.1:19991'


def request(opener, path, body=None, csrf=None):
    headers = {}
    if body is not None:
        headers = {'Content-Type': 'application/json', 'Origin': BASE, 'X-CSRF-Token': csrf or ''}
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode('utf-8'),
        headers=headers,
    )
    with opener.open(req, timeout=20) as response:
        return json.load(response)


def main():
    opener = owner_opener(Path('/opt/dsh-space'), BASE)
    for _ in range(5):
        try:
            status = request(opener, '/api/status')
        except Exception as error:
            print('auto-resume: status failed ' + str(error), flush=True)
            time.sleep(2)
            continue
        print('auto-resume: paused=' + str(status.get('paused')) +
              ' blockers=' + str(status.get('blockers')), flush=True)
        if not status.get('paused'):
            return 0
        if status.get('blockers'):
            print('auto-resume: blockers present, stay paused', flush=True)
            return 0
        try:
            print('auto-resume: resume ' +
                  json.dumps(request(opener, '/api/control/resume', {}, status['csrf']),
                             ensure_ascii=False), flush=True)
            return 0
        except Exception as error:
            print('auto-resume: resume failed ' + str(error), flush=True)
            time.sleep(2)
    print('auto-resume: gave up', flush=True)
    return 1


if __name__ == '__main__':
    sys.exit(main())
