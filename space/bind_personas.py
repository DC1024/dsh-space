"""Provision blank, separate sessions; never prompt, resume autonomy or enable dispatch."""
import json
import uuid
from dsh_rpc import rpc
from space import ROOT, Store


def main():
    store = Store(ROOT)
    config = store.config
    bindings = config['runtime']['persona_bindings']
    for persona in ('biao', 'li'):
        binding = bindings[persona]
        if binding.get('session_id'):
            # Reading projections verifies the binding without creating a turn.
            rpc('session/projections', {'request': {'sessionId': binding['session_id']}})
            continue
        requested = 'session-'+str(uuid.uuid4())
        created = rpc('session/create', {'request': {'sessionId': requested,
            'cwd': str(ROOT/'workdir'/persona), 'agentPreset': binding['agent_preset']}})
        sid = created['sessionId']
        from owner_settings import model_values
        llm = model_values(store)
        rpc('session/selectModel', {'request': {'sessionId': sid, 'provider':llm['provider'],
                                             'model':llm['model'],'reasoningEffort':llm['reasoning_effort']}})
        binding['session_id'] = sid
        # Checkpoint each created SID so an interrupted provisioning never creates duplicates.
        config['runtime']['session_id'] = bindings['biao'].get('session_id')
        temp = ROOT/'config.bind.tmp'
        temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(ROOT/'config.json')
    store.config = config
    public = store.identity.publish_binding()
    store.db.close()
    print(json.dumps({'sessions':public['registrations'],'dispatch_enabled':bool(config.get('dispatch_enabled')),'autonomy_enabled':bool(config.get('autonomy_enabled'))},ensure_ascii=True))


if __name__=='__main__':
    main()
