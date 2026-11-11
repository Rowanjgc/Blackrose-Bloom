"""Bounded transcript windows, earlier-history pages and batched evidence reads."""
import unittest
from unittest.mock import AsyncMock, patch

from blackrose.storage import repository
from blackrose.storage.repository import NewAttachment
from blackrose.ui_documents import UIEnvelope
import test_gui_images as fixtures
from test_turns import Backend
from test_ui_documents import source


class TranscriptHistoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown
    latest = fixtures.ImageTests.latest

    async def seed(self, turns, *, images=(), evidence=()):
        """Alternating saved turns; selected turns carry an image or saved sources."""
        c = self.runtime.controller
        repo = c._repo
        for turn in range(turns):
            attachments = []
            if turn in images:
                blob = c._store.write(fixtures.png(), '.png')
                attachments = [NewAttachment('image/png', f'turn-{turn}.png', blob.relative_path,
                                             blob.digest, blob.size, 320, 200)]
            await repo.add_message(c.conversation.id, 'user', f'Question {turn}', attachments=attachments)
            reply = await repo.add_message(c.conversation.id, 'assistant', f'Answer {turn}')
            if turn in evidence:
                await repo.save_evidence(reply.id, [{'title': f'source {turn}'}])

    async def all_history(self, *, render=True):
        if render:
            await self.session.render_transcript()
        page = self.latest('transcript')
        history, earlier = list(page['messages']), page['earlier']
        pages = 0
        while earlier:
            page = await self.session.earlier_messages(
                self.runtime.controller.conversation.id, history[0]['id'])
            history[:0], earlier = page['messages'], page['earlier']
            pages += 1
        return history, pages

    async def test_one_evidence_read_for_every_rendered_reply(self):
        await self.seed(30, evidence={3, 17})
        repo = self.runtime.controller._repo
        batch = AsyncMock(wraps=repo.evidence_for_messages)
        with patch.object(repo, 'evidence_for', AsyncMock(side_effect=AssertionError('per-message read'))), \
                patch.object(repo, 'evidence_for_messages', batch):
            await self.session.render_transcript()
        batch.assert_awaited_once()
        messages = self.latest('transcript')['messages']
        replies = [m for m in messages if m['role'] == 'assistant']
        self.assertEqual(len(replies), 30)
        self.assertEqual(sorted(batch.await_args.args[0]), sorted(m['id'] for m in replies))
        self.assertEqual(replies[3]['sources'], [{'title': 'source 3'}])
        self.assertEqual(replies[17]['sources'], [{'title': 'source 17'}])
        self.assertEqual(replies[4]['sources'], [])

    async def test_storage_round_trips_do_not_grow_with_history(self):
        database = self.runtime.controller._repo._db
        counts = []
        for turns in (5, 60):
            await self.runtime.controller.new_conversation()
            await self.seed(turns, evidence=set(range(turns)))
            calls = AsyncMock(wraps=database.run)
            with patch.object(database, 'run', calls):
                await self.session.render_transcript()
            counts.append(calls.await_count)
        self.assertEqual(counts[0], counts[1])

    async def test_large_transcript_loads_only_a_page_of_message_details(self):
        c = self.runtime.controller
        repo = c._repo

        def seed(conn):
            conn.executemany('INSERT INTO messages(conversation_id, role, content, created_at) '
                             'VALUES (?, ?, ?, ?)',
                [(c.conversation.id, 'user' if i % 2 == 0 else 'assistant', f'Message {i}',
                  '2026-11-03T00:00:00+00:00') for i in range(10000)])
            conn.commit()
        await repo._db.run(seed)
        for operation in (self.session.render_transcript, self.session.redraw):
            with patch.object(repo, 'messages', side_effect=AssertionError('full history read')), \
                    patch.object(repository, '_message', wraps=repository._message) as loaded:
                await operation()
            self.assertLessEqual(loaded.call_count, self.session.history_window + 2)
        snapshot = self.latest('snapshot')
        self.assertEqual(len(snapshot['messages']), 100)
        self.assertEqual(snapshot['messages'][0]['text'], 'Message 9900')
        self.assertTrue(snapshot['earlier'])
        with patch.object(repo, 'messages', side_effect=AssertionError('full history read')), \
                patch.object(repository, '_message', wraps=repository._message) as loaded:
            older = await self.session.earlier_messages(c.conversation.id, snapshot['messages'][0]['id'])
        self.assertLessEqual(loaded.call_count, self.session.history_window + 2)
        self.assertEqual(older['messages'][0]['text'], 'Message 9800')
        self.assertTrue(older['earlier'])

    async def test_summary_preserves_metadata_outside_the_window(self):
        c = self.runtime.controller
        user = await c._repo.add_message(c.conversation.id, 'user', 'Original request')
        await c._repo.add_message(c.conversation.id, 'assistant', 'Answer', model_used='earlier-model')
        for i in range(8):
            await c._repo.add_message(c.conversation.id, 'user', f'Later request {i}')
        self.session.history_window = 2
        await self.session.render_transcript()
        self.assertEqual(self.session._response_model, 'earlier-model')
        self.assertTrue(self.session._can_retry)
        self.assertEqual(self.session._request_state, 'complete')
        self.assertTrue(await c.request_was_stored(c.conversation.id, 0, user.content, 100))
        self.assertFalse(await c.request_was_stored(c.conversation.id, user.id, user.content, 100))

    async def test_active_reply_filter_fills_page_without_hiding_summary(self):
        await self.seed(5)
        c = self.runtime.controller
        saved = await c.current_messages()
        baseline = saved[-1].id
        user, reply = await c._repo.begin_turn(c.conversation.id, 'Live request', 'live-model')
        page = await c.conversation_message_page(c.conversation.id, limit=4, hide_assistant_after=baseline)
        # The extra predecessor keeps the existing request/reply boundary rule.
        self.assertEqual([m.id for m in page.items], [m.id for m in saved[-4:]] + [user.id])
        self.assertTrue(page.more)
        self.assertEqual(page.summary.last_id, reply.id)
        self.assertEqual(page.summary.last_user_id, user.id)
        self.assertEqual(page.summary.latest_assistant.model_used, 'live-model')

    async def test_batched_evidence_query(self):
        await self.seed(3, evidence={0, 2})
        repo = self.runtime.controller._repo
        replies = [m.id for m in await self.runtime.controller.current_messages() if m.role == 'assistant']
        statements = []
        self.runtime.database.connection.set_trace_callback(statements.append)
        try:
            found = await repo.evidence_for_messages(replies + [replies[0], 999999])
        finally:
            self.runtime.database.connection.set_trace_callback(None)
        self.assertEqual(len(statements), 1)
        self.assertEqual(found, {replies[0]: [{'title': 'source 0'}], replies[1]: [],
                                 replies[2]: [{'title': 'source 2'}], 999999: []})
        self.assertEqual(await repo.evidence_for_messages([]), {})

    async def test_initial_window_is_latest_and_earlier_pages_reach_all_history(self):
        self.session.history_window = 6
        await self.seed(10, images={0, 9}, evidence={1})
        await self.session.render_transcript()
        window = self.latest('transcript')
        self.assertTrue(window['earlier'])
        self.assertEqual(window['messages'][0]['text'], 'Question 7')
        self.assertIn('Answer 7', window['messages'][1]['html'])
        self.assertEqual(len(window['messages']), 6)
        history, pages = await self.all_history()
        saved = await self.runtime.controller.current_messages()
        self.assertEqual([m['id'] for m in history], [m.id for m in saved])
        self.assertEqual(pages, 3)
        self.assertEqual(history[3]['sources'], [{'title': 'source 1'}])
        self.assertEqual([a['filename'] for m in history for a in m['attachments']],
                         ['turn-0.png', 'turn-9.png'])
        # Older previews join the displayed set without revoking the window's.
        for message in (history[0], history[-2]):
            preview = message['attachments'][0]['preview_id']
            self.assertTrue((await self.session.image_preview(preview))['src'].startswith('data:image/png'))

    async def test_a_window_never_starts_with_an_orphaned_reply(self):
        self.session.history_window = 5
        await self.seed(4)
        await self.session.render_transcript()
        window = self.latest('transcript')['messages']
        self.assertEqual(window[0]['role'], 'user')
        self.assertEqual(len(window), 6)
        page = await self.session.earlier_messages(self.runtime.controller.conversation.id, window[0]['id'])
        self.assertEqual([m['role'] for m in page['messages']], ['user', 'assistant'])
        self.assertFalse(page['earlier'])

    async def test_short_conversations_have_no_earlier_control(self):
        await self.seed(2)
        await self.session.render_transcript()
        self.assertFalse(self.latest('transcript')['earlier'])
        await self.session.redraw()
        snapshot = self.latest('snapshot')
        self.assertFalse(snapshot['earlier'])
        self.assertEqual(len(snapshot['messages']), 4)

    async def test_reload_snapshot_is_bounded_too(self):
        self.session.history_window = 4
        await self.seed(5)
        await self.session.redraw()
        snapshot = self.latest('snapshot')
        self.assertTrue(snapshot['earlier'])
        self.assertEqual([m['text'] for m in snapshot['messages'] if m['role'] == 'user'],
                         ['Question 3', 'Question 4'])

    async def test_earlier_page_for_another_conversation_is_refused(self):
        self.session.history_window = 2
        await self.seed(3, images={0})
        c = self.runtime.controller
        first = c.conversation
        self.assertIsNone(await self.session.earlier_messages(first.id + 1, 10 ** 9))
        # A switch while the page is read neither returns nor registers it.
        repo = c._repo
        real = repo.evidence_for_messages
        second = await repo.create_conversation('Other')

        async def switch(ids):
            c.conversation = second
            return await real(ids)
        self.session._preview_images.clear()
        with patch.object(repo, 'evidence_for_messages', switch):
            self.assertIsNone(await self.session.earlier_messages(first.id, 10 ** 9))
        self.assertEqual(self.session._preview_images, {})

    async def test_earlier_interactive_blocks_keep_action_identity(self):
        c = self.runtime.controller
        c.config.interactive_ui_enabled = True
        c.config.interactive_ui_actions_enabled = True
        c.backend = Backend([[('activate_tools', {'capability': 'interactive_ui', 'profile': 'form'})], source()])
        await c.send('Collect my reading preferences.')
        self.session.history_window = 2
        await self.seed(2)
        await self.session.render_transcript()
        window = self.latest('transcript')
        self.assertTrue(all('blocks' not in m for m in window['messages']))
        views = dict(self.session._ui_views)
        history, _ = await self.all_history(render=False)
        block = next(b for m in history for b in m.get('blocks', ()) if b['kind'] == 'interactive_ui')
        envelope = UIEnvelope.parse({'conversation_id': block['conversation_id'],
                                     'assistant_message_id': block['message_id'],
                                     'block_id': block['block_id'], 'source_revision': 1,
                                     'view_id': block['view_id']})
        self.assertEqual(self.session._check_ui_view(envelope), c.conversation.id)
        self.assertLessEqual(views.items(), self.session._ui_views.items())
        # A replaced transcript retires views it no longer displays.
        await self.session.render_transcript()
        with self.assertRaises(ValueError):
            self.session._check_ui_view(envelope)


if __name__ == '__main__':
    unittest.main()
