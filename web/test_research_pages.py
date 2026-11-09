import asyncio
import json
import unittest

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from .test_web import Fixture
from .agent_runtime import AgentRuntime, ResearchContext
from .charts import register_evidence, register_materials
from .knowledge import Knowledge
from .research_pages import PageSpec, ResearchPages, validate_page, page_requested
from .server import create_app
from .service import kb


def spec_for(ref):
    return {'title': '合同研究展示', 'summary': '来源记载合同，履约待查。', 'summary_references': [ref],
            'regions': ['中国'], 'tags': ['合同'], 'coverage': '仅依据检索片段，不代表全行业覆盖。',
            'gaps': ['未取得履约进展'], 'blocks': [
                {'kind': 'cards', 'title': '核心发现', 'items': [{'label': '合同披露', 'text': '企业披露合同。', 'references': [ref]}]},
                {'kind': 'table', 'title': '阶段比较', 'columns': ['事项', '进展'], 'items': [{'cells': ['合同', '待查履约'], 'references': [ref]}]},
                {'kind': 'prose', 'title': '待查', 'items': [{'text': '需要进一步读取履约公告。', 'nature': 'inference'}]}]}


class PageTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.pages = ResearchPages(self.store)
        self.ctx = ResearchContext(self.store, 'alpha')
        self.ref = register_evidence(self.ctx, self.store.evidence('alpha', '合同'))[0]['chart_ref']

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        return self.pages.commit('alpha', validate_page(self.ctx, PageSpec.model_validate(spec_for(self.ref))), '生成展示页')

    def test_explicit_all_industry_page_remains_readable_and_scoped(self):
        ctx = ResearchContext(self.store, 'all')
        ref = register_evidence(ctx, self.store.evidence('all', '合同'))[0]['chart_ref']
        page = self.pages.commit('all', validate_page(ctx, PageSpec.model_validate(spec_for(ref))),
                                 '跨行业生成展示页', context=ctx)
        self.assertEqual(self.pages.detail(page['id'])['industry'], 'all')
        self.assertIn(page['id'], [p['id'] for p in self.pages.listing('all')])
        self.assertNotIn(page['id'], [p['id'] for p in self.pages.listing('alpha')])

    def test_explicit_all_industry_draft_is_visible_without_implicit_local_sharing(self):
        from .page_recovery import PageDrafts
        draft = {'id':'cross-industry-draft','industry':'all','status':'paused',
                 'plan':{'scenes':[]},'scenes':{},'updated_at':kb.now()}
        kb.atomic_write(self.store.root/'reports/page-drafts/cross-industry-draft.json', kb.json_text(draft))
        drafts = PageDrafts(self.store)
        self.assertEqual(drafts.get(draft['id'])['industry'], 'all')
        self.assertIn(draft['id'], [d['id'] for d in drafts.listing('all')])
        self.assertEqual(drafts.listing('alpha'), [])

    def test_explicit_trigger_excludes_questions_negations_and_ordinary_qa(self):
        for text in ('生成合同研究展示页', '把搜索结果整理成展示页', '制作一份项目计划展示页', '做一个政策前端页面'):
            self.assertTrue(page_requested(text), text)
        for text in ('查询合同', '不要生成展示页', '能否生成展示页', '制定生成页面的开发计划', '生成页面功能怎么做'):
            self.assertFalse(page_requested(text), text)

    def test_atomic_revisions_metadata_copy_restore_delete_and_filters(self):
        before = list((self.store.home('alpha')/'evidence').glob('*.json'))
        p = self.create()
        self.assertEqual(self.pages.listing(q='合同', region='中国', tag='合同')[0]['id'], p['id'])
        renamed = self.pages.mutate(p['id'], 'metadata', 1, title='新名称', favorite=True)
        self.assertEqual(renamed['version'], 2)
        self.assertEqual(self.pages.detail(p['id'], 1)['revision']['content']['title'], '合同研究展示')
        with self.assertRaisesRegex(ValueError, '版本'):
            self.pages.mutate(p['id'], 'delete', 1)
        copied = self.pages.mutate(p['id'], 'copy', 2)
        self.assertNotEqual(copied['id'], p['id'])
        self.assertEqual(copied['version'], 1)
        self.pages.mutate(p['id'], 'restore', 2, revision=1)
        self.assertEqual(self.pages.detail(p['id'])['title'], '合同研究展示')
        self.pages.mutate(p['id'], 'metadata', 3, archived=True)
        self.assertEqual(len(self.pages.listing(archived=True)), 1)
        self.pages.mutate(p['id'], 'delete', 4)
        self.assertEqual(len(self.pages.listing()), 1)
        self.assertEqual(list((self.store.home('alpha')/'evidence').glob('*.json')), before)

    def test_invalid_references_missing_sources_malformed_tables_and_credentials(self):
        for mutate in (
            lambda s: s.update(summary_references=['forged']),
            lambda s: s['blocks'][0]['items'][0].update(references=[]),
            lambda s: s['blocks'][1]['items'][0].update(cells=['only one']),
            lambda s: s.update(title='sk-'+'a'*32),
        ):
            spec=spec_for(self.ref);mutate(spec)
            with self.assertRaises(ValueError):
                validate_page(self.ctx, PageSpec.model_validate(spec))
        with self.assertRaises(ValueError):
            self.pages.get('../outside')

    def test_material_revocation_and_version_changes_block_model_edits_but_preserve_history(self):
        knowledge=Knowledge(self.store)
        material=knowledge.save('alpha', {'title':'项目说明','content':'合同实施进展待查。','model_allowed':True})
        ref=register_materials(self.ctx, knowledge.search('alpha','合同'))[0]['snippets'][0]['chart_ref']
        payload=validate_page(self.ctx,PageSpec.model_validate(spec_for(ref)))
        p=self.pages.commit('alpha',payload,'生成页面')
        knowledge.save('alpha', {'id':material['id'],'model_allowed':False})
        self.assertTrue(self.pages.detail(p['id'])['warnings'])
        with self.assertRaisesRegex(ValueError,'授权'):
            self.pages.prepare_edit(ResearchContext(self.store,'alpha'),p['id'],1)
        self.assertEqual(self.pages.detail(p['id'])['revision']['content']['title'],'合同研究展示')

    def test_plain_evidence_changes_warn_and_block_staged_page(self):
        p=self.create()
        self.store.import_evidence('alpha',{**self.record,'summary':'新版内容'})
        self.assertTrue(self.pages.detail(p['id'])['warnings'])
        with self.assertRaisesRegex(ValueError,'版本'):
            validate_page(self.ctx,PageSpec.model_validate(spec_for(self.ref)))
        ctx=ResearchContext(self.store,'alpha')
        refreshed=self.pages.prepare_edit(ctx,p['id'],1,refresh=True)
        self.assertIn('original_request',refreshed)
        self.assertNotIn('合同研究展示',refreshed)
        self.assertEqual(ctx.chart_sources,{})

    def test_api_local_guards_and_history(self):
        p=self.create()
        with TestClient(create_app(self.root),headers={'X-KB-Request':'1'}) as client:
            self.assertEqual(len(client.get('/api/pages').json()),1)
            self.assertEqual(client.get('/api/pages/'+p['id']).json()['version'],1)
            self.assertEqual(client.post('/api/pages/'+p['id']+'/metadata',json={'version':1,'title':'新名称'},headers={'Origin':'https://evil.example'}).status_code,403)
            self.assertEqual(client.post('/api/pages/'+p['id']+'/metadata',json={'version':1,'title':'新名称'}).status_code,200)
            self.assertEqual(client.post('/api/pages/'+p['id']+'/delete',json={'version':1}).status_code,400)
            self.assertEqual(client.get('/api/pages/'+p['id']+'?revision=1').json()['revision']['content']['title'],'合同研究展示')

    def test_page_chart_budget_is_independent_and_does_not_leak_on_retry(self):
        spec=spec_for(self.ref)
        spec['blocks'].append({'kind':'chart','title':'阶段','chart':{'kind':'timeline','title':'合同阶段',
            'items':[{'label':'履约日期未知','status':'uncertain','references':[self.ref]}]}})
        self.ctx.chart_count=3
        payload=validate_page(self.ctx,PageSpec.model_validate(spec))
        self.assertEqual(self.ctx.chart_count,3)
        self.assertEqual(payload['content']['blocks'][-1]['figure']['kind'],'timeline')


class PageAgentTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup()

    def tearDown(self):
        self.temp.cleanup()

    def job(self, **changes):
        return {'id':kb.uuid4().hex,'conversation':kb.uuid4().hex,'industry':'alpha','research':False,
                'status':'running','events':[],'started_at':kb.now(),'generate_page':True,**changes}

    async def generate(self, job=None, after_prepare=None):
        runtime=AgentRuntime(self.store)
        job=job or self.job()
        def model(messages,info):
            returns=[p for m in messages for p in m.parts if isinstance(p,ToolReturnPart)]
            if not returns:
                return ModelResponse(parts=[ToolCallPart('search_evidence',{'query':'合同'},tool_call_id='search')])
            if not any(p.tool_name=='compose_research_page' for p in returns):
                ref=next(p.content[0]['chart_ref'] for p in returns if p.tool_name=='search_evidence')
                return ModelResponse(parts=[ToolCallPart('compose_research_page',{'page':spec_for(ref)},tool_call_id='page')])
            if after_prepare:
                after_prepare()
            return ModelResponse(parts=[TextPart('已整理展示页。')])
        await runtime.run(job,'生成合同研究展示页',model_override=FunctionModel(model))
        return job

    async def test_real_tools_save_page_event_and_opt_out_conversation_stays_empty(self):
        job=await self.generate()
        self.assertEqual(job['status'],'completed',job['events'])
        saved=next(e['page'] for e in job['events'] if e['type']=='page_saved')
        self.assertTrue(ResearchPages(self.store).detail(saved['id'])['revision']['sources'])
        self.assertEqual(Knowledge(self.store).conversations('alpha'),[])
        self.assertFalse(any('content' in e for e in kb.read_json(self.root/'reports/agent-runs'/(job['id']+'.json'))['events']))

    async def test_model_without_page_submission_fails_without_creating_artifact(self):
        job=self.job()
        await AgentRuntime(self.store).run(job,'生成展示页',model_override=FunctionModel(lambda m,i:ModelResponse(parts=[TextPart('仅文字')])) )
        self.assertEqual(job['status'],'failed')
        self.assertEqual(ResearchPages(self.store).listing(),[])

    async def test_cancel_after_staging_does_not_save(self):
        def cancel():
            raise asyncio.CancelledError()
        job=await self.generate(after_prepare=cancel)
        self.assertEqual(job['status'],'cancelled')
        self.assertEqual(ResearchPages(self.store).listing(),[])

    async def test_ordinary_qa_has_no_page_tool(self):
        def model(messages,info):
            self.assertNotIn('compose_research_page',[t.name for t in info.function_tools])
            return ModelResponse(parts=[TextPart('普通回答')])
        job=self.job(generate_page=False)
        await AgentRuntime(self.store).run(job,'检索合同',model_override=FunctionModel(model))
        self.assertEqual(job['status'],'completed',job['events'])
        self.assertEqual(ResearchPages(self.store).listing(),[])

    async def test_late_model_cannot_overwrite_new_metadata(self):
        first=await self.generate()
        p=next(e['page'] for e in first['events'] if e['type']=='page_saved')
        def rename():
            ResearchPages(self.store).mutate(p['id'],'metadata',1,title='用户新改名')
        second=await self.generate(self.job(page_id=p['id'],page_version=1,page_operation='layout'),after_prepare=rename)
        self.assertEqual(second['status'],'failed',second['events'])
        self.assertEqual(ResearchPages(self.store).detail(p['id'])['title'],'用户新改名')

    async def test_refresh_after_source_revision_uses_new_sources_and_keeps_original_topic(self):
        first=await self.generate()
        p=next(e['page'] for e in first['events'] if e['type']=='page_saved')
        pages=ResearchPages(self.store)
        self.store.import_evidence('alpha',{**self.record,'summary':'企业披露合同的新版内容。'})
        second=await self.generate(self.job(page_id=p['id'],page_version=1,page_operation='refresh'))
        self.assertEqual(second['status'],'completed',second['events'])
        detail=pages.detail(p['id'])
        self.assertEqual(detail['version'],2)
        self.assertIn('新版内容',detail['revision']['sources'][0]['text'])
        self.assertEqual(pages.get(p['id'])['research_request'],'生成合同研究展示页')
