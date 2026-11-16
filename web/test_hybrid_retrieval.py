import asyncio
import copy
import math
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from .agent_runtime import AgentRuntime
from .knowledge import Knowledge
from .material_index import MaterialIndex
from .retrieval import LocalModels, local_url, normalized_vector, rrf, settings
from .server import create_app
from .service import kb, ws
from .test_web import Fixture


class HybridRetrievalTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.index = MaterialIndex(self.store)
        self.cfg = {**settings(self.root), 'enabled': True,
            'embedding': {'model': 'test', 'revision': 'v1', 'dimension': 2},
            'reranker': {'model': 'test-rank'}}
        kb.atomic_write(self.root / 'config/retrieval.json', kb.json_text(self.cfg))
        self.seen = []
        def encode(backend, texts, query=False):
            self.seen.extend(texts)
            return [[1.0, 0.0] if '检修' in text or '保养' in text else [0.0, 1.0] for text in texts]
        self.encoder = patch.object(LocalModels, 'embed', encode)
        self.encoder.start()
        self.addCleanup(self.encoder.stop)

    def tearDown(self):
        self.temp.cleanup()

    def material(self, key='manual', text='每季度检修设备，操作须断电。', industry='alpha', **values):
        return self.knowledge.save(industry, {'id': key, 'title': key, 'content': text,
                                             'model_allowed': True, **values})

    def test_semantic_branch_bypasses_literal_prefilter_and_preserves_exact_quotes(self):
        self.material()
        self.material('menu', '食堂今天提供面条。')
        self.assertEqual(self.index.search('alpha', '保养频率', retrieval_mode='lexical'), [])
        result = self.index.search('alpha', '保养频率')
        self.assertEqual(result[0]['id'], 'manual')
        self.assertEqual(result[0]['retrieval_method'], 'hybrid_rrf')
        source = self.knowledge.get('alpha', 'manual')
        self.assertTrue(kb.material_quote_matches(self.store.home('alpha'), source, {
            'citation': result[0]['snippets'][0]['citation'], 'quote': result[0]['snippets'][0]['text']}))

    def test_catalog_search_includes_semantic_hits_without_reapplying_literal_filter(self):
        self.material()
        with TestClient(create_app(self.root)) as client:
            found = client.get('/api/materials', params={'industry': 'alpha', 'q': '保养频率'}).json()
        self.assertEqual(found[0]['id'], 'manual')
        self.assertEqual(found[0]['retrieval_method'], 'hybrid_rrf')

    def test_non_latin_query_can_use_vector_retrieval_without_fts_tokens(self):
        self.material()
        found = self.index.search('alpha', 'обслуживание', retrieval_mode='dense')
        self.assertEqual(found[0]['retrieval_method'], 'dense_vector')
        self.assertIn('обслуживание', self.seen)

    def test_vectors_cached_and_encoder_revision_change_requires_reencoding(self):
        self.material()
        self.index.search('alpha', '保养')
        self.seen.clear()
        self.index.search('alpha', '保养')
        self.assertEqual(self.seen, ['保养'])
        self.cfg['embedding']['revision'] = 'v2'
        kb.atomic_write(self.root / 'config/retrieval.json', kb.json_text(self.cfg))
        self.index.search('alpha', '保养')
        self.assertTrue(any('每季度' in text for text in self.seen))

    def test_revoked_and_restricted_materials_are_never_encoded_or_returned(self):
        self.material()
        self.index.search('alpha', '保养')
        self.knowledge.save('alpha', {'id': 'manual', 'model_allowed': False})
        self.material('restricted', '禁止发送的正文', sensitivity='restricted', model_allowed=False)
        self.seen.clear()
        self.assertEqual(self.index.search('alpha', '保养'), [])
        self.assertFalse(any('正文' in text or '每季度' in text for text in self.seen))

    def test_version_change_drops_old_vectors_and_only_returns_current_text(self):
        self.material()
        self.index.search('alpha', '保养')
        self.knowledge.save('alpha', {'id': 'manual', 'content': '新版改为每月检测。'})
        result = self.index.search('alpha', '每月检测', retrieval_mode='dense')
        self.assertIn('新版', result[0]['snippets'][0]['text'])
        with self.index.connect('alpha') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chunk_vectors').fetchone()[0], 1)

    def test_single_industry_and_explicit_material_scope_filter_before_encoding(self):
        ws.industry_command(self.root, self.store.args(action='add', id='beta', name='其他行业', region='示例地区', categories=None))
        self.material('foreign', '跨行业秘密正文', industry='beta')
        self.material()
        self.material('unselected', '未选中的秘密正文')
        found = self.index.search('alpha', '保养', material_scope=[{'industry_id': 'alpha', 'id': 'manual'}])
        self.assertEqual([r['id'] for r in found], ['manual'])
        self.assertFalse(any('秘密正文' in text for text in self.seen))

    def test_permission_change_during_encoding_stops_remaining_batches_and_commit(self):
        self.material(text='每季度检修设备。' * 1100)
        original = LocalModels.embed
        def revoke(backend, texts, query=False):
            values = original(backend, texts, query)
            if not query:
                self.knowledge.save('alpha', {'id': 'manual', 'model_allowed': False})
            return values
        with patch.object(LocalModels, 'embed', revoke):
            self.assertEqual(self.index.search('alpha', '保养'), [])
        with self.index.connect('alpha') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chunk_vectors').fetchone()[0], 0)
        self.assertEqual(len(self.seen), 4)

    def test_unavailable_encoder_has_explicit_lexical_fallback(self):
        self.material()
        with patch.object(LocalModels, 'embed', side_effect=OSError('unavailable')):
            found = self.index.search('alpha', '检修')
        self.assertEqual(found[0]['retrieval_method'], 'lexical_fallback')
        self.assertIn('关键词', found[0]['retrieval_note'])

    def test_dedicated_ranker_gets_full_chunks_and_context_is_verbatim(self):
        self.material(text='安全须知。' * 200 + '每季度检修设备，操作须断电。' + '附录。' * 300)
        records = self.index.search('alpha', '检修')
        def rank(backend, query, texts):
            self.assertTrue(any(len(text) > 320 for text in texts))
            return [{'index': n, 'relevance_score': 1 / (n + 1)} for n in range(len(texts))]
        with patch.object(LocalModels, 'rank', rank):
            result = asyncio.run(AgentRuntime(self.store).rerank_materials('检修', records, industry='alpha'))
        self.assertEqual(result[0]['ranking_method'], 'dedicated_rerank')
        source = self.knowledge.get('alpha', 'manual')
        for snippet in result[0]['snippets']:
            self.assertTrue(kb.material_quote_matches(self.store.home('alpha'), source,
                {'citation': snippet['citation'], 'quote': snippet['text']}))
            self.assertLessEqual(len(snippet['text']), 2200)

    def test_ranker_failure_keeps_fusion_order_and_never_uses_paid_model(self):
        self.material()
        records = self.index.search('alpha', '检修')
        with patch.object(LocalModels, 'rank', side_effect=OSError('offline')), patch('web.agent_runtime.AsyncOpenAI') as paid:
            result = asyncio.run(AgentRuntime(self.store).rerank_materials('检修', records, industry='alpha'))
        paid.assert_not_called()
        self.assertEqual(result[0]['ranking_method'], 'local_rank_fallback')

    def test_ranker_revocation_does_not_return_cached_context(self):
        self.material()
        records = self.index.search('alpha', '检修')
        def revoke(*args):
            self.knowledge.save('alpha', {'id': 'manual', 'model_allowed': False})
            return [{'index': 0, 'relevance_score': 1}]
        with patch.object(LocalModels, 'rank', revoke):
            self.assertEqual(asyncio.run(AgentRuntime(self.store).rerank_materials('检修', records, industry='alpha')), [])

    def test_invalid_vectors_and_non_loopback_endpoints_rejected(self):
        for vector in ([0, 0], [math.nan, 1], [math.inf, 0], ['x'], []):
            with self.assertRaises(ValueError):
                normalized_vector(vector)
        for url in ('https://example.org', 'http://localhost:8767', 'http://127.0.0.1:8767@evil.org:80',
                    'http://127.0.0.1:8767/path', 'http://127.0.0.1:8767?token=bad'):
            with self.assertRaises(ValueError):
                local_url(url)

    def test_rrf_uses_rank_not_score_scales_and_deduplicates(self):
        a, b = ('alpha', {'id': 1, 'rank': -999}), ('alpha', {'id': 2, 'rank': -1})
        found = rrf([[a, b, a], [b]])
        self.assertEqual(found[0][1]['id'], 2)
        self.assertAlmostEqual(found[0][1]['fusion_score'], 1/62 + 1/61)

    def test_failed_rebuild_keeps_vectors_transactionally(self):
        import sqlite3
        self.material(text='每季度检修。' * 500)
        self.index.search('alpha', '检修')
        with self.index.connect('alpha') as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM chunk_vectors ORDER BY chunk_id')]
            db.execute("CREATE TRIGGER abort_rebuild BEFORE INSERT ON chunks WHEN new.start>0 BEGIN SELECT raise(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.build('alpha', 'manual', force=True)
        with self.index.connect('alpha') as db:
            self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM chunk_vectors ORDER BY chunk_id')])


if __name__ == '__main__':
    unittest.main()
