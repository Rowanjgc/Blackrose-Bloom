"""The maintenance runner retains failures and continues to later checks."""
import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'check_health', Path(__file__).resolve().parents[1] / 'scripts/check_health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class HealthRunnerTests(unittest.TestCase):
    def test_interruption_reaps_the_active_probe(self):
        children = []
        wait = subprocess.Popen.wait

        def interrupt(process, timeout=None):
            if timeout is not None:
                children.append(process.pid)
                raise KeyboardInterrupt
            return wait(process)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(subprocess.Popen, 'wait', interrupt), redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    health.run_checks([[sys.executable, '-c', 'import time; time.sleep(60)']],
                                      root, dict(os.environ), root)
            self.assertEqual(len(children), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(children[0], 0)

    def test_failure_and_timeout_keep_diagnostics_and_run_remaining_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = [[sys.executable, '-c', source] for source in (
                'print("probe failure", flush=True); raise SystemExit(7)',
                'import time; print("stuck probe", flush=True); time.sleep(60)',
                'print("later probe passed")',
            )]
            commands.insert(0, [str(root / 'missing-executable')])
            with redirect_stdout(io.StringIO()):
                self.assertFalse(health.run_checks(commands, root, dict(os.environ), root, timeout=1))
            results = json.loads((root / 'results.json').read_text())
            self.assertEqual([r['status'] for r in results], ['failed', 'failed', 'timeout', 'passed'])
            self.assertIsNone(results[0]['returncode'])
            self.assertIn('missing-executable', (root / results[0]['log']).read_text())
            self.assertEqual(results[1]['returncode'], 7)
            for result, message in zip(results[1:], ('probe failure', 'stuck probe', 'later probe passed'), strict=False):
                self.assertIn(message, (root / result['log']).read_text())
