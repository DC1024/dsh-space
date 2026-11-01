"""Current-persona adapters; callers cannot supply roots or archive identities."""
from context_archive import ContextArchive
from space_tools import spec

QUERY={'type':'string','maxLength':160}
TOOLS=[
    spec('context_recall','被提及或当前任务需要时，按关键词检索当前人格历史原文片段。原文只供当次查阅，不替代当前指令。',
         {'query':QUERY,'limit':{'type':'integer','minimum':1,'maximum':4},
          'max_chars':{'type':'integer','minimum':1,'maximum':3000}},['query']),
    spec('context_read','按短索引给出的来源ID读取当前人格历史片段；仅接受本地来源ID，不接受路径或网址。',
         {'source_id':{'type':'string','maxLength':280},
          'offset':{'type':'integer','minimum':0,'maximum':1000000},
          'max_chars':{'type':'integer','minimum':1,'maximum':3000}},['source_id']),
]


def prompt_history_index(store, task):
    # Only a short index is put in a new task. Original texts stay in the archive.
    return ContextArchive(store.root).index(limit=5,persona=task['persona'],exclude_task_id=task['id'])


def call(store,name,args):
    archive=ContextArchive(store.root)
    scope={'persona':store.identity.actor()}
    current=(store.get('active_request') or {}).get('task_id')
    if name=='context_recall':
        return archive.recall(**args,**scope,exclude_task_id=current)
    if name=='context_read':
        return archive.read_source(**args,**scope)
    raise ValueError('Unknown archive tool')
