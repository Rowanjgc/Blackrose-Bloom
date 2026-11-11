"""Capture Orca speech dispatch while a scratch native accessibility probe runs.

Run with the private Xvfb/D-Bus/AT-SPI wrapper. Speech Dispatcher uses its own
Unix socket, config and espeak-ng process with ALSA's null sink. No audio is
played through the desktop. Dispatch logs are evidence of screen-reader output,
not a substitute for human speech or braille review.

With --braille, a private BRLTTY runs its Virtual driver and this script plays
the 40-cell display it drives, recording each line Orca puts on it. BrlAPI's
socket directory is fixed at /var/lib/BrlAPI, so BRLTTY and Orca run under
bubblewrap with a scratch directory mounted there; nothing on the host changes
and no network port is opened. A recorded display is not a human braille review.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]


def timed_dispatches(log: str) -> list[tuple[float | None, str]]:
    """Each plain-text dispatch, with Orca's local time-of-day stamp when it has one."""
    records = re.split(r'(?m)(?=^(?:\d{2}:\d{2}:\d{2}\.\d+ - |SPEECH DISPATCHER: Speaking ))', log)
    dispatched = []
    for record in records:
        match = re.fullmatch(
            r"(?:(\d{2}):(\d{2}):(\d{2}\.\d+) - )?SPEECH DISPATCHER: Speaking '(.*?)' as (?:string|char)\s*",
            record, re.DOTALL)
        if match:
            when = (int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])) if match[1] else None
            dispatched.append((when, match[4]))
    return dispatched


def speech_observations(log: str) -> dict:
    """Keep dispatch evidence distinct from Orca's output-only debug messages."""
    dispatched = [text for _, text in timed_dispatches(log)]
    return {
        'dispatch_count': len(dispatched),
        'dispatched_utterances': [item[:4000] for item in dispatched[:250]],
        'utterances_truncated': len(dispatched) > 250,
        'reply_complete_dispatched': any('reply complete' in item.casefold()
                                         for item in dispatched),
        'help_dispatched': any('help' in item.casefold() for item in dispatched),
        # The page has no combo box; one in speech is a misexposed control (bra-yp9).
        'combo_box_dispatched': [item[:200] for item in dispatched if 'combo box' in item.casefold()][:30],
        'speech_backend_errors': [line[:1000] for line in log.splitlines()
                                  if 'SPEECH DISPATCHER' in line
                                  and ('error' in line.casefold() or 'exception' in line.casefold())][:30],
    }


# What a listener should hear and feel during each task journey; any match counts.
JOURNEY_EXPECTATIONS = {
    'model_picker': ('Models', 'Test Other'),
    'workspace_reference': ('notes.md', 'Reply complete'),
    'edit_review': ('Replace', 'Approve'),
    'send_stop': ('Reply stopped',),
    'send_error': ('Reply failed',),
    'conversation_drawer': ('Conversations', 'New conversation'),
    'image_provider_picker': ('Images', 'Off'),
    'sources_panel': ('Local knowledge', 'Advisory'),
    'keys_sheet': ('Keys and commands',),
}


def journey_observations(journeys: list[dict], spoken: list[tuple[float | None, str]],
                         brailled: list[tuple[float, str]]) -> list[dict]:
    """Attribute speech and braille to the probe journey running when it was produced."""
    rows = []
    for journey in journeys:
        start, end = journey['start'], journey.get('end', float('inf'))
        speech = [text for when, text in spoken if when is not None and start <= when < end]
        braille = []
        for when, text in brailled:
            if start <= when < end and (not braille or braille[-1] != text):
                braille.append(text)
        expected = JOURNEY_EXPECTATIONS.get(journey['name'], ())
        rows.append({
            'name': journey['name'],
            'spoken': [text[:600] for text in speech[:80]],
            'brailled': [text[:400] for text in braille[:80]],
            'expected': list(expected),
            'expected_spoken': {phrase: any(phrase.casefold() in text.casefold() for text in speech)
                                for phrase in expected},
            'expected_brailled': {phrase: any(phrase.casefold() in text.casefold() for text in braille)
                                  for phrase in expected},
        })
    return rows


def stop_owned(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    # Each child has its own process group and this is its only reaping parent.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def protocol_observations(log: str) -> dict:
    """Read bounded single-line SSML messages sent to the private speech daemon."""
    utterances = []
    errors = 0
    for match in re.finditer(r'(?m)^.*DATA:\|(<speak>[^\r\n]*</speak>)\r?$', log):
        if len(match[1]) > 32_000:
            errors += 1
            continue
        try:
            utterances.append(''.join(ElementTree.fromstring(match[1]).itertext()))
        except ElementTree.ParseError:
            errors += 1
    return {
        'ssip_messages_queued': log.count('225 OK MESSAGE QUEUED'),
        'ssip_end_notifications': log.count('702 END'),
        'ssip_single_line_utterances': [value[:4000] for value in utterances[:250]],
        'ssip_parse_errors': errors,
        'reply_complete_received_by_speechd': any('reply complete' in value.casefold()
                                                  for value in utterances),
        'existing_live_control_received_by_speechd': any('Existing status control marker.' in value
                                                         for value in utterances),
        'visible_live_control_received_by_speechd': any('Visible status control marker.' in value
                                                        for value in utterances),
    }


def wait_for_socket(path: Path, process: subprocess.Popen, seconds: float = 8,
                    owner: str = 'Speech Dispatcher') -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'Private {owner} exited before creating its socket')
        if path.is_socket():
            return
        time.sleep(.05)
    raise TimeoutError(f'Private {owner} socket did not become ready')


BRAILLE_CELLS = 40
MAX_BRAILLE_LINES = 20_000


def private_brlapi_mount(directory: Path) -> list[str]:
    """A bubblewrap prefix that shows ``directory`` as /var/lib/BrlAPI and leaves the rest of the system as is."""
    command = ['bwrap', '--die-with-parent', '--dev-bind', '/', '/', '--tmpfs', '/var/lib']
    for entry in sorted(Path('/var/lib').iterdir()):
        if entry.name != 'BrlAPI':
            command += ['--dev-bind', str(entry), str(entry)]
    return command + ['--bind', str(directory), '/var/lib/BrlAPI']


class VirtualBrailleDisplay(threading.Thread):
    """Play the display BRLTTY's Virtual driver drives, and keep what it is shown."""

    def __init__(self, path: Path) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.lines: list[str] = []
        self.times: list[float] = []
        self.connected = threading.Event()
        self.closing = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        import socket
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect(str(self.path))
                connection.sendall(f'cells {BRAILLE_CELLS}\n'.encode())
                connection.settimeout(.2)
                self.connected.set()
                pending = b''
                while not self.closing.is_set():
                    try:
                        data = connection.recv(65536)
                    except TimeoutError:
                        continue
                    if not data:
                        break
                    *complete, pending = (pending + data).split(b'\n')
                    room = MAX_BRAILLE_LINES - len(self.lines)
                    now = datetime.now()
                    when = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
                    for line in complete[:max(room, 0)]:
                        self.lines.append(line.decode('utf-8', 'replace'))
                        self.times.append(when)
        except OSError as error:
            self.error = f'{type(error).__name__}: {error}'
        finally:
            self.connected.set()


def braille_observations(lines: list[str]) -> dict:
    """What the display showed, in order, without repeating an unchanged line."""
    shown = []
    for line in lines:
        match = re.fullmatch(r'Visual "(.*)"', line)
        if match:
            text = match[1].rstrip()
            if not shown or shown[-1] != text:
                shown.append(text)
    lowered = [text.casefold() for text in shown]
    return {
        'braille_cells': BRAILLE_CELLS,
        'braille_protocol_lines': len(lines),
        'braille_protocol_lines_truncated': len(lines) >= MAX_BRAILLE_LINES,
        'braille_dot_frames': sum(line.startswith('Braille "') for line in lines),
        'brailled_lines': [text[:400] for text in shown[:400]],
        'brailled_lines_truncated': len(shown) > 400,
        'reply_complete_brailled': any('reply complete' in text for text in lowered),
        # Orca brailles an edit field as its label then the $l field marker.
        'message_entry_brailled': any(text.startswith('message') and '$l' in text for text in lowered),
        'send_button_brailled': any(text.startswith('send') and 'button' in text for text in lowered),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--braille', action='store_true',
                        help='also record Orca braille on a private BRLTTY Virtual display')
    parser.add_argument('--probe', choices=('housekeeping', 'tasks'), default='housekeeping',
                        help='the H10 keyboard probe, or the task review with per-journey speech')
    parser.add_argument('--large-text', action='store_true', help='tasks probe: double the root text size')
    parser.add_argument('--reduced-motion', action='store_true', help='tasks probe: turn off GTK animations')
    args = parser.parse_args()
    if not 30 <= args.timeout <= 600:
        parser.error('--timeout must be between 30 and 600 seconds')
    if (args.large_text or args.reduced_motion) and args.probe != 'tasks':
        parser.error('--large-text and --reduced-motion apply to --probe tasks')
    if (os.environ.get('BLACKROSE_PRIVATE_ATSPI') != '1'
            or not os.environ.get('AT_SPI_BUS_ADDRESS')
            or not os.environ.get('DBUS_SESSION_BUS_ADDRESS')):
        parser.error('Use dbus-run-session -- bash scripts/run_private_atspi.sh ...')
    for executable in ('orca', 'speech-dispatcher') + (('brltty', 'bwrap') if args.braille else ()):
        if not shutil.which(executable):
            parser.error(f'{executable} is unavailable; no installation was attempted')
    module_config = Path('/etc/speech-dispatcher/modules/espeak-ng.conf')
    if not module_config.is_file():
        parser.error('The installed espeak-ng module config is unavailable')

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    logs = Path(tempfile.mkdtemp(prefix=output.stem + '-', dir=output.parent))
    report = {
        'recorded_at': datetime.now(UTC).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'logs_directory': str(logs),
        'audio_output': 'private espeak-ng Speech Dispatcher module; ALSA null sink',
        'human_listening_performed': False,
        'limitations': [
            'Orca dispatch records do not prove intelligibility, timing or complete spoken delivery.',
            'No human speech/braille usability review is performed.',
            'Braille is recorded from a software display; no physical braille display is used.'
            if args.braille else 'Braille output is not recorded (run with --braille).',
            'The scratch probe covers selected native keyboard journeys, not every workflow.',
            *(['Task probe message text is entered through the DOM; navigation and activation keys are '
               'native XTest, paced 0.6 s apart.']
              if args.probe == 'tasks' else []),
        ],
    }
    speechd = orca = brltty = display = None
    streams = []
    try:
        with tempfile.TemporaryDirectory(prefix='blackrose-orca-') as directory, ExitStack() as children:
            scratch = Path(directory)
            environment = dict(os.environ)
            for variable in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
                             'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR'):
                location = scratch / variable.lower()
                location.mkdir(mode=0o700)
                environment[variable] = str(location)
            environment['DCONF_PROFILE'] = str(scratch / 'dconf-profile')
            Path(environment['DCONF_PROFILE']).write_text('user-db:user\n')
            environment['GSETTINGS_BACKEND'] = 'memory'
            socket_path = scratch / 'speech.sock'
            environment['SPEECHD_ADDRESS'] = 'unix_socket:' + str(socket_path)
            config = scratch / 'speech-config'
            config.mkdir()
            (config / 'speechd.conf').write_text(
                'LogLevel 5\n'
                f'LogDir "{logs}"\n'
                f'CustomLogFile "protocol" "{logs / "ssip.log"}"\n'
                'CommunicationMethod "unix_socket"\n'
                f'SocketPath "{socket_path}"\n'
                'Timeout 0\n'
                f'AddModule "espeak-ng" "sd_espeak-ng" "{module_config}"\n'
                'DefaultModule "espeak-ng"\n'
                'AudioOutputMethod "alsa"\n'
                'AudioALSADevice "null"\n')
            speech_log = (logs / 'speech-process.log').open('w')
            streams.append(speech_log)
            speechd = subprocess.Popen([
                'speech-dispatcher', '--run-single', '--config-dir', str(config),
                '--socket-path', str(socket_path), '--pid-file', str(scratch / 'speech.pid'),
            ], env=environment, stdout=speech_log, stderr=subprocess.STDOUT,
                start_new_session=True)
            children.callback(stop_owned, speechd)
            wait_for_socket(socket_path, speechd)
            wrap: list[str] = []
            if args.braille:
                api = scratch / 'brlapi'
                api.mkdir(mode=0o700)
                wrap = private_brlapi_mount(api)
                virtual = scratch / 'braille.sock'
                brltty_log = (logs / 'brltty.log').open('w')
                streams.append(brltty_log)
                brltty = subprocess.Popen(wrap + [
                    'brltty', '--no-daemon', '--standard-error', '--quiet',
                    '--braille-driver=vr', f'--braille-device=server:{virtual}',
                    '--screen-driver=no', '--speech-driver=no', '--api-parameters=auth=none,host=:0',
                    '--configuration-file=/dev/null', f'--writable-directory={scratch}',
                    f'--updatable-directory={scratch}', f'--pid-file={scratch / "brltty.pid"}',
                    '--log-level=warning',
                ], env=environment, stdout=brltty_log, stderr=subprocess.STDOUT, start_new_session=True)
                children.callback(stop_owned, brltty)
                wait_for_socket(virtual, brltty, owner='BRLTTY')
                display = VirtualBrailleDisplay(virtual)
                display.start()
                if not display.connected.wait(8) or display.error:
                    raise RuntimeError(f'The virtual braille display did not connect: {display.error}')
                wait_for_socket(api / '0', brltty, owner='BrlAPI')
                environment['BRLAPI_HOST'] = ':0'
            orca_log = logs / 'orca-debug.log'
            (scratch / 'orca-prefs').mkdir()
            console = (logs / 'orca-process.log').open('w')
            streams.append(console)
            orca = subprocess.Popen(wrap + [
                'orca', '--user-prefs', str(scratch / 'orca-prefs'),
                '--enable', 'speech', '--enable' if args.braille else '--disable', 'braille',
                '--disable', 'sound',
                '--debug-file', str(orca_log),
            ], env=environment, stdout=console, stderr=subprocess.STDOUT,
                start_new_session=True)
            children.callback(stop_owned, orca)
            time.sleep(1)
            if orca.poll() is not None:
                raise RuntimeError('Private Orca exited before the accessibility probe')
            probe_output = logs / 'native.json'
            if args.probe == 'tasks':
                # Orca can still be in browse mode when a scripted letter arrives, and
                # would take it as a navigation command; message text goes in through
                # the DOM, while every navigation and activation key stays native.
                command = ['scripts/check_gui_accessibility_tasks.py', '--output', str(probe_output),
                           '--script-text', '--key-pause', '0.6']
                command += ['--large-text'] * args.large_text + ['--reduced-motion'] * args.reduced_motion
            else:
                command = ['scripts/check_gui_housekeeping.py', '--output', str(probe_output),
                           '--accessibility-pause', '1', '--speech-controls']
            report['probe'] = {'name': args.probe, 'large_text': args.large_text,
                               'reduced_motion': args.reduced_motion}
            with (logs / 'native-process.log').open('w') as stream:
                probe = subprocess.Popen([sys.executable, *command], cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True)
                children.callback(stop_owned, probe)
                report['native_returncode'] = probe.wait(timeout=args.timeout)
            native = {}
            if probe_output.exists():
                native = json.loads(probe_output.read_text())
                report['native_passed'] = native.get('passed', False)
                report['native_versions'] = native.get('native_versions')
                if 'mode' in native:
                    report['probe']['observed_mode'] = native['mode']
            time.sleep(.5)
            stop_owned(orca)
            orca = None
            report.update(speech_observations(orca_log.read_text(errors='replace')
                                             if orca_log.exists() else ''))
            stop_owned(speechd)
            speechd = None
            protocol_log = logs / 'ssip.log'
            protocol = protocol_log.read_text(errors='replace') if protocol_log.exists() else ''
            report.update(protocol_observations(protocol))
            if display is not None:
                time.sleep(.3)
                display.closing.set()
                display.join(2)
                stop_owned(brltty)
                brltty = None
                report.update(braille_observations(display.lines))
            if native.get('journeys'):
                brailled = []
                if display is not None:
                    for when, line in zip(display.times, display.lines, strict=True):
                        if match := re.fullmatch(r'Visual "(.*)"', line):
                            brailled.append((when, match[1].rstrip()))
                report['journeys'] = journey_observations(
                    native['journeys'], timed_dispatches(orca_log.read_text(errors='replace')
                                                         if orca_log.exists() else ''), brailled)
            report['passed'] = (probe.returncode == 0 and report.get('native_passed', False)
                                and report['dispatch_count'] > 0
                                and (display is None or report['braille_protocol_lines'] > 0))
    except Exception as error:
        report['passed'] = False
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        stop_owned(orca)
        stop_owned(speechd)
        stop_owned(brltty)
        if display is not None:
            display.closing.set()
        for stream in streams:
            stream.close()
        output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Private Orca observations: {output}; passed={report["passed"]}', flush=True)
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
