"""SM-21 invariants: renderer recovery, the consent sheet, hand-off and the emitter.

No GTK, WebKit or display is needed. Recovery runs against a fake window, GLib and
GTK message dialog; the consent sheet is built with ``__new__`` and stub widgets;
the emitter delivers to a window whose ``evaluate_js`` records the batch. The tests
pin transition guards, the reload budget, terminal-event survival and the two
"stranded obligation" races that were fixed.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import inspect
from pathlib import Path
import signal
from types import ModuleType, SimpleNamespace
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from blackrose.gui import external
from blackrose.gui.bridge import Api, Emitter, coalesce
from blackrose.gui.consent import WIDER, ConsentSheet
from blackrose.gui.renderer_recovery import RendererRecovery


# --- fakes for RendererRecovery -------------------------------------------------


class _FutureApi:
    """Hands out pre-answered settlement futures; an exception result raises."""

    def __init__(self, *results):
        self.results = list(results)

    def _renderer_termination_future(self, _timeout):
        value = self.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, Future):
            return value
        future = Future()
        future.set_result(value)
        return future


class _GLib:
    SOURCE_REMOVE = False

    def __init__(self):
        self.timeouts = {}
        self.removed = []
        self._next_id = 1

    def idle_add(self, callback, *args):
        return callback(*args)

    def timeout_add(self, _milliseconds, callback, *args):
        identifier = self._next_id
        self._next_id += 1
        self.timeouts[identifier] = (callback, args)
        return identifier

    def timeout_add_seconds(self, _seconds, callback, *args):
        return self.timeout_add(0, callback, *args)

    def source_remove(self, identifier):
        self.removed.append(identifier)
        self.timeouts.pop(identifier, None)

    def fire(self, identifier):
        entry = self.timeouts.pop(identifier, None)
        if entry is not None:
            callback, args = entry
            callback(*args)
        return entry is not None


class _Dialog:
    def __init__(self, **_kwargs):
        self.buttons = []
        self.response_handler = None
        self.detail = ''
        self.destroyed = False

    def set_title(self, _title):
        pass

    def format_secondary_text(self, text):
        self.detail = text

    def add_button(self, label, response):
        self.buttons.append((label, response))

    def connect(self, _signal, handler):
        self.response_handler = handler

    def show_all(self):
        pass

    def destroy(self):
        self.destroyed = True

    def respond(self, response):
        self.response_handler(self, response)


class _Gtk:
    class DialogFlags:
        MODAL = 1

    class MessageType:
        ERROR = 2

    class ButtonsType:
        NONE = 0

    def __init__(self):
        self.dialogs = []

    def MessageDialog(self, **kwargs):
        dialog = _Dialog(**kwargs)
        self.dialogs.append(dialog)
        return dialog


class _Window:
    uid = 'test-window'
    real_url = 'file:///fixture/index.html'
    native = object()

    def __init__(self):
        self.loads = []
        self.destroyed = False

    def load_url(self, url):
        self.loads.append(url)

    def destroy(self):
        self.destroyed = True


class _InlineThread:
    def __init__(self, *, target, args, **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def _deferred_factory(store):
    """A thread factory that records the worker so a test runs it explicitly."""
    def make(*, target, args, **_kwargs):
        worker = SimpleNamespace(run=lambda: target(*args))
        store.append(worker)
        return SimpleNamespace(start=lambda: None)
    return make


class _FailOnceThread:
    """Fails the first ``start`` (as an exhausted thread pool would), then runs inline."""

    def __init__(self):
        self.failures = 1

    def __call__(self, *, target, args, **_kwargs):
        outer = self

        class Worker:
            def start(self):
                if outer.failures:
                    outer.failures -= 1
                    raise RuntimeError("can't start new thread")
                target(*args)

        return Worker()


class RendererRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.window = _Window()
        self.glib = _GLib()
        self.gtk = _Gtk()

    def recovery(self, *results, thread_factory=_InlineThread):
        self.api = _FutureApi(*results)
        return RendererRecovery(self.window, self.api, self.glib,
                                gtk=self.gtk, thread_factory=thread_factory)

    def crash(self, recovery):
        recovery._web_process_terminated(None, 0)

    def test_the_first_termination_settles_then_reloads_once_and_a_handshake_ends_it(self):
        recovery = self.recovery('settled')
        self.crash(recovery)
        self.assertEqual(self.window.loads, [self.window.real_url])
        self.assertEqual(recovery._reload_count, 1)
        self.assertTrue(recovery._reloading)
        self.assertEqual(self.gtk.dialogs, [])
        ready = recovery._ready_timeout_id
        self.assertIsNotNone(ready)

        recovery.page_ready()
        self.assertFalse(recovery._reloading)
        self.assertIsNone(recovery._ready_timeout_id)
        self.assertIn(ready, self.glib.removed)

    def test_a_termination_while_settling_ends_in_the_fallback_without_a_reload(self):
        workers = []
        recovery = self.recovery('settled', thread_factory=_deferred_factory(workers))
        self.crash(recovery)
        self.assertTrue(recovery._settling)
        self.crash(recovery)  # The renderer died again before settlement finished.
        self.assertTrue(recovery._failure_during_settlement)
        self.assertEqual(self.window.loads, [])

        workers[0].run()
        self.assertFalse(recovery._settling)
        self.assertEqual(self.window.loads, [])
        self.assertEqual(len(self.gtk.dialogs), 1)
        self.assertIn('repeatedly', self.gtk.dialogs[0].detail)

    def test_a_crash_during_reload_ends_the_handshake_epoch(self):
        recovery = self.recovery('settled', 'settled', 'settled')
        self.crash(recovery)
        ready = recovery._ready_timeout_id
        self.assertTrue(recovery._awaiting_ready())

        self.crash(recovery)  # The page died while the host waited for its handshake.
        self.assertFalse(recovery._awaiting_ready())
        self.assertIsNone(recovery._ready_timeout_id)
        self.assertIn(ready, self.glib.removed)
        self.assertEqual(len(self.gtk.dialogs), 1)
        self.assertIn('during recovery', self.gtk.dialogs[0].detail)

        # The fallback is terminal for that attempt, but a later termination can
        # still start a fresh settlement instead of being folded into it.
        self.assertEqual(len(self.api.results), 2)
        self.crash(recovery)
        self.assertEqual(len(self.api.results), 1)
        self.assertEqual(len(self.gtk.dialogs), 1)

    def test_an_unanswered_reload_fails_at_the_ready_timeout(self):
        recovery = self.recovery('settled')
        self.crash(recovery)
        ready = recovery._ready_timeout_id
        self.assertTrue(self.glib.fire(ready))
        self.assertFalse(recovery._reloading)
        self.assertEqual(len(self.gtk.dialogs), 1)
        self.assertIn('did not become ready', self.gtk.dialogs[0].detail)
        self.assertIn(('Reload page', recovery.RELOAD), self.gtk.dialogs[0].buttons)

    def test_the_reload_budget_is_three_loads_and_the_bounded_fallback_offers_no_reload(self):
        recovery = self.recovery(*['settled'] * 10)
        self.crash(recovery)
        self.assertEqual(len(self.window.loads), 1)
        recovery.page_ready()

        for expected in (2, 3):
            self.crash(recovery)  # Second and later failures never reload automatically.
            dialog = self.gtk.dialogs[-1]
            self.assertIn(('Reload page', recovery.RELOAD), dialog.buttons)
            dialog.respond(recovery.RELOAD)
            self.assertEqual(len(self.window.loads), expected)
            self.assertTrue(dialog.destroyed)
            recovery.page_ready()

        self.crash(recovery)
        bounded = self.gtk.dialogs[-1]
        self.assertNotIn(('Reload page', recovery.RELOAD), bounded.buttons)
        self.assertEqual(recovery._reload_count, recovery.MAX_RELOADS)
        bounded.respond(recovery.STOP)
        self.assertEqual(len(self.window.loads), 3)

    def test_a_pending_permission_pauses_recovery_until_the_poll_resumes_it(self):
        recovery = self.recovery('permission_pending', 'settled')
        self.crash(recovery)
        self.assertTrue(recovery._waiting_permission)
        self.assertEqual(self.window.loads, [])
        self.assertEqual(self.gtk.dialogs, [])
        poll = recovery._permission_timer_id
        self.assertIsNotNone(poll)

        self.glib.fire(poll)
        self.assertFalse(recovery._waiting_permission)
        self.assertEqual(self.window.loads, [self.window.real_url])
        self.assertTrue(recovery._reloading)
        recovery.page_ready()

    def test_a_crash_while_waiting_for_permission_ends_in_the_fallback(self):
        recovery = self.recovery('permission_pending', 'settled', 'settled')
        self.crash(recovery)
        self.crash(recovery)
        self.assertTrue(recovery._waiting_permission)
        self.assertTrue(recovery._failure_during_settlement)

        self.glib.fire(recovery._permission_timer_id)
        self.assertFalse(recovery._waiting_permission)
        self.assertEqual(self.window.loads, [])
        self.assertEqual(len(self.gtk.dialogs), 1)

    def test_stop_settles_without_reloading_and_keeps_the_fallback(self):
        recovery = self.recovery('busy', 'settled')
        self.crash(recovery)
        dialog = self.gtk.dialogs[-1]
        dialog.respond(recovery.STOP)
        self.assertEqual(self.window.loads, [])
        self.assertIs(recovery._dialog, dialog)
        self.assertFalse(dialog.destroyed)
        self.assertIn('stopped', dialog.detail)

    def test_closing_during_settlement_discards_the_result_and_later_signals(self):
        workers = []
        recovery = self.recovery('settled', thread_factory=_deferred_factory(workers))
        self.crash(recovery)
        self.assertTrue(recovery._settling)

        recovery.window_closed()
        self.assertTrue(recovery._closed)
        workers[0].run()  # The worker still reports back after the window is gone.
        self.assertFalse(recovery._settling)
        self.assertEqual(self.gtk.dialogs, [])
        self.assertEqual(self.window.loads, [])

        self.crash(recovery)
        self.assertEqual(self.gtk.dialogs, [])
        self.assertTrue(recovery._cleanup_done)

    def test_a_failed_settlement_worker_shows_the_fallback_instead_of_stranding_recovery(self):
        recovery = self.recovery('settled', 'settled', thread_factory=_FailOnceThread())
        self.crash(recovery)  # The worker cannot start: no callback would ever clear _settling.
        self.assertFalse(recovery._settling)
        self.assertEqual(len(self.gtk.dialogs), 1)
        self.assertIn('could not be settled', self.gtk.dialogs[0].detail)

        # Recovery is not wedged: the next termination starts a settlement that runs.
        self.crash(recovery)
        self.assertEqual(self.api.results, [])
        self.assertEqual(len(self.gtk.dialogs), 1)
        self.assertIn('stopped', self.gtk.dialogs[0].detail)

    def test_an_unavailable_session_goes_straight_to_the_fallback(self):
        recovery = self.recovery('settled')
        recovery._api = lambda: None
        self.crash(recovery)
        self.assertFalse(recovery._settling)
        self.assertEqual(len(self.gtk.dialogs), 1)
        self.assertIn('session has closed', self.gtk.dialogs[0].detail)


# --- fakes for ConsentSheet -----------------------------------------------------


class _Timers:
    SOURCE_REMOVE = False

    def __init__(self):
        self.timers = {}
        self.removed = []
        self._next = 1

    def timeout_add(self, _milliseconds, callback):
        identifier = self._next
        self._next += 1
        self.timers[identifier] = callback
        return identifier

    def source_remove(self, identifier):
        self.removed.append(identifier)
        self.timers.pop(identifier, None)

    def fire(self, identifier):
        callback = self.timers.pop(identifier, None)
        if callback is not None:
            callback()
        return callback is not None


class _Widget:
    def __init__(self, *, focused=False):
        self.focused = focused
        self.sensitive = True
        self.active = True
        self.text = None

    def set_sensitive(self, value):
        self.sensitive = value

    def get_sensitive(self):
        return self.sensitive

    def has_focus(self):
        return self.focused

    def grab_focus(self):
        self.focused = True

    def set_active(self, value):
        self.active = value

    def get_active(self):
        return self.active

    def set_text(self, value):
        self.text = value

    def set_reveal_child(self, value):
        self.revealed = value


def _sheet(*, wider=False, checks=None):
    """A sheet with its widgets stubbed; only the decision logic runs."""
    sheet = ConsentSheet.__new__(ConsentSheet)
    sheet._glib = _Timers()
    sheet._gdk = SimpleNamespace(keyval_name=lambda value: value)
    sheet._accept = _Widget()
    sheet._decline = _Widget(focused=True)
    sheet._wider = _Widget()
    sheet._revealer = _Widget()
    sheet._scrim = _Widget()
    sheet._webview = _Widget()
    sheet._closed = False
    sheet._current = {'answer': False, 'done': threading.Event(), 'wider': wider, 'checks': checks}
    sheet._arm()
    return sheet


def _press(sheet, key):
    return sheet._key(None, SimpleNamespace(keyval=key))


class ConsentSheetTests(unittest.TestCase):
    def test_no_accept_path_works_before_the_arm_timer_fires(self):
        sheet = _sheet(wider=True)
        state = sheet._current
        self.assertTrue(_press(sheet, 'a'))
        sheet._answer(True)          # The button's clicked handler.
        sheet._answer(WIDER)        # The wider button's clicked handler.
        self.assertIs(sheet._current, state)
        self.assertFalse(sheet._accept.get_sensitive())
        self.assertFalse(sheet._wider.get_sensitive())

        sheet._glib.fire(state['timer'])
        self.assertTrue(sheet._accept.get_sensitive())
        _press(sheet, 'y')
        self.assertIs(state['answer'], True)
        self.assertIsNone(sheet._current)

    def test_refocus_restarts_exactly_one_arm_delay_and_disarms_again(self):
        sheet = _sheet()
        state = sheet._current
        first = state['timer']
        sheet._refocus()  # Before the first delay elapses: the old timer is cancelled.
        second = state['timer']
        self.assertNotEqual(second, first)
        self.assertIn(first, sheet._glib.removed)
        self.assertEqual(list(sheet._glib.timers), [second])

        sheet._glib.fire(second)
        self.assertTrue(state['armed'])
        sheet._refocus()  # Regaining focus after it armed disarms it again.
        self.assertFalse(state['armed'])
        self.assertFalse(sheet._accept.get_sensitive())
        _press(sheet, 'a')
        self.assertIs(sheet._current, state)

        sheet._glib.fire(state['timer'])
        _press(sheet, 'a')
        self.assertIs(state['answer'], True)

    def test_enter_and_space_never_accept(self):
        for focused_decline in (True, False):
            sheet = _sheet()
            sheet._glib.fire(sheet._current['timer'])
            sheet._decline.focused = focused_decline
            state = sheet._current
            for key in ('Return', 'KP_Enter', 'space'):
                _press(sheet, key)
            self.assertIsNot(state['answer'], True)
            self.assertEqual(state['done'].is_set(), focused_decline)

    def test_declining_before_arming_always_restores_the_page(self):
        for key in ('Escape', 'd', 'N'):
            sheet = _sheet()
            sheet._webview.set_sensitive(False)
            _press(sheet, key)
            self.assertIsNone(sheet._current)
            self.assertTrue(sheet._webview.get_sensitive())

    def test_every_answer_restores_webview_sensitivity(self):
        for key, expected in (('a', True), ('s', WIDER)):
            sheet = _sheet(wider=True)
            sheet._webview.set_sensitive(False)
            sheet._glib.fire(sheet._current['timer'])
            state = sheet._current
            _press(sheet, key)
            self.assertEqual(state['answer'], expected)
            self.assertTrue(sheet._webview.get_sensitive())

    def test_a_window_closed_before_the_queued_sheet_runs_releases_the_caller(self):
        class Queued:
            SOURCE_REMOVE = False

            def __init__(self):
                self.queued = []

            def idle_add(self, callback, *args):
                self.queued.append((callback, args))
                return 1

        sheet = ConsentSheet.__new__(ConsentSheet)
        sheet._glib = Queued()
        sheet._turn = threading.Lock()
        sheet._current = None
        sheet._closed = False
        result = []
        caller = threading.Thread(target=lambda: result.append(sheet.ask('T', 'M', 'Allow')), daemon=True)
        caller.start()
        deadline = time.monotonic() + 2
        while not sheet._glib.queued and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(sheet._glib.queued, 'the caller never queued the sheet')

        sheet._destroyed()  # The window dies before GTK runs the queued show callback.
        caller.join(2)
        self.assertFalse(caller.is_alive(), 'a destroyed window left the approval caller blocked')
        self.assertEqual(result, [False])


# --- external hand-off ----------------------------------------------------------


class ExternalHandoffTests(unittest.TestCase):
    def handler(self):
        return external.Handler('Example Browser', 'your default web browser', None,
                                SimpleNamespace(launch_uris=Mock(return_value=True)))

    def test_a_raising_question_does_not_leave_the_handoff_mutex_held(self):
        app = self.handler()
        with patch.object(external, 'handler_for', return_value=app), \
                patch.object(external, 'ask', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                external.hand_off(object(), external.link('https://example.com/'))

        with patch.object(external, 'handler_for', return_value=app), \
                patch.object(external, 'ask', return_value=False):
            self.assertFalse(external.hand_off(object(), external.link('https://example.com/')))
        app.app.launch_uris.assert_not_called()

    def test_launch_happens_only_after_the_mutex_is_released(self):
        app = self.handler()
        seen = []

        def launch(uris, _context):
            seen.append(external._asking.locked())
            return True

        app.app.launch_uris.side_effect = launch
        with patch.object(external, 'handler_for', return_value=app), \
                patch.object(external, 'ask', return_value=True):
            self.assertTrue(external.hand_off(object(), external.link('https://example.com/')))
        self.assertEqual(seen, [False])


# --- emitter and coalescer ------------------------------------------------------


class CoalesceTests(unittest.TestCase):
    def test_a_terminal_is_never_dropped_and_later_updates_do_not_cross_it(self):
        batch = [
            ('reply_update', {'key': 1, 'html': 'old'}),
            ('reply_update', {'key': 1, 'html': 'new'}),
            ('reply_end', {'key': 1, 'state': 'complete'}),
            ('reply_update', {'key': 1, 'html': 'after'}),
        ]
        out = coalesce(batch)
        self.assertEqual([kind for kind, _ in out],
                         ['reply_update', 'reply_end', 'reply_update'])
        self.assertEqual(out[0][1]['html'], 'new')
        self.assertEqual(out[2][1]['html'], 'after')

    def test_a_snapshot_separates_coalescing_epochs(self):
        events = [
            ('reasoning', {'key': 1, 'text': 'Before'}),
            ('reply_update', {'key': 1, 'html': 'Before'}),
            ('snapshot', {'active': {'key': 1, 'reasoning': 'Before'}}),
            ('reasoning', {'key': 1, 'text': 'After'}),
            ('reply_update', {'key': 1, 'html': 'After'}),
        ]
        self.assertEqual(coalesce(events), events)

    def test_reasoning_deltas_join_in_order_within_one_epoch(self):
        out = coalesce([
            ('reasoning', {'key': 1, 'text': 'a'}),
            ('reasoning', {'key': 1, 'text': 'b'}),
            ('audit_update', {'key': 1, 'audit': {}}),
        ])
        self.assertEqual(out[0], ('reasoning', {'key': 1, 'text': 'ab'}))
        self.assertEqual([kind for kind, _ in out], ['reasoning', 'audit_update'])

    def test_update_streams_of_different_kinds_never_coalesce_together(self):
        out = coalesce([
            ('reply_update', {'key': 1, 'html': 'h'}),
            ('ui_update', {'key': 1, 'blocks': []}),
        ])
        self.assertEqual(len(out), 2)

    def test_cross_key_coalescing_moves_the_surviving_payload_earlier(self):
        # Documented in the spec (H5): the newest payload keeps the first
        # occurrence's slot, so it passes interleaved events of other keys.
        out = coalesce([
            ('reply_update', {'key': 1, 'html': 'old'}),
            ('audit_update', {'key': 1, 'audit': {'status': 'checking'}}),
            ('reply_update', {'key': 1, 'html': 'new'}),
        ])
        self.assertEqual([kind for kind, _ in out], ['reply_update', 'audit_update'])
        self.assertEqual(out[0][1]['html'], 'new')


class EmitterTests(unittest.TestCase):
    class _Page:
        def __init__(self):
            self.scripts = []
            self.terminal = threading.Event()

        def evaluate_js(self, script):
            self.scripts.append(script)
            if '"reply_end"' in script:
                self.terminal.set()

    def test_a_terminal_queued_before_close_reaches_the_page_exactly_once(self):
        page = self._Page()
        emitter = Emitter()
        emitter.attach(page)
        emitter.start()
        try:
            emitter.page_ready()
            emitter('reply_update', {'key': 1, 'html': 'first'})
            emitter('reply_end', {'key': 1, 'state': 'complete'})
            self.assertTrue(page.terminal.wait(2), 'the terminal event was never delivered')
        finally:
            emitter.close()
        delivered = ''.join(page.scripts)
        self.assertEqual(delivered.count('"reply_end"'), 1)
        self.assertIn('"complete"', delivered)


class ReadyTests(unittest.TestCase):
    class Loop:
        def __init__(self):
            self.submitted = []

        def submit(self, coro):
            self.submitted.append(coro)
            if inspect.iscoroutine(coro):
                asyncio.run(coro)
            return None

    class Session:
        def __init__(self):
            self.boot_calls = 0
            self.redraw_calls = 0

        def boot(self):
            self.boot_calls += 1
            return 'boot'

        async def redraw(self):
            self.redraw_calls += 1

        def initial_state(self):
            return {'view': 'state'}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_ready_boots_once_then_redraws_and_signals_both_recovery_hooks(self):
        api = Api.__new__(Api)
        api._emitter = SimpleNamespace(page_ready=Mock())
        api._session = self.Session()
        api._loop = self.Loop()
        api._booted = False
        api._renderer_recovery = None
        api._layout_path = Path(self.temporary.name) / 'window.json'

        state = api.ready()
        self.assertEqual(state, {'view': 'state', 'layout': {}, 'engine': 'webkitgtk'})
        self.assertEqual(api._s.boot_calls, 1)
        self.assertEqual(api._s.redraw_calls, 0)
        api._emitter.page_ready.assert_called_once_with()

        recovery = SimpleNamespace(page_ready=Mock())
        api._renderer_recovery = lambda: recovery
        api.ready()
        self.assertEqual(api._s.boot_calls, 1)       # Never boots twice.
        self.assertEqual(api._s.redraw_calls, 1)     # A reload redraws instead.
        self.assertEqual(api._emitter.page_ready.call_count, 2)
        self.assertEqual(recovery.page_ready.call_count, 1)


# --- exit signals ---------------------------------------------------------------


class ExitSignalTests(unittest.TestCase):
    def test_the_first_signal_restores_the_default_before_destroying_the_window(self):
        calls = []

        class GLib:
            PRIORITY_HIGH = 0
            SOURCE_REMOVE = False

            @staticmethod
            def unix_signal_add(priority, sig, callback, argument):
                calls.append((sig, callback, argument))
                return 1

        repository = ModuleType('gi.repository')
        repository.GLib = GLib
        window = SimpleNamespace(destroy=Mock())
        with patch.dict(sys.modules, {'gi': ModuleType('gi'), 'gi.repository': repository}), \
                patch.object(signal, 'signal') as install:
            from blackrose.gui.app import _route_exit_signals
            _route_exit_signals(window)
            self.assertEqual([sig for sig, _, _ in calls],
                             [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
            for sig, callback, argument in calls:
                self.assertEqual(argument, sig)
                self.assertFalse(callback(sig))
        self.assertEqual(install.call_args_list,
                         [call(sig, signal.SIG_DFL) for sig in
                          (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)])
        self.assertEqual(window.destroy.call_count, 3)


if __name__ == '__main__':
    unittest.main()
