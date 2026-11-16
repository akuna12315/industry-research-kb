import hashlib
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from .test_web import Fixture
from .server import create_app
from .knowledge import Knowledge
from .material_index import MaterialIndex
from .service import kb
from .document_parse import parse_native

class DocumentProcessingTests(Fixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts.make_structure_fixtures import make
        scratch=kb.DEFAULT_ROOT/'work/tests';scratch.mkdir(parents=True,exist_ok=True)
        cls.fixture_folder=tempfile.TemporaryDirectory(dir=scratch)
        cls.fixtures=make(Path(cls.fixture_folder.name))
    @classmethod
    def tearDownClass(cls):cls.fixture_folder.cleanup()
    def setUp(self):
        self.setup();self.knowledge=Knowledge(self.store)
        self.client=TestClient(create_app(self.root),headers={'X-KB-Request':'1'}).__enter__()
    def tearDown(self):
        self.client.__exit__(None,None,None);self.temp.cleanup()
    def upload(self,filename,**extra):
        return self.knowledge.upload('alpha','sales',filename,(self.fixtures/filename).read_bytes(),metadata={'id':'sales','title':'合成销售数据','model_allowed':True,**extra})
    def process(self,**options):
        response=self.client.post('/api/materials/alpha/sales/document/process',json={'ocr':False,'vision':False,**options})
        self.assertEqual(response.status_code,200,response.text);job=response.json()
        for _ in range(250):
            job=self.client.get('/api/document-jobs/'+job['id']).json()
            if job['status'] not in ('queued','running'):break
            time.sleep(.02)
        self.assertIn(job['status'],('completed','partial'),job)
        return self.client.get('/api/materials/alpha/sales/document').json(),job
    def test_csv_structure_enters_retrieval_with_real_locator_without_changing_original(self):
        before=self.upload('sales.csv');evidence=self.store.evidence('alpha')
        data,job=self.process();analysis=data['analysis'];table=analysis['tables'][0]
        self.assertEqual(table['headers'][2]['path'],['2024销售额（万元）'])
        self.assertIn('120',str(table));self.assertTrue(table['row_citations'])
        after=self.knowledge.get('alpha','sales')
        self.assertEqual(before['attachment'],after['attachment']);self.assertEqual(evidence,self.store.evidence('alpha'))
        hits=MaterialIndex(self.store).search('alpha','华东 2024销售额')
        self.assertTrue(hits)
        for hit in hits:
            for snippet in hit['snippets']:self.assertTrue(kb.material_quote_matches(self.store.home('alpha'),after,{'quote':snippet['text'],'citation':snippet['citation']}))
        response=self.client.post('/api/materials/alpha/sales/document/calculate',json={'table_id':table['id'],'column':2,'rows':[2,3],'operation':'sum'})
        self.assertEqual(response.json()['value'],'180');self.assertEqual(len(response.json()['input_cell_ids']),2)
        self.assertEqual(self.client.get('/api/materials/alpha/sales/document/export?format=markdown').status_code,200)
        self.knowledge.save('alpha',{'id':'sales','notes':'新版适用范围'})
        self.assertIsNone(self.client.get('/api/materials/alpha/sales/document').json()['analysis'])
    def test_xlsx_merges_and_missing_formula_cache_are_preserved(self):
        self.upload('sales.xlsx');data,_=self.process(header_rows=2)
        table=data['analysis']['tables'][0]
        self.assertEqual(table['headers'][2]['path'],['销售额（万元）','2024年'])
        formula=next(c for c in table['cells'] if c['source'].get('coordinate')=='B5')
        self.assertEqual(formula['text'],'');self.assertFalse(formula['source']['cached_value_available'])
        self.assertEqual(formula['source']['formula'],'=SUM(B3:B4)')
        from .table_structure import row_records
        self.assertTrue(any('公式结果未缓存' in r['text'] for r in row_records(table)))
        self.assertEqual(self.client.get('/api/materials/alpha/sales/file').status_code,200)
    def test_docx_native_parts_survive_and_table_headers_keep_relationships(self):
        before=self.upload('sales.docx');native=[p['text'] for _,p in self.knowledge.index_parts(before)]
        data,_=self.process(header_rows=2);after=self.knowledge.get('alpha','sales')
        parts=[p['text'] for _,p in self.knowledge.index_parts(after)]
        self.assertEqual(parts[:len(native)],native)
        table=data['analysis']['tables'][0]
        self.assertEqual(table['headers'][2]['path'],['销售额（万元）','2024年'])
        self.assertIn('空白',str(table['notes']))
    def test_pdf_bounded_pages_accumulate_without_losing_prior_table(self):
        self.upload('native-table.pdf');data,job=self.process(max_pages=1)
        self.assertEqual(job['status'],'partial');self.assertEqual(data['analysis']['coverage']['pages_covered'],[1])
        data,job=self.process(max_pages=1,start_page=2)
        self.assertEqual(job['status'],'completed');self.assertEqual(data['analysis']['coverage']['pages_covered'],[1,2])
        self.assertEqual(len(data['analysis']['tables']),2)
    def test_unallowed_image_never_sent_to_vision_and_reports_waiting_permission(self):
        self.upload('sales-table.png',model_allowed=False)
        with patch('web.document_processing.LocalVision.analyze',side_effect=AssertionError('must not call')):
            data,job=self.process(vision=True)
        self.assertEqual(job['status'],'partial')
        self.assertEqual(data['analysis']['figures'][0]['vision_status'],'waiting_permission')

    def test_permission_withdrawal_during_vision_prevents_commit(self):
        before=self.upload('sales-chart.png')
        def revoke(*args):
            self.knowledge.save('alpha',{'id':'sales','model_allowed':False})
            return {'kind':'image','summary':'合成说明','tables':[],'observations':[],'limitations':[]}
        with patch('web.document_processing.LocalVision.analyze',side_effect=revoke):
            job=self.client.post('/api/materials/alpha/sales/document/process',json={'ocr':False,'vision':True}).json()
            for _ in range(100):
                job=self.client.get('/api/document-jobs/'+job['id']).json()
                if job['status'] not in ('queued','running'):break
                time.sleep(.02)
        self.assertEqual(job['status'],'failed');self.assertIn('未授权',job['note'])
        after=self.knowledge.get('alpha','sales')
        self.assertFalse(after['model_allowed']);self.assertEqual(before['attachment'],after['attachment'])
        self.assertNotIn('document_analysis',after.get('extraction',{}))

    def test_restart_marks_persisted_running_job_interrupted_without_model_call(self):
        from .document_processing import DocumentProcessing
        job={'id':'document-interrupted','industry_id':'alpha','material_id':'sales','status':'running','created_at':kb.now()}
        kb.atomic_write(self.root/'data/document-jobs/document-interrupted.json',kb.json_text(job))
        runtime=DocumentProcessing(self.store)
        with patch('web.document_processing.LocalVision.analyze',side_effect=AssertionError('must not run')):runtime.restore()
        self.assertEqual(runtime.get(job['id'])['status'],'interrupted');self.assertEqual(runtime.tasks,{})

if __name__=='__main__':unittest.main()
