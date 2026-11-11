"""SM-19: index schema/identity, atomic rebuild, lazy embedder and fence states.

Deterministic and offline: real SQLite/FTS/vec files under a temporary directory,
a deterministic embedding provider, and a fake tokenizer module so no model stack
is imported and no network access is attempted.
"""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from local_knowledge.chunking import chunk_sections
from local_knowledge.config import Config
from local_knowledge.database import connect, doctor, vector_dimensions
from local_knowledge.embeddings import EmbeddingInitializationError, GemmaProvider
from local_knowledge.extraction import Section
from local_knowledge.ingestion import import_file, rebuild, remove
from local_knowledge.retrieval import search
from test_retriever import FakeProvider

TOKENIZER_MODULE = "transformers.models.auto.tokenization_auto"
PROSE = " ".join(f"word{index}" for index in range(60))


def fake_tokenizer_module(factory):
    """A stand-in for the lazily imported AutoTokenizer without importing it."""
    module = types.ModuleType(TOKENIZER_MODULE)
    module.AutoTokenizer = type("AutoTokenizer", (), {"from_pretrained": classmethod(lambda cls, *a, **k: factory())})
    return module


class IndexSchemaStateTests(unittest.TestCase):
    """user_version is the persisted schema state; connect is its only door."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(database_path=str(self.root / "knowledge.db"),
                             dimensions=128, chunk_tokens=32, chunk_overlap=4)

    def tearDown(self):
        self.temporary.cleanup()

    def raw(self):
        return sqlite3.connect(self.config.database_path)

    def test_fresh_database_reaches_current_schema_without_a_vector_index(self):
        db = connect(self.config)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            self.assertLessEqual({"metadata", "documents", "chunks", "embeddings",
                                  "source_sections", "chunks_fts"}, tables)
            # The vector table is created by the first import, not by migration.
            self.assertIsNone(vector_dimensions(db))
        finally:
            db.close()
        connect(self.config, readonly=True).close()

    def test_unknown_lower_user_version_is_refused_instead_of_returning_a_handle(self):
        connect(self.config).close()
        raw = self.raw()
        raw.execute("PRAGMA user_version=-1")
        raw.commit()
        raw.close()
        for readonly in (False, True):
            with self.assertRaises(ValueError):
                connect(self.config, readonly=readonly)

    def test_partially_migrated_zero_version_fails_closed_and_stays_partial(self):
        raw = self.raw()
        raw.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            connect(self.config)
        raw = self.raw()
        try:
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(raw.execute("SELECT count(*) FROM sqlite_master WHERE name='chunks'").fetchone()[0], 0)
        finally:
            raw.close()


class IndexConsistencyStateTests(unittest.TestCase):
    """Derived index tables either agree with chunks or an explicit rebuild is owed."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(database_path=str(self.root / "knowledge.db"),
                             dimensions=128, chunk_tokens=32, chunk_overlap=4)
        self.provider = FakeProvider()

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name="reference.md", text="# Database\n\nDatabase transactions preserve records.\n"):
        source = self.root / name
        source.write_text(text)
        return source

    def add(self, source=None, provider=None, config=None):
        return import_file(config or self.config, provider or self.provider, source or self.source())

    def counts(self, config=None):
        db = connect(config or self.config, readonly=True)
        try:
            return [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("chunks", "chunks_fts", "embeddings", "vectors")]
        finally:
            db.close()

    def test_vector_rows_can_vanish_without_blocking_writes_or_search(self):
        self.add()
        self.assertEqual(self.counts(), [1, 1, 1, 1])
        db = connect(self.config)
        try:
            with db:
                for (rowid,) in db.execute("SELECT rowid FROM vectors").fetchall():
                    db.execute("DELETE FROM vectors WHERE rowid=?", (rowid,))
            self.assertIn("Vector/chunk row mismatch", doctor(db, 128)["issues"])
        finally:
            db.close()
        # Keyword retrieval still works; unfiltered vector retrieval silently loses
        # the row because it queries vectors, while filtered vector retrieval reads
        # embeddings. Neither path raises IndexRebuildRequired.
        self.assertTrue(search(self.config, self.provider, "database", mode="keyword")["results"])
        self.assertEqual(search(self.config, self.provider, "database", mode="vector")["results"], [])
        self.assertEqual(self.add(self.source("second.md", "# Forest\n\nForest habitats.\n"))["status"], "imported")
        rebuild(self.config, self.provider)
        self.assertEqual(self.counts(), [2, 2, 2, 2])

    def test_deleting_a_chunk_row_removes_every_derived_row(self):
        self.add()
        db = connect(self.config)
        try:
            rowid = db.execute("SELECT rowid FROM chunks").fetchone()[0]
            with db:
                db.execute("DELETE FROM chunks WHERE rowid=?", (rowid,))
        finally:
            db.close()
        # Foreign-key cascade covers embeddings; the trigger covers vectors and FTS.
        self.assertEqual(self.counts(), [0, 0, 0, 0])

    def test_rebuild_publishes_a_private_replacement_and_leaves_no_staging_file(self):
        self.add()
        before = os.stat(self.config.database_path).st_ino
        changed = replace(self.config, dimensions=256)
        rebuild(changed, FakeProvider(256))
        after = os.stat(changed.database_path)
        self.assertNotEqual(after.st_ino, before)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)
        self.assertEqual(list(self.root.glob("knowledge-rebuild-*")), [])
        db = connect(changed, readonly=True)
        try:
            self.assertEqual(vector_dimensions(db), 256)
            self.assertTrue(doctor(db, 256)["ok"])
        finally:
            db.close()

    def test_rebuild_integrity_gate_keeps_the_original_database(self):
        self.add()
        self.assertEqual(self.counts(), [1, 1, 1, 1])
        with patch("local_knowledge.database.doctor", return_value={"ok": False, "issues": ["synthetic"]}):
            with self.assertRaisesRegex(ValueError, "integrity"):
                rebuild(self.config, self.provider)
        self.assertEqual(self.counts(), [1, 1, 1, 1])
        self.assertEqual(list(self.root.glob("knowledge-rebuild-*")), [])

    def test_rebuild_refuses_a_document_without_retained_extraction(self):
        result = self.add()
        db = connect(self.config)
        try:
            with db:
                db.execute("DELETE FROM source_sections WHERE document_id=?", (result["document_id"],))
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, "retained extraction"):
            rebuild(self.config, self.provider)
        self.assertEqual(self.counts(), [1, 1, 1, 1])
        self.assertEqual(list(self.root.glob("knowledge-rebuild-*")), [])

    def test_remove_leaves_a_healthy_empty_index(self):
        result = self.add()
        remove(self.config, result["document_id"])
        self.assertEqual(self.counts(), [0, 0, 0, 0])
        db = connect(self.config, readonly=True)
        try:
            self.assertTrue(doctor(db, 128)["ok"])
        finally:
            db.close()


class ChunkerFenceStateTests(unittest.TestCase):
    """chunk_sections tracks one fence at a time, per section, until a real closer."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(database_path=str(self.root / "knowledge.db"),
                             dimensions=128, chunk_tokens=32, chunk_overlap=0)
        self.provider = FakeProvider()

    def tearDown(self):
        self.temporary.cleanup()

    def headings(self, sections):
        chunks = chunk_sections(sections, "T", self.provider, self.config)
        return {chunk.heading for chunk in chunks} - {""}

    def text(self, value):
        return [Section(value)]

    def test_closing_fence_must_be_at_least_as_long_as_the_opening(self):
        self.assertEqual(self.headings(self.text(f"````\ncode\n```\n# Not a heading\n\n{PROSE}\n")), set())

    def test_fence_line_with_an_info_string_does_not_close(self):
        self.assertEqual(self.headings(self.text(f"```\ncode\n```python\n# Not a heading\n\n{PROSE}\n")), set())

    def test_a_different_fence_character_does_not_close(self):
        self.assertEqual(self.headings(self.text(f"```\ncode\n~~~\n# Not a heading\n~~~\n\n{PROSE}\n")), set())

    def test_a_real_closer_restores_heading_detection(self):
        self.assertEqual(self.headings(self.text(f"```\ncode\n```\n# Real heading\n\n{PROSE}\n")), {"Real heading"})

    def test_unterminated_fence_hides_headings_to_the_end_of_its_section(self):
        self.assertEqual(self.headings(self.text(f"```\ncode\n# Hidden\n\n{PROSE}\n")), set())

    def test_fence_state_does_not_leak_into_the_next_section(self):
        sections = [Section("```\ncode\n# Hidden\n"), Section("# Visible\n\nbody words here\n")]
        self.assertEqual(self.headings(sections), {"Visible"})


class GemmaProviderLifecycleTests(unittest.TestCase):
    """Unpinned -> pinned snapshot -> tokenizer -> model, with failures never cached."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        self.state.start()
        self.config = Config(data_dir=str(self.root / "data"), cache_dir=str(self.root / "cache"))

    def tearDown(self):
        self.state.stop()
        self.temporary.cleanup()

    def test_unpinned_provider_refuses_the_snapshot_and_keeps_identity_available(self):
        config = replace(self.config, model_revision="")
        provider = GemmaProvider(config)
        with self.assertRaisesRegex(ValueError, "pinned model revision"):
            provider._snapshot()
        self.assertIsNone(provider._tokenizer)
        self.assertIsNone(provider._model)
        # Identity is configuration, not model state, so it never loads weights.
        self.assertEqual(provider.identity()["revision"], "")

    def test_identity_does_not_load_the_tokenizer_or_model(self):
        provider = GemmaProvider(self.config)
        identity = provider.identity()
        self.assertEqual(identity["revision"], self.config.model_revision)
        self.assertEqual(identity["dimensions"], self.config.dimensions)
        self.assertIsNone(provider._tokenizer)
        self.assertIsNone(provider._model)

    def test_failed_tokenizer_initialization_is_not_cached_and_a_later_call_retries(self):
        attempts = []

        def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("tokenizer unavailable")
            return "tokenizer"

        provider = GemmaProvider(self.config)
        with (patch.dict(sys.modules, {TOKENIZER_MODULE: fake_tokenizer_module(factory)}),
              patch.object(GemmaProvider, "_snapshot", lambda _provider: str(self.root / "snapshot"))):
            with self.assertRaises(EmbeddingInitializationError):
                _ = provider.tokenizer
            self.assertIsNone(provider._tokenizer)
            self.assertEqual(provider.tokenizer, "tokenizer")
        self.assertEqual(len(attempts), 2)

    def test_failed_model_initialization_is_not_cached_and_a_later_call_retries(self):
        attempts = []

        def load(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("weights unavailable")
            self._model = "model"
            return self._model

        provider = GemmaProvider(self.config)
        with patch.object(GemmaProvider, "_load_model", load):
            with self.assertRaises(EmbeddingInitializationError):
                _ = provider.model
            self.assertIsNone(provider._model)
            self.assertEqual(provider.model, "model")
        self.assertEqual(len(attempts), 2)

    def test_concurrent_first_use_loads_each_model_once(self):
        calls = []
        guard = threading.Lock()

        def slow_load(self):
            if self._model is None:
                with guard:
                    calls.append(id(self))
                time.sleep(0.05)
                self._model = object()
            return self._model

        providers = [GemmaProvider(self.config), GemmaProvider(self.config)]
        barrier = threading.Barrier(4)
        errors = []

        def worker(provider):
            try:
                barrier.wait(timeout=5)
                _ = provider.model
            except BaseException as error:  # reported below, not swallowed
                errors.append(error)

        with patch.object(GemmaProvider, "_load_model", slow_load):
            threads = [threading.Thread(target=worker, args=(provider,))
                       for provider in providers for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(calls), sorted(id(provider) for provider in providers))
        self.assertTrue(all(provider._model is not None for provider in providers))

    def test_concurrent_tokenizer_initialization_is_serialized_process_wide(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def factory():
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return "tokenizer"

        providers = [GemmaProvider(self.config), GemmaProvider(self.config)]
        barrier = threading.Barrier(2)
        errors = []

        def worker(provider):
            try:
                barrier.wait(timeout=5)
                self.assertEqual(provider.tokenizer, "tokenizer")
            except BaseException as error:
                errors.append(error)

        with (patch.dict(sys.modules, {TOKENIZER_MODULE: fake_tokenizer_module(factory)}),
              patch.object(GemmaProvider, "_snapshot", lambda _provider: str(self.root / "snapshot"))):
            threads = [threading.Thread(target=worker, args=(provider,)) for provider in providers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(maximum, 1)
        self.assertTrue(all(provider._tokenizer == "tokenizer" for provider in providers))


if __name__ == "__main__":
    unittest.main()
