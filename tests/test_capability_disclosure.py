"""Deferred capabilities preserve execution, admission and existing effect budgets."""
import asyncio
import json
import unittest
from unittest.mock import patch

from blackrose.motion_canvas import MODEL_INSTRUCTIONS
from blackrose.config import (EVIDENCE_GUIDANCE, IMAGE_GUIDANCE, KNOWLEDGE_GUIDANCE,
                              SKILL_GUIDANCE, WEB_GUIDANCE)
from blackrose.errors import InputRejected
from blackrose.prompts import ANIMATION_SKILL_ID, DraftMessage, PromptBuilder, estimated_tokens
from blackrose import ui_documents
from blackrose.skills import CATALOG_START
from test_animations import example
import test_gui_images as fixtures
from test_turns import Backend


class DisclosureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    def controller(self, steps, context=128000):
        c = self.runtime.controller
        c.model_name = 'opencode-go/test-vision'
        c.backend = Backend(steps, context)
        return c

    def tools(self, c, index):
        return {t['function']['name'] for t in c.backend.offered[index]}

    def install_skill(self, body='Review the evidence.', name='focused-review'):
        package = self.root / 'source-skill'
        package.mkdir()
        (package / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: Review evidence.\n---\n' + body)
        self.runtime.controller.skills.install(str(package), approved=True)

    async def test_cached_ui_validation_preserves_prompt_and_tool_definitions(self):
        self.install_skill()
        c = self.controller([])
        c.config.interactive_ui_enabled = True
        c.config.citation_rigor = 'off'
        builder = PromptBuilder(c.config, c.skills, c._store, {})
        ui_documents._validated_contract.cache_clear()
        ui_documents._validated_bundle.cache_clear()
        cold_prompt = builder.build([], DraftMessage('Hello'))
        cold_tools = c.tool_definitions()
        self.assertEqual(builder.build([], DraftMessage('Hello')), cold_prompt)
        self.assertEqual(c.tool_definitions(), cold_tools)

    async def test_ordinary_chat_keeps_discovery_without_animation_or_empty_skill_payload(self):
        c = self.controller(['Hello.'])
        await c.send('Hello')
        system = c.backend.requests[0][0]['content']
        self.assertNotIn(MODEL_INSTRUCTIONS, system)
        self.assertNotIn(CATALOG_START, json.dumps(c.backend.requests[0]))
        self.assertNotIn(SKILL_GUIDANCE, system)
        self.assertNotIn('read_skill', self.tools(c, 0))
        self.assertNotIn('animation_create', self.tools(c, 0))
        self.assertIn('activate_tools', self.tools(c, 0))

    async def test_explicit_animation_request_is_ready_without_activation_round(self):
        c = self.controller([[('animation_create', {'scene': example(), 'loop': True})], 'Ready.'])
        await c.send('Make a looping animation.')
        self.assertIn('animation_create', self.tools(c, 0))
        self.assertNotIn('activate_tools', self.tools(c, 0))
        self.assertEqual(c.backend.requests[0][0]['content'].count(MODEL_INSTRUCTIONS), 1)
        saved = (await c.current_messages())[-1].attachments[0]
        self.assertTrue((await c.read_animation(saved.id))[1]['loop'])

    async def test_activation_does_not_spend_the_existing_execution_round(self):
        c = self.controller([[('activate_tools', {'capability': 'animations'})],
                             [('animation_create', {'scene': example(), 'loop': True})], 'Ready.'])
        c.config.max_tool_rounds = 1
        await c.send('Show the packet moving in a playable visual.')
        self.assertNotIn('animation_create', self.tools(c, 0))
        self.assertIn('animation_create', self.tools(c, 1))
        self.assertNotIn('activate_tools', self.tools(c, 1))
        self.assertEqual(c.backend.requests[1][0]['content'].count(MODEL_INSTRUCTIONS), 1)
        self.assertEqual(c.backend.offered[2], [])
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, 'complete')
        self.assertEqual(len(saved.attachments), 1)

    async def test_new_turn_can_revise_saved_scene_without_matching_user_keywords(self):
        c = self.controller([[('animation_create', {'scene': example()})], 'Ready.'])
        await c.send('Animate the pipeline.')
        saved = (await c.current_messages())[-1].attachments[0]
        revised = dict(example(), title='Revised scene')
        c.backend = Backend([[('animation_read', {'attachment_id': saved.id})],
                             [('animation_create', {'scene': revised, 'revises_attachment_id': saved.id})], 'Updated.'])
        await c.send('Slow that down and use its existing source.')
        self.assertIn('animation_read', self.tools(c, 0))
        self.assertIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])
        self.assertEqual((await c.current_messages())[-1].attachments[0].filename, 'Revised scene.animation.json')

    async def test_activation_resets_between_turns_without_saved_artifacts(self):
        c = self.controller([[('activate_tools', {'capability': 'animations'})], 'Here is a plan.', 'Hello.'])
        await c.send('Show a moving visual.')
        await c.send('Hello')
        self.assertIn('activate_tools', self.tools(c, 2))
        self.assertNotIn('animation_create', self.tools(c, 2))
        self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[2][0]['content'])

    async def test_successful_skill_read_activates_animation_without_preloading_references(self):
        self.install_skill('Use animation_create for the visual. See references/optional.md if needed.',
                           name=ANIMATION_SKILL_ID)
        c = self.controller([[('read_skill', {'skill_id': ANIMATION_SKILL_ID})],
                             [('animation_create', {'scene': example()})], 'Ready.'])
        await c.send('Show this process using the relevant skill.')
        self.assertNotIn('animation_create', self.tools(c, 0))
        self.assertIn('animation_create', self.tools(c, 1))
        self.assertNotIn('Use animation_create for the visual.', c.backend.requests[0][0]['content'])
        self.assertEqual(len((await c.current_messages())[-1].attachments), 1)

    async def test_explicit_skill_loads_animation_contract_before_first_request(self):
        self.install_skill('Use animation_create to explain the process.', name=ANIMATION_SKILL_ID)
        c = self.controller(['Here is the plan.'])
        await c.send(f'${ANIMATION_SKILL_ID} Explain this.', refs=[{'kind': 'skill', 'id': ANIMATION_SKILL_ID}])
        self.assertIn('animation_create', self.tools(c, 0))
        self.assertIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])

    async def test_other_skill_text_naming_capabilities_switches_no_contract(self):
        # Only a named bundled skill's identity loads a host contract, never matching text.
        self.install_skill('Use animation_create and illustration_draft. Ian Xiaohei style. '
                           'Read ian-xiaohei-illustrations next.')
        c = self.controller([[('read_skill', {'skill_id': 'focused-review'})], 'Ready.', 'Planned.'])
        await c.send('Explain the process using the relevant skill.')
        await c.send('$focused-review Explain it again.', refs=[{'kind': 'skill', 'id': 'focused-review'}])
        for index in range(3):
            with self.subTest(request=index):
                self.assertEqual(self.tools(c, index), self.tools(c, 0))
                self.assertNotIn('animation_create', self.tools(c, index))
                self.assertNotIn('illustration_draft', self.tools(c, index))
                self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[index][0]['content'])
        self.assertIn('read_skill', self.tools(c, 0))

    async def test_skill_availability_is_recomputed_on_next_request(self):
        c = self.controller(['Hello.', 'Reviewed.', 'Hello again.'])
        await c.send('Hello')
        self.install_skill()
        await c.send('Review this.')
        c.skills.set_enabled('focused-review', False)
        await c.send('Hello again')
        self.assertNotIn('read_skill', self.tools(c, 0))
        self.assertIn('read_skill', self.tools(c, 1))
        self.assertNotIn('read_skill', self.tools(c, 2))
        self.assertIn(SKILL_GUIDANCE, c.backend.requests[1][0]['content'])
        # The catalog rides in the request message; the system message has only policy.
        self.assertNotIn('Review evidence.', c.backend.requests[1][0]['content'])
        self.assertIn(CATALOG_START, c.backend.requests[1][-1]['content'])
        self.assertNotIn(CATALOG_START, json.dumps(c.backend.requests[2]))

    async def test_repeated_activation_cannot_extend_tool_budget(self):
        c = self.controller([[('activate_tools', {'capability': 'animations'})],
                             [('activate_tools', {'capability': 'animations'})], 'Unavailable.'])
        c.config.max_tool_rounds = 1
        await c.send('Show a moving visual.')
        result = json.loads(c.backend.requests[2][-2]['content'])
        self.assertIn('unavailable', result['error'])
        self.assertEqual(c.backend.offered[2], [])
        self.assertEqual(len(c.backend.requests), 3)

    async def test_invalid_capability_spends_an_ordinary_round_and_cannot_enable_other_tools(self):
        c = self.controller([[('activate_tools', {'capability': 'shell'})], 'Unavailable.'])
        c.config.max_tool_rounds = 1
        await c.send('Inspect the options.')
        self.assertEqual(c.backend.offered[1], [])
        self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[1][0]['content'])
        self.assertIn('error', json.loads(c.backend.requests[1][-2]['content']))

    async def test_mixed_file_and_activation_batch_spends_an_ordinary_round(self):
        c = self.controller([[('activate_tools', {'capability': 'animations'}),
                              ('file_read', {'path': 'note.txt'})], 'Read the file.'])
        c.config.max_tool_rounds = 1
        (self.root / 'note.txt').write_text('Fixture evidence.')
        await c.set_workspace(str(self.root))
        await c.send('Read note.txt and consider a moving visual.')
        self.assertEqual(c.backend.offered[1], [])
        results = [json.loads(m['content']) for m in c.backend.requests[1] if m['role'] == 'tool']
        self.assertTrue(results[0]['enabled'])
        self.assertEqual(results[1]['text'], 'Fixture evidence.')

    async def test_context_is_readmitted_after_loading_animation_schema_and_instructions(self):
        c = self.controller(['Done.'])
        text = 'Show a moving visual.'
        await c.send(text)
        initial_bound = estimated_tokens(c.backend.requests[0], c.backend.offered[0])
        await c.new_conversation()
        c.backend = Backend([[('activate_tools', {'capability': 'animations'})], 'Must not run.'],
                            context=int(initial_bound / .875) + 256)
        with self.assertRaises(InputRejected):
            await c.send(text)
        self.assertEqual(len(c.backend.requests), 1)
        self.assertEqual((await c.current_messages())[-1].state.value, 'error')

    async def test_cancel_after_activation_does_not_leak_to_next_turn(self):
        c = self.controller([[('activate_tools', {'capability': 'animations'})], 'Must not run.'])
        def stop(name, phase, result=None):
            if name == 'activate_tools' and phase == 'complete':
                raise asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await c.send('Show a moving visual.', on_tool=stop)
        self.assertEqual((await c.current_messages())[-1].state.value, 'cancelled')
        c.backend = Backend(['Hello.'])
        await c.send('Hello')
        self.assertIn('activate_tools', self.tools(c, 0))
        self.assertNotIn(MODEL_INSTRUCTIONS, c.backend.requests[0][0]['content'])

    async def test_default_guidance_tracks_offered_capabilities(self):
        cases = (
            ('knowledge_enabled', ('knowledge_search', 'knowledge_open')),
            ('web_enabled', ('web_search', 'fetch_content')),
            ('image_generation_enabled', ('image_gen',)),
        )
        for setting, names in cases:
            for enabled in (False, True):
                with self.subTest(setting=setting, enabled=enabled):
                    c = self.controller(['Done.'])
                    await c.new_conversation()
                    c.config.knowledge_enabled = True
                    c.config.web_enabled = True
                    c.config.image_generation_enabled = True
                    c.config.image_provider = 'openrouter'
                    c.image_generator = fixtures.Service()
                    c.local_image_generator = None
                    setattr(c.config, setting, enabled)
                    await c.send('Hello')
                    system = c.backend.requests[0][0]['content']
                    for name in names:
                        self.assertEqual(name in system, enabled)
                        self.assertEqual(name in self.tools(c, 0), enabled)

    async def test_default_guidance_uses_request_web_override_and_image_provider(self):
        c = self.controller(['Done.'])
        c.config.knowledge_enabled = True
        c.config.web_enabled = True
        c.config.image_generation_enabled = True
        c.image_generator = None
        c.local_image_generator = None
        await c.send('Hello', web=False)
        system = c.backend.requests[0][0]['content']
        self.assertIn('knowledge_search', system)
        for name in ('web_search', 'fetch_content', 'image_gen'):
            self.assertNotIn(name, system)
            self.assertNotIn(name, self.tools(c, 0))

    async def test_capability_gating_reduces_default_prompt_bytes(self):
        results = {}
        for label, enabled in (('enabled', True), ('disabled', False)):
            c = self.controller(['Done.'])
            await c.new_conversation()
            c.config.knowledge_enabled = enabled
            c.config.web_enabled = enabled
            c.config.image_generation_enabled = enabled
            c.config.trusted_hosts = []
            c.config.image_provider = 'openrouter'
            c.image_generator = fixtures.Service() if enabled else None
            c.local_image_generator = None
            await c.send('Hello')
            messages = c.backend.requests[0]
            tools = c.backend.offered[0]
            results[label] = (
                len(messages[0]['content'].encode()),
                len(json.dumps({'messages': messages, 'tools': tools}).encode()),
            )
        removed = sum(len(section.encode()) for section in
                      (KNOWLEDGE_GUIDANCE, WEB_GUIDANCE, EVIDENCE_GUIDANCE, IMAGE_GUIDANCE))
        self.assertEqual(results['enabled'][0] - results['disabled'][0], removed)
        self.assertLess(results['disabled'][1], results['enabled'][1])

    async def test_filesystem_safety_rules_have_one_authoritative_location(self):
        c = self.controller(['Done.'])
        await c.send('Hello')
        system = c.backend.requests[0][0]['content']
        tools = c.backend.offered[0]
        combined = system + '\n' + json.dumps(tools)
        for text in ('Never claim a file was saved', 'accepts new files immediately',
                     'New files elsewhere and existing-file edits', 'File creation does not execute scripts'):
            self.assertNotIn(text, system)
        for text in ('Never claim a file was saved unless the tool result confirms the save.',
                     'Existing files are never overwritten.', 'Scripts are saved, never executed.'):
            self.assertEqual(combined.count(text), 1)
        descriptions = {t['function']['name']: t['function']['description'] for t in tools}
        self.assertIn('pending approval', descriptions['document_create'])
        self.assertIn('no change occurs until the user reviews and approves', descriptions['file_edit'])
        self.assertIn(str(c.outputs_folder), system)
        self.assertIn('Additional user-approved directories', system)

    async def test_custom_system_prompt_is_preserved_with_capabilities_disabled(self):
        c = self.controller(['Done.'])
        c.config.system_prompt = ('Custom instructions. ' + KNOWLEDGE_GUIDANCE + WEB_GUIDANCE
                                  + EVIDENCE_GUIDANCE + IMAGE_GUIDANCE + SKILL_GUIDANCE)
        c.config.knowledge_enabled = False
        c.config.web_enabled = False
        c.config.image_generation_enabled = False
        c.image_generator = None
        c.local_image_generator = None
        await c.send('Hello')
        self.assertTrue(c.backend.requests[0][0]['content'].startswith(c.config.system_prompt))

    async def test_custom_system_prompt_is_preserved_without_enabled_skills(self):
        c = self.controller(['Done.'])
        c.config.system_prompt = 'Custom instructions. ' + SKILL_GUIDANCE
        with patch.object(c.skills, 'read', side_effect=AssertionError('Unexpected read')):
            await c.send('Hello')
        self.assertTrue(c.backend.requests[0][0]['content'].startswith(c.config.system_prompt))
