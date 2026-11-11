"""Stored HTML reuse must preserve fresh metadata and view registrations."""
from datetime import datetime, UTC
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blackrose.citations import EvidenceRegistry, answer_units, validate_assessment
from blackrose.gui import render
from blackrose.gui.session import Session
from blackrose.storage.records import StoredAttachment, StoredMessage
from test_citations import assessment, RESULT


class StoredHTMLTests(unittest.TestCase):
    def setUp(self):
        self.cache = render.StoredHTMLCache()
        self.enterContext(patch.object(render, '_STORED_HTML', self.cache))
        self.message = StoredMessage(1, 2, 'assistant', '**Answer**',
                                     datetime.now(UTC))

    def test_unchanged_messages_skip_parse_but_payload_metadata_stays_fresh(self):
        with patch.object(render, 'to_html', wraps=render.to_html) as parse:
            first = render.message_payload(self.message)
            self.message.error = 'New error'
            second = render.message_payload(self.message, model='other')
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(first['html'], second['html'])
            self.assertEqual(second['error'], 'New error')
            self.assertEqual(second['model'], 'other')
            self.message.content = '**Changed**'
            self.assertIn('Changed', render.message_payload(self.message)['html'])
            self.assertEqual(parse.call_count, 2)

    def test_audit_edits_and_removal_invalidate_rendered_controls(self):
        text = 'The study enrolled 40 participants.'
        self.message.content = text
        registry = EvidenceRegistry(1)
        registry.capture('knowledge_open', RESULT)
        self.message.audit = validate_assessment(assessment({
            'sources': registry.sources, 'answer_units': answer_units(text)}),
            text, registry, 'balanced', 'fixture')
        with patch.object(render, 'audited_html', wraps=render.audited_html) as parse:
            first = render.message_payload(self.message)['html']
            self.assertEqual(first, render.message_payload(self.message)['html'])
            self.assertEqual(parse.call_count, 1)
            self.assertIn('data-status="supported"', first)
            self.message.audit['claims'][0]['status'] = 'unsupported'
            self.assertIn('data-status="unsupported"', render.message_payload(self.message)['html'])
            self.message.audit = None
            self.assertNotIn('claim-citation', render.message_payload(self.message)['html'])
            self.assertEqual(parse.call_count, 3)

    def test_block_edits_identity_and_promotion_invalidate_html(self):
        block = {'kind': 'markdown', 'block_id': 'original',
                 'source': '```mermaid\nflowchart LR\nA-->B\n```'}
        self.message.blocks = [block]
        first = render.message_payload(self.message)['blocks'][0]['html']
        self.assertIn('data-diagram-block="original"', first)
        block['block_id'] = 'replacement'
        block['source'] = block['source'].replace('B', 'C')
        second = render.message_payload(self.message)['blocks'][0]['html']
        self.assertIn('data-diagram-block="replacement"', second)
        self.assertIn('A--&gt;C', second)
        self.message.content = 'Before.\n\n' + block['source'] + '\n\nAfter.'
        block['diagram_promoted'] = True
        with patch.object(render, 'diagram_tokens', wraps=render.diagram_tokens) as parse:
            promoted = render.message_payload(self.message)
            self.assertEqual(promoted, render.message_payload(self.message))
            self.assertEqual(parse.call_count, 1)
        self.assertNotIn('blocks', promoted)
        self.assertIn('Before.', promoted['html'])
        block['source'] = block['source'].replace('C', 'D')
        self.assertIn('A--&gt;D', render.message_payload(self.message)['html'])
        self.message.blocks = []
        self.assertNotIn('data-diagram-block="replacement"', render.message_payload(self.message)['html'])

    def test_session_re_registers_views_even_on_cache_hit(self):
        controller = SimpleNamespace(config=SimpleNamespace(interactive_ui_actions_enabled=True),
                                     attachment_path=lambda _: Path('/tmp/preview.png'))
        session = Session(controller, lambda *args: None)
        self.message.attachments = [StoredAttachment(10, 1, 'image', 'image/png',
            'preview.png', 'preview.png', 100, 'digest', self.message.created_at,
            width=10, height=10)]
        self.message.blocks = [
            {'kind': 'markdown', 'source': '**Block**', 'block_id': 'markdown'},
            {'kind': 'ui', 'source': '{}', 'block_id': 'interactive'}]
        with patch.object(render, 'to_html', wraps=render.to_html) as parse:
            first = session._message_payloads([self.message], {1: ['first source']})[0]
            session._ui_views.clear()
            session._preview_images.clear()
            controller.config.interactive_ui_actions_enabled = False
            second = session._message_payloads([self.message], {1: ['new source']})[0]
            self.assertEqual(parse.call_count, 1)
        self.assertNotEqual(first['blocks'][1]['view_id'], second['blocks'][1]['view_id'])
        self.assertFalse(second['blocks'][1]['actions_enabled'])
        self.assertEqual(second['sources'], ['new source'])
        self.assertEqual(second['attachments'][0]['preview_id'], 10)
        self.assertIs(session._preview_images[10], self.message.attachments[0])
        first['blocks'][0]['html'] = 'Mutated by caller'
        self.assertIn('<strong>Block</strong>', second['blocks'][0]['html'])

    def test_entry_and_byte_budgets_and_oversized_bypass(self):
        cache = render.StoredHTMLCache(max_entries=2, max_bytes=10000)
        with patch.object(render, 'audited_html', wraps=render.audited_html) as parse:
            for identity in (1, 2, 1, 3, 2):
                cache.render(identity, 'Answer')
            self.assertEqual(parse.call_count, 4)  # entry 2 was least recently used
        cache = render.StoredHTMLCache(max_entries=20, max_bytes=800)
        for identity in range(10):
            cache.render(identity, 'x' * 100)
            self.assertLessEqual(cache.bytes, cache.max_bytes)
        self.assertLess(len(cache.entries), 10)
        before = dict(cache.entries)
        cache.render(100, 'x' * 10000)
        self.assertEqual(dict(cache.entries), before)


if __name__ == '__main__':
    unittest.main()
