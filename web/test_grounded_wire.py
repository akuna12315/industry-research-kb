import json
import unittest
import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent,PromptedOutput
from pydantic_ai.messages import ModelResponse,TextPart,ThinkingPart,RetryPromptPart,ModelRequest
from pydantic_ai import UnexpectedModelBehavior
from .agent_runtime import configured_model,configured_model_settings
from .source_support import GroundedAnswer
from .model_diagnostics import failure_diagnostics

class GroundedWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_bonsai_prompted_schema_has_one_leading_system_message(self):
        requests=[]
        def respond(request):
            body=json.loads(request.content);requests.append(body)
            self.assertEqual([m['role'] for m in body['messages']],['system','user'])
            self.assertEqual(body['reasoning_effort'],'medium')
            return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':1,'model':'bonsai',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{"insufficient":true,"claims":[],"limitation":"本次片段缺少支付记录。"}'}}]})
        cfg={'model':'bonsai-2-27b-abliterated-v2','base_url':'http://127.0.0.1:8092/v1','protocol':'chat_completions'}
        async with AsyncOpenAI(base_url=cfg['base_url'],api_key='synthetic',http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
            agent=Agent(configured_model(cfg,client),output_type=PromptedOutput(GroundedAnswer),instructions='只能根据片段回答。')
            output=await agent.run('合成测试',model_settings=configured_model_settings(cfg))
        self.assertTrue(output.output.insufficient);self.assertEqual(len(requests),1)

    async def test_diagnostics_count_private_responses_without_persisting_content(self):
        messages=[ModelResponse(parts=[ThinkingPart('private-thinking-marker'),TextPart('private-text-marker')]),
            ModelRequest(parts=[RetryPromptPart('unsupported_number: private-material-marker')])]
        result=failure_diagnostics(UnexpectedModelBehavior('Exceeded maximum retries'),messages)
        self.assertEqual(result['code'],'output_validation');self.assertEqual(result['thinking_parts'],1)
        self.assertEqual(result['grounding_errors'],['unsupported_number'])
        self.assertNotIn('private-',json.dumps(result))

if __name__=='__main__':unittest.main()
