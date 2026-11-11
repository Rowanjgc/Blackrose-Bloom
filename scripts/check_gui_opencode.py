"""Verify provider browsing and the compact Go model picker in real GTK/WebKit.

Uses a temporary database/config and mock Go HTTP transport. No real API keys,
paid requests, local router operations, or user preferences are touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import Mock

import httpx
import webview

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from blackrose.backend.opencode import PREFIX, OpenCodeGoClient
from blackrose.backend.providers import ChatProviders
from blackrose.gui.app import run_gui

failures = []
requests = []
offline = False
original_create = webview.create_window


def handler(request):
    requests.append(request)
    if request.method == 'GET':
        return httpx.Response(503 if offline else 200, json={'data': [
            {'id': f'glm-test-{i:02d}'} for i in range(80)]})
    return httpx.Response(200, text='data: ' + json.dumps({'choices': [
        {'delta': {'content': 'Go mock reply.'}, 'finish_reason': 'stop'}]}) + '\n\n')


def create(*args, **kwargs):
    kwargs['hidden'] = True
    window = original_create(*args, **kwargs)

    def loaded():
        global offline
        try:
            def wait(script):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if window.evaluate_js(script):
                        return
                    time.sleep(.05)
                raise AssertionError(script)

            def provider(value):
                window.evaluate_js("(() => {const s=document.querySelector('[aria-label=\"Model provider\"]');"
                    f"s.value={json.dumps(value)};s.dispatchEvent(new Event('change'));" + '})()')

            wait("document.querySelector('#status-text').textContent === 'Ready'")
            assert not requests, 'Local startup contacted Go'
            window.evaluate_js("document.querySelector('#models-button').click()")
            wait("document.querySelector('.model-name')?.textContent === 'test-vision'")
            assert not requests, 'Local picker contacted Go'
            provider('opencode-go')
            wait("document.querySelector('.model-count')?.textContent === '80'")
            assert window.evaluate_js("document.querySelectorAll('.model-row').length === 80")
            assert window.evaluate_js("[...document.querySelectorAll('.model-row')].every(r => !r.getClientRects().length)")
            assert runtime.controller.model_name == 'test-vision', 'Browsing selected a model'
            keychain.get.assert_not_called()
            window.evaluate_js("const s=document.querySelector('.model-search');"
                "s.value='glm-test-79';s.dispatchEvent(new Event('input'))")
            wait("document.querySelectorAll('.model-row').length === 1")
            window.evaluate_js("document.querySelector('.model-favorite').click()")
            wait("document.querySelector('.model-favorite')?.getAttribute('aria-pressed') === 'true'")
            assert runtime.controller.config.favorite_models == [PREFIX + 'glm-test-79']
            keychain.get.assert_not_called()
            assert runtime.controller.model_name == 'test-vision'
            window.evaluate_js("document.querySelector('.model-more').click()")
            window.evaluate_js("document.querySelector('.model-default').click()")
            wait("document.querySelector('.model-summary')?.textContent.includes('Default')")
            assert runtime.controller.config.default_model == PREFIX + 'glm-test-79'
            assert runtime.controller.model_name == PREFIX + 'glm-test-79'
            assert window.evaluate_js("document.querySelectorAll('.model-row').length === 1")
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            wait("!document.querySelector('#dialog').open")
            window.evaluate_js("document.querySelector('#input').value='Hello';document.querySelector('#send').click()")
            wait("document.querySelector('.msg.assistant .body')?.textContent.includes('Go mock reply')")
            post = next(r for r in requests if r.method == 'POST')
            assert post.headers['x-opencode-session']
            assert json.loads(post.content)['model'] == 'glm-test-79'
            offline = True
            window.evaluate_js("document.querySelector('#models-button').click()")
            wait("document.querySelector('.panel-error')?.textContent.includes('503')")
            provider('local')
            wait("document.querySelector('.model-name')?.textContent === 'test-vision'")
            window.evaluate_js("document.querySelector('.model-choice').click()")
            wait("document.querySelector('.model-summary')?.textContent.includes('Current')")
            assert runtime.controller.model_name == 'test-vision'
            assert runtime.controller.config.default_model == PREFIX + 'glm-test-79'
            print('GTK/WebKit passed: lazy provider discovery, 80 folded/searchable models, saved Favorites without loading, browsing isolation, '
                  'saved Go default, mock streamed reply, and local recovery after a Go outage.', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            window.destroy()

    window.events.loaded += loaded
    return window


with tempfile.TemporaryDirectory(prefix='bloom-go-check-') as directory:
    root = Path(directory)
    for name, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[name] = str(root / sub)
    config, runtime = make_runtime(root)
    keychain = Mock(get=Mock(return_value='test-go-secret-never-log'))
    runtime.controller.backend = ChatProviders(runtime.controller.backend,
        OpenCodeGoClient(keychain=keychain, transport=httpx.MockTransport(handler)))
    webview.create_window = create
    result = run_gui(config, runtime=runtime)
if failures:
    raise SystemExit(1)
