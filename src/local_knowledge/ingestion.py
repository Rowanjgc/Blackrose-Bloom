from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, date, UTC
from pathlib import Path
import fcntl
import hashlib
import json
import os
import tempfile
import uuid
import stat
import sqlite_vec
from .database import connect, check_identity, vector_dimensions, doctor
from .extraction import extract, Section, SUPPORTED
from .chunking import chunk_sections
from .embeddings import validate_vector
from .provenance import store_metadata, validate_metadata, PREFIX


def now():
    return datetime.now(UTC).isoformat()


@contextmanager
def writer_lock(config):
    Path(config.database_path).parent.mkdir(parents=True, exist_ok=True)
    with open(config.database_path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def ensure_vectors(db, dimensions):
    existing = vector_dimensions(db)
    if existing is not None and existing != dimensions:
        # Older versions could leave an empty vector table after a failed first
        # import. Repair only that uninitialized state; populated indexes require
        # an explicit rebuild and must never be silently discarded.
        initialized = db.execute("SELECT 1 FROM metadata WHERE key='index_identity'").fetchone()
        if initialized or db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() or db.execute("SELECT 1 FROM vectors LIMIT 1").fetchone():
            raise ValueError("Vector index dimensions mismatch; run knowledge rebuild explicitly")
        db.execute("DROP TRIGGER IF EXISTS vectors_delete")
        db.execute("DROP TABLE vectors")
    db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(embedding float[{int(dimensions)}] distance_metric=cosine)")
    db.execute('''CREATE TRIGGER IF NOT EXISTS vectors_delete AFTER DELETE ON chunks BEGIN
                  DELETE FROM vectors WHERE rowid=old.rowid; END''')
    db.execute('''CREATE TABLE IF NOT EXISTS source_sections(document_id TEXT PRIMARY KEY
                  REFERENCES documents(id) ON DELETE CASCADE, sections TEXT NOT NULL)''')


def insert_chunks(db, doc_id, revision, title, chunks, vectors, dimensions):
    if len(chunks) != len(vectors):
        raise ValueError("Embedding count does not match chunk count")
    for chunk, vector in zip(chunks, vectors, strict=True):
        blob = sqlite_vec.serialize_float32(validate_vector(vector, dimensions))
        chunk_id = hashlib.sha256(f"{doc_id}:{revision}:{chunk.order}:{chunk.content_hash}".encode()).hexdigest()[:32]
        row = db.execute('''INSERT INTO chunks(id,document_id,revision,chunk_order,text,heading,page,line_start,line_end,token_count,content_hash)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                         (chunk_id, doc_id, revision, chunk.order, chunk.text, chunk.heading, chunk.page,
                          chunk.line_start, chunk.line_end, chunk.token_count, chunk.content_hash)).lastrowid
        db.execute("INSERT INTO embeddings VALUES(?,?)", (row, blob))
        db.execute("INSERT INTO vectors(rowid,embedding) VALUES(?,?)", (row, blob))
        db.execute("INSERT INTO chunks_fts(rowid,text,title,heading,source) SELECT ?,?,?,?,path FROM documents WHERE id=?",
                   (row, chunk.text, title, chunk.heading, doc_id))


def import_file(config, provider, path, tags=(), source_url=None, publication_date=None, title=None, source_metadata=None, expected_sha256=None):
    if source_metadata is not None:
        validate_metadata(source_metadata)
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Source must be a regular file, not a symbolic link")
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError("Unsupported file type")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 500 or any(ord(c) < 32 for c in title)):
        raise ValueError("title must be a nonempty string of at most 500 characters without control characters")
    tags = sorted(set(tags))
    if len(tags) > 50 or any(not t.strip() or len(t) > 100 for t in tags):
        raise ValueError("At most 50 nonempty tags, each at most 100 characters")
    if publication_date:
        date.fromisoformat(publication_date)
    if source_url and (len(source_url) > 2048 or not source_url.startswith(("http://", "https://"))):
        raise ValueError("source_url must be an HTTP(S) URL of at most 2048 characters")
    # Snapshot source once. Hash and extraction see exactly the same bytes.
    # O_NOFOLLOW prevents final-component symlink races; originals are never written.
    with writer_lock(config):
        with tempfile.TemporaryDirectory(prefix="knowledge-import-") as temporary:
            snapshot = Path(temporary) / ("source" + path.suffix.lower())
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source, snapshot.open("wb") as target:
                original_stat = os.fstat(source.fileno())
                if not stat.S_ISREG(original_stat.st_mode) or original_stat.st_size > config.max_file_bytes:
                    raise ValueError("Source exceeds size limit or is not a regular file")
                digest = hashlib.sha256()
                total = 0
                while data := source.read(65536):
                    total += len(data)
                    if total > config.max_file_bytes:
                        raise ValueError("Source exceeds size limit")
                    digest.update(data)
                    target.write(data)
            revision_hash = digest.hexdigest()
            if expected_sha256 is not None and revision_hash != expected_sha256:
                raise ValueError('Source changed after approval; capture integrity check failed')
            db = connect(config)
            try:
                old = db.execute("SELECT * FROM documents WHERE path=?", (str(path),)).fetchone()
                if db.execute("SELECT 1 FROM metadata WHERE key='index_identity'").fetchone():
                    check_identity(db, config, provider)
                if old and old["content_hash"] == revision_hash and (title is None or old["title"] == title):
                    with db:
                        db.executemany("INSERT OR IGNORE INTO tags VALUES(?,?)", [(old["id"], t) for t in tags])
                        db.execute("UPDATE documents SET source_url=coalesce(?,source_url), publication_date=coalesce(?,publication_date) WHERE id=?", (source_url, publication_date, old["id"]))
                        store_metadata(db, old["id"], source_metadata)
                    return {"document_id": old["id"], "status": "unchanged"}
                sections = extract(snapshot)
                title = title or (path.stem if path.suffix.lower() == ".pdf" else next((line.lstrip("# ").strip() for s in sections for line in s.text.splitlines() if line.startswith("# ")), path.stem))
                if len(title) > 500:
                    title = path.stem[:500]
                chunks = chunk_sections(sections, title, provider, config)
                if not chunks:
                    raise ValueError("No indexable text")
                vectors = provider.embed_documents([(title, c.text) for c in chunks])
                doc_id = old["id"] if old else uuid.uuid4().hex
                revision = old["revision"] + 1 if old else 1
                with db:
                    # DDL, identity and first chunks must either all land or all
                    # roll back, including on extraction/embedding failures.
                    db.execute("BEGIN IMMEDIATE")
                    ensure_vectors(db, config.dimensions)
                    check_identity(db, config, provider, initialize=True)
                    if old:
                        db.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
                        db.execute('''UPDATE documents SET title=?, content_hash=?, revision=?, ingested_at=?, modified_at=?,
                                      source_url=coalesce(?,source_url), publication_date=coalesce(?,publication_date) WHERE id=?''',
                                   (title, revision_hash, revision, now(), datetime.fromtimestamp(original_stat.st_mtime, UTC).isoformat(), source_url, publication_date, doc_id))
                    # Separate explicit columns keep metadata changes reviewable.
                    if not old:
                        db.execute('''INSERT INTO documents(id,path,title,source_url,file_type,content_hash,revision,ingested_at,modified_at,publication_date,extraction_status)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (doc_id, str(path), title, source_url, path.suffix.lower()[1:], revision_hash, revision, now(),
                                      datetime.fromtimestamp(original_stat.st_mtime, UTC).isoformat(), publication_date, "complete"))
                    db.execute("INSERT OR REPLACE INTO source_sections VALUES(?,?)", (doc_id, json.dumps([asdict(s) for s in sections])))
                    # A changed source without explicit provenance must not inherit an old version.
                    db.execute("DELETE FROM metadata WHERE key=?", (PREFIX + doc_id,))
                    store_metadata(db, doc_id, source_metadata)
                    db.executemany("INSERT OR IGNORE INTO tags VALUES(?,?)", [(doc_id, t) for t in tags])
                    insert_chunks(db, doc_id, revision_hash, title, chunks, vectors, config.dimensions)
                return {"document_id": doc_id, "status": "updated" if old else "imported", "chunks": len(chunks)}
            finally:
                db.close()


def selected_files(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.exists():
        raise ValueError("Explicit source path must exist and cannot be a symbolic link")
    if path.is_file():
        yield path
        return
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(root) / d).is_symlink())
        for name in sorted(files):
            item = Path(root) / name
            if not item.is_symlink() and item.suffix.lower() in SUPPORTED:
                yield item


def remove(config, document_id):
    with writer_lock(config):
        db = connect(config)
        try:
            with db:
                db.execute("DELETE FROM metadata WHERE key=?", (PREFIX + document_id,))
                if not db.execute("DELETE FROM documents WHERE id=?", (document_id,)).rowcount:
                    raise ValueError("Unknown document ID")
        finally:
            db.close()


def rebuild(config, provider):
    """Build from retained extraction snapshots; replace atomically under writer lock."""
    with writer_lock(config):
        original = connect(config)
        temporary = None
        try:
            handle, temporary = tempfile.mkstemp(prefix="knowledge-rebuild-", dir=Path(config.database_path).parent)
            os.close(handle)
            staging_config = replace(config, database_path=temporary)
            staging = connect(staging_config)
            try:
                # Backup preserves source metadata even when source files disappeared.
                original.backup(staging)
                with staging:
                    staging.execute("DROP TRIGGER IF EXISTS vectors_delete")
                    staging.execute("DROP TABLE IF EXISTS vectors")
                    staging.execute("DELETE FROM chunks")
                    staging.execute("DELETE FROM chunks_fts")
                    staging.execute("DELETE FROM metadata WHERE key NOT LIKE 'source:%'")
                    ensure_vectors(staging, config.dimensions)
                    check_identity(staging, config, provider, initialize=True)
                for doc in original.execute("SELECT * FROM documents ORDER BY id"):
                    source = original.execute("SELECT sections FROM source_sections WHERE document_id=?", (doc["id"],)).fetchone()
                    if not source:
                        raise ValueError("Missing retained extraction; re-import original source")
                    sections = [Section(**s) for s in json.loads(source[0])]
                    chunks = chunk_sections(sections, doc["title"], provider, config)
                    vectors = provider.embed_documents([(doc["title"], c.text) for c in chunks])
                    with staging:
                        insert_chunks(staging, doc["id"], doc["content_hash"], doc["title"], chunks, vectors, config.dimensions)
                if not doctor(staging, config.dimensions)["ok"]:
                    raise ValueError("Rebuilt database failed integrity check")
            finally:
                staging.close()
            original.close()
            os.chmod(temporary, 0o600)
            with open(temporary, "rb") as data:
                os.fsync(data.fileno())
            os.replace(temporary, config.database_path)
            temporary = None
            parent_fd = os.open(Path(config.database_path).parent, os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            original.close()
            if temporary:
                os.unlink(temporary)
