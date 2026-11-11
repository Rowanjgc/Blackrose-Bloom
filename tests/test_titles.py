"""Background naming never owns a reply, a manual name, or the active chat."""
import asyncio
import contextlib
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from blackrose.backend.providers import ChatProviders
from blackrose.backend.router import RouterClient
from blackrose.storage.records import TitleSource
from blackrose.storage.repository import DEFAULT_TITLE, derive_title
from blackrose.titles import TITLE_INSTRUCTIONS, clean_title, title_messages
import test_gui_images as fixtures


class NamingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown
    latest = fixtures.ImageTests.latest

    async def send(self, text='Could you help me debug a SQLite lock?', **kwargs):
        await self.session.submit(text, **kwargs)
        await asyncio.wait_for(self.session._generation, 2)

    async def settle_title(self):
        task = self.runtime.controller._title_task
        if task:
            await asyncio.wait_for(task, 2)

    def block_title(self):
        started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def suggest(*args, **kwargs):
            started.set()
            try:
                await release.wait()
                return 'Debugging a SQLite lock'
            finally:
                closed.set()

        self.runtime.controller.backend.suggest_title = AsyncMock(side_effect=suggest)
        return started, release, closed

    async def test_reply_finishes_while_title_is_pending_and_sidebar_updates_later(self):
        c = self.runtime.controller
        started, release, _ = self.block_title()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        self.assertEqual(self.latest('reply_end')['state'], 'complete')
        self.assertFalse(self.latest('status')['generating'])
        self.assertFalse(c.is_generating)
        self.assertEqual(c.conversation.title, DEFAULT_TITLE)
        messages = await c.current_messages()
        self.assertEqual(len(messages), 2)
        release.set()
        await self.settle_title()
        self.assertEqual(c.conversation.title, 'Debugging a SQLite lock')
        self.assertEqual(self.latest('conversations')['items'][0]['title'], c.conversation.title)
        self.assertEqual(c.conversation.title_source, TitleSource.AUTO)
        await c.open_conversation(c.conversation.id)
        self.assertEqual(c.conversation.title, 'Debugging a SQLite lock')
        self.assertEqual(len(await c.current_messages()), 2)
        await self.send('And how do transactions help?')
        await self.settle_title()
        c.backend.suggest_title.assert_awaited_once()

    async def test_manual_name_wins_even_when_it_matches_placeholder(self):
        c = self.runtime.controller
        started, release, _ = self.block_title()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        await c.rename_conversation(c.conversation.id, DEFAULT_TITLE)
        release.set()
        await self.settle_title()
        self.assertEqual(c.conversation.title, DEFAULT_TITLE)
        self.assertEqual(c.conversation.title_source, TitleSource.MANUAL)
        await self.send('A follow-up')
        c.backend.suggest_title.assert_awaited_once()

    async def test_switching_chat_does_not_apply_title_to_the_new_chat(self):
        c = self.runtime.controller
        started, release, _ = self.block_title()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        original_id = c.conversation.id
        await self.session.new_conversation()
        new_id = c.conversation.id
        release.set()
        await self.settle_title()
        self.assertEqual(c.conversation.id, new_id)
        self.assertEqual(c.conversation.title, DEFAULT_TITLE)
        original = await c._repo.get_conversation(original_id)
        self.assertEqual(original.title, 'Debugging a SQLite lock')
        self.assertEqual(self.latest('conversations')['current'], new_id)

    async def test_deleted_chat_is_not_recreated_by_late_title(self):
        c = self.runtime.controller
        started, release, _ = self.block_title()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        original_id = c.conversation.id
        await c.delete_current_conversation()
        release.set()
        await self.settle_title()
        self.assertIsNone(await c._repo.get_conversation(original_id))
        self.assertEqual(c.conversation.title, DEFAULT_TITLE)

    async def test_followup_preempts_title_and_retries_with_the_original_exchange(self):
        c = self.runtime.controller
        started, _, closed = self.block_title()
        first_request = 'Help debug a SQLite lock'
        await self.send(first_request)
        await asyncio.wait_for(started.wait(), 2)
        old_task = c._title_task
        await self.send('Yes, please')
        self.assertTrue(old_task.cancelled())
        self.assertTrue(closed.is_set())
        await c.cancel_title()
        self.assertEqual(c.conversation.title_source, TitleSource.PLACEHOLDER)
        c.backend.suggest_title = AsyncMock(return_value='Debugging a SQLite lock')
        await self.send('Go ahead')
        await self.settle_title()
        payload = json.loads(c.backend.suggest_title.call_args.args[1][1]['content'])
        self.assertEqual(payload['request'], first_request)
        self.assertEqual(len(await c.current_messages()), 6)

    async def test_model_change_preempts_title_and_naming_uses_new_selected_model(self):
        c = self.runtime.controller
        started, _, closed = self.block_title()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        await c.select_model('another-local-model')
        self.assertTrue(closed.is_set())
        c.backend.suggest_title = AsyncMock(return_value='Debugging a SQLite lock')
        await self.send('Please continue')
        await self.settle_title()
        self.assertEqual(c.backend.suggest_title.call_args.args[0], 'another-local-model')

    async def test_timeout_failure_and_invalid_output_leave_successful_reply_intact(self):
        c = self.runtime.controller
        for behavior in ('timeout', 'failure', 'invalid'):
            with self.subTest(behavior=behavior):
                await self.session.new_conversation()
                if behavior == 'timeout':
                    self.block_title()
                elif behavior == 'failure':
                    c.backend.suggest_title = AsyncMock(side_effect=RuntimeError('Unavailable'))
                else:
                    c.backend.suggest_title = AsyncMock(return_value=None)
                with patch('blackrose.chat.TITLE_TIMEOUT', 0.02):
                    await self.send('Help with my SQLite database')
                    await self.settle_title()
                self.assertEqual(self.latest('reply_end')['state'], 'complete')
                self.assertEqual(c.conversation.title, derive_title('Help with my SQLite database'))
                self.assertFalse(c.is_generating)

    async def test_failed_and_cancelled_replies_do_not_start_naming(self):
        c = self.runtime.controller
        c.backend.suggest_title = AsyncMock(return_value='Should not appear')

        async def fail(*args, **kwargs):
            raise RuntimeError('Provider failed')
            yield

        c.backend.stream = fail
        await self.send()
        self.assertEqual(self.latest('reply_end')['state'], 'error')
        c.backend.suggest_title.assert_not_awaited()
        started = asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
            yield

        c.backend.stream = blocked
        await self.session.submit('Try again')
        await asyncio.wait_for(started.wait(), 2)
        task = self.session._generation
        self.session.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertEqual(self.latest('reply_end')['state'], 'cancelled')
        c.backend.suggest_title.assert_not_awaited()

    async def test_close_cancels_title_before_backend_and_database_close(self):
        c = self.runtime.controller
        started, _, closed = self.block_title()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        task = c._title_task
        await self.session.close()
        self.assertTrue(closed.is_set())
        self.assertTrue(task.cancelled())
        self.assertIsNone(c._title_task)

    async def test_naming_input_is_small_and_excludes_document_body_and_chat_instructions(self):
        c = self.runtime.controller
        c.backend.suggest_title = AsyncMock(return_value='Reviewing research notes')
        await self.session.attach_bytes(b'PRIVATE_DOCUMENT_BODY', 'notes.txt')
        await self.send('Please summarize these notes. ' + 'detail ' * 1000)
        await self.settle_title()
        messages = c.backend.suggest_title.call_args.args[1]
        self.assertEqual(messages[0]['content'], TITLE_INSTRUCTIONS)
        payload = json.loads(messages[1]['content'])
        self.assertEqual(payload['attachments'], ['notes.txt'])
        self.assertLessEqual(len(payload['request']), 1600)
        self.assertNotIn('PRIVATE_DOCUMENT_BODY', repr(messages))
        self.assertNotIn(c.config.system_prompt, repr(messages))


class TitleTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_title_reuses_transport_without_model_load_or_preset_overrides(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.path, '/v1/chat/completions')
            body = json.loads(request.content)
            self.assertEqual(body['model'], 'already-loaded')
            self.assertEqual(set(body), {'model', 'messages', 'stream', 'stream_options', 'timings_per_token'})
            packet = {'choices': [{'delta': {'content': 'Debugging a SQLite lock'}, 'finish_reason': 'stop'}]}
            return httpx.Response(200, text='data: ' + json.dumps(packet) + '\n\ndata: [DONE]\n\n')

        client = httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler))
        router = RouterClient('http://test', client=client)
        try:
            title = await router.suggest_title('already-loaded', [
                {'role': 'system', 'content': TITLE_INSTRUCTIONS}, {'role': 'user', 'content': '{}'}])
            self.assertEqual(title, 'Debugging a SQLite lock')
            self.assertEqual(len(requests), 1)
        finally:
            await router.aclose()

    async def test_provider_identity_no_tools_and_early_stream_close(self):
        class Backend(fixtures.Backend):
            def __init__(self):
                self.closed = False
                self.requests = []

            async def stream(self, model, messages, tools, **kwargs):
                self.requests.append((model, deepcopy(messages), tools, kwargs))
                try:
                    yield {'delta': {'reasoning_content': 'Internal thoughts'}}
                    yield {'delta': {'content': 'Planning a Kyoto trip'}}
                    yield {'delta': {'content': '\n'}}
                    raise AssertionError('Must stop after the title line')
                finally:
                    self.closed = True

        local, cloud = Backend(), Backend()
        providers = ChatProviders(local, cloud)
        for model, backend in [('local-model', local), ('opencode-go/glm-5.3', cloud)]:
            with self.subTest(model=model):
                title = await providers.suggest_title(model, [], session_id='stable-chat-id')
                self.assertEqual(title, 'Planning a Kyoto trip')
                self.assertTrue(backend.closed)
                request = backend.requests[0]
                self.assertEqual(request[2], ())
                self.assertEqual(request[3], {'session_id': 'stable-chat-id'} if backend is cloud else {})


class TitleTextTests(unittest.TestCase):
    def test_cosmetic_wrappers_are_removed_and_non_english_names_are_kept(self):
        for text, expected in [
            ('Title: **Debugging SQLite.**', 'Debugging SQLite'),
            ('“京都の旅行計画”', '京都の旅行計画'),
            ('<think>Choose a topic.</think>\nSQLite locks', 'SQLite locks'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(clean_title(text), expected)

    def test_empty_reasoning_multiline_and_long_explanations_are_rejected(self):
        for text in ('', '<think>Unfinished', 'Title\nExplanation', 'a' * 200, '\x00'):
            with self.subTest(text=text):
                self.assertIsNone(clean_title(text))

    def test_answer_excerpt_is_bounded(self):
        from types import SimpleNamespace
        messages = title_messages(SimpleNamespace(content='question', attachments=[]),
                                  SimpleNamespace(content='answer ' * 1000))
        self.assertEqual(len(json.loads(messages[1]['content'])['reply']), 1000)
