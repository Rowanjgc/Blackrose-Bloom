"""SM-01: turn phases, reservation flags and the settlement boundary.

Each test pins one claim from ``docs/state-machines/sm-01-turn-orchestration.md``.
The tuple under test is::

    (is_generating, last_request_stored, assistant, completed,
     checkpoint_task, citation_audit)

with the last two read as "attached" (not ``None``), exactly as the brief states
them.  ``ALLOWED_TUPLES`` is the 13-row reachable subset of the 64 combinations;
``test_flag_combinations_are_confined_to_the_documented_matrix`` asserts that a
real turn never leaves it.
"""
import asyncio
import hashlib
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from blackrose.errors import ChatBusy
from blackrose.storage.records import MessageState
from blackrose.tool_execution import ToolExecutor
from blackrose.turns import TurnRunner
import test_gui_images as fixtures
from test_turns import Backend, block_final_save, stop_during_final_save


# Every reachable value of (is_generating, last_request_stored, assistant,
# completed, checkpoint_task, citation_audit).  The other 51 combinations are
# structurally impossible; see the specification's matrix.
ALLOWED_TUPLES = {
    # No reply identity yet.
    (0, 0, 0, 0, 0, 0),   # idle, or phase 1 (preparing) failed
    (0, 1, 0, 0, 0, 0),   # at rest between turns, or H1 before admission
    (1, 0, 0, 0, 0, 0),   # preparing: the generation reservation is held
    # Committed reply, settling or settled.
    (1, 1, 1, 0, 0, 0),   # committed, no reply services attached yet
    (1, 1, 1, 0, 1, 0),   # answering, plain-reply checkpoints
    (1, 1, 1, 0, 0, 1),   # answering, citation audit attached
    (1, 1, 1, 1, 1, 0),   # complete write landed during Stop, still reserved
    (1, 1, 1, 1, 0, 1),   # same, audited reply
    (0, 1, 1, 0, 1, 0),   # settled, inside the finally
    (0, 1, 1, 0, 0, 1),   # settled audited reply, inside the finally
    (0, 1, 1, 0, 0, 0),   # settled before services attached, inside the finally
    (0, 1, 1, 1, 1, 0),   # returned/raised after a complete plain reply
    (0, 1, 1, 1, 0, 1),   # returned/raised after a complete audited reply
}

EVIDENCE = {'results': [{'document_id': 'document', 'chunk_id': 'chunk',
                         'title': 'Source', 'text': 'Source text.'}]}


def snapshot(runner):
    """The six field values named in the brief."""
    return (int(runner.chat.is_generating), int(runner.chat.last_request_stored),
            int(runner.assistant is not None), int(runner.completed is not None),
            int(runner.checkpoint_task is not None), int(runner.citation_audit is not None))


def live_services(runner):
    """Which reply services are attached *and* still running."""
    task = runner.checkpoint_task
    return (task is not None and not task.done(), runner.citation_audit is not None)


class Recorder(TurnRunner):
    """A TurnRunner that records the tuple at every phase boundary."""

    observed = []

    def note(self, phase):
        self.observed.append((phase, snapshot(self), live_services(self)))

    async def run(self, *args, **kwargs):
        self.note('run:enter')
        try:
            return await super().run(*args, **kwargs)
        finally:
            self.note('run:exit')

    async def _prepare(self, *args, **kwargs):
        self.note('prepare:enter')
        prepared = await super()._prepare(*args, **kwargs)
        self.note('prepare:exit')
        return prepared

    async def _begin_turn(self, *args, **kwargs):
        try:
            return await super()._begin_turn(*args, **kwargs)
        finally:
            self.note('begin:exit')

    async def _start_reply(self, *args, **kwargs):
        reply = await super()._start_reply(*args, **kwargs)
        self.note('start_reply:exit')
        return reply

    async def _answer(self, *args, **kwargs):
        self.note('answer:enter')
        await super()._answer(*args, **kwargs)
        self.note('answer:exit')

    async def _complete(self, *args, **kwargs):
        self.note('complete:enter')
        try:
            return await super()._complete(*args, **kwargs)
        finally:
            self.note('complete:exit')

    async def _settle_interrupted(self, *args, **kwargs):
        self.note('settle:enter')
        await super()._settle_interrupted(*args, **kwargs)


class FailingBackend(Backend):
    def __init__(self, message='Original provider failure'):
        super().__init__([])
        self.message = message

    async def stream(self, *args, **kwargs):
        yield {'delta': {'content': 'Partial answer.'}}
        raise RuntimeError(self.message)


class BlockingBackend(Backend):
    """Streams one accepted chunk, then waits until released or cancelled."""

    def __init__(self):
        super().__init__([])
        self.streaming = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, *args, **kwargs):
        yield {'delta': {'content': 'Accepted answer.'}}
        self.streaming.set()
        await self.release.wait()
        yield {'delta': {}, 'finish_reason': 'stop'}


class TurnOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    def controller(self, steps=('Done.',)):
        c = self.runtime.controller
        c.backend = Backend(steps)
        c.schedule_title = Mock()
        c.schedule_memory_sync = Mock()
        return c

    async def reject_terminal_writes(self):
        await self.runtime.database.run(lambda conn: conn.executescript('''
            CREATE TRIGGER reject_terminal BEFORE UPDATE ON messages
            WHEN NEW.role = 'assistant' AND NEW.state != 'pending'
            BEGIN SELECT RAISE(ABORT, 'terminal storage unavailable'); END;
        '''))

    # --- the derived state matrix ------------------------------------------

    async def test_flag_combinations_are_confined_to_the_documented_matrix(self):
        c = self.controller(['Plain answer.', 'Audited answer.'])
        observed = {}
        for mode in ('off', 'balanced'):
            c.config.citation_rigor = mode
            Recorder.observed = []
            with patch('blackrose.chat.TurnRunner', Recorder):
                await c.send(f'Question ({mode})')
            observed[mode] = list(Recorder.observed)
            tuples = {row for _, row, _ in Recorder.observed}
            self.assertLessEqual(tuples, ALLOWED_TUPLES, f'{mode}: undocumented combination')

        # A committed reply implies a stored request, and reply services are
        # attached exactly one at a time.
        for mode, rows in observed.items():
            for _phase, row, _ in rows:
                if row[2]:
                    self.assertEqual(row[1], 1, f'{mode}: assistant without last_request_stored')
                self.assertFalse(row[4] and row[5], f'{mode}: checkpoint and audit both attached')
            phases = {phase: (row, live) for phase, row, live in rows}
            self.assertEqual(phases['begin:exit'][0], (1, 1, 1, 0, 0, 0))
            if mode == 'off':
                self.assertEqual(phases['answer:enter'][0], (1, 1, 1, 0, 1, 0))
                self.assertEqual(phases['complete:exit'][0], (1, 1, 1, 1, 1, 0))
                self.assertEqual(phases['answer:enter'][1], (True, False))
            else:
                self.assertEqual(phases['answer:enter'][0], (1, 1, 1, 0, 0, 1))
                self.assertEqual(phases['complete:exit'][0], (1, 1, 1, 1, 0, 1))
                self.assertEqual(phases['answer:enter'][1], (False, True))
            self.assertEqual(phases['run:exit'][0], (0, 1, 1, 1, int(mode == 'off'), int(mode != 'off')))

    async def test_a_send_cancelled_before_admission_reports_nothing_stored(self):
        """H1 (fixed): the admitted-request flag is cleared before the first await.

        ``TurnRunner.run`` clears ``last_request_stored`` beside
        ``last_completed_turn`` before ``ensure_conversation`` and the keyed
        credential lookup, so a Stop during either cannot inherit the previous
        turn's ``True`` (gui/session.py:1452, 1469, 1520).  The refusal guard
        moved up with it: a ``ChatBusy``-refused send must not clear the live
        turn's flag.
        """
        c = self.controller(['First answer.', 'Unused answer.'])
        await c.send('First question')
        self.assertTrue(c.last_request_stored)
        entered, release = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def held_credentials():
            entered.set()
            await release.wait()
            yield

        c.backend.turn_credentials = lambda model: held_credentials()
        task = asyncio.create_task(c.send('Second question'))
        try:
            self.assertTrue(await asyncio.wait_for(entered.wait(), 5))
            # Not admitted yet: the generation reservation is not held either.
            self.assertFalse(c.is_generating)
            task.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(c.last_request_stored)         # H1: nothing was stored
        self.assertIsNone(c.last_completed_turn)
        self.assertFalse(c.is_generating)
        self.assertEqual([m.content for m in await c.current_messages()],
                         ['First question', 'First answer.'])

    # --- phase 2: the commit and its identity ------------------------------

    async def test_cancelling_the_commit_still_yields_the_stored_identity(self):
        c = self.controller()
        committed, release = asyncio.Event(), asyncio.Event()
        begin = c._repo.begin_turn

        async def blocked(*args, **kwargs):
            result = await begin(*args, **kwargs)
            committed.set()
            await release.wait()
            return result

        c._repo.begin_turn = blocked
        task = asyncio.create_task(c.send('Keep this request'))
        try:
            await asyncio.wait_for(committed.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        request, reply = await c.current_messages()
        self.assertEqual([request.state, reply.state], [MessageState.COMPLETE, MessageState.CANCELLED])
        self.assertEqual(reply.reply_to, request.id)
        self.assertTrue(c.last_request_stored)
        self.assertIsNone(c.last_completed_turn)
        self.assertFalse(c.is_generating)
        self.assertIsNone(c.outbound)
        c.schedule_title.assert_not_called()

    async def test_cancel_while_persisting_attachments_leaves_no_turn_documented_follow_up(self):
        """H4: attachment blobs are written before the turn row exists."""
        c = self.controller()
        entered, release = threading.Event(), threading.Event()
        persist = c._persist_attachments

        def blocked(attachments):
            written = persist(attachments)
            entered.set()
            release.wait(5)
            return written

        c._persist_attachments = blocked
        pending = c.attach(b'FILE BODY', 'notes.txt')
        task = asyncio.create_task(c.send('Question', attachments=[pending]))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await c.current_messages(), [])
        self.assertFalse(c.last_request_stored)
        self.assertFalse(c.is_generating)
        self.assertIsNone(c.outbound)
        # The blob is on disk with no row pointing at it; artifact cleanup only
        # reconciles recorded obligations, so it survives until explicit repair.
        blobs = await asyncio.to_thread(
            lambda: [path for path in Path(self.root, 'attachments').rglob('*') if path.is_file()])
        self.assertEqual(len(blobs), 1)

    async def test_cancel_after_the_commit_but_before_any_service_settles_an_empty_reply(self):
        """The committed-but-unserviced tuple (1,1,1,0,0,0) is reachable."""
        c = self.controller()

        def blocked(*args, **kwargs):
            raise asyncio.CancelledError

        with patch.object(ToolExecutor, '__init__', blocked):
            with self.assertRaises(asyncio.CancelledError):
                await c.send('Question')
        request, reply = await c.current_messages()
        self.assertEqual(request.state, MessageState.COMPLETE)
        self.assertEqual(reply.state, MessageState.CANCELLED)
        self.assertEqual(reply.content, '')
        self.assertEqual(reply.error, 'Stopped')
        self.assertTrue(c.last_request_stored)
        self.assertFalse(c.is_generating)
        self.assertIsNone(c.outbound)
        c.schedule_title.assert_not_called()

    # --- phase 5: the complete write ---------------------------------------

    async def test_a_committed_final_save_is_never_followed_by_settlement(self):
        c = self.controller(['Complete answer.'])
        settle = TurnRunner._settle_interrupted
        settlements = []

        async def spied(runner, error, mode):
            settlements.append(type(error).__name__)
            return await settle(runner, error, mode)

        entered, release = await block_final_save(self.runtime.database)
        with patch.object(TurnRunner, '_settle_interrupted', spied):
            await stop_during_final_save(self, asyncio.create_task(c.send('Question')),
                                         entered, release)
        stored = (await c.current_messages())[-1]
        self.assertEqual(settlements, [])
        self.assertEqual(stored.state, MessageState.COMPLETE)
        self.assertEqual(stored.content, 'Complete answer.')
        self.assertIsNotNone(c.last_completed_turn)
        self.assertEqual(c.last_completed_turn.assistant_message.id, stored.id)
        self.assertEqual(c.last_completed_turn.assistant_message.state, MessageState.COMPLETE)

    async def test_a_failed_complete_write_settles_cancelled_and_leaves_no_completed_turn(self):
        c = self.controller(['Complete answer.'])
        entered, release = await block_final_save(self.runtime.database, fail=True)
        with self.assertLogs('blackrose.turns', level='WARNING') as logs:
            await stop_during_final_save(self, asyncio.create_task(c.send('Question')),
                                         entered, release)
        self.assertIn('failed during Stop', logs.output[0])
        stored = (await c.current_messages())[-1]
        self.assertEqual(stored.state, MessageState.CANCELLED)
        self.assertEqual(stored.error, 'Stopped')
        self.assertIsNone(c.last_completed_turn)
        self.assertTrue(c.last_request_stored)
        self.assertFalse(c.is_generating)
        self.assertIsNone(c.outbound)

    async def test_checkpoint_drain_is_idempotent_across_complete_and_settlement(self):
        c = self.controller()
        await self.reject_terminal_writes()
        checkpointed = asyncio.Event()
        drains, order = [], []
        drain = TurnRunner._stop_checkpoints
        finish = c._repo.finish_message
        checkpoint = c._repo.checkpoint_content

        async def counted(runner):
            drains.append(1)
            return await drain(runner)

        async def traced_finish(*args, **kwargs):
            order.append('finish')
            return await finish(*args, **kwargs)

        async def traced_checkpoint(*args, **kwargs):
            order.append('checkpoint')
            result = await checkpoint(*args, **kwargs)
            checkpointed.set()
            return result

        class Checkpointed(Backend):
            async def stream(self, *args, **kwargs):
                yield {'delta': {'content': 'Prefix'}}
                await checkpointed.wait()
                yield {'delta': {}, 'finish_reason': 'stop'}

        c.backend = Checkpointed()
        with patch.object(TurnRunner, '_stop_checkpoints', counted), \
                patch.object(c._repo, 'finish_message', traced_finish), \
                patch.object(c._repo, 'checkpoint_content', traced_checkpoint), \
                patch('blackrose.turns.CONTENT_CHECKPOINT_SECONDS', 0.01), \
                self.assertLogs('blackrose.turns', level='WARNING'):
            with self.assertRaisesRegex(Exception, 'terminal storage unavailable'):
                await c.send('Question')
        # _complete drains, its write fails, _save_interrupted drains again.
        self.assertEqual(len(drains), 2)
        self.assertEqual(order, ['checkpoint', 'finish', 'finish', 'finish'])
        self.assertEqual((await c.current_messages())[-1].state, MessageState.PENDING)

    async def test_settlement_failure_masks_the_stop_with_the_storage_error(self):
        c = self.controller()
        c.backend = BlockingBackend()
        await self.reject_terminal_writes()
        task = asyncio.create_task(c.send('Question'))
        try:
            await asyncio.wait_for(c.backend.streaming.wait(), 5)
            task.cancel()
            with self.assertLogs('blackrose.turns', level='WARNING'):
                with self.assertRaisesRegex(Exception, 'terminal storage unavailable'):
                    await task
        finally:
            c.backend.release.set()
        stored = (await c.current_messages())[-1]
        self.assertEqual(stored.state, MessageState.PENDING)   # startup recovery owns it
        self.assertTrue(c.last_request_stored)
        self.assertIsNone(c.last_completed_turn)
        self.assertFalse(c.is_generating)
        self.assertIsNone(c.outbound)

    # --- reply services and the audit path ---------------------------------

    async def test_cancelling_the_audit_checkpoint_settles_an_unchecked_report(self):
        c = self.controller([[('knowledge_search', {'query': 'source'})], 'Accepted answer.'])
        c.config.citation_rigor = 'balanced'
        c.config.knowledge_enabled = True
        c.knowledge.call = AsyncMock(return_value=EVIDENCE)
        entered, release = asyncio.Event(), asyncio.Event()
        save_audit = c._repo.save_audit
        seen = []

        async def blocked(identity, report, **kwargs):
            seen.append(report['status'])
            if report['status'] == 'checking':
                entered.set()
                await release.wait()
            return await save_audit(identity, report, **kwargs)

        with patch.object(c._repo, 'save_audit', blocked):
            task = asyncio.create_task(c.send('Find the source'))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        stored = (await c.current_messages())[-1]
        audit = await c.message_audit(stored.id)
        self.assertIn('checking', seen)
        self.assertEqual(stored.state, MessageState.CANCELLED)
        self.assertEqual(stored.content, 'Accepted answer.')
        self.assertEqual(audit['status'], 'incomplete')
        self.assertEqual(audit['body_hash'], hashlib.sha256(stored.content.encode()).hexdigest())
        self.assertIn('interrupted before completion', ' '.join(audit['limitations']))
        self.assertIsNone(c.last_completed_turn)

    async def test_an_audited_reply_without_evidence_completes_as_not_applicable(self):
        c = self.controller(['Plain answer.'])
        c.config.citation_rigor = 'balanced'
        turn = await c.send('Question')
        self.assertEqual(turn.assistant_message.state, MessageState.COMPLETE)
        self.assertEqual(turn.audit['status'], 'not_applicable')
        self.assertEqual(turn.audit['body_hash'],
                         hashlib.sha256(b'Plain answer.').hexdigest())
        stored = (await c.current_messages())[-1]
        self.assertEqual(await c.message_audit(stored.id), turn.audit)

    # --- the generation reservation ----------------------------------------

    async def test_a_refused_second_send_does_not_disturb_the_live_turn(self):
        c = self.controller()
        c.backend = BlockingBackend()
        task = asyncio.create_task(c.send('First question'))
        try:
            await asyncio.wait_for(c.backend.streaming.wait(), 5)
            with self.assertRaises(ChatBusy):
                await c.send('Second question')
            self.assertTrue(c.is_generating)
            self.assertTrue(c.last_request_stored)
            self.assertEqual([m.role for m in await c.current_messages()], ['user', 'assistant'])
            c.backend.release.set()
            await task
        finally:
            c.backend.release.set()
        self.assertEqual([m.state for m in await c.current_messages()],
                         [MessageState.COMPLETE, MessageState.COMPLETE])
        self.assertFalse(c.is_generating)

    async def test_a_provider_failure_settles_error_and_releases_the_reservation(self):
        c = self.controller()
        c.backend = FailingBackend()
        with self.assertRaisesRegex(RuntimeError, 'Original provider failure'):
            await c.send('Question')
        stored = (await c.current_messages())[-1]
        self.assertEqual(stored.state, MessageState.ERROR)
        self.assertIn('Original provider failure', stored.error)
        self.assertEqual(stored.content, 'Partial answer.')
        self.assertTrue(c.last_request_stored)
        self.assertIsNone(c.last_completed_turn)
        self.assertFalse(c.is_generating)
        self.assertIsNone(c.outbound)


if __name__ == '__main__':
    unittest.main()
