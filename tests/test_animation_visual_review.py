"""Deterministic review, failure and bounded-repair regressions."""
import base64
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, UnidentifiedImageError
from blackrose.animation_visual_review import contact_sheet, event_times, inspect_and_repair, parse_review


def samples():
    output = BytesIO()
    Image.new('RGB', (960, 540), '#664444').save(output, 'PNG')
    png = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()
    return {'duration': 2, 'samples': [{'time': t, 'png': png} for t in (0, 1, 2)]}


def review(category='motion', obj='ball'):
    return {'assessment': 'fail', 'fulfills_request': False, 'findings': [{
        'category': category, 'timestamp': 1, 'object': obj,
        'observation': 'The ball reverses above the floor.',
        'correction': 'Move the lowest point to the floor.',
        'confidence': 'high', 'repairable': True}]}


PASS = {'assessment': 'pass', 'fulfills_request': True, 'findings': []}


class VisualReviewTests(unittest.TestCase):
    def test_plan_boundaries_are_bounded_and_ordered(self):
        plan = {'timeline': [{'transition': {'duration': 1}}, {'wait': 1},
                             {'parallel': [{'duration': 1}, {'duration': 2}]}]}
        self.assertEqual(event_times(plan, 5), (1, 2, 4))
        self.assertEqual(event_times(None, 5), ())

    def test_contact_sheet_orders_and_bounds_frames(self):
        result = samples()
        result['samples'].reverse()
        url, times = contact_sheet(result)
        self.assertEqual(times, [0, 1, 2])
        self.assertTrue(url.startswith('data:image/jpeg;base64,'))
        self.assertLess(len(url), 3_000_000)
        result['samples'][0]['png'] = 'data:image/png;base64,AAAA'
        with self.assertRaises(UnidentifiedImageError):
            contact_sheet(result)

    def test_contact_sheet_is_a_single_time_ordered_column(self):
        url, _ = contact_sheet(samples())
        sheet = Image.open(BytesIO(base64.b64decode(url.split(',', 1)[1])))
        self.assertEqual(sheet.size, (480, 3 * (270 + 22)))

    def test_invalid_and_incomplete_reviews_are_rejected(self):
        for value in ({}, PASS | {'findings': [review()['findings'][0]]},
                      review() | {'findings': [{'category': 'motion'}]},
                      review() | {'assessment': 'fail', 'fulfills_request': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_review(json.dumps(value), 2)
        self.assertEqual(parse_review(json.dumps(review()), 2)['assessment'], 'fail')

    def test_fenced_json_is_accepted_but_fenced_invalid_json_is_not(self):
        fenced = '```json\n' + json.dumps(review()) + '\n```'
        self.assertEqual(parse_review(fenced, 2)['assessment'], 'fail')
        self.assertEqual(parse_review(json.dumps(review()), 2)['assessment'], 'fail')
        with self.assertRaises(ValueError):
            parse_review('```json\n{"assessment": "pass"}\n```', 2)


class VisualReviewWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, responses, *, repair=True):
        chat = SimpleNamespace(config=SimpleNamespace(motion_visual_repair=repair))
        scene = {'source': 'old', 'duration': 2, 'compiled': 'old compiled'}
        calls = AsyncMock(side_effect=responses)
        with patch('blackrose.animation_visual_review.motion_canvas.player_document', return_value='document'), \
             patch('blackrose.animation_visual_review.motion_canvas.probe_document', return_value=samples()), \
             patch('blackrose.animation_visual_review.motion_canvas.prepare_scene',
                   side_effect=lambda candidate: candidate | {'compiled': 'new compiled'}), \
             patch('blackrose.animation_visual_review.repair_text', calls):
            result = await inspect_and_repair(chat, scene, 'Bounce a ball', None,
                                              None, {}, None, lambda phase: None)
        return result, calls

    async def test_success_and_motion_repair_revalidated(self):
        (scene, assessment), calls = await self.exercise([json.dumps(review()), 'new', json.dumps(PASS)])
        self.assertEqual(scene['source'], 'new')
        self.assertTrue(assessment['repaired'])
        self.assertEqual(calls.await_count, 3)
        self.assertEqual(calls.await_args_list[0].args[1][1]['content'][1]['type'], 'image_url')

    async def test_unchanged_saddle_stitch_defect_keeps_original(self):
        defect = review('conceptual', 'needle and folded spine')
        (scene, assessment), calls = await self.exercise([json.dumps(defect), 'new', json.dumps(defect)])
        self.assertEqual(scene['source'], 'old')
        self.assertEqual(assessment['assessment'], 'uncertain')
        self.assertEqual(calls.await_count, 3)

    async def test_invalid_response_and_disabled_repair(self):
        (scene, assessment), calls = await self.exercise(['not json'])
        self.assertEqual(scene['source'], 'old')
        self.assertEqual(assessment['assessment'], 'unavailable')
        (scene, assessment), calls = await self.exercise([json.dumps(review())], repair=False)
        self.assertEqual(assessment['assessment'], 'fail')
        self.assertEqual(calls.await_count, 1)

    async def test_cancellation_propagates(self):
        import asyncio
        with self.assertRaises(asyncio.CancelledError):
            await self.exercise([asyncio.CancelledError()])

    async def test_review_timeout_preserves_valid_original(self):
        (scene, assessment), calls = await self.exercise([TimeoutError('review timed out')])
        self.assertEqual(scene['source'], 'old')
        self.assertEqual(assessment['assessment'], 'unavailable')
        self.assertEqual(calls.await_count, 1)

    async def test_capability_and_configuration_gate(self):
        from pathlib import Path
        from blackrose.tool_execution import ToolExecutor
        chat = SimpleNamespace(_store=SimpleNamespace(root=Path('/tmp')),
            config=SimpleNamespace(motion_visual_review=True), _supports_vision=lambda: False)
        executor = ToolExecutor(chat, SimpleNamespace(id=1, conversation_id=2),
                                SimpleNamespace(), False, on_delta=lambda _: None)
        executor.visual_review_context = ('request', None, {}, None)
        self.assertIsNone(executor.visual_review_callback('animation_create'))
        chat._supports_vision = lambda: True
        self.assertIsNotNone(executor.visual_review_callback('animation_create'))
        chat.config.motion_visual_review = False
        self.assertIsNone(executor.visual_review_callback('animation_create'))
