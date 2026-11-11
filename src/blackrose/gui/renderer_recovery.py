"""Bounded host-owned recovery after a WebKit renderer terminates."""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError
import logging
import threading
import weakref

log = logging.getLogger(__name__)


class RendererRecovery:
    """Settle current work, reload once automatically, then offer native controls.

    The recovery object is retained by its pywebview window but keeps only weak
    references back to the window/API. Waiting for session work happens on a
    daemon worker; GTK is used only for bounded dispatch and native fallback UI.
    """

    SETTLE_TIMEOUT_SECONDS = 8.0
    READY_TIMEOUT_SECONDS = 15
    PERMISSION_POLL_MILLISECONDS = 500
    MAX_RELOADS = 3

    RELOAD = 7401
    STOP = 7402
    CLOSE = 7403

    def __init__(self, window, api, glib, *, gtk=None, thread_factory=threading.Thread):
        self._window = weakref.ref(window)
        self._api = weakref.ref(api)
        self._glib = glib
        self._gtk = gtk
        self._thread_factory = thread_factory
        self._termination_count = 0
        self._reload_count = 0
        self._automatic_used = False
        self._settling = False
        self._waiting_permission = False
        self._failure_during_settlement = False
        self._pending_reload = False
        self._reloading = False
        self._ready_timeout_id = None
        self._permission_timer_id = None
        self._dialog = None
        self._closed = False
        self._cleanup_done = False

    def attach(self, *_args) -> bool:
        """Connect to this window's WebKit instance before its first page load."""
        window = self._window()
        if window is None:
            return False
        try:
            from webview.platforms.gtk import BrowserView

            browser = BrowserView.instances.get(window.uid)
            if browser is None:
                log.error("WebKit recovery could not find the window's GTK view")
                return False
            browser.window.connect("destroy", self._native_window_destroyed)
            browser.webview.connect("web-process-terminated", self._web_process_terminated)
        except (ImportError, AttributeError, KeyError):
            log.exception("Unable to connect the WebKit termination signal")
            return False
        return True

    def _web_process_terminated(self, _view, reason) -> None:
        """Defer all recovery work out of WebKit's termination signal."""
        self._glib.idle_add(self._handle_termination, str(reason))

    def _handle_termination(self, reason: str) -> bool:
        if self._closed:
            return self._glib.SOURCE_REMOVE
        self._termination_count += 1
        log.warning("WebKit renderer terminated (%s), occurrence %d",
                    reason, self._termination_count)
        if self._awaiting_ready():
            self._clear_ready_timeout()
            self._show_fallback("The page renderer stopped during recovery.")
            return self._glib.SOURCE_REMOVE
        if self._settling:
            self._failure_during_settlement = True
            return self._glib.SOURCE_REMOVE
        if self._waiting_permission:
            # The existing native approval dialog is the only decision shown.
            self._failure_during_settlement = True
            return self._glib.SOURCE_REMOVE
        if not self._automatic_used:
            self._automatic_used = True
            self._begin_settlement(reload=True)
        else:
            # Settle active work and wait for any outstanding host permission
            # before introducing a second native decision.
            self._begin_settlement(reload=False)
        return self._glib.SOURCE_REMOVE

    def _awaiting_ready(self) -> bool:
        return bool(getattr(self, "_reloading", False))

    def _begin_settlement(self, *, reload: bool, preserve_failure: bool = False) -> None:
        if self._closed or self._settling:
            return
        api = self._api()
        if api is None:
            self._show_fallback("The page is unavailable and its session has closed.")
            return
        self._settling = True
        self._pending_reload = reload
        if not preserve_failure:
            self._failure_during_settlement = False
        try:
            future = api._renderer_termination_future(self.SETTLE_TIMEOUT_SECONDS)
        except BaseException:
            self._settling = False
            log.exception("Could not settle the session after renderer termination")
            self._show_fallback("The current reply could not be settled safely.")
            return
        controller_ref = weakref.ref(self)
        try:
            worker = self._thread_factory(
                target=self._wait_for_settlement,
                args=(controller_ref, future),
                name="blackrose-renderer-recovery",
                daemon=True,
            )
            worker.start()
        except BaseException:
            # No worker means no _settlement_finished callback: leaving _settling
            # set would make every later termination a no-op.
            self._settling = False
            log.exception("Could not start the renderer settlement worker")
            self._show_fallback("The current reply could not be settled safely.")

    @classmethod
    def _wait_for_settlement(cls, controller_ref, future: Future) -> None:
        try:
            result = future.result(timeout=cls.SETTLE_TIMEOUT_SECONDS + 1.0)
        except TimeoutError:
            result = "busy"
        except BaseException:
            log.exception("Session settlement failed after renderer termination")
            result = "busy"
        controller = controller_ref()
        if controller is not None:
            controller._glib.idle_add(controller._settlement_finished, result)

    def _settlement_finished(self, result: str) -> bool:
        self._settling = False
        if self._closed:
            return self._glib.SOURCE_REMOVE
        if result == "permission_pending":
            self._waiting_permission = True
            if self._permission_timer_id is None:
                self._permission_timer_id = self._glib.timeout_add(
                    self.PERMISSION_POLL_MILLISECONDS, self._resume_after_permission)
            return self._glib.SOURCE_REMOVE

        if result not in ("settled", "idle") or self._failure_during_settlement:
            self._show_fallback(
                "The reply is still settling." if result != "settled"
                else "The renderer stopped repeatedly during recovery.")
            return self._glib.SOURCE_REMOVE

        if self._pending_reload:
            self._issue_reload()
        else:
            detail = ("The active reply has been stopped. The page is still unavailable."
                      if result == "settled"
                      else "No reply was active. The page is still unavailable.")
            self._show_fallback(detail)
        return self._glib.SOURCE_REMOVE

    def _resume_after_permission(self) -> bool:
        self._permission_timer_id = None
        self._waiting_permission = False
        if self._closed:
            return self._glib.SOURCE_REMOVE
        self._begin_settlement(reload=self._pending_reload, preserve_failure=True)
        return self._glib.SOURCE_REMOVE

    def _issue_reload(self) -> None:
        window = self._window()
        if window is None or self._closed:
            return
        if self._reload_count >= self.MAX_RELOADS:
            self._show_fallback("The page could not be restored after repeated attempts.")
            return
        self._dismiss_dialog()
        self._reload_count += 1
        self._reloading = True
        try:
            window.load_url(window.real_url)
        except BaseException:
            self._reloading = False
            log.exception("Host reload after renderer termination failed")
            self._show_fallback("The page could not be reloaded.")
            return
        self._ready_timeout_id = self._glib.timeout_add_seconds(
            self.READY_TIMEOUT_SECONDS, self._ready_timeout)

    def page_ready(self) -> None:
        """Called by Api.ready after the reloaded page has completed its handshake."""
        self._glib.idle_add(self._page_ready)

    def window_closed(self, *_args) -> None:
        """Drop recovery UI/timers when pywebview closes the owning window."""
        self._closed = True
        self._glib.idle_add(self._cleanup_closed_window)

    def _native_window_destroyed(self, *_args) -> None:
        """Synchronously clean GTK-owned state before its main loop exits."""
        self._closed = True
        self._cleanup_closed_window()

    def _cleanup_closed_window(self) -> bool:
        self._closed = True
        if self._cleanup_done:
            return self._glib.SOURCE_REMOVE
        self._cleanup_done = True
        self._clear_ready_timeout()
        if self._permission_timer_id is not None:
            self._glib.source_remove(self._permission_timer_id)
            self._permission_timer_id = None
        self._dismiss_dialog()
        return self._glib.SOURCE_REMOVE

    def _page_ready(self) -> bool:
        if self._reloading:
            self._reloading = False
            self._clear_ready_timeout()
            log.info("Bloom page handshake completed after renderer recovery")
        return self._glib.SOURCE_REMOVE

    def _ready_timeout(self) -> bool:
        self._ready_timeout_id = None
        if self._reloading and not self._closed:
            self._reloading = False
            self._show_fallback("The page did not become ready after reload.")
        return self._glib.SOURCE_REMOVE

    def _clear_ready_timeout(self) -> None:
        if self._ready_timeout_id is not None:
            self._glib.source_remove(self._ready_timeout_id)
            self._ready_timeout_id = None

    def _show_fallback(self, detail: str) -> None:
        if self._closed:
            return
        # The fallback ends this recovery attempt: no reload handshake is still
        # outstanding, so a later termination starts a new settlement instead of
        # being folded into one that gave up.
        self._reloading = False
        self._clear_ready_timeout()
        if self._dialog is not None:
            self._dialog.format_secondary_text(
                f"{detail}\n\nNo request will be replayed. A stopped reply remains saved."
            )
            return
        window = self._window()
        if window is None:
            return
        gtk = self._gtk
        if gtk is None:
            from gi.repository import Gtk

            gtk = Gtk
        dialog = gtk.MessageDialog(
            transient_for=window.native,
            flags=gtk.DialogFlags.MODAL,
            message_type=gtk.MessageType.ERROR,
            buttons=gtk.ButtonsType.NONE,
            text="Blackrose cannot display its conversation",
        )
        dialog.set_title("Blackrose Bloom recovery")
        dialog.format_secondary_text(
            f"{detail}\n\nNo request will be replayed. A stopped reply remains saved."
        )
        if self._reload_count < self.MAX_RELOADS:
            dialog.add_button("Reload page", self.RELOAD)
        dialog.add_button("Stop reply", self.STOP)
        dialog.add_button("Close window", self.CLOSE)
        dialog.connect("response", self._dialog_response)
        self._dialog = dialog
        dialog.show_all()

    def _dialog_response(self, _dialog, response: int) -> None:
        if response == self.STOP:
            if self._dialog is not None:
                self._dialog.format_secondary_text(
                    "Stopping the active reply before restoring the page."
                )
            self._begin_settlement(reload=False)
            return
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.destroy()
        if response == self.RELOAD:
            self._begin_settlement(reload=True)
        else:
            self._close_window()

    def _dismiss_dialog(self) -> None:
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.destroy()

    def _close_window(self) -> None:
        if self._closed:
            return
        self._cleanup_closed_window()
        window = self._window()
        if window is not None:
            window.destroy()
