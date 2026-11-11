"""User exports appear at their chosen name only when complete (bra-my67).

The native probes wait for an export to exist and then read it. An in-place
write created the file before writing it, so a slow runner could read it empty.
"""
import asyncio
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from blackrose.gui import bridge
from blackrose.gui.bridge import Api, save_export


class Loop:
    def run(self, coroutine):
        return asyncio.run(coroutine)


def leftovers(directory):
    return [p.name for p in Path(directory).iterdir() if p.name.endswith('.partial')]


class SaveExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.target = self.root / 'answer.json'

    def tearDown(self):
        self.directory.cleanup()

    def test_target_appears_only_once_the_content_is_complete(self):
        seen = []
        replace = os.replace

        def observed(source, destination):
            seen.append((self.target.exists(), Path(source).read_text()))
            replace(source, destination)

        with patch.object(bridge.os, 'replace', side_effect=observed):
            save_export(str(self.target), '{"type": "form"}')
        self.assertEqual(seen, [(False, '{"type": "form"}')])
        self.assertEqual(self.target.read_text(), '{"type": "form"}')
        self.assertEqual(leftovers(self.root), [])

    def test_overwrite_replaces_content_and_keeps_permissions(self):
        self.target.write_text('old')
        self.target.chmod(0o640)
        save_export([str(self.target)], 'new')
        self.assertEqual(self.target.read_text(), 'new')
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o640)

    def test_failed_save_leaves_the_existing_file_and_no_temporary(self):
        self.target.write_text('kept')
        with patch.object(bridge.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                save_export(str(self.target), 'lost')
        self.assertEqual(self.target.read_text(), 'kept')
        self.assertEqual(leftovers(self.root), [])

    def test_symlinked_target_updates_the_linked_file(self):
        real = self.root / 'real.json'
        real.write_text('old')
        self.target.symlink_to(real)
        save_export(str(self.target), 'new')
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(real.read_text(), 'new')


class ExportEntryPointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.api = Api.__new__(Api)
        self.api._loop = Loop()
        self.webview = SimpleNamespace(FileDialog=SimpleNamespace(SAVE='save'))

    def tearDown(self):
        self.directory.cleanup()

    def choose(self, path):
        self.api._window = SimpleNamespace(create_file_dialog=lambda *_a, **_kw: [str(path)])

    def test_interactive_answer_export_writes_through_save_export(self):
        async def ui_source(_payload):
            return '{"type": "form"}'

        self.api._session = SimpleNamespace(ui_source=ui_source)
        self.choose(self.root / 'interactive-answer.json')
        with patch.dict(sys.modules, {'webview': self.webview}):
            self.assertEqual(self.api.export_ui({}), {'ok': True})
        self.assertEqual((self.root / 'interactive-answer.json').read_text(), '{"type": "form"}')

    def test_animation_export_writes_through_save_export_and_reports_failure(self):
        async def read_animation(_attachment_id):
            return None, {'title': 'gears'}

        self.api._runtime = SimpleNamespace(controller=SimpleNamespace(read_animation=read_animation))
        self.choose(self.root / 'animation.html')
        with patch.dict(sys.modules, {'webview': self.webview}), \
                patch.object(bridge, 'export_html', return_value='<html>gears</html>'):
            self.assertEqual(self.api.export_animation(1), {'ok': True})
            self.assertEqual((self.root / 'animation.html').read_text(), '<html>gears</html>')
            self.choose(self.root / 'missing-directory' / 'animation.html')
            self.assertIn('error', self.api.export_animation(1))


if __name__ == '__main__':
    unittest.main()
