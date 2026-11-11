"""SM-17: Honcho backoff, profile staleness and the durable memory outbox.

See docs/state-machines/sm-17-memory-outbox.md. The tests pin the transitions the
outbox and the client rely on: one attempt counted before the send, a duplicate
check on every later attempt, conversion of an unsent turn into a forget, and the
profile mark that must precede every destructive forget.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from blackrose.memory import CARD_STALE, HonchoMemory, MemoryOutbox, MemoryUnavailable
from blackrose.storage.records import MessageState
from test_gui_images import make_runtime
from test_memory import StatefulHoncho


class OutboxHoncho:
    """A small Honcho stand-in with per-path faults, one optional gate and a log.

    ``fail`` raises a transport error for matching paths, ``reject`` answers 404 (as
    a workspace Honcho no longer has would), and a gate holds the first matching
    request open so a test can interleave a user action with an in-flight drain.
    """

    def __init__(self, gate_path=None):
        self.calls = []
        self.fail = set()
        self.reject = set()
        self.gate_path = gate_path
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.listed = []
        self.batches = []
        self.deleted = []

    async def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if self.gate_path and path.endswith(self.gate_path) and not self.entered.is_set():
            self.entered.set()
            await self.gate.wait()
        if any(path.endswith(suffix) for suffix in self.fail):
            raise httpx.ConnectError('refused', request=request)
        if path.endswith('/messages/list'):
            return httpx.Response(200, json={'items': self.listed})
        if any(path.endswith(suffix) for suffix in self.reject):
            return httpx.Response(404, json={'detail': 'not found'})
        if request.method == 'DELETE' and '/sessions/' in path:
            self.deleted.append(path.rsplit('/', 1)[1])
            return httpx.Response(202, json={})
        if request.method == 'POST' and path.endswith('/messages'):
            self.batches.append(body['messages'])
            return httpx.Response(201, json={})
        return httpx.Response(200, json={'id': 'ok'})

    def calls_list(self):
        return [(method, path) for method, path, _ in self.calls]


class BackoffStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_object_honcho_says_is_missing_ends_the_pause(self):
        failing = {'on': True}

        def handler(request):
            if failing['on']:
                raise httpx.ConnectError('refused', request=request)
            return httpx.Response(404, json={'detail': 'no card'})

        memory = HonchoMemory('http://127.0.0.1:8765', transport=httpx.MockTransport(handler))
        with self.assertRaises(MemoryUnavailable):
            await memory.request('GET', '/x')
        self.assertFalse(memory.available)
        failing['on'] = False
        # A 404 under missing_ok is an answer: Honcho is reachable, the object is not
        # there. Background memory must resume instead of waiting out the window.
        self.assertEqual(await memory.card(), [])
        self.assertTrue(memory.available)
        await memory.aclose()


class OutboxStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config, self.runtime = make_runtime(self.root)
        self.config.memory_enabled = True
        self.honcho = OutboxHoncho()
        self.memory = HonchoMemory('http://127.0.0.1:8765', transport=httpx.MockTransport(self.honcho))
        c = self.runtime.controller
        c.memory = self.memory
        c._memory_outbox = MemoryOutbox(c._repo, self.memory, self.config)
        await self.runtime.open()
        self.c, self.repo = c, c._repo

    async def asyncTearDown(self):
        await self.runtime.close()
        await self.memory.aclose()
        self.temp.cleanup()

    async def settle(self):
        if self.c._memory_task is not None:
            await self.c._memory_task

    async def queue_turn(self, conversation, text='A typed preference.', answer='Noted.'):
        user, assistant = await self.repo.begin_turn(conversation.id, text, 'test-vision')
        await self.repo.finish_message(assistant.id, answer, MessageState.COMPLETE, remember=True)
        return assistant

    async def session_name(self, conversation):
        return f'bb-{await self.repo.installation_id()}-{conversation.id}'

    async def test_a_failed_send_counts_its_attempt_and_keeps_the_row(self):
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation)
        self.honcho.fail = {'/messages'}
        with self.assertRaises(MemoryUnavailable):
            await self.c._memory_outbox.drain()
        [job] = await self.repo.memory_jobs()
        # Counted before the remote call, so a retry after a crash looks first.
        self.assertEqual((job['kind'], job['attempts']), ('turn', 1))
        self.assertFalse(self.memory.available)
        self.honcho.fail = set()
        self.memory._retry_at = 0.0
        await self.c._memory_outbox.drain()
        self.assertEqual(await self.repo.memory_jobs(), [])
        self.assertEqual(len(self.honcho.batches), 1)

    async def test_a_drain_cancelled_mid_send_retries_with_a_duplicate_check(self):
        self.honcho.gate_path = '/sessions'
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation)
        task = asyncio.create_task(self.c._memory_outbox.drain())
        await asyncio.wait_for(self.honcho.entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        [job] = await self.repo.memory_jobs()
        self.assertEqual(job['attempts'], 1)
        self.assertTrue(self.memory.available)  # a cancellation is not an outage
        # As if the cancelled request had reached Honcho: the retry must look first.
        self.honcho.listed = [{'id': 'already-sent'}]
        self.honcho.gate.set()
        await self.c._memory_outbox.drain()
        self.assertEqual(self.honcho.batches, [])
        self.assertEqual(await self.repo.memory_jobs(), [])

    async def test_excluding_a_chat_converts_its_unsent_turn_into_a_forget(self):
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation)
        self.honcho.fail = {'/messages'}
        with self.assertRaises(MemoryUnavailable):
            await self.c._memory_outbox.drain()  # the row now has attempts = 1
        self.honcho.fail = set()
        self.memory._retry_at = 0.0
        await self.repo.set_memory_excluded(conversation.id, True)
        self.assertEqual([job['kind'] for job in await self.repo.memory_jobs()], ['forget'])
        await self.c._memory_outbox.drain()
        self.assertEqual(self.honcho.batches, [])
        self.assertEqual(self.honcho.deleted, [await self.session_name(conversation)])
        self.assertEqual(await self.repo.memory_jobs(), [])

    async def test_a_turn_a_drain_is_sending_is_removed_by_the_forget_that_follows(self):
        self.honcho.gate_path = '/sessions'
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation)
        task = asyncio.create_task(self.c._memory_outbox.drain())
        await asyncio.wait_for(self.honcho.entered.wait(), 5)
        # The drain already holds the row and has passed its exclusion check, so this
        # turn is delivered; the forget job it receives must remove it again.
        await self.repo.set_memory_excluded(conversation.id, True)
        self.honcho.gate.set()
        await task
        session = await self.session_name(conversation)
        self.assertEqual(len(self.honcho.batches), 1)
        self.assertEqual(self.honcho.deleted, [session])
        calls = self.honcho.calls_list()
        sent_at = calls.index(('POST', f'/v3/workspaces/blackrose/sessions/{session}/messages'))
        deleted_at = next(index for index, (method, path) in enumerate(calls)
                          if method == 'DELETE' and path.endswith(f'/sessions/{session}'))
        self.assertLess(sent_at, deleted_at)
        self.assertEqual(await self.repo.memory_jobs(), [])
        # And the exclusion holds for the next turn.
        await self.queue_turn(conversation, 'Excluded now.')
        self.assertEqual(await self.repo.memory_jobs(), [])

    async def test_memory_off_drops_turns_but_still_forgets_an_excluded_chat(self):
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation)
        self.config.memory_enabled = False
        await self.repo.set_memory_excluded(conversation.id, True)
        self.assertEqual([job['kind'] for job in await self.repo.memory_jobs()], ['forget'])
        await self.c._memory_outbox.drain()
        self.assertEqual(self.honcho.batches, [])
        self.assertEqual(self.honcho.deleted, [await self.session_name(conversation)])
        self.assertEqual(await self.repo.memory_jobs(), [])

    async def test_a_turn_settled_during_a_drain_is_sent_by_the_same_sync(self):
        self.honcho.gate_path = '/messages'
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation, 'First preference.')
        self.c.schedule_memory_sync()
        await asyncio.wait_for(self.honcho.entered.wait(), 5)
        await self.queue_turn(conversation, 'Second preference.')
        self.c.schedule_memory_sync()  # coalesced while the first drain holds the lock
        self.honcho.gate.set()
        await self.settle()
        self.assertEqual(await self.repo.memory_jobs(), [])
        self.assertEqual([batch[0]['content'] for batch in self.honcho.batches],
                         ['First preference.', 'Second preference.'])

    async def test_a_failed_sync_leaves_every_settled_turn_for_the_next_one(self):
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation, 'First preference.')
        self.honcho.fail = {'/messages'}
        self.c.schedule_memory_sync()
        await self.settle()
        self.assertEqual([job['attempts'] for job in await self.repo.memory_jobs()], [1])
        await self.queue_turn(conversation, 'Second preference.')
        self.honcho.fail = set()
        self.memory._retry_at = 0.0
        self.c.schedule_memory_sync()
        await self.settle()
        self.assertEqual(await self.repo.memory_jobs(), [])
        self.assertEqual([batch[0]['content'] for batch in self.honcho.batches],
                         ['First preference.', 'Second preference.'])

    async def test_a_rejected_setup_repeats_without_a_backoff_and_settles_nothing(self):
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation)
        self.honcho.reject = {'/v3/workspaces'}
        with self.assertRaises(MemoryUnavailable):
            await self.c._memory_outbox.drain()
        [job] = await self.repo.memory_jobs()
        self.assertEqual(job['attempts'], 0)  # no row starts: setup failed first
        self.assertTrue(self.memory.available)  # a refusal is not an outage
        calls = len(self.honcho.calls)
        with self.assertRaises(MemoryUnavailable):
            await self.c._memory_outbox.drain()
        self.assertEqual(len(self.honcho.calls), calls + 1)  # tried again at once
        self.assertEqual(len(await self.repo.memory_jobs()), 1)

    async def test_a_workspace_honcho_no_longer_has_drops_a_turn_for_good(self):
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation, 'First preference.')
        self.honcho.reject = {'/sessions'}
        await self.c._memory_outbox.drain()
        self.assertEqual(await self.repo.memory_jobs(), [])
        self.assertEqual(self.honcho.batches, [])
        # Readiness is per process: the workspace is never set up again, so the
        # dropped turn is gone rather than retried.
        self.assertEqual([path for _, path in self.honcho.calls_list() if path == '/v3/workspaces'],
                         ['/v3/workspaces'])
        self.honcho.reject = set()
        await self.queue_turn(conversation, 'Second preference.')
        await self.c._memory_outbox.drain()
        self.assertEqual([batch[0]['content'] for batch in self.honcho.batches],
                         ['Second preference.'])

    async def test_an_unexpected_error_stops_the_drain_and_keeps_every_row(self):
        conversation = await self.repo.create_conversation()
        await self.queue_turn(conversation, 'First preference.')
        await self.queue_turn(conversation, 'Second preference.')
        with patch('blackrose.turns.typed_by_user', side_effect=RuntimeError('unexpected')):
            with self.assertRaises(RuntimeError):
                await self.c._memory_outbox.drain()
        self.assertEqual([job['attempts'] for job in await self.repo.memory_jobs()], [1, 0])
        self.assertEqual(self.honcho.batches, [])
        await self.c._memory_outbox.drain()
        self.assertEqual(await self.repo.memory_jobs(), [])
        self.assertEqual(len(self.honcho.batches), 2)


    async def test_settling_the_same_turn_twice_sends_it_twice(self):
        conversation = await self.repo.create_conversation()
        assistant = await self.queue_turn(conversation, 'A typed preference.')
        await self.c._memory_outbox.drain()
        self.assertEqual(len(self.honcho.batches), 1)
        # Settling the same reply again re-queues it with attempts = 0, so the
        # duplicate check is skipped: the row's uniqueness only lasts until it settles.
        await self.repo.finish_message(assistant.id, 'Noted.', MessageState.COMPLETE, remember=True)
        [job] = await self.repo.memory_jobs()
        self.assertEqual(job['attempts'], 0)
        await self.c._memory_outbox.drain()
        self.assertEqual(len(self.honcho.batches), 2)
        self.assertEqual(self.honcho.batches[0], self.honcho.batches[1])


class StaleProfileStateTests(unittest.IsolatedAsyncioTestCase):
    """The profile mark that makes recall leave the profile out while it is rebuilt."""

    async def asyncSetUp(self):
        self.fake = StatefulHoncho()
        self.failing = {'on': False}
        original = self.fake.__call__

        def handler(request):
            if self.failing['on'] and request.method == 'DELETE' and '/conclusions/' in request.url.path:
                raise httpx.ConnectError('refused', request=request)
            return original(request)

        self.memory = HonchoMemory('http://127.0.0.1:8765', transport=httpx.MockTransport(handler))

    async def asyncTearDown(self):
        await self.memory.aclose()

    async def test_a_forget_interrupted_after_the_mark_keeps_the_profile_hidden(self):
        self.failing['on'] = True
        with self.assertRaises(MemoryUnavailable):
            await self.memory.forget_session('bb-x-1')
        self.assertTrue(self.fake.stale)  # set before the first delete, so it survives
        self.memory._retry_at = 0.0
        notes = await self.memory.recall('explain this')
        self.assertNotIn({'note': 'Prefers worked examples'}, notes)
        # Only the mark explains that: clearing it (as the maintenance job does) restores it.
        self.fake.metadata.pop(CARD_STALE)
        self.assertIn({'note': 'Prefers worked examples'}, await self.memory.recall('explain this'))
        self.fake.metadata[CARD_STALE] = '2026-11-01T00:00:00+00:00'
        self.failing['on'] = False
        await self.memory.forget_session('bb-x-1')
        self.assertTrue(self.fake.stale)  # repeating the forget does not clear it
        self.assertEqual(self.fake.card, ['Prefers worked examples'])  # kept for the rebuild
        self.memory._retry_at = 0.0
        self.assertNotIn({'note': 'Prefers worked examples'}, await self.memory.recall('explain this'))
        await self.memory.forget_everything()
        self.assertFalse(self.fake.stale)

    async def test_a_saved_profile_stays_out_of_recall_while_the_mark_holds(self):
        await self.memory.forget_session('bb-x-1')
        self.assertTrue(self.fake.stale)
        await self.memory.save_card(['Prefers worked examples'])
        self.assertEqual(await self.memory.card(), ['Prefers worked examples'])
        # Saving the profile is not a rebuild: recall still leaves it out.
        self.assertTrue(await self.memory.card_stale())
        self.assertNotIn({'note': 'Prefers worked examples'},
                         await self.memory.recall('explain this'))

    async def test_a_rebuild_request_only_sets_the_mark(self):
        self.assertFalse(await self.memory.card_stale())
        await self.memory.rebuild_card()
        self.assertTrue(self.fake.stale)
        self.assertTrue(self.fake.metadata[CARD_STALE].startswith('20'))  # a UTC timestamp
        self.assertEqual(self.fake.metadata['other'], 'kept')
        self.assertTrue(await self.memory.card_stale())

    async def test_a_forget_everything_that_fails_leaves_the_old_profile_in_recall(self):
        original = self.fake.__call__

        def stubborn(request):
            if request.method == 'DELETE' and '/conclusions/' in request.url.path:
                return httpx.Response(204)
            return original(request)

        memory = HonchoMemory('http://127.0.0.1:8765', transport=httpx.MockTransport(stubborn))
        with self.assertRaises(MemoryUnavailable):
            await memory.forget_everything()
        self.assertEqual(self.fake.sessions, set())      # the sessions did go
        self.assertEqual(self.fake.card, ['Prefers worked examples'])
        self.assertFalse(self.fake.stale)                 # and nothing marks it stale
        memory._retry_at = 0.0
        self.assertIn({'note': 'Prefers worked examples'}, await memory.recall('explain this'))
        await memory.aclose()


if __name__ == '__main__':
    unittest.main()
