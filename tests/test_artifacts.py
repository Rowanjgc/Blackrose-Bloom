"""Offline lifecycle failures, crash retries and conservative artifact repair."""
import asyncio
from contextlib import closing, redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from blackrose import artifact_cli
from blackrose.errors import ChatBusy
from blackrose.instance import desktop_instance
from blackrose.storage.db import Database
from blackrose.storage.migrations import MIGRATIONS
from blackrose.storage.repository import NewAttachment
import test_gui_images as fixtures


class ArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.runtime = fixtures.make_runtime(self.root)
        await self.runtime.open()
        self.c = self.runtime.controller
        await self.c.ensure_conversation()
        self.store = self.c._store

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def seed(self, data=b'original document', text=b'extracted text', conversation=None):
        blob = self.store.write(data, '.md')
        extracted = self.store.write(text, '.txt') if text else None
        row = NewAttachment('text/markdown', 'notes.md', blob.relative_path, blob.digest, blob.size,
                            kind='document', text_path=extracted.relative_path if extracted else None)
        await self.c._repo.add_message(conversation or self.c.conversation.id, 'user', 'Read this', attachments=[row])
        return blob.relative_path, extracted.relative_path if extracted else None

    async def repair(self):
        with desktop_instance(self.root / 'desktop.lock') as ownership:
            return await self.c.repair_artifacts(ownership)

    def attachment_bytes(self):
        return {p.relative_to(self.store.root).as_posix(): p.read_bytes()
                for p in self.store.root.rglob('*') if p.is_file()}

    async def test_failed_linkage_is_reported_and_explicitly_repaired(self):
        pending = self.c.attach(fixtures.png(), 'fail.png')
        await self.runtime.database.run(lambda conn: conn.executescript('''
            CREATE TRIGGER reject_attachment BEFORE INSERT ON message_attachments
            BEGIN SELECT RAISE(ABORT, 'injected linkage failure'); END;'''))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.c.send('Describe', attachments=[pending])
        self.assertFalse(self.c.last_request_stored)
        self.assertEqual(await self.c.current_messages(), [])
        before = self.attachment_bytes()
        report = await self.c.artifact_report()
        self.assertEqual(report['blobs']['unreferenced'], list(before))
        self.assertEqual(before, self.attachment_bytes())
        result = await self.repair()
        self.assertFalse(result['cleanup']['pending'])
        self.assertFalse(self.attachment_bytes())

    async def test_partial_persistence_failure_reports_original_and_extracted_blobs(self):
        first = self.c.prepare_attachment(b'# First\n\nDocument one.', 'one.md')
        second = self.c.prepare_attachment(b'# Second\n\nDocument two.', 'two.md')
        write = self.store.write
        def fail_second(data, suffix):
            if data == second.data:
                raise OSError('injected disk failure')
            return write(data, suffix)
        with patch.object(self.store, 'write', side_effect=fail_second):
            with self.assertRaises(OSError):
                await self.c.send('Summarize', attachments=[first, second])
        report = await self.c.artifact_report()
        self.assertEqual(len(report['blobs']['unreferenced']), 2)
        self.assertFalse(await self.c.current_messages())
        await self.repair()
        self.assertFalse(self.attachment_bytes())

    async def test_transaction_journal_survives_restart_between_row_and_file_deletion(self):
        paths = await self.seed()
        deleted = self.c.conversation.id
        await self.c.animation_recovery.claim(deleted, 10, 'bad frame')
        draft = self.store.root / 'illustration-drafts' / str(deleted) / '10/draft-1.json'
        draft.parent.mkdir(parents=True)
        draft.write_text('{"revision":1}')
        # Simulate process death immediately after the database transaction.
        await self.c._repo.delete_conversation(deleted)
        self.assertTrue(await self.c._repo.cleanup_jobs())
        self.assertTrue(all(self.store.exists(p) for p in paths))
        await self.runtime.close()
        _, self.runtime = fixtures.make_runtime(self.root)
        await self.runtime.open()
        self.c = self.runtime.controller
        self.assertFalse(await self.c._repo.cleanup_jobs())
        self.assertFalse(any(self.store.exists(p) for p in paths))
        self.assertFalse(draft.exists())
        self.assertEqual(await self.c.animation_recovery.states(deleted), {})
        self.assertFalse((await self.c.retry_artifact_cleanup())['completed'])

    async def test_delete_transaction_rolls_back_rows_and_obligations_together(self):
        paths = await self.seed()
        await self.runtime.database.run(lambda conn: conn.executescript('''
            CREATE TRIGGER reject_delete BEFORE DELETE ON conversations
            BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END;'''))
        with self.assertRaises(sqlite3.IntegrityError):
            await self.c.delete_current_conversation()
        self.assertFalse(await self.c._repo.cleanup_jobs())
        self.assertEqual(len(await self.c.current_messages()), 1)
        self.assertTrue(all(self.store.exists(p) for p in paths))

    async def test_controller_deletes_recovery_and_drafts_but_preserves_external_output(self):
        paths = await self.seed()
        deleted = self.c.conversation.id
        await self.c.animation_recovery.claim(deleted, 42, 'bad')
        await self.c.animation_recovery.finish(deleted, 42, 'replaced', 43)
        external = self.root / 'Downloads/report.md'
        external.parent.mkdir()
        external.write_text('external output')
        await self.runtime.database.run(lambda conn: conn.execute(
            'UPDATE message_attachments SET output_path=?', (str(external),)))
        # Commit the fixture before the lifecycle transaction.
        await self.runtime.database.run(lambda conn: conn.commit())
        draft = self.store.root / 'illustration-drafts' / str(deleted) / '42/draft-1-r1.json'
        draft.parent.mkdir(parents=True)
        draft.write_text('private draft')
        await self.c.delete_current_conversation()
        self.assertFalse(any(self.store.exists(p) for p in paths))
        self.assertFalse(draft.exists())
        self.assertFalse(await self.c.animation_recovery.states(deleted))
        self.assertEqual(external.read_text(), 'external output')
        self.assertFalse(self.c.cleanup_result['pending'])

    async def test_shared_original_and_cross_column_text_survive_until_last_owner(self):
        original, text = await self.seed()
        other = await self.c._repo.create_conversation('other')
        blob = self.store.write(b'another', '.md')
        await self.c._repo.add_message(other.id, 'user', 'keep', attachments=[
            NewAttachment('text/plain', 'extracted.txt', text, 'unused', 10),
            NewAttachment('text/markdown', 'another.md', blob.relative_path, blob.digest, blob.size,
                          text_path=original)])
        await self.c.delete_current_conversation()
        self.assertTrue(self.store.exists(original))
        self.assertTrue(self.store.exists(text))
        self.assertFalse(self.c.cleanup_result['pending'])
        await self.c.delete_current_conversation()
        self.assertFalse(any(self.store.exists(p) for p in (original, text, blob.relative_path)))

    async def test_failed_unlink_is_visible_and_durable_until_retry(self):
        original, text = await self.seed()
        delete = self.store.delete
        with patch.object(self.store, 'delete', side_effect=lambda p: False if p == original else delete(p)):
            with self.assertLogs('blackrose.chat', level='WARNING'):
                await self.c.delete_current_conversation()
        pending = self.c.cleanup_result['pending']
        self.assertEqual([(j['kind'], j['target']) for j in pending], [('blob', original)])
        self.assertIn('retry', pending[0]['error'])
        self.assertFalse(self.store.exists(text))
        await self.runtime.database.close()
        await self.runtime.database.connect()
        self.assertEqual(await self.c._repo.cleanup_jobs(), pending)
        self.assertFalse((await self.c.retry_artifact_cleanup())['pending'])
        self.assertFalse(self.store.exists(original))

    async def test_failed_auxiliary_cleanup_does_not_hide_deletion_and_can_retry(self):
        deleted = self.c.conversation.id
        await self.c.animation_recovery.claim(deleted, 1, 'bad')
        with patch.object(self.c.animation_recovery, 'delete', side_effect=sqlite3.OperationalError('locked')):
            with self.assertLogs('blackrose.chat', level='WARNING'):
                await self.c.delete_current_conversation()
        self.assertIsNone(await self.c._repo.get_conversation(deleted))
        self.assertEqual(self.c.cleanup_result['pending'][0]['kind'], 'recovery')
        self.assertFalse((await self.c.retry_artifact_cleanup())['pending'])
        self.assertFalse(await self.c.animation_recovery.states(deleted))

    async def test_cancelled_delete_waits_for_worker_then_leaves_consistent_view(self):
        paths = await self.seed()
        deleted = self.c.conversation.id
        started, release = threading.Event(), threading.Event()
        delete = self.store.delete
        def blocked(path):
            started.set()
            if not release.wait(5):
                raise TimeoutError('worker not released')
            return delete(path)
        with patch.object(self.store, 'delete', side_effect=blocked):
            task = asyncio.create_task(self.c.delete_current_conversation())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                with self.assertRaises(ChatBusy):
                    await self.c.send('during cleanup')
                with self.assertRaises(ChatBusy):
                    await self.c.new_conversation()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertNotEqual(self.c.conversation.id, deleted)
        self.assertFalse(any(self.store.exists(p) for p in paths))
        self.assertFalse(await self.c._repo.cleanup_jobs())

    async def test_cancelled_persistence_keeps_repair_excluded_until_writer_stops(self):
        pending = self.c.attach(fixtures.png(), 'slow.png')
        started, release = threading.Event(), threading.Event()
        persist = self.c._persist_attachments
        def blocked(items):
            result = persist(items)
            started.set()
            if not release.wait(5):
                raise TimeoutError('writer not released')
            return result
        with patch.object(self.c, '_persist_attachments', side_effect=blocked):
            task = asyncio.create_task(self.c.send('Describe', attachments=[pending]))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                with desktop_instance(self.root / 'desktop.lock') as ownership:
                    with self.assertRaises(ChatBusy):
                        await self.c.repair_artifacts(ownership)
                self.assertTrue(self.attachment_bytes())
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(await self.c.current_messages())
        await self.repair()
        self.assertFalse(self.attachment_bytes())

    async def test_reconciliation_distinguishes_blobs_partials_drafts_and_recovery(self):
        original, text = await self.seed()
        unused = self.store.write(b'orphan', '.md').relative_path
        final = self.store.path_for(unused)
        partial = final.with_name('.' + final.name + '.partial')
        partial.write_bytes(b'interrupted write')
        unknown = self.store.root / 'unrecognized.txt'
        unknown.write_text('keep')
        draft = self.store.root / 'illustration-drafts/999/1/draft-1.json'
        draft.parent.mkdir(parents=True)
        draft.write_text('orphan draft')
        await self.c.animation_recovery.claim(999, 9, 'orphan recovery')
        await self.c.animation_recovery.claim(self.c.conversation.id, 1, 'live recovery')
        before = self.attachment_bytes()
        report = await self.c.artifact_report()
        self.assertEqual(self.attachment_bytes(), before)
        self.assertEqual(report['blobs']['unreferenced'], [unused])
        self.assertEqual(report['drafts']['orphaned'], [999])
        self.assertEqual(report['recovery']['orphaned'], [999])
        self.assertEqual(report['unrecognized'], ['unrecognized.txt'])
        self.assertEqual(len(report['partials']), 1)
        result = await self.repair()
        self.assertFalse(result['after']['blobs']['unreferenced'])
        self.assertFalse(partial.exists())
        self.assertFalse(draft.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(self.store.exists(original))
        self.assertTrue(self.store.exists(text))
        self.assertEqual(await self.c.animation_recovery.states(999), {})
        self.assertTrue(await self.c.animation_recovery.states(self.c.conversation.id))

    async def test_retry_rechecks_new_references_and_protects_noncanonical_aliases(self):
        original, _ = await self.seed(text=None)
        await self.c._repo.queue_cleanup([('blob', original)])
        alias = original[:2] + '/./' + original[3:]
        await self.runtime.database.run(lambda conn: conn.execute(
            'UPDATE message_attachments SET storage_path=?', (alias,)))
        await self.runtime.database.run(lambda conn: conn.commit())
        self.assertFalse((await self.c.artifact_report())['blobs']['unreferenced'])
        result = await self.c.retry_artifact_cleanup()
        self.assertEqual(result['preserved'][0]['target'], original)
        self.assertTrue(self.store.exists(original))

    async def test_missing_files_and_corrupt_recovery_are_reported_without_inference(self):
        original, _ = await self.seed()
        self.store.delete(original)
        self.c.animation_recovery.path.write_bytes(b'not sqlite')
        report = await self.c.artifact_report()
        self.assertEqual(report['blobs']['missing'], [original])
        self.assertTrue(report['recovery']['error'])
        result = await self.repair()
        self.assertTrue(result['after']['recovery']['error'])
        self.assertEqual(self.c.animation_recovery.path.read_bytes(), b'not sqlite')

    async def test_symlinked_owned_directories_and_escaped_rows_never_delete_external_files(self):
        outside = self.root / 'outside'
        outside.mkdir()
        secret = outside / 'secret.txt'
        secret.write_text('keep')
        (self.store.root / 'illustration-drafts').parent.mkdir(parents=True, exist_ok=True)
        (self.store.root / 'illustration-drafts').symlink_to(outside, target_is_directory=True)
        (self.store.root / 'animation-recovery.sqlite3').symlink_to(secret)
        await self.c._repo.queue_cleanup([('blob', '../outside/secret.txt'),
            ('drafts', '999'), ('recovery', '999')])
        result = await self.repair()
        self.assertEqual(len(result['cleanup']['pending']), 3)
        self.assertEqual(secret.read_text(), 'keep')

    async def test_extensionless_and_unicode_suffix_artifacts_are_owned_blobs(self):
        paths = [self.store.write(b'no extension', '').relative_path,
                 self.store.write(b'unicode extension', '.笔记').relative_path]
        report = await self.c.artifact_report()
        self.assertEqual(report['blobs']['unreferenced'], sorted(paths))
        await self.repair()
        self.assertFalse(any(self.store.exists(p) for p in paths))

    async def test_recovery_sidecar_symlinks_are_refused_before_sqlite_can_open_them(self):
        await self.c.animation_recovery.claim(999, 1, 'orphan')
        external = self.root / 'external-journal'
        external.write_text('external bytes')
        (self.store.root / 'animation-recovery.sqlite3-journal').symlink_to(external)
        await self.c._repo.queue_cleanup([('recovery', '999')])
        report = await self.c.artifact_report()
        self.assertTrue(report['recovery']['error'])
        result = await self.repair()
        self.assertEqual(result['cleanup']['pending'][0]['kind'], 'recovery')
        self.assertEqual(external.read_text(), 'external bytes')

    async def test_approved_edit_excludes_repair_until_its_attachment_is_linked(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        output = workspace / 'README'
        output.write_text('before')
        await self.c.set_workspace(str(workspace))
        assistant = await self.c._repo.add_message(self.c.conversation.id, 'assistant', 'Proposed edit')
        proposal = await self.c.file_tool('file_edit', {'path': 'README', 'content': 'after'}, assistant)
        started, release = threading.Event(), threading.Event()
        write = self.store.write
        def blocked(data, suffix):
            started.set()
            if not release.wait(5):
                raise TimeoutError('edit writer not released')
            return write(data, suffix)
        with patch.object(self.store, 'write', side_effect=blocked):
            task = asyncio.create_task(self.c.review_edit(proposal['approval_id'], approve=True))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                with desktop_instance(self.root / 'desktop.lock') as ownership:
                    with self.assertRaises(ChatBusy):
                        await self.c.repair_artifacts(ownership)
                with self.assertRaises(ChatBusy):
                    await self.c.delete_current_conversation()
            finally:
                release.set()
            await task
        self.assertFalse((await self.c.artifact_report())['blobs']['unreferenced'])
        await self.c.delete_current_conversation()
        self.assertEqual(output.read_text(), 'after')
        self.assertFalse(self.c.cleanup_result['pending'])

    async def test_unlink_before_acknowledgement_can_be_repeated_after_interruption(self):
        original, _ = await self.seed(text=None)
        await self.c._repo.delete_conversation(self.c.conversation.id)
        finish = self.c._repo.finish_cleanup
        async def interrupt(kind, target, error=None):
            if kind == 'blob':
                raise asyncio.CancelledError
            return await finish(kind, target, error)
        with patch.object(self.c._repo, 'finish_cleanup', side_effect=interrupt):
            with self.assertRaises(asyncio.CancelledError):
                await self.c.retry_artifact_cleanup()
        self.assertFalse(self.store.exists(original))
        self.assertTrue(await self.c._repo.cleanup_jobs())
        self.assertFalse((await self.c.retry_artifact_cleanup())['pending'])

    async def test_session_reports_partial_cleanup_without_undoing_conversation_deletion(self):
        from blackrose.gui.session import Session
        await self.seed()
        events = []
        session = Session(self.c, lambda kind, payload: events.append((kind, payload)))
        try:
            with patch.object(self.store, 'delete', return_value=False), self.assertLogs('blackrose.chat'):
                self.assertTrue(await session.delete_conversation(self.c.conversation.id))
            notices = [p for kind, p in events if kind == 'notice']
            self.assertTrue(any('retry on restart' in json.dumps(p) for p in notices))
        finally:
            await session.close()

    async def test_repair_requires_active_ownership_of_the_same_database(self):
        blob = self.store.write(b'keep', '.txt')
        with desktop_instance(self.root / 'other/desktop.lock') as wrong:
            with self.assertRaises(RuntimeError):
                await self.c.repair_artifacts(wrong)
        with desktop_instance(self.root / 'desktop.lock') as expired:
            pass
        with self.assertRaises(RuntimeError):
            await self.c.repair_artifacts(expired)
        self.assertTrue(self.store.exists(blob.relative_path))

    async def test_cli_dry_run_never_migrates_or_changes_artifacts_and_repair_refuses_live_desktop(self):
        await self.seed()
        before = self.attachment_bytes()
        # The CLI's production filename differs from the injected fixture.
        with patch.object(artifact_cli, 'data_dir', return_value=self.root):
            missing_output = StringIO()
            with redirect_stderr(missing_output):
                self.assertEqual(await artifact_cli.run(), 1)
            self.assertFalse((self.root / 'chats.db').exists())
        await self.runtime.close()
        path = self.root / 'chats.db'
        (self.root / 'test.db').rename(path)
        with desktop_instance(self.root / 'desktop.lock'):
            pass
        files_before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with patch.object(artifact_cli, 'data_dir', return_value=self.root):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(await artifact_cli.run(), 0)
            self.assertFalse(json.loads(output.getvalue())['blobs']['unreferenced'])
            self.assertEqual(before, self.attachment_bytes())
            self.assertEqual(files_before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
            with desktop_instance(self.root / 'desktop.lock'), redirect_stderr(StringIO()):
                self.assertEqual(await artifact_cli.run('repair'), 1)

    async def test_cli_wal_snapshot_reads_committed_rows_without_touching_source(self):
        # Keep the fixture connection open so committed data is still in its WAL.
        import shutil
        path = self.root / 'chats.db'
        await self.seed()
        for suffix in ('', '-wal'):
            shutil.copyfile(Path(str(self.runtime.database.path) + suffix), Path(str(path) + suffix))
        with desktop_instance(self.root / 'desktop.lock'):
            pass
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with patch.object(artifact_cli, 'data_dir', return_value=self.root), redirect_stdout(StringIO()) as output:
            self.assertEqual(await artifact_cli.run(), 0)
        self.assertEqual(len(json.loads(output.getvalue())['blobs']['referenced']), 2)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})


class CleanupMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_populated_schema_three_upgrades_and_reopens_without_changing_saved_data(self):
        from blackrose.storage.repository import Repository
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'chats.db'
            database = Database(path)
            with patch('blackrose.storage.db.MIGRATIONS', MIGRATIONS[:3]):
                await database.connect()
            repo = Repository(database)
            conversation = await repo.create_conversation('saved')
            user, reply = await repo.begin_turn(conversation.id, 'request', 'model',
                [NewAttachment('text/plain', 'note.txt', 'blob', 'digest', 10, text_path='extracted')])
            await repo.save_evidence(reply.id, [{'title': 'citation'}])
            await database.close()
            with desktop_instance(path.parent / 'desktop.lock'):
                before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
                snapshot = artifact_cli.read_inventory(path)
                self.assertEqual(snapshot[1], {'blob', 'extracted'})
                self.assertFalse(snapshot[2])
                self.assertEqual(before, {p.name: p.read_bytes() for p in path.parent.iterdir()})
            for _ in range(2):
                await database.connect()
                self.assertEqual(await database.schema_version(), MIGRATIONS[-1][0])
                self.assertEqual([m.id for m in await repo.messages(conversation.id)], [user.id, reply.id])
                self.assertEqual((await repo.attachments_for(user.id))[0].text_path, 'extracted')
                self.assertEqual(await repo.evidence_for(reply.id), [{'title': 'citation'}])
                self.assertFalse(await repo.cleanup_jobs())
                self.assertFalse(await database.run(lambda conn: conn.execute('PRAGMA foreign_key_check').fetchall()))
                await database.close()

    async def test_failed_schema_four_upgrade_preserves_rows_and_can_retry(self):
        from blackrose.storage.db import StorageError
        from blackrose.storage.repository import Repository
        from blackrose.storage.snapshot import readonly_snapshot
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'chats.db'
            database = Database(path)
            with patch('blackrose.storage.db.MIGRATIONS', MIGRATIONS[:3]):
                await database.connect()
            repo = Repository(database)
            conversation = await repo.create_conversation('saved')
            await repo.add_message(conversation.id, 'user', 'retained')
            await database.run(lambda conn: conn.executescript('''
                CREATE TRIGGER reject_cleanup_version BEFORE INSERT ON schema_metadata
                WHEN NEW.version=4 BEGIN SELECT RAISE(ABORT, 'injected upgrade failure'); END;'''))
            await database.close()
            with self.assertRaises(StorageError):
                await database.connect()
            with readonly_snapshot(path) as conn:
                self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_metadata').fetchone()[0], 3)
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_cleanup'").fetchone())
                self.assertEqual(conn.execute('SELECT content FROM messages').fetchone()[0], 'retained')
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute('DROP TRIGGER reject_cleanup_version')
            await database.connect()
            self.assertEqual(await database.schema_version(), MIGRATIONS[-1][0])
            self.assertEqual((await repo.messages(conversation.id))[0].content, 'retained')
            await database.close()
