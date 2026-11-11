"""Explicit source metadata, independent of embeddings and source contents."""
import json


FIELDS = {"collection", "project", "version", "upstream_revision", "retrieved_at",
          "document_type", "applicability", "maintainer", "raw_sha256",
          "origin_conversation_id", "origin_message_id", "origin_attachment_id",
          "generator_model", "original_filename", "capture_completeness"}
PREFIX = "source:"


def validate_metadata(value):
    if not isinstance(value, dict) or value.keys() - FIELDS:
        raise ValueError("Unknown source metadata fields")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in value.values()):
        raise ValueError("Source metadata values must be nonempty strings of at most 1000 characters")
    return json.dumps(value, sort_keys=True)


def store_metadata(db, document_id, value):
    if value is not None:
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                   (PREFIX + document_id, validate_metadata(value)))


def read_metadata(db, document_id):
    row = db.execute("SELECT value FROM metadata WHERE key=?", (PREFIX + document_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def update_metadata(config, document_id, value):
    """Metadata-only update: no extraction, model load or chunk/embedding changes."""
    from .database import connect
    from .ingestion import writer_lock
    validate_metadata(value)
    with writer_lock(config):
        db = connect(config)
        try:
            with db:
                if not db.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone():
                    raise ValueError("Unknown indexed document")
                store_metadata(db, document_id, value)
        finally:
            db.close()
