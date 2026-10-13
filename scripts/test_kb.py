import json
from pathlib import Path
import tempfile
import unittest
import kb


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        scratch = kb.DEFAULT_ROOT / 'work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        self.record = {'id': 'test-policy', 'title': '商业航天政策', 'summary': '卫星与火箭标准调查',
                       'url': 'https://example.org/policy', 'publisher': '测试机构',
                       'region': '中国', 'category': '政策', 'status': 'confirmed',
                       'checked_date': '2026-01-01', 'published_date': '2025-12-01'}

    def tearDown(self):
        self.temp.cleanup()

    def test_idempotent_import_and_chinese_search(self):
        self.assertEqual(kb.import_records(self.root, self.record)['inserted'], 1)
        self.assertEqual(kb.import_records(self.root, self.record)['unchanged'], 1)
        self.assertEqual(kb.status(self.root)['evidence_count'], 1)
        self.assertEqual(kb.search(self.root, '航天 标准')[0]['path'], 'evidence/test-policy.md')
        self.assertEqual(kb.search(self.root, '火箭')[0]['path'], 'evidence/test-policy.md')
        self.assertFalse(kb.search(self.root, '不存在关键词'))
        self.assertFalse(list((self.root / 'history').rglob('*.json')))

    def test_revision_preserves_previous_source(self):
        kb.import_records(self.root, self.record)
        amended = {**self.record, 'summary': '已补充卫星政策口径', 'status': 'reported'}
        self.assertEqual(kb.import_records(self.root, amended)['updated'], 1)
        old = kb.read_json(next((self.root / 'history').rglob('*.json')))
        self.assertEqual(old['summary'], self.record['summary'])
        self.assertEqual(kb.read_json(self.root / 'evidence/test-policy.json')['status'], 'reported')
        self.assertEqual(kb.search(self.root, '已补充')[0]['title'], '商业航天政策')

    def test_invalid_batch_writes_nothing(self):
        for invalid in ({**self.record, 'id': '../escape'}, {**self.record, 'url': 'file:///secret'},
                        {**self.record, 'checked_date': '2026-02-30'},
                        {**self.record, 'status': 'fabricated'},
                        {**self.record, 'event_date': '2027-01-01'},
                        {**self.record, 'metrics': [{'name': '市场规模', 'value': 10}]},
                        {**self.record, 'published_date': '2026-01-02'}):
            with self.assertRaises(ValueError):
                kb.import_records(self.root, [self.record, invalid])
            self.assertFalse((self.root / 'evidence').exists())

    def test_concurrent_write_lock_is_respected(self):
        with kb.lock(self.root):
            with self.assertRaises(ValueError):
                kb.import_records(self.root, self.record)
        self.assertFalse((self.root / 'data/write.lock').exists())

    def test_manual_topic_reindex_and_literal_query(self):
        topic = self.root / 'topics/example.md'
        kb.atomic_write(topic, '# 市场口径\n卫星市场的百分比 10% 不等于营收。\n')
        kb.reindex(self.root)
        self.assertEqual(len(kb.search(self.root, '10%')), 1)
        self.assertFalse(kb.search(self.root, '%不存在'))


if __name__ == '__main__':
    unittest.main()
