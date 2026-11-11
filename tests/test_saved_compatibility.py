"""Populated historical databases and saved answers, without inference or WebKit."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from blackrose.animations import encode_scene
from blackrose.gui.session import Session
from blackrose.storage.db import Database, StorageError
from blackrose.storage.repository import Repository
from blackrose.storage.migrations import MIGRATIONS
from blackrose.ui_documents import ARTIFACT, available
import test_gui_images as gui

FIXTURES = Path(__file__).parent / 'fixtures/upgrades'
STAMP = '2026-10-15T12:00:00+00:00'
SAVED = json.loads((FIXTURES / 'saved-form.json').read_text())
WORKSPACE = {'path': '/synthetic/workspace', 'device': 123, 'inode': 456}
REFS = [{'kind': 'file', 'path': '/synthetic/workspace/notes.md', 'label': 'Notes'}]
EVIDENCE = [{'title': 'Preserved citation', 'document_id': 'doc-42', 'chunk_id': 'chunk-9',
             'path': '/synthetic/library/source.md', 'quote': 'Evidence survives upgrades.'}]


def historical_database(path, version):
    """Frozen DDL and raw inserts deliberately bypass current migration/writer code."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        for step in range(1, version + 1):
            conn.executescript((FIXTURES / f'chat-{step}.sql').read_text())
        conn.execute('INSERT INTO conversations(id,title,created_at,updated_at,title_source) '
                     'VALUES (7,?,?,?,?)', ('Saved conversation', STAMP, STAMP, 'manual'))
        for identity, role, content, reply in ((11, 'user', 'A question with evidence', None),
                (12, 'assistant', 'A saved answer — café', 11),
                (13, 'user', 'Use preferences\nDays per week: 3', None)):
            conn.execute('INSERT INTO messages(id,conversation_id,role,content,created_at,model_used,reply_to) '
                         'VALUES (?,7,?,?,?,?,?)',
                         (identity, role, content, STAMP, 'historical-model' if role == 'assistant' else None, reply))
        for identity, message, kind, mime, name, blob, text in (
                (21, 11, 'document', 'text/plain', 'notes.txt', 'original.txt', 'extracted.txt'),
                (22, 12, 'image', 'image/png', 'garden.png', 'image.png', None),
                (23, 12, 'animation', 'application/vnd.blackrose.animation+json', 'saved.animation.json', 'legacy.json', None)):
            conn.execute('INSERT INTO message_attachments(id,message_id,kind,mime_type,filename,storage_path,'
                         'digest,size,text_path,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (identity, message, kind, mime, name, blob, 'saved-digest', 42, text, STAMP))
        conn.execute('INSERT INTO evidence VALUES (12,?)', (json.dumps(EVIDENCE),))
        if version >= 2:
            conn.execute('UPDATE conversations SET workspace=? WHERE id=7', (json.dumps(WORKSPACE),))
            conn.execute('UPDATE messages SET refs=? WHERE id=11', (json.dumps(REFS),))
            conn.execute("UPDATE message_attachments SET output_path='/synthetic/Downloads/saved.html' WHERE id=23")
        if version >= 3:
            conn.execute('INSERT INTO message_blocks VALUES (?,12,0,?)', (SAVED['block_id'], json.dumps(SAVED)))
            conn.execute('INSERT INTO ui_state VALUES (?,4,?)', (SAVED['block_id'], '{"days":3}'))
            conn.execute('INSERT INTO ui_events VALUES (?,?,?, ?,?,?,?)',
                         ('historical_event_0001', SAVED['block_id'], 'submit', '{"values":{"days":3}}',
                          'complete', 13, STAMP))


def snapshot(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {table: ([r[1] for r in conn.execute(f'PRAGMA table_info({table})')],
                    [tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')])
            for table in tables}


class PopulatedUpgradeTests(unittest.IsolatedAsyncioTestCase):
    def assert_preserved(self, before, conn):
        for table, (columns, rows) in before.items():
            if table != 'schema_metadata':
                self.assertEqual([tuple(r) for r in conn.execute(
                    f'SELECT {",".join(columns)} FROM {table} ORDER BY rowid')], rows, table)
        self.assertEqual([r[0] for r in conn.execute('SELECT version FROM schema_metadata ORDER BY version')], [v for v, _ in MIGRATIONS])
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    async def verify_upgrade(self, database, version, before):
        repo = Repository(database)
        first = None
        for _ in range(2):
            await database.connect()
            try:
                self.assertEqual(await database.schema_version(), MIGRATIONS[-1][0])
                await database.run(lambda conn: self.assert_preserved(before, conn))
                conversation = await repo.get_conversation(7)
                self.assertEqual(conversation.workspace, WORKSPACE if version >= 2 else None)
                messages = await repo.messages(7)
                self.assertEqual([m.id for m in messages], [11, 12, 13])
                self.assertEqual(messages[1].reply_to, 11)
                self.assertEqual(messages[0].refs, REFS if version >= 2 else [])
                self.assertEqual([a.id for m in messages for a in m.attachments], [21, 22, 23])
                self.assertEqual(messages[0].attachments[0].text_path, 'extracted.txt')
                self.assertEqual(messages[1].attachments[-1].output_path,
                                 '/synthetic/Downloads/saved.html' if version >= 2 else None)
                self.assertEqual(await repo.evidence_for(12), EVIDENCE)
                if version >= 3:
                    self.assertEqual(messages[1].blocks, [SAVED | {'saved_state': {'days': 3}, 'state_sequence': 4}])
                    self.assertEqual(messages[2].ui_context['submitted_values'], {'days': 3})
                else:
                    self.assertEqual(messages[1].blocks, [])
                self.assertEqual(await repo.cleanup_jobs(), [])
                current = (conversation, messages, await database.run(snapshot))
                if first is not None:
                    self.assertEqual(current, first)
                first = current
            finally:
                await database.close()

    async def test_all_prior_schemas_preserve_populated_rows_and_reopen_idempotently(self):
        for version in (1, 2, 3, 4):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'chats.db'
                historical_database(path, version)
                with closing(sqlite3.connect(path)) as conn:
                    before = snapshot(conn)
                await self.verify_upgrade(Database(path), version, before)

    async def test_each_pending_migration_rolls_back_on_failure_and_retries(self):
        for version in (1, 2, 3, 4):
            for failed in range(version + 1, MIGRATIONS[-1][0] + 1):
                with self.subTest(version=version, failed=failed), tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / 'chats.db'
                    historical_database(path, version)
                    with closing(sqlite3.connect(path)) as conn, conn:
                        before = snapshot(conn)
                        conn.executescript(f'''CREATE TRIGGER reject_version BEFORE INSERT ON schema_metadata
                            WHEN NEW.version={failed} BEGIN SELECT RAISE(ABORT, 'injected upgrade failure'); END;''')
                    database = Database(path)
                    try:
                        for _ in range(2):
                            with self.assertRaisesRegex(StorageError, f'migration {failed} failed'):
                                await database.connect()
                            with self.assertRaises(StorageError):
                                _ = database.connection
                            with closing(sqlite3.connect(path)) as conn:
                                self.assertEqual(conn.execute('SELECT MAX(version) FROM schema_metadata').fetchone()[0], failed - 1)
                                if failed in (2, 8):
                                    column = {2: 'workspace', 8: 'local_data_exposed'}[failed]
                                    self.assertNotIn(column, [r[1] for r in conn.execute('PRAGMA table_info(conversations)')])
                                elif failed in (10, 11, 12):
                                    indexes = {10: ('idx_message_attachments_message',),
                                               11: ('idx_message_blocks_status', 'idx_messages_state'),
                                               12: ('idx_messages_conversation_role',)}[failed]
                                    for index in indexes:
                                        self.assertIsNone(conn.execute(
                                            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                                            (index,)).fetchone())
                                else:
                                    table = {3: 'message_blocks', 4: 'artifact_cleanup', 5: 'message_audits', 6: 'composer_drafts',
                                             7: 'edit_backups', 9: 'memory_outbox'}[failed]
                                    self.assertIsNone(conn.execute('SELECT name FROM sqlite_master WHERE name=?', (table,)).fetchone())
                                for table, (columns, rows) in before.items():
                                    if table != 'schema_metadata':
                                        self.assertEqual(conn.execute(f'SELECT {",".join(columns)} FROM {table} ORDER BY rowid').fetchall(), rows)
                        with closing(sqlite3.connect(path)) as conn, conn:
                            conn.execute('DROP TRIGGER reject_version')
                        await self.verify_upgrade(database, version, before)
                    finally:
                        await database.close()


class SavedAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        historical_database(self.root / 'test.db', 3)
        self.config, self.runtime = gui.make_runtime(self.root)
        self.config.interactive_ui_actions_enabled = True
        await self.runtime.open()
        self.events = []
        self.session = Session(self.runtime.controller, lambda k, p: self.events.append((k, p)))
        await self.session.boot()
        await self.runtime.controller.open_conversation(7)

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.temp.cleanup()

    async def replay(self):
        await self.session.render_transcript()
        return next(p for k, p in reversed(self.events) if k == 'transcript')['messages'][1]['blocks'][0]

    async def assert_readable_and_refused(self, reason):
        block = await self.replay()
        self.assertEqual(block['fallback_text'], SAVED['fallback_text'])
        self.assertEqual(block['saved_state'], {'days': 3})
        envelope = {'conversation_id': 7, 'assistant_message_id': 12, 'block_id': block['block_id'],
                    'source_revision': 1, 'view_id': block['view_id']}
        before = await self.runtime.database.run(snapshot)
        self.assertEqual(await self.session.ui_source(envelope), SAVED['source'])
        state = await self.session.ui_state(envelope | {'state_sequence': 5, 'values': {'days': 4}})
        action = await self.session.ui_action(envelope | {'event_id': 'refused_event_0001',
            'action_id': 'submit', 'action_type': 'continue_conversation', 'values': {'days': 4}})
        for result in (state, action):
            self.assertFalse(result['ok'])
            self.assertIn(reason, result['error'])
        self.assertIsNone(self.session._generation)
        self.assertEqual(await self.runtime.database.run(snapshot), before)

    async def test_unknown_saved_protocol_library_and_schema_remain_readable_without_effects(self):
        for changes in ({'protocol_version': 999}, {'library_version': 'blackrose-semantic/999'},
                        {'schema_hash': 'unsupported-historical-schema'}):
            with self.subTest(changes=changes):
                block = deepcopy(SAVED) | changes
                await self.runtime.database.run(lambda conn, block=block: conn.execute(
                    'UPDATE message_blocks SET payload=? WHERE block_id=?', (json.dumps(block), block['block_id'])))
                # Commit the deliberately historical payload; normal writes prohibit identity changes.
                await self.runtime.database.run(lambda conn: conn.commit())
                await self.assert_readable_and_refused('unsupported')

    async def test_missing_and_corrupt_contract_or_bundle_refuse_effects_and_allow_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for contract_bytes, bundle_bytes in ((None, b''), (b'{', b''), (b'null', b''), (b'[]', b''),
                    (ARTIFACT.read_bytes(), None), (ARTIFACT.read_bytes(), b'// corrupt bundle'),
                    (ARTIFACT.read_bytes().replace(b'"artifact_hash":', b'"invalid_hash":'), b'')):
                with self.subTest(contract=contract_bytes is not None, bundle=bundle_bytes):
                    for name, data in (('ui-contract.json', contract_bytes), ('ui-runtime.js', bundle_bytes)):
                        path = root / name
                        path.unlink(missing_ok=True)
                        if data is not None:
                            path.write_bytes(data)
                    with patch('blackrose.ui_documents.ARTIFACT', root / 'ui-contract.json'):
                        self.assertFalse(available())
                        await self.assert_readable_and_refused('runtime unavailable')

    async def test_supported_saved_form_replays_state_and_accepts_the_next_sequence(self):
        block = await self.replay()
        self.assertEqual(block['saved_state'], {'days': 3})
        result = await self.session.ui_state({'conversation_id': 7, 'assistant_message_id': 12,
            'block_id': block['block_id'], 'source_revision': 1, 'view_id': block['view_id'],
            'state_sequence': 5, 'values': {'days': 4}})
        self.assertTrue(result['ok'], result)
        self.assertEqual((await self.replay())['saved_state'], {'days': 4})

    async def test_legacy_json_animation_replays_from_an_upgraded_conversation(self):
        scene = json.loads((Path(__file__).parents[1] / 'skills/explanatory-animations/references/rag.animation.json').read_text())
        store = self.runtime.controller._store
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / 'legacy.json').write_bytes(encode_scene(scene))
        await self.session.render_transcript()
        message = next(p for k, p in reversed(self.events) if k == 'transcript')['messages'][1]
        animation = next(a for a in message['attachments'] if a['kind'] == 'animation')
        self.assertEqual(animation['scene'], scene)
        self.assertNotIn('player_document', animation)
        self.assertNotIn('error', animation)
