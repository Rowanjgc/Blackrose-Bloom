"""Crash and stale-stage contracts for newly published workspace files."""
from __future__ import annotations

import os
import errno
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blackrose.workspace import WorkspaceFiles, authorize_directory


class AtomicPublishTests(unittest.TestCase):
    def test_process_death_during_write_leaves_no_partial_final_and_stage_is_hidden(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = r'''
import os, sys
from blackrose import workspace
from blackrose.workspace import WorkspaceFiles, authorize_directory
original = os.fdopen
def die_after_partial(fd, mode):
    os.write(fd, b"com")
    os._exit(23)
workspace.os.fdopen = die_after_partial
WorkspaceFiles(authorize_directory(sys.argv[1])).write("crash.txt", b"complete output", reviewed=True)
'''
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
            process = subprocess.run([sys.executable, '-c', code, str(root)], env=env,
                                     capture_output=True, check=False)
            self.assertEqual(process.returncode, 23, process.stderr.decode(errors='replace'))
            self.assertFalse((root / 'crash.txt').exists())
            stages = list(root.glob('.blackrose-stage-*'))
            self.assertEqual(len(stages), 1)
            self.assertEqual(stages[0].read_bytes(), b'com')
            files = WorkspaceFiles(authorize_directory(root), root / 'downloads')
            self.assertEqual(files.browse()['entries'], [])

    def test_old_abandoned_stage_is_collected_before_new_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            from blackrose import workspace
            workspace._CLEANED_STAGE_DIRS.clear()
            files = WorkspaceFiles(authorize_directory(root), root / 'downloads')
            # Use the exact private-name shape while retaining a regular file.
            stage = root / ('.blackrose-stage-' + 'a' * 32)
            stage.write_bytes(b'partial')
            stage.chmod(0o600)
            os.utime(stage, (1, 1))
            files.write('complete.txt', b'complete', reviewed=True)
            self.assertFalse(stage.exists())
            self.assertEqual((root / 'complete.txt').read_bytes(), b'complete')

    def test_sync_failure_removes_unpublished_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = WorkspaceFiles(authorize_directory(root), root / 'downloads')
            with patch('blackrose.workspace.os.fsync', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    files.write('incomplete.txt', b'payload', reviewed=True)
            self.assertFalse((root / 'incomplete.txt').exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_short_write_failure_removes_unpublished_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = WorkspaceFiles(authorize_directory(root), root / 'downloads')
            original_fdopen = os.fdopen

            class ShortWriter:
                def __init__(self, fd):
                    self.stream = original_fdopen(fd, 'wb')

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return self.stream.__exit__(*exc)

                def write(self, data):
                    self.stream.write(data[:3])
                    raise OSError(errno.ENOSPC, 'short write')

            with patch('blackrose.workspace.os.fdopen', side_effect=lambda fd, mode: ShortWriter(fd)):
                with self.assertRaisesRegex(OSError, 'short write'):
                    files.write('short.txt', b'complete payload', reviewed=True)
            self.assertFalse((root / 'short.txt').exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_destination_created_during_staging_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = WorkspaceFiles(authorize_directory(root), root / 'downloads')
            original_link = os.link
            raced = False

            def create_racing_destination(source, destination, **kwargs):
                nonlocal raced
                if not raced:
                    raced = True
                    (root / 'race.txt').write_bytes(b'other writer')
                return original_link(source, destination, **kwargs)

            with patch('blackrose.workspace.os.link', side_effect=create_racing_destination):
                result = files.write('race.txt', b'our complete output', reviewed=True)
            self.assertEqual(Path(result['path']).name, 'race (1).txt')
            self.assertEqual((root / 'race.txt').read_bytes(), b'other writer')
            self.assertEqual((root / 'race (1).txt').read_bytes(), b'our complete output')


if __name__ == '__main__':
    unittest.main()
