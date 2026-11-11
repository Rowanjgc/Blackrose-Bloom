"""Exercise workspace, autocomplete, artifact and approval UI in real WebKit.

Uses scratch data and scripted providers; no model, paid image or network calls.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from test_turns import Backend
from blackrose.documents import render_pdf
from blackrose.gui.app import run_gui
import webview

failures = []
original_create = webview.create_window


def create(*args, **kwargs):
    kwargs['hidden'] = True
    window = original_create(*args, **kwargs)
    window.create_file_dialog = lambda *a, **k: [str(work)]
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
                import json
                window.evaluate_js(f"document.querySelector('#input').value={json.dumps(value)}; "
                    "document.querySelector('#input').focus(); document.querySelector('#input').dispatchEvent(new Event('input'))")

            def choose():
                window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}))")

            wait("document.querySelector('#status-text').textContent === 'Ready'")
            window.evaluate_js("document.querySelector('#workspace-button').click()")
            wait("document.querySelector('#app').inert && document.querySelector('#dialog-title').textContent === 'Workspace'")
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            wait("document.querySelector('#workspace-value').textContent === 'work'")
            type_text('@no-matching-workspace-file')
            wait("document.querySelector('.command-status')?.textContent.includes('No matching files')")
            window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")
            type_text('$ani')
            wait("document.querySelector('#commands [role=option]')?.textContent.includes('animation-explainer')")
            window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowDown', bubbles:true}))")
            choose()
            wait("document.querySelector('#input').value === '$animation-explainer '")
            type_text('$animation-explainer Summarize @notes')
            wait("document.querySelector('#commands [role=option]')?.textContent.includes('notes.md')")
            choose()
            wait("document.querySelector('#input').value.includes('@{')")
            text = window.evaluate_js("document.querySelector('#input').value")
            type_text(text + 'and @report')
            wait("document.querySelector('#commands [role=option]')?.textContent.includes('report.pdf')")
            window.evaluate_js("document.querySelector('#commands [role=option]').dispatchEvent(new PointerEvent('pointerdown', {bubbles:true}))")
            wait("document.querySelector('#input').value.includes('report.pdf}')")
            # Reference payloads survive an actual page reload with the draft.
            window.evaluate_js("window.pywebview.api.update_draft(document.querySelector('#input').value).then(() => {window.__saved=true})")
            wait('window.__saved === true')
            window.evaluate_js("window.__beforeReload=true; setTimeout(() => location.reload(), 0)")
            wait("!window.__beforeReload && document.querySelector('#input').value.includes('report.pdf}')")
            window.evaluate_js("document.querySelector('#send').click()")
            # New workspace files are proposals until each is approved on its review card.
            wait("document.querySelectorAll('[data-approval]').length === 3 && document.querySelector('#send').textContent === 'Send'")
            assert not (work / 'summary.md').exists()
            for remaining in (2, 1, 0):
                window.evaluate_js("document.querySelector('[data-approval] button').click()")
                wait("document.querySelector('#app').inert && document.querySelector('#dialog-title').textContent.startsWith('Create ')")
                window.evaluate_js("document.querySelectorAll('#dialog-actions button')[1].click()")
                wait(f"!document.querySelector('#app').inert && document.querySelectorAll('[data-approval]').length === {remaining}")
            wait("document.querySelectorAll('.artifact-card strong').length === 3")
            controller = runtime.controller
            import json
            request = controller.backend.requests[0][-1]['content']
            refs, _ = json.JSONDecoder().raw_decode(request.split('Selected references (local files/directories are pointers; use file tools to read):\n')[1])
            assert len(refs) == 3, refs
            # Selected skill instructions travel with the request, outside the system message.
            assert 'GUI_EXPLICIT_SKILL' not in controller.backend.requests[0][0]['content']
            assert 'GUI_EXPLICIT_SKILL' in request
            assert (work / 'summary.md').is_file() and (work / 'summary.pdf').read_bytes().startswith(b'%PDF-')
            window.evaluate_js("[...document.querySelectorAll('.artifact-card')].find(c => c.textContent.includes('summary.html')).querySelector('button').click()")
            wait("document.querySelector('iframe.artifact-html') !== null")
            assert window.evaluate_js("document.querySelector('iframe.artifact-html').getAttribute('sandbox') === ''")
            assert window.evaluate_js("!window.__unsafeArtifact")
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            wait("!document.querySelector('#app').inert")
            window.evaluate_js("[...document.querySelectorAll('.artifact-card')].find(c => c.textContent.includes('summary.md')).querySelector('button').click()")
            wait("document.querySelector('#dialog-detail h1')?.textContent === 'Summary'")
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            type_text('Revise notes')
            window.evaluate_js("document.querySelector('#send').click()")
            wait("document.querySelector('[data-approval] button') !== null && document.querySelector('#send').textContent === 'Send'")
            assert (work / 'notes.md').read_text() == 'Original notes'
            window.evaluate_js("document.querySelector('[data-approval] button').click()")
            wait("document.querySelector('#dialog-detail').textContent.includes('Proposed replacement:')")
            window.evaluate_js("document.querySelectorAll('#dialog-actions button')[1].click()")
            wait("[...document.querySelectorAll('.artifact-card strong')].some(e => e.textContent === 'notes.md')")
            assert (work / 'notes.md').read_text() == 'Revised notes'
            # The original leaves the project for the private backup store.
            assert sorted(p.name for p in work.glob('notes.md*')) == ['notes.md']
            [backup] = (root / 'edit-backups').glob('*/*-notes.md')
            assert backup.read_text() == 'Original notes'
            # Browsing directories and referencing a folder uses the same composer path.
            window.evaluate_js("document.querySelector('#workspace-button').click()")
            wait("document.querySelector('.workspace-file') !== null")
            window.evaluate_js("[...document.querySelectorAll('.workspace-file')].find(e => e.textContent.includes('subfolder')).querySelector('button:last-child').click()")
            wait("document.querySelector('#input').value.includes('subfolder}')")
            # Create more candidates than the scan cap; after the bounded scan,
            # choose one proven to be absent from the actual cached result.
            candidates = []
            for index in range(2100):
                candidate = work / f'candidate file with spaces {index:04}.txt'
                candidate.touch()
                candidates.append(candidate)
            omitted_nested = work / 'omitted nested'
            omitted_nested.mkdir()
            (omitted_nested / 'nested file.txt').write_text('also reachable')
            type_text('@no-such-large-entry')
            wait("document.querySelector('.command-status')?.textContent.includes('bounded set')")
            indexed = {entry['path'] for entry in runtime.controller._reference_index[2]}
            omitted = next(candidate for candidate in candidates if str(candidate) not in indexed)
            window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")
            type_text('@{' + omitted.name)
            wait(f"document.querySelector('#commands [role=option]')?.textContent.includes({json.dumps(omitted.name)})")
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
            window.evaluate_js("window.pywebview.api.clear_workspace()")
            wait("document.querySelector('#workspace-value').textContent === 'Downloads outputs'")
            type_text('@no-saved-output-matches')
            wait("document.querySelector('.command-status')?.textContent.includes('No workspace selected')")
            window.evaluate_js("document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")
            window.evaluate_js("document.querySelector('#workspace-button').click()")
            wait("document.querySelector('#app').inert && document.querySelector('#dialog-title').textContent === 'Workspace'")
            window.evaluate_js("document.querySelectorAll('#dialog-actions button')[1].click()")
            wait("document.querySelector('#workspace-value').textContent === 'Downloads outputs'")
            print('Native GTK/WebKit passed: workspace picker/clear/browser, fuzzy $/@ selection, keyboard/mouse, multiple durable references, real outputs, rendered Markdown, sandboxed HTML, reviewed new files, reviewed edit and backup.', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            window.destroy()

    window.events.loaded += loaded
    return window


with tempfile.TemporaryDirectory(prefix='bloom-workspace-check-') as directory:
    root = Path(directory)
    for name, sub in [('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')]:
        os.environ[name] = str(root / sub)
    # Keep outputs out of the real Downloads folder.
    (root / 'config').mkdir()
    (root / 'config' / 'user-dirs.dirs').write_text(f'XDG_DOWNLOAD_DIR="{root / "downloads"}"\n')
    work = root / 'work'
    work.mkdir()
    (work / 'notes.md').write_text('Original notes')
    (work / 'report.pdf').write_bytes(render_pdf('# Report\nUseful report'))
    (work / 'subfolder').mkdir()
    config, runtime = make_runtime(root)
    skill = runtime.controller.skills.root / 'animation-explainer'
    skill.mkdir()
    (skill / 'SKILL.md').write_text('---\nname: Animation Explainer\ndescription: Animated explanations\n---\nGUI_EXPLICIT_SKILL')
    runtime.controller.skills.approve('animation-explainer', runtime.controller.skills.review('animation-explainer')['digest'])
    runtime.controller.backend = Backend([
        [('file_read', {'path': 'notes.md'})],
        [('document_create', {'path': 'summary.md', 'content': '# Summary\nA useful summary'}),
         ('document_create', {'path': 'summary.html', 'content': '<h1>Summary</h1><script>parent.__unsafeArtifact=true</script>'}),
         ('document_create', {'path': 'summary.pdf', 'content': '# Summary\nA useful summary'})],
        'Files saved.',
        [('file_edit', {'path': 'notes.md', 'content': 'Revised notes'})],
        'Review the proposed edit.',
    ])
    webview.create_window = create
    run_gui(config, runtime=runtime)
if failures:
    raise SystemExit(1)
