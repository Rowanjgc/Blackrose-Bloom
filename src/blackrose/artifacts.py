"""Conversation artifact ownership, durable cleanup retries and reconciliation.

The controller holds ``exclusive`` across complete turns and lifecycle commands.
The desktop lock excludes other cooperating processes; neither a filesystem scan
nor a previously printed report is permission to delete a newly referenced file.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
import sqlite3

from .errors import ChatBusy
from .instance import DesktopOwnership
from .storage.db import run_to_completion
from .storage.snapshot import readonly_snapshot
from .illustrations import delete_conversation_drafts

_BLOB = re.compile(r'([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})(\.[^/\x00]+)?')


async def settle(operation):
    """Receive an operation's outcome before releasing lifecycle ownership."""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def blob_name(path):
    match = _BLOB.fullmatch(path)
    return bool(match and match[1] == match[3][:2] and match[2] == match[3][2:4])


def partial_blob(path):
    parent, _, name = path.rpartition('/')
    if name.startswith('.') and name.endswith('.partial'):
        candidate = parent + '/' + name[1:-len('.partial')]
        if blob_name(candidate):
            return candidate
    return None


def owned_path(store, path):
    """Refuse aliases and symlinks, including symlinked parent directories."""
    expected = store.root.resolve() / path
    actual = store.path_for(path)
    if actual != expected or expected.is_symlink():
        raise ValueError('Artifact path contains a symlink or noncanonical component.')
    return actual


def canonical_key(store, path):
    """The store-relative path a delete would act on, or ``None`` if it escapes."""
    try:
        return store.path_for(path).relative_to(store.root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def recovery_path(store):
    name = 'animation-recovery.sqlite3'
    for suffix in ('', '-wal', '-shm', '-journal'):
        owned_path(store, name + suffix)
    return store.root / name


def reconciliation(store, snapshot):
    """Read files only. Unknown layouts and symlinks are reported, never repaired."""
    conversations, references, pending = snapshot
    protected, invalid, missing = set(), [], []
    for path in sorted(references):
        try:
            resolved = store.path_for(path)
            protected.add(resolved.relative_to(store.root.resolve()).as_posix())
            if not resolved.is_file():
                missing.append(path)
            if not blob_name(path) or resolved != store.root.resolve() / path:
                invalid.append(path)
        except (OSError, ValueError):
            invalid.append(path)
    blobs, partials, unknown = [], [], []
    # os.walk does not follow directory symlinks. Prune distinct owned data.
    def scan_error(error):
        raise error
    for directory, folders, files in os.walk(store.root, followlinks=False, onerror=scan_error):
        parent = Path(directory)
        for name in list(folders):
            path = parent / name
            relative = path.relative_to(store.root).as_posix()
            if path.is_symlink():
                unknown.append(relative)
                folders.remove(name)
            elif parent == store.root and name == 'illustration-drafts':
                folders.remove(name)
        for name in files:
            path = parent / name
            relative = path.relative_to(store.root).as_posix()
            if path.is_symlink():
                unknown.append(relative)
            elif blob_name(relative):
                blobs.append(relative)
            elif partial_blob(relative):
                partials.append(relative)
            elif parent != store.root or name not in (
                    'animation-recovery.sqlite3', 'animation-recovery.sqlite3-wal',
                    'animation-recovery.sqlite3-shm', 'animation-recovery.sqlite3-journal'):
                unknown.append(relative)
    drafts, orphan_drafts = [], []
    draft_root = store.root / 'illustration-drafts'
    if draft_root.is_dir() and not draft_root.is_symlink():
        for path in sorted(draft_root.iterdir()):
            if (path.is_dir() and not path.is_symlink() and path.name.isascii()
                    and path.name.isdigit() and str(int(path.name)) == path.name and int(path.name) > 0):
                drafts.append(int(path.name))
                if int(path.name) not in conversations:
                    orphan_drafts.append(int(path.name))
            else:
                unknown.append(path.relative_to(store.root).as_posix())
    recovery = {'conversations': [], 'orphaned': [], 'error': None}
    ledger = store.root / 'animation-recovery.sqlite3'
    if ledger.exists() or ledger.is_symlink():
        try:
            recovery_path(store)
            with readonly_snapshot(ledger) as conn:
                ids = sorted(row[0] for row in conn.execute('SELECT DISTINCT conversation_id FROM repairs'))
            recovery.update(conversations=ids, orphaned=[cid for cid in ids if cid not in conversations])
        except (OSError, ValueError, sqlite3.Error) as error:
            recovery['error'] = str(error)
    return {
        'blobs': {'referenced': sorted(references), 'unreferenced': sorted(set(blobs) - protected),
                  'missing': missing, 'invalid_references': invalid},
        'partials': sorted(partials), 'unrecognized': sorted(unknown),
        'drafts': {'conversations': drafts, 'orphaned': orphan_drafts},
        'recovery': recovery, 'pending': pending,
    }


class ArtifactLifecycle:
    def __init__(self, repository, store, recovery):
        self.repo, self.store, self.recovery = repository, store, recovery
        self._lock = asyncio.Lock()

    @property
    def busy(self):
        return self._lock.locked()

    @asynccontextmanager
    async def exclusive(self):
        # Refuse immediately so a queued command cannot act on a different chat.
        if self._lock.locked():
            raise ChatBusy('Wait for the current reply or artifact cleanup to finish')
        async with self._lock:
            yield

    async def report(self):
        snapshot = await self.repo.ownership_inventory()
        async with self.recovery._lock:
            return await run_to_completion(lambda: reconciliation(self.store, snapshot))

    async def retry(self):
        """Drain recorded obligations. Caller holds exclusive until workers settle."""
        completed, preserved = [], []
        if not await self.repo.cleanup_jobs():
            return {'completed': completed, 'preserved': preserved, 'pending': []}
        _, references, jobs = await self.repo.ownership_inventory()
        # Include canonical aliases from damaged rows: they still own their bytes.
        # The snapshot covers both columns, under the same writer exclusion.
        protected = set(references)
        for path in references:
            canonical = canonical_key(self.store, path)
            if canonical is not None:
                protected.add(canonical)
        for job in jobs:
            kind, target = job['kind'], job['target']
            try:
                if kind in ('blob', 'partial'):
                    # A delete acts on the canonical path, so protection is decided
                    # there: a damaged alias must neither remove bytes a surviving row
                    # references nor become an obligation no later retry can clear.
                    if canonical_key(self.store, target) in protected:
                        preserved.append(job)
                    elif not (blob_name(target) if kind == 'blob' else partial_blob(target)):
                        raise ValueError('Unrecognized blob layout; manual inspection required.')
                    else:
                        owned_path(self.store, target)
                        if not await run_to_completion(lambda target=target: self.store.delete(target)):
                            raise OSError('Attachment deletion failed; retry required.')
                        completed.append(job)
                else:
                    conversation_id = int(target)
                    if str(conversation_id) != target or conversation_id <= 0:
                        raise ValueError('Invalid conversation cleanup identity.')
                    if await self.repo.get_conversation(conversation_id) is not None:
                        preserved.append(job)
                    elif kind == 'drafts':
                        await run_to_completion(lambda conversation_id=conversation_id: delete_conversation_drafts(
                            self.store.root, conversation_id))
                        completed.append(job)
                    elif kind == 'recovery':
                        recovery_path(self.store)
                        await self.recovery.delete(conversation_id)
                        completed.append(job)
                    else:
                        raise ValueError('Unknown cleanup obligation.')
                await self.repo.finish_cleanup(kind, target)
            except (OSError, ValueError, sqlite3.Error) as error:
                await self.repo.finish_cleanup(kind, target, str(error))
        return {'completed': completed, 'preserved': preserved, 'pending': await self.repo.cleanup_jobs()}

    async def repair(self, ownership):
        if not isinstance(ownership, DesktopOwnership):
            raise RuntimeError('Artifact repair requires desktop ownership.')
        ownership.require(self.repo.database_path)
        report = await self.report()
        jobs = [('blob', path) for path in report['blobs']['unreferenced']]
        jobs += [('partial', path) for path in report['partials']]
        jobs += [('drafts', str(cid)) for cid in report['drafts']['orphaned']]
        jobs += [('recovery', str(cid)) for cid in report['recovery']['orphaned']]
        await self.repo.queue_cleanup(jobs)
        return {'before': report, 'cleanup': await self.retry(), 'after': await self.report()}
