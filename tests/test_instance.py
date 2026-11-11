"""The desktop guard covers independent processes and releases on failure."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from blackrose.instance import AlreadyRunning, desktop_instance


class InstanceTests(unittest.TestCase):
    def test_desktop_rejects_second_window_before_runtime_startup(self):
        from blackrose.gui.app import run_gui
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'desktop.lock'
            runtime = SimpleNamespace(database=SimpleNamespace(path=Path(root) / 'chats.db'))
            with desktop_instance(path), patch('blackrose.gui.app._run_gui') as run:
                with self.assertRaises(AlreadyRunning):
                    run_gui(runtime=runtime)
                run.assert_not_called()

    def test_nested_acquisition_fails_and_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'desktop.lock'
            with self.assertRaisesRegex(ValueError, 'boom'), desktop_instance(path):
                with self.assertRaises(AlreadyRunning), desktop_instance(path):
                    self.fail('second desktop entered')
                raise ValueError('boom')
            with desktop_instance(path):
                self.assertTrue(path.exists())

    def test_independent_process_cannot_enter_until_owner_exits(self):
        script = ('from pathlib import Path\n'
                  'from blackrose.instance import desktop_instance, AlreadyRunning\n'
                  'import sys\n'
                  'try:\n'
                  '    with desktop_instance(Path(sys.argv[1])): print("entered")\n'
                  'except AlreadyRunning:\n'
                  '    sys.exit(9)\n')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'desktop.lock'
            with desktop_instance(path):
                result = subprocess.run([sys.executable, '-c', script, str(path)],
                                        capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 9, result.stderr)
                self.assertNotIn('entered', result.stdout)
            result = subprocess.run([sys.executable, '-c', script, str(path)],
                                    capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'entered')
