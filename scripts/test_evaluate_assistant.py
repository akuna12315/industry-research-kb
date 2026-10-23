import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json
import sys

spec = importlib.util.spec_from_file_location('evaluate_assistant', Path(__file__).with_name('evaluate_assistant.py'))
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        scratch = evaluation.ROOT / 'work/qa-runs'
        scratch.mkdir(parents=True, exist_ok=True)
        self.bank_folder = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.bank_folder.cleanup)
        self.bank = Path(self.bank_folder.name) / 'questions.py'
        rows = [dict(no=n, module='Synthetic', q=f'Question {n}?', a=f'Answer {n}',
                     src='Synthetic section', quote=f'Answer {n}', note='Synthetic fixture') for n in range(1,101)]
        self.bank.write_text('QA = [' + ','.join('dict(' + ','.join(f'{k}={v!r}' for k,v in row.items()) + ')' for row in rows) + ']', encoding='utf-8')

    def test_synthetic_question_bank_is_complete_and_payload_does_not_leak_answers(self):
        items = evaluation.load_questions(self.bank)
        self.assertEqual(len(items), 100)
        for item in items:
            payload = evaluation.assistant_payload(item, 'forestry-jiangxi', 'separate-question')
            self.assertEqual(payload['prompt'], item['q'])
            self.assertFalse(payload['research'])
            self.assertFalse(payload['allow_source_changes'])
            self.assertNotIn('a', payload)
            self.assertNotIn('reference', payload)

    def test_question_loader_does_not_execute_code(self):
        with tempfile.TemporaryDirectory(dir=evaluation.ROOT / 'work/qa-runs') as folder:
            source = Path(folder) / 'bank.py'
            marker = Path(folder) / 'must-not-exist'
            source.write_text(f'QA = [dict(no=1, q=__import__("pathlib").Path({str(marker)!r}).write_text("bad"))]', encoding='utf-8')
            with self.assertRaises((ValueError, TypeError)):
                evaluation.load_questions(source)
            self.assertFalse(marker.exists())

    def test_numeric_check_excludes_citation_numbers_and_preserves_reference_warning(self):
        item = {'a': '面积28367亩，另有8项。', 'quote': '面积28367亩。'}
        answer = '资料给出28367亩。[M8]\n\n原文定位：\nplan#block-8-chars-1-100'
        result = evaluation.compare(item, answer)
        self.assertEqual(result['matched_numbers'], ['28367'])
        self.assertIn('8', result['missing_numbers'])
        self.assertIn('8', result['reference_warning'])
        self.assertEqual(result['manual_verdict'], '待核对')

    def test_numeric_normalization_handles_comma_separators_and_decimal_equivalence(self):
        self.assertEqual(evaluation.numeric_tokens('26,328,790.04元、4.0%'), ['26328790.04', '4'])

    def test_checkpoint_resume_does_not_repeat_completed_questions(self):
        class FakeAPI:
            def __init__(self):
                self.prompts = []
            def request(self, path, payload=None):
                if path == '/api/agent/jobs':
                    self.prompts.append(payload['prompt'])
                    return {'id': 'job-' + str(len(self.prompts)), 'conversation': payload['conversation']}
                return {'status': 'completed', 'events': [{'type': 'answer', 'text': '合成测试回答', 'requests': 1}]}
        api = FakeAPI()
        with tempfile.TemporaryDirectory(dir=evaluation.ROOT / 'work/qa-runs') as folder:
            args = ['evaluate_assistant.py', '--questions', str(self.bank), '--industry', 'example', '--material', 'example-material', '--run-dir', folder, '--output', str(Path(folder)/'result.xlsx'), '--limit', '1']
            with patch.object(sys, 'argv', args), patch.object(evaluation, 'API', return_value=api), patch.object(
                    evaluation, 'snapshot', return_value={'model': 'synthetic'}), patch.object(
                    evaluation, 'validate_citations', return_value={'citations': [], 'citation_check': '无原文定位'}):
                evaluation.main()
                evaluation.main()
            result = json.loads((Path(folder)/'run.json').read_text(encoding='utf-8'))
            self.assertEqual(result['counts']['已回答'], 2)
            questions = evaluation.load_questions(self.bank)
            self.assertEqual(api.prompts, [questions[0]['q'], questions[1]['q']])
            self.assertEqual(result['counts']['待测试'], 98)


if __name__ == '__main__':
    unittest.main()
