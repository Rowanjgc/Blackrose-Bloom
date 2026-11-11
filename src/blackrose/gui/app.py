"""Launch the Gothic Bloom GTK/WebKit window and close client resources on exit.

Closing the window also stops the managed local router and its loaded models.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import signal
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

# Allow an in-flight native index operation to settle during shutdown.
_SHUTDOWN_SECONDS = 180.0


def gui_problem() -> str | None:
    """Why the window cannot open here, or ``None`` when it can."""
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return "no graphical display (neither WAYLAND_DISPLAY nor DISPLAY is set)"
    if importlib.util.find_spec("webview") is None:
        return "pywebview is not installed (pip install 'pywebview>=5')"
    if importlib.util.find_spec("gi") is None:
        return ("PyGObject is not installed (pip install PyGObject; it needs the "
                "gobject-introspection-devel and cairo-gobject-devel packages)")
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
    except (ImportError, ValueError) as exc:
        return f"GTK WebKit is unavailable ({exc}; install webkit2gtk4.1)"
    return None


def run_gui(config=None, *, runtime=None, workspace=None) -> int:
    """Open the desktop client, optionally over an injected runtime."""
    from ..instance import desktop_instance

    # Acquire before opening storage or adopting the shared router. The lock
    # stays held through shutdown and is released by the OS even after a crash.
    # Injected scratch runtimes use their own database directory as well.
    lock = (Path(runtime.database.path).parent / 'desktop.lock'
            if runtime is not None and runtime.database.path != ':memory:' else None)
    with desktop_instance(lock):
        return _run_gui(config, runtime=runtime, workspace=workspace)


def _run_gui(config=None, *, runtime=None, workspace=None) -> int:
    import webview
    from gi.repository import GLib

    GLib.set_prgname('blackrose-bloom')
    GLib.set_application_name('Blackrose Bloom')

    from ..config import load_config
    from ..runtime import build_runtime
    from .bridge import Api, Emitter, LoopThread

    resolved = config or load_config()
    runtime = runtime or build_runtime(resolved)
    loop = LoopThread()
    loop.start()
    api = None
    try:
        loop.run(runtime.open())
        if workspace is not None:
            loop.run(runtime.controller.configure_launch_workspace(workspace))
        emitter = Emitter()
        api = Api(runtime, loop, emitter)
        window = webview.create_window(
            "Blackrose Bloom",
            url=str(STATIC / "index.html"),
            js_api=api,
            width=1180,
            height=820,
            min_size=(640, 480),
            background_color="#0D0D0D",
            text_select=True,
        )
        if window is None:
            raise RuntimeError("pywebview did not create the Blackrose window")
        api._attach(window)
        from .renderer_recovery import RendererRecovery

        recovery = RendererRecovery(window, api, GLib)
        window._blackrose_renderer_recovery = recovery
        api._set_renderer_recovery(recovery)
        window.events.closed += recovery.window_closed
        from .frame_isolation import protect_desktop_bridge
        protect_desktop_bridge(window)
        from .consent import install as install_consent
        install_consent(window)
        window.events.before_show += recovery.attach
        emitter.attach(window)
        emitter.start()
        _route_exit_signals(window)
        webview.start(gui="gtk", private_mode=True, icon=str(STATIC / 'blackrose-bloom.png'),
                      debug=bool(os.environ.get("BLACKROSE_GUI_DEBUG")))
    finally:
        log.info("window closed; shutting down")
        try:
            if api is not None:
                api._shutdown(_SHUTDOWN_SECONDS)
        except Exception:  # noqa: BLE001 - the runtime must still be closed
            log.exception("stopping the session failed")
        try:
            loop.run(runtime.close(), _SHUTDOWN_SECONDS)
        except Exception:  # noqa: BLE001
            log.exception("runtime shutdown failed")
        finally:
            loop.stop()
    _exit_past_stuck_workers()
    return 0


def _exit_past_stuck_workers() -> None:
    """Leave even if a pywebview worker is still waiting on the dead window.

    By now native workers have settled and the database is closed. pywebview runs
    each page call on a non-daemon thread that answers through the window; one
    caught mid-reply when the window closed waits forever, and the interpreter
    would wait for it. Nothing is left to lose by not waiting.
    """
    stuck = [t for t in threading.enumerate()
             if t is not threading.main_thread() and not t.daemon and t.is_alive()]
    if not stuck:
        return
    log.warning("leaving %d window worker(s) blocked on the closed window", len(stuck))
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def _route_exit_signals(window) -> None:
    """Turn termination signals into closing the window.

    GTK's main loop runs in C, where a Python signal handler would wait for
    the next time the interpreter happens to run. GLib's own Unix signal
    source is serviced by the main loop itself, so the window closes at once
    and the normal shutdown path runs. A second signal kills the process
    outright, so a wedged unload can still be escaped.
    """
    try:
        from gi.repository import GLib
    except ImportError:  # pragma: no cover - gui_problem() rules this out
        return

    def close(sig: int) -> bool:
        log.info("%s received; closing the window", signal.Signals(sig).name)
        signal.signal(sig, signal.SIG_DFL)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            log.debug("window already gone", exc_info=True)
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, close, sig)
