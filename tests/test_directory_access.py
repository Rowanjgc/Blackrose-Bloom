"""Directory access must originate in a user gesture, with bounded grants."""
import asyncio
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from blackrose.main import main
from blackrose.workspace import FileAccessError, WorkspaceFiles, authorize_directory, mentioned_directories
import test_gui_images as fixtures
from test_turns import Backend


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.downloads = self.root / 'Downloads'
        self.enterContext(patch('blackrose.main.state_dir', return_value=self.root))
        self.config = self.enterContext(patch('blackrose.main.load_config'))
        self.enterContext(patch('blackrose.diagnostics.BoundedLogHandler'))
        self.enterContext(patch('blackrose.main.logging.basicConfig'))
        self.enterContext(patch('blackrose.gui.app.gui_problem', return_value=None))
        self.run_gui = self.enterContext(patch('blackrose.gui.app.run_gui', return_value=0))
        self.enterContext(patch('blackrose.workspace.downloads_directory', return_value=self.downloads))
        self.enterContext(patch.dict(os.environ, HOME=str(self.root)))

    def test_launch_paths_and_default_output_folder(self):
        outputs = self.downloads / 'Blackrose'
        dev = self.root / 'Dev'
        dev.mkdir()
        for args, expected in (([], outputs), (['.'], Path.cwd()),
                               (['~/Dev/'], dev), ([str(dev)], dev),
                               (['chat', str(dev)], dev)):
            with self.subTest(args=args):
                self.assertEqual(main(args), 0)
                self.run_gui.assert_called_with(self.config.return_value, workspace=str(expected))
        # Plain launch narrows default reads to the private output folder.
        self.assertEqual(stat.S_IMODE(outputs.stat().st_mode), 0o700)
        for args in ([str(self.root / 'missing')], ['doctor', 'unexpected'],
                     ['openrouter-key', 'invalid']):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                main(args)

    def test_planted_output_folder_link_is_not_authorized(self):
        self.downloads.mkdir()
        (self.downloads / 'Blackrose').symlink_to(self.root, target_is_directory=True)
        with self.assertLogs('blackrose.main', 'WARNING') as logs:
            self.assertEqual(main([]), 0)
        self.run_gui.assert_called_with(self.config.return_value, workspace=None)
        self.assertIn('without symlinks', logs.output[0])
        self.assertTrue((self.downloads / 'Blackrose').is_symlink())

    def test_path_extraction_is_literal_and_does_not_grant_file_parents(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / 'with spaces'
            folder.mkdir()
            file = root / 'note.txt'
            file.touch()
            with patch.dict(os.environ, HOME=raw):
                grants = mentioned_directories(f'Look in "~/with spaces" and `{folder}` or {root}.')
                self.assertEqual({g['path'] for g in grants}, {str(folder), str(root)})
                self.assertEqual(mentioned_directories(f'{file} https://example.com{root} $HOME/with'), [])


class DirectoryAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def folder(self):
        folder = self.root / 'outside'
        folder.mkdir()
        (folder / 'note.txt').write_text('outside evidence')
        return folder

    async def test_approval_enables_tools_and_followup_without_switching_workspace(self):
        c = self.runtime.controller
        work = self.root / 'work'
        work.mkdir()
        await c.configure_launch_workspace(str(work))
        outside = await self.folder()
        c.backend = Backend([[('file_read', {'path': str(outside / 'note.txt')})], 'Read.', 'Done.'])
        approvals = []
        self.session._approve_directories = lambda grants: approvals.append(grants) or True
        self.assertEqual(await self.session.submit(f'Please read "{outside}".'), {'clear': True})
        await self.session._generation
        self.assertEqual(len(approvals), 1)
        self.assertEqual(c.conversation.workspace['path'], str(work))
        self.assertIn(str(outside), c.backend.requests[0][0]['content'])
        self.assertIn('outside evidence', c.backend.requests[1][-1]['content'])
        await self.session.submit(f'Look at {outside} again')
        await self.session._generation
        self.assertEqual(len(approvals), 1)
        self.assertEqual(await c.directory_access_requests(str(work)), [])
        first = c.conversation.id
        await c.new_conversation()
        self.assertEqual(c.conversation.workspace['path'], str(work))
        with self.assertRaises(FileAccessError):
            (await c.workspace_files()).read(str(outside / 'note.txt'))
        await c.open_conversation(first)
        self.assertEqual((await c.workspace_files()).read(str(outside / 'note.txt'))['text'], 'outside evidence')
        await c.set_workspace(None)
        with self.assertRaises(FileAccessError):
            (await c.workspace_files()).read(str(outside / 'note.txt'))

    async def test_denial_retains_draft_and_does_not_send_or_grant_access(self):
        outside = await self.folder()
        c = self.runtime.controller
        c.backend = Backend()
        self.session._approve_directories = lambda grants: False
        text = f'Read {outside}'
        self.session.update_draft(text)
        self.assertEqual(await self.session.submit(text), {'clear': False})
        self.assertEqual(self.session._draft_text, text)
        self.assertEqual(c.backend.requests, [])
        self.assertEqual(c.directory_grants, {})

    async def test_bare_absolute_directory_is_chat_and_changed_identity_is_refused(self):
        outside = await self.folder()
        c = self.runtime.controller
        c.backend = Backend()
        self.session._approve_directories = lambda grants: True
        self.assertEqual(await self.session.submit(str(outside)), {'clear': True})
        await self.session._generation
        outside.rename(self.root / 'old')
        outside.mkdir()
        (outside / 'note.txt').write_text('replacement')
        with self.assertRaises(FileAccessError):
            (await c.workspace_files()).read(str(outside / 'note.txt'))
        self.assertTrue(await c.directory_access_requests(str(outside)))

    async def test_conversation_change_during_confirmation_cannot_transfer_access(self):
        outside = await self.folder()
        entered, release = threading.Event(), threading.Event()
        def approve(grants):
            entered.set()
            release.wait(5)
            return True
        self.session._approve_directories = approve
        pending = asyncio.create_task(self.session.submit(f'Read {outside}'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            await self.runtime.controller.new_conversation()
        finally:
            release.set()
        self.assertEqual(await pending, {'clear': False})
        self.assertEqual(self.runtime.controller.directory_grants, {})

    async def test_additional_root_preserves_symlink_and_traversal_boundaries(self):
        outside = await self.folder()
        files = WorkspaceFiles(additional=[authorize_directory(outside)])
        self.assertEqual(files.read(str(outside / 'note.txt'))['text'], 'outside evidence')
        self.assertEqual(len(files.browse(str(outside))['entries']), 1)
        with self.assertRaisesRegex(FileAccessError, 'without review'):
            files.write(str(outside / 'new.txt'), b'new')
        self.assertFalse((outside / 'new.txt').exists())
        files.write(str(outside / 'new.txt'), b'new', reviewed=True)
        (outside / 'link').symlink_to(self.root, target_is_directory=True)
        for path in (str(outside / '../private'), str(outside / 'link/private'), str(self.root / 'private')):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                files.write(path, b'bad', reviewed=True)
        self.assertFalse((self.root / 'private').exists())

    async def test_direct_controller_send_does_not_auto_authorize_mentions(self):
        outside = await self.folder()
        c = self.runtime.controller
        c.backend = Backend()
        await c.send(f'Read {outside}')
        self.assertEqual(c.directory_grants, {})
        with self.assertRaises(FileAccessError):
            (await c.workspace_files()).read(str(outside / 'note.txt'))
