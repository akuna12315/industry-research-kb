"""Durable-worker transaction races, using isolated local files and fake models."""
import asyncio
from datetime import datetime, timedelta
import threading
import unittest
from unittest.mock import AsyncMock, patch

from .agent_runtime import AgentRuntime
from .full_extraction import FullExtractionRuntime
from .knowledge import Knowledge
from .service import kb
from .test_web import Fixture
from .verification import VerificationQueue


class ExtractionTransactionTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.runtime = AgentRuntime(self.store)
        self.runtime.save_config({'provider_id': 'synthetic-extractor', 'base_url': 'https://model.example/v1',
            'protocol': 'chat_completions', 'model': 'synthetic-model'})
        self.jobs = FullExtractionRuntime(self.store, self.runtime)
        self.jobs.model_override = object()
        self.workers = [self.jobs]
        self.release_events = []

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()
        for worker in self.workers:
            await worker.close()
        self.temp.cleanup()

    def other_worker(self):
        worker = FullExtractionRuntime(self.store, self.runtime)
        worker.model_override = object()
        self.workers.append(worker)
        return worker

    async def approved_job(self, *, long=False):
        text = '方案计划造林，尚未批复。'
        if long:
            text = '\n\n'.join(f'第{i}项方案计划造林，尚未批复。' for i in range(800))
        self.knowledge.save('alpha', {'id': 'transaction-plan', 'title': '事务合成方案',
            'content': text, 'kind': 'project', 'model_allowed': True})
        queue = VerificationQueue(self.store)
        review = queue.prepare_material('alpha', 'transaction-plan')
        queue.approve_material('alpha', review['id'], {'decision': 'approve'})
        # Claims and workers are started explicitly, making race ordering deterministic.
        with patch.object(self.jobs, '_schedule'):
            return await self.jobs.create('alpha', 'transaction-plan')

    def analysis(self, worker, job, batch):
        material = self.knowledge.get('alpha', job['material_id'])
        source = worker._hydrate(material, batch)['sources'][0]
        quote = source['text'].strip()[:100]
        return {'complete': True, 'processed_no_evidence': False,
            'usage': {'input_tokens': 10}, 'candidates': [{
                'title': '方案计划', 'summary': quote, 'quote': quote,
                'citation': source['citation'], 'category': '企业与竞争',
                'region': '中国', 'status': 'planned',
                'supports': [{'citation': source['citation'], 'quote': quote}],
                'semantic': {'subject': '事务合成方案', 'predicate': '计划内容',
                    'object': quote, 'nature': 'planned'}}]}

    def blocking_import(self):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = kb.import_records

        def blocked(*args, **kwargs):
            # _commit holds the actual workspace write.lock at this point.
            entered.set()
            if not release.wait(5):
                raise AssertionError('测试未释放提交事务')
            return original(*args, **kwargs)

        return entered, release, blocked

    async def test_second_worker_claim_waits_for_real_commit_lock(self):
        job = await self.approved_job(long=True)
        claimed, batch = self.jobs._claim('alpha', job['id'])
        self.assertGreater(len(claimed['batches']), 1)
        analysis = self.analysis(self.jobs, claimed, batch)
        second = self.other_worker()
        entered, release, blocked = self.blocking_import()
        with patch.object(kb, 'import_records', side_effect=blocked):
            committing = asyncio.create_task(asyncio.to_thread(self.jobs._commit,
                'alpha', job['id'], batch['id'], analysis, batch['claim_id']))
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            claiming = asyncio.create_task(asyncio.to_thread(second._claim, 'alpha', job['id']))
            try:
                await asyncio.sleep(0.08)
                self.assertFalse(claiming.done(), '争用写锁应等待，不应让 claim 失败退出')
            finally:
                release.set()
            await committing
            _, next_batch = await claiming
        self.assertNotEqual(next_batch['id'], batch['id'])
        self.assertEqual(next_batch['owner'], second.owner)
        stored = self.jobs._get('alpha', job['id'])
        self.assertEqual(stored['batches'][0]['status'], 'completed')
        self.assertEqual(len(stored['evidence_ids']), 1)

    async def test_cancellation_waits_for_commit_and_never_requeues_committed_batch(self):
        job = await self.approved_job()
        entered, release, blocked = self.blocking_import()

        async def extract(runtime, material, batch, **kwargs):
            return self.analysis(self.jobs, self.jobs._get('alpha', job['id']), batch)

        with patch('web.full_extraction.extract_batch', side_effect=extract), \
                patch.object(kb, 'import_records', side_effect=blocked):
            running = asyncio.create_task(self.jobs.run('alpha', job['id']))
            self.jobs.tasks[('alpha', job['id'])] = running
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            running.cancel()
            try:
                await asyncio.sleep(0.05)
                self.assertFalse(running.done(), '取消应等待已开始的提交事务落盘')
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
        stored = self.jobs._get('alpha', job['id'])
        self.assertEqual(stored['batches'][0]['status'], 'completed')
        self.assertEqual(len(stored['evidence_ids']), 1)
        with patch('web.full_extraction.extract_batch', new=AsyncMock()) as model:
            await self.jobs.run('alpha', job['id'])
            model.assert_not_awaited()
        self.assertEqual(self.jobs.get('alpha', job['id'])['status'], 'completed')
        self.assertEqual(len(self.store.evidence('alpha')), 2)

    async def test_expired_claim_cannot_commit_after_another_worker_takes_over(self):
        job = await self.approved_job()
        claimed, old = self.jobs._claim('alpha', job['id'])
        analysis = self.analysis(self.jobs, claimed, old)
        with self.jobs._lock():
            stored = self.jobs._get('alpha', job['id'])
            stored['batches'][0]['lease_until'] = (datetime.now(kb.TZ) - timedelta(seconds=1)).isoformat()
            self.jobs._save(stored)
        second = self.other_worker()
        _, current = second._claim('alpha', job['id'])
        self.assertNotEqual(old['claim_id'], current['claim_id'])
        self.jobs._commit('alpha', job['id'], old['id'], analysis, old['claim_id'])
        stored = self.jobs._get('alpha', job['id'])
        self.assertEqual(stored['evidence_ids'], [])
        self.assertEqual(stored['batches'][0]['owner'], second.owner)
        self.assertEqual(stored['batches'][0]['claim_id'], current['claim_id'])
        second._commit('alpha', job['id'], current['id'], analysis, current['claim_id'])
        self.assertEqual(len(self.jobs._get('alpha', job['id'])['evidence_ids']), 1)

    async def test_old_token_cannot_commit_with_same_worker_owner(self):
        job = await self.approved_job()
        claimed, old = self.jobs._claim('alpha', job['id'])
        analysis = self.analysis(self.jobs, claimed, old)
        with self.jobs._lock():
            stored = self.jobs._get('alpha', job['id'])
            stored['batches'][0]['claim_id'] = 'newer-claim-token'
            self.jobs._save(stored)
        self.jobs._commit('alpha', job['id'], old['id'], analysis, old['claim_id'])
        stored = self.jobs._get('alpha', job['id'])
        self.assertEqual(stored['batches'][0]['status'], 'running')
        self.assertEqual(stored['batches'][0]['claim_id'], 'newer-claim-token')
        self.assertEqual(stored['evidence_ids'], [])

    async def test_cancelled_old_attempt_does_not_release_new_claim(self):
        job = await self.approved_job()
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch('web.full_extraction.extract_batch', side_effect=blocked):
            running = asyncio.create_task(self.jobs.run('alpha', job['id']))
            self.jobs.tasks[('alpha', job['id'])] = running
            await asyncio.wait_for(entered.wait(), 3)
            with self.jobs._lock():
                stored = self.jobs._get('alpha', job['id'])
                stored['batches'][0]['claim_id'] = 'newer-claim-token'
                self.jobs._save(stored)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        stored = self.jobs._get('alpha', job['id'])
        self.assertEqual(stored['batches'][0]['claim_id'], 'newer-claim-token')
        self.assertEqual(stored['batches'][0]['status'], 'running',
            '旧尝试的取消必须比较 claim_id，不能释放后来的租约')

    async def test_loopback_runs_one_worker_while_remote_keeps_three(self):
        for endpoint, expected in (
                ('http://127.0.0.1:8091/v1', 1), ('http://localhost:8091/v1', 1),
                ('http://[::1]:8091/v1', 1), ('https://api.example/v1', 3),
                ('https://open.bigmodel.cn/api/paas/v4', 1)):
            active = peak = calls = 0

            async def worker(*args):
                nonlocal active, peak, calls
                calls += 1
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(.01)
                active -= 1

            with self.subTest(endpoint=endpoint), \
                    patch.object(self.jobs, '_get', return_value={'provider': {'base_url': endpoint, 'model': 'glm-4.7-flash'}}), \
                    patch.object(self.jobs, '_worker', side_effect=worker):
                await self.jobs.run('alpha', 'synthetic-job')
                self.assertEqual(calls, expected)
                self.assertEqual(peak, expected)

    async def test_pause_waits_for_claim_thread_before_releasing_old_owner(self):
        job = await self.approved_job(long=True)
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = self.jobs._claim
        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('未释放合成claim线程')
            return original(*args)
        with patch.object(self.jobs, '_claim', side_effect=blocked):
            running = asyncio.create_task(self.jobs.run('alpha', job['id']))
            self.jobs.tasks[('alpha', job['id'])] = running
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            pausing = asyncio.create_task(self.jobs.control('alpha', job['id'], 'pause'))
            try:
                await asyncio.sleep(.05)
                self.assertFalse(pausing.done())
            finally:
                release.set()
            await pausing
        stored = self.jobs._get('alpha', job['id'])
        self.assertEqual(stored['status'], 'paused')
        self.assertFalse(any(b['status'] == 'running' for b in stored['batches']))

    async def test_split_children_run_before_unrelated_remaining_batches(self):
        job = await self.approved_job(long=True)
        claimed, parent = self.jobs._claim('alpha', job['id'])
        others = {batch['id'] for batch in claimed['batches'] if batch['id'] != parent['id']}
        self.jobs._commit('alpha', job['id'], parent['id'],
            {'complete': False, 'needs_split': True}, parent['claim_id'])
        stored, child = self.jobs._claim('alpha', job['id'])
        self.assertNotIn(child['id'], others | {parent['id']})
        self.assertEqual(sum(batch['characters'] for batch in stored['batches'] if batch['status'] != 'split'),
            job['progress']['total_characters'])

    async def test_model_switch_preserves_old_provenance_and_fences_inflight_claim(self):
        job = await self.approved_job(long=True)
        claimed, completed = self.jobs._claim('alpha', job['id'])
        previous = dict(claimed['provider'])
        self.jobs._commit('alpha', job['id'], completed['id'],
            self.analysis(self.jobs, claimed, completed), completed['claim_id'])
        claimed, inflight = self.jobs._claim('alpha', job['id'])
        old_analysis = self.analysis(self.jobs, claimed, inflight)
        with self.jobs._lock():
            stored = self.jobs._get('alpha', job['id'])
            stored['status'] = 'paused'
            stored.pop('provider_history')
            # Reproduce a persisted manifest created before per-batch provenance.
            for batch in stored['batches']:
                batch.pop('provider', None)
            self.jobs._save(stored)
        self.runtime.save_config({'provider_id': 'local-qwen', 'base_url': 'http://127.0.0.1:8091/v1',
            'model': 'qwen-27b', 'protocol': 'chat_completions'})
        with patch.object(self.jobs, '_schedule'):
            await self.jobs.control('alpha', job['id'], 'resume')
        stored = self.jobs._get('alpha', job['id'])
        finished = next(b for b in stored['batches'] if b['id'] == completed['id'])
        self.assertEqual(finished['provider'], previous)
        self.assertTrue(finished['provider_inferred'])
        self.assertEqual(finished['status'], 'completed')
        self.assertEqual([entry['provider'] for entry in stored['provider_history']],
            [previous, self.jobs._binding()])
        self.assertTrue(stored['provider_history'][0]['inferred'])
        self.assertEqual(stored['provider_history'][1]['action'], 'resume')

        # Switching the binding invalidates the old in-flight attempt immediately.
        self.jobs._commit('alpha', job['id'], inflight['id'], old_analysis, inflight['claim_id'])
        claimed, current = self.jobs._claim('alpha', job['id'])
        self.assertEqual(current['id'], inflight['id'])
        self.assertNotEqual(current['claim_id'], inflight['claim_id'])
        self.assertEqual(current['provider'], self.jobs._binding())
        self.assertNotIn('provider_inferred', current)
        self.jobs._release('alpha', job['id'], inflight['id'], inflight['claim_id'], 'partial', '旧模型失败')
        self.assertEqual(self.jobs._get('alpha', job['id'])['status'], 'running')
        self.jobs._commit('alpha', job['id'], current['id'],
            self.analysis(self.jobs, claimed, current), current['claim_id'])
        stored = self.jobs._get('alpha', job['id'])
        for batch, provider in ((finished, previous), (current, self.jobs._binding())):
            result = next(b for b in stored['batches'] if b['id'] == batch['id'])
            for evidence_id in result['evidence_ids']:
                record = kb.read_json(self.store.home('alpha') / 'evidence' / (evidence_id + '.json'))
                self.assertEqual(record['extraction_provider'], provider)


if __name__ == '__main__':
    unittest.main()
