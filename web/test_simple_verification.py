"""One material approval builds a local complete index, without model extraction."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from .knowledge import Knowledge
from .server import create_app
from .service import kb
from .test_web import Fixture
from .verification import VerificationQueue


class SimpleVerificationTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.queue = VerificationQueue(self.store)
        self.app = create_app(self.root)
        self.knowledge.save('alpha', {'id': 'local-plan', 'title': '县级造林方案', 'kind': 'project',
            'region': '示例省示例县', 'model_allowed': True,
            'content': '本方案计划开展造林。项目是否批复，尚待取得正式依据。'})
        self.client = TestClient(self.app, headers={'X-KB-Request': '1'})
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def analysis(self):
        context = self.queue.material_context('alpha', 'local-plan')
        return {'extraction_method': 'model', 'coverage_note': '仅分析核验预览片段。', 'candidates': [{
            'title': '核验抽样候选（不能当全文结果）', 'summary': '资料记载拟开展造林，批复情况未知。',
            'quote': '本方案计划开展造林。', 'citation': context['excerpts'][0]['citation'],
            'status': 'planned', 'category': '企业与竞争', 'region': '示例省示例县'}]}

    def review(self, mode='manual'):
        return self.queue.prepare_material('alpha', 'local-plan', mode,
            self.analysis() if mode == 'automatic' else None)

    @staticmethod
    def url(review):
        return '/api/verifications/alpha/' + review['id'] + '/decision'

    def test_approval_is_saved_before_index_without_generating_facts(self):
        review = self.review()
        original = self.app.state.material_index.build
        def build(*args, **kwargs):
            self.assertEqual(self.knowledge.get('alpha', 'local-plan')['verification']['status'], 'reviewed')
            self.assertEqual(self.queue.get('alpha', review['id'])['status'], 'approved')
            return original(*args, **kwargs)
        with patch.object(self.app.state.material_index, 'build', side_effect=build), patch(
                'web.full_extraction.extract_batch', new=AsyncMock()) as model:
            response = self.client.post(self.url(review), json={'decision': 'approve'})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertTrue(result['material_verified'])
            self.assertEqual(result['extraction_status'], 'indexed')
            self.assertEqual(result['extraction_job']['usage']['requests'], 0)
            again = self.client.post(self.url(review), json={'decision': 'approve'}).json()
            self.assertEqual(again['extraction_job']['id'], result['extraction_job']['id'])
            model.assert_not_called()
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        self.assertEqual(self.queue.get('alpha', review['id'])['extraction_status'], 'indexed')

    def test_model_failure_does_not_affect_index_or_rebuild(self):
        review = self.review()
        with patch('web.full_extraction.extract_batch', new=AsyncMock(side_effect=ValueError('模型不可用'))) as model:
            first = self.client.post(self.url(review), json={'decision': 'approve'}).json()
            rebuilt = self.client.post('/api/materials/alpha/local-plan/index', json={})
            self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
            self.assertEqual(rebuilt.json()['id'], first['extraction_job']['id'])
            self.assertEqual(rebuilt.json()['status'], 'completed')
            model.assert_not_called()
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    def test_index_write_failure_keeps_approval_original_and_evidence_for_retry(self):
        review = self.review()
        original = self.knowledge.get('alpha', 'local-plan')['content']
        evidence = self.store.evidence('alpha')
        with patch.object(self.app.state.material_index, 'connect', side_effect=OSError('模拟索引不可写')):
            result = self.client.post(self.url(review), json={'decision': 'approve'})
        self.assertEqual(result.status_code, 409)
        material = self.knowledge.get('alpha', 'local-plan')
        self.assertEqual(material['verification']['status'], 'reviewed')
        self.assertEqual(material['content'], original)
        self.assertEqual(self.store.evidence('alpha'), evidence)
        retried = self.client.post('/api/materials/alpha/local-plan/index', json={})
        self.assertEqual(retried.status_code, 200, retried.text)

    def test_automatic_preview_does_not_replace_full_source_index(self):
        review = self.review('automatic')
        result = self.client.post(self.url(review), json={'decision': 'approve'})
        self.assertEqual(result.status_code, 200, result.text)
        job = result.json()['extraction_job']
        text = self.knowledge.get('alpha', 'local-plan')['content']
        self.assertEqual(job['progress']['processed_characters'], len(text))
        self.assertEqual(job['evidence_ids'], [])
        self.assertEqual(job['usage']['requests'], 0)

    def test_old_extraction_failure_cannot_overwrite_completed_index(self):
        first = self.review()
        self.queue.approve_material('alpha', first['id'], {'decision': 'approve'})
        second = self.review('automatic')
        indexed = self.client.post(self.url(second), json={'decision': 'approve'}).json()['extraction_job']
        result = self.queue.extraction_failed('alpha', first['id'], '旧任务失败')
        self.assertEqual(result['extraction_status'], 'indexed')
        self.assertEqual(self.app.state.material_index.latest('alpha', 'local-plan')['id'], indexed['id'])
        self.assertEqual(self.knowledge.get('alpha', 'local-plan')['verification']['review_id'], second['id'])
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    def test_explicit_candidate_client_keeps_compatible_original_workflow(self):
        review = self.review()
        with patch('web.full_extraction.extract_batch', new=AsyncMock()) as model:
            response = self.client.post(self.url(review), json={'decision': 'approve',
                'candidates': self.analysis()['candidates']})
            model.assert_not_called()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['extraction_status'], 'completed')
        self.assertIsNone(self.app.state.extractions.latest('alpha', 'local-plan'))

    def test_source_change_during_index_prevents_stale_index_commit(self):
        from .material_index import tokens
        review = self.review()
        changed = False
        def change(text):
            nonlocal changed
            if not changed:
                changed = True
                self.knowledge.save('alpha', {'id': 'local-plan', 'content': '新版原文另有计划。'})
            return tokens(text)
        with patch('web.material_index.tokens', side_effect=change):
            response = self.client.post(self.url(review), json={'decision': 'approve'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIsNone(self.app.state.material_index.latest('alpha', 'local-plan'))
        self.assertEqual(self.queue.get('alpha', review['id'])['status'], 'invalidated')
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    def test_concurrent_approval_does_not_build_twice(self):
        review = self.review()
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.material_index.build
        def build(*args, **kwargs):
            entered.set()
            release.wait(10)
            return original(*args, **kwargs)
        with patch.object(self.app.state.material_index, 'build', side_effect=build) as indexed:
            with ThreadPoolExecutor(max_workers=2) as callers:
                first = callers.submit(self.client.post, self.url(review), json={'decision': 'approve'})
                try:
                    self.assertTrue(entered.wait(5))
                    repeated = callers.submit(self.client.post, self.url(review), json={'decision': 'approve'}).result(timeout=5)
                    self.assertEqual(repeated.status_code, 200)
                    self.assertEqual(repeated.json()['status'], 'approved')
                finally:
                    release.set()
                result = first.result(timeout=10)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['extraction_status'], 'indexed')
            self.assertEqual(indexed.call_count, 1)
        self.assertEqual(len(self.store.evidence('alpha')), 1)


if __name__ == '__main__':
    unittest.main()
