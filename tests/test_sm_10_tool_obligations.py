"""SM-10: per-turn artifact obligations, image effect budgets and draft revisions.

See ``docs/state-machines/sm-10-tool-obligations.md``. These tests pin the
acceptance predicate at the end of a reply, the latching rules behind it, the
plan-first fallback latches and the revision gate on illustration drafts.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from blackrose.errors import RecoveryRequired
from blackrose.image_generation import GeneratedImage
from blackrose.illustrations import IllustrationDrafts
from blackrose.tool_execution import MAX_IMAGE_JOBS, EffectBudget, ToolExecutor
from blackrose.tools import FILE_TOOLS, animation_tools, illustration_draft_tool, image_tool

VISUAL = 'export function build(view: any): Record<string, any> { return {}; }'


def scene():
    return {
        'core_idea': 'Two needles share one thread.',
        'facts': ['A saddle stitch uses two needles on one thread.'],
        'scene': 'Two aligned leather layers in a stitching pony.',
        'action': 'Xiaohei pulls both thread ends apart.',
        'labels': [],
    }


def valid_plan():
    return {'version': 1, 'objective': 'Move a dot', 'scene': 'A dot moves right',
            'constraints': 'Readable labels',
            'elements': [{'id': 'dot', 'description': 'A dot',
                          'initial': {'x': 0, 'y': 0}, 'final': {'x': 100}}],
            'timeline': [{'transition': {'target': 'dot', 'property': 'x', 'value': 100,
                                        'duration': 1}}],
            'final_hold': 0.5}


def animation_scene():
    return {'title': 'Dot', 'description': 'A dot moves', 'duration': 1, 'source': VISUAL}


class Harness:
    """The controller surface a ToolExecutor needs, with the real tool schemas."""

    def __init__(self, test, *, xiaohei=False, repair_id=None):
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image_outcome = 'ok'
        self.animation_outcome = 'ok'
        self.document_result = {'saved_path': str(self.root / 'out.md')}
        self.cancel_document = False
        self.attachment_read_error = False
        self.image_requests = []
        self.animation_requests = []
        self.saved_images = []

        async def create_animation(assistant, arguments, **kwargs):
            self.animation_requests.append((dict(arguments), kwargs))
            if self.animation_outcome == 'fail':
                raise RuntimeError('compilation failed')
            return {'attachment_id': 7, 'notice': 'Animation saved.'}, []

        async def generate_image(assistant, arguments, on_progress, *, on_start):
            self.image_requests.append(dict(arguments))
            if self.image_outcome == 'pre_start':
                raise ValueError('Image specification rejected')
            on_start()
            if self.image_outcome == 'fail':
                raise RuntimeError('Local image generation failed')
            info = SimpleNamespace(width=320, height=180, suffix='.png')
            flags = {}
            if self.image_outcome in {'cancel', 'both'}:
                flags['cancelled'] = True
            if self.image_outcome in {'warning', 'both'}:
                flags['cleanup_warning'] = 'release failed'
            generated = GeneratedImage(b'png-bytes', info, 'test', 0.0, **flags)
            saved = [SimpleNamespace(id=11)]
            return ({'attachment_id': 11, 'width': 320, 'height': 180}, saved, generated)

        async def file_tool(name, arguments, assistant):
            if self.cancel_document:
                raise asyncio.CancelledError
            if isinstance(self.document_result, BaseException):
                raise self.document_result
            return self.document_result

        async def attachments_for(message_id):
            if self.attachment_read_error:
                raise RuntimeError('database is locked')
            return []

        self.controller = SimpleNamespace(
            _store=SimpleNamespace(root=self.root),
            config=SimpleNamespace(knowledge_enabled=False, motion_visual_review=False),
            automatic_animation_repair=True,
            animations_manual_only=False,
            outbound=None,
            uses_local_images=False,
            local_image_generator=SimpleNamespace(recovery_error=None),
            _create_animation=create_animation,
            _generate_image=generate_image,
            file_tool=file_tool,
            _sources=lambda name, result: [],
            _repo=SimpleNamespace(attachments_for=attachments_for),
            _supports_vision=lambda: False,
            tool_definitions=self.tool_definitions,
        )
        self.prompt = SimpleNamespace(
            animation_ids=set(), xiaohei=xiaohei, plan_only=False, animations_enabled=True,
            skills_available=False, library_write_enabled=False, ui_used=False, messages=[])
        self.assistant = SimpleNamespace(id=1, conversation_id=2)
        self.executor = ToolExecutor(
            self.controller, self.assistant, self.prompt, False,
            on_delta=lambda part: None, on_images=self.saved_images.append,
            on_animations=lambda saved: None, animation_repair_id=repair_id)

    def tool_definitions(self, web=None, **flags):
        definitions = [image_tool(local=True, xiaohei=flags.get('xiaohei', False))]
        if flags.get('xiaohei'):
            definitions.append(illustration_draft_tool())
        definitions += FILE_TOOLS
        definitions += animation_tools([], plan_first=True)
        return definitions

    async def call(self, name, arguments):
        call = {'id': f'call-{name}-{len(self.prompt.messages)}',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}
        await self.executor.execute(call, {})
        return json.loads(self.prompt.messages[-1]['content'])


class DraftRevisionStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drafts = IllustrationDrafts(self.root)

    def files(self):
        return sorted(path.name for path in self.root.iterdir())

    def test_every_mutating_path_requires_the_current_revision(self):
        saved = self.drafts.save(scene())
        self.assertEqual(saved['revision'], 1)
        before = self.files()
        for revision in (2, True, 1.0, '1', None):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(ValueError, 'Stale'):
                    self.drafts.save({'action': 'Changed'}, 'draft-1', revision)
            with self.subTest(revision=revision, path='validated'):
                with self.assertRaisesRegex(ValueError, 'Stale'):
                    self.drafts.validated('draft-1', revision)
        # No rejected path may write a revision snapshot or move the current pointer.
        self.assertEqual(self.files(), before)
        self.assertEqual(self.drafts.read('draft-1', 1)['plan']['action'], scene()['action'])

    def test_two_repairs_are_allowed_and_snapshots_stay_immutable(self):
        self.drafts.save({'core_idea': 'Original'})
        self.drafts.save({'scene': scene()['scene']}, 'draft-1', 1)
        repaired = self.drafts.save(scene(), 'draft-1', 2)
        self.assertTrue(repaired['valid'])
        self.assertEqual(repaired['revision'], 3)
        with self.assertRaisesRegex(ValueError, 'repair limit'):
            self.drafts.save({'action': 'Late'}, 'draft-1', 3)
        first = json.loads((self.root / 'draft-1-r1.json').read_text())
        self.assertEqual(first['plan']['core_idea'], 'Original')
        current = self.drafts.read('draft-1', 3)['plan']
        self.assertEqual({key: current[key] for key in scene()}, scene())
        self.assertEqual(current['placement'], 'standalone')
        self.assertNotIn('draft-1-r4.json', self.files())

    def test_foreign_draft_ids_cannot_be_read_or_written(self):
        self.drafts.save(scene())
        for draft_id in ('draft-0', 'draft-5', '../draft-1', 'draft-1 ', ''):
            with self.subTest(draft_id=draft_id):
                with self.assertRaises(ValueError):
                    self.drafts.save({'action': 'Changed'}, draft_id, 1)
        self.assertEqual(self.files(), ['draft-1-r1.json', 'draft-1.json'])

    def test_new_drafts_count_current_files_not_revision_snapshots(self):
        self.drafts.save(scene())
        self.drafts.save({'action': 'Changed'}, 'draft-1', 1)
        second = IllustrationDrafts(self.root).save(scene())
        self.assertEqual((second['draft_id'], second['revision']), ('draft-2', 1))
        self.assertIn('draft-2-r1.json', self.files())


class ObligationStateTests(unittest.IsolatedAsyncioTestCase):
    def test_acceptance_predicate_covers_every_reachable_combination(self):
        cases = [
            ('empty turn', {}, None),
            ('attempted draft never valid', {'draft_attempted': True},
             'unresolved blocking issues; no image was generated'),
            ('valid draft only', {'draft_attempted': True, 'draft_valid': True}, None),
            ('requested illustration never created', {'illustration_requested': True},
             'Illustration generation did not complete'),
            ('created illustration', {'draft_attempted': True, 'draft_valid': True,
                                      'illustration_requested': True,
                                      'illustration_created': True}, None),
            ('created illustration then invalid repair',
             {'draft_attempted': True, 'draft_valid': False,
              'illustration_requested': True, 'illustration_created': True},
             'predates the rejected draft revision'),
            ('attempted animation never created', {'animation_attempted': True},
             'Animation creation failed'),
            ('created animation', {'animation_attempted': True, 'animation_created': True}, None),
            ('attempted document never created', {'document_attempted': True},
             'Document creation failed'),
            ('created document', {'document_attempted': True, 'document_created': True}, None),
            ('started image failed', {'image_error': 'timed out'},
             'Image generation failed: timed out'),
            ('animation error reported first',
             {'animation_attempted': True, 'image_error': 'timed out'},
             'Animation creation failed'),
            ('illustration error reported before image error',
             {'illustration_requested': True, 'image_error': 'timed out'},
             'Illustration generation did not complete'),
        ]
        for label, flags, message in cases:
            with self.subTest(label=label):
                harness = Harness(self)
                for name, value in flags.items():
                    if name == 'image_error':
                        harness.executor.effects.image_error = value
                    else:
                        setattr(harness.executor, name, value)
                if message is None:
                    harness.executor.require_successful_artifacts()
                else:
                    with self.assertRaises(RuntimeError) as caught:
                        harness.executor.require_successful_artifacts()
                    self.assertIn(message, str(caught.exception))

    async def test_later_failed_attempts_do_not_revoke_created_latches(self):
        harness = Harness(self)
        await harness.call('animation_create', {'scene': animation_scene(), 'loop': False})
        self.assertTrue(harness.executor.animation_created)
        harness.animation_outcome = 'fail'
        await harness.call('animation_create', {'scene': animation_scene(), 'loop': False})
        self.assertTrue(harness.executor.animation_created)
        await harness.call('document_create', {'path': 'out.md', 'content': 'x'})
        self.assertTrue(harness.executor.document_created)
        harness.document_result = ValueError('disk full')
        await harness.call('document_create', {'path': 'out.md', 'content': 'x'})
        self.assertTrue(harness.executor.document_created)
        self.assertIn('disk full', harness.executor.document_error)
        harness.executor.require_successful_artifacts()

    async def test_invalid_repair_after_a_saved_illustration_keeps_the_artifact_claim(self):
        harness = Harness(self, xiaohei=True)
        await harness.call('illustration_draft', {'changes': scene()})
        result = await harness.call('image_gen', {'draft_id': 'draft-1', 'revision': 1})
        self.assertEqual((result['draft_id'], result['revision']), ('draft-1', 1))
        self.assertEqual(len(harness.saved_images), 1)
        harness.executor.require_successful_artifacts()
        invalid = scene() | {'action': ' '}
        await harness.call('illustration_draft',
                           {'changes': invalid, 'draft_id': 'draft-1', 'revision': 1})
        self.assertTrue(harness.executor.illustration_created)
        with self.assertRaises(RuntimeError) as caught:
            harness.executor.require_successful_artifacts()
        self.assertIn('predates the rejected draft revision', str(caught.exception))
        self.assertNotIn('no image was generated', str(caught.exception))

    async def test_invalid_draft_without_an_image_reports_no_image(self):
        harness = Harness(self, xiaohei=True)
        await harness.call('illustration_draft', {'changes': {}})
        self.assertFalse(harness.executor.illustration_created)
        with self.assertRaisesRegex(RuntimeError, 'no image was generated'):
            harness.executor.require_successful_artifacts()

    async def test_started_image_failure_fails_the_turn_after_an_earlier_success(self):
        harness = Harness(self)
        harness.image_outcome = 'ok'
        await harness.call('image_gen', {'prompt': 'a cat'})
        harness.image_outcome = 'fail'
        result = await harness.call('image_gen', {'prompt': 'a cat'})
        self.assertIn('Further image execution is blocked', result['notice'])
        self.assertEqual(harness.executor.effects.image_error, 'Local image generation failed')
        with self.assertRaisesRegex(RuntimeError, 'Image generation failed'):
            harness.executor.require_successful_artifacts()

    async def test_pre_start_image_failure_does_not_fail_the_turn(self):
        harness = Harness(self)
        harness.image_outcome = 'pre_start'
        result = await harness.call('image_gen', {'prompt': 'a cat'})
        self.assertIn('no image job has started', result['notice'])
        self.assertEqual(harness.executor.effects.image_jobs, 0)
        self.assertIsNone(harness.executor.effects.image_error)
        harness.executor.require_successful_artifacts()

    async def test_four_started_jobs_hide_the_tool_without_an_error_latch(self):
        harness = Harness(self)
        for _ in range(MAX_IMAGE_JOBS):
            await harness.call('image_gen', {'prompt': 'a cat'})
        self.assertEqual(harness.executor.effects.image_jobs, MAX_IMAGE_JOBS)
        self.assertNotIn('image_gen', [tool['function']['name']
                                      for tool in harness.executor.definitions()])
        fifth = await harness.call('image_gen', {'prompt': 'a cat'})
        self.assertIn('unavailable', fifth['error'])
        self.assertIsNone(harness.executor.effects.image_error)
        harness.executor.require_successful_artifacts()
        # The budget guard behind the schema check still refuses a started job.
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            EffectBudget(image_jobs=MAX_IMAGE_JOBS).start_image()

    async def test_cancelled_download_keeps_the_artifact_and_latches_no_error(self):
        for outcome in ('cancel', 'both'):
            with self.subTest(outcome=outcome):
                harness = Harness(self, xiaohei=True)
                await harness.call('illustration_draft', {'changes': scene()})
                harness.image_outcome = outcome
                with self.assertRaises(asyncio.CancelledError):
                    await harness.call('image_gen', {'draft_id': 'draft-1', 'revision': 1})
                self.assertTrue(harness.executor.illustration_created)
                self.assertIsNone(harness.executor.effects.image_error)
                self.assertEqual(len(harness.saved_images), 1)

    async def test_cleanup_warning_after_a_saved_image_requires_recovery(self):
        harness = Harness(self, xiaohei=True)
        await harness.call('illustration_draft', {'changes': scene()})
        harness.image_outcome = 'warning'
        with self.assertRaises(RecoveryRequired) as caught:
            await harness.call('image_gen', {'draft_id': 'draft-1', 'revision': 1})
        self.assertIn('release failed', str(caught.exception))
        self.assertTrue(harness.executor.illustration_created)
        self.assertIsNone(harness.executor.effects.image_error)
        self.assertEqual(len(harness.saved_images), 1)

    async def test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan(self):
        harness = Harness(self)
        accepted = await harness.call('animation_plan', {'plan': valid_plan()})
        self.assertTrue(accepted['valid'])
        self.assertIsNotNone(harness.executor.motion_plan)
        broken = valid_plan()
        broken['timeline'][0]['transition']['target'] = 'missing'
        fallback = await harness.call('animation_plan', {'plan': broken})
        self.assertIn('animation_create directly', fallback['notice'])
        self.assertTrue(harness.executor.motion_plan_disabled)
        self.assertIsNone(harness.executor.motion_plan)
        self.assertEqual(harness.executor.rejected_motion_plan, broken)
        self.assertIn('unknown target', harness.executor.motion_plan_error)
        refused = await harness.call('animation_plan', {'plan': valid_plan()})
        self.assertIn('unavailable', refused['error'])
        # The refusal overwrites the root diagnostic in animation_error (SM-10 H3).
        self.assertIn('Plan-first is unavailable', harness.executor.animation_error)
        with self.assertRaisesRegex(RuntimeError, 'Animation creation failed'):
            harness.executor.require_successful_artifacts()
        created = await harness.call('animation_create',
                                     {'scene': animation_scene(), 'loop': False})
        self.assertEqual(created['strategy'], 'direct')
        harness.executor.require_successful_artifacts()
        self.assertEqual(harness.prompt.animation_ids, {7})

    async def test_planned_creation_failure_falls_back_without_keeping_the_plan(self):
        harness = Harness(self)
        await harness.call('animation_plan', {'plan': valid_plan()})
        harness.animation_outcome = 'fail'
        result = await harness.call('animation_create_planned',
                                    {'scene': animation_scene(), 'loop': False})
        self.assertIn('animation_create directly', result['notice'])
        self.assertTrue(harness.executor.motion_plan_disabled)
        self.assertIsNone(harness.executor.motion_plan)
        self.assertFalse(harness.executor.animation_created)
        self.assertEqual(len(harness.animation_requests), 1)

    async def test_document_proposal_counts_as_created_without_a_file(self):
        harness = Harness(self)
        harness.document_result = {'approval_required': True, 'approval_id': 'token',
                                   'notice': 'No file was created.'}
        result = await harness.call('document_create',
                                    {'path': '/elsewhere/note.md', 'content': 'text'})
        self.assertTrue(result['approval_required'])
        self.assertTrue(harness.executor.document_attempted)
        # SM-10 H2: the latch means "the handler completed", not "a file exists".
        self.assertTrue(harness.executor.document_created)
        harness.executor.require_successful_artifacts()

    async def test_cancelled_document_creation_still_propagates_cancellation(self):
        harness = Harness(self)
        harness.cancel_document = True
        with self.assertRaises(asyncio.CancelledError):
            await harness.call('document_create', {'path': 'out.md', 'content': 'text'})

    async def test_cancelled_document_creation_becomes_a_storage_error(self):
        """SM-10 H4: a failed attachment refresh replaces the user's Stop.

        This characterizes current behaviour so the hazard is visible; the
        proposed fix is recorded in the specification, not applied here.
        """
        harness = Harness(self)
        harness.cancel_document = True
        harness.attachment_read_error = True
        with self.assertRaisesRegex(RuntimeError, 'database is locked'):
            await harness.call('document_create', {'path': 'out.md', 'content': 'text'})


if __name__ == '__main__':
    unittest.main()
