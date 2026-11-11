"""Durable diagram-repair claims, terminal states and report coordination.

Pins the state machine described in `docs/state-machines/sm-08-diagram-repair.md`:
the persisted per-diagram status set, the re-claim table, the per-message budget,
promotion of a plain reply to one diagram block, and the Session queue's
deduplication and deferral rules.
"""
import asyncio
from pathlib import Path
import hashlib
import tempfile
import unittest

from blackrose.gui.session import Session
from test_gui_images import make_runtime


BAD = 'flowchart LR\nA[Start] --> B[Verify]\\nB --> C[Close]\n'
SECOND_BAD = 'flowchart TD\nX[One] --> Y[Two]\\nY --> Z[Three]\n'
GOOD = 'flowchart LR\nA[Start] --> B[Verify]\nB --> C[Close]\n'
ANSWER = 'Before.\n\n```mermaid\n' + BAD + '```\n\nAfter.'
TWO = ('Before.\n\n```mermaid\n' + BAD + '```\n\nMiddle.\n\n```mermaid\n'
       + SECOND_BAD + '```\n\nAfter.')


class Backend:
    """One scripted answer per request; an optional gate holds a request open."""

    def __init__(self):
        self.requests = []
        self.answer = ANSWER
        self.gate = None
        self.started = asyncio.Event()

    async def load(self, model):
        pass

    async def models(self, provider=None):
        return [{'id': 'test-vision'}]

    async def aclose(self):
        pass

    async def stream(self, model, messages, tools, **options):
        self.requests.append((messages, tools, options))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        yield {'delta': {'content': self.answer}, 'finish_reason': 'stop'}


class DiagramRepairStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config, self.runtime = make_runtime(Path(directory))
        self.backend = Backend()
        self.runtime.controller.backend = self.backend
        await self.runtime.open()
        self.addAsyncCleanup(self.runtime.close)
        self.events = []
        self.session = Session(self.runtime.controller, lambda kind, payload: self.events.append((kind, payload)))
        self.addAsyncCleanup(self.session.close)
        await self.session.boot()
        await self.say(ANSWER)

    async def say(self, answer):
        """Submit one complete reply; returns the stored assistant message."""
        self.backend.answer = answer
        self.backend.started.clear()
        await self.session.submit('Explain a workflow.')
        await self.session._generation
        self.backend.started.clear()
        return (await self.runtime.controller.current_messages())[-1]

    async def report(self, message, source=BAD, index=0, block_id=''):
        return await self.session.diagram_failed(message.id, block_id, index, source, 'Parse error')

    async def drain(self):
        task = self.session._diagram_repair_task
        if task:
            await asyncio.wait_for(asyncio.shield(task), 5)

    async def latest(self, message):
        return next(m for m in await self.runtime.controller.current_messages() if m.id == message.id)

    def results(self):
        return [payload['repair']['status'] for kind, payload in self.events if kind == 'diagram_update']

    # -- persisted state set -------------------------------------------------

    async def test_persisted_states_are_closed_and_every_terminal_state_stays_put(self):
        repo = self.runtime.controller._repo
        first = await self.say(ANSWER)
        claim = await repo.claim_diagram_repair(first.conversation_id, first.id, '', 0, BAD, 1)
        self.assertEqual(claim['status'], 'repairing')
        await repo.finish_diagram_repair(claim, 'stopped')
        self.assertEqual((await repo.claim_diagram_repair(
            first.conversation_id, first.id, claim['block_id'], 0, BAD, 1))['status'], 'stopped')

        second = await self.say(TWO)
        repo = self.runtime.controller._repo
        conversation, message = second.conversation_id, second.id
        claim = await repo.claim_diagram_repair(conversation, message, '', 0, BAD, 2)
        self.assertEqual(claim['status'], 'repairing')
        # A second claim is the crash/interruption recovery path, not a retry.
        self.assertEqual((await repo.claim_diagram_repair(
            conversation, message, claim['block_id'], 0, BAD, 2))['status'], 'interrupted')
        # The invalidated claim cannot finish, and the block keeps its source.
        await repo.finish_diagram_repair(claim, 'updated', GOOD)
        block = (await self.latest(second)).blocks[0]
        self.assertEqual(block['diagram_repairs']['0']['status'], 'interrupted')
        self.assertIn(BAD, block['source'])
        self.assertEqual(block['source_revision'], 1)
        self.assertEqual((await repo.claim_diagram_repair(
            conversation, message, block['block_id'], 0, BAD, 2))['status'], 'interrupted')
        # A failed attempt is terminal and does not consume another slot.
        claim = await repo.claim_diagram_repair(conversation, message, block['block_id'], 1, SECOND_BAD, 2)
        self.assertEqual(claim['status'], 'repairing')
        await repo.finish_diagram_repair(claim, 'failed')
        self.assertEqual((await repo.claim_diagram_repair(
            conversation, message, block['block_id'], 1, SECOND_BAD, 2))['status'], 'failed')
        statuses = {entry['status'] for entry in (await self.latest(second)).blocks[0]['diagram_repairs'].values()}
        self.assertLessEqual(statuses, {'repairing', 'updated', 'failed', 'stopped', 'interrupted'})

        third = await self.say(ANSWER)
        claim = await self.runtime.controller._repo.claim_diagram_repair(
            third.conversation_id, third.id, '', 0, BAD, 1)
        await self.runtime.controller._repo.finish_diagram_repair(claim, 'updated', GOOD)
        updated = (await self.latest(third)).blocks[0]
        self.assertEqual(updated['diagram_repairs']['0']['status'], 'updated')
        self.assertIn(GOOD, updated['source'])
        # The repaired diagram failing again flips `updated` to `failed` once.
        self.assertEqual((await self.runtime.controller._repo.claim_diagram_repair(
            third.conversation_id, third.id, updated['block_id'], 0, GOOD, 1))['status'], 'failed')
        self.assertEqual((await self.runtime.controller._repo.claim_diagram_repair(
            third.conversation_id, third.id, updated['block_id'], 0, GOOD, 1))['status'], 'failed')

    async def test_finish_requires_the_claim_it_started_with(self):
        message = await self.latest(await self.say(ANSWER))
        repo = self.runtime.controller._repo
        claim = await repo.claim_diagram_repair(message.conversation_id, message.id, '', 0, BAD, 1)
        await repo.finish_diagram_repair(dict(claim, source=SECOND_BAD), 'updated', GOOD)
        block = (await self.latest(message)).blocks[0]
        self.assertEqual(block['diagram_repairs']['0']['status'], 'repairing')
        self.assertEqual(block['source'], ANSWER)
        self.assertEqual(block['source_revision'], 1)

    async def test_claim_refuses_stale_sources_foreign_owners_and_unknown_blocks(self):
        message = await self.latest(await self.say(ANSWER))
        conversation = self.runtime.controller.selected_conversation.id
        repo = self.runtime.controller._repo
        self.backend.requests.clear()
        claim = await repo.claim_diagram_repair(conversation, message.id, '', 0, BAD, 1)
        block = claim['block_id']
        refused = {
            'stale source': await repo.claim_diagram_repair(conversation, message.id, block, 0, SECOND_BAD, 1),
            'out of range': await repo.claim_diagram_repair(conversation, message.id, block, 3, BAD, 1),
            'foreign chat': await repo.claim_diagram_repair(conversation + 999, message.id, block, 0, BAD, 1),
            'user message': await repo.claim_diagram_repair(
                conversation, message.id - 1, block, 0, BAD, 1),
            'unknown block': await repo.claim_diagram_repair(conversation, message.id, 'missing', 0, BAD, 1),
        }
        self.assertEqual({name: state['status'] for name, state in refused.items()}, {
            'stale source': 'stale', 'out of range': 'stale', 'foreign chat': 'unavailable',
            'user message': 'unavailable', 'unknown block': 'unavailable'})
        self.assertFalse(self.backend.requests)

    async def test_claim_requires_a_complete_assistant_message(self):
        self.backend.gate = asyncio.Event()
        self.backend.started.clear()
        await self.session.submit('Another question')
        generation = self.session._generation
        await asyncio.wait_for(self.backend.started.wait(), 5)
        conversation = self.runtime.controller.selected_conversation.id
        pending = (await self.runtime.controller._repo.messages(conversation))[-1]
        self.assertEqual(pending.state.value, 'pending')
        claim = await self.runtime.controller._repo.claim_diagram_repair(
            conversation, pending.id, '', 0, BAD, 1)
        self.assertEqual(claim['status'], 'unavailable')
        self.session.stop()
        await asyncio.gather(generation, return_exceptions=True)
        self.assertFalse((await self.latest(pending)).blocks)

    # -- budget and promotion ------------------------------------------------

    async def test_budget_is_counted_per_message_across_all_diagram_blocks(self):
        message = await self.say(TWO)
        self.config.diagram_repairs = 1
        self.backend.answer = GOOD
        self.backend.requests.clear()
        await self.report(message, BAD, 0)
        await self.report(message, SECOND_BAD, 1)
        await self.drain()
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(self.results(), ['updated', 'exhausted'])
        block = (await self.latest(message)).blocks[0]
        self.assertEqual(block['source'].count(GOOD), 1)
        self.assertEqual(block['source'].count(SECOND_BAD), 1)
        self.assertEqual(set(block['diagram_repairs']), {'0'})

    async def test_every_queued_failure_of_a_promoted_plain_reply_is_resolved(self):
        # Both reports are queued while the reply has no block. The first claim
        # promotes the reply to `diagram-{id}`; the second must resolve that same
        # block instead of falling back to "unavailable".
        message = await self.say(TWO)
        self.config.diagram_repairs = 2
        self.backend.answer = GOOD
        self.backend.requests.clear()
        await self.report(message, BAD, 0)
        await self.report(message, SECOND_BAD, 1)
        await self.drain()
        self.assertEqual(len(self.backend.requests), 2)
        self.assertEqual(self.results(), ['updated', 'updated'])
        saved = await self.latest(message)
        self.assertEqual(len(saved.blocks), 1)
        block = saved.blocks[0]
        self.assertEqual(block['block_id'], f'diagram-{message.id}')
        self.assertTrue(block['diagram_promoted'])
        self.assertEqual(block['source_revision'], 3)
        self.assertEqual(block['source_sequence'], 3)
        self.assertEqual({key: entry['status'] for key, entry in block['diagram_repairs'].items()},
                         {'0': 'updated', '1': 'updated'})

    async def test_an_unpromoted_block_is_never_guessed_from_an_empty_report(self):
        message = await self.latest(await self.say(ANSWER))
        block = {'block_id': 'original', 'message_id': message.id,
            'conversation_id': message.conversation_id, 'position': 0, 'kind': 'markdown',
            'source': message.content, 'source_revision': 1, 'source_sequence': 1,
            'protocol_version': 1, 'library_version': 'blackrose-markdown/1',
            'schema_hash': '', 'source_hash': hashlib.sha256(message.content.encode()).hexdigest(),
            'document': None, 'status': 'ready', 'fallback_text': message.content}
        await self.runtime.controller._repo.save_blocks(message.id, [block])
        self.backend.answer = GOOD
        self.backend.requests.clear()
        await self.report(message)
        await self.drain()
        self.assertEqual(self.results(), ['unavailable'])
        self.assertFalse(self.backend.requests)
        self.assertEqual((await self.latest(message)).blocks[0]['source'], ANSWER)
        # The same block is repairable when the page names it.
        await self.report(message, BAD, 0, 'original')
        await self.drain()
        self.assertEqual(self.results(), ['unavailable', 'updated'])
        self.assertEqual(len(self.backend.requests), 1)

    # -- Session queue coordination -----------------------------------------

    async def test_a_duplicate_failure_during_inference_cannot_claim_again(self):
        message = await self.latest(await self.say(ANSWER))
        self.backend.answer = GOOD
        self.backend.gate = asyncio.Event()
        self.backend.requests.clear()
        await self.report(message)
        await asyncio.wait_for(self.backend.started.wait(), 5)
        block = (await self.latest(message)).blocks[0]
        self.assertEqual(block['diagram_repairs']['0']['status'], 'repairing')
        await self.report(message)
        self.assertEqual(len(self.session._pending_diagram_failures), 1)
        self.backend.gate.set()
        await self.drain()
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(self.results(), ['updated'])
        block = (await self.latest(message)).blocks[0]
        self.assertEqual(block['diagram_repairs']['0']['status'], 'updated')
        self.assertEqual(block['source_revision'], 2)

    async def test_failures_are_queued_while_a_reply_owns_the_generation_slot(self):
        message = await self.latest(await self.say(ANSWER))
        self.backend.gate = asyncio.Event()
        self.backend.started.clear()
        await self.session.submit('Another question')
        generation = self.session._generation
        await asyncio.wait_for(self.backend.started.wait(), 5)
        self.backend.requests.clear()
        for index in range(8):
            self.assertEqual((await self.report(message, f'flowchart LR\nA{index}-->B\n'))['status'], 'queued')
        self.assertEqual(len(self.session._pending_diagram_failures), 8)
        self.assertEqual((await self.report(message, 'flowchart LR\nA8-->B\n'))['status'], 'unavailable')
        self.assertFalse(self.backend.requests)
        self.session.stop()
        await asyncio.gather(generation, return_exceptions=True)

    async def test_busy_lifecycle_refusal_drops_the_queued_failure_without_feedback(self):
        # Hazard pin (see SM-08): a lifecycle command holding `artifacts.exclusive()`
        # makes `DiagramRepairs.repair` raise ChatBusy, and Session discards the
        # whole queue without a `diagram_update`, a claim, or a retry. If the
        # refusal is ever reported to the page, this test should be updated.
        message = await self.latest(await self.say(ANSWER))
        self.backend.answer = GOOD
        self.backend.requests.clear()
        async with self.runtime.controller.artifacts.exclusive():
            with self.assertLogs('blackrose.gui.session', level='WARNING'):
                await self.report(message)
                await self.drain()
        self.assertFalse(self.backend.requests)
        self.assertEqual(self.results(), [])
        self.assertFalse((await self.latest(message)).blocks)


if __name__ == '__main__':
    unittest.main()
