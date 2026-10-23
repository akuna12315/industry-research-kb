import unittest
from unittest.mock import patch

from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from .agent_runtime import AgentRuntime
from .knowledge import Knowledge
from .service import kb, ws
from .test_web import Fixture
from .verification import VerificationQueue


class VerifiedMaterialExtractionTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.material = self.knowledge.save('alpha', {'id': 'test-plan', 'title': '项目原文',
            'content': '项目计划营造林100亩，尚未批复。不得将计划目标作为已完成实绩。',
            'region': '示例省示例县', 'kind': 'project', 'model_allowed': True})
        self.queue = VerificationQueue(self.store)
        self.runtime = AgentRuntime(self.store)
        self.runtime._credential = (self.runtime.provider_binding(self.runtime.config()), '')

    def tearDown(self):
        self.temp.cleanup()

    def approve(self):
        record = self.knowledge.get('alpha', self.material['id'])
        with kb.lock(self.store.root):
            self.knowledge._save('alpha', record, verification={'status': 'reviewed',
                'fingerprint': kb.material_fingerprint(record), 'reviewed_at': kb.now()})
        return self.queue.material_context('alpha', self.material['id'])

    def response(self, info, candidates=None):
        context = self.queue.material_context('alpha', self.material['id'])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            'findings': ['项目仍为计划，尚未批复。'],
            'candidates': candidates if candidates is not None else [{
                'title': '营造林计划', 'summary': '资料计划营造林100亩，尚未批复。',
                'quote': '项目计划营造林100亩，尚未批复。',
                'citation': context['excerpts'][0]['citation'], 'status': 'planned',
                'category': '企业与竞争', 'region': '示例省示例县'}],
            'coverage_note': '仅归纳给定片段。'}, tool_call_id='extract-grounded')])

    async def test_model_extracts_grounded_evidence_after_material_approval(self):
        context = self.approve()

        def model(messages, info):
            self.assertEqual(info.function_tools, [])
            self.assertTrue(any(isinstance(part, UserPromptPart)
                for message in messages for part in message.parts))
            return self.response(info)

        result = await self.runtime.extract_verified_material(context, model_override=FunctionModel(model))
        self.assertEqual(result['extraction_method'], 'model')
        self.assertEqual(result['candidates'][0]['status'], 'planned')
        self.assertNotIn('候选仍须用户确认', result['coverage_note'])
        self.assertIn('自动提取入库', result['coverage_note'])
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        self.assertEqual(self.queue.list('alpha'), [])

    async def test_unapproved_material_never_reaches_model_or_excerpt_extraction(self):
        context = self.queue.material_context('alpha', self.material['id'])
        with patch('web.agent_runtime.AsyncOpenAI') as client:
            with self.assertRaisesRegex(ValueError, '尚未通过当前版本'):
                await self.runtime.extract_verified_material(context)
            client.assert_not_called()

    async def test_missing_key_uses_real_local_excerpt_without_model(self):
        context = self.approve()
        with patch('web.agent_runtime.AsyncOpenAI') as client:
            result = await self.runtime.extract_verified_material(context)
            client.assert_not_called()
        candidate = result['candidates'][0]
        self.assertEqual(result['extraction_method'], 'source_excerpt')
        self.assertEqual(candidate['summary'], candidate['quote'])
        self.assertIn(candidate['quote'], context['excerpts'][0]['text'])
        self.assertEqual(candidate['citation'], context['excerpts'][0]['citation'])
        self.assertEqual(candidate['status'], 'reported')
        self.assertEqual(candidate['region'], '示例省示例县')
        self.assertIn('未配置模型密钥', result['findings'][0])
        self.assertIn('未调用模型', result['coverage_note'])
        self.assertIn('仅表示资料记载', result['coverage_note'])

    async def test_private_and_restricted_documents_stay_local_even_with_key(self):
        for sensitivity in ('internal', 'restricted'):
            with self.subTest(sensitivity=sensitivity):
                self.knowledge.save('alpha', {'id': self.material['id'],
                    'model_allowed': False, 'sensitivity': sensitivity})
                context = self.approve()
                self.runtime._credential = (self.runtime.provider_binding(self.runtime.config()), 'test-key')
                with patch('web.agent_runtime.AsyncOpenAI') as client:
                    result = await self.runtime.extract_verified_material(context)
                    client.assert_not_called()
                self.assertEqual(result['extraction_method'], 'source_excerpt')
                self.assertIn('未授权模型读取', result['findings'][0])

    async def test_excerpt_method_uses_current_source_instead_of_supplied_text(self):
        context = self.approve()
        forged = {**context, 'excerpts': [{'citation': 'forged', 'text': '伪造的额外文本'}]}
        result = await self.runtime.extract_verified_material(forged)
        self.assertNotIn('伪造的额外文本', str(result))
        self.assertEqual(result['candidates'][0]['citation'], context['excerpts'][0]['citation'])

    async def test_excerpts_are_bounded_grounded_and_classified_in_existing_scope(self):
        self.knowledge.save('alpha', {'id': self.material['id'], 'content': ''.join(
            f'第{index}项：项目拟进行经营活动，具体实施以批复为准。' for index in range(1200))})
        context = self.approve()
        result = await self.runtime.extract_verified_material(context)
        self.assertLessEqual(len(result['candidates']), 6)
        supplied = {item['citation']: item['text'] for item in context['excerpts']}
        for candidate in result['candidates']:
            self.assertLessEqual(len(candidate['quote']), 1200)
            self.assertEqual(candidate['summary'], candidate['quote'])
            self.assertIn(candidate['quote'], supplied[candidate['citation']])
            self.assertIn(candidate['category'], ws.scope(self.store.home('alpha'))['topics'])

    async def test_changed_version_or_missing_approval_cannot_extract(self):
        context = self.approve()
        self.knowledge.save('alpha', {'id': self.material['id'], 'version': '第二版'})
        with self.assertRaisesRegex(ValueError, '版本已变化'):
            await self.runtime.extract_verified_material(context)
        with self.assertRaisesRegex(ValueError, '尚未通过当前版本'):
            await self.runtime.extract_verified_material(self.queue.material_context('alpha', self.material['id']))

    async def test_actual_model_failure_is_redacted_and_never_becomes_excerpt_success(self):
        context = self.approve()

        def model(messages, info):
            raise ValueError('provider error with secret-marker-do-not-expose')

        with self.assertRaisesRegex(ValueError, '证据提取失败') as caught:
            await self.runtime.extract_verified_material(context, model_override=FunctionModel(model))
        self.assertNotIn('secret-marker-do-not-expose', str(caught.exception))
        self.assertEqual(self.knowledge.get('alpha', self.material['id'])['verification']['status'], 'reviewed')
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    async def test_empty_model_result_fails_without_fake_evidence(self):
        context = self.approve()
        with self.assertRaisesRegex(ValueError, '没有返回可追溯'):
            await self.runtime.extract_verified_material(context,
                model_override=FunctionModel(lambda messages, info: self.response(info, [])))
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    async def test_permission_revocation_during_model_call_discards_extraction(self):
        context = self.approve()

        def model(messages, info):
            self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': False})
            return self.response(info)

        with self.assertRaisesRegex(ValueError, '版本或权限已变化'):
            await self.runtime.extract_verified_material(context, model_override=FunctionModel(model))

    async def test_material_approval_revocation_during_model_call_discards_extraction(self):
        context = self.approve()

        def model(messages, info):
            record = self.knowledge.get('alpha', self.material['id'])
            with kb.lock(self.store.root):
                self.knowledge._save('alpha', record, verification={'status': 'unverified'})
            return self.response(info)

        with self.assertRaisesRegex(ValueError, '尚未通过当前版本'):
            await self.runtime.extract_verified_material(context, model_override=FunctionModel(model))


if __name__ == '__main__':
    unittest.main()
