# SM-19: Local knowledge index schema, identity, atomic rebuild and lazy embedder

Scope note: `bra-vom.19` cites `initialize_database`, `_ensure_index_identity`,
`_verify_vector_table`, `rebuild_index` and `chunk_sections` at
`chunking.py:135-182`. Those symbols no longer exist. At the current head the
responsibilities are `database.migrate`/`check_identity`/`doctor`,
`ingestion.ensure_vectors`/`rebuild`, `embeddings.GemmaProvider` and
`chunking.chunk_sections` (20-112). This document describes the code that is
present; the bead's questions are answered against it.

## Scope and ownership

| Area | File | Owner |
| --- | --- | --- |
| Schema version and migration | `src/local_knowledge/database.py:9-97` (`SCHEMA_VERSION`, `connect`, `migrate`) | every process that opens the database; `connect` is the only door |
| Index identity and compatibility gate | `src/local_knowledge/database.py:109-128` (`index_identity`, `check_identity`) | callers that write or read derived rows |
| Integrity report | `src/local_knowledge/database.py:131-178` (`doctor`) | read-only inspection (`knowledge doctor`, rebuild gate) |
| Derived table maintenance | `src/local_knowledge/ingestion.py:31-62` (`ensure_vectors`, `insert_chunks`) | the single writer holding `writer_lock` |
| Document mutation | `ingestion.py:65-149` (`import_file`), `167-176` (`remove`) | one writer per database file |
| Atomic rebuild | `ingestion.py:179-229` (`rebuild`) | the writer lock holder |
| Lazy embedder | `src/local_knowledge/embeddings.py:43-150` (`GemmaProvider`) | in-process, serialized by a module-level `RLock` |
| Markdown fence tracking | `src/local_knowledge/chunking.py:20-112` (`chunk_sections`) | pure function, one call per section list |

Ownership per [architecture.md](../architecture.md): `local_knowledge/` owns
"index identity and coherent documents/chunks/FTS/vector/snapshot mutation"
(line 42). Writers serialize on an advisory `flock` on `<database>.lock`
(`ingestion.py:23-28`), and every SQLite open in the package goes through
`database.connect`. A single writer operation therefore holds at most one
writable connection to the live database. Readers do not take the lock; they open
`mode=ro`, publish `PRAGMA query_only=ON` (`database.py:33`) and re-check identity
inside one read transaction (`retrieval.py:123-138`). Published replacement is
`os.replace`, so a reader holding the old inode keeps a consistent snapshot.

The embedder is owned by whichever process constructed it: the desktop's
`KnowledgeService`, `knowledge serve` (MCP) or a CLI run. It holds no durable
state; identity is configuration plus library versions. The desktop already
serializes all native work through `KnowledgeService._worker_lock`
(`src/blackrose/knowledge.py:14`, `17-19`); the module-level embedder lock exists
for the MCP server, whose sync tools run on worker threads, and `initialize()` is
called once from `create_server` (`mcp_server.py:12-18`).

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `PRAGMA user_version` | read `database.py:27`, `39`; written `database.py:78`, `95` | int32 in the SQLite header | persisted schema generation: 0 none, 1 keyword-only FTS, 2 FTS with `source` | `migrate` only |
| `SCHEMA_VERSION` | `database.py:9` | int constant | generation this code can serve | module load |
| `metadata['index_identity']` | `database.py:114-125` | canonical JSON text | hashable embedding + chunking + extraction identity (`database.py:109-111`) | `check_identity(initialize=True)` |
| `metadata['query_prefix']` | `database.py:124-128` | text | query-side prompt compatibility (`embeddings.py:10`) | `check_identity(initialize=True)` |
| `metadata['source:<document_id>']` | `provenance.py:20-23` | JSON text | explicit source provenance, independent of content | `import_file:143-144`, `update_metadata`, `rebuild` preserves it (`ingestion.py:199`) |
| `documents.rowid`/`id`, `documents.content_hash`, `documents.revision` | `database.py:53-57` | rows | document identity and change detection | `import_file:106-140` |
| `source_sections.sections` | `ingestion.py:141` | JSON list of `Section` | retained extraction; the only rebuild input | `import_file:141` |
| `chunks.rowid` | `database.py:63-69` | INTEGER PRIMARY KEY | the shared row identity of every derived row | `insert_chunks:55-58` |
| `embeddings.chunk_rowid` → `vector` | `database.py:70-71` | row + float32 blobs | authoritative embedding, FK cascade on chunk delete | `insert_chunks:59` |
| `vectors.rowid` → `embedding` | `ingestion.py:42` | vec0 virtual table | KNN index over the same row identities | `insert_chunks:60`; trigger `ingestion.py:43-44` |
| `chunks_fts.rowid` | `database.py:88-94` | FTS5 virtual table | keyword index incl. `source` from schema 2 | `insert_chunks:61-62`; trigger `database.py:92-94` |
| `vector_dimensions(db)` | `database.py:100-106` | `None` / `0` / int | declared `float[N]` shape parsed from `sqlite_master`, works with zero rows | reader |
| `Config.dimensions`, `model_revision`, `chunk_tokens`, `chunk_overlap` | `config.py:14-47` | ints/str | inputs of the identity | config load |
| `GemmaProvider._tokenizer` | `embeddings.py:47` | object or `None` | lazy tokenizer cache | `tokenizer` property `60-70` |
| `GemmaProvider._model` | `embeddings.py:46` | object or `None` | lazy weights cache | `_load_model` `103-118` |
| `_INITIALIZATION_LOCK` | `embeddings.py:15` | module `RLock` | serializes lazy construction process-wide | `tokenizer`/`model` |
| `chunk_sections.fence` | `chunking.py:29` | `tuple[str, int]` or `None` | `(fence char, opening run length)` inside one section | loop `31-40` |
| `chunk_sections.headings` | `chunking.py:22` | `list[str]` | current heading path, reset only by depth | loop `41-46` |
| staging file | `ingestion.py:187` | path `knowledge-rebuild-*` | unpublished replacement database | `rebuild` |

## States

Schema state (`connect` decides from `user_version`, `database.py:27-40`):

| State | Predicate |
| --- | --- |
| `Current` | `user_version == 2`; `migrate` is a no-op |
| `LegacyKeywordFTS` | `user_version == 1`; migrated in place on a writable open |
| `Uninitialized` | `user_version == 0`; creates v1 then v2 in one call |
| `FutureSchema` | `user_version > 2`; refused before migration |
| `UnknownSchema` | `user_version < 0` (or any value `migrate` cannot reach); now refused |
| `PartialZero` | `user_version == 0` but schema objects already exist; migration fails closed |

Index consistency state (only meaningful in `Current`; `doctor` names most of them):

| State | Predicate |
| --- | --- |
| `EmptyInitialized` | no `chunks`, no `index_identity`, no `vectors` table |
| `Healthy` | identity present and equal; `chunks == embeddings == vectors == chunks_fts`; declared vector dimensions equal `config.dimensions` |
| `IdentityMissingPopulated` | `chunks` nonempty and no `index_identity` (`database.py:122-123`) |
| `IdentityMismatch` | stored identity differs from the computed one (`database.py:117`) |
| `QueryIncompatible` | `query_prefix` missing or different (`database.py:126-128`) |
| `VectorDimensionRepairable` | dimensions differ, but no identity, no chunks, no vectors (`ingestion.py:37-41`) |
| `VectorDimensionBlocking` | dimensions differ on a populated index (`ingestion.py:38-39`) |
| `VectorRowsMissing` | vector rows are a strict subset of chunk rows; only `doctor` sees it (`database.py:152-153`) |
| `DerivedRowMissing` | a chunk lacks an `embeddings`/`chunks_fts` row; only `doctor` sees it (`database.py:145-147`) |

Rebuild state (`ingestion.py:179-229`):

| State | Predicate |
| --- | --- |
| `Idle` | no staging file, no lock held |
| `Staging` | `knowledge-rebuild-*` exists; the lock is held; the original is untouched |
| `Verified` | staging passes `doctor` (`ingestion.py:211-212`) and has been fsynced |
| `Published` | `os.replace` returned and the directory fsynced (`ingestion.py:219-225`) |
| `Failed` | an exception escaped; the `finally` unlinked the staging file (`ingestion.py:226-229`) |
| `Stranded` | the process died before the `finally`; the staging file remains |

Embedder state (per provider instance):

| State | Predicate |
| --- | --- |
| `Unpinned` | `config.model_revision == ""`; every snapshot path refuses (`embeddings.py:50-51`) |
| `PinnedSnapshotUnavailable` | `snapshot_download(local_files_only=True)` raised `OSError` (`embeddings.py:52-57`) |
| `TokenizerUnloaded` | `_tokenizer is None` |
| `TokenizerReady` | `_tokenizer is not None` |
| `ModelUnloaded` | `_model is None` |
| `ModelReady` | `_model is not None` |
| `Failed(component)` | an attempt raised; the caches are unchanged, the error is in diagnostics (`embeddings.py:72-77`) |

Chunker state (per section text): `OutsideFence`, and `InsideFence(char, run)`.

## Events

- `connect(config)` / `connect(config, readonly=True)` — every database operation.
- `migrate(db, version)` — called by a writable `connect` (`database.py:35`).
- `check_identity(db, config, provider, initialize=bool)` — called on import
  (`ingestion.py:107`, `130`), on search (`retrieval.py:127`, `138`), in `doctor`
  (`cli.py:121`) and in rebuild (`ingestion.py:201`).
- `ensure_vectors(db, dimensions)` — on import and rebuild.
- `import_file`, `remove`, `rebuild` — the three writers; each takes `writer_lock`.
- `search`, `open_document`, `list_documents`, `knowledge doctor` — read-only opens.
- Process exit / `SIGKILL` / power loss — the only event that can strand a staging file.
- `GemmaProvider.tokenizer`, `.model`, `.initialize()`, `.identity()` — lazy-load and identity entry points.
- `chunk_sections(sections, title, provider, config)` — once per section list.

## Transition table

### Schema and identity

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | `connect(rw)` | `user_version > 2` (`database.py:28-29`) | raise `ValueError("Database is newer…")`; handle closed (`42-44`) | unchanged |
| `Uninitialized` | `connect(rw)` | `version == 0` (`49`) | one transaction creates all v1 tables, `PRAGMA user_version=1` (`50-80`) | `LegacyKeywordFTS` |
| `Uninitialized` | same call | falls through `version = 1` (`81`) | one transaction rebuilds FTS with `source`, `user_version=2` (`84-96`) | `Current` |
| `LegacyKeywordFTS` | `connect(rw)` | `version == 1` (`82`) | FTS recreated and backfilled from `chunks`/`documents`, `user_version=2` (`84-96`) | `Current` |
| `UnknownSchema` | `connect(rw)` | `migrate` matched nothing | re-read version differs from `SCHEMA_VERSION` → `ValueError` (`36-40`) | unchanged |
| `PartialZero` | `connect(rw)` | `version == 0` and a table exists | `CREATE TABLE` fails; `executescript` rolls the step back; handle closed | `PartialZero` |
| `Current` | `connect(rw)` | `version == 2` | no statement written; identity untouched | `Current` |
| `Current` | `connect(ro)` | `version != 2` (`31-32`) | raise "Run knowledge init…" | unchanged |
| `Current` | `connect(ro)` | `version == 2` | `PRAGMA query_only=ON` (`33`) | `Current` read-only |
| `EmptyInitialized` | `import_file` | identity absent, no chunks (`database.py:119-125`) | insert `index_identity` + `query_prefix` inside the write transaction | `Healthy` |
| `IdentityMissingPopulated` | `import_file` | identity absent, chunks present (`122-123`) | raise "Index identity is missing; run knowledge rebuild explicitly" | unchanged |
| `IdentityMismatch` | `import_file`/`search` | stored ≠ computed (`117-118`) | raise "Index configuration mismatch; run knowledge rebuild explicitly" | unchanged |
| `QueryIncompatible` | `check_identity` | query prefix differs (`126-128`) | raise "Query configuration incompatible…" | unchanged |
| `VectorDimensionRepairable` | `ensure_vectors` | dims differ and no identity/chunks/vectors (`37-41`) | drop trigger and table, recreate at `config.dimensions` (`42-44`) | `EmptyInitialized` |
| `VectorDimensionBlocking` | `ensure_vectors` | dims differ on populated index (`38-39`) | raise "…run knowledge rebuild explicitly" | unchanged |
| `Healthy` | `ensure_vectors` | dims equal | `CREATE … IF NOT EXISTS` only (`42-44`) | `Healthy` |
| any | `doctor` | — | reads only; returns `{"ok", "issues"}` (`database.py:131-178`) | unchanged |

### Rebuild

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `Idle` | `rebuild` | flock acquired (`183`) | `mkstemp` staging file (`187-188`), migrate it (`190`) | `Staging` |
| `Staging` | backup | — | `original.backup(staging)` copies documents, tags, metadata, `source_sections` (`193`) | `Staging` |
| `Staging` | reset | — | drop `vectors` + trigger, delete chunks/FTS/index metadata, keep `source:%` (`195-199`), recreate vectors, initialize identity (`200-201`) | `Staging` |
| `Staging` | per document | `source_sections` row present (`203-205`) | chunk + embed + insert into staging (`206-210`) | `Staging` |
| `Staging` | per document | row absent (`204-205`) | raise "Missing retained extraction; re-import original source" | `Failed` |
| `Staging` | integrity gate | `doctor(staging)` not ok (`211-212`) | raise "Rebuilt database failed integrity check" | `Failed` |
| `Staging` | publish | — | close connections, chmod `0o600`, fsync file, `os.replace`, fsync directory (`213-225`) | `Published` |
| `Staging` | any exception | — | `finally` closes and unlinks staging; original untouched (`226-229`) | `Failed` |
| `Staging` | process killed | no `finally` runs | staging file remains | `Stranded` |
| `Published`/`Failed` | next `import_file`/`search` | identity in the new file | normal `Current` behaviour | `Healthy` |

### Embedder

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `Unpinned` | `tokenizer`/`model` | `not model_revision` (`50-51`) | raise `ValueError` inside the lock; nothing cached | `Failed(tokenizer\|model)` |
| `PinnedSnapshotUnavailable` | `_snapshot` | `snapshot_download` `OSError` (`56-57`) | raise "Model unavailable offline…" | `Failed(...)` |
| `TokenizerUnloaded` | `tokenizer` | lock acquired (`61`), `_tokenizer is None` (`62`) | import `AutoTokenizer`, `from_pretrained(snapshot)` (`66-67`) | `TokenizerReady` |
| `TokenizerUnloaded` | `tokenizer` | construction raised (`68-69`) | record diagnostic, raise `EmbeddingInitializationError`, `_tokenizer` still `None` | `Failed(tokenizer)` |
| `Failed(tokenizer)` | `tokenizer` again | `_tokenizer is None` | retry from scratch — no backoff, no cached failure (`62`) | `TokenizerReady` or `Failed(tokenizer)` |
| `ModelUnloaded` | `model` | lock held by caller (`97`), `_model is None` (`104`) | import torch + sentence-transformers, set threads, load snapshot on CPU (`105-112`), validate 8192 limit and `SearchQuery` prompt (`113-116`) | `ModelReady` |
| `ModelUnloaded` | `model` | any exception (`100-101`) | diagnostic + `EmbeddingInitializationError`, `_model` still `None` | `Failed(model)` |
| any | `identity()` | — | read config + `importlib.metadata` versions (`139-147`); no lock, no weights | unchanged |
| any | `initialize()` | — | `embed_query` drives tokenizer+model+inference; failures re-raise as initialization errors (`85-90`) | `ModelReady` or unchanged |

### Chunker fence

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `OutsideFence` | fence run `≥3` of `` ` `` or `~` | `fence is None` (`35-36`) | remember `(char, run length)` | `InsideFence(char, n)` |
| `InsideFence` | marker line | same char and `run ≥ n` and no info string (`37-40`) | `fence = None` | `OutsideFence` |
| `InsideFence` | marker line | shorter run, other char, or info string | ignored; the line stays content | `InsideFence` |
| `InsideFence` | heading line | `fence is not None` (`41`) | no heading, no boundary | `InsideFence` |
| `InsideFence` | blank line | `fence is not None` (`48`) | no boundary | `InsideFence` |
| `OutsideFence` | heading line | matches `^(#{1,6})\s+…` (`41-46`) | update heading path; add a heading boundary | `OutsideFence` |
| `InsideFence` | end of section | — | state discarded; the next section starts `OutsideFence` (`23-29`) | `OutsideFence` |

## Invariants

1. A writable `connect` returns a handle only when `user_version == SCHEMA_VERSION`;
   a read-only one refuses anything else. Enforced `database.py:27-40`; tests
   `IndexSchemaStateTests.test_unknown_lower_user_version_is_refused_instead_of_returning_a_handle`,
   `test_fresh_database_reaches_current_schema_without_a_vector_index`, and
   `tests/test_retriever.py::test_future_schema_and_symbolic_source_are_rejected`.
2. Migration advances one generation per transaction (`BEGIN IMMEDIATE` …
   `COMMIT` inside each `executescript`), so a crash leaves the previous
   generation, which the next open resumes from. Enforced `database.py:48-97`;
   test `test_partially_migrated_zero_version_fails_closed_and_stays_partial`.
3. A failed migration never leaves a partially created schema; the failed step
   rolls back. Enforced by the same scripts; same test asserts `chunks` does not
   appear and the version is unchanged.
4. Every derived row shares `chunks.rowid`, and a chunk insert writes chunks,
   embeddings, vectors and FTS inside one transaction (`ingestion.py:125-146`).
   Test `IndexConsistencyStateTests.test_vector_rows_can_vanish_without_blocking_writes_or_search`
   (counts go 1→1→1→1 on import) and `tests/test_retriever.py::test_import_search_modes_filters_and_citations` (`healthy`).
5. Deleting a chunk removes its embedding (FK cascade, `database.py:70-71` +
   `foreign_keys=ON` at `database.py:21`), its vector (trigger
   `ingestion.py:43-44`) and its FTS row (`database.py:92-94`). Test
   `test_deleting_a_chunk_row_removes_every_derived_row`.
6. `index_identity` is inserted only while the index is empty; a populated index
   without identity refuses writes with an explicit rebuild message. Enforced
   `database.py:114-125`; `tests/test_retriever.py::test_missing_identity_on_populated_index_requires_explicit_rebuild`.
7. A stored identity that differs from the computed one refuses both import and
   search without mutating anything. Enforced `database.py:117-118`;
   `tests/test_retriever.py::test_identity_mismatch_rejects_search_and_import_without_mutation`.
8. An empty vector table at the wrong dimensions is repaired; a populated one
   raises instead of being discarded. Enforced `ingestion.py:32-44`; tests
   `test_rebuild_publishes_a_private_replacement_and_leaves_no_staging_file`
   (dims change on a populated index is done by rebuild) and
   `tests/test_retriever.py::test_legacy_empty_vector_table_is_repaired_but_doctor_reports_mismatch`.
9. Rebuild publishes only a `doctor`-clean database, and publishes it under the
   writer lock with `fsync(file)`, `os.replace`, `fsync(dir)`. Enforced
   `ingestion.py:183-225`; tests `test_rebuild_publishes_a_private_replacement_and_leaves_no_staging_file`,
   `test_rebuild_integrity_gate_keeps_the_original_database`.
10. Any in-process rebuild failure leaves the original database logically
    unchanged and removes the staging file. Enforced `ingestion.py:226-229`;
    same two tests plus `tests/test_retriever.py::test_failed_rebuild_preserves_original_database`.
11. Rebuild reads only `source_sections`; a document without retained extraction
    aborts the rebuild instead of being dropped. Enforced `ingestion.py:202-205`;
    test `test_rebuild_refuses_a_document_without_retained_extraction`.
12. `GemmaProvider.identity()` never loads the tokenizer or the weights and works
    in the `Unpinned` state. Enforced `embeddings.py:139-147`; tests
    `test_identity_does_not_load_the_tokenizer_or_model`,
    `test_unpinned_provider_refuses_the_snapshot_and_keeps_identity_available`.
13. At most one successful construction per provider per component, and
    constructions are serialized process-wide by one `RLock`. Enforced
    `embeddings.py:15`, `61`, `97`; tests
    `test_concurrent_first_use_loads_each_model_once`,
    `test_concurrent_tokenizer_initialization_is_serialized_process_wide`.
14. A failed initialization caches nothing, so a later call retries the same
    component. Enforced `embeddings.py:62-70`, `98-101`; tests
    `test_failed_tokenizer_initialization_is_not_cached_and_a_later_call_retries`,
    `test_failed_model_initialization_is_not_cached_and_a_later_call_retries`.
15. Fence state is (a) one fence at a time, (b) closed only by the same
    character, an equal-or-longer run, and no info string, (c) never carried into
    the next section. Enforced `chunking.py:29-40`; all six
    `ChunkerFenceStateTests` tests.
16. While inside a fence, heading lines and blank lines produce neither heading
    metadata nor chunk boundaries (`chunking.py:41`, `48`).

## Hazards and open questions

| # | Severity | Location | Finding | Demonstrated |
| --- | --- | --- | --- | --- |
| H1 | `defect` (fixed) | `chunking.py:32-40` (pre-fix `32-38`) | The closer accepted any run of the same character, including one shorter than the opener and one with an info string (` ```python `). A line inside a four-backtick block or after a ```python opener therefore closed the fence; subsequent `#` lines became headings and blank lines became boundaries, so fenced code was split and mislabelled. | Yes — `test_closing_fence_must_be_at_least_as_long_as_the_opening`, `test_fence_line_with_an_info_string_does_not_close` fail before, pass after. |
| H2 | `fragile` (fixed) | `database.py:35-40` (pre-fix `35`) | `migrate` handles only 0, 1 and 2. A negative `user_version` returned a handle with no schema; the first query died with `sqlite3.OperationalError: no such table`. Not reachable through this code, but unguarded and indistinguishable from a corrupted DB. | Yes — `test_unknown_lower_user_version_is_refused_instead_of_returning_a_handle` fails before, passes after. |
| H3 | `fragile` | `ingestion.py:187`, `226-229` | The staging file is unlinked only by the `finally`. `SIGKILL`, a power cut or an OOM kill during rebuild strands a full-size copy of the database as `knowledge-rebuild-*` in the data directory, and nothing sweeps it later. Disk-space leak only; the published database is never at risk because `os.replace` is atomic. Confirming it needs a kill-during-rebuild run; no test pins a leak. | No test. |
| H4 | `fragile` | `ingestion.py:129-130`, `107`, `database.py:137-157` | `vectors` can diverge from `chunks` without triggering a rebuild: `ensure_vectors` only checks declared dimensions and `check_identity` only checks identity, so a missing vector row, a missing `embeddings` row, a missing FTS row or a `DROP TABLE vectors` is invisible to `import_file`. Only `doctor` reports it (`database.py:137-153`), and unfiltered vector search then silently returns fewer results because it queries `vectors` (`retrieval.py:148`), while filtered vector search reads `embeddings` (`retrieval.py:152-155`) and still works. Repaired only by `rebuild`. | Yes — `test_vector_rows_can_vanish_without_blocking_writes_or_search`. |
| H5 | `fragile` | `ingestion.py:37-42` | If the `vectors` table is absent entirely while chunks exist, `vector_dimensions` returns `None`, the dimension branch is skipped, and an **empty** vector index is created with no backfill. The result is exactly the H4 state reached through the repair path rather than an error. | Same test covers row loss; the table-drop variant is asserted in the probe, not in a test. |
| H6 | `fragile` | `ingestion.py:61-62` | `insert_chunks` writes the FTS row with `INSERT … SELECT … FROM documents WHERE id=?`. If the document row were missing, the FTS insert would silently no-op while chunks/embeddings/vectors were written. Both current callers insert the document first (`ingestion.py:137-140`) or copy it (`193`), so this is unreachable today; `doctor` would report "FTS/chunk row mismatch". | No test (unreachable). |
| H7 | `none-found` | `ingestion.py:215`, `227` | `original.close()` runs twice (once on the success path, once in `finally`). `sqlite3.Connection.close()` is idempotent; no state effect. Cosmetic only. | n/a |
| H8 | `documentation-gap` | `chunking.py:32` | `^\s*` accepts any indentation, so an indented code block (4+ spaces) is treated as a fenced block, and a fence marker inside indented code changes the fence state. A backtick info string containing a backtick is also accepted as an opener, which CommonMark forbids. Neither was changed: they do not break the stated intent (group fenced code) and changing them would alter chunk boundaries of existing indexes. | No test. |
| H9 | `none-found` | `database.py:12-44` | `connect` is the only constructor of SQLite handles, always enables `foreign_keys` and loads `sqlite-vec`; read-only handles are additionally `query_only`. The one trust boundary that matters — a writer that bypasses `writer_lock` — is outside this module (an external process can still open the file). Readers rely on `os.replace` atomicity plus the identity re-check, which `tests/test_retriever.py::test_search_rechecks_identity_after_a_rebuild_during_inference` covers. | Existing tests. |
| H10 | `none-found` | `embeddings.py:61`, `97`, `104` | Two first-callers cannot both load: the check and the assignment are inside one process-wide `RLock`, not a `double-checked` read outside it. The lock also covers two independent `GemmaProvider` instances, which is what MCP worker threads need (`KnowledgeService` additionally serializes with its own worker lock). `torch.set_num_threads` (`107`) mutates process-global state, but only under the same lock. Two *processes* each load their own copy; that is a memory cost, not a state hazard. | Tests 13 above. |
| H11 | `documentation-gap` | `embeddings.py:139-147` | Identity includes `transformers`/`sentence-transformers` versions, so a dependency patch bump makes every existing index report `IdentityMismatch` and requires a full rebuild. That is deliberate (input format and prompt validation can change) but the user-visible message says "Index configuration mismatch", not "library changed". | No test. |

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

1. `src/local_knowledge/chunking.py:29-40` — fence state is now
   `(character, opening run length)` and a closer must match the character, be at
   least as long, and carry no info string. Reason: H1 broke the function's own
   contract ("Group fenced code as one unit", line 26) and produced wrong
   `chunks.heading` values that are user-visible in search results. The change is
   local to the fence tracker; heading/boundary logic is untouched.
2. `src/local_knowledge/database.py:36-40` — after `migrate`, re-read
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

```
.venv/bin/python scripts/check_unit_tests.py -p test_sm_19_knowledge_index.py -v
  Ran 21 tests in 0.449s
  OK

.venv/bin/python scripts/check_unit_tests.py -p test_retriever.py
  Ran 22 tests in 0.879s
  OK

.venv/bin/python scripts/check_unit_tests.py -p test_library.py
  Ran 17 tests in 1.296s
  OK

.venv/bin/python scripts/check_unit_tests.py -p test_mcp_server.py
  Ran 1 test in 0.069s
  OK

.venv/bin/python scripts/check_unit_tests.py -p test_measure_retriever_embedder.py
  Ran 2 tests in 0.001s
  OK

.venv/bin/python scripts/check_unit_tests.py -p test_workspace.py
  Ran 48 tests in 0.741s
  OK

.venv/bin/ruff check src/local_knowledge/chunking.py src/local_knowledge/database.py tests/test_sm_19_knowledge_index.py
  All checks passed!
```

Before/after demonstration for the two fixes was run against a copy of the
pre-change tree (`PYTHONPATH=/tmp/opencode/sm19-old/src` plus `tests` on the path):
three failures there (`test_closing_fence_must_be_at_least_as_long_as_the_opening`,
`test_fence_line_with_an_info_string_does_not_close`,
`test_unknown_lower_user_version_is_refused_instead_of_returning_a_handle`), all
passing in the worktree.

The full offline gate (`.venv/bin/python scripts/check_health.py --report-dir
/tmp/opencode/sm19-health`) was run after the changes and did **not** pass in this
worktree: `check_unit_tests.py` reported `Ran 1204 tests … FAILED (failures=6,
errors=53, skipped=23)`, `check_recovery.py` and `ruff check` also failed. The
causes are outside this bead and reproduce without these changes: 49 errors are
`FileNotFoundError` for generated artefacts (`src/blackrose/gui/static/
ui-contract.json`, the `ui-runtime` bundle → "Interactive UI runtime unavailable;
rebuild ui-runtime"), 2 are "Motion Canvas needs setup", and the remaining
failures are sibling `test_sm_02_*`/`test_sm_04_*` work plus one
`B017` ruff finding in `tests/test_sm_02_stored_message_lifecycle.py`. No failing
test imports `local_knowledge`. `mypy` and the five `node --check` runs passed.

No live model, GPU or network probe was run; `GemmaProvider` weight loading is
exercised only through the injected fake tokenizer module and a patched
`_load_model`, so real snapshot resolution, `torch` dtype handling and
`SentenceTransformer` prompt validation remain unverified here (they are covered
by the separate live probes documented in `CONTRIBUTING.md`).
