"""Run the offline unittests and fail if any test writes outside its own scratch data.

Each test that saves preferences, data or state must point the XDG directories at
its own temporary root. This runner gives the whole suite empty XDG directories and
fails when anything is left in them, so a test that would have written into the
developer's real settings is reported instead of hidden.

Extra arguments go to ``unittest discover``, for example ``-p test_local_images.py -v``.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

XDG = (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
       ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache'))


def main(arguments):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='blackrose-unit-') as temporary:
        scratch = Path(temporary)
        environment = dict(os.environ)
        for variable, child in XDG:
            environment[variable] = str(scratch / child)
        code = subprocess.call(
            [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', *(arguments or ['-q'])],
            cwd=root, env=environment)
        leaked = sorted(str(path.relative_to(scratch)) for path in scratch.rglob('*') if path.is_file())
    if leaked:
        print('Tests wrote to the shared XDG directories instead of their own temporary root; '
              'outside this runner these files would land in your real settings:', file=sys.stderr)
        for path in leaked:
            print(f'  {path}', file=sys.stderr)
        return code or 1
    return code


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
