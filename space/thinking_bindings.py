"""Pure format parsing for explicit formal-session bindings; no I/O or defaults.

Filesystem, URL, model and concrete SID syntax checks belong to validate_binding.
Active persona/session fields are UI hints only and never change returned order.
"""
from __future__ import annotations

import copy


class BindingsFormatError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise BindingsFormatError(code)


def _session_id(entry):
    values = [entry[key] for key in ('sessionId', 'sid', 'session_id') if key in entry]
    if not values:
        _fail('BINDINGS_SID_REQUIRED')
    if any(value != values[0] for value in values[1:]):
        _fail('BINDINGS_SID_CONFLICT')
    value = values[0]
    if not isinstance(value, str) or not value.strip():
        _fail('BINDINGS_SID_INVALID')
    return value


def _registration_entries(payload):
    allowed = {'schemaVersion', 'home', 'profile', 'base', 'model', 'provider', 'agent_preset',
               'activePersona', 'sessionId', 'registrations'}
    if 'schemaVersion' in payload and (type(payload['schemaVersion']) is not int or payload['schemaVersion'] != 2):
        _fail('BINDINGS_VERSION_UNSUPPORTED')
    if any(key not in allowed for key in payload):
        _fail('BINDINGS_TOP_LEVEL_FIELD_UNKNOWN')
    entries = payload.get('registrations')
    if not isinstance(entries, list) or not entries:
        _fail('BINDINGS_SESSIONS_REQUIRED')
    if len(entries) > 2:
        _fail('BINDINGS_TOO_MANY_SESSIONS')
    ids, by_persona, result = set(), {}, []
    for entry in entries:
        if not isinstance(entry, dict) or not entry:
            _fail('BINDINGS_ENTRY_INVALID')
        persona = entry.get('persona')
        if not isinstance(persona, str) or persona not in ('biao', 'li'):
            _fail('BINDINGS_PERSONA_INVALID')
        # The top-level current SID is never a default for an absent entry SID.
        sid = _session_id(entry)
        if sid in ids:
            _fail('BINDINGS_SID_DUPLICATE')
        if persona in by_persona:
            _fail('BINDINGS_PERSONA_DUPLICATE')
        ids.add(sid)
        by_persona[persona] = sid
        merged = copy.deepcopy(entry)
        for key in ('home', 'profile', 'base', 'model', 'provider'):
            if key not in merged and key in payload:
                merged[key] = copy.deepcopy(payload[key])
        result.append(merged)
    if 'activePersona' in payload:
        active = payload['activePersona']
        if not isinstance(active, str) or active not in ('biao', 'li'):
            _fail('BINDINGS_ACTIVE_PERSONA_INVALID')
        if active not in by_persona:
            _fail('BINDINGS_ACTIVE_PERSONA_UNREGISTERED')
    if 'sessionId' in payload:
        current = _session_id({'sessionId': payload['sessionId']})
        if current not in ids:
            _fail('BINDINGS_ACTIVE_SESSION_UNREGISTERED')
        if 'activePersona' in payload and by_persona[payload['activePersona']] != current:
            _fail('BINDINGS_ACTIVE_IDENTITY_CONFLICT')
    return result


def binding_entries(payload):
    """Return independent entry copies from a legacy object or a v2 manifest.

    The coordination format uses registrations with biao/li identities and
    optional shared fields; the older sessions surface/inner form also works.
    Legacy entries retain all public extension fields and SID alias spelling.
    Entries in any form never become a default query identity.
    """
    if not isinstance(payload, dict) or not payload:
        _fail('BINDINGS_OBJECT_REQUIRED')
    if 'registrations' in payload and 'sessions' in payload:
        _fail('BINDINGS_FORMATS_MIXED')
    if 'registrations' in payload:
        return _registration_entries(payload)
    is_manifest = 'sessions' in payload or payload.get('schemaVersion') == 2
    if not is_manifest:
        _session_id(payload)
        return [copy.deepcopy(payload)]

    if type(payload.get('schemaVersion')) is not int or payload['schemaVersion'] != 2:
        _fail('BINDINGS_VERSION_UNSUPPORTED')
    if any(key not in ('schemaVersion', 'sessions', 'activeSessionId') for key in payload):
        _fail('BINDINGS_TOP_LEVEL_FIELD_UNKNOWN')
    entries = payload.get('sessions')
    if not isinstance(entries, list) or not entries:
        _fail('BINDINGS_SESSIONS_REQUIRED')
    if len(entries) > 2:
        _fail('BINDINGS_TOO_MANY_SESSIONS')
    session_ids, personas = set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry:
            _fail('BINDINGS_ENTRY_INVALID')
        persona = entry.get('persona')
        if not isinstance(persona, str) or persona not in ('surface', 'inner'):
            _fail('BINDINGS_PERSONA_INVALID')
        sid = _session_id(entry)
        if sid in session_ids:
            _fail('BINDINGS_SID_DUPLICATE')
        if persona in personas:
            _fail('BINDINGS_PERSONA_DUPLICATE')
        session_ids.add(sid)
        personas.add(persona)
    if 'activeSessionId' in payload:
        active = payload['activeSessionId']
        if not isinstance(active, str) or not active.strip():
            _fail('BINDINGS_ACTIVE_SESSION_INVALID')
        if active not in session_ids:
            _fail('BINDINGS_ACTIVE_SESSION_UNREGISTERED')
    return copy.deepcopy(entries)
