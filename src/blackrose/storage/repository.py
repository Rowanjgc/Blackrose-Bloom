"""Conversation and message persistence.

Blackrose owns conversation state independently of the inference server. Every inference
rebuilds its message list from these rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
import hashlib

from .db import Database
from .records import Conversation, MessageState, StoredAttachment, StoredMessage, TitleSource, UIBlock
from ..diagrams import diagram_tokens, replace_diagram
from ..citations import digest, unchecked_report

DEFAULT_TITLE = "New conversation"
_TITLE_MAX = 48


@dataclass(frozen=True, slots=True)
class NewAttachment:
    """An attachment about to be persisted, once its bytes are already stored."""

    mime_type: str
    filename: str
    storage_path: str
    digest: str
    size: int
    width: int | None = None
    height: int | None = None
    kind: str = "image"
    text_path: str | None = None
    pages: int | None = None
    characters: int | None = None
    output_path: str | None = None


# A position in sidebar order: the stored (updated_at, id) of a row, compared
# exactly as SQLite stores it rather than reparsed.
ConversationCursor = tuple[str, int]


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """Conversations in sidebar order, and how to continue below them."""

    items: list[Conversation]
    cursor: ConversationCursor | None   # the last item's position
    more: bool                          # whether older conversations exist
    total: int


@dataclass(frozen=True, slots=True)
class MessageSummary:
    """Conversation-wide markers; the latest assistant has no related rows loaded."""

    last_id: int
    last_user_id: int
    latest_assistant: StoredMessage | None


@dataclass(frozen=True, slots=True)
class MessagePage:
    """One chronological transcript page and independent conversation markers."""

    items: list[StoredMessage]
    more: bool
    summary: MessageSummary


def _now() -> str:
    """Microsecond precision.

    Second resolution ties on ordering: two conversations created in the same
    second, then activity on the older one, would sort wrongly in the sidebar.
    Fixed-width ISO strings still sort lexicographically.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def derive_title(text: str) -> str:
    """A readable title from the first user message.

    Deterministic local fallback when the background title request is unavailable.
    """
    cleaned = text.strip()
    if not cleaned:
        return DEFAULT_TITLE
    # Prefer the first non-empty line that is not a code fence.
    for line in cleaned.splitlines():
        line = line.strip()
        if line and not line.startswith("```"):
            cleaned = line
            break
    cleaned = re.sub(r"^#+\s*", "", cleaned)               # heading markers
    cleaned = re.sub(r"[*_`>]+", "", cleaned)              # inline markdown
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:\t")
    if not cleaned:
        return DEFAULT_TITLE
    if len(cleaned) <= _TITLE_MAX:
        return cleaned
    clipped = cleaned[:_TITLE_MAX].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return (clipped or cleaned[:_TITLE_MAX]).rstrip() + "…"


def _conversation(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        title=row["title"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
        title_source=TitleSource(row["title_source"]),
        workspace=json.loads(row["workspace"]) if row["workspace"] else None,
    )


def _message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=row["role"],
        content=row["content"],
        created_at=_dt(row["created_at"]),
        model_used=row["model_used"],
        state=MessageState(row["state"]),
        error=row["error"],
        reply_to=row["reply_to"],
        refs=json.loads(row["refs"]),
    )


def _attachment(row: sqlite3.Row) -> StoredAttachment:
    return StoredAttachment(
        id=row["id"],
        message_id=row["message_id"],
        kind=row["kind"],
        mime_type=row["mime_type"],
        filename=row["filename"],
        storage_path=row["storage_path"],
        digest=row["digest"],
        size=row["size"],
        width=row["width"],
        height=row["height"],
        text_path=row["text_path"],
        pages=row["pages"],
        characters=row["characters"],
        output_path=row["output_path"],
        created_at=_dt(row["created_at"]),
    )


def _attach_all(
    conn: sqlite3.Connection, messages: list[StoredMessage]
) -> list[StoredMessage]:
    """Load related rows in bounded batches, including for full-history exports."""
    batch_size = min(500, conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    for start in range(0, len(messages), batch_size):
        _attach_batch(conn, messages[start:start + batch_size])
    return messages


def _attach_batch(
    conn: sqlite3.Connection, messages: list[StoredMessage]
) -> list[StoredMessage]:
    if not messages:
        return messages
    ids = [message.id for message in messages]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM message_attachments WHERE message_id IN ({placeholders}) "
        "ORDER BY message_id, id",
        ids,
    ).fetchall()
    block_rows = conn.execute(
        f'SELECT b.*, s.sequence, s.state FROM message_blocks b '
        f'LEFT JOIN ui_state s USING(block_id) WHERE message_id IN ({placeholders}) '
        'ORDER BY message_id, position', ids).fetchall()
    by_id = {message.id: message for message in messages}
    for row in block_rows:
        block = json.loads(row['payload'])
        block.update(saved_state=json.loads(row['state'] or '{}'), state_sequence=row['sequence'] or 0)
        by_id[row['message_id']].blocks.append(block)
    for row in conn.execute(
            f'SELECT e.user_message_id, e.payload AS event_payload, b.payload AS block_payload '
            f'FROM ui_events e JOIN message_blocks b USING(block_id) WHERE e.user_message_id IN ({placeholders})', ids):
        by_id[row['user_message_id']].ui_context = _ui_context(row)
    if not rows:
        return messages
    by_message: dict[int, list[StoredAttachment]] = {}
    for row in rows:
        by_message.setdefault(row["message_id"], []).append(_attachment(row))
    for message in messages:
        message.attachments = by_message.get(message.id, [])
    return messages


def _load_messages(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> list[StoredMessage]:
    messages = _attach_all(conn, [_message(row) for row in rows])
    batch_size = min(500, conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    for start in range(0, len(messages), batch_size):
        by_id = {m.id: m for m in messages[start:start + batch_size]}
        placeholders = ','.join('?' * len(by_id))
        for row in conn.execute(
                f'SELECT message_id, report FROM message_audits WHERE message_id IN ({placeholders})',
                list(by_id)):
            by_id[row['message_id']].audit = json.loads(row['report'])
    return messages


def _message_summary(conn: sqlite3.Connection, conversation_id: int) -> MessageSummary:
    last = conn.execute('SELECT id FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1',
                        (conversation_id,)).fetchone()
    user = conn.execute("SELECT id FROM messages WHERE conversation_id=? AND role='user' "
                        'ORDER BY id DESC LIMIT 1', (conversation_id,)).fetchone()
    assistant = conn.execute("SELECT * FROM messages WHERE conversation_id=? AND role='assistant' "
                             'ORDER BY id DESC LIMIT 1', (conversation_id,)).fetchone()
    return MessageSummary(last['id'] if last else 0, user['id'] if user else 0,
                          _message(assistant) if assistant else None)


def _forget_in_memory(conn, conversation_id, stamp):
    """Drop a chat's unsent turns and, if any were queued, delete its Honcho session."""
    conn.execute("DELETE FROM memory_outbox WHERE kind = 'turn' AND conversation_id = ?", (conversation_id,))
    conn.execute('''INSERT OR IGNORE INTO memory_outbox(kind, conversation_id, created_at)
        SELECT 'forget', id, ? FROM conversations WHERE id = ? AND memory_used = 1''',
                 (stamp, conversation_id))


def _ui_context(row):
    block = json.loads(row['block_payload'])
    event = json.loads(row['event_payload'])
    return {'block_id': block['block_id'], 'source_revision': block['source_revision'],
            'document_source': block['source'][:12000],
            'source_excerpt_truncated': len(block['source']) > 12000,
            'submitted_values': event['values']}


def _insert_attachments(conn: sqlite3.Connection, message_id: int,
                        attachments: Sequence[NewAttachment], stamp: str) -> None:
    for a in attachments:
        conn.execute(
            "INSERT INTO message_attachments "
            "(message_id, kind, mime_type, filename, storage_path, digest, size, "
            "width, height, text_path, pages, characters, created_at, output_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, a.kind, a.mime_type, a.filename, a.storage_path, a.digest,
             a.size, a.width, a.height, a.text_path, a.pages, a.characters, stamp, a.output_path))


def _insert_message(conn: sqlite3.Connection, conversation_id: int, role: str,
                    content: str, stamp: str, model_used: str | None,
                    state: MessageState, attachments: Sequence[NewAttachment] = (),
                    reply_to: int | None = None) -> int:
    row = conn.execute(
        "INSERT INTO messages "
        "(conversation_id, role, content, created_at, model_used, state, reply_to) "
        "VALUES (?,?,?,?,?,?,?)",
        (conversation_id, role, content, stamp, model_used, state.value, reply_to))
    message_id = row.lastrowid
    assert message_id is not None  # INSERT into this rowid table always assigns an identity.
    _insert_attachments(conn, message_id, attachments, stamp)
    return message_id


def read_ownership_inventory(connection: sqlite3.Connection) -> tuple[set[int], set[str], list[dict[str, Any]]]:
    """Read all ownership columns in one SQLite snapshot; also supports schema 1–3.

    Shared by ``Repository.ownership_inventory`` and the offline read-only
    ``artifact_cli`` report, which opens its own snapshot connection.
    """
    with connection:
        connection.execute('BEGIN')
        conversations = {row[0] for row in connection.execute('SELECT id FROM conversations')}
        references = {row[0] for row in connection.execute('''
            SELECT storage_path FROM message_attachments
            UNION SELECT text_path FROM message_attachments WHERE text_path IS NOT NULL''')}
        has_queue = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_cleanup'").fetchone()
        pending = [dict(zip(('kind', 'target', 'error'), row, strict=True)) for row in connection.execute(
            'SELECT kind, target, error FROM artifact_cleanup ORDER BY kind, target')] if has_queue else []
    return conversations, references, pending


class Repository:
    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def database_path(self):
        """The chat database file, for ownership checks."""
        return self._db.path

    async def ownership_inventory(self) -> tuple[set[int], set[str], list[dict[str, Any]]]:
        """Conversations, referenced blobs and cleanup obligations in one snapshot."""
        return await self._db.run(read_ownership_inventory)

    # --- conversations ------------------------------------------------------

    async def create_conversation(
        self,
        title: str = DEFAULT_TITLE,
        *,
        title_source: TitleSource | None = None,
    ) -> Conversation:
        """Create a conversation.

        An unnamed chat records that nothing has named it yet, which is what
        makes automatic naming safe: it only ever replaces a placeholder.
        """
        stamp = _now()
        if title_source is None:
            title_source = (
                TitleSource.PLACEHOLDER if title == DEFAULT_TITLE else TitleSource.MANUAL
            )

        def _create(conn: sqlite3.Connection) -> Conversation:
            cur = conn.execute(
                "INSERT INTO conversations (title, created_at, updated_at, title_source) "
                "VALUES (?,?,?,?)",
                (title, stamp, stamp, title_source.value),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _conversation(row)

        return await self._db.run(_create)

    async def list_conversations(self, limit: int = 200) -> list[Conversation]:
        def _list(conn: sqlite3.Connection) -> list[Conversation]:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_conversation(r) for r in rows]

        return await self._db.run(_list)

    async def conversation_page(
        self,
        limit: int = 200,
        *,
        after: ConversationCursor | None = None,
        through: ConversationCursor | None = None,
        including: int | None = None,
    ) -> ConversationPage:
        """One keyset page of the sidebar order, newest activity first.

        ``after`` continues strictly below a previous page's cursor. ``through``
        instead reloads every row from the newest down to that cursor, so a
        loaded window keeps its rows when activity moves one to the top.
        ``including`` extends that window down to a conversation that lies
        below it. Equal timestamps order by id, so pages never overlap or skip.
        """

        newest = "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC LIMIT ?"
        window = ("SELECT * FROM conversations WHERE (updated_at, id) >= (?, ?) "
                  "ORDER BY updated_at DESC, id DESC")

        def _page(conn: sqlite3.Connection) -> ConversationPage:
            if after is not None:
                rows = conn.execute(
                    "SELECT * FROM conversations WHERE (updated_at, id) < (?, ?) "
                    "ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (*after, limit),
                ).fetchall()
            else:
                rows = conn.execute(window, through).fetchall() if through else \
                    conn.execute(newest, (limit,)).fetchall()
                key = conn.execute(
                    "SELECT updated_at, id FROM conversations WHERE id = ?", (including,)
                ).fetchone() if including is not None else None
                if key and all(r["id"] != including for r in rows):
                    rows = conn.execute(window, tuple(key)).fetchall()
            cursor = (rows[-1]["updated_at"], rows[-1]["id"]) if rows else after
            more = cursor is not None and conn.execute(
                "SELECT 1 FROM conversations WHERE (updated_at, id) < (?, ?) LIMIT 1", cursor
            ).fetchone() is not None
            total = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
            return ConversationPage([_conversation(r) for r in rows], cursor, more, total)

        return await self._db.run(_page)

    async def adjacent_conversation(self, conversation_id: int) -> Conversation | None:
        """The conversation to select after this one is deleted.

        The next older chat in sidebar order, or the nearest newer one when it
        was the oldest. Independent of how many pages the sidebar has loaded.
        """

        def _adjacent(conn: sqlite3.Connection) -> Conversation | None:
            key = conn.execute(
                "SELECT updated_at, id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if key is None:
                return None
            row = conn.execute(
                "SELECT * FROM conversations WHERE (updated_at, id) < (?, ?) "
                "ORDER BY updated_at DESC, id DESC LIMIT 1", tuple(key),
            ).fetchone() or conn.execute(
                "SELECT * FROM conversations WHERE (updated_at, id) > (?, ?) "
                "ORDER BY updated_at ASC, id ASC LIMIT 1", tuple(key),
            ).fetchone()
            return _conversation(row) if row else None

        return await self._db.run(_adjacent)

    async def get_conversation(self, conversation_id: int) -> Conversation | None:
        def _get(conn: sqlite3.Connection) -> Conversation | None:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            return _conversation(row) if row else None

        return await self._db.run(_get)

    async def rename_conversation(
        self,
        conversation_id: int,
        title: str,
        *,
        source: TitleSource = TitleSource.MANUAL,
        only_if_unnamed: bool = False,
    ) -> bool:
        """Rename a conversation. Returns whether the row actually changed.

        ``only_if_unnamed`` makes the guard atomic rather than a check the
        caller performs and then hopes still holds: automatic naming runs after
        a reply lands, and the user may have renamed the chat in the meantime.
        """

        def _rename(conn: sqlite3.Connection) -> bool:
            sql = "UPDATE conversations SET title = ?, title_source = ? WHERE id = ?"
            params: tuple[object, ...] = (title, source.value, conversation_id)
            if only_if_unnamed:
                sql += " AND title_source = ?"
                params += (TitleSource.PLACEHOLDER.value,)
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount > 0

        return await self._db.run(_rename)

    async def set_workspace(self, conversation_id, workspace):
        def update(conn):
            conn.execute('UPDATE conversations SET workspace = ? WHERE id = ?',
                         (json.dumps(workspace) if workspace else None, conversation_id))
            conn.commit()
        await self._db.run(update)

    async def mark_local_data_exposed(self, conversation_id: int) -> None:
        """Record, permanently for this conversation, that local data reached the model."""
        def update(conn):
            conn.execute('UPDATE conversations SET local_data_exposed = 1 WHERE id = ?', (conversation_id,))
            conn.commit()
        await self._db.run(update)

    async def local_data_exposed(self, conversation_id: int) -> bool:
        def get(conn):
            row = conn.execute('SELECT local_data_exposed FROM conversations WHERE id = ?',
                               (conversation_id,)).fetchone()
            return bool(row and row[0])
        return await self._db.run(get)

    async def touch_conversation(self, conversation_id: int) -> None:
        stamp = _now()

        def _touch(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (stamp, conversation_id),
            )
            conn.commit()

        await self._db.run(_touch)

    async def delete_conversation(self, conversation_id: int) -> None:
        def _delete(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if not conn.execute('SELECT 1 FROM conversations WHERE id=?', (conversation_id,)).fetchone():
                    return
                conn.execute('''INSERT OR IGNORE INTO artifact_cleanup(kind, target)
                    SELECT 'blob', a.storage_path FROM message_attachments a
                    JOIN messages m ON m.id=a.message_id WHERE m.conversation_id=?
                    UNION SELECT 'blob', a.text_path FROM message_attachments a
                    JOIN messages m ON m.id=a.message_id
                    WHERE m.conversation_id=? AND a.text_path IS NOT NULL''',
                    (conversation_id, conversation_id))
                conn.executemany('INSERT OR IGNORE INTO artifact_cleanup(kind, target) VALUES (?, ?)',
                                 [('drafts', str(conversation_id)), ('recovery', str(conversation_id))])
                _forget_in_memory(conn, conversation_id, _now())
                conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

        await self._db.run(_delete)

    async def cleanup_jobs(self):
        return await self._db.run(lambda conn: [dict(row) for row in conn.execute(
            'SELECT kind, target, error FROM artifact_cleanup ORDER BY kind, target')])

    async def queue_cleanup(self, jobs):
        def queue(conn):
            with conn:
                conn.executemany('INSERT OR IGNORE INTO artifact_cleanup(kind, target) VALUES (?, ?)', jobs)
        await self._db.run(queue)

    async def finish_cleanup(self, kind, target, error=None):
        def finish(conn):
            with conn:
                if error is None:
                    conn.execute('DELETE FROM artifact_cleanup WHERE kind=? AND target=?', (kind, target))
                else:
                    conn.execute('UPDATE artifact_cleanup SET error=? WHERE kind=? AND target=?',
                                 (error, kind, target))
        await self._db.run(finish)

    # --- persistent memory outbox ------------------------------------------

    async def installation_id(self) -> str:
        return await self._db.run(lambda conn: conn.execute('SELECT id FROM installation').fetchone()[0])

    async def memory_excluded(self, conversation_id: int) -> bool:
        def get(conn):
            row = conn.execute('SELECT memory_excluded FROM conversations WHERE id = ?',
                               (conversation_id,)).fetchone()
            return bool(row and row[0])
        return await self._db.run(get)

    async def set_memory_excluded(self, conversation_id: int, excluded: bool) -> None:
        """Excluding a chat drops its unsent turns and forgets what was already sent."""
        def update(conn):
            with conn:
                conn.execute('UPDATE conversations SET memory_excluded = ? WHERE id = ?',
                             (int(excluded), conversation_id))
                if excluded:
                    _forget_in_memory(conn, conversation_id, _now())
        await self._db.run(update)

    async def memory_jobs(self, limit: int = 50) -> list[dict]:
        return await self._db.run(lambda conn: [dict(row) for row in conn.execute(
            'SELECT id, kind, conversation_id, message_id, attempts FROM memory_outbox '
            'ORDER BY id LIMIT ?', (limit,))])

    async def start_memory_job(self, job_id: int) -> None:
        def update(conn):
            with conn:
                conn.execute('UPDATE memory_outbox SET attempts = attempts + 1 WHERE id = ?', (job_id,))
        await self._db.run(update)

    async def finish_memory_job(self, job_id: int) -> None:
        def delete(conn):
            with conn:
                conn.execute('DELETE FROM memory_outbox WHERE id = ?', (job_id,))
        await self._db.run(delete)

    async def drop_memory_turns(self) -> None:
        """Unsent turns are discarded when memory is switched off."""
        def delete(conn):
            with conn:
                conn.execute("DELETE FROM memory_outbox WHERE kind = 'turn'")
        await self._db.run(delete)

    async def memory_turn(self, message_id: int) -> tuple[StoredMessage | None, StoredMessage] | None:
        """A complete reply and its request, or None once it is gone or excluded."""
        def get(conn):
            row = conn.execute('''SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id
                WHERE m.id = ? AND m.role = 'assistant' AND m.state = ? AND c.memory_excluded = 0''',
                               (message_id, MessageState.COMPLETE.value)).fetchone()
            if row is None:
                return None
            reply = _message(row)
            request = conn.execute('SELECT * FROM messages WHERE id = ?', (reply.reply_to,)).fetchone()
            loaded = _attach_all(conn, [reply, *([_message(request)] if request else [])])
            return (loaded[1] if request else None), loaded[0]
        return await self._db.run(get)

    # --- approved-edit backups ---------------------------------------------

    async def begin_edit_backup(self, backup_id: str, conversation_id: int, original_path: str,
                                approval_id: str | None, approved_digest: str, published_digest: str) -> None:
        def save(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute('''INSERT INTO edit_backups(id, conversation_id, original_path, approval_id,
                    approved_digest, published_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (backup_id, conversation_id, original_path, approval_id,
                     approved_digest, published_digest, _now()))
        await self._db.run(save)

    async def settle_edit_backup(self, backup_id: str, *, location: str, stored: bool,
                                 digest: str | None, size: int | None, mode: int | None,
                                 conflict: bool) -> None:
        def save(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute('''UPDATE edit_backups SET state='saved', location=?, stored=?, digest=?,
                    size=?, mode=?, conflict=? WHERE id=?''',
                    (location, int(stored), digest, size, mode, int(conflict), backup_id))
        await self._db.run(save)

    async def edit_backups(self, conversation_id: int | None = None, *,
                           state: str = 'saved') -> list[dict[str, Any]]:
        def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            if conversation_id is None:
                rows = conn.execute('SELECT * FROM edit_backups WHERE state=? ORDER BY created_at, id', (state,))
            else:
                rows = conn.execute('''SELECT * FROM edit_backups WHERE state=? AND conversation_id=?
                    ORDER BY created_at DESC, id''', (state, conversation_id))
            return [dict(row) for row in rows]
        return await self._db.run(read)

    async def edit_backup(self, backup_id: str) -> dict[str, Any] | None:
        def read(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute('SELECT * FROM edit_backups WHERE id=?', (backup_id,)).fetchone()
            return dict(row) if row else None
        return await self._db.run(read)

    async def orphaned_edit_backups(self) -> list[dict[str, Any]]:
        """Saved backups whose conversation was deleted; they remain until removed."""
        def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [dict(row) for row in conn.execute('''SELECT * FROM edit_backups b WHERE state='saved'
                AND NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id=b.conversation_id)
                ORDER BY created_at DESC, id''')]
        return await self._db.run(read)

    async def mark_edit_backup_restored(self, backup_id: str) -> None:
        def save(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute('UPDATE edit_backups SET restored_at=? WHERE id=?', (_now(), backup_id))
        await self._db.run(save)

    async def delete_edit_backup(self, backup_id: str) -> None:
        def delete(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute('DELETE FROM edit_backups WHERE id=?', (backup_id,))
        await self._db.run(delete)

    # --- composer drafts ----------------------------------------------------
    # drafts.ComposerDrafts bounds and encodes the values; these methods own the rows.

    async def save_composer_draft(self, conversation_id: int, *, text: str, refs: str,
                                  attachments: str, sending: str | None, updated_at: str,
                                  files: dict[str, bytes]) -> bool:
        """Replace a draft and its attachment copies. False once the conversation is deleted."""
        def save(conn: sqlite3.Connection) -> bool:
            with conn:
                # An upsert, never REPLACE: replacing the row would cascade away
                # its files. Selecting the owner refuses a deleted conversation.
                inserted = conn.execute(
                    'INSERT INTO composer_drafts(conversation_id, text, refs, attachments, sending, updated_at) '
                    'SELECT id, ?, ?, ?, ?, ? FROM conversations WHERE id=? '
                    'ON CONFLICT(conversation_id) DO UPDATE SET text=excluded.text, refs=excluded.refs, '
                    'attachments=excluded.attachments, sending=excluded.sending, '
                    'updated_at=excluded.updated_at',
                    (text, refs, attachments, sending, updated_at, conversation_id)).rowcount
                if not inserted:
                    return False
                stored = {r[0] for r in conn.execute(
                    'SELECT digest FROM composer_draft_files WHERE conversation_id=?', (conversation_id,))}
                conn.executemany('INSERT INTO composer_draft_files VALUES (?,?,?)',
                                 [(conversation_id, d, files[d]) for d in files.keys() - stored])
                conn.executemany('DELETE FROM composer_draft_files WHERE conversation_id=? AND digest=?',
                                 [(conversation_id, d) for d in stored - files.keys()])
            return True
        return await self._db.run(save)

    async def delete_composer_draft(self, conversation_id: int) -> None:
        """Remove a draft; its attachment copies cascade with it."""
        def delete(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute('DELETE FROM composer_drafts WHERE conversation_id=?', (conversation_id,))
        await self._db.run(delete)

    async def composer_draft(self, conversation_id: int) -> tuple[dict[str, Any], dict[str, bytes]] | None:
        """The draft row and its attachment copies by digest, or None."""
        def read(conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, bytes]] | None:
            row = conn.execute('SELECT * FROM composer_drafts WHERE conversation_id=?',
                               (conversation_id,)).fetchone()
            if row is None:
                return None
            files = {r[0]: r[1] for r in conn.execute(
                'SELECT digest, data FROM composer_draft_files WHERE conversation_id=?', (conversation_id,))}
            return dict(row), files
        return await self._db.run(read)

    # --- messages -----------------------------------------------------------

    async def begin_turn(
        self,
        conversation_id: int,
        content: str,
        model_used: str | None,
        attachments: Sequence[NewAttachment] = (),
        refs=(),
        ui_event_id=None,
    ) -> tuple[StoredMessage, StoredMessage]:
        """Persist a request and its pending reply together, or persist neither."""
        stamp = _now()

        def _begin(conn: sqlite3.Connection) -> tuple[StoredMessage, StoredMessage]:
            with conn:
                user_id = _insert_message(conn, conversation_id, "user", content, stamp,
                                          None, MessageState.COMPLETE, attachments)
                conn.execute("UPDATE messages SET refs = ? WHERE id = ?", (json.dumps(list(refs)), user_id))
                assistant_id = _insert_message(conn, conversation_id, "assistant", "", stamp,
                                               model_used, MessageState.PENDING,
                                               reply_to=user_id)
                if ui_event_id:
                    changed = conn.execute("UPDATE ui_events SET user_message_id=?, status='started' "
                        "WHERE event_id=? AND status='accepted'", (user_id, ui_event_id))
                    if changed.rowcount != 1:
                        raise ValueError('UI event was already consumed')
                conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                             (stamp, conversation_id))
                rows = conn.execute("SELECT * FROM messages WHERE id IN (?,?) ORDER BY id",
                                    (user_id, assistant_id)).fetchall()
                user, assistant = _attach_all(conn, [_message(row) for row in rows])
            return user, assistant

        return await self._db.run(_begin)

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str = "",
        *,
        model_used: str | None = None,
        state: MessageState = MessageState.COMPLETE,
        attachments: Sequence[NewAttachment] = (),
        reply_to: int | None = None,
    ) -> StoredMessage:
        """Insert a message and its attachments in one transaction."""
        stamp = _now()

        def _add(conn: sqlite3.Connection) -> StoredMessage:
            with conn:
                message_id = _insert_message(conn, conversation_id, role, content, stamp,
                                             model_used, state, attachments, reply_to)
                conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                             (stamp, conversation_id))
                row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
                message = _attach_all(conn, [_message(row)])[0]
            return message

        return await self._db.run(_add)

    async def attachments_for(self, message_id: int) -> list[StoredAttachment]:
        def _load(conn: sqlite3.Connection) -> list[StoredAttachment]:
            rows = conn.execute(
                "SELECT * FROM message_attachments WHERE message_id = ? ORDER BY id",
                (message_id,),
            ).fetchall()
            return [_attachment(r) for r in rows]

        return await self._db.run(_load)

    async def append_attachments(self, message_id: int, attachments: Sequence[NewAttachment]):
        """Keep generated artifacts even if a later tool or continuation fails."""
        def _append(conn: sqlite3.Connection):
            with conn:
                _insert_attachments(conn, message_id, attachments, _now())
        await self._db.run(_append)
        return await self.attachments_for(message_id)

    async def orphaned_blobs(self, storage_paths: Sequence[str]) -> list[str]:
        """Which of these stored files no message points at any more.

        Blobs are content-addressed and therefore shared, so "the message that
        referenced this is gone" is not the same as "this file is unused".
        The caller deletes; the database is the only thing that can answer
        which files are safe to delete.

        Both columns are checked. A document keeps its extracted text in a
        second blob, and a path that is some other row's ``text_path`` is
        still in use even if nothing points at it as ``storage_path``.
        """
        if not storage_paths:
            return []
        unique = list(dict.fromkeys(storage_paths))

        def _check(conn: sqlite3.Connection) -> list[str]:
            placeholders = ",".join("?" * len(unique))
            rows = conn.execute(
                "SELECT storage_path AS path FROM message_attachments "
                f"WHERE storage_path IN ({placeholders}) "
                "UNION "
                "SELECT text_path AS path FROM message_attachments "
                f"WHERE text_path IN ({placeholders})",
                (*unique, *unique),
            ).fetchall()
            still_used = {row["path"] for row in rows}
            return [path for path in unique if path not in still_used]

        return await self._db.run(_check)

    async def conversation_blobs(self, conversation_id: int) -> list[str]:
        """Every stored file the messages of one conversation reference.

        Extracted-document text is a file too, and a delete that forgot it
        would leak one blob per document for the life of the install.
        """

        def _paths(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT DISTINCT a.storage_path AS path FROM message_attachments a "
                "JOIN messages m ON m.id = a.message_id "
                "WHERE m.conversation_id = ? "
                "UNION "
                "SELECT DISTINCT a.text_path AS path FROM message_attachments a "
                "JOIN messages m ON m.id = a.message_id "
                "WHERE m.conversation_id = ? AND a.text_path IS NOT NULL",
                (conversation_id, conversation_id),
            ).fetchall()
            return [row["path"] for row in rows]

        return await self._db.run(_paths)

    async def checkpoint_content(self, message_id: int, content: str) -> bool:
        """Save a pending plain reply without changing its state or audit.

        A late checkpoint must never overwrite a terminal answer (or an audited
        draft, whose body and report must be saved together by save_audit).
        """
        def save(conn: sqlite3.Connection) -> bool:
            with conn:
                changed = conn.execute(
                    "UPDATE messages SET content=? WHERE id=? AND role='assistant' "
                    "AND state='pending' AND NOT EXISTS "
                    "(SELECT 1 FROM message_audits WHERE message_id=?)",
                    (content, message_id, message_id))
            return changed.rowcount == 1

        return await self._db.run(save)

    async def finish_message(
        self,
        message_id: int,
        content: str,
        state: MessageState,
        *,
        error: str | None = None,
        audit: dict | None = None,
        evidence: list[dict] | None = None,
        remember: bool = False,
    ) -> StoredMessage:
        """Atomically resolve an assistant row with its final audit and evidence.

        ``remember`` queues a complete reply for memory unless its chat is excluded.
        """
        stamp = _now()

        def _finish(conn: sqlite3.Connection) -> StoredMessage:
            if audit is not None:
                if audit['body_hash'] != digest(content):
                    raise ValueError('Audit does not match the final answer')
                conn.execute('INSERT OR REPLACE INTO message_audits VALUES (?, ?)',
                             (message_id, json.dumps(audit, ensure_ascii=False)))
            conn.execute(
                "UPDATE messages SET content = ?, state = ?, error = ? WHERE id = ?",
                (content, state.value, error, message_id),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? "
                "WHERE id = (SELECT conversation_id FROM messages WHERE id = ?)",
                (stamp, message_id),
            )
            if evidence is not None:
                conn.execute("INSERT OR REPLACE INTO evidence VALUES (?, ?)",
                             (message_id, json.dumps(evidence)))
            if remember and state == MessageState.COMPLETE:
                queued = conn.execute('''INSERT OR IGNORE INTO memory_outbox(kind, conversation_id,
                    message_id, created_at) SELECT 'turn', c.id, ?, ? FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.id = ? AND c.memory_excluded = 0''', (message_id, stamp, message_id))
                if queued.rowcount:
                    conn.execute('UPDATE conversations SET memory_used = 1 WHERE id = '
                                 '(SELECT conversation_id FROM messages WHERE id = ?)', (message_id,))
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            stored = _attach_all(conn, [_message(row)])[0]
            stored.audit = audit
            conn.commit()
            return stored

        return await self._db.run(_finish)

    async def messages(self, conversation_id: int) -> list[StoredMessage]:
        def _messages(conn: sqlite3.Connection) -> list[StoredMessage]:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
            return _load_messages(conn, rows)

        return await self._db.run(_messages)

    async def message_page(self, conversation_id: int, *, limit: int = 100,
                           before_id: int | None = None,
                           hide_assistant_after: int | None = None) -> MessagePage:
        """Newest visible rows, with one extra predecessor for a reply boundary."""
        if type(limit) is not int or limit < 1:
            raise ValueError('Message page size must be positive')

        def _page(conn: sqlite3.Connection) -> MessagePage:
            where = 'conversation_id=?'
            args = [conversation_id]
            if before_id is not None:
                where += ' AND id<?'
                args.append(before_id)
            if hide_assistant_after is not None:
                where += " AND (role!='assistant' OR id<=?)"
                args.append(hide_assistant_after)
            rows = conn.execute(f'SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?',
                                [*args, limit + 2]).fetchall()
            size = min(limit, len(rows))
            if size and len(rows) > size and rows[size - 1]['role'] == 'assistant':
                size += 1
            return MessagePage(_load_messages(conn, list(reversed(rows[:size]))),
                               len(rows) > size, _message_summary(conn, conversation_id))

        return await self._db.run(_page)

    async def message_summary(self, conversation_id: int) -> MessageSummary:
        return await self._db.run(lambda conn: _message_summary(conn, conversation_id))

    async def message(self, conversation_id: int, message_id: int) -> StoredMessage | None:
        def _get(conn: sqlite3.Connection) -> StoredMessage | None:
            rows = conn.execute('SELECT * FROM messages WHERE conversation_id=? AND id=?',
                                (conversation_id, message_id)).fetchall()
            messages = _load_messages(conn, rows)
            return messages[0] if messages else None
        return await self._db.run(_get)

    async def latest_request(self, conversation_id: int, *, exclude_prefix: str) -> StoredMessage | None:
        def _get(conn: sqlite3.Connection) -> StoredMessage | None:
            rows = conn.execute("SELECT * FROM messages WHERE conversation_id=? AND role='user' "
                                'AND substr(content, 1, ?) != ? ORDER BY id DESC LIMIT 1',
                                (conversation_id, len(exclude_prefix), exclude_prefix)).fetchall()
            messages = _load_messages(conn, rows)
            return messages[0] if messages else None
        return await self._db.run(_get)

    async def request_was_stored(self, conversation_id: int, baseline: int,
                                 text: str, max_chars: int) -> bool:
        return await self._db.run(lambda conn: conn.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND role='user' AND id>? "
            'AND substr(content, 1, ?)=? LIMIT 1',
            (conversation_id, baseline, max_chars, text)).fetchone() is not None)

    async def reap_pending(self) -> int:
        """Resolve rows left ``pending`` by a crash.

        Without this, an interrupted answer would look complete on restart.
        """

        def _reap(conn: sqlite3.Connection) -> int:
            for row in conn.execute('SELECT a.message_id, a.report, m.content FROM message_audits a '
                                    'JOIN messages m ON m.id=a.message_id WHERE m.state="pending"').fetchall():
                report = unchecked_report(json.loads(row['report']), row['content'],
                                          'Audit interrupted when Blackrose exited.')
                conn.execute('UPDATE message_audits SET report=? WHERE message_id=?',
                             (json.dumps(report, ensure_ascii=False), row['message_id']))
            for row in conn.execute("SELECT block_id, payload FROM message_blocks "
                                    "WHERE json_extract(payload, '$.status')='streaming'").fetchall():
                block = json.loads(row['payload'])
                block['status'] = 'cancelled'
                conn.execute('UPDATE message_blocks SET payload=? WHERE block_id=?',
                             (json.dumps(block), row['block_id']))
            conn.execute("UPDATE ui_events SET status='interrupted' WHERE status IN ('accepted','started')")
            cur = conn.execute(
                "UPDATE messages SET state = ?, error = ? "
                "WHERE state = ?",
                (
                    MessageState.ERROR.value,
                    "interrupted: Blackrose exited during generation",
                    MessageState.PENDING.value,
                ),
            )
            conn.commit()
            return cur.rowcount

        return await self._db.run(_reap)

    async def save_blocks(self, message_id, blocks):
        """Source becomes immutable on terminal acceptance; state lives elsewhere."""
        def save(conn):
            with conn:
                owner = conn.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
                if owner is None or owner['role'] != 'assistant':
                    raise ValueError('UI owner must be an assistant message')
                for block in blocks:
                    if block['message_id'] != message_id or block['conversation_id'] != owner['conversation_id']:
                        raise ValueError('UI ownership mismatch')
                    old = conn.execute('SELECT payload FROM message_blocks WHERE block_id=?',
                                       (block['block_id'],)).fetchone()
                    if old:
                        prior = json.loads(old['payload'])
                        if any(prior[key] != block[key] for key in (
                                'block_id', 'message_id', 'conversation_id', 'position', 'source_revision')):
                            raise ValueError('UI block identity is immutable')
                        if block['source_sequence'] < prior['source_sequence']:
                            raise ValueError('Stale UI source sequence')
                        if prior['status'] != 'streaming':
                            if prior != block:
                                raise ValueError('Finalized UI source is immutable')
                            continue
                    conn.execute('INSERT INTO message_blocks VALUES (?,?,?,?) '
                                 'ON CONFLICT(block_id) DO UPDATE SET payload=excluded.payload',
                                 (block['block_id'], message_id, block['position'], json.dumps(block)))
        await self._db.run(save)

    async def repair_block(self, message_id, block):
        """Replace one invalid block with its validated repair, advancing the revision."""
        def save(conn):
            with conn:
                owner = conn.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
                if owner is None or owner['role'] != 'assistant':
                    raise ValueError('UI owner must be an assistant message')
                row = conn.execute('SELECT payload FROM message_blocks WHERE block_id=?',
                                   (block['block_id'],)).fetchone()
                if row is None:
                    raise ValueError('UI block does not exist')
                prior = json.loads(row['payload'])
                if prior['status'] != 'invalid' or block['status'] != 'ready':
                    raise ValueError('Only an invalid UI block can be repaired')
                if any(prior[key] != block[key] for key in
                       ('block_id', 'message_id', 'conversation_id', 'position')):
                    raise ValueError('UI block identity is immutable')
                if block['source_revision'] != prior['source_revision'] + 1:
                    raise ValueError('UI repair must advance the source revision once')
                if block['message_id'] != message_id:
                    raise ValueError('UI ownership mismatch')
                conn.execute('UPDATE message_blocks SET payload=? WHERE block_id=?',
                             (json.dumps(block), block['block_id']))
        await self._db.run(save)

    async def claim_diagram_repair(self, conversation_id, message_id, block_id, index, source, limit):
        """Claim before inference; keep the original answer and all other blocks intact."""

        def claim(conn):
            with conn:
                owner = conn.execute('SELECT * FROM messages WHERE id=? AND conversation_id=? '
                    "AND role='assistant' AND state='complete'", (message_id, conversation_id)).fetchone()
                if owner is None:
                    return {'status': 'unavailable'}
                blocks = [json.loads(row[0]) for row in conn.execute(
                    'SELECT payload FROM message_blocks WHERE message_id=? ORDER BY position', (message_id,))]
                target = next((b for b in blocks if b['block_id'] == block_id), None)
                if not block_id and not blocks:
                    target = {'block_id': f'diagram-{message_id}', 'message_id': message_id,
                        'conversation_id': conversation_id, 'position': 0, 'kind': 'markdown',
                        'source': owner['content'], 'source_revision': 1, 'source_sequence': 1,
                        'protocol_version': 1, 'library_version': 'blackrose-markdown/1',
                        'schema_hash': '', 'source_hash': hashlib.sha256(owner['content'].encode()).hexdigest(), 'document': None,
                        'status': 'ready', 'fallback_text': owner['content'], 'diagram_promoted': True}
                if target is None or target['kind'] != 'markdown':
                    return {'status': 'unavailable'}
                tokens = diagram_tokens(target['source'])
                if not 0 <= index < len(tokens) or tokens[index].content != source:
                    return {'status': 'stale'}
                repairs = target.setdefault('diagram_repairs', {})
                if str(index) in repairs:
                    prior = repairs[str(index)]
                    if prior['status'] == 'repairing':
                        prior['status'] = 'interrupted'
                    elif prior['status'] == 'updated':
                        prior['status'] = 'failed'
                    conn.execute('UPDATE message_blocks SET payload=? WHERE block_id=?',
                        (json.dumps(target), target['block_id']))
                    return {'status': prior['status']}
                used = sum(len(b.get('diagram_repairs', {})) for b in blocks)
                if used >= limit:
                    return {'status': 'disabled' if limit == 0 else 'exhausted'}
                repairs[str(index)] = {'status': 'repairing', 'source': source}
                conn.execute('INSERT INTO message_blocks VALUES (?,?,?,?) '
                    'ON CONFLICT(block_id) DO UPDATE SET payload=excluded.payload',
                    (target['block_id'], message_id, target['position'], json.dumps(target)))
                return {'status': 'repairing', 'block_id': target['block_id'],
                        'message_id': message_id, 'conversation_id': conversation_id,
                        'index': index, 'source': source}
        return await self._db.run(claim)

    async def finish_diagram_repair(self, claim, status, source=None):
        def finish(conn):
            with conn:
                row = conn.execute('SELECT payload FROM message_blocks WHERE block_id=? AND message_id=?',
                    (claim['block_id'], claim['message_id'])).fetchone()
                if row is None:
                    return
                block = json.loads(row[0])
                repair = block.get('diagram_repairs', {}).get(str(claim['index']))
                if not repair or repair['status'] != 'repairing' or repair['source'] != claim['source']:
                    return
                if source is not None:
                    block['source'] = replace_diagram(block['source'], claim['index'], source)
                    block['source_hash'] = hashlib.sha256(block['source'].encode()).hexdigest()
                    block['fallback_text'] = block['source']
                    block['source_revision'] += 1
                    block['source_sequence'] += 1
                repair['status'] = status
                conn.execute('UPDATE message_blocks SET payload=? WHERE block_id=?',
                    (json.dumps(block), block['block_id']))
        await self._db.run(finish)

    async def ui_block(self, conversation_id: int, message_id: int, block_id: str) -> UIBlock:
        def read(conn):
            row = conn.execute('SELECT b.payload, s.sequence, s.state FROM message_blocks b '
                'JOIN messages m ON m.id=b.message_id LEFT JOIN ui_state s USING(block_id) '
                'WHERE b.block_id=? AND b.message_id=? AND m.conversation_id=? AND m.role=\'assistant\'',
                (block_id, message_id, conversation_id)).fetchone()
            if not row:
                raise ValueError('UI block is not owned by this conversation')
            return cast(UIBlock, json.loads(row['payload']) | {'saved_state': json.loads(row['state'] or '{}'),
                                                               'state_sequence': row['sequence'] or 0})
        return await self._db.run(read)

    async def save_ui_state(self, block_id: str, sequence: int, state: dict[str, Any]) -> int:
        def save(conn):
            with conn:
                row = conn.execute('SELECT sequence FROM ui_state WHERE block_id=?', (block_id,)).fetchone()
                current = row['sequence'] if row else 0
                if sequence != current + 1:
                    raise ValueError('Stale UI state sequence')
                conn.execute('INSERT INTO ui_state VALUES (?,?,?) ON CONFLICT(block_id) '
                    'DO UPDATE SET sequence=excluded.sequence, state=excluded.state',
                    (block_id, sequence, json.dumps(state)))
            return sequence
        return await self._db.run(save)

    async def accept_ui_event(self, block_id: str, action_id: str, event_id: str,
                              payload: dict[str, Any]) -> None:
        def save(conn):
            try:
                with conn:
                    conn.execute('INSERT INTO ui_events(event_id,block_id,action_id,payload,created_at) '
                                 'VALUES (?,?,?,?,?)', (event_id, block_id, action_id, json.dumps(payload), _now()))
            except sqlite3.IntegrityError as error:
                raise ValueError('Duplicate or in-flight UI submission') from error
        await self._db.run(save)

    async def ui_event_context(self, event_id, conversation_id):
        def read(conn):
            row = conn.execute('SELECT e.payload AS event_payload, b.payload AS block_payload '
                'FROM ui_events e JOIN message_blocks b USING(block_id) JOIN messages m ON m.id=b.message_id '
                'WHERE e.event_id=? AND e.status=\'accepted\' AND m.conversation_id=?',
                (event_id, conversation_id)).fetchone()
            if row is None:
                raise ValueError('UI event is stale or belongs to another conversation')
            return _ui_context(row)
        return await self._db.run(read)

    async def finish_ui_event(self, event_id: str, status: str, user_message_id: int | None = None) -> None:
        def save(conn):
            conn.execute('UPDATE ui_events SET status=?, user_message_id=COALESCE(?,user_message_id) WHERE event_id=?',
                         (status, user_message_id, event_id))
            conn.commit()
        await self._db.run(save)

    async def save_evidence(self, message_id: int, sources: list[dict]) -> None:
        def save(conn):
            conn.execute("INSERT OR REPLACE INTO evidence VALUES (?, ?)",
                         (message_id, json.dumps(sources)))
            conn.commit()
        await self._db.run(save)

    async def evidence_for(self, message_id: int) -> list[dict]:
        def read(conn):
            row = conn.execute("SELECT sources FROM evidence WHERE message_id=?", (message_id,)).fetchone()
            return json.loads(row[0]) if row else []
        return await self._db.run(read)

    async def evidence_for_messages(self, message_ids: Sequence[int]) -> dict[int, list[dict]]:
        """Saved sources in bounded batches; absent rows map to []."""
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            return {}

        def read(conn):
            found: dict[int, list[dict]] = {message_id: [] for message_id in ids}
            batch_size = min(500, conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
            for start in range(0, len(ids), batch_size):
                batch = ids[start:start + batch_size]
                for row in conn.execute(f"SELECT message_id, sources FROM evidence "
                                        f"WHERE message_id IN ({','.join('?' * len(batch))})", batch):
                    found[row['message_id']] = json.loads(row['sources'])
            return found
        return await self._db.run(read)

    async def save_audit(self, message_id: int, report: dict, *, content: str | None = None) -> None:
        """Checkpoint only a pending assistant; completed reports are immutable."""
        def save(conn):
            row = conn.execute('SELECT role, state FROM messages WHERE id=?', (message_id,)).fetchone()
            if row is None or row['role'] != 'assistant' or row['state'] != 'pending':
                raise ValueError('Audit checkpoints require a pending assistant message')
            if content is not None:
                if report['body_hash'] != digest(content):
                    raise ValueError('Audit checkpoint does not match the draft')
                conn.execute('UPDATE messages SET content=? WHERE id=?', (content, message_id))
            conn.execute('INSERT OR REPLACE INTO message_audits VALUES (?, ?)',
                         (message_id, json.dumps(report, ensure_ascii=False)))
            conn.commit()
        await self._db.run(save)

    async def audit_for(self, message_id: int) -> dict | None:
        def read(conn):
            row = conn.execute('SELECT report FROM message_audits WHERE message_id=?', (message_id,)).fetchone()
            return json.loads(row[0]) if row else None
        return await self._db.run(read)
