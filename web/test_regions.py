"""Generic industry boundaries and region settings, including legacy metadata."""
import asyncio
import contextlib
import io
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart
from .test_web import Fixture
from .server import create_app
from .knowledge import Knowledge
from .service import kb, ws
from .agent_runtime import build_agent, ResearchContext


class RegionTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.client=TestClient(create_app(self.root),headers={'X-KB-Request':'1'})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def add(self,key,region):
        r=self.client.post('/api/industries',json={'id':key,'name':key,'region':region})
        self.assertEqual(r.status_code,200,r.text)

    def legacy(self):
        self.add('reference','全国')
        self.add('region-other','另一地区')
        for key,role in [('reference','national-foundation'),('region-other','regional')]:
            home=self.store.home(key)
            scope=ws.scope(home)
            kb.atomic_write(home/'config/scope.json',kb.json_text(scope|{'specialty':'forestry','library_role':role}))
        self.store.import_evidence('reference',self.record|{'id':'reference-standard','title':'标准原文'})
        Knowledge(self.store).save('reference',{'id':'reference-material','title':'参考原文','content':'参考原文参数。',
            'sensitivity':'public','model_allowed':True})

    def test_new_industry_has_no_special_template_or_seeded_sources(self):
        self.add('region-other','另一地区')
        home=self.store.home('region-other');scope=ws.scope(home)
        self.assertEqual(scope['topics'],ws.DEFAULT_CATEGORIES)
        self.assertNotIn('specialty',scope)
        self.assertNotIn('query_template',scope)
        self.assertFalse(self.store.entry('region-other')['enabled'])
        self.assertEqual(self.store.evidence('region-other'),[])
        self.assertEqual(self.store.sources('region-other')['sources'],[])
        self.assertNotIn('forestry',kb.json_text(kb.read_json(home/'config/queries.json')))

    def test_legacy_flags_do_not_extend_selected_industry(self):
        self.legacy()
        self.assertEqual([e['id'] for e in self.store.research_entries('region-other')],['region-other'])
        self.assertEqual(Knowledge(self.store).search('region-other','参考原文'),[])
        self.assertEqual(self.client.get('/api/evidence?industry=region-other').json(),[])
        self.assertEqual(self.store.industry_preview('reference').get('shared_with',[]),[])

    def test_cross_industry_requires_explicit_all_and_retains_live_permissions(self):
        self.legacy();knowledge=Knowledge(self.store)
        self.assertEqual([r['industry_id'] for r in knowledge.search('all','参考原文')],['reference'])
        knowledge.save('reference',{'id':'reference-material','model_allowed':False})
        self.assertEqual(knowledge.search('all','参考原文'),[])

    def test_scope_change_preserves_queries_and_existing_facts_even_with_legacy_flags(self):
        self.legacy();home=self.store.home('region-other')
        self.store.import_evidence('region-other',self.record)
        facts=(home/'evidence'/f"{self.record['id']}.json").read_bytes()
        queries=(home/'config/queries.json').read_bytes()
        result=self.client.put('/api/scope/region-other',json={'primary_region':'乙地区','research_regions':['乙地区','丙地区']})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual((home/'config/queries.json').read_bytes(),queries)
        self.assertEqual((home/'evidence'/f"{self.record['id']}.json").read_bytes(),facts)

    def test_custom_queries_not_overwritten_on_scope_change(self):
        self.add('region-other','甲地区')
        path=self.store.home('region-other')/'config/queries.json'
        custom={'技术标准':['用户自定义 标准']};kb.atomic_write(path,kb.json_text(custom))
        self.store.save_scope('region-other',{'primary_region':'乙地区'})
        self.assertEqual(kb.read_json(path),custom)

    def test_invalid_regions_rejected(self):
        self.add('region-other','甲地区')
        for value in ('丙地区',[],[''],[1]):
            self.assertEqual(self.client.put('/api/scope/region-other',json={'research_regions':value}).status_code,400)

    def test_agent_instructions_are_generic_and_follow_selected_scope(self):
        self.add('region-other','乙地区');captured=[]
        def model(messages,info):
            captured.append(info.instructions);captured.extend(str(m) for m in messages)
            return ModelResponse(parts=[TextPart('使用所选行业的资料。')])
        asyncio.run(build_agent(FunctionModel(model)).run('检查范围',deps=ResearchContext(self.store,'region-other')))
        text=''.join(captured);self.assertIn('乙地区',text)
        for phrase in ('林草专业','三调','造林','国家基础库可共同引用'):
            self.assertNotIn(phrase,text)

    def test_opt_in_cross_industry_conversation_can_be_saved(self):
        knowledge=Knowledge(self.store);knowledge.save_settings({'save_conversations':True})
        knowledge.save_exchange({'conversation':'cross-region-chat','industry':'all'},'比较两个行业','分别标明适用范围')
        self.assertEqual(knowledge.conversations('all')[0]['industry'],'all')

    def test_specialty_api_and_cli_entry_points_are_removed(self):
        self.assertEqual(self.client.post('/api/industries',json={'id':'old-template','name':'旧模板','specialty':'forestry'}).status_code,422)
        with patch('sys.argv',['kb.py','--root',str(self.root),'industry','add','other-new','新行业','--specialty','forestry']),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:ws.run_cli()
        self.assertEqual(caught.exception.code,2)
        self.assertEqual(len(ws.registry(self.root)['industries']),1)

    def test_report_uses_only_selected_industry_and_all_can_include_other_archives(self):
        self.legacy()
        local=self.client.post('/api/reports',json={'industry':'region-other'}).json()
        self.assertNotIn('标准原文',self.store.read_document(local['path']))
        combined=self.client.post('/api/reports',json={'industry':'all'}).json()
        self.assertIn('标准原文',self.store.read_document(combined['path']))
