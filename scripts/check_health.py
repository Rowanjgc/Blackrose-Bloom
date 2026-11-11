"""Run the deterministic development checks; optional GUI probes use scratch data."""
from pathlib import Path
import argparse
from contextlib import nullcontext
import json
import os
import signal
import subprocess
import sys
import tempfile
import time


# Shared by the explicit local GUI run and CI's GUI-only job.
GUI_PROBES = (
    ('check_gui_images.py',), ('check_gui_generation.py',), ('check_gui_animations.py',),
    ('check_gui_motion_canvas.py',), ('check_gui_opencode.py',), ('check_gui_session.py',),
    ('check_gui_workspace.py',), ('check_gui_workspace_autocomplete.py',),
    ('check_gui_library.py',), ('check_gui_memory.py',), ('check_gui_interactive_ui.py',),
    ('check_gui_skill_scripts.py',),
    ('check_gui_citations.py',),
    ('check_gui_diagrams.py',),
    ('check_gui_motion_canvas.py', '--recovery'),
)


def run_checks(commands, root, environment, report_dir=None, timeout=180):
    results = []
    if report_dir:
        report_dir.mkdir(parents=True, exist_ok=True)
    for index, command in enumerate(commands, 1):
        print('+ ' + ' '.join(command), flush=True)
        output = report_dir / f'{index:02d}.log' if report_dir else None
        started = time.monotonic()
        with output.open('w') if output else nullcontext(None) as stream:
            # Kill the entire probe group on timeout, including native children.
            try:
                with subprocess.Popen(command, cwd=root, env=environment, stdout=stream,
                                      stderr=subprocess.STDOUT, start_new_session=True) as process:
                    try:
                        code = process.wait(timeout=timeout)
                        status = 'passed' if code == 0 else 'failed'
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        code = process.wait()
                        status = 'timeout'
                    except BaseException:
                        # An interrupted runner must not leave native probes alive.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                        raise
            except OSError as error:
                print(str(error), file=stream or sys.stderr, flush=True)
                code, status = None, 'failed'
        result = {'command': command, 'status': status, 'returncode': code,
                  'seconds': round(time.monotonic() - started, 3),
                  'log': output.name if output else None}
        results.append(result)
        if report_dir:
            (report_dir / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        print(f'{status}: {" ".join(command)}' + (f' (log: {output})' if output else ''), flush=True)
        if status != 'passed' and output:
            print(output.read_text(errors='replace')[-16000:], flush=True)
    return all(result['status'] == 'passed' for result in results)


def main():
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--gui', action='store_true', help='Also exercise native GTK/WebKit (requires a display)')
    modes.add_argument('--gui-only', action='store_true', help='Run only native GTK/WebKit checks (requires a display)')
    parser.add_argument('--report-dir', type=Path, help='Save per-command logs and results.json')
    args = parser.parse_args()
    environment = dict(os.environ)
    graphical = args.gui or args.gui_only
    if graphical:
        if not (environment.get('DISPLAY') or environment.get('WAYLAND_DISPLAY')):
            parser.error('Native checks require a display; use xvfb-run -a dbus-run-session -- ...')
        environment['BLACKROSE_TEST_WEBKIT'] = '1'
        # A native probe that aborts (bra-q59) otherwise leaves an empty log:
        # dump every Python thread's stack on a fatal signal.
        environment.setdefault('PYTHONFAULTHANDLER', '1')
    else:
        environment.pop('BLACKROSE_TEST_WEBKIT', None)
    root = Path(__file__).resolve().parents[1]
    commands = [
        [sys.executable, 'scripts/check_unit_tests.py'],
        [sys.executable, 'scripts/check_recovery.py'],
        [sys.executable, 'scripts/check_docs.py'],
        [sys.executable, '-m', 'ruff', 'check', 'src', 'tests', 'scripts', 'experiments'],
        [sys.executable, '-m', 'mypy'],
        ['node', '--check', 'src/blackrose/gui/static/bloom.js'],
        ['node', '--check', 'src/blackrose/gui/static/diagrams.js'],
        ['node', '--check', 'src/blackrose/gui/static/animations.js'],
        ['node', '--check', 'src/blackrose/gui/static/motion-canvas-player.js'],
        ['node', '--check', 'src/blackrose/gui/static/motion-canvas-frame.js'],
        ['npm', 'test', '--prefix', 'ui-runtime'],
        ['uv', 'lock', '--check', '--offline'],
    ]
    if args.gui_only:
        commands = [[sys.executable, 'scripts/check_unit_tests.py', '-p', pattern, '-v']
                    for pattern in ('test_motion_canvas*.py', 'test_animation_recovery.py')]
    if graphical:
        commands += [[sys.executable, f'scripts/{name}', *options] for name, *options in GUI_PROBES]
    with tempfile.TemporaryDirectory(prefix='blackrose-health-') as temporary:
        # Isolate any incidental preferences/cache access as well as probe data.
        for variable, child in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                                ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache')):
            environment[variable] = str(Path(temporary) / child)
        if not run_checks(commands, root, environment,
                          args.report_dir.resolve() if args.report_dir else None):
            raise SystemExit(1)
    print('All health checks passed.', flush=True)


if __name__ == '__main__':
    main()
