"""Verbatim support IDs reduce copying mistakes without relaxing source/value checks."""
from copy import deepcopy
import unittest
from .test_web import Fixture
from .agent_runtime import ResearchContext
from .charts import ChartValidationError, register_evidence, register_materials, validate_chart
from .visual_pages import VisualNode, VisualScene, VisualPlan, validate_scene
from .visual_supports import bind_support
from .page_recovery import PageDrafts, scene_feedback
from .page_scope import requested_scene_limit
from .knowledge import Knowledge
from .test_visual_skill import QUOTE, category


class SupportTests(Fixture,unittest.TestCase):
    def setUp(self):
        self.setup()
        self.ctx=ResearchContext(self.store,'alpha',generate_page=True)
        self.store.import_evidence('alpha',{**self.record,'id':'quant','summary':QUOTE})
        record=next(r for r in register_evidence(self.ctx,self.store.evidence('alpha','',limit=50)) if r['id']=='quant')
        self.ref=record['chart_ref'];self.support=record['visual_supports'][0]['support_ref']

    def tearDown(self):self.temp.cleanup()

    def test_chart_binds_exact_text_and_reference_without_model_copying(self):
        chart=category(self.ref)
        for point in chart.items:point.references=[];point.quote='';point.support_ref=self.support
        result=validate_chart(self.ctx,chart)
        self.assertEqual(result['items'][0]['quote'],QUOTE)
        self.assertEqual(result['items'][0]['sources'][0]['ref'],self.ref)
        self.assertEqual(result['items'][0]['support_ref'],'')

    def test_table_alias_does_not_approve_an_invented_number(self):
        scene=VisualScene(id='table',kind='table',title='数据',nodes=[{'id':'a','label':'算法A','support_ref':self.support}],
            dimensions=[{'id':'rmse','label':'RMSE（%）'}],
            cells=[{'node':'a','dimension':'rmse','text':'999','support_ref':self.support}])
        with self.assertRaises(ValueError) as caught:
            validate_scene(self.ctx,scene,lambda refs:None)
        feedback=scene_feedback(caught.exception)
        self.assertEqual(feedback['code'],'data')
        self.assertEqual(feedback['fields'][0]['path'],['cells',0,'text'])
        scene.cells[0].text='1.2'
        validate_scene(self.ctx,scene,lambda refs:None)
        self.assertEqual(scene.cells[0].quote,QUOTE)

    def test_unknown_alias_and_conflicting_quote_are_rejected(self):
        for node in [VisualNode(id='a',label='节点',support_ref='invented'),
                     VisualNode(id='a',label='节点',support_ref=self.support,quote='被改写的原文')]:
            with self.assertRaises(ChartValidationError):bind_support(self.ctx,node,['nodes',0])

    def test_material_permission_is_rechecked_when_alias_is_used(self):
        knowledge=Knowledge(self.store)
        material=knowledge.save('alpha',{'id':'paper','title':'论文','content':QUOTE,'model_allowed':True})
        records=register_materials(self.ctx,knowledge.search('alpha','RMSE'))
        support=records[0]['snippets'][0]['visual_supports'][0]['support_ref']
        knowledge.save('alpha',{'id':material['id'],'model_allowed':False})
        with self.assertRaisesRegex(ValueError,'撤回'):
            bind_support(self.ctx,VisualNode(id='a',label='节点',support_ref=support),['nodes',0])

    def test_excerpt_catalog_survives_restart_beyond_source_preview(self):
        text='背景信息。'*350+QUOTE
        self.store.import_evidence('alpha',{**self.record,'id':'long','summary':text})
        record=next(r for r in register_evidence(self.ctx,self.store.evidence('alpha','',limit=50)) if r['id']=='long')
        entry=next(e for e in record['visual_supports'] if QUOTE in e['quote'])
        self.ctx.visual_plan=VisualPlan(title='研究',question='问题',summary='原文',summary_references=[self.ref],coverage='合成数据',
            scenes=[{'id':'main','kind':'flow','title':'研究'}])
        self.ctx.page_checkpoint={'id':'support-draft','industry':'alpha','request':'生成研究页','options':{}}
        PageDrafts(self.store).save(self.ctx)
        restored=ResearchContext(self.store,'alpha',generate_page=True)
        PageDrafts(self.store).restore(restored,PageDrafts(self.store).get('support-draft'))
        node=VisualNode(id='a',label='结果',support_ref=entry['support_ref'])
        bind_support(restored,node,['nodes',0])
        self.assertIn(QUOTE,node.quote)
        self.assertEqual(node.references,[record['chart_ref']])

    def test_explicit_view_count_is_a_bound(self):
        self.assertEqual(requested_scene_limit('做2个视图：路线与数据'),2)
        self.assertEqual(requested_scene_limit('只要两个场景'),2)
        self.assertIsNone(requested_scene_limit('整理研究结果'))

    def test_locator_numbers_are_not_suggested_as_measurements(self):
        from .visual_supports import source_supports
        self.store.import_evidence('alpha',{**self.record,'id':'markers','summary':'【正文·段落805】样本误差为1.2。'})
        record=next(r for r in register_evidence(self.ctx,self.store.evidence('alpha','',limit=50)) if r['id']=='markers')
        numbers=source_supports(self.ctx,record['chart_ref'])[0]['numbers']
        self.assertEqual(numbers,['1.2'])


class SupportAgentTests(Fixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.setup()
    def tearDown(self):self.temp.cleanup()

    async def test_sdk_saves_page_with_aliases_only_and_persists_resolved_originals(self):
        from pydantic_ai.messages import ModelResponse,ToolCallPart,ToolReturnPart,TextPart
        from pydantic_ai.models.function import FunctionModel
        from .agent_runtime import AgentRuntime
        from .test_visual_skill import table_spec
        from .test_visual_pages import plan_for
        from .research_pages import ResearchPages
        from .service import kb
        self.store.import_evidence('alpha',{**self.record,'id':'quant','summary':QUOTE})
        job={'id':kb.uuid4().hex,'conversation':kb.uuid4().hex,'industry':'alpha','research':False,
             'generate_page':True,'status':'running','events':[],'started_at':kb.now()}
        def model(messages,info):
            returns=[p for m in messages for p in m.parts if isinstance(p,ToolReturnPart)]
            if not returns:return ModelResponse(parts=[ToolCallPart('search_evidence',{'query':'RMSE'})])
            names=[p.tool_name for p in returns]
            record=next(p.content[0] for p in returns if p.tool_name=='search_evidence')
            spec=table_spec(record['chart_ref'])
            for item in [*spec['scenes'][0]['nodes'],*spec['scenes'][0]['cells']]:
                item.update(quote='',references=[],support_ref=record['visual_supports'][0]['support_ref'])
            if 'plan_visual_page' not in names:return ModelResponse(parts=[ToolCallPart('plan_visual_page',{'plan':plan_for(spec)})])
            if 'add_visual_scene' not in names:return ModelResponse(parts=[ToolCallPart('add_visual_scene',{'scene':spec['scenes'][0]})])
            if 'finish_visual_page' not in names:return ModelResponse(parts=[ToolCallPart('finish_visual_page',{})])
            return ModelResponse(parts=[TextPart('完成')])
        await AgentRuntime(self.store).run(job,'生成实验结果展示页',model_override=FunctionModel(model))
        self.assertEqual(job['status'],'completed',job['events'])
        page=ResearchPages(self.store).detail(job['page_id'])
        cell=page['revision']['content']['scenes'][0]['cells'][0]
        self.assertEqual(cell['quote'],QUOTE)
        self.assertTrue(cell['references'])
        self.assertEqual(cell['support_ref'],'')
