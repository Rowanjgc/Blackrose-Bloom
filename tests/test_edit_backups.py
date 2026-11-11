"""Approved-edit backups live outside project folders, durably recorded and restorable."""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from blackrose import workspace
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository
from blackrose.workspace import FileAccessError, WorkspaceFiles, authorize_directory, replacement_stage
import test_gui_images as fixtures
from test_turns import Backend


def digest(data):
    return hashlib.sha256(data).hexdigest()


class StoreTests(unittest.TestCase):
    """Filesystem contract of the replacement's move into the backup store."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.backups = self.root / 'data' / 'edit-backups'
        self.files = WorkspaceFiles(authorize_directory(self.work), self.root / 'downloads',
                                    backups=self.backups, backup_group='7')

    def replace(self, name, old, new, mode=0o640):
        path = self.work / name
        path.write_bytes(old)
        path.chmod(mode)
        return path, self.files.write(name, new, collision='replace', expected_digest=digest(old), reviewed=True)

    def test_original_moves_to_private_store_with_its_mode(self):
        path, result = self.replace('notes.md', b'old', b'new', mode=0o640)
        backup = Path(result['backup'])
        self.assertEqual(path.read_bytes(), b'new')
        self.assertEqual(backup.read_bytes(), b'old')
        self.assertEqual(backup.parent, self.backups / '7')
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        for folder in (self.backups, self.backups / '7'):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual((result['backup_digest'], result['backup_conflict'], result['backup_stored']),
                         (digest(b'old'), False, True))
        self.assertEqual([p.name for p in self.work.iterdir()], ['notes.md'])

    def test_cross_filesystem_store_copies_syncs_then_unlinks(self):
        move = workspace._move_entry
        events = []

        def cross_device(source_fd, source, destination_fd, destination):
            if source_fd != destination_fd:
                raise OSError(errno.EXDEV, 'Invalid cross-device link')
            return move(source_fd, source, destination_fd, destination)

        fsync, unlink, copy = os.fsync, os.unlink, workspace._copy_entry
        original = b'secret = 1\n' * 1000

        def record_copy(*args):
            events.append('copy')
            return copy(*args)

        def record_fsync(fd):
            events.append('fsync')
            return fsync(fd)

        def record_unlink(name, *args, **kwargs):
            if name.endswith('-edit'):
                stored = [p.read_bytes() for p in (self.backups / '7').iterdir() if not p.name.startswith('.')]
                events.append(('unlink stage', stored))
            return unlink(name, *args, **kwargs)

        with patch('blackrose.workspace._move_entry', side_effect=cross_device), \
                patch('blackrose.workspace._copy_entry', side_effect=record_copy), \
                patch('blackrose.workspace.os.fsync', side_effect=record_fsync), \
                patch('blackrose.workspace.os.unlink', side_effect=record_unlink):
            path, result = self.replace('settings.toml', original, b'secret = 2\n', mode=0o600)
        backup = Path(result['backup'])
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertTrue(result['backup_stored'])
        self.assertNotIn('backup_warning', result)
        self.assertEqual([p.name for p in self.work.iterdir()], ['settings.toml'])
        # The complete copy and the store directory are synced before the project entry goes.
        start = events.index('copy')
        [(index, (_, stored))] = [(i, e) for i, e in enumerate(events) if isinstance(e, tuple)]
        self.assertEqual(stored, [original])
        self.assertEqual(events[start:index].count('fsync'), 2)
        self.assertEqual([p.name for p in (self.backups / '7').iterdir()], [backup.name])

    def test_store_failure_keeps_original_beside_and_reports_its_path(self):
        with patch('blackrose.workspace.store_entry', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            path, result = self.replace('.env', b'TOKEN=old\n', b'TOKEN=new\n', mode=0o600)
        kept = self.work / '.env.backup'
        self.assertEqual(result['backup'], str(kept))
        self.assertFalse(result['backup_stored'])
        self.assertIn(str(kept), result['backup_warning'])
        self.assertIn('No space left', result['backup_warning'])
        self.assertEqual(kept.read_bytes(), b'TOKEN=old\n')
        self.assertEqual(path.read_bytes(), b'TOKEN=new\n')

    def test_store_and_rename_failure_still_names_the_retained_entry(self):
        with patch('blackrose.workspace.store_entry', side_effect=OSError(errno.EACCES, 'denied')), \
                patch('blackrose.workspace.keep_beside', side_effect=lambda fd, entry, name, full: str(full.with_name(entry))):
            path, result = self.replace('notes.md', b'old', b'new')
        self.assertEqual(Path(result['backup']).read_bytes(), b'old')
        self.assertIn(result['backup'], result['backup_warning'])
        # A retained replacement stage is hidden from listings but never collected as abandoned.
        stage = Path(result['backup'])
        os.utime(stage, (1, 1))
        stage.chmod(0o600)
        workspace._CLEANED_STAGE_DIRS.clear()
        self.files.write('other.md', b'x', reviewed=True)
        self.assertTrue(stage.exists())
        self.assertEqual([e['filename'] for e in self.files.browse()['entries']], ['notes.md', 'other.md'])

    def test_file_tools_cannot_reach_the_store(self):
        _, result = self.replace('notes.md', b'old', b'new')
        files = WorkspaceFiles(authorize_directory(self.root), self.root / 'downloads', backups=self.backups)
        with self.assertRaisesRegex(FileAccessError, 'not workspace files'):
            files.read(result['backup'])

    def test_crash_after_exchange_is_recovered_from_the_named_stage(self):
        path = self.work / 'notes.md'
        path.write_text('new')
        stage = self.work / replacement_stage('a' * 32)
        stage.write_text('old')
        stored = workspace.recover_replacement_stage(path, 'a' * 32, self.backups, '7', digest(b'new'))
        self.assertFalse(stage.exists())
        self.assertEqual(Path(stored['location']).read_text(), 'old')
        # A stage that holds the approved replacement was never published.
        stage.write_text('new')
        self.assertIsNone(workspace.recover_replacement_stage(path, 'a' * 32, self.backups, '7', digest(b'new')))
        self.assertFalse(stage.exists())


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def edit(self, work, name, content):
        c = self.runtime.controller
        await c.set_workspace(str(work))
        c.backend = Backend([[('file_edit', {'path': name, 'content': content})], 'Please review.'])
        await c.send('Edit ' + name)
        await c.cancel_title()
        token = next(t for t, p in c.pending_edits.items() if p['data'] == content.encode())
        return c, await c.review_edit(token, approve=True)

    async def test_secret_edit_in_git_repository_leaves_only_the_edited_file(self):
        if not shutil.which('git'):
            self.skipTest('git is unavailable')
        work = self.root / 'repo'
        work.mkdir()
        git = ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-C', str(work)]
        subprocess.run([*git, 'init', '-q'], check=True)
        (work / '.gitignore').write_text('.env\n')
        (work / '.env').write_text('API_TOKEN=old-secret\n')
        (work / '.env').chmod(0o600)
        subprocess.run([*git, 'add', '.gitignore'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'init'], check=True)
        c, result = await self.edit(work, '.env', 'API_TOKEN=new-secret\n')
        status = subprocess.run([*git, 'status', '--porcelain', '--ignored'], check=True,
                                capture_output=True, text=True).stdout
        self.assertEqual(status.splitlines(), ['!! .env'])
        self.assertEqual(sorted(p.name for p in work.iterdir()), ['.env', '.git', '.gitignore'])
        backup = Path(result['backup'])
        self.assertTrue(backup.is_relative_to(self.root / 'edit-backups' / str(c.conversation.id)))
        self.assertEqual(backup.read_text(), 'API_TOKEN=old-secret\n')
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        [record] = (await c.edit_backups.listing(c.conversation))['items']
        self.assertEqual((record['path'], record['location'], record['conflict']), (str(work / '.env'), str(backup), False))
        row = await c._repo.edit_backup(record['id'])
        self.assertEqual(row['digest'], digest(b'API_TOKEN=old-secret\n'))
        self.assertEqual(row['mode'], 0o600)

    async def test_restore_round_trip_is_guarded_and_itself_reversible(self):
        work = self.root / 'restore'
        work.mkdir()
        target = work / 'notes.md'
        target.write_bytes(b'before\r\n')
        c, result = await self.edit(work, 'notes.md', 'after\n')
        backup_id = result['backup_id']
        target.write_text('newer outside change')
        with self.assertRaisesRegex(FileAccessError, 'changed after this edit'):
            await c.edit_backup_action('restore', backup_id)
        self.assertEqual(target.read_text(), 'newer outside change')
        target.write_text('after\n')
        preview = await c.edit_backup_action('preview', backup_id)
        self.assertEqual(preview['text'], 'before\r\n')
        restored = await c.edit_backup_action('restore', backup_id)
        self.assertEqual(target.read_bytes(), b'before\r\n')
        self.assertEqual(Path(restored['backup']).read_text(), 'after\n')
        items = {i['id']: i for i in (await c.edit_backup_action('list'))['items']}
        self.assertIsNotNone(items[backup_id]['restored_at'])
        self.assertIn(restored['backup_id'], items)
        # Another conversation cannot use this conversation's backup.
        first = c.conversation.id
        await c.new_conversation()
        with self.assertRaisesRegex(ValueError, 'Open the conversation'):
            await c.edit_backup_action('restore', backup_id)
        await c.open_conversation(first)
        # A backup whose bytes no longer match its record is refused.
        Path(result['backup']).write_text('tampered')
        target.write_text('after\n')
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            await c.edit_backup_action('restore', backup_id)

    async def test_records_survive_restart_and_conversation_deletion(self):
        work = self.root / 'persist'
        work.mkdir()
        (work / 'notes.md').write_text('before')
        c, result = await self.edit(work, 'notes.md', 'after')
        conversation = c.conversation.id
        await c.delete_current_conversation()
        db = Database(self.runtime.database.path)
        await db.connect()
        try:
            repository = Repository(db)
            [row] = await repository.edit_backups()
            self.assertEqual((row['conversation_id'], row['location'], row['state']),
                             (conversation, result['backup'], 'saved'))
            self.assertEqual([r['id'] for r in await repository.orphaned_edit_backups()], [row['id']])
        finally:
            await db.close()
        self.assertEqual(Path(result['backup']).read_text(), 'before')
        # From another chat it is listed, previewable and removable, but never restored.
        [orphan] = (await c.edit_backup_action('list'))['deleted_chats']
        self.assertEqual((await c.edit_backup_action('preview', orphan['id']))['text'], 'before')
        with self.assertRaisesRegex(ValueError, 'Open the conversation'):
            await c.edit_backup_action('restore', orphan['id'])
        self.assertEqual(await c.edit_backup_action('remove', orphan['id']), {'removed': True})
        self.assertFalse(Path(result['backup']).exists())

    async def test_interrupted_replacement_is_settled_at_startup(self):
        c = self.runtime.controller
        work = self.root / 'crash'
        work.mkdir()
        (work / 'notes.md').write_text('after')
        conversation = (await c.ensure_conversation()).id
        await c._repo.begin_edit_backup('b' * 32, conversation, str(work / 'notes.md'), 'token',
                                        digest(b'before'), digest(b'after'))
        (work / replacement_stage('b' * 32)).write_text('before')
        await c._repo.begin_edit_backup('c' * 32, conversation, str(work / 'notes.md'), 'token',
                                        digest(b'before'), digest(b'after'))
        await c.recover_interrupted()
        self.assertEqual([p.name for p in work.iterdir()], ['notes.md'])
        [row] = await c._repo.edit_backups(conversation)
        self.assertEqual((row['id'], row['conflict']), ('b' * 32, 0))
        self.assertEqual(Path(row['location']).read_text(), 'before')
        self.assertEqual(await c._repo.edit_backups(state='pending'), [])

    async def test_retention_keeps_conflicts_and_removal_is_explicit(self):
        work = self.root / 'retention'
        work.mkdir()
        (work / 'a.md').write_text('a-before')
        c, plain = await self.edit(work, 'a.md', 'a-after')
        (work / 'b.md').write_text('b-before')
        exchange = workspace._exchange

        def race(fd, source, destination):
            (work / 'b.md').write_text('b-external')
            exchange(fd, source, destination)

        c.backend = Backend([[('file_edit', {'path': 'b.md', 'content': 'b-after'})], 'Review.'])
        await c.send('Edit b')
        token = next(t for t, p in c.pending_edits.items() if p['data'] == b'b-after')
        with patch('blackrose.workspace._exchange', side_effect=race), self.assertRaises(FileAccessError) as raised:
            await c.review_edit(token, approve=True)
        conflict = raised.exception.details
        await self.runtime.database.run(lambda conn: (
            conn.execute("UPDATE edit_backups SET created_at='2000-01-01T00:00:00.000000+00:00'"), conn.commit()))
        self.assertEqual(await c.edit_backup_action('prune'), {'removed': 1, 'kept': 1, 'days': 30})
        self.assertFalse(Path(plain['backup']).exists())
        self.assertEqual(Path(conflict['backup']).read_text(), 'b-external')
        [item] = (await c.edit_backup_action('list'))['items']
        self.assertTrue(item['conflict'])
        self.assertEqual(await c.edit_backup_action('remove', item['id']), {'removed': True})
        self.assertFalse(Path(conflict['backup']).exists())
        self.assertEqual((await c.edit_backup_action('list'))['items'], [])


if __name__ == '__main__':
    unittest.main()
