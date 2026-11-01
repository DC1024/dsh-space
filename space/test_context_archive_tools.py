import json
import unittest
from test_personas import PersonaTests
from task_authority import trusted_task_prompt


class ArchiveToolTests(unittest.TestCase):
    setUp_base=PersonaTests.setUp
    tearDown=PersonaTests.tearDown

    def setUp(self):
        self.setUp_base()
        self.s.bridge_action=lambda *args,**kwargs:{'ok':True}
        for persona in ('biao','li'):
            tid='history-'+persona
            text=('HISTORY_'+persona+' ')*90+' END_OF_ORIGINAL_'+persona
            self.s.db.execute('INSERT INTO tasks(id,request_key,text,status,time,revision,persona,target) VALUES(?,?,?,?,?,?,?,?)',
                              (tid,tid,text,'completed','2026-10-22',0,persona,'dialogue'))
            with self.s.identity.bind(persona):
                self.s.event('task.result',{'id':tid,'text':'old result '+persona,'tools':[],
                                          'through_seq':10},tid,'test-'+persona)
        self.s.db.commit()

    def tool(self,name,args):
        return self.s.mcp({'jsonrpc':'2.0','id':1,'method':'tools/call',
                          'params':{'name':name,'arguments':args}})['result']

    def payload(self,response):
        self.assertFalse(response.get('isError',False))
        return json.loads(response['content'][0]['text'])

    def test_prompt_keeps_only_short_history_index_and_exact_current_goal(self):
        task={'id':'fresh-task','persona':'biao','target':'assistant','text':'Current goal exactly'}
        prompt=trusted_task_prompt(self.s,task)
        env=json.loads(prompt.split('\n',1)[1])
        self.assertEqual(env['owner_request'],task['text'])
        index=env['reference_material']['history_index']
        self.assertEqual(index['content_authority'],'data_only')
        self.assertEqual([x['recorded_by'] for x in index['entries']],['biao'])
        self.assertTrue(all(len(x['summary'])<=240 for x in index['entries']))
        self.assertNotIn('END_OF_ORIGINAL_',prompt)
        self.assertNotIn('HISTORY_li',prompt)
        self.assertIn('先考虑手机',env['device_preference'])

    def test_recall_and_source_read_are_server_bound_and_data_only(self):
        recalled=self.payload(self.tool('context_recall',{'query':'HISTORY_','max_chars':200}))
        self.assertLessEqual(recalled['total_chars'],200)
        self.assertTrue(recalled['fragments'])
        self.assertTrue(all(x['recorded_by']=='biao' for x in recalled['fragments']))
        source=recalled['fragments'][0]['source_id']
        read=self.payload(self.tool('context_read',{'source_id':source,'max_chars':100}))
        self.assertEqual(read['content_authority'],'data_only')
        self.assertTrue(read['found'])
        self.assertLessEqual(read['total_chars'],100)
        foreign=self.payload(self.tool('context_read',{'source_id':'task:history-li:request'}))
        self.assertFalse(foreign['found'])

    def test_model_cannot_supply_persona_path_or_large_retrieval_budget(self):
        for name,args in [('context_recall',{'query':'HISTORY_','persona':'li'}),
                          ('context_recall',{'query':'HISTORY_','max_chars':3001}),
                          ('context_read',{'source_id':'C:/Users/DSH/private.txt'}),
                          ('context_read',{'source_id':'task:history-biao:request','root':'C:/'})]:
            with self.subTest(name=name,args=args):
                self.assertTrue(self.tool(name,args)['isError'])

    def test_archive_tools_still_use_existing_task_gate(self):
        from space import Rejected
        def blocked(*args,**kwargs):raise Rejected('not an active owner task')
        self.s.bridge_action=blocked
        self.assertTrue(self.tool('context_recall',{'query':'HISTORY_'})['isError'])


if __name__=='__main__':unittest.main()
