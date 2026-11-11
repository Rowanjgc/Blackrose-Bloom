"""Native GTK/WebKit Mermaid probe with scratch data and no live providers."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from blackrose.gui.app import run_gui
from blackrose.gui.render import to_html
import webview


failures = []
repair_calls = []
original_create = webview.create_window
BAD_DIAGRAM = 'flowchart LR\nA[Start]-->B[Verify]\\nB-->C[Close]\n'
GOOD_DIAGRAM = BAD_DIAGRAM.replace('\\n', '\n')


async def stream(model, messages, tools, **options):
    if messages[0]['content'].startswith('Repair syntax in one Mermaid diagram'):
        assert tools == []
        repair_calls.append(messages)
        answer = GOOD_DIAGRAM
    else:
        answer = 'Before.\n\n```mermaid\n' + BAD_DIAGRAM + '```\n\nAfter.'
    yield {'delta': {'content': answer}, 'finish_reason': 'stop'}


def create(*args, **kwargs):
    # WebKit suspends IntersectionObserver in hidden windows. Exercise the same
    # visibility-driven scheduling as the real desktop (CI supplies Xvfb).
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
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if window.evaluate_js(script):
                        return
                    time.sleep(.04)
                raise AssertionError((script, window.evaluate_js(
                    "document.querySelector('.diagram-status')?.textContent")))

            def show(source):
                payload = [['transcript', {'messages': [{'role': 'assistant', 'state': 'complete',
                    'html': to_html('```mermaid\n' + source + '\n```')}]}]]
                window.evaluate_js('bloom.receive(' + json.dumps(payload) + ')')

            wait("document.querySelector('#status-text').textContent === 'Ready'")
            assert window.evaluate_js("!document.querySelector('.diagram-renderer')")
            sources = (
                'flowchart LR\naccTitle: Native flow\nA[Read]-->B[Understand]',
                'sequenceDiagram\nUser->>Assistant: Ask\nAssistant-->>User: Explain',
                'stateDiagram-v2\n[*]-->Draft\nDraft-->Ready',
                'classDiagram\nclass Reader\nReader-->Book',
                'erDiagram\nREADER ||--o{ BOOK : reads',
                'mindmap\n root((Learning))\n  Read\n  Practice',
                'architecture-beta\n group api(cloud)[API]\n service db(database)[Database] in api',
            )
            for source in sources:
                show(source)
                wait("document.querySelector('#column .diagram-image')?.naturalWidth > 0 && "
                     "!document.querySelector('.diagram-viewport').hidden")
                assert window.evaluate_js("document.querySelectorAll('.diagram-renderer').length === 1")
                assert window.evaluate_js("document.querySelector('.diagram-renderer').contentDocument === null")
                assert window.evaluate_js("!document.querySelector('.msg .body svg')")
            window.evaluate_js("document.querySelector('.diagram-control').click()")
            wait("!document.querySelector('#scrim').hidden && document.querySelector('#app').inert")
            assert window.evaluate_js("document.querySelector('.image-viewer img').naturalWidth > 0")
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            wait("!document.querySelector('#app').inert")
            assert window.evaluate_js("document.activeElement.textContent === 'Expand'")
            window.evaluate_js("[...document.querySelectorAll('.diagram-control')].find(b=>b.textContent==='Source').click()")
            assert window.evaluate_js("!document.querySelector('figure[data-mermaid] pre').hidden")
            show('%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B')
            wait("document.querySelector('.diagram-status')?.textContent.startsWith('Diagram unavailable:')")
            assert window.evaluate_js("!document.querySelector('figure[data-mermaid] pre').hidden")
            show('flowchart LR\nA["<script>parent.bad=true</script>"]-->B[Safe]')
            wait("document.querySelector('#column .diagram-image')?.naturalWidth > 0")
            assert window.evaluate_js("!window.bad")
            window.evaluate_js("const i=document.querySelector('#input'); i.value='Explain a workflow.'; i.dispatchEvent(new Event('input')); document.querySelector('#send').click()")
            wait("document.querySelector('.msg[data-message-id] .diagram-image')?.naturalWidth > 0 && "
                 "document.querySelector('#send').textContent === 'Send'")
            assert len(repair_calls) == 1
            assert window.evaluate_js("document.querySelector('.msg[data-message-id] .body').textContent.includes('Before.') && "
                "document.querySelector('.msg[data-message-id] .body').textContent.includes('After.')")
            assert window.evaluate_js("!document.querySelector('.msg[data-message-id] pre code').textContent.includes('\\\\n')")
            print('Native Mermaid passed: seven full-library types, passive SVG images, one isolated renderer, source/expand/focus and configuration fallback.', flush=True)
            print('Native Mermaid repair passed: real bridge syntax report, one tool-free request, in-place update and preserved prose.', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            from gi.repository import GLib
            GLib.idle_add(lambda: window.destroy() or False)

    window.events.loaded += loaded
    return window


with tempfile.TemporaryDirectory(prefix='bloom-diagrams-check-') as directory:
    root = Path(directory)
    for name, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[name] = str(root / sub)
    config, runtime = make_runtime(root)
    runtime.controller.backend.stream = stream
    webview.create_window = create
    run_gui(config, runtime=runtime)
if failures:
    raise SystemExit(1)
