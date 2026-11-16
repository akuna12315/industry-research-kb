import unittest

from .table_structure import from_csv,from_html,table_from_cells,row_records,calculate,markdown


class TableStructureTests(unittest.TestCase):
    def test_csv_values_keep_year_and_unit_context_and_blank_is_not_zero(self):
        table=from_csv('地区,2023销售额（万元）,2024销售额（万元）\n华东,100,120\n华南,,80',caption='销售表')[0]
        records=list(row_records(table))
        self.assertIn('2024销售额（万元）：120',records[0]['text'])
        self.assertIn('单位提示：万元',records[0]['text'])
        self.assertIn('空白（不等于0）',records[1]['text'])
        self.assertIn('华东',markdown(table))

    def test_multilevel_headers_and_merged_row_labels_survive(self):
        html='<table><caption>季度销售（万元）</caption><thead><tr><th rowspan="2">地区</th><th colspan="2">2024</th></tr><tr><th>一季度</th><th>二季度</th></tr></thead><tbody><tr><th scope="row">华东</th><td>10</td><td>20</td></tr></tbody></table>'
        table=from_html(html)[0]
        self.assertEqual(table['header_rows'],2)
        self.assertEqual(table['headers'][2]['path'],['2024','二季度'])
        self.assertIn('2024 / 二季度：20',list(row_records(table))[0]['text'])

    def test_explicit_calculation_requires_actual_numeric_values_and_distinct_cells(self):
        table=from_csv('地区,销量（台）\n甲,10\n乙,20\n丙,')[0]
        result=calculate(table,2,[2,3],'sum')
        self.assertEqual(result['value'],'30')
        self.assertEqual(result['unit'],'台')
        self.assertEqual(len(result['input_cell_ids']),2)
        for rows in ([2,2],[2,4],[1,2]):
            with self.assertRaises(ValueError):calculate(table,2,rows,'sum')

    def test_overlapping_cells_are_not_silently_repaired(self):
        with self.assertRaises(ValueError):table_from_cells([
            {'row':1,'column':1,'row_span':2,'text':'甲'}, {'row':2,'column':1,'text':'乙'}])

    def test_units_from_separate_column_must_match_before_calculation(self):
        table=from_csv('地区,数值,单位\n甲,10,万元\n乙,20,亿元')[0]
        with self.assertRaises(ValueError):calculate(table,2,[2,3],'sum')
        table=from_csv('地区,数值,单位\n甲,10,万元\n乙,20,万元')[0]
        result=calculate(table,2,[2,3],'sum')
        self.assertEqual(result['unit'],'万元');self.assertEqual(result['value'],'30')


if __name__=='__main__':unittest.main()
