"""Exercise attachment and paste rendering in the actual GTK/WebKit window.

Run .venv/bin/python scripts/check_gui_images.py. Uses a scratch database and
scripted backend; leaves the user's conversations, clipboard and models alone.
"""
from __future__ import annotations
import base64
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime, png
import webview
from blackrose.gui.app import run_gui

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
                raise AssertionError(script)
            wait("document.querySelector('#status-text').textContent === 'Ready'")
            assert window.evaluate_js("!!document.querySelector('#models-button') && !!document.querySelector('#library-button') && !!document.querySelector('#skills-button')")
            image = base64.b64encode(png()).decode()
            # The same validated path used by the native attachment picker.
            window.evaluate_js('void window.pywebview.api.attach_path(' + json.dumps(str(image_path)) + ')')
            wait("document.querySelector('.thumbnail img')?.naturalWidth === 320")
            window.evaluate_js("document.querySelector('.thumbnail .image-open').click()")
            wait("document.querySelector('.image-viewer img')?.naturalWidth === 320")
            window.evaluate_js("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape',bubbles:true}))")
            wait("document.activeElement.classList.contains('image-open')")
            window.evaluate_js("document.querySelector('#attachments > .attachment > button').click()")
            wait("document.querySelector('#attachments').childElementCount === 0")
            # Exercise browser paste -> FileReader -> bridge -> preview, without
            # reading or changing the desktop clipboard.
            window.evaluate_js('''(() => {
                const bytes = Uint8Array.from(atob(''' + json.dumps(image) + '''), c => c.charCodeAt(0));
                const transfer = new DataTransfer();
                transfer.items.add(new File([bytes], 'pasted.png', {type:'image/png'}));
                document.querySelector('#input').focus();
                document.dispatchEvent(new ClipboardEvent('paste', {clipboardData:transfer,bubbles:true,cancelable:true}));
            })()''')
            wait("document.querySelector('.thumbnail img')?.naturalWidth === 320")
            assert window.evaluate_js("document.querySelector('.attachment .name').textContent.includes('clipboard-image.png')")
            window.evaluate_js("document.querySelector('#input').value = 'Describe this image'; document.querySelector('#send').click()")
            wait("document.querySelector('.msg.user .image-frame img')?.naturalWidth === 320")
            wait("document.querySelector('.msg.assistant .body')?.textContent.includes('A framed image is visible')")
            window.evaluate_js("document.querySelector('.chat-item[aria-current=true]').click()")
            wait("document.querySelector('.image-frame[data-preview-id]')")
            window.evaluate_js("document.querySelector('.image-frame[data-preview-id] .image-load')?.click()")
            wait("document.querySelector('.msg.user .image-frame img')?.naturalWidth === 320")
            window.evaluate_js("document.querySelector('.msg.user .image-open').click()")
            wait("document.querySelector('.image-viewer img')?.naturalWidth === 320")
            print('Native GTK/WebKit passed: file attachment, synthetic paste, thumbnail, enlargement, send, saved preview, and focus restoration.', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            window.destroy()
    window.events.loaded += loaded
    return window

with tempfile.TemporaryDirectory(prefix='bloom-image-check-') as directory:
    root = Path(directory)
    for name, sub in [('XDG_CONFIG_HOME','config'), ('XDG_DATA_HOME','data'), ('XDG_STATE_HOME','state')]:
        os.environ[name] = str(root / sub)
    image_path = root / 'attached.png'
    image_path.write_bytes(png())
    config, runtime = make_runtime(root)
    webview.create_window = create
    run_gui(config, runtime=runtime)
if failures:
    raise SystemExit(1)
