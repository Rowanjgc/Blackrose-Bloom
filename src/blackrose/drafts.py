"""Durable per-conversation composer drafts.

A draft is unsent user work. It is retained until it is sent, cleared or its
conversation is deleted, when the rows cascade. Attachment copies live in the
chat database rather than the content-addressed attachment store, so a draft
never creates files that artifact cleanup or reconciliation must protect.

Saved references and attachments record what the user selected; they are not
authority. The desktop session revalidates them against the current workspace
grants and attachment rules before showing them again.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

MAX_DRAFT_CHARS = 200_000
MAX_DRAFT_REFS = 20
_REF_KEYS = ('token', 'kind', 'id', 'name', 'path', 'filename', 'identity')
_MAX_REF_BYTES = 8192


def bounded_refs(refs):
    """Keep only plain selection fields, so page data cannot grow the row."""
    result = []
    for ref in list(refs)[:MAX_DRAFT_REFS] if isinstance(refs, (list, tuple)) else ():
        if isinstance(ref, dict) and isinstance(ref.get('token'), str):
            kept = {key: ref[key] for key in _REF_KEYS if key in ref}
            if len(json.dumps(kept)) <= _MAX_REF_BYTES:
                result.append(kept)
    return result


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


class ComposerDrafts:
    """Draft rows, written through the chat database's serialized worker."""

    def __init__(self, repository) -> None:
        self._repo = repository
        # Digest each attachment once; a text-only save writes no bytes.
        self._digests: dict[int, tuple[object, str]] = {}

    def _digest(self, pending) -> str:
        cached = self._digests.get(id(pending))
        if cached is None or cached[0] is not pending:
            cached = (pending, hashlib.sha256(pending.data).hexdigest())
            self._digests[id(pending)] = cached
        return cached[1]

    async def save(self, conversation_id: int, text: str, refs, attachments, sending=None) -> bool:
        """Replace one conversation's draft. Returns False once it is deleted.

        ``sending`` is a submitted request that may not be stored yet:
        ``{'text', 'refs', 'attachments', 'baseline'}``. It stays recoverable
        until the session observes whether its turn was admitted.
        """
        pending = [*attachments, *(sending['attachments'] if sending else ())]
        digests = await asyncio.to_thread(lambda: [self._digest(a) for a in pending])
        self._digests = {id(a): self._digests[id(a)] for a in pending}
        files = {digest: a.data for digest, a in zip(digests, pending, strict=True)}

        def entries(items):
            return json.dumps([{'digest': self._digest(a), 'filename': a.filename} for a in items])

        text = text[:MAX_DRAFT_CHARS]
        refs = bounded_refs(refs)
        saved_sending = json.dumps({
            'text': sending['text'][:MAX_DRAFT_CHARS], 'refs': bounded_refs(sending['refs']),
            'attachments': json.loads(entries(sending['attachments'])),
            'baseline': int(sending['baseline'])}) if sending else None
        empty = not (text or refs or attachments or sending)
        row = (text, json.dumps(refs), entries(attachments), saved_sending,
               datetime.now(UTC).isoformat(timespec='microseconds'), conversation_id)

        def write(conn) -> bool:
            with conn:
                if empty:
                    conn.execute('DELETE FROM composer_drafts WHERE conversation_id=?', (conversation_id,))
                    return True
                # An upsert, never REPLACE: replacing the row would cascade away
                # its files. Selecting the owner refuses a deleted conversation.
                inserted = conn.execute(
                    'INSERT INTO composer_drafts(conversation_id, text, refs, attachments, sending, updated_at) '
                    'SELECT id, ?, ?, ?, ?, ? FROM conversations WHERE id=? '
                    'ON CONFLICT(conversation_id) DO UPDATE SET text=excluded.text, refs=excluded.refs, '
                    'attachments=excluded.attachments, sending=excluded.sending, '
                    'updated_at=excluded.updated_at', row).rowcount
                if not inserted:
                    return False
                stored = {r[0] for r in conn.execute(
                    'SELECT digest FROM composer_draft_files WHERE conversation_id=?', (conversation_id,))}
                conn.executemany('INSERT INTO composer_draft_files VALUES (?,?,?)',
                                 [(conversation_id, d, files[d]) for d in files.keys() - stored])
                conn.executemany('DELETE FROM composer_draft_files WHERE conversation_id=? AND digest=?',
                                 [(conversation_id, d) for d in stored - files.keys()])
            return True

        return await self._repo._db.run(write)

    async def load(self, conversation_id: int) -> dict | None:
        """The saved draft, with ``None`` in place of any missing attachment copy."""
        def read(conn):
            row = conn.execute('SELECT * FROM composer_drafts WHERE conversation_id=?',
                               (conversation_id,)).fetchone()
            if row is None:
                return None
            files = {r[0]: r[1] for r in conn.execute(
                'SELECT digest, data FROM composer_draft_files WHERE conversation_id=?', (conversation_id,))}
            return dict(row), files

        loaded = await self._repo._db.run(read)
        if loaded is None:
            return None
        row, files = loaded

        def attachments(items):
            items = items if isinstance(items, list) else []
            return [(str(item.get('filename') or 'attachment'), files.get(item.get('digest')))
                    for item in items if isinstance(item, dict)][:16]

        text = row['text'] if isinstance(row['text'], str) else ''
        sending = _loads(row['sending'], None)
        if isinstance(sending, dict) and isinstance(sending.get('text'), str):
            sending = {'text': sending['text'], 'refs': bounded_refs(sending.get('refs')),
                       'attachments': attachments(sending.get('attachments')),
                       'baseline': sending.get('baseline') if type(sending.get('baseline')) is int else 0}
        else:
            sending = None
        return {'text': text, 'truncated': len(text) >= MAX_DRAFT_CHARS,
                'refs': bounded_refs(_loads(row['refs'], [])),
                'attachments': attachments(_loads(row['attachments'], [])), 'sending': sending}
