"""Exercise inline animation playback and offline export in real GTK/WebKit.

Uses a scripted tool-calling model, a real SQLite database, and a temporary store.
Run: .venv/bin/python scripts/check_gui_animations.py
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

import webview

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from test_animations import ScriptedBackend, example
from blackrose.animations import export_html, load_scene
from blackrose.gui.app import run_gui

failures = []
original_create = webview.create_window


def verify_export(document):
    """Use a plain WebKit view: no injected desktop bridge or eval-based helper."""
    from gi.repository import GLib, Gtk, WebKit2

    done = threading.Event()
    errors = []

    def open_view():
        native = Gtk.Window(title='Standalone animation check')
        native.set_default_size(960, 900)
        view = WebKit2.WebView()
        native.add(view)

        def finish(error=None):
            nonlocal native, view
            if not done.is_set():
                if error:
                    errors.append(error)
                # evaluate and evaluated reference each other; release the widgets here, on
                # the GTK thread, so cyclic GC never finalizes the WebView on another thread.
                native.destroy()
                native = view = None
                done.set()
            return False

        def evaluated(view, result, final=False):
            try:
                if not view.evaluate_javascript_finish(result).to_boolean():
                    raise AssertionError('Standalone player state was incorrect')
                if final:
                    def snapshot(view, result):
                        try:
                            view.get_snapshot_finish(result).write_to_png('/tmp/blackrose-animation-player.png')
                            finish()
                        except BaseException as error:
                            finish(error)
                    view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, snapshot)
                else:
                    GLib.timeout_add(400, lambda: evaluate(
                        "parseFloat(document.querySelector('.animation-time').textContent) > 0.1", True))
            except BaseException as error:
                finish(error)

        def evaluate(script, final=False):
            if done.is_set():
                return False
            view.evaluate_javascript(script, -1, None, None, None,
                                     lambda v, r: evaluated(v, r, final))
            return False

        def loaded(view, event):
            if event == WebKit2.LoadEvent.FINISHED:
                evaluate("(() => { const ready = document.querySelector('.animation-time')?.textContent === '0.0 / 12.0s'"
                         " && typeof window.pywebview === 'undefined' && typeof window.animationAttack === 'undefined'"
                         " && !document.querySelector('img'); document.querySelector('.animation-controls button').click(); return ready; })()")

        view.connect('load-changed', loaded)
        native.show_all()
        view.load_html(document, None)
        GLib.timeout_add_seconds(10, lambda: finish(AssertionError('Standalone player timed out')))
        return False

    GLib.idle_add(open_view)
    if not done.wait(15):
        raise AssertionError('Standalone WebKit view did not complete')
    if errors:
        raise errors[0]


def create(*args, **kwargs):
    window = original_create(*args, **kwargs)

    def loaded():
        try:
            def wait(script):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if window.evaluate_js(script):
                        return
                    time.sleep(.05)
                raise AssertionError(script)

            wait("document.querySelector('#status-text').textContent === 'Ready'")
            window.evaluate_js("document.querySelector('#input').value = 'Animate RAG'; document.querySelector('#send').click()")
            wait("document.querySelector('.animation-canvas')")
            wait("document.querySelector('.msg.assistant .body').textContent.includes('Ready')")
            assert window.evaluate_js("document.querySelector('.animation-time').textContent === '0.0 / 12.0s'")
            assert window.evaluate_js("getComputedStyle(document.querySelector('.animation-player')).backgroundColor === 'rgb(18, 18, 18)'")
            assert window.evaluate_js("getComputedStyle(document.querySelector('.animation-play')).backgroundColor === 'rgb(239, 100, 97)'")
            # A fractional desktop font DPI (GDK_DPI_SCALE, or Xft/DPI from text scaling 1.1)
            # makes WebKitGTK report the 2.75rem target as ~43.99px: layout units are 1/64px.
            play_height = window.evaluate_js("document.querySelector('.animation-play').getBoundingClientRect().height")
            assert play_height >= 44 - 1 / 32, f'Play control is {play_height}px tall'
            assert window.evaluate_js("document.querySelector('.animation-canvas').getContext('2d').getImageData(0,0,1,1).data[0] === 13")
            # A paused canvas must update its physical resolution after resizing.
            window.evaluate_js("document.querySelector('.animation-canvas').style.width = '320px'")
            wait("document.querySelector('.animation-canvas').width === Math.round(320 * devicePixelRatio)")
            assert window.evaluate_js("document.querySelector('.animation-time').textContent === '0.0 / 12.0s'")
            window.evaluate_js("document.querySelector('.animation-canvas').style.width = ''")
            wait("(() => { const c = document.querySelector('.animation-canvas'); return c.width === Math.min(2880, Math.round(c.getBoundingClientRect().width * devicePixelRatio)); })()")
            window.evaluate_js("document.querySelector('.animation-controls button').click()")
            wait("parseFloat(document.querySelector('.animation-time').textContent) > 0.1")
            window.evaluate_js("document.querySelector('.animation-controls button').click()")
            paused = window.evaluate_js("document.querySelector('.animation-time').textContent")
            time.sleep(.15)
            assert window.evaluate_js("document.querySelector('.animation-time').textContent") == paused
            window.evaluate_js("const seek = document.querySelector('.animation-controls input'); seek.value=6; seek.dispatchEvent(new Event('input'))")
            wait("document.querySelector('.animation-cue').textContent.includes('Doc A and Doc C')")
            image = window.evaluate_js("document.querySelector('.animation-canvas').toDataURL('image/png').split(',')[1]")
            Path('/tmp/blackrose-rag-frame.png').write_bytes(base64.b64decode(image))
            window.evaluate_js("document.querySelectorAll('.animation-controls button')[1].click()")
            wait("parseFloat(document.querySelector('.animation-time').textContent) < 1")
            window.evaluate_js("BlackroseAnimations.pauseAll(); document.querySelector('.chat-item[aria-current=true]').click()")
            wait("document.querySelector('.animation-time')?.textContent === '0.0 / 12.0s'")
            assert window.evaluate_js("document.querySelector('.animation-description').textContent.includes('does not guarantee')")
            # Test the actual export writer through its file dialog seam.
            destination = root / 'exported.html'
            window.create_file_dialog = lambda *a, **kw: str(destination)
            window.evaluate_js("document.querySelectorAll('.animation-controls button')[2].click()")
            deadline = time.monotonic() + 10
            while not destination.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert destination.exists(), 'Export HTML did not save'
            exported = destination.read_text()
            assert 'pywebview' not in exported
            verify_export(exported)
            hostile = example()
            hostile['title'] = '</script><script>window.animationAttack=1</script><img src=x>'
            hostile['nodes'][0]['label'] = hostile['title']
            # Verify hostile strings first so the final screenshot shows the clean export.
            verify_export(export_html(hostile))
            verify_export(exported)
            window.evaluate_js("document.querySelector('#input').value = 'Create a short, looping animation that explains the TCP three-way handshake.'; document.querySelector('#send').click()")
            wait("document.querySelectorAll('.animation-player').length === 2")
            wait("document.querySelectorAll('.msg.assistant .body')[1].textContent.includes('TCP ready')")
            assert window.evaluate_js("document.querySelectorAll('.animation-loop input')[1].checked")
            window.evaluate_js("const t=document.querySelectorAll('.animation-controls input[type=range]')[1]; t.value=9.95; t.dispatchEvent(new Event('input')); document.querySelectorAll('.animation-play')[1].click()")
            wait("parseFloat(document.querySelectorAll('.animation-time')[1].textContent) < 1 && document.querySelectorAll('.animation-play')[1].textContent === 'Pause'")
            window.evaluate_js("document.querySelectorAll('.animation-play')[1].click()")
            assert window.evaluate_js("document.querySelectorAll('.animation-play')[1].textContent === 'Play'")
            print('Native GTK/WebKit passed: inline canvas, play/pause, seek/captions, replay, reopen, offline HTML export playback.', flush=True)
            print('Gothic Bloom tokens, compatible duplicate tracks, TCP looping, and inert export labels passed.', flush=True)
            print('Inspected frame: /tmp/blackrose-rag-frame.png', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            window.destroy()

    window.events.loaded += loaded
    return window


with tempfile.TemporaryDirectory(prefix='bloom-animations-check-') as directory:
    root = Path(directory)
    for name, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[name] = str(root / sub)
    config, runtime = make_runtime(root)
    runtime.controller.model_name = 'opencode-go/test-vision'
    rag = example()
    original = rag['tracks'][3]
    rag['tracks'][3:4] = [dict(original, keyframes=original['keyframes'][:2]),
                         dict(original, keyframes=original['keyframes'][2:])]
    tcp = load_scene(Path(__file__).resolve().parents[1] / 'skills/explanatory-animations/references/tcp-handshake.animation.json')
    runtime.controller.backend = ScriptedBackend([
        ('animation_create', {'scene': rag}), 'Ready.',
        ('animation_create', {'scene': tcp}), 'TCP ready.'])
    webview.create_window = create
    run_gui(config, runtime=runtime)
if failures:
    raise SystemExit(1)
