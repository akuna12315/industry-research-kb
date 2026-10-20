import json
import math
import unittest

from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from .test_web import Fixture
from .service import kb
from .knowledge import Knowledge
from .agent_runtime import AgentRuntime, ResearchContext
from .charts import ChartPoint, ResearchChart, register_evidence, register_materials, validate_chart


class ChartTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.ctx = ResearchContext(self.store, 'alpha')
        self.store.import_evidence('alpha', {**self.record, 'event_date': '2025-01-03', 'published_date': '2025-01-10'})
        self.ref = register_evidence(self.ctx, self.store.evidence('alpha', '合同'))[0]['chart_ref']

    def tearDown(self):
        self.temp.cleanup()

    def timeline(self, **point):
        return ResearchChart(kind='timeline', title='合同时间脉络', items=[ChartPoint(
            label='合同披露', references=[self.ref], **point)])

    def test_known_and_unknown_dates_and_date_kinds(self):
        result = validate_chart(self.ctx, ResearchChart(kind='timeline', title='时间线', items=[
            ChartPoint(label='时间待核对', status='uncertain', references=[self.ref]),
            ChartPoint(label='公开发布', date='2025-01-10', date_kind='published', references=[self.ref]),
            ChartPoint(label='事件发生', date='2025-01-03', references=[self.ref])]))
        self.assertEqual([p['date'] for p in result['items']], ['2025-01-03', '2025-01-10', None])
        self.assertEqual(result['items'][-1]['date_kind'], 'unknown')
        self.assertEqual(result['items'][1]['sources'][0]['published_date'], '2025-01-10')
        self.assertIn('不代表完整覆盖', result['coverage_note'])

    def test_checked_date_and_wrong_date_kind_cannot_be_borrowed(self):
        for day in (self.record['checked_date'], '2025-01-10', '2024-02-01'):
            with self.subTest(day=day), self.assertRaisesRegex(ValueError, '日期'):
                validate_chart(self.ctx, self.timeline(date=day, date_kind='event'))

    def test_partial_dates_keep_precision_without_inventing_days(self):
        result = validate_chart(self.ctx, self.timeline(date='2025-01'))
        self.assertEqual(result['items'][0]['date'], '2025-01')

    def test_unknown_or_previous_turn_reference_rejected(self):
        with self.assertRaisesRegex(ValueError, '本轮'):
            validate_chart(ResearchContext(self.store, 'alpha'), self.timeline(date='2025'))

    def test_unauthorized_material_never_registered_and_revocation_blocks_chart(self):
        knowledge = Knowledge(self.store)
        record = knowledge.save('alpha', {'title': '资料日期', 'content': '2025年1月3日实际发生，原文定位。'})
        self.assertEqual(register_materials(self.ctx, knowledge.search('alpha', '原文定位')), [])
        knowledge.save('alpha', {'id': record['id'], 'model_allowed': True})
        found = register_materials(self.ctx, knowledge.search('alpha', '原文定位'))
        ref = found[0]['snippets'][0]['chart_ref']
        chart = ResearchChart(kind='timeline', title='资料事件', items=[ChartPoint(label='事件', date='2025-01-03', references=[ref])])
        self.assertEqual(validate_chart(self.ctx, chart)['items'][0]['sources'][0]['citation'], found[0]['snippets'][0]['citation'])
        knowledge.save('alpha', {'id': record['id'], 'model_allowed': False})
        with self.assertRaisesRegex(ValueError, '撤回'):
            validate_chart(self.ctx, chart)

    def test_other_archive_reference_is_rejected(self):
        self.ctx.chart_sources[self.ref]['industry_id'] = 'unselected'
        with self.assertRaisesRegex(ValueError, '档案'):
            validate_chart(self.ctx, self.timeline(date='2025'))

    def test_finite_values_calendar_and_size_limits(self):
        for value in (True, '2', math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ChartPoint(label='数值', value=value, references=[self.ref])
        for day in ('2025-02-30', '2025-13', '0000', '2025年', ''):
            with self.subTest(day=day), self.assertRaises(ValidationError):
                self.timeline(date=day)
        with self.assertRaises(ValidationError):
            ResearchChart(kind='timeline', title='太多节点', items=[ChartPoint(label='x', references=[self.ref])]*31)

    def test_chart_budget_is_per_turn(self):
        for _ in range(3):
            validate_chart(self.ctx, self.timeline(date='2025'))
        with self.assertRaisesRegex(ValueError, '3幅'):
            validate_chart(self.ctx, self.timeline(date='2025'))

    def test_future_nodes_and_planned_sources_are_not_past_actual_events(self):
        self.ctx.chart_sources[self.ref].update(event_date='2099-01-01', status='planned')
        with self.assertRaisesRegex(ValueError, '计划'):
            validate_chart(self.ctx, self.timeline(date='2099-01-01'))
        chart = self.timeline(date='2099-01-01', status='planned')
        self.assertEqual(validate_chart(self.ctx, chart)['items'][0]['status'], 'planned')

    def test_fabricated_quote_rejected_even_for_a_registered_date(self):
        chart = self.timeline(date='2025', quote='完全没有在原文出现的句子')
        with self.assertRaisesRegex(ValueError, '逐字'):
            validate_chart(self.ctx, chart)

    def trend(self, **changes):
        self.ctx = ResearchContext(self.store, 'alpha')
        items = []
        contract = {'metric': '造林面积', 'unit': '公顷', 'geography': '中国', 'scope': '示范林', 'basis': '年度实际完成', 'nature': 'actual'}
        for year, value in (('2023', 0), ('2025', 1200)):
            metric = {'name': contract['metric'], **{k:v for k,v in contract.items() if k!='metric'}, 'period': year, 'value': value}
            self.store.import_evidence('alpha', {**self.record, 'id': 'metric-'+year, 'metrics': [metric]})
            found = register_evidence(self.ctx, self.store.evidence('alpha', '', limit=50))
            ref = next(r['chart_ref'] for r in found if r['id']=='metric-'+year)
            items.append(ChartPoint(label=year+'面积', date=year, value=value, references=[ref]))
        return ResearchChart(kind='line', title='实际面积变化', items=items, **{**contract, **changes})

    def test_registered_metrics_use_exact_common_scope_and_preserve_zero(self):
        chart = self.trend()
        result = validate_chart(self.ctx, chart)
        self.assertEqual([p['value'] for p in result['items']], [0, 1200])
        self.assertTrue(all(p['support']=='registered-metric' for p in result['items']))

    def test_reported_actual_metric_keeps_reported_status(self):
        chart = self.trend()
        for source in self.ctx.chart_sources.values():
            for metric in source.get('metrics') or []:
                metric['nature'] = 'reported_actual'
        result = validate_chart(self.ctx, chart)
        self.assertEqual(result['nature'], 'actual')
        self.assertTrue(all(p['status']=='reported' for p in result['items']))

    def test_incompatible_units_scope_and_plan_are_rejected(self):
        for changes in ({'unit':'亩'}, {'scope':'全省所有林地'}, {'basis':'累计完成'}, {'nature':'planned'}):
            with self.subTest(changes=changes):
                chart = self.trend(**changes)
                with self.assertRaises(ValueError):
                    validate_chart(self.ctx, chart)

    def test_missing_values_not_zero_and_no_duplicate_or_mixed_periods(self):
        chart = self.trend()
        missing_quote = '2024年，中国示范林造林面积，单位公顷，年度实际完成数值未公布。'
        self.store.import_evidence('alpha', {**self.record, 'id':'metric-missing', 'summary':missing_quote})
        ref = next(r['chart_ref'] for r in register_evidence(self.ctx, self.store.evidence('alpha','',limit=50)) if r['id']=='metric-missing')
        chart.items.append(ChartPoint(label='2024缺口', date='2024', value=None, quote=missing_quote, references=[ref]))
        result = validate_chart(self.ctx, chart)
        self.assertIsNone(result['items'][1]['value'])
        chart.items[-1].date='2023'
        with self.assertRaisesRegex(ValueError, '重复'):
            validate_chart(self.ctx, chart)
        chart.items[-1].date='2024-01'
        with self.assertRaisesRegex(ValueError, '粒度'):
            validate_chart(self.ctx, chart)

    def test_document_number_requires_verbatim_complete_quote(self):
        knowledge = Knowledge(self.store)
        text = '2023年中国示范林造林面积1200公顷，年度实际完成。2025年中国示范林造林面积1500公顷，年度实际完成。'
        knowledge.save('alpha', {'title':'年度记录','content':text,'region':'中国','model_allowed':True})
        ref = register_materials(self.ctx, knowledge.search('alpha','造林面积'))[0]['snippets'][0]['chart_ref']
        chart = ResearchChart(kind='line',title='原文提取趋势',metric='造林面积',unit='公顷',geography='中国',scope='示范林',basis='年度实际完成',items=[
            ChartPoint(label='2023',date='2023',value=1200,quote='2023年中国示范林造林面积1200公顷，年度实际完成。',references=[ref]),
            ChartPoint(label='2025',date='2025',value=1500,quote='2025年中国示范林造林面积1500公顷，年度实际完成。',references=[ref])])
        self.assertEqual(validate_chart(self.ctx,chart)['items'][0]['support'],'quoted-extraction')
        chart.items[1].value=999
        with self.assertRaisesRegex(ValueError,'口径'):
            validate_chart(self.ctx,chart)
        chart.items[1].value=2025
        with self.assertRaisesRegex(ValueError,'口径'):
            validate_chart(self.ctx,chart)

    def test_plotting_never_archives_or_accepts_credentials(self):
        before = set((self.store.home('alpha')/'evidence').glob('*.json'))
        chart=self.timeline(date='2025')
        validate_chart(self.ctx,chart)
        self.assertEqual(set((self.store.home('alpha')/'evidence').glob('*.json')),before)
        chart.title='sk-'+'a'*32
        with self.assertRaisesRegex(ValueError,'凭据'):
            validate_chart(self.ctx,chart)


class ChartAgentTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.marker='chart-private-fixture-excerpt'
        self.store.import_evidence('alpha',{**self.record,'summary':self.marker,'event_date':'2025-01-03'})

    def tearDown(self):
        self.temp.cleanup()

    async def chart_job(self, save_history=False):
        Knowledge(self.store).save_settings({'save_conversations':save_history})
        runtime=AgentRuntime(self.store)
        job={'id':kb.uuid4().hex,'conversation':kb.uuid4().hex,'industry':'alpha','research':False,'status':'running','events':[],'started_at':kb.now()}
        def model(messages,info):
            returns=[p for m in messages for p in m.parts if isinstance(p,ToolReturnPart)]
            if not returns:
                return ModelResponse(parts=[ToolCallPart('search_evidence',{'query':'合同'},tool_call_id='search')])
            if not any(p.tool_name=='draw_research_chart' for p in returns):
                ref=next(p.content[0]['chart_ref'] for p in returns if p.tool_name=='search_evidence')
                return ModelResponse(parts=[ToolCallPart('draw_research_chart',{'chart':{'kind':'timeline','title':'合同事件','items':[{'label':'披露合同','date':'2025-01-03','references':[ref]}]}},tool_call_id='draw')])
            return ModelResponse(parts=[TextPart('已绘制合同事件，依据见图。')])
        await runtime.run(job,'整理合同时间线并画图',model_override=FunctionModel(model))
        self.assertEqual(job['status'],'completed',job['events'])
        return job

    async def test_real_tools_emit_chart_in_readonly_and_logs_exclude_payload(self):
        job=await self.chart_job()
        chart=next(e['chart'] for e in job['events'] if e['type']=='chart')
        self.assertEqual(chart['items'][0]['sources'][0]['excerpt'],self.marker)
        self.assertEqual(next(e for e in job['events'] if e['type']=='done')['archived_ids'],[])
        saved=kb.read_json(self.root/'reports/agent-runs'/ (job['id']+'.json'))
        self.assertNotIn(self.marker,json.dumps(saved))
        event=next(e for e in saved['events'] if e['type']=='chart')
        self.assertNotIn('chart',event)
        self.assertEqual(event['points'],1)
        self.assertEqual(Knowledge(self.store).conversations('alpha'),[])

    async def test_opt_in_history_keeps_chart_with_answer(self):
        await self.chart_job(True)
        turn=Knowledge(self.store).conversations('alpha')[0]['turns'][0]
        self.assertEqual(turn['charts'][0]['kind'],'timeline')
        self.assertEqual(turn['charts'][0]['items'][0]['sources'][0]['excerpt'],self.marker)
        self.assertIn('已绘制',turn['answer'])
