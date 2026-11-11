"""Mermaid is a rendering capability, not a new provider protocol."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from blackrose.config import Config, SKILL_GUIDANCE
from blackrose.diagrams import MODEL_GUIDANCE
from blackrose.gui.render import to_html
from blackrose.gui.session import Session
from blackrose.prompts import DraftMessage, PromptBuilder, estimated_tokens
from test_gui_images import make_runtime


SOURCE = 'flowchart LR\n  A[Read] --> B[Understand]\n'
FENCED = '```mermaid\n' + SOURCE + '```'


class DiagramMarkdownTests(unittest.TestCase):
    def test_only_explicitly_closed_mermaid_fences_are_candidates(self):
        for marker in ('```', '~~~~'):
            for language in ('mermaid', 'Mermaid'):
                text = marker + language + '\n' + SOURCE
                with self.subTest(marker=marker, language=language):
                    self.assertNotIn('data-mermaid', to_html(text))
                    self.assertNotIn('data-mermaid', to_html(text + marker[:-1]))
                    self.assertIn('data-mermaid', to_html(text + marker + '  \n'))
                    self.assertNotIn('data-mermaid', to_html(text + marker + ' invalid'))

    def test_container_fences_and_parent_autoclosure(self):
        for prefix, rest in (('> ', '\nOutside'), ('  ', '\nOutside')):
            first = '> ' if prefix == '> ' else '- '
            text = first + '```mermaid\n' + prefix + 'graph LR\n' + prefix + 'A-->B\n'
            with self.subTest(prefix=prefix):
                self.assertIn('data-mermaid', to_html(text + prefix + '```' + rest))
                self.assertNotIn('data-mermaid', to_html(text + rest))

    def test_fences_still_interrupt_paragraphs_without_a_blank_line(self):
        for prefix in ('', '> ', '- '):
            indent = '> ' if prefix == '> ' else '  ' if prefix == '- ' else ''
            text = prefix + 'Introduction\n' + indent + '```mermaid\n'
            text += indent + 'flowchart LR\n' + indent + 'A-->B\n' + indent + '```'
            with self.subTest(prefix=prefix):
                self.assertIn('data-mermaid', to_html(text))
                ordinary = text.replace('mermaid', 'python')
                self.assertIn('<figure class="code">', to_html(ordinary))

    def test_sources_remain_escaped_and_other_code_unchanged(self):
        text = '```mermaid\nflowchart LR\nA["<script>alert(1)</script>"]\n```'
        result = to_html(text)
        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt;', result)
        self.assertNotIn('data-mermaid', to_html(FENCED.replace('mermaid', 'mermaid!')))
        result = to_html('```python\nprint(1)\n```')
        self.assertIn('code-wrap', result)
        self.assertNotIn('data-mermaid', result)
        self.assertEqual(to_html(FENCED + '\n\n' + FENCED).count('data-mermaid'), 2)

    def test_prompt_is_small_optional_and_not_duplicated_on_prepared_rounds(self):
        skills = Mock()
        skills.catalog.return_value = ''
        skills.list.return_value = []
        for custom in (False, True):
            config = Config()
            if custom:
                config.system_prompt = 'Custom assistant.'
            builder = PromptBuilder(config, skills, None, {})
            request = DraftMessage('Explain a state machine.')
            with patch('blackrose.prompts.diagrams_available', return_value=False):
                baseline = builder.build([], request)
            with patch('blackrose.prompts.diagrams_available', return_value=True):
                first = builder.build([], request)
                prepared = builder.build([], request, prepared=first)
            self.assertLess(len(MODEL_GUIDANCE.encode('utf-8')), 512)
            self.assertNotIn(MODEL_GUIDANCE, baseline.instructions)
            self.assertEqual(first.instructions.count(MODEL_GUIDANCE), 1)
            self.assertEqual(prepared.instructions.count(MODEL_GUIDANCE), 1)
            self.assertEqual(prepared.messages, first.messages)
            self.assertGreater(estimated_tokens(first.messages, []), estimated_tokens(baseline.messages, []))
            if not custom:
                self.assertNotIn(SKILL_GUIDANCE, first.instructions)


class DiagramSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_provider_request_persists_source_and_replays_renderable_html(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        _, runtime = make_runtime(Path(temporary))
        events, requests = [], []

        async def stream(model, messages, tools):
            requests.append(messages)
            for chunk in ('```mermaid\n', SOURCE, '```', '\n\nExplanation.'):
                yield {'delta': {'content': chunk}}
            yield {'delta': {}, 'finish_reason': 'stop'}

        runtime.controller.backend.stream = stream
        await runtime.open()
        self.addAsyncCleanup(runtime.close)
        session = Session(runtime.controller, lambda kind, payload: events.append((kind, payload)))
        self.addAsyncCleanup(session.close)
        await session.boot()
        await session.submit('Explain with a diagram.')
        await session._generation
        self.assertEqual(len(requests), 1)
        saved = (await runtime.controller.current_messages())[-1]
        self.assertEqual(saved.content, FENCED + '\n\nExplanation.')
        await session.render_transcript()
        transcript = next(payload for kind, payload in reversed(events) if kind == 'transcript')
        self.assertIn('data-mermaid', transcript['messages'][-1]['html'])
        self.assertNotIn('<svg', transcript['messages'][-1]['html'])
