"""Finite owner-created acceptance grants. Never opens normal dispatch."""
from contextlib import closing
import hashlib
import json
import math
import re
import sqlite3
import time


class AcceptanceDenied(ValueError):
    pass


def grant(store, consumer):
    try:
        raw = (store.data / 'acceptance-grants-v03.json').read_bytes()
        doc = json.loads(raw)
        current = time.time()
        if (doc.get('enabled') is not True or not current < doc['expires_at'] <= current + 1800
                or doc['created_at'] > current or doc['expires_at'] - doc['created_at'] > 1800):
            return None
        item = doc['consumers'].get(consumer)
        if not item or not isinstance(item['max_requests'], int) or not 1 <= item['max_requests'] <= 20:
            return None
        if not 0 < item['max_upper_cny'] <= 2 or not 0 < item['max_total_cny'] <= 3:
            return None
        persona = item.get('persona')
        if persona:
            with store.identity.bind(persona):
                if (store.identity.runtime()['session_id'] != item['session_id']
                        or store.get('revision') != item['revision']
                        or store.get('cancel_epoch') != item['cancel_epoch']):
                    return None
        return dict(item, grant_id=doc['id'], digest=hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError, KeyError, TypeError):
        return None


def model_gate(store, body):
    sid = body.get('session_id')
    item = grant(store, 'space-runtime:' + str(sid))
    if not item or body.get('phase') != 'model_step' or type(body.get('step')) is not int or not 0 <= body['step'] < item['max_requests']:
        return None
    if store.get('budget_hold'):
        raise AcceptanceDenied('Budget hold remains closed')
    observe = store.observer_status(sid)
    # turn/start frames may still be committing when pre-step arrives. Wait for
    # that finite bootstrap to drain; never treat uncommitted frames as ready.
    deadline = time.monotonic() + 3
    while (body['step'] <= 1 and observe.get('code') == 'MODEL_UNKNOWN'
           and observe.get('freshness', {}).get('pendingFrames', 0) > 0
           and observe.get('freshness', {}).get('backlogAgeMs', 5000) < 5000
           and time.monotonic() < deadline):
        time.sleep(.1)
        observe = store.observer_status(sid)
    if not store.observation_fresh(observe) and not (body['step'] <= 1 and store.initial_model_observation(observe)):
        raise AcceptanceDenied('Observer is unavailable or stale')
    return {'ok': True, 'acceptance_only': True, 'grant_id': item['grant_id']}


def reserve(store, body):
    item = grant(store, body.get('consumer'))
    if not item:
        return None
    upper = body.get('upper_cny')
    if type(upper) not in (float, int) or not math.isfinite(upper) or not 0 < upper <= item['max_upper_cny']:
        raise AcceptanceDenied('Acceptance per-request bound exceeded')
    rid = body.get('id')
    if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', rid):
        raise AcceptanceDenied('Invalid acceptance request id')
    # BEGIN IMMEDIATE serializes the finite grant counter across processes.
    # A crash after authority.reserve keeps the grant slot and held cost.
    with closing(sqlite3.connect(store.data / 'acceptance-v03.sqlite', timeout=15)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS requests(grant_id TEXT,consumer TEXT,id TEXT PRIMARY KEY,upper REAL,digest TEXT)')
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT grant_id,consumer,upper FROM requests WHERE id=?', (rid,)).fetchone()
        if old:
            if old != (item['grant_id'], body['consumer'], upper):
                raise AcceptanceDenied('Acceptance request conflict')
        else:
            count, total = db.execute('SELECT COUNT(*),COALESCE(SUM(upper),0) FROM requests WHERE grant_id=? AND consumer=?', (item['grant_id'], body['consumer'])).fetchone()
            if count >= item['max_requests'] or total + upper > item['max_total_cny']:
                raise AcceptanceDenied('Acceptance grant exhausted')
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?)', (item['grant_id'], body['consumer'], rid, upper, item['digest']))
        db.commit()
    return store.authority.reserve(rid, upper, body['consumer'])
