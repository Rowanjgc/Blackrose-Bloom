"""Evaluation bookkeeping must not turn malformed judge output into a pass."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'grade_citation_answers', Path(__file__).resolve().parents[1] / 'scripts/grade_citation_answers.py')
grading = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grading)

GOOD = {**dict.fromkeys(grading.SCORES, 5), 'overcaution': 0,
        'reason': 'The answer states the supported count.', 'issues': [], 'improvement': 'None'}
PROMPT = {'final_answer_body': '40 participants.', 'annotation_details': {}}


class GradeValidationTests(unittest.TestCase):
    def test_invented_judge_issue_quotation_is_rejected(self):
        grade = {**GOOD, 'issues': [{'kind': 'false_alarm', 'severity': 'major',
                                  'quote': 'invented', 'explanation': 'Not in the answer.'}]}
        with self.assertRaisesRegex(ValueError, 'does not occur'):
            grading.parse_grade(json.dumps(grade), PROMPT)

    def test_failed_grades_are_not_counted_as_passing_or_given_zero_scores(self):
        identity = {'cohort': 'current', 'mode': 'balanced'}
        summary = grading.summarize([{'identity': identity, 'grade': GOOD},
                                     {'identity': identity, 'error': 'Incomplete response'}])[0]
        self.assertEqual((summary['answers'], summary['graded'], summary['errors']), (2, 1, 1))
        self.assertEqual(summary['mean_scores']['body_usefulness'], 5)
        self.assertEqual(summary['useful_and_faithful'], 1)

    def test_major_false_badge_blocks_review_gate_even_with_high_scores(self):
        grade = {**GOOD, 'issues': [{'kind': 'false_support', 'severity': 'major',
                                  'quote': '40 participants.', 'explanation': 'Wrong support attribution.'}]}
        summary = grading.summarize([{'identity': {'cohort': 'current', 'mode': 'balanced'},
                                      'grade': grade}])[0]
        self.assertEqual(summary['useful_and_faithful'], 1)
        self.assertEqual(summary['meets_review_gate'], 0)

    def test_generated_controls_must_share_the_same_draft(self):
        base = {'case': 'long-task', 'question': 'Summarize.', 'draft_origin': 'generated_shared_control',
                'source_context': [{'text': '40 participants.'}], 'draft_text': '40 participants.',
                'final_text': '40 participants.', 'report': None, 'answer_model': 'test'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'answers.json'
            path.write_text(json.dumps({'live': [{**base, 'mode': 'off'}, {**base, 'mode': 'balanced'}]}))
            jobs = grading.prepare([('paired', path)], [])
            self.assertEqual(len(jobs), 2)
            changed = {**base, 'mode': 'balanced', 'draft_text': 'A different generated baseline.'}
            path.write_text(json.dumps({'live': [{**base, 'mode': 'off'}, changed]}))
            with self.assertRaisesRegex(ValueError, 'identical generated drafts'):
                grading.prepare([('paired', path)], [])


class GraderStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalized_stream_resume_and_incomplete_output(self):
        class Client:
            calls = 0

            def __init__(self, **kwargs):
                pass

            async def models(self, provider=None):
                return [{'id': grading.MODEL}]

            async def load(self, model):
                pass

            async def aclose(self):
                pass

            async def stream(self, model, messages, **kwargs):
                Client.calls += 1
                yield {'delta': {'content': json.dumps(GOOD)}}
                yield {'delta': {}, 'finish_reason': 'stop' if Client.calls == 1 else 'length'}
                yield {'delta': {}, '_usage': {'completion_tokens': 100}}
                yield {'delta': {}, '_usage': {'completion_tokens': 100}}

        jobs = [{'key': str(i), 'identity': {'case': str(i), 'cohort': 'current', 'mode': 'balanced'},
                 'prompt': PROMPT} for i in range(2)]
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(output=Path(directory) / 'grades.json', timeout=10, concurrency=1)
            with patch.object(grading, 'OpenCodeGoClient', Client), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    await grading.run(args, jobs)
                result = json.loads(args.output.read_text())
                self.assertEqual(result['summary'][0]['graded'], 1)
                self.assertEqual(result['summary'][0]['errors'], 1)
                self.assertEqual(result['request_attempts'], 2)
                self.assertEqual(len(result['rejected_attempts']), 1)
                self.assertEqual(result['usage_totals']['completion_tokens'], 200)
                with self.assertRaises(SystemExit):
                    await grading.run(args, jobs)
                self.assertEqual(Client.calls, 3)  # successful first request was reused
