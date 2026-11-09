from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock
import httpx
from openai import APIStatusError

from fastapi.testclient import TestClient
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from .agent_runtime import AgentRuntime, ResearchContext
from .page_recovery import PageDrafts, recovery_prompt
from .research_pages import ResearchPages
from .server import create_app
from .service import kb
from .test_visual_pages import visual_spec_for, plan_for
from .test_web import Fixture


class PageRecoveryTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.runtime = AgentRuntime(self.store)
        self.reference = None

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **changes):
        return {'id': kb.uuid4().hex, 'conversation': kb.uuid4().hex, 'industry': 'alpha',
                'research': False, 'generate_page': True, 'status': 'running', 'events': [],
                'started_at': kb.now(), **changes}

    def scenarios(self, ref):
        spec = visual_spec_for(ref)
        for key in ('second', 'third'):
            scene = deepcopy(spec['scenes'][0])
            scene.update(id=key, title='视角' + key, kind='architecture', emphasis='normal')
            spec['scenes'].append(scene)
        return spec

    async def initial(self, permanent=False, schema=False):
        def respond(messages, info):
            returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not returns:
                return ModelResponse(parts=[ToolCallPart('search_evidence', {'query': '合同'})])
            names = [p.tool_name for p in returns]
            self.reference = next(p.content[0]['chart_ref'] for p in returns if p.tool_name == 'search_evidence')
            spec = self.scenarios(self.reference)
            if 'plan_visual_page' not in names:
                return ModelResponse(parts=[ToolCallPart('plan_visual_page', {'plan': plan_for(spec)})])
            completed = {p.content.get('prepared') for p in returns if p.tool_name == 'add_visual_scene'}
            failures = [p.content for p in returns if p.tool_name == 'add_visual_scene' and p.content.get('prepared') is False]
            if 'research' not in completed:
                scene = spec['scenes'][0]
            elif 'second' not in completed and len(failures) < (3 if permanent else 1):
                scene = deepcopy(spec['scenes'][1])
                if schema:
                    scene['nodes'][1]['icon'] = 'private-invalid-schema-marker'
                else:
                    scene['nodes'][1]['quote'] = 'private-invalid-quote-marker'
            elif not permanent and 'second' not in completed:
                self.assertEqual([str(v) for v in failures[-1]['fields'][0]['path'][:2]], ['nodes', '1'])
                scene = spec['scenes'][1]
            elif 'third' not in completed:
                scene = spec['scenes'][2]
            elif 'finish_visual_page' not in names:
                return ModelResponse(parts=[ToolCallPart('finish_visual_page', {})])
            else:
                return ModelResponse(parts=[TextPart('处理完成或保留待继续草稿。')])
            return ModelResponse(parts=[ToolCallPart('add_visual_scene', {'scene': scene})])
        job = self.job()
        await self.runtime.run(job, '生成合同可视化页面', model_override=FunctionModel(respond))
        return job

    async def test_bad_quote_is_retried_in_isolation_and_valid_peers_are_preserved(self):
        job = await self.initial()
        self.assertEqual(job['status'], 'completed', job['events'])
        page = ResearchPages(self.store).detail(job['page_id'])
        self.assertEqual(len(page['revision']['content']['scenes']), 3)
        self.assertEqual([s['id'] for s in page['revision']['content']['scenes']], ['research', 'second', 'third'])
        self.assertEqual(PageDrafts(self.store).listing(), [])
        saved = PageDrafts(self.store).get(job['id'])
        self.assertEqual(saved['status'], 'completed')
        self.assertNotIn('sources', saved)
        self.assertTrue(any(e.get('title') == '场景已隔离，正在局部重试' for e in job['events']))

    async def test_bad_schema_is_a_local_failure_not_an_sdk_retry_exhaustion(self):
        job = await self.initial(schema=True)
        self.assertEqual(job['status'], 'completed', job['events'])
        self.assertNotIn('diagnostics', job)

    async def test_exhausted_scene_is_quarantined_others_finish_and_nothing_is_published(self):
        job = await self.initial(permanent=True)
        self.assertEqual(job['status'], 'paused', job['events'])
        self.assertEqual(ResearchPages(self.store).listing(), [])
        draft = PageDrafts(self.store).get(job['id'])
        self.assertEqual(set(draft['scenes']), {'research', 'third'})
        self.assertEqual(draft['failures']['second']['attempts'], 3)
        self.assertTrue(draft['failures']['second']['quarantined'])
        self.assertNotIn('private-invalid-quote-marker', json.dumps(draft))
        self.assertFalse(any(e['type'] == 'page_saved' for e in job['events']))
        self.assertEqual(job['events'][-1]['draft']['completed'], 2)

    async def test_restart_resume_loads_valid_scenes_and_only_rebuilds_the_missing_scene(self):
        original = await self.initial(permanent=True)
        ref = self.reference
        visited = []
        def resume(messages, info):
            returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not returns:
                visited.append('second')
                return ModelResponse(parts=[ToolCallPart('add_visual_scene', {'scene': self.scenarios(ref)['scenes'][1]})])
            if not any(p.tool_name == 'finish_visual_page' for p in returns):
                return ModelResponse(parts=[ToolCallPart('finish_visual_page', {})])
            return ModelResponse(parts=[TextPart('仅修复第二个场景。')])
        # A fresh runtime represents losing all previous service memory.
        job = self.job(resume_draft_id=original['id'], draft_id=original['id'])
        await AgentRuntime(self.store).run(job, '生成合同可视化页面', model_override=FunctionModel(resume))
        self.assertEqual(job['status'], 'completed', job['events'])
        self.assertEqual(visited, ['second'])
        page = ResearchPages(self.store).get(job['page_id'])
        self.assertEqual(page['research_request'], '生成合同可视化页面')
        self.assertNotIn('remaining', page['research_request'])
        self.assertEqual(PageDrafts(self.store).get(original['id'])['status'], 'completed')

    async def test_sdk_response_failure_gets_a_fresh_bounded_repair_run(self):
        original = await self.initial(permanent=True)
        ref = self.reference
        calls = 0
        def resume(messages, info):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise UnexpectedModelBehavior('synthetic response failure')
            returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not returns:
                return ModelResponse(parts=[ToolCallPart('add_visual_scene', {'scene': self.scenarios(ref)['scenes'][1]})])
            if not any(p.tool_name == 'finish_visual_page' for p in returns):
                return ModelResponse(parts=[ToolCallPart('finish_visual_page', {})])
            return ModelResponse(parts=[TextPart('恢复成功。')])
        job = self.job(resume_draft_id=original['id'], draft_id=original['id'])
        await self.runtime.run(job, '生成合同可视化页面', model_override=FunctionModel(resume))
        self.assertEqual(job['status'], 'completed', job['events'])
        self.assertTrue(any(e.get('title') == '正在隔离恢复未完成的场景' for e in job['events']))

    async def test_resume_rechecks_source_fingerprint_and_cannot_publish_changed_sources(self):
        original = await self.initial(permanent=True)
        self.store.import_evidence('alpha', {**self.record, 'summary': '内容已换版'})
        ctx = ResearchContext(self.store, 'alpha')
        with self.assertRaisesRegex(ValueError, '版本'):
            PageDrafts(self.store).restore(ctx, PageDrafts(self.store).get(original['id']))
        self.assertEqual(ResearchPages(self.store).listing(), [])

    async def test_api_exposes_only_draft_progress_and_start_is_single_flight(self):
        original = await self.initial(permanent=True)
        with TestClient(create_app(self.root), headers={'X-KB-Request': '1'}) as client:
            data = client.get('/api/page-drafts?industry=alpha').json()
            self.assertEqual(data[0]['completed'], 2)
            self.assertNotIn('sources', data[0]); self.assertNotIn('scenes', data[0])
        runtime = AgentRuntime(self.store)
        runtime.save_config(runtime.config(), secret='synthetic-model-key')
        runtime.run = AsyncMock()
        first = runtime.start('继续', 'alpha', False, resume_draft_id=original['id'])
        with self.assertRaisesRegex(ValueError, '继续处理'):
            runtime.start('继续', 'alpha', False, resume_draft_id=original['id'])
        await runtime.tasks[first['id']]

    async def test_raw_failed_inputs_never_enter_recovery_prompt(self):
        job = await self.initial(permanent=True, schema=True)
        ctx = ResearchContext(self.store, 'alpha')
        PageDrafts(self.store).restore(ctx, PageDrafts(self.store).get(job['id']))
        text = recovery_prompt(ctx)
        self.assertNotIn('private-invalid-schema-marker', text)
        self.assertIn('completed', text)

    async def test_recovery_passes_share_one_request_budget(self):
        original = await self.initial(permanent=True)
        self.runtime.save_config({**self.runtime.config(), 'request_limit': 2})
        count = 0
        def invalid(messages, info):
            nonlocal count
            count += 1
            raise UnexpectedModelBehavior('synthetic response failure')
        job = self.job(resume_draft_id=original['id'], draft_id=original['id'])
        await self.runtime.run(job, '生成合同可视化页面', model_override=FunctionModel(invalid))
        self.assertLessEqual(count, 2)
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(ResearchPages(self.store).listing(), [])

    async def test_legacy_compose_cannot_bypass_quarantined_scenes(self):
        from .test_research_pages import spec_for
        original = await self.initial(permanent=True)
        def bypass(messages, info):
            return ModelResponse(parts=[ToolCallPart('compose_research_page', {'page': spec_for(self.reference)})])
        job = self.job(resume_draft_id=original['id'], draft_id=original['id'])
        await self.runtime.run(job, '生成合同可视化页面', model_override=FunctionModel(bypass))
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(ResearchPages(self.store).listing(), [])

    async def test_rate_limit_is_paused_immediately_without_scanning_other_scenes(self):
        original = await self.initial(permanent=True)
        calls = 0
        def limited(messages, info):
            nonlocal calls
            calls += 1
            raise APIStatusError('synthetic-rate-limit', response=httpx.Response(429,
                request=httpx.Request('POST', 'https://model.example/v1')), body={'error': 'synthetic'})
        job = self.job(resume_draft_id=original['id'], draft_id=original['id'])
        await self.runtime.run(job, '生成合同可视化页面', model_override=FunctionModel(limited))
        self.assertEqual(calls, 1)
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(ResearchPages(self.store).listing(), [])
