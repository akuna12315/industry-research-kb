import asyncio
import unittest
from fastapi.testclient import TestClient
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse,TextPart,ToolCallPart
from .test_web import Fixture
from .server import create_app
from .knowledge import Knowledge
from .service import kb,ws,professions
from .agent_runtime import build_agent,ResearchContext


class RegionTests(Fixture,unittest.TestCase):
    def setUp(self):
        self.setup()
        self.client=TestClient(create_app(self.root),headers={'X-KB-Request':'1'})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def add(self,key,region):
        r=self.client.post('/api/industries',json={'id':key,'name':region+'林草','region':region,'specialty':'forestry'})
        self.assertEqual(r.status_code,200,r.text)

    def national(self):
        self.add('national','全国')
        self.store.save_scope('national',{'library_role':'national-foundation'})
        record={**self.record,'id':'national-standard','region':'全国','title':'全国造林测试依据'}
        self.store.import_evidence('national',record)
        Knowledge(self.store).save('national',{'id':'national-material','title':'全国造林测试依据','summary':'全国原始依据',
            'region':'全国','sensitivity':'public','model_allowed':True,'related_evidence_ids':['national-standard']})

    def test_new_region_uses_professional_template_without_facts_or_local_sources(self):
        self.add('region-other','另一地区')
        home=self.store.home('region-other')
        scope=ws.scope(home)
        self.assertEqual(scope['topics'],professions.FORESTRY_TOPICS)
        self.assertEqual(scope['research_regions'],['另一地区'])
        self.assertFalse(self.store.entry('region-other')['enabled'])
        self.assertEqual(self.store.evidence('region-other'),[])
        self.assertTrue(all(not s['verified'] for s in self.store.sources('region-other')['sources']))
        text=kb.json_text(kb.read_json(home/'config/queries.json'))
        self.assertIn('另一地区',text)
        self.assertNotIn('江西',text)

    def test_shared_national_materials_available_but_other_local_policies_excluded(self):
        self.national()
        self.add('region-other','另一地区')
        Knowledge(self.store).save('alpha',{'title':'另一省造林政策','region':'旧地区','model_allowed':True})
        result=Knowledge(self.store).search('region-other','造林')
        self.assertEqual([r['industry_id'] for r in result],['national'])
        evidence=self.client.get('/api/evidence',params={'industry':'region-other','q':'造林'}).json()
        self.assertEqual([r['industry_id'] for r in evidence],['national'])
        self.assertEqual(self.store.evidence('region-other'),[])

    def test_shared_private_material_not_sent_to_regional_agent(self):
        self.national()
        self.add('region-other','另一地区')
        Knowledge(self.store).save('national',{'title':'内部造林文件','sensitivity':'internal','model_allowed':True})
        self.assertEqual(len(Knowledge(self.store).search('region-other','造林')),1)

    def test_scope_change_regenerates_only_template_queries_preserving_existing_facts(self):
        self.add('region-other','甲地区')
        self.store.import_evidence('region-other',self.record)
        before=(self.store.home('region-other')/'evidence'/f"{self.record['id']}.json").read_bytes()
        r=self.client.put('/api/scope/region-other',json={'primary_region':'乙地区','research_regions':['乙地区','丙地区']})
        self.assertEqual(r.status_code,200,r.text)
        text=kb.json_text(kb.read_json(self.store.home('region-other')/'config/queries.json'))
        self.assertNotIn('甲地区',text)
        self.assertIn('乙地区',text)
        self.assertIn('丙地区',text)
        self.assertEqual((self.store.home('region-other')/'evidence'/f"{self.record['id']}.json").read_bytes(),before)

    def test_custom_queries_not_overwritten_on_scope_change(self):
        self.add('region-other','甲地区')
        path=self.store.home('region-other')/'config/queries.json'
        custom={'技术标准':['用户自定义 标准']}
        kb.atomic_write(path,kb.json_text(custom))
        self.store.save_scope('region-other',{'primary_region':'乙地区'})
        self.assertEqual(kb.read_json(path),custom)

    def test_invalid_regions_rejected(self):
        self.add('region-other','甲地区')
        for value in ('丙地区',[],[''],[1]):
            r=self.client.put('/api/scope/region-other',json={'research_regions':value})
            self.assertEqual(r.status_code,400)

    def test_agent_instructions_follow_selected_regions(self):
        self.add('region-other','乙地区')
        captured=[]
        def model(messages,info):
            captured.extend(str(m) for m in messages)
            return ModelResponse(parts=[TextPart('使用当前地区。')])
        asyncio.run(build_agent(FunctionModel(model)).run('检查范围',deps=ResearchContext(self.store,'region-other')))
        self.assertIn('乙地区',''.join(captured))
        self.assertNotIn('江西',''.join(captured))

    def test_opt_in_cross_region_conversation_can_be_saved(self):
        knowledge=Knowledge(self.store)
        knowledge.save_settings({'save_conversations':True})
        knowledge.save_exchange({'conversation':'cross-region-chat','industry':'all'},'比较两地','分别标明适用地区')
        self.assertEqual(knowledge.conversations('all')[0]['industry'],'all')

    def test_shared_fact_not_duplicated_by_regional_agent(self):
        self.national()
        self.add('region-other','另一地区')
        calls=0
        def model(messages,info):
            nonlocal calls
            calls+=1
            return ModelResponse(parts=[ToolCallPart('archive_evidence',{'record':{**self.record,'id':'national-standard'}},
                tool_call_id='archive') if calls==1 else TextPart('保留全国基础原记录。')])
        ctx=ResearchContext(self.store,'region-other',True,pages={self.record['url']:{'url':self.record['url'],'checked_date':self.record['checked_date']}})
        asyncio.run(build_agent(FunctionModel(model)).run('归档',deps=ctx))
        self.assertFalse((self.store.home('region-other')/'evidence/national-standard.json').exists())

    def test_regional_report_includes_shared_basis_with_region(self):
        self.national()
        self.add('region-other','另一地区')
        result=self.client.post('/api/reports',json={'industry':'region-other'}).json()
        text=self.store.read_document(result['path'])
        self.assertIn('资料适用地区：全国',text)
        self.assertIn('全国造林测试依据',text)
        self.assertIn('未执行联网复核',text)
