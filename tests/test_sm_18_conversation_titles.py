"""SM-18: conversation naming, ``TitleSource`` and the background title task.

See ``docs/state-machines/sm-18-conversation-titles.md``. These pin the guards the
machine relies on: automatic naming may replace a placeholder exactly once and
never a manual title, a title task cannot outlive its owner's deletion, and
cancellation settles the provider stream before returning. They also pin the two
gaps the specification records: H1 (an unnameable first exchange marks the chat
named while its text is still the placeholder) and H2 (a second
``schedule_title`` abandons the first task without cancelling it).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from blackrose.storage.db import Database
from blackrose.storage.records import TitleSource
from blackrose.storage.repository import DEFAULT_TITLE, Repository, derive_title
from blackrose.titles import TITLE_INSTRUCTIONS, clean_title, generate_title, title_messages
import test_gui_images as fixtures


class RepoTitleSourceTests(unittest.IsolatedAsyncioTestCase):
    """The persisted half of the machine: one guarded UPDATE and nothing else."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'chats.db'
        self.db = Database(self.path)
        await self.db.connect()
        self.repo = Repository(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self.temporary.cleanup()

    async def row_count(self):
        return await self.db.run(lambda conn: conn.execute(
            'SELECT count(*) FROM conversations').fetchone()[0])

    async def test_creation_classifies_the_default_title_as_unnamed_and_a_custom_one_as_manual(self):
        unnamed = await self.repo.create_conversation()
        self.assertEqual((unnamed.title, unnamed.title_source), (DEFAULT_TITLE, TitleSource.PLACEHOLDER))
        self.assertTrue(unnamed.is_unnamed)
        named = await self.repo.create_conversation('Planning a Kyoto trip')
        self.assertEqual(named.title_source, TitleSource.MANUAL)
        self.assertFalse(named.is_unnamed)
        # An explicit source outranks the equality inference.
        manual_placeholder = await self.repo.create_conversation(
            DEFAULT_TITLE, title_source=TitleSource.MANUAL)
        self.assertFalse(manual_placeholder.is_unnamed)

    async def test_automatic_naming_replaces_the_placeholder_exactly_once(self):
        conversation = await self.repo.create_conversation()
        changed = await self.repo.rename_conversation(
            conversation.id, 'Debugging a SQLite lock',
            source=TitleSource.AUTO, only_if_unnamed=True)
        self.assertTrue(changed)
        after = await self.repo.get_conversation(conversation.id)
        self.assertEqual((after.title, after.title_source),
                         ('Debugging a SQLite lock', TitleSource.AUTO))
        self.assertFalse(after.is_unnamed)
        # AUTO is terminal for the guard: a second automatic name is refused.
        self.assertFalse(await self.repo.rename_conversation(
            conversation.id, 'A different name',
            source=TitleSource.AUTO, only_if_unnamed=True))
        self.assertEqual((await self.repo.get_conversation(conversation.id)).title,
                         'Debugging a SQLite lock')

    async def test_a_manual_title_is_terminal_for_automatic_naming(self):
        conversation = await self.repo.create_conversation()
        self.assertTrue(await self.repo.rename_conversation(conversation.id, 'My own name'))
        self.assertFalse(await self.repo.rename_conversation(
            conversation.id, 'Automatic name',
            source=TitleSource.AUTO, only_if_unnamed=True))
        after = await self.repo.get_conversation(conversation.id)
        self.assertEqual((after.title, after.title_source), ('My own name', TitleSource.MANUAL))

    async def test_a_manual_title_equal_to_the_placeholder_text_is_still_named(self):
        conversation = await self.repo.create_conversation()
        await self.repo.rename_conversation(conversation.id, DEFAULT_TITLE)
        after = await self.repo.get_conversation(conversation.id)
        self.assertEqual((after.title, after.title_source), (DEFAULT_TITLE, TitleSource.MANUAL))
        self.assertFalse(after.is_unnamed)
        self.assertFalse(await self.repo.rename_conversation(
            conversation.id, 'Automatic name', source=TitleSource.AUTO, only_if_unnamed=True))

    async def test_renaming_a_missing_conversation_changes_nothing_and_creates_no_row(self):
        before = await self.row_count()
        self.assertFalse(await self.repo.rename_conversation(999999, 'A name'))
        self.assertFalse(await self.repo.rename_conversation(
            999999, 'A name', source=TitleSource.AUTO, only_if_unnamed=True))
        self.assertEqual(await self.row_count(), before)
        self.assertIsNone(await self.repo.get_conversation(999999))

    async def test_a_deleted_conversation_id_is_never_reused(self):
        """The late-title guard is id-based, so it depends on this schema property."""
        conversation = await self.repo.create_conversation()
        deleted = conversation.id
        await self.repo.delete_conversation(deleted)
        replacement = await self.repo.create_conversation()
        self.assertNotEqual(replacement.id, deleted)
        self.assertTrue(replacement.is_unnamed)

    async def test_an_unknown_stored_title_source_fails_the_whole_listing(self):
        """H5: the column has no CHECK, and one unknown value breaks every read."""
        conversation = await self.repo.create_conversation()
        await self.repo.create_conversation('A named chat')

        def corrupt(conn):
            conn.execute('UPDATE conversations SET title_source = ? WHERE id = ?',
                         ('robot', conversation.id))
            conn.commit()

        await self.db.run(corrupt)
        with self.assertRaises(ValueError):
            await self.repo.list_conversations()


class NamingTaskTests(unittest.IsolatedAsyncioTestCase):
    """``ChatController._title_task`` across rename, deletion, switch and close."""

    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def send(self, text='Could you help me debug a SQLite lock?'):
        await self.session.submit(text)
        await asyncio.wait_for(self.session._generation, 2)

    async def settle_title(self):
        task = self.runtime.controller._title_task
        if task is not None:
            await asyncio.wait_for(task, 2)

    def blocking_suggest(self, title='Debugging a SQLite lock'):
        """A title request that is entered once and returns only when released."""
        started, release = asyncio.Event(), asyncio.Event()
        closed = asyncio.Event()

        async def suggest(*args, **kwargs):
            started.set()
            try:
                await release.wait()
                return title
            finally:
                closed.set()

        self.runtime.controller.backend.suggest_title = AsyncMock(side_effect=suggest)
        return started, release, closed

    async def test_cancel_title_awaits_the_generation_and_is_idempotent(self):
        c = self.runtime.controller
        started, release, closed = self.blocking_suggest()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        task = c._title_task
        await c.cancel_title()
        # The awaited task has run the generator's finally before cancel_title returns.
        self.assertTrue(closed.is_set())
        self.assertTrue(task.cancelled())
        self.assertIsNone(c._title_task)
        self.assertEqual(c.conversation.title_source, TitleSource.PLACEHOLDER)
        self.assertEqual((await c._repo.get_conversation(c.conversation.id)).title, DEFAULT_TITLE)
        # A second cancel settles nothing and raises nothing.
        await c.cancel_title()
        self.assertIsNone(c._title_task)

    async def test_a_named_conversation_is_never_scheduled(self):
        c = self.runtime.controller
        await c.new_conversation()
        await c.rename_conversation(c.conversation.id, 'Named by hand')
        c._title_task = None
        c.schedule_title(c.conversation,
                         SimpleNamespace(content='a question', attachments=[]),
                         SimpleNamespace(content='an answer'))
        self.assertIsNone(c._title_task)

    async def test_a_stale_placeholder_object_cannot_name_a_renamed_conversation(self):
        c = self.runtime.controller
        c.backend.suggest_title = AsyncMock(return_value='Automatic name')
        await c.new_conversation()
        stale = c.conversation
        await c.rename_conversation(stale.id, 'Manual name')
        # The object still says PLACEHOLDER, as a task scheduled before the rename would.
        self.assertTrue(stale.is_unnamed)
        await c._name_conversation(stale, SimpleNamespace(content='a question', attachments=[]),
                                   SimpleNamespace(content='an answer'), c.model_name, None)
        after = await c._repo.get_conversation(stale.id)
        self.assertEqual((after.title, after.title_source), ('Manual name', TitleSource.MANUAL))
        # The re-read guard returns before the provider request is made.
        c.backend.suggest_title.assert_not_awaited()

    async def test_an_inflight_title_cannot_replace_a_manual_title(self):
        c = self.runtime.controller
        await c.new_conversation()
        conversation = c.conversation
        started, release = asyncio.Event(), asyncio.Event()

        async def suggest(*args, **kwargs):
            started.set()
            await release.wait()
            return 'Automatic name'

        c.backend.suggest_title = AsyncMock(side_effect=suggest)
        reported = []

        async def on_title():
            reported.append(True)

        c.schedule_title(conversation, SimpleNamespace(content='a question about locks',
                                                       attachments=[]),
                         SimpleNamespace(content='an answer'), on_title)
        await asyncio.wait_for(started.wait(), 2)
        await c.rename_conversation(conversation.id, 'Manual name')
        release.set()
        await asyncio.wait_for(c._title_task, 2)
        after = await c._repo.get_conversation(conversation.id)
        self.assertEqual((after.title, after.title_source), ('Manual name', TitleSource.MANUAL))
        self.assertEqual(c.selected_conversation.title, 'Manual name')
        self.assertEqual(reported, [])

    async def test_a_late_title_cannot_resurrect_a_deleted_conversation(self):
        c = self.runtime.controller
        started, release, _ = self.blocking_suggest()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        deleted = c.conversation.id
        await c.delete_current_conversation()
        release.set()
        await self.settle_title()
        self.assertIsNone(await c._repo.get_conversation(deleted))
        self.assertNotEqual(c.conversation.id, deleted)
        self.assertEqual(c.conversation.title, DEFAULT_TITLE)

    async def test_naming_uses_the_model_captured_when_the_title_was_scheduled(self):
        c = self.runtime.controller
        c.backend.suggest_title = AsyncMock(return_value='Captured at scheduling')
        await c.new_conversation()
        c.schedule_title(c.conversation, SimpleNamespace(content='a question', attachments=[]),
                         SimpleNamespace(content='an answer'))
        task = c._title_task
        # Only select_model() is a supported switch and it cancels first; a raw
        # assignment models the narrow window the capture exists for.
        selected = c.model_name
        c.model_name = 'another-local-model'
        await asyncio.wait_for(task, 2)
        self.assertEqual(c.backend.suggest_title.call_args.args[0], selected)
        self.assertEqual((await c._repo.get_conversation(c.conversation.id)).title_source,
                         TitleSource.AUTO)

    async def test_a_second_schedule_abandons_the_first_task_uncancelled(self):
        """H2: ``schedule_title`` overwrites the pointer without cancelling the old task."""
        c = self.runtime.controller
        started, release, _ = self.blocking_suggest('Automatic name')
        request = SimpleNamespace(content='a question about locks', attachments=[])
        answer = SimpleNamespace(content='an answer')
        await c.new_conversation()
        c.schedule_title(c.conversation, request, answer)
        first = c._title_task
        await asyncio.wait_for(started.wait(), 2)
        c.schedule_title(c.conversation, request, answer)
        second = c._title_task
        self.assertIsNot(second, first)
        self.assertFalse(first.cancelled())
        self.assertFalse(first.done())
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        # The abandoned task still completes and still writes the title.
        self.assertTrue(first.done())
        self.assertEqual((await c._repo.get_conversation(c.conversation.id)).title,
                         'Automatic name')

    async def test_an_unnameable_first_exchange_waits_for_a_later_nameable_turn(self):
        """An unnameable first exchange stays a placeholder instead of writing it as auto."""
        c = self.runtime.controller
        c.backend.suggest_title = AsyncMock(return_value=None)
        self.assertEqual(derive_title('---'), DEFAULT_TITLE)
        await self.send('---')
        await self.settle_title()
        after = await c._repo.get_conversation(c.conversation.id)
        self.assertEqual((after.title, after.title_source), (DEFAULT_TITLE, TitleSource.PLACEHOLDER))
        self.assertTrue(after.is_unnamed)
        # No paid request was spent on an exchange that cannot name the chat.
        self.assertEqual(c.backend.suggest_title.await_count, 0)
        await self.send('A later question that could have been named')
        await self.settle_title()
        self.assertEqual(c.backend.suggest_title.await_count, 1)
        named = await c._repo.get_conversation(c.conversation.id)
        self.assertEqual(named.title, derive_title('A later question that could have been named'))
        self.assertEqual(named.title_source, TitleSource.AUTO)

    async def test_a_nameable_request_is_named_from_the_fallback_when_generation_fails(self):
        c = self.runtime.controller
        c.backend.suggest_title = AsyncMock(side_effect=RuntimeError('Unavailable'))
        await self.send('Help with my SQLite database')
        await self.settle_title()
        after = await c._repo.get_conversation(c.conversation.id)
        self.assertEqual(after.title, derive_title('Help with my SQLite database'))
        self.assertEqual(after.title_source, TitleSource.AUTO)
        # Only an unnameable exchange keeps the placeholder; a named chat is not retried.
        await self.send('A follow-up question')
        await self.settle_title()
        self.assertEqual(c.backend.suggest_title.await_count, 1)

    async def test_renaming_a_deleted_conversation_reports_success_without_a_row(self):
        """H4: the controller returns the requested title even when no row changed."""
        c = self.runtime.controller
        await c.new_conversation()
        gone = await c._repo.create_conversation()
        await c._repo.delete_conversation(gone.id)
        self.assertEqual(await c.rename_conversation(gone.id, 'A name'), 'A name')
        self.assertIsNone(await c._repo.get_conversation(gone.id))
        self.assertNotEqual(c.selected_conversation.id, gone.id)

    async def test_close_cancels_the_title_before_the_session_is_torn_down(self):
        c = self.runtime.controller
        started, _, closed = self.blocking_suggest()
        await self.send()
        await asyncio.wait_for(started.wait(), 2)
        task = c._title_task
        await self.session.close()
        self.assertTrue(closed.is_set())
        self.assertTrue(task.cancelled())
        self.assertIsNone(c._title_task)


class TitlePayloadTests(unittest.IsolatedAsyncioTestCase):
    """What leaves the process, and what is refused before it can be stored."""

    class Backend:
        def __init__(self, *chunks):
            self.chunks = chunks
            self.seen = None
            self.closed = False

        async def stream(self, model, messages, tools, **options):
            self.seen = (model, messages, tools, options)
            try:
                for chunk in self.chunks:
                    yield chunk
            finally:
                self.closed = True

    async def test_the_title_payload_is_bounded_and_excludes_document_and_prompt_context(self):
        backend = self.Backend({'delta': {'content': 'Reviewing research notes'},
                                'finish_reason': 'stop'})
        request = SimpleNamespace(content='detail ' * 4000, attachments=[
            SimpleNamespace(filename='PRIVATE_NAME_' + 'x' * 400) for _ in range(6)])
        answer = SimpleNamespace(content='summary ' * 4000)
        messages = title_messages(request, answer)
        payload = messages[1]
        self.assertEqual(messages[0]['content'], TITLE_INSTRUCTIONS)
        body = json.loads(payload['content'])
        self.assertEqual(len(body['request']), 1600)
        self.assertEqual(len(body['reply']), 1000)
        self.assertEqual(len(body['attachments']), 4)
        self.assertTrue(all(len(name) <= 180 for name in body['attachments']))
        title = await generate_title(backend, 'local-model', messages, session_id='chat-1')
        self.assertEqual(title, 'Reviewing research notes')
        model, sent, tools, options = backend.seen
        self.assertEqual(model, 'local-model')
        self.assertEqual(sent, messages)
        # No tool schemas, no extra transport options, and the stream is closed early.
        self.assertEqual(tools, ())
        self.assertEqual(options, {'session_id': 'chat-1'})
        self.assertTrue(backend.closed)

    async def test_a_tool_call_or_non_stop_finish_yields_no_title(self):
        for chunks, expected in [
            (({'delta': {'tool_calls': [{'id': 'call'}]}},), None),
            (({'delta': {'content': 'Debugging locks'}, 'finish_reason': 'length'},), None),
            (({'delta': {'content': 'Debugging locks'}},), None),  # stream ended without stop
            (({'delta': {'content': 'Debugging locks'}, 'finish_reason': 'stop'},),
             'Debugging locks'),
        ]:
            with self.subTest(chunks=chunks):
                backend = self.Backend(*chunks)
                self.assertEqual(await generate_title(backend, 'm', []), expected)
                self.assertTrue(backend.closed)

    def test_reasoning_and_multiline_answers_are_not_titles(self):
        for text in ('<think>', 'Title\nExplanation', ''):
            with self.subTest(text=text):
                self.assertIsNone(clean_title(text))


if __name__ == '__main__':
    unittest.main()
