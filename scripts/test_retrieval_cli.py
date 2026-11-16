import contextlib
import io
import unittest
from unittest.mock import patch

from retrieval_cli import evaluate
from web.knowledge import Knowledge
from web.test_web import Fixture


class RetrievalEvaluationTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        Knowledge(self.store).save('alpha', {'id': 'manual', 'title': '示例资料', 'content': '每季度检修设备。'})
        self.case = {'id': 'frequency', 'industry': 'alpha', 'query': '保养频率',
                     'relevant': [{'material_id': 'manual', 'quote': '每季度检修设备'}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_overlap_does_not_inflate_ndcg(self):
        records = [{'id': 'manual', 'industry_id': 'alpha', 'retrieval_method': 'hybrid_rrf',
                    'snippets': [{'text': '每季度检修设备。', 'citation': 'manual#chars-1-8', 'retrieval_score': 1}] * 4}]
        with patch('retrieval_cli.MaterialIndex.search', return_value=records), contextlib.redirect_stdout(io.StringIO()):
            result = evaluate(self.store, [self.case], ['hybrid'])
        self.assertEqual(result['summary']['hybrid']['ndcg_at_6'], 1)
        self.assertEqual(result['summary']['hybrid']['recall_at_6'], 1)

    def test_invalid_gold_never_runs_retrieval(self):
        self.case['relevant'][0]['quote'] = '原文没有的句子'
        with patch('retrieval_cli.MaterialIndex.search') as search:
            with self.assertRaises(ValueError):
                evaluate(self.store, [self.case], ['hybrid'])
        search.assert_not_called()

    def test_cross_industry_gold_requires_explicit_industry(self):
        self.case['industry'] = 'all'
        with self.assertRaises(ValueError):
            evaluate(self.store, [self.case], ['hybrid'])

    def test_fallback_is_reported_and_not_counted_as_successful_semantic_method(self):
        records = [{'id': 'manual', 'industry_id': 'alpha', 'retrieval_method': 'lexical_fallback',
                    'snippets': [{'text': '每季度检修设备。', 'citation': 'manual#chars-1-8'}]}]
        with patch('retrieval_cli.MaterialIndex.search', return_value=records), contextlib.redirect_stdout(io.StringIO()):
            result = evaluate(self.store, [self.case], ['hybrid'])
        self.assertEqual(result['summary']['hybrid']['fallbacks'], 1)


if __name__ == '__main__':
    unittest.main()
