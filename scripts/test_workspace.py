import argparse
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

import kb
import workspace as ws


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        scratch = kb.DEFAULT_ROOT / 'work/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name)
        kb.atomic_write(self.root / 'config/industries.json', kb.json_text({
            'version': 1, 'active_industry': None, 'industries': []}))
        self.today = kb.datetime.now(kb.TZ).date()
        self.record = {'id': 'same-fact', 'title': '企业合同', 'summary': '企业已签订单，统计范围需要核对',
                       'url': 'https://example.org/order', 'publisher': '企业', 'region': '中国',
                       'category': '企业与竞争', 'status': 'reported', 'checked_date': self.today.isoformat()}

    def tearDown(self):
        self.temp.cleanup()

    def add(self, key, name):
        ws.industry_command(self.root, argparse.Namespace(action='add', id=key, name=name,
                                                        region='中国', categories=None))
        return ws.selected(self.root, key)[0]

    def filters(self, **kwargs):
        return argparse.Namespace(query='合同', limit=10, category=None, tag=None, publisher=None,
                                  source_id=None, status=None, freshness=None, **kwargs)

    def run_result(self, **kwargs):
        data = {'status': 'success', 'run_date': self.today.isoformat(),
                'coverage_from': (self.today - timedelta(days=6)).isoformat(),
                'coverage_through': self.today.isoformat(), 'queries': ['行业 公告'],
                'sources_accessed': ['https://example.org/order'], 'sources_failed': [],
                'inserted_ids': [], 'updated_ids': [], 'unchanged_ids': [],
                'uncovered_ranges': [], 'open_questions': [], 'next_focus': '政策变化'}
        data.update(kwargs)
        return data

    def test_no_active_industry_and_disabled_profiles(self):
        entry = self.add('alpha', '甲行业')
        with self.assertRaises(ValueError):
            ws.selected(self.root)
        self.assertEqual(ws.update_plan(self.root)['industries'], [])
        ws.industry_command(self.root, argparse.Namespace(action='use', id='alpha'))
        self.assertEqual(ws.selected(self.root)[0]['id'], 'alpha')
        self.assertEqual(ws.update_plan(self.root)['industries'], [])
        ws.industry_command(self.root, argparse.Namespace(action='enable', id='alpha'))
        self.assertEqual(ws.update_plan(self.root)['industries'][0]['industry_id'], 'alpha')

    def test_industry_isolation_and_cross_industry_search(self):
        first = self.add('alpha', '甲行业')
        second = self.add('beta', '乙行业')
        kb.import_records(ws.profile_path(self.root, first), self.record)
        kb.import_records(ws.profile_path(self.root, second), {**self.record, 'publisher': '第二企业'})
        self.assertEqual(len(ws.find(self.root, [first], self.filters())), 1)
        hits = ws.find(self.root, ws.selected(self.root, 'all', True), self.filters())
        self.assertEqual({r['industry_id'] for r in hits}, {'alpha', 'beta'})
        self.assertEqual({r['id'] for r in hits}, {'same-fact'})
        self.assertTrue(all(r['url'] == self.record['url'] and r['checked_date'] == self.today.isoformat() for r in hits))

    def test_freshness_boundaries_and_explicit_validity(self):
        day = date(2026, 10, 13)
        record = {**self.record, 'checked_date': '2026-10-06'}
        policy = {'freshness': {'default_days': 30, 'category_days': {'企业与竞争': 7}}}
        self.assertEqual(ws.freshness(record, policy, day)['freshness'], 'review_due')
        self.assertEqual(ws.freshness(record, policy, day - timedelta(days=1))['freshness'], 'current')
        explicit = {**record, 'review_after': '2026-10-20'}
        self.assertEqual(ws.freshness(explicit, policy, day)['freshness'], 'current')
        expired = {**explicit, 'valid_until': '2026-10-12'}
        self.assertEqual(ws.freshness(expired, policy, day)['freshness'], 'expired')
        self.assertEqual(ws.freshness({**expired, 'status': 'superseded'}, policy, day)['freshness'], 'superseded')
        self.assertEqual(ws.freshness({**explicit, 'status': 'needs_verification'}, policy, day)['freshness'], 'needs_verification')
        self.assertEqual(record['status'], 'reported')

    def test_filters_apply_before_limit(self):
        entry = self.add('alpha', '甲行业')
        home = ws.profile_path(self.root, entry)
        kb.import_records(home, [{**self.record, 'id': 'old-fact', 'title': '合同合同合同',
                                 'checked_date': (self.today - timedelta(days=60)).isoformat()}, self.record])
        args = self.filters()
        args.limit = 1
        args.freshness = 'current'
        self.assertEqual(ws.find(self.root, [entry], args)[0]['id'], 'same-fact')

    def test_sources_are_unverified_and_only_mode(self):
        entry = self.add('alpha', '甲行业')
        args = argparse.Namespace(action='add', id='official', name='官网', url='https://example.org',
                                  type='primary', categories=['政策与监管'], priority=1)
        catalog = ws.source_command(self.root, entry, args)
        self.assertFalse(catalog['sources'][0]['verified'])
        self.assertIsNone(catalog['sources'][0]['checked_date'])
        ws.source_command(self.root, entry, argparse.Namespace(action='mode', mode='only'))
        ws.industry_command(self.root, argparse.Namespace(action='enable', id='alpha'))
        self.assertEqual(ws.update_plan(self.root)['industries'][0]['source_mode'], 'only')
        ws.source_command(self.root, entry, argparse.Namespace(action='disable', id='official'))
        self.assertEqual(ws.update_plan(self.root)['industries'][0]['sources'], [])

    def test_coverage_failure_preservation_and_append(self):
        entry = self.add('alpha', '甲行业')
        failed = self.run_result(status='failed', sources_accessed=[], sources_failed=['超时'],
                                 uncovered_ranges=[{'reason': '网络失败'}])
        result = ws.finish_run(self.root, entry, failed)
        self.assertNotIn('covered_through', result['state'])
        good = ws.finish_run(self.root, entry, self.run_result())
        self.assertEqual(good['state']['covered_through'], self.today.isoformat())
        ws.finish_run(self.root, entry, failed)
        state = kb.read_json(ws.profile_path(self.root, entry) / 'data/update-state.json')
        self.assertEqual(state['covered_through'], self.today.isoformat())
        text = Path(result['report']).read_text(encoding='utf-8')
        self.assertEqual(text.count('## 本次运行'), 3)
        self.assertIn('网络失败', text)

    def test_gap_and_invalid_success_rejected(self):
        entry = self.add('alpha', '甲行业')
        home = ws.profile_path(self.root, entry)
        kb.atomic_write(home / 'data/update-state.json', kb.json_text({
            'covered_through': (self.today - timedelta(days=20)).isoformat()}))
        with self.assertRaises(ValueError):
            ws.finish_run(self.root, entry, self.run_result())
        for patch in ({'sources_failed': ['超时']}, {'uncovered_ranges': [{'reason': '空档'}]}, {'sources_accessed': []}):
            with self.assertRaises(ValueError):
                ws.finish_run(self.root, entry, self.run_result(**patch))
        ws.industry_command(self.root, argparse.Namespace(action='enable', id='alpha'))
        plan = ws.update_plan(self.root)['industries'][0]
        self.assertEqual(plan['from'], (self.today - timedelta(days=19)).isoformat())

    def test_brief_keeps_sources_and_metrics_and_missing_data(self):
        entry = self.add('alpha', '甲行业')
        home = ws.profile_path(self.root, entry)
        record = {**self.record, 'checked_date': (self.today - timedelta(days=60)).isoformat(),
                  'metrics': [{'name': '收入', 'value': 5, 'unit': '亿元', 'currency': 'CNY',
                               'period': '2025', 'geography': '中国', 'scope': '集团', 'basis': '财报', 'nature': 'reported_actual'}]}
        kb.import_records(home, record)
        args = self.filters()
        args.query = None
        result = ws.brief(self.root, [entry], args)
        text = Path(result['path']).read_text(encoding='utf-8')
        self.assertIn('待复核', text)
        self.assertIn(self.record['url'], text)
        self.assertIn('CNY', text)
        self.assertIn('../../industries/alpha/evidence/same-fact.md', text)
        empty = self.add('beta', '乙行业')
        empty_report = ws.brief(self.root, [empty], args)
        self.assertEqual(empty_report['evidence_count'], 0)

    def test_path_escape_and_concurrent_profile_mutations_rejected(self):
        with self.assertRaises(ValueError):
            self.add('../escape', '坏路径')
        with self.assertRaises(ValueError):
            ws.profile_path(self.root, {'path': '../outside'})
        with kb.lock(self.root):
            with self.assertRaises(ValueError):
                self.add('alpha', '甲行业')
        self.assertEqual(ws.registry(self.root)['industries'], [])

    def test_freshness_fields_validate_without_changing_checked_date(self):
        for patch in ({'review_after': '2026-02-30'}, {'source_id': '../escape'},
                      {'superseded_by': '../escape'}, {'review_after': '2020-01-01'}):
            with self.assertRaises(ValueError):
                kb.validate({**self.record, **patch})


if __name__ == '__main__':
    unittest.main()
