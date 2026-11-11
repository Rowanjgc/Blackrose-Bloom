"""SM-02: persisted message lifecycle, terminal settlement and usable history.

These tests pin the transitions documented in
``docs/state-machines/sm-02-stored-message-lifecycle.md``: what may move a
``messages.state`` value, what a checkpoint may write, what startup recovery
settles, and which rows the prompt builder is allowed to replay.

Tests whose name ends in ``documented_follow_up`` are characterization tests for
a guard the repository does not have yet. They assert today's behavior so the
gap is visible in the suite; they must be inverted when the proposed guard in
the specification lands.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
import tempfile
import unittest
from pathlib import Path

from blackrose.citations import EvidenceRegistry, digest, new_report
from blackrose.prompts import wire_message
from blackrose.storage.db import Database
from blackrose.storage.records import MessageState, StoredAttachment, StoredMessage
from blackrose.storage.repository import NewAttachment, Repository


def attachment(filename="image.png"):
    return NewAttachment("image/png", filename, "blob.png", "digest", 12)


def stored_attachment(message_id=1, kind="image", filename="image.png"):
    return StoredAttachment(
        id=1, message_id=message_id, kind=kind, mime_type="image/png", filename=filename,
        storage_path="blob.png", size=12, digest="digest",
        created_at=datetime(2026, 1, 1, tzinfo=UTC))


def block(index=0, status="ready", kind="markdown", source="block source"):
    return {"block_id": f"block-{index}", "message_id": 1, "conversation_id": 1,
            "position": index, "kind": kind, "protocol_version": 1,
            "library_version": "blackrose-markdown/1", "schema_hash": "", "source_revision": 1,
            "source_sequence": 1, "source": source, "source_hash": "", "status": status,
            "fallback_text": source, "document": None}


def message(role, content="", state=MessageState.COMPLETE, *, attachments=(), blocks=()):
    return StoredMessage(id=1, conversation_id=1, role=role, content=content,
                         created_at=datetime(2026, 1, 1, tzinfo=UTC), state=state,
                         attachments=list(attachments), blocks=list(blocks))


class _Store:
    """Stand-in for the attachment store's byte reader."""

    def read(self, path):
        return b"PNGDATA"


class MessageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "chats.db"
        self.db = Database(self.path)
        await self.db.connect()
        self.repo = Repository(self.db)
        self.conversation = await self.repo.create_conversation()

    async def asyncTearDown(self):
        await self.db.close()
        self.temporary.cleanup()

    async def latest(self):
        return (await self.repo.messages(self.conversation.id))[-1]

    async def audit(self, message_id, text, mode="balanced"):
        registry = EvidenceRegistry(message_id)
        return new_report(mode, "test-model", text, registry)

    # --- creation ---------------------------------------------------------

    async def test_begin_turn_creates_one_complete_request_and_one_pending_reply(self):
        user, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a",
                                                [attachment()], refs=[{"kind": "file", "path": "a.txt"}])
        self.assertEqual((user.role, user.state, user.error), ("user", MessageState.COMPLETE, None))
        self.assertTrue(user.is_usable_history)
        self.assertEqual((reply.role, reply.state, reply.content, reply.error),
                         ("assistant", MessageState.PENDING, "", None))
        self.assertFalse(reply.is_usable_history)
        self.assertEqual(reply.reply_to, user.id)
        self.assertEqual(user.refs, [{"kind": "file", "path": "a.txt"}])
        # The conversation's ordering stamp moves with the turn, not with settlement.
        stored = await self.repo.get_conversation(self.conversation.id)
        self.assertGreaterEqual(stored.updated_at, self.conversation.updated_at)

    async def test_schema_accepts_only_the_four_lifecycle_states_and_three_roles(self):
        _, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a")
        for sql, args in (
            ("UPDATE messages SET state='lost' WHERE id=?", (reply.id,)),
            ("UPDATE messages SET role='tool' WHERE id=?", (reply.id,)),
        ):
            def apply(conn, sql=sql, args=args):
                conn.execute(sql, args)
                conn.commit()
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                await self.db.run(apply)
        # A raw insert that names no state still lands on the schema default.
        def raw(conn):
            cursor = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                (self.conversation.id, "user", "raw", "2026-01-01T00:00:00+00:00"))
            conn.commit()
            return cursor.lastrowid
        raw_id = await self.db.run(raw)
        stored = [m for m in await self.repo.messages(self.conversation.id) if m.id == raw_id][0]
        self.assertEqual(stored.state, MessageState.COMPLETE)

    # --- pending-state writes --------------------------------------------

    async def test_checkpoint_content_only_writes_pending_unaudited_assistant_text(self):
        user, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a")
        self.assertTrue(await self.repo.checkpoint_content(reply.id, "prefix"))
        current = await self.latest()
        self.assertEqual((current.content, current.state, current.error), ("prefix", MessageState.PENDING, None))
        self.assertFalse(await self.repo.checkpoint_content(user.id, "wrong"))
        self.assertFalse(await self.repo.checkpoint_content(-1, "wrong"))
        await self.repo.finish_message(reply.id, "final", MessageState.COMPLETE)
        self.assertFalse(await self.repo.checkpoint_content(reply.id, "late"))
        self.assertEqual((await self.latest()).content, "final")

        _, audited = await self.repo.begin_turn(self.conversation.id, "audited", "model-a")
        report = await self.audit(audited.id, "audited prefix")
        await self.repo.save_audit(audited.id, report, content="audited prefix")
        self.assertFalse(await self.repo.checkpoint_content(audited.id, "wrong"))
        self.assertEqual((await self.latest()).content, "audited prefix")
        self.assertEqual(await self.repo.audit_for(audited.id), report)

    async def test_save_audit_requires_a_pending_assistant_and_keeps_body_and_report_atomic(self):
        user, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a")
        report = await self.audit(reply.id, "draft")
        with self.assertRaises(ValueError):
            await self.repo.save_audit(user.id, report, content="draft")
        with self.assertRaises(ValueError):
            await self.repo.save_audit(-1, report, content="draft")
        # A report whose hash does not match the body is refused before any write.
        with self.assertRaises(ValueError):
            await self.repo.save_audit(reply.id, report, content="different draft")
        self.assertIsNone(await self.repo.audit_for(reply.id))
        self.assertEqual((await self.latest()).content, "")
        await self.repo.finish_message(reply.id, "final", MessageState.COMPLETE)
        with self.assertRaises(ValueError):
            await self.repo.save_audit(reply.id, report, content="late")
        self.assertEqual(await self.repo.audit_for(reply.id), None)

    # --- terminal settlement ---------------------------------------------

    async def test_finish_message_settles_content_state_error_audit_and_evidence_together(self):
        for state, error in ((MessageState.COMPLETE, None), (MessageState.CANCELLED, "Stopped"),
                             (MessageState.ERROR, "RuntimeError")):
            _, reply = await self.repo.begin_turn(self.conversation.id, state.value, "model-a")
            report = await self.audit(reply.id, "answer")
            stored = await self.repo.finish_message(
                reply.id, "answer", state, error=error, audit=report,
                evidence=[{"title": "Source"}])
            self.assertEqual((stored.content, stored.state, stored.error), ("answer", state, error))
            self.assertEqual(stored.audit, report)
            self.assertEqual(await self.repo.evidence_for(reply.id), [{"title": "Source"}])
            self.assertEqual(await self.repo.audit_for(reply.id), report)
            self.assertEqual((await self.latest()).state, state)

    async def test_finish_message_rejects_an_audit_that_does_not_match_the_answer(self):
        _, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a")
        report = await self.audit(reply.id, "answer")
        with self.assertRaises(ValueError):
            await self.repo.finish_message(reply.id, "revised answer", MessageState.COMPLETE,
                                           audit=report, evidence=[{"title": "Source"}])
        current = await self.latest()
        self.assertEqual((current.content, current.state), ("", MessageState.PENDING))
        self.assertIsNone(await self.repo.audit_for(reply.id))
        self.assertEqual(await self.repo.evidence_for(reply.id), [])

    async def test_memory_outbox_is_queued_only_for_a_complete_remembered_turn(self):
        for state in (MessageState.ERROR, MessageState.CANCELLED):
            _, reply = await self.repo.begin_turn(self.conversation.id, state.value, "model-a")
            await self.repo.finish_message(reply.id, "partial", state, error="reason", remember=True)
            self.assertEqual(await self.repo.memory_jobs(), [])
        _, reply = await self.repo.begin_turn(self.conversation.id, "remembered", "model-a")
        await self.repo.finish_message(reply.id, "answer", MessageState.COMPLETE, remember=True)
        self.assertEqual([job["message_id"] for job in await self.repo.memory_jobs()], [reply.id])
        # Re-settling a complete turn does not duplicate the queued row.
        await self.repo.finish_message(reply.id, "answer", MessageState.COMPLETE, remember=True)
        self.assertEqual([job["message_id"] for job in await self.repo.memory_jobs()], [reply.id])

    # --- startup recovery -------------------------------------------------

    async def test_reap_pending_settles_the_message_its_audit_blocks_and_events(self):
        _, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a")
        registry = EvidenceRegistry(reply.id)
        report = new_report("balanced", "test-model", "partial", registry)
        await self.repo.save_audit(reply.id, report, content="partial")
        await self.repo.save_evidence(reply.id, [{"title": "Captured"}])

        def stream(conn):
            conn.execute("INSERT INTO message_blocks VALUES ('streaming', ?, 0, ?)",
                         (reply.id, json.dumps({"kind": "markdown", "status": "streaming"})))
            conn.execute("INSERT INTO message_blocks VALUES ('ready', ?, 1, ?)",
                         (reply.id, json.dumps({"kind": "markdown", "status": "ready"})))
            conn.execute("INSERT INTO ui_events(event_id, block_id, action_id, payload, status, created_at) "
                         "VALUES ('accepted', 'ready', 'a', '{}', 'accepted', 'now'), "
                         "('started', 'ready', 'b', '{}', 'started', 'now'), "
                         "('complete', 'ready', 'c', '{}', 'complete', 'now')")
            conn.commit()
        await self.db.run(stream)

        self.assertEqual(await self.repo.reap_pending(), 1)
        current = await self.latest()
        self.assertEqual(current.state, MessageState.ERROR)
        self.assertIn("interrupted: Blackrose exited during generation", current.error)
        self.assertEqual(current.content, "partial")
        self.assertFalse(current.is_usable_history)
        # The crashed audit keeps its captured sources and body, and is marked incomplete.
        saved = await self.repo.audit_for(reply.id)
        self.assertEqual(saved["status"], "incomplete")
        self.assertEqual(saved["body_hash"], digest("partial"))
        self.assertEqual(await self.repo.evidence_for(reply.id), [{"title": "Captured"}])
        self.assertEqual(await self.db.run(lambda conn: [tuple(row) for row in conn.execute(
            "SELECT block_id, json_extract(payload, '$.status') FROM message_blocks ORDER BY block_id")]),
            [("ready", "ready"), ("streaming", "cancelled")])
        self.assertEqual(await self.db.run(lambda conn: [row[0] for row in conn.execute(
            "SELECT status FROM ui_events ORDER BY event_id")]),
            ["interrupted", "complete", "interrupted"])

    async def test_reap_pending_is_idempotent_and_never_rewrites_terminal_rows(self):
        settled = {}
        for state, error in ((MessageState.COMPLETE, None), (MessageState.CANCELLED, "Stopped"),
                             (MessageState.ERROR, "RuntimeError")):
            _, reply = await self.repo.begin_turn(self.conversation.id, state.value, "model-a")
            report = await self.audit(reply.id, f"{state.value} answer")
            await self.repo.finish_message(reply.id, f"{state.value} answer", state, error=error,
                                           audit=report)
            settled[reply.id] = (state, error, report)
        _, pending = await self.repo.begin_turn(self.conversation.id, "crash", "model-a")

        self.assertEqual(await self.repo.reap_pending(), 1)
        self.assertEqual(await self.repo.reap_pending(), 0)
        messages = await self.repo.messages(self.conversation.id)
        for current in messages:
            if current.role == "user":
                continue
            if current.id == pending.id:
                self.assertEqual(current.state, MessageState.ERROR)
                continue
            state, error, report = settled[current.id]
            self.assertEqual((current.state, current.error), (state, error), current.content)
            self.assertEqual(await self.repo.audit_for(current.id), report)
        self.assertEqual(sum(m.state is MessageState.PENDING for m in messages), 0)

    async def test_reaping_an_audited_pending_row_is_stable_across_reopen(self):
        _, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a")
        report = await self.audit(reply.id, "partial")
        await self.repo.save_audit(reply.id, report, content="partial")
        await self.repo.reap_pending()
        first = await self.repo.audit_for(reply.id)
        await self.db.close()
        await self.db.connect()
        self.assertEqual(await self.repo.reap_pending(), 0)
        self.assertEqual(await self.repo.audit_for(reply.id), first)
        self.assertEqual((await self.repo.messages(self.conversation.id))[-1].state,
                         MessageState.ERROR)

    # --- usable history ---------------------------------------------------

    async def test_usable_history_truth_table(self):
        cases = (
            ("user text", message("user", "question"), True),
            ("user attachment only", message("user", "", attachments=[stored_attachment()]), True),
            ("user blank", message("user", "   "), False),
            ("user blank with blocks", message("user", "", blocks=[block()]), False),
            ("assistant pending", message("assistant", "text", MessageState.PENDING), False),
            ("assistant pending with blocks",
             message("assistant", "text", MessageState.PENDING, blocks=[block()]), False),
            ("assistant error with text",
             message("assistant", "partial text", MessageState.ERROR), False),
            ("assistant complete", message("assistant", "answer"), True),
            ("assistant complete blank", message("assistant", "   "), False),
            ("assistant complete attachment only",
             message("assistant", "", attachments=[stored_attachment()]), True),
            ("assistant cancelled with text",
             message("assistant", "partial", MessageState.CANCELLED), True),
            ("assistant cancelled with blocks only",
             message("assistant", "", MessageState.CANCELLED, blocks=[block()]), True),
            ("assistant cancelled blank",
             message("assistant", "   ", MessageState.CANCELLED), False),
        )
        for label, stored, expected in cases:
            self.assertIs(stored.is_usable_history, expected, label)

    async def test_history_wire_reads_what_usable_history_admits(self):
        store = _Store()
        cancelled_blocks = message("assistant", "", MessageState.CANCELLED,
                                   blocks=[block(source="persisted block source")])
        self.assertTrue(cancelled_blocks.is_usable_history)
        self.assertEqual(wire_message(cancelled_blocks, store, True)["content"],
                         "persisted block source")

        errored = message("assistant", "partial answer", MessageState.ERROR)
        self.assertFalse(errored.is_usable_history)
        # The wire serializer would carry the text; only the usability predicate drops it.
        self.assertEqual(wire_message(errored, store, True)["content"], "partial answer")

        image_only = message("user", "", attachments=[stored_attachment()])
        wire = wire_message(image_only, store, True)
        self.assertTrue(image_only.is_usable_history)
        self.assertIsInstance(wire["content"], list)
        self.assertEqual(wire["content"][1]["image_url"]["url"],
                         "data:image/png;base64," + "UE5HREFUQQ==")

    # --- documented gaps --------------------------------------------------

    async def test_finish_message_can_rewrite_a_terminal_row_documented_follow_up(self):
        """The repository, unlike checkpoint_content/save_audit, has no settle guard.

        No product path reaches this: TurnRunner settles only while ``completed``
        is unset (``TurnRunner._run``) and ``reap_pending`` filters on ``pending``.
        The specification proposes adding ``AND role='assistant' AND state='pending'``
        to ``Repository.finish_message``; invert this test when that lands.
        """
        user, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a")
        await self.repo.finish_message(reply.id, "answer", MessageState.COMPLETE)
        await self.repo.finish_message(reply.id, "stopped instead", MessageState.CANCELLED,
                                       error="Stopped")
        self.assertEqual((await self.latest()).state, MessageState.CANCELLED)
        # The same statement is not role-scoped either: any row id is accepted.
        await self.repo.finish_message(user.id, "rewritten", MessageState.ERROR, error="reason")
        user_after = (await self.repo.messages(self.conversation.id))[0]
        self.assertEqual((user_after.content, user_after.state), ("rewritten", MessageState.ERROR))

    async def test_finish_message_does_not_couple_error_text_to_state_documented_follow_up(self):
        """Only TurnRunner's discipline keeps COMPLETE/error NULL and failures named."""
        _, completed = await self.repo.begin_turn(self.conversation.id, "complete", "model-a")
        await self.repo.finish_message(completed.id, "answer", MessageState.COMPLETE, error="boom")
        stored = [m for m in await self.repo.messages(self.conversation.id) if m.id == completed.id][0]
        self.assertEqual((stored.state, stored.error), (MessageState.COMPLETE, "boom"))
        _, failed = await self.repo.begin_turn(self.conversation.id, "error", "model-a")
        await self.repo.finish_message(failed.id, "partial", MessageState.ERROR)
        stored = [m for m in await self.repo.messages(self.conversation.id) if m.id == failed.id][0]
        self.assertEqual((stored.state, stored.error), (MessageState.ERROR, None))


if __name__ == "__main__":
    unittest.main()
