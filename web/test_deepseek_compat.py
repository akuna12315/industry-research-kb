"""Real SDK wire tests, with no vendor calls or real credentials."""
import json
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .agent_runtime import AgentRuntime, configured_model, configured_model_settings, model_failure
from .knowledge import Knowledge
from .test_web import Fixture
from .verification import VerificationQueue


class DeepSeekCompatibilityTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.runtime = AgentRuntime(self.store)
        self.runtime.save_config({'base_url': 'https://api.deepseek.com',
                                  'protocol': 'chat_completions', 'model': 'deepseek-flash'},
                                 secret='synthetic-test-key')
        self.knowledge = Knowledge(self.store)
        material = self.knowledge.save('alpha', {'id': 'test-plan', 'title': '合成项目方案',
            'content': '项目计划营造林100亩，尚未批复。', 'model_allowed': True})
        self.queue = VerificationQueue(self.store)
        self.review = self.queue.prepare_material('alpha', material['id'])
        self.queue.approve_material('alpha', self.review['id'], {'decision': 'approve'})
        self.context = self.queue.material_context('alpha', material['id'])
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, invalid_first=False):
        def respond(request):
            body = json.loads(request.content)
            self.requests.append(body)
            if body.get('tool_choice') != 'auto':
                return httpx.Response(400, json={'error': {
                    'message': 'Thinking mode does not support this tool_choice synthetic-private-marker'}})
            tool = body['tools'][0]['function']['name']
            citation = self.context['excerpts'][0]['citation']
            candidate = {'title': '营造林计划', 'summary': '方案计划营造林100亩，尚未批复。',
                'quote': '不存在的原文' if invalid_first and len(self.requests) == 1 else '项目计划营造林100亩，尚未批复。',
                'citation': citation, 'category': '企业与竞争', 'status': 'planned', 'region': '中国'}
            result = {'findings': [], 'candidates': [candidate], 'coverage_note': '只检查所给合成片段。'}
            return httpx.Response(200, json={'id': 'synthetic-completion', 'object': 'chat.completion',
                'created': 1, 'model': 'deepseek-flash', 'choices': [{'index': 0,
                'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                'reasoning_content': 'synthetic-reasoning', 'tool_calls': [{'id': 'synthetic-call',
                'type': 'function', 'function': {'name': tool, 'arguments': json.dumps(result)}}]}}],
                'usage': {'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}})
        return httpx.MockTransport(respond)

    def client(self, invalid_first=False):
        return AsyncOpenAI(api_key='synthetic-test-key', base_url='https://api.deepseek.com',
            max_retries=0, http_client=httpx.AsyncClient(transport=self.transport(invalid_first)))

    async def test_old_openai_profile_reproduces_400_with_required_tool_choice(self):
        async with self.client() as client:
            model = OpenAIChatModel('deepseek-flash', provider=OpenAIProvider(openai_client=client))
            with self.assertRaises(ValueError) as caught:
                await self.runtime.extract_verified_material(self.context, model_override=model)
        self.assertEqual(self.requests[0]['tool_choice'], 'required')
        self.assertIn('思考模式不接受', str(caught.exception))
        self.assertNotIn('没有生成通过结论', str(caught.exception))
        self.assertNotIn('自动核验失败', str(caught.exception))
        self.assertNotIn('synthetic-private-marker', str(caught.exception))

    async def test_real_configured_path_uses_auto_and_preserves_user_approval(self):
        with patch('web.agent_runtime.AsyncOpenAI', return_value=self.client()):
            result = await self.runtime.extract_verified_material(self.context)
        self.assertEqual(self.requests[0]['tool_choice'], 'auto')
        self.assertEqual(result['extraction_method'], 'model')
        self.assertEqual(result['candidates'][0]['status'], 'planned')
        completed = self.queue.complete_material_extraction('alpha', self.review['id'], result)
        self.assertEqual(completed['extraction_status'], 'completed')
        self.assertEqual(len(completed['evidence_ids']), 1)
        record = self.knowledge.get('alpha', 'test-plan')
        self.assertEqual(record['verification']['status'], 'reviewed')
        self.assertEqual(record['verification']['evidence_ids'], completed['evidence_ids'])

    async def test_grounding_retry_keeps_reasoning_content_in_wire_history(self):
        with patch('web.agent_runtime.AsyncOpenAI', return_value=self.client(invalid_first=True)):
            result = await self.runtime.extract_verified_material(self.context)
        self.assertEqual(len(self.requests), 2)
        assistant = next(item for item in self.requests[1]['messages'] if item['role'] == 'assistant')
        self.assertEqual(assistant['reasoning_content'], 'synthetic-reasoning')
        self.assertEqual(self.requests[1]['tool_choice'], 'auto')
        self.assertEqual(result['candidates'][0]['quote'], '项目计划营造林100亩，尚未批复。')

    async def test_other_provider_keeps_original_profile_and_endpoint(self):
        async with self.client() as client:
            cfg = {**self.runtime.config(), 'base_url': 'https://gateway.example/v1'}
            model = configured_model(cfg, client)
            self.assertEqual(model.system, 'openai')
            self.assertNotEqual(model.profile.get('supports_forced_tool_choice'), False)

    async def test_local_qwen_sends_enabled_thinking_on_actual_sdk_wire(self):
        cfg = {**self.runtime.config(), 'base_url': 'http://127.0.0.1:8091/v1', 'model': 'qwen3.8-27b'}
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'id': 'synthetic-local', 'object': 'chat.completion',
                'created': 1, 'model': cfg['model'], 'choices': [{'index': 0,
                    'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '4'}}]})
        from pydantic_ai import Agent
        async with AsyncOpenAI(api_key='local', base_url=cfg['base_url'],
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
            result = await Agent(configured_model(cfg, client)).run('2+2',
                model_settings=configured_model_settings(cfg, 32))
        self.assertEqual(result.output, '4')
        self.assertEqual(requests[0]['reasoning_effort'], 'low')
        self.assertEqual(requests[0]['thinking_budget'], 1024)
        for changes in ({'base_url': 'https://gateway.example/v1'}, {'model': 'other-model'},
                {'protocol': 'responses'}):
            self.assertNotIn('openai_reasoning_effort', configured_model_settings({**cfg, **changes}))

    async def test_fixed_error_diagnostics_never_echo_provider_body(self):
        class Failure(Exception):
            status_code = 400
            body = {'message': 'reasoning_content missing; synthetic-private-marker'}
        message = model_failure(Failure())
        self.assertIn('保留思考内容', message)
        self.assertNotIn('synthetic-private-marker', message)

    async def test_local_qwen_thinking_reranker_uses_auto_tool_choice(self):
        from pydantic_ai import Agent
        from .agent_runtime import MaterialRanking
        cfg = {**self.runtime.config(), 'base_url': 'http://127.0.0.1:8091/v1', 'model': 'qwen3.8-27b'}
        requests = []
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body['tool_choice'], 'auto')
            self.assertEqual(body['reasoning_effort'], 'low')
            return httpx.Response(200, json={'id': 'synthetic-rank', 'object': 'chat.completion',
                'created': 1, 'model': cfg['model'], 'choices': [{'index': 0, 'finish_reason': 'tool_calls',
                    'message': {'role': 'assistant', 'content': None, 'reasoning_content': 'synthetic',
                        'tool_calls': [{'id': 'rank', 'type': 'function', 'function': {
                            'name': body['tools'][0]['function']['name'], 'arguments': '{"indices":[1,0]}'}}]}}]})
        async with AsyncOpenAI(api_key='local', base_url=cfg['base_url'],
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
            result = await Agent(configured_model(cfg, client), output_type=MaterialRanking).run('rank',
                model_settings=configured_model_settings(cfg, 1024))
        self.assertEqual(result.output.indices, [1, 0])


if __name__ == '__main__':
    unittest.main()
