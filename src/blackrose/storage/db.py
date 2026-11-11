"""Serialized SQLite work with atomic migrations and failure rollback."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from .migrations import MIGRATIONS

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    pass


async def run_to_completion[T](fn: Callable[[], T]) -> T:
    """Run ``fn`` in a thread and wait for it to finish, even if the caller is cancelled.

    Cancellation is re-raised only after the thread completes, so a caller's lock
    is held until the effect has settled. Database work, blob writes and artifact
    cleanup use it wherever a started effect must not outlive its owner.
    """
    task = asyncio.create_task(asyncio.to_thread(fn))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("database operation failed during cancellation", exc_info=error)
        raise asyncio.CancelledError
    return task.result()


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = path if path == ":memory:" else Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("database is not open")
        return self._conn

    async def connect(self) -> None:
        async with self._lock:
            if self._conn is not None:
                return

            def _open() -> None:
                if isinstance(self.path, Path):
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(self.path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                try:
                    conn.execute("PRAGMA foreign_keys = ON")
                    if self.path != ":memory:":
                        conn.execute("PRAGMA journal_mode = WAL")
                    conn.execute("PRAGMA synchronous = NORMAL")
                    self._migrate_connection(conn)
                except BaseException:
                    conn.close()
                    raise
                # Publish only a fully initialized connection. The worker remains
                # protected by the lock if its awaiting task is cancelled.
                self._conn = conn

            await run_to_completion(_open)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is None:
                return
            conn, self._conn = self._conn, None
            await run_to_completion(conn.close)

    async def run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Serialize work and discard unfinished writes if the operation fails."""
        async with self._lock:
            conn = self.connection

            def _execute():
                try:
                    return fn(conn)
                except BaseException:
                    conn.rollback()
                    raise

            return await run_to_completion(_execute)

    @staticmethod
    def _migrate_connection(conn: sqlite3.Connection) -> int:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_metadata ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.commit()
        row = conn.execute("SELECT MAX(version) AS v FROM schema_metadata").fetchone()
        current = row["v"] or 0
        latest = max((version for version, _ in MIGRATIONS), default=0)
        if current > latest:
            raise StorageError("database schema is newer than this application")

        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            log.info("applying schema migration %d", version)
            try:
                # executescript commits any previous transaction. Start the new
                # transaction inside the script so DDL and version commit together.
                conn.executescript("BEGIN IMMEDIATE;\n" + sql)
                conn.execute("INSERT INTO schema_metadata (version) VALUES (?)", (version,))
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise StorageError(f"migration {version} failed: {exc}") from exc
            current = version
        return current

    async def migrate(self) -> int:
        return await self.run(self._migrate_connection)

    async def schema_version(self) -> int:
        def _get(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_metadata").fetchone()
            return row["v"] or 0
        return await self.run(_get)
