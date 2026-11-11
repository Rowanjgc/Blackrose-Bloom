"""A prompt's system message is rendered from one copy of its application policy."""
import unittest
from unittest.mock import Mock, patch

from blackrose.config import Config
from blackrose.prompts import DraftMessage, PromptBuilder
from test_prompt_history import attachment, message

FILESYSTEM = '\nLocal filesystem: active workspace /scratch.'
UI_CONTRACT = '\n\nUI contract: mixed'
# A generated image on an assistant message: listed in the manifest, never read
# from storage for a text-only model.
HISTORY = [message(1, 'assistant', attachments=[attachment(1)])]
MANIFEST = ('\nConversation image attachments for image_gen references:\n'
            '[{"attachment_id": 1, "filename": "1.png"}]')


def builder(config=None):
    skills = Mock()
    skills.catalog.return_value = ''
    return PromptBuilder(config or Config(), skills, Mock(), {}, animations_available=True,
                         motion_plan_first=True)


@patch('blackrose.ui_documents.instructions', lambda profile='mixed': f'UI contract: {profile}')
class PromptInstructionTests(unittest.TestCase):
    def test_every_policy_change_keeps_the_system_message_equal_to_the_instructions(self):
        prompt = builder().build([message(1)], DraftMessage('Continue'))
        sent = [prompt.messages[0]['content']]
        steps = [
            ('filesystem', lambda: prompt.add_instruction(FILESYSTEM)),
            ('library write', prompt.enable_library_write),
            ('animations', prompt.enable_animations),
            ('interactive UI', prompt.enable_ui),
            ('UI unloaded', prompt.disable_ui),
            ('filesystem removed', lambda: prompt.remove_instruction(FILESYSTEM)),
        ]
        self.assertEqual(sent[0], prompt.instructions)
        for name, step in steps:
            step()
            sent.append(prompt.messages[0]['content'])
            with self.subTest(name):
                self.assertNotEqual(sent[-1], sent[-2])
                self.assertEqual(sent[-1], prompt.instructions)
        # Unloading restores the exact earlier bytes; the reply still cannot reactivate UI.
        self.assertEqual(sent[5], sent[3])
        self.assertNotIn(FILESYSTEM, sent[6])
        self.assertIsNone(prompt.ui_profile)
        with self.assertRaises(ValueError):
            prompt.enable_ui()

    def test_manifest_stays_out_of_the_policy_and_additions_append(self):
        b = builder(Config(system_prompt='Custom policy.'))
        prompt = b.build(HISTORY, DraftMessage('Continue'))
        policy = prompt.instructions
        self.assertNotIn('Conversation image attachments', policy)
        self.assertEqual(prompt.messages[0]['content'], policy + MANIFEST)
        prompt.add_instruction(FILESYSTEM)
        prompt.enable_ui()
        # Each addition appends, leaving bytes already sent unchanged.
        self.assertEqual(prompt.messages[0]['content'], policy + MANIFEST + FILESYSTEM + UI_CONTRACT)
        self.assertEqual(prompt.instructions, policy + FILESYSTEM + UI_CONTRACT)
        prompt.disable_ui()
        self.assertEqual(prompt.messages[0]['content'], policy + MANIFEST + FILESYSTEM)
        # Removing policy ahead of the manifest moves the insertion point with it.
        head = policy.replace('Custom policy.', '')
        prompt.remove_instruction('Custom policy.')
        prompt.add_instruction(UI_CONTRACT)
        self.assertEqual(prompt.messages[0]['content'], head + MANIFEST + FILESYSTEM + UI_CONTRACT)
        self.assertEqual(prompt.instructions, head + FILESYSTEM + UI_CONTRACT)
        # The prepared rebuild carries the policy and places the manifest last.
        second = b.build(HISTORY, message(2), prepared=prompt)
        self.assertEqual(second.instructions, prompt.instructions)
        self.assertEqual(second.messages[0]['content'], prompt.instructions + MANIFEST)

    def test_instructions_cannot_be_assigned_around_the_system_message(self):
        prompt = builder().build([], DraftMessage('Continue'))
        with self.assertRaises(AttributeError):
            prompt.instructions += FILESYSTEM
        with self.assertRaises(AttributeError):
            prompt.manifest = FILESYSTEM
        self.assertNotIn(FILESYSTEM, prompt.messages[0]['content'])
        self.assertEqual(prompt.messages[0]['content'], prompt.instructions)


if __name__ == '__main__':
    unittest.main()
