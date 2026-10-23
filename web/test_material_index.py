import asyncio
import copy
import unittest

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from .agent_runtime import AgentRuntime, ResearchContext
from .knowledge import Knowledge
from .material_index import CHUNK_SIZE, OVERLAP, MaterialIndex
from .service import kb
from .test_web import Fixture
from .verification import VerificationQueue


class MaterialIndexTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.index = MaterialIndex(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def material(self, **changes):
        text = ('前文普通内容。' * 650) + '尾章独有赤松密度参数为每亩七十株。'
        record = self.knowledge.save('alpha', {'id': 'full-plan', 'title': '项目资料', 'content': text,
            'model_allowed': True, **changes})
        queue = VerificationQueue(self.store)
        review = queue.prepare_material('alpha', record['id'])
        queue.approve_material('alpha', review['id'], {'decision': 'approve'})
        return self.knowledge.get('alpha', record['id'])

    def test_hard_chunks_cover_every_character_and_tail_without_model_or_new_facts(self):
        record = self.material()
        before = self.store.evidence('alpha')
        result = self.index.build('alpha', record['id'])
        self.assertEqual(result['mode'], 'hard_chunk_index')
        self.assertEqual(result['usage']['requests'], 0)
        with self.index.connect('alpha') as db:
            rows = db.execute('SELECT start,stop FROM chunks ORDER BY start').fetchall()
        self.assertEqual(rows[0]['start'], 0)
        self.assertEqual(rows[-1]['stop'], len(record['content']))
        for left, right in zip(rows, rows[1:]):
            self.assertEqual(left['stop'] - right['start'], OVERLAP)
            self.assertLessEqual(left['stop']-left['start'], CHUNK_SIZE)
        found = self.index.search('alpha', '赤松密度')
        self.assertTrue(any('七十株' in s['text'] for r in found for s in r['snippets']))
        for r in found:
            for s in r['snippets']:
                self.assertTrue(kb.material_quote_matches(self.store.home('alpha'), record,
                    {'citation': s['citation'], 'quote': s['text']}))
        self.assertEqual(before, self.store.evidence('alpha'))

    def test_permissions_and_version_changes_take_effect_with_existing_index(self):
        record = self.material()
        self.index.build('alpha', record['id'])
        self.knowledge.save('alpha', {'id': record['id'], 'model_allowed': False})
        self.assertEqual(self.index.search('alpha', '赤松密度'), [])
        self.knowledge.save('alpha', {'id': record['id'], 'model_allowed': True, 'content': '新版只有杉木株距。'})
        self.assertEqual(self.index.latest('alpha', record['id'])['status'], 'invalidated')
        self.assertEqual(self.index.search('alpha', '赤松密度'), [])
        self.assertIn('杉木', self.index.search('alpha', '杉木株距')[0]['snippets'][0]['text'])

    def test_no_model_permission_or_key_needed_to_index_approved_material(self):
        record = self.material(model_allowed=False)
        self.assertEqual(self.index.build('alpha', record['id'])['status'], 'completed')
        self.assertEqual(self.index.search('alpha', '赤松'), [])

    def test_model_rerank_only_reorders_exact_source_and_rechecks_permission(self):
        record = self.material()
        records = self.index.search('alpha', '普通内容')
        runtime = AgentRuntime(self.store)
        original = copy.deepcopy(records)
        def respond(messages, info):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {'indices': [1, 0]})])
        ranked = asyncio.run(runtime.rerank_materials('普通内容', records, model_override=FunctionModel(respond), industry='alpha'))
        self.assertEqual(ranked[0]['ranking_method'], 'model_rerank')
        self.assertEqual(ranked[0]['snippets'][0]['text'], original[0]['snippets'][1]['text'])
        self.assertEqual(ranked[0]['snippets'][0]['citation'], original[0]['snippets'][1]['citation'])
        def revoke(messages, info):
            self.knowledge.save('alpha', {'id': record['id'], 'model_allowed': False})
            return respond(messages, info)
        self.assertEqual(asyncio.run(runtime.rerank_materials('普通内容', records,
            model_override=FunctionModel(revoke), industry='alpha')), [])

    def test_invalid_rerank_ids_fall_back_without_fabricating_source(self):
        self.material()
        records = self.index.search('alpha', '赤松')
        def respond(messages, info):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {'indices': [999]})])
        result = asyncio.run(AgentRuntime(self.store).rerank_materials('赤松', records, model_override=FunctionModel(respond)))
        self.assertEqual(result[0]['ranking_method'], 'local_rank_fallback')
        self.assertEqual(result[0]['snippets'][0]['text'], records[0]['snippets'][0]['text'])

    def test_ranking_preview_is_a_literal_window_and_finds_tail_match(self):
        from .agent_runtime import ranking_preview, MaterialRanking
        text = '普通开头。' * 200 + '尾部独有的赤松密度要求为七十株。'
        preview = ranking_preview('赤松密度', text)
        self.assertIn(preview, text)
        self.assertIn('赤松密度', preview)
        self.assertLessEqual(len(preview), 320)
        self.assertEqual(MaterialRanking.model_validate({'indices': '[1,0]'}).indices, [1, 0])

    def test_failed_rebuild_rolls_back_deleted_chunks_and_keeps_previous_full_index(self):
        import sqlite3
        record = self.material()
        before = self.index.build('alpha', record['id'])
        with self.index.connect('alpha') as db:
            count = db.execute('SELECT count(*) FROM chunks').fetchone()[0]
            db.execute("CREATE TRIGGER fail_rebuild BEFORE INSERT ON chunks WHEN new.start>0 "
                "BEGIN SELECT raise(ABORT,'synthetic disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.build('alpha', record['id'], force=True)
        self.assertEqual(self.index.latest('alpha', record['id']), before)
        with self.index.connect('alpha') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chunks').fetchone()[0], count)
        self.assertIn('七十株', self.index.search('alpha', '赤松密度')[0]['snippets'][0]['text'])

    def test_direct_document_answer_uses_retrieved_full_chunk_and_real_citation(self):
        from unittest.mock import AsyncMock, patch
        from pydantic_ai.messages import TextPart
        self.material()
        runtime = AgentRuntime(self.store)
        records = self.index.search('alpha', '赤松密度')
        def respond(messages, info):
            self.assertFalse(info.function_tools)
            self.assertIn('七十株', str(messages))
            return ModelResponse(parts=[TextPart('资料记载赤松密度为每亩七十株。[M1]')])
        with patch.object(runtime, 'rerank_materials', new=AsyncMock(return_value=records)):
            result = asyncio.run(runtime.answer_material_question('赤松密度', ResearchContext(self.store, 'alpha'),
                runtime.config(), answer_model=FunctionModel(respond)))
        self.assertIn('每亩七十株', result['text'])
        self.assertIn(records[0]['snippets'][0]['citation'], result['text'])
        self.assertEqual(len(self.store.evidence('alpha')), 1)
