import io
import json
from pathlib import Path
import unittest
import zipfile
from fastapi.testclient import TestClient
from .test_web import Fixture
from .knowledge import Knowledge
from .server import create_app
from .docx_index import extract_docx

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def document(body,extra=None):
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml',f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        for name,value in (extra or {}).items():
            archive.writestr(name,value)
    return raw.getvalue()


class DocxIndexTests(Fixture,unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge=Knowledge(self.store)
        self.client=TestClient(create_app(self.root),headers={'X-KB-Request':'1'})
        self.material=self.knowledge.save('alpha',{'id':'large-project','title':'大型项目文档','kind':'project'})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def ingest(self,body,extra=None):
        source=self.root/'work/project.docx'
        source.parent.mkdir(parents=True,exist_ok=True)
        source.write_bytes(document(body,extra))
        return self.knowledge.import_docx('alpha','large-project',source)

    def test_large_body_tail_is_searchable_without_replacing_full_source(self):
        r=self.ingest('<w:p><w:r><w:t>'+('普通内容'*55000)+'</w:t></w:r></w:p><w:p><w:r><w:t>末尾特有林场标记</w:t></w:r></w:p>')
        self.assertGreater(r['extraction']['characters'],200000)
        self.assertLess(len(r['content']),200000)
        self.assertEqual(len(self.knowledge.list('alpha','末尾特有林场标记')),1)
        parts=self.client.get('/api/materials/alpha/large-project/sections',params={'q':'末尾特有林场标记'}).json()
        self.assertGreater(parts['total'],0)
        self.assertIn('末尾特有林场标记',parts['items'][0]['text'])
        source,_=self.knowledge.attachment('alpha','large-project')
        self.assertEqual(source.read_bytes(),(self.root/'work/project.docx').read_bytes())

    def test_private_large_doc_is_not_returned_to_model(self):
        self.ingest('<w:p><w:r><w:t>私有造林计划</w:t></w:r></w:p>')
        self.assertEqual(self.knowledge.search('alpha','造林'),[])
        self.knowledge.save('alpha',{'id':'large-project','model_allowed':True})
        hit=self.knowledge.search('alpha','造林')[0]
        self.assertIn('#block-',hit['snippets'][0]['citation'])
        self.assertIn('私有造林计划',hit['snippets'][0]['text'])

    def test_tables_preserve_cell_locations_and_dont_break_words_at_runs(self):
        r=self.ingest('<w:p><w:r><w:t>前文</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>人工</w:t></w:r><w:r><w:t>造林</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>一号地块</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        self.assertEqual(r['extraction']['tables'],1)
        text=self.knowledge.sections('alpha','large-project')['items'][0]['text']
        self.assertIn('人工造林',text)
        self.assertIn('表1·行1·单元格2',text)

    def test_media_does_not_trigger_small_legacy_uncompressed_limit(self):
        r=self.ingest('<w:p><w:r><w:t>正文</w:t></w:r></w:p>',{'word/media/image.png':b'x'*26_000_000})
        self.assertEqual(r['extraction']['images'],1)

    def test_entity_declarations_rejected(self):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            z.writestr('word/document.xml','<!DOCTYPE foo [<!ENTITY x "expanded">]><foo>&x;</foo>')
        with self.assertRaises(ValueError):
            extract_docx(io.BytesIO(raw.getvalue()))

    def test_note_and_cached_page_count_are_not_claimed_as_rendered_pages(self):
        r=self.ingest('<w:p><w:r><w:t>正文</w:t></w:r></w:p>',{
            'word/footnotes.xml':f'<w:footnotes xmlns:w="{W}"><w:p><w:r><w:t>脚注中的补充资料</w:t></w:r></w:p></w:footnotes>',
            'docProps/app.xml':'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Pages>1110</Pages></Properties>'})
        self.assertEqual(r['extraction']['cached_page_count'],'1110')
        self.assertIn('未经渲染核对',r['extraction']['note'])
        self.assertGreater(self.knowledge.sections('alpha','large-project','脚注中的补充资料')['total'],0)

    def test_metadata_can_be_edited_without_discarding_index(self):
        self.ingest('<w:p><w:r><w:t>全文独有词</w:t></w:r></w:p>')
        self.knowledge.save('alpha',{'id':'large-project','notes':'仅改元数据'})
        self.assertEqual(self.knowledge.sections('alpha','large-project','全文独有词')['total'],1)
        self.assertEqual(self.client.get('/api/materials/alpha/large-project/sections?offset=-1').status_code,400)

    def test_second_attachment_preserves_previous_index_and_original(self):
        first=self.ingest('<w:p><w:r><w:t>旧正文</w:t></w:r></w:p>')
        second=self.ingest('<w:p><w:r><w:t>新正文</w:t></w:r></w:p>')
        self.assertNotEqual(first['extraction']['text_index'],second['extraction']['text_index'])
        self.assertTrue((self.knowledge.folder('alpha')/first['extraction']['text_index']).exists())
        self.assertTrue((self.knowledge.folder('alpha')/'files'/first['attachment']['storage_name']).exists())
