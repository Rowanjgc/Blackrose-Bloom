"""Native WebKit probe for incomplete workspace autocomplete and its recovery path."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from test_turns import Backend
from blackrose.gui.app import run_gui
import webview

failures = []
original_create = webview.create_window


def create(*args, **kwargs):
    # Native test runs use the isolated Xvfb display, so keep the WebKit view
    # visible to let requestAnimationFrame and GTK snapshot rendering advance.
    kwargs['hidden'] = False
    window = original_create(*args, **kwargs)
    checked = False

    def loaded():
        nonlocal checked
        if checked:
            return
        checked = True
        try:
            def wait(script):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if window.evaluate_js(script):
                        return
                    time.sleep(.04)
                raise AssertionError(script)

            def type_text(value):
                window.evaluate_js(f"document.querySelector('#input').value={json.dumps(value)}; "
                    "document.querySelector('#input').focus(); document.querySelector('#input').dispatchEvent(new Event('input'))")

            def choose():
                window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}))")

            def screenshot():
                from gi.repository import GLib, WebKit2
                from webview.platforms.gtk import BrowserView
                done = threading.Event()
                errors = []

                def capture():
                    view = BrowserView.instances[window.uid].webview

                    def saved(view, result):
                        try:
                            view.get_snapshot_finish(result).write_to_png('/tmp/blackrose-workspace-suggestions-incomplete.png')
                        except BaseException as error:
                            errors.append(error)
                        finally:
                            done.set()

                    view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, saved)
                    return False

                GLib.idle_add(capture)
                assert done.wait(10), 'Screenshot timed out'
                if errors:
                    raise errors[0]

            wait("document.querySelector('#status-text').textContent === 'Ready' && document.querySelector('#workspace-value').textContent === 'work'")
            wait("document.querySelector('#scrim').hidden")
            type_text('@nothing-matches')
            wait("document.querySelector('.command-status')?.textContent.includes('No matching files')")
            window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")

            candidates = []
            for index in range(2100):
                candidate = work / f'candidate file with spaces {index:04}.txt'
                candidate.touch()
                candidates.append(candidate)
            nested = work / 'omitted nested'
            nested.mkdir()
            (nested / 'nested file.txt').write_text('nested')
            controller = runtime.controller
            controller._reference_index = None
            type_text('@no-such-large-entry')
            wait("document.querySelector('.command-status')?.textContent.includes('bounded set')")
            indexed = {item['path'] for item in controller._reference_index[2]}
            omitted = next(candidate for candidate in candidates if str(candidate) not in indexed)

            type_text('@{' + omitted.name)
            wait(f"document.querySelector('#commands [role=option]')?.textContent.includes({json.dumps(omitted.name)})")
            wait("document.querySelector('#scrim').hidden && !document.querySelector('#commands').hidden && Number(getComputedStyle(document.querySelector('#commands')).opacity) >= .99 && document.querySelector('#commands').getBoundingClientRect().width > 0 && document.querySelector('#commands').getBoundingClientRect().height > 0")
            screenshot()
            window.evaluate_js("document.querySelector('#commands [role=option]').dispatchEvent(new PointerEvent('pointerdown', {bubbles:true}))")
            wait(f"document.querySelector('#input').value.includes({json.dumps(omitted.name + '}')})")

            type_text('@{omitted nested/nested file.txt')
            wait("document.querySelector('#commands [role=option]')?.textContent.includes('nested file.txt')")
            window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowDown', bubbles:true}))")
            choose()
            wait("document.querySelector('#input').value.includes('nested file.txt}')")

            work.rename(work.with_name('work-unavailable'))
            type_text('@notes.md')
            wait("document.querySelector('.command-status')?.textContent.includes('Workspace is unavailable')")
            window.evaluate_js('window.pywebview.api.clear_workspace()')
            wait("document.querySelector('#workspace-value').textContent === 'Downloads outputs'")
            type_text('@no-saved-output-matches')
            wait("document.querySelector('.command-status')?.textContent.includes('No workspace selected')")
            print('Native WebKit autocomplete passed: no-match and unavailable states; omitted flat/nested path recovery with spaces; pointer and keyboard selection. Screenshot: /tmp/blackrose-workspace-suggestions-incomplete.png', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            window.destroy()

    window.events.loaded += loaded
    return window


with tempfile.TemporaryDirectory(prefix='bloom-workspace-autocomplete-') as directory:
    root = Path(directory)
    for name, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[name] = str(root / sub)
    work = root / 'work'
    work.mkdir()
    (work / 'notes.md').write_text('A note')
    config, runtime = make_runtime(root)
    runtime.controller.backend = Backend(['Done'])
    webview.create_window = create
    run_gui(config, runtime=runtime, workspace=str(work))
if failures:
    raise SystemExit(1)
