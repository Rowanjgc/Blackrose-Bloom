"""Native GTK/WebKit citation replay and source-integrity probe; synthetic data only."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import AsyncMock, Mock

import webview

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from test_citations import Backend, RESULT, PASSAGE, assessment
from blackrose.gui.app import run_gui

release = threading.Event()
failures = []
original_create = webview.create_window


class PausedBackend(Backend):
    async def stream(self, model, messages, tools):
        if 'assess source support' in messages[0]['content']:
            await asyncio.to_thread(release.wait, 25)
        async for choice in super().stream(model, messages, tools):
            yield choice


def create(*args, **kwargs):
    kwargs['min_size'] = (360, 480)
    window = original_create(*args, **kwargs)
    checked = False

    def loaded():
        nonlocal checked
        if checked:
            return
        checked = True
        try:
            def wait(script):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if window.evaluate_js(script):
                        return
                    time.sleep(.04)
                raise AssertionError(script)

            wait("document.querySelector('#status-text').textContent === 'Ready'")
            rigor = "document.querySelector('#citation-rigor [data-mode=\"%s\"]')"
            unlocked = "document.querySelector('#citation-rigor [data-mode=\"off\"]').getAttribute('aria-disabled') === 'false'"
            assert window.evaluate_js((rigor % 'off') + ".getAttribute('aria-checked') === 'true'")
            window.evaluate_js("document.querySelector('#sources').click()")
            wait("!document.querySelector('#sources-panel').hidden")
            window.evaluate_js("const strict=" + (rigor % 'strict') + "; strict.focus(); strict.click()")
            wait("document.querySelector('#citation-rigor').dataset.saved === 'strict' && " + unlocked)
            assert window.evaluate_js("document.activeElement === " + (rigor % 'strict') + " && document.activeElement.closest('[role=radiogroup]').getAttribute('aria-labelledby') === 'rigor-heading'")
            window.evaluate_js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            wait("document.querySelector('#sources-panel').hidden")
            window.evaluate_js("document.querySelector('#input').value='Summarize the synthetic study.'; document.querySelector('#send').click()")
            wait("document.querySelector('#status-text').textContent === 'Checking claims…'")
            assert window.evaluate_js("document.querySelector('.audit-label').textContent.includes('Provisional') && !(" + unlocked + ")")
            window.evaluate_js('window.__beforeReload=true; setTimeout(() => location.reload(), 0)')
            wait("!window.__beforeReload && document.querySelector('.audit-label')?.textContent.includes('Provisional')")
            release.set()
            wait("document.querySelector('.claim-citation') !== null && " + unlocked)
            window.evaluate_js("document.querySelector('.claim-citation').focus(); document.querySelector('.claim-citation').click()")
            wait("document.querySelector('#dialog-detail .supporting-excerpt') !== null")
            assert window.evaluate_js("document.querySelector('.supporting-excerpt').textContent.includes('40 participants')")
            window.evaluate_js("document.querySelector('.captured-passage summary').click()")
            assert window.evaluate_js("document.querySelector('.captured-passage mark') !== null")
            wait("getComputedStyle(document.querySelector('#dialog')).opacity > .99")
            # Snapshot capture uses real WebKit rendering, independent of browser fixtures.
            from gi.repository import GLib, WebKit2
            from webview.platforms.gtk import BrowserView
            done = threading.Event()
            def capture():
                view = BrowserView.instances[window.uid].webview
                def saved(view, result):
                    try:
                        view.get_snapshot_finish(result).write_to_png('/tmp/blackrose-citations-native.png')
                    except BaseException as error:
                        failures.append(error)
                    finally:
                        done.set()
                view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, saved)
                return False
            GLib.idle_add(capture)
            assert done.wait(10)
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            wait("document.activeElement.classList.contains('claim-citation')")
            runtime.controller.knowledge.call.return_value = {'results': []}
            window.evaluate_js('window.__beforeReload=true; setTimeout(() => location.reload(), 0)')
            wait("!window.__beforeReload && document.querySelector('.claim-citation') !== null")
            window.evaluate_js("document.querySelector('.claim-citation').click()")
            wait("document.querySelector('.supporting-excerpt') !== null")
            expected = json.dumps(PASSAGE)
            assert window.evaluate_js(f'document.querySelector(".supporting-excerpt").textContent === {expected}')
            assert 'citation_rigor = "strict"' in (root / 'config/blackrose-bloom/config.toml').read_text()
            print('GTK/WebKit citation probe passed: default-off setting, accessible selection, provisional/active reload, '
                  'atomic final replacement, exact highlighted supporting text, focus restoration, saved replay after source removal. '
                  'Screenshot: /tmp/blackrose-citations-native.png', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            release.set()
            window.destroy()

    window.events.loaded += loaded
    return window


with tempfile.TemporaryDirectory(prefix='bloom-citation-check-') as directory:
    root = Path(directory)
    for variable, child in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[variable] = str(root / child)
    config, runtime = make_runtime(root)
    config.knowledge_enabled = True
    runtime.controller.knowledge.call = AsyncMock(return_value=RESULT)
    runtime.controller.schedule_title = Mock()
    runtime.controller.backend = PausedBackend([
        [('knowledge_open', {'document_id': 'doc-a'})], 'The study enrolled 40 participants.', assessment])
    webview.create_window = create
    run_gui(config, runtime=runtime, workspace=str(root))
if failures:
    raise SystemExit(1)
