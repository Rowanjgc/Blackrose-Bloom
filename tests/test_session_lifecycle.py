"""Draft ownership, concurrent imports, and session replay with scripted providers."""
from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from blackrose.gui.bridge import coalesce
from blackrose.gui.session import Session
from blackrose.attachments import documents
from test_gui_images import make_runtime, png


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from blackrose.storage.records import MessageState
        from blackrose.ui_documents import UIStream, contract

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.runtime = make_runtime(self.root)
        await self.runtime.open()
        c = self.runtime.controller
        await c.ensure_conversation()
        _, original = await c._repo.begin_turn(c.conversation.id, 'Show forms', c.model_name)
        stream = UIStream(original)
        stream.begin('form')
        source = json.dumps(contract()['examples']['form']) + '\n'
        stream.append(source * 2)
        stream.end()
        await c._repo.save_blocks(original.id, stream.blocks)
        await c._repo.finish_message(original.id, '', MessageState.COMPLETE)
        # Exercise both accepted actions and actions with a persisted request.
        block_id = stream.blocks[0]['block_id']
        await c._repo.accept_ui_event(block_id, 'submit', 'started-event', {'values': {}})
        _, pending = await c._repo.begin_turn(c.conversation.id, 'Interrupted action', c.model_name,
                                              ui_event_id='started-event')
        await c._repo.accept_ui_event(stream.blocks[1]['block_id'], 'submit', 'accepted-event',
                                     {'values': {}})
        unfinished = UIStream(pending)
        unfinished.begin('form')
        unfinished.append(source)
        await c._repo.save_blocks(pending.id, unfinished.blocks)
        await self.runtime.close()

        # Reopen the same on-disk history with a fresh controller and session.
        _, self.runtime = make_runtime(self.root)
        await self.runtime.open()
        self.events = []
        self.session = Session(self.runtime.controller,
                               lambda kind, payload: self.events.append((kind, payload)))

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.temp.cleanup()

    async def assert_recovered_transcript(self):
        c = self.runtime.controller
        transcript = next(p for k, p in self.events if k == 'transcript')['messages']
        saved = await c.current_messages()
        self.assertEqual([m['id'] for m in transcript], [m.id for m in saved])
        for shown, stored in zip(transcript, saved, strict=True):
            self.assertEqual(shown['state'], stored.state.value)
            if stored.role == 'assistant' and stored.id != saved[1].id:
                self.assertEqual(shown['state'], 'error')
                self.assertEqual(shown['error'], stored.error)
                self.assertIn('interrupted', shown['error'])
        self.assertEqual(transcript[1]['state'], 'complete')
        self.assertTrue(all(b['status'] == 'ready' for b in transcript[1]['blocks']))
        self.assertEqual(transcript[-1]['blocks'][0]['status'], 'cancelled')
        self.assertEqual(saved[-1].blocks[0]['status'], 'cancelled')
        rows = await self.runtime.database.run(lambda conn: [dict(row) for row in
            conn.execute('SELECT event_id, status FROM ui_events ORDER BY event_id')])
        self.assertEqual(rows, [
            {'event_id': 'accepted-event', 'status': 'interrupted'},
            {'event_id': 'started-event', 'status': 'interrupted'},
        ])
        for row in rows:
            with self.assertRaisesRegex(ValueError, 'stale'):
                await c._repo.ui_event_context(row['event_id'], c.conversation.id)
        self.assertEqual(len(saved), 4)  # Startup must not replay either action.

    async def test_recovery_is_visible_before_model_loading_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def load(model):
            entered.set()
            await release.wait()

        self.runtime.controller.backend.load = load
        boot = asyncio.create_task(self.session.boot())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertFalse(boot.done())
            await self.assert_recovered_transcript()
        finally:
            release.set()
            await boot
        self.assertTrue(self.session._ready)

    async def test_recovery_is_visible_when_model_loading_fails(self):
        async def load(model):
            raise RuntimeError('Provider offline')

        self.runtime.controller.backend.load = load
        await self.session.boot()
        await self.assert_recovered_transcript()
        self.assertFalse(self.session._ready)
        self.assertEqual(next(p for k, p in reversed(self.events) if k == 'status')['state'],
                         'Backend unavailable')


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.runtime = make_runtime(self.root)
        await self.runtime.open()
        self.events = []
        self.session = Session(self.runtime.controller,
                               lambda kind, payload: self.events.append((kind, payload)))
        await self.session.boot()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.temp.cleanup()

    def latest(self, kind):
        return next(payload for name, payload in reversed(self.events) if name == kind)

    async def block_loading(self):
        started = asyncio.Event()

        async def load(model):
            started.set()
            await asyncio.Event().wait()

        self.runtime.controller.backend.load = load
        return started

    async def stop_turn(self):
        task = self.session._generation
        self.assertTrue(self.session.stop())
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_retry_repeats_latest_prompt_and_keeps_composer(self):
        await self.session.attach_bytes(png(10, 10), 'original.png')
        await self.session.submit('Describe the original')
        await self.session._generation
        await self.session.attach_bytes(png(12, 12), 'draft.png')
        self.session.update_draft('Unsent next question')
        self.assertTrue(await self.session.retry())
        # The reservation also rejects a second retry and chat changes.
        task = self.session._generation
        self.assertFalse(await self.session.retry())
        self.assertFalse(await self.session.new_conversation())
        await task
        messages = await self.runtime.controller.current_messages()
        self.assertEqual([m.content for m in messages if m.role == 'user'],
                         ['Describe the original', 'Describe the original'])
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[2].attachments[0].digest, messages[0].attachments[0].digest)
        self.assertEqual([a.filename for a in self.session._attachments], ['draft.png'])
        self.assertEqual(self.session._draft_text, 'Unsent next question')
        self.assertTrue(self.latest('status')['can_retry'])

    async def test_model_labels_follow_answer_provenance_across_selection_and_reopen(self):
        c = self.runtime.controller
        c.model_name = 'gemma-4-e4b-it'
        self.session.status()
        self.assertEqual(self.latest('status')['availability'], 'Gemma 4 E4B IT Ready')
        self.assertEqual(self.latest('status')['response_model'], '')
        await self.session.submit('First model')
        await self.session._generation
        conversation_id = c.conversation.id
        self.assertEqual(self.latest('reply_end')['model'], 'gemma-4-e4b-it')
        self.assertEqual(self.latest('status')['response_model'], 'Gemma 4 E4B IT')

        # Selecting the next model must not relabel the existing answer.
        c.model_name = 'opencode-go/another-model'
        c.catalog.append({'id': c.model_name, 'name': 'Another Model Pro'})
        catalog = list(c.catalog)

        async def models(provider=None):
            return catalog

        c.backend.models = models
        self.session.status()
        self.assertEqual(self.latest('status')['availability'], 'Another Model Pro Ready')
        self.assertEqual(self.latest('status')['model'], 'opencode-go/another-model')
        self.assertEqual(self.latest('status')['response_model'], 'Gemma 4 E4B IT')
        await self.session.redraw()
        self.assertEqual(self.latest('snapshot')['status']['response_model'], 'Gemma 4 E4B IT')
        await self.session.new_conversation()
        self.assertEqual(self.latest('status')['response_model'], '')
        await self.session.open_conversation(conversation_id)
        self.assertEqual(self.latest('status')['response_model'], 'Gemma 4 E4B IT')

        # Replay must keep both answers' original identifiers.
        await self.session.submit('Second model')
        await self.session._generation
        self.assertEqual(self.latest('status')['response_model'], 'Another Model Pro')
        await self.session.redraw()
        replies = [m for m in self.latest('snapshot')['messages'] if m['role'] == 'assistant']
        self.assertEqual([m['model'] for m in replies],
                         ['gemma-4-e4b-it', 'opencode-go/another-model'])

    async def test_retry_revalidates_original_references(self):
        c = self.runtime.controller
        await c.set_workspace(str(self.root))
        path = self.root / 'notes.txt'
        path.write_text('Original notes')
        files = await c.workspace_files()
        ref = files.metadata(str(path))
        text = 'Read @notes'
        await self.session.submit(text, [{**ref, 'token': '@notes'}])
        await self.session._generation
        original = (await c.current_messages())[0]
        await self.session.retry()
        await self.session._generation
        retried = (await c.current_messages())[2]
        self.assertEqual(retried.refs, original.refs)
        path.unlink()
        await self.session.retry()
        await self.session._generation
        self.assertEqual(len(await c.current_messages()), 4)
        self.assertEqual(self.latest('notice')['tone'], 'error')

    async def test_retry_missing_attachment_does_not_send_incomplete_prompt(self):
        await self.session.attach_bytes(png(10, 10), 'original.png')
        await self.session.submit('Describe')
        await self.session._generation
        messages = await self.runtime.controller.current_messages()
        self.runtime.controller._store.delete(messages[0].attachments[0].storage_path)
        self.session.update_draft('Keep this draft')
        await self.session.retry()
        await self.session._generation
        self.assertEqual(len(await self.runtime.controller.current_messages()), 2)
        self.assertEqual(self.session._draft_text, 'Keep this draft')
        self.assertIn('unavailable', self.latest('notice')['html'])
        self.assertFalse(self.latest('status')['generating'])

    async def test_retry_cancelled_before_persistence_preserves_next_draft(self):
        await self.session.submit('Original question')
        await self.session._generation
        started = await self.block_loading()
        self.session.update_draft('Next question')
        await self.session.retry()
        await started.wait()
        await self.stop_turn()
        self.assertEqual(len(await self.runtime.controller.current_messages()), 2)
        self.assertEqual(self.session._draft_text, 'Next question')
        self.assertFalse(any(kind == 'restore_draft' for kind, _ in self.events))
        self.assertTrue(self.latest('status')['can_retry'])

    async def test_retry_empty_chat_and_after_reopen(self):
        self.assertFalse(self.latest('status')['can_retry'])
        await self.session.retry()
        await self.session._generation
        self.assertEqual(await self.runtime.controller.current_messages(), [])
        await self.session.submit('Saved question')
        await self.session._generation
        conversation_id = self.runtime.controller.conversation.id
        await self.session.new_conversation()
        self.assertFalse(self.latest('status')['can_retry'])
        await self.session.open_conversation(conversation_id)
        self.assertTrue(self.latest('status')['can_retry'])
        await self.session.retry()
        await self.session._generation
        self.assertEqual(len(await self.runtime.controller.current_messages()), 4)

    async def test_retry_after_failed_reply(self):
        async def fail(*args, **kwargs):
            raise RuntimeError('Provider failed')
            yield
        c = self.runtime.controller
        original_stream = c.backend.stream
        c.backend.stream = fail
        await self.session.submit('Try again')
        await self.session._generation
        self.assertEqual((await c.current_messages())[1].state.value, 'error')
        c.backend.stream = original_stream
        await self.session.retry()
        await self.session._generation
        self.assertEqual((await c.current_messages())[3].state.value, 'complete')

    async def test_stop_before_persistence_restores_question_and_attachment(self):
        await self.session.attach_bytes(png(10, 10), 'only.png')
        started = await self.block_loading()
        await self.session.submit('Unstored question')
        await started.wait()
        self.session.update_draft('Next question')
        await self.stop_turn()
        self.assertEqual(await self.runtime.controller.current_messages(), [])
        self.assertEqual(self.latest('restore_draft')['text'], 'Unstored question\n\nNext question')
        self.assertEqual([a.filename for a in self.session._attachments], ['only.png'])
        self.assertFalse(self.latest('status')['generating'])

    async def test_stop_immediately_after_submit_recovers_draft(self):
        await self.block_loading()
        await self.session.submit('Immediate stop')
        await self.stop_turn()
        self.assertEqual(self.latest('restore_draft')['text'], 'Immediate stop')
        self.assertIsNone(self.session._generation)

    async def test_close_immediately_after_submit_settles_recovery(self):
        await self.block_loading()
        await self.session.submit('Close immediately')
        await self.session.close()
        self.assertEqual(self.latest('restore_draft')['text'], 'Close immediately')
        self.assertIsNone(self.session._generation)

    async def test_five_concurrent_images_accept_only_four(self):
        original = self.runtime.controller.prepare_attachment
        barrier = threading.Barrier(5)

        def prepare(data, filename):
            result = original(data, filename)
            barrier.wait(timeout=5)
            return result

        with patch.object(self.runtime.controller, 'prepare_attachment', prepare):
            accepted = await asyncio.gather(*(
                self.session.attach_bytes(png(10, 10), f'{i}.png') for i in range(5)))
        self.assertEqual(sum(accepted), 4)
        await self.session.submit('Describe')
        await self.session._generation
        self.assertEqual(len((await self.runtime.controller.current_messages())[0].attachments), 4)

    async def test_path_import_extracts_document_once(self):
        path = self.root / 'notes.txt'
        path.write_text('A document to read once.')
        with patch.object(documents, 'inspect_document', wraps=documents.inspect_document) as inspect:
            self.assertTrue(await self.session.attach_path(str(path)))
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(self.session._attachments[0].text, 'A document to read once.')

    async def test_import_finishing_after_send_starts_is_refused(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.runtime.controller.prepare_attachment

        def prepare(data, filename):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('test import was not released')
            return original(data, filename)

        started = await self.block_loading()
        with patch.object(self.runtime.controller, 'prepare_attachment', prepare):
            importing = asyncio.create_task(self.session.attach_bytes(png(10, 10), 'late.png'))
            try:
                await asyncio.to_thread(entered.wait, 5)
                await self.session.submit('Start a turn')
                await started.wait()
            finally:
                release.set()
            self.assertFalse(await importing)
        self.assertEqual(self.session._attachments, [])
        await self.stop_turn()

    async def test_snapshot_before_persistence_contains_active_user_and_stop_state(self):
        await self.session.attach_bytes(png(10, 10), 'only.png')
        started = await self.block_loading()
        await self.session.submit('Loading request')
        await started.wait()
        await self.session.redraw()
        snapshot = self.latest('snapshot')
        self.assertEqual(snapshot['messages'], [])
        self.assertEqual(snapshot['active']['user']['text'], 'Loading request')
        self.assertEqual(snapshot['active']['user']['attachments'][0]['filename'], 'only.png')
        self.assertTrue(snapshot['status']['generating'])
        self.assertEqual(snapshot['status']['state'], 'Generating…')
        await self.stop_turn()

    async def test_snapshot_restores_stream_reasoning_tool_and_draft_without_duplicates(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def stream(model, messages, tools):
            yield {'delta': {'content': 'Partial answer', 'reasoning_content': 'Consider the evidence.'}}
            started.set()
            await release.wait()
            yield {'delta': {'content': ' completed.'}, 'finish_reason': 'stop'}

        original_send = self.runtime.controller.send

        async def send(text, **kwargs):
            kwargs['on_tool']('knowledge_search', 'start', None)
            return await original_send(text, **kwargs)

        self.runtime.controller.backend.stream = stream
        with patch.object(self.runtime.controller, 'send', send):
            await self.session.submit('Question')
            await started.wait()
            self.session.update_draft('A follow-up draft')
            await self.session.redraw()
            snapshot = self.latest('snapshot')
            self.assertEqual(len(snapshot['messages']), 1)
            self.assertEqual(snapshot['messages'][0]['role'], 'user')
            self.assertIsNone(snapshot['active']['user'])
            self.assertIn('Partial answer', snapshot['active']['html'])
            self.assertEqual(snapshot['active']['reasoning'], 'Consider the evidence.')
            self.assertEqual(snapshot['active']['tool']['name'], 'knowledge_search')
            self.assertEqual(snapshot['draft'], 'A follow-up draft')
            key = snapshot['active']['key']
            release.set()
            await self.session._generation
        self.assertEqual(self.latest('reply_end')['key'], key)
        await self.session.redraw()
        self.assertIsNone(self.latest('snapshot')['active'])
        self.assertEqual(len(self.latest('snapshot')['messages']), 2)
        self.assertFalse(self.latest('snapshot')['status']['generating'])

    async def test_idle_snapshot_restores_attachment_draft(self):
        await self.session.attach_bytes(b'A saved draft document.', 'notes.txt')
        self.session.update_draft('Not sent yet')
        await self.session.redraw()
        self.assertEqual(self.latest('snapshot')['attachments']['items'][0]['filename'], 'notes.txt')
        self.assertEqual(self.latest('snapshot')['draft'], 'Not sent yet')

    async def test_new_active_snapshot_keeps_prior_cancelled_answer(self):
        started = asyncio.Event()

        async def stream(model, messages, tools):
            yield {'delta': {'content': 'A partial answer'}}
            started.set()
            await asyncio.Event().wait()

        self.runtime.controller.backend.stream = stream
        await self.session.submit('First question')
        await started.wait()
        await self.stop_turn()
        self.assertNotIn('restore_draft', [name for name, _ in self.events])
        started.clear()
        await self.session.submit('Second question')
        await started.wait()
        await self.session.redraw()
        snapshot = self.latest('snapshot')
        self.assertEqual([m['role'] for m in snapshot['messages']], ['user', 'assistant', 'user'])
        self.assertEqual(snapshot['messages'][1]['state'], 'cancelled')
        self.assertIn('A partial answer', snapshot['messages'][1]['html'])
        self.assertIsNone(snapshot['active']['user'])
        await self.stop_turn()

    async def test_metrics_follow_conversation_and_survive_redraw(self):
        async def stream(model, messages, tools):
            yield {'delta': {'content': 'Done'}, 'finish_reason': 'stop',
                   '_usage': {'prompt_tokens': 1200, 'completion_tokens': 24},
                   '_timings': {'predicted_per_second': 31.5}}

        self.runtime.controller.backend.stream = stream
        original = self.runtime.controller.conversation.id
        await self.session.submit('Question')
        await self.session._generation
        metrics = self.latest('status')['metrics']
        self.assertEqual(metrics['context_used'], 1224)
        self.assertGreater(metrics['admission_input_bound'], 1200)
        self.assertEqual(metrics['output_reserve'], 16000)
        self.assertIsNone(metrics['tokens_per_second'])
        self.assertTrue(any(kind == 'metrics' and p.get('tokens_per_second') == 31.5
                            for kind, p in self.events))
        await self.session.redraw()
        self.assertEqual(self.latest('snapshot')['status']['metrics'], metrics)
        await self.session.new_conversation()
        self.assertIsNone(self.latest('status')['metrics']['context_used'])
        await self.session.open_conversation(original)
        self.assertEqual(self.latest('status')['metrics'], metrics)

    async def test_reasoning_only_failure_keeps_terminal_outcome_after_reopen(self):
        async def stream(*args, **kwargs):
            yield {'delta': {'reasoning_content': 'Considering the request.'},
                   '_timings': {'predicted_per_second': 28.0}, 'finish_reason': 'stop'}

        self.runtime.controller.backend.stream = stream
        await self.session.submit('Question')
        await self.session._generation
        end = self.latest('reply_end')
        self.assertEqual(end['state'], 'error')
        self.assertEqual(end['html'], '')
        self.assertIn('no visible answer', end['error'])
        self.assertIn('Reply state: error', end['diagnostics'])
        self.assertNotIn('no visible answer', end['diagnostics'])
        self.assertEqual(self.latest('status')['request_state'], 'error')
        self.assertEqual(self.latest('status')['availability'], 'Test Vision Ready')
        self.assertFalse(self.latest('status')['generating'])
        self.assertIsNone(self.latest('status')['metrics']['tokens_per_second'])
        conversation_id = self.runtime.controller.conversation.id
        await self.session.new_conversation()
        self.assertIsNone(self.latest('status')['request_state'])
        await self.session.open_conversation(conversation_id)
        await self.session.redraw()
        self.assertEqual(self.latest('snapshot')['status']['request_state'], 'error')
        self.assertEqual(self.latest('snapshot')['messages'][-1]['error'], end['error'])
        self.assertIn('Assistant message ID:', self.latest('snapshot')['messages'][-1]['diagnostics'])

    async def test_repair_exhaustion_reports_phases_and_clears_speed(self):
        from test_turns import Backend
        c = self.runtime.controller
        c.model_name = 'opencode-go/test-vision'
        c.backend = Backend([[('animation_create', {'scene': {}, 'loop': False})],
                             [('animation_create', {'scene': {}, 'loop': False})],
                             'I need to adjust the animation.'])
        await self.session.submit('Create an animation')
        await self.session._generation
        self.assertEqual(self.latest('reply_end')['state'], 'error')
        self.assertIn('no animation was saved', self.latest('reply_end')['error'])
        states = [p['state'] for kind, p in self.events if kind == 'status']
        for phase in ('Preparing tool arguments…', 'Validating animation…', 'Repairing animation…'):
            self.assertIn(phase, states)
        self.assertEqual(self.latest('status')['request_state'], 'error')
        self.assertIsNone(self.latest('status')['metrics']['tokens_per_second'])

    async def test_stop_and_complete_outcomes_survive_snapshot(self):
        await self.session.submit('Complete this')
        await self.session._generation
        self.assertEqual(self.latest('status')['request_state'], 'complete')
        self.assertIsNone(self.latest('reply_end')['diagnostics'])
        self.assertIn('Saving…', [p['state'] for kind, p in self.events if kind == 'status'])
        started = asyncio.Event()

        async def stream(*args, **kwargs):
            yield {'delta': {'reasoning_content': 'Working.'},
                   '_timings': {'predicted_per_second': 42.0}}
            started.set()
            await asyncio.Event().wait()

        self.runtime.controller.backend.stream = stream
        await self.session.submit('Stop this')
        await started.wait()
        await self.session.redraw()
        self.assertEqual(self.latest('snapshot')['status']['request_state'], 'pending')
        self.assertEqual(self.latest('snapshot')['status']['metrics']['tokens_per_second'], 42.0)
        await self.stop_turn()
        self.assertIn('Reply state: cancelled', self.latest('reply_end')['diagnostics'])
        await self.session.redraw()
        status = self.latest('snapshot')['status']
        self.assertEqual(status['request_state'], 'cancelled')
        self.assertFalse(status['generating'])
        self.assertIsNone(status['metrics']['tokens_per_second'])
        self.assertIn('Reply state: cancelled', self.latest('snapshot')['messages'][-1]['diagnostics'])

    async def test_pre_admission_rejection_restores_draft_with_failed_status(self):
        from test_turns import Backend
        self.runtime.controller.backend = Backend(context=4096)
        await self.session.submit('x' * 10000)
        await self.session._generation
        self.assertEqual(self.session._draft_text, 'x' * 10000)
        self.assertEqual(self.latest('status')['request_state'], 'error')
        self.assertFalse(self.latest('status')['generating'])
        self.assertIn('conservative host estimate', self.latest('notice')['html'])
        metrics = self.latest('status')['metrics']
        self.assertEqual(metrics['context_window'], 4096)
        self.assertGreater(metrics['admission_input_bound'] + metrics['output_reserve'], 4096)
        diagnostics = self.latest('notice')['diagnostics']
        self.assertIn('Reply state: error', diagnostics)
        self.assertNotIn('conservative host estimate', diagnostics)
        await self.session.redraw()
        self.assertEqual(self.latest('snapshot')['status']['request_state'], 'error')
        self.assertIn('conservative host estimate', self.latest('snapshot')['request_notice'])
        self.assertEqual(self.latest('snapshot')['draft'], 'x' * 10000)
        self.assertEqual(self.latest('snapshot')['request_diagnostics'], diagnostics)

    async def test_copy_diagnostics_excludes_private_exception_and_configuration(self):
        private = 'sk-secret-token https://private.invalid/?api_key=secret /home/private/prompt'

        async def fail(*args, **kwargs):
            raise RuntimeError(private)

        self.runtime.controller.send = fail
        await self.session.submit('Private question')
        await self.session._generation
        diagnostics = self.latest('reply_end')['diagnostics']
        self.assertNotIn(private, diagnostics)
        self.assertNotIn('Private question', diagnostics)
        self.assertNotIn(self.runtime.controller.config.endpoint, diagnostics)
        self.assertEqual(self.latest('notice')['diagnostics'], diagnostics)
        await self.session.redraw()
        self.assertEqual(self.latest('snapshot')['request_diagnostics'], diagnostics)


class SnapshotBatchTests(unittest.TestCase):
    def test_reasoning_and_updates_never_coalesce_across_snapshot(self):
        events = [
            ('reasoning', {'key': 1, 'text': 'Before'}),
            ('reply_update', {'key': 1, 'html': 'Before'}),
            ('snapshot', {'active': {'key': 1, 'reasoning': 'Before'}}),
            ('reasoning', {'key': 1, 'text': 'After'}),
            ('reply_update', {'key': 1, 'html': 'After'}),
        ]
        self.assertEqual(coalesce(events), events)
