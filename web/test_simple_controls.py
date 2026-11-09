"""Opt-in retrieval and default Agent actions, using isolated stores only."""
import asyncio
import hashlib
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from .agent_runtime import AgentRuntime, ResearchContext, build_agent, turn_permissions
from .knowledge import Knowledge
from .material_index import MaterialIndex
from .server import AgentRequest, create_app
from .test_web import Fixture
from .verification import VerificationQueue


class RetrievalControlsTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.app = create_app(self.root)
        self.client = TestClient(self.app, headers={'X-KB-Request':'1'})
        self.knowledge = Knowledge(self.store)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_save_enabled_material_indexes_without_review_or_model(self):
        original = '原文普通内容。' * 500 + '末尾赤松密度每亩七十株。'
        with patch('web.agent_runtime.AsyncOpenAI') as model:
            response = self.client.post('/api/materials/alpha', json={
                'title':'未核验的新资料', 'content':original, 'model_allowed':True})
            self.assertEqual(response.status_code, 200, response.text)
            item = response.json()
            self.assertEqual(item['verification_status'], 'unverified')
            self.assertEqual(item['extraction_job']['status'], 'completed')
            found = self.client.get('/api/materials', params={'industry':'alpha','q':'赤松密度'}).json()
            self.assertIn('七十株', found[0]['snippets'][0]['text'])
            self.assertIn(found[0]['snippets'][0]['text'], original)
            model.assert_not_called()
        self.assertEqual(VerificationQueue(self.store).list('alpha'), [])
        self.assertEqual(len(self.store.evidence('alpha')), 1)

    def test_toggle_excludes_search_and_agent_without_changing_original(self):
        item = self.knowledge.save('alpha', {'id':'chosen-file','title':'保留原件',
            'content':'赤松密度每亩七十株。','model_allowed':False})
        route = '/api/materials/alpha/chosen-file/retrieval'
        for enabled in (True, False, True):
            response = self.client.post(route, json={'enabled':enabled})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(bool(MaterialIndex(self.store).search('alpha','赤松')), enabled)
            self.assertEqual(bool(self.client.get('/api/materials',params={'industry':'alpha','q':'赤松'}).json()),enabled)
            actual = self.knowledge.get('alpha', item['id'])
            self.assertEqual(actual['content'], item['content'])
            self.assertNotIn('verification', actual)
            self.assertIsNone(actual['checked_date'])
        self.assertEqual(len(self.client.get('/api/materials',params={'industry':'alpha'}).json()),1)

    def test_restricted_and_invalid_switch_values_do_not_grant_access(self):
        self.knowledge.save('alpha', {'id':'local-only','title':'受限原件','sensitivity':'restricted'})
        route = '/api/materials/alpha/local-only/retrieval'
        for value in (True, 'true', 1, None):
            self.assertEqual(self.client.post(route,json={'enabled':value}).status_code,400)
            self.assertFalse(self.knowledge.get('alpha','local-only')['model_allowed'])

    def test_upload_excluded_then_join_needs_no_review(self):
        response = self.client.post('/api/materials/alpha/import',
            data={'metadata':'{"title":"新文件","model_allowed":false}'},
            files={'file':('original.txt','新文件独有松树株距。'.encode(),'text/plain')})
        self.assertEqual(response.status_code,200,response.text)
        key=response.json()['id']
        self.assertIsNone(response.json()['extraction_job'])
        joined=self.client.post('/api/materials/alpha/'+key+'/retrieval',json={'enabled':True}).json()
        self.assertEqual(joined['extraction_job']['status'],'completed')
        self.assertEqual(joined['verification_status'],'unverified')
        self.assertTrue(MaterialIndex(self.store).search('alpha','松树株距'))

    def test_index_failure_keeps_file_and_choice_for_retry(self):
        with patch.object(self.app.state.material_index,'build',side_effect=OSError('模拟索引不可写')):
            response=self.client.post('/api/materials/alpha',json={
                'id':'retry-file','title':'已保存原件','content':'赤松株距。','model_allowed':True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('模拟索引不可写',response.json()['index_error'])
        self.assertEqual(self.knowledge.get('alpha','retry-file')['content'],'赤松株距。')
        self.assertTrue(self.knowledge.get('alpha','retry-file')['model_allowed'])
        retried=self.client.post('/api/materials/alpha/retry-file/index',json={})
        self.assertEqual(retried.json()['status'],'completed')
        self.assertNotIn('verification',self.knowledge.get('alpha','retry-file'))

    def test_switch_preserves_optional_review_and_industry_does_not_enable_collection(self):
        self.knowledge.save('alpha', {'id':'reviewed-file','title':'原资料','content':'原文内容。'})
        queue=VerificationQueue(self.store)
        review=queue.prepare_material('alpha','reviewed-file')
        queue.approve_material('alpha',review['id'],{'decision':'approve'})
        before=self.knowledge.get('alpha','reviewed-file')['verification']
        self.client.post('/api/materials/alpha/reviewed-file/retrieval',json={'enabled':True})
        self.assertEqual(self.knowledge.get('alpha','reviewed-file')['verification'],before)
        self.client.put('/api/active-industry',json={'id':'alpha'})
        state=self.client.get('/api/state').json()
        self.assertEqual(state['active_industry'],'alpha')
        self.assertFalse(state['industries'][0]['enabled'])
        self.client.put('/api/active-industry',json={'id':None})
        self.assertIsNone(self.client.get('/api/state').json()['active_industry'])


class AgentDefaultControlsTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_defaults_and_explicit_exclusions_are_applied_before_run(self):
        request=AgentRequest(prompt='研究这个问题',industry='alpha')
        self.assertTrue(request.research)
        self.assertTrue(request.allow_source_changes)
        self.assertEqual(turn_permissions('研究并整理资料'),(True,True))
        for prompt in ('只回答这个问题','不要归档','本次不加入检索','不要把结果保存','这次不加入'):
            self.assertEqual(turn_permissions(prompt),(False,False),prompt)
        for prompt in ('不要添加信源','不新增来源','不要自动扩展信息来源'):
            self.assertEqual(turn_permissions(prompt),(True,False),prompt)
        runtime=AgentRuntime(self.store)
        with patch.object(runtime,'model_key',return_value='synthetic-test-key'),patch.object(runtime,'run',new=AsyncMock()):
            for prompt,industry,expected in [('正常研究','alpha',(True,True)),('不要归档','alpha',(False,False)),
                                             ('不添加信源','alpha',(True,False)),('跨行业提问','all',(False,False))]:
                info=runtime.start(prompt,industry)
                job=runtime.jobs[info['id']]
                self.assertEqual((job['allow_archive'],job['allow_source_changes']),expected)
                self.assertEqual(job['research'],industry!='all')
                await runtime.tasks[job['id']]
                job['status']='completed'

    async def test_excluded_write_tools_are_not_offered_to_model(self):
        def model(messages,info):
            names={t.name for t in info.function_tools}
            self.assertNotIn('archive_evidence',names)
            self.assertNotIn('add_information_source',names)
            self.assertIn('read_public_source',names)
            return ModelResponse(parts=[TextPart('仅回答，未保存。')])
        ctx=ResearchContext(self.store,'alpha',research=True,allow_archive=False,allow_source_changes=False)
        await build_agent(FunctionModel(model)).run('只回答，不要归档',deps=ctx)
        self.assertEqual(Knowledge(self.store).list('alpha'),[])

    async def test_web_recollection_preserves_existing_exclusion_without_approval(self):
        url=self.record['url']
        key='web-'+hashlib.sha256(url.encode()).hexdigest()[:32]
        knowledge=Knowledge(self.store)
        knowledge.save('alpha',{'id':key,'title':'原收录','url':url,'content':'原摘录','model_allowed':False})
        ctx=ResearchContext(self.store,'alpha',research=True,allow_source_changes=True)
        ctx.pages[url]={'url':url,'title':'本轮原文','text':'企业披露合同，执行情况待确认。','checked_date':self.record['checked_date']}
        calls=0
        def model(messages,info):
            nonlocal calls
            calls+=1
            if calls==1:
                return ModelResponse(parts=[ToolCallPart('archive_evidence',{'record':{**self.record,'quote':'企业披露合同'}},tool_call_id='save')])
            returned=[p for m in messages for p in m.parts if isinstance(p,ToolReturnPart)][-1].content
            self.assertEqual(returned['status'],'saved')
            self.assertEqual(returned['index_status'],'excluded')
            return ModelResponse(parts=[TextPart('已保存，保留未加入检索选择。')])
        await build_agent(FunctionModel(model)).run('整理此来源',deps=ctx)
        self.assertFalse(knowledge.get('alpha',key)['model_allowed'])
        self.assertNotIn('verification',knowledge.get('alpha',key))
        self.assertEqual(VerificationQueue(self.store).list('alpha'),[])
        self.assertEqual(len(self.store.evidence('alpha')),1)
