"""Draft validation, revision ownership, and the image effect boundary."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from blackrose.attachments.images import inspect_image
from blackrose.image_generation import GeneratedImage
from blackrose.illustrations import (IllustrationDrafts, SKILL_ID, normalize_changes,
                                    prepare_illustration, validate_draft)
from jsonschema import ValidationError

from blackrose.tools import image_tool, illustration_draft_tool, validate_call
import test_gui_images as fixtures


def scene():
    return {
        'core_idea': 'Two needles share one thread and pass through each hole from opposite sides.',
        'facts': ['A saddle stitch uses two needles on opposite ends of one thread.',
                  'Both needles pass through the same pre-punched hole from opposite sides.'],
        'scene': 'Two aligned leather layers in a stitching pony. Sparse flat view with broad empty margins.',
        'action': 'Xiaohei grips a needle in each hand and pulls the thread ends evenly apart.',
        'objects': [{'id': 'leather', 'description': 'Two aligned leather layers with pre-punched holes',
                     'position': 'Center, taking half the frame'}],
        'labels': [{'text': 'Same hole', 'target': 'leather', 'position': 'Beside the active hole'}]}


def gen(revision=1, **kwargs):
    return ('image_gen', {'draft_id': 'draft-1', 'revision': revision, **kwargs})


def save(changes=None, **kwargs):
    return ('illustration_draft', {'changes': scene() if changes is None else changes, **kwargs})


class Backend(fixtures.Backend):
    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []

    async def stream(self, model, messages, tools):
        self.requests.append({'messages': deepcopy(messages), 'tools': deepcopy(tools)})
        step = next(self.steps, 'Done.')
        if isinstance(step, str):
            yield {'delta': {'content': step}, 'finish_reason': 'stop'}
        else:
            yield {'delta': {'tool_calls': [
                {'index': i, 'id': f'call-{len(self.requests)}-{i}', 'function': {
                    'name': name, 'arguments': json.dumps(args)}}
                for i, (name, args) in enumerate(step)]}, 'finish_reason': 'tool_calls'}


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drafts = IllustrationDrafts(self.root)

    def test_save_all_missing_fields_then_patch_preserves_decisions_and_persists(self):
        result = self.drafts.save({'core_idea': 'Preserve this thought'})
        self.assertFalse(result['valid'])
        self.assertEqual({i['path'] for i in result['issues']}, {'facts', 'scene', 'action', 'labels'})
        changes = scene()
        del changes['core_idea']
        result = self.drafts.save(changes, 'draft-1', 1)
        self.assertTrue(result['valid'])
        restored = IllustrationDrafts(self.root).validated('draft-1', 2)
        self.assertEqual(restored['core_idea'], 'Preserve this thought')
        self.assertEqual(restored['placement'], 'standalone')
        self.assertNotIn('plan', result)
        self.assertEqual(result['review']['facts'], 'unverified')

    def test_reopening_store_does_not_overwrite_existing_draft_or_revision(self):
        self.drafts.save(scene())
        second = IllustrationDrafts(self.root).save(scene())
        self.assertEqual(second['draft_id'], 'draft-2')
        self.drafts.save({'action': 'A repaired physical action'}, 'draft-1', 1)
        original = json.loads((self.root / 'draft-1-r1.json').read_text())
        self.assertEqual(original['plan']['action'], scene()['action'])
        self.assertEqual(self.drafts.validated('draft-1', 2)['action'], 'A repaired physical action')

    def test_stale_revision_invalid_id_and_invalid_plan_cannot_generate(self):
        self.drafts.save({})
        with self.assertRaisesRegex(ValueError, 'blocking'):
            self.drafts.validated('draft-1', 1)
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.drafts.save(scene(), 'draft-1', 2)
        for value in ('../private', 'draft-2', 'draft-100'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.drafts.validated(value, 1)
        self.assertEqual(self.drafts.read('draft-1', 1)['revision'], 1)

    def test_two_repairs_and_four_drafts_are_enforced_without_overwriting(self):
        self.drafts.save({})
        self.drafts.save({'core_idea': 'First repair'}, 'draft-1', 1)
        self.drafts.save({'scene': 'Second repair'}, 'draft-1', 2)
        with self.assertRaisesRegex(ValueError, 'repair limit'):
            self.drafts.save(scene(), 'draft-1', 3)
        for _ in range(3):
            self.drafts.save(scene())
        with self.assertRaisesRegex(ValueError, 'Draft limit'):
            self.drafts.save(scene())

    def test_conflicting_aliases_unknown_fields_bad_types_and_size_are_not_hidden(self):
        original = {'"scene"': 'A scene'}
        self.assertEqual(normalize_changes(original), {'scene': 'A scene'})
        self.assertIn('"scene"', original)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            normalize_changes(original | {'scene': 'Different scene'})
        bad = scene() | {'unknown': 'Do not discard', 'facts': 'Not a list', 'action': ' '}
        result = self.drafts.save(bad)
        self.assertFalse(result['valid'])
        self.assertTrue({'$', 'facts', 'action'}.issubset({i['path'] for i in result['issues']}))
        with self.assertRaisesRegex(ValueError, '16000 bytes'):
            self.drafts.save({'scene': 'x' * 16001})

    def test_cross_references_and_script_checks_return_all_issues(self):
        bad = scene()
        bad['objects'].append({'id': 'leather', 'description': 'Duplicate'})
        bad['labels'] = [{'text': '错误', 'target': 'missing'}]
        bad['connections'] = [{'from': 'bad', 'to': 'worse', 'meaning': 'broken'}]
        result = self.drafts.save(bad)
        self.assertEqual(len(result['issues']), 5)
        self.assertFalse(result['valid'])

    def test_reference_issues_are_reported_alongside_missing_content(self):
        bad = scene()
        del bad['action']
        bad['labels'][0]['target'] = 'missing'
        result = self.drafts.save(bad)
        self.assertEqual({i['path'] for i in result['issues']}, {'action', 'labels.0.target'})

    def test_empty_labels_and_optional_detail_compile_without_ids_or_qa_claims(self):
        draft = scene()
        draft['labels'] = []
        draft['connections'] = [{'from': 'xiaohei', 'to': 'leather', 'meaning': 'pulls thread through'}]
        self.drafts.save(draft)
        plan = self.drafts.validated('draft-1', 1)
        prompt = prepare_illustration(plan)
        self.assertIn('SOLID BLACK', prompt)
        self.assertIn('WHITE DOT eyes', prompt)
        self.assertIn('No visible text at all', prompt)
        self.assertIn('not independently verified', prompt)
        self.assertIn('pulls thread through', prompt)
        self.assertNotIn('"leather"', prompt)

    def test_validation_is_structural_and_does_not_claim_to_verify_facts(self):
        incorrect = scene() | {'facts': ['The moon is made of leather.']}
        result = self.drafts.save(incorrect)
        self.assertTrue(result['valid'])
        self.assertEqual(result['review']['facts'], 'unverified')
        plan = self.drafts.validated('draft-1', 1)
        self.assertEqual(validate_draft(plan), [])

    def test_no_mutation_and_changes_replace_arrays_without_erasing_other_fields(self):
        original = scene()
        expected = deepcopy(original)
        self.drafts.save(original)
        self.drafts.save({'labels': []}, 'draft-1', 1)
        plan = self.drafts.validated('draft-1', 2)
        self.assertEqual(original, expected)
        self.assertEqual(plan['labels'], [])
        self.assertEqual(plan['objects'], original['objects'])

    def test_schema_is_small_and_generation_requires_identity_for_both_providers(self):
        definitions = [image_tool(local=True, xiaohei=True), illustration_draft_tool()]
        self.assertLess(len(json.dumps(definitions)), 5200)
        for local in (False, True):
            image = image_tool(local=local, xiaohei=True)
            validate_call([image], *gen())
            with self.assertRaises(ValidationError):
                validate_call([image], 'image_gen', {'prompt': 'Cannot bypass validation'})
            validate_call([image_tool(local=local)], 'image_gen', {'prompt': 'A cat'})


class IllustrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    def configure(self, steps, *, local=True):
        c = self.runtime.controller
        c.skills.install(str(Path(__file__).resolve().parents[1] / 'skills' / SKILL_ID),
                         approved=True, origin='bundled')
        c.backend = Backend(steps)
        c.model_name = 'gemma-4-e4b-it' if local else 'test-vision'
        provider = AsyncMock()
        async def generate(*args, **kwargs):
            self.assertTrue(any(kind == 'reply_update' and 'Same hole' in str(payload)
                                for kind, payload in self.events))
            data = fixtures.png(320, 180)
            return GeneratedImage(data, inspect_image(data), 'test', 0.0)
        provider.generate.side_effect = generate
        if local:
            c.local_image_generator = provider
        else:
            c.config.image_provider = 'openrouter'
            c.image_generator = provider
        return c, provider

    async def submit(self, text='Use Xiaohei to illustrate a saddle stitch. One image.'):
        await self.session.submit(text)
        await self.session._generation

    async def test_validated_draft_generates_and_plan_is_visible_without_context_duplication(self):
        c, provider = self.configure([[save()], [gen()]])
        await self.submit()
        provider.generate.assert_awaited_once()
        prompt = provider.generate.call_args.args[0]
        self.assertIn('same pre-punched hole', prompt)
        self.assertIn('Same hole', prompt)
        saved = (await c.current_messages())[-1]
        self.assertTrue(saved.attachments)
        self.assertIn('Illustration draft', saved.content)
        last = c.backend.requests[-1]['messages']
        self.assertFalse(any('**Illustration draft**' in (m.get('content') or '')
                             for m in last if isinstance(m.get('content'), str)))
        result = next(json.loads(m['content']) for m in last if m.get('name') == 'image_gen')
        self.assertEqual(result['qa']['aspect_ratio'], 'passed')
        self.assertEqual(result['qa']['visual_quality'], 'unreviewed')
        self.assertEqual(result['revision'], 1)

    async def test_paid_provider_uses_same_validated_compiler(self):
        c, provider = self.configure([[save()], [gen()]], local=False)
        await self.submit()
        provider.generate.assert_awaited_once()
        self.assertIn('SOLID BLACK', provider.generate.call_args.args[0])

    async def test_aggregate_feedback_and_targeted_repair_do_not_start_extra_image_jobs(self):
        initial = scene()
        initial.update(facts=[], action=' ')
        c, provider = self.configure([
            [save(initial)],
            [save({'facts': scene()['facts'], 'action': scene()['action']}, draft_id='draft-1', revision=1)],
            [gen(2)]])
        await self.submit()
        provider.generate.assert_awaited_once()
        result = next(json.loads(m['content']) for m in c.backend.requests[1]['messages']
                      if m.get('name') == 'illustration_draft')
        self.assertFalse(result['valid'])
        self.assertEqual({i['path'] for i in result['issues']}, {'facts', 'action'})
        self.assertNotIn('plan', result)
        self.assertEqual((await c.current_messages())[-1].state.value, 'complete')

    async def test_missing_or_stale_revision_cannot_bypass_validation(self):
        c, provider = self.configure([
            [gen()], [save()], [save({'action': scene()['action']}, draft_id='draft-1', revision=1)],
            [gen(1)], [gen(2)]])
        await self.submit()
        provider.generate.assert_awaited_once()
        self.assertEqual((await c.current_messages())[-1].state.value, 'complete')

    async def test_invalid_draft_never_generates_and_cannot_end_as_success(self):
        c, provider = self.configure([[save({})], [gen()], 'All done.'])
        await self.submit()
        provider.generate.assert_not_awaited()
        self.assertEqual((await c.current_messages())[-1].state.value, 'error')

    async def test_plan_only_keeps_draft_tool_and_does_not_offer_generation(self):
        c, provider = self.configure([[save()], 'Plan prepared.'])
        await self.submit('Use Xiaohei to plan only a saddle stitch. Do not generate yet.')
        provider.generate.assert_not_awaited()
        for request in c.backend.requests:
            names = [t['function']['name'] for t in request['tools']]
            self.assertNotIn('image_gen', names)
            self.assertIn('illustration_draft', names)

    async def test_skill_read_mid_round_activates_draft_gate(self):
        c, provider = self.configure([
            [('read_skill', {'skill_id': SKILL_ID}), ('image_gen', {'prompt': 'Bypass'})],
            [save()], [gen()]])
        await self.submit('Illustrate using the appropriate enabled skill.')
        provider.generate.assert_awaited_once()
        self.assertNotIn('illustration_draft', [t['function']['name'] for t in c.backend.requests[0]['tools']])
        self.assertIn('illustration_draft', [t['function']['name'] for t in c.backend.requests[1]['tools']])

    async def test_drafts_are_reply_scoped_and_removed_with_conversation(self):
        c, provider = self.configure([[save()], [gen()]])
        await self.submit()
        stored = (await c.current_messages())[-1]
        root = c._store.root / 'illustration-drafts' / str(stored.conversation_id)
        self.assertTrue((root / str(stored.id) / 'draft-1.json').is_file())
        provider.generate.reset_mock()
        c.backend = Backend([[gen()]])
        await self.submit()
        provider.generate.assert_not_awaited()
        await c.delete_current_conversation()
        self.assertFalse(root.exists())
