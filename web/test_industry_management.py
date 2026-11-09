"""Reversible industry management with real isolated registry and source files."""
from concurrent.futures import Future
from unittest.mock import patch
import unittest

from fastapi.testclient import TestClient

from .knowledge import Knowledge
from .material_index import MaterialIndex
from .page_recovery import PageDrafts
from .research_pages import ResearchPages
from .server import create_app
from .service import kb, ws
from .test_web import Fixture


class IndustryManagementTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.app = create_app(self.root)
        self.client = TestClient(self.app, headers={'X-KB-Request':'1'})
        self.knowledge = Knowledge(self.store)
        self.knowledge.save('alpha', {'id':'original','title':'保留原资料','content':'赤松密度每亩七十株。','model_allowed':True})
        MaterialIndex(self.store).build('alpha','original')

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def preview(self, key='alpha'):
        response=self.client.get('/api/industries/'+key+'/deletion-preview')
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def manage(self, action, key='alpha', token=None, **extra):
        if token is None:
            config=ws.registry(self.root)
            items=config.get('deleted_industries',[]) if action=='restore' else config['industries']
            token=ws.industry_token(next(e for e in items if e['id']==key))
        return self.client.post('/api/industries/'+key+'/manage',json={
            'action':action,'expected_token':token,**extra})

    def test_delete_current_hides_retrieval_and_restore_retains_all_source_bytes(self):
        ws.industry_command(self.root,self.store.args(action='use',id='alpha'))
        ws.industry_command(self.root,self.store.args(action='enable',id='alpha'))
        home=self.store.home('alpha')
        before={p.relative_to(home).as_posix():p.read_bytes() for p in home.rglob('*') if p.is_file()}
        preview=self.preview()
        self.assertTrue(preview['is_current'])
        self.assertEqual(preview['material_count'],1)
        self.assertEqual(self.manage('delete').status_code,200)
        state=self.client.get('/api/state').json()
        self.assertIsNone(state['active_industry'])
        self.assertEqual(state['industries'],[])
        self.assertEqual(state['deleted_industries'][0]['id'],'alpha')
        self.assertFalse(state['deleted_industries'][0]['enabled'])
        self.assertEqual(self.client.get('/api/materials?industry=all&q=赤松').json(),[])
        self.assertEqual(self.client.get('/api/materials/alpha/original').status_code,400)
        self.assertEqual(before,{p.relative_to(home).as_posix():p.read_bytes() for p in home.rglob('*') if p.is_file()})
        self.assertEqual(self.manage('restore').status_code,200)
        self.assertIsNone(self.client.get('/api/state').json()['active_industry'])
        self.assertFalse(self.store.entry('alpha')['enabled'])
        self.assertIn('七十株',MaterialIndex(self.store).search('alpha','赤松')[0]['snippets'][0]['text'])

    def test_root_profile_deletion_never_moves_program_or_root_data(self):
        config=ws.registry(self.root)
        config['industries'][0]['path']='.'
        kb.atomic_write(self.root/'config/industries.json',kb.json_text(config))
        kb.atomic_write(self.root/'program.py','print("keep program")\n')
        kb.atomic_write(self.root/'evidence/legacy.json',kb.json_text(self.record|{'id':'legacy'}))
        before=(self.root/'evidence/legacy.json').read_bytes()
        self.assertEqual(self.manage('delete').status_code,200)
        self.assertEqual((self.root/'program.py').read_text(),'print("keep program")\n')
        self.assertEqual((self.root/'evidence/legacy.json').read_bytes(),before)
        self.assertEqual(ws.selected(self.root,'all',many=True),[])
        self.assertEqual(self.manage('restore').status_code,200)
        self.assertEqual(self.store.home('alpha'),self.root)

    def test_deleted_pages_drafts_memory_and_conversations_are_hidden_and_restored(self):
        page={'id':'saved-page','industry':'alpha','title':'保留页面','tags':[],'favorite':False,'archived':False,
            'created_at':kb.now(),'updated_at':kb.now(),'version':1,'revisions':[{'content':{'title':'保留页面',
                'regions':[],'summary':'旧原文摘要','scenes':[]}}]}
        kb.atomic_write(self.root/'reports/pages/saved-page.json',kb.json_text(page))
        kb.atomic_write(self.root/'reports/page-drafts/draft-one.json',kb.json_text({
            'id':'draft-one','industry':'alpha','status':'paused','plan':{'scenes':[]},'scenes':{},'updated_at':kb.now()}))
        self.knowledge.save_memory({'industry':'alpha','title':'偏好','content':'资料偏好','model_allowed':True})
        conversation={'id':'conversation-one','industry':'alpha','updated_at':kb.now(),'turns':[]}
        kb.atomic_write(self.root/'data/conversations/conversation-one.json',kb.json_text(conversation))
        self.assertEqual(self.preview()['page_count'],1)
        self.assertEqual(self.preview()['draft_count'],1)
        self.manage('delete')
        self.assertEqual(ResearchPages(self.store).listing('all'),[])
        self.assertEqual(PageDrafts(self.store).listing('all'),[])
        self.assertEqual(self.knowledge.memories('all',model_only=True),[])
        self.assertEqual(self.knowledge.conversations('all'),[])
        with self.assertRaises(ValueError):
            ResearchPages(self.store).get('saved-page')
        self.manage('restore')
        self.assertEqual(len(ResearchPages(self.store).listing('all')),1)
        self.assertEqual(len(PageDrafts(self.store).listing('all')),1)
        self.assertEqual(len(self.knowledge.memories('all')),1)
        self.assertEqual(len(self.knowledge.conversations('all')),1)

    def test_rename_stale_confirmation_and_restore_aba_protection(self):
        token=self.preview()['management_token']
        renamed=self.manage('rename',token=token,name='新的行业名称')
        self.assertEqual(renamed.status_code,200,renamed.text)
        self.assertEqual(self.store.entry('alpha')['name'],'新的行业名称')
        self.assertEqual(self.manage('delete',token=token).status_code,400)
        token=self.preview()['management_token']
        self.manage('delete',token=token)
        self.manage('restore')
        self.assertEqual(self.manage('delete',token=token).status_code,400)
        self.assertEqual(self.knowledge.get('alpha','original')['content'],'赤松密度每亩七十株。')

    def test_busy_cross_site_and_bad_input_do_not_delete(self):
        token=self.preview()['management_token']
        with patch.dict(self.app.state.runtime.tasks,{'running':Future()}):
            self.assertEqual(self.manage('delete',token=token).status_code,400)
        self.assertEqual(self.client.post('/api/industries/alpha/manage',json={
            'action':'delete','expected_token':token},headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.manage('rename',name='\n').status_code,400)
        self.assertEqual(self.client.post('/api/industries/alpha/manage',json={'action':'delete'}).status_code,422)
        self.assertEqual(self.store.entry('alpha')['id'],'alpha')

    def test_trash_reserves_id_without_implicit_industry_dependencies(self):
        scope=ws.scope(self.store.home('alpha'))
        kb.atomic_write(self.store.home('alpha')/'config/scope.json',kb.json_text(scope|{
            'specialty':'forestry','library_role':'national-foundation'}))
        ws.industry_command(self.root,self.store.args(action='add',id='beta',name='地方试点',region='中国',
            categories=None))
        self.assertNotIn('shared_with',self.preview())
        self.manage('delete')
        self.assertEqual(self.client.post('/api/industries',json={'id':'alpha','name':'同编号','region':'中国'}).status_code,400)
        self.assertEqual([e['id'] for e in self.store.research_entries('beta')],['beta'])
        self.manage('restore')
        self.assertEqual([e['id'] for e in self.store.research_entries('beta')],['beta'])
