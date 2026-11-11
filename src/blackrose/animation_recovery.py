"""Durable, bounded playback recovery, separate from the main chat schema.

This experiment can be switched out without upgrading the main chat database.
Claims commit before inference; replaying/reopening a failed scene cannot spend
another attempt. A repaired attachment joins the same two-attempt chain.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import logging
import sqlite3

from .storage.db import run_to_completion

MAX_REPAIRS = 2
REPAIR_REQUEST_PREFIX = 'Automatic animation repair (app-generated).'
log = logging.getLogger(__name__)


class AnimationRecovery:
    def __init__(self, path):
        self.path = path
        self._lock = asyncio.Lock()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('''CREATE TABLE IF NOT EXISTS repairs (
                conversation_id INTEGER NOT NULL, attachment_id INTEGER NOT NULL,
                root_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
                status TEXT NOT NULL, error TEXT NOT NULL,
                replacement_attachment_id INTEGER,
                PRIMARY KEY(conversation_id, attachment_id))''')
            connection.commit()
        except BaseException:
            connection.close()
            raise
        return connection

    async def states(self, conversation_id):
        def read():
            if not self.path.exists():
                return {}
            with closing(self._connect()) as connection:
                return {row['attachment_id']: dict(row) for row in connection.execute(
                    'SELECT * FROM repairs WHERE conversation_id=?', (conversation_id,))}
        try:
            async with self._lock:
                return await run_to_completion(read)
        except (sqlite3.Error, OSError):
            # Auxiliary recovery state must never prevent a chat from opening.
            log.warning('Animation recovery state is unavailable', exc_info=True)
            return {}

    async def claim(self, conversation_id, attachment_id, error):
        def claim():
            with closing(self._connect()) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                previous = connection.execute(
                    'SELECT * FROM repairs WHERE conversation_id=? AND attachment_id=?',
                    (conversation_id, attachment_id)).fetchone()
                if previous:
                    return dict(previous) | {'claimed': False}
                parent = connection.execute(
                    'SELECT * FROM repairs WHERE conversation_id=? AND replacement_attachment_id=?',
                    (conversation_id, attachment_id)).fetchone()
                root = parent['root_id'] if parent else attachment_id
                attempt = parent['attempt'] + 1 if parent else 1
                status = 'repairing' if attempt <= MAX_REPAIRS else 'exhausted'
                connection.execute('INSERT INTO repairs VALUES (?,?,?,?,?,?,NULL)',
                    (conversation_id, attachment_id, root, attempt, status, error[:1000]))
                return {'attachment_id': attachment_id, 'root_id': root, 'attempt': attempt,
                        'status': status, 'claimed': status == 'repairing'}
        async with self._lock:
            return await run_to_completion(claim)

    async def finish(self, conversation_id, attachment_id, status, replacement_id=None):
        def finish():
            with closing(self._connect()) as connection, connection:
                connection.execute('''UPDATE repairs SET status=?, replacement_attachment_id=?
                    WHERE conversation_id=? AND attachment_id=? AND status='repairing' ''',
                    (status, replacement_id, conversation_id, attachment_id))
        async def write():
            # The lock is also the writer exclusion that lets a read-only
            # artifact report open the ledger with immutable=1 (storage.snapshot.readonly_snapshot).
            async with self._lock:
                await run_to_completion(finish)
        operation = asyncio.create_task(write())
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            # The replacement link is recorded before the new attachment is
            # published, so a Stop must not drop it: an unlinked replacement
            # would later start a fresh two-attempt chain. run_to_completion already
            # completes the write; this also keeps the lock wait.
            try:
                await operation
            except Exception:
                log.warning('Animation recovery settlement failed during cancellation',
                            exc_info=True)
            raise

    async def delete(self, conversation_id):
        def delete():
            if self.path.exists():
                with closing(self._connect()) as connection, connection:
                    connection.execute('DELETE FROM repairs WHERE conversation_id=?', (conversation_id,))
        async with self._lock:
            await run_to_completion(delete)
