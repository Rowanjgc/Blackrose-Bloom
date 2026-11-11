"""Task-based native keyboard/accessibility observations with scripted local data.

Run under scripts/run_private_atspi.sh. The app uses a temporary database and
workspace plus a deterministic backend; no model, network, paid service, or
production state is used. This captures keyboard/AT-SPI state, not spoken audio.

Each journey's local wall-clock bounds are recorded under ``journeys`` so that
check_gui_orca.py --probe tasks can attribute Orca speech and braille to it.
Under a screen reader, --script-text enters message text through the DOM:
Orca may still be in browse mode when a scripted letter arrives, and then
treats it as a navigation command rather than text. --key-pause sets the
pause after each key; at machine pace the page can re-render before Orca has
handled a focus event, so the event's object is gone and nothing is spoken.
--large-text doubles the root text size and --reduced-motion turns off GTK
animations for the process (WebKit then reports prefers-reduced-motion) before
any journey starts.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_gui_images import make_runtime
from blackrose.gui.app import run_gui
import webview


def seconds_of_day() -> float:
    """Local wall-clock time, the clock Orca's debug log timestamps use."""
    now = datetime.now()
    return now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6


class TaskBackend:
    """Deterministic provider for local file, Stop, and error journeys."""

    def __init__(self):
        self.requests = []
        self.stop_started = threading.Event()
        self.loaded_model = None

    async def load(self, model):
        self.loaded_model = model

    async def models(self, provider=None):
        return [
            {'id': 'test-vision', 'name': 'Test Vision',
             'architecture': {'input_modalities': ['text', 'image']},
             'meta': {'n_ctx': 128000}},
            {'id': 'test-other', 'name': 'Test Other',
             'architecture': {'input_modalities': ['text']},
             'meta': {'n_ctx': 64000}},
        ]

    async def stream(self, model, messages, tools):
        self.requests.append({'model': model, 'messages': messages, 'tools': tools})
        prompt = next((item.get('content') or '' for item in reversed(messages)
                       if item.get('role') == 'user'), '')
        tool_result = any(item.get('role') == 'tool' for item in messages)
        if 'REFERENCE_TASK' in prompt and not tool_result:
            yield {'delta': {'tool_calls': [{'index': 0, 'id': 'native-reference',
                'function': {'name': 'file_read',
                             'arguments': json.dumps({'path': 'notes.md'})}}]},
                'finish_reason': 'tool_calls'}
        elif 'EDIT_TASK' in prompt and not tool_result:
            yield {'delta': {'tool_calls': [{'index': 0, 'id': 'native-edit',
                'function': {'name': 'file_edit',
                             'arguments': json.dumps({'path': 'notes.md',
                                                      'content': 'Revised by native task probe'})}}]},
                'finish_reason': 'tool_calls'}
        elif 'STOP_TASK' in prompt:
            self.stop_started.set()
            yield {'delta': {'content': 'Synthetic streamed response for Stop testing.'}}
            while True:
                await asyncio.sleep(.1)
        elif 'ERROR_TASK' in prompt:
            raise RuntimeError('Synthetic native accessibility probe failure')
        else:
            yield {'delta': {'content': 'Synthetic task completed.'},
                   'finish_reason': 'stop'}

    async def aclose(self):
        return None


def atspi_snapshot(pid: int) -> dict:
    result = subprocess.run([sys.executable, __file__, '--atspi', str(pid)],
                            capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError('AT-SPI snapshot failed: ' + result.stderr[-1500:])
    rows = json.loads(result.stdout)
    return {
        'node_count': len(rows),
        'named_controls': [row for row in rows if row.get('name') and
                           row.get('role') in CONTROL_ROLES][:200],
        'focused': [row for row in rows if row.get('focused')][:12],
    }


# at-spi2-core 2.58 names ATSPI_ROLE_PUSH_BUTTON "button"; older releases say
# "push button". A button with aria-pressed is a "toggle button".
CONTROL_ROLES = ('button', 'push button', 'toggle button', 'entry', 'text', 'dialog',
                 'list box', 'list item', 'status bar', 'combo box')


def image_provider_atspi(snapshot: dict, options: list[dict]) -> list[dict]:
    """Compare each picker button in the DOM with its AT-SPI toggle button."""
    exposed = {row['name']: row for row in snapshot['named_controls']
               if row.get('role') == 'toggle button'}
    result = []
    for option in options:
        row = exposed.get(option['name'])
        states = set(row['states']) if row else set()
        result.append({
            'name': option['name'],
            'exposed': row is not None,
            'pressed': 'pressed' in states,
            'pressed_matches_dom': row is not None and ('pressed' in states) == (option['pressed'] == 'true'),
            'enabled': 'enabled' in states,
            'enabled_matches_dom': row is not None and ('enabled' in states) == (option['aria_disabled'] != 'true'),
            'description': row.get('description') if row else None,
            'description_matches_dom': row is not None and row.get('description') == option['described'],
        })
    return result


def snapshot_process():
    import gi
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi
    Atspi.set_timeout(2000, 3000)
    desktop = Atspi.get_desktop(0)
    roots = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
    apps = [app for app in roots if app and app.get_process_id() == int(sys.argv[2])]
    rows = []

    def visit(item, depth=0):
        if item is None or depth > 30 or len(rows) >= 10000:
            return
        states = item.get_state_set()
        rows.append({'role': item.get_role_name(), 'name': item.get_name(),
                     'description': item.get_description(),
                     'focused': states.contains(Atspi.StateType.FOCUSED),
                     'states': [nick for nick in ('enabled', 'focusable', 'pressed', 'checked')
                                if states.contains(getattr(Atspi.StateType, nick.upper()))],
                     'attributes': item.get_attributes()})
        for index in range(item.get_child_count()):
            visit(item.get_child_at_index(index), depth + 1)

    for app in apps:
        visit(app)
    print(json.dumps(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--atspi', action='store_true')
    parser.add_argument('--large-text', action='store_true', help='double the root text size first')
    parser.add_argument('--reduced-motion', action='store_true',
                        help='turn off GTK animations for this process first')
    parser.add_argument('--script-text', action='store_true',
                        help='enter message text through the DOM; navigation keys stay native')
    parser.add_argument('--key-pause', type=float, default=.12,
                        help='seconds after each key; a screen reader needs a listener\'s pace')
    parser.add_argument('pid', nargs='?', type=int)
    args = parser.parse_args()
    if not .05 <= args.key_pause <= 3:
        parser.error('--key-pause must be between 0.05 and 3 seconds')
    if args.atspi:
        if args.pid is None:
            parser.error('--atspi needs the target process ID')
        snapshot_process()
        return
    if args.output is None:
        parser.error('--output is required')
    if (os.environ.get('BLACKROSE_PRIVATE_ATSPI') != '1'
            or not os.environ.get('AT_SPI_BUS_ADDRESS')
            or not os.environ.get('DBUS_SESSION_BUS_ADDRESS')):
        parser.error('Run through dbus-run-session -- scripts/run_private_atspi.sh')

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    report = {
        'probe': 'bra-max-native-task-review',
        'environment': 'private Xvfb, private D-Bus and AT-SPI',
        'data': 'temporary XDG state, temporary workspace, deterministic scripted backend',
        'real_model_or_network_used': False,
        'spoken_audio_captured': False,
        'human_speech_or_braille_review': False,
        'checks': {},
        'atspi': {},
        'journeys': [],
        'mode': {'large_text': args.large_text, 'reduced_motion': args.reduced_motion,
                 'script_text': args.script_text, 'key_pause': args.key_pause},
        'limitations': [
            'Keyboard keys are delivered through XTest/xdotool; selected focus setup uses DOM focus.',
            'AT-SPI snapshots record exposed roles, names, descriptions, states and focus; they do not establish speech delivery.',
            'No human speech/braille review, rendered paper-texture contrast measurement, or reduced-motion preference run.',
            'Scripted backend outcomes do not certify a deployed model/router or real tool latency.',
        ],
    }
    original_create = webview.create_window
    with tempfile.TemporaryDirectory(prefix='blackrose-a11y-task-') as directory:
        root = Path(directory)
        for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
            (root / name.lower()).mkdir()
            os.environ[name] = str(root / name.lower())
        os.environ['GSETTINGS_BACKEND'] = 'memory'
        os.environ['DCONF_PROFILE'] = str(root / 'dconf-profile')
        (root / 'dconf-profile').write_text('user-db:user\n')
        work = root / 'workspace'
        work.mkdir()
        (work / 'notes.md').write_text('Original synthetic notes.')
        config, runtime = make_runtime(root)
        backend = TaskBackend()
        runtime.controller.backend = backend

        def create(*positional, **kwargs):
            kwargs['hidden'] = False
            window = original_create(*positional, **kwargs)
            checked = False

            def loaded():
                nonlocal checked
                if checked:
                    return
                checked = True
                keyboard_window = None

                def evaluate(script):
                    return window.evaluate_js(script)

                def wait(script, seconds=20):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if evaluate(script):
                            return
                        time.sleep(.035)
                    raise AssertionError('Timed out: ' + script)

                def xid():
                    found = subprocess.check_output([
                        'xdotool', 'search', '--onlyvisible', '--pid',
                        str(os.getpid()), '--name', '^Blackrose Bloom$'],
                        text=True, timeout=5).splitlines()
                    if not found:
                        raise RuntimeError('Native application window is not visible')
                    return found[0]

                def key(value):
                    nonlocal keyboard_window
                    if keyboard_window is None:
                        keyboard_window = xid()
                        subprocess.run(['xdotool', 'windowfocus', '--sync', keyboard_window],
                                       check=True, timeout=5)
                    subprocess.run(['xdotool', 'key', '--clearmodifiers', value],
                                   check=True, timeout=5)
                    time.sleep(args.key_pause)

                def type_text(value):
                    nonlocal keyboard_window
                    if args.script_text:
                        evaluate(f"""(() => {{ const input = document.activeElement;
                          input.setRangeText({json.dumps(value)}, input.selectionStart, input.selectionEnd, 'end');
                          input.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText',
                            data: {json.dumps(value)}}})); }})()""")
                        return
                    if keyboard_window is None:
                        keyboard_window = xid()
                        subprocess.run(['xdotool', 'windowfocus', '--sync', keyboard_window],
                                       check=True, timeout=5)
                    # A screen reader switches the entry to focus mode on its focus event;
                    # letters typed before that are browse-mode commands, not text.
                    time.sleep(.5)
                    subprocess.run(['xdotool', 'type', '--clearmodifiers', '--delay', '25', value],
                                   check=True, timeout=10)

                def active():
                    return evaluate("""(() => {
                      const item = document.activeElement;
                      return {tag:item?.tagName, id:item?.id, className:item?.className,
                        text:(item?.innerText || item?.textContent || '').trim().slice(0,90),
                        model:item?.closest('[data-model-id]')?.dataset.modelId || null,
                        chat:item?.closest('.chat-item')?.dataset.id || null};
                    })()""")

                def journey(name=None):
                    """Close the current journey and, given a name, start the next."""
                    if report['journeys']:
                        # Let late speech land inside the journey that caused it.
                        time.sleep(args.key_pause * 2)
                    now = seconds_of_day()
                    if report['journeys'] and 'end' not in report['journeys'][-1]:
                        report['journeys'][-1]['end'] = now
                    if name:
                        report['journeys'].append({'name': name, 'start': now})

                def on_gtk_thread(function):
                    from gi.repository import GLib
                    done = threading.Event()
                    result = {}

                    def run():
                        try:
                            result['value'] = function()
                        except BaseException as error:  # noqa: BLE001 - re-raised on the probe thread
                            result['error'] = error
                        done.set()
                        return False
                    GLib.idle_add(run)
                    if not done.wait(10):
                        raise TimeoutError('The GTK thread did not run the probe call')
                    if 'error' in result:
                        raise result['error']
                    return result['value']

                def tab_until(predicate, limit=28):
                    trace = []
                    for _ in range(limit):
                        current = active()
                        trace.append(current)
                        if predicate(current):
                            return current
                        key('Tab')
                    raise AssertionError('Could not reach keyboard target; last focus=' +
                                         json.dumps({'active': active(), 'trace': trace}))

                try:
                    wait("document.querySelector('#status-text').textContent==='Ready'")
                    wait("document.querySelector('#scrim').hidden")
                    report['native_versions'] = evaluate("""(() => {
                      return {viewport:{width:innerWidth,height:innerHeight},
                        appRole:document.querySelector('#app').getAttribute('role')};
                    })()""")
                    from gi.repository import Gtk, WebKit2
                    report['native_versions'].update({
                        'gtk': '.'.join(str(value) for value in (
                            Gtk.get_major_version(), Gtk.get_minor_version(),
                            Gtk.get_micro_version())),
                        'webkit': '.'.join(str(value) for value in (
                            WebKit2.get_major_version(), WebKit2.get_minor_version(),
                            WebKit2.get_micro_version())),
                    })
                    # Keep a bounded trace of focus moves for failure diagnosis.
                    evaluate("""(() => { window.__focusTrace = [];
                      const describe = (n) => n && n.nodeType === 1 ? (n.tagName + (n.id ? '#' + n.id : '') +
                        (n.className && typeof n.className === 'string' ? '.' + n.className.split(' ')[0] : '') +
                        (n.getAttribute('aria-label') ? '[' + n.getAttribute('aria-label') + ']' : '')) : String(n);
                      for (const type of ['focusin', 'focusout', 'keydown'])
                        document.addEventListener(type, (e) => { window.__focusTrace.push([Math.round(performance.now()), type,
                          type === 'keydown' ? e.key : describe(e.target), type === 'focusout' ? describe(e.relatedTarget) : '']);
                          if (window.__focusTrace.length > 400) window.__focusTrace.shift(); }, true); })()""")
                    if args.reduced_motion:
                        def animations_off():
                            settings = Gtk.Settings.get_default()
                            settings.set_property('gtk-enable-animations', False)
                            return bool(settings.get_property('gtk-enable-animations'))
                        report['mode']['gtk_enable_animations'] = on_gtk_thread(animations_off)
                        wait("matchMedia('(prefers-reduced-motion: reduce)').matches", seconds=10)
                    if args.large_text:
                        base = evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
                        evaluate(f"document.documentElement.style.fontSize='{base * 2}px'")
                        wait(f"parseFloat(getComputedStyle(document.documentElement).fontSize)==={base * 2}")
                    report['mode'].update(evaluate("""(() => ({
                      root_font_size: getComputedStyle(document.documentElement).fontSize,
                      prefers_reduced_motion: matchMedia('(prefers-reduced-motion: reduce)').matches,
                      viewport: {width: innerWidth, height: innerHeight}}))()"""))

                    # Open Models with the shipped keyboard shortcut. Navigate
                    # to the second model and switch to it with Tab/Return.
                    # Tab alone, from the composer round to it again: every stop is
                    # recorded with whether it is fully on screen once focused.
                    journey('tab_walk')
                    evaluate("document.querySelector('#input').focus()")
                    stops = []
                    for _ in range(60):
                        key('Tab')
                        stop = evaluate("""(() => {
                          const item = document.activeElement;
                          const rect = item.getBoundingClientRect();
                          const inside = rect.width > 0 && rect.height > 0 && rect.left >= 0 && rect.top >= 0
                            && rect.right <= innerWidth && rect.bottom <= innerHeight;
                          const hit = inside ? document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) : null;
                          return {id: item.id || null, tag: item.tagName,
                            name: (item.getAttribute('aria-label') || item.innerText || item.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60),
                            fully_on_screen: inside && Boolean(hit) && (hit === item || item.contains(hit) || hit.contains(item))};
                        })()""")
                        stops.append(stop)
                        if stop['id'] == 'input':
                            break
                    reached = {stop['id'] for stop in stops if stop['id']}
                    expected_stops = ['menu-toggle', 'models-button', 'workspace-button', 'library-button',
                                      'skills-button', 'new-chat', 'input', 'sources', 'attach', 'send']
                    report['checks']['tab_walk'] = {
                        'stops': stops,
                        'returned_to_composer': bool(stops) and stops[-1]['id'] == 'input',
                        'missing_expected': [name for name in expected_stops if name not in reached],
                        'not_fully_on_screen': [stop for stop in stops if not stop['fully_on_screen']],
                    }

                    journey('model_picker')
                    evaluate("document.querySelector('#input').focus()")
                    key('ctrl+m')
                    wait("!document.querySelector('#scrim').hidden && document.querySelector('#dialog-title').textContent==='Models'")
                    report['checks']['model_picker_opened_by_shortcut'] = True
                    report['atspi']['model_picker'] = atspi_snapshot(os.getpid())
                    browse = tab_until(lambda item: item and
                                       'model-browse' in item.get('className', ''))
                    key('Return')
                    wait("document.querySelector('[data-model-id=\"test-other\"] .model-choice')")
                    choice = tab_until(lambda item: item and item.get('model') == 'test-other'
                                       and 'model-choice' in item.get('className', ''))
                    key('Return')
                    wait("document.querySelector('#model-value').textContent.includes('Test Other')")
                    report['checks']['model_picker_keyboard_selection'] = {
                        'browse_focus': browse, 'choice_focus': choice,
                        'selected_model': evaluate("document.querySelector('#model-value').textContent.trim()"),
                    }
                    report['atspi']['model_picker_selected'] = atspi_snapshot(os.getpid())
                    key('Escape')
                    wait("document.querySelector('#scrim').hidden && document.activeElement.id==='models-button'")
                    report['checks']['model_picker_escape_restores_trigger'] = True

                    # Select a workspace file with real keyboard input and
                    # complete the reference suggestion with ArrowDown/Enter.
                    journey('workspace_reference')
                    evaluate("document.querySelector('#input').focus()")
                    type_text('@notes')
                    wait("!document.querySelector('#commands').hidden && document.querySelector('#commands [role=option]')?.textContent.includes('notes.md')")
                    report['atspi']['workspace_reference_suggestion'] = atspi_snapshot(os.getpid())
                    key('Down')
                    selected_option = evaluate("document.querySelector('#commands [role=option][aria-selected=true]')?.textContent.trim()")
                    key('Return')
                    wait("document.querySelector('#input').value.includes('notes.md')")
                    evaluate("document.querySelector('#input').value += ' REFERENCE_TASK'; document.querySelector('#input').dispatchEvent(new Event('input',{bubbles:true}))")
                    key('ctrl+Return')
                    wait("document.querySelector('#reply-announcement').textContent.includes('Reply complete')")
                    reference_requests = [request for request in backend.requests
                                          if any('REFERENCE_TASK' in (item.get('content') or '')
                                                 for item in request['messages']
                                                 if item.get('role') == 'user')]
                    report['checks']['workspace_reference_keyboard_send'] = {
                        'suggestion_selected': 'notes.md' in (selected_option or ''),
                        'completed_reference': bool(evaluate("document.querySelector('.msg.user .bubble')?.textContent.includes('notes.md')")),
                        'backend_requests_for_reference_turn': len(reference_requests),
                        'file_read_tool_call_seen': any(
                            item.get('role') == 'assistant' and item.get('tool_calls')
                            and any(call.get('function', {}).get('name') == 'file_read'
                                    for call in item['tool_calls'])
                            for request in reference_requests for item in request['messages']),
                        'file_contents_in_backend_context': any(
                            any(item.get('role') == 'tool'
                                and 'Original synthetic notes.' in (item.get('content') or '')
                                for item in request['messages'])
                            for request in reference_requests),
                        'reply_state': evaluate("document.querySelector('#reply-announcement').textContent.trim()"),
                    }
                    report['atspi']['reference_reply'] = atspi_snapshot(os.getpid())

                    # An edit proposal remains unapplied until the user reviews
                    # the diff. Focus is placed on its control; XTest activates it.
                    journey('edit_review')
                    evaluate("document.querySelector('#input').focus()")
                    type_text('EDIT_TASK')
                    key('ctrl+Return')
                    wait("document.querySelector('[data-approval] button') && document.querySelector('#send').textContent==='Send'")
                    review_button = evaluate("document.querySelector('[data-approval] button').textContent")
                    evaluate("document.querySelector('[data-approval] button').focus()")
                    key('Return')
                    wait("!document.querySelector('#scrim').hidden && document.querySelector('#dialog-title').textContent.startsWith('Replace ')")
                    report['atspi']['edit_review_dialog'] = atspi_snapshot(os.getpid())
                    dialog_focus_before = active()
                    key('Tab')
                    dialog_focus_approve = active()
                    key('Return')
                    wait("!document.querySelector('[data-approval]') && document.querySelector('#send').textContent==='Send'")
                    report['checks']['edit_review_keyboard'] = {
                        'review_control': review_button,
                        'dialog_initial_focus': dialog_focus_before,
                        'tab_focus': dialog_focus_approve,
                        'approved_with_enter': True,
                        'file_changed_only_after_approval':
                            (work / 'notes.md').read_text() == 'Revised by native task probe',
                    }

                    # Stop a scripted stream only after actual text has painted;
                    # the shortcut/button routes to Session.stop().
                    journey('send_stop')
                    evaluate("document.querySelector('#input').focus()")
                    type_text('STOP_TASK')
                    key('ctrl+Return')
                    wait("document.querySelector('#send').textContent==='Stop' && document.querySelector('.msg.assistant:last-of-type .body')?.textContent.trim().length>0")
                    report['atspi']['streaming_stop_state'] = atspi_snapshot(os.getpid())
                    key('ctrl+c')
                    wait("document.querySelector('#send').textContent==='Send' && document.querySelector('#reply-announcement').textContent.includes('Reply stopped')")
                    report['checks']['send_stop_outcome'] = {
                        'stop_shortcut': 'Ctrl+C',
                        'send_control_restored': evaluate("document.querySelector('#send').textContent==='Send'"),
                        'announcement_text': evaluate("document.querySelector('#reply-announcement').textContent.trim()"),
                        'announcement_role': evaluate("document.querySelector('#reply-announcement').getAttribute('role')"),
                        'announcement_live': evaluate("document.querySelector('#reply-announcement').getAttribute('aria-live')"),
                    }
                    report['atspi']['stopped_reply'] = atspi_snapshot(os.getpid())

                    journey('send_error')
                    evaluate("document.querySelector('#input').focus()")
                    type_text('ERROR_TASK')
                    key('ctrl+Return')
                    wait("document.querySelector('#status-text').textContent==='Reply failed' && document.querySelector('#reply-announcement').textContent.includes('Reply failed')")
                    report['checks']['send_error_outcome'] = {
                        'send_shortcut': 'Ctrl+Enter',
                        'status_text': evaluate("document.querySelector('#status-text').textContent.trim()"),
                        'announcement_text': evaluate("document.querySelector('#reply-announcement').textContent.trim()"),
                        'announcement_role': evaluate("document.querySelector('#reply-announcement').getAttribute('role')"),
                        'announcement_live': evaluate("document.querySelector('#reply-announcement').getAttribute('aria-live')"),
                        'error_note_present': bool(evaluate("document.querySelector('.reply-note[data-tone=error]')")),
                    }
                    report['atspi']['failed_reply'] = atspi_snapshot(os.getpid())

                    # Toggle the conversation drawer with Ctrl+Backslash, then
                    # move and open a row using only native key events.
                    journey('conversation_drawer')
                    key('ctrl+n')
                    wait("document.querySelectorAll('.chat-item').length>=2")
                    # At large text the drawer may start as a closed overlay, so the
                    # shortcut is checked relative to where it starts.
                    expanded = "document.querySelector('#menu-toggle').getAttribute('aria-expanded')"
                    drawer_initial = evaluate(expanded)
                    key('ctrl+backslash')
                    wait(f"{expanded}!=={json.dumps(drawer_initial)}")
                    drawer_toggled = evaluate(expanded)
                    key('ctrl+backslash')
                    wait(f"{expanded}==={json.dumps(drawer_initial)}")
                    if drawer_initial != 'true':
                        key('ctrl+backslash')
                        wait(f"{expanded}==='true'")
                    key('ctrl+l')
                    wait("document.activeElement.classList.contains('chat-item')")
                    key('Home')
                    first_chat = active().get('chat')
                    key('Down')
                    second_chat = active().get('chat')
                    key('Return')
                    wait("document.querySelector('.chat-item[aria-current=true]')?.dataset.id===" +
                         json.dumps(second_chat))
                    report['checks']['conversation_drawer_keyboard'] = {
                        'initially_expanded': drawer_initial,
                        'shortcut_toggled': drawer_toggled != drawer_initial,
                        'shortcut_restored': True,
                        'expanded_for_list': evaluate(expanded),
                        'home_row': first_chat, 'arrow_down_row': second_chat,
                        'row_changed': first_chat != second_chat,
                        'opened_selected_row_with_enter': True,
                        'selected_row_accessible_name': evaluate("document.querySelector('.chat-item[aria-current=true]')?.getAttribute('aria-label')"),
                    }
                    report['atspi']['conversation_drawer'] = atspi_snapshot(os.getpid())

                    # Image generation picker in the Models dialog: unavailable
                    # providers stay in the Tab order and announce their reason,
                    # then an available provider is chosen by keyboard. The probe
                    # runtime has no local service or OpenRouter key, so only
                    # Automatic and Off are available here.
                    journey('image_provider_picker')
                    evaluate("document.querySelector('#input').focus()")
                    key('ctrl+m')
                    wait("!document.querySelector('#scrim').hidden && document.querySelectorAll('.image-provider-option').length===4")
                    # Image generation is the Models dialog's second tab: Tab round to the
                    # tab list, then an arrow selects Images.
                    tab_until(lambda item: item and item.get('id') == 'model-tab-chat', limit=40)
                    key('Right')
                    wait("document.querySelector('#model-tab-images').getAttribute('aria-selected')==='true' && !document.querySelector('#model-panel-images').hidden")
                    report['atspi']['image_provider_picker'] = atspi_snapshot(os.getpid())
                    image_options_script = """(() => [...document.querySelectorAll('.image-provider-option')].map(button => ({
                      id: button.dataset.optionId, name: button.textContent.trim(), disabled: button.disabled,
                      aria_disabled: button.getAttribute('aria-disabled'), pressed: button.getAttribute('aria-pressed'),
                      described: document.getElementById(button.getAttribute('aria-describedby'))?.textContent.trim()})))()"""
                    report['checks']['image_provider_options'] = evaluate(image_options_script)
                    report['checks']['image_provider_atspi'] = image_provider_atspi(
                        report['atspi']['image_provider_picker'], report['checks']['image_provider_options'])
                    local_focus = tab_until(lambda item: item and 'image-provider-option' in item.get('className', '')
                                            and item.get('text', '').startswith('Local'))
                    local_reason = evaluate("document.querySelector('#image-provider-detail-local').textContent")
                    key('Return')
                    wait("document.querySelector('.image-provider-message').textContent.trim().length > 0")
                    report['checks']['image_provider_unavailable_keyboard'] = {
                        'focus': local_focus,
                        'aria_disabled': evaluate("document.querySelector('.image-provider-option[data-option-id=\"local\"]').getAttribute('aria-disabled')"),
                        'status_text': evaluate("document.querySelector('.image-provider-message').textContent.trim()"),
                        'reason_in_description': evaluate("document.querySelector('.image-provider-message').textContent.trim()") in local_reason,
                        'pressed_after_return': evaluate("document.querySelector('.image-provider-option[data-option-id=\"local\"]').getAttribute('aria-pressed')"),
                        'focus_stayed': 'Local' in (active() or {}).get('text', ''),
                    }
                    off_focus = tab_until(lambda item: item and 'image-provider-option' in item.get('className', '')
                                          and item.get('text', '') == 'Off')
                    key('Return')
                    wait("document.querySelector('.image-provider-option[data-option-id=\"off\"]').getAttribute('aria-pressed')==='true'")
                    wait("document.querySelector('.image-provider-message').textContent.includes('Image generation')")
                    report['checks']['image_provider_keyboard_choice'] = {
                        'focus_before_choice': off_focus,
                        'pressed_after_return': evaluate("document.querySelector('.image-provider-option[data-option-id=\"off\"]').getAttribute('aria-pressed')"),
                        'status_text': evaluate("document.querySelector('.image-provider-message').textContent.trim()"),
                        'status_role': evaluate("document.querySelector('.image-provider-message').getAttribute('role')"),
                        'focus_after_choice': active(),
                        'saved_choice': evaluate("document.querySelector('.image-provider-option[aria-pressed=true]')?.dataset.optionId"),
                    }
                    report['atspi']['image_provider_selected'] = atspi_snapshot(os.getpid())
                    report['checks']['image_provider_atspi_selected'] = image_provider_atspi(
                        report['atspi']['image_provider_selected'], evaluate(image_options_script))
                    key('Escape')
                    wait("document.querySelector('#scrim').hidden && document.activeElement.id==='models-button'")
                    report['checks']['image_provider_escape_restores_trigger'] = True

                    # Sources: Ctrl+comma opens the panel on its first switch,
                    # an arrow changes citation rigor, Escape returns to the trigger.
                    journey('sources_panel')
                    evaluate("document.querySelector('#input').focus()")
                    key('ctrl+comma')
                    wait("!document.querySelector('#sources-panel').hidden && document.activeElement.id==='knowledge-toggle'")
                    report['atspi']['sources_panel'] = atspi_snapshot(os.getpid())
                    rigor_focus = tab_until(lambda item: item and item.get('text') == 'Off', limit=12)
                    key('Right')
                    wait("document.querySelector('#citation-rigor [data-mode=advisory]').getAttribute('aria-checked')==='true'")
                    rigor_detail = evaluate("document.querySelector('#rigor-detail').textContent.trim()")
                    key('Left')
                    wait("document.querySelector('#citation-rigor [data-mode=off]').getAttribute('aria-checked')==='true'")
                    key('Escape')
                    wait("document.querySelector('#sources-panel').hidden && document.activeElement.id==='sources'")
                    report['checks']['sources_panel_keyboard'] = {
                        'opened_by_shortcut_on_first_switch': True,
                        'rigor_focus': rigor_focus,
                        'advisory_detail': rigor_detail,
                        'restored_off': True,
                        'escape_restores_trigger': True,
                        'trigger_name': evaluate("document.querySelector('#sources').textContent.trim().replace(/\\s+/g,' ')"),
                    }

                    # Keys and commands: the reference sheet opens from its
                    # control, keeps Tab inside, and Escape returns to it.
                    journey('keys_sheet')
                    # Its control is in the drawer; a closed overlay drawer is inert.
                    if evaluate("document.querySelector('#help-button').closest('[inert]') !== null"):
                        key('ctrl+backslash')
                        wait("document.querySelector('#help-button').closest('[inert]') === null")
                    evaluate("document.querySelector('#help-button').focus()")
                    wait("document.activeElement.id==='help-button'", seconds=5)
                    key('Return')
                    wait("!document.querySelector('#scrim').hidden && document.querySelector('#dialog-title').textContent==='Keys and commands'")
                    report['atspi']['keys_sheet'] = atspi_snapshot(os.getpid())
                    sheet_focus = []
                    for _ in range(3):
                        key('Tab')
                        sheet_focus.append(evaluate("document.querySelector('#dialog').contains(document.activeElement)"))
                    key('Escape')
                    wait("document.querySelector('#scrim').hidden && document.activeElement.id==='help-button'")
                    report['checks']['keys_sheet_keyboard'] = {
                        'opened_with_return': True,
                        'tab_stays_in_sheet': all(sheet_focus),
                        'escape_restores_trigger': True,
                        'row_count': evaluate("document.querySelectorAll('#dialog-detail tr, #dialog-detail [role=row], #dialog-detail dt').length"),
                    }
                    journey()

                    required = (
                        report['checks']['tab_walk']['returned_to_composer'],
                        not report['checks']['tab_walk']['missing_expected'],
                        not report['checks']['tab_walk']['not_fully_on_screen'],
                        report['checks']['model_picker_opened_by_shortcut'],
                        report['checks']['model_picker_keyboard_selection']['selected_model'] == 'Test Other',
                        report['checks']['model_picker_escape_restores_trigger'],
                        report['checks']['workspace_reference_keyboard_send']['suggestion_selected'],
                        report['checks']['workspace_reference_keyboard_send']['file_contents_in_backend_context'],
                        report['checks']['edit_review_keyboard']['approved_with_enter'],
                        report['checks']['edit_review_keyboard']['file_changed_only_after_approval'],
                        report['checks']['send_stop_outcome']['send_control_restored'],
                        'Reply stopped' in report['checks']['send_stop_outcome']['announcement_text'],
                        report['checks']['send_error_outcome']['status_text'] == 'Reply failed',
                        report['checks']['send_error_outcome']['error_note_present'],
                        report['checks']['conversation_drawer_keyboard']['shortcut_toggled'],
                        report['checks']['conversation_drawer_keyboard']['row_changed'],
                        report['checks']['conversation_drawer_keyboard']['opened_selected_row_with_enter'],
                        report['checks']['image_provider_unavailable_keyboard']['aria_disabled'] == 'true',
                        report['checks']['image_provider_unavailable_keyboard']['reason_in_description'],
                        report['checks']['image_provider_unavailable_keyboard']['pressed_after_return'] == 'false',
                        report['checks']['image_provider_unavailable_keyboard']['focus_stayed'],
                        report['checks']['image_provider_keyboard_choice']['pressed_after_return'] == 'true',
                        report['checks']['image_provider_keyboard_choice']['saved_choice'] == 'off',
                        report['checks']['image_provider_escape_restores_trigger'],
                        *(row['exposed'] and row['pressed_matches_dom'] and row['enabled_matches_dom']
                          and row['description_matches_dom']
                          for name in ('image_provider_atspi', 'image_provider_atspi_selected')
                          for row in report['checks'][name]),
                        [row['name'] for row in report['checks']['image_provider_atspi_selected']
                         if row['pressed']] == ['Off'],
                        bool(report['checks']['sources_panel_keyboard']['advisory_detail']),
                        report['checks']['keys_sheet_keyboard']['tab_stays_in_sheet'],
                        not args.large_text or report['mode']['root_font_size'] == '32px',
                        not args.reduced_motion or report['mode']['prefers_reduced_motion'],
                    )
                    report['passed'] = all(required)
                    report['checks']['backend_request_count'] = len(backend.requests)
                except BaseException as error:
                    journey()
                    failures.append(error)
                    report['error'] = f'{type(error).__name__}: {error}'
                    report['passed'] = False
                    for name in ('send_stop_outcome', 'send_error_outcome',
                                 'conversation_drawer_keyboard'):
                        report['checks'].setdefault(name, {
                            'tested': False,
                            'reason': 'The scripted journey stopped before this check ran.',
                        })
                    report['failure_snapshot'] = evaluate("""(() => ({
                      status:document.querySelector('#status-text')?.textContent,
                      send:document.querySelector('#send')?.textContent,
                      input:document.querySelector('#input')?.value,
                      announcement:document.querySelector('#reply-announcement')?.textContent,
                      dialogOpen:!document.querySelector('#scrim')?.hidden,
                      dialogTitle:document.querySelector('#dialog-title')?.textContent,
                      focus:{tag:document.activeElement?.tagName, id:document.activeElement?.id,
                        className:String(document.activeElement?.className || ''),
                        label:document.activeElement?.getAttribute('aria-label')},
                      documentHasFocus:document.hasFocus(),
                      focusTrace:(window.__focusTrace || []).slice(-60),
                      lastAssistant:document.querySelector('.msg.assistant:last-of-type .body')?.textContent
                    }))()""")
                    report['backend_calls'] = [{
                        'model': request['model'],
                        'last_user': next((item.get('content') for item in reversed(request['messages'])
                                           if item.get('role') == 'user'), None),
                        'tool_names': [call.get('function', {}).get('name')
                                       for item in request['messages']
                                       if item.get('role') == 'assistant'
                                       for call in (item.get('tool_calls') or [])],
                    } for request in backend.requests]
                    traceback.print_exc()
                finally:
                    output.write_text(json.dumps(report, indent=2) + '\n')
                    window.destroy()

            window.events.loaded += loaded
            return window

        try:
            webview.create_window = create
            run_gui(config, runtime=runtime, workspace=str(work))
        except BaseException as error:
            failures.append(error)
            report['error'] = f'{type(error).__name__}: {error}'
            output.write_text(json.dumps(report, indent=2) + '\n')
        finally:
            webview.create_window = original_create
    if failures or not report.get('passed'):
        raise SystemExit(1)
    print(f'Native accessibility task observations: {output}', flush=True)


if __name__ == '__main__':
    main()
