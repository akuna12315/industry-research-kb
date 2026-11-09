from copy import deepcopy
import unittest

from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from .test_web import Fixture
from .agent_runtime import AgentRuntime, ResearchContext
from .charts import register_evidence
from .research_pages import PageSpec, ResearchPages, stored_spec, validate_page, visual_draft
from .visual_pages import VisualPlan, VisualScene
from .service import kb


def visual_spec_for(ref, kind='flow'):
    item = {'references': [ref], 'quote': '企业披露合同，待核对履约进度。'}
    scene = {'id': 'research', 'kind': kind, 'title': '从披露到履约', 'emphasis': 'primary',
             'nodes': [{'id': 'notice', 'label': '合同披露', 'icon': 'document', 'detail': '来源记载合同披露。', **item},
                       {'id': 'progress', 'label': '履约进度', 'icon': 'workflow', 'detail': '履约进度仍待核对。', **item}],
             'edges': [{'source': 'notice', 'target': 'progress', 'label': '后续调查重点',
                        'relation': 'sequence' if kind == 'flow' else 'related', 'nature': 'inference'}]}
    if kind == 'comparison':
        scene.update(edges=[], dimensions=[{'id': 'known', 'label': '已知信息'}], cells=[
            {'node': n, 'dimension': 'known', 'text': text, **item}
            for n, text in [('notice', '来源披露合同'), ('progress', '待核对履约进度')]])
    return {'schema_version': 2, 'title': '合同可视化', 'question': '如何理解合同进展？',
            'summary': '披露与履约分开理解。', 'summary_references': [ref],
            'coverage': '合成测试资料片段，不代表真实调查。', 'scenes': [scene]}


def plan_for(spec):
    return {**{k: v for k, v in spec.items() if k not in ('schema_version', 'scenes')},
            'scenes': [{k: s[k] for k in ('id', 'kind', 'title')} for s in spec['scenes']]}


class VisualPageTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.ctx = ResearchContext(self.store, 'alpha', generate_page=True)
        self.ref = register_evidence(self.ctx, self.store.evidence('alpha', '合同'))[0]['chart_ref']
        self.pages = ResearchPages(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def validate(self, spec):
        return validate_page(self.ctx, PageSpec.model_validate(spec))

    def test_different_visual_families_are_serializable_and_manageable(self):
        for kind in ('flow', 'architecture', 'concept', 'comparison'):
            payload = self.validate(visual_spec_for(self.ref, kind))
            p = self.pages.commit('alpha', payload, '生成可视化页面', context=self.ctx)
            self.assertEqual(p['visual_preview']['kind'], kind)
            copied = self.pages.mutate(p['id'], 'copy', 1)
            self.assertEqual(self.pages.detail(copied['id'])['revision']['content']['schema_version'], 2)
            self.pages.mutate(p['id'], 'metadata', 1, title='新名称')
            restored = self.pages.mutate(p['id'], 'restore', 2, revision=1)
            self.assertEqual(restored['version'], 3)
            self.assertEqual(self.pages.detail(p['id'])['revision']['content']['title'], '合同可视化')

    def test_duplicate_and_dangling_structure_and_flow_cycles_are_rejected(self):
        for change in (
            lambda s: s['scenes'].append(deepcopy(s['scenes'][0])),
            lambda s: s['scenes'][0]['nodes'].append(deepcopy(s['scenes'][0]['nodes'][0])),
            lambda s: s['scenes'][0]['edges'][0].update(target='missing'),
            lambda s: s['scenes'][0]['edges'].append({'source': 'progress', 'target': 'notice', 'label': '回到起点', 'nature': 'inference'}),
            lambda s: s['scenes'][0].update(edges=[]),
            lambda s: s.update(question=''),
        ):
            spec = visual_spec_for(self.ref); change(spec)
            with self.assertRaises(ValueError):
                self.validate(spec)

    def test_relationship_quotes_and_node_quotes_require_actual_sources(self):
        for change in (
            lambda s: s['scenes'][0]['nodes'][0].update(quote='原文没有的结论'),
            lambda s: s['scenes'][0]['nodes'][0].update(references=['forged']),
            lambda s: s['scenes'][0]['edges'][0].update(nature='reported'),
            lambda s: s['scenes'][0]['edges'][0].update(nature='reported', references=[self.ref], quote='虚构关系'),
        ):
            spec = visual_spec_for(self.ref); change(spec)
            with self.assertRaises(ValueError):
                self.validate(spec)

    def test_unknown_values_are_explicit_and_comparison_must_be_complete(self):
        spec = visual_spec_for(self.ref, 'comparison')
        spec['scenes'][0]['cells'][1].update(nature='unknown', references=[], quote='', text='未获得数据')
        self.validate(spec)
        spec['scenes'][0]['cells'].pop()
        with self.assertRaisesRegex(ValueError, 'cell'):
            self.validate(spec)

    def test_untrusted_code_and_icon_names_and_unverified_numbers_cannot_become_diagrams(self):
        for key, value in (('icon', 'javascript:alert(1)'), ('onclick', 'alert(1)'), ('value', 98)):
            spec = visual_spec_for(self.ref)
            spec['scenes'][0]['nodes'][0][key] = value
            with self.assertRaises(ValidationError):
                self.validate(spec)

    def test_chart_scene_still_rejects_unsupported_numeric_data(self):
        spec = visual_spec_for(self.ref)
        spec['scenes'] = [{'id': 'figure', 'kind': 'chart', 'title': '虚构数量', 'chart': {
            'kind': 'bar', 'title': '数量', 'metric': '合同数量', 'unit': '个', 'geography': '中国',
            'scope': '合同', 'basis': '年度', 'items': [{'label': '合同', 'value': 999, 'references': [self.ref]}]}}]
        with self.assertRaises(ValueError):
            self.validate(spec)

    def test_incremental_completion_requires_every_scene(self):
        spec = visual_spec_for(self.ref)
        self.ctx.visual_plan = VisualPlan.model_validate(plan_for(spec))
        with self.assertRaisesRegex(ValueError, '未完成'):
            visual_draft(self.ctx)
        self.ctx.visual_scenes = {'research': VisualScene.model_validate(spec['scenes'][0])}
        self.assertEqual(visual_draft(self.ctx)['content']['schema_version'], 2)

    def test_bounded_support_beyond_source_preview_survives_layout_roundtrip(self):
        self.store.import_evidence('alpha', {**self.record, 'summary': '背景说明。' * 400 + '企业披露合同，待核对履约进度。合同有待后续研究。'})
        ctx = ResearchContext(self.store, 'alpha', generate_page=True)
        ref = register_evidence(ctx, self.store.evidence('alpha', '合同'))[0]['chart_ref']
        spec = visual_spec_for(ref)
        second = deepcopy(spec['scenes'][0])
        second.update(id='second', title='另一个视角', emphasis='normal')
        for node in second['nodes']:
            node['quote'] = '合同有待后续研究。'
        spec['scenes'].append(second)
        payload = validate_page(ctx, PageSpec.model_validate(spec))
        self.assertNotIn('企业披露合同', payload['sources'][0]['text'])
        self.assertIn('企业披露合同，待核对履约进度。', payload['sources'][0]['support_quotes'])
        self.assertIn('合同有待后续研究。', payload['sources'][0]['support_quotes'])
        page = self.pages.commit('alpha', payload, '生成展示页', context=ctx)
        edit = ResearchContext(self.store, 'alpha', generate_page=True, page_operation='layout')
        self.pages.prepare_edit(edit, page['id'], 1)
        edited = validate_page(edit, stored_spec(payload['content']))
        self.assertEqual(edited['sources'][0]['support_quotes'], payload['sources'][0]['support_quotes'])

    def test_scene_edit_preserves_other_scenes_and_rechecks_source_at_commit(self):
        spec = visual_spec_for(self.ref)
        second = deepcopy(spec['scenes'][0]); second.update(id='second', title='系统组成', kind='architecture', emphasis='normal')
        spec['scenes'].append(second)
        p = self.pages.commit('alpha', self.validate(spec), '原始研究要求')
        ctx = ResearchContext(self.store, 'alpha', generate_page=True, page_operation='layout', page_scene_id='research')
        self.pages.prepare_edit(ctx, p['id'], 1)
        change = deepcopy(spec); change['scenes'] = change['scenes'][:1]
        change['scenes'][0]['nodes'][0]['detail'] = '调整后的解释'
        ctx.visual_plan = VisualPlan.model_validate(plan_for(change))
        ctx.visual_scenes = {'research': VisualScene.model_validate(change['scenes'][0])}
        payload = visual_draft(ctx)
        self.assertEqual(payload['content']['scenes'][1], stored_spec(spec).model_dump()['scenes'][1])
        self.pages.commit('alpha', payload, '仅调整图形', p['id'], 1, 'layout', context=ctx)
        self.assertEqual(self.pages.get(p['id'])['research_request'], '原始研究要求')
        self.store.import_evidence('alpha', {**self.record, 'summary': '新版内容'})
        with self.assertRaisesRegex(ValueError, '版本'):
            self.pages.commit('alpha', payload, '再次修改', p['id'], 2, 'layout', context=ctx)
        self.assertEqual(self.pages.get(p['id'])['version'], 2)


class VisualAgentTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()

    def tearDown(self):
        self.temp.cleanup()

    async def generate(self, kind='flow', stop_early=False):
        runtime = AgentRuntime(self.store)
        job = {'id': kb.uuid4().hex, 'conversation': kb.uuid4().hex, 'industry': 'alpha',
               'research': False, 'generate_page': True, 'status': 'running', 'events': [], 'started_at': kb.now()}
        def model(messages, info):
            returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            names = [p.tool_name for p in returns]
            if not returns:
                self.assertTrue({'plan_visual_page', 'add_visual_scene', 'finish_visual_page'} <= {t.name for t in info.function_tools})
                return ModelResponse(parts=[ToolCallPart('search_evidence', {'query': '合同'})])
            ref = next(p.content[0]['chart_ref'] for p in returns if p.tool_name == 'search_evidence')
            spec = visual_spec_for(ref, kind)
            if 'plan_visual_page' not in names:
                return ModelResponse(parts=[ToolCallPart('plan_visual_page', {'plan': plan_for(spec)})])
            if stop_early:
                return ModelResponse(parts=[TextPart('提前停止')])
            if 'add_visual_scene' not in names:
                return ModelResponse(parts=[ToolCallPart('add_visual_scene', {'scene': spec['scenes'][0]})])
            if 'finish_visual_page' not in names:
                return ModelResponse(parts=[ToolCallPart('finish_visual_page', {})])
            return ModelResponse(parts=[TextPart('可视化页面已准备完成。')])
        await runtime.run(job, '生成可视化研究展示页', model_override=FunctionModel(model))
        return job

    async def test_real_tool_chain_creates_three_different_visual_structures(self):
        for kind in ('flow', 'architecture', 'comparison'):
            job = await self.generate(kind)
            self.assertEqual(job['status'], 'completed', job['events'])
            p = next(e['page'] for e in job['events'] if e['type'] == 'page_saved')
            self.assertEqual(ResearchPages(self.store).detail(p['id'])['revision']['content']['scenes'][0]['kind'], kind)
        self.assertEqual(len(ResearchPages(self.store).listing()), 3)

    async def test_unfinished_design_is_not_saved(self):
        job = await self.generate(stop_early=True)
        self.assertEqual(job['status'], 'paused')
        self.assertEqual(ResearchPages(self.store).listing(), [])

    async def test_ordinary_conversation_hides_all_visual_tools(self):
        def model(messages, info):
            self.assertFalse({'plan_visual_page', 'add_visual_scene', 'finish_visual_page'} & {t.name for t in info.function_tools})
            return ModelResponse(parts=[TextPart('只回答问题')])
        job = {'id': kb.uuid4().hex, 'conversation': kb.uuid4().hex, 'industry': 'alpha', 'research': False,
               'status': 'running', 'events': [], 'started_at': kb.now()}
        await AgentRuntime(self.store).run(job, '解释概念', model_override=FunctionModel(model))
        self.assertEqual(job['status'], 'completed', job['events'])
