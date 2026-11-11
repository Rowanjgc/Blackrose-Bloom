"""Real filesystem, document, reference and conversation workflow contracts."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from blackrose.documents import render_pdf
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository
from blackrose.workspace import FileAccessError, WorkspaceFiles, _directory_fd, authorize_directory, downloads_directory
import test_gui_images as fixtures
from test_turns import Backend


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.downloads = self.root / 'downloads'
        self.backups = self.root / 'data' / 'edit-backups'
        self.files = WorkspaceFiles(authorize_directory(self.work), self.downloads, backups=self.backups)

    def test_relative_and_absolute_creation_collision_and_bounded_read(self):
        first = self.files.write('notes/a.md', b'abcdefghij', reviewed=True)['path']
        self.assertEqual(Path(first), self.work / 'notes/a.md')
        second = self.files.write(first, b'new', reviewed=True)['path']
        self.assertEqual(Path(second).name, 'a (1).md')
        self.assertEqual(Path(first).read_text(), 'abcdefghij')
        result = self.files.read(first, offset=3, limit=4)
        self.assertEqual(result['text'], 'defg')
        self.assertEqual(result['next_offset'], 7)
        self.assertEqual(self.files.metadata('notes')['kind'], 'directory')

    def test_empty_text_file_is_readable_without_fabricating_content(self):
        self.files.write('empty.txt', b'', reviewed=True)
        result = self.files.read('empty.txt')
        self.assertEqual(result['text'], '')
        self.assertEqual(result['characters'], 0)
        self.assertIsNone(result['next_offset'])

    def test_traversal_outside_and_malicious_arguments_are_refused(self):
        for path in ('../escape.txt', 'sub/../../escape', str(self.root / 'escape'), 'bad\x00path', '', 12):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.files.write(path, b'no', reviewed=True)
        self.assertFalse((self.root / 'escape').exists())
        self.assertFalse((self.work / 'sub').exists())

    def test_directory_walk_refuses_parent_traversal_before_opening(self):
        # Without the guard, the walk would follow '..' and open the sibling target.
        base = self.root / 'base'
        (base / 'inner').mkdir(parents=True)
        (base / 'target').mkdir()
        with self.assertRaises(FileAccessError) as caught:
            with _directory_fd(f'{base}/inner/../target'):
                self.fail('directory walk followed parent traversal')
        self.assertEqual(caught.exception.code, 'invalid_path')

    def test_destination_refuses_traversal_when_the_output_folder_is_missing(self):
        # A missing output folder makes parent() fail before its own '..' check, so
        # destination() builds the path itself; its fallback must refuse traversal too.
        files = WorkspaceFiles(None, self.root / 'missing-downloads', backups=self.backups)
        self.assertFalse(files.outputs.exists())
        for raw in ('../escape.txt', 'a/../../../escape.txt', str(files.outputs)):
            with self.subTest(raw=raw), self.assertRaises(FileAccessError) as caught:
                files.destination(raw)
            self.assertEqual(caught.exception.code, 'invalid_path')

    def test_symlinks_at_every_depth_and_final_component(self):
        secret = self.root / 'secret.txt'
        secret.write_text('secret')
        (self.work / 'link').symlink_to(self.root, target_is_directory=True)
        (self.work / 'secret.txt').symlink_to(secret)
        for path in ('link/secret.txt', 'secret.txt'):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.files.read(path)
            with self.subTest(write=path), self.assertRaises((ValueError, OSError)):
                self.files.write(path, b'bad', collision='error', reviewed=True)
        self.assertEqual(secret.read_text(), 'secret')
        self.assertFalse(self.files.browse()['entries'])
        # A collision with a symlink creates a separately named file safely.
        self.assertEqual(Path(self.files.write('secret.txt', b'new', reviewed=True)['path']).name, 'secret (1).txt')

    def test_metadata_and_typed_references_refuse_symlinks_even_to_outside_files(self):
        # A typed @reference must not describe a link's target: that would reveal an outside file's size and identity.
        outside = self.root / 'outside.txt'
        outside.write_text('outside contents')
        (self.work / 'link.txt').symlink_to(outside)
        for call in (self.files.metadata, self.files.direct_reference):
            with self.subTest(call=call.__name__):
                with self.assertRaises(FileAccessError) as refused:
                    call('link.txt')
                self.assertEqual(refused.exception.code, 'symlink_refused')

    def test_changed_workspace_identity_requires_reauthorization(self):
        self.work.rename(self.root / 'old-work')
        self.work.mkdir()
        with self.assertRaisesRegex(FileAccessError, 'identity changed'):
            self.files.write('new.txt', b'no', reviewed=True)
        self.assertFalse((self.work / 'new.txt').exists())

    def test_no_workspace_can_create_downloads_but_cannot_read_unrelated_files(self):
        files = WorkspaceFiles(downloads=self.downloads)
        output = files.write('report.txt', b'report')['path']
        with self.assertRaises(FileAccessError):
            files.read(output)
        files.readable.add(output)
        self.assertEqual(files.read(output)['text'], 'report')
        unrelated = self.downloads / 'private.txt'
        unrelated.write_text('private')
        with self.assertRaises(FileAccessError):
            files.read(str(unrelated))
        with self.assertRaises(FileAccessError):
            files.browse(str(self.downloads))

    def test_explicit_output_folder_with_active_workspace(self):
        output = self.files.write(str(self.downloads / 'Blackrose' / 'report.txt'), b'report')['path']
        self.files.readable.add(output)
        self.assertEqual(self.files.read(output)['text'], 'report')
        self.assertFalse((self.work / 'report.txt').exists())
        # Downloads itself is a destination only for a reviewed file.
        with self.assertRaisesRegex(FileAccessError, 'without review'):
            self.files.write(str(self.downloads / 'other.txt'), b'no')
        self.assertFalse((self.downloads / 'other.txt').exists())
        output = self.files.write(str(self.downloads / 'other.txt'), b'yes', reviewed=True)['path']
        self.assertEqual(Path(output).read_bytes(), b'yes')

    def test_downloads_symlink_and_ancestor_symlink_do_not_expand_implicit_access(self):
        private = self.root / 'private'
        private.mkdir()
        self.downloads.symlink_to(private, target_is_directory=True)
        files = WorkspaceFiles(downloads=self.downloads)
        with self.assertRaises((OSError, ValueError)):
            files.write('output.txt', b'no')
        self.assertFalse((private / 'output.txt').exists())
        alias = self.root / 'alias'
        alias.symlink_to(private, target_is_directory=True)
        files = WorkspaceFiles(downloads=alias / 'Downloads')
        with self.assertRaises((OSError, ValueError)):
            files.write('output.txt', b'no')
        self.assertFalse((private / 'Downloads').exists())

    def test_permission_failure_is_not_reported_as_empty_text(self):
        target = self.work / 'private.txt'
        target.write_text('private')
        target.chmod(0)
        self.addCleanup(target.chmod, 0o600)
        if os.getuid() == 0:
            self.skipTest('root bypasses file read permissions')
        with self.assertRaises(PermissionError):
            self.files.read('private.txt')

    def test_concurrent_collisions_preserve_all_outputs(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda n: self.files.write('same.txt', str(n).encode(), reviewed=True)['path'], range(8)))
        self.assertEqual(len(set(outputs)), 8)
        self.assertEqual({Path(p).read_text() for p in outputs}, {str(n) for n in range(8)})

    def test_replace_checks_digest_and_keeps_backup(self):
        self.files.write('note.md', b'old', reviewed=True)
        with self.assertRaises(FileAccessError):
            self.files.write('note.md', b'bad', collision='replace', expected_digest='invalid', reviewed=True)
        result = self.files.write('note.md', b'new', collision='replace',
                                  expected_digest=hashlib.sha256(b'old').hexdigest(), reviewed=True)
        self.assertEqual(Path(result['path']).read_text(), 'new')
        self.assertEqual(Path(result['backup']).read_text(), 'old')
        self.assertEqual(Path(result['backup']).parent, self.backups / 'unassigned')
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ['note.md'])

    def test_replacement_retains_edits_at_publication(self):
        from blackrose import workspace
        for replace_inode in (False, True):
            with self.subTest(replace_inode=replace_inode):
                path = self.work / f'race-{replace_inode}.txt'
                path.write_text('old')
                exchange = workspace._exchange

                def race(fd, source, destination, *, replace_inode=replace_inode,
                         path=path, exchange=exchange):
                    if replace_inode:
                        other = self.work / 'external.tmp'
                        other.write_text('external')
                        os.replace(other, path)
                    else:
                        path.write_text('external')
                    exchange(fd, source, destination)

                with patch('blackrose.workspace._exchange', side_effect=race):
                    with self.assertRaisesRegex(FileAccessError, 'published.*retained') as raised:
                        self.files.write(path.name, b'new', collision='replace',
                                         expected_digest=hashlib.sha256(b'old').hexdigest(), reviewed=True)
                self.assertEqual(path.read_text(), 'new')
                # The displaced external edit is retained in the backup store, both paths reported.
                backup = Path(raised.exception.details['backup'])
                self.assertTrue(backup.is_relative_to(self.backups))
                self.assertIn(str(backup), str(raised.exception))
                self.assertEqual(backup.read_text(), 'external')
                self.assertTrue(raised.exception.details['backup_conflict'])

    def test_exchange_failure_leaves_original_and_no_staged_backup(self):
        path = self.work / 'exchange-failure.txt'
        path.write_text('old')
        with patch('blackrose.workspace._exchange', side_effect=OSError('unavailable')):
            with self.assertRaises(OSError):
                self.files.write(path.name, b'new', collision='replace',
                                 expected_digest=hashlib.sha256(b'old').hexdigest(), reviewed=True)
        self.assertEqual(path.read_text(), 'old')
        self.assertFalse(Path(str(path) + '.backup').exists())

    def test_change_before_second_check_refuses_publication(self):
        path = self.work / 'second-check.txt'
        path.write_text('old')
        read = self.files.read_bytes
        calls = 0

        def change_before_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_text('external')
            return read(*args, **kwargs)

        with patch.object(self.files, 'read_bytes', side_effect=change_before_read):
            with self.assertRaisesRegex(FileAccessError, 'changed'):
                self.files.write(path.name, b'new', collision='replace',
                                 expected_digest=hashlib.sha256(b'old').hexdigest(), reviewed=True)
        self.assertEqual(path.read_text(), 'external')
        self.assertFalse(Path(str(path) + '.backup').exists())

    def test_interruption_after_exchange_retains_displaced_file(self):
        from blackrose import workspace
        path = self.work / 'interrupted.txt'
        path.write_text('old')
        exchange = workspace._exchange

        def interrupt(fd, source, destination):
            exchange(fd, source, destination)
            raise KeyboardInterrupt()

        with patch('blackrose.workspace._exchange', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.files.write(path.name, b'new', collision='replace',
                                 expected_digest=hashlib.sha256(b'old').hexdigest(), reviewed=True)
        self.assertEqual(path.read_text(), 'new')
        self.assertEqual(Path(str(path) + '.backup').read_text(), 'old')

    def test_approved_replacement_preserves_ordinary_permission_modes(self):
        for index, mode in enumerate((0o640, 0o644, 0o755)):
            with self.subTest(mode=oct(mode)):
                path = self.work / f'mode-{index}.txt'
                path.write_bytes(b'old')
                path.chmod(mode)
                self.files.write(path.name, b'new', collision='replace',
                                 expected_digest=hashlib.sha256(b'old').hexdigest(), reviewed=True)
                self.assertEqual(path.stat().st_mode & 0o777, mode)
                self.assertEqual(path.read_bytes(), b'new')

    def test_approved_replacement_strips_special_permission_bits(self):
        path = self.work / 'special.txt'
        path.write_bytes(b'old')
        path.chmod(0o7777)
        self.assertTrue(path.stat().st_mode & 0o7000)
        self.files.write(path.name, b'new', collision='replace',
                         expected_digest=hashlib.sha256(b'old').hexdigest(), reviewed=True)
        self.assertEqual(path.stat().st_mode & 0o777, 0o777)
        self.assertFalse(path.stat().st_mode & 0o7000)

    def test_replacement_metadata_or_chmod_failure_leaves_original_in_place(self):
        path = self.work / 'protected.txt'
        path.write_bytes(b'old')
        path.chmod(0o640)
        digest = hashlib.sha256(b'old').hexdigest()
        with patch('blackrose.workspace.os.stat', side_effect=PermissionError('metadata denied')):
            with self.assertRaises(PermissionError):
                self.files.write(path.name, b'new', collision='replace', expected_digest=digest, reviewed=True)
        self.assertEqual(path.read_bytes(), b'old')
        self.assertFalse(Path(str(path) + '.backup').exists())
        with patch('blackrose.workspace.os.fchmod', side_effect=PermissionError('chmod denied')):
            with self.assertRaises(PermissionError):
                self.files.write(path.name, b'new', collision='replace', expected_digest=digest, reviewed=True)
        self.assertEqual(path.read_bytes(), b'old')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertFalse(any(p.name.startswith('.blackrose-') for p in self.work.iterdir()))

    def test_new_outputs_remain_private_and_non_executable(self):
        path = Path(self.files.write('new-script.py', b'print(1)', reviewed=True)['path'])
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertFalse(mode & 0o111)

    def test_special_and_oversized_files_are_refused(self):
        fifo = self.work / 'pipe'
        os.mkfifo(fifo)
        with self.assertRaises(FileAccessError):
            self.files.read('pipe')
        (self.work / 'large.txt').write_bytes(b'a' * 20)
        with self.assertRaises(FileAccessError):
            self.files.read_bytes('large.txt', limit=10)

    def test_bounded_search_and_pagination(self):
        for n in range(65):
            (self.work / f'note-{n:03}.md').write_text('content')
        listing = self.files.browse()
        self.assertEqual(len(listing['entries']), 50)
        self.assertEqual(listing['next_offset'], 50)
        self.assertEqual(len(self.files.browse(offset=50)['entries']), 15)
        self.assertEqual(len(self.files.browse(query='n064')['entries']), 1)
        with patch('blackrose.workspace.MAX_SCAN', 10):
            result = self.files.browse(recursive=True)
        self.assertEqual(result['scanned'], 10)
        self.assertTrue(result['scan_truncated'])

    def test_xdg_downloads_is_parsed_as_data_never_executed(self):
        config = self.root / 'config'
        config.mkdir()
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}), patch('pathlib.Path.home', return_value=self.root):
            (config / 'user-dirs.dirs').write_text('XDG_DOWNLOAD_DIR="$HOME/My Downloads"\n')
            self.assertEqual(downloads_directory(), self.root / 'My Downloads')
            (config / 'user-dirs.dirs').write_text('XDG_DOWNLOAD_DIR="$(touch pwned)"\n')
            self.assertEqual(downloads_directory(), self.root / 'Downloads')
            self.assertFalse((self.root / 'pwned').exists())

    def test_pdf_is_real_readable_paginated_and_does_not_load_images(self):
        source = '# Research report\n\n- First finding\n- Second finding\n\n| Source | Finding |\n|---|---|\n| Example | Evidence |\n\n'
        data = render_pdf(source + '\n\n'.join('A readable paragraph.' for _ in range(100)))
        self.assertTrue(data.startswith(b'%PDF-'))
        reader = PdfReader(io.BytesIO(data))
        self.assertGreater(len(reader.pages), 1)
        text = '\n'.join(p.extract_text() for p in reader.pages)
        for part in ('Research report', 'First finding', 'Source', 'Evidence'):
            self.assertIn(part, text)
        self.assertNotIn('/JavaScript', str(reader.trailer))


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown
    latest = fixtures.ImageTests.latest

    async def workspace(self):
        work = self.root / 'workspace'
        work.mkdir(exist_ok=True)
        await self.runtime.controller.set_workspace(str(work))
        return work

    def backend(self, steps):
        c = self.runtime.controller
        c.backend = Backend(steps)
        return c

    async def test_output_metadata_failure_recovers_without_repeating_disk_effect(self):
        work = self.root / 'downloads' / 'Blackrose'
        for failure in ('blob', 'metadata'):
            with self.subTest(failure=failure), patch('blackrose.workspace.downloads_directory',
                                                      return_value=self.root / 'downloads'):
                c = self.backend([[('document_create', {
                    'path': failure + '.txt', 'content': 'saved'})], 'Saved.'])
                target = c._store if failure == 'blob' else c._repo
                method = 'write' if failure == 'blob' else 'append_attachments'
                with patch.object(target, method, side_effect=OSError('disk full')):
                    turn = await c.send('Create output')
                result = json.loads(next(m['content'] for m in c.backend.requests[-1]
                                         if m['role'] == 'tool'))
                self.assertTrue(result['artifact_pending'])
                self.assertEqual(result['path'], str(work / (failure + '.txt')))
                self.assertEqual(Path(result['path']).read_text(), 'saved')
                self.assertIn('Saved on disk', result['notice'])
                # Recovery uses only the durable record; reopen storage first.
                db = Database(self.runtime.database.path)
                await db.connect()
                try:
                    with patch.object(c, '_repo', Repository(db)):
                        files = await c.workspace_files()
                        with patch.object(files, 'write', side_effect=AssertionError('replayed')):
                            recovered = await c._save_output(files, turn.assistant_message,
                                failure + '.txt', b'saved')
                    self.assertIn('attachment_id', recovered)
                finally:
                    await db.close()
                self.assertFalse((work / (failure + ' (1).txt')).exists())

    async def test_edit_metadata_failure_retains_backup_and_checks_external_changes(self):
        work = await self.workspace()
        (work / 'notes.md').write_text('before')
        c = self.backend([[('file_edit', {'path': 'notes.md', 'content': 'after'})], 'Review.'])
        turn = await c.send('Edit')
        token = next(iter(c.pending_edits))
        pending = c.pending_edits[token]
        with patch.object(c._repo, 'append_attachments', side_effect=OSError('disk full')):
            result = await c.review_edit(token, approve=True)
        self.assertTrue(result['artifact_pending'])
        self.assertEqual(Path(result['backup']).read_text(), 'before')
        files = await c.workspace_files()
        (work / 'notes.md').write_text('external change')
        with self.assertRaisesRegex(ValueError, 'changed'):
            await c._save_output(files, turn.assistant_message, 'notes.md', b'after', reviewed=True,
                collision='replace', expected_digest=pending['digest'])
        self.assertEqual((work / 'notes.md').read_text(), 'external change')
        await c.set_workspace(None)
        with self.assertRaisesRegex(ValueError, 'original workspace'):
            await c.recover_output(result['recovery_id'])
        await c.set_workspace(str(work))
        (work / 'notes.md').write_text('after')
        recovered = await c.recover_output(result['recovery_id'])
        self.assertEqual(recovered['backup'], result['backup'])
        self.assertIn('attachment_id', recovered)

    async def test_workspace_persists_across_database_reopen_switches_and_clears(self):
        work = await self.workspace()
        c = self.runtime.controller
        first = c.conversation.id
        db = Database(self.runtime.database.path)
        await db.connect()
        try:
            stored = await Repository(db).get_conversation(first)
            self.assertEqual(stored.workspace, authorize_directory(work))
        finally:
            await db.close()
        await c.new_conversation()
        self.assertIsNone(c.conversation.workspace)
        await c.open_conversation(first)
        self.assertEqual(c.conversation.workspace['path'], str(work))
        await c.set_workspace(None)
        self.assertIsNone((await c._repo.get_conversation(first)).workspace)

    async def test_workspace_selection_rejects_navigation_delete_and_replacement(self):
        from blackrose.chat import ChatBusy
        c = self.runtime.controller
        work = self.root / 'chosen'
        work.mkdir()
        for action in ('switch', 'delete', 'replace', 'switch_back'):
            await c.new_conversation()
            original = c.conversation.id
            selection = await c.begin_workspace_selection()
            if action == 'delete':
                await c._delete_current_conversation()
            elif action == 'replace':
                await c.new_conversation()
            else:
                await c.new_conversation()
                if action == 'switch_back':
                    await c.open_conversation(original)
            with self.assertRaisesRegex(ChatBusy, 'choose the directory again'):
                await c.set_workspace(str(work), selection=selection)
            self.assertIsNone(c.conversation.workspace)
            stored = await c._repo.get_conversation(original)
            if stored:
                self.assertIsNone(stored.workspace)

    async def test_workspace_navigation_during_authorization(self):
        from blackrose.chat import ChatBusy
        c = self.runtime.controller
        await c.ensure_conversation()
        original = c.conversation.id
        entered, release = threading.Event(), threading.Event()
        def authorize(path):
            entered.set()
            release.wait(5)
            return authorize_directory(path)
        with patch('blackrose.workspace.authorize_directory', side_effect=authorize):
            task = asyncio.create_task(c.set_workspace(str(self.root)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                await c.new_conversation()
            finally:
                release.set()
            with self.assertRaisesRegex(ChatBusy, 'expired'):
                await task
        self.assertIsNone(c.conversation.workspace)
        self.assertIsNone((await c._repo.get_conversation(original)).workspace)

    async def test_workspace_newer_selection_supersedes_pending_picker(self):
        from blackrose.chat import ChatBusy
        c = self.runtime.controller
        first = await c.begin_workspace_selection()
        second = await c.begin_workspace_selection()
        with self.assertRaisesRegex(ChatBusy, 'expired'):
            await c.set_workspace(str(self.root), selection=first)
        await c.set_workspace(str(self.root), selection=second)
        self.assertEqual(c.conversation.workspace, authorize_directory(self.root))

    async def test_native_workspace_picker_completion_after_navigation(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from blackrose.gui.bridge import Api
        c = self.runtime.controller
        await c.ensure_conversation()
        original = c.conversation.id
        loop = asyncio.get_running_loop()
        def run(coro):
            return asyncio.run_coroutine_threadsafe(coro, loop).result(5)
        def choose(*args):
            run(c.new_conversation())
            return [str(self.root)]
        api = Api.__new__(Api)
        api._runtime = self.runtime
        api._loop = SimpleNamespace(run=run)
        api._window = SimpleNamespace(create_file_dialog=choose)
        api._session = SimpleNamespace(notice=Mock())
        with patch.dict('sys.modules', webview=SimpleNamespace(
                FileDialog=SimpleNamespace(FOLDER='folder'))):
            result = await asyncio.to_thread(api.pick_workspace)
        self.assertIn('expired', result['error'])
        api._s.notice.assert_called_once()
        self.assertIsNone(c.conversation.workspace)
        self.assertIsNone((await c._repo.get_conversation(original)).workspace)

    async def test_workspace_navigation_during_database_write_keeps_owner(self):
        from blackrose.chat import ChatBusy
        c = self.runtime.controller
        await c.ensure_conversation()
        original = c.conversation.id
        entered, release = asyncio.Event(), asyncio.Event()
        write = c._repo.set_workspace
        async def delayed(owner, grant):
            entered.set()
            await release.wait()
            await write(owner, grant)
        with patch.object(c._repo, 'set_workspace', side_effect=delayed):
            task = asyncio.create_task(c.set_workspace(str(self.root)))
            await asyncio.wait_for(entered.wait(), 5)
            await c.new_conversation()
            current = c.conversation
            release.set()
            with self.assertRaisesRegex(ChatBusy, 'expired'):
                await task
        self.assertIs(c.conversation, current)
        self.assertIsNone(current.workspace)
        self.assertEqual((await c._repo.get_conversation(original)).workspace,
                         authorize_directory(self.root))

    async def test_v1_database_migrates_without_losing_conversations_or_messages(self):
        from blackrose.storage.migrations import MIGRATIONS
        path = self.root / 'version1.db'
        database = Database(path)
        with patch('blackrose.storage.db.MIGRATIONS', MIGRATIONS[:1]):
            await database.connect()
            def seed(conn):
                stamp = '2026-10-15T12:00:00+00:00'
                conn.execute('INSERT INTO conversations(id, title, created_at, updated_at) VALUES(1, ?, ?, ?)',
                             ('Existing chat', stamp, stamp))
                conn.execute('INSERT INTO messages(conversation_id, role, content, created_at) VALUES(1, ?, ?, ?)',
                             ('user', 'Existing question', stamp))
                conn.commit()
            await database.run(seed)
            await database.close()
        await database.connect()
        try:
            repo = Repository(database)
            self.assertEqual((await repo.get_conversation(1)).title, 'Existing chat')
            self.assertIsNone((await repo.get_conversation(1)).workspace)
            messages = await repo.messages(1)
            self.assertEqual(messages[0].content, 'Existing question')
            self.assertEqual(messages[0].refs, [])
            from blackrose.storage.migrations import MIGRATIONS
            self.assertEqual(await database.schema_version(), MIGRATIONS[-1][0])
        finally:
            await database.close()

    async def test_multiple_references_are_pointers_and_persist(self):
        work = await self.workspace()
        (work / 'notes.md').write_text('SECRET_CONTENT_READ_ONLY_ON_DEMAND')
        (work / 'data.json').write_text('{"answer": 42}')
        (work / 'folder').mkdir()
        c = self.backend(['Done'])
        refs = await c.reference_suggestions('@', '')
        await c.send('Compare the selected references', refs=refs)
        request = c.backend.requests[0][-1]['content']
        self.assertNotIn('SECRET_CONTENT_READ_ONLY_ON_DEMAND', request)
        self.assertIn(str(work / 'notes.md'), request)
        messages = await c.current_messages()
        self.assertEqual(len(messages[0].refs), 3)
        self.assertEqual({r['kind'] for r in messages[0].refs}, {'file', 'directory'})

    async def test_reference_cannot_authorize_outside_or_changed_workspace(self):
        work = await self.workspace()
        (work / 'note.md').write_text('inside')
        c = self.backend(['Done'])
        ref = (await c.reference_suggestions('@', 'note'))[0]
        outside = self.root / 'secret.txt'
        outside.write_text('outside')
        with self.assertRaises(ValueError):
            await c.send('read', refs=[{**ref, 'path': str(outside)}])
        await c.set_workspace(None)
        with self.assertRaises(ValueError):
            await c.send('read', refs=[ref])
        self.assertFalse(await c.current_messages())

    async def test_explicit_skill_is_loaded_without_model_tool_call_and_resources_stay_lazy(self):
        c = self.backend(['Done'])
        skill = c.skills.root / 'animation-explainer'
        skill.mkdir()
        (skill / 'SKILL.md').write_text('---\nname: Animation Explainer\ndescription: Explain with animations\n---\nEXPLICIT_INSTRUCTIONS_SENTINEL')
        (skill / 'reference.md').write_text('RESOURCE_NOT_PRELOADED')
        self.assertEqual(await c.reference_suggestions('$', 'ani'), [])  # Unreviewed.
        c.skills.approve('animation-explainer', c.skills.review('animation-explainer')['digest'])
        refs = await c.reference_suggestions('$', 'ani')
        self.assertEqual(refs[0]['id'], 'animation-explainer')
        await c.send('$animation-explainer Explain a queue', refs=refs[:1])
        system, request = (c.backend.requests[0][i]['content'] for i in (0, -1))
        # Selected instructions travel with the request, outside the system message.
        self.assertNotIn('EXPLICIT_INSTRUCTIONS_SENTINEL', system)
        self.assertIn('EXPLICIT_INSTRUCTIONS_SENTINEL', request)
        self.assertNotIn('RESOURCE_NOT_PRELOADED', json.dumps(c.backend.requests[0]))
        self.assertEqual((await c.current_messages())[0].refs, [{'kind': 'skill', 'id': 'animation-explainer'}])
        c.skills.set_enabled('animation-explainer', False)
        with self.assertRaisesRegex(ValueError, 'disabled'):
            await c.send('again', refs=refs[:1])

    async def test_ordinary_skill_discovery_stays_metadata_only(self):
        c = self.backend(['Done'])
        skill = c.skills.root / 'example'
        skill.mkdir()
        (skill / 'SKILL.md').write_text('---\nname: Example\ndescription: Useful metadata\n---\nLAZY_BODY')
        c.skills.approve('example', c.skills.review('example')['digest'])
        await c.send('Hello')
        system, request = (c.backend.requests[0][i]['content'] for i in (0, -1))
        self.assertNotIn('Useful metadata', system)
        self.assertIn('Useful metadata', request)
        self.assertNotIn('LAZY_BODY', json.dumps(c.backend.requests[0]))

    async def test_multi_step_read_recover_and_create_real_artifacts(self):
        work = await self.workspace()
        (work / 'notes.md').write_text('# Evidence\nWorkspace evidence')
        c = self.backend([
            [('file_read', {'path': '../secret'})],
            [('file_read', {'path': 'notes.md'})],
            [('document_create', {'path': 'summary.md', 'content': '# Summary\nWorkspace evidence'}),
             ('document_create', {'path': 'summary.html', 'content': '<h1>Summary</h1>'}),
             ('document_create', {'path': 'summary.pdf', 'content': '# Summary\nWorkspace evidence'}),
             ('document_create', {'path': 'helper.py', 'content': 'raise RuntimeError("NEVER EXECUTED")'})],
            'Four files saved.',
        ])
        turn = await c.send('Summarize my notes and create outputs')
        first_error = json.loads(next(m['content'] for m in c.backend.requests[1] if m['role'] == 'tool'))
        self.assertEqual(first_error['code'], 'outside_workspace')
        self.assertIn('Workspace evidence', str(c.backend.requests[2]))
        creates = {p['path']: token for token, p in c.pending_edits.items() if p['operation'] == 'create'}
        self.assertEqual(list(creates), [str(work / name) for name in
                                         ('summary.md', 'summary.html', 'summary.pdf', 'helper.py')])
        self.assertEqual([p.name for p in work.iterdir()], ['notes.md'])
        review = await c.review_edit(creates[str(work / 'summary.pdf')])
        self.assertEqual((review['operation'], review['before'], review['after']),
                         ('create', '', '# Summary\nWorkspace evidence'))
        for token in creates.values():
            await c.review_edit(token, approve=True)
        self.assertEqual(c.pending_edits, {})
        self.assertEqual(len((await c.current_messages())[-1].attachments), 4)
        self.assertTrue((work / 'summary.pdf').read_bytes().startswith(b'%PDF-'))
        self.assertIn('Workspace evidence', PdfReader(work / 'summary.pdf').pages[0].extract_text())
        self.assertEqual((work / 'summary.html').read_text(), '<h1>Summary</h1>')
        self.assertEqual((work / 'summary.md').read_text(), '# Summary\nWorkspace evidence')
        self.assertFalse((work / 'helper.py').stat().st_mode & 0o111)
        self.assertEqual(turn.sources[0]['origin'], 'Local file read')
        await self.session.render_transcript()
        payload = self.latest('transcript')['messages'][-1]['attachments']
        self.assertEqual(payload[0]['kind'], 'artifact')
        self.assertEqual(payload[0]['path'], str(work / 'summary.md'))
        # Reloading a chat advertises outputs without pasting their entire text.
        c.backend = Backend(['Done'])
        await c.send('Thanks')
        self.assertIn('Saved artifact:', str(c.backend.requests[0]))

    async def test_web_native_rag_and_workspace_tools_combine_into_cited_pdf(self):
        import httpx
        from unittest.mock import AsyncMock
        from blackrose.knowledge import KnowledgeService
        from blackrose.web.access import WebAccess
        from blackrose.web.fetcher import PageFetcher
        from blackrose.web.transport import PublicNetworkBackend, PublicTransport
        from local_knowledge.config import Config as KnowledgeConfig
        from test_retriever import FakeProvider
        from test_web import RecordingNetwork, addresses, response

        work = await self.workspace()
        (work / 'local.md').write_text('Local database notes')
        c = self.runtime.controller
        knowledge = KnowledgeService(str(self.root / 'unused.toml'))
        knowledge._config = KnowledgeConfig(database_path=str(self.root / 'knowledge.db'),
                                           dimensions=128, chunk_tokens=32, chunk_overlap=4)
        knowledge._provider = FakeProvider()
        source = self.root / 'indexed.md'
        source.write_text('# Database\n\nDatabase transactions preserve records.\n')
        await knowledge.call('add', path=str(source))
        hits = await knowledge.call('search', query='database', limit=1)
        citation = hits['results'][0]['citation']
        c.knowledge = knowledge
        c.config.knowledge_enabled = True
        c.config.web_enabled = True
        search_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={'results': [{'url': 'https://public.example/article',
                'title': 'Public article', 'content': 'Database public evidence'}]})))
        web = WebAccess('https://search.example', client=search_client)
        await web.fetcher.aclose()
        network = RecordingNetwork([response(body=b'Database public page evidence')])
        resolver = AsyncMock(return_value=addresses('93.184.216.34'))
        transport = PublicTransport(network_backend=PublicNetworkBackend(network, resolver))
        page_client = httpx.AsyncClient(transport=transport, trust_env=False)
        web.fetcher = PageFetcher(client=page_client)
        c.web = web
        content = '# Cited report\n\nLocal notes and indexed records, plus public evidence.\n\n' + citation + '\n\nhttps://public.example/article'
        c.backend = Backend([
            [('knowledge_search', {'query': 'database', 'limit': 1}),
             ('file_read', {'path': 'local.md'}),
             ('web_search', {'query': 'database'})],
            [('fetch_content', {'url': 'https://public.example/article'})],
            [('document_create', {'path': 'cited.pdf', 'content': content})],
            'Cited report saved.',
        ])
        asked = []
        async def approve(request):
            asked.append(request)
            return True
        try:
            turn = await c.send('Combine my notes, the library and the web in a cited report',
                                approve_outbound=approve)
            # The search follows local reads in the same round, so it is approved explicitly.
            self.assertEqual([(r.kind, r.target) for r in asked], [('search', 'database')])
            self.assertFalse((work / 'cited.pdf').exists())
            await c.review_edit(next(iter(c.pending_edits)), approve=True)
            pdf = PdfReader(work / 'cited.pdf').pages[0].extract_text()
            self.assertIn(' '.join(citation.split()), ' '.join(pdf.split()))
            self.assertIn('https://public.example/article', pdf)
            self.assertEqual(len(turn.sources), 4)
            self.assertIn('Database public page evidence', str(c.backend.requests[2]))
            self.assertEqual(network.connections, [('93.184.216.34', 443)])
        finally:
            await page_client.aclose()

    async def test_output_folder_default_and_collision_and_only_own_outputs_accessible(self):
        downloads = self.root / 'downloads' / 'Blackrose'
        c = self.backend([
            [('document_create', {'path': 'report.txt', 'content': 'first'})],
            [('document_create', {'path': 'report.txt', 'content': 'second'})], 'Saved.',
        ])
        with patch('blackrose.workspace.downloads_directory', return_value=downloads.parent):
            turn = await c.send('Create two reports')
            self.assertEqual((downloads / 'report.txt').read_text(), 'first')
            self.assertEqual((downloads / 'report (1).txt').read_text(), 'second')
            items = await c.reference_suggestions('@', 'report')
            self.assertEqual(len(items), 2)
            self.assertEqual(len(turn.assistant_message.attachments), 2)

    async def test_edit_is_blocked_until_review_backup_and_stale_checks(self):
        work = await self.workspace()
        target = work / 'notes.md'
        target.write_text('before')
        c = self.backend([[('file_edit', {'path': 'notes.md', 'content': 'after'})], 'Please approve.'])
        await c.send('Improve my notes')
        token = next(iter(c.pending_edits))
        self.assertEqual(target.read_text(), 'before')
        review = await c.review_edit(token)
        self.assertEqual((review['before'], review['after']), ('before', 'after'))
        result = await c.review_edit(token, approve=True)
        self.assertEqual(target.read_text(), 'after')
        self.assertEqual(Path(result['backup']).read_text(), 'before')
        self.assertEqual((await c.current_messages())[-1].attachments[-1].output_path, str(target))
        with self.assertRaises(ValueError):
            await c.review_edit(token, approve=True)
        c.backend = Backend([[('file_edit', {'path': 'notes.md', 'content': 'next'})], 'Approve'])
        await c.send('Revise')
        token = next(iter(c.pending_edits))
        target.write_text('changed by user')
        with self.assertRaisesRegex(FileAccessError, 'changed'):
            await c.review_edit(token, approve=True)
        self.assertEqual(target.read_text(), 'changed by user')

    async def test_published_edit_conflict_expires_approval(self):
        from blackrose import workspace
        work = await self.workspace()
        target = work / 'notes.md'
        target.write_text('before')
        c = self.backend([[('file_edit', {'path': 'notes.md', 'content': 'after'})], 'Please approve.'])
        await c.send('Improve my notes')
        token = next(iter(c.pending_edits))
        exchange = workspace._exchange

        def race(fd, source, destination):
            target.write_text('external edit')
            exchange(fd, source, destination)

        with patch('blackrose.workspace._exchange', side_effect=race):
            with self.assertRaises(FileAccessError) as raised:
                await c.review_edit(token, approve=True)
        self.assertEqual(raised.exception.code, 'file_changed_published')
        self.assertIn(str(target), str(raised.exception))
        backup = Path(raised.exception.details['backup'])
        self.assertTrue(backup.is_relative_to(c.edit_backups.root))
        self.assertIn(str(backup), str(raised.exception))
        self.assertEqual(target.read_text(), 'after')
        self.assertEqual(backup.read_text(), 'external edit')
        self.assertEqual([p.name for p in work.iterdir()], ['notes.md'])
        record = (await c.edit_backups.listing(c.conversation))['items'][0]
        self.assertEqual((record['location'], record['conflict']), (str(backup), True))
        self.assertNotIn(token, c.pending_edits)
        self.assertFalse(c.is_generating)
        with self.assertRaisesRegex(ValueError, 'expired'):
            await c.review_edit(token, approve=True)

    async def test_selected_reference_identity_is_revalidated_before_acceptance(self):
        work = await self.workspace()
        target = work / 'notes.md'
        target.write_text('old')
        c = self.backend(['Done'])
        reference = (await c.reference_suggestions('@', 'notes'))[0]
        target.rename(work / 'previous.md')
        target.write_text('new file at the same name')
        with self.assertRaisesRegex(ValueError, 'changed'):
            await c.send('read', refs=[reference])
        self.assertFalse(await c.current_messages())

    async def test_multiple_pending_edits_remain_reviewable_after_partial_approval(self):
        work = await self.workspace()
        (work / 'first.md').write_text('before')
        (work / 'second.md').write_text('before')
        c = self.backend([[('file_edit', {'path': 'first.md', 'content': 'first'}),
                           ('file_edit', {'path': 'second.md', 'content': 'second'})], 'Review both.'])
        await self.session.submit('Edit both')
        await self.session._generation
        tokens = list(c.pending_edits)
        await c.review_edit(tokens[0], approve=True)
        self.events.clear()
        await self.session.render_transcript()
        pending_events = [p for k, p in self.events if k == 'edit_approval']
        self.assertEqual([p['approval_id'] for p in pending_events], tokens[1:])
        await c.review_edit(tokens[1], reject=True)
        self.assertEqual((work / 'second.md').read_text(), 'before')

    async def test_invalid_pdf_attempt_reports_failure_without_fake_artifact(self):
        await self.workspace()
        c = self.backend([[('document_create', {'path': '../report.pdf', 'content': '# No'})], 'Saved!'])
        with self.assertRaisesRegex(RuntimeError, 'no output was saved'):
            await c.send('Create PDF')
        self.assertFalse((await c.current_messages())[-1].attachments)

    async def test_cancellation_settles_inflight_output_and_preserves_attachment(self):
        work = self.root / 'downloads' / 'Blackrose'
        self.enterContext(patch('blackrose.workspace.downloads_directory', return_value=work.parent))
        c = self.backend([[('document_create', {'path': 'saved.md', 'content': '# Saved'})], 'Done'])
        started = threading.Event()
        release = threading.Event()
        original = WorkspaceFiles.write
        def blocked(files, *args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            return original(files, *args, **kwargs)
        updates = []
        with patch.object(WorkspaceFiles, 'write', blocked):
            task = asyncio.create_task(c.send('Save', on_images=updates.append))
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((work / 'saved.md').read_text(), '# Saved')
        self.assertEqual(len((await c.current_messages())[-1].attachments), 1)
        self.assertEqual(updates[-1][-1].filename, 'saved.md')
