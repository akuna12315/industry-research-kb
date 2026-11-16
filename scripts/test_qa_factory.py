import json
from pathlib import Path
import tempfile
import unittest

from qa_factory import validate_item, connect, progress, export, parse_response, restore_individual_answers,set_control


class QAFactoryTests(unittest.TestCase):
    def setUp(self):
        self.context = {'task_id':'reference-1','kind':'existing_reference','question_id':'1',
            'question':'示例设备保修期限是多久？', 'sources':[{'source_id':'S1','doc_id':'d1',
            'text':'示例设备的保修期限为12个月，耗材不在保修范围内。','start':20,'stop':48,'source_sha256':'fixture'}]}
        self.item = {'task_id':'reference-1','question':self.context['question'],'answer':'保修期限为12个月。',
                     'answerable':True,'supports':[{'source_id':'S1','quote':'示例设备的保修期限为12个月'}]}

    def test_valid_quote_gets_real_offsets_and_unreviewed_status(self):
        result = validate_item(self.item,self.context)
        self.assertEqual(result['supports'][0]['start'],20)
        self.assertEqual(result['supports'][0]['stop'],20+len(self.item['supports'][0]['quote']))
        self.assertEqual(result['quality_status'],'unreviewed_model_draft')

    def test_changed_quote_and_unsupported_numbers_rejected(self):
        self.item['supports'][0]['quote']='示例设备的保修期限为24个月'
        with self.assertRaises(ValueError):validate_item(self.item,self.context)
        self.item['supports'][0]['quote']='示例设备的保修期限为12个月'
        self.item['answer']='保修期限为24个月。'
        with self.assertRaises(ValueError):validate_item(self.item,self.context)

    def test_original_question_cannot_be_rewritten(self):
        self.item['question']='任意其他问题是什么？'
        with self.assertRaises(ValueError):validate_item(self.item,self.context)

    def test_insufficient_excerpt_is_not_marked_as_global_unanswerable_gold(self):
        self.item.update(answerable=False,answer='本次片段没有提供该项具体数值。',supports=[])
        result=validate_item(self.item,self.context)
        self.assertFalse(result['answerable_from_provided_excerpts'])
        self.assertIn('not a full-corpus absence',result['coverage'])

    def test_committed_qa_survives_reopen_and_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            db=connect(folder)
            record=validate_item(self.item,self.context)
            with db:db.execute('INSERT INTO qa VALUES(?,?,?,?,?,?)',(record['id'],record['task_id'],record['kind'],record['question_key'],json.dumps(record),record['created_at']))
            db.close()
            db=connect(folder)
            result=export(db,folder)
            self.assertEqual(result['qa_saved'],1)
            self.assertEqual(json.loads((folder/'qa-drafts.jsonl').read_text())['id'],record['id'])
            self.assertEqual(restore_individual_answers(db,folder),1)
            self.assertEqual(restore_individual_answers(db,folder),0)
            self.assertEqual(json.loads((folder/'answers'/(record['id']+'.json')).read_text(encoding='utf-8'))['id'],record['id'])
            db.close()

    def test_invalid_json_is_not_silently_accepted(self):
        with self.assertRaises(ValueError):parse_response('{"items":[')
        self.assertEqual(parse_response('```json\n{"items":[]}\n```'),[])

    def test_source_span_id_generates_original_quote_and_rejects_unknown_id(self):
        self.item['supports']=[{'source_id':'S1','span_id':'S1-S1'}]
        result=validate_item(self.item,self.context)
        self.assertEqual(result['supports'][0]['quote'],self.context['sources'][0]['text'])
        self.assertEqual(result['supports'][0]['mapping'],'source_span_id')
        self.item['supports'][0]['span_id']='invented'
        with self.assertRaises(ValueError):validate_item(self.item,self.context)

    def test_missing_worker_is_interrupted_without_advancing_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            db=connect(Path(temp))
            with db:
                set_control(db,'state','running');set_control(db,'worker',{'pid':2147483000})
                set_control(db,'heartbeat_at','2026-11-12T19:52:42+08:00')
            result=progress(db,Path(temp));db.close()
        self.assertEqual(result['state'],'interrupted');self.assertFalse(result['process_alive'])
        self.assertEqual(result['updated_at'],'2026-11-12T19:52:42+08:00')

    def test_numeric_sign_is_not_discarded(self):
        self.context['sources'][0]['text']='设备的正常工作温度为-20摄氏度。'
        self.item['supports']=[{'source_id':'S1','quote':'设备的正常工作温度为-20摄氏度。'}]
        self.item['answer']='工作温度为20摄氏度。'
        with self.assertRaises(ValueError):validate_item(self.item,self.context)


if __name__=='__main__':unittest.main()
