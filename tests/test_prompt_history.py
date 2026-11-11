"""Turn-local history reuse preserves the persisted request's wire representation."""
from datetime import UTC, datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from blackrose.clock_context import request_messages
from blackrose.config import Config
from blackrose.prompts import DraftMessage, PromptBuilder
from blackrose.storage.records import MessageState, StoredAttachment, StoredMessage
from test_gui_images import png


NOW = datetime(2026, 11, 3, tzinfo=UTC)


def attachment(identity, kind='image'):
    return StoredAttachment(identity, identity, kind, 'image/png', f'{identity}.png',
                            str(identity), 100, 'digest', NOW,
                            text_path=str(identity) if kind == 'document' else None)


def message(identity, role='user', **kwargs):
    return StoredMessage(identity, 1, role, f'Message {identity}', NOW, **kwargs)


class PromptHistoryTests(unittest.TestCase):
    def test_prepared_history_matches_full_build_and_reads_each_attachment_once(self):
        for vision in (False, True):
            with self.subTest(vision=vision):
                skills = Mock()
                skills.catalog.return_value = 'Available skills: reviewer'
                skills.read.return_value = {'id': 'reviewer', 'text': 'Selected instructions'}
                store = Mock()
                store.read.side_effect = lambda path: b'Document evidence' if path == '2' else png()
                builder = PromptBuilder(Config(citation_rigor='advisory'), skills, store,
                    {'architecture': {'input_modalities': ['text', 'image'] if vision else ['text']}},
                    memory_notes=[{'note': 'Remembered preference'}])
                history = [
                    message(1, attachments=[attachment(1)]),
                    message(2, attachments=[attachment(2, 'document')]),
                    message(3, 'assistant', attachments=[attachment(3)],
                            audit={'status': 'supported', 'claims': []}),
                    message(4, 'assistant', state=MessageState.ERROR),
                    message(5, 'assistant', blocks=[{
                        'kind': 'interactive_ui', 'status': 'ready', 'block_id': 'form',
                        'source': '{"form":true}', 'fallback_text': 'Form',
                        'saved_state': {'choice': 'A'}}]),
                ]
                refs = ({'kind': 'skill', 'id': 'reviewer'},)
                pending_image = SimpleNamespace(kind='image', filename='new.png', data=png(),
                                                image=SimpleNamespace(mime_type='image/png'))
                context = {'submission': 'A'}
                text = 'Adjust the form using my saved values'
                draft = DraftMessage(text, (pending_image,), refs=refs, ui_context=context)
                saved = message(6, attachments=[attachment(6)], refs=list(refs), ui_context=context)
                saved.content = text
                first = builder.build(history, draft)
                first_wire = json.dumps(first.messages)
                reads = store.read.call_count
                second = builder.build(history, saved, prepared=first)
                # Only the newly persisted request is read in the second pass.
                self.assertEqual(store.read.call_count, reads + 1)
                self.assertEqual(reads, 3 if vision else 2)
                self.assertEqual(json.dumps(first.messages), first_wire)
                self.assertNotIn('"attachment_id": 6', first.messages[0]['content'])
                self.assertIn('"attachment_id": 6', second.messages[0]['content'])
                self.assertEqual(skills.read.call_count, 1)
                full = builder.build(history, saved)
                with patch('blackrose.clock_context.clock_context', return_value='Fixed clock'):
                    self.assertEqual(json.dumps(request_messages(second.messages)).encode(),
                                     json.dumps(request_messages(full.messages)).encode())
                self.assertEqual(second.animation_ids, full.animation_ids)
                self.assertEqual(second.skills_available, full.skills_available)

    def test_prepared_instructions_keep_filesystem_and_animation_contracts(self):
        skills = Mock()
        skills.catalog.return_value = ''
        builder = PromptBuilder(Config(), skills, None, {}, animations_available=True)
        history = [message(1, 'assistant', attachments=[attachment(1, 'animation')])]
        first = builder.build(history, DraftMessage('Continue'))
        filesystem = '\nLocal filesystem: active workspace /scratch'
        first.add_instruction(filesystem)
        second = builder.build(history, message(2), prepared=first)
        self.assertEqual(second.messages[1:], [first.messages[1],
                                              {'role': 'user', 'content': 'Message 2'}])
        self.assertTrue(second.messages[0]['content'].startswith(first.instructions))
        self.assertEqual(second.messages[0]['content'].count(filesystem), 1)
        self.assertIn('"attachment_id": 1', second.messages[0]['content'])
        self.assertEqual(second.instructions, first.instructions)
        self.assertTrue(second.animations_enabled)
        self.assertEqual(second.animation_ids, {1})
