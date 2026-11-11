"""H10 native WebKit timing, keyboard and AT-SPI observations on scratch data.

Run under isolated Xvfb/dbus: .venv/bin/python scripts/check_gui_housekeeping.py
--output artifacts/h10-native.json. Requires GTK/WebKit, Atspi typelibs,
xdotool/XTest, and a working accessibility bus in the private D-Bus session.
This records a baseline; it is deliberately not a performance or speech-output gate.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

from measure_housekeeping import identity, seed_chat, summary


def accessibility_snapshot(pid):
    import gi
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi
    Atspi.set_timeout(2000, 3000)
    desktop = Atspi.get_desktop(0)
    roots = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
    roots = [app for app in roots if app and app.get_process_id() == pid]
    rows = []

    def visit(item, depth=0):
        if item is None or depth > 35 or len(rows) >= 15000:
            return
        states = item.get_state_set()
        rows.append({'role': item.get_role_name(), 'name': item.get_name(),
                     'focused': states.contains(Atspi.StateType.FOCUSED),
                     'attributes': item.get_attributes()})
        for index in range(item.get_child_count()):
            visit(item.get_child_at_index(index), depth + 1)

    for app in roots:
        visit(app)
    print(json.dumps(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--accessibility-pause', type=float, default=0,
                        help='Pause after native waits/keys to let a screen reader process events')
    parser.add_argument('--speech-controls', action='store_true',
                        help='Compare existing and visible live-region updates on the scratch page')
    args = parser.parse_args()
    if not 0 <= args.accessibility_pause <= 2:
        parser.error('--accessibility-pause must be between 0 and 2 seconds')
    import webview
    from blackrose.gui.app import run_gui
    from blackrose.ui_documents import contract, UIStream

    report = identity()
    report['accessibility_pause_seconds'] = args.accessibility_pause
    report['native_script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['limitations'].append('AT-SPI roles/names/states reviewed; synthesized speech and human screen-reader usability not tested')
    errors = []
    original_create = webview.create_window
    with tempfile.TemporaryDirectory(prefix='blackrose-h10-native-') as directory:
        root = Path(directory)
        for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
            os.environ[name] = str(root / name)

        async def prepare():
            config, runtime = await seed_chat(root, 1000, 1000, 100)
            config.interactive_ui_enabled = True
            config.interactive_ui_actions_enabled = True
            repo = runtime.controller._repo
            message = await repo.add_message(runtime.controller.conversation.id, 'assistant', 'Editable preferences')
            stream = UIStream(message)
            # The example leaves preferences blank; supply the initial value the
            # native arrow-key check steps from, as the browser runtime test does.
            form = copy.deepcopy(contract()['examples']['form'])
            form['fields'][0]['default'] = 5
            stream.begin('form')
            stream.append(json.dumps(form) + '\n')
            stream.end()
            await repo.save_blocks(message.id, stream.blocks)
            # Reopen through the desktop lifecycle, outside the seeding event loop.
            await runtime.database.close()
            return config, runtime

        config, runtime = asyncio.run(prepare())
        start = time.perf_counter()

        def create(*positional, **kwargs):
            window = original_create(*positional, **kwargs)
            checked = False

            def loaded():
                nonlocal checked
                if checked:
                    return
                checked = True

                def evaluate(script):
                    return window.evaluate_js(script)

                def wait(script, seconds=40):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if evaluate(script):
                            time.sleep(args.accessibility_pause)
                            return
                        time.sleep(.03)
                    raise AssertionError('Timed out: ' + script)

                def key(name, shift=False, control=False):
                    # XTest delivers native focus/default-key behavior in the
                    # isolated X11 display; DOM KeyboardEvent does not.
                    windows = subprocess.check_output(['xdotool', 'search', '--onlyvisible', '--pid',
                        str(os.getpid()), '--name', '^Blackrose Bloom$'], text=True, timeout=5)
                    xid = windows.splitlines()[0]
                    subprocess.run(['xdotool', 'windowfocus', '--sync', xid], check=True, timeout=5)
                    combo = ('ctrl+' if control else '') + ('shift+' if shift else '') + name
                    subprocess.run(['xdotool', 'key', '--clearmodifiers', combo], check=True, timeout=5)
                    time.sleep(.08)
                    time.sleep(args.accessibility_pause)

                def tree():
                    print('Reading AT-SPI tree', flush=True)
                    result = subprocess.run([sys.executable, __file__, '--atspi', str(os.getpid())],
                                            capture_output=True, text=True, timeout=30)
                    if result.returncode:
                        raise RuntimeError('AT-SPI snapshot failed: ' + result.stderr[-2000:])
                    rows = json.loads(result.stdout)
                    assert rows, 'Application is absent from the AT-SPI tree'
                    print(f'AT-SPI tree: {len(rows)} nodes', flush=True)
                    # at-spi2-core 2.58 names push buttons "button"; aria-pressed makes a "toggle button".
                    roles = {'button', 'push button', 'toggle button', 'entry', 'text', 'spin button', 'combo box', 'dialog', 'status bar', 'form'}
                    return {'node_count': len(rows), 'controls': [r for r in rows if
                            r['focused'] or (r['role'] in roles and r['name']) or
                            any('live' in str(a) for a in r['attributes'])]}

                try:
                    wait("document.querySelector('#status-text').textContent==='Ready'")
                    wait("document.querySelector('.blackrose-ui input[name=days]')")
                    report['startup_to_ready_ms'] = round((time.perf_counter() - start) * 1000, 3)
                    report['dataset'] = {'conversations': 1000, 'messages': 1001, 'images': 100, 'editable_forms': 1}
                    report['rendered'] = evaluate("({messages:document.querySelectorAll('.msg').length, conversations:document.querySelectorAll('.chat-item').length, nodes:document.querySelectorAll('*').length})")
                    from gi.repository import Gtk, WebKit2
                    report['native_versions'] = {'gtk': '.'.join(str(f()) for f in (Gtk.get_major_version, Gtk.get_minor_version, Gtk.get_micro_version)),
                                                 'webkit': '.'.join(str(f()) for f in (WebKit2.get_major_version, WebKit2.get_minor_version, WebKit2.get_micro_version))}
                    samples = []
                    # Opening a conversation redraws through 'snapshot', which renders
                    # the transcript; 'transcript' alone comes from other render paths.
                    for _ in range(5):
                        evaluate("window.__h10done=false;window.__h10start=performance.now();window.__h10receive=bloom.receive;bloom.receive=events=>{window.__h10receive(events);if(events.some(([kind])=>kind==='snapshot'||kind==='transcript'))requestAnimationFrame(()=>requestAnimationFrame(()=>{window.__h10elapsed=performance.now()-window.__h10start;window.__h10done=true;}));};document.querySelector('.chat-item[aria-current=true]').click()")
                        wait('window.__h10done')
                        samples.append(evaluate('window.__h10elapsed'))
                        evaluate('bloom.receive=window.__h10receive')
                    report['reopen_through_bridge_and_two_animation_frames'] = summary(samples)
                    report['accessibility_main'] = tree()
                    print('Checking native keyboard navigation', flush=True)
                    # Native key events exercise WebKit focus defaults as well as app handlers.
                    evaluate("document.querySelector('#help-button').focus()")
                    key('Return')
                    wait("document.querySelector('#app').inert")
                    key('Tab')
                    report['dialog_tab_stays_inside'] = evaluate("document.querySelector('#dialog').contains(document.activeElement)")
                    evaluate("window.__h10keys=[];document.addEventListener('keydown',e=>window.__h10keys.push({key:e.key,code:e.code,keyCode:e.keyCode,shift:e.shiftKey,target:e.target.id}),true)")
                    key('Tab', shift=True)
                    report['dialog_shift_tab_stays_inside'] = evaluate("document.querySelector('#dialog').contains(document.activeElement)")
                    report['dialog_reverse_tab_observation'] = evaluate("({keys:window.__h10keys,active:document.activeElement.outerHTML.slice(0,300)})")
                    assert report['dialog_shift_tab_stays_inside'], report['dialog_reverse_tab_observation']
                    report['accessibility_dialog'] = tree()
                    key('Escape')
                    wait("!document.querySelector('#app').inert")
                    report['dialog_restores_trigger'] = evaluate("document.activeElement.id==='help-button'")
                    evaluate("document.querySelector('.chat-item').focus()")
                    key('Down')
                    report['conversation_arrow_navigation'] = evaluate("document.activeElement===document.querySelectorAll('.chat-item')[1]")
                    key('Escape')
                    report['list_escape_to_composer'] = evaluate("document.activeElement.id==='input'")
                    evaluate("document.querySelector('.blackrose-ui input[name=days]').focus()")
                    key('Down')
                    wait("document.querySelector('.blackrose-ui input[name=days]').value==='4'")
                    report['editable_form_native_key'] = True
                    report['accessibility_editable_form'] = tree()
                    # A fresh chat keeps the synthetic 1000-message history out
                    # of model-context admission. No inference is performed.
                    key('n', control=True)
                    wait("document.querySelectorAll('.msg').length===0")
                    evaluate("document.querySelector('#input').value='Complete synthetic reply';document.querySelector('#send').click()")
                    wait("document.querySelector('#reply-announcement').textContent.includes('Reply complete')")
                    report['reply_live_region'] = evaluate("({role:document.querySelector('#reply-announcement').getAttribute('role'),live:document.querySelector('#reply-announcement').getAttribute('aria-live'),text:document.querySelector('#reply-announcement').textContent})")
                    report['accessibility_completed_reply'] = tree()
                    if args.speech_controls:
                        evaluate("document.querySelector('#reply-announcement').textContent='Existing status control marker.'")
                        wait("document.querySelector('#reply-announcement').textContent==='Existing status control marker.'")
                        def control_state(selector):
                            return evaluate("""((selector) => {
                              const node = document.querySelector(selector);
                              const rect = node.getBoundingClientRect();
                              const style = getComputedStyle(node);
                              return {text: node.textContent, role: node.getAttribute('role'),
                                live: node.getAttribute('aria-live'), atomic: node.getAttribute('aria-atomic'),
                                display: style.display, visibility: style.visibility,
                                opacity: style.opacity, width: rect.width, height: rect.height,
                                clip: style.clip, clipPath: style.clipPath,
                                observed_at: new Date().toISOString()};
                            })(""" + json.dumps(selector) + ")")
                        existing_control = control_state('#reply-announcement')
                        time.sleep(3)
                        evaluate("""(() => {
                          const control = document.createElement('div');
                          control.id = 'h10-speech-control';
                          control.setAttribute('role', 'status');
                          control.setAttribute('aria-live', 'polite');
                          control.setAttribute('aria-atomic', 'true');
                          control.style.cssText = 'position:fixed;bottom:0;right:0;background:black;color:white;z-index:9999';
                          document.body.appendChild(control);
                          setTimeout(() => { control.textContent = 'Visible status control marker.'; }, 100);
                        })()""")
                        wait("document.querySelector('#h10-speech-control')?.textContent==='Visible status control marker.'")
                        visible_control = control_state('#h10-speech-control')
                        time.sleep(3)
                        evaluate("document.querySelector('#h10-speech-control').remove()")
                        report['speech_controls'] = {
                            'existing': 'Existing status control marker.',
                            'visible': 'Visible status control marker.',
                            'observed_existing': existing_control,
                            'observed_visible': visible_control,
                            'idle_seconds_after_each': 3,
                            'note': 'Synthetic controls for the screen-reader capture; no spoken result inferred here',
                        }
                    # Exercise actual app CSS at 200% root text size. Capture
                    # principal scrolling surfaces and toolbar/composer controls
                    # so a human can review the measurements without treating
                    # this synthetic resize as a screen-reader or usability test.
                    report['scaled_text_200_percent'] = evaluate("""(() => {
                      const root = document.documentElement;
                      const previous = root.style.fontSize;
                      const before = parseFloat(getComputedStyle(root).fontSize);
                      root.style.fontSize = `${before * 2}px`;
                      const selectors = ['#app', '#app-toolbar', '#menu-toggle',
                        '#models-button', '#workspace-button', '#library-button',
                        '#skills-button', '#new-chat', '#sidebar', '#transcript', '#composer',
                        '#input', '#send', '#status'];
                      const layout = Object.fromEntries(selectors.map(selector => {
                        const element = document.querySelector(selector);
                        if (!element) return [selector, null];
                        const rect = element.getBoundingClientRect();
                        return [selector, {x: Math.round(rect.x), y: Math.round(rect.y),
                          width: Math.round(rect.width), height: Math.round(rect.height),
                          clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
                          clientHeight: element.clientHeight, scrollHeight: element.scrollHeight,
                          fontSize: getComputedStyle(element).fontSize}];
                      }));
                      const result = {requestedScale: 2, rootFontBefore: `${before}px`,
                        rootFontAtScale: getComputedStyle(root).fontSize,
                        viewport: {width: innerWidth, height: innerHeight},
                        document: {clientWidth: root.clientWidth, scrollWidth: root.scrollWidth,
                          clientHeight: root.clientHeight, scrollHeight: root.scrollHeight},
                        layout, reducedMotionPreference: matchMedia('(prefers-reduced-motion: reduce)').matches};
                      root.style.fontSize = previous;
                      return result;
                    })()""")
                    assert report['scaled_text_200_percent']['layout']['#app-toolbar']['scrollWidth'] <= \
                        report['scaled_text_200_percent']['layout']['#app-toolbar']['clientWidth'], \
                        report['scaled_text_200_percent']['layout']['#app-toolbar']
                    scaled = report['scaled_text_200_percent']
                    assert scaled['document']['scrollWidth'] <= scaled['viewport']['width'], scaled['document']
                    for selector in ('#menu-toggle', '#models-button', '#workspace-button',
                                     '#library-button', '#skills-button', '#new-chat'):
                        control = scaled['layout'][selector]
                        assert control['x'] >= 0 and control['x'] + control['width'] <= scaled['viewport']['width'], {
                            'selector': selector, 'control': control, 'viewport': scaled['viewport'],
                        }
                    evaluate("document.documentElement.style.fontSize='32px';document.querySelector('#menu-toggle').focus()")
                    report['scaled_toolbar_native_tab_order'] = []
                    for selector in ('#models-button', '#workspace-button', '#library-button',
                                     '#skills-button', '#new-chat'):
                        key('Tab')
                        active = evaluate('document.activeElement.id')
                        report['scaled_toolbar_native_tab_order'].append(active)
                        assert active == selector.removeprefix('#'), {
                            'expected': selector, 'actual': active,
                        }
                    wait("document.querySelector('#main').classList.contains('is-height-constrained')")
                    evaluate("document.querySelector('#input').focus()")
                    time.sleep(.2)  # WebKit scrolls the newly focused control after layout.
                    report['scaled_composer_reachability'] = []
                    for selector in ('#input', '#sources', '#attach', '#retry', '#send'):
                        if selector != '#input':
                            key('Tab')
                        observation = evaluate("""(() => {
                          const e=document.activeElement, r=e.getBoundingClientRect();
                          const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                          return {id:e.id,top:r.top,bottom:r.bottom,left:r.left,right:r.right,
                            toolbarBottom:document.querySelector('#app-toolbar').getBoundingClientRect().bottom,
                            viewportWidth:innerWidth,viewportHeight:innerHeight,
                            reachable:hit===e || e.contains(hit)};
                        })()""")
                        report['scaled_composer_reachability'].append(observation)
                        assert observation['id'] == selector.removeprefix('#'), observation
                        assert (observation['reachable']
                                and observation['top'] >= observation['toolbarBottom']
                                and observation['bottom'] <= observation['viewportHeight']
                                and observation['left'] >= 0
                                and observation['right'] <= observation['viewportWidth']), observation
                    assert all(report[name] for name in (
                        'dialog_tab_stays_inside', 'dialog_shift_tab_stays_inside',
                        'dialog_restores_trigger', 'conversation_arrow_navigation',
                        'list_escape_to_composer', 'editable_form_native_key'))
                    report['keyboard_review_complete'] = True
                    report['passed'] = True
                except BaseException as error:
                    errors.append(error)
                    report['error'] = repr(error)
                    traceback.print_exc()
                finally:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(report, indent=2) + '\n')
                    window.destroy()

            window.events.loaded += loaded
            return window

        webview.create_window = create
        run_gui(config, runtime=runtime)
    if errors:
        raise SystemExit(1)
    print(f'Native H10 measurements recorded: {args.output}')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--atspi':
        accessibility_snapshot(int(sys.argv[2]))
    else:
        main()
