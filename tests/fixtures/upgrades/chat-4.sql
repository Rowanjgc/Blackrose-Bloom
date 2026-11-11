CREATE TABLE artifact_cleanup (
 kind TEXT NOT NULL CHECK(kind IN ('blob', 'partial', 'drafts', 'recovery')),
 target TEXT NOT NULL,
 error TEXT,
 PRIMARY KEY(kind, target)
);
INSERT INTO schema_metadata(version) VALUES (4);
