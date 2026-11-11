"""Real GTK/WebKit script review and native consent with isolated scripted providers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

import webview

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime
from test_turns import Backend
from blackrose.gui.app import run_gui
from blackrose.skill_execution import ScriptUnavailable, runtime_command

ROOT = Path(__file__).resolve().parents[1]
original_create = webview.create_window
failures = []


def confinement_problem():
    """Why this host cannot start the confined skill runtime, or None.

    Runs the real launcher with a trivial entry point, so a missing Bubblewrap,
    runtime or libseccomp, or a refused namespace, is reported with its own
    diagnostic instead of as a timeout waiting for the artifact card.
    """
    with tempfile.TemporaryDirectory(prefix='bloom-skill-probe-') as stage:
        Path(stage, 'probe.py').write_text("print('ok')")
        try:
            command = runtime_command(Path(stage), 'probe.py')
        except ScriptUnavailable as error:
            return str(error)
        probe = subprocess.run(command, capture_output=True, timeout=30, env={})
    if probe.returncode or not probe.stdout.startswith(b'BLACKROSE_SCRIPT_V1\n'):
        detail = probe.stderr.decode('utf-8', errors='replace')[-2000:].strip()
        return f'confined runtime exited {probe.returncode}: {detail or "no diagnostic output"}'
    return None


def create(*args, **kwargs):
    window = original_create(*args, **kwargs)
    checked = False

    def loaded():
        nonlocal checked
        if checked:
            return
        checked = True
        try:
            def wait_for(condition):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if condition():
                        return
                    time.sleep(.04)
                raise AssertionError('Timed out waiting for skill script UI')

            def wait(script):
                wait_for(lambda: window.evaluate_js(script))

            def click(label):
                wait(f"(() => {{const button = [...document.querySelectorAll('#dialog-detail button:not(:disabled)')]"
                     f".find(b => b.textContent === {json.dumps(label)}); if (button) button.click(); return !!button;}})()")

            def review():
                click('Review scripts…')
                wait("document.querySelector('#dialog-title').textContent === 'Review skill scripts'")
                assert window.evaluate_js("document.querySelector('#dialog-detail').textContent.includes('statistics.mean')")
                assert window.evaluate_js("document.querySelector('#dialog-detail').textContent.includes('SHA-256')")

            def continue_review():
                window.evaluate_js("document.querySelectorAll('#dialog-actions button')[1].click()")
                wait_for(lambda: window._blackrose_consent._current is not None)
                assert window._blackrose_consent._title.get_text() == 'Allow skill script execution?'
                assert 'statistics.mean' in window._blackrose_consent._payload.get_text()

            def answer(yes):
                from gi.repository import GLib
                sheet = window._blackrose_consent
                wait_for(lambda: sheet._current is not None and (not yes or sheet._current.get('armed')))
                GLib.idle_add(lambda: (sheet._answer(yes), GLib.SOURCE_REMOVE)[1])
                wait_for(lambda: sheet._current is None)

            wait("document.querySelector('#status-text').textContent === 'Ready'")
            window.evaluate_js("document.querySelector('#skills-button').click()")
            review()
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            wait("document.querySelector('#dialog-title').textContent === 'Skills'")
            assert not runtime.controller.skills.has_execution_approval()
            review()
            continue_review()
            answer(False)
            wait("document.querySelector('#dialog-title').textContent === 'Skills'")
            assert not runtime.controller.skills.has_execution_approval()
            review()
            continue_review()
            answer(True)
            wait("document.querySelector('#dialog-title').textContent === 'Skills'")
            assert runtime.controller.skills.has_execution_approval()
            click('Revoke execution')
            wait_for(lambda: not runtime.controller.skills.has_execution_approval())
            review()
            continue_review()
            answer(True)
            wait("document.querySelector('#dialog-title').textContent === 'Skills'")
            window.evaluate_js("document.querySelector('#dialog-actions button').click()")
            window.evaluate_js("document.querySelector('#input').value='Summarize 1, 2 and 6'; document.querySelector('#send').click()")
            wait("document.querySelector('.artifact-card strong')?.textContent.includes('numeric-summary')")
            wait("document.querySelector('#send').textContent === 'Send'")
            report = root / 'downloads/Blackrose/numeric-summary.txt'
            assert 'Mean: 3' in report.read_text()
            # A resource change leaves instruction reads working and denies execution.
            package = runtime.controller.skills.root / 'numeric-summary'
            (package / 'new-resource.txt').write_text('changed after approval')
            try:
                runtime.controller.skills.prepare_execution('numeric-summary', 'summarize')
            except ValueError:
                pass
            else:
                raise AssertionError('Changed resources retained execution permission')
            assert runtime.controller.skills.read('numeric-summary')['text']
            print('GTK/WebKit skill scripts passed: source review, Cancel, native denial/acceptance, '
                  'revocation, explicit execution, saved report and resource invalidation.', flush=True)
        except BaseException as error:
            failures.append(error)
            traceback.print_exc()
        finally:
            window.destroy()

    window.events.loaded += loaded
    return window


problem = confinement_problem()
if problem:
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        # Hosted runners refuse Bubblewrap's loopback setup, so this probe cannot
        # run there. The offline unit tests skip the same way; run this probe locally.
        print(f'SKIPPED skill script GUI probe on hosted CI: {problem}', flush=True)
        raise SystemExit(0)
    print(f'Skill script confinement is unavailable on this host: {problem}', file=sys.stderr, flush=True)
    raise SystemExit(1)

with tempfile.TemporaryDirectory(prefix='bloom-skill-scripts-') as temporary:
    root = Path(temporary)
    for variable, name in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state')):
        os.environ[variable] = str(root / name)
    (root / 'config').mkdir()
    (root / 'config/user-dirs.dirs').write_text(f'XDG_DOWNLOAD_DIR="{root / "downloads"}"\n')
    config, runtime = make_runtime(root)
    runtime.controller.skills.install(str(ROOT / 'skills/numeric-summary'), approved=True)
    runtime.controller.backend = Backend([[('run_skill_script', {
        'skill_id': 'numeric-summary', 'entry_point': 'summarize', 'input': {'values': [1, 2, 6]}})], 'Report saved.'])
    webview.create_window = create
    run_gui(config, runtime=runtime)
if failures:
    raise SystemExit(1)
