import json
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from .agent_runtime import AgentRuntime, configured_model
from .model_diagnostics import failure_diagnostics, failure_message
from .research_pages import ResearchPages
from .service import kb
from .test_research_pages import spec_for
from .test_web import Fixture


class DiagnosticTests(unittest.TestCase):
    def test_token_limit_is_actionable_and_never_exposes_response_body(self):
        private='synthetic-private-marker-sk-'+'a'*32
        exc=UnexpectedModelBehavior('Model token limit (4096) exceeded',body=private)
        diagnostic=failure_diagnostics(exc,[ModelResponse(parts=[TextPart(private)],
            finish_reason='length',usage=RequestUsage(output_tokens=4096))],page=True)
        self.assertEqual(diagnostic['code'],'output_limit')
        self.assertIn('上限',failure_message(diagnostic))
        self.assertNotIn(private,json.dumps(diagnostic)+failure_message(diagnostic))
        self.assertNotIn('模型连接',failure_message(diagnostic))
        ordinary=failure_diagnostics(exc)
        self.assertIn('回答',failure_message(ordinary))
        self.assertNotIn('页面',failure_message(ordinary))

    def test_validation_metadata_keeps_paths_without_inputs_or_user_field_names(self):
        private='synthetic-private-marker'
        messages=[ModelRequest(parts=[RetryPromptPart(tool_name='compose_research_page',content=[
            {'type':'missing','loc':('page','blocks',1,'items',0,'references'),'input':private},
            {'type':'extra_forbidden','loc':('page',private),'input':private}])])]
        diagnostic=failure_diagnostics(UnexpectedModelBehavior('Exceeded maximum retries'),messages,page=True)
        self.assertEqual(diagnostic['code'],'output_validation')
        self.assertIn('references',json.dumps(diagnostic))
        self.assertNotIn(private,json.dumps(diagnostic))


class PageWireTests(Fixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.runtime=AgentRuntime(self.store)
        self.runtime.save_config({'provider_id':'synthetic-deepseek','base_url':'https://api.deepseek.com',
                                  'protocol':'chat_completions','model':'deepseek-flash'},secret='synthetic-key')
        self.requests=[]

    def tearDown(self):
        self.temp.cleanup()

    def job(self):
        return {'id':kb.uuid4().hex,'conversation':kb.uuid4().hex,'industry':'alpha','research':False,
                'status':'running','events':[],'started_at':kb.now(),'generate_page':True}

    def transport(self,request):
        body=json.loads(request.content)
        self.requests.append(body)
        self.assertEqual(body['tool_choice'],'auto')
        self.assertNotIn('max_completion_tokens',body)
        for message in body['messages']:
            if message['role']=='assistant':
                self.assertEqual(message.get('reasoning_content'),'synthetic-reasoning')
        tools=[m for m in body['messages'] if m['role']=='tool']
        finish='tool_calls'
        message={'role':'assistant','content':None,'reasoning_content':'synthetic-reasoning'}
        if not tools:
            name,args='search_evidence',json.dumps({'query':'合同'})
        elif len(tools)==1:
            ref=json.loads(tools[0]['content'])[0]['chart_ref']
            spec=spec_for(ref)
            spec['blocks'][0]['items']=[{'label':'原文分析','text':'企业披露合同，履约情况仍待核对。'*70,
                                      'references':[ref]} for _ in range(3)]
            name,args='compose_research_page',json.dumps({'page':spec},ensure_ascii=False)
            if body['max_tokens']<6000:
                args=args[:80]  # Faithful provider truncation in the middle of JSON.
                finish='length'
        else:
            finish='stop'
            message['content']='页面已整理。'
        if finish!='stop':
            message['tool_calls']=[{'id':'call-'+str(len(tools)),'type':'function',
                'function':{'name':name,'arguments':args}}]
        return httpx.Response(200,json={'id':'synthetic-page','object':'chat.completion','created':1,
            'model':'deepseek-flash','choices':[{'index':0,'finish_reason':finish,'message':message}],
            'usage':{'prompt_tokens':100,'completion_tokens':4096 if finish=='length' else 7000,'total_tokens':7100}})

    async def run_wire(self):
        job=self.job()
        async with AsyncOpenAI(api_key='synthetic-key',base_url='https://api.deepseek.com',max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.transport))) as client:
            await self.runtime.run(job,'生成合同研究展示页',model_override=configured_model(self.runtime.config(),client))
        return job

    async def test_4096_truncated_tool_response_is_reported_without_saving_page(self):
        with patch('web.agent_runtime.PAGE_OUTPUT_TOKENS',4096):
            job=await self.run_wire()
        self.assertEqual(job['status'],'failed',job['events'])
        self.assertEqual(job['diagnostics']['code'],'output_limit',job['diagnostics'])
        self.assertEqual(job['diagnostics']['truncated_responses'],1)
        self.assertEqual(ResearchPages(self.store).listing(),[])
        note=kb.read_json(self.root/'reports/agent-runs'/(job['id']+'.json'))
        self.assertEqual(note['diagnostics']['code'],'output_limit')

    async def test_larger_page_budget_preserves_deepseek_wire_and_commits_complete_page(self):
        job=await self.run_wire()
        self.assertEqual(job['status'],'completed',job['events'])
        self.assertTrue(all(r['max_tokens']==12288 for r in self.requests))
        self.assertTrue(any(e['type']=='page_saved' for e in job['events']))
        self.assertEqual(len(ResearchPages(self.store).listing()),1)

    async def test_repeated_invalid_page_arguments_fail_with_safe_validation_diagnostics(self):
        from pydantic_ai.messages import ToolCallPart,ToolReturnPart
        def respond(messages,info):
            returns=[p for m in messages for p in m.parts if isinstance(p,ToolReturnPart)]
            if not returns:
                return ModelResponse(parts=[ToolCallPart('search_evidence',{'query':'合同'})])
            return ModelResponse(parts=[ToolCallPart('compose_research_page',{'page':{'title':'private-marker'}})])
        job=self.job()
        await self.runtime.run(job,'生成展示页',model_override=FunctionModel(respond))
        self.assertEqual(job['status'],'failed',job['events'])
        self.assertEqual(job['diagnostics']['code'],'output_validation')
        encoded=kb.json_text(kb.read_json(self.root/'reports/agent-runs'/(job['id']+'.json')))
        self.assertNotIn('private-marker',encoded)
        self.assertEqual(ResearchPages(self.store).listing(),[])
