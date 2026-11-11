"""Real GTK/WebKit check and optional persistent in-app demo (no model needed).

Run with --demo to leave the app open for inspection. Uses isolated chat storage.
"""
from __future__ import annotations

import argparse
import json
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
from test_animations import ScriptedBackend
from blackrose.gui.app import run_gui

ROOT = Path(__file__).resolve().parents[1]


def verify_export(document):
    """Exercise offline export in a plain WebKit view with no desktop bridge."""
    from gi.repository import GLib, Gtk, WebKit2
    done = threading.Event()
    errors = []

    def open_view():
        native = Gtk.Window(title='Motion Canvas offline export check')
        native.set_default_size(960, 720)
        view = WebKit2.WebView()
        native.add(view)
        started = False

        def finish(error=None):
            nonlocal native, view
            if done.is_set():
                return False
            if error:
                errors.append(error)
            # evaluate and evaluated reference each other, so this cycle would keep the
            # WebView alive until cyclic GC frees it on whichever thread collects next.
            # WebKit aborts when finalized off the GTK thread, so release it here.
            native.destroy(); native = view = None; done.set()
            return False

        def evaluated(v, result):
            nonlocal started
            if done.is_set():
                return
            try:
                state = json.loads(v.evaluate_javascript_finish(result).to_string())
                if state.get('error'):
                    return finish(AssertionError(state['error']))
                if state.get('bridge'):
                    return finish(AssertionError('Offline export has a native bridge'))
                if state.get('ready') and not started:
                    started = True
                    view.evaluate_javascript("document.querySelector('.animation-play').click()", -1, None, None, None, None)
                if state.get('time', 0) > .2:
                    return finish()
            except BaseException as error:
                return finish(error)
            if not done.is_set():
                GLib.timeout_add(100, evaluate)

        def evaluate():
            if not done.is_set():
                view.evaluate_javascript("JSON.stringify({ready:document.querySelector('.motion-canvas-player')?.dataset.ready,"
                    "error:document.querySelector('.motion-canvas-player')?.dataset.error,"
                    "time:parseFloat(document.querySelector('.animation-time')?.textContent)||0,bridge:typeof window.pywebview !== 'undefined'})",
                    -1, None, None, None, evaluated)
            return False

        view.connect('load-changed', lambda v, e: evaluate() if e == WebKit2.LoadEvent.FINISHED else None)
        native.show_all(); view.load_html(document, None)
        GLib.timeout_add_seconds(15, lambda: finish(AssertionError('Offline playback timed out')))
        return False

    GLib.idle_add(open_view)
    if not done.wait(20):
        raise AssertionError('Offline export check did not finish')
    if errors:
        raise errors[0]


def example():
    return {'version': 2, 'engine': 'motion-canvas', 'title': 'Gears · a 2:1 reduction',
        'description': 'Meshing gears turn in opposite directions. Twice the teeth means half the angular speed.',
        'duration': 8, 'source': (ROOT / 'skills/explanatory-animations/references/gears.tsx').read_text(),
        'cues': [{'time': 0, 'text': 'The small gear has 12 teeth; the large gear has 24.'},
                 {'time': .6, 'text': 'Teeth carry motion across the contact point, reversing direction.'},
                 {'time': 6.6, 'text': 'Two input turns produce one output turn: a 2:1 speed reduction.'}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--scene', type=Path, help='Preview a saved Motion Canvas scene instead of the bundled example')
    parser.add_argument('--recovery', action='store_true', help='Inject a runtime failure and verify automatic repair')
    parser.add_argument('--verify-export', type=Path, help='Check a standalone exported HTML animation in plain WebKit')
    args = parser.parse_args()
    if args.verify_export:
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('WebKit2', '4.1')
        from gi.repository import GLib, Gtk
        errors = []
        def check_export():
            try:
                verify_export(args.verify_export.read_text())
                print('PASS: standalone Motion Canvas export plays in WebKit.', flush=True)
            except BaseException as error:
                errors.append(error)
                traceback.print_exc()
            finally:
                GLib.idle_add(Gtk.main_quit)
        threading.Thread(target=check_export).start()
        Gtk.main()
        if errors:
            raise SystemExit(1)
        return
    original_create = webview.create_window
    failures = []
    with tempfile.TemporaryDirectory(prefix='bloom-motion-canvas-') as directory:
        root = Path(directory)
        for name, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
            os.environ[name] = str(root / sub)
        config, runtime = make_runtime(root)
        runtime.controller.model_name = 'opencode-go/test-vision'
        scene = json.loads(args.scene.read_text()) if args.scene else example()
        scene.pop('compiled', None)
        scene['source'] = '''
// Verify that the opaque frame cannot reach the trusted DOM or desktop API.
try { parent.document.body; throw new Error('Parent DOM exposed'); }
catch (error) { if (error.message === 'Parent DOM exposed') throw error; }
if (typeof (window as any).pywebview !== 'undefined') throw new Error('Desktop API exposed');
(window as any).webkit?.messageHandlers?.jsBridge?.postMessage(JSON.stringify({
  funcName: 'frame_probe', params: [], id: 'untrusted-frame'
}));
''' + scene['source']
        revision = dict(scene, title='Gears · slower input', duration=10,
                        source=scene['source'].replace('angle(720, 6, linear)', 'angle(720, 8, linear)'))
        runtime.controller.backend = ScriptedBackend([
            ('animation_create', {'scene': scene, 'loop': True}),
            'Motion Canvas: 12 teeth drive 24 teeth. Play to see the 2:1 reduction.',
            ('animation_read', {'attachment_id': 1}),
            ('animation_create', {'scene': revision, 'loop': False, 'revises_attachment_id': 1}),
            'The input now takes eight seconds for two turns.'])
        if args.recovery:
            runtime.controller.backend = ScriptedBackend([
                ('animation_create', {'scene': scene, 'loop': True}), 'Initial animation saved.',
                ('animation_read', {'attachment_id': 1}),
                ('animation_create', {'scene': scene, 'loop': True, 'revises_attachment_id': 1}),
                'Repaired the injected playback failure.'])

        def create(*a, **kw):
            native_calls = []
            kw['js_api'].frame_probe = lambda: native_calls.append('called') or True
            window = original_create(*a, **kw)

            def loaded():
                def wait(script, seconds=30):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if window.evaluate_js(script):
                            return
                        error = window.evaluate_js("document.querySelector('.motion-canvas-player')?.dataset.error")
                        if error and not args.recovery:
                            raise AssertionError(error)
                        time.sleep(.1)
                    raise AssertionError('Timed out: ' + script)
                try:
                    wait("document.querySelector('#status-text').textContent === 'Ready'")
                    window.evaluate_js('pywebview.api.frame_probe(); true')
                    deadline = time.monotonic() + 5
                    while not native_calls and time.monotonic() < deadline:
                        time.sleep(.05)
                    assert native_calls == ['called'], 'Trusted desktop bridge failed'
                    window.evaluate_js("document.querySelector('#input').value='Animate how meshing gears reduce speed'; document.querySelector('#send').click()")
                    if args.recovery:
                        wait("document.querySelector('.motion-canvas-player')?.dataset.ready === 'true'")
                        window.evaluate_js("(() => { const frame=document.querySelector('.motion-canvas-frame'); "
                            "window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,"
                            "data:{channel:'blackrose-motion',type:'error',message:'Injected playback failure'}})); })()")
                        wait("document.querySelectorAll('.motion-canvas-player')[1]?.dataset.ready === 'true'")
                        wait("document.querySelector('.motion-canvas-player').dataset.recovery === 'replaced'")
                        wait("document.querySelector('#status-text').textContent === 'Reply complete'")
                        assert window.evaluate_js("document.querySelectorAll('.motion-canvas-player').length === 2")
                        assert window.evaluate_js("document.querySelector('.animation-recovery').textContent.includes('repaired version')")
                        window.evaluate_js("document.querySelectorAll('.motion-canvas-frame')[1].scrollIntoView({block:'center'})")
                        time.sleep(.2)
                        window.evaluate_js("document.querySelectorAll('.animation-play')[1].click()")
                        wait("parseFloat(document.querySelectorAll('.animation-time')[1].textContent) > .2")
                        window.evaluate_js("document.querySelectorAll('.animation-play')[1].click(); document.querySelector('.animation-retry').click()")
                        time.sleep(.3)
                        assert window.evaluate_js("document.querySelectorAll('.motion-canvas-player').length === 2")
                        window.evaluate_js("document.querySelector('.chat-item[aria-current=true]').click()")
                        wait("document.querySelectorAll('.motion-canvas-player')[1]?.dataset.ready === 'true'")
                        time.sleep(.3)
                        assert window.evaluate_js("document.querySelectorAll('.motion-canvas-player').length === 2")
                        window.evaluate_js("document.querySelectorAll('.motion-canvas-frame')[1].scrollIntoView({block:'center'})")
                        print('PASS: runtime failure → automatic source repair → playable new version; retry/reopen do not duplicate recovery.', flush=True)
                        return
                    wait("document.querySelector('.motion-canvas-player')?.dataset.ready === 'true'")
                    wait("document.querySelector('.msg.assistant .body').textContent.includes('Motion Canvas:')")
                    assert window.evaluate_js("document.querySelector('.motion-canvas-frame').getAttribute('sandbox') === 'allow-scripts'")
                    assert window.evaluate_js("(() => {const d=document.querySelector('.motion-canvas-player').dataset; return Math.abs(Number(d.width)-Math.min(2880,Math.max(960,Number(d.displayWidth)*Number(d.pixelRatio)*2)))<=1;})()")
                    window.evaluate_js("document.querySelector('.animation-play').click()")
                    wait("parseFloat(document.querySelector('.animation-time').textContent) > .3")
                    window.evaluate_js("document.querySelector('.animation-play').click()")
                    paused = window.evaluate_js("document.querySelector('.animation-time').textContent")
                    time.sleep(.3)
                    assert window.evaluate_js("document.querySelector('.animation-time').textContent") == paused
                    window.evaluate_js("const seek=document.querySelector('.animation-controls input[type=range]'); seek.value=4; seek.dispatchEvent(new Event('input'))")
                    wait("parseFloat(document.querySelector('.animation-time').textContent) >= 3.9")
                    assert window.evaluate_js("document.querySelector('.animation-cue').textContent.includes('Teeth carry')")
                    window.evaluate_js("document.querySelector('.motion-canvas-frame').style.width='320px'")
                    wait("document.querySelector('.motion-canvas-player').dataset.displayWidth === '320'")
                    assert window.evaluate_js("document.querySelector('.motion-canvas-player').dataset.width === '960'")
                    assert window.evaluate_js("parseFloat(document.querySelector('.animation-time').textContent) >= 3.9")
                    window.evaluate_js("document.querySelector('.motion-canvas-frame').style.width=''")
                    wait("Number(document.querySelector('.motion-canvas-player').dataset.displayWidth) > 320")
                    assert native_calls == ['called'], 'Generated frame reached the native bridge'
                    window.evaluate_js("document.querySelector('.motion-canvas-frame').scrollIntoView({block:'center'})")
                    # Capture the actual desktop webview, including the sandboxed frame.
                    from gi.repository import GLib, WebKit2
                    def capture():
                        view = window.gui.BrowserView.instances[window.uid].webview
                        def snapshot(v, result):
                            try:
                                v.get_snapshot_finish(result).write_to_png('/tmp/blackrose-motion-canvas.png')
                            except BaseException:
                                traceback.print_exc()
                        view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, snapshot)
                        return False
                    GLib.idle_add(capture)
                    destination = root / 'motion-canvas.html'
                    window.create_file_dialog = lambda *a, **kw: str(destination)
                    window.evaluate_js("Array.from(document.querySelectorAll('.animation-controls button')).find(b=>b.textContent==='Export HTML').click()")
                    wait("document.querySelector('.animation-controls button:last-child').disabled === false")
                    deadline = time.monotonic() + 10
                    while not destination.exists() and time.monotonic() < deadline:
                        time.sleep(.1)
                    assert destination.exists()
                    document = destination.read_text()
                    Path('/tmp/blackrose-motion-canvas.html').write_text(document)
                    if not args.demo:
                        verify_export(document)
                    window.evaluate_js("document.querySelector('.chat-item[aria-current=true]').click()")
                    wait("document.querySelector('.motion-canvas-player')?.dataset.ready === 'true'")
                    assert window.evaluate_js("document.querySelector('.animation-loop input').checked")
                    if not args.demo:
                        window.evaluate_js("document.querySelector('#input').value='Slow down that animation'; document.querySelector('#send').click()")
                        wait("document.querySelectorAll('.motion-canvas-player')[1]?.dataset.ready === 'true'")
                        wait("document.querySelectorAll('.msg.assistant .body')[1].textContent.includes('eight seconds')")
                        assert window.evaluate_js("!document.querySelectorAll('.animation-loop input')[1].checked")
                    else:
                        window.evaluate_js("document.querySelector('.motion-canvas-frame').scrollIntoView({block:'center'})")
                    print('PASS: Motion Canvas in GTK/WebKit: create, play/pause, seek, captions, reopen, export' +
                          (', source-based revision.' if not args.demo else '. Demo remains open.'), flush=True)
                    print('Screenshot: /tmp/blackrose-motion-canvas.png; export: /tmp/blackrose-motion-canvas.html', flush=True)
                except BaseException as error:
                    failures.append(error)
                    traceback.print_exc()
                finally:
                    if not args.demo or failures:
                        window.destroy()
            window.events.loaded += loaded
            return window
        webview.create_window = create
        run_gui(config, runtime=runtime)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
