"""SM-11: workspace selection, review tokens, output journal and edit-backup recovery.

The specification is `docs/state-machines/sm-11-workspace-files.md`. These tests
pin the guards, terminal states and crash-recovery paths that are cheap to check
offline: every file operation runs in a temporary directory, and controller tests
use the shared fake runtime whose XDG directories are already scratch.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from blackrose.chat import ChatBusy
from blackrose.edit_backups import EditBackups
from blackrose.library import Capture, LibraryProposals
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository
from blackrose.workspace import FileAccessError, WorkspaceFiles, authorize_directory, replacement_stage
import test_gui_images as fixtures
from test_turns import Backend


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    return Path(path).read_bytes()


def read_text(path):
    return Path(path).read_text()


class EditBackupCrashTests(unittest.IsolatedAsyncioTestCase):
    """The on-disk/DB pairs `EditBackups.recover()` must settle or refuse."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.store = self.root / 'store'
        self.db = Database(self.root / 'test.db')
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.files = WorkspaceFiles(authorize_directory(self.work), self.root / 'downloads', backups=self.store)
        self.edits = EditBackups(await self.repository(), self.store)

    async def repository(self):
        self.repo = Repository(self.db)
        self.conversation = await self.repo.create_conversation()
        return self.repo

    async def pending(self, backup_id, *, old=b'old', new=b'new', name='notes.md'):
        """A row written before publication, with the file already holding the replacement."""
        target = self.work / name
        target.write_bytes(new)
        await self.repo.begin_edit_backup(backup_id, self.conversation.id, str(target), 'token',
                                          digest(old), digest(new))
        return target, self.work / replacement_stage(backup_id)

    async def test_pending_row_without_a_stage_or_store_copy_is_dropped(self):
        target, stage = await self.pending('a' * 32)
        self.assertEqual(await self.edits.recover(), 0)
        self.assertIsNone(await self.repo.edit_backup('a' * 32))
        self.assertEqual(target.read_bytes(), b'new')
        self.assertFalse(stage.exists())
        self.assertFalse(self.store.exists())

    async def test_stage_holding_the_approved_replacement_is_removed_as_unpublished(self):
        target, stage = await self.pending('b' * 32)
        stage.write_bytes(b'new')
        self.assertEqual(await self.edits.recover(), 0)
        self.assertIsNone(await self.repo.edit_backup('b' * 32))
        self.assertFalse(stage.exists())
        self.assertEqual(target.read_bytes(), b'new')
        # Nothing was published, so no backup may be invented from the replacement.
        self.assertFalse(self.store.exists())

    async def test_exchanged_stage_is_moved_to_the_store_and_settled(self):
        target, stage = await self.pending('c' * 32)
        stage.write_bytes(b'old')
        self.assertEqual(await self.edits.recover(), 1)
        row = await self.repo.edit_backup('c' * 32)
        self.assertEqual((row['state'], row['stored'], row['conflict'], row['digest']),
                         ('saved', 1, 0, digest(b'old')))
        self.assertEqual(row['location'], str(self.store / str(self.conversation.id) / f'{"c" * 32}-notes.md'))
        self.assertFalse(stage.exists())
        self.assertEqual(read_bytes(row['location']), b'old')
        self.assertEqual(target.read_bytes(), b'new')

    async def test_displaced_content_that_changed_before_the_crash_is_a_conflict(self):
        _, stage = await self.pending('d' * 32)
        stage.write_bytes(b'external edit')
        self.assertEqual(await self.edits.recover(), 1)
        row = await self.repo.edit_backup('d' * 32)
        self.assertEqual((row['stored'], row['conflict'], row['digest']), (1, 1, digest(b'external edit')))
        self.assertEqual(read_bytes(row['location']), b'external edit')

    async def test_row_whose_copy_already_reached_the_store_settles_from_it(self):
        await self.pending('e' * 32)
        stored = self.store / str(self.conversation.id) / f'{"e" * 32}-notes.md'
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b'old')
        self.assertEqual(await self.edits.recover(), 1)
        row = await self.repo.edit_backup('e' * 32)
        self.assertEqual((row['state'], row['location'], row['digest']), ('saved', str(stored), digest(b'old')))

    async def test_leftover_stage_from_a_published_copy_is_removed_not_moved_again(self):
        # Regression: a cross-filesystem copy can publish the store file and leave
        # its source named as the stage. Moving it again collided with the stored
        # name, so the row stayed pending and the hidden copy stayed in the project.
        _, stage = await self.pending('f' * 32)
        stage.write_bytes(b'old')
        stored = self.store / str(self.conversation.id) / f'{"f" * 32}-notes.md'
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b'old')
        self.assertEqual(await self.edits.recover(), 1)
        row = await self.repo.edit_backup('f' * 32)
        self.assertEqual((row['state'], row['location'], row['stored']), ('saved', str(stored), 1))
        self.assertFalse(stage.exists())
        self.assertEqual(stored.read_bytes(), b'old')

    async def test_a_differing_leftover_stage_is_not_discarded(self):
        # The same collision with different bytes fails closed: the row stays
        # pending and both copies remain, rather than losing either version.
        _, stage = await self.pending('a1' * 16)
        stage.write_bytes(b'older still')
        stored = self.store / str(self.conversation.id) / f'{"a1" * 16}-notes.md'
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b'old')
        self.assertEqual(await self.edits.recover(), 0)
        row = await self.repo.edit_backup('a1' * 16)
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(stage.read_bytes(), b'older still')
        self.assertEqual(stored.read_bytes(), b'old')

    async def test_recovery_is_idempotent_and_never_duplicates_a_stored_backup(self):
        _, stage = await self.pending('b1' * 16)
        stage.write_bytes(b'old')
        self.assertEqual(await self.edits.recover(), 1)
        row = await self.repo.edit_backup('b1' * 16)
        entries = sorted(p.name for p in Path(row['location']).parent.iterdir())
        self.assertEqual(await self.edits.recover(), 0)
        self.assertEqual(await self.repo.edit_backup('b1' * 16), row)
        self.assertEqual(sorted(p.name for p in Path(row['location']).parent.iterdir()), entries)
        self.assertEqual(len(entries), 1)

    async def test_saved_records_are_never_revised_by_startup_recovery(self):
        await self.pending('c1' * 16)
        location = str(self.store / str(self.conversation.id) / f'{"c1" * 16}-notes.md')
        await self.repo.settle_edit_backup('c1' * 16, location=location, stored=True, digest=digest(b'old'),
                                           size=3, mode=0o640, conflict=False)
        await self.repo.mark_edit_backup_restored('c1' * 16)
        before = await self.repo.edit_backup('c1' * 16)
        self.assertEqual(await self.edits.recover(), 0)
        self.assertEqual(await self.repo.edit_backup('c1' * 16), before)
        self.assertIsNotNone(before['restored_at'])

    async def test_missing_store_group_does_not_block_removal(self):
        # Regression: removing or pruning a record whose store directory was
        # deleted raised FileNotFoundError, so the stale row could never go away.
        await self.pending('d1' * 16)
        location = str(self.store / str(self.conversation.id) / f'{"d1" * 16}-notes.md')
        await self.repo.settle_edit_backup('d1' * 16, location=location, stored=True, digest=digest(b'old'),
                                           size=3, mode=0o640, conflict=False)
        record = await self.repo.edit_backup('d1' * 16)
        with self.assertRaises(FileNotFoundError):
            await self.edits.read(record, self.files)
        self.assertEqual(await self.edits.remove(SimpleNamespace(id=self.conversation.id), 'd1' * 16),
                         {'removed': True})
        self.assertIsNone(await self.repo.edit_backup('d1' * 16))

    async def test_prune_forgets_records_whose_files_are_already_gone(self):
        await self.pending('e1' * 16)
        location = str(self.store / str(self.conversation.id) / f'{"e1" * 16}-notes.md')
        await self.repo.settle_edit_backup('e1' * 16, location=location, stored=True, digest=digest(b'old'),
                                           size=3, mode=0o640, conflict=False)
        await self.db.run(lambda conn: (conn.execute(
            "UPDATE edit_backups SET created_at='2000-01-01T00:00:00.000000+00:00'"), conn.commit()))
        self.assertEqual(await self.edits.prune(), {'removed': 1, 'kept': 0, 'days': 30})
        self.assertIsNone(await self.repo.edit_backup('e1' * 16))

    async def test_unwritable_store_leaves_the_row_pending_with_its_stage(self):
        _, stage = await self.pending('f1' * 16)
        stage.write_bytes(b'old')
        with patch('blackrose.workspace.store_entry', side_effect=OSError(errno.ENOSPC, 'No space left')):
            self.assertEqual(await self.edits.recover(), 0)
        self.assertEqual((await self.repo.edit_backup('f1' * 16))['state'], 'pending')
        self.assertEqual(stage.read_bytes(), b'old')


class WorkspaceSelectionTests(unittest.IsolatedAsyncioTestCase):
    """The selection token and the pending-edit review lifecycle."""

    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def edit(self, content='after', name='notes.md', work=None):
        c = self.runtime.controller
        work = work or self.root / 'ws'
        work.mkdir(exist_ok=True)
        await c.set_workspace(str(work))
        (work / name).write_text('before')
        c.backend = Backend([[('file_edit', {'path': name, 'content': content})], 'Review.'])
        await c.send('Edit ' + name)
        token = next(t for t, p in c.pending_edits.items() if p['data'] == content.encode())
        return c, work, token

    async def test_expiry_before_persist_leaves_no_grant_behind(self):
        c = self.runtime.controller
        await c.ensure_conversation()
        original = c.conversation.id
        work = self.root / 'chosen'
        work.mkdir()
        entered, release = threading.Event(), threading.Event()

        def authorize(path):
            entered.set()
            release.wait(5)
            return authorize_directory(path)

        with patch('blackrose.chat.authorize_directory', side_effect=authorize):
            task = asyncio.create_task(c.set_workspace(str(work)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                await c.new_conversation()
            finally:
                release.set()
            with self.assertRaisesRegex(ChatBusy, 'expired'):
                await task
        self.assertIsNone((await c._repo.get_conversation(original)).workspace)
        self.assertIsNone(c.conversation.workspace)

    async def test_expiry_after_the_persist_reports_failure_for_an_applied_grant(self):
        # Hazard H3: the grant is durable before the last check, so navigating
        # during the database write yields an error for a change that happened.
        c = self.runtime.controller
        await c.ensure_conversation()
        original = c.conversation.id
        work = self.root / 'chosen'
        work.mkdir()
        write = c._repo.set_workspace

        async def navigate_after_persist(conversation_id, grant):
            await write(conversation_id, grant)
            await c.new_conversation()

        with patch.object(c._repo, 'set_workspace', side_effect=navigate_after_persist):
            with self.assertRaisesRegex(ChatBusy, 'expired'):
                await c.set_workspace(str(work))
        self.assertEqual((await c._repo.get_conversation(original)).workspace, authorize_directory(work))
        self.assertIsNone(c.conversation.workspace)

    async def test_a_title_rename_does_not_expire_an_open_picker(self):
        # bra-lfou: the first reply's namer replaces the selected conversation
        # object for the same chat, which used to fail the identity check.
        c = self.runtime.controller
        c.backend.suggest_title = AsyncMock(return_value='Locks in SQLite')
        await c.new_conversation()
        original = c.conversation.id
        work = self.root / 'chosen'
        work.mkdir()
        selection = await c.begin_workspace_selection()
        await c._name_conversation(
            selection[1], SimpleNamespace(content='Why does the SQLite lock time out?', attachments=[]),
            SimpleNamespace(content='An answer'), c.model_name, None)
        self.assertEqual(c.conversation.id, original)
        self.assertIsNot(c.conversation, selection[1])
        self.assertEqual(c.conversation.title, 'Locks in SQLite')
        await c.set_workspace(str(work), selection=selection)
        self.assertEqual((await c._repo.get_conversation(original)).workspace, authorize_directory(work))
        self.assertEqual(c.conversation.workspace['path'], str(work))

    async def test_navigation_still_expires_a_picker_whose_chat_was_renamed(self):
        # The id comparison must not outlive navigation: leaving the chat and
        # returning to it is a different selection, rename or not.
        c = self.runtime.controller
        c.backend.suggest_title = AsyncMock(return_value='Locks in SQLite')
        await c.new_conversation()
        original = c.conversation.id
        work = self.root / 'chosen'
        work.mkdir()
        selection = await c.begin_workspace_selection()
        await c._name_conversation(
            selection[1], SimpleNamespace(content='Why does the SQLite lock time out?', attachments=[]),
            SimpleNamespace(content='An answer'), c.model_name, None)
        await c.new_conversation()
        await c.open_conversation(original)
        with self.assertRaisesRegex(ChatBusy, 'choose the directory again'):
            await c.set_workspace(str(work), selection=selection)
        self.assertIsNone((await c._repo.get_conversation(original)).workspace)

    async def test_workspace_change_expires_pending_edits_and_directory_grants(self):
        c, work, token = await self.edit()
        other = self.root / 'other'
        other.mkdir()
        c.directory_grants[c.conversation.id] = {'/granted': {'path': '/granted'}}
        await c.set_workspace(str(other))
        self.assertNotIn(token, c.pending_edits)
        self.assertNotIn(c.conversation.id, c.directory_grants)
        with self.assertRaisesRegex(ValueError, 'expired'):
            await c.review_edit(token, approve=True)
        self.assertEqual((work / 'notes.md').read_text(), 'before')

    async def test_a_token_belongs_to_its_conversation(self):
        c, _, token = await self.edit()
        original = c.conversation.id
        await c.new_conversation()
        with self.assertRaisesRegex(ValueError, 'expired'):
            await c.review_edit(token)
        await c.open_conversation(original)
        self.assertEqual((await c.review_edit(token))['before'], 'before')

    async def test_a_second_approval_is_refused_while_the_first_writes(self):
        c, work, token = await self.edit()
        entered, release = threading.Event(), threading.Event()
        write = WorkspaceFiles.write
        calls = []

        def blocked(files, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            calls.append(args[0])
            return write(files, *args, **kwargs)

        with patch.object(WorkspaceFiles, 'write', blocked):
            task = asyncio.create_task(c.review_edit(token, approve=True))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                with self.assertRaises(ChatBusy):
                    await c.review_edit(token, approve=True)
            finally:
                release.set()
            result = await task
        self.assertEqual(calls, ['notes.md'])
        self.assertNotIn(token, c.pending_edits)
        self.assertEqual((work / 'notes.md').read_text(), 'after')
        self.assertEqual(read_text(result['backup']), 'before')
        with self.assertRaisesRegex(ValueError, 'expired'):
            await c.review_edit(token, approve=True)

    async def test_reject_during_an_approval_loses_the_rejection(self):
        # Hazard H4: rejection pops the token without checking the busy latch, so
        # an approval already in flight still publishes the file. The library
        # machine refuses instead (`test_reject_while_committing_is_busy`).
        c, work, token = await self.edit()
        entered, release = threading.Event(), threading.Event()
        write = WorkspaceFiles.write

        def blocked(files, *args, **kwargs):
            entered.set()
            release.wait(5)
            return write(files, *args, **kwargs)

        with patch.object(WorkspaceFiles, 'write', blocked):
            task = asyncio.create_task(c.review_edit(token, approve=True))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                self.assertEqual(await c.review_edit(token, reject=True), {'rejected': True})
            finally:
                release.set()
            await task
        self.assertNotIn(token, c.pending_edits)
        self.assertEqual((work / 'notes.md').read_text(), 'after')

    async def test_an_external_change_refuses_approval_and_keeps_the_token(self):
        c, work, token = await self.edit()
        (work / 'notes.md').write_text('changed by user')
        with self.assertRaises(FileAccessError) as raised:
            await c.review_edit(token, approve=True)
        self.assertEqual(raised.exception.code, 'file_changed')
        self.assertIn(token, c.pending_edits)
        self.assertEqual((work / 'notes.md').read_text(), 'changed by user')
        # The token stays usable only while the reviewed original is what is there.
        (work / 'notes.md').write_text('before')
        await c.review_edit(token, approve=True)
        self.assertEqual((work / 'notes.md').read_text(), 'after')
        self.assertNotIn(token, c.pending_edits)

    async def test_navigation_during_an_approval_files_the_backup_under_the_new_chat(self):
        # Hazard H5: the token check binds the proposal to `self.conversation`,
        # but the publication re-reads it after two awaits. Opening an existing
        # chat with the same grant in between publishes the edit and records its
        # backup under the chat that is selected when the write finishes.
        c, work, token = await self.edit()
        first = c.conversation.id
        await c.new_conversation()
        second = c.conversation.id
        await c.set_workspace(str(work))
        await c.open_conversation(first)
        files = c.workspace_files

        async def navigate_after_files():
            built = await files()
            await c.open_conversation(second)
            return built

        with patch.object(c, 'workspace_files', navigate_after_files):
            result = await c.review_edit(token, approve=True)
        self.assertEqual((work / 'notes.md').read_text(), 'after')
        self.assertEqual(c.conversation.id, second)
        self.assertEqual((await c._repo.edit_backup(result['backup_id']))['conversation_id'], second)
        self.assertNotIn(token, c.pending_edits)

    async def test_an_edit_whose_metadata_failed_is_consumed_and_keeps_a_journal(self):
        c, work, token = await self.edit()
        with patch.object(c._repo, 'append_attachments', side_effect=OSError('disk full')):
            result = await c.review_edit(token, approve=True)
        self.assertTrue(result['artifact_pending'])
        self.assertEqual(read_text(result['backup']), 'before')
        self.assertRegex(result['recovery_id'], r'^[0-9a-f]{64}$')
        journal = c._store.root / 'output-recovery' / f'{result["recovery_id"]}.json'
        self.assertTrue(journal.exists())
        self.assertNotIn(token, c.pending_edits)
        self.assertEqual((work / 'notes.md').read_text(), 'after')


class OutputJournalTests(unittest.IsolatedAsyncioTestCase):
    """`output-recovery/<key>.json` bridges disk publication and attachment metadata."""

    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def output(self, content='saved', name='output.txt', fail=None):
        """Create one output through a real turn; ``fail`` patches a metadata step."""
        downloads = self.root / 'downloads'
        self.enterContext(patch('blackrose.workspace.downloads_directory', return_value=downloads))
        c = self.runtime.controller
        c.backend = Backend([[('document_create', {'path': name, 'content': content})], 'Saved.'])
        if fail:
            with patch.object(c._repo, fail, side_effect=OSError('disk full')):
                turn = await c.send('Create the output')
        else:
            turn = await c.send('Create the output')
        result = json.loads(next(m['content'] for m in c.backend.requests[-1] if m['role'] == 'tool'))
        return c, turn, downloads / 'Blackrose' / name, result

    def journal(self, c):
        directory = c._store.root / 'output-recovery'
        return sorted(directory.glob('*.json')) if directory.exists() else []

    async def test_a_successful_output_leaves_one_attachment_and_no_journal(self):
        c, turn, path, result = await self.output()
        self.assertEqual(path.read_text(), 'saved')
        self.assertIn('attachment_id', result)
        self.assertEqual(self.journal(c), [])
        self.assertEqual(len(await c._repo.attachments_for(turn.assistant_message.id)), 1)

    async def test_a_journal_replays_metadata_without_writing_the_file_again(self):
        c, turn, path, result = await self.output(name='kept.txt', content='kept', fail='append_attachments')
        self.assertTrue(result['artifact_pending'])
        [journal] = self.journal(c)
        self.assertEqual(journal.name, f'{result["recovery_id"]}.json')
        files = await c.workspace_files()
        with patch.object(WorkspaceFiles, 'write', side_effect=AssertionError('replayed the disk write')):
            recovered = await c._save_output(files, turn.assistant_message, 'kept.txt', b'kept')
        self.assertIn('attachment_id', recovered)
        self.assertEqual(self.journal(c), [])
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ['kept.txt'])
        self.assertEqual(len(await c._repo.attachments_for(turn.assistant_message.id)), 1)

    async def test_recovery_is_bound_to_its_conversation_and_workspace(self):
        work = self.root / 'bound'
        work.mkdir()
        c = self.runtime.controller
        await c.set_workspace(str(work))
        c.backend = Backend([[('document_create', {'path': 'bound.txt', 'content': 'bound'})], 'Review.'])
        turn = await c.send('Create the output')
        token = next(iter(c.pending_edits))
        with patch.object(c._repo, 'append_attachments', side_effect=OSError('disk full')):
            result = await c.review_edit(token, approve=True)
        self.assertTrue(result['artifact_pending'])
        journal = result['recovery_id']
        original = c.conversation.id
        await c.new_conversation()
        with self.assertRaisesRegex(ValueError, 'original conversation'):
            await c.recover_output(journal)
        await c.open_conversation(original)
        await c.set_workspace(None)
        with self.assertRaisesRegex(ValueError, 'original workspace'):
            await c.recover_output(journal)
        await c.set_workspace(str(work))
        recovered = await c.recover_output(journal)
        self.assertIn('attachment_id', recovered)
        self.assertEqual(self.journal(c), [])
        self.assertEqual((work / 'bound.txt').read_text(), 'bound')
        # Metadata lands on the reply that produced the file, not a later turn.
        self.assertEqual(len(await c._repo.attachments_for(turn.assistant_message.id)), 1)

    async def test_a_replay_does_not_duplicate_an_attachment_that_already_exists(self):
        # The attachment row, not the journal, is the record of success: a replay
        # matches it by reply, path and digest instead of appending a second one.
        downloads = self.root / 'downloads'
        self.enterContext(patch('blackrose.workspace.downloads_directory', return_value=downloads))
        c = self.runtime.controller
        c.backend = Backend([[('document_create', {'path': 'once.txt', 'content': 'once'})], 'Saved.'])
        unlink = Path.unlink

        def keep_journal(path, *args, **kwargs):
            if path.name.endswith('.json'):
                raise OSError('could not remove the record')
            return unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', keep_journal):
            turn = await c.send('Create the output')
        result = json.loads(next(m['content'] for m in c.backend.requests[-1] if m['role'] == 'tool'))
        self.assertTrue(result['artifact_pending'])
        self.assertEqual(len(await c._repo.attachments_for(turn.assistant_message.id)), 1)
        files = await c.workspace_files()
        replayed = await c._save_output(files, turn.assistant_message, 'once.txt', b'once')
        self.assertIn('attachment_id', replayed)
        self.assertEqual(len(await c._repo.attachments_for(turn.assistant_message.id)), 1)
        self.assertEqual(self.journal(c), [])

    async def test_recovery_refuses_changed_bytes_and_keeps_the_record(self):
        c, _, path, result = await self.output(name='changed.txt', fail='append_attachments')
        path.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            await c.recover_output(result['recovery_id'])
        self.assertTrue((c._store.root / 'output-recovery' / f'{result["recovery_id"]}.json').exists())

    async def test_recovery_ids_are_validated_before_any_read(self):
        c = self.runtime.controller
        for value in ('', '0' * 63, 'g' * 64, '../' + '0' * 61, 7):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Invalid output recovery ID'):
                await c.recover_output(value)


class LibraryProposalTests(unittest.IsolatedAsyncioTestCase):
    """Library review tokens: committed once, refused after consumption."""

    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    class Knowledge:
        def __init__(self, error=None):
            self.calls = []
            self.error = error

        async def call(self, operation, **arguments):
            self.calls.append((operation, arguments))
            if self.error:
                raise RuntimeError(self.error)
            return {'status': 'imported', 'document_id': 1}

        async def close(self):
            pass

    async def proposal(self, c, text='# Database\nNotes.'):
        _, assistant = await c._repo.begin_turn(c.conversation.id, 'Save this', c.model_name)
        capture = c.library_proposals.web_capture(
            {'url': 'https://example.org/x', 'text': text, 'truncated': False,
             'retrieved_at': '2026-10-15T12:00:00+00:00'}, assistant)
        return c.library_proposals.propose(c.conversation.id, capture)['approval_id']

    async def library(self, error=None):
        c = self.runtime.controller
        await c.ensure_conversation()
        c.config.knowledge_enabled = True
        c.knowledge = self.Knowledge(error)
        return c, await self.proposal(c)

    async def test_approval_commits_the_snapshot_once(self):
        c, token = await self.library()
        result = await c.review_library_add(token, approve=True)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['title'], 'https://example.org/x')
        self.assertNotIn(token, c.library_proposals.pending)
        [(operation, arguments)] = c.knowledge.calls
        self.assertEqual(operation, 'capture')
        self.assertEqual(arguments['proposal'].capture.text, '# Database\nNotes.')
        with self.assertRaisesRegex(ValueError, 'expired'):
            await c.review_library_add(token, approve=True)

    async def test_rejection_is_terminal_and_foreign_tokens_are_refused(self):
        c, token = await self.library()
        original = c.conversation.id
        await c.new_conversation()
        with self.assertRaisesRegex(ValueError, 'another conversation'):
            await c.review_library_add(token, approve=True)
        await c.open_conversation(original)
        self.assertEqual(await c.review_library_add(token, reject=True), {'status': 'rejected'})
        self.assertEqual(c.knowledge.calls, [])
        for approve in (True, False):
            with self.subTest(approve=approve), self.assertRaisesRegex(ValueError, 'expired'):
                await c.review_library_add(token, approve=approve)

    async def test_a_failed_commit_keeps_the_token_and_clears_the_latch(self):
        c, token = await self.library()
        c.knowledge.error = 'embedding unavailable'
        with self.assertRaisesRegex(RuntimeError, 'embedding unavailable'):
            await c.review_library_add(token, approve=True)
        self.assertIn(token, c.library_proposals.pending)
        self.assertFalse(c.is_generating)
        c.knowledge.error = None
        self.assertEqual((await c.review_library_add(token, approve=True))['status'], 'imported')
        self.assertNotIn(token, c.library_proposals.pending)

    async def test_reject_while_committing_is_busy(self):
        c, token = await self.library()
        c.is_generating = True
        try:
            for kwargs in ({'approve': True}, {'reject': True}):
                with self.subTest(**kwargs), self.assertRaises(ChatBusy):
                    await c.review_library_add(token, **kwargs)
        finally:
            c.is_generating = False
        self.assertIn(token, c.library_proposals.pending)
        self.assertEqual(c.knowledge.calls, [])

    async def test_dedup_cap_and_conversation_discard(self):
        proposals = LibraryProposals()
        capture = Capture('https://example.org/x', '# Notes', 'Notes', 'https://example.org/x',
                          (('document_type', 'web_capture'), ('capture_completeness', 'complete')))
        first = proposals.propose(1, capture)
        self.assertEqual(proposals.propose(1, capture)['approval_id'], first['approval_id'])
        for index in range(7):
            proposals.propose(1, capture, f'Title {index}')
        with self.assertRaisesRegex(ValueError, 'pending'):
            proposals.propose(1, capture, 'Overflow')
        with self.assertRaisesRegex(ValueError, 'another conversation'):
            proposals.get(first['approval_id'], 2)
        proposals.discard_conversation(1)
        self.assertEqual(proposals.pending, {})


def lambda_parse(message):
    pass


if __name__ == '__main__':
    unittest.main()
