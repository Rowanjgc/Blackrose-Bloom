"""Exercise session reload and accessibility in the actual GTK/WebKit window.

Run .venv/bin/python scripts/check_gui_session.py. Uses a scratch database,
private XDG directories and a scripted backend; no model or network calls.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from blackrose.backend.effort import LEVELS
from blackrose.gui.app import run_gui
import webview

failures = []
copied_diagnostics = []
original_create = webview.create_window
navigation_release = threading.Event()


async def capture_copy(text):
    copied_diagnostics.append(text)
    return True

async def models(provider='local'):
    return [{'id': 'opencode-go/test-vision', 'meta': {'n_ctx': 128000}}]


async def stream(model, messages, tools):
    text = next(m['content'] for m in reversed(messages) if m['role'] == 'user')
    if text.startswith('Navigation '):
        yield {'delta': {'content': 'Navigation partial answer', 'reasoning_content': 'Navigation reasoning'}}
        if not await asyncio.to_thread(navigation_release.wait, 15):
            raise RuntimeError('Navigation probe was not released')
        if text == 'Navigation error':
            raise RuntimeError('Navigation scripted provider error')
        yield {'delta': {'content': ' completed'}, 'finish_reason': 'stop'}
        return
    if text in ('Reasoning only', 'Repair exhaustion animation', 'Context rejection animation', 'Complete'):
        yield {'delta': {'reasoning_content': 'Consider the evidence.'},
               '_timings': {'predicted_per_second': 31.5}}
        await asyncio.sleep(.1)
        if text == 'Reasoning only':
            yield {'delta': {}, 'finish_reason': 'stop'}
        elif text == 'Complete':
            yield {'delta': {'content': 'Finished.'}, 'finish_reason': 'stop'}
        elif sum(m['role'] == 'tool' for m in messages) < 2:
            arguments = {'scene': {}, 'loop': False}
            if text == 'Context rejection animation':
                # The failed tool call remains in the repair history. The next
                # real host-admission check rejects it before another stream.
                arguments['scene']['unused'] = 'x' * 160000
            yield {'delta': {'tool_calls': [{'index': 0, 'id': 'scripted',
                'function': {'name': 'animation_create', 'arguments': ''}}]}}
            await asyncio.sleep(.1)
            yield {'delta': {'tool_calls': [{'index': 0,
                'function': {'arguments': json.dumps(arguments)}}]}, 'finish_reason': 'tool_calls'}
        else:
            yield {'delta': {'content': 'I need to adjust the animation.'}, 'finish_reason': 'stop'}
        return
    yield {'delta': {'content': 'Partial answer', 'reasoning_content': 'Consider the evidence.'},
           '_usage': {'prompt_tokens': 1200, 'completion_tokens': 24},
           '_timings': {'predicted_per_second': 31.5}}
    await asyncio.Event().wait()

def create(*args, **kwargs):
    kwargs['hidden'] = True
    window = original_create(*args, **kwargs)
    checked = False
    def loaded():
        nonlocal checked
        if checked:
            return
        checked = True
        try:
            def wait(script):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if window.evaluate_js(script):
                        return
                    time.sleep(.03)
                raise AssertionError((script, window.evaluate_js("({context: document.querySelector('#context-value')?.textContent, speed: document.querySelector('#generation-speed')?.textContent, status: document.querySelector('#status-text')?.textContent})")))
            wait("document.querySelector('#status-text').textContent === 'Ready'")
            assert window.evaluate_js("document.querySelector('#composer-legend').textContent.includes('Drop images or documents')")
            wait("[...document.querySelectorAll('.brand-logo')].every(img => img.naturalWidth > 0)")
            from gi.repository import GLib
            assert GLib.get_prgname() == 'blackrose-bloom'
            assert window.native.get_icon() is not None
            window.evaluate_js("document.querySelector('#help-button').click()")
            wait("document.querySelector('#app').inert && document.querySelector('#scrim').hidden === false")
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            wait("!document.querySelector('#app').inert")
            # While the backdrop fades it must not intercept a follow-up toolbar click.
            # Don't wait for the animation to rest: a background window can pause rAF.
            assert window.evaluate_js("document.querySelector('#scrim').hidden || getComputedStyle(document.querySelector('#scrim')).pointerEvents === 'none'")
            # WebKitGTK reports Shift+Tab as Unidentified; in the composer it cycles effort through the real bridge.
            wait("!document.querySelector('#effort').disabled")
            window.evaluate_js("const i=document.querySelector('#input'); i.focus(); i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Unidentified', code: 'Tab', shiftKey: true, bubbles: true, cancelable: true}))")
            wait("document.querySelector('#effort').dataset.level === 'off' && document.querySelector('#effort-value').textContent === 'Off'")
            assert window.evaluate_js("document.activeElement.id === 'input'")
            # The composer shows the new level before the bridge call that saves it returns.
            preferences = Path(os.environ['XDG_CONFIG_HOME']) / 'blackrose-bloom/preferences.json'
            deadline = time.monotonic() + 10
            saved = None
            while time.monotonic() < deadline:
                try:
                    saved = json.loads(preferences.read_text()).get('reasoning_effort')
                except (OSError, ValueError):
                    saved = None
                if saved == {'opencode-go/test-vision': 'off'}:
                    break
                time.sleep(.03)
            assert saved == {'opencode-go/test-vision': 'off'}, saved
            window.evaluate_js("window.pywebview.api.set_reasoning_effort('default')")
            wait("document.querySelector('#effort').dataset.level === 'default'")
            window.evaluate_js("const i=document.querySelector('#input'); i.value='Question'; i.dispatchEvent(new Event('input')); document.querySelector('#send').click()")
            wait("document.querySelector('.msg.assistant .body')?.textContent.includes('Partial answer')")
            wait("document.querySelector('#send').textContent === 'Stop'")
            wait("document.querySelector('#context-value').textContent.startsWith('Context ' + (1224).toLocaleString() + ' / ') && document.querySelector('#context-metric').title.includes('Provider-reported input and output tokens for the latest model request.')")
            wait("document.querySelector('#generation-speed').textContent === '31.5 tokens/s'")
            context_label = window.evaluate_js("document.querySelector('#context-value').textContent")
            assert window.evaluate_js("document.querySelector('#context-metric').title.includes('output reserve ' + (16000).toLocaleString())")
            window.evaluate_js("const d=document.querySelector('#input'); d.value='Follow-up draft'; d.dispatchEvent(new Event('input')); "
                               "window.pywebview.api.update_draft(d.value).then(() => {window.__reviewDraftSaved = true;})")
            wait('window.__reviewDraftSaved === true')
            # Deferring navigation lets the eval return before its page is gone.
            window.evaluate_js("window.__reviewBeforeReload = true; setTimeout(() => location.reload(), 0)")
            wait("!window.__reviewBeforeReload && document.querySelector('.msg.assistant .body')?.textContent.includes('Partial answer') && document.querySelector('#input').value === 'Follow-up draft'")
            assert window.evaluate_js("document.querySelectorAll('.msg.assistant').length === 1 && document.querySelectorAll('.msg.user').length === 1")
            assert window.evaluate_js("document.querySelector('.thinking pre').textContent === 'Consider the evidence.'")
            assert window.evaluate_js("document.querySelector('#context-value').textContent.startsWith('Context ' + (1224).toLocaleString() + ' / ') && document.querySelector('#context-metric').title.includes('Provider-reported input and output tokens for the latest model request.') && document.querySelector('#generation-speed').textContent === '31.5 tokens/s'")
            assert window.evaluate_js("document.querySelector('#context-value').textContent") == context_label
            assert window.evaluate_js("document.querySelector('#send').textContent === 'Stop' && document.querySelector('#attach').disabled")
            window.evaluate_js("document.querySelector('#send').click()")
            wait("document.querySelector('#send').textContent === 'Send'")
            wait("document.querySelector('#reply-announcement').textContent.includes('Reply stopped.')")
            assert window.evaluate_js("document.querySelector('#input').value === 'Follow-up draft'")
            wait("!document.querySelector('#retry').disabled")
            window.evaluate_js("document.querySelector('#retry').click()")
            wait("document.querySelector('#send').textContent === 'Stop' && document.querySelector('#retry').disabled")
            assert window.evaluate_js("document.querySelector('#input').value === 'Follow-up draft'")
            window.evaluate_js("document.querySelector('#send').click()")
            wait("!document.querySelector('#retry').disabled")
            assert window.evaluate_js("document.querySelectorAll('.msg.user').length === 2")
            window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key: 'r', ctrlKey: true, bubbles: true, cancelable: true}))")
            wait("document.querySelector('#send').textContent === 'Stop' && document.querySelectorAll('.msg.user').length === 3")
            assert window.evaluate_js("document.querySelector('#input').value === 'Follow-up draft'")
            window.evaluate_js("document.querySelector('#send').click()")
            wait("!document.querySelector('#retry').disabled")
            wait("document.querySelector('#status-text').textContent === 'Reply stopped'")
            assert window.evaluate_js("document.querySelector('#generation-speed').textContent === '— tokens/s'")
            for prompt, outcome, diagnostic in [
                ('Reasoning only', 'Reply failed', 'no visible answer'),
                ('Repair exhaustion animation', 'Reply failed', 'no animation was saved'),
                ('Context rejection animation', 'Reply failed', 'conservative host estimate'),
                ('Complete', 'Reply complete', ''),
            ]:
                before = window.evaluate_js("document.querySelectorAll('.msg.assistant').length")
                window.evaluate_js("window.__phases = []; const receive = bloom.receive; bloom.receive = events => {for (const [kind, p] of events) if (kind === 'status') window.__phases.push(p.state); receive(events);}")
                window.evaluate_js("const i = document.querySelector('#input'); i.value = " + json.dumps(prompt) + "; i.dispatchEvent(new Event('input')); document.querySelector('#send').click()")
                wait("document.querySelectorAll('.msg.assistant').length > " + str(before))
                wait("document.querySelector('#status-text').textContent === " + json.dumps(outcome))
                wait("document.querySelector('#send').textContent === 'Send' && !document.querySelector('#retry').disabled")
                assert window.evaluate_js("[...document.querySelectorAll('.reply-note')].at(-1).textContent.includes(" + json.dumps(diagnostic) + ")")
                assert window.evaluate_js("document.querySelector('#model-availability').textContent === 'Test Vision Ready' && document.querySelector('#active-model').textContent === 'Test Vision' && !document.querySelector('.is-streaming, .tool-activity')")
                assert window.evaluate_js("document.querySelector('#generation-speed').textContent === '— tokens/s'")
                assert window.evaluate_js("document.querySelector('#reply-announcement').textContent.includes(" + json.dumps(outcome) + ")")
                if diagnostic:
                    window.evaluate_js("const b = [...document.querySelectorAll('.copy-diagnostics')].at(-1); b.focus(); b.click()")
                    wait("document.querySelector('#toast').textContent === 'Diagnostics copied'")
                    assert copied_diagnostics[-1].startswith('Blackrose Bloom reply diagnostics\n')
                    assert 'Reply state: error' in copied_diagnostics[-1]
                    assert prompt not in copied_diagnostics[-1]
                    assert diagnostic not in copied_diagnostics[-1]
                assert window.evaluate_js("[...document.querySelectorAll('.thinking summary')].every(s => s.textContent === 'Reasoning')")
                if prompt == 'Repair exhaustion animation':
                    assert window.evaluate_js("['Preparing tool arguments…', 'Validating animation…', 'Repairing animation…'].every(phase => window.__phases.includes(phase))")
                    assert window.evaluate_js("document.querySelector('.tool-log summary').textContent === 'Tool activity'")
                if prompt == 'Context rejection animation':
                    assert window.evaluate_js("[...document.querySelectorAll('.reply-note')].at(-1).textContent.includes('Tool results and any repair history')")
                if prompt == 'Complete':
                    assert window.evaluate_js("window.__phases.includes('Saving…')")
                window.evaluate_js("window.__terminalReload = true; setTimeout(() => location.reload(), 0)")
                wait("!window.__terminalReload && document.querySelector('#status-text').textContent === 'Ready'")
                if diagnostic:
                    assert window.evaluate_js("[...document.querySelectorAll('.reply-note')].at(-1).textContent.includes(" + json.dumps(diagnostic) + ")")
                    assert window.evaluate_js("[...document.querySelectorAll('.reply-note')].every(n => !n.open)")
                    assert window.evaluate_js("[...document.querySelectorAll('.reply-diagnostic')].every(n => n.getClientRects().length === 0)")
                    window.evaluate_js("[...document.querySelectorAll('.reply-note summary')].at(-1).click()")
                    assert window.evaluate_js("[...document.querySelectorAll('.reply-diagnostic')].at(-1).getClientRects().length > 0")
                    before_copies = len(copied_diagnostics)
                    window.evaluate_js("const b = [...document.querySelectorAll('.copy-diagnostics')].at(-1); b.focus(); b.click()")
                    deadline = time.monotonic() + 5
                    while len(copied_diagnostics) == before_copies and time.monotonic() < deadline:
                        time.sleep(.03)
                    assert len(copied_diagnostics) == before_copies + 1
                    assert 'Reply state: error' in copied_diagnostics[-1]
                    assert 'Assistant message ID: unavailable' not in copied_diagnostics[-1]
                assert window.evaluate_js("document.querySelector('#generation-speed').textContent === '— tokens/s' && !document.querySelector('.is-streaming, .tool-activity')")
            # Admission before persistence restores the composer and has no
            # stored reply row. Reload retains its draft and folds its notice.
            window.evaluate_js("const i = document.querySelector('#input'); i.value = 'x'.repeat(160000); i.dispatchEvent(new Event('input')); document.querySelector('#send').click()")
            wait("document.querySelector('#status-text').textContent === 'Reply failed' && document.querySelector('.notice[data-tone=error]')?.textContent.includes('conservative host estimate')")
            assert window.evaluate_js("document.querySelector('#input').value.length === 160000 && document.querySelector('#send').textContent === 'Send'")
            window.evaluate_js("window.__admissionReload = true; setTimeout(() => location.reload(), 0)")
            wait("!window.__admissionReload && document.querySelector('#status-text').textContent === 'Ready' && document.querySelector('.notice[data-tone=error]')?.textContent.includes('conservative host estimate')")
            assert window.evaluate_js("!document.querySelector('.notice[data-tone=error]').open && document.querySelector('.notice[data-tone=error] .prose').getClientRects().length === 0")
            assert window.evaluate_js("document.querySelector('#input').value.length === 160000")
            window.evaluate_js("document.querySelector('.notice[data-tone=error] summary').click(); document.querySelector('.notice .copy-diagnostics').click()")
            wait("document.querySelector('#toast').textContent === 'Diagnostics copied'")
            assert 'Assistant message ID: unavailable' in copied_diagnostics[-1]
            assert 'conservative host estimate' not in copied_diagnostics[-1]
            # Navigate through the actual bridge while a provider is held open.
            saved_chat = window.evaluate_js("document.querySelector('.chat-item[aria-current=true]').dataset.id")
            saved_count = window.evaluate_js("document.querySelectorAll('.msg').length")
            window.evaluate_js("window.pywebview.api.new_conversation()")
            wait("document.querySelectorAll('.msg').length === 0")
            running_chat = window.evaluate_js("document.querySelector('.chat-item[aria-current=true]').dataset.id")

            def open_chat(chat_id):
                window.evaluate_js(f"document.querySelector('.chat-item[data-id=\"{chat_id}\"]').click()")
                wait(f"document.querySelector('.chat-item[aria-current=true]').dataset.id === '{chat_id}'")

            for prompt, outcome in [('Navigation complete', 'complete'), ('Navigation error', 'error')]:
                navigation_release.clear()
                window.evaluate_js("const i = document.querySelector('#input'); i.value = " + json.dumps(prompt)
                                   + "; i.dispatchEvent(new Event('input')); document.querySelector('#send').click()")
                wait("document.querySelector('.is-streaming .body')?.textContent.includes('Navigation partial answer')")
                for _ in range(2):
                    open_chat(saved_chat)
                    wait(f"document.querySelectorAll('.msg').length === {saved_count}")
                    assert window.evaluate_js("!document.querySelector('#column').textContent.includes('Navigation partial answer') && document.querySelector('#send').textContent === 'Stop'")
                    # Scrolling history in B remains usable during A's response.
                    window.evaluate_js("document.querySelector('#transcript').scrollTop = 50")
                    assert window.evaluate_js("document.querySelector('#transcript').scrollTop > 0")
                    open_chat(running_chat)
                    wait("document.querySelector('.is-streaming .body')?.textContent.includes('Navigation partial answer')")
                    assert window.evaluate_js("document.querySelectorAll('.is-streaming').length === 1 && document.querySelector('.is-streaming .thinking pre').textContent === 'Navigation reasoning'")
                open_chat(saved_chat)
                wait(f"document.querySelectorAll('.msg').length === {saved_count}")
                navigation_release.set()
                wait("document.querySelector('#send').textContent === 'Send'")
                assert window.evaluate_js(f"document.querySelector('.chat-item[aria-current=true]').dataset.id === '{saved_chat}' && document.querySelectorAll('.msg').length === {saved_count}")
                assert window.evaluate_js("!document.querySelector('#column').textContent.includes('Navigation scripted provider error')")
                open_chat(running_chat)
                wait("[...document.querySelectorAll('.msg.assistant .body')].at(-1)?.textContent.includes('Navigation partial answer')")
                assert window.evaluate_js("!document.querySelector('.is-streaming')")
                if outcome == 'error':
                    assert window.evaluate_js("[...document.querySelectorAll('.reply-note')].at(-1).textContent.includes('Navigation scripted provider error')")
                else:
                    assert window.evaluate_js("[...document.querySelectorAll('.msg.assistant .body')].at(-1).textContent.includes('completed')")
            print('Native chat navigation passed: repeated live replay, readable/scrollable other chat, background completion/error without navigation or transcript contamination.', flush=True)
            print('Native GTK/WebKit passed: modal focus, Shift+Tab reasoning effort, reload during streaming, reasoning, stop/Retry/Ctrl+R, draft preservation, reasoning-only failure, repair exhaustion, real context rejection, completion, phase/outcome announcements, cleared activity/speed, quiet history with expandable diagnostics, metadata-only copying before/after reload and on unsaved failures, drop guidance.', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            navigation_release.set()
            window.destroy()
    window.events.loaded += loaded
    return window

with tempfile.TemporaryDirectory(prefix='bloom-session-check-') as directory:
    root = Path(directory)
    for name, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[name] = str(root / sub)
    config, runtime = make_runtime(root)
    config.default_model = runtime.controller.model_name = 'opencode-go/test-vision'
    runtime.controller.backend.stream = stream
    runtime.controller.backend.models = models
    runtime.controller.backend.reasoning_levels = lambda model: LEVELS
    webview.create_window = create
    with patch('blackrose.clipboard.copy_text', capture_copy):
        run_gui(config, runtime=runtime)
if failures:
    raise SystemExit(1)
