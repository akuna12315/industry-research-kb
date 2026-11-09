"""Synthetic quantitative sources only; no user materials or external API calls."""
from pathlib import Path
import unittest

from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from .test_web import Fixture
from .agent_runtime import AgentRuntime, ResearchContext
from .charts import ChartPoint, ResearchChart, register_evidence, validate_chart
from .research_pages import PageSpec, ResearchPages, validate_page
from .test_visual_pages import plan_for
from .visual_skill import visual_skill_instructions
from .service import kb


QUOTE = 'UDDS工况，测试电池的RMSE，单位%，同工况比较：算法A为1.2，算法B为2.4。'
PIE_QUOTE = '年度样本，测试样本的数量，单位个，全部分类：样本总体共100个，正常80个，异常20个，未知0个。'


def category(ref):
    return ResearchChart(kind='bar', axis='category', title='算法误差比较', period='UDDS工况',
        metric='RMSE', unit='%', geography='中国', scope='测试电池', basis='同工况比较', nature='reported',
        items=[ChartPoint(label=label, value=value, references=[ref], quote=QUOTE)
               for label, value in [('算法A', 1.2), ('算法B', 2.4)]])


def table_spec(ref):
    return {'schema_version': 2, 'title': '误差比较', 'question': '如何比较算法？',
        'summary': '同工况下比较RMSE。', 'summary_references': [ref], 'coverage': '离线合成数据。',
        'scenes': [{'id': 'metrics', 'kind': 'table', 'title': '精确数值', 'emphasis': 'primary',
            'nodes': [{'id': key, 'label': label, 'references': [ref], 'quote': QUOTE}
                      for key, label in [('a', '算法A'), ('b', '算法B')]],
            'dimensions': [{'id': 'rmse', 'label': 'RMSE（%）'}, {'id': 'remark', 'label': '说明'}],
            'cells': [{'node': key, 'dimension': dim, 'text': text, 'references': [ref], 'quote': QUOTE}
                for key, value in [('a', '1.2'), ('b', '2.4')]
                for dim, text in [('rmse', value), ('remark', '同工况比较')]]}]}


class QuantitativeTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.ctx = ResearchContext(self.store, 'alpha', generate_page=True)
        self.ref = self.source(QUOTE)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, text):
        key = 'quant-' + kb.uuid4().hex[:8]
        self.store.import_evidence('alpha', {**self.record, 'id': key, 'summary': text, 'region': '中国'})
        return next(r['chart_ref'] for r in register_evidence(self.ctx, self.store.evidence('alpha', '', limit=50)) if r['id'] == key)

    def pie(self):
        ref = self.source(PIE_QUOTE)
        return ResearchChart(kind='pie', axis='category', title='样本组成', period='年度样本',
            metric='数量', unit='个', geography='中国', scope='测试样本', basis='全部分类', nature='reported',
            whole='样本总体', total=100, items=[ChartPoint(label=label, value=value, references=[ref], quote=PIE_QUOTE)
                for label, value in [('正常', 80), ('异常', 20), ('未知', 0)]])

    def test_category_comparison_preserves_shared_experiment_without_fake_dates(self):
        result = validate_chart(self.ctx, category(self.ref))
        self.assertEqual(result['axis'], 'category')
        self.assertEqual([p['value'] for p in result['items']], [1.2, 2.4])
        self.assertTrue(all(p['date'] is None for p in result['items']))
        self.assertEqual(result['nature'], 'reported')

    def test_research_fitting_is_not_misclassified_as_a_future_plan(self):
        from .charts import _has_plan_language
        self.assertFalse(_has_plan_language('模拟实验的拟合结果采用目标函数评估'))
        self.assertTrue(_has_plan_language('拟完成试验，目标为20'))

    def test_category_cannot_be_line_or_have_fabricated_values_or_conditions(self):
        for change in (lambda c: setattr(c, 'kind', 'line'), lambda c: setattr(c.items[0], 'value', 9.8),
                       lambda c: setattr(c, 'period', 'WLTC'), lambda c: setattr(c.items[0], 'label', '不存在算法'),
                       lambda c: setattr(c, 'nature', 'actual')):
            chart = category(self.ref); change(chart)
            with self.assertRaises(ValueError):
                validate_chart(self.ctx, chart)

    def test_number_axis_requires_real_distinct_x_and_preserves_spacing_order(self):
        quote = '循环实验，测试电池的容量，单位Ah，同条件：循环次数单位次，10次为1.2Ah，100次为1.0Ah。'
        ref = self.source(quote)
        chart = ResearchChart(kind='line', axis='number', title='循环容量', period='循环实验',
            metric='容量', unit='Ah', geography='中国', scope='测试电池', basis='同条件', nature='reported',
            x_metric='循环次数', x_unit='次', items=[ChartPoint(label=str(x), x=x, value=value, quote=quote, references=[ref])
                for x, value in [(100, 1.0), (10, 1.2)]])
        self.assertEqual([p['x'] for p in validate_chart(self.ctx, chart)['items']], [10, 100])
        chart.items[0].x = 99
        with self.assertRaises(ValueError):
            validate_chart(self.ctx, chart)
        chart.items[0].x = 10
        with self.assertRaisesRegex(ValueError, '不重复'):
            validate_chart(self.ctx, chart)

    def test_pie_shares_are_computed_from_original_values_with_zero_preserved(self):
        result = validate_chart(self.ctx, self.pie())
        self.assertEqual([p['share'] for p in result['items']], [80, 20, 0])
        self.assertEqual(result['whole'], '样本总体')

    def test_pie_rejects_partial_negative_missing_and_unquoted_denominator(self):
        for change in (lambda c: c.items.pop(), lambda c: setattr(c.items[0], 'value', -80),
                       lambda c: setattr(c.items[0], 'value', None), lambda c: setattr(c, 'whole', '另一总体'),
                       lambda c: setattr(c, 'total', 101), lambda c: setattr(c, 'axis', 'time')):
            chart = self.pie(); change(chart)
            if len(chart.items) == 2: chart.items[0].value = 70
            with self.assertRaises(ValueError):
                validate_chart(self.ctx, chart)
        for value in (True, float('nan'), float('inf')):
            with self.assertRaises(ValidationError):
                ResearchChart(kind='pie', title='invalid', total=value, items=[ChartPoint(label='x', references=[self.ref])])

    def test_table_persists_copies_and_rejects_invented_numeric_inference(self):
        pages = ResearchPages(self.store)
        spec = table_spec(self.ref)
        saved = pages.commit('alpha', validate_page(self.ctx, PageSpec.model_validate(spec)), '生成表格', context=self.ctx)
        copied = pages.mutate(saved['id'], 'copy', 1)
        scene = pages.detail(copied['id'])['revision']['content']['scenes'][0]
        self.assertEqual(scene['kind'], 'table')
        self.assertEqual(len(scene['cells']), 4)
        spec['scenes'][0]['cells'][0].update(text='99.8', nature='inference')
        with self.assertRaisesRegex(ValueError, '数值'):
            validate_page(self.ctx, PageSpec.model_validate(spec))
        spec = table_spec(self.ref); spec['scenes'][0]['cells'].pop()
        with self.assertRaisesRegex(ValueError, 'cell'):
            validate_page(self.ctx, PageSpec.model_validate(spec))

    def test_bundled_skill_is_utf8_and_packaged_as_explicit_public_resources(self):
        instructions = visual_skill_instructions()
        self.assertIn('饼图', instructions)
        self.assertIn('plan_visual_page', instructions)
        self.assertNotIn('description:', instructions)
        import importlib.util
        spec = importlib.util.spec_from_file_location('source_release', Path(__file__).resolve().parents[1] / 'packaging/source_release.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        files = module.source_files()
        self.assertIn('web/skills/research-visual-report/SKILL.md', files)
        self.assertNotIn('config/agent.json', files)


class SkillAgentTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup()
    def tearDown(self): self.temp.cleanup()

    async def test_skill_guides_staged_chart_and_table_tools_before_atomic_save(self):
        self.store.import_evidence('alpha', {**self.record, 'id': 'quant', 'summary': QUOTE, 'region': '中国'})
        job = {'id': kb.uuid4().hex, 'conversation': kb.uuid4().hex, 'industry': 'alpha',
               'research': False, 'generate_page': True, 'status': 'running', 'events': [], 'started_at': kb.now()}
        def model(messages, info):
            if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
                self.assertIn('图表选择与数据契约', str(messages))
                return ModelResponse(parts=[ToolCallPart('search_evidence', {'query': 'RMSE'})])
            returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            names = [p.tool_name for p in returns]
            ref = next(p.content[0]['chart_ref'] for p in returns if p.tool_name == 'search_evidence')
            spec = table_spec(ref)
            spec['scenes'].append({'id': 'bars', 'kind': 'chart', 'title': '误差对比', 'chart': category(ref).model_dump()})
            if 'plan_visual_page' not in names:
                return ModelResponse(parts=[ToolCallPart('plan_visual_page', {'plan': plan_for(spec)})])
            count = names.count('add_visual_scene')
            if count < 2:
                return ModelResponse(parts=[ToolCallPart('add_visual_scene', {'scene': spec['scenes'][count]})])
            if 'finish_visual_page' not in names:
                return ModelResponse(parts=[ToolCallPart('finish_visual_page', {})])
            return ModelResponse(parts=[TextPart('研究图表页面已完成。')])
        await AgentRuntime(self.store).run(job, '制作图表展示网页', model_override=FunctionModel(model))
        self.assertEqual(job['status'], 'completed', job['events'])
        page = next(e['page'] for e in job['events'] if e['type'] == 'page_saved')
        scenes = ResearchPages(self.store).detail(page['id'])['revision']['content']['scenes']
        self.assertEqual([s['kind'] for s in scenes], ['table', 'chart'])
        self.assertEqual(scenes[1]['figure']['axis'], 'category')
