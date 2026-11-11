"""Viewing saved chats must not change a running turn's ownership."""
from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blackrose.gui.session import Session
from test_gui_images import make_runtime, png


class ChatNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.runtime = make_runtime(self.root)
        await self.runtime.open()
        self.c = self.runtime.controller
        self.events = []
        self.s = Session(self.c, lambda k, p: self.events.append((k, p)))
        await self.s.boot()
        await self.s.submit('Saved B question')
        await self.s._generation
        self.b = self.c.conversation.id
        self.b_messages = await self.c.current_messages()
        self.s.update_draft('B draft')
        await self.s.new_conversation()
        self.a = self.c.conversation.id

    async def asyncTearDown(self):
        await self.s.close()
        await self.runtime.close()
        self.temp.cleanup()

    def latest(self, kind):
        return next(p for k, p in reversed(self.events) if k == kind)

    async def start_stream(self, *, fail=False):
        entered, advance, advanced, finish = (asyncio.Event() for _ in range(4))

        async def stream(*args, **kwargs):
            yield {'delta': {'content': 'Partial A', 'reasoning_content': 'A reasoning'}}
            entered.set()
            await advance.wait()
            yield {'delta': {'content': ' continued'},
                   '_usage': {'prompt_tokens': 1200, 'completion_tokens': 24}}
            advanced.set()
            await finish.wait()
            if fail:
                raise RuntimeError('A provider failed')
            yield {'delta': {'content': ' finished'}, 'finish_reason': 'stop'}

        self.c.backend.stream = stream
        await self.s.submit('A question')
        await asyncio.wait_for(entered.wait(), 3)
        return advance, advanced, finish

    async def test_switch_back_and_forth_replays_live_answer_once(self):
        advance, advanced, finish = await self.start_stream()
        generation = self.s._generation
        self.s.update_draft('A follow-up')
        for _ in range(2):
            self.assertTrue(await self.s.open_conversation(self.b))
            snapshot = self.latest('snapshot')
            self.assertIsNone(snapshot['active'])
            self.assertEqual(snapshot['draft'], 'B draft')
            self.assertEqual([m['id'] for m in snapshot['messages']], [m.id for m in self.b_messages])
            self.assertTrue(snapshot['status']['generating'])
            self.assertFalse(snapshot['status']['can_retry'])
            self.assertEqual(await self.s.submit('No concurrent reply'), {'clear': False})
            self.assertTrue(await self.s.open_conversation(self.a))
            snapshot = self.latest('snapshot')
            self.assertEqual([m['role'] for m in snapshot['messages']], ['user'])
            self.assertIsNone(snapshot['active']['user'])
            self.assertIn('Partial A', snapshot['active']['html'])
            self.assertEqual(snapshot['active']['reasoning'], 'A reasoning')
            self.assertEqual(snapshot['draft'], 'A follow-up')
            self.assertIs(self.s._generation, generation)
        advance.set()
        await asyncio.wait_for(advanced.wait(), 3)
        finish.set()
        await generation
        self.assertEqual(self.latest('reply_end')['conversation_id'], self.a)
        await self.s.redraw()
        self.assertIsNone(self.latest('snapshot')['active'])
        self.assertEqual(len(self.latest('snapshot')['messages']), 2)
        self.assertIn('Partial A continued finished', self.latest('snapshot')['messages'][-1]['html'])

    async def test_background_completion_and_error_do_not_change_visible_chat(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                advance, advanced, finish = await self.start_stream(fail=fail)
                generation = self.s._generation
                await self.s.open_conversation(self.b)
                self.s.update_draft('B remains editable')
                boundary = len(self.events)
                advance.set()
                await asyncio.wait_for(advanced.wait(), 3)
                finish.set()
                await generation
                self.assertEqual(self.c.conversation.id, self.b)
                self.assertEqual(self.s._draft_text, 'B remains editable')
                self.assertEqual(await self.c.current_messages(), self.b_messages)
                self.assertFalse(any(k in ('reply_update', 'reply_end', 'reasoning', 'metrics',
                                          'notice', 'restore_draft', 'snapshot', 'transcript')
                                     for k, _ in self.events[boundary:]))
                self.assertTrue(all(p['current'] == self.b for k, p in self.events[boundary:]
                                    if k == 'conversations'))
                self.assertFalse(self.latest('status')['generating'])
                self.assertEqual(self.latest('status')['request_state'], 'complete')  # B's outcome
                await self.s.open_conversation(self.a)
                reply = self.latest('snapshot')['messages'][-1]
                self.assertEqual(reply['state'], 'error' if fail else 'complete')
                self.assertIn('Partial A continued', reply['html'])
                if fail:
                    self.assertIn('A provider failed', reply['error'])
                await self.s.new_conversation()
                self.a = self.c.conversation.id

    async def test_pre_admission_failure_recovers_only_origin_draft_and_attachments(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def load(model):
            entered.set()
            await release.wait()
            raise RuntimeError('Load failed for A')

        self.c.backend.load = load
        await self.s.attach_bytes(png(10, 10), 'A.png')
        await self.s.submit('A unsaved question')
        await asyncio.wait_for(entered.wait(), 3)
        generation = self.s._generation
        await self.s.open_conversation(self.b)
        await self.s.open_conversation(self.a)
        self.assertEqual(self.latest('snapshot')['draft'], '')
        self.assertEqual(self.latest('snapshot')['active']['user']['text'], 'A unsaved question')
        await self.s.open_conversation(self.b)
        release.set()
        await generation
        self.assertEqual(self.s._draft_text, 'B draft')
        self.assertEqual(self.s._attachments, [])
        await self.s.open_conversation(self.a)
        snapshot = self.latest('snapshot')
        self.assertEqual(snapshot['draft'], 'A unsaved question')
        self.assertEqual(snapshot['attachments']['items'][0]['filename'], 'A.png')
        self.assertIn('Load failed for A', snapshot['request_notice'])
        self.assertEqual(snapshot['status']['request_state'], 'error')

    async def test_tools_and_child_tasks_keep_origin_workspace_and_privacy(self):
        for owner, name in ((self.b, 'B'), (self.a, 'A')):
            await self.s.open_conversation(owner)
            directory = self.root / name
            directory.mkdir()
            (directory / 'scope.txt').write_text(name)
            await self.c.set_workspace(str(directory))
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []

        async def inspect():
            files = await self.c.workspace_files()
            observed.append((self.c.conversation.id, files.workspace['path']))
            await self.c.expose_local_data()

        async def stream(*args, **kwargs):
            yield {'delta': {'content': 'Started'}}
            entered.set()
            await release.wait()
            await asyncio.create_task(inspect())
            yield {'delta': {'content': ' done'}, 'finish_reason': 'stop'}

        self.c.backend.stream = stream
        await self.s.submit('Use the origin workspace')
        await asyncio.wait_for(entered.wait(), 3)
        generation = self.s._generation
        await self.s.open_conversation(self.b)
        release.set()
        await generation
        self.assertEqual(observed, [(self.a, str(self.root / 'A'))])
        self.assertTrue(await self.c._repo.local_data_exposed(self.a))
        self.assertFalse(await self.c._repo.local_data_exposed(self.b))
        self.assertEqual(self.c.conversation.id, self.b)

    async def test_stop_from_other_chat_settles_origin_without_navigation(self):
        await self.start_stream()
        generation = self.s._generation
        await self.s.open_conversation(self.b)
        self.assertTrue(self.s.stop())
        with contextlib.suppress(asyncio.CancelledError):
            await generation
        self.assertEqual(self.c.conversation.id, self.b)
        self.assertEqual(self.s._draft_text, 'B draft')
        await self.s.open_conversation(self.a)
        self.assertEqual(self.latest('snapshot')['messages'][-1]['state'], 'cancelled')

    async def test_delayed_sidebar_refresh_cannot_restore_previous_selection(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.c.conversation_page

        async def page(**kwargs):
            result = await original(**kwargs)
            if kwargs.get('including') == self.a:
                entered.set()
                await release.wait()
            return result

        with patch.object(self.c, 'conversation_page', page):
            refresh = asyncio.create_task(self.s.refresh_conversations())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await self.s.open_conversation(self.b)
            finally:
                release.set()
            await refresh
        self.assertEqual(self.latest('conversations')['current'], self.b)

    async def test_rejection_during_draft_validation_restores_sending_envelope(self):
        loading, fail = asyncio.Event(), asyncio.Event()
        validating, release = asyncio.Event(), asyncio.Event()

        async def load(model):
            loading.set()
            await fail.wait()
            raise RuntimeError('Rejected during navigation')

        self.c.backend.load = load
        await self.s.submit('Recover this prompt')
        await asyncio.wait_for(loading.wait(), 3)
        generation = self.s._generation
        await self.s.open_conversation(self.b)
        original = self.s._valid_draft_refs

        async def validate(*args):
            validating.set()
            await release.wait()
            return await original(*args)

        with patch.object(self.s, '_valid_draft_refs', validate):
            navigation = asyncio.create_task(self.s.open_conversation(self.a))
            try:
                await asyncio.wait_for(validating.wait(), 3)
                fail.set()
                await generation
            finally:
                release.set()
            await navigation
        self.assertEqual(self.latest('snapshot')['draft'], 'Recover this prompt')
        self.assertIn('Rejected during navigation', self.latest('snapshot')['request_notice'])
