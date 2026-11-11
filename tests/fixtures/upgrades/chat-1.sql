-- Frozen synthetic historical chat schema; apply files 1 through the desired version.
CREATE TABLE schema_metadata (
 version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

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

INSERT INTO schema_metadata(version) VALUES (1);
