"""Evaluation instrumentation must preserve transport and separate control replies."""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import unittest

from blackrose.backend.stream_limits import StreamBounds, StreamBudget


SCRIPT = Path(__file__).resolve().parents[1] / 'experiments/interactive-ui/evaluate.py'
spec = importlib.util.spec_from_file_location('evaluate_interactive_ui', SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_snapshot_keeps_live_arguments_and_choices_unchanged(self):
        budget = StreamBudget(StreamBounds(duration_seconds=30))
        messages = [{'role': 'user', 'content': 'Synthetic task'}]
        tools = [{'function': {'name': 'activate_tools'}}]
        choices = [{'delta': {'reasoning_content': 'Thinking'}},
                   {'delta': {'content': 'Answer'}, '_usage': {'completion_tokens': 4},
                    '_timings': {'predicted_ms': 12}}]
        captured = []

        async def provider(model, sent, offered, **options):
            self.assertEqual(model, 'fixture')
            self.assertIs(sent, messages)
            self.assertIs(offered, tools)
            self.assertIs(options['stream_budget'], budget)
            self.assertIs(options['bounds'], budget.bounds)
            self.assertEqual(options['effort'], 'default')
            budget.add_output('reply', 'Answer')
            messages[0]['content'] = 'Provider mutation'
            for choice in choices:
                yield choice

        stream = evaluation.capture_stream(provider, captured)
        received = [choice async for choice in stream('fixture', messages, tools,
                    stream_budget=budget, bounds=budget.bounds, effort='default')]
        for actual, expected in zip(received, choices, strict=True):
            self.assertIs(actual, expected)
        entry = json.loads(json.dumps(captured))[0]
        self.assertEqual(entry['messages'][0]['content'], 'Synthetic task')
        self.assertEqual(entry['options']['bounds'], asdict(budget.bounds))
        self.assertEqual(entry['options']['stream_budget']['reply_bytes'], 0)
        self.assertEqual(entry['options']['stream_budget']['deadline'], budget.deadline)
        self.assertEqual(budget.reply_bytes, 6)
        self.assertEqual(entry['usage'], choices[-1]['_usage'])
        self.assertEqual(entry['timings'], choices[-1]['_timings'])
        self.assertIsNotNone(entry['first_text_seconds'])
        self.assertIsNotNone(entry['first_reasoning_seconds'])

    async def test_provider_failure_still_propagates_with_request_evidence(self):
        captured = []

        async def provider(*args, **options):
            raise RuntimeError('Transport failed')
            yield  # An async generator, like the real provider.

        with self.assertRaisesRegex(RuntimeError, 'Transport failed'):
            async for _ in evaluation.capture_stream(provider, captured)('fixture', []):
                pass
        self.assertEqual(len(captured), 1)
        with self.assertRaisesRegex(TypeError, 'Unsupported capture metadata'):
            evaluation.capture_value(object())


class OutcomeTests(unittest.TestCase):
    def test_markdown_is_a_control_and_never_a_schema_valid_ui_reply(self):
        markdown = [{'kind': 'markdown', 'status': 'ready'}]
        for blocks in ([], markdown):
            ui = evaluation.answer_checks('ui', blocks, 'A plain answer')
            self.assertFalse(ui['schema_valid'])
            self.assertIsNone(ui['markdown_control_valid'])
            control = evaluation.answer_checks('markdown', blocks, 'A plain answer')
            self.assertTrue(control['markdown_control_valid'])
            self.assertFalse(control['schema_valid'])
        self.assertFalse(evaluation.answer_checks('markdown', [], '')['markdown_control_valid'])

    def test_ui_requires_ready_documents_even_in_mixed_replies(self):
        ready = {'kind': 'interactive_ui', 'status': 'ready', 'document': {'type': 'form'}}
        markdown = {'kind': 'markdown', 'status': 'ready'}
        self.assertTrue(evaluation.answer_checks('ui', [markdown, ready], '')['schema_valid'])
        self.assertFalse(evaluation.answer_checks('markdown', [ready], 'JSON')['markdown_control_valid'])
        for invalid in ({**ready, 'status': 'invalid'}, {**ready, 'document': None}):
            self.assertFalse(evaluation.answer_checks('ui', [ready, invalid], '')['schema_valid'])

    def test_broken_and_partial_runs_are_explicit(self):
        failure = {'mode': 'ui', 'error': 'Capture failed'}
        success = {'mode': 'markdown', 'schema_valid': False, 'markdown_control_valid': True}
        self.assertEqual(evaluation.run_summary([failure] * 10)['status'], 'failed')
        self.assertEqual(evaluation.run_summary([])['status'], 'failed')
        summary = evaluation.run_summary([failure, success])
        self.assertEqual(summary['status'], 'partial_errors')
        self.assertEqual(summary['completed_turns'], 1)
        self.assertEqual(summary['markdown_controls_valid'], 1)
        self.assertEqual(summary['ui_schema_valid'], 0)
