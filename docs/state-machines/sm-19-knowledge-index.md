# SM-19: Local knowledge index schema, identity, atomic rebuild and lazy embedder

## Scope and ownership

| Area | Code | Owner |
| --- | --- | --- |
| Schema version and migration | `src/local_knowledge/database.py::SCHEMA_VERSION`, `src/local_knowledge/database.py::connect`, `src/local_knowledge/database.py::migrate` | every process that opens the database; `connect` is the only door |
| Index identity and compatibility gate | `src/local_knowledge/database.py::index_identity`, `src/local_knowledge/database.py::check_identity` | callers that write or read derived rows |
| Integrity report | `src/local_knowledge/database.py::doctor` | read-only inspection (`knowledge doctor`, rebuild gate) |
| Derived table maintenance | `src/local_knowledge/ingestion.py::ensure_vectors`, `src/local_knowledge/ingestion.py::insert_chunks` | the single writer holding `writer_lock` |
| Document mutation | `src/local_knowledge/ingestion.py::import_file`, `src/local_knowledge/ingestion.py::remove` | one writer per database file |
| Atomic rebuild | `src/local_knowledge/ingestion.py::rebuild` | the writer lock holder |
| Lazy embedder | `src/local_knowledge/embeddings.py::GemmaProvider` | in-process, serialized by a module-level `RLock` |
| Markdown fence tracking | `src/local_knowledge/chunking.py::chunk_sections` | pure function, one call per section list |

Ownership per [architecture.md](../architecture.md#modules-and-invariants): `local_knowledge/` owns
"index identity and coherent documents/chunks/FTS/vector/snapshot mutation". Writers
serialize on an advisory `flock` on `<database>.lock`
(`src/local_knowledge/ingestion.py::writer_lock`), and every SQLite open in the package
goes through `database.connect`. A single writer operation therefore holds at most one
writable connection to the live database. Readers do not take the lock; they open
`mode=ro`, publish `PRAGMA query_only=ON` (the read-only branch of
`src/local_knowledge/database.py::connect`) and re-check identity inside one read
transaction (`src/local_knowledge/retrieval.py::search`). Published replacement is
`os.replace`, so a reader holding the old inode keeps a consistent snapshot.

The embedder is owned by whichever process constructed it: the desktop's
`KnowledgeService`, `knowledge serve` (MCP) or a CLI run. It holds no durable
state; identity is configuration plus library versions. The desktop already
serializes all native work through `KnowledgeService._worker_lock`
(`src/blackrose/knowledge.py::KnowledgeService.__init__`,
`src/blackrose/knowledge.py::KnowledgeService._run`); the module-level embedder lock
exists for the MCP server, whose sync tools run on worker threads, and `initialize()` is
called once from `create_server` (`src/local_knowledge/mcp_server.py::create_server`).

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `PRAGMA user_version` | read in `src/local_knowledge/database.py::connect` (before and after `migrate`); written by the `PRAGMA user_version=` statements in `src/local_knowledge/database.py::migrate` | int32 in the SQLite header | persisted schema generation: 0 none, 1 keyword-only FTS, 2 FTS with `source` | `migrate` only |
| `SCHEMA_VERSION` | `src/local_knowledge/database.py::SCHEMA_VERSION` | int constant | generation this code can serve | module load |
| `metadata['index_identity']` | `src/local_knowledge/database.py::check_identity` | canonical JSON text | hashable embedding + chunking + extraction identity (`src/local_knowledge/database.py::index_identity`) | `check_identity(initialize=True)` |
| `metadata['query_prefix']` | `src/local_knowledge/database.py::check_identity` | text | query-side prompt compatibility (`src/local_knowledge/embeddings.py::QUERY_PREFIX`) | `check_identity(initialize=True)` |
| `metadata['source:<document_id>']` | `src/local_knowledge/provenance.py::store_metadata` | JSON text | explicit source provenance, independent of content | `import_file` (delete then `store_metadata` in its write transaction), `update_metadata`, `rebuild` preserves it (its `NOT LIKE 'source:%'` delete) |
| `documents.rowid`/`id`, `documents.content_hash`, `documents.revision` | the `documents` table in `src/local_knowledge/database.py::migrate` | rows | document identity and change detection | `src/local_knowledge/ingestion.py::import_file` |
| `source_sections.sections` | `src/local_knowledge/ingestion.py::import_file` (`INSERT OR REPLACE INTO source_sections`) | JSON list of `Section` | retained extraction; the only rebuild input | `import_file` |
| `chunks.rowid` | the `chunks` table in `src/local_knowledge/database.py::migrate` | INTEGER PRIMARY KEY | the shared row identity of every derived row | `src/local_knowledge/ingestion.py::insert_chunks` (the `chunks` insert) |
| `embeddings.chunk_rowid` → `vector` | the `embeddings` table in `src/local_knowledge/database.py::migrate` | row + float32 blobs | authoritative embedding, FK cascade on chunk delete | `insert_chunks` (the `embeddings` insert) |
| `vectors.rowid` → `embedding` | `src/local_knowledge/ingestion.py::ensure_vectors` (`CREATE VIRTUAL TABLE … vec0`) | vec0 virtual table | KNN index over the same row identities | `insert_chunks` (the `vectors` insert); trigger `vectors_delete` in `ensure_vectors` |
| `chunks_fts.rowid` | the version-1 step of `src/local_knowledge/database.py::migrate` | FTS5 virtual table | keyword index incl. `source` from schema 2 | `insert_chunks` (the `chunks_fts` insert); trigger `chunks_delete` in `migrate` |
| `vector_dimensions(db)` | `src/local_knowledge/database.py::vector_dimensions` | `None` / `0` / int | declared `float[N]` shape parsed from `sqlite_master`, works with zero rows | reader |
| `Config.dimensions`, `model_revision`, `chunk_tokens`, `chunk_overlap` | `src/local_knowledge/config.py::Config` | ints/str | inputs of the identity | config load |
| `GemmaProvider._tokenizer` | `src/local_knowledge/embeddings.py::GemmaProvider.__init__` | object or `None` | lazy tokenizer cache | `src/local_knowledge/embeddings.py::GemmaProvider.tokenizer` |
| `GemmaProvider._model` | `src/local_knowledge/embeddings.py::GemmaProvider.__init__` | object or `None` | lazy weights cache | `src/local_knowledge/embeddings.py::GemmaProvider._load_model` |
| `_INITIALIZATION_LOCK` | `src/local_knowledge/embeddings.py::_INITIALIZATION_LOCK` | module `RLock` | serializes lazy construction process-wide | `tokenizer`/`model` |
| `chunk_sections.fence` | `src/local_knowledge/chunking.py::chunk_sections` | `tuple[str, int]` or `None` | `(fence char, opening run length)` inside one section | the per-line marker check |
| `chunk_sections.headings` | `src/local_knowledge/chunking.py::chunk_sections` | `list[str]` | current heading path, reset only by depth | the per-line heading match |
| staging file | `src/local_knowledge/ingestion.py::rebuild` (`tempfile.mkstemp`) | path `knowledge-rebuild-*` | unpublished replacement database | `rebuild` |

## States

Schema state (`connect` decides from `user_version`, `src/local_knowledge/database.py::connect`):

| State | Predicate |
| --- | --- |
| `Current` | `user_version == 2`; `migrate` is a no-op |
| `LegacyKeywordFTS` | `user_version == 1`; migrated in place on a writable open |
| `Uninitialized` | `user_version == 0`; creates v1 then v2 in one call |
| `FutureSchema` | `user_version > 2`; refused before migration |
| `UnknownSchema` | `user_version < 0` (or any value `migrate` cannot reach); now refused |
| `PartialZero` | `user_version == 0` but schema objects already exist; migration fails closed |

Index consistency state (only meaningful in `Current`; `doctor` names most of them).
Guards are in `src/local_knowledge/database.py::check_identity`,
`src/local_knowledge/ingestion.py::ensure_vectors` and
`src/local_knowledge/database.py::doctor` as named:

| State | Predicate |
| --- | --- |
| `EmptyInitialized` | no `chunks`, no `index_identity`, no `vectors` table |
| `Healthy` | identity present and equal; `chunks == embeddings == vectors == chunks_fts`; declared vector dimensions equal `config.dimensions` |
| `IdentityMissingPopulated` | `chunks` nonempty and no `index_identity` (the "Index identity is missing" branch of `check_identity`) |
| `IdentityMismatch` | stored identity differs from the computed one (the "Index configuration mismatch" branch of `check_identity`) |
| `QueryIncompatible` | `query_prefix` missing or different (the final check of `check_identity`) |
| `VectorDimensionRepairable` | dimensions differ, but no identity, no chunks, no vectors (the repair path of `ensure_vectors`) |
| `VectorDimensionBlocking` | dimensions differ on a populated index (the `raise` in `ensure_vectors`) |
| `VectorRowsMissing` | vector rows are a strict subset of chunk rows; only `doctor` sees it ("Vector/chunk row mismatch") |
| `DerivedRowMissing` | a chunk lacks an `embeddings`/`chunks_fts` row; only `doctor` sees it ("Missing embeddings", "FTS/chunk row mismatch") |

Rebuild state (`src/local_knowledge/ingestion.py::rebuild`):

| State | Predicate |
| --- | --- |
| `Idle` | no staging file, no lock held |
| `Staging` | `knowledge-rebuild-*` exists; the lock is held; the original is untouched |
| `Verified` | staging passes `doctor` (the integrity gate in `rebuild`) and has been fsynced |
| `Published` | `os.replace` returned and the directory fsynced |
| `Failed` | an exception escaped; the `finally` unlinked the staging file |
| `Stranded` | the process died before the `finally`; the staging file remains |

Embedder state (per provider instance):

| State | Predicate |
| --- | --- |
| `Unpinned` | `config.model_revision == ""`; every snapshot path refuses (`src/local_knowledge/embeddings.py::GemmaProvider._snapshot`) |
| `PinnedSnapshotUnavailable` | `snapshot_download(local_files_only=True)` raised `OSError` (`src/local_knowledge/embeddings.py::GemmaProvider._snapshot`) |
| `TokenizerUnloaded` | `_tokenizer is None` |
| `TokenizerReady` | `_tokenizer is not None` |
| `ModelUnloaded` | `_model is None` |
| `ModelReady` | `_model is not None` |
| `Failed(component)` | an attempt raised; the caches are unchanged, the error is in diagnostics (`src/local_knowledge/embeddings.py::GemmaProvider._initialization_failed`) |

Chunker state (per section text): `OutsideFence`, and `InsideFence(char, run)`.

## Events

- `connect(config)` / `connect(config, readonly=True)` — every database operation.
- `migrate(db, version)` — called by a writable `connect` (`src/local_knowledge/database.py::connect`).
- `check_identity(db, config, provider, initialize=bool)` — called on import
  (twice in `src/local_knowledge/ingestion.py::import_file`), on search (twice in
  `src/local_knowledge/retrieval.py::search`), in `doctor`
  (`src/local_knowledge/cli.py::run`) and in rebuild (`src/local_knowledge/ingestion.py::rebuild`).
- `ensure_vectors(db, dimensions)` — on import and rebuild.
- `import_file`, `remove`, `rebuild` — the three writers; each takes `writer_lock`.
- `search`, `open_document`, `list_documents`, `knowledge doctor` — read-only opens.
- Process exit / `SIGKILL` / power loss — the only event that can strand a staging file.
- `GemmaProvider.tokenizer`, `.model`, `.initialize()`, `.identity()` — lazy-load and identity entry points.
- `chunk_sections(sections, title, provider, config)` — once per section list.

## Transition table

### Schema and identity

Schema rows are in `src/local_knowledge/database.py::connect` and
`src/local_knowledge/database.py::migrate`; identity rows in
`src/local_knowledge/database.py::check_identity`; vector rows in
`src/local_knowledge/ingestion.py::ensure_vectors`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | `connect(rw)` | `user_version > 2` (`version > SCHEMA_VERSION` in `connect`) | raise `ValueError("Database is newer…")`; handle closed (the `except BaseException` in `connect`) | unchanged |
| `Uninitialized` | `connect(rw)` | `version == 0` in `migrate` | one transaction creates all v1 tables, `PRAGMA user_version=1` (the first `executescript`) | `LegacyKeywordFTS` |
| `Uninitialized` | same call | falls through `version = 1` | one transaction rebuilds FTS with `source`, `user_version=2` (the second `executescript`) | `Current` |
| `LegacyKeywordFTS` | `connect(rw)` | `version == 1` in `migrate` | FTS recreated and backfilled from `chunks`/`documents`, `user_version=2` (the second `executescript`) | `Current` |
| `UnknownSchema` | `connect(rw)` | `migrate` matched nothing | re-read version differs from `SCHEMA_VERSION` → `ValueError` (the post-`migrate` check in `connect`) | unchanged |
| `PartialZero` | `connect(rw)` | `version == 0` and a table exists | `CREATE TABLE` fails; `executescript` rolls the step back; handle closed | `PartialZero` |
| `Current` | `connect(rw)` | `version == 2` | no statement written; identity untouched | `Current` |
| `Current` | `connect(ro)` | `version != 2` (the read-only branch of `connect`) | raise "Run knowledge init…" | unchanged |
| `Current` | `connect(ro)` | `version == 2` | `PRAGMA query_only=ON` | `Current` read-only |
| `EmptyInitialized` | `import_file` | identity absent, no chunks (`check_identity`) | insert `index_identity` + `query_prefix` inside the write transaction | `Healthy` |
| `IdentityMissingPopulated` | `import_file` | identity absent, chunks present (`check_identity`) | raise "Index identity is missing; run knowledge rebuild explicitly" | unchanged |
| `IdentityMismatch` | `import_file`/`search` | stored ≠ computed (`check_identity`) | raise "Index configuration mismatch; run knowledge rebuild explicitly" | unchanged |
| `QueryIncompatible` | `check_identity` | query prefix differs | raise "Query configuration incompatible…" | unchanged |
| `VectorDimensionRepairable` | `ensure_vectors` | dims differ and no identity/chunks/vectors | drop trigger and table, recreate at `config.dimensions` | `EmptyInitialized` |
| `VectorDimensionBlocking` | `ensure_vectors` | dims differ on populated index | raise "…run knowledge rebuild explicitly" | unchanged |
| `Healthy` | `ensure_vectors` | dims equal | `CREATE … IF NOT EXISTS` only | `Healthy` |
| any | `doctor` | — | reads only; returns `{"ok", "issues"}` (`src/local_knowledge/database.py::doctor`) | unchanged |

### Rebuild

All rows are in `src/local_knowledge/ingestion.py::rebuild`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `Idle` | `rebuild` | flock acquired (`writer_lock`) | `mkstemp` staging file, migrate it (`connect(staging_config)`) | `Staging` |
| `Staging` | backup | — | `original.backup(staging)` copies documents, tags, metadata, `source_sections` | `Staging` |
| `Staging` | reset | — | drop `vectors` + trigger, delete chunks/FTS/index metadata, keep `source:%`, recreate vectors, initialize identity | `Staging` |
| `Staging` | per document | `source_sections` row present | chunk + embed + insert into staging | `Staging` |
| `Staging` | per document | row absent | raise "Missing retained extraction; re-import original source" | `Failed` |
| `Staging` | integrity gate | `doctor(staging)` not ok | raise "Rebuilt database failed integrity check" | `Failed` |
| `Staging` | publish | — | close connections, chmod `0o600`, fsync file, `os.replace`, fsync directory | `Published` |
| `Staging` | any exception | — | `finally` closes and unlinks staging; original untouched | `Failed` |
| `Staging` | process killed | no `finally` runs | staging file remains | `Stranded` |
| `Published`/`Failed` | next `import_file`/`search` | identity in the new file | normal `Current` behaviour | `Healthy` |

### Embedder

Rows are in `src/local_knowledge/embeddings.py::GemmaProvider` (`_snapshot`,
`tokenizer`, `model`, `_load_model`, `identity`, `initialize`).

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `Unpinned` | `tokenizer`/`model` | `not model_revision` (`_snapshot`) | raise `ValueError` inside the lock; nothing cached | `Failed(tokenizer\|model)` |
| `PinnedSnapshotUnavailable` | `_snapshot` | `snapshot_download` `OSError` | raise "Model unavailable offline…" | `Failed(...)` |
| `TokenizerUnloaded` | `tokenizer` | lock acquired, `_tokenizer is None` (`tokenizer`) | import `AutoTokenizer`, `from_pretrained(snapshot)` | `TokenizerReady` |
| `TokenizerUnloaded` | `tokenizer` | construction raised | record diagnostic, raise `EmbeddingInitializationError`, `_tokenizer` still `None` | `Failed(tokenizer)` |
| `Failed(tokenizer)` | `tokenizer` again | `_tokenizer is None` | retry from scratch — no backoff, no cached failure | `TokenizerReady` or `Failed(tokenizer)` |
| `ModelUnloaded` | `model` | lock held by caller (`model`), `_model is None` (`_load_model`) | import torch + sentence-transformers, set threads, load snapshot on CPU, validate 8192 limit and `SearchQuery` prompt (`_load_model`) | `ModelReady` |
| `ModelUnloaded` | `model` | any exception (the `except` in `model`) | diagnostic + `EmbeddingInitializationError`, `_model` still `None` | `Failed(model)` |
| any | `identity()` | — | read config + `importlib.metadata` versions (`identity`); no lock, no weights | unchanged |
| any | `initialize()` | — | `embed_query` drives tokenizer+model+inference; failures re-raise as initialization errors (`initialize`) | `ModelReady` or unchanged |

### Chunker fence

Rows are the per-line loop of `src/local_knowledge/chunking.py::chunk_sections`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `OutsideFence` | fence run `≥3` of `` ` `` or `~` | `fence is None` | remember `(char, run length)` | `InsideFence(char, n)` |
| `InsideFence` | marker line | same char and `run ≥ n` and no info string (the closing-fence `elif`) | `fence = None` | `OutsideFence` |
| `InsideFence` | marker line | shorter run, other char, or info string | ignored; the line stays content | `InsideFence` |
| `InsideFence` | heading line | `fence is not None` (the heading match is skipped) | no heading, no boundary | `InsideFence` |
| `InsideFence` | blank line | `fence is not None` (the blank-line boundary check) | no boundary | `InsideFence` |
| `OutsideFence` | heading line | matches `^(#{1,6})\s+…` | update heading path; add a heading boundary | `OutsideFence` |
| `InsideFence` | end of section | — | state discarded; the next section starts `OutsideFence` (`fence = None` at the top of each section) | `OutsideFence` |

## Invariants

1. A writable `connect` returns a handle only when `user_version == SCHEMA_VERSION`;
   a read-only one refuses anything else. Enforced `src/local_knowledge/database.py::connect`; tests
   `IndexSchemaStateTests.test_unknown_lower_user_version_is_refused_instead_of_returning_a_handle`,
   `test_fresh_database_reaches_current_schema_without_a_vector_index`, and
   `tests/test_retriever.py::test_future_schema_and_symbolic_source_are_rejected`.
2. Migration advances one generation per transaction (`BEGIN IMMEDIATE` …
   `COMMIT` inside each `executescript`), so a crash leaves the previous
   generation, which the next open resumes from. Enforced
   `src/local_knowledge/database.py::migrate`; test
   `test_partially_migrated_zero_version_fails_closed_and_stays_partial`.
3. A failed migration never leaves a partially created schema; the failed step
   rolls back. Enforced by the same scripts; same test asserts `chunks` does not
   appear and the version is unchanged.
4. Every derived row shares `chunks.rowid`, and a chunk insert writes chunks,
   embeddings, vectors and FTS inside one transaction (the `with db:` write
   transaction of `src/local_knowledge/ingestion.py::import_file`).
   Test `IndexConsistencyStateTests.test_vector_rows_can_vanish_without_blocking_writes_or_search`
   (counts go 1→1→1→1 on import) and `tests/test_retriever.py::test_import_search_modes_filters_and_citations` (`healthy`).
5. Deleting a chunk removes its embedding (FK cascade on the `embeddings` table in
   `src/local_knowledge/database.py::migrate` + `foreign_keys=ON` in
   `src/local_knowledge/database.py::connect`), its vector (trigger `vectors_delete` in
   `src/local_knowledge/ingestion.py::ensure_vectors`) and its FTS row (trigger
   `chunks_delete` in `migrate`). Test
   `test_deleting_a_chunk_row_removes_every_derived_row`.
6. `index_identity` is inserted only while the index is empty; a populated index
   without identity refuses writes with an explicit rebuild message. Enforced
   `src/local_knowledge/database.py::check_identity`; `tests/test_retriever.py::test_missing_identity_on_populated_index_requires_explicit_rebuild`.
7. A stored identity that differs from the computed one refuses both import and
   search without mutating anything. Enforced `src/local_knowledge/database.py::check_identity`;
   `tests/test_retriever.py::test_identity_mismatch_rejects_search_and_import_without_mutation`.
8. An empty vector table at the wrong dimensions is repaired; a populated one
   raises instead of being discarded. Enforced `src/local_knowledge/ingestion.py::ensure_vectors`; tests
   `test_rebuild_publishes_a_private_replacement_and_leaves_no_staging_file`
   (dims change on a populated index is done by rebuild) and
   `tests/test_retriever.py::test_legacy_empty_vector_table_is_repaired_but_doctor_reports_mismatch`.
9. Rebuild publishes only a `doctor`-clean database, and publishes it under the
   writer lock with `fsync(file)`, `os.replace`, `fsync(dir)`. Enforced
   `src/local_knowledge/ingestion.py::rebuild`; tests `test_rebuild_publishes_a_private_replacement_and_leaves_no_staging_file`,
   `test_rebuild_integrity_gate_keeps_the_original_database`.
10. Any in-process rebuild failure leaves the original database logically
    unchanged and removes the staging file. Enforced by the outer `finally` of
    `src/local_knowledge/ingestion.py::rebuild`;
    same two tests plus `tests/test_retriever.py::test_failed_rebuild_preserves_original_database`.
11. Rebuild reads only `source_sections`; a document without retained extraction
    aborts the rebuild instead of being dropped. Enforced by the "Missing retained
    extraction" check in `src/local_knowledge/ingestion.py::rebuild`;
    test `test_rebuild_refuses_a_document_without_retained_extraction`.
12. `GemmaProvider.identity()` never loads the tokenizer or the weights and works
    in the `Unpinned` state. Enforced `src/local_knowledge/embeddings.py::GemmaProvider.identity`; tests
    `test_identity_does_not_load_the_tokenizer_or_model`,
    `test_unpinned_provider_refuses_the_snapshot_and_keeps_identity_available`.
13. At most one successful construction per provider per component, and
    constructions are serialized process-wide by one `RLock`. Enforced
    `src/local_knowledge/embeddings.py::_INITIALIZATION_LOCK`, held by
    `src/local_knowledge/embeddings.py::GemmaProvider.tokenizer` and
    `src/local_knowledge/embeddings.py::GemmaProvider.model`; tests
    `test_concurrent_first_use_loads_each_model_once`,
    `test_concurrent_tokenizer_initialization_is_serialized_process_wide`.
14. A failed initialization caches nothing, so a later call retries the same
    component. Enforced `src/local_knowledge/embeddings.py::GemmaProvider.tokenizer`,
    `src/local_knowledge/embeddings.py::GemmaProvider.model`; tests
    `test_failed_tokenizer_initialization_is_not_cached_and_a_later_call_retries`,
    `test_failed_model_initialization_is_not_cached_and_a_later_call_retries`.
15. Fence state is (a) one fence at a time, (b) closed only by the same
    character, an equal-or-longer run, and no info string, (c) never carried into
    the next section. Enforced by the fence tracker in
    `src/local_knowledge/chunking.py::chunk_sections`; all six
    `ChunkerFenceStateTests` tests.
16. While inside a fence, heading lines and blank lines produce neither heading
    metadata nor chunk boundaries (the heading match and blank-line check in
    `src/local_knowledge/chunking.py::chunk_sections`).

## Hazards and open questions

| # | Severity | Location | Finding | Demonstrated |
| --- | --- | --- | --- | --- |
| H1 | `defect` (fixed) | the fence tracker in `src/local_knowledge/chunking.py::chunk_sections` | The closer accepted any run of the same character, including one shorter than the opener and one with an info string (` ```python `). A line inside a four-backtick block or after a ```python opener therefore closed the fence; subsequent `#` lines became headings and blank lines became boundaries, so fenced code was split and mislabelled. | Yes — `test_closing_fence_must_be_at_least_as_long_as_the_opening`, `test_fence_line_with_an_info_string_does_not_close` fail before, pass after. |
| H2 | `fragile` (fixed) | the post-`migrate` check in `src/local_knowledge/database.py::connect` | `migrate` handles only 0, 1 and 2. A negative `user_version` returned a handle with no schema; the first query died with `sqlite3.OperationalError: no such table`. Not reachable through this code, but unguarded and indistinguishable from a corrupted DB. | Yes — `test_unknown_lower_user_version_is_refused_instead_of_returning_a_handle` fails before, passes after. |
| H3 | `fragile` | `src/local_knowledge/ingestion.py::rebuild` (`mkstemp` and the outer `finally`) | The staging file is unlinked only by the `finally`. `SIGKILL`, a power cut or an OOM kill during rebuild strands a full-size copy of the database as `knowledge-rebuild-*` in the data directory, and nothing sweeps it later. Disk-space leak only; the published database is never at risk because `os.replace` is atomic. Confirming it needs a kill-during-rebuild run; no test pins a leak. | No test. |
| H4 | `fragile` | `src/local_knowledge/ingestion.py::import_file`, `src/local_knowledge/database.py::doctor` | `vectors` can diverge from `chunks` without triggering a rebuild: `ensure_vectors` only checks declared dimensions and `check_identity` only checks identity, so a missing vector row, a missing `embeddings` row, a missing FTS row or a `DROP TABLE vectors` is invisible to `import_file`. Only `doctor` reports it, and unfiltered vector search then silently returns fewer results because it queries `vectors` (the `vectors … MATCH` query in `src/local_knowledge/retrieval.py::search`), while filtered vector search reads `embeddings` (the `embeddings` join in the same function) and still works. Repaired only by `rebuild`. | Yes — `test_vector_rows_can_vanish_without_blocking_writes_or_search`. |
| H5 | `fragile` | `src/local_knowledge/ingestion.py::ensure_vectors` | If the `vectors` table is absent entirely while chunks exist, `vector_dimensions` returns `None`, the dimension branch is skipped, and an **empty** vector index is created with no backfill. The result is exactly the H4 state reached through the repair path rather than an error. | Same test covers row loss; the table-drop variant is asserted in the probe, not in a test. |
| H6 | `fragile` | `src/local_knowledge/ingestion.py::insert_chunks` (the `chunks_fts` insert) | `insert_chunks` writes the FTS row with `INSERT … SELECT … FROM documents WHERE id=?`. If the document row were missing, the FTS insert would silently no-op while chunks/embeddings/vectors were written. Both current callers insert the document first (`src/local_knowledge/ingestion.py::import_file`) or copy it (`original.backup` in `src/local_knowledge/ingestion.py::rebuild`), so this is unreachable today; `doctor` would report "FTS/chunk row mismatch". | No test (unreachable). |
| H7 | `none-found` | `src/local_knowledge/ingestion.py::rebuild` | `original.close()` runs twice (once on the success path, once in `finally`). `sqlite3.Connection.close()` is idempotent; no state effect. Cosmetic only. | n/a |
| H8 | `documentation-gap` | the marker regex in `src/local_knowledge/chunking.py::chunk_sections` | `^\s*` accepts any indentation, so an indented code block (4+ spaces) is treated as a fenced block, and a fence marker inside indented code changes the fence state. A backtick info string containing a backtick is also accepted as an opener, which CommonMark forbids. Neither was changed: they do not break the stated intent (group fenced code) and changing them would alter chunk boundaries of existing indexes. | No test. |
| H9 | `none-found` | `src/local_knowledge/database.py::connect` | `connect` is the only constructor of SQLite handles, always enables `foreign_keys` and loads `sqlite-vec`; read-only handles are additionally `query_only`. The one trust boundary that matters — a writer that bypasses `writer_lock` — is outside this module (an external process can still open the file). Readers rely on `os.replace` atomicity plus the identity re-check, which `tests/test_retriever.py::test_search_rechecks_identity_after_a_rebuild_during_inference` covers. | Existing tests. |
| H10 | `none-found` | `src/local_knowledge/embeddings.py::GemmaProvider.tokenizer`, `src/local_knowledge/embeddings.py::GemmaProvider.model`, `src/local_knowledge/embeddings.py::GemmaProvider._load_model` | Two first-callers cannot both load: the check and the assignment are inside one process-wide `RLock`, not a `double-checked` read outside it. The lock also covers two independent `GemmaProvider` instances, which is what MCP worker threads need (`KnowledgeService` additionally serializes with its own worker lock). `torch.set_num_threads` (in `_load_model`) mutates process-global state, but only under the same lock. Two *processes* each load their own copy; that is a memory cost, not a state hazard. | Tests 13 above. |
| H11 | `documentation-gap` | `src/local_knowledge/embeddings.py::GemmaProvider.identity` | Identity includes `transformers`/`sentence-transformers` versions, so a dependency patch bump makes every existing index report `IdentityMismatch` and requires a full rebuild. That is deliberate (input format and prompt validation can change) but the user-visible message says "Index configuration mismatch", not "library changed". | No test. |

Open questions:

- Should `rebuild` sweep `knowledge-rebuild-*` after taking the writer lock
  (proposal F1)? That would turn H3 into a bounded retry, but unlinking files in
  the user's data directory is a destructive operation that deserves its own
  decision.
- Should `import_file` refuse to write when `doctor`-level divergence exists, as
  it already does for dimension and identity mismatch (H4/H5)? It would cost a
  `doctor` pass per import (full table scans plus vector deserialization) or a
  cheaper count check per write. Not enough evidence to pick.

## Formalization decisions

Two source changes, each with a failing-before regression test. Nothing else was
refactored or reformatted.

1. `src/local_knowledge/chunking.py::chunk_sections` — fence state is now
   `(character, opening run length)` and a closer must match the character, be at
   least as long, and carry no info string. Reason: H1 broke the function's own
   contract (its comment "Group fenced code as one unit") and produced wrong
   `chunks.heading` values that are user-visible in search results. The change is
   local to the fence tracker; heading/boundary logic is untouched.
2. `src/local_knowledge/database.py::connect` — after `migrate`, re-read
   `user_version` and refuse the handle unless it equals `SCHEMA_VERSION`. Reason:
   H2; the guard also fails loudly if a future migration branch is added to
   `SCHEMA_VERSION` but not to `migrate` (an unreachable-but-unguarded state).

No source change proposed for:

- H3 (staging sweep): wide enough to need an explicit decision about deleting
  files in the data directory; see proposal F1.
- H4/H5 (divergence detection on write): a per-import `doctor` pass or a new
  count invariant is a behaviour change with a runtime cost; `doctor` already
  detects and `rebuild` already repairs. Proposal F2.
- H6 (FTS insert guard): unreachable through both callers; a guard would add a
  branch that cannot happen today.
- H8/H11: correct as implemented for this project's purpose; the wording and
  CommonMark indentation limits are documentation, not defects.
- The double `close()` (H7): no behavioural effect; removing it is churn.

## Verification

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_19_knowledge_index.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_retriever.py
.venv/bin/python scripts/check_unit_tests.py -p test_library.py
.venv/bin/python scripts/check_unit_tests.py -p test_mcp_server.py
.venv/bin/python scripts/check_unit_tests.py -p test_measure_retriever_embedder.py
.venv/bin/python scripts/check_unit_tests.py -p test_workspace.py
```

To confirm the regression tests detect the two defects, run
`test_closing_fence_must_be_at_least_as_long_as_the_opening`,
`test_fence_line_with_an_info_string_does_not_close` and
`test_unknown_lower_user_version_is_refused_instead_of_returning_a_handle` against a
copy of `src/local_knowledge` with the two changes above reverted (put that copy first
on `PYTHONPATH`): all three fail there and pass against the current tree.

`GemmaProvider` weight loading is exercised only through the injected fake tokenizer
module and a patched `_load_model`, so real snapshot resolution, `torch` dtype handling
and `SentenceTransformer` prompt validation are not established by these suites; they
are covered by the separate live probes documented in `CONTRIBUTING.md`.
