"""Kill only this scratch window's WebKit renderer and test host recovery.

Run under a private Xvfb/D-Bus session. The probe uses a scratch runtime and a
scripted backend, identifies WebKitWebProcess descendants of this PID, and will
send SIGKILL only to one such renderer. It checks first automatic recovery,
repeated-failure native controls, pending directory approval, no replay, and
normal shutdown after active work.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from blackrose.gui.app import run_gui
from blackrose.gui.renderer_recovery import RendererRecovery
import webview

ROOT = Path(__file__).resolve().parents[1]
_create_window = webview.create_window
_events_lock = threading.Lock()
_loaded_count = 0
_api_ready_count = 0
_api_ready_changed = threading.Event()
_browser_ready = threading.Event()
_terminated: list[str] = []
_terminate_lock = threading.Lock()
_permission_entered = threading.Event()
_permission_finished = threading.Event()
_shutdown_finished = threading.Event()
_exercise_finished = threading.Event()
_dialog_found = threading.Event()
_dialog_holder = []
_recovery_dialog_holder = []
_recovery_dialog_found = threading.Event()
_failures: list[str] = []
_metrics: dict[str, int] = {}
_window = None
_runtime = None
_backend = None
_scratch_root: Path | None = None


class StreamingBackend:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.cancelled = threading.Event()

    async def load(self, _model):
        return None

    async def models(self):
        return [{'id': 'test-vision', 'architecture': {'input_modalities': ['text']}}]

    def reasoning_levels(self, _model):
        return []

    async def stream(self, _model, messages, _tools):
        text = next(message['content'] for message in reversed(messages)
                    if message['role'] == 'user')
        self.requests.append(text)
        try:
            yield {'delta': {'content': 'STREAM_MARKER '}}
            while True:
                await asyncio.sleep(.1)
                yield {'delta': {'content': '.'}}
        finally:
            self.cancelled.set()

    async def aclose(self):
        return None


class Service:
    async def close(self):
        return None


def _process_rows(proc_root: Path = Path('/proc')) -> dict[int, tuple[int, str]]:
    """Read process ancestry and command names without external process tools."""
    rows = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_fields = (entry / 'stat').read_text().split()
            parent = int(stat_fields[3])
            executable = Path(os.readlink(entry / 'exe')).name
            command = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(
                'utf-8', errors='replace')
            rows[int(entry.name)] = parent, executable + ' ' + command
        except (OSError, ValueError, IndexError):
            continue
    return rows


def _renderer_descendants(rows: dict[int, tuple[int, str]], owner_pid: int) -> list[tuple[int, str]]:
    found = []
    for pid, (parent, description) in rows.items():
        # _process_rows prefixes the executable basename; do not match a
        # command-line argument that merely mentions the renderer name.
        if description.split(' ', 1)[0] != 'WebKitWebProcess':
            continue
        ancestor = parent
        while ancestor in rows and ancestor != owner_pid:
            ancestor = rows[ancestor][0]
        if ancestor == owner_pid:
            found.append((pid, description[:240]))
    return found


def _owned_renderers() -> list[tuple[int, str]]:
    return _renderer_descendants(_process_rows(), os.getpid())


def _kill_owned_renderer() -> int:
    renderers = _owned_renderers()
    if len(renderers) != 1:
        raise AssertionError(f'Expected one owned WebKitWebProcess descendant; found {renderers!r}')
    pid, command = renderers[0]
    # A pidfd pins the selected process across the ownership recheck and signal,
    # preventing PID reuse from redirecting the termination to another process.
    if not hasattr(signal, 'pidfd_send_signal'):
        raise RuntimeError('Safe renderer termination requires pidfd_send_signal')
    handle = os.pidfd_open(pid)
    try:
        fresh = dict((child, desc) for child, desc in _owned_renderers())
        if pid not in fresh or fresh[pid] != command:
            raise AssertionError('Renderer ownership changed before termination')
        signal.pidfd_send_signal(handle, signal.SIGKILL)
    finally:
        os.close(handle)
    print(f'Terminated owned WebKit renderer pid={pid} command={command}', flush=True)
    return pid


def _rss_kib() -> int:
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1])
    return 0


def _record_ready(*_args) -> None:
    global _loaded_count
    with _events_lock:
        _loaded_count += 1


def _record_api_ready() -> None:
    global _api_ready_count
    with _events_lock:
        _api_ready_count += 1
        _api_ready_changed.set()


def _api_ready_value() -> int:
    with _events_lock:
        return _api_ready_count


def _connect_native(*_args) -> None:
    from webview.platforms.gtk import BrowserView
    browser = BrowserView.instances.get(_window.uid)
    if browser is None:
        _failures.append('GTK BrowserView was unavailable at before_show')
        return

    def terminated(_view, reason):
        with _terminate_lock:
            _terminated.append(str(reason))
        print(f'web-process-terminated reason={reason}', flush=True)

    browser.webview.connect('web-process-terminated', terminated)
    _window._probe_browser = browser
    _browser_ready.set()


def _create(*args, **kwargs):
    global _window
    kwargs['hidden'] = True
    _window = _create_window(*args, **kwargs)
    _window.events.before_show += _connect_native
    _window.events.loaded += _record_ready
    original_ready = _window._js_api.ready

    def tracked_ready():
        result = original_ready()
        _record_api_ready()
        return result

    _window._js_api.ready = tracked_ready

    from blackrose.gui import external
    original_confirmation = external.ask

    def tracked_confirmation(*confirmation_args, **confirmation_kwargs):
        _permission_entered.set()
        try:
            return original_confirmation(*confirmation_args, **confirmation_kwargs)
        finally:
            _permission_finished.set()

    external.ask = tracked_confirmation

    original_shutdown = _window._js_api._shutdown

    def tracked_shutdown(timeout):
        try:
            return original_shutdown(timeout)
        finally:
            _shutdown_finished.set()
            print(f'host_shutdown_backend_cancelled={_backend.cancelled.is_set()}', flush=True)
            if not _exercise_finished.wait(5):
                print('probe_thread_finished_before_host_exit=False', flush=True)

    _window._js_api._shutdown = tracked_shutdown
    threading.Thread(target=_exercise, name='web-process-probe', daemon=True).start()
    return _window


def _wait(condition, description: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.025)
    raise AssertionError(f'Timed out waiting for {description}')


def _loaded_value() -> int:
    with _events_lock:
        return _loaded_count


def _wait_page(script: str, description: str, timeout: float = 10) -> None:
    _wait(lambda: bool(_window.evaluate_js(script)), description, timeout)


def _kill_and_observe(label: str) -> int:
    before = len(_terminated)
    pid = _kill_owned_renderer()
    _wait(lambda: len(_terminated) > before, f'{label} web-process-terminated signal', 8)
    _wait(lambda: pid not in {p for p, _ in _owned_renderers()}, f'{label} renderer exit', 5)
    return pid


def _find_permission_dialog() -> bool:
    """The in-window approval sheet showing a question; no separate dialog window may appear."""
    from gi.repository import Gtk, GLib
    if any(isinstance(item, Gtk.MessageDialog) and item.get_visible() for item in Gtk.Window.list_toplevels()):
        raise AssertionError('Permission opened a separate dialog window instead of the in-window sheet')
    sheet = getattr(_window, '_blackrose_consent', None)
    if sheet is not None and sheet._current is not None:
        _dialog_holder[:] = [sheet]
        _dialog_found.set()
        return GLib.SOURCE_REMOVE
    return GLib.SOURCE_CONTINUE


def _decline_permission() -> None:
    from gi.repository import GLib
    GLib.idle_add(lambda: (_dialog_holder[0]._answer(False), GLib.SOURCE_REMOVE)[1])


def _find_recovery_dialog() -> bool:
    from gi.repository import GLib, Gtk
    for item in Gtk.Window.list_toplevels():
        if (isinstance(item, Gtk.MessageDialog)
                and item.get_title() == 'Blackrose Bloom recovery'
                and item.get_modal()):
            _recovery_dialog_holder[:] = [item]
            _recovery_dialog_found.set()
            return GLib.SOURCE_REMOVE
    return GLib.SOURCE_CONTINUE


def _wait_for_recovery_dialog(description: str) -> object:
    from gi.repository import GLib
    _recovery_dialog_holder.clear()
    _recovery_dialog_found.clear()
    GLib.idle_add(_find_recovery_dialog)
    _wait(lambda: _recovery_dialog_found.is_set(), description, 8)
    return _recovery_dialog_holder[0]


def _respond_recovery(dialog, response: int) -> None:
    from gi.repository import GLib
    GLib.idle_add(lambda: (dialog.response(response), GLib.SOURCE_REMOVE)[1])


def _wait_for_api_ready(after: int, description: str) -> None:
    _wait(lambda: _api_ready_value() > after, description, 20)


def _sample_during_renderer_loss(seconds: float) -> None:
    emitter = _window._js_api._emitter
    queue_high, rss_high = 0, _rss_kib()
    rss_start = rss_high
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        queue_high = max(queue_high, emitter._queue.qsize())
        rss_high = max(rss_high, _rss_kib())
        time.sleep(.05)
    _metrics['lost_renderer_queue_max'] = queue_high
    _metrics['lost_renderer_rss_growth_kib'] = max(0, rss_high - rss_start)


def _exercise() -> None:
    try:
        _wait(lambda: _loaded_value() > 0 and _browser_ready.is_set(), 'initial page and WebKit hook', 20)
        _wait(lambda: _api_ready_value() > 0, 'initial API ready handshake', 10)
        _wait(lambda: len(_owned_renderers()) == 1, 'one owned WebKit child', 12)

        # The first renderer death settles in the host and reloads without
        # replaying user work.
        idle_ready = _api_ready_value()
        _kill_and_observe('idle')
        _wait_for_api_ready(idle_ready, 'host idle recovery handshake')
        idle_auto_recovered = True
        print(f'idle_host_recovery={idle_auto_recovered}', flush=True)
        _wait_page("document.querySelector('#status-text')?.textContent === 'Ready'",
                   'idle page ready after recovery')

        # Start one controlled stream, terminate its renderer, measure bounded
        # event queue and process RSS while the reply continues without a page.
        _window.evaluate_js("const i=document.querySelector('#input'); i.value='Renderer recovery stream'; "
                            "i.dispatchEvent(new Event('input')); document.querySelector('#send').click()")
        _wait(lambda: len(_backend.requests) == 1, 'scripted request start', 10)
        _wait_page("document.querySelector('#send')?.textContent === 'Stop' && "
                   "document.querySelector('.msg.assistant .body')?.textContent.includes('STREAM_MARKER')",
                   'active partial stream')
        _kill_and_observe('streaming')
        _wait(lambda: _backend.cancelled.is_set(), 'recovery Stop settles streamed reply', 10)
        _sample_during_renderer_loss(1.5)
        dialog = _wait_for_recovery_dialog('native repeated-failure recovery dialog')
        print(f'stream_host_stopped_before_fallback={_backend.cancelled.is_set()}', flush=True)
        print(f"lost_renderer_queue_max={_metrics['lost_renderer_queue_max']} "
              f"rss_growth_kib={_metrics['lost_renderer_rss_growth_kib']}", flush=True)
        messages = _runtime.controller.current_messages()
        stored = _window._js_api._loop.run(messages)
        assistant_text = next((message.content for message in reversed(stored)
                               if message.role == 'assistant'), '')
        if 'STREAM_MARKER' not in assistant_text:
            raise AssertionError(f'Cancelled partial reply was not persisted: {assistant_text!r}')
        _respond_recovery(dialog, RendererRecovery.STOP)
        time.sleep(.1)
        if len(_backend.requests) != 1:
            raise AssertionError(f'Host Stop replayed inference: {_backend.requests!r}')
        dialog = _wait_for_recovery_dialog('fallback after explicit Stop')
        before_reload = _api_ready_value()
        _respond_recovery(dialog, RendererRecovery.RELOAD)
        _wait_for_api_ready(before_reload, 'stream recovery page handshake')
        _wait_page("document.querySelector('#send')?.textContent === 'Send' && "
                   "document.querySelector('.msg.assistant .body')?.textContent.includes('STREAM_MARKER')",
                   'saved partial stream restored after host reload')
        _wait_page("document.querySelectorAll('.msg.user').length === 1",
                   'single stored user turn')
        if len(_backend.requests) != 1:
            raise AssertionError(f'Reload duplicated inference: {_backend.requests!r}')

        # Hold an actual directory-access modal open across renderer death.
        assert _scratch_root is not None
        permission_dir = _scratch_root / 'permission'
        permission_dir.mkdir(parents=True, exist_ok=False)
        _window.evaluate_js("const i=document.querySelector('#input'); i.value="
                            + json.dumps(f'Read files in "{permission_dir}"')
                            + "; i.dispatchEvent(new Event('input')); document.querySelector('#send').click()")
        _wait(lambda: _permission_entered.is_set(), 'directory permission prompt', 10)
        from gi.repository import GLib
        GLib.idle_add(_find_permission_dialog)
        _wait(lambda: _dialog_found.is_set(), 'visible directory permission sheet', 5)
        _kill_and_observe('permission-pending')
        if _permission_finished.is_set():
            raise AssertionError('Renderer death unexpectedly settled the native permission dialog')
        recovery = _window._blackrose_renderer_recovery
        _wait(lambda: recovery._waiting_permission,
              'recovery waits instead of stacking a native dialog', 12)
        _recovery_dialog_found.clear()
        GLib.idle_add(_find_recovery_dialog)
        time.sleep(.2)
        if _recovery_dialog_found.is_set():
            raise AssertionError('Renderer recovery stacked a dialog over pending permission')
        if _backend.requests != ['Renderer recovery stream']:
            raise AssertionError(f'Permission prompt unexpectedly started inference: {_backend.requests!r}')
        grants = _runtime.controller.directory_grants
        if any(grant.get('path') == permission_dir.as_posix() for per_conversation in grants.values()
               for grant in per_conversation.values()):
            raise AssertionError('Pending directory permission was granted without user approval')
        print('pending_permission_survived_renderer_death=True; grant_count=0', flush=True)

        # Explicitly decline the native sheet; never auto-approve to unblock test.
        _decline_permission()
        _wait(lambda: _permission_finished.is_set(), 'permission cancellation', 8)
        _wait(lambda: not _window._js_api._session._permission_pending,
              'permission state cleared after denial', 8)
        if _backend.requests != ['Renderer recovery stream']:
            raise AssertionError(f'Denied permission started inference: {_backend.requests!r}')
        recovery_dialog = _wait_for_recovery_dialog('fallback after permission denial')
        ready_before = _api_ready_value()
        _respond_recovery(recovery_dialog, RendererRecovery.RELOAD)
        _wait_for_api_ready(ready_before, 'page reload after permission decision')
        _wait_page("document.querySelector('#status-text')?.textContent === 'Ready'",
                   'page ready after permission denial and reload')

        # Shutdown while a separate reply is active. This verifies that the
        # normal host teardown settles the backend task after renderer recovery.
        _backend.cancelled.clear()
        _window.evaluate_js("const i=document.querySelector('#input'); i.value='Renderer recovery shutdown'; "
                            "i.dispatchEvent(new Event('input')); document.querySelector('#send').click()")
        _wait(lambda: len(_backend.requests) == 2, 'shutdown request start', 10)
        _wait_page("document.querySelector('#send')?.textContent === 'Stop' && "
                   "document.querySelector('.msg.assistant .body')?.textContent.includes('STREAM_MARKER')",
                   'active reply before host shutdown')
        _window.destroy()
        _wait(lambda: _shutdown_finished.is_set(), 'host shutdown completion', 10)
        if not _backend.cancelled.is_set():
            raise AssertionError('Host shutdown returned before the active backend task settled')
        if len(_backend.requests) != 2:
            raise AssertionError(f'Shutdown/reload duplicated inference: {_backend.requests!r}')

        print('PASS: automatic idle recovery, repeated streaming failure fallback, partial-turn '
              'settlement/host reload, pending permission decision, no request replay, Stop, '
              'shutdown settlement, and bounded queue sample.', flush=True)
    except BaseException:
        _failures.append(traceback.format_exc())
        traceback.print_exc()
    finally:
        if _dialog_holder and not _permission_finished.is_set():
            _decline_permission()
            _permission_finished.wait(3)
        try:
            _window.destroy()
        except BaseException:
            pass
        _exercise_finished.set()


def main() -> int:
    global _backend, _runtime, _scratch_root
    with tempfile.TemporaryDirectory(prefix='blackrose-web-process-') as directory:
        root = Path(directory)
        _scratch_root = root
        for name, sub in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                          ('XDG_STATE_HOME', 'state')):
            os.environ[name] = str(root / sub)
        config, _runtime = make_runtime(root)
        _backend = StreamingBackend()
        _runtime.controller.backend = _backend
        webview.create_window = _create
        run_gui(config, runtime=_runtime)
    if _failures:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
