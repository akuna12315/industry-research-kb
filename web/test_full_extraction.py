import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from .agent_runtime import AgentRuntime
from .full_extraction import FullExtractionRuntime
from .knowledge import Knowledge
from .server import create_app
from .service import kb
from .test_web import Fixture
from .verification import VerificationQueue


class FullExtractionTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        text = '\n'.join(f'第{i}项方案计划造林{i}亩，尚未批复。' for i in range(3000)) + '\n终章明确计划建设防火道路，尚未施工。'
        self.material = self.knowledge.save('alpha', {'id': 'long-plan', 'title': '合成长方案',
            'content': text, 'kind': 'project', 'model_allowed': True})
        queue = VerificationQueue(self.store)
        self.review = queue.prepare_material('alpha', 'long-plan')
        queue.approve_material('alpha', self.review['id'], {'decision': 'approve'})
        self.runtime = AgentRuntime(self.store)
        self.runtime.save_config({'provider_id': 'synthetic-extractor', 'base_url': 'https://model.example/v1',
            'protocol': 'chat_completions', 'model': 'synthetic-model'})
        self.runtime._credential = (self.runtime.provider_binding(self.runtime.config()), '')
        self.jobs = FullExtractionRuntime(self.store, self.runtime)

    async def asyncTearDown(self):
        await self.jobs.close()
        self.temp.cleanup()

    @staticmethod
    async def extract(runtime, material, batch, **kwargs):
        source = batch['sources'][-1]
        quote = source['text'][-100:].strip()
        return {'complete': True, 'processed_no_evidence': False, 'usage': {'input_tokens': 10},
            'candidates': [{'title': '方案计划', 'summary': quote, 'quote': quote, 'citation': source['citation'],
            'category': '企业与竞争', 'region': '中国', 'status': 'planned',
            'supports': [{'citation': source['citation'], 'quote': quote}],
            'semantic': {'subject': '合成方案', 'predicate': '计划内容', 'object': quote, 'nature': 'planned'}}]}

    async def wait(self, job):
        task = self.jobs.tasks.get(('alpha', job['id']))
        if task:
            await asyncio.wait_for(asyncio.shield(task), 30)
        return self.jobs.get('alpha', job['id'])

    async def test_missing_key_waits_without_excerpt_evidence_or_lost_approval(self):
        job = await self.jobs.create('alpha', 'long-plan', restart=True)
        self.assertEqual(job['status'], 'waiting_model')
        repeated = await self.jobs.create('alpha', 'long-plan', restart=True)
        self.assertEqual(repeated['id'], job['id'])
        self.assertGreater(job['progress']['total'], 6)
        self.assertEqual(job['progress']['processed_characters'], 0)
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        self.assertEqual(self.knowledge.get('alpha', 'long-plan')['verification']['status'], 'reviewed')
        stored = self.jobs._get('alpha', job['id'])
        self.assertTrue(all('text' not in source for b in stored['batches'] for source in b['sources']))

    async def test_full_tail_is_processed_and_reextract_does_not_duplicate_facts(self):
        self.jobs.model_override = object()
        with patch('web.full_extraction.extract_batch', side_effect=self.extract):
            first = await self.wait(await self.jobs.create('alpha', 'long-plan', restart=True))
            second = await self.wait(await self.jobs.create('alpha', 'long-plan', restart=True))
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['progress']['completed'], first['progress']['total'])
        self.assertEqual(first['progress']['processed_characters'], first['progress']['total_characters'])
        self.assertEqual(set(first['evidence_ids']), set(second['evidence_ids']))
        self.assertEqual(len(self.store.evidence('alpha')), len(first['evidence_ids']) + 1)
        self.assertTrue(any('终章明确计划建设防火道路' in r['summary'] for r in self.store.evidence('alpha', limit=1000)))

    async def test_empty_success_is_allowed_and_not_fabricated(self):
        self.jobs.model_override = object()
        result = {'complete': True, 'processed_no_evidence': True, 'candidates': []}
        with patch('web.full_extraction.extract_batch', new=AsyncMock(return_value=result)):
            job = await self.wait(await self.jobs.create('alpha', 'long-plan'))
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(job['evidence_ids'], [])
        self.assertTrue(all(b['status'] == 'no_evidence' for b in job['batches']))
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    async def test_partial_retry_only_repeats_failed_batch(self):
        self.jobs.model_override = object()
        counts = {}
        async def model(runtime, material, batch, **kwargs):
            counts[batch['id']] = counts.get(batch['id'], 0) + 1
            if len(counts) == 2 and counts[batch['id']] == 1:
                raise ValueError('合成暂时失败')
            return await self.extract(runtime, material, batch)
        with patch('web.full_extraction.extract_batch', side_effect=model):
            first = await self.wait(await self.jobs.create('alpha', 'long-plan'))
            self.assertEqual(first['status'], 'partial')
            self.assertEqual(first['progress']['failed'], 1)
            resumed = await self.jobs.control('alpha', first['id'], 'retry')
            final = await self.wait(resumed)
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(sorted(counts.values()).count(2), 1)

    async def test_capacity_limit_pauses_without_failing_or_skipping_source_batches(self):
        from .extraction_model import ExtractionValidationError
        self.runtime.save_config({'provider_id': 'synthetic-glm',
            'base_url': 'https://open.bigmodel.cn/api/paas/v4', 'model': 'glm-4.7-flash'})
        self.jobs.model_override = object()
        failure = ExtractionValidationError('HTTP 429（业务码1302）', usage={'requests': 1})
        failure.retry_later = True
        with patch('web.full_extraction.extract_batch', new=AsyncMock(side_effect=failure)) as model:
            result = await self.wait(await self.jobs.create('alpha', 'long-plan'))
        self.assertEqual(model.await_count, 1)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['progress']['failed'], 0)
        self.assertEqual(result['progress']['processed_characters'], 0)
        self.assertEqual(result['usage']['requests'], 1)
        self.assertTrue(all(batch['status'] == 'pending' for batch in result['batches']))

    async def test_pause_and_new_runtime_resume_from_disk(self):
        self.jobs.model_override = object()
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch('web.full_extraction.extract_batch', side_effect=blocked):
            job = await self.jobs.create('alpha', 'long-plan')
            await asyncio.wait_for(entered.wait(), 5)
            paused = await self.jobs.control('alpha', job['id'], 'pause')
        self.assertEqual(paused['status'], 'paused')
        self.assertFalse(any(b['status'] == 'running' for b in self.jobs.get('alpha', job['id'])['batches']))
        await self.jobs.close()
        self.jobs = FullExtractionRuntime(self.store, self.runtime)
        self.jobs.model_override = object()
        with patch('web.full_extraction.extract_batch', side_effect=self.extract):
            resumed = await self.jobs.control('alpha', job['id'], 'resume')
            done = await self.wait(resumed)
        self.assertEqual(done['status'], 'completed')

    async def test_revoked_permission_during_request_blocks_commit(self):
        self.jobs.model_override = object()
        async def revoke(runtime, material, batch, **kwargs):
            self.knowledge.save('alpha', {'id': 'long-plan', 'model_allowed': False})
            return await self.extract(runtime, material, batch)
        with patch('web.full_extraction.extract_batch', side_effect=revoke):
            job = await self.wait(await self.jobs.create('alpha', 'long-plan'))
        self.assertEqual(job['status'], 'waiting_permission')
        self.assertEqual(job['evidence_ids'], [])
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    async def test_source_version_change_needs_reverification(self):
        job = await self.jobs.create('alpha', 'long-plan')
        self.knowledge.save('alpha', {'id': 'long-plan', 'version': '另一个版本'})
        with self.assertRaisesRegex(ValueError, '版本或核验'):
            await self.jobs.control('alpha', job['id'], 'resume')

    async def test_status_cache_write_failure_does_not_replay_authoritative_claim(self):
        job = await self.jobs.create('alpha', 'long-plan')
        self.jobs.model_override = object()
        stored = self.jobs._get('alpha', job['id'])
        stored['status'] = 'queued'
        self.jobs._save(stored)
        original_write = self.jobs._atomic_write
        def fail_cache(path, text):
            if path.parent.name == 'status':
                raise PermissionError('synthetic status cache reader')
            return original_write(path, text)
        with patch.object(self.jobs, '_atomic_write', side_effect=fail_cache):
            claimed_job, batch = await self.jobs._transaction(self.jobs._claim, 'alpha', job['id'])
        authoritative = self.jobs._get('alpha', job['id'])
        self.assertEqual(sum(b['status'] == 'running' for b in authoritative['batches']), 1)
        self.assertEqual(authoritative['batches'][0]['claim_id'], batch['claim_id'])
        self.assertEqual(self.jobs.get('alpha', job['id'])['status'], 'running')

    async def test_incomplete_output_splits_instead_of_silently_completing(self):
        self.jobs.model_override = object()
        first_call = True
        async def model(runtime, material, batch, **kwargs):
            nonlocal first_call
            if first_call:
                first_call = False
                return {'complete': False, 'needs_split': True, 'candidates': []}
            return await self.extract(runtime, material, batch)
        with patch('web.full_extraction.extract_batch', side_effect=model):
            job = await self.wait(await self.jobs.create('alpha', 'long-plan'))
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(any(b['status'] == 'split' for b in self.jobs._get('alpha', job['id'])['batches']))
        self.assertEqual(job['progress']['processed_characters'], job['progress']['total_characters'])


class FullExtractionAPITests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.app = create_app(self.root)
        self.app.state.runtime.save_config({'provider_id': 'synthetic-extractor', 'base_url': 'https://model.example/v1',
            'protocol': 'chat_completions', 'model': 'synthetic-model'})
        self.client = TestClient(self.app, headers={'X-KB-Request': '1'})
        self.knowledge = Knowledge(self.store)
        self.knowledge.save('alpha', {'id': 'local-plan', 'title': '项目方案', 'content': '方案计划造林，尚未实施。', 'model_allowed': True})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_one_approval_indexes_full_text_without_model_and_index_is_visible_on_material(self):
        queue = VerificationQueue(self.store)
        review = queue.prepare_material('alpha', 'local-plan')
        result = self.client.post(f'/api/verifications/alpha/{review["id"]}/decision', json={'decision': 'approve'})
        self.assertEqual(result.status_code, 200, result.text)
        job = result.json()['extraction_job']
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['mode'], 'hard_chunk_index')
        self.assertEqual(job['usage']['requests'], 0)
        self.assertEqual(self.client.get('/api/materials/alpha/local-plan').json()['extraction_job']['id'], job['id'])
        listed = self.client.get('/api/materials', params={'industry': 'alpha'}).json()
        self.assertEqual(listed[0]['extraction_job']['id'], job['id'])
        again = self.client.post('/api/materials/alpha/local-plan/extractions', json={'restart': True})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    def test_unapproved_material_cannot_start_or_cross_site_write(self):
        result = self.client.post('/api/materials/alpha/local-plan/extractions', json={'restart': True})
        self.assertEqual(result.status_code, 400)
        result = self.client.post('/api/materials/alpha/local-plan/extractions', json={'restart': True}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(result.status_code, 403)
