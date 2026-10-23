"""Offline long-document extraction and real-SDK protocol regression tests."""
import copy
import json
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from .agent_runtime import AgentRuntime, configured_model
from .extraction_model import (ExtractionValidationError, ExtractionWaiting,
    LocalBatchExtraction, MAX_BATCH_CHARACTERS, _model_settings, extract_batch, semantic_key)
from .knowledge import Knowledge
from .service import kb
from .test_web import Fixture
from .verification import VerificationQueue


class BatchExtractionTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.runtime = AgentRuntime(self.store)
        self.runtime.save_config({'provider_id': 'synthetic-extractor', 'base_url': 'https://model.example/v1',
            'protocol': 'chat_completions', 'model': 'synthetic-model'})
        self.knowledge = Knowledge(self.store)
        text = '\n'.join(f'第{i}项计划营造林{i}亩，尚未批复。' for i in range(1, 21))
        saved = self.knowledge.save('alpha', {'id': 'long-plan', 'title': '合成大型项目方案',
            'content': text, 'model_allowed': True, 'region': '中国'})
        queue = VerificationQueue(self.store)
        review = queue.prepare_material('alpha', saved['id'])
        queue.approve_material('alpha', review['id'], {'decision': 'approve'})
        record = self.knowledge.get('alpha', saved['id'])
        self.material = {**record, 'fingerprint': kb.material_fingerprint(record)}
        self.batch = {'id': 'batch-middle-tail', 'sources': [{'citation': 'long-plan#chars-1-' + str(len(text)),
            'text': text, 'location': '段落1至20'}], 'context': []}
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self, index=1, **changes):
        quote = f'第{index}项计划营造林{index}亩，尚未批复。'
        result = {'title': f'第{index}项营造林计划', 'summary': quote, 'quote': quote,
            'citation': self.batch['sources'][0]['citation'], 'category': '企业与竞争',
            'region': '中国', 'status': 'planned', 'supports': [],
            'semantic': {'subject': f'第{index}项', 'predicate': '计划营造林', 'object': f'{index}亩',
                'period': '', 'scope': '', 'conditions': '尚未批复', 'nature': 'planned'}}
        result.update(changes)
        return result

    @staticmethod
    def payload(messages):
        prompt = next(part.content for message in messages for part in message.parts
            if isinstance(part, UserPromptPart))
        return json.loads(prompt)

    def model(self, candidates=None, extraction=None, audit=None, mutate=None, finish_reason=None):
        def respond(messages, info):
            tool = info.output_tools[0].name
            supplied = self.payload(messages)
            self.calls.append({'tool': tool, 'payload': supplied, 'settings': info.model_settings})
            if tool == 'extract_document_batch':
                if mutate:
                    mutate()
                output = extraction if extraction is not None else {
                    'candidates': candidates if candidates is not None else [self.candidate()],
                    'findings': [], 'complete': True, 'needs_split': False, 'processed_no_evidence': False}
            else:
                output = audit if audit is not None else {'checks': [
                    {'index': item['index'], 'supported': True, 'reason': ''}
                    for item in supplied['candidates']], 'complete': True}
            return ModelResponse(parts=[ToolCallPart(tool, output, tool_call_id='synthetic-batch')],
                finish_reason=finish_reason if tool == 'extract_document_batch' else 'tool_call',
                usage=RequestUsage(input_tokens=100, output_tokens=100))
        return FunctionModel(respond)

    async def test_complete_middle_and_tail_sources_with_more_than_twelve_candidates(self):
        result = await extract_batch(self.runtime, self.material, self.batch,
            model_override=self.model(candidates=[self.candidate(i) for i in range(1, 21)]))
        self.assertEqual(self.calls[0]['payload']['sources'],
            [{**self.batch['sources'][0], 'citation': 'S1'}])
        self.assertIn('第20项计划营造林20亩', self.calls[0]['payload']['sources'][0]['text'])
        self.assertEqual(len(result['candidates']), 20)
        self.assertTrue(result['complete'])
        self.assertEqual(result['usage']['requests'], 2)
        self.assertEqual(result['usage']['input_tokens'], 200)
        self.assertEqual(result['support_validation'], 'model_checked')
        self.assertEqual(len(self.store.evidence('alpha')), 1)  # extractor never archives

    async def test_explicit_zero_evidence_is_successfully_processed_without_audit(self):
        result = await extract_batch(self.runtime, self.material, self.batch, model_override=self.model(
            extraction={'candidates': [], 'findings': ['本批为目录导航，无独立断言。'],
                'complete': True, 'processed_no_evidence': True}))
        self.assertTrue(result['complete'])
        self.assertTrue(result['processed_no_evidence'])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(len(self.calls), 1)

    async def test_empty_candidate_response_without_explicit_processing_result_fails(self):
        with self.assertRaises(ExtractionValidationError):
            await extract_batch(self.runtime, self.material, self.batch,
                model_override=self.model(candidates=[]))
        self.assertEqual(len(self.calls), 2)

    async def test_dense_batch_must_split_and_partial_candidates_are_not_committable(self):
        result = await extract_batch(self.runtime, self.material, self.batch, model_override=self.model(
            extraction={'candidates': [self.candidate()], 'findings': ['尚有后续内容。'],
                'complete': False, 'needs_split': True}))
        self.assertFalse(result['complete'])
        self.assertTrue(result['needs_split'])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(len(self.calls), 1)

    async def test_length_finish_reason_does_not_claim_complete_even_with_valid_json(self):
        result = await extract_batch(self.runtime, self.material, self.batch,
            model_override=self.model(finish_reason='length'))
        self.assertTrue(result['needs_split'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['candidates'], [])

    async def test_incomplete_tool_json_requires_split_without_fake_completion(self):
        def respond(messages, info):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
                '{"candidates":', tool_call_id='truncated')], finish_reason='length')
        result = await extract_batch(self.runtime, self.material, self.batch, model_override=FunctionModel(respond))
        self.assertTrue(result['needs_split'])
        self.assertFalse(result['complete'])

    async def test_oversized_input_is_never_silently_sampled(self):
        batch = {**self.batch, 'sources': [{**self.batch['sources'][0], 'text': '甲' * (MAX_BATCH_CHARACTERS + 1)}]}
        result = await extract_batch(self.runtime, self.material, batch, model_override=self.model())
        self.assertTrue(result['needs_split'])
        self.assertEqual(self.calls, [])

    async def test_forged_quote_or_citation_is_rejected_with_one_grounding_retry(self):
        for changes in ({'quote': '项目已批复100亩。'}, {'citation': 'another-document#chars-1-10'}):
            self.calls.clear()
            with self.subTest(changes=changes), self.assertRaises(ExtractionValidationError):
                await extract_batch(self.runtime, self.material, self.batch,
                    model_override=self.model(candidates=[self.candidate(**changes)]))
            self.assertEqual(len(self.calls), 2)

    async def test_number_unit_or_plan_changes_cannot_be_supported_by_a_real_quote(self):
        for summary in ('第1项计划营造林900亩，尚未批复。', '第1项计划营造林1公顷，尚未批复。',
                        '第1项已完成营造林1亩。'):
            self.calls.clear()
            with self.subTest(summary=summary), self.assertRaises(ExtractionValidationError):
                await extract_batch(self.runtime, self.material, self.batch,
                    model_override=self.model(candidates=[self.candidate(summary=summary)]))
            self.assertEqual(len(self.calls), 2)

    async def test_independent_support_check_rejects_unsupported_summary(self):
        with self.assertRaisesRegex(ExtractionValidationError, '原文支持性复核'):
            await extract_batch(self.runtime, self.material, self.batch,
                model_override=self.model(candidates=[self.candidate(summary='第1项计划用于珍稀树种保护，尚未批复。')],
                    audit={'checks': [{'index': 0, 'supported': False,
                        'reason': 'synthetic-untrusted-private-marker'}], 'complete': True}))
        self.assertEqual(len(self.calls), 2)

    async def test_support_check_must_cover_every_candidate_exactly_once(self):
        for checks in ([], [{'index': 0, 'supported': True}, {'index': 0, 'supported': True}]):
            with self.subTest(checks=checks), self.assertRaisesRegex(ExtractionValidationError, '完整覆盖'):
                await extract_batch(self.runtime, self.material, self.batch,
                    model_override=self.model(candidates=[self.candidate(1), self.candidate(2)],
                        audit={'checks': checks, 'complete': True}))

    async def test_multi_support_table_preserves_header_units_notes_and_geometry(self):
        header = {'citation': 'long-plan#chars-1-5', 'text': '投资（万元）', 'location': '表1表头'}
        row = {'citation': 'long-plan#chars-6-15', 'text': '合计\t100', 'location': '表1第3行'}
        note = {'citation': 'long-plan#chars-16-30', 'text': '注：本表为计划投资。', 'location': '表1注释'}
        geometry = [{'table': 1, 'row': 3, 'cells': [{'column': 1, 'column_span': 1,
            'vertical_merge': 'continue', 'text': ''}, {'column': 2, 'text': '100'}]}]
        batch = {**self.batch, 'sources': [row], 'context': [header, note], 'table_structure': geometry}
        candidate = self.candidate(title='计划投资', summary='计划投资合计100万元。', citation=row['citation'],
            quote=row['text'], supports=[{'citation': header['citation'], 'quote': header['text']},
                {'citation': note['citation'], 'quote': note['text']}],
            semantic={'subject': '合计', 'predicate': '计划投资', 'object': '100万元',
                'conditions': '本表为计划投资', 'nature': 'planned'})
        result = await extract_batch(self.runtime, self.material, batch,
            model_override=self.model(candidates=[candidate]))
        self.assertEqual(self.calls[0]['payload']['table_structure'], geometry)
        alias_context = [{**header, 'citation': 'C1'}, {**note, 'citation': 'C2'}]
        self.assertEqual(self.calls[0]['payload']['context'], alias_context)
        self.assertEqual(len(result['candidates'][0]['supports']), 3)
        self.assertEqual(self.calls[1]['payload']['sources'], [{**row, 'citation': 'S1'}])
        self.assertEqual(self.calls[1]['payload']['context'], alias_context)
        self.assertEqual([s['citation'] for s in result['candidates'][0]['supports']],
            [row['citation'], header['citation'], note['citation']])

    async def test_context_only_fact_is_not_extracted_again_as_primary_evidence(self):
        context = {'citation': 'long-plan#chars-900-1000', 'text': '上下文独有项目。', 'location': '上文'}
        batch = {**self.batch, 'context': [context]}
        with self.assertRaises(ExtractionValidationError):
            await extract_batch(self.runtime, self.material, batch, model_override=self.model(
                candidates=[self.candidate(citation=context['citation'], quote=context['text'])]))

    async def test_no_key_waits_without_model_or_excerpt_fallback(self):
        self.runtime._credential = (self.runtime.provider_binding(self.runtime.config()), '')
        with patch('web.extraction_model.AsyncOpenAI') as client:
            with self.assertRaisesRegex(ExtractionWaiting, '等待模型密钥'):
                await extract_batch(self.runtime, self.material, self.batch)
            client.assert_not_called()
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    async def test_permission_revoked_during_extraction_prevents_audit_and_return(self):
        def revoke():
            self.knowledge.save('alpha', {'id': 'long-plan', 'model_allowed': False})
        with self.assertRaisesRegex(ExtractionWaiting, '等待授权'):
            await extract_batch(self.runtime, self.material, self.batch, model_override=self.model(mutate=revoke))
        self.assertEqual(len(self.calls), 1)

    async def test_version_change_during_extraction_prevents_audit_and_return(self):
        def change():
            self.knowledge.save('alpha', {'id': 'long-plan', 'version': '下一版本'})
        with self.assertRaisesRegex(ValueError, '资料版本已变化'):
            await extract_batch(self.runtime, self.material, self.batch, model_override=self.model(mutate=change))
        self.assertEqual(len(self.calls), 1)

    async def test_model_binding_change_stops_followup_requests(self):
        def change():
            self.runtime.save_config({'model': 'synthetic-another-model'})
        with self.assertRaisesRegex(ValueError, '模型配置已变化'):
            await extract_batch(self.runtime, self.material, self.batch, model_override=self.model(mutate=change))
        self.assertEqual(len(self.calls), 1)

    async def test_incompatible_model_error_body_never_leaks(self):
        def fail(messages, info):
            class VendorError(Exception):
                status_code = 400
                body = 'tool_choice not allowed in thinking mode; synthetic-private-secret'
            raise VendorError()
        with self.assertRaisesRegex(ExtractionValidationError, '思考模式不接受') as caught:
            await extract_batch(self.runtime, self.material, self.batch, model_override=FunctionModel(fail))
        self.assertNotIn('synthetic-private-secret', str(caught.exception))

    async def test_arbitrary_provider_value_error_is_redacted(self):
        def fail(messages, info):
            raise ValueError('synthetic-private-provider-body')
        with self.assertRaises(ExtractionValidationError) as caught:
            await extract_batch(self.runtime, self.material, self.batch, model_override=FunctionModel(fail))
        self.assertNotIn('synthetic-private-provider-body', str(caught.exception))

    async def test_capacity_error_keeps_only_known_vendor_code_and_requests_pause(self):
        def fail(messages, info):
            class Busy(Exception):
                status_code = 429
                body = {'error': {'code': '1302', 'message': 'synthetic-private-provider-body'}}
            raise Busy()
        with self.assertRaises(ExtractionValidationError) as caught:
            await extract_batch(self.runtime, self.material, self.batch, model_override=FunctionModel(fail))
        self.assertTrue(caught.exception.retry_later)
        self.assertIn('业务码1302', str(caught.exception))
        self.assertNotIn('synthetic-private-provider-body', str(caught.exception))

    async def test_fixed_diagnostics_distinguish_schema_primary_auxiliary_and_numeric_failures(self):
        malformed = self.candidate()
        malformed.pop('semantic')
        cases = [
            ('schema', malformed),
            ('primary_reference', self.candidate(quote='合成并不存在的原文-private-marker')),
            ('support_reference', self.candidate(supports=[{'citation': 'S1', 'quote': '辅助引用-private-marker'}])),
            ('numeric_or_nature', self.candidate(summary='第1项已完成营造林999亩。')),
        ]
        for category, candidate in cases:
            with self.subTest(category=category), self.assertRaises(ExtractionValidationError) as caught:
                await extract_batch(self.runtime, self.material, self.batch,
                    model_override=self.model(candidates=[candidate]))
            self.assertEqual(caught.exception.category, category)
            self.assertEqual(caught.exception.stage, 'extraction')
            self.assertNotIn('private-marker', str(caught.exception))

    async def test_normal_text_instead_of_output_tool_has_fixed_safe_diagnostic(self):
        def respond(messages, info):
            return ModelResponse(parts=[TextPart('synthetic-private-model-body')])
        with self.assertRaises(ExtractionValidationError) as caught:
            await extract_batch(self.runtime, self.material, self.batch, model_override=FunctionModel(respond))
        self.assertEqual(caught.exception.category, 'no_output_tool')
        self.assertIn('模型未调用所需的结构化输出工具', str(caught.exception))
        self.assertNotIn('synthetic-private-model-body', str(caught.exception))

    async def test_title_rewrite_does_not_change_assertion_identity_but_unit_does(self):
        result = await extract_batch(self.runtime, self.material, self.batch, model_override=self.model())
        self.calls.clear()
        rewritten = await extract_batch(self.runtime, self.material, self.batch,
            model_override=self.model(candidates=[self.candidate(title='改写的展示标题')]))
        self.assertEqual(result['candidates'][0]['semantic_key'], rewritten['candidates'][0]['semantic_key'])
        original = self.candidate()['semantic']
        self.assertNotEqual(semantic_key(original), semantic_key({**original, 'object': '1公顷'}))
        self.assertNotEqual(semantic_key(original), semantic_key({**original, 'period': '2026年'}))

    async def test_local_model_selects_ranges_without_recopying_original_quotes(self):
        self.runtime.save_config({'base_url': 'http://127.0.0.1:8091/v1', 'model': 'qwen3.8-27b'})
        text = '\n'.join(self.batch['sources'][0]['text'].splitlines()[:5])
        self.batch['sources'][0].update(text=text, citation=f'long-plan#chars-1-{len(text)}')
        candidate = self.candidate()
        candidate.pop('quote')
        candidate.pop('supports')
        candidate.update(citation='S1', support_citations=['S1'])
        result = await extract_batch(self.runtime, self.material, self.batch,
            model_override=self.model(candidates=[candidate]))
        self.assertTrue(result['complete'])
        actual = result['candidates'][0]
        self.assertEqual(actual['citation'], self.batch['sources'][0]['citation'])
        self.assertEqual(actual['quote'], self.batch['sources'][0]['text'])
        self.assertEqual(actual['supports'][0]['quote'], self.batch['sources'][0]['text'])
        self.assertEqual(len(self.calls), 2, '范围选择仍须独立支持性复核')
        self.assertEqual(self.calls[0]['settings']['max_tokens'], 4096)

    async def test_local_retry_identifies_locator_in_scope_without_approving_it(self):
        from .extraction_model import BatchExtraction, _grounded, _local_retry_feedback
        from pydantic_ai import ModelRetry
        candidate = self.candidate()
        candidate['semantic']['scope'] = '正文段落5122'
        output = BatchExtraction.model_validate({'candidates': [candidate], 'complete': True})
        source = self.batch['sources'][0]
        with self.assertRaises(ModelRetry) as caught:
            _grounded(output, {source['citation']: source['text']},
                {source['citation']: source['text']}, ['企业与竞争'])
        feedback = _local_retry_feedback(output, caught.exception.message)
        self.assertIn('candidates[0]字段semantic.scope(未支持数字:5122)', feedback)
        self.assertIn('定位编号只能保留在citation', feedback)
        candidate['semantic']['scope'] = ''
        corrected = BatchExtraction.model_validate({'candidates': [candidate], 'complete': True})
        self.assertIs(_grounded(corrected, {source['citation']: source['text']},
            {source['citation']: source['text']}, ['企业与竞争']), corrected)

    async def test_local_retry_still_rejects_changed_quantity_and_keeps_fixed_public_diagnostic(self):
        self.runtime.save_config({'base_url': 'http://127.0.0.1:8091/v1', 'model': 'qwen3.8-27b'})
        text = '第1项计划营造林1亩，尚未批复。'
        self.batch['sources'][0].update(text=text, citation=f'long-plan#chars-1-{len(text)}')
        candidate = self.candidate(summary='第1项计划营造林999亩，尚未批复。')
        candidate.pop('quote')
        candidate.pop('supports')
        candidate.update(citation='S1', support_citations=[])
        with self.assertRaises(ExtractionValidationError) as caught:
            await extract_batch(self.runtime, self.material, self.batch,
                model_override=self.model(candidates=[candidate]))
        self.assertEqual(caught.exception.category, 'numeric_or_nature')
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('999', str(caught.exception))

    async def test_local_retry_distinguishes_internal_table_number_from_real_quantity(self):
        from .extraction_model import BatchExtraction, _local_retry_feedback
        candidate = self.candidate(summary='表150中，第1项计划营造林1亩，尚未批复。')
        output = BatchExtraction.model_validate({'candidates': [candidate], 'complete': True})
        feedback = _local_retry_feedback(output, '数字校验失败')
        self.assertIn('summary(未支持数字:150)', feedback)
        self.assertNotIn('未支持数字:1)', feedback)
        self.assertIn('保留该行真正的技术要求', feedback)

    async def test_local_retry_points_to_available_header_without_adding_support_itself(self):
        from .extraction_model import BatchExtraction, _local_retry_feedback
        candidate = self.candidate()
        candidate['semantic']['period'] = '2027年'
        output = BatchExtraction.model_validate({'candidates': [candidate], 'complete': True})
        feedback = _local_retry_feedback(output, '数字校验失败', {'C1': '2027年实施计划', 'C2': '其他标题'})
        self.assertIn('输入编号C1', feedback)
        self.assertIn('不得只因数字相同而引用', feedback)
        self.assertEqual(output.candidates[0].supports, [])

    async def test_local_unknown_support_reference_still_rejects_entire_batch(self):
        self.runtime.save_config({'base_url': 'http://localhost:8091/v1', 'model': 'qwen3.8-27b'})
        text = '\n'.join(self.batch['sources'][0]['text'].splitlines()[:5])
        self.batch['sources'][0].update(text=text, citation=f'long-plan#chars-1-{len(text)}')
        candidate = self.candidate()
        candidate.pop('quote')
        candidate.pop('supports')
        candidate.update(citation='S1', support_citations=['invented-reference'])
        with self.assertRaises(ExtractionValidationError) as caught:
            await extract_batch(self.runtime, self.material, self.batch,
                model_override=self.model(candidates=[candidate]))
        self.assertEqual(caught.exception.category, 'support_reference')

    async def test_local_large_batch_requests_lossless_split_without_calling_model(self):
        self.runtime.save_config({'base_url': 'http://127.0.0.1:8091/v1', 'model': 'qwen3.8-27b'})
        self.batch['sources'][0]['text'] *= 5
        result = await extract_batch(self.runtime, self.material, self.batch, model_override=self.model())
        self.assertTrue(result['needs_split'])
        self.assertFalse(result['complete'])
        self.assertEqual(self.calls, [])

    def test_local_single_or_json_encoded_container_keeps_every_candidate_and_validates_fields(self):
        candidate = self.candidate()
        candidate.pop('quote')
        candidate.pop('supports')
        candidate.update(citation='S1', support_citations=['S1'])
        for container in (candidate, json.dumps(candidate), json.dumps([candidate, candidate]),
                json.dumps([candidate, candidate]) + '}'):
            result = LocalBatchExtraction.model_validate({'candidates': container, 'complete': True})
            self.assertEqual(len(result.candidates), 2 if isinstance(container, str) and container.startswith('[') else 1)
        with self.assertRaises(ValueError):
            LocalBatchExtraction.model_validate({'candidates': {'arbitrary': candidate}, 'complete': True})
        for suffix in (' {}', ' another-fact', ' ]}'):
            with self.assertRaises(ValueError):
                LocalBatchExtraction.model_validate({'candidates': json.dumps([candidate]) + suffix, 'complete': True})


class BatchDeepSeekWireTests(Fixture, unittest.IsolatedAsyncioTestCase):
    """Use the configured DeepSeek adapter; no real model credentials or network."""

    setUp = BatchExtractionTests.setUp
    tearDown = BatchExtractionTests.tearDown
    candidate = BatchExtractionTests.candidate

    async def test_official_glm_flash_uses_auto_tools_disabled_thinking_and_same_grounding(self):
        self.runtime.save_config({'provider_id': 'zhipu-flash', 'base_url': 'https://open.bigmodel.cn/api/paas/v4',
            'protocol': 'chat_completions', 'model': 'glm-4.7-flash'}, secret='synthetic-glm-key')
        calls = []
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            self.assertEqual(request.url.path, '/api/paas/v4/chat/completions')
            self.assertEqual(body['tool_choice'], 'auto')
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            name = body['tools'][0]['function']['name']
            if name == 'extract_document_batch':
                candidate = self.candidate(citation='S1')
                if len(calls) == 1:
                    candidate['quote'] = '合成不匹配原文'
                output = {'candidates': [candidate], 'findings': [], 'complete': True}
            else:
                output = {'checks': [{'index': 0, 'supported': True}], 'complete': True}
            return httpx.Response(200, json={'id': 'synthetic-glm', 'object': 'chat.completion',
                'created': 1, 'model': 'glm-4.7-flash', 'choices': [{'index': 0,
                    'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                        'tool_calls': [{'id': 'glm-result', 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(output)}}]}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}})
        client = AsyncOpenAI(api_key='synthetic-glm-key', base_url=self.runtime.config()['base_url'],
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=0)
        with patch('web.extraction_model.AsyncOpenAI', return_value=client):
            result = await extract_batch(self.runtime, self.material, self.batch)
        self.assertTrue(result['complete'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['candidates'][0]['citation'], self.batch['sources'][0]['citation'])
        for changes in ({'model': 'glm-4.7-flashx'}, {'protocol': 'responses'},
                {'base_url': 'https://open.bigmodel.cn.untrusted.example'}):
            self.assertNotIn('extra_body', _model_settings({**self.runtime.config(), **changes}, 8192))

    async def test_official_deepseek_disabled_thinking_requires_output_tools_and_roundtrips_aliases(self):
        self.runtime.save_config({'provider_id': 'deepseek-flash', 'base_url': 'https://api.deepseek.com',
            'protocol': 'chat_completions', 'model': 'deepseek-flash', 'request_limit': 1},
            secret='synthetic-test-key')
        requests = []
        configuration = self.runtime.config()

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body['tool_choice'], 'required')
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            tool = body['tools'][0]['function']['name']
            if tool == 'extract_document_batch':
                candidate = self.candidate()
                candidate['citation'] = 'S1'
                if len(requests) == 1:
                    candidate['quote'] = '不存在的原文'
                output = {'candidates': [candidate], 'findings': [], 'complete': True,
                    'processed_no_evidence': False}
            else:
                output = {'checks': [{'index': 0, 'supported': True, 'reason': ''}], 'complete': True}
            return httpx.Response(200, json={'id': 'synthetic-completion', 'object': 'chat.completion',
                'created': 1, 'model': 'deepseek-flash', 'choices': [{'index': 0,
                    'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'batch-output',
                        'type': 'function', 'function': {'name': tool, 'arguments': json.dumps(output)}}]}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}})

        client = AsyncOpenAI(api_key='synthetic-test-key', base_url='https://api.deepseek.com', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        generic = configured_model(self.runtime.config(), client)
        profile_before = dict(generic.profile)
        with patch('web.extraction_model.AsyncOpenAI', return_value=client):
            result = await extract_batch(self.runtime, self.material, self.batch)
        self.assertTrue(result['complete'])
        self.assertEqual(len(requests), 3)  # not the research request_limit=1
        retry_assistant = next(item for item in requests[1]['messages'] if item['role'] == 'assistant')
        self.assertFalse(retry_assistant.get('reasoning_content'))
        self.assertEqual(result['usage']['requests'], 3)
        self.assertEqual(self.runtime.config(), configuration)
        self.assertEqual(dict(generic.profile), profile_before)
        self.assertFalse(generic.profile['supports_forced_tool_choice'])
        self.assertEqual(result['candidates'][0]['citation'], self.batch['sources'][0]['citation'])
        self.assertEqual(result['candidates'][0]['supports'][0]['citation'], self.batch['sources'][0]['citation'])
        prompt = json.loads(next(m['content'] for m in requests[0]['messages'] if m['role'] == 'user'))
        self.assertEqual(prompt['sources'][0]['citation'], 'S1')
        self.assertEqual(prompt['sources'][0]['text'], self.batch['sources'][0]['text'])
        self.assertNotIn(self.batch['sources'][0]['citation'], json.dumps(requests, ensure_ascii=False))
        self.assertEqual([body['tools'][0]['function']['name'] for body in requests],
            ['extract_document_batch', 'extract_document_batch', 'verify_document_support'])

    def test_thinking_override_is_only_for_official_deepseek_chat_completions(self):
        official = {'base_url': 'https://api.deepseek.com', 'protocol': 'chat_completions'}
        expected = {'max_tokens': 8192, 'extra_body': {'thinking': {'type': 'disabled'}}}
        self.assertEqual(_model_settings(official, 8192), expected)
        for cfg in ({**official, 'protocol': 'responses'},
                    {**official, 'base_url': 'https://gateway.example/v1'},
                    {**official, 'base_url': 'http://127.0.0.1:1234/v1'},
                    {**official, 'base_url': 'https://api.deepseek.com.untrusted.example'}):
            with self.subTest(cfg=cfg):
                settings = _model_settings(cfg, 8192)
                self.assertNotIn('extra_body', settings)
                if cfg['base_url'].startswith('http://127.0.0.1:'):
                    self.assertEqual(settings, {'max_tokens': 4096, 'temperature': 0})
                else:
                    self.assertEqual(settings, {'max_tokens': 8192})
        self.assertEqual(official, {'base_url': 'https://api.deepseek.com', 'protocol': 'chat_completions'})


if __name__ == '__main__':
    unittest.main()
