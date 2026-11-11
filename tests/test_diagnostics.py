"""Logs stay bounded across large records, UTF-8, legacy files and router runs."""
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blackrose.diagnostics import FORMAT_CACHE, BoundedLogHandler, RouterOutput, reply_diagnostics


class DiagnosticTests(unittest.TestCase):
    def test_reply_report_accepts_only_host_metadata(self):
        for state in ('error', 'pending', 'cancelled'):
            report = reply_diagnostics(state, conversation_id=7, message_id=12)
            self.assertIn(f'Reply state: {state}', report)
            self.assertIn('Conversation ID: 7', report)
            self.assertIn('Assistant message ID: 12', report)
            self.assertLess(len(report), 1024)
        self.assertIsNone(reply_diagnostics('complete'))
        self.assertIsNone(reply_diagnostics('provider-secret'))
        # Even corrupted metadata cannot carry arbitrary text into the report.
        report = reply_diagnostics('error', conversation_id='sk-private', message_id='private prompt')
        self.assertNotIn('sk-private', report)
        self.assertNotIn('private prompt', report)

    def assert_bounded(self, root, limit):
        logs = list(root.glob('*.log*'))
        self.assertLessEqual(len(logs), 4)
        self.assertTrue(all(p.stat().st_size <= limit for p in logs))
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in logs))

    def test_rotation_restart_and_oversized_exception_preserve_recent_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'blackrose.log'
            for suffix in ('', '.1', '.2', '.3'):
                Path(str(path) + suffix).write_bytes(('春' * 2000 + 'legacy recent error\n').encode())
            handler = BoundedLogHandler(path, max_bytes=1024)
            self.addCleanup(handler.close)
            self.assert_bounded(root, 1024)
            self.assertIn('legacy recent error', path.read_text())
            for index in range(50):
                handler.handle(logging.makeLogRecord({'msg': f'{index} ' + '春' * 250}))
                self.assert_bounded(root, 1024)
            try:
                raise RuntimeError('x' * 5000 + ' recent exception details')
            except RuntimeError:
                handler.handle(logging.makeLogRecord({'msg': 'Failure', 'exc_info': sys.exc_info()}))
            handler.close()
            handler = BoundedLogHandler(path, max_bytes=1024)
            handler.handle(logging.makeLogRecord({'msg': 'restart diagnostic'}))
            handler.close()
            retained = ''.join(p.read_text() for p in root.glob('*.log*'))
            self.assertIn('recent exception details', retained)
            self.assertIn('restart diagnostic', retained)
            self.assert_bounded(root, 1024)

    def test_each_emitted_record_is_formatted_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'blackrose.log'
            handler = BoundedLogHandler(path, max_bytes=16384)
            self.addCleanup(handler.close)
            original = logging.Formatter.format
            calls = []

            def counted(formatter, record):
                calls.append(record.msg)
                return original(formatter, record)

            record = logging.makeLogRecord({'msg': 'single format pass'})
            with patch.object(logging.Formatter, 'format', counted):
                handler.handle(record)
            self.assertEqual(calls, ['single format pass'])
            self.assertNotIn(FORMAT_CACHE, record.__dict__)
            self.assertIn('single format pass', path.read_text())

    def test_router_pipe_rotates_without_newlines_and_drains_final_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'llama-router.log'
            for _ in range(2):
                output = RouterOutput(BoundedLogHandler(path, max_bytes=16384))
                process = subprocess.Popen([sys.executable, '-c',
                    "import os; os.write(1, b'x' * 400000); os.write(2, b'final router error')"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                output.start(process.stdout)
                self.assertEqual(process.wait(timeout=10), 0)
                output.join(5)
                self.assertFalse(output._thread.is_alive())
                self.assertIn('final router error', path.read_text())
                self.assert_bounded(root, 16384)
