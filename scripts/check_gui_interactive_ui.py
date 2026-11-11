"""Exercise semantic streaming, state, actions and replay in real GTK/WebKit.

Uses isolated storage and a deterministic provider; never calls a real model.
"""
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

import webview

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime, Backend
from blackrose.gui.app import run_gui
from blackrose.ui_documents import contract


class FixtureBackend(Backend):
    def __init__(self):
        self.calls = 0
        self.release = threading.Event()

    async def stream(self, model, messages, tools):
        self.calls += 1
        if self.calls == 1:
            yield {'delta': {'tool_calls': [{'index': 0, 'id': 'ui-activate', 'function': {
                'name': 'activate_tools', 'arguments': '{"capability":"interactive_ui"}'}}]}, 'finish_reason': 'tool_calls'}
        elif self.calls == 2:
            examples = contract()['examples']
            yield {'delta': {'content': json.dumps(examples['form']) + '\n'}}
            deadline = time.monotonic() + 25
            while not self.release.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            for kind in ('calculation', 'steps', 'comparison', 'metrics'):
                await asyncio.sleep(.08)
                document = examples[kind]
                if kind == 'calculation':
                    document = document | {'title': 'Office estimate', 'scale_label': 'Total Cost', 'items': [
                        {'label': 'Desk', 'quantity': 1, 'unit_price': 320},
                        {'label': 'Chair', 'quantity': 2, 'unit_price': 210},
                        {'label': 'Monitor', 'quantity': 0, 'unit_price': 180},
                    ]}
                yield {'delta': {'content': json.dumps(document) + '\n'}}
            yield {'delta': {}, 'finish_reason': 'stop'}
        else:
            yield {'delta': {'content': 'Your submitted preferences were received.'}, 'finish_reason': 'stop'}


def main():
    original_create = webview.create_window
    errors = []
    with tempfile.TemporaryDirectory(prefix='bloom-interactive-ui-') as directory:
        root = Path(directory)
        for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
            os.environ[name] = str(root / name.lower())
        config, runtime = make_runtime(root)
        config.interactive_ui_enabled = True
        config.interactive_ui_actions_enabled = True
        backend = runtime.controller.backend = FixtureBackend()

        def create(*args, **kwargs):
            window = original_create(*args, **kwargs)

            def loaded():
                def wait(script, seconds=20):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if window.evaluate_js(script):
                            return
                        time.sleep(.05)
                    raise AssertionError('Timed out: ' + script)

                def evaluate(script):
                    return window.evaluate_js(script)

                def memory_rss():
                    # Coarse process-tree RSS, including shared pages; not a
                    # per-component allocation or PSS measurement.
                    pending = [os.getpid()]
                    seen = set()
                    total = 0
                    while pending:
                        pid = pending.pop()
                        if pid in seen:
                            continue
                        seen.add(pid)
                        try:
                            status = Path(f'/proc/{pid}/status').read_text()
                            total += int(next(line.split()[1] for line in status.splitlines() if line.startswith('VmRSS:')))
                            pending += [int(child) for child in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]
                        except (OSError, StopIteration):
                            pass
                    return total

                def capture(path):
                    from gi.repository import GLib, WebKit2
                    done = threading.Event()
                    def request():
                        view = window.gui.BrowserView.instances[window.uid].webview
                        def saved(v, result):
                            try:
                                v.get_snapshot_finish(result).write_to_png(path)
                            finally:
                                done.set()
                        view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, saved)
                        return False
                    GLib.idle_add(request)
                    assert done.wait(10), 'Screenshot timed out'

                try:
                    wait("document.querySelector('#status-text').textContent === 'Ready'")
                    baseline_rss = memory_rss()
                    cold_load_ms = evaluate("performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd")
                    start = time.monotonic()
                    evaluate("document.querySelector('#input').value='Show interactive reading preferences';document.querySelector('#send').click()")
                    wait("document.querySelector('.blackrose-ui input[name=days]')")
                    first_paint = time.monotonic() - start
                    assert evaluate("document.querySelector('.blackrose-ui input[name=days]').value===''")
                    assert evaluate("document.querySelector('.blackrose-ui select[name=format]').value===''")
                    assert evaluate("document.querySelector('.blackrose-ui button[type=submit]').disabled")
                    evaluate("window.retainedInput=document.querySelector('.blackrose-ui input[name=days]');const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;setter.call(retainedInput,'4');retainedInput.dispatchEvent(new Event('input',{bubbles:true}));retainedInput.dispatchEvent(new Event('change',{bubbles:true}));true")
                    wait("BlackroseUI.inspect()[0].state.days === 4")
                    wait("BlackroseUI.inspect()[0].sequence >= 1")
                    # Replay an active snapshot before later source blocks arrive.
                    evaluate("pywebview.api.ready();true")
                    wait("retainedInput!==document.querySelector('.blackrose-ui input[name=days]') && document.querySelector('.blackrose-ui input[name=days]')?.value==='4'")
                    evaluate("window.retainedInput=document.querySelector('.blackrose-ui input[name=days]');true")
                    backend.release.set()
                    wait("document.querySelector('#send').getAttribute('aria-label').startsWith('Send') && document.querySelectorAll('.blackrose-ui').length === 5")
                    assert evaluate("retainedInput===document.querySelector('.blackrose-ui input[name=days]')"), 'Streaming remounted the form'
                    assert evaluate("document.querySelector('.blackrose-ui input[name=days]').value==='4'")
                    wait("BlackroseUI.inspect()[0].sequence >= 1")
                    assert evaluate("document.querySelectorAll('.ui-total output')[0].textContent==='740 USD'")
                    evaluate("(() => {const input=document.querySelectorAll('.blackrose-ui input[type=number]')[1];Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'2');input.dispatchEvent(new Event('input',{bubbles:true}));})();true")
                    wait("document.querySelectorAll('.ui-total output')[0].textContent===(1480).toLocaleString()+' USD'")
                    for label, value in (('Quantity multiplier', '1'), ('Quantity for Chair', '1'), ('Quantity for Monitor', '1')):
                        selector = '[aria-label=' + json.dumps(label) + ']' if label != 'Quantity multiplier' else 'label.ui-field input'
                        evaluate("(() => {const input=document.querySelector('.blackrose-ui " + selector + "');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input," + json.dumps(value) + ");input.dispatchEvent(new Event('input',{bubbles:true}));})();true")
                    wait("document.querySelectorAll('.ui-total output')[0].textContent==='710 USD'")
                    evaluate("[...document.querySelectorAll('.blackrose-ui button')].find(b=>b.textContent==='Next').click();true")
                    wait("document.querySelector('.blackrose-ui h4')?.textContent==='Check'")
                    assert evaluate("document.activeElement===document.querySelector('.blackrose-ui h4')")
                    assert evaluate("document.activeElement.getAttribute('aria-label')==='Step 2 of 2: Check'")
                    assert evaluate("document.querySelector('select[aria-label=\"Sort by\"]').value===''")
                    evaluate("const sort=document.querySelector('select[aria-label=\"Sort by\"]');sort.value='Approach';sort.dispatchEvent(new Event('change',{bubbles:true}));true")
                    evaluate("const direction=document.querySelector('select[aria-label=\"Sort direction\"]');direction.value='descending';direction.dispatchEvent(new Event('change',{bubbles:true}));true")
                    wait("document.querySelector('th[aria-sort=descending]')?.textContent==='Approach'")
                    assert evaluate("[...document.querySelectorAll('.blackrose-ui table')].at(-1).querySelector('tbody tr').textContent.includes('Semantic')")
                    evaluate("document.querySelector('.blackrose-ui input[type=search]').value='';true")
                    assert evaluate("!document.querySelector('[data-ui-error]')")
                    # Clearing an optional choice deletes it from the saved state.
                    evaluate("(() => {const select=document.querySelector('.blackrose-ui select[name=format]');select.value='Ebook';select.dispatchEvent(new Event('change',{bubbles:true}));})();true")
                    wait("BlackroseUI.inspect()[0].state.format==='Ebook'")
                    evaluate("(() => {const select=document.querySelector('.blackrose-ui select[name=format]');select.value='';select.dispatchEvent(new Event('change',{bubbles:true}));})();true")
                    wait("!('format' in BlackroseUI.inspect()[0].state)")
                    # Reopen flushes current edits before rotating view identities.
                    evaluate("window.beforeReopen=document.querySelector('.blackrose-ui input[name=days]');document.querySelector('.chat-item[aria-current=true]').click();true")
                    wait("beforeReopen!==document.querySelector('.blackrose-ui input[name=days]') && document.querySelector('.blackrose-ui input[name=days]')?.value==='4'")
                    assert evaluate("document.querySelector('.blackrose-ui select[name=format]').value===''")
                    assert evaluate("!('format' in BlackroseUI.inspect()[0].state)")
                    wait("document.querySelectorAll('.ui-total output')[0]?.textContent==='710 USD'")
                    assert evaluate("document.querySelector('[aria-label=\"Quantity for Monitor\"]').value==='1'")
                    assert evaluate("document.querySelector('select[aria-label=\"Sort direction\"]').value==='descending'")
                    assert evaluate("document.querySelector('.blackrose-ui h4').textContent==='Check'")
                    wait("!document.querySelector('.blackrose-ui button[type=submit]').disabled")
                    evaluate("document.querySelector('.blackrose-ui button[type=submit]').click();true")
                    wait("document.querySelectorAll('.msg.user').length===2 && document.querySelector('#send').getAttribute('aria-label').startsWith('Send')")
                    assert backend.calls == 3, backend.calls
                    assert evaluate("document.querySelectorAll('.msg.user')[1].textContent.includes('4')")
                    assert evaluate("!document.querySelectorAll('.msg.user')[1].textContent.includes('Preferred format')")
                    # Replay remains available with generation/actions switched off.
                    config.interactive_ui_enabled = False
                    config.interactive_ui_actions_enabled = False
                    evaluate("document.querySelector('.chat-item[aria-current=true]').click();true")
                    wait("document.querySelector('.blackrose-ui input[name=days]')?.value==='4' && document.querySelector('.blackrose-ui button[type=submit]').disabled")
                    destination = root / 'interactive-answer.json'
                    window.create_file_dialog = lambda *a, **kw: str(destination)
                    evaluate("document.querySelector('.blackrose-ui .ui-fallback').open=true;[...document.querySelectorAll('.blackrose-ui button')].find(b=>b.textContent==='Export source').click();true")
                    deadline = time.monotonic()+10
                    while not destination.exists() and time.monotonic()<deadline:
                        time.sleep(.05)
                    assert json.loads(destination.read_text())['type']=='form'
                    evaluate("document.querySelector('.blackrose-ui .ui-fallback').open=false;true")
                    evaluate("document.querySelector('.blackrose-ui').scrollIntoView({block:'start'});true")
                    capture('/tmp/blackrose-interactive-ui-desktop.png')
                    assert evaluate("document.documentElement.scrollWidth <= window.innerWidth+1"), 'Document overflows'
                    Path('/tmp/blackrose-interactive-ui.html').write_text(evaluate('document.documentElement.outerHTML'))
                    print(json.dumps({'result': 'PASS', 'engine': 'GTK/WebKit', 'first_fixture_paint_seconds': round(first_paint, 3),
                        'dom_content_loaded_ms': round(cold_load_ms, 2),
                        'process_tree_rss_kib_before_fixtures': baseline_rss,
                        'process_tree_rss_kib_after_fixtures': memory_rss(),
                        'bundle_bytes': (Path(__file__).resolve().parents[1] / 'src/blackrose/gui/static/ui-runtime.js').stat().st_size,
                        'checks': ['offline fixtures', 'streaming stable mount', 'local calculations', 'local steps',
                                   'active snapshot restores state', 'durable state', 'one visible continuation', 'generation disabled replay', 'source export', 'desktop layout']}), flush=True)
                except BaseException as error:
                    errors.append(error)
                    traceback.print_exc()
                    print(evaluate("JSON.stringify({status:document.querySelector('#status-text').textContent,views:BlackroseUI.inspect(),text:document.querySelector('#column').textContent})"), flush=True)
                    Path('/tmp/blackrose-interactive-ui-failure.html').write_text(evaluate('document.documentElement.outerHTML'))
                finally:
                    backend.release.set()
                    from gi.repository import GLib
                    GLib.idle_add(lambda: window.destroy() or False)

            window.events.loaded += loaded
            return window

        webview.create_window = create
        run_gui(config, runtime=runtime)
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
