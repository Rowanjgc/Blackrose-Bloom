"""Deterministic Retriever lifecycle tests using real SQLite/FTS/vec indexes."""
from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_knowledge.config import Config
from local_knowledge.database import connect, doctor, vector_dimensions
from local_knowledge.ingestion import ensure_vectors, import_file, rebuild, remove
from local_knowledge.provenance import update_metadata
from local_knowledge.retrieval import list_documents, open_document, search


class FakeProvider:
    def __init__(self, dimensions=128):
        self.dimensions = dimensions
        self.document_calls = 0
        self.query_calls = 0
        self.before_query = None

    def identity(self):
        return {"test_model": "deterministic-v1", "dimensions": self.dimensions}

    def count_tokens(self, text):
        return len(text.split())

    def vector(self, text):
        values = [1 + text.lower().count(term) for term in ("database", "forest", "music")]
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values] + [0.] * (self.dimensions - len(values))

    def embed_documents(self, items):
        self.document_calls += 1
        return [self.vector(title + " " + text) for title, text in items]

    def embed_query(self, query):
        self.query_calls += 1
        if self.before_query:
            self.before_query()
        return self.vector(query)


class RetrieverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(database_path=str(self.root / "knowledge.db"), dimensions=128,
                             chunk_tokens=32, chunk_overlap=4)
        self.provider = FakeProvider()

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name="reference.md", text="# Database\n\nDatabase transactions preserve records.\n"):
        source = self.root / name
        source.write_text(text)
        return source

    def add(self, source=None, **metadata):
        return import_file(self.config, self.provider, source or self.source(), **metadata)

    def state(self):
        db = connect(self.config, readonly=True)
        try:
            return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for table in ("documents", "chunks", "tags", "embeddings", "vectors", "chunks_fts", "metadata", "source_sections")}
        finally:
            db.close()

    def healthy(self, config=None):
        db = connect(config or self.config, readonly=True)
        try:
            report = doctor(db, (config or self.config).dimensions)
            self.assertTrue(report["ok"], report)
            counts = [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("chunks", "chunks_fts", "embeddings", "vectors")]
            self.assertEqual(len(set(counts)), 1, counts)
            return counts[0]
        finally:
            db.close()

    def test_import_search_modes_filters_and_citations(self):
        source = self.source()
        original = source.read_bytes()
        result = self.add(source, tags=["technical"], source_url="https://example.org/reference",
                          publication_date="2026-01-02", source_metadata={"project": "Retriever"})
        self.assertEqual(result["status"], "imported")
        for mode in ("hybrid", "keyword", "vector"):
            passages = search(self.config, self.provider, "database", mode=mode, tags=["technical"],
                              source_type="md", published_after="2026-01-01")["results"]
            self.assertTrue(passages, mode)
            passage = passages[0]
            self.assertEqual(passage["document_id"], result["document_id"])
            self.assertEqual(passage["source_metadata"], {"project": "Retriever"})
            self.assertEqual(passage["source_url"], "https://example.org/reference")
            opened = open_document(self.config, chunk_id=passage["chunk_id"], limit=1)["results"][0]
            self.assertEqual(opened["text"], passage["text"])
            self.assertIn(passage["chunk_id"], opened["citation"])
            self.assertIn(opened["text"], source.read_text())
        self.assertEqual(search(self.config, self.provider, "database", tags=["missing"])["results"], [])
        self.assertEqual(search(self.config, self.provider, "database", published_before="2025-01-01")["results"], [])
        self.assertEqual(source.read_bytes(), original)
        self.healthy()

    def test_unchanged_import_updates_metadata_without_reembedding(self):
        source = self.source()
        first = self.add(source, tags=["old"], source_metadata={"version": "1"})
        before = self.state()
        result = self.add(source, tags=["new"], source_url="https://example.org/new",
                          source_metadata={"version": "2"})
        self.assertEqual(result, {"document_id": first["document_id"], "status": "unchanged"})
        self.assertEqual(self.provider.document_calls, 1)
        after = self.state()
        for table in ("chunks", "embeddings", "vectors", "chunks_fts", "source_sections"):
            self.assertEqual(before[table], after[table], table)
        document = list_documents(self.config)["documents"][0]
        self.assertEqual(document["tags"], ["new", "old"])
        self.assertEqual(document["source_metadata"], {"version": "2"})
        self.healthy()

    def test_revision_replaces_all_derived_rows_and_drops_stale_provenance(self):
        source = self.source()
        first = self.add(source, source_metadata={"version": "1"})
        old_chunks = {r["chunk_id"] for r in open_document(self.config, document_id=first["document_id"])["results"]}
        source.write_text("# Forest\n\nForest conservation protects habitats.\n")
        result = self.add(source)
        self.assertEqual(result["document_id"], first["document_id"])
        self.assertEqual(result["status"], "updated")
        document = list_documents(self.config)["documents"][0]
        self.assertEqual(document["revision"], 2)
        self.assertEqual(document["source_metadata"], {})
        self.assertEqual(search(self.config, self.provider, "database", mode="keyword")["results"], [])
        current = open_document(self.config, document_id=first["document_id"])["results"]
        self.assertTrue(old_chunks.isdisjoint({r["chunk_id"] for r in current}))
        self.assertEqual(current[0]["title"], "Forest")
        self.healthy()

    def test_failed_revision_preserves_previous_index_and_identity(self):
        source = self.source()
        self.add(source, source_metadata={"version": "1"})
        before = self.state()
        source.write_text("# Forest\n\nChanged document content.\n")
        with patch.object(self.provider, "embed_documents", return_value=[[0.] * 128]):
            with self.assertRaisesRegex(ValueError, "unit"):
                self.add(source)
        self.assertEqual(self.state(), before)
        self.healthy()

    def test_embedding_count_failure_rolls_back_first_index_initialization(self):
        with patch.object(self.provider, "embed_documents", return_value=[]):
            with self.assertRaisesRegex(ValueError, "count"):
                self.add()
        db = connect(self.config)
        try:
            self.assertIsNone(vector_dimensions(db))
            self.assertEqual(db.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM metadata").fetchone()[0], 0)
        finally:
            db.close()
        changed = replace(self.config, dimensions=256)
        result = import_file(changed, FakeProvider(256), self.source())
        self.assertEqual(result["status"], "imported")
        self.healthy(changed)

    def test_extraction_failure_does_not_pin_vector_dimensions(self):
        source = self.source(text="")
        with self.assertRaisesRegex(ValueError, "No extractable text"):
            self.add(source)
        changed = replace(self.config, dimensions=256)
        source.write_text("A database document.")
        self.assertEqual(import_file(changed, FakeProvider(256), source)["status"], "imported")
        self.healthy(changed)

    def test_legacy_empty_vector_table_is_repaired_but_doctor_reports_mismatch(self):
        db = connect(self.config)
        try:
            ensure_vectors(db, 768)
            db.commit()
            self.assertIn("Vector index dimensions mismatch", doctor(db, 128)["issues"])
        finally:
            db.close()
        self.assertEqual(self.add()["status"], "imported")
        self.healthy()

    def test_identity_mismatch_rejects_search_and_import_without_mutation(self):
        source = self.source()
        self.add(source)
        before = self.state()
        changed = replace(self.config, dimensions=256)
        for operation in (lambda: import_file(changed, FakeProvider(256), source),
                          lambda: search(changed, FakeProvider(256), "database")):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                operation()
        self.assertEqual(self.state(), before)

    def test_missing_identity_on_populated_index_requires_explicit_rebuild(self):
        source = self.source()
        self.add(source)
        db = connect(self.config)
        with db:
            db.execute("DELETE FROM metadata WHERE key IN ('index_identity','query_prefix')")
        db.close()
        source.write_text("Changed database source.")
        with self.assertRaisesRegex(ValueError, "identity is missing"):
            self.add(source)
        rebuild(self.config, self.provider)
        self.healthy()

    def test_missing_query_identity_is_rejected(self):
        self.add()
        db = connect(self.config)
        with db:
            db.execute("DELETE FROM metadata WHERE key='query_prefix'")
        db.close()
        with self.assertRaisesRegex(ValueError, "Query configuration"):
            search(self.config, self.provider, "database")

    def test_metadata_only_update_keeps_all_index_rows(self):
        result = self.add()
        before = self.state()
        update_metadata(self.config, result["document_id"], {"project": "Example", "version": "2"})
        after = self.state()
        for table in before.keys() - {"metadata"}:
            self.assertEqual(before[table], after[table], table)
        self.assertEqual(self.provider.document_calls, 1)
        self.assertEqual(list_documents(self.config)["documents"][0]["source_metadata"]["version"], "2")

    def test_document_details_are_batched_for_empty_missing_and_duplicate_rows(self):
        def traced(operation):
            statements = []

            def traced_connect(config, **kwargs):
                db = connect(config, **kwargs)
                db.set_trace_callback(statements.append)
                return db

            with patch("local_knowledge.retrieval.connect", side_effect=traced_connect):
                result = operation()
            detail_queries = [statement for statement in statements
                              if "FROM tags" in statement or "SELECT key,value FROM metadata" in statement]
            return result, detail_queries

        connect(self.config).close()
        empty, queries = traced(lambda: list_documents(self.config))
        self.assertEqual(empty["documents"], [])
        self.assertEqual(queries, [])

        source = self.source(text="\n\n".join(
            f"## Section {index}\n\nDatabase transactions preserve durable atomic records."
            for index in range(8)))
        annotated = self.add(source, tags=["zeta", "alpha"], source_metadata={"project": "Retriever"})
        unannotated = self.add(self.source("plain.md", "# Plain\n\nA document without optional rows."))

        listed, queries = traced(lambda: list_documents(self.config))
        documents = {document["id"]: document for document in listed["documents"]}
        self.assertEqual(documents[annotated["document_id"]]["tags"], ["alpha", "zeta"])
        self.assertEqual(documents[annotated["document_id"]]["source_metadata"], {"project": "Retriever"})
        self.assertEqual(documents[unannotated["document_id"]]["tags"], [])
        self.assertEqual(documents[unannotated["document_id"]]["source_metadata"], {})
        self.assertEqual(len(queries), 2, queries)

        opened, queries = traced(lambda: open_document(
            self.config, document_id=annotated["document_id"], limit=10))
        self.assertGreater(len(opened["results"]), 1)
        self.assertTrue(all(item["tags"] == ["alpha", "zeta"] for item in opened["results"]))
        self.assertTrue(all(item["source_metadata"] == {"project": "Retriever"}
                            for item in opened["results"]))
        self.assertIsNot(opened["results"][0]["tags"], opened["results"][1]["tags"])
        self.assertIsNot(opened["results"][0]["source_metadata"], opened["results"][1]["source_metadata"])
        self.assertEqual(len(queries), 2, queries)

        found, queries = traced(lambda: search(
            self.config, self.provider, "database", mode="keyword", limit=10))
        self.assertGreater(len(found["results"]), 1)
        self.assertTrue(all(item["tags"] == ["alpha", "zeta"] for item in found["results"]))
        self.assertTrue(all(item["source_metadata"] == {"project": "Retriever"}
                            for item in found["results"]))
        self.assertEqual(len(queries), 2, queries)

    def test_remove_cascades_every_index_row_but_keeps_source(self):
        source = self.source()
        result = self.add(source, tags=["technical"], source_metadata={"version": "1"})
        remove(self.config, result["document_id"])
        state = self.state()
        for table in state.keys() - {"metadata"}:
            self.assertEqual(state[table], [], table)
        self.assertFalse(any(row[0].startswith("source:") for row in state["metadata"]))
        self.assertTrue(source.exists())
        self.assertEqual(search(self.config, self.provider, "database")["results"], [])
        self.healthy()

    def test_rebuild_uses_retained_extraction_and_updates_dimensions(self):
        source = self.source()
        result = self.add(source, tags=["technical"], source_metadata={"version": "1"})
        original_document = list_documents(self.config)["documents"][0]
        source.unlink()
        changed = replace(self.config, dimensions=256, chunk_tokens=48)
        rebuild(changed, FakeProvider(256))
        self.assertEqual(list_documents(changed)["documents"][0], original_document)
        self.assertEqual(search(changed, FakeProvider(256), "database")["results"][0]["document_id"],
                         result["document_id"])
        self.healthy(changed)
        self.assertEqual(list(self.root.glob("knowledge-rebuild-*")), [])

    def test_failed_rebuild_preserves_original_database(self):
        self.add()
        before = self.state()
        with patch.object(self.provider, "embed_documents", side_effect=RuntimeError("inference failed")):
            with self.assertRaisesRegex(RuntimeError, "inference failed"):
                rebuild(self.config, self.provider)
        self.assertEqual(self.state(), before)
        self.assertEqual(list(self.root.glob("knowledge-rebuild-*")), [])
        self.healthy()

    def test_empty_library_search_does_not_initialize_embeddings(self):
        connect(self.config).close()
        self.assertEqual(search(self.config, self.provider, "database")["results"], [])
        self.assertEqual(self.provider.query_calls, 0)

    def test_keyword_search_does_not_run_embeddings(self):
        self.add()
        with patch.object(self.provider, "embed_query", side_effect=AssertionError("unexpected inference")):
            self.assertTrue(search(self.config, self.provider, 'database " OR *', mode="keyword")["results"])

    def test_inference_allows_external_writer_to_remove_current_document(self):
        result = self.add()
        def quick_connect(config, **kwargs):
            db = connect(config, **kwargs)
            db.execute("PRAGMA busy_timeout=100")
            return db
        self.provider.before_query = lambda: remove(self.config, result["document_id"])
        with patch("local_knowledge.ingestion.connect", side_effect=quick_connect):
            self.assertEqual(search(self.config, self.provider, "database")["results"], [])
        self.healthy()

    def test_search_rechecks_identity_after_a_rebuild_during_inference(self):
        self.add()
        changed = replace(self.config, dimensions=256)
        self.provider.before_query = lambda: rebuild(changed, FakeProvider(256))
        with self.assertRaisesRegex(ValueError, "mismatch"):
            search(self.config, self.provider, "database")
        self.healthy(changed)

    def test_filtered_vector_candidates_are_selected_after_filtering(self):
        limited = replace(self.config, vector_candidates=1)
        self.add(self.source("outside.md", "# Database\n\nDatabase database database database."), tags=["outside"])
        target = self.add(self.source("inside.md", "# Forest\n\nForest conservation."), tags=["inside"])
        rows = search(limited, self.provider, "database", mode="vector", tags=["inside"])["results"]
        self.assertEqual([r["document_id"] for r in rows], [target["document_id"]])

    def test_schema_one_migration_preserves_vectors_and_backfills_fts_sources(self):
        self.add()
        before = self.state()
        db = connect(self.config)
        db.executescript("""
            BEGIN IMMEDIATE;
            DROP TRIGGER chunks_delete;
            DROP TABLE chunks_fts;
            CREATE VIRTUAL TABLE chunks_fts USING fts5(text,title,heading);
            INSERT INTO chunks_fts(rowid,text,title,heading)
              SELECT c.rowid,c.text,d.title,c.heading FROM chunks c JOIN documents d ON d.id=c.document_id;
            CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN
              DELETE FROM chunks_fts WHERE rowid=old.rowid;
            END;
            PRAGMA user_version=1;
            COMMIT;
        """)
        db.close()
        with self.assertRaisesRegex(ValueError, "initialize/migrate"):
            connect(self.config, readonly=True)
        connect(self.config).close()
        self.assertEqual(self.state(), before)
        self.healthy()

    def test_future_schema_and_symbolic_source_are_rejected(self):
        self.add()
        link = self.root / "linked.md"
        link.symlink_to(self.root / "reference.md")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.add(link)
        db = connect(self.config)
        db.execute("PRAGMA user_version=999")
        db.close()
        for readonly in (False, True):
            with self.assertRaisesRegex(ValueError, "newer"):
                connect(self.config, readonly=readonly)
