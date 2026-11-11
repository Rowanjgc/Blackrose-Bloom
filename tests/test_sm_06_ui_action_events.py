"""SM-06: the `ui_events` in-flight lock, acceptance and settlement.

Pins the lifecycle of one interactive-answer submission: the durable
`accepted -> started -> complete|cancelled|error|interrupted` row, the partial
unique index that makes the first two of those an exclusive lock per block, and
the paths in `Session.ui_action` / `TurnRunner` that must settle it.
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from copy import deepcopy
from unittest.mock import patch

from blackrose.ui_documents import UIEnvelope, contract

import test_gui_images as fixtures
from test_turns import Backend


def form_source():
    return json.dumps(deepcopy(contract()['examples']['form']))


class UIActionStateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    # --- fixtures ---------------------------------------------------------

    async def publish_form(self):
        """Publish one conversation whose last block is a ready form."""
        c = self.runtime.controller
        c.config.interactive_ui_enabled = True
        c.config.interactive_ui_actions_enabled = True
        c.backend = Backend([[('activate_tools', {'capability': 'interactive_ui', 'profile': 'form'})],
                             form_source()])
        await c.send('Collect my reading preferences.')
        await self.session.render_transcript()
        block = next(p for k, p in reversed(self.events) if k == 'transcript')['messages'][-1]['blocks'][-1]
        self.assertEqual(block['status'], 'ready')
        return c, block

    def payload(self, block, event, values=None):
        return {'conversation_id': block['conversation_id'], 'assistant_message_id': block['message_id'],
                'block_id': block['block_id'], 'source_revision': 1, 'view_id': block['view_id'],
                'event_id': event, 'action_id': 'submit', 'action_type': 'continue_conversation',
                'values': {'days': 4} if values is None else values}

    async def rows(self, c):
        return await c._repo._db.run(lambda conn: [dict(row) for row in conn.execute(
            'SELECT event_id, status, user_message_id, action_id FROM ui_events ORDER BY rowid')])

    async def status(self, c, event):
        return await c._repo._db.run(lambda conn: (row := conn.execute(
            'SELECT status FROM ui_events WHERE event_id=?', (event,)).fetchone()) and row[0])

    async def accept(self, c, block, event, values=None):
        """Accept through the collaborator, skipping the ephemeral view token."""
        body = self.payload(block, event, values)
        owned = await c.interactive_answers.owned_block(
            UIEnvelope.parse(body), block['conversation_id'], for_action=True)
        return await c.interactive_answers.accept_action(owned, body)

    async def wait_for_status(self, c, event, wanted, timeout=5.0):  # noqa: ASYNC109 - poll deadline
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self.status(c, event) == wanted:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f'{event} never reached {wanted!r}: {await self.rows(c)}')

    def slow_backend(self, entered, release, text='Partial answer.'):
        backend = Backend([text])
        base = backend.stream

        async def stream(model, messages, tools):
            async for choice in base(model, messages, tools):
                yield choice
            entered.set()
            await release.wait()

        backend.stream = stream
        return backend

    # --- the lock ---------------------------------------------------------

    async def test_inflight_index_refuses_a_second_action_on_the_same_block(self):
        c, block = await self.publish_form()
        schema = await c._repo._db.run(lambda conn: conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_ui_inflight'").fetchone()[0])
        self.assertIn('UNIQUE INDEX', schema)
        self.assertIn("WHERE status IN ('accepted', 'started')", schema)

        await self.accept(c, block, 'first_action_0123456', {'days': 4})
        # A different submission, a different event identity, the same block.
        with self.assertRaisesRegex(ValueError, 'Duplicate or in-flight'):
            await self.accept(c, block, 'second_action_012345', {'days': 6})
        self.assertEqual([row['event_id'] for row in await self.rows(c)], ['first_action_0123456'])
        # The only writer passes action_id='submit', so the index's second column
        # is constant: one in-flight submission per block, whatever the action.
        self.assertEqual({row['action_id'] for row in await self.rows(c)}, {'submit'})

    async def test_settled_event_frees_the_block_and_a_new_identity_is_required(self):
        c, block = await self.publish_form()
        c.backend = Backend(['Your plan.'])
        payload = self.payload(block, 'settled_action_012345')
        self.assertTrue((await self.session.ui_action(payload))['ok'])
        await self.session._generation
        self.assertEqual(await self.status(c, 'settled_action_012345'), 'complete')

        # The same identity is refused forever; the block itself is free again.
        self.assertFalse((await self.session.ui_action(payload))['ok'])
        c.backend = Backend(['A second plan.'])
        self.assertTrue((await self.session.ui_action(
            self.payload(block, 'followup_action_01234')))['ok'])
        await self.session._generation
        self.assertEqual([row['status'] for row in await self.rows(c)], ['complete', 'complete'])

    # --- acceptance guards ------------------------------------------------

    async def test_accept_action_refuses_inputs_before_writing_any_row(self):
        c, block = await self.publish_form()
        base = self.payload(block, 'guard_action_01234567')
        owned = await c.interactive_answers.owned_block(
            UIEnvelope.parse(base), block['conversation_id'], for_action=True)
        c.config.interactive_ui_actions_enabled = False
        with self.assertRaisesRegex(ValueError, 'disabled'):
            await c.interactive_answers.accept_action(owned, base)
        c.config.interactive_ui_actions_enabled = True
        refusals = [
            (base | {'action_type': 'delete'}, 'Unsupported UI action type'),
            (base | {'event_id': 'short'}, 'Invalid UI event identity'),
            (base | {'event_id': None}, 'Invalid UI event identity'),
            (base | {'action_id': 'file_edit'}, 'Undeclared UI action'),
            (base | {'values': {'days': True}}, 'outside allowed range'),
            (base | {'values': {'days': 4, 'extra': 'x'}}, 'Unknown form fields'),
            (base | {'values': None}, 'State must be an object'),
        ]
        for body, reason in refusals:
            with self.subTest(body=body):
                with self.assertRaisesRegex(ValueError, reason):
                    await c.interactive_answers.accept_action(owned, body)
        self.assertEqual(await self.rows(c), [])
        # Nothing was consumed: the validated identity is still usable.
        await self.accept(c, block, 'guard_action_01234567')
        self.assertEqual(len(await self.rows(c)), 1)

    # --- acceptance -> turn ------------------------------------------------

    async def test_acceptance_reads_as_accepted_before_begin_turn_and_started_after(self):
        c, block = await self.publish_form()
        observed = {}
        context_read = c._repo.ui_event_context
        begin_turn = c._repo.begin_turn

        async def read_context(event_id, conversation_id):
            observed['context'] = await self.status(c, event_id)
            return await context_read(event_id, conversation_id)

        async def start(*args, **kwargs):
            observed['begin_turn'] = await self.status(c, kwargs['ui_event_id'])
            result = await begin_turn(*args, **kwargs)
            observed['after_begin_turn'] = await self.status(c, kwargs['ui_event_id'])
            return result

        with patch.object(c._repo, 'ui_event_context', side_effect=read_context), \
             patch.object(c._repo, 'begin_turn', side_effect=start):
            c.backend = Backend(['Your plan.'])
            self.assertTrue((await self.session.ui_action(
                self.payload(block, 'order_action_01234567')))['ok'])
            await self.session._generation
        self.assertEqual(observed['context'], 'accepted')
        self.assertEqual(observed['begin_turn'], 'accepted')
        self.assertEqual(observed['after_begin_turn'], 'started')
        self.assertEqual(await self.status(c, 'order_action_01234567'), 'complete')

    async def test_failed_begin_turn_does_not_consume_the_accepted_event(self):
        c, block = await self.publish_form()
        accepted = await self.accept(c, block, 'atomic_action_0123456')
        with patch('blackrose.storage.repository._attach_all', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                await c._repo.begin_turn(c.conversation.id, accepted.text, c.model_name,
                                         ui_event_id=accepted.event_id)
        # One transaction: the status change and the two message rows roll back together.
        self.assertEqual(await self.status(c, accepted.event_id), 'accepted')
        self.assertEqual(len(await c.current_messages()), 2)
        request, _ = await c._repo.begin_turn(c.conversation.id, accepted.text, c.model_name,
                                              ui_event_id=accepted.event_id)
        row = (await self.rows(c))[0]
        self.assertEqual((row['status'], row['user_message_id']), ('started', request.id))

    async def test_consumed_event_cannot_start_a_second_turn(self):
        c, block = await self.publish_form()
        c.backend = Backend(['One answer.'])
        self.assertTrue((await self.session.ui_action(
            self.payload(block, 'once_action_012345678')))['ok'])
        await self.session._generation
        before = await self.rows(c)
        messages = len(await c.current_messages())
        with self.assertRaisesRegex(ValueError, 'already consumed'):
            await c._repo.begin_turn(c.conversation.id, 'again', c.model_name,
                                     ui_event_id='once_action_012345678')
        self.assertEqual(await self.rows(c), before)
        self.assertEqual(len(await c.current_messages()), messages)

    async def test_view_rotation_between_acceptance_and_turn_start_settles_interrupted(self):
        c, block = await self.publish_form()
        original = c.interactive_answers.accept_action

        async def rotate(owned, body):
            result = await original(owned, body)
            await self.session.redraw()          # rotates every view token
            return result

        c.backend = Backend(['Must not run.'])
        with patch.object(c.interactive_answers, 'accept_action', side_effect=rotate):
            result = await self.session.ui_action(
                self.payload(block, 'rotate_action_0123456'))
        self.assertFalse(result['ok'])
        self.assertIn('obsolete', result['error'])
        self.assertEqual(await self.rows(c), [{'event_id': 'rotate_action_0123456',
                                               'status': 'interrupted',
                                               'user_message_id': None,
                                               'action_id': 'submit'}])
        self.assertEqual(c.backend.requests, [])
        self.assertIsNone(self.session._generation)
        self.assertEqual(len(await c.current_messages()), 2)

    # --- settlement --------------------------------------------------------

    async def test_turn_cancelled_after_consumption_settles_the_event(self):
        c, block = await self.publish_form()
        entered, release = asyncio.Event(), asyncio.Event()
        c.backend = self.slow_backend(entered, release)
        self.assertTrue((await self.session.ui_action(
            self.payload(block, 'cancel_action_0123456')))['ok'])
        await self.wait_for_status(c, 'cancel_action_0123456', 'started')
        self.assertTrue(self.session.stop())
        await asyncio.wait_for(entered.wait(), 5)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.session._generation
        row = (await self.rows(c))[0]
        self.assertEqual(row['status'], 'cancelled')
        self.assertIsNotNone(row['user_message_id'])
        stored = await c.current_messages()
        self.assertEqual((stored[-1].state.value, stored[-1].reply_to),
                         ('cancelled', row['user_message_id']))

    async def test_pre_turn_failure_releases_the_event_as_error(self):
        c, block = await self.publish_form()

        async def boom(*args, **kwargs):
            raise RuntimeError('injected provider failure')

        c.send = boom
        self.assertTrue((await self.session.ui_action(
            self.payload(block, 'boom_action_012345678')))['ok'])
        await self.wait_for_status(c, 'boom_action_012345678', 'error')
        row = (await self.rows(c))[0]
        self.assertIsNone(row['user_message_id'])
        self.assertEqual(len(await c.current_messages()), 2)   # nothing was stored

    async def test_a_cancelled_caller_between_acceptance_and_turn_start_strands_the_lock(self):
        """The one live-session leak: the caller dies in the acceptance-to-task gap.

        ``Session.ui_action`` accepts, then creates the turn task.  A cancellation
        that arrives in between (possible only for a caller that is not the GTK
        bridge, which never cancels the submitted coroutine) unwinds without
        settling the row.  Nothing in a live session releases it; the block's
        submit is refused until the next startup recovery.
        """
        c, block = await self.publish_form()
        original = c.interactive_answers.accept_action
        entered, release = asyncio.Event(), asyncio.Event()

        async def linger(owned, body):
            result = await original(owned, body)
            entered.set()
            await release.wait()
            return result

        with patch.object(c.interactive_answers, 'accept_action', side_effect=linger):
            task = asyncio.create_task(self.session.ui_action(
                self.payload(block, 'stranded_action_01234')))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await self.status(c, 'stranded_action_01234'), 'accepted')
        self.assertIsNone(self.session._generation)
        self.assertFalse(self.session._ui_action_pending)

        # The stale row still holds idx_ui_inflight for this block.
        refused = await self.session.ui_action(self.payload(block, 'retry_action_01234567'))
        self.assertFalse(refused['ok'])
        self.assertIn('in-flight', refused['error'])
        self.assertEqual(len(await c.current_messages()), 2)

        # Startup recovery is the only release, and it is idempotent.
        await c._repo.reap_pending()
        self.assertEqual(await self.status(c, 'stranded_action_01234'), 'interrupted')
        self.assertEqual(await c._repo.reap_pending(), 0)
        self.assertEqual(await self.status(c, 'stranded_action_01234'), 'interrupted')
        c.backend = Backend(['Recovered.'])
        self.assertTrue((await self.session.ui_action(
            self.payload(block, 'retry_action_01234567')))['ok'])
        await self.session._generation

    async def test_settlement_cancelled_while_queued_on_the_writer_is_lost(self):
        """The database layer completes a write it has started, not one it queued."""
        c, block = await self.publish_form()
        accepted = await self.accept(c, block, 'queued_action_01234567')
        holder = asyncio.create_task(c._repo._db.run(lambda conn: time.sleep(0.3)))
        await asyncio.sleep(0.01)                    # the writer lock is now held
        settler = asyncio.create_task(
            c.interactive_answers.finish(accepted.event_id, 'cancelled'))
        await asyncio.sleep(0.05)                    # settler waits on Database._lock
        settler.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await settler
        await holder
        self.assertEqual(await self.status(c, accepted.event_id), 'accepted')

        # Once the operation reaches its worker, cancellation no longer loses it.
        settler = asyncio.create_task(
            c.interactive_answers.finish(accepted.event_id, 'cancelled'))
        await asyncio.sleep(0)
        settler.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await settler
        self.assertEqual(await self.status(c, accepted.event_id), 'cancelled')

    async def test_reap_during_a_live_turn_flips_a_started_row_and_settlement_overwrites_it(self):
        c, block = await self.publish_form()
        entered, release = asyncio.Event(), asyncio.Event()
        c.backend = self.slow_backend(entered, release)
        self.assertTrue((await self.session.ui_action(
            self.payload(block, 'live_action_012345678')))['ok'])
        await self.wait_for_status(c, 'live_action_012345678', 'started')

        # Only a second writer can reach this during a live turn (the product has
        # one caller, chat.py:231, at boot).  It marks the live row interrupted.
        await c._repo.reap_pending()
        self.assertEqual(await self.status(c, 'live_action_012345678'), 'interrupted')
        with self.assertRaisesRegex(ValueError, 'stale'):
            await c._repo.ui_event_context('live_action_012345678', c.conversation.id)

        # The turn's own unconditional settlement then rewrites the terminal row.
        release.set()
        await self.session._generation
        self.assertEqual(await self.status(c, 'live_action_012345678'), 'complete')

    # --- repository-boundary gaps (documented, not fixed here) -------------

    async def test_finish_refuses_outcomes_outside_the_terminal_vocabulary(self):
        c, block = await self.publish_form()
        accepted = await self.accept(c, block, 'vocabulary_action_012')
        for outcome in ('accepted', 'started', 'pending', '', None):
            with self.subTest(outcome=outcome):
                with self.assertRaisesRegex(ValueError, 'Invalid UI event outcome'):
                    await c.interactive_answers.finish(accepted.event_id, outcome)
        self.assertEqual(await self.status(c, accepted.event_id), 'accepted')

    async def test_finish_rewrites_a_settled_row_and_accepts_out_of_vocabulary_status_documented_follow_up(self):
        c, block = await self.publish_form()
        accepted = await self.accept(c, block, 'rewrite_action_0123456')
        await c.interactive_answers.finish(accepted.event_id, 'complete')
        # No transition guard at the repository boundary: a settled row is rewritten.
        await c.interactive_answers.finish(accepted.event_id, 'cancelled')
        self.assertEqual(await self.status(c, accepted.event_id), 'cancelled')

        # A status outside the vocabulary is accepted, is not reaped, and is not
        # covered by idx_ui_inflight, so it silently releases the block.
        await c._repo.finish_ui_event(accepted.event_id, 'not-a-status')
        await c._repo.reap_pending()
        self.assertEqual(await self.status(c, accepted.event_id), 'not-a-status')
        await self.accept(c, block, 'after_bogus_01234567')
        self.assertEqual(len(await self.rows(c)), 2)

    async def test_begin_turn_links_an_event_from_another_conversation_documented_follow_up(self):
        c, block = await self.publish_form()
        accepted = await self.accept(c, block, 'foreign_action_0123456')
        other = await c.new_conversation()
        request, _ = await c._repo.begin_turn(other.id, accepted.text, c.model_name,
                                              ui_event_id=accepted.event_id)
        # The event's block belongs to the first conversation; the request does not.
        self.assertEqual(request.conversation_id, other.id)
        self.assertEqual(await self.status(c, 'foreign_action_0123456'), 'started')
        with self.assertRaisesRegex(ValueError, 'stale or belongs'):
            await c._repo.ui_event_context('foreign_action_0123456', block['conversation_id'])


if __name__ == '__main__':
    unittest.main()
