"""Final evidence failures cannot contradict durable reply outcomes."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from blackrose.citations import EvidenceRegistry, new_report
from blackrose.storage.records import MessageState
import test_gui_images as fixtures
from test_turns import Backend, block_final_save, stop_during_final_save


RESULT = {'results': [{'document_id': 'document', 'chunk_id': 'chunk',
                       'title': 'Source', 'text': 'Source text.'}] * 2}


class ReplyBackend(Backend):
    def __init__(self, outcome):
        super().__init__([[('knowledge_search', {'query': 'source'})]])
        self.outcome = outcome
        self.streaming = asyncio.Event()

    async def stream(self, *args, **kwargs):
        if not self.requests:
            async for choice in super().stream(*args, **kwargs):
                yield choice
            return
        yield {'delta': {'content': 'Accepted answer.'}}
        self.streaming.set()
        if self.outcome == 'error':
            raise RuntimeError('Original provider failure')
        if self.outcome == 'cancelled':
            await asyncio.Event().wait()
        yield {'delta': {}, 'finish_reason': 'stop'}


class TerminalEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown
    latest = fixtures.ImageTests.latest

    async def configure(self, outcome):
        c = self.runtime.controller
        c.backend = ReplyBackend(outcome)
        c.config.knowledge_enabled = True
        c.knowledge.call = AsyncMock(return_value=RESULT)
        c.schedule_title = Mock()
        await c.new_conversation()
        return c

    async def reject_evidence(self):
        await self.runtime.database.run(lambda conn: conn.executescript('''
            CREATE TRIGGER reject_evidence BEFORE INSERT ON evidence
            BEGIN SELECT RAISE(ABORT, 'injected evidence failure'); END;
        '''))

    async def submit(self, c, outcome):
        await self.session.submit('Find the source')
        if outcome == 'cancelled':
            await asyncio.wait_for(c.backend.streaming.wait(), 5)
            self.assertTrue(self.session.stop())
            with self.assertRaises(asyncio.CancelledError):
                await self.session._generation
        else:
            await self.session._generation

    async def assert_reopened_outcome(self, c, state, error=None):
        live = self.latest('reply_end')
        self.assertEqual(live['state'], state)
        if error:
            self.assertIn(error, live['error'])
        await self.runtime.database.close()
        await self.runtime.database.connect()
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, state)
        self.assertEqual(saved.content, 'Accepted answer.')
        if error:
            self.assertIn(error, saved.error)
        if state == 'cancelled':
            self.assertEqual(saved.error, 'Stopped')
        self.assertFalse(c.is_generating)
        await self.session.render_transcript()
        reopened = self.latest('transcript')['messages'][-1]
        self.assertEqual(reopened['state'], live['state'])
        self.assertEqual(reopened['html'], live['html'])
        return saved, live, reopened

    async def test_success_commits_reply_and_deduplicated_evidence_together(self):
        c = await self.configure('complete')
        await self.submit(c, 'complete')
        saved, live, reopened = await self.assert_reopened_outcome(c, 'complete')
        self.assertEqual(len(live['sources']), 1)
        self.assertEqual(await c.message_evidence(saved.id), live['sources'])
        self.assertEqual(reopened['sources'], live['sources'])

    async def test_evidence_failure_cannot_leave_success_behind_live_error(self):
        c = await self.configure('complete')
        await self.reject_evidence()
        with self.assertLogs('blackrose.turns', level='WARNING'):
            await self.submit(c, 'complete')
        saved, _, reopened = await self.assert_reopened_outcome(c, 'error', 'injected evidence failure')
        self.assertEqual(await c.message_evidence(saved.id), [])
        self.assertEqual(reopened['sources'], [])

    async def test_provider_failure_preserves_original_reason_when_evidence_fails(self):
        c = await self.configure('error')
        await self.reject_evidence()
        with self.assertLogs('blackrose.turns', level='WARNING'):
            await self.submit(c, 'error')
        saved, _, _ = await self.assert_reopened_outcome(c, 'error', 'Original provider failure')
        self.assertNotIn('evidence', saved.error)

    async def test_cancellation_survives_evidence_failure(self):
        c = await self.configure('cancelled')
        await self.reject_evidence()
        with self.assertLogs('blackrose.turns', level='WARNING'):
            await self.submit(c, 'cancelled')
        await self.assert_reopened_outcome(c, 'cancelled')

    async def test_provider_failure_and_cancellation_keep_evidence_when_storage_works(self):
        for outcome in ('error', 'cancelled'):
            with self.subTest(outcome=outcome):
                c = await self.configure(outcome)
                await self.submit(c, outcome)
                saved, _, reopened = await self.assert_reopened_outcome(
                    c, outcome, 'Original provider failure' if outcome == 'error' else None)
                self.assertEqual(len(await c.message_evidence(saved.id)), 1)
                self.assertEqual(len(reopened['sources']), 1)

    async def test_stop_during_final_save_shows_the_committed_reply_live(self):
        c = await self.configure('complete')
        entered, release = await block_final_save(self.runtime.database)
        await self.session.submit('Find the source')
        await stop_during_final_save(self, self.session._generation, entered, release, stop=self.session.stop)
        saved, live, reopened = await self.assert_reopened_outcome(c, 'complete')
        self.assertIsNone(saved.error)
        self.assertEqual(live['id'], saved.id)
        self.assertEqual(len(live['sources']), 1)
        self.assertEqual(await c.message_evidence(saved.id), live['sources'])
        self.assertEqual(reopened['sources'], live['sources'])
        self.assertEqual(self.session._request_state, 'complete')

    async def test_evidence_failure_rolls_back_answer_audit_and_state(self):
        c = await self.configure('complete')
        _, pending = await c._repo.begin_turn(c.conversation.id, 'Question', c.model_name)
        registry = EvidenceRegistry(pending.id)
        original = new_report('strict', c.model_name, 'Draft.', registry)
        await c._repo.save_audit(pending.id, original, content='Draft.')
        await self.reject_evidence()
        revised = new_report('strict', c.model_name, 'Revised.', registry)
        with self.assertRaisesRegex(Exception, 'injected evidence failure'):
            await c._repo.finish_message(pending.id, 'Revised.', MessageState.COMPLETE,
                                         audit=revised, evidence=[{'title': 'source'}])
        # An unrelated commit must not publish any part of the rejected finalization.
        await c._repo.create_conversation('Unrelated')
        await self.runtime.database.close()
        await self.runtime.database.connect()
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state, MessageState.PENDING)
        self.assertEqual(saved.content, 'Draft.')
        self.assertEqual(saved.audit, original)
        self.assertEqual(await c.message_evidence(saved.id), [])

    async def test_failure_to_settle_terminal_state_still_propagates(self):
        c = await self.configure('error')
        await self.runtime.database.run(lambda conn: conn.executescript('''
            CREATE TRIGGER reject_terminal BEFORE UPDATE ON messages
            WHEN NEW.role = 'assistant' AND NEW.state != 'pending'
            BEGIN SELECT RAISE(ABORT, 'terminal storage unavailable'); END;
        '''))
        with self.assertLogs('blackrose.turns', level='WARNING'):
            with self.assertRaisesRegex(Exception, 'terminal storage unavailable'):
                await c.send('Find the source')
        self.assertEqual((await c.current_messages())[-1].state, MessageState.PENDING)
        self.assertFalse(c.is_generating)
