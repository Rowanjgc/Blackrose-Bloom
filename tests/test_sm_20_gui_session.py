"""SM-20: desktop session lifecycle, generation slot, draft recovery, view tokens.

Pins the four interlocking sub-machines owned by `blackrose.gui.session.Session`:

* boot/availability (`_ready`, `_boot_task`, `_quit_requested`, `_status_state`),
* the single `_generation` slot shared by user turns, retry and both deferred
  repair drains (`_wait_for_repair_slot`),
* the composer draft debounce and the submitted envelope (`_draft_owner`,
  `_draft_sending`), and
* the ephemeral UI view tokens (`_ui_views`) that gate page events.

See `docs/state-machines/sm-20-gui-session.md`. The companion machines are
SM-06 (`ui_events`), SM-08 (diagram repair) and SM-09 (animation recovery).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blackrose.gui.session import Session
from blackrose.ui_documents import UIEnvelope

import test_gui_images as fixtures
from test_gui_images import make_runtime


def bare_session(**fields):
    """A Session with only the slot/permission attributes the planner reads."""
    session = Session.__new__(Session)
    session._generation = fields.get('generation')
    session._ready = fields.get('ready', True)
    session._quit_requested = fields.get('quit', False)
    session._permission_pending = fields.get('permission', False)
    session._ui_action_pending = fields.get('action_pending', False)
    return session


class GenerationSlotPlannerTests(unittest.IsolatedAsyncioTestCase):
    """`_wait_for_repair_slot` is the whole arbitration: one owner, no clobber."""

    async def test_claimants_take_the_single_slot_one_at_a_time(self):
        session = bare_session()
        order = []

        async def owner():
            await asyncio.sleep(0.02)

        session._generation = asyncio.create_task(owner())
        held = session._generation

        async def claimant(name):
            acquired = await session._wait_for_repair_slot()
            order.append((name, acquired, session._generation is asyncio.current_task(),
                          held.done()))
            await asyncio.sleep(0.01)
            self.assertIs(session._generation, asyncio.current_task())
            session._generation = None

        await asyncio.gather(claimant('first'), claimant('second'))
        self.assertEqual([name for name, *_ in order], ['first', 'second'])
        self.assertTrue(all(acquired and owned for _, acquired, owned, _ in order))
        self.assertTrue(all(held_done for *_, held_done in order))
        self.assertIsNone(session._generation)

    async def test_a_finished_slot_is_cleared_before_the_next_claim(self):
        session = bare_session()

        async def owner():
            return 'done'

        session._generation = asyncio.create_task(owner())
        await asyncio.sleep(0)
        self.assertTrue(session._generation.done())
        self.assertTrue(await session._wait_for_repair_slot())
        self.assertIs(session._generation, asyncio.current_task())

    async def test_refusals_never_take_or_clobber_the_slot(self):
        for fields in ({'permission': True}, {'ready': False}, {'quit': True}):
            with self.subTest(**fields):
                session = bare_session(**fields)
                self.assertFalse(await session._wait_for_repair_slot())
                self.assertIsNone(session._generation)

    async def test_a_refusal_after_waiting_leaves_the_slot_free(self):
        session = bare_session(permission=True)
        started = asyncio.Event()

        async def owner():
            started.set()

        session._generation = asyncio.create_task(owner())
        self.assertFalse(await session._wait_for_repair_slot())
        self.assertIsNone(session._generation)
        self.assertTrue(started.is_set())

    async def test_a_cancelled_claimant_does_not_cancel_the_owner(self):
        session = bare_session()
        release = asyncio.Event()

        async def owner():
            await release.wait()

        session._generation = asyncio.create_task(owner())
        owner = session._generation
        claimant = asyncio.create_task(session._wait_for_repair_slot())
        await asyncio.sleep(0.01)
        claimant.cancel()
        await asyncio.sleep(0)
        self.assertTrue(claimant.cancelled())
        self.assertIs(session._generation, owner)
        self.assertFalse(owner.cancelled())
        release.set()
        await owner
        self.assertIs(session._generation, owner)

    async def test_busy_covers_the_action_interlock_and_a_live_task(self):
        session = bare_session(action_pending=True)
        self.assertTrue(session._busy())
        session._ui_action_pending = False
        self.assertFalse(session._busy())

        async def nothing():
            return None

        done = asyncio.create_task(nothing())
        await done
        session._generation = done
        self.assertFalse(session._busy())


class SessionFixture(unittest.IsolatedAsyncioTestCase):
    """A real controller/repository on scratch XDG and storage directories."""

    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            name: str(Path(directory) / child) for name, child in (
                ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache'))}))
        self.root = Path(directory)
        self.config, self.runtime = make_runtime(self.root)
        await self.runtime.open()
        self.addAsyncCleanup(self.runtime.close)
        self.events = []
        self.session = Session(self.runtime.controller,
                               lambda kind, payload: self.events.append((kind, payload)))
        self.addAsyncCleanup(self.session.close)

    @property
    def controller(self):
        return self.runtime.controller

    async def boot(self):
        await self.session.boot()

    def kinds(self):
        return [kind for kind, _ in self.events]

    def latest(self, kind):
        return next(payload for name, payload in reversed(self.events) if name == kind)

    def all_of(self, kind):
        return [payload for name, payload in self.events if name == kind]

    async def block_loading(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def load(model):
            started.set()
            await release.wait()

        self.controller.backend.load = load
        return started, release

    async def gated_stream(self):
        """Replace the provider stream with one that waits for the returned Event."""
        entered, release = asyncio.Event(), asyncio.Event()

        async def stream(model, messages, tools):
            entered.set()
            await release.wait()
            yield {'delta': {'content': 'answer'}, 'finish_reason': 'stop'}

        self.controller.backend.stream = stream
        return entered, release

    async def draft_rows(self):
        return await self.runtime.database.run(lambda conn: [dict(row) for row in conn.execute(
            'SELECT conversation_id, text, sending FROM composer_drafts ORDER BY conversation_id')])

    async def settle_draft(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        for _ in range(3):
            await asyncio.sleep(0)
        while self.session._draft_save is not None or (
                self.session._draft_save_task is not None
                and not self.session._draft_save_task.done()):
            if loop.time() > deadline:
                raise AssertionError('the composer draft never settled')
            await asyncio.sleep(0.01)


class BootStateTests(SessionFixture):
    async def test_nothing_is_admitted_until_a_transcript_and_model_are_ready(self):
        started, release = await self.block_loading()
        boot = asyncio.create_task(self.session.boot())
        try:
            await asyncio.wait_for(started.wait(), 5)
            # The transcript and the composer owner exist before the model loads.
            self.assertFalse(self.session._ready)
            self.assertIs(self.session._boot_task, boot)
            self.assertEqual(self.session._draft_owner, self.controller.conversation.id)
            self.assertIn('transcript', self.kinds())
            # Every admission path is closed, and none of them takes the slot.
            self.assertEqual(await self.session.submit('Early question'), {'clear': False})
            self.assertFalse(await self.session.retry())
            self.assertEqual(await self.session.ui_action({}),
                             {'ok': False, 'error': 'Wait for the current reply to finish'})
            self.assertIsNone(self.session._generation)
            self.assertIn('bell', self.kinds())
            self.session.update_draft('Typed while loading')
            self.assertEqual(self.session._draft_text, 'Typed while loading')
            self.assertEqual(await self.draft_rows(), [])
        finally:
            release.set()
            await boot
        self.assertTrue(self.session._ready)
        self.assertIsNone(self.session._boot_task)
        self.assertEqual(self.latest('status')['state'], 'Ready')

    async def test_a_failed_boot_stays_unavailable_until_a_model_load_succeeds(self):
        async def fail(model):
            raise RuntimeError('Provider offline')

        self.controller.backend.load = fail
        await self.boot()
        status = self.latest('status')
        self.assertFalse(self.session._ready)
        self.assertEqual(status['state'], 'Backend unavailable')
        self.assertEqual(status['availability'], 'Test Vision Unavailable')
        self.assertIn('Cannot connect', self.latest('notice')['html'])
        # The unavailable state refuses every admission and queued repair.
        self.assertEqual(await self.session.submit('Anything'), {'clear': False})
        self.assertFalse(await self.session.retry())
        self.assertEqual((await self.session.diagram_failed(
            1, 'b', 0, 'graph TD; A-->B', 'boom'))['status'], 'unavailable')
        self.assertEqual((await self.session.animation_failed(1, 'boom'))['status'], 'unavailable')
        self.assertIsNone(self.session._generation)

        # A later successful selection is the only exit from Backend unavailable.
        self.controller.backend.load = fixtures.Backend().load
        self.assertEqual(await self.session.select_model('test-vision', False), {'ok': True})
        self.assertTrue(self.session._ready)
        self.assertEqual(self.latest('status')['state'], 'Ready')
        self.controller.backend.stream = fixtures.Backend().stream
        self.assertEqual(await self.session.submit('Now it works'), {'clear': True})
        await self.session._generation

    async def test_a_diagram_report_is_accepted_while_boot_is_still_loading(self):
        started, release = await self.block_loading()
        boot = asyncio.create_task(self.session.boot())
        try:
            await asyncio.wait_for(started.wait(), 5)
            # The page renders the first transcript before the model loads, so
            # its render failures still enter the queue (they wait for the boot).
            self.assertEqual((await self.session.diagram_failed(
                1, 'diagram-1', 0, 'graph TD; A-->B', 'Parse error'))['status'], 'queued')
            repair = self.session._diagram_repair_task
            self.assertIsNotNone(repair)
            self.assertIsNone(self.session._generation)
        finally:
            release.set()
            await boot
        await asyncio.wait_for(repair, 5)
        self.assertIsNone(self.session._generation)
        self.assertEqual(self.session._pending_diagram_failures, {})

    async def test_a_diagram_report_after_a_failed_boot_is_refused(self):
        async def offline(model):
            raise RuntimeError('Provider offline')

        self.controller.backend.load = offline
        await self.boot()
        self.assertFalse(self.session._ready)
        self.assertIsNone(self.session._boot_task)
        self.assertEqual((await self.session.diagram_failed(
            1, 'diagram-1', 0, 'graph TD; A-->B', 'Parse error'))['status'], 'unavailable')
        self.assertIsNone(self.session._diagram_repair_task)


class GenerationSlotTests(SessionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.boot()

    async def test_a_queued_repair_waits_for_the_reply_and_never_shares_the_slot(self):
        entered, release = await self.gated_stream()
        await self.session.submit('Question')
        await entered.wait()
        reply = self.session._generation
        self.assertEqual((await self.session.diagram_failed(
            1, 'diagram-1', 0, 'graph TD; A-->B', 'boom'))['status'], 'queued')
        repair = self.session._diagram_repair_task
        self.assertIsNotNone(repair)
        self.assertIsNot(repair, reply)
        # The reservation is global: a user turn, a retry and an action cannot
        # start beside a streaming reply, queued repair or not.
        self.assertEqual(await self.session.submit('Second'), {'clear': False})
        self.assertFalse(await self.session.retry())
        self.assertEqual(await self.session.ui_action({}),
                         {'ok': False, 'error': 'Wait for the current reply to finish'})
        self.assertIs(self.session._generation, reply)
        release.set()
        await reply
        await asyncio.wait_for(repair, 5)
        self.assertIsNone(self.session._generation)
        self.assertIsNone(self.session._diagram_repair_task)

    async def test_stop_cancels_a_queued_repair_and_releases_the_slot(self):
        entered, release = await self.gated_stream()
        await self.session.submit('Question')
        await entered.wait()
        await self.session.diagram_failed(1, 'diagram-1', 0, 'graph TD; A-->B', 'boom')
        self.assertTrue(self.session.stop())
        self.assertEqual(self.session._pending_diagram_failures, {})
        self.assertEqual(self.latest('diagram_update')['repair'], {'status': 'stopped'})
        with self.assertRaises(asyncio.CancelledError):
            await self.session._generation
        await asyncio.sleep(0)
        self.assertIsNone(self.session._generation)
        # The repair task was cancelled before its first step, so its own
        # `finally` never ran: the field keeps the finished task.
        self.assertTrue(self.session._diagram_repair_task.done())
        self.assertEqual(self.latest('reply_end')['state'], 'cancelled')
        release.set()

    async def test_stop_with_no_work_only_rings_the_bell(self):
        self.assertFalse(self.session.stop())
        self.assertIn('bell', self.kinds())
        self.assertNotIn('Stopping…', [p['state'] for p in self.all_of('status')])

    async def test_close_cancels_queued_repairs_and_marks_the_session_closed(self):
        entered, release = await self.gated_stream()
        await self.session.submit('Question')
        await entered.wait()
        await self.session.diagram_failed(1, 'diagram-1', 0, 'graph TD; A-->B', 'boom')
        await self.session.close()
        self.assertTrue(self.session._quit_requested)
        self.assertEqual(self.session._pending_diagram_failures, {})
        self.assertTrue(self.session._diagram_repair_task.done())
        self.assertIsNone(self.session._generation)
        release.set()

    async def test_submit_and_retry_refuse_once_quit_is_requested(self):
        # H4: close() sets _quit_requested and then keeps awaiting, so a send or
        # retry can arrive while it shuts down. Neither may take the slot.
        await self.session.submit('Stored question')
        await self.session._generation
        finished = self.session._generation
        self.session._draft_text = 'typed while closing'
        self.session._quit_requested = True
        self.events.clear()
        self.assertEqual(await self.session.submit('Sent while closing'), {'clear': False})
        self.assertFalse(await self.session.retry())
        self.assertEqual(self.kinds(), ['bell', 'bell'])
        self.assertIs(self.session._generation, finished)
        self.assertEqual(self.session._draft_text, 'typed while closing')
        self.assertEqual(len(await self.controller.current_messages()), 2)

    async def test_retry_reservation_is_stoppable_before_it_reads_anything(self):
        await self.session.submit('Stored question')
        await self.session._generation
        self.assertTrue(await self.session.retry())
        retry = self.session._generation
        self.assertIsNotNone(retry)
        self.assertTrue(self.session.stop())
        with self.assertRaises(asyncio.CancelledError):
            await retry
        self.assertIsNone(self.session._generation)
        self.assertEqual(len(await self.controller.current_messages()), 2)
        self.assertEqual(self.session._request_state, 'cancelled')


class ReplyEpochTests(SessionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.boot()

    async def test_the_epoch_advances_once_on_start_and_once_on_settle(self):
        start = self.session._reply_epoch
        entered, release = await self.gated_stream()
        await self.session.submit('Question')
        await entered.wait()
        streaming = self.session._reply_epoch
        self.assertEqual(streaming, start + 1)
        release.set()
        await self.session._generation
        self.assertEqual(self.session._reply_epoch, streaming + 1)

    async def test_a_transcript_read_restarts_when_a_reply_settles_mid_read(self):
        calls = {'page': 0, 'evidence': 0}
        page = self.controller.conversation_message_page
        evidence = self.controller.messages_evidence

        async def counted_page(conversation_id, **window):
            calls['page'] += 1
            return await page(conversation_id, **window)

        async def bumping_evidence(message_ids):
            calls['evidence'] += 1
            if calls['evidence'] == 1:
                # A stream settles while the transcript is being assembled.
                self.session._reply_epoch += 1
            return await evidence(message_ids)

        self.controller.conversation_message_page = counted_page
        self.controller.messages_evidence = bumping_evidence
        await self.session._transcript_payload()
        self.assertEqual(calls['page'], 2)

    async def test_a_settled_reply_cannot_emit_a_late_renderer_callback(self):
        self.config.interactive_ui_enabled = True
        settled = {}
        original = self.controller.send

        async def send(text, **kwargs):
            settled['delta'] = kwargs['on_delta']
            settled['ui'] = kwargs['on_ui']
            return await original(text, **kwargs)

        self.controller.send = send
        await self.session.submit('First question')
        await self.session._generation
        self.assertIsNone(self.session._active_reply)

        entered, release = await self.gated_stream()
        self.controller.send = original
        await self.session.submit('Second question')
        await entered.wait()
        active = self.session._active_reply
        self.assertIsNotNone(active)
        before = len(self.events)
        # Replaying the settled reply's callbacks must not touch the new reply.
        settled['delta']('stale delta')
        settled['ui']([{'block_id': 'late-ui', 'kind': 'interactive_ui',
                        'saved_state': {}, 'state_sequence': 1}])
        await asyncio.sleep(0.05)
        self.assertEqual([kind for kind, _ in self.events[before:]
                          if kind in ('reply_update', 'ui_update', 'reasoning')], [])
        self.assertIs(self.session._active_reply, active)
        self.assertNotIn('stale delta', active.renderer.text)
        self.assertNotIn('late-ui', [block.get('block_id') for block in active.blocks])
        release.set()
        await self.session._generation


class DraftRecoveryTests(SessionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(patch('blackrose.gui.session.DRAFT_SAVE_SECONDS', 0))
        await self.boot()
        self.first = self.controller.conversation.id
        await self.session.new_conversation()
        self.second = self.controller.conversation.id

    async def park_a_request(self):
        """Submit on `first`, hold it before admission, then go to `second`."""
        started, release = await self.block_loading()
        await self.session.open_conversation(self.first)
        await self.session.submit('Unstored request')
        await started.wait()
        self.assertFalse(self.controller.last_request_stored)
        self.assertTrue(await self.session.open_conversation(self.second))
        return release

    async def test_leaving_a_reply_parks_its_envelope_on_its_own_chat(self):
        release = await self.park_a_request()
        self.assertIsNone(self.session._draft_sending)
        row, = await self.draft_rows()
        self.assertEqual(row['conversation_id'], self.first)
        self.assertIn('Unstored request', row['sending'])
        self.assertTrue(self.session.stop())
        with self.assertRaises(asyncio.CancelledError):
            await self.session._generation
        # The visible chat keeps its own empty composer.
        self.assertEqual(self.session._draft_text, '')
        self.assertFalse([p for p in self.all_of('restore_draft') if 'Unstored' in str(p)])
        release.set()
        # Opening the request's chat is the documented recovery point.
        self.assertTrue(await self.session.open_conversation(self.first))
        self.assertIn('Unstored request', self.latest('restore_draft')['text'])
        await self.settle_draft()
        row, = await self.draft_rows()
        self.assertIsNone(row['sending'])
        self.assertIn('Unstored request', row['text'])

    async def test_returning_to_the_reply_rearms_the_envelope_for_stop(self):
        release = await self.park_a_request()
        self.assertIsNone(self.session._draft_sending)
        # Coming back re-arms the in-memory envelope from the live reply.
        self.assertTrue(await self.session.open_conversation(self.first))
        self.assertIsNotNone(self.session._draft_sending)
        self.assertEqual(self.session._draft_sending['text'], 'Unstored request')
        self.assertTrue(self.session.stop())
        with self.assertRaises(asyncio.CancelledError):
            await self.session._generation
        self.assertIn('Unstored request', self.latest('restore_draft')['text'])
        self.assertIsNone(self.session._draft_sending)
        self.assertEqual(self.session._draft_text, 'Unstored request')
        release.set()

    async def test_a_submit_in_the_ownerless_redraw_window_is_refused(self):
        from test_turns import Backend

        await self.session.new_conversation()
        doomed = self.controller.conversation.id
        self.controller.backend = Backend(context=4096)
        text = 'x' * 10000
        original = self.controller.conversation_message_page
        observed = []

        async def page(conversation_id, **window):
            if not observed and self.session._draft_owner is None:
                observed.append(self.session._ready)
                # The composer is mid-replacement: a write would be refused, so the
                # submission is refused rather than accepted with no recovery.
                self.assertEqual(await self.session.submit(text), {'clear': False})
            return await original(conversation_id, **window)

        self.controller.conversation_message_page = page
        self.assertTrue(await self.session.delete_conversation(doomed))
        self.assertEqual(observed, [True])
        # Nothing was admitted, so nothing had to be restored.
        self.assertIsNone(self.session._generation)
        await self.settle_draft()
        self.assertEqual(self.session._draft_text, '')
        self.assertIsNone(self.session._draft_sending)
        self.assertEqual(await self.draft_rows(), [])
        self.assertEqual(self.session._draft_owner, self.controller.conversation.id)

    async def test_a_save_is_refused_while_the_composer_has_no_owner(self):
        self.session._draft_owner = None
        self.session.update_draft('Ownerless typing')
        await self.session._flush_draft()
        self.assertEqual(await self.draft_rows(), [])
        self.assertEqual(self.session._draft_text, 'Ownerless typing')

    async def test_the_debounced_save_always_writes_the_newest_state(self):
        self.session.update_draft('first')
        self.session.update_draft('second')
        self.session.update_draft('third')
        await self.settle_draft()
        row, = await self.draft_rows()
        self.assertEqual(row['text'], 'third')
        self.assertEqual(row['conversation_id'], self.second)


class UIViewTokenTests(SessionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.boot()
        self.conversation = self.controller.conversation.id

    def interactive(self, block_id='ui-1'):
        return {'block_id': block_id, 'kind': 'interactive_ui', 'source': '{}',
                'status': 'ready', 'position': 0, 'source_revision': 1}

    def envelope(self, view_id, block_id='ui-1', conversation_id=None):
        return UIEnvelope(conversation_id or self.conversation, 1, block_id, 1, view_id)

    def token(self, block_id='ui-1'):
        return self.session._ui_payload([self.interactive(block_id)])[0]['view_id']

    async def test_every_complete_redraw_rotates_the_token_and_refuses_the_old_one(self):
        first = self.token()
        self.assertEqual(self.session._ui_views['ui-1'], first)
        await self.session.render_transcript()
        second = self.token()
        self.assertNotEqual(first, second)
        with self.assertRaisesRegex(ValueError, 'obsolete'):
            self.session._check_ui_view(self.envelope(first))
        self.assertEqual(self.session._check_ui_view(self.envelope(second)), self.conversation)

    async def test_a_token_from_another_conversation_is_refused(self):
        view = self.token()
        await self.session.new_conversation()
        with self.assertRaisesRegex(ValueError, 'another conversation'):
            self.session._check_ui_view(self.envelope(view))

    async def test_an_obsolete_token_cannot_start_a_ui_action(self):
        stale = self.token()
        await self.session.render_transcript()
        self.token()
        result = await self.session.ui_action({
            'conversation_id': self.conversation, 'assistant_message_id': 1,
            'block_id': 'ui-1', 'source_revision': 1, 'view_id': stale,
            'event_id': 'event-1234', 'action_id': 'submit',
            'action_type': 'continue_conversation', 'values': {}})
        self.assertEqual(result, {'ok': False,
                                  'error': 'UI view is obsolete; reload this conversation'})
        self.assertIsNone(self.session._generation)
        self.assertFalse(self.session._ui_action_pending)

    async def test_the_action_interlock_closes_the_other_admissions(self):
        self.session._ui_action_pending = True
        try:
            self.assertEqual(await self.session.submit('Beside an action'), {'clear': False})
            self.assertFalse(await self.session.retry())
            self.assertTrue(self.session._busy())
            self.assertIsNone(self.session._generation)
        finally:
            self.session._ui_action_pending = False


class PermissionInterlockTests(SessionFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.boot()

    async def test_a_pending_native_decision_refuses_every_admission(self):
        self.session._permission_pending = True
        try:
            self.assertEqual(await self.session.submit('While asking'), {'clear': False})
            self.assertFalse(await self.session.retry())
            self.assertEqual(await self.session.ui_action({}),
                             {'ok': False, 'error': 'Wait for the current reply to finish'})
            self.assertEqual((await self.session.diagram_failed(
                1, 'b', 0, 'graph TD; A-->B', 'boom'))['status'], 'unavailable')
            self.assertEqual((await self.session.animation_failed(1, 'boom'))['status'],
                             'unavailable')
            self.assertFalse(self.session._status_payload()['can_retry'])
            self.assertIsNone(self.session._generation)
        finally:
            self.session._permission_pending = False
        self.assertEqual(await self.session.submit('After the answer'), {'clear': True})
        await self.session._generation

    async def test_renderer_settlement_waits_for_the_native_decision(self):
        entered, release = await self.gated_stream()
        await self.session.submit('Question')
        await entered.wait()
        generation = self.session._generation
        self.session._permission_pending = True
        self.assertEqual(
            await Session.settle_after_renderer_termination(self.session, 0.05),
            'permission_pending')
        self.assertFalse(generation.cancelled())
        self.session._permission_pending = False
        self.assertEqual(
            await Session.settle_after_renderer_termination(self.session, 0.5), 'settled')
        self.assertIsNone(self.session._generation)
        release.set()

    async def test_stop_is_not_blocked_by_a_pending_decision(self):
        entered, release = await self.gated_stream()
        await self.session.submit('Question')
        await entered.wait()
        self.session._permission_pending = True
        self.assertTrue(self.session.stop())
        with self.assertRaises(asyncio.CancelledError):
            await self.session._generation
        # The prompt still belongs to the user; Stop never answers it.
        self.assertTrue(self.session._permission_pending)
        self.session._permission_pending = False
        release.set()


if __name__ == '__main__':
    unittest.main()
