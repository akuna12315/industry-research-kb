"""Full-source coverage and citation integrity, without a model or real data."""
import io
import copy
import hashlib
import json
from pathlib import Path
import unittest
import zipfile

from .docx_index import extract_docx, index_records
from .extraction_source import build_plan, split_hydrated_batch, MAX_CHARACTERS
from .knowledge import Knowledge
from .service import kb
from .test_web import Fixture

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def paragraph(text, style=''):
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ''
    return f'<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>'


def document(body, extra=None):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return raw.getvalue()


def cell(text, props=''):
    return f'<w:tc><w:tcPr>{props}</w:tcPr>{paragraph(text)}</w:tc>'


class ExtractionSourceTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.knowledge.save('alpha', {'id': 'full-project', 'title': '全文测试项目', 'kind': 'project'})

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, body, extra=None):
        source = self.root / 'work/project.docx'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(document(body, extra))
        return self.knowledge.import_docx('alpha', 'full-project', source)

    def plan(self):
        return build_plan(self.store, 'alpha', 'full-project')

    def assert_citations(self, plan):
        material = self.knowledge.get('alpha', 'full-project')
        bodies = {number + 1: part['text'] for number, part in self.knowledge.index_parts(material)}
        seen = []
        for batch in plan['batches']:
            self.assertEqual(batch['characters'], sum(len(item['text']) for item in batch['sources']))
            self.assertLessEqual(batch['characters'], MAX_CHARACTERS)
            for item in batch['sources'] + batch['context']:
                source = {'citation': item['citation'], 'quote': item['text']}
                self.assertTrue(kb.material_quote_matches(self.store.home('alpha'), material, source), item)
                if 'block' in item:
                    body = bodies[item['block']] if item['block'] else material['content'] or material['summary']
                    self.assertEqual(body[item['start']:item['end']], item['text'])
            seen.extend(batch['unit_ids'])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(plan['total_units'], len(seen))

    def test_all_middle_and_tail_text_is_covered_once_with_exact_citations(self):
        texts = [f'第{number}段：' + ('完整正文内容。' * 145) for number in range(47)]
        texts[21] += '中部独有事实'
        texts[-1] += '最后附录结尾事实'
        material = self.ingest(''.join(paragraph(text) for text in texts))
        plan = self.plan()
        all_text = ''.join(item['text'] for batch in plan['batches'] for item in batch['sources'])
        self.assertEqual(all_text, ''.join(texts))
        self.assertEqual(plan['total_characters'], material['extraction']['characters'])
        self.assertGreater(len(plan['batches']), 6)
        self.assert_citations(plan)

    def test_storage_block_boundaries_do_not_drop_long_paragraph_characters(self):
        text = '段首' + ('常规字' * 5400) + '段尾关键数值99公顷'
        self.ingest(paragraph(text))
        plan = self.plan()
        self.assertEqual(''.join(item['text'] for batch in plan['batches'] for item in batch['sources']), text)
        self.assertEqual(plan['total_characters'], len(text))
        self.assert_citations(plan)

    def test_legacy_index_is_reparsed_without_rewriting_any_material_or_part(self):
        self.ingest(paragraph('旧索引内容' * 1500))
        material = self.knowledge.get('alpha', 'full-project')
        index_path = self.knowledge.folder('alpha') / material['extraction']['text_index']
        for file in index_path.parent.glob('*.json'):
            value = kb.read_json(file)
            value.pop('units', None)
            value.pop('structure_version', None)
            file.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        before = {file: file.read_bytes() for file in self.knowledge.folder('alpha').rglob('*') if file.is_file()}
        first = self.plan()
        second = self.plan()
        self.assertEqual(first['source_manifest_hash'], second['source_manifest_hash'])
        self.assertEqual([b['id'] for b in first['batches']], [b['id'] for b in second['batches']])
        self.assertEqual(before, {file: file.read_bytes() for file in before})
        self.assert_citations(first)

    def test_table_headers_units_notes_and_merged_geometry_are_available(self):
        body = paragraph('表1 造林面积') + paragraph('单位：公顷')
        body += '<w:tbl><w:tblGrid><w:gridCol w:w="100"/><w:gridCol w:w="100"/><w:gridCol w:w="100"/></w:tblGrid>'
        body += '<w:tr><w:trPr><w:tblHeader/></w:trPr>' + cell('区域', '<w:vMerge w:val="restart"/>') + cell('计划规模', '<w:gridSpan w:val="2"/>') + '</w:tr>'
        body += '<w:tr><w:trPr><w:tblHeader/></w:trPr>' + cell('', '<w:vMerge/>') + cell('年度') + cell('面积') + '</w:tr>'
        for number in range(120):
            body += '<w:tr>' + cell(f'地块{number}') + cell('2026') + cell(str(number * 10)) + '</w:tr>'
        body += '</w:tbl>' + paragraph('注：面积为规划值，不代表验收实绩。')
        self.ingest(body)
        plan = self.plan()
        self.assertEqual(plan['table_count'], 1)
        shape = plan['batches'][0]['table_structure'][0]
        self.assertEqual(len(shape['grid']), 3)
        first_row = shape['rows'][0]
        self.assertEqual(first_row['cells'][1]['column_span'], 2)
        self.assertEqual(first_row['cells'][0]['vertical_merge'], 'restart')
        self.assertEqual(shape['rows'][1]['cells'][0]['vertical_merge'], 'continue')
        # Empty continuation cells retain their geometry even though no source
        # text is invented for them.
        self.assertEqual(shape['rows'][1]['cells'][1]['column'], 2)
        joined = '\n'.join(item['text'] for batch in plan['batches'] for item in batch['sources'] + batch['context'])
        self.assertIn('地块119', joined)
        self.assertIn('单位：公顷', joined)
        self.assertIn('规划值', joined)
        self.assert_citations(plan)

    def test_long_table_rows_stay_together_and_later_batches_repeat_headers(self):
        body = '<w:tbl><w:tr><w:trPr><w:tblHeader/></w:trPr>' + cell('地块名称') + cell('规定及单位') + '</w:tr>'
        for number in range(30):
            body += '<w:tr>' + cell(f'完整行{number}') + cell('措施' * 300) + '</w:tr>'
        self.ingest(body + '</w:tbl>')
        plan = self.plan()
        self.assertGreater(len(plan['batches']), 2)
        row_batch = {}
        for number, batch in enumerate(plan['batches']):
            for item in batch['sources']:
                if item.get('table_row', 1) > 1:
                    key = (item['table_id'], item['table_row'])
                    self.assertEqual(row_batch.setdefault(key, number), number)
            if number:
                self.assertIn('地块名称', [item['text'] for item in batch['context']])
        self.assert_citations(plan)

    def test_oversize_cell_is_split_without_losing_text_or_row_key(self):
        long_text = '超长表格说明' * 2600
        body = '<w:tbl><w:tr>' + cell('对象') + cell('要求') + '</w:tr><w:tr>' + cell('末行地块') + cell(long_text) + '</w:tr></w:tbl>'
        self.ingest(body)
        plan = self.plan()
        sources = [item for batch in plan['batches'] for item in batch['sources']]
        self.assertEqual(''.join(item['text'] for item in sources), '对象要求末行地块' + long_text)
        for batch in plan['batches'][1:]:
            self.assertIn('末行地块', [item['text'] for item in batch['context']])
        self.assert_citations(plan)

    def test_all_heading_levels_and_last_heading_are_retained_as_context(self):
        styles = f'<w:styles xmlns:w="{W}"><w:style w:styleId="h4"><w:name w:val="Heading 4"/></w:style></w:styles>'
        body = ''.join(paragraph(f'第{n}节', 'h4') + paragraph('正文内容' * 1400) for n in range(110))
        self.ingest(body, {'word/styles.xml': styles})
        plan = self.plan()
        last = plan['batches'][-1]['sources'][-1]
        self.assertEqual(last['heading_path'][-1]['text'], '第109节')
        self.assertEqual(last['heading_path'][-1]['level'], 4)
        self.assert_citations(plan)

    def test_footnote_and_footer_text_are_not_skipped_and_images_are_unprocessed(self):
        notes = f'<w:footnotes xmlns:w="{W}">{paragraph("脚注限制条件")}</w:footnotes>'
        footer = f'<w:ftr xmlns:w="{W}">{paragraph("页脚版权及时间")}</w:ftr>'
        self.ingest(paragraph('正文'), {'word/footnotes.xml': notes, 'word/footer1.xml': footer, 'word/media/image.png': b'not decoded'})
        plan = self.plan()
        self.assertEqual(plan['image_count'], 1)
        self.assertTrue(any('未做OCR' in limitation for limitation in plan['limitations']))
        self.assertIn('脚注限制条件', ''.join(item['text'] for batch in plan['batches'] for item in batch['sources']))
        self.assert_citations(plan)

    def test_changed_attachment_or_index_rejects_false_complete_coverage(self):
        self.ingest(paragraph('必须一致的原文'))
        path, _ = self.knowledge.attachment('alpha', 'full-project')
        original = path.read_bytes()
        path.write_bytes(document(paragraph('不同的原文')))
        with self.assertRaisesRegex(ValueError, '指纹'):
            self.plan()
        path.write_bytes(original)
        record = self.knowledge.get('alpha', 'full-project')
        first = self.knowledge.folder('alpha') / record['extraction']['text_index']
        value = kb.read_json(first.parent / '00001.json')
        value['text'] = value['text'].replace('原文', '错字')
        (first.parent / '00001.json').write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '逐字对齐'):
            self.plan()

    def test_non_indexed_text_preserves_every_character_and_splits_large_unit(self):
        text = '第一段。\n\n' + ('长正文' * 9000) + '\n\n末尾附录。\n'
        self.knowledge.save('alpha', {'id': 'full-project', 'content': text})
        plan = self.plan()
        # Knowledge normalizes outer whitespace before storing content.
        expected = self.knowledge.get('alpha', 'full-project')['content']
        self.assertEqual(''.join(item['text'] for batch in plan['batches'] for item in batch['sources']), expected)
        self.assertEqual(plan['total_characters'], len(expected))
        self.assert_citations(plan)

    def test_empty_material_does_not_create_a_successful_zero_batch_plan(self):
        with self.assertRaisesRegex(ValueError, '没有可提取'):
            self.plan()

    def test_index_text_output_is_compatible_with_legacy_joining(self):
        blocks, _ = extract_docx(io.BytesIO(document(paragraph('普通内容' * 2000) + paragraph('尾部'))))
        records = index_records(blocks)
        self.assertTrue(records)
        for record in records:
            for span in record['units']:
                self.assertNotIn('【正文', record['text'][span['start']:span['end']])

    def test_split_preserves_every_source_character_and_does_not_mutate_parent(self):
        self.ingest(''.join(paragraph(f'段{number}。' + '原文措施。' * 95) for number in range(35)))
        original = self.plan()['batches'][0]
        before = copy.deepcopy(original)
        children = split_hydrated_batch(original)
        self.assertEqual(original, before)
        self.assertEqual(''.join(item['text'] for child in children for item in child['sources']),
                         ''.join(item['text'] for item in original['sources']))
        self.assertEqual(sum(child['characters'] for child in children), original['characters'])
        self.assertTrue(all(0 < child['characters'] < original['characters'] for child in children))
        for child in children:
            self.assertLessEqual(child['context_characters'], 8000)
            self.assertLessEqual(len(json.dumps(child['table_structure'], ensure_ascii=False)), 64000)
        self.assert_citations({**self.plan(), 'batches': children, 'total_units': sum(len(c['sources']) for c in children)})

    def test_split_moves_original_headers_and_notes_to_needed_child_context(self):
        body = paragraph('表1 建设措施') + paragraph('单位：公顷')
        body += '<w:tbl><w:tr><w:trPr><w:tblHeader/></w:trPr>' + cell('项目对象') + cell('限制及规模') + '</w:tr>'
        for number in range(35):
            body += '<w:tr>' + cell(f'地块{number}') + cell('具体技术要求。' * 35) + '</w:tr>'
        self.ingest(body + '</w:tbl>' + paragraph('注：均为规划要求。'))
        plan = self.plan()
        parent = plan['batches'][0]
        left, right = split_hydrated_batch(parent)
        right_text = [item['text'] for item in right['context']]
        self.assertIn('项目对象', right_text)
        self.assertIn('限制及规模', right_text)
        self.assertIn('单位：公顷', right_text)
        self.assertNotEqual(left['table_structure'][0]['rows'], right['table_structure'][0]['rows'])
        self.assert_citations({**plan, 'batches': [left, right], 'total_units': len(left['sources']) + len(right['sources'])})

    def test_split_single_range_updates_exact_citations_hashes_and_unit_ids(self):
        text = '完整原文' * 1500
        source = {'citation': f'full-project#block-3-chars-101-{100 + len(text)}',
                  'text': text, 'text_hash': 'obsolete', 'unit_id': 'original-unit'}
        parent = {'id': 'parent', 'sources': [source], 'context': [], 'characters': len(text)}
        left, right = split_hydrated_batch(parent)
        self.assertEqual(left['sources'][0]['start'], 100)
        self.assertEqual(left['sources'][0]['end'], right['sources'][0]['start'])
        self.assertEqual(right['sources'][0]['end'], 100 + len(text))
        self.assertEqual(left['sources'][0]['text'] + right['sources'][0]['text'], text)
        self.assertNotEqual(left['unit_ids'], right['unit_ids'])
        for child in (left, right):
            item = child['sources'][0]
            self.assertEqual(item['text_hash'], hashlib.sha256(kb.json_text(item['text']).encode()).hexdigest())

    def test_split_discards_unrelated_table_context_and_geometry(self):
        def item(first, text, table, row, header=False):
            return {'citation': f'full-project#chars-{first}-{first + len(text) - 1}', 'text': text,
                    'table_id': table, 'table_row': row, 'column': 1, 'table_cell': 1, 'table_header': header}
        sources = [item(1, '甲表事实', 'table-a', 999), item(50, '乙表事实', 'table-b', 999)]
        context = [dict(item(100, '甲表头' * 1200, 'table-a', 1, True), context_reason='表头原文'),
                   dict(item(4000, '乙表头' * 1200, 'table-b', 1, True), context_reason='表头原文')]
        geometry = [{'id': table, 'grid': ['100'], 'rows': [{'row': row, 'header': row == 1,
                     'cells': [{'cell': 1, 'column': 1, 'column_span': 1}]} for row in range(1, 1000)]}
                    for table in ('table-a', 'table-b')]
        left, right = split_hydrated_batch({'sources': sources, 'context': context, 'table_structure': geometry, 'characters': 8})
        self.assertEqual(left['table_ids'], ['table-a'])
        self.assertEqual(right['table_ids'], ['table-b'])
        self.assertEqual(left['context_characters'], 3600)
        self.assertEqual(right['context_characters'], 3600)
        self.assertEqual(len(left['table_structure'][0]['rows']), 2)
        self.assertEqual(len(right['table_structure'][0]['rows']), 2)
        self.assertTrue(all(item['table_id'] == 'table-a' for item in left['context'] if item.get('table_id')))

    def test_split_rejects_oversize_necessary_context_or_geometry_without_truncation(self):
        source = {'citation': 'full-project#chars-1-1000', 'text': '正' * 1000}
        context = {'citation': 'full-project#chars-2001-11000', 'text': '定义' * 4500, 'context_reason': '跨章节定义'}
        parent = {'sources': [source], 'context': [context], 'characters': 1000}
        with self.assertRaisesRegex(ValueError, '必要表头.*8000'):
            split_hydrated_batch(parent)
        table_source = dict(source, table_id='table-a', table_row=1, column=1)
        shape = {'id': 'table-a', 'grid': ['1'], 'rows': [{'row': 1, 'cells': [
            {'cell': number, 'column': number, 'column_span': 1} for number in range(1500)]}]}
        with self.assertRaisesRegex(ValueError, '64000'):
            split_hydrated_batch({'sources': [table_source], 'context': [], 'table_structure': [shape], 'characters': 1000})

    def test_split_rejects_overlap_invalid_lengths_and_unsplittable_single_character(self):
        first = {'citation': 'full-project#chars-1-5', 'text': '第一范围内'}
        second = {'citation': 'full-project#chars-4-8', 'text': '重叠范围内'}
        with self.assertRaisesRegex(ValueError, '重叠'):
            split_hydrated_batch({'sources': [first, second], 'characters': 10})
        with self.assertRaisesRegex(ValueError, '长度'):
            split_hydrated_batch({'sources': [dict(first, text='不匹配')], 'characters': 3})
        with self.assertRaisesRegex(ValueError, '无法继续缩小'):
            split_hydrated_batch({'sources': [{'citation': 'full-project#chars-1-1', 'text': '单'}], 'characters': 1})

    def test_recursively_split_header_deduplicates_context_subranges(self):
        source = {'citation': 'full-project#chars-1-6000', 'text': '表头' * 3000,
                  'table_id': 'table-a', 'table_row': 1, 'table_header': True, 'column': 1}
        batch = {'sources': [source], 'context': [], 'characters': 6000}
        for _ in range(3):
            batch = split_hydrated_batch(batch)[1]
            self.assertEqual(batch['characters'] + batch['context_characters'], 6000)
            contexts = sorted((item['start'], item['end']) for item in batch['context'])
            self.assertTrue(all(a[1] <= b[0] for a, b in zip(contexts, contexts[1:])))


if __name__ == '__main__':
    unittest.main()
