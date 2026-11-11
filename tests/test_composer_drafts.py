"""Durable per-conversation composer drafts across switches, restarts and sends."""
from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blackrose.gui.session import Session
from test_gui_images import make_runtime, png


class ComposerDraftTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patch = patch('blackrose.gui.session.DRAFT_SAVE_SECONDS', 0)
        self.patch.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtimes, self.sessions = [], []
        self.runtime, self.session, self.events = await self.start()

    async def asyncTearDown(self):
        for session in reversed(self.sessions):
            await session.close()
        for runtime in reversed(self.runtimes):
            await runtime.close()
        self.patch.stop()
        self.temp.cleanup()

    async def start(self):
        """A fresh controller, repository and session on the same files."""
        _, runtime = make_runtime(self.root)
        await runtime.open()
        events = []
        session = Session(runtime.controller, lambda kind, payload: events.append((kind, payload)))
        self.runtimes.append(runtime)
        self.sessions.append(session)
        await session.boot()
        return runtime, session, events

    async def saved(self, session=None):
        session = session or self.session
        for _ in range(3):
            await asyncio.sleep(0)
        while session._draft_save is not None or (
                session._draft_save_task is not None and not session._draft_save_task.done()):
            await asyncio.sleep(0.01)

    async def rows(self, table='composer_drafts', runtime=None):
        runtime = runtime or self.runtime
        return await runtime.database.run(lambda conn: [dict(r) for r in conn.execute(
            f'SELECT * FROM {table} ORDER BY conversation_id')])

    @staticmethod
    def latest(events, kind):
        return next((payload for name, payload in reversed(events) if name == kind), None)

    async def test_switching_conversations_restores_each_draft(self):
        first = self.runtime.controller.conversation.id
        await self.session.attach_bytes(b'First notes.', 'first.txt')
        self.session.update_draft('First draft')
        await self.session.new_conversation()
        second = self.runtime.controller.conversation.id
        self.assertEqual(self.latest(self.events, 'restore_draft')['text'], '')
        self.assertEqual(self.latest(self.events, 'attachments')['items'], [])
        self.assertEqual((self.session._draft_text, self.session._attachments), ('', []))
        self.session.update_draft('Second draft')
        await self.session.open_conversation(first)
        self.assertEqual(self.latest(self.events, 'restore_draft')['text'], 'First draft')
        self.assertEqual([a['filename'] for a in self.latest(self.events, 'attachments')['items']], ['first.txt'])
        await self.session.open_conversation(second)
        self.assertEqual(self.session._draft_text, 'Second draft')
        self.assertEqual(self.session._attachments, [])
        await self.saved()
        self.assertEqual([(r['conversation_id'], r['text']) for r in await self.rows()],
                         [(first, 'First draft'), (second, 'Second draft')])

    async def test_switch_and_close_save_typing_before_the_debounce(self):
        first = self.runtime.controller.conversation.id
        with patch('blackrose.gui.session.DRAFT_SAVE_SECONDS', 60):
            self.session.update_draft('Typed just before switching')
            await self.session.new_conversation()
            self.assertEqual([r['text'] for r in await self.rows()], ['Typed just before switching'])
            self.session.update_draft('Typed just before closing')
            await self.session.close()
        self.sessions.remove(self.session)
        rows = {r['conversation_id']: r['text'] for r in await self.rows()}
        self.assertEqual(rows, {first: 'Typed just before switching',
                                self.runtime.controller.conversation.id: 'Typed just before closing'})

    async def test_restart_restores_text_and_attachments(self):
        await self.session.attach_bytes(png(10, 10), 'diagram.png')
        await self.session.attach_bytes(b'Context notes.', 'notes.txt')
        self.session.update_draft('Unsent after restart')
        # A crash: no orderly close, only the debounced save has run.
        await self.saved()
        runtime, session, events = await self.start()
        self.assertEqual(self.latest(events, 'restore_draft')['text'], 'Unsent after restart')
        self.assertEqual([a.filename for a in session._attachments], ['diagram.png', 'notes.txt'])
        self.assertTrue(self.latest(events, 'attachments')['items'][0]['src'].startswith('data:image/png'))
        # Copies live in the chat database, not the content-addressed store.
        self.assertFalse((self.root / 'attachments').exists())
        self.assertIsNone(self.latest(events, 'notice'))

    async def test_missing_or_invalid_attachment_copies_are_dropped_with_notice(self):
        await self.session.attach_bytes(png(10, 10), 'gone.png')
        await self.session.attach_bytes(b'Still here.', 'kept.txt')
        await self.session.attach_bytes(png(12, 12), 'corrupt.png')
        self.session.update_draft('Keep the text')
        await self.saved()
        files = await self.rows('composer_draft_files')
        entries = {e['filename']: e['digest'] for e in json.loads((await self.rows())[0]['attachments'])}

        def damage(conn):
            with conn:
                conn.execute('DELETE FROM composer_draft_files WHERE digest=?', (entries['gone.png'],))
                conn.execute('UPDATE composer_draft_files SET data=? WHERE digest=?',
                             (b'\x89PNG broken', entries['corrupt.png']))
        await self.runtime.database.run(damage)
        self.assertEqual(len(files), 3)
        runtime, session, events = await self.start()
        self.assertEqual(session._draft_text, 'Keep the text')
        self.assertEqual([a.filename for a in session._attachments], ['kept.txt'])
        notice = self.latest(events, 'notice')
        self.assertIn('gone.png', notice['html'])
        self.assertIn('corrupt.png', notice['html'])
        self.assertEqual(notice['tone'], 'warning')
        await self.saved(session)
        self.assertEqual([e['filename'] for e in json.loads((await self.rows(runtime=runtime))[0]['attachments'])],
                         ['kept.txt'])
        self.assertEqual(len(await self.rows('composer_draft_files', runtime)), 1)

    async def test_workspace_references_are_revalidated_on_restore(self):
        c = self.runtime.controller
        work, other = self.root / 'work', self.root / 'other'
        work.mkdir()
        other.mkdir()
        (work / 'notes.md').write_text('notes')
        await c.set_workspace(str(work))
        meta = next(m for m in await c.reference_suggestions('@', 'notes') if m['filename'] == 'notes.md')
        ref = dict(meta, token='@{' + meta['path'] + '}')
        text = f'Summarize {ref["token"]}'
        self.session.update_draft(text, [ref])
        await self.saved()
        _, session, events = await self.start()
        self.assertEqual([r['token'] for r in session._draft_refs], [ref['token']])
        self.assertEqual(self.latest(events, 'restore_draft')['refs'][0]['token'], ref['token'])
        await session.close()
        self.sessions.remove(session)
        # The conversation's workspace changes before the next launch.
        await c.set_workspace(str(other))
        runtime, session, events = await self.start()
        self.assertEqual(session._draft_text, text)
        self.assertEqual(session._draft_refs, [])
        self.assertIn('notes.md', self.latest(events, 'notice')['html'])
        await self.saved(session)
        self.assertEqual(json.loads((await self.rows(runtime=runtime))[0]['refs']), [])

    async def test_deleting_a_conversation_deletes_its_draft(self):
        c = self.runtime.controller
        await self.session.new_conversation()
        doomed = c.conversation.id
        await self.session.attach_bytes(b'Secret notes.', 'secret.txt')
        self.session.update_draft('Sensitive unsent text')
        await self.saved()
        self.assertEqual(len(await self.rows('composer_draft_files')), 1)
        self.assertTrue(await self.session.delete_conversation(doomed))
        await self.saved()
        self.assertNotEqual(c.conversation.id, doomed)
        self.assertEqual(self.latest(self.events, 'restore_draft')['text'], '')
        self.assertEqual((self.session._draft_text, self.session._attachments), ('', []))
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.rows('composer_draft_files'), [])

    async def admit_and_type(self):
        release = asyncio.Event()
        original = self.runtime.controller.backend.stream

        async def stream(model, messages, tools):
            await release.wait()
            async for chunk in original(model, messages, tools):
                yield chunk

        self.runtime.controller.backend.stream = stream
        await self.session.attach_bytes(png(10, 10), 'sent.png')
        self.session.update_draft('Sent question')
        await self.session.submit('Sent question')
        while not self.runtime.controller.last_request_stored:
            await asyncio.sleep(0.01)
        self.session.update_draft('Typed during the reply')
        await self.saved()
        return release

    async def test_crash_after_admission_does_not_restore_the_stored_request(self):
        release = await self.admit_and_type()
        self.assertIsNotNone((await self.rows())[0]['sending'])
        _, session, _ = await self.start()
        self.assertEqual(session._draft_text, 'Typed during the reply')
        self.assertEqual(session._attachments, [])
        self.assertEqual([m.content for m in await session.controller.current_messages()
                          if m.role == 'user'], ['Sent question'])
        release.set()
        await self.session._generation

    async def test_admitted_send_clears_only_the_sent_text(self):
        release = await self.admit_and_type()
        release.set()
        await self.session._generation
        await self.saved()
        row, = await self.rows()
        self.assertEqual((row['text'], json.loads(row['attachments']), row['sending']),
                         ('Typed during the reply', [], None))
        self.assertEqual(await self.rows('composer_draft_files'), [])

    async def test_rejected_send_keeps_request_and_later_typing(self):
        from test_turns import Backend
        self.runtime.controller.backend = Backend(context=4096)
        await self.session.attach_bytes(b'Attached notes.', 'notes.txt')
        await self.session.submit('x' * 10000)
        self.session.update_draft('Later typing')
        await self.session._generation
        await self.saved()
        row, = await self.rows()
        self.assertEqual(row['text'], 'x' * 10000 + '\n\nLater typing')
        self.assertEqual([e['filename'] for e in json.loads(row['attachments'])], ['notes.txt'])
        self.assertIsNone(row['sending'])

    async def test_crash_before_admission_restores_the_submitted_request(self):
        started = asyncio.Event()

        async def load(model):
            started.set()
            await asyncio.Event().wait()

        self.runtime.controller.backend.load = load
        await self.session.attach_bytes(b'Request notes.', 'request.txt')
        await self.session.submit('Never admitted')
        await started.wait()
        self.session.update_draft('Typed while waiting')
        await self.saved()
        self.assertIsNotNone((await self.rows())[0]['sending'])
        _, session, events = await self.start()
        self.assertEqual(self.latest(events, 'restore_draft')['text'], 'Never admitted\n\nTyped while waiting')
        self.assertEqual([a.filename for a in session._attachments], ['request.txt'])
        self.assertEqual(await session.controller.current_messages(), [])
        with contextlib.suppress(asyncio.CancelledError):
            self.session.stop()
            await self.session._generation

    async def test_slash_command_text_is_not_saved_as_the_previous_draft(self):
        first = self.runtime.controller.conversation.id
        self.session.update_draft('/new')
        await self.session.submit('/new')
        await self.saved()
        self.assertNotEqual(self.runtime.controller.conversation.id, first)
        self.assertEqual(await self.rows(), [])


if __name__ == '__main__':
    unittest.main()
