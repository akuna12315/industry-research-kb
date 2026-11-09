"""Source locking and actionable, bounded chart recovery using synthetic records."""
from copy import deepcopy
import unittest

from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from .test_web import Fixture
from .agent_runtime import AgentRuntime, ResearchContext
from .charts import ChartValidationError, _source, register_evidence, register_materials, validate_chart
from .knowledge import Knowledge
from .page_scope import resolve_material_scope, validate_material_scope
from .page_recovery import PageDrafts, repair_sources, scene_feedback
from .research_pages import ResearchPages
from .visual_pages import VisualScene
from .test_visual_pages import plan_for
from .test_visual_skill import QUOTE, category, table_spec


class PreventionTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.targets = []
        for key, title, content in [('thesis', '电池学位论文', '研究结果 SOC 估计 '+QUOTE),
                                    ('project', '林草项目方案', '研究结果 林草项目规划造林100亩。')]:
            self.knowledge.save('alpha', {'id':key,'title':title,'content':content,'model_allowed':True})
            self.targets.append({'industry_id':'alpha','id':key})

    def tearDown(self): self.temp.cleanup()

    def context(self):
        ctx = ResearchContext(self.store, 'alpha', generate_page=True)
        self.store.import_evidence('alpha', {**self.record,'id':'quant','summary':QUOTE})
        ref = next(r['chart_ref'] for r in register_evidence(ctx,self.store.evidence('alpha','',limit=50)) if r['id']=='quant')
        return ctx, ref

    def test_ambiguous_personal_page_never_guesses_a_material(self):
        with self.assertRaisesRegex(ValueError,'选择'):
            resolve_material_scope(self.store,'alpha','把我的毕设整理成网页')
        self.assertEqual(resolve_material_scope(self.store,'alpha','把电池学位论文整理成网页'),[self.targets[0]])
        self.assertEqual(resolve_material_scope(self.store,'alpha','展示研究结果',[self.targets[0]]),[self.targets[0]])
        self.assertEqual(validate_material_scope(self.store,'alpha',[self.targets[0]]*10),[self.targets[0]])
        runtime = AgentRuntime(self.store)
        runtime.save_config({},secret='synthetic-test-key')
        with self.assertRaisesRegex(ValueError,'选择'):
            runtime.start('把我的毕设整理成网页','alpha',False,generate_page=True)
        self.assertEqual(runtime.jobs,{})

    def test_locked_retrieval_filters_before_ranking_and_citation_enforces_same_boundary(self):
        records = self.knowledge.search('alpha','研究结果',fuzzy=True,material_scope=[self.targets[0]])
        self.assertEqual([r['id'] for r in records],['thesis'])
        ctx = ResearchContext(self.store,'alpha',generate_page=True)
        refs = register_materials(ctx,self.knowledge.search('alpha','研究结果',fuzzy=True))
        ctx.page_materials=[self.targets[0]]
        thesis = next(r for r in refs if r['id']=='thesis')['snippets'][0]['chart_ref']
        project = next(r for r in refs if r['id']=='project')['snippets'][0]['chart_ref']
        self.assertEqual(_source(ctx,thesis)['id'],'thesis')
        with self.assertRaisesRegex(ChartValidationError,'选定资料'):
            _source(ctx,project)
        self.knowledge.save('alpha',{'id':'thesis','model_allowed':False})
        with self.assertRaisesRegex(ValueError,'授权'):
            validate_material_scope(self.store,'alpha',[self.targets[0]])

    def test_chart_numeric_error_has_point_path_and_exact_repair_source(self):
        ctx, ref = self.context()
        chart = category(ref); chart.items[1].value=999
        with self.assertRaises(ChartValidationError) as caught:
            validate_chart(ctx,chart)
        feedback=scene_feedback(caught.exception)
        self.assertEqual(feedback['code'],'data')
        self.assertEqual(feedback['fields'][0]['path'],['chart','items',1,'value'])
        scene={'id':'bars','kind':'chart','title':'误差','chart':chart.model_dump()}
        sources=repair_sources(ctx,scene,feedback)
        self.assertEqual(sources[0]['chart_ref'],ref)
        self.assertEqual(sources[0]['text'],QUOTE)
        self.assertNotIn('999',str(feedback)+str(sources))

    def test_contract_and_reference_errors_are_distinct_and_invalid_refs_receive_valid_choices(self):
        ctx, ref = self.context()
        chart=category(ref);chart.basis='原文没有的比较条件'
        with self.assertRaises(ChartValidationError) as caught:
            validate_chart(ctx,chart)
        feedback=scene_feedback(caught.exception)
        self.assertEqual(feedback['code'],'data')
        self.assertIn(['chart','basis'],[f['path'] for f in feedback['fields']])
        chart=category(ref);chart.items[0].references=['invalid-secret-marker']
        with self.assertRaises(ChartValidationError) as caught:
            validate_chart(ctx,chart)
        feedback=scene_feedback(caught.exception)
        self.assertEqual(feedback['code'],'reference')
        self.assertEqual(feedback['fields'][0]['path'],['chart','items',0,'references',0])
        sources=repair_sources(ctx,{'chart':chart.model_dump()},feedback)
        self.assertEqual(sources[0]['chart_ref'],ref)
        self.assertNotIn('invalid-secret-marker',str(feedback)+str(sources))
        legacy=scene_feedback(ValueError('数值或比较口径无匹配依据；请引用登记指标'))
        self.assertEqual(legacy['code'],'data')

    def test_schema_hint_explains_references_belong_on_each_item(self):
        ctx, ref = self.context()
        scene={'id':'bars','kind':'chart','title':'误差','chart':{**category(ref).model_dump(),'references':[ref]}}
        with self.assertRaises(ValidationError) as caught:
            VisualScene.model_validate(scene)
        feedback=scene_feedback(caught.exception)
        self.assertIn('chart.items[i].references',feedback['hint'])
        self.assertEqual(feedback['fields'][0]['path'],['chart','references'])


class PreventionAgentTests(Fixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup()
    def tearDown(self): self.temp.cleanup()

    async def test_failed_chart_can_be_replanned_once_without_skipping_numeric_checks(self):
        self.store.import_evidence('alpha',{**self.record,'id':'quant','summary':QUOTE})
        from .service import kb
        job={'id':kb.uuid4().hex,'conversation':kb.uuid4().hex,'industry':'alpha','research':False,
             'generate_page':True,'status':'running','events':[],'started_at':kb.now()}
        def model(messages,info):
            self.assertNotIn('draw_research_chart',{t.name for t in info.function_tools})
            returns=[p for m in messages for p in m.parts if isinstance(p,ToolReturnPart)]
            if not returns:return ModelResponse(parts=[ToolCallPart('search_evidence',{'query':'RMSE'})])
            names=[p.tool_name for p in returns]
            ref=next(p.content[0]['chart_ref'] for p in returns if p.tool_name=='search_evidence')
            spec=table_spec(ref)
            spec['scenes'].append({'id':'bars','kind':'chart','title':'误差对比','chart':category(ref).model_dump()})
            if 'plan_visual_page' not in names:
                return ModelResponse(parts=[ToolCallPart('plan_visual_page',{'plan':plan_for(spec)})])
            adds=[p.content for p in returns if p.tool_name=='add_visual_scene']
            if not adds:return ModelResponse(parts=[ToolCallPart('add_visual_scene',{'scene':spec['scenes'][0]})])
            if len(adds)==1:
                scene=deepcopy(spec['scenes'][1]);scene['chart']['items'][0]['value']=999
                return ModelResponse(parts=[ToolCallPart('add_visual_scene',{'scene':scene})])
            if 'revise_visual_scene' not in names:
                self.assertEqual(adds[-1]['code'],'data')
                self.assertEqual(adds[-1]['fields'][0]['path'],['chart','items',0,'value'])
                self.assertEqual(adds[-1]['repair_sources'][0]['chart_ref'],ref)
                return ModelResponse(parts=[ToolCallPart('revise_visual_scene',{'scene_id':'bars','reason':'只提供精确表格，保留逐字数值依据'})])
            if not any(p.get('prepared')=='bars' for p in adds):
                scene=deepcopy(spec['scenes'][0]);scene.update(id='bars',title='误差对比',emphasis='normal')
                if len(adds)==2:scene['cells'][0]['text']='999'
                return ModelResponse(parts=[ToolCallPart('add_visual_scene',{'scene':scene})])
            if names.count('revise_visual_scene')==1:
                return ModelResponse(parts=[ToolCallPart('revise_visual_scene',{'scene_id':'metrics','reason':'不能修改已通过场景'})])
            self.assertFalse(next(p.content for p in reversed(returns) if p.tool_name=='revise_visual_scene')['revised'])
            if 'finish_visual_page' not in names:
                return ModelResponse(parts=[ToolCallPart('finish_visual_page',{})])
            return ModelResponse(parts=[TextPart('已用精确表格完成。')])
        await AgentRuntime(self.store).run(job,'生成实验结果展示页',model_override=FunctionModel(model))
        self.assertEqual(job['status'],'completed',job['events'])
        page=ResearchPages(self.store).detail(job['page_id'])
        self.assertEqual([s['kind'] for s in page['revision']['content']['scenes']],['table','table'])
        self.assertNotIn('999',str(page))
        self.assertTrue(any(e.get('title')=='图表条件不足，改用精确表格' for e in job['events']))
        self.assertEqual(PageDrafts(self.store).get(job['id'])['status'],'completed')
