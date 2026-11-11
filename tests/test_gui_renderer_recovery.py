"""Bounded state and cancellation behavior for native renderer recovery."""
from concurrent.futures import Future
import asyncio
import unittest

from blackrose.gui.renderer_recovery import RendererRecovery
from blackrose.gui.session import Session


class _SessionStub:
    _permission_pending = False
    _generation = None

    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if self._generation is not None:
            self._generation.cancel()


class _FutureApi:
    def __init__(self, *results):
        self.results = list(results)

    def _renderer_termination_future(self, _timeout):
        future = Future()
        future.set_result(self.results.pop(0))
        return future


class _InlineThread:
    def __init__(self, *, target, args, **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


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


class _Dialog:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.buttons = []
        self.response_handler = None
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
    uid = "test-window"
    real_url = "file:///fixture/index.html"
    native = object()

    def __init__(self):
        self.loads = []
        self.destroyed = False

    def load_url(self, url):
        self.loads.append(url)

    def destroy(self):
        self.destroyed = True


class SessionRendererSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_recovery_has_nothing_to_cancel(self):
        session = _SessionStub()
        result = await Session.settle_after_renderer_termination(session, 0.1)
        self.assertEqual(result, "idle")
        self.assertEqual(session.stop_calls, 0)

    async def test_active_generation_is_cancelled_and_settled(self):
        session = _SessionStub()
        started = asyncio.Event()

        async def generation():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(generation())
        session._generation = task
        await started.wait()
        result = await Session.settle_after_renderer_termination(session, 0.5)
        self.assertEqual(result, "settled")
        self.assertTrue(task.cancelled())
        self.assertEqual(session.stop_calls, 1)

    async def test_pending_permission_is_not_cancelled_or_decided_by_host(self):
        session = _SessionStub()
        session._permission_pending = True
        started = asyncio.Event()

        async def generation():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(generation())
        session._generation = task
        await started.wait()
        result_task = asyncio.create_task(
            Session.settle_after_renderer_termination(session, 0.5))
        await asyncio.sleep(0.05)
        self.assertFalse(task.cancelled())
        self.assertEqual(session.stop_calls, 0)
        session._permission_pending = False
        self.assertEqual(await result_task, "settled")
        self.assertTrue(task.cancelled())

    async def test_slow_cancellation_cleanup_returns_busy_then_can_settle(self):
        session = _SessionStub()
        started = asyncio.Event()
        cleanup = asyncio.Event()

        async def generation():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await cleanup.wait()

        task = asyncio.create_task(generation())
        session._generation = task
        await started.wait()
        first = await Session.settle_after_renderer_termination(session, 0.05)
        self.assertEqual(first, "busy")
        self.assertFalse(task.done())
        cleanup.set()
        await task
        second = await Session.settle_after_renderer_termination(session, 0.1)
        self.assertEqual(second, "idle")


class RendererRecoveryStateTests(unittest.TestCase):
    def setUp(self):
        self.window = _Window()
        self.glib = _GLib()
        self.gtk = _Gtk()

    def recovery(self, *results):
        self.api = _FutureApi(*results)
        return RendererRecovery(self.window, self.api, self.glib,
                                gtk=self.gtk, thread_factory=_InlineThread)

    def test_first_failure_settles_then_reloads_without_page_replay(self):
        recovery = self.recovery("settled")
        recovery._web_process_terminated(None, 0)
        self.assertEqual(self.window.loads, [self.window.real_url])
        self.assertTrue(recovery._awaiting_ready())
        self.assertEqual(self.gtk.dialogs, [])
        recovery.page_ready()
        self.assertFalse(recovery._awaiting_ready())

    def test_repeated_failure_has_native_fallback_and_finite_reload_budget(self):
        recovery = self.recovery(*(["settled"] * 6))
        recovery._web_process_terminated(None, 0)
        recovery.page_ready()
        recovery._web_process_terminated(None, 0)
        for expected_loads in (2, 3):
            dialog = self.gtk.dialogs[-1]
            self.assertIn(("Reload page", recovery.RELOAD), dialog.buttons)
            self.assertIn(("Stop reply", recovery.STOP), dialog.buttons)
            self.assertIn(("Close window", recovery.CLOSE), dialog.buttons)
            dialog.respond(recovery.RELOAD)
            self.assertEqual(len(self.window.loads), expected_loads)
            self.assertTrue(dialog.destroyed)
            recovery.page_ready()

        recovery._web_process_terminated(None, 0)
        bounded = self.gtk.dialogs[-1]
        self.assertNotIn(("Reload page", recovery.RELOAD), bounded.buttons)
        self.assertIn(("Close window", recovery.CLOSE), bounded.buttons)

    def test_pending_permission_suppresses_recovery_dialog_until_user_decides(self):
        recovery = self.recovery("permission_pending", "settled")
        recovery._web_process_terminated(None, 0)
        self.assertEqual(self.window.loads, [])
        self.assertEqual(self.gtk.dialogs, [])
        self.assertTrue(recovery._waiting_permission)
        callback, args = next(iter(self.glib.timeouts.values()))
        callback(*args)
        self.assertEqual(self.window.loads, [self.window.real_url])

    def test_repeated_failure_waits_for_existing_permission_dialog(self):
        recovery = self.recovery("settled", "permission_pending", "settled")
        recovery._web_process_terminated(None, 0)
        recovery.page_ready()
        recovery._web_process_terminated(None, 0)
        self.assertEqual(self.window.loads, [self.window.real_url])
        self.assertEqual(self.gtk.dialogs, [])
        callback, args = next(iter(self.glib.timeouts.values()))
        callback(*args)
        self.assertEqual(len(self.gtk.dialogs), 1)
        self.assertEqual(self.window.loads, [self.window.real_url])

    def test_native_close_response_destroys_host_window(self):
        recovery = self.recovery("busy", "settled")
        recovery._web_process_terminated(None, 0)
        self.gtk.dialogs[-1].respond(recovery.CLOSE)
        self.assertTrue(self.window.destroyed)

    def test_window_close_dismisses_fallback_and_marks_recovery_closed(self):
        recovery = self.recovery("busy")
        recovery._web_process_terminated(None, 0)
        dialog = self.gtk.dialogs[-1]
        recovery.window_closed()
        self.assertTrue(recovery._closed)
        self.assertTrue(dialog.destroyed)
        self.assertIsNone(recovery._dialog)

    def test_stop_button_settles_without_reloading_or_dismissing_fallback(self):
        recovery = self.recovery("busy", "settled")
        recovery._web_process_terminated(None, 0)
        dialog = self.gtk.dialogs[-1]
        dialog.respond(recovery.STOP)
        self.assertEqual(self.window.loads, [])
        self.assertIs(recovery._dialog, dialog)
        self.assertFalse(dialog.destroyed)
        self.assertIn("stopped", dialog.detail)


if __name__ == "__main__":
    unittest.main()
