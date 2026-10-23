import json
import unittest
from unittest.mock import AsyncMock, patch

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from .agent_runtime import AgentRuntime
from .knowledge import Knowledge
from .service import kb
from .test_web import Fixture
from .verification import VerificationQueue


class MaterialReviewAgentTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.material = self.knowledge.save('alpha', {'id': 'test-plan', 'title': '待核验项目方案',
            'content': '项目计划营造林100亩，尚未批复。不得将计划目标作为已完成实绩。',
            'region': '示例省示例县', 'model_allowed': True})
        self.queue = VerificationQueue(self.store)
        self.context = self.queue.material_context('alpha', self.material['id'])
        self.runtime = AgentRuntime(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self, **changes):
        return {'title': '营造林计划', 'summary': '项目方案计划营造林100亩，所给片段说明尚未批复。',
            'quote': '项目计划营造林100亩，尚未批复。',
            'citation': self.context['excerpts'][0]['citation'], 'status': 'planned',
            'category': '企业与竞争', 'region': '示例省示例县', **changes}

    def response(self, info, candidates=None):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            'findings': ['片段表述为计划且尚未批复，不能作为完成实绩。'],
            'candidates': candidates if candidates is not None else [self.candidate()],
            'coverage_note': '仅检查所给文字片段。'}, tool_call_id='structured-review')])

    async def test_real_sdk_produces_grounded_drafts_without_approving_or_importing(self):
        def model(messages, info):
            self.assertEqual(info.function_tools, [])
            prompt = next(part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart))
            supplied = json.loads(prompt)
            self.assertNotIn('content', supplied['metadata'])
            self.assertEqual(supplied['excerpts'], self.context['excerpts'])
            self.assertIn('不能声称全文已检查', info.instructions)
            return self.response(info)
        result = await self.runtime.review_material(self.context, model_override=FunctionModel(model))
        self.assertEqual(result['candidates'][0]['status'], 'planned')
        self.assertIn('候选仍须用户确认', result['coverage_note'])
        self.assertEqual(self.queue.list('alpha'), [])
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        review = self.queue.prepare_material('alpha', self.material['id'], mode='automatic', analysis=result)
        self.assertEqual(review['status'], 'pending')
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    async def test_forged_quote_and_confirmed_status_cannot_become_drafts(self):
        for changes in ({'quote': '政府已批复并验收100亩'}, {'status': 'confirmed'}):
            calls = []
            def model(messages, info):
                calls.append(1)
                return self.response(info, [self.candidate(**changes)])
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, '自动核验失败'):
                await self.runtime.review_material(self.context, model_override=FunctionModel(model))
            self.assertEqual(len(calls), 2)
        self.assertEqual(self.queue.list('alpha'), [])
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    async def test_revoked_permission_or_stale_version_blocks_before_model(self):
        calls = []
        def model(messages, info):
            calls.append(1)
            return self.response(info)
        self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': False})
        with self.assertRaisesRegex(ValueError, '未授权模型'):
            await self.runtime.review_material(self.context, model_override=FunctionModel(model))
        self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': True, 'version': '第二版'})
        with self.assertRaisesRegex(ValueError, '版本或权限已变化'):
            await self.runtime.review_material(self.context, model_override=FunctionModel(model))
        self.assertEqual(calls, [])

    async def test_revocation_during_model_call_discards_result(self):
        def model(messages, info):
            self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': False})
            return self.response(info)
        with self.assertRaisesRegex(ValueError, '版本或权限已变化'):
            await self.runtime.review_material(self.context, model_override=FunctionModel(model))
        self.assertEqual(self.queue.list('alpha'), [])

    async def test_no_key_fails_explicitly_without_fake_review_or_network(self):
        self.runtime._credential = (self.runtime.provider_binding(self.runtime.config()), '')
        with patch('web.agent_runtime.AsyncOpenAI') as client:
            with self.assertRaisesRegex(ValueError, '密钥'):
                await self.runtime.review_material(self.context)
            client.assert_not_called()
        self.assertEqual(self.queue.list('alpha'), [])

    async def test_provider_failure_does_not_expose_error_body(self):
        def model(messages, info):
            raise ValueError('provider response containing secret-marker-do-not-expose')
        with self.assertRaises(ValueError) as caught:
            await self.runtime.review_material(self.context, model_override=FunctionModel(model))
        self.assertNotIn('secret-marker-do-not-expose', str(caught.exception))
        self.assertEqual(self.queue.list('alpha'), [])

    async def test_context_cannot_smuggle_additional_text_to_model(self):
        forged = {**self.context, 'excerpts': [{'citation': 'forged', 'text': 'unapproved-extra-secret'}]}
        def model(messages, info):
            self.assertNotIn('unapproved-extra-secret', str(messages))
            return self.response(info)
        result = await self.runtime.review_material(forged, model_override=FunctionModel(model))
        self.assertEqual(result['candidates'][0]['citation'], self.context['excerpts'][0]['citation'])

    async def test_permission_revocation_prevents_resending_cached_material_text(self):
        marker = '旧授权专用正文片段'
        self.knowledge.save('alpha', {'id': self.material['id'], 'content': marker})
        conversation = kb.uuid4().hex
        def job():
            return {'id': kb.uuid4().hex, 'conversation': conversation, 'industry': 'alpha',
                'research': False, 'status': 'running', 'events': [], 'started_at': kb.now()}
        calls = 0
        def first_model(messages, info):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(parts=[ToolCallPart('search_materials', {'query': marker}, tool_call_id='find-private')])
            self.assertIn(marker, str(messages))
            return ModelResponse(parts=[TextPart('已按当时的授权读取资料片段。')])
        first = job()
        await self.runtime.run(first, '检索当前授权资料', model_override=FunctionModel(first_model))
        self.assertEqual(first['status'], 'completed', first['events'])
        history_key = (conversation, 'alpha', False)
        self.assertIn(marker, str(self.runtime.histories[history_key]))
        self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': False})
        def second_model(messages, info):
            self.assertNotIn(marker, str(messages))
            self.assertNotIn('find-private', str(messages))
            return ModelResponse(parts=[TextPart('本轮重新按当前授权回答。')])
        second = job()
        await self.runtime.run(second, '继续回答', model_override=FunctionModel(second_model))
        self.assertEqual(second['status'], 'completed', second['events'])
        self.assertNotIn(marker, str(self.runtime.histories[history_key]))

    async def test_material_version_change_and_config_save_invalidate_history_binding(self):
        old_binding = self.runtime.history_source_binding('alpha')
        self.knowledge.save('alpha', {'id': self.material['id'], 'version': '修订版'})
        self.assertNotEqual(old_binding, self.runtime.history_source_binding('alpha'))
        self.runtime.histories['fixture'] = ['old text']
        self.runtime.history_source_bindings['fixture'] = old_binding
        self.runtime.save_config({})
        self.assertEqual(self.runtime.histories, {})
        self.assertEqual(self.runtime.history_source_bindings, {})


class WebDraftAgentTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()

    def tearDown(self):
        self.temp.cleanup()

    async def run_script(self, records, research=True):
        step = 0
        def model(messages, info):
            nonlocal step
            step += 1
            if step == 1:
                return ModelResponse(parts=[ToolCallPart('read_public_source', {'url': self.record['url']}, tool_call_id='read')])
            if step - 2 < len(records):
                return ModelResponse(parts=[ToolCallPart('archive_evidence', {'record': records[step - 2]}, tool_call_id=f'draft-{step}')])
            return ModelResponse(parts=[TextPart('草稿需要用户确认，未入库。')])
        page = {'url': self.record['url'], 'requested_url': self.record['url'], 'title': '测试合同原文',
            'text': '企业披露合同，仍需核对履约进度。', 'links': [], 'truncated': False, 'checked_date': self.record['checked_date']}
        runtime = AgentRuntime(self.store)
        job = {'id': kb.uuid4().hex, 'conversation': kb.uuid4().hex, 'industry': 'alpha',
            'research': research, 'status': 'running', 'events': [], 'started_at': kb.now()}
        with patch('web.agent_runtime.fetch_page', new=AsyncMock(return_value=page)):
            await runtime.run(job, '研究合同', model_override=FunctionModel(model))
        self.assertEqual(job['status'], 'completed', job['events'])
        return job

    async def test_pending_new_facts_obey_limit_without_importing(self):
        scope_path = self.store.home('alpha') / 'config/scope.json'
        kb.atomic_write(scope_path, kb.json_text({**kb.read_json(scope_path), 'max_new_evidence_per_run': 1}))
        job = await self.run_script([{**self.record, 'id': f'pending-{index}', 'quote': '企业披露合同'} for index in range(2)])
        self.assertEqual(len(VerificationQueue(self.store).list('alpha')), 1)
        self.assertEqual(len(job['events'][-1]['pending_review_ids']), 1)
        self.assertEqual(job['events'][-1]['archived_ids'], [])
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    async def test_missing_quote_and_readonly_mode_do_not_prepare_review(self):
        await self.run_script([{**self.record, 'id': 'no-grounding'}])
        await self.run_script([{**self.record, 'id': 'readonly-draft', 'quote': '企业披露合同'}], research=False)
        self.assertEqual(VerificationQueue(self.store).list('alpha'), [])
        self.assertEqual(len(self.store.evidence('alpha')), 1)


if __name__ == '__main__':
    unittest.main()
