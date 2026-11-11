"""Verify generated-image display in real GTK/WebKit using a mocked paid API."""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx
import webview

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime, png
from test_image_generation import GeneratingBackend, Keychain
from blackrose.gui.app import run_gui
from blackrose.image_generation import OpenRouterImages

failures = []
original_create = webview.create_window


def create(*args, **kwargs):
    kwargs['hidden'] = True
    window = original_create(*args, **kwargs)

    def loaded():
        try:
            def wait(script):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if window.evaluate_js(script):
                        return
                    time.sleep(.05)
                print(window.evaluate_js("document.querySelector('#transcript').innerHTML"), flush=True)
                print(window.evaluate_js("document.querySelector('#status-text').textContent"), flush=True)
                raise AssertionError(script)

            wait("document.querySelector('#status-text').textContent === 'Ready'")
            window.evaluate_js("document.querySelector('#input').value = 'Generate an illustration'; document.querySelector('#send').click()")
            # Hidden WebKit windows do not advance entrance animations or
            # IntersectionObserver, so explicitly request the lazy preview.
            wait("document.querySelector('.reply-images .image-frame')")
            window.evaluate_js("document.querySelector('.reply-images .image-load')?.click()")
            wait("document.querySelector('.reply-images img')?.naturalWidth === 320")
            wait("document.querySelector('.msg.assistant .body')?.textContent.includes('The illustration is ready')")
            window.evaluate_js("document.querySelector('.reply-images .image-open').click()")
            wait("document.querySelector('.image-viewer img')?.naturalWidth === 320")
            window.evaluate_js("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape',bubbles:true}))")
            window.evaluate_js("document.querySelector('.chat-item[aria-current=true]').click()")
            wait("document.querySelector('.reply-images .image-load')")
            window.evaluate_js("document.querySelector('.reply-images .image-load')?.click()")
            wait("document.querySelector('.reply-images img')?.naturalWidth === 320")
            assert window.evaluate_js("document.querySelector('.reply-images .image-frame').dataset.previewId > 0")
            print('Native GTK/WebKit passed: generated image, enlargement, and saved reply preview.', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            window.destroy()

    window.events.loaded += loaded
    return window


with tempfile.TemporaryDirectory(prefix='bloom-generation-check-') as directory:
    root = Path(directory)
    for name, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[name] = str(root / sub)
    config, runtime = make_runtime(root)
    runtime.controller.backend = GeneratingBackend()
    runtime.controller.config.image_provider = 'openrouter'
    runtime.controller.image_generator = OpenRouterImages(config.image_model, keychain=Keychain(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})))
    webview.create_window = create
    run_gui(config, runtime=runtime)
if failures:
    raise SystemExit(1)
