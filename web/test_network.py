import asyncio
import unittest
from unittest.mock import patch, AsyncMock

import httpx
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from .test_web import Fixture
from .network import NetworkRuntime, Robots, canonical_url
from .agent_runtime import fetch_page, public_url, build_agent, ResearchContext, AgentRuntime
from .service import kb
from .verification import VerificationQueue
from .knowledge import Knowledge


class NetworkTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()
        self.network = NetworkRuntime(self.store)
        self.fetches = []

    def tearDown(self):
        self.temp.cleanup()

    def page(self, url, text='测试原文，仅是候选资料。', links=None):
        return {'url': url, 'requested_url': url, 'title': '测试页面', 'text': text,
                'truncated': False, 'links': [{'url': u, 'title': u} for u in links or []],
                'checked_date': self.record['checked_date']}

    async def test_cached_crawl_respects_changed_source_boundary(self):
        url = self.record['url']
        crawl = await self.batch([url], max_pages=1, max_depth=0,
                                 fetch=AsyncMock(return_value=self.page(url)))
        self.store.save_sources('alpha', {'mode': 'only', 'sources': [{'id': 'official', 'name': '限定来源',
            'url': 'https://official.example/public/', 'enabled': True, 'priority': 1, 'categories': []}]})
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            parts = [ToolCallPart('read_public_source', {'url': url}, tool_call_id='read'),
                ToolCallPart('archive_evidence', {'record': {**self.record, 'id': 'outside-cache'}}, tool_call_id='archive'),
                TextPart('来源边界已改变。')]
            return ModelResponse(parts=[parts[min(calls - 1, 2)]])
        runtime = AgentRuntime(self.store, self.network)
        job = {'id': kb.uuid4().hex, 'conversation': kb.uuid4().hex, 'industry': 'alpha', 'research': True,
               'crawl_id': crawl['id'], 'status': 'running', 'events': [], 'started_at': kb.now()}
        with patch('web.agent_runtime.fetch_page', new=AsyncMock()) as fetch:
            await runtime.run(job, '整理候选', model_override=FunctionModel(model))
        fetch.assert_not_awaited()
        self.assertFalse((self.store.home('alpha') / 'evidence/outside-cache.json').exists())

    async def test_long_page_tail_change_changes_fingerprint(self):
        client = httpx.AsyncClient
        tail = 'OLD'
        def handler(request):
            return httpx.Response(200, text='<main>' + 'x' * 20000 + tail + '</main>',
                                  headers={'Content-Type': 'text/html'})
        with patch('web.agent_runtime.public_url', new=AsyncMock(return_value=None)), \
             patch.object(self.network.robots, 'check', new=AsyncMock()), \
             patch('web.agent_runtime.httpx.AsyncClient', lambda **kw: client(transport=httpx.MockTransport(handler), **kw)):
            first = await self.network.crawl('alpha', ['https://example.org/long'], max_pages=1, max_depth=0)
            tail = 'CHANGED'
            second = await self.network.crawl('alpha', ['https://example.org/long'], max_pages=1, max_depth=0)
        self.assertTrue(second['items'][0]['truncated'])
        self.assertEqual(second['items'][0]['state'], 'changed')
        self.assertNotEqual(first['items'][0]['hash'], second['items'][0]['hash'])

    async def test_industry_new_budget_allows_revision_but_rejects_second_new_fact(self):
        self.store.save_scope('alpha', {'max_new_evidence_per_run': 1})
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            ids = ['budget-one', self.record['id'], 'budget-two']
            part = ToolCallPart('archive_evidence', {'record': {**self.record, 'id': ids[calls - 1],
                'summary': '本轮修订资料', 'quote': '本轮修订资料'}}, tool_call_id=str(calls)) if calls <= 3 else TextPart('遵守新增上限，待用户核验确认。')
            return ModelResponse(parts=[part])
        ctx = ResearchContext(self.store, 'alpha', True,
            pages={self.record['url']: self.page(self.record['url'], text='本轮修订资料：企业披露合同，仍需核对履约进度。')})
        await build_agent(FunctionModel(model)).run('归档', deps=ctx)
        self.assertEqual(ctx.new_ids, {'budget-one'})
        self.assertEqual(ctx.imported_ids, [])
        self.assertEqual(len(ctx.pending_review_ids), 2)
        self.assertFalse((self.store.home('alpha') / 'evidence/budget-one.json').exists())
        self.assertFalse((self.store.home('alpha') / 'evidence/budget-two.json').exists())
        self.assertEqual(kb.read_json(self.store.home('alpha') / 'evidence' / (self.record['id'] + '.json'))['summary'], self.record['summary'])
        queue = VerificationQueue(self.store)
        for review_id in ctx.pending_review_ids:
            pending = queue.get('alpha', review_id)
            self.assertEqual(pending['status'], 'pending')
            approved = queue.decide('alpha', review_id, {'decision': 'approve', 'note': '已手动核对原文'})
            self.assertEqual(approved['evidence_ids'], [pending['web_record']['id']])
            material = Knowledge(self.store).get('alpha', approved['material_id'])
            self.assertIn(pending['web_record']['id'], material['related_evidence_ids'])
        self.assertTrue((self.store.home('alpha') / 'evidence/budget-one.json').exists())
        self.assertEqual(kb.read_json(self.store.home('alpha') / 'evidence' / (self.record['id'] + '.json'))['summary'], '本轮修订资料')
        self.assertTrue(list((self.store.home('alpha') / 'history' / self.record['id']).glob('*.json')))
        self.assertFalse((self.store.home('alpha') / 'evidence/budget-two.json').exists())

    async def test_agent_single_reader_obeys_robots_disallow(self):
        client = httpx.AsyncClient
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, text='User-agent: *\nDisallow: /private')
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            return ModelResponse(parts=[ToolCallPart('read_public_source', {'url': 'https://example.org/private/a'},
                tool_call_id='read') if calls == 1 else TextPart('禁止采集。')])
        ctx = ResearchContext(self.store, 'alpha', True, network=self.network)
        with patch('web.agent_runtime.public_url', new=AsyncMock(return_value=None)), \
             patch('web.agent_runtime.httpx.AsyncClient', lambda **kw: client(transport=httpx.MockTransport(handler), **kw)):
            await build_agent(FunctionModel(model)).run('读取', deps=ctx)
        self.assertEqual(paths, ['/robots.txt'])
        self.assertEqual(ctx.pages, {})

    async def fake_fetch(self, url, catalog, guard=None):
        if guard:
            await guard(url)
        self.fetches.append(url)
        return self.page(url, text=url, links=['https://example.org/public/a', 'https://example.org/public/b',
                                              'https://other.example/outside', 'https://example.org/public/a?utm_source=x'])

    async def batch(self, urls=None, max_pages=5, max_depth=1, fetch=None):
        with patch('web.agent_runtime.fetch_page', new=fetch or self.fake_fetch), patch.object(self.network.robots, 'check', new=AsyncMock()):
            return await self.network.crawl('alpha', urls or ['https://example.org/public/'], max_pages, max_depth)

    async def test_search_results_deduplicate_and_remain_unverified(self):
        with patch('web.network.DDGS') as engine:
            engine.return_value.__enter__.return_value.text.return_value = [
                {'href': 'https://example.org/a?utm_source=x', 'title': '第一项', 'body': '摘要'},
                {'href': 'https://example.org/a', 'title': '重复项'}, {'href': 'javascript:alert(1)'},
                {'href': 'https://else.example/b', 'title': '第二项'}]
            result = await self.network.search('alpha', '合同', 10, 'w')
        self.assertEqual(len(result['items']), 2)
        self.assertEqual({i['state'] for i in result['items']}, {'unverified'})
        self.assertEqual(self.store.state()['industries'][0]['evidence_count'], 1)

    async def test_source_only_filters_before_result_limit(self):
        self.store.save_sources('alpha', {'mode': 'only', 'sources': [{'id': 'official', 'name': '公开来源',
            'url': 'https://example.org/public/', 'enabled': True, 'priority': 1, 'categories': []}]})
        with patch('web.network.DDGS') as engine:
            search = engine.return_value.__enter__.return_value.text
            search.return_value = [{'href': 'https://example.org/outside'}, {'href': 'https://other.example/a'},
                                   {'href': 'https://example.org/public/article'}]
            result = await self.network.search('alpha', '调查', 1)
        self.assertEqual(result['items'][0]['url'], 'https://example.org/public/article')
        self.assertIn('site:example.org', search.call_args.args[0])

    async def test_empty_only_sources_and_backend_failure_are_not_empty_success(self):
        self.store.save_sources('alpha', {'mode': 'only', 'sources': []})
        with self.assertRaisesRegex(ValueError, '尚无启用'):
            await self.network.search('alpha', '测试')
        self.store.save_sources('alpha', {'mode': 'preferred', 'sources': []})
        with patch('web.network.DDGS', side_effect=RuntimeError('network down')):
            with self.assertRaisesRegex(ValueError, '未完成'):
                await self.network.search('alpha', '测试')

    async def test_searxng_json_adapter_and_validation(self):
        self.network.save_config({'provider': 'searxng', 'searxng_url': 'http://127.0.0.1:8080'})
        client = httpx.AsyncClient
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, json={'results': [{'url': 'https://example.org/a', 'title': '搜索', 'content': '摘要'}]})
        with patch('web.network.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs)):
            result = await self.network.search('alpha', '测试', period='w')
        self.assertEqual(result['items'][0]['title'], '搜索')
        self.assertEqual(captured[0].url.params['time_range'], 'week')
        with self.assertRaises(ValueError):
            self.network.save_config({'backend': 'auto'})
        with self.assertRaises(ValueError):
            self.network.save_config({'searxng_url': 'https://secret@example.org'})

    async def test_crawler_same_site_depth_and_url_dedup(self):
        job = await self.batch()
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['attempted'], 3)
        self.assertEqual(len(self.fetches), 3)
        self.assertNotIn('https://other.example/outside', self.fetches)
        self.assertFalse(any('utm_' in u for u in self.fetches))
        self.assertEqual(self.store.state()['industries'][0]['evidence_count'], 1)

    async def test_crawler_page_and_depth_limits(self):
        job = await self.batch(max_pages=1)
        self.assertEqual(job['attempted'], 1)
        self.assertEqual(job['remaining'], 2)
        job = await self.batch(max_depth=0)
        self.assertEqual(job['attempted'], 1)
        self.assertEqual(job['remaining'], 0)
        with self.assertRaises(ValueError):
            self.network.start('alpha', ['https://example.org'], 51, 1)

    async def test_candidates_are_incremental_not_full_article_copies(self):
        marker = 'DO-NOT-PERSIST-WHOLE-ARTICLE'
        async def fetch(url, catalog, guard=None):
            return self.page(url, 'a' * 800 + marker)
        first = await self.batch(max_depth=0, fetch=fetch)
        second = await self.batch(max_depth=0, fetch=fetch)
        self.assertEqual(first['counts']['new'], 1)
        self.assertEqual(second['counts']['unchanged'], 1)
        async def changed(url, catalog, guard=None):
            return self.page(url, 'changed-original')
        third = await self.batch(max_depth=0, fetch=changed)
        self.assertEqual(third['counts']['changed'], 1)
        for path in self.root.rglob('*.json'):
            self.assertNotIn(marker, path.read_text(encoding='utf-8'))
        self.assertIn(marker, self.network.pages[first['id']]['https://example.org/public/']['text'])
        restored = NetworkRuntime(self.store)
        self.assertIn(first['id'], restored.jobs)
        self.assertEqual(restored.pages, {})

    async def test_duplicate_content_and_per_item_failure(self):
        async def fetch(url, catalog, guard=None):
            if url.endswith('/bad'):
                raise ValueError('读取失败')
            return self.page(url, text='同一正文')
        job = await self.batch(['https://example.org/a', 'https://example.org/b', 'https://example.org/bad'], max_depth=0, fetch=fetch)
        self.assertEqual(job['status'], 'partial')
        self.assertEqual(job['counts']['duplicate'], 1)
        self.assertEqual(job['counts']['failed'], 1)
        self.assertIn('读取失败', job['items'][-1]['error'])

    async def test_all_failed_not_success(self):
        job = await self.batch(max_depth=0, fetch=AsyncMock(side_effect=ValueError('robots禁止')))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['counts']['failed'], 1)

    async def test_cancel_batch_preserves_finished_candidates(self):
        started = asyncio.Event()
        async def fetch(url, catalog, guard=None):
            if url.endswith('/slow'):
                started.set()
                await asyncio.Event().wait()
            return self.page(url)
        with patch('web.agent_runtime.fetch_page', new=fetch):
            job = self.network.start('alpha', ['https://example.org/a', 'https://example.org/slow'], max_depth=0)
            await asyncio.wait_for(started.wait(), 2)
            self.network.tasks[job['id']].cancel()
            await self.network.tasks[job['id']]
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(len(job['items']), 1)
        self.assertTrue((self.root / 'reports/crawls' / (job['id'] + '.json')).exists())

    async def test_redirect_cannot_leave_seed_site(self):
        original_client = httpx.AsyncClient
        urls = []
        def handler(request):
            urls.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://else.example/article'})
        with patch('web.agent_runtime.public_url', new=AsyncMock(return_value=None)), patch.object(self.network.robots, 'check', new=AsyncMock()), patch('web.agent_runtime.httpx.AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)):
            job = await self.network.crawl('alpha', ['https://example.org/a'], max_depth=0)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(urls, ['https://example.org/a'])

    async def test_only_mode_crawler_cannot_follow_outside_paths(self):
        self.store.save_sources('alpha', {'mode': 'only', 'sources': [{'id': 'official', 'name': '官网',
            'url': 'https://example.org/public/', 'enabled': True, 'priority': 1, 'categories': []}]})
        with self.assertRaises(ValueError):
            self.network.start('alpha', ['https://example.org/outside'])
        with self.assertRaises(ValueError):
            self.network.start('alpha', ['https://example.org/public/%2e%2e/private'])

    async def test_robots_disallow_missing_and_failure(self):
        original_client = httpx.AsyncClient
        cases = [(200, 'User-agent: *\nDisallow: /private', False), (404, '', True), (403, '', False), (500, '', False)]
        for status, body, allowed in cases:
            robots = Robots(delay=0)
            with patch('web.agent_runtime.public_url', new=AsyncMock(return_value=None)), patch('web.network.httpx.AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body)), **kwargs)):
                if allowed:
                    await robots.check('https://example.org/private/article')
                else:
                    with self.assertRaises((ValueError, httpx.HTTPStatusError)):
                        await robots.check('https://example.org/private/article')

    async def test_rss_links_are_extracted_as_original_candidates(self):
        client = httpx.AsyncClient
        xml = '<rss><channel><title>公告</title><item><title>新公告</title><link>https://example.org/news/a</link><pubDate>Mon, 12 Oct 2026</pubDate></item></channel></rss>'
        with patch('web.agent_runtime.public_url', new=AsyncMock(return_value=None)), patch('web.agent_runtime.httpx.AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=xml, headers={'Content-Type': 'application/rss+xml'})), **kwargs)):
            page = await fetch_page('https://example.org/feed', {'mode': 'preferred'})
        self.assertEqual(page['links'][0]['url'], 'https://example.org/news/a')
        self.assertIn('新公告', page['text'])

    async def test_agent_search_then_read_waits_for_confirmation_before_real_archive(self):
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            parts = [ToolCallPart('search_web', {'query': '合同'}, tool_call_id='search'),
                     ToolCallPart('read_public_source', {'url': self.record['url']}, tool_call_id='read'),
                     ToolCallPart('archive_evidence', {'record': {**self.record, 'id': 'network-archived',
                        'quote': '企业披露合同'}}, tool_call_id='archive')]
            return ModelResponse(parts=[parts[calls - 1] if calls <= 3 else TextPart('已读原文，是否通过核验并入库？')])
        deps = ResearchContext(self.store, 'alpha', True, network=self.network)
        with patch.object(self.network, 'search', new=AsyncMock(return_value={'items': [{'url': self.record['url']}]})), patch('web.agent_runtime.fetch_page', new=AsyncMock(return_value=self.page(self.record['url'], text='企业披露合同，仍需核对履约进度。'))):
            result = await build_agent(FunctionModel(model)).run('搜索并归档', deps=deps)
        self.assertIn('是否通过核验', result.output)
        self.assertFalse((self.store.home('alpha') / 'evidence/network-archived.json').exists())
        self.assertEqual(deps.imported_ids, [])
        queue = VerificationQueue(self.store)
        pending = queue.get('alpha', deps.pending_review_ids[0])
        self.assertEqual(pending['status'], 'pending')
        approved = queue.decide('alpha', pending['id'], {'decision': 'approve'})
        self.assertEqual(approved['evidence_ids'], ['network-archived'])
        self.assertTrue((self.store.home('alpha') / 'evidence/network-archived.json').exists())
        evidence = kb.read_json(self.store.home('alpha') / 'evidence/network-archived.json')
        self.assertEqual(evidence['related_material']['material_id'], approved['material_id'])
        self.assertIn(evidence['id'], Knowledge(self.store).get('alpha', approved['material_id'])['related_evidence_ids'])

    async def test_readonly_agent_cannot_search_or_add_sources(self):
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            return ModelResponse(parts=[ToolCallPart('search_web', {'query': '测试'}, tool_call_id='search') if calls == 1 else TextPart('当前只读。')])
        with patch.object(self.network, 'search', new=AsyncMock()) as search:
            await build_agent(FunctionModel(model)).run('搜索', deps=ResearchContext(self.store, 'alpha', False, network=self.network))
            search.assert_not_called()

    async def test_agent_batch_pages_are_real_archive_gate(self):
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            part = ToolCallPart('crawl_public_sources', {'urls': [self.record['url']], 'max_pages': 1, 'max_depth': 0}, tool_call_id='crawl') if calls == 1 else ToolCallPart('archive_evidence', {'record': {**self.record, 'id': 'batch-archived', 'quote': '企业披露合同'}}, tool_call_id='archive') if calls == 2 else TextPart('采集后等待用户核验确认。')
            return ModelResponse(parts=[part])
        deps = ResearchContext(self.store, 'alpha', True, network=self.network)
        with patch('web.agent_runtime.fetch_page', new=AsyncMock(return_value=self.page(self.record['url'], text='企业披露合同，仍需核对履约进度。'))):
            await build_agent(FunctionModel(model)).run('批量采集', deps=deps)
        self.assertFalse((self.store.home('alpha') / 'evidence/batch-archived.json').exists())
        self.assertEqual(deps.imported_ids, [])
        self.assertEqual(len(deps.pending_review_ids), 1)
        queue = VerificationQueue(self.store)
        pending = queue.get('alpha', deps.pending_review_ids[0])
        self.assertEqual(pending['status'], 'pending')
        approved = queue.decide('alpha', pending['id'], {'decision': 'approve'})
        self.assertEqual(approved['evidence_ids'], ['batch-archived'])
        self.assertTrue((self.store.home('alpha') / 'evidence/batch-archived.json').exists())
        self.assertIn('batch-archived', Knowledge(self.store).get('alpha', approved['material_id'])['related_evidence_ids'])

    async def test_fake_ip_resolution_pins_real_public_target(self):
        import socket
        original_client = httpx.AsyncClient
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.7', 443))]
        def handler(request):
            self.assertEqual(request.url.host, '1.1.1.1')
            self.assertEqual(request.url.params['name'], 'public.example')
            return httpx.Response(200, json={'Status': 0, 'Answer': [{'type': 1, 'data': '93.184.215.14'}]})
        with patch('web.agent_runtime.socket.getaddrinfo', return_value=addresses), patch('web.agent_runtime.httpx.AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)):
            self.assertEqual(await public_url('https://public.example/article'), 'https://93.184.215.14/article')

    async def test_public_dns_cannot_authorize_private_or_failed_answer(self):
        import socket
        original_client = httpx.AsyncClient
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.7', 443))]
        for data in ({'Status': 0, 'Answer': [{'type': 1, 'data': '127.0.0.1'}]}, {'Status': 3, 'Answer': [{'type': 1, 'data': '93.184.215.14'}]}):
            with patch('web.agent_runtime.socket.getaddrinfo', return_value=addresses), patch('web.agent_runtime.httpx.AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)), **kwargs)):
                with self.assertRaises(ValueError):
                    await public_url('https://public.example/article')

    async def test_private_literals_and_names_never_query_public_dns(self):
        import socket
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.10', 443))]
        with patch('web.agent_runtime.socket.getaddrinfo', return_value=addresses), patch('web.agent_runtime.httpx.AsyncClient') as client:
            for url in ['https://192.168.1.10/', 'https://public.example/', 'https://printer.local/', 'http://localhost/']:
                with self.assertRaises(ValueError):
                    await public_url(url)
            client.assert_not_called()

    async def test_fetch_uses_pinned_ip_with_original_host_and_tls_name(self):
        original_client = httpx.AsyncClient
        def handler(request):
            self.assertEqual(request.url.host, '93.184.215.14')
            self.assertEqual(request.headers['Host'], 'public.example')
            self.assertEqual(request.extensions['sni_hostname'], 'public.example')
            return httpx.Response(200, text='<title>原文</title><p>正文</p>', headers={'Content-Type': 'text/html'})
        with patch('web.agent_runtime.public_url', new=AsyncMock(return_value='https://93.184.215.14/article')), patch('web.agent_runtime.httpx.AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)):
            page = await fetch_page('https://public.example/article', {'mode': 'preferred'})
        self.assertEqual(page['url'], 'https://public.example/article')

    async def test_write_lock_failure_does_not_report_persisted_success(self):
        with kb.lock(self.root):
            job = await self.batch(max_depth=0)
        self.assertEqual(job['status'], 'partial')
        self.assertIn('记录写入失败', job['error'])
        self.assertFalse((self.root / 'reports/crawls' / (job['id'] + '.json')).exists())

    async def test_source_tool_requires_ui_permission_and_respects_only_mode(self):
        async def run(allow):
            calls = 0
            def model(messages, info):
                nonlocal calls
                calls += 1
                part = ToolCallPart('add_information_source', {'name': '新来源', 'url': 'https://example.org/', 'source_id': 'new-source'}, tool_call_id='source') if calls == 1 else TextPart('完成。')
                return ModelResponse(parts=[part])
            with patch('web.agent_runtime.public_url', new=AsyncMock(return_value=None)):
                return await build_agent(FunctionModel(model)).run('添加来源', deps=ResearchContext(self.store, 'alpha', True, network=self.network, allow_source_changes=allow))
        await run(False)
        self.assertEqual(self.store.sources('alpha')['sources'], [])
        await run(True)
        self.assertEqual(self.store.sources('alpha')['sources'][0]['id'], 'new-source')
        self.assertFalse(self.store.sources('alpha')['sources'][0]['verified'])
        self.store.save_sources('alpha', {'mode': 'only', 'sources': []})
        await run(True)
        self.assertEqual(self.store.sources('alpha')['sources'], [])


if __name__ == '__main__':
    unittest.main()
