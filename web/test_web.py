import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from .server import create_app
from .service import Store, kb, ws
from .agent_runtime import AgentRuntime, allowed_source, model_url, public_url, fetch_page


class Fixture:
    def setup(self):
        scratch = kb.DEFAULT_ROOT / 'work/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        kb.atomic_write(self.root / 'config/industries.json', kb.json_text({'version': 1, 'active_industry': None, 'industries': []}))
        kb.atomic_write(self.root / 'config/automation.json', kb.json_text({'schedule': '每天09:00', 'status': 'ACTIVE'}))
        kb.atomic_write(self.root / 'config/agent.json', kb.json_text(kb.read_json(kb.DEFAULT_ROOT / 'config/agent.json')))
        ws.industry_command(self.root, Store(self.root).args(action='add', id='alpha', name='测试行业', region='中国', categories=None))
        self.store = Store(self.root)
        self.record = {'id': 'test-contract', 'title': '测试合同', 'summary': '企业披露合同，待核对履约进度。',
                       'url': 'https://example.org/public/contract', 'publisher': '测试企业', 'region': '中国',
                       'category': '企业与竞争', 'status': 'reported', 'checked_date': datetime.now(kb.TZ).date().isoformat()}
        self.store.import_evidence('alpha', self.record)


class WebTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.client = TestClient(create_app(self.root), headers={'X-KB-Request': '1'})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_real_store_read_and_chinese_search(self):
        response = self.client.get('/api/evidence', params={'industry': 'alpha', 'q': '合同'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['id'], self.record['id'])
        self.assertEqual(response.json()[0]['freshness'], 'current')
        self.assertEqual(self.client.get('/api/state').json()['active_industry'], None)

    def test_cross_site_writes_and_arbitrary_documents_rejected(self):
        self.assertEqual(self.client.put('/api/active-industry', json={'id': 'alpha'}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/document', params={'path': 'config/agent.json'}).status_code, 400)
        self.assertEqual(self.client.get('/api/document', params={'path': '../outside.md'}).status_code, 400)

    def test_industry_switch_and_enable_independent(self):
        self.client.put('/api/industries/alpha', json={'active': True})
        state = self.client.get('/api/state').json()
        self.assertEqual(state['active_industry'], 'alpha')
        self.assertFalse(state['industries'][0]['enabled'])
        self.client.put('/api/industries/alpha', json={'enabled': True})
        self.assertTrue(self.client.get('/api/state').json()['industries'][0]['enabled'])

    def test_source_edit_resets_verification(self):
        catalog = {'mode': 'only', 'sources': [{'id': 'official', 'name': '官网', 'url': 'https://example.org/public/',
                   'priority': 1, 'enabled': True, 'categories': [], 'verified': True, 'checked_date': '2026-10-13'}]}
        result = self.client.put('/api/sources/alpha', json=catalog)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()['sources'][0]['verified'])
        self.assertIsNone(result.json()['sources'][0]['checked_date'])

    def test_revision_history_and_validation_still_apply(self):
        self.assertEqual(self.client.post('/api/evidence/alpha', json={**self.record, 'summary': '合同变更披露'}).status_code, 200)
        record = self.client.get('/api/evidence/alpha/test-contract').json()
        self.assertEqual(record['history'][0]['summary'], self.record['summary'])
        self.assertEqual(self.client.post('/api/evidence/alpha', json={**self.record, 'id': '../escape'}).status_code, 400)

    def test_reports_resolve_to_local_sources_and_download(self):
        result = self.client.post('/api/reports', json={'industry': 'alpha', 'query': '合同'}).json()
        document = self.client.get('/api/document', params={'path': result['path']}).json()['text']
        self.assertIn(self.record['url'], document)
        self.assertIn('未执行联网复核', document)
        download = self.client.get('/api/download', params={'path': result['path']})
        self.assertEqual(download.status_code, 200)
        self.assertIn('attachment', download.headers['content-disposition'])

    def test_model_secret_is_not_returned_or_written(self):
        marker = 'test-only-secret-unique'
        response = self.client.put('/api/agent/config', json={'api_key': marker, 'model': 'test-model'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(marker, response.text)
        self.assertNotIn(marker, self.client.get('/api/state').text)
        self.assertNotIn(marker, (self.root / 'config/agent.json').read_text(encoding='utf-8'))

    def test_model_address_and_source_boundary(self):
        with self.assertRaises(ValueError):
            model_url('http://api.example.org/v1')
        with self.assertRaises(ValueError):
            model_url('https://token@example.org/v1')
        catalog = {'mode': 'only', 'sources': [{'url': 'https://example.org/public/index.html', 'enabled': True}]}
        self.assertTrue(allowed_source('https://example.org/public/contract', catalog))
        self.assertFalse(allowed_source('https://example.org/private/contract', catalog))
        self.assertFalse(allowed_source('https://example.org.evil.test/public/a', catalog))

    def test_review_filter_runs_before_the_100_record_limit(self):
        old = {**self.record, 'id': 'old-needs-review',
               'checked_date': (datetime.now(kb.TZ).date() - timedelta(days=60)).isoformat()}
        self.store.import_evidence('alpha', [{**self.record, 'id': f'fresh-{i}'} for i in range(99)] + [old])
        response = self.client.get('/api/evidence', params={'industry': 'alpha', 'review_only': 'true'})
        self.assertEqual([r['id'] for r in response.json()], ['old-needs-review'])
        self.assertEqual(self.client.get('/api/state').json()['industries'][0]['review_count'], 1)

    def test_topic_hits_do_not_displace_evidence_search_results(self):
        home = self.store.home('alpha')
        for index in range(13):
            kb.atomic_write(home / f'topics/high-score-{index}.md', '# 合同合同合同\n' + '合同' * 100)
        kb.reindex(home)
        records = self.store.evidence('alpha', '合同', limit=12)
        self.assertEqual([r['id'] for r in records], ['test-contract'])
        # The CLI's mixed-document search remains available.
        mixed = ws.find(self.root, ws.selected(self.root, 'alpha'), self.store.args(query='合同', limit=1))
        self.assertTrue(mixed[0]['path'].startswith('industries/alpha/topics/'))

    def test_edit_preserves_auto_deadline_when_policy_changes(self):
        record = self.client.get('/api/evidence/alpha/test-contract').json()
        self.assertIsNone(record['review_after'])
        original_due = record['review_due_date']
        updated = {**self.record, 'summary': '修改摘要，保留自动复核策略', 'review_after': record['review_after']}
        self.assertEqual(self.client.post('/api/evidence/alpha', json=updated).status_code, 200)
        self.client.put('/api/scope/alpha', json={'freshness': {'default_days': 90, 'category_days': {}}})
        changed = self.client.get('/api/evidence/alpha/test-contract').json()
        self.assertIsNone(changed['review_after'])
        self.assertNotEqual(changed['review_due_date'], original_due)
        self.assertEqual(changed['review_due_date'], (datetime.fromisoformat(self.record['checked_date']).date() + timedelta(days=90)).isoformat())

    def test_explicit_review_override_stays_fixed(self):
        due = (datetime.now(kb.TZ).date() + timedelta(days=10)).isoformat()
        self.store.import_evidence('alpha', {**self.record, 'review_after': due})
        self.client.put('/api/scope/alpha', json={'freshness': {'default_days': 90, 'category_days': {}}})
        record = self.client.get('/api/evidence/alpha/test-contract').json()
        self.assertEqual(record['review_after'], due)
        self.assertEqual(record['review_due_date'], due)

    def test_failed_source_save_does_not_change_saved_catalog(self):
        source = {'id': 'retry-source', 'name': '测试来源', 'url': 'ftp://example.org/public/',
                  'priority': 1, 'enabled': True, 'categories': []}
        self.assertEqual(self.client.put('/api/sources/alpha', json={'mode': 'preferred', 'sources': [source]}).status_code, 400)
        self.assertEqual(self.client.get('/api/sources/alpha').json()['sources'], [])
        source['url'] = 'https://example.org/public/'
        self.assertEqual(self.client.put('/api/sources/alpha', json={'mode': 'preferred', 'sources': [source]}).status_code, 200)
        self.assertEqual(len(self.client.get('/api/sources/alpha').json()['sources']), 1)


class AgentTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()

    def tearDown(self):
        self.temp.cleanup()

    async def run_model(self, function, research=False):
        runtime = AgentRuntime(self.store)
        job = {'id': kb.uuid4().hex, 'conversation': kb.uuid4().hex, 'industry': 'alpha',
               'research': research, 'status': 'running', 'events': [], 'started_at': kb.now()}
        await runtime.run(job, '查合同', model_override=FunctionModel(function))
        return job

    async def test_sdk_executes_real_search_tool(self):
        def model(messages, info):
            if not any(isinstance(m, ModelResponse) for m in messages):
                return ModelResponse(parts=[ToolCallPart('search_evidence', {'query': '合同'}, tool_call_id='call-search')])
            return ModelResponse(parts=[TextPart('已查到测试合同，来源：https://example.org/public/contract')])
        job = await self.run_model(model)
        self.assertEqual(job['status'], 'completed', job['events'])
        citations = next(e for e in job['events'] if e['type'] == 'citations')
        self.assertEqual(citations['items'][0]['id'], self.record['id'])
        self.assertTrue(any(e['type'] == 'answer' for e in job['events']))

    async def test_unread_source_cannot_be_archived(self):
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(parts=[ToolCallPart('archive_evidence', {'record': {**self.record, 'id': 'unauthorized-fact'}}, tool_call_id='bad-archive')])
            return ModelResponse(parts=[TextPart('未读原文，未归档。')])
        job = await self.run_model(model, research=True)
        self.assertEqual(job['status'], 'completed', job['events'])
        self.assertFalse((self.store.home('alpha') / 'evidence/unauthorized-fact.json').exists())
        self.assertEqual(self.store.state()['industries'][0]['evidence_count'], 1)

    async def test_public_fetch_denies_local_addresses(self):
        with self.assertRaises(ValueError):
            await public_url('http://127.0.0.1/')

    async def test_changing_provider_clears_key_and_blocks_outbound_requests(self):
        runtime = AgentRuntime(self.store)
        runtime.save_config({}, 'old-test-provider-key')
        config = runtime.save_config({'base_url': 'https://different-provider.example/v1'})
        self.assertFalse(config['key_configured'])
        with patch('web.agent_runtime.httpx.AsyncClient') as client:
            with self.assertRaises(ValueError):
                await runtime.test_connection()
            client.assert_not_called()
        with self.assertRaises(ValueError):
            runtime.start('测试', 'alpha', False)

    async def test_same_provider_model_change_keeps_bound_key(self):
        runtime = AgentRuntime(self.store)
        runtime.save_config({}, 'same-provider-test-key')
        result = runtime.save_config({'model': 'different-model'})
        self.assertTrue(result['key_configured'])
        self.assertEqual(runtime.model_key(runtime.config()), 'same-provider-test-key')
        self.assertNotIn('same-provider-test-key', kb.json_text(result))

    async def test_changing_provider_requires_explicit_new_key(self):
        runtime = AgentRuntime(self.store)
        runtime.save_config({}, 'old-test-provider-key')
        runtime.save_config({'base_url': 'https://different-provider.example/v1'}, 'new-test-provider-key')
        original_client = __import__('httpx').AsyncClient
        captured = []
        def handler(request):
            captured.append(request.headers.get('authorization'))
            return __import__('httpx').Response(200, json={'data': [{'id': runtime.config()['model']}]})
        transport = __import__('httpx').MockTransport(handler)
        with patch('web.agent_runtime.httpx.AsyncClient', lambda **kwargs: original_client(transport=transport, **kwargs)):
            await runtime.test_connection()
        self.assertEqual(captured, ['Bearer new-test-provider-key'])

    async def test_direct_config_edit_cannot_move_a_bound_key(self):
        runtime = AgentRuntime(self.store)
        runtime.save_config({}, 'old-test-provider-key')
        kb.atomic_write(runtime.config_path, kb.json_text({**runtime.config(), 'base_url': 'https://other-service.example/v1'}))
        self.assertFalse(runtime.public_config()['key_configured'])
        with self.assertRaises(ValueError):
            await runtime.test_connection()

    async def test_source_dot_paths_are_rejected_before_http_requests(self):
        catalog = {'mode': 'only', 'sources': [{'url': 'https://example.org/public/index.html', 'enabled': True}]}
        paths = ['/public/../outside/a', '/public/%2e%2e/outside/a', '/public/%252e%252e/outside/a', '/public/%5c../outside/a']
        for path in paths:
            url = 'https://example.org' + path
            self.assertFalse(allowed_source(url, catalog), url)
            with patch('web.agent_runtime.httpx.AsyncClient') as client:
                with self.assertRaises(ValueError):
                    await fetch_page(url, catalog)
                client.assert_not_called()

    async def test_source_redirect_cannot_escape_allowed_directory(self):
        import httpx
        original_client = httpx.AsyncClient
        requests = []
        def handler(request):
            requests.append(request.url.path)
            return httpx.Response(302, headers={'location': '../outside/article'})
        catalog = {'mode': 'only', 'sources': [{'url': 'https://example.org/public/index.html', 'enabled': True}]}
        with patch('web.agent_runtime.public_url', new=AsyncMock(return_value=None)), patch('web.agent_runtime.httpx.AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)):
            with self.assertRaises(ValueError):
                await fetch_page('https://example.org/public/index.html', catalog)
        self.assertEqual(requests, ['/public/index.html'])

    async def test_agent_review_tool_finds_old_records_beyond_latest_100(self):
        old = {**self.record, 'id': 'old-needs-review',
               'checked_date': (datetime.now(kb.TZ).date() - timedelta(days=60)).isoformat()}
        self.store.import_evidence('alpha', [{**self.record, 'id': f'fresh-{i}'} for i in range(99)] + [old])
        def model(messages, info):
            if not any(isinstance(message, ModelResponse) for message in messages):
                return ModelResponse(parts=[ToolCallPart('list_review_due', {}, tool_call_id='review-old')])
            from pydantic_ai.messages import ToolReturnPart
            returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
            self.assertEqual([record['id'] for record in returned[-1].content], ['old-needs-review'])
            return ModelResponse(parts=[TextPart('找到1条旧证据。')])
        job = await self.run_model(model)
        self.assertEqual(job['status'], 'completed', job['events'])

    async def test_research_reads_original_before_real_import(self):
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(parts=[ToolCallPart('read_public_source', {'url': self.record['url']}, tool_call_id='read-original')])
            if calls == 2:
                return ModelResponse(parts=[ToolCallPart('archive_evidence', {'record': {**self.record, 'id': 'read-then-archived'}}, tool_call_id='archive-after-read')])
            return ModelResponse(parts=[TextPart('原文已读，证据已归档。')])
        page = {'url': self.record['url'], 'requested_url': self.record['url'], 'title': '测试合同原文',
                'text': '企业披露合同', 'links': [], 'truncated': False, 'checked_date': self.record['checked_date']}
        with patch('web.agent_runtime.fetch_page', new=AsyncMock(return_value=page)):
            job = await self.run_model(model, research=True)
        self.assertEqual(job['status'], 'completed', job['events'])
        evidence = kb.read_json(self.store.home('alpha') / 'evidence/read-then-archived.json')
        self.assertEqual(evidence['checked_date'], self.record['checked_date'])
        self.assertEqual(evidence['status'], 'reported')


if __name__ == '__main__':
    unittest.main()
