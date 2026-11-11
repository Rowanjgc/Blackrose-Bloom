"""Fresh fork schema: chats, attachments and evidence; no routing tables."""
MIGRATIONS = [(1, '''
CREATE TABLE conversations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 title_source TEXT NOT NULL DEFAULT 'placeholder'
);
CREATE TABLE messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('system','user','assistant')),
 content TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, model_used TEXT,
 state TEXT NOT NULL DEFAULT 'complete' CHECK(state IN ('pending','complete','cancelled','error')),
 error TEXT, reply_to INTEGER REFERENCES messages(id)
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id, id);
CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);
CREATE TABLE message_attachments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, mime_type TEXT NOT NULL, filename TEXT NOT NULL,
 storage_path TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL,
 width INTEGER, height INTEGER, text_path TEXT, pages INTEGER, characters INTEGER,
 created_at TEXT NOT NULL
);
CREATE TABLE evidence (
 message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
 sources TEXT NOT NULL
);
'''), (2, '''
ALTER TABLE conversations ADD COLUMN workspace TEXT;
ALTER TABLE messages ADD COLUMN refs TEXT NOT NULL DEFAULT '[]';
ALTER TABLE message_attachments ADD COLUMN output_path TEXT;
'''), (3, '''
CREATE TABLE message_blocks (
 block_id TEXT PRIMARY KEY,
 message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 position INTEGER NOT NULL,
 payload TEXT NOT NULL,
 UNIQUE(message_id, position)
);
CREATE TABLE ui_state (
 block_id TEXT PRIMARY KEY REFERENCES message_blocks(block_id) ON DELETE CASCADE,
 sequence INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE ui_events (
 event_id TEXT PRIMARY KEY,
 block_id TEXT NOT NULL REFERENCES message_blocks(block_id) ON DELETE CASCADE,
 action_id TEXT NOT NULL,
 payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'accepted',
 user_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_ui_inflight ON ui_events(block_id, action_id)
 WHERE status IN ('accepted', 'started');
'''), (4, '''
-- No conversation foreign key: these obligations outlive the deleted rows.
CREATE TABLE artifact_cleanup (
 kind TEXT NOT NULL CHECK(kind IN ('blob', 'partial', 'drafts', 'recovery')),
 target TEXT NOT NULL,
 error TEXT,
 PRIMARY KEY(kind, target)
);
'''), (5, '''
CREATE TABLE message_audits (
 message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
 report TEXT NOT NULL
);
'''), (6, '''
-- Unsent composer work. Rows cascade with their conversation; saved references
-- and attachment copies are revalidated before the composer shows them again.
CREATE TABLE composer_drafts (
 conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
 text TEXT NOT NULL DEFAULT '',
 refs TEXT NOT NULL DEFAULT '[]',
 attachments TEXT NOT NULL DEFAULT '[]',
 sending TEXT,
 updated_at TEXT NOT NULL
);
CREATE TABLE composer_draft_files (
 conversation_id INTEGER NOT NULL REFERENCES composer_drafts(conversation_id) ON DELETE CASCADE,
 digest TEXT NOT NULL,
 data BLOB NOT NULL,
 PRIMARY KEY(conversation_id, digest)
);
'''), (7, '''
-- Originals displaced by approved edits, kept outside project folders. No
-- conversation foreign key: deleting a chat never silently deletes them.
-- A pending row is written before publication and settled afterwards, so a
-- crash in between is recovered from the named same-folder stage.
CREATE TABLE edit_backups (
 id TEXT PRIMARY KEY,
 conversation_id INTEGER NOT NULL,
 original_path TEXT NOT NULL,
 approval_id TEXT,
 approved_digest TEXT NOT NULL,
 published_digest TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'saved')),
 location TEXT,
 stored INTEGER NOT NULL DEFAULT 0,
 digest TEXT,
 size INTEGER,
 mode INTEGER,
 conflict INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 restored_at TEXT
);
CREATE INDEX idx_edit_backups_conversation ON edit_backups(conversation_id, created_at);
'''), (8, '''
-- Set before local file names, contents or library data first reach the model,
-- and never cleared: the values stay in assistant history whatever is pruned or
-- cited, so later model-written web and image requests keep needing approval.
-- A new conversation starts clear. Earlier versions recorded only file reads, so
-- any chat that has run a reply may have exposed metadata and starts guarded.
ALTER TABLE conversations ADD COLUMN local_data_exposed INTEGER NOT NULL DEFAULT 0;
UPDATE conversations SET local_data_exposed = 1 WHERE EXISTS (
 SELECT 1 FROM messages m WHERE m.conversation_id = conversations.id AND m.role = 'assistant');
'''), (9, '''
-- Completed turns waiting to reach the optional Honcho memory, and Honcho
-- sessions to delete. Turn text is read from messages when sent, so a deleted
-- chat's turns are never sent. No foreign key: a deletion outlives its chat.
-- attempts counts sends started, so a retry checks Honcho for a duplicate first.
CREATE TABLE memory_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(kind IN ('turn', 'forget')),
 conversation_id INTEGER NOT NULL,
 message_id INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 UNIQUE(kind, conversation_id, message_id)
);
-- memory_used: a turn was queued, so the chat may have a Honcho session to delete.
ALTER TABLE conversations ADD COLUMN memory_excluded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE conversations ADD COLUMN memory_used INTEGER NOT NULL DEFAULT 0;
-- Names this database's Honcho sessions, so a recreated database never reuses them.
CREATE TABLE installation (id TEXT NOT NULL);
INSERT INTO installation VALUES (lower(hex(randomblob(6))));
'''), (10, '''
CREATE INDEX idx_message_attachments_message ON message_attachments(message_id, id);
'''), (11, '''
-- Recovery only visits unfinished work, independent of retained history size.
CREATE INDEX idx_message_blocks_status ON message_blocks(json_extract(payload, '$.status'));
CREATE INDEX idx_messages_state ON messages(state);
'''), (12, '''
-- Transcript metadata and retry lookup do not scan a conversation's history.
CREATE INDEX idx_messages_conversation_role ON messages(conversation_id, role, id);
''')]
