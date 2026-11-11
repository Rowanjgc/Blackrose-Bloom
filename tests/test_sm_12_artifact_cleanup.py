"""SM-12: durable cleanup obligations, reconciliation buckets and exclusion.

These pin the `artifact_cleanup` row lifecycle, the classification rules of
`reconciliation` and the fail-fast lifecycle lock. They are offline and use
their own temporary data directory.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from blackrose.animation_recovery import AnimationRecovery
from blackrose.artifacts import ArtifactLifecycle
from blackrose.attachments import AttachmentStore
from blackrose.errors import ChatBusy
from blackrose.instance import desktop_instance
from blackrose.storage.db import Database
from blackrose.storage.repository import NewAttachment, Repository
import test_gui_images as fixtures


class LifecycleFixture(unittest.IsolatedAsyncioTestCase):
    """A repository, a store and a lifecycle with no GUI or provider."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = Database(self.root / 'chats.db')
        await self.db.connect()
        self.repo = Repository(self.db)
        self.store = AttachmentStore(self.root / 'attachments')
        self.recovery = AnimationRecovery(self.store.root / 'animation-recovery.sqlite3')
        self.lifecycle = ArtifactLifecycle(self.repo, self.store, self.recovery)

    async def asyncTearDown(self):
        await self.db.close()
        self.temporary.cleanup()

    async def seed(self, title='chat', data=b'original', storage_path=None):
        """One user message whose attachment owns one blob."""
        conversation = await self.repo.create_conversation(title)
        blob = self.store.write(data, '.md')
        await self.repo.add_message(conversation.id, 'user', 'Read this', attachments=[
            NewAttachment('text/markdown', 'notes.md', storage_path or blob.relative_path,
                          blob.digest, blob.size, kind='document')])
        return conversation, blob.relative_path

    async def repair(self):
        with desktop_instance(self.root / 'desktop.lock') as ownership:
            return await self.lifecycle.repair(ownership)

    def relative(self, path):
        return path.relative_to(self.store.root).as_posix()

    def partial_of(self, blob):
        path = self.store.path_for(blob)
        return path.with_name('.' + path.name + '.partial')

    @staticmethod
    def of_kind(result, key, kind):
        return [job['target'] for job in result[key] if job['kind'] == kind]


class QueueTests(LifecycleFixture):
    """`artifact_cleanup` rows: Queued -> completed | preserved | failed."""

    async def test_exclusive_refuses_a_second_lifecycle_command(self):
        self.assertFalse(self.lifecycle.busy)
        async with self.lifecycle.exclusive():
            self.assertTrue(self.lifecycle.busy)
            with self.assertRaises(ChatBusy):
                async with self.lifecycle.exclusive():
                    self.fail('nested exclusive must be refused, not queued')
        self.assertFalse(self.lifecycle.busy)
        async with self.lifecycle.exclusive():
            self.assertTrue(self.lifecycle.busy)

    async def test_queued_obligations_complete_and_their_rows_disappear(self):
        _, referenced = await self.seed()
        orphan = self.store.write(b'orphan', '.txt').relative_path
        partial = self.partial_of(orphan)
        partial.write_bytes(b'interrupted write')
        partial_target = self.relative(partial)
        await self.repo.queue_cleanup([('blob', orphan), ('partial', partial_target)])
        self.assertEqual(len(await self.repo.cleanup_jobs()), 2)
        result = await self.lifecycle.retry()
        self.assertEqual(self.of_kind(result, 'completed', 'blob'), [orphan])
        self.assertEqual(self.of_kind(result, 'completed', 'partial'), [partial_target])
        self.assertFalse(result['preserved'])
        self.assertFalse(result['pending'])
        self.assertFalse(await self.repo.cleanup_jobs())
        self.assertFalse(self.store.exists(orphan))
        self.assertFalse(partial.exists())
        self.assertTrue(self.store.exists(referenced))

    async def test_preserved_obligation_is_rearmed_by_the_next_delete(self):
        first, shared = await self.seed('first', data=b'shared bytes')
        second, also_shared = await self.seed('second', data=b'shared bytes')
        self.assertEqual(shared, also_shared)
        await self.repo.delete_conversation(first.id)
        preserving = await self.lifecycle.retry()
        self.assertEqual(self.of_kind(preserving, 'preserved', 'blob'), [shared])
        self.assertTrue(self.store.exists(shared))
        self.assertFalse(await self.repo.cleanup_jobs())
        await self.repo.delete_conversation(second.id)
        final = await self.lifecycle.retry()
        self.assertEqual(self.of_kind(final, 'completed', 'blob'), [shared])
        self.assertFalse(self.store.exists(shared))

    async def test_referenced_noncanonical_target_is_preserved_not_failed(self):
        """A damaged alias owns its bytes through the surviving canonical row."""
        first, blob = await self.seed('damaged', data=b'alias bytes')
        alias = blob[:2] + '/./' + blob[3:]

        def damage(conn):
            with conn:
                conn.execute('UPDATE message_attachments SET storage_path = ?', (alias,))
        await self.db.run(damage)
        await self.seed('survivor', data=b'alias bytes')
        await self.repo.delete_conversation(first.id)
        queued = {(row['kind'], row['target']) for row in await self.repo.cleanup_jobs()}
        self.assertIn(('blob', alias), queued)
        result = await self.lifecycle.retry()
        self.assertEqual(self.of_kind(result, 'preserved', 'blob'), [alias])
        self.assertFalse(result['pending'])
        self.assertTrue(self.store.exists(blob))
        self.assertEqual(self.store.read(blob), b'alias bytes')

    async def test_unrecognized_unreferenced_target_is_refused_and_retained(self):
        self.store.root.mkdir(parents=True)
        stranger = self.store.root / 'notes.txt'
        stranger.write_text('keep')
        await self.repo.queue_cleanup([('blob', 'notes.txt')])
        result = await self.lifecycle.retry()
        self.assertFalse(result['completed'])
        self.assertEqual([job['target'] for job in result['pending']], ['notes.txt'])
        self.assertIn('Unrecognized blob layout', result['pending'][0]['error'])
        self.assertEqual(stranger.read_text(), 'keep')
        # Refusal is deterministic: a second drain changes nothing and hides nothing.
        self.assertEqual(await self.lifecycle.retry(), result)

    async def test_failed_acknowledgement_after_unlink_is_reported_and_retried(self):
        conversation, blob = await self.seed(data=b'deleted before acknowledgement')
        # The bytes are gone and the owning rows are gone; only the acknowledgement
        # of the recorded obligation is missing.
        self.assertTrue(self.store.delete(blob))
        await self.repo.delete_conversation(conversation.id)
        acknowledge = self.repo.finish_cleanup
        refused = []

        async def refuse_once(kind, target, error=None):
            if error is None and not refused:
                refused.append(True)
                raise sqlite3.OperationalError('database is locked')
            return await acknowledge(kind, target, error)

        with patch.object(self.repo, 'finish_cleanup', side_effect=refuse_once):
            result = await self.lifecycle.retry()
        # The effect succeeded, so the obligation is reported as completed *and*
        # as still recorded; the next drain treats the absent file as success.
        self.assertEqual(self.of_kind(result, 'completed', 'blob'), [blob])
        self.assertEqual([job['target'] for job in result['pending']], [blob])
        self.assertIn('locked', result['pending'][0]['error'])
        final = await self.lifecycle.retry()
        self.assertFalse(final['pending'])
        self.assertFalse(await self.repo.cleanup_jobs())

    async def test_ambiguous_conversation_identities_are_refused_without_side_effects(self):
        draft = self.store.root / 'illustration-drafts' / '7' / '1' / 'draft-1.json'
        draft.parent.mkdir(parents=True)
        draft.write_text('{}')
        await self.repo.queue_cleanup([
            ('drafts', '007'), ('drafts', '0'), ('drafts', '-1'), ('recovery', '7.0')])
        result = await self.lifecycle.retry()
        self.assertFalse(result['completed'])
        self.assertEqual(len(result['pending']), 4)
        self.assertTrue(all(job['error'] for job in result['pending']))
        self.assertTrue(draft.exists())

    async def test_drain_leaves_each_row_completed_or_recorded_with_its_error(self):
        conversation = await self.repo.create_conversation('live')
        await self.repo.add_message(conversation.id, 'user', 'keep me')
        blob = self.store.write(b'retained', '.md').relative_path
        partial = self.partial_of(blob)
        partial.write_bytes(b'interrupted write')
        drafts = self.store.root / 'illustration-drafts'
        drafts.mkdir(parents=True)
        (drafts / '999').write_text('not a directory')
        self.recovery.path.write_bytes(b'not sqlite')
        jobs = [('blob', blob), ('partial', self.relative(partial)),
                ('drafts', '999'), ('recovery', '999')]
        await self.repo.queue_cleanup(jobs)
        with patch.object(self.store, 'delete', return_value=False):
            result = await self.lifecycle.retry()
        self.assertFalse(result['completed'])
        pending = await self.repo.cleanup_jobs()
        self.assertEqual(sorted((row['kind'], row['target']) for row in pending), sorted(jobs))
        errors = {(row['kind'], row['target']): row['error'] for row in pending}
        self.assertTrue(all(errors.values()))
        self.assertIn('retry', errors[('blob', blob)])
        self.assertTrue(self.store.exists(blob))
        self.assertTrue((drafts / '999').is_file())
        self.assertEqual(self.recovery.path.read_bytes(), b'not sqlite')
        # Clearing the obstruction completes every recorded obligation.
        (drafts / '999').unlink()
        self.recovery.path.unlink()
        final = await self.lifecycle.retry()
        self.assertFalse(final['pending'])
        self.assertFalse(self.store.exists(blob))
        self.assertFalse(partial.exists())
        self.assertFalse(await self.repo.cleanup_jobs())

    async def test_obligations_outlive_their_conversation_and_reject_unknown_kinds(self):
        conversation, blob = await self.seed(data=b'outlives')
        await self.seed('other', data=b'other')
        await self.repo.delete_conversation(conversation.id)
        self.assertIsNone(await self.repo.get_conversation(conversation.id))
        rows = {(row['kind'], row['target']) for row in await self.repo.cleanup_jobs()}
        self.assertEqual(rows, {('blob', blob), ('drafts', str(conversation.id)),
                                ('recovery', str(conversation.id))})
        # Re-queueing is idempotent and keeps any recorded error.
        await self.repo.queue_cleanup([('blob', blob)])
        self.assertEqual(len(await self.repo.cleanup_jobs()), 3)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.run(lambda conn: conn.execute(
                "INSERT INTO artifact_cleanup(kind, target) VALUES ('bogus', 'x')"))
        self.assertEqual(len(await self.repo.cleanup_jobs()), 3)


class ReconciliationTests(LifecycleFixture):
    """Classification is read-only, total over files and disjoint."""

    def files(self):
        return {path.relative_to(self.store.root).as_posix(): path.read_bytes()
                for path in self.store.root.rglob('*') if path.is_file()}

    async def test_buckets_are_exhaustive_disjoint_and_repair_only_touches_its_own(self):
        conversation, referenced = await self.seed('live', data=b'referenced')
        orphan = self.store.write(b'orphan', '.md').relative_path
        partial = self.partial_of(orphan)
        partial.write_bytes(b'interrupted write')
        unknown = self.store.root / 'unrecognized.txt'
        unknown.write_text('keep')
        outside = self.root / 'outside.txt'
        outside.write_text('external')
        link = self.store.root / 'link.txt'
        link.symlink_to(outside)
        (self.store.root / 'unexpected-empty-directory').mkdir()
        nested = self.store.root / 'unexpected-directory' / 'inner.bin'
        nested.parent.mkdir()
        nested.write_bytes(b'nested')
        orphan_draft = self.store.root / 'illustration-drafts' / '999' / '1' / 'draft-1.json'
        orphan_draft.parent.mkdir(parents=True)
        orphan_draft.write_text('orphan draft')
        live_draft = (self.store.root / 'illustration-drafts' / str(conversation.id)
                      / '1' / 'draft-1.json')
        live_draft.parent.mkdir(parents=True)
        live_draft.write_text('live draft')
        await self.recovery.claim(999, 1, 'orphan recovery')
        await self.recovery.claim(conversation.id, 1, 'live recovery')
        before = self.files()

        report = await self.lifecycle.report()

        self.assertEqual(self.files(), before)
        self.assertEqual(report['blobs']['referenced'], [referenced])
        self.assertEqual(report['blobs']['unreferenced'], [orphan])
        self.assertEqual(report['blobs']['missing'], [])
        self.assertEqual(report['blobs']['invalid_references'], [])
        self.assertEqual(report['partials'], [self.relative(partial)])
        self.assertEqual(report['drafts'], {'conversations': [conversation.id, 999],
                                           'orphaned': [999]})
        self.assertEqual(report['recovery']['conversations'], [conversation.id, 999])
        self.assertEqual(report['recovery']['orphaned'], [999])
        self.assertIsNone(report['recovery']['error'])
        self.assertFalse(report['pending'])
        # Files are classified exactly once: blobs, partials and unknowns never overlap.
        blob_paths = set(report['blobs']['referenced']) | set(report['blobs']['unreferenced'])
        self.assertFalse(blob_paths & set(report['partials']))
        self.assertFalse(blob_paths & set(report['unrecognized']))
        self.assertFalse(set(report['partials']) & set(report['unrecognized']))
        self.assertEqual(set(report['unrecognized']),
                         {'unrecognized.txt', 'link.txt', 'unexpected-directory/inner.bin'})
        # An empty unexpected directory produces no entry: only files and symlinks
        # are reported. It also never becomes a repair target.
        self.assertNotIn('unexpected-empty-directory', report['unrecognized'])

        result = await self.repair()

        self.assertFalse(result['cleanup']['pending'])
        self.assertFalse(result['after']['blobs']['unreferenced'])
        self.assertFalse(result['after']['partials'])
        self.assertEqual(result['after']['drafts']['orphaned'], [])
        self.assertEqual(result['after']['recovery']['orphaned'], [])
        self.assertFalse(orphan_draft.exists())
        self.assertFalse(partial.exists())
        self.assertTrue(live_draft.exists())
        self.assertTrue(self.store.exists(referenced))
        self.assertTrue(await self.recovery.states(conversation.id))
        self.assertEqual(await self.recovery.states(999), {})
        self.assertTrue(self.recovery.path.is_file())
        # Unrecognized layouts are reported and retained, never repaired.
        self.assertEqual(unknown.read_text(), 'keep')
        self.assertEqual(outside.read_text(), 'external')
        self.assertTrue(link.is_symlink())
        self.assertEqual(nested.read_bytes(), b'nested')
        self.assertEqual(result['after']['unrecognized'], report['unrecognized'])
        self.assertTrue((self.store.root / 'unexpected-empty-directory').is_dir())


class TurnExclusionTests(unittest.IsolatedAsyncioTestCase):
    """Cleanup ownership excludes complete turns, not just other cleanup."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _, self.runtime = fixtures.make_runtime(self.root)
        await self.runtime.open()
        self.controller = self.runtime.controller

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temporary.cleanup()

    async def test_a_turn_is_refused_while_a_lifecycle_command_holds_exclusive(self):
        async with self.controller.artifacts.exclusive():
            with self.assertRaises(ChatBusy):
                await self.controller.send('Describe this')
        self.assertFalse(self.controller.is_generating)
        self.assertFalse(self.controller.artifacts.busy)
        self.assertFalse(await self.controller.current_messages())

    async def test_a_lifecycle_command_is_refused_while_the_lock_is_held(self):
        async with self.controller.artifacts.exclusive():
            for command in (self.controller.artifact_report,
                            self.controller.retry_artifact_cleanup,
                            self.controller.delete_current_conversation):
                with self.assertRaises(ChatBusy):
                    await command()


if __name__ == '__main__':
    unittest.main()
