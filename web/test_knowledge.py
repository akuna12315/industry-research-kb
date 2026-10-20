import asyncio
from datetime import timedelta
import hashlib
import io
import json
import unittest
import zipfile

from fastapi.testclient import TestClient
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, TextPart
from pypdf import PdfWriter

from .test_web import Fixture
from .knowledge import Knowledge
from .server import create_app
from .service import kb, ws
from .agent_runtime import build_agent, ResearchContext, AgentRuntime


class KnowledgeTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.client = TestClient(create_app(self.root), headers={'X-KB-Request': '1'})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def material(self, **changes):
        return self.knowledge.save('alpha', {'title': '造林技术规程', 'kind': 'standard',
            'number': 'GB/T 15776-2023', 'content': '资料正文：造林作业设计依据，需要核验引用版本。', **changes})

    def import_file(self, metadata, filename, raw):
        serialized=metadata if isinstance(metadata,str) else json.dumps(metadata,ensure_ascii=False)
        return self.client.post('/api/materials/alpha/import',data={'metadata':serialized},
            files={'file':(filename,raw,'application/octet-stream')})

    @staticmethod
    def import_docx_bytes(text):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml',
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+text+'</w:t></w:r></w:p></w:body></w:document>')
        return raw.getvalue()

    def assert_import_absent(self,key):
        folder=self.knowledge.folder('alpha')
        self.assertFalse((folder/(key+'.json')).exists())
        self.assertEqual(list((folder/'history'/key).glob('*.json')),[])

    def material_file_snapshot(self):
        folder=self.knowledge.folder('alpha')
        return {path.relative_to(folder):path.read_bytes() for path in folder.rglob('*') if path.is_file()}

    def test_atomic_import_broken_docx_does_not_create_record_or_history(self):
        for metadata in ({'id':'atomic-broken','title':'坏附件不应建目录'}, {'title':'自动ID坏附件不应建目录'}):
            with self.subTest(metadata=metadata):
                before=set(self.knowledge.folder('alpha').glob('*.json'))
                response=self.import_file(metadata,'broken.docx',b'not a DOCX archive')
                self.assertEqual(response.status_code,400,response.text)
                self.assertEqual(set(self.knowledge.folder('alpha').glob('*.json')),before)
                if metadata.get('id'):
                    self.assert_import_absent(metadata['id'])

    def test_atomic_import_invalid_metadata_does_not_create_record_or_history(self):
        for suffix,raw in (('.txt',b'synthetic original'),('.docx',self.import_docx_bytes('原文已解析但元数据无效'))):
            for kind,changes in (('empty',{'title':''}),('restricted',{'title':'受限资料','sensitivity':'restricted','model_allowed':True})):
                key='atomic-invalid-'+kind+suffix.replace('.','-')
                with self.subTest(key=key):
                    before_files=self.material_file_snapshot()
                    response=self.import_file({'id':key,**changes},'original'+suffix,raw)
                    self.assertEqual(response.status_code,400,response.text)
                    self.assert_import_absent(key)
                    self.assertEqual(self.material_file_snapshot(),before_files,
                        'failed metadata must not leave an original, index, history or staging file')

    def test_atomic_import_failed_replacement_keeps_existing_json_original_index_and_history(self):
        key='atomic-preserve'
        first=self.import_file({'id':key,'title':'第一版标题','version':'v1','model_allowed':True},
            'v1.docx',self.import_docx_bytes('第一版独有原文与表述'))
        self.assertEqual(first.status_code,200,first.text)
        record=first.json()
        folder=self.knowledge.folder('alpha')
        metadata=folder/(key+'.json')
        original=folder/'files'/record['attachment']['storage_name']
        index=folder/record['extraction']['text_index']
        tracked={path:path.read_bytes() for path in (metadata,original,*index.parent.glob('*.json'))}
        before_history={path:path.read_bytes() for path in (folder/'history'/key).glob('*.json')}
        before_files=self.material_file_snapshot()
        for name,changes,raw in (
            ('broken-docx',{'title':'第二版标题','version':'v2','summary':'不应保存'},b'broken DOCX'),
            ('invalid-metadata',{'title':'','version':'v2','notes':'不应保存'},self.import_docx_bytes('第二版不应发布的原文')),
        ):
            with self.subTest(name=name):
                response=self.import_file({'id':key,**changes},'v2.docx',raw)
                self.assertEqual(response.status_code,400,response.text)
                self.assertEqual({path:path.read_bytes() for path in tracked},tracked)
                self.assertEqual({path:path.read_bytes() for path in (folder/'history'/key).glob('*.json')},before_history)
                self.assertEqual(self.material_file_snapshot(),before_files)
                self.assertEqual(self.knowledge.get('alpha',key)['version'],'v1')
                old_hit=self.knowledge.search('alpha','第一版独有原文')
                self.assertEqual(len(old_hit),1)
                self.assertIn('第一版独有原文',old_hit[0]['snippets'][0]['text'])
                self.assertEqual(self.knowledge.search('alpha','第二版不应发布的原文'),[])

    def test_atomic_import_txt_create_replace_preserves_original_and_metadata_history(self):
        key='atomic-text'
        first_raw='第一版文字独有词'.encode()
        first=self.import_file({'id':key,'title':'第一版资料','version':'v1'},'v1.txt',first_raw)
        self.assertEqual(first.status_code,200,first.text)
        record=first.json()
        self.assertEqual(record['sensitivity'],'internal')
        self.assertFalse(record['model_allowed'])
        original,_=self.knowledge.attachment('alpha',key)
        self.assertEqual(self.client.get('/api/materials/alpha/'+key+'/file').content,first_raw)
        second_raw='第二版文字独有词'.encode()
        second=self.import_file({'id':key,'title':'第二版资料','version':'v2'},'v2.txt',second_raw)
        self.assertEqual(second.status_code,200,second.text)
        revised=second.json()
        self.assertEqual(revised['title'],'第二版资料')
        self.assertEqual(revised['version'],'v2')
        self.assertEqual(revised['attachment']['sha256'],hashlib.sha256(second_raw).hexdigest())
        self.assertEqual(original.read_bytes(),first_raw)
        self.assertEqual(self.client.get('/api/materials/alpha/'+key+'/file').content,second_raw)
        history=[kb.read_json(path) for path in (self.knowledge.folder('alpha')/'history'/key).glob('*.json')]
        self.assertEqual(len(history),1)
        self.assertEqual(history[0]['version'],'v1')
        self.assertEqual(history[0]['attachment'],record['attachment'])
        self.assertEqual(len(self.knowledge.list('alpha','第二版文字独有词')),1)
        self.assertEqual(self.knowledge.list('alpha','第一版文字独有词'),[])

    def test_atomic_import_docx_create_replace_commits_new_index_and_retains_old(self):
        key='atomic-docx'
        first_raw=self.import_docx_bytes('旧版可定位正文')
        first=self.import_file({'id':key,'title':'旧版DOCX','version':'v1','model_allowed':True},'v1.docx',first_raw)
        self.assertEqual(first.status_code,200,first.text)
        before=first.json()
        second_raw=self.import_docx_bytes('新版可定位正文')
        second=self.import_file({'id':key,'title':'新版DOCX','version':'v2'},'v2.docx',second_raw)
        self.assertEqual(second.status_code,200,second.text)
        after=second.json()
        self.assertEqual(after['title'],'新版DOCX')
        self.assertEqual(after['version'],'v2')
        self.assertNotEqual(after['extraction']['text_index'],before['extraction']['text_index'])
        folder=self.knowledge.folder('alpha')
        self.assertTrue((folder/before['extraction']['text_index']).is_file())
        self.assertEqual((folder/'files'/before['attachment']['storage_name']).read_bytes(),first_raw)
        self.assertEqual(self.client.get('/api/materials/alpha/'+key+'/file').content,second_raw)
        parts=self.client.get('/api/materials/alpha/'+key+'/sections',params={'q':'新版可定位正文'}).json()
        self.assertEqual(parts['total'],1)
        self.assertIn('正文·段落1',parts['items'][0]['first_location'])
        self.assertEqual(self.knowledge.search('alpha','旧版可定位正文'),[])
        self.assertIn('新版可定位正文',self.knowledge.search('alpha','新版可定位正文')[0]['snippets'][0]['text'])
        history=[kb.read_json(path) for path in (folder/'history'/key).glob('*.json')]
        self.assertEqual(len(history),1)
        self.assertEqual(history[0]['version'],'v1')

    def test_atomic_import_file_size_checks_attachment_without_counting_multipart_overhead(self):
        response=self.import_file({'id':'atomic-too-large','title':'超过上传限额'},'large.tif',b'x'*10_000_001)
        self.assertEqual(response.status_code,400,response.text)
        self.assert_import_absent('atomic-too-large')
        response=self.import_file({'id':'atomic-at-limit','title':'恰好上传限额'},'limit.tif',b'x'*10_000_000)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('未做OCR',response.json()['extraction']['note'])

    def test_atomic_import_rejects_invalid_json_and_top_level_arrays(self):
        for serialized in ('{', '[]', '[{"title":"不能以数组导入"}]', 'null', 'true', '"string"'):
            with self.subTest(serialized=serialized):
                before=set(self.knowledge.folder('alpha').glob('*.json'))
                response=self.import_file(serialized,'small.txt',b'original')
                self.assertEqual(response.status_code,400,response.text)
                self.assertEqual(set(self.knowledge.folder('alpha').glob('*.json')),before)
        serialized=' '*1_000_001+'{}'
        response=self.import_file(serialized,'small.txt',b'original')
        self.assertEqual(response.status_code,400,response.text)
        self.assertIn('大小上限',response.json()['detail'])

    def test_atomic_import_rejects_invalid_metadata_arrays_without_publishing(self):
        for key,changes in (
            ('atomic-tags-type',{'tags':'not-an-array'}),
            ('atomic-tags-size',{'tags':['x'*101]}),
            ('atomic-related-type',{'related_evidence_ids':{}}),
            ('atomic-related-missing',{'related_evidence_ids':['missing-evidence']}),
        ):
            with self.subTest(key=key):
                response=self.import_file({'id':key,'title':'数组边界',**changes},'small.txt',b'original')
                self.assertEqual(response.status_code,400,response.text)
                self.assert_import_absent(key)

    def test_atomic_import_nontext_replacement_preserves_omitted_content(self):
        record=self.material(id='atomic-image',content='应保留的原目录说明')
        response=self.import_file({'id':record['id'],'title':'新图像标题','version':'v2','notes':'仅改附件说明'},
            'image.png',b'synthetic image metadata only')
        self.assertEqual(response.status_code,200,response.text)
        updated=response.json()
        self.assertEqual(updated['content'],'应保留的原目录说明')
        self.assertEqual(updated['title'],'新图像标题')
        self.assertEqual(updated['notes'],'仅改附件说明')
        self.assertEqual(updated['extraction']['sections'],0)

    def test_private_material_not_returned_to_agent(self):
        record = self.material()
        self.assertEqual(len(self.knowledge.list('alpha', '造林')), 1)
        self.assertEqual(self.knowledge.search('alpha', '造林'), [])
        self.knowledge.save('alpha', {'id': record['id'], 'model_allowed': True})
        hit = self.knowledge.search('alpha', '造林')[0]
        self.assertIn('#chars-', hit['snippets'][0]['citation'])
        self.assertNotIn('content', hit)

    def test_restricted_material_cannot_enable_model(self):
        with self.assertRaises(ValueError):
            self.material(sensitivity='restricted', model_allowed=True)

    def test_standard_number_search_normalizes_spaces_and_hyphens(self):
        self.material()
        self.assertEqual(len(self.knowledge.list('alpha', 'GB/T15776')), 1)

    def test_metadata_revision_preserves_old_content(self):
        record = self.material()
        self.knowledge.save('alpha', {'id': record['id'], 'version': '修订备注'})
        files = list((self.knowledge.folder('alpha') / 'history' / record['id']).glob('*.json'))
        self.assertEqual(kb.read_json(files[0])['content'], record['content'])

    def test_invalid_id_and_unknown_evidence_rejected(self):
        with self.assertRaises(ValueError):
            self.material(id='../outside')
        with self.assertRaises(ValueError):
            self.material(related_evidence_ids=['missing-evidence'])

    def test_factual_version_status_requires_source_and_date(self):
        with self.assertRaises(ValueError):
            self.material(status='current')
        record = self.material(status='current', url='https://example.org/standard', checked_date=self.record['checked_date'])
        self.assertEqual(record['status_label'], '现行/有效')

    def test_upcoming_date_does_not_automatically_become_current(self):
        past = (kb.datetime.now(kb.TZ).date() - timedelta(days=1)).isoformat()
        record = self.material(status='upcoming', effective_date=past, url='https://example.org/standard', checked_date=self.record['checked_date'])
        self.assertTrue(record['review_due'])
        self.assertIn('需重新核对', record['status_label'])
        self.assertEqual(record['status'], 'upcoming')

    def test_utf8_upload_extracts_content_and_downloads_original(self):
        record = self.material()
        raw = '江西造林资料正文。'.encode()
        result = self.client.post(f"/api/materials/alpha/{record['id']}/upload", files={'file': ('project.txt', raw, 'text/plain')})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn('江西造林', result.json()['content'])
        downloaded = self.client.get(f"/api/materials/alpha/{record['id']}/file")
        self.assertEqual(downloaded.content, raw)
        self.assertIn('attachment', downloaded.headers['content-disposition'])

    def test_attachment_revision_keeps_previous_original(self):
        record = self.material()
        self.knowledge.upload('alpha', record['id'], 'project.txt', b'first original')
        previous, _ = self.knowledge.attachment('alpha', record['id'])
        self.knowledge.upload('alpha', record['id'], 'project.txt', b'second original')
        current, _ = self.knowledge.attachment('alpha', record['id'])
        self.assertNotEqual(previous, current)
        self.assertEqual(previous.read_bytes(), b'first original')
        self.assertEqual(current.read_bytes(), b'second original')

    def test_docx_text_extraction_and_archive_size_limit(self):
        record = self.material()
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>江西项目批复说明</w:t></w:r></w:p></w:document>')
        result = self.knowledge.upload('alpha', record['id'], 'project.docx', raw.getvalue())
        self.assertIn('江西项目批复', result['content'])
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', 'x' * 25_000_001)
        with self.assertRaises(ValueError):
            self.knowledge.upload('alpha', record['id'], 'large.docx', raw.getvalue())

    def test_blank_pdf_reports_ocr_limit(self):
        record = self.material()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        raw = io.BytesIO()
        writer.write(raw)
        result = self.knowledge.upload('alpha', record['id'], 'scan.pdf', raw.getvalue())
        self.assertIn('未做OCR', result['extraction']['note'])

    def test_spatial_attachment_over_one_mb_is_metadata_only(self):
        record = self.material(kind='dataset', content='江西资源图目录，时相与坐标系待核对。')
        raw = b'0' * 1_100_000
        response = self.client.post(f"/api/materials/alpha/{record['id']}/upload", files={'file': ('resource.tif', raw, 'image/tiff')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('江西资源图目录', response.json()['content'])
        self.assertIn('未做OCR', response.json()['extraction']['note'])

    def test_body_limit_checks_actual_body(self):
        response = self.client.post('/api/memories', content=b'x' * 1_000_001, headers={'Content-Type': 'application/json', 'Content-Length': '1'})
        self.assertEqual(response.status_code, 413)
        response = self.client.post('/api/memories', content=b'{}', headers={'Content-Type': 'application/json', 'Content-Length': 'bad'})
        self.assertEqual(response.status_code, 400)

    def test_library_listing_does_not_return_full_body(self):
        self.material()
        records = self.client.get('/api/materials', params={'industry': 'alpha'}).json()
        self.assertNotIn('content', records[0])

    def test_memory_scope_permission_and_soft_archive(self):
        memory = self.knowledge.save_memory({'industry': 'alpha', 'title': '偏好', 'content': '关注江西省'})
        self.assertEqual(self.knowledge.memories('alpha', True), [])
        self.knowledge.save_memory({**memory, 'model_allowed': True})
        self.assertEqual(len(self.knowledge.memories('alpha', True)), 1)
        self.knowledge.archive_memory(memory['id'])
        self.assertEqual(self.knowledge.memories('alpha'), [])
        self.assertTrue(kb.read_json(self.root / 'data/user-memory.json')[0]['archived'])

    def test_memory_rejects_api_secret(self):
        with self.assertRaises(ValueError):
            self.knowledge.save_memory({'title': '密钥', 'content': 'sk-' + 'x' * 32})

    def test_conversation_history_is_opt_in_and_redacts_tokens(self):
        job = {'conversation': 'test-conversation', 'industry': 'alpha'}
        self.knowledge.save_exchange(job, '提问', '回答')
        self.assertEqual(self.knowledge.conversations('alpha'), [])
        self.knowledge.save_settings({'save_conversations': True})
        self.knowledge.save_exchange(job, 'sk-' + 'a' * 32, '回答')
        records = self.knowledge.conversations('alpha')
        self.assertIn('已隐去', records[0]['turns'][0]['prompt'])

    def test_conversation_history_is_separate_when_industry_changes(self):
        ws.industry_command(self.root, self.store.args(action='add', id='beta', name='另一行业', region='中国', categories=None))
        self.knowledge.save_settings({'save_conversations': True})
        self.knowledge.save_exchange({'conversation': 'same-chat', 'industry': 'alpha'}, '行业A提问', 'A回答')
        self.knowledge.save_exchange({'conversation': 'same-chat', 'industry': 'beta'}, '行业B提问', 'B回答')
        self.assertEqual(self.knowledge.conversations('alpha')[0]['turns'][0]['prompt'], '行业A提问')
        self.assertEqual(len(self.knowledge.conversations('alpha')[0]['turns']), 1)
        self.assertEqual(self.knowledge.conversations('beta')[0]['turns'][0]['prompt'], '行业B提问')

    def test_runtime_log_excludes_answer_when_history_is_off(self):
        marker = 'private-answer-only-in-live-response'
        runtime = AgentRuntime(self.store)
        job = {'id': kb.uuid4().hex, 'conversation': kb.uuid4().hex, 'industry': 'alpha',
               'research': False, 'status': 'running', 'events': [], 'started_at': kb.now()}
        model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart(marker)]))
        asyncio.run(runtime.run(job, '提问', model_override=model))
        self.assertEqual(job['status'], 'completed')
        self.assertIn(marker, str(job['events']))
        log = self.root / 'reports/agent-runs' / (job['id'] + '.json')
        self.assertNotIn(marker, log.read_text(encoding='utf-8'))
        self.assertEqual(self.knowledge.conversations('alpha'), [])

    def test_model_retrieves_authorized_professional_excerpt(self):
        self.material(model_allowed=True, region='江西省')
        calls = 0
        def model(messages, info):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(parts=[ToolCallPart('search_materials', {'query': '造林'}, tool_call_id='materials')])
            parts = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            self.assertEqual(parts[-1].content[0]['region'], '江西省')
            self.assertIn('citation', parts[-1].content[0]['snippets'][0])
            return ModelResponse(parts=[TextPart('引用已授权江西资料。')])
        result = asyncio.run(build_agent(FunctionModel(model)).run('查资料', deps=ResearchContext(self.store, 'alpha')))
        self.assertIn('江西', result.output)


if __name__ == '__main__':
    unittest.main()
