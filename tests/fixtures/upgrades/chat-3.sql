-- Frozen synthetic historical chat schema; apply files 1 through the desired version.

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

INSERT INTO schema_metadata(version) VALUES (3);
