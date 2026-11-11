"""Persistence contracts, including failures between otherwise valid writes."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from blackrose.storage.db import Database, StorageError
from blackrose.storage.migrations import MIGRATIONS
from blackrose.storage.records import MessageState
from blackrose.storage.repository import NewAttachment, Repository


def attachment(filename="ok.png"):
    return NewAttachment("image/png", filename, "blob.png", "digest", 12)


class StorageTests(unittest.IsolatedAsyncioTestCase):
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

    async def reject_attachment(self):
        def install(conn):
            conn.executescript("""
                CREATE TRIGGER reject_attachment BEFORE INSERT ON message_attachments
                WHEN NEW.filename='fail.png' BEGIN
                    SELECT RAISE(ABORT, 'injected attachment failure');
                END;
            """)
        await self.db.run(install)

    async def test_failed_add_rolls_back_all_rows_before_an_unrelated_commit(self):
        await self.reject_attachment()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.repo.add_message(self.conversation.id, "user", "request",
                                        attachments=[attachment(), attachment("fail.png")])
        self.assertFalse(self.db.connection.in_transaction)
        await self.repo.create_conversation("unrelated write")
        with closing(sqlite3.connect(self.path)) as disk, disk:
            self.assertEqual(disk.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(disk.execute("SELECT count(*) FROM message_attachments").fetchone()[0], 0)

    async def test_failed_append_preserves_existing_attachments_only(self):
        await self.reject_attachment()
        message = await self.repo.add_message(self.conversation.id, "assistant", "answer",
                                              attachments=[attachment("existing.png")])
        with self.assertRaises(sqlite3.IntegrityError):
            await self.repo.append_attachments(message.id, [attachment(), attachment("fail.png")])
        await self.repo.touch_conversation(self.conversation.id)
        self.assertEqual([a.filename for a in await self.repo.attachments_for(message.id)],
                         ["existing.png"])

    async def test_database_rolls_back_arbitrary_failed_callbacks(self):
        def partial(conn):
            conn.execute("UPDATE conversations SET title='partial'")
            raise RuntimeError("operation failed")
        with self.assertRaisesRegex(RuntimeError, "operation failed"):
            await self.db.run(partial)
        await self.repo.touch_conversation(self.conversation.id)
        self.assertEqual((await self.repo.get_conversation(self.conversation.id)).title,
                         self.conversation.title)

    async def test_begin_turn_commits_request_attachments_and_pending_reply(self):
        user, assistant = await self.repo.begin_turn(self.conversation.id, "request", "model-a",
                                                    [attachment()])
        self.assertEqual(user.role, "user")
        self.assertEqual(user.content, "request")
        self.assertEqual(user.state, MessageState.COMPLETE)
        self.assertEqual(len(user.attachments), 1)
        self.assertEqual(assistant.role, "assistant")
        self.assertEqual(assistant.state, MessageState.PENDING)
        self.assertEqual(assistant.reply_to, user.id)
        self.assertEqual(assistant.model_used, "model-a")
        await self.db.close()
        await self.db.connect()
        messages = await self.repo.messages(self.conversation.id)
        self.assertEqual([m.id for m in messages], [user.id, assistant.id])
        self.assertEqual(messages[1].reply_to, messages[0].id)

    async def test_full_history_details_and_evidence_respect_sqlite_parameter_limit(self):
        for i in range(40):
            request, reply = await self.repo.begin_turn(self.conversation.id, f'Request {i}', 'model',
                                                       [attachment()])
            await self.repo.save_evidence(reply.id, [{'title': f'Source {i}'}])
            await self.db.run(lambda conn, identity=reply.id: (
                conn.execute('INSERT INTO message_audits VALUES (?, ?)',
                             (identity, json.dumps({'status': 'unchecked'}))), conn.commit()))
        original_limit = self.db.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 16)
        try:
            messages = await self.repo.messages(self.conversation.id)
            sources = await self.repo.evidence_for_messages([m.id for m in messages])
        finally:
            self.db.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original_limit)
        self.assertEqual(len(messages), 80)
        self.assertEqual(sum(len(m.attachments) for m in messages), 40)
        self.assertEqual(sum(m.audit is not None for m in messages), 40)
        self.assertEqual(sum(len(items) for items in sources.values()), 40)

    async def test_begin_turn_rolls_back_request_if_pending_reply_fails(self):
        def reject(conn):
            conn.executescript("""
                CREATE TRIGGER reject_reply BEFORE INSERT ON messages
                WHEN NEW.role='assistant' BEGIN
                    SELECT RAISE(ABORT, 'injected reply failure');
                END;
            """)
        await self.db.run(reject)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.repo.begin_turn(self.conversation.id, "request", "model-a", [attachment()])
        await self.repo.create_conversation("unrelated write")
        self.assertEqual(await self.repo.messages(self.conversation.id), [])
        count = await self.db.run(lambda conn: conn.execute("SELECT count(*) FROM message_attachments").fetchone()[0])
        self.assertEqual(count, 0)
        self.assertEqual((await self.repo.get_conversation(self.conversation.id)).updated_at,
                         self.conversation.updated_at)

    async def test_delete_cascades_turn_attachments_and_evidence(self):
        user, assistant = await self.repo.begin_turn(self.conversation.id, "request", "model-a", [attachment()])
        await self.repo.save_evidence(assistant.id, [{"title": "source"}])
        await self.repo.finish_message(assistant.id, "answer", MessageState.COMPLETE)
        await self.repo.delete_conversation(self.conversation.id)
        for table in ("messages", "message_attachments", "evidence"):
            count = await self.db.run(lambda conn, table=table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            self.assertEqual(count, 0, table)
        self.assertEqual(await self.repo.orphaned_blobs(["blob.png"]), ["blob.png"])

    async def test_repeated_cancellation_keeps_worker_protected_until_close(self):
        started, release = threading.Event(), threading.Event()
        def slow_write(conn):
            conn.execute("UPDATE conversations SET title='finished'")
            started.set()
            if not release.wait(5):
                raise TimeoutError("test worker was not released")
            conn.commit()
        task = asyncio.create_task(self.db.run(slow_write))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            close_task = asyncio.create_task(self.db.close())
            await asyncio.sleep(0)
            self.assertFalse(close_task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await close_task
        with closing(sqlite3.connect(self.path)) as disk, disk:
            self.assertEqual(disk.execute("SELECT title FROM conversations").fetchone()[0], "finished")

    async def test_concurrent_connect_calls_share_initialized_connection(self):
        await self.db.close()
        await asyncio.gather(*(self.db.connect() for _ in range(8)))
        self.assertEqual(await self.db.schema_version(), MIGRATIONS[-1][0])
        self.assertEqual(len(await self.repo.list_conversations()), 1)

    async def test_batch_attachment_lookup_uses_message_index(self):
        first = await self.repo.add_message(self.conversation.id, "user", "first",
                                            attachments=[attachment("first.png"), attachment("second.png")])
        second = await self.repo.add_message(self.conversation.id, "user", "second",
                                             attachments=[attachment("third.png")])
        plan = await self.db.run(lambda conn: conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM message_attachments "
            "WHERE message_id IN (?,?) ORDER BY message_id, id", (first.id, second.id)
        ).fetchall())
        details = " ".join(row["detail"] for row in plan)
        self.assertIn("SEARCH message_attachments USING INDEX idx_message_attachments_message", details)
        self.assertNotIn("SCAN", details)
        self.assertNotIn("TEMP B-TREE", details)
        messages = await self.repo.messages(self.conversation.id)
        self.assertEqual([[a.filename for a in m.attachments] for m in messages],
                         [["first.png", "second.png"], ["third.png"]])

    async def test_recovery_only_parses_streaming_blocks_and_uses_indexes(self):
        from blackrose.citations import unchecked_report  # Load dependencies before counting JSON reads.
        self.assertTrue(callable(unchecked_report))
        _, reply = await self.repo.begin_turn(self.conversation.id, "request", "model-a")
        ready = json.dumps({"kind": "ui", "status": "ready", "source": "retained"})
        def seed(conn):
            conn.executemany("INSERT INTO message_blocks VALUES (?,?,?,?)",
                             ((f"ready-{i}", reply.id, i, ready) for i in range(4000)))
            for i, kind in enumerate(("ui", "diagram"), 4000):
                conn.execute("INSERT INTO message_blocks VALUES (?,?,?,?)",
                             (kind, reply.id, i, json.dumps({"kind": kind, "status": "streaming"})))
            conn.executemany("INSERT INTO ui_events(event_id,block_id,action_id,payload,status,created_at) "
                             "VALUES (?, 'ui', ?, '{}', ?, 'now')",
                             ((status, status, status) for status in ("accepted", "started", "complete")))
            conn.commit()
        await self.db.run(seed)
        statements = []
        await self.db.run(lambda conn: conn.set_trace_callback(statements.append))
        try:
            with patch("blackrose.storage.repository.json.loads", wraps=json.loads) as loads:
                self.assertEqual(await self.repo.reap_pending(), 1)
                self.assertEqual(loads.call_count, 2)
                parsed = list(loads.call_args_list)
            self.assertTrue(all(json.loads(call.args[0])["status"] == "streaming" for call in parsed))
        finally:
            await self.db.run(lambda conn: conn.set_trace_callback(None))
        queries = [sql for sql in statements if sql.startswith("SELECT block_id") or
                   sql.startswith("UPDATE messages SET state") or sql.startswith("SELECT a.message_id")]
        self.assertEqual(len(queries), 3)
        for sql in queries:
            plan = await self.db.run(lambda conn, sql=sql: conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
            details = " ".join(row["detail"] for row in plan)
            self.assertIn("USING INDEX", details)
            self.assertNotIn("SCAN", details)
        def recovered(conn):
            return ([tuple(row) for row in conn.execute(
                "SELECT block_id, json_extract(payload, '$.status') FROM message_blocks "
                "WHERE block_id IN ('ui','diagram','ready-0') ORDER BY block_id")],
                [row[0] for row in conn.execute("SELECT status FROM ui_events ORDER BY event_id")])
        blocks, events = await self.db.run(recovered)
        self.assertEqual(blocks, [("diagram", "cancelled"), ("ready-0", "ready"), ("ui", "cancelled")])
        self.assertEqual(events, ["interrupted", "complete", "interrupted"])
        with patch("blackrose.storage.repository.json.loads", wraps=json.loads) as loads:
            self.assertEqual(await self.repo.reap_pending(), 0)
            loads.assert_not_called()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "chats.db"
        self.db = Database(self.path)

    async def asyncTearDown(self):
        await self.db.close()
        self.temporary.cleanup()

    async def test_failed_migration_rolls_back_ddl_and_can_retry_connect(self):
        with patch("blackrose.storage.db.MIGRATIONS", [(1, "CREATE TABLE partial(id); CREATE TABLE partial(id);")]):
            with self.assertRaises(StorageError):
                await self.db.connect()
        with self.assertRaises(StorageError):
            _ = self.db.connection
        with closing(sqlite3.connect(self.path)) as disk, disk:
            self.assertIsNone(disk.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())
            self.assertEqual(disk.execute("SELECT count(*) FROM schema_metadata").fetchone()[0], 0)
        await self.db.connect()
        self.assertEqual(await self.db.schema_version(), MIGRATIONS[-1][0])

    async def test_attachment_index_upgrade_preserves_populated_v9_rows(self):
        with patch("blackrose.storage.db.MIGRATIONS", [m for m in MIGRATIONS if m[0] <= 9]):
            await self.db.connect()
        self.assertEqual(await self.db.schema_version(), 9)
        repo = Repository(self.db)
        for title in ("first", "second"):
            conversation = await repo.create_conversation(title)
            await repo.add_message(conversation.id, "user", title,
                                   attachments=[attachment(title + ".png"), attachment("extra.png")])
        def rows(conn):
            return [tuple(row) for row in conn.execute("SELECT * FROM message_attachments ORDER BY id")]
        before = await self.db.run(rows)
        self.assertEqual(len(before), 4)
        await self.db.close()
        await self.db.connect()
        self.assertEqual(await self.db.schema_version(), MIGRATIONS[-1][0])
        self.assertEqual(await self.db.run(rows), before)
        columns = await self.db.run(lambda conn: conn.execute(
            "PRAGMA index_info('idx_message_attachments_message')").fetchall())
        self.assertEqual([row["name"] for row in columns], ["message_id", "id"])

    async def test_recovery_index_upgrade_preserves_blocks_and_pending_messages(self):
        with patch("blackrose.storage.db.MIGRATIONS", [m for m in MIGRATIONS if m[0] <= 10]):
            await self.db.connect()
        repo = Repository(self.db)
        conversation = await repo.create_conversation()
        _, reply = await repo.begin_turn(conversation.id, "request", "model-a")
        payload = json.dumps({"kind": "diagram", "status": "streaming"})
        def seed(conn):
            conn.execute("INSERT INTO message_blocks VALUES ('diagram', ?, 0, ?)", (reply.id, payload))
            conn.commit()
        await self.db.run(seed)
        await self.db.close()
        await self.db.connect()
        self.assertEqual(await self.db.schema_version(), MIGRATIONS[-1][0])
        self.assertEqual(await self.db.run(lambda conn: conn.execute(
            "SELECT payload FROM message_blocks").fetchone()[0]), payload)
        self.assertEqual(await repo.reap_pending(), 1)
        self.assertEqual(await self.db.run(lambda conn: conn.execute(
            "SELECT json_extract(payload, '$.status') FROM message_blocks").fetchone()[0]), "cancelled")

    async def test_version_write_failure_rolls_back_migration_ddl(self):
        await self.db.connect()
        next_version = MIGRATIONS[-1][0] + 1
        await self.db.run(lambda conn: conn.executescript(f"""
            CREATE TRIGGER reject_version BEFORE INSERT ON schema_metadata
            WHEN NEW.version={next_version} BEGIN SELECT RAISE(ABORT, 'injected version failure'); END;
        """))
        with patch("blackrose.storage.db.MIGRATIONS", [*MIGRATIONS, (next_version, "CREATE TABLE partial(id);")]):
            with self.assertRaises(StorageError):
                await self.db.migrate()
        self.assertEqual(await self.db.schema_version(), MIGRATIONS[-1][0])
        self.assertIsNone(await self.db.run(lambda conn: conn.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone()))

    async def test_chats_with_replies_start_guarded_after_the_exposure_migration(self):
        # Earlier versions did not record metadata reads, so any chat with a reply may have had one.
        with patch("blackrose.storage.db.MIGRATIONS", [m for m in MIGRATIONS if m[0] < 8]):
            await self.db.connect()
        repo = Repository(self.db)
        replied, fresh = await repo.create_conversation(), await repo.create_conversation()
        await repo.begin_turn(replied.id, "hello", "test-model")
        await self.db.close()
        await self.db.connect()
        self.assertTrue(await repo.local_data_exposed(replied.id))
        self.assertFalse(await repo.local_data_exposed(fresh.id))
        await repo.mark_local_data_exposed(fresh.id)
        self.assertTrue(await repo.local_data_exposed(fresh.id))

    async def test_future_schema_is_rejected_without_publishing_connection(self):
        with closing(sqlite3.connect(self.path)) as disk, disk:
            disk.execute("CREATE TABLE schema_metadata(version INTEGER PRIMARY KEY)")
            disk.execute("INSERT INTO schema_metadata VALUES (999)")
        with self.assertRaisesRegex(StorageError, "newer"):
            await self.db.connect()
        with self.assertRaises(StorageError):
            _ = self.db.connection
        with closing(sqlite3.connect(self.path)) as disk, disk:
            self.assertEqual(disk.execute("SELECT version FROM schema_metadata").fetchone()[0], 999)
            self.assertIsNone(disk.execute("SELECT name FROM sqlite_master WHERE name='conversations'").fetchone())
