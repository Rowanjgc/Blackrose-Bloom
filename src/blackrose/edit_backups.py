"""Records and policy for originals displaced by approved edits.

`WorkspaceFiles.write` publishes an approved replacement by atomic exchange
with a hidden same-folder stage, then moves the displaced original into the
private store `data_dir()/edit-backups/<conversation>/`, outside every project.
This module keeps the durable record of each backup in the chat database.

A pending row is written before publication and settled afterwards. If the app
dies in between, startup recovery finds the named stage or the stored file, so
the original is never left hidden in the project or untracked. Records outlive
their conversation; only an explicit removal deletes a backup, and age-based
cleanup never deletes conflict content (a file that changed during approval).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import logging
from pathlib import Path
import secrets

from .workspace import (MAX_WRITE_BYTES, FileAccessError, default_backup_store, find_stored_backup,
                        read_stored_backup, recover_replacement_stage, remove_stored_backup)

log = logging.getLogger(__name__)

MAX_RESTORE_BYTES = MAX_WRITE_BYTES * 10
MAX_PREVIEW_CHARS = 256_000
RETENTION_DAYS = 30


class EditBackups:
    def __init__(self, repository, root=None):
        self._repo = repository
        self.root = Path(root or default_backup_store()).expanduser().resolve()

    async def replace(self, conversation_id, original_path, approval_id, approved_digest, published_digest, write):
        """Record a backup around one approved replacement. ``write(backup_id)`` publishes it."""
        backup_id = secrets.token_hex(16)
        await self._repo.begin_edit_backup(backup_id, conversation_id, original_path, approval_id,
                                           approved_digest, published_digest)
        try:
            result = await write(backup_id)
        except FileAccessError as error:
            await asyncio.shield(self._settle(backup_id, error.details))
            raise
        except BaseException:
            await asyncio.shield(self._settle(backup_id, None))
            raise
        await asyncio.shield(self._settle(backup_id, result))
        return result

    async def _settle(self, backup_id, details):
        if details and details.get('backup'):
            await self._repo.settle_edit_backup(
                backup_id, location=details['backup'], stored=details['backup_stored'],
                digest=details['backup_digest'], size=details['backup_size'],
                mode=details['backup_mode'], conflict=details['backup_conflict'])
            return
        # No result reached here: the write failed before publication, or its
        # result was lost to cancellation. Settle from what is on disk.
        record = await self._repo.edit_backup(backup_id)
        if record is not None:
            await self._recover(record)

    def _recover_files(self, record):
        group = str(record['conversation_id'])
        # A cross-filesystem copy can publish the store file and leave its source
        # entry behind under the stage name. The store then already holds those
        # exact bytes, so the stage is that leftover source: passing the stored
        # digest lets recovery remove it instead of moving it onto the stored name
        # (a second move would collide and never settle). Content is compared
        # before anything is unlinked, so differing bytes still stay pending.
        existing = find_stored_backup(self.root, group, record['id'])
        digest = record['published_digest'] if existing is None else existing['digest']
        stored = recover_replacement_stage(record['original_path'], record['id'], self.root, group, digest)
        return stored or existing

    async def _recover(self, record):
        try:
            stored = await asyncio.to_thread(self._recover_files, record)
        except (OSError, ValueError) as error:
            # Kept pending, with its stage still named; retried at next start.
            log.warning('Edit backup %s for %s is not yet recovered: %s', record['id'], record['original_path'], error)
            return False
        if stored is None:
            await self._repo.delete_edit_backup(record['id'])
            return False
        await self._repo.settle_edit_backup(
            record['id'], location=stored['location'], stored=True, digest=stored['digest'],
            size=stored['size'], mode=stored['mode'], conflict=stored['digest'] != record['approved_digest'])
        return True

    async def recover(self):
        """Settle replacements interrupted by process death. Returns how many were kept."""
        recovered = 0
        for record in await self._repo.edit_backups(state='pending'):
            recovered += await self._recover(record)
        return recovered

    async def owned(self, conversation, backup_id, *, deleted_chats=False):
        """A saved backup of this conversation; with ``deleted_chats``, also one whose chat is gone."""
        record = await self._repo.edit_backup(backup_id) if isinstance(backup_id, str) else None
        if record is None or record['state'] != 'saved':
            raise ValueError('Backup not found')
        if conversation is not None and record['conversation_id'] == conversation.id:
            return record
        if deleted_chats and await self._repo.get_conversation(record['conversation_id']) is None:
            return record
        raise ValueError('Open the conversation that made this edit to use its backup')

    async def read(self, record, files):
        """The exact displaced bytes, refused if the copy no longer matches its record."""
        if record['stored']:
            data = await asyncio.to_thread(read_stored_backup, self.root, record['location'], MAX_RESTORE_BYTES)
        else:
            data = await asyncio.to_thread(files.read_bytes, record['location'], MAX_RESTORE_BYTES)
        if hashlib.sha256(data).hexdigest() != record['digest']:
            raise ValueError(f"The backup at {record['location']} no longer matches the saved original")
        return data

    async def listing(self, conversation):
        rows = await self._repo.edit_backups(conversation.id) if conversation else []
        # Backups from deleted chats can be previewed and removed, not restored.
        orphans = await self._repo.orphaned_edit_backups()
        return {'items': [self._wire(row) for row in rows], 'store': str(self.root),
                'deleted_chats': [self._wire(row) for row in orphans],
                'retention_days': RETENTION_DAYS}

    @staticmethod
    def _wire(row):
        return {'id': row['id'], 'path': row['original_path'], 'filename': Path(row['original_path']).name,
                'location': row['location'], 'stored': bool(row['stored']), 'size': row['size'],
                'conflict': bool(row['conflict']), 'created_at': row['created_at'],
                'restored_at': row['restored_at']}

    async def preview(self, conversation, backup_id, files):
        record = await self.owned(conversation, backup_id, deleted_chats=True)
        text = (await self.read(record, files)).decode('utf-8', errors='replace')
        return {**self._wire(record), 'text': text[:MAX_PREVIEW_CHARS],
                'truncated': len(text) > MAX_PREVIEW_CHARS}

    async def _delete(self, record):
        if record['stored']:
            try:
                await asyncio.to_thread(remove_stored_backup, self.root, record['location'])
            except FileNotFoundError:
                # The store group is already gone; only the record remains, so
                # forgetting it is the whole removal. prune() must not stop here.
                pass
        await self._repo.delete_edit_backup(record['id'])

    async def remove(self, conversation, backup_id):
        """Explicit removal. A copy kept in the project is only forgotten, never deleted there."""
        record = await self.owned(conversation, backup_id, deleted_chats=True)
        await self._delete(record)
        if record['stored']:
            return {'removed': True}
        return {'removed': True, 'notice': f"Blackrose no longer lists this backup; the file remains at {record['location']}."}

    async def prune(self, days=RETENTION_DAYS):
        """Remove stored backups older than ``days`` across chats, including deleted ones.

        Conflict content is never removed by age, and project copies are left alone.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec='microseconds')
        removed = kept = 0
        for record in await self._repo.edit_backups():
            if record['created_at'] >= cutoff:
                continue
            if record['conflict'] or not record['stored']:
                kept += 1
                continue
            await self._delete(record)
            removed += 1
        return {'removed': removed, 'kept': kept, 'days': days}
