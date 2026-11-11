-- Frozen synthetic historical chat schema; apply files 1 through the desired version.

ALTER TABLE conversations ADD COLUMN workspace TEXT;
ALTER TABLE messages ADD COLUMN refs TEXT NOT NULL DEFAULT '[]';
ALTER TABLE message_attachments ADD COLUMN output_path TEXT;

INSERT INTO schema_metadata(version) VALUES (2);
