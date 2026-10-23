"""Isolated confirmation, provenance, privacy and rollback regression coverage."""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from .knowledge import Knowledge
from .server import create_app
from .service import kb, ws
from .test_web import Fixture
from .verification import VerificationQueue


class VerificationTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.queue = VerificationQueue(self.store)
        self.app = create_app(self.root)
        self.client = TestClient(self.app, headers={'X-KB-Request': '1'})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def material(self, key='local-plan', **changes):
        return self.knowledge.save('alpha', {'id': key, 'title': '县级绿化方案', 'kind': 'project',
            'region': '示例省示例县', 'content': '本方案计划开展造林。项目是否批复，尚待取得正式依据。', **changes})

    def analysis(self, key='local-plan'):
        context = self.queue.material_context('alpha', key)
        part = context['excerpts'][0]
        return {'findings': ['计划与批复须区分'], 'coverage_note': '抽样无法判断批复状态。', 'candidates': [{
            'title': '方案中的造林计划', 'summary': '资料记载拟开展造林，批复情况未知。',
            'quote': '本方案计划开展造林。', 'citation': part['citation'], 'status': 'planned',
            'category': self.store.state()['industries'][0]['scope']['topics'][0], 'region': '示例省示例县'}]}

    def approve(self, review, **payload):
        return self.queue.decide('alpha', review['id'], {'decision': 'approve', **payload})

    def test_manual_pending_then_approved_creates_linked_local_evidence(self):
        self.material()
        context = self.queue.material_context('alpha', 'local-plan')
        self.assertNotIn('content', context['material'])
        self.assertLessEqual(sum(len(p['text']) for p in context['excerpts']), 9600)
        review = self.queue.prepare_material('alpha', 'local-plan')
        self.assertEqual(review['status'], 'pending')
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        self.assertEqual(self.knowledge.display(self.knowledge.get('alpha', 'local-plan'))['verification_status'], 'pending')
        result = self.approve(review)
        material = self.knowledge.get('alpha', 'local-plan')
        self.assertEqual(material['status'], 'unknown')
        self.assertIsNone(material['checked_date'])
        self.assertEqual(material['verification']['status'], 'reviewed')
        self.assertEqual(material['related_evidence_ids'], result['evidence_ids'])
        evidence = kb.read_json(self.store.home('alpha') / 'evidence' / (result['evidence_ids'][0] + '.json'))
        self.assertEqual(evidence['source_kind'], 'local_material')
        self.assertEqual(evidence['url'], '')
        self.assertEqual(evidence['material_source']['material_id'], 'local-plan')
        self.assertEqual(evidence['sensitivity'], 'internal')
        self.assertFalse(evidence['model_allowed'])
        self.assertNotIn(evidence['id'], [r['id'] for r in self.store.evidence('alpha', model_only=True)])

    def test_approval_is_idempotent_and_new_review_reuses_fact_id(self):
        self.material()
        review = self.queue.prepare_material('alpha', 'local-plan')
        first = self.approve(review)
        second = self.approve(review)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.evidence('alpha')), 2)
        again = self.queue.prepare_material('alpha', 'local-plan')
        third = self.approve(again)
        self.assertEqual(first['evidence_ids'], third['evidence_ids'])
        self.assertEqual(len(self.store.evidence('alpha')), 2)
        self.assertTrue(list((self.store.home('alpha') / 'history' / first['evidence_ids'][0]).glob('*.json')))

    def test_rejection_does_not_create_evidence_or_claim_validity(self):
        self.material()
        review = self.queue.prepare_material('alpha', 'local-plan')
        result = self.queue.decide('alpha', review['id'], {'decision': 'reject', 'note': '缺少批复依据'})
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        self.assertEqual(self.knowledge.get('alpha', 'local-plan')['verification']['status'], 'unverified')
        self.assertEqual(self.queue.decide('alpha', review['id'], {'decision': 'reject'}), result)
        with self.assertRaises(ValueError):
            self.approve(review)

    def test_rejecting_new_draft_preserves_previous_approved_source_review(self):
        self.material()
        initial = self.approve(self.queue.prepare_material('alpha', 'local-plan'))
        next_review = self.queue.prepare_material('alpha', 'local-plan')
        self.queue.decide('alpha', next_review['id'], {'decision': 'reject'})
        material = self.knowledge.get('alpha', 'local-plan')
        self.assertEqual(material['verification']['status'], 'reviewed')
        self.assertEqual(material['verification']['review_id'], initial['id'])
        self.assertEqual(material['related_evidence_ids'], initial['evidence_ids'])

    def test_automatic_requires_permission_and_exact_quote_and_preserves_limits(self):
        self.material()
        with self.assertRaisesRegex(ValueError, '授权'):
            self.queue.prepare_material('alpha', 'local-plan', 'automatic', self.analysis())
        self.knowledge.save('alpha', {'id': 'local-plan', 'model_allowed': True})
        analysis = self.analysis()
        analysis['candidates'][0]['quote'] = '项目已经完成验收'
        with self.assertRaisesRegex(ValueError, '逐字摘录'):
            self.queue.prepare_material('alpha', 'local-plan', 'automatic', analysis)
        analysis = self.analysis()
        analysis['candidates'][0]['status'] = 'confirmed'
        with self.assertRaisesRegex(ValueError, '不能自动'):
            self.queue.prepare_material('alpha', 'local-plan', 'automatic', analysis)
        review = self.queue.prepare_material('alpha', 'local-plan', 'automatic', self.analysis())
        self.assertIn('抽样无法判断批复状态', review['coverage_note'])
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    def test_restricted_material_supports_manual_review_without_model(self):
        self.material(sensitivity='restricted')
        with self.assertRaises(ValueError):
            self.queue.prepare_material('alpha', 'local-plan', 'automatic', self.analysis())
        result = self.approve(self.queue.prepare_material('alpha', 'local-plan'))
        evidence = kb.read_json(self.store.home('alpha') / 'evidence' / (result['evidence_ids'][0] + '.json'))
        self.assertEqual(evidence['sensitivity'], 'restricted')
        self.assertFalse(evidence['model_allowed'])

    def test_batch_partial_failure_and_automatic_denial_before_model_call(self):
        self.material()
        response = self.client.post('/api/verifications/materials', json={'items': [
            {'industry_id': 'alpha', 'material_id': 'local-plan'},
            {'industry_id': 'alpha', 'material_id': 'missing-material'}], 'mode': 'manual'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['items']), 1)
        self.assertEqual(len(response.json()['errors']), 1)
        with patch.object(self.app.state.runtime, 'review_material', new=AsyncMock()) as model:
            response = self.client.post('/api/verifications/materials', json={'items': [
                {'industry_id': 'alpha', 'material_id': 'local-plan'}], 'mode': 'automatic'})
            self.assertEqual(len(response.json()['errors']), 1)
            model.assert_not_called()
        response = self.client.post('/api/verifications/materials', json={'items': [{}] * 11})
        self.assertEqual(response.status_code, 400)

    def test_automatic_batch_uses_bounded_context_and_user_decision_endpoint(self):
        self.material(model_allowed=True)
        with patch.object(self.app.state.runtime, 'review_material', new=AsyncMock(return_value=self.analysis())) as model:
            response = self.client.post('/api/verifications/materials', json={'items': [
                {'industry_id': 'alpha', 'material_id': 'local-plan'}], 'mode': 'automatic'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['errors'], [])
            supplied = model.call_args.args[0]
            self.assertNotIn('content', supplied['material'])
        review = response.json()['items'][0]
        self.assertEqual(self.client.get('/api/verifications', params={'industry': 'alpha'}).json()[0]['id'], review['id'])
        result = self.client.post('/api/verifications/alpha/' + review['id'] + '/decision', json={'decision': 'approve'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['status'], 'approved')

    def test_changed_source_invalidates_pending_without_get_mutation(self):
        self.material()
        review = self.queue.prepare_material('alpha', 'local-plan')
        self.knowledge.save('alpha', {'id': 'local-plan', 'content': '新版另有造林计划。'})
        path = self.queue.path('alpha', review['id'])
        before = path.read_bytes()
        self.assertEqual(self.queue.get('alpha', review['id'])['status'], 'invalidated')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.queue.list('alpha', 'pending'), [])
        with self.assertRaisesRegex(ValueError, '版本已变化'):
            self.approve(review)
        self.assertEqual(kb.read_json(path)['status'], 'invalidated')
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    def test_new_attachment_clears_old_lifecycle_check_and_links(self):
        original = self.knowledge.import_material('alpha', {'id': 'local-plan', 'title': '方案', 'status': 'current',
            'checked_date': self.record['checked_date'], 'url': 'https://example.org/plan'}, 'v1.txt', '旧版方案原文'.encode())
        result = self.approve(self.queue.prepare_material('alpha', original['id']))
        updated = self.knowledge.upload('alpha', original['id'], 'v2.txt', '新版未核验原文'.encode())
        self.assertEqual(updated['status'], 'unknown')
        self.assertIsNone(updated['checked_date'])
        self.assertEqual(updated['related_evidence_ids'], [])
        self.assertEqual(updated['verification_status'], 'invalidated')
        self.assertNotEqual(updated['attachment']['sha256'], original['attachment']['sha256'])
        self.assertEqual(self.queue.get('alpha', result['id'])['status'], 'invalidated')

    def test_approval_failure_rolls_back_evidence_material_review_and_index(self):
        self.material()
        review = self.queue.prepare_material('alpha', 'local-plan')
        material_path = self.knowledge.folder('alpha') / 'local-plan.json'
        index_path = self.store.home('alpha') / 'data/search.sqlite3'
        before_material, before_index = material_path.read_bytes(), index_path.read_bytes()
        with patch.object(self.queue, '_write', side_effect=OSError('模拟写入失败')):
            with self.assertRaises(OSError):
                self.approve(review)
        self.assertEqual(material_path.read_bytes(), before_material)
        self.assertEqual(index_path.read_bytes(), before_index)
        self.assertEqual(self.queue.get('alpha', review['id'])['status'], 'pending')
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        self.assertEqual(len(self.approve(review)['evidence_ids']), 1)

    def test_empty_material_cannot_be_marked_reviewed(self):
        self.material(content='')
        with self.assertRaisesRegex(ValueError, '没有可核验'):
            self.queue.prepare_material('alpha', 'local-plan')

    def test_public_web_approval_creates_material_and_reuses_existing_evidence(self):
        quote = '企业披露合同，待核对履约进度。'
        page = {'url': self.record['url'], 'title': '公开合同原文', 'text': quote,
            'checked_date': self.record['checked_date']}
        review = self.queue.prepare_web('alpha', {**self.record, 'quote': quote}, page)
        self.assertFalse(self.knowledge.list('alpha'))
        result = self.approve(review)
        self.assertEqual(result['evidence_ids'], [self.record['id']])
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        material = self.knowledge.get('alpha', result['material_id'])
        self.assertEqual(material['related_evidence_ids'], result['evidence_ids'])
        self.assertEqual(material['verification']['status'], 'reviewed')
        evidence = kb.read_json(self.store.home('alpha') / 'evidence' / (self.record['id'] + '.json'))
        self.assertEqual(evidence['url'], page['url'])
        self.assertEqual(evidence['related_material']['material_id'], material['id'])
        self.assertTrue(list((self.store.home('alpha') / 'history' / self.record['id']).glob('*.json')))
        next_review = self.queue.prepare_web('alpha', {**self.record, 'quote': quote}, page)
        next_result = self.approve(next_review)
        self.assertEqual(next_result['material_id'], result['material_id'])
        self.assertEqual(len(self.knowledge.list('alpha')), 1)

    def test_public_web_cannot_overwrite_another_source_id(self):
        record = {**self.record, 'url': 'https://another.example.org/report', 'quote': '公开原文内容'}
        page = {'url': record['url'], 'title': '原文', 'text': record['quote'], 'checked_date': self.record['checked_date']}
        review = self.queue.prepare_web('alpha', record, page)
        with self.assertRaisesRegex(ValueError, '其他来源'):
            self.approve(review)
        self.assertFalse(self.knowledge.list('alpha'))
        self.assertEqual(self.queue.get('alpha', review['id'])['status'], 'pending')

    def test_queue_includes_shared_national_reviews_but_not_other_regions(self):
        for key, region in (('national', '全国'), ('region-local', '本地区'), ('region-other', '其他地区')):
            result = self.client.post('/api/industries', json={'id': key, 'name': region + '林草',
                'region': region, 'specialty': 'forestry'})
            self.assertEqual(result.status_code, 200, result.text)
        self.store.save_scope('national', {'library_role': 'national-foundation'})
        for industry in ('national', 'region-other'):
            self.knowledge.save(industry, {'id': 'regional-doc', 'title': '范围测试资料', 'content': '资料中的计划内容。'})
            self.queue.prepare_material(industry, 'regional-doc')
        reviews = self.queue.list('region-local')
        self.assertEqual([review['industry_id'] for review in reviews], ['national'])
        self.queue.decide('national', reviews[0]['id'], {'decision': 'approve'})
        self.assertEqual(len(self.store.evidence('region-local')), 0)
        self.assertEqual(len(self.store.evidence('national')), 1)

    def test_public_web_approval_rechecks_latest_source_policy(self):
        quote = self.record['summary']
        page = {'url': self.record['url'], 'title': '公开原文', 'text': quote, 'checked_date': self.record['checked_date']}
        review = self.queue.prepare_web('alpha', {**self.record, 'quote': quote}, page)
        self.store.save_sources('alpha', {'mode': 'only', 'sources': []})
        with self.assertRaisesRegex(ValueError, '允许范围'):
            self.approve(review)
        self.assertEqual(self.queue.get('alpha', review['id'])['status'], 'pending')
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        self.assertFalse(self.knowledge.list('alpha'))

    def test_older_web_queue_cannot_overwrite_a_newer_approved_material(self):
        page = {'url': self.record['url'], 'title': '公开原文', 'text': self.record['summary'],
            'checked_date': self.record['checked_date']}
        older = self.queue.prepare_web('alpha', {**self.record, 'quote': page['text']}, page)
        newer_page = {**page, 'text': page['text'] + '\n后续补充了新的履约信息。'}
        newer = self.queue.prepare_web('alpha', {**self.record, 'quote': page['text']}, newer_page)
        self.approve(newer)
        self.assertEqual(self.queue.get('alpha', older['id'])['status'], 'invalidated')
        with self.assertRaisesRegex(ValueError, '版本已变化'):
            self.approve(older)

    def test_same_public_page_multiple_facts_can_be_approved_in_sequence(self):
        quote = self.record['summary']
        second_quote = '该来源还披露了另一项后续计划。'
        page = {'url': self.record['url'], 'title': '公开原文', 'text': quote + '中间背景。' * 1000 + second_quote,
            'checked_date': self.record['checked_date']}
        first = self.queue.prepare_web('alpha', {**self.record, 'quote': quote}, page)
        second = self.queue.prepare_web('alpha', {**self.record, 'id': 'another-fact', 'title': '同页另一事实', 'quote': second_quote}, page)
        first_result = self.approve(first)
        self.assertEqual(self.queue.get('alpha', second['id'])['status'], 'pending')
        self.approve(second)
        self.assertEqual(len(self.store.evidence('alpha')), 2)
        material = self.knowledge.get('alpha', first_result['material_id'])
        self.assertEqual(set(material['related_evidence_ids']), {'test-contract', 'another-fact'})
        self.assertIn(quote, material['content'])
        self.assertIn(second_quote, material['content'])
        self.assertEqual(self.queue.get('alpha', first['id'])['status'], 'approved')


if __name__ == '__main__':
    unittest.main()
