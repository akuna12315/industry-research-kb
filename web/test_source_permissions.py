"""Local evidence provenance and current source permissions, using synthetic data."""
import unittest

from .knowledge import Knowledge
from .service import kb, ws
from .test_web import Fixture
from .agent_runtime import ResearchContext
from .charts import register_evidence, register_materials, _source


class LocalEvidenceSourceTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup()
        self.knowledge = Knowledge(self.store)
        self.material = self.knowledge.save('alpha', {'id': 'source-plan', 'title': '合成方案',
            'content': '方案计划造林100亩，尚未批复。', 'region': '测试地区', 'model_allowed': False})
        self.local = self.evidence_for(self.material)

    def tearDown(self):
        self.temp.cleanup()

    def evidence_for(self, material):
        body = material['content']
        return {**self.record, 'id': 'local-' + material['id'], 'title': '合成方案的造林计划',
            'summary': body, 'url': '', 'source_kind': 'local_material',
            'sensitivity': material['sensitivity'], 'model_allowed': material['model_allowed'],
            'material_source': {'industry_id': material['industry_id'], 'material_id': material['id'],
                'fingerprint': kb.material_fingerprint(material), 'attachment_sha256': None,
                'citation': material['id'] + f'#chars-1-{len(body)}', 'quote': body}}

    def test_local_import_resolves_real_quote_and_does_not_invent_web_url(self):
        result = kb.import_records(self.store.home('alpha'), self.local)
        self.assertEqual(result['inserted'], 1)
        found = self.store.evidence('alpha', '造林计划')[0]
        self.assertEqual(found['url'], '')
        self.assertEqual(found['material_source']['quote'], self.material['content'])
        rendered = kb.render(found)
        self.assertIn('本地资料 source-plan', rendered)
        self.assertNotIn('](http', rendered)

    def test_forged_quote_and_location_are_rejected_before_any_import(self):
        home = self.store.home('alpha')
        for changes in ({'quote': '已验收999亩'}, {'citation': '../secret#chars-1-9'},
                        {'fingerprint': '0' * 64}, {'citation': 'source-plan#chars-1-99999'}):
            with self.subTest(changes=changes):
                record = {**self.local, 'material_source': {**self.local['material_source'], **changes}}
                with self.assertRaises(ValueError):
                    kb.import_records(home, record)
                self.assertFalse((home / 'evidence' / (record['id'] + '.json')).exists())

    def test_import_cannot_broaden_document_permission(self):
        kb.import_records(self.store.home('alpha'), {**self.local, 'sensitivity': 'public', 'model_allowed': True})
        record = self.store.evidence('alpha', '造林计划')[0]
        self.assertEqual(record['sensitivity'], 'internal')
        self.assertFalse(record['model_allowed'])
        self.assertEqual(self.store.evidence('alpha', '造林计划', model_only=True), [])

    def test_current_permission_controls_existing_evidence_and_charts(self):
        self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': True})
        kb.import_records(self.store.home('alpha'), self.local)
        hits = self.store.evidence('alpha', '造林计划', model_only=True)
        self.assertEqual(len(hits), 1)
        ctx = ResearchContext(self.store, 'alpha')
        reference = register_evidence(ctx, hits)[0]['chart_ref']
        self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': False})
        self.assertEqual(self.store.evidence('alpha', '造林计划', model_only=True), [])
        with self.assertRaisesRegex(ValueError, '权限或版本已变化'):
            _source(ctx, reference)

    def test_changed_document_preserves_old_evidence_but_requires_review(self):
        self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': True})
        kb.import_records(self.store.home('alpha'), self.local)
        self.knowledge.save('alpha', {'id': self.material['id'], 'content': '新版计划造林200亩。'})
        record = self.store.evidence('alpha', '造林计划')[0]
        self.assertTrue(record['source_version_changed'])
        self.assertEqual(record['freshness'], 'review_due')
        self.assertEqual(record['material_source']['quote'], self.material['content'])
        self.assertEqual(self.store.evidence('alpha', '造林计划', model_only=True), [])

    def test_shared_internal_source_stays_out_of_regional_model_results(self):
        for key, region in (('national', '全国'), ('local-region', '测试地区')):
            ws.industry_command(self.root, self.store.args(action='add', id=key, name=key,
                region=region, specialty='forestry', categories=None))
        self.store.save_scope('national', {'library_role': 'national-foundation'})
        material = self.knowledge.save('national', {'id': 'national-source', 'title': '内部全国方案',
            'content': '全国基础造林计划。', 'model_allowed': True})
        kb.import_records(self.store.home('national'), self.evidence_for(material))
        self.assertEqual(len(self.store.evidence('local-region', include_shared=True)), 1)
        self.assertEqual(self.store.evidence('local-region', include_shared=True, model_only=True), [])
        self.knowledge.save('national', {'id': material['id'], 'sensitivity': 'public'})
        self.assertEqual(len(self.store.evidence('local-region', include_shared=True, model_only=True)), 1)

    def test_direct_web_import_cannot_skip_material_review(self):
        with self.assertRaisesRegex(ValueError, '资料核验入口'):
            self.store.import_evidence('alpha', self.local)

    def test_malformed_material_reference_is_rejected_and_legacy_record_fails_closed(self):
        for reference in ('bad-reference-type', [], {}, {'material_id': '../escape'}):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                kb.validate({**self.record, 'related_material': reference})
        path = self.store.home('alpha') / 'evidence' / (self.record['id'] + '.json')
        kb.atomic_write(path, kb.json_text({**kb.read_json(path), 'related_material': 'legacy-bad-data'}))
        self.assertEqual(len(self.store.evidence('alpha')), 1)
        self.assertEqual(self.store.evidence('alpha', model_only=True), [])
        self.assertFalse(self.store.evidence_model_allowed({'source_kind': 'local_material'}))

    def test_material_chart_reference_cannot_survive_version_or_spatial_scope_change(self):
        self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': True})
        ctx = ResearchContext(self.store, 'alpha')
        found = register_materials(ctx, self.knowledge.search('alpha', '造林'))
        ref = found[0]['snippets'][0]['chart_ref']
        self.knowledge.save('alpha', {'id': self.material['id'], 'coordinate_system': '合成坐标系'})
        with self.assertRaisesRegex(ValueError, '版本已变化'):
            _source(ctx, ref)

    def test_evidence_chart_reference_binds_the_retrieved_fact_version(self):
        self.knowledge.save('alpha', {'id': self.material['id'], 'model_allowed': True})
        kb.import_records(self.store.home('alpha'), self.local)
        ctx = ResearchContext(self.store, 'alpha')
        ref = register_evidence(ctx, self.store.evidence('alpha', '造林计划', model_only=True))[0]['chart_ref']
        kb.import_records(self.store.home('alpha'), {**self.local, 'summary': '修订后的计划摘录说明。'})
        with self.assertRaisesRegex(ValueError, '证据版本已变化'):
            _source(ctx, ref)


if __name__ == '__main__':
    unittest.main()
