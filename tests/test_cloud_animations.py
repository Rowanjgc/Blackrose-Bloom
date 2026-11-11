"""Provider policy: local models author only on an explicit user request."""
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from blackrose.animations import export_html
from blackrose.motion_canvas import MODEL_INSTRUCTIONS
from blackrose.prompts import ANIMATION_SKILL_ID
from test_animations import example
from test_turns import Backend
import test_gui_images as fixtures

SKILL_DIR = Path(__file__).resolve().parents[1] / 'skills/explanatory-animations'


class AnimationProviderPolicyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    def controller(self, steps):
        c = self.runtime.controller
        c.backend = Backend(steps)
        return c

    def tools(self, c, index):
        return {t['function']['name'] for t in c.backend.offered[index]}

    def capabilities(self, c, index):
        for definition in c.backend.offered[index]:
            if definition['function']['name'] == 'activate_tools':
                return set(definition['function']['parameters']['properties']['capability']['enum'])
        return set()

    def assert_no_animation_tools(self, backend):
        for definitions in backend.offered:
            self.assertFalse(any(t['function']['name'].startswith('animation_') for t in definitions))
            for definition in definitions:
                if definition['function']['name'] == 'activate_tools':
                    self.assertNotIn('animations', definition['function']['parameters']
                                     ['properties']['capability']['enum'])

    async def seed(self):
        c = self.controller([[('animation_create', {'scene': example(), 'loop': False})], 'Created.'])
        c.model_name = 'opencode-go/test-vision'
        turn = await c.send('Animate this.')
        return c, turn.assistant_message.attachments[0]

    async def test_local_unrelated_request_has_no_animation_overhead(self):
        c = self.controller(['An explanation.'])
        await c.send('Explain this.')
        self.assert_no_animation_tools(c.backend)
        self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])
        self.assertTrue(c.animations_manual_only)
        self.assertFalse(c.automatic_animation_repair)

    async def test_local_mention_of_animation_adds_no_authoring_overhead(self):
        c = self.controller(['An explanation of the word.'])
        await c.send('Animate this, or explain the animation concept.')
        self.assert_no_animation_tools(c.backend)
        self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])

    async def test_local_model_chosen_skill_read_cannot_enable_authoring(self):
        c = self.controller([
            [('read_skill', {'skill_id': ANIMATION_SKILL_ID})],
            [('activate_tools', {'capability': 'animations'})], 'An explanation.'])
        c.skills.install(str(SKILL_DIR), approved=True, origin='bundled')
        await c.send('Use the relevant skill for a moving visual.')
        self.assert_no_animation_tools(c.backend)
        self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[-1][0]['content'])
        result = json.loads(c.backend.requests[-1][-1]['content'])
        self.assertNotIn('animation_availability', result)
        self.assertIn('error', json.loads(c.backend.requests[2][-1]['content']))

    async def test_local_explicit_skill_selection_enables_authoring(self):
        c = self.controller(['An explanation.'])
        c.skills.install(str(SKILL_DIR), approved=True, origin='bundled')
        await c.send('Explain this.', refs=[{'kind': 'skill', 'id': ANIMATION_SKILL_ID}])
        self.assertIn('animation_create', self.tools(c, 0))
        self.assertIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])
        self.assertNotIn('animations', self.capabilities(c, 0))

    async def test_literal_skill_token_enables_local_authoring(self):
        c = self.controller([
            [('animation_create', {'scene': example(), 'loop': False})], 'Created.'])
        c.skills.install(str(SKILL_DIR), approved=True, origin='bundled')
        await c.send(f'Please animate a saddle stitch using ${ANIMATION_SKILL_ID}')
        self.assertIn('animation_create', self.tools(c, 0))
        self.assertIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])
        stored = (await c.current_messages())[0]
        self.assertEqual(stored.refs, [{'kind': 'skill', 'id': ANIMATION_SKILL_ID}])

    async def test_unknown_dollar_tokens_are_not_references(self):
        c = self.controller(['An explanation.'])
        c.skills.install(str(SKILL_DIR), approved=True, origin='bundled')
        await c.send(f'Explain $HOME and ${ANIMATION_SKILL_ID}-not-a-skill.')
        self.assertEqual((await c.current_messages())[0].refs, [])
        self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])

    async def test_local_saved_scene_does_not_enable_authoring_without_request(self):
        c, attachment = await self.seed()
        c.model_name = 'test-vision'
        c.backend = Backend(['An explanation.'])
        await c.send('Slow that down.')
        self.assert_no_animation_tools(c.backend)
        self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])
        _, scene = await c.read_animation(attachment.id)
        self.assertIn('html', export_html(scene))
        self.assertIn('scene', self.session._attachment_payload(attachment))
        read = await c._call_tool('animation_read', {'attachment_id': attachment.id})
        self.assertEqual(read['attachment_id'], attachment.id)
        # Playback-failure repair is model-initiated, so it stays cloud-only.
        state = await self.session.animation_failed(attachment.id, 'Renderer failed')
        self.assertEqual(state['status'], 'unavailable')
        self.assertIsNone(self.session._animation_repair_task)

    async def test_local_selected_skill_can_revise_a_saved_scene(self):
        c, attachment = await self.seed()
        c.model_name = 'test-vision'
        c.skills.install(str(SKILL_DIR), approved=True, origin='bundled')
        revised = dict(example(), title='Revised scene')
        c.backend = Backend([
            [('animation_read', {'attachment_id': attachment.id})],
            [('animation_create', {'scene': revised,
                                   'revises_attachment_id': attachment.id})], 'Updated.'])
        await c.send('Revise the animation and slow it down.',
                     refs=[{'kind': 'skill', 'id': ANIMATION_SKILL_ID}])
        self.assertIn('animation_read', self.tools(c, 0))
        self.assertIn('animation_create', self.tools(c, 1))
        saved = (await c.current_messages())[-1].attachments[0]
        self.assertEqual(saved.filename, 'Revised scene.animation.json')

    async def test_local_animation_tools_require_manual_enablement(self):
        c = self.controller([])
        self.assertFalse(any(t['function']['name'].startswith('animation_')
                             for t in c.tool_definitions(animation_ids={1})))
        self.assertTrue(any(t['function']['name'].startswith('animation_')
                            for t in c.tool_definitions(animations_enabled=True)))

    async def test_explicit_repair_turn_cannot_bypass_local_policy(self):
        c = self.controller([])
        with self.assertRaisesRegex(ValueError, 'cloud model'):
            await c.send('Repair animation.', animation_repair_id=1)
        c.backend.load.assert_not_awaited()
        self.assertEqual(c.backend.requests, [])
        self.assertFalse(c.last_request_stored)

    async def test_queued_repair_rechecks_provider_after_current_reply(self):
        c, attachment = await self.seed()
        release = asyncio.Event()
        async def current_reply():
            await release.wait()
            self.session._generation = None
        self.session._generation = asyncio.create_task(current_reply())
        # Queue a source-frame failure; this fixture's saved legacy payload is
        # irrelevant to provider switching after the current reply finishes.
        with patch.object(c, 'read_animation', return_value=(attachment, {'engine': 'motion-canvas'})):
            state = await self.session.animation_failed(attachment.id, 'Runtime failure')
        self.assertEqual(state['status'], 'queued')
        c.model_name = 'test-vision'
        c.backend = Backend([])
        release.set()
        await self.session._animation_repair_task
        self.assertEqual(c.backend.requests, [])
        self.assertEqual(await c.animation_recovery.states(c.conversation.id), {})
        self.assertEqual(self.session._animation_recovery_states[attachment.id]['status'], 'unavailable')
