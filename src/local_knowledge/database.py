"""Schema v2. Slow extraction/embedding happens outside write transactions."""
from pathlib import Path
import json
import re
import sqlite3
import sqlite_vec
from .embeddings import QUERY_PREFIX

SCHEMA_VERSION = 2


def connect(config, readonly=False):
    path = Path(config.database_path)
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    # URI quoting is needed for paths containing '?' or '#'.
    from urllib.parse import quote
    db = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode={'ro' if readonly else 'rwc'}", uri=True, timeout=30)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        finally:
            db.enable_load_extension(False)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("Database is newer than this application")
        if readonly:
            if version != SCHEMA_VERSION:
                raise ValueError("Run knowledge init to initialize/migrate database")
            db.execute("PRAGMA query_only=ON")
        else:
            migrate(db, version)
            # migrate() only knows 0 -> 1 -> 2. A negative or otherwise unknown
            # user_version leaves no schema behind, so refuse the handle instead
            # of failing later on the first query.
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("Unsupported database schema version; restore an index backup")
        return db
    except BaseException:
        db.close()
        raise



def migrate(db, version):
    if version == 0:
        db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE documents(
          id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
          source_url TEXT, file_type TEXT NOT NULL, content_hash TEXT NOT NULL,
          revision INTEGER NOT NULL, ingested_at TEXT NOT NULL, modified_at TEXT,
          publication_date TEXT, extraction_status TEXT NOT NULL);
        CREATE TABLE tags(document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
          tag TEXT NOT NULL, PRIMARY KEY(document_id,tag));
        CREATE INDEX tags_name ON tags(tag);
        CREATE INDEX document_type ON documents(file_type);
        CREATE INDEX document_date ON documents(publication_date);
        CREATE TABLE chunks(
          rowid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
          document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          revision TEXT NOT NULL, chunk_order INTEGER NOT NULL, text TEXT NOT NULL,
          heading TEXT NOT NULL, page INTEGER, line_start INTEGER, line_end INTEGER,
          token_count INTEGER NOT NULL, content_hash TEXT NOT NULL);
        CREATE INDEX chunk_document ON chunks(document_id, chunk_order);
        CREATE TABLE embeddings(chunk_rowid INTEGER PRIMARY KEY REFERENCES chunks(rowid) ON DELETE CASCADE,
          vector BLOB NOT NULL);
        CREATE TABLE source_sections(document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
          sections TEXT NOT NULL);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(text, title, heading);
        CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN
          DELETE FROM chunks_fts WHERE rowid=old.rowid;
        END;
        PRAGMA user_version=1;
        COMMIT;
        ''')
        version = 1
    if version == 1:
        # Keyword-only metadata addition leaves document vectors/identity intact.
        db.executescript('''
        BEGIN IMMEDIATE;
        DROP TRIGGER chunks_delete;
        DROP TABLE chunks_fts;
        CREATE VIRTUAL TABLE chunks_fts USING fts5(text, title, heading, source);
        INSERT INTO chunks_fts(rowid,text,title,heading,source)
          SELECT c.rowid,c.text,d.title,c.heading,d.path
          FROM chunks c JOIN documents d ON d.id=c.document_id;
        CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN
          DELETE FROM chunks_fts WHERE rowid=old.rowid;
        END;
        PRAGMA user_version=2;
        COMMIT;
        ''')


def vector_dimensions(db):
    """Read the declared shape even when the vector table has no rows."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE name='vectors'").fetchone()
    if row is None:
        return None
    match = re.search(r"embedding\s+float\s*\[\s*(\d+)\s*\]", row[0], re.IGNORECASE)
    return int(match[1]) if match else 0


def index_identity(config, provider):
    return {"embedding": provider.identity(), "chunking": {"version": 2, "tokens": config.chunk_tokens,
            "overlap": config.chunk_overlap}, "extraction_version": 1}


def check_identity(db, config, provider, initialize=False):
    identity = json.dumps(index_identity(config, provider), sort_keys=True)
    row = db.execute("SELECT value FROM metadata WHERE key='index_identity'").fetchone()
    if row and row[0] != identity:
        raise ValueError("Index configuration mismatch; run knowledge rebuild explicitly")
    if not row:
        if not initialize:
            raise ValueError("Index is empty; import documents first")
        if db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
            raise ValueError("Index identity is missing; run knowledge rebuild explicitly")
        db.execute("INSERT INTO metadata VALUES('index_identity',?)", (identity,))
        db.execute("INSERT INTO metadata VALUES('query_prefix',?)", (QUERY_PREFIX,))
    query = db.execute("SELECT value FROM metadata WHERE key='query_prefix'").fetchone()
    if not query or query[0] != QUERY_PREFIX:
        raise ValueError("Query configuration incompatible; explicit compatibility review required")


def doctor(db, dimensions):
    issues = []
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        issues.append(integrity)
    if db.execute("PRAGMA foreign_key_check").fetchall():
        issues.append("Foreign key violations")
    missing = db.execute("SELECT rowid FROM chunks EXCEPT SELECT rowid FROM chunks_fts").fetchall()
    extra = db.execute("SELECT rowid FROM chunks_fts EXCEPT SELECT rowid FROM chunks").fetchall()
    if missing or extra:
        issues.append("FTS/chunk row mismatch")
    if db.execute('''SELECT 1 FROM chunks c JOIN documents d ON d.id=c.document_id
                     JOIN chunks_fts f ON f.rowid=c.rowid
                     WHERE f.text != c.text OR f.heading != c.heading OR f.title != d.title OR f.source != d.path LIMIT 1''').fetchone():
        issues.append("FTS content mismatch")
    if db.execute("SELECT rowid FROM chunks EXCEPT SELECT chunk_rowid FROM embeddings").fetchall():
        issues.append("Missing embeddings")
    has_vectors = db.execute("SELECT 1 FROM sqlite_master WHERE name='vectors'").fetchone()
    if has_vectors:
        if vector_dimensions(db) != dimensions:
            issues.append("Vector index dimensions mismatch")
        if db.execute("SELECT rowid FROM chunks EXCEPT SELECT rowid FROM vectors").fetchone() or db.execute("SELECT rowid FROM vectors EXCEPT SELECT rowid FROM chunks").fetchone():
            issues.append("Vector/chunk row mismatch")
        try:
            for vector in db.execute("SELECT v.embedding,e.vector FROM vectors v JOIN embeddings e ON v.rowid=e.chunk_rowid"):
                if vector[0] != vector[1]:
                    issues.append("Vector payload mismatch")
                    break
        except sqlite3.DatabaseError:
            issues.append("Unreadable vector index")
    elif db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
        issues.append("Missing vector index")
    if db.execute("SELECT 1 FROM documents d LEFT JOIN source_sections s ON s.document_id=d.id WHERE s.document_id IS NULL LIMIT 1").fetchone():
        issues.append("Missing retained extraction")
    if db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
        keys = {r[0] for r in db.execute("SELECT key FROM metadata WHERE key IN ('index_identity','query_prefix')")}
        if keys != {"index_identity", "query_prefix"}:
            issues.append("Missing index identity")
    from .embeddings import validate_vector
    import struct
    for row in db.execute("SELECT vector FROM embeddings"):
        try:
            validate_vector(struct.unpack(f'{len(row[0]) // 4}f', row[0]), dimensions)
        except (ValueError, struct.error):
            issues.append("Invalid embedding")
            break
    return {"ok": not issues, "issues": issues, "sqlite": sqlite3.sqlite_version,
            "sqlite_vec": db.execute("SELECT vec_version()").fetchone()[0], "schema": SCHEMA_VERSION}
