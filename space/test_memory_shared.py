"""Shared recall/provenance regression tests; all state is synthetic and local."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from space import ROOT, Store, Rejected
from worker import prompt_with_memory


class SharedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='dsh_shared_memory_')
        self.root=Path(self.tmp.name)
        cfg=json.loads((ROOT/'config.json').read_text('utf-8-sig'))
        cfg.update(min_free_gb=0,owner_hold={'paused':True},autonomy_enabled=False,dispatch_enabled=False)
        cfg['runtime'].update(session_id=None,ready=False,chat_ready=False,budget_source_ready=False,
            persona_bindings={p:{'session_id':'session-fixture-'+p} for p in ('biao','li')})
        (self.root/'config.json').write_text(json.dumps(cfg),'utf-8')
        self.s=Store(self.root)
        self.s.check_storage=lambda:None

    def tearDown(self):
        self.s.db.close()
        self.tmp.cleanup()

    def tool(self,name,args,actor='biao'):
        with self.s.identity.bind(actor), patch.object(self.s,'bridge_action',return_value={'ok':True}):
            return self.s.mcp({'id':1,'method':'tools/call','params':{'name':name,'arguments':args}})['result']

    def receipt(self,actor='biao',task='fixture-original'):
        with self.s.identity.bind(actor):
            self.s.identity.log('desktop.click','合成测试动作回执，非真实设备',task,'session-fixture-'+actor)
        return self.s.db.execute('SELECT id FROM activities ORDER BY rowid DESC LIMIT 1').fetchone()[0]

    def test_cross_mode_recall_keeps_actor_recorder_source_and_time_unchanged(self):
        action=self.receipt('biao')
        saved=self.s.add_memory({'kind':'experience','text':'表完成合成整理任务','action_id':action},'assistant:合成动作回执')
        before=dict(self.s.db.execute('SELECT * FROM memory WHERE id=?',(saved['id'],)).fetchone())
        with self.s.identity.bind('li'):
            row=self.s.memories('合成整理')[0]
        self.assertEqual((row['performed_by'],row['recorded_by']),('biao','biao'))
        self.assertEqual((row['action_id'],row['task_id']),(action,'fixture-original'))
        self.assertEqual((row['source'],row['time']),(before['source'],before['time']))
        self.assertEqual(dict(self.s.db.execute('SELECT * FROM memory WHERE id=?',(saved['id'],)).fetchone()),before)

    def test_recording_owner_fact_does_not_turn_it_into_recorder_experience(self):
        saved=self.s.add_memory({'kind':'preference','text':'主人喜欢蓝色','recorded_by':'li',
                                 'time':'forged','source':'forged'})
        row=self.s.memories(memory_id=saved['id'])[0]
        self.assertEqual(row['performed_by'],'unknown')
        self.assertEqual(row['recorded_by'],'biao')
        self.assertEqual(row['source'],'owner')
        self.assertNotEqual(row['time'],'forged')

    def test_selective_filters_are_optional_and_work_from_either_mode(self):
        a=self.s.add_memory({'kind':'project','text':'主人提供项目蓝图','performed_by':'owner'})
        with self.s.identity.bind('li'):
            b=self.s.add_memory({'kind':'project','text':'外部提供项目参考','performed_by':'external'})
            self.assertEqual({x['id'] for x in self.s.memories('项目')},{a['id'],b['id']})
            self.assertEqual([x['id'] for x in self.s.memories('项目',recorded_by='biao')],[a['id']])
        self.assertEqual([x['id'] for x in self.s.memories(performed_by='external',kind='project')],[b['id']])
        self.assertEqual(len(self.s.memories(source_query='owner')),2)

    def test_recall_can_find_older_relevant_memory_without_loading_whole_pool(self):
        wanted=self.s.add_memory({'kind':'project','text':'彗星轨道方案由主人提供'})
        for i in range(110):
            self.s.add_memory({'kind':'interest','text':'无关话题编号'+str(i)})
        rows=self.s.memories('彗星轨道',limit=3,max_chars=420)
        self.assertEqual([r['id'] for r in rows],[wanted['id']])
        self.assertEqual(self.s.memories('不存在的目标'),[])

    def test_literal_wildcards_do_not_force_full_pool_recall(self):
        wanted=self.s.add_memory({'kind':'project','text':'进度50%'})
        self.s.add_memory({'kind':'project','text':'无关条目'})
        self.assertEqual([x['id'] for x in self.s.memories('%')],[wanted['id']])

    def test_recorder_is_server_bound_even_when_owner_view_is_other_mode(self):
        self.s.command('persona',{'persona':'li'})
        result=self.tool('memory_remember',{'kind':'project','text':'来自表会话的笔记','source_note':'合成会话'},'biao')
        self.assertFalse(result.get('isError',False))
        row=self.s.memories('来自表会话')[0]
        self.assertEqual(row['recorded_by'],'biao')
        self.assertTrue(self.tool('memory_remember',{'kind':'project','text':'不能伪造记录者',
                        'source_note':'合成会话','recorded_by':'li'},'biao').get('isError'))

    def test_model_performer_claim_needs_matching_action_not_recorder_mode(self):
        with self.assertRaises(Rejected):
            self.s.add_memory({'kind':'experience','text':'没有回执的完成声称','performed_by':'biao'},'assistant:声称')
        action=self.receipt('biao')
        with self.assertRaises(Rejected):
            self.s.add_memory({'kind':'experience','text':'归属冲突','performed_by':'li','action_id':action},'assistant:回执')
        with self.s.identity.bind('li'):
            saved=self.s.add_memory({'kind':'experience','text':'读取表的合成行动','action_id':action},'assistant:引用表回执')
        self.assertEqual((saved['performed_by'],saved['recorded_by']),('biao','li'))

    def test_continuation_keeps_original_and_next_actor_as_separate_records(self):
        a=self.receipt('biao','fixture-stage-one')
        first=self.s.add_memory({'kind':'experience','text':'表完成第一步','action_id':a},'assistant:第一步')
        b=self.receipt('li','fixture-stage-two')
        with self.s.identity.bind('li'):
            second=self.s.add_memory({'kind':'experience','text':'里接着完成第二步','action_id':b},'assistant:接续第一步')
        self.assertEqual(first['performed_by'],'biao')
        self.assertEqual(second['performed_by'],'li')
        self.assertEqual(len(self.s.memories(kind='experience')),2)

    def test_cross_mode_revision_preserves_original_and_inherits_execution_origin(self):
        action=self.receipt('biao')
        original=self.s.add_memory({'kind':'experience','text':'表的原始行动记录','action_id':action},'assistant:原始回执')
        raw=dict(self.s.db.execute('SELECT * FROM memory WHERE id=?',(original['id'],)).fetchone())
        with self.s.identity.bind('li'):
            revised=self.s.add_memory({'kind':'experience','text':'里补充说明，仍是表做的',
                'supersedes':original['id']},'assistant:补充说明')
        self.assertEqual((revised['recorded_by'],revised['performed_by']),('li','biao'))
        self.assertEqual(revised['action_id'],action)
        self.assertEqual(dict(self.s.db.execute('SELECT * FROM memory WHERE id=?',(original['id'],)).fetchone()),raw)
        self.assertEqual([x['id'] for x in self.s.memories()],[revised['id']])
        self.assertEqual(len(self.s.memories(include_history=True)),2)
        self.assertEqual(self.s.memories(memory_id=original['id'])[0]['source'],raw['source'])
        with self.assertRaises(Rejected):
            self.s.add_memory({'kind':'experience','text':'过期修订','supersedes':original['id']})

    def test_revising_does_not_allow_silent_performer_reassignment(self):
        old=self.s.add_memory({'kind':'experience','text':'主人确认表做过','performed_by':'biao'})
        with self.s.identity.bind('li'):
            with self.assertRaises(Rejected):
                self.s.add_memory({'kind':'experience','text':'不能冒领','performed_by':'li',
                                  'supersedes':old['id']},'assistant:无新回执')

    def test_owner_can_explicitly_correct_attribution_with_prior_version_retained(self):
        action=self.receipt('biao')
        old=self.s.add_memory({'kind':'experience','text':'待主人纠正的记录','action_id':action})
        new=self.s.add_memory({'kind':'experience','text':'主人确认实际是外部做的','performed_by':'external',
                               'supersedes':old['id']})
        self.assertEqual(new['performed_by'],'external')
        self.assertIsNone(new['action_id'])
        self.assertEqual(self.s.memories(memory_id=old['id'])[0]['performed_by'],'biao')

    def test_prompt_does_not_automatically_inject_old_memory_bodies(self):
        self.s.add_memory({'kind':'project','text':'星图项目'*400,'performed_by':'owner'})
        with self.s.identity.bind('li'):
            prompt=prompt_with_memory(self.s,'请继续星图项目')
        self.assertEqual(prompt,'请继续星图项目')
        saved=self.s.memories(query='星图项目')[0]
        self.assertEqual((saved['recorded_by'],saved['performed_by']),('biao','owner'))
        self.assertEqual(prompt_with_memory(self.s,'完全不相关'),'完全不相关')

    def test_tool_recall_filters_and_budgets_and_returns_original_attribution(self):
        self.s.add_memory({'kind':'project','text':'表记录的主人任务','performed_by':'owner'})
        result=self.tool('memory_recall',{'query':'任务','recorded_by':'biao','performed_by':'owner',
                          'limit':1,'max_chars':4},'li')
        self.assertFalse(result.get('isError',False))
        rows=json.loads(result['content'][0]['text'])
        self.assertEqual(len(rows[0]['text']),4)
        self.assertEqual(rows[0]['recorded_by'],'biao')
        for invalid in ({'limit':0},{'max_chars':24001},{'recorded_by':'bad'},{'persona':'biao'}):
            self.assertTrue(self.tool('memory_recall',invalid).get('isError'))

    def test_memory_count_is_common_and_latest_revisions_only(self):
        first=self.s.add_memory({'kind':'preference','text':'原约定'})
        with self.s.identity.bind('li'):
            self.s.add_memory({'kind':'preference','text':'修订约定','supersedes':first['id']})
        with patch.object(self.s,'observer_status',return_value={}):
            self.assertEqual(self.s.status()['memory_count'],1)

    def test_legacy_migration_preserves_all_original_columns_and_never_guesses_performer(self):
        self.s.db.close()
        db_path=self.root/'data/space.sqlite'
        db_path.unlink()
        db=sqlite3.connect(db_path)
        db.execute('CREATE TABLE memory(id TEXT PRIMARY KEY,kind TEXT,text TEXT,source TEXT,time TEXT,supersedes TEXT,persona TEXT)')
        legacy=[('old-biao','experience','旧表记录','legacy-source','2020-01-01',None,'biao'),
                ('old-shared','project','旧共享记录','owner','2020-01-02',None,'shared')]
        db.executemany('INSERT INTO memory VALUES(?,?,?,?,?,?,?)',legacy);db.commit();db.close()
        self.s=Store(self.root)
        self.s.check_storage=lambda:None
        rows=list(self.s.db.execute('SELECT id,kind,text,source,time,supersedes,persona FROM memory ORDER BY id'))
        self.assertEqual([tuple(r) for r in rows],legacy)
        by_id={x['id']:x for x in self.s.memories()}
        self.assertEqual(by_id['old-biao']['recorded_by'],'biao')
        self.assertEqual(by_id['old-shared']['recorded_by'],'unknown')
        self.assertEqual({x['performed_by'] for x in by_id.values()},{'unknown'})
        with self.s.identity.bind('li'):
            self.assertEqual(len(self.s.memories()),2)
        new=self.s.add_memory({'kind':'experience','text':'新主人确认记录','performed_by':'owner'})
        self.s.db.close();self.s=Store(self.root)
        self.assertEqual(self.s.memories(memory_id=new['id'])[0]['performed_by'],'owner')


if __name__=='__main__':
    unittest.main(verbosity=2)
