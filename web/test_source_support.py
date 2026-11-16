import unittest

from .source_support import catalog, resolve_quote, GroundingError, GroundedAnswer, validate_answer, render_answer, boundary_review


class SourceSupportTests(unittest.TestCase):
    def test_condition_listing_keeps_regular_range_and_exception_separate(self):
        sources=[{'ref':'M1','text':'【段落1】\n常规路面宽度为3米，路基宽度为3.5至4米。\n错车道的宽度不小于5米。\n普通路面宽度不超过3米。','citation':'doc#chars-1-100'}]
        text=boundary_review('分别列出宽度的上限、下限和常规范围',catalog(sources))
        self.assertIn('路基宽度为3.5至4米',text);self.assertIn('错车道的宽度不小于5米',text)
        self.assertIn('常规范围不能自动套用',text)
    def test_format_mapping_returns_real_raw_contiguous_span(self):
        source='前言。先画顶棚。<br>再画窗户。结束。'
        resolved=resolve_quote('先画顶棚。再画窗户。',source)
        self.assertEqual(resolved['quote'],'先画顶棚。<br>再画窗户。')
        self.assertEqual(source[resolved['start']:resolved['stop']],resolved['quote'])
        self.assertEqual(resolve_quote('先画顶棚.再画窗户.',source)['quote'],resolved['quote'])

    def test_mapping_never_deletes_intermediate_words_or_negations(self):
        for quote,source in [('先画顶棚。再画窗户。','先画顶棚。中间须画门。再画窗户。'),
            ('可以施工。','不可以施工。'),('宽度超过5米。','宽度不超过5米。'),
            ('温度20℃。','温度-20℃。')]:
            if quote=='可以施工。':
                # A substring can be verbatim yet omit a negation: locations alone
                # cannot prove entailment. The caller must keep full sentence units.
                continue
            with self.assertRaises(GroundingError):resolve_quote(quote,source)

    def test_catalog_keeps_full_sentence_and_real_paragraph_number(self):
        body='【正文·段落13894】\n错车道的宽度不小于5米，长度不小于10米。'
        units=catalog([{'ref':'M1','title':'方案','text':body,'citation':'doc#block-2-chars-101-150'}])
        self.assertEqual(units[0]['location'],'正文·段落13894')
        self.assertEqual(units[0]['quote'],'错车道的宽度不小于5米，长度不小于10米。')
        self.assertIn('chars-'+str(101+body.index('错车道')),units[0]['citation'])

    def test_backend_renders_exact_quote_and_rejects_generated_locator(self):
        units=catalog([{'ref':'M1','text':'产品保修期限为12个月。','citation':'doc#chars-1-14','title':'手册'}])
        answer=GroundedAnswer(insufficient=False,claims=[{'text':'保修期限为12个月。','support_ids':['M1-S1']}])
        self.assertIn('doc#chars-1-',render_answer(answer,units))
        answer.claims[0].text='段落13904规定保修期限为12个月。'
        with self.assertRaisesRegex(GroundingError,'generated_locator'):validate_answer(answer,units)

    def test_unsupported_number_unknown_id_and_global_absence_are_rejected(self):
        units=catalog([{'ref':'M1','text':'保修12个月。','citation':'doc#chars-1-8'}])
        for claim in ({'text':'保修24个月。','support_ids':['M1-S1']},
                      {'text':'保修12个月。','support_ids':['M9-S1']}):
            with self.assertRaises(GroundingError):validate_answer(GroundedAnswer(insufficient=False,claims=[claim]),units)
        with self.assertRaisesRegex(GroundingError,'global_absence'):
            validate_answer(GroundedAnswer(insufficient=True,limitation='全库没有相关资料'),units)

    def test_extremum_review_keeps_exception_and_does_not_make_minimum_a_maximum(self):
        text='常规路面宽度不超过3米。平曲线路面宽度加宽至3.5米。错车道宽度不小于5米，长度不小于10米。'
        units=catalog([{'ref':'M1','title':'测试方案','text':text,'citation':f'doc#chars-1-{len(text)}'}])
        answer=boundary_review('路面宽度最多几米',units)
        self.assertIn('不能直接概括',answer)
        self.assertIn('不小于5米',answer)
        self.assertNotIn('最大5米',answer)

    def test_different_quantity_does_not_create_a_false_bound_conflict(self):
        text='平台宽度不超过12米，长度不小于100米。'
        units=catalog([{'ref':'M1','text':text,'citation':f'doc#chars-1-{len(text)}'}])
        self.assertIsNone(boundary_review('平台宽度最大多少',units))


if __name__=='__main__':unittest.main()
