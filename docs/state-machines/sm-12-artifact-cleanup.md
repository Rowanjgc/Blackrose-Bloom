# SM-12: Durable artifact cleanup queue and reconciliation

## Scope and ownership

| Surface | Location |
| --- | --- |
| `ArtifactLifecycle` (`exclusive`, `report`, `retry`, `repair`), `reconciliation`, `settle`, path predicates; the ownership `inventory` is `src/blackrose/storage/repository.py::read_ownership_inventory` | `src/blackrose/artifacts.py`, `src/blackrose/storage/repository.py` |
| Cleanup rows and their transitions | `src/blackrose/storage/repository.py::Repository.delete_conversation`, `src/blackrose/storage/repository.py::Repository.cleanup_jobs`, `src/blackrose/storage/repository.py::Repository.queue_cleanup`, `src/blackrose/storage/repository.py::Repository.finish_cleanup`; schema `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 4) |
| Command entry points and the controller's last result | `src/blackrose/chat.py::ChatController.delete_current_conversation`, `src/blackrose/chat.py::ChatController.artifact_report`, `src/blackrose/chat.py::ChatController.retry_artifact_cleanup`, `src/blackrose/chat.py::ChatController.repair_artifacts`, `src/blackrose/chat.py::ChatController._delete_current_conversation` |
| Startup retry | `src/blackrose/runtime.py::Runtime.open` |
| Offline inspection/repair CLI | `src/blackrose/artifact_cli.py::run` |
| User-facing contract | `docs/artifact-cleanup.md` |

One `ArtifactLifecycle` exists per `ChatController` (a process-level object); the
drain is owned by the single asyncio loop that runs turns
([runtime ownership](../architecture.md#runtime-ownership-and-code-health)). Three different owners meet here:

- **The obligation rows** are owned by `storage/repository.py`. Only
  `delete_conversation`, `queue_cleanup` and `finish_cleanup` write them, and
  `delete_conversation` writes the blob obligations in the same transaction that
  deletes the chat rows (`src/blackrose/storage/repository.py::Repository.delete_conversation`), so an obligation cannot exist
  without its effect having been requested, and rows cannot be deleted without
  their obligations.
- **The bytes** are owned by `AttachmentStore`, whose names are content
  addresses and which treats an already-absent file as a successful delete
  (`src/blackrose/attachments/store.py::AttachmentStore.delete`).
- **The right to act** is the in-process `_lock` plus the cross-process desktop
  lock. `exclusive()` is taken by every complete turn
  (`src/blackrose/turns.py::TurnRunner.run`), every lifecycle command (the four
  `ChatController` commands above), output recovery (`src/blackrose/chat.py::ChatController.recover_output`),
  approved file-edit persistence (`src/blackrose/chat.py::ChatController.review_edit`), backup actions
  (`src/blackrose/chat.py::ChatController.edit_backup_action`), diagram repair
  (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`) and the CLI repair
  (`src/blackrose/artifact_cli.py::run`). The CLI
  inspection path takes desktop ownership read-only in name only — it is still
  `LOCK_EX` (`src/blackrose/instance.py::desktop_instance`) — so it cannot race a live desktop either.

`ArtifactLifecycle.report()` itself does not acquire `exclusive()`
(`src/blackrose/artifacts.py::ArtifactLifecycle.report`); it is safe only because both callers already hold it or
hold desktop ownership. Nothing in the module prevents a future caller from
reporting without ownership; `reconciliation` is read-only (`src/blackrose/artifacts.py::reconciliation`),
so that remains a report-consistency risk, not a deletion risk.

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `artifact_cleanup.kind` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 4) | TEXT, CHECK in `blob,partial,drafts,recovery` | what class of target the row owns | repository |
| `artifact_cleanup.target` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 4) | TEXT | store-relative path, or the decimal conversation id | repository |
| `artifact_cleanup.error` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 4) | TEXT NULL | `NULL` = never failed; text = last failure | `finish_cleanup` |
| PK `(kind,target)` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 4) | — | one obligation per class and target; makes queueing idempotent | repository |
| `message_attachments.storage_path` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 1) | TEXT | canonical blob path; also read as a raw alias | turns/approvals |
| `message_attachments.text_path` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 1) | TEXT NULL | extracted-text blob; second ownership column | turns/approvals |
| `conversations.id` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 1) | INTEGER PK AUTOINCREMENT | chat identity; never reused, so a live id always means the same chat | repository |
| store layout | `src/blackrose/attachments/store.py::AttachmentStore.write`, `src/blackrose/artifacts.py::_BLOB`, `src/blackrose/artifacts.py::partial_blob` | filesystem | `aa/bb/<sha256><suffix>`, `.<name>.partial`, `illustration-drafts/<cid>/…`, `animation-recovery.sqlite3{,-wal,-shm,-journal}` | store, tool paths, SQLite |
| `ArtifactLifecycle._lock` | `src/blackrose/artifacts.py::ArtifactLifecycle.__init__` | `asyncio.Lock` | the single writer/cleanup exclusion | `exclusive()` |
| `busy` | `src/blackrose/artifacts.py::ArtifactLifecycle.busy` | bool (derived) | `_lock.locked()` | — |
| `cleanup_result` | `src/blackrose/chat.py::ChatController.__init__` | dict of three lists | last drain outcome shown by Session | `src/blackrose/chat.py::ChatController.retry_artifact_cleanup`, `src/blackrose/chat.py::ChatController.repair_artifacts`, `src/blackrose/chat.py::ChatController._delete_current_conversation` |
| `is_generating` | `src/blackrose/chat.py::ChatController.__init__` | bool | a turn or approved edit holds the effect budget | turns, review, backups |
| `DesktopOwnership.active` | `src/blackrose/instance.py::DesktopOwnership` | bool | the process still holds the desktop flock | `desktop_instance` |
| `AnimationRecovery.path` | `src/blackrose/animation_recovery.py::AnimationRecovery.__init__` | Path | the auxiliary ledger and its own `_lock` | recovery store |
| report dict | `src/blackrose/artifacts.py::reconciliation` (its return value) | dict | derived snapshot: `blobs`, `partials`, `drafts`, `recovery`, `pending`, `unrecognized` | never persisted |

## States

**Cleanup row states** (persisted, mutually exclusive by `error`):

| State | Predicate |
| --- | --- |
| `Absent` | no row for `(kind,target)` |
| `Queued` | row exists, `error IS NULL` |
| `Failed` | row exists, `error IS NOT NULL` |

A drain additionally has in-memory phases per row — *verified* → *effect applied*
→ *acknowledging* — which are not persisted; an interruption between them lands
back in `Queued` (or `Failed` if the acknowledgement itself raised).

**Lifecycle ownership states** (in-memory):

| State | Predicate |
| --- | --- |
| `Idle` | `_lock` unlocked |
| `Held(turn)` | locked by `src/blackrose/turns.py::TurnRunner.run` |
| `Held(command)` | locked by the `ChatController` lifecycle commands, `recover_output`, `review_edit`, `edit_backup_action`, `src/blackrose/diagram_repairs.py::DiagramRepairs.repair`, `src/blackrose/artifact_cli.py::run` |
| `Held(command, settling)` | the command's task was cancelled while `settle` waits for its worker (`src/blackrose/chat.py::ChatController.delete_current_conversation`, `src/blackrose/artifacts.py::settle`) |

**Classification states** (derived per path by `reconciliation`):

| State | Predicate |
| --- | --- |
| `ReferencedPresent` | in `storage_path`/`text_path`, `is_file()`, canonical and blob-shaped |
| `ReferencedMissing` | referenced, `not is_file()` |
| `ReferencedInvalid` | referenced but not blob-shaped or not the resolved path, or unresolvable |
| `UnreferencedBlob` | on disk, blob-shaped, canonical path not in the protected set (`src/blackrose/artifacts.py::reconciliation`) |
| `Partial` | basename `.<blob>.partial` with a valid blob candidate (`src/blackrose/artifacts.py::partial_blob`) |
| `Unrecognized` | symlink, or a file that is neither a blob, a partial, nor a root recovery file (`src/blackrose/artifacts.py::reconciliation`, the file loop of the walk) |
| `DraftLive` / `DraftOrphan` | numeric top-level `illustration-drafts/<cid>` whose id is / is not in `conversations` (`src/blackrose/artifacts.py::reconciliation`, the `illustration-drafts` scan) |
| `LedgerLive` / `LedgerOrphan` / `LedgerError` | ids in `repairs` with a live / dead conversation, or a read error (`src/blackrose/artifacts.py::reconciliation`, the ledger read) |
| `NotClassified` | an unexpected **directory**; traversed, never listed |

## Events

- `ChatController.delete_current_conversation` → `exclusive` → `settle` → repository
  delete + immediate `retry()` (`src/blackrose/chat.py::ChatController._delete_current_conversation`).
- `src/blackrose/chat.py::ChatController.retry_artifact_cleanup`, called by
  `src/blackrose/runtime.py::Runtime.open` at startup.
- `ChatController.repair_artifacts(ownership)` → `ArtifactLifecycle.repair`
  (`src/blackrose/chat.py::ChatController.repair_artifacts`, `src/blackrose/artifacts.py::ArtifactLifecycle.repair`).
- `src/blackrose/chat.py::ChatController.artifact_report`; `blackrose artifacts`
  / `blackrose artifacts repair` (`src/blackrose/artifact_cli.py::run`).
- A complete turn starting or ending (`src/blackrose/turns.py::TurnRunner.run`), an
  approved edit (`src/blackrose/chat.py::ChatController.review_edit`), a backup restore/remove/prune
  (`src/blackrose/chat.py::ChatController.edit_backup_action`), diagram repair.
- Per-row failures: `OSError`, `ValueError`, `sqlite3.Error`, a `False` return
  from `AttachmentStore.delete`, an unreadable ledger, a symlinked or noncanonical
  path, an escaping target (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`).
- Interruptions: cancellation (`asyncio.CancelledError`, not caught), process death
  between effect and acknowledgement, SQLite unavailability.
- `finish_cleanup` idempotent re-queue (`INSERT OR IGNORE`) and error overwrite.

## Transition table

Guards and effects in `ArtifactLifecycle.retry` are named by the statement they
are in; all live in `src/blackrose/artifacts.py::ArtifactLifecycle.retry` unless
another symbol is given.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Absent` | delete chat `c` with attachment paths | chat row exists; `BEGIN IMMEDIATE` (`src/blackrose/storage/repository.py::Repository.delete_conversation`) | `INSERT OR IGNORE ('blob', storage_path)` and `('blob', text_path)`, then the conversation row is deleted, in the same transaction | `Queued` ×N |
| `Absent` | delete chat `c` | same transaction | `('drafts', str(c))`, `('recovery', str(c))` | `Queued` ×2 |
| `Absent` | `repair()` | `DesktopOwnership` for this DB + `exclusive` (`src/blackrose/artifacts.py::ArtifactLifecycle.repair`) | `queue_cleanup` of unreferenced blobs, partials, orphan drafts and orphan ledger ids | `Queued` |
| `Queued`/`Failed` | re-queue same `(kind,target)` | PK + `INSERT OR IGNORE` (`src/blackrose/storage/repository.py::Repository.queue_cleanup`) | none (error text preserved) | unchanged |
| `Queued` | `retry()`, `blob`/`partial`, canonical target protected | `inventory` snapshot builds `protected`; `canonical_key(target) in protected` | `preserved.append`; `finish_cleanup(kind,target)` | `Absent` (preserved) |
| `Queued` | `retry()`, `blob`, recognized, unreferenced | `blob_name(target)`, `owned_path`, `store.delete` → `True` | `completed.append`; `finish_cleanup` | `Absent` (completed) |
| `Queued` | `retry()`, `partial`, recognized, unreferenced | `partial_blob(target)` | same as above | `Absent` (completed) |
| `Queued` | `retry()`, blob/partial, `store.delete` → `False` | the `store.delete` result | `raise OSError('Attachment deletion failed; retry required.')` | `Failed` |
| `Queued` | `retry()`, blob/partial, unrecognized layout | not protected, and neither `blob_name` nor `partial_blob` | `ValueError('Unrecognized blob layout…')` → `finish_cleanup(error)` | `Failed` |
| `Queued` | `retry()`, drafts/recovery, `target` not the canonical decimal of a positive int | the `str(int(target)) != target or <= 0` check | `ValueError` → `finish_cleanup(error)` | `Failed` |
| `Queued` | `retry()`, drafts/recovery, conversation live | `get_conversation(id) is not None` | `finish_cleanup` | `Absent` (preserved) |
| `Queued` | `retry()`, drafts, orphan | `kind == 'drafts'` | `delete_conversation_drafts` (rmtree when present), `completed` | `Absent` (completed) |
| `Queued` | `retry()`, recovery, orphan | `recovery_path` non-symlink check, then `AnimationRecovery.delete` | `DELETE FROM repairs` if the ledger exists, `completed` | `Absent` (completed) |
| `Queued` | `retry()`, any row, `OSError`/`ValueError`/`sqlite3.Error` | the `except` around each job | `finish_cleanup(kind,target,str(error))` | `Failed` |
| `Failed` | `retry()` and the obstruction is gone | guards above | verified effect + `finish_cleanup` | `Absent` |
| `Failed` | `retry()` and it fails again | the `except` around each job | `UPDATE artifact_cleanup SET error=?` (`src/blackrose/storage/repository.py::Repository.finish_cleanup`) | `Failed` (new text) |
| `Queued` | cancellation between effect and acknowledgement | `run_to_completion` finishes the thread, then re-raises `CancelledError` (`src/blackrose/storage/db.py::run_to_completion`) | no acknowledgement | `Queued` |
| `Queued`/`Failed` | `finish_cleanup` succeeds for an already-absent file | `store.delete` is `missing_ok` (`src/blackrose/attachments/store.py::AttachmentStore.delete`) | `completed` | `Absent` |
| `Queued`/`Failed` | process death before acknowledgement | rows committed with the delete transaction | none | unchanged at next start |
| `Queued`/`Failed` | startup `Runtime.open` | `exclusive` via `src/blackrose/chat.py::ChatController.retry_artifact_cleanup` | `retry()` | per rows above |

Ownership transitions:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Idle` | turn starts | — | `async with exclusive()` around the whole `_run` (`src/blackrose/turns.py::TurnRunner.run`) | `Held(turn)` |
| `Idle` | lifecycle command | — | acquire `_lock` (`src/blackrose/artifacts.py::ArtifactLifecycle.exclusive`) | `Held(command)` |
| `Held(*)` | another turn or command | `_lock.locked()` | `raise ChatBusy`; nothing queues | unchanged |
| `Held(command)` | command returns/raises | — | context-manager exit | `Idle` |
| `Held(command)` | cancel `delete_current_conversation` | operation wrapped by `settle` (`src/blackrose/chat.py::ChatController.delete_current_conversation`) | wait for the worker, then re-raise `CancelledError` | `Held(command, settling)` → `Idle` |
| `Held(command)` | cancel `report`/`retry`/`repair` | not wrapped by `settle`; each `run_to_completion` still settles | next await raises; rows keep their last state | `Idle` (partial drain) |
| `Held(turn)` | turn completes, fails or is stopped | `TurnRunner` settles its own persistence | unlock | `Idle` |

## Invariants

1. **Acknowledgement follows effect.** `finish_cleanup(kind, target)` with
   `error=None` is called only after the effect was verified: `store.delete`
   returned true, the conversation was absent, the draft tree was removed, or the
   ledger rows were deleted (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`).
2. **Protection is decided on the canonical path.** A `blob`/`partial` target
   whose resolved store-relative path is any `message_attachments.storage_path` or
   `text_path` is never unlinked (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`,
   the `protected` set). Both columns are read in one snapshot
   (`src/blackrose/storage/repository.py::read_ownership_inventory`).
3. **Deletion is confined.** No unlink happens through a symlink, a noncanonical
   path, or a path outside the store root: `owned_path` runs before
   `store.delete` (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`,
   `src/blackrose/artifacts.py::owned_path`,
   `src/blackrose/attachments/store.py::AttachmentStore.path_for`).
4. **Unrecognized layouts are never deleted.** Neither `reconciliation` nor
   `repair` queues `unrecognized` entries; `retry` refuses a target that is not
   blob- or partial-shaped (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`,
   `src/blackrose/artifacts.py::ArtifactLifecycle.repair`).
5. **Conversation identities are canonical.** `drafts`/`recovery` targets must
   satisfy `str(int(target)) == target` and `int(target) > 0`
   (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`); `'007'`, `'0'`, `'-1'`,
   `'7.0'` are refused.
6. **A live chat protects its auxiliary state.** Drafts and ledger rows are
   preserved while `get_conversation(id)` returns a row
   (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`); `AUTOINCREMENT` ids
   (`src/blackrose/storage/migrations.py::MIGRATIONS`, migration 1) mean a live id
   cannot belong to a different chat.
7. **Nothing is lost when an acknowledgement fails.** Every row that survives a
   drain has non-null `error`; every repeatable effect is idempotent
   (`store.delete` missing-ok, `rmtree` only when the root exists,
   `DELETE FROM repairs` only when the ledger exists). `completed` and `pending`
   may name the same obligation in that case, which is the honest report.
8. **Obligations outlive their rows and are drained exactly once per event.**
   `artifact_cleanup` has no foreign key
   (`src/blackrose/storage/migrations.py::MIGRATIONS`, migration 4); `retry` runs
   only under `exclusive` and returns `pending` from a fresh read
   (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`, its first and last
   `cleanup_jobs` calls).
9. **One holder.** `exclusive()` admits at most one turn or command and refuses
   the next immediately with `ChatBusy` rather than queueing it
   (`src/blackrose/artifacts.py::ArtifactLifecycle.exclusive`).
10. **Reconciliation is read-only.** `report()` performs no write to the store or
    the ledger: the walk is `followlinks=False` and the ledger is opened through
    `readonly_snapshot` (`src/blackrose/artifacts.py::reconciliation`).
11. **File classification is total and disjoint** outside the pruned
    `illustration-drafts` subtree and the root recovery sidecars: each regular
    file is a referenced blob, an unreferenced blob, a partial, or unrecognized
    (`src/blackrose/artifacts.py::reconciliation`, the file loop of the walk).
12. **Repair queues only what it classified.** The job list is exactly
    unreferenced blobs, partials, orphan draft ids and orphan ledger ids
    (`src/blackrose/artifacts.py::ArtifactLifecycle.repair`); nothing else is ever
    created by repair.

## Hazards and open questions

Answers to the investigation's focus questions, then the numbered findings.

- *Is the row lifecycle total?* Yes in the sense that a row is only removed by a
  verified effect and never disappears silently; `Queued` and `Failed` are the
  only persistent states, and every write to them is a row-level decision. It is
  not total in the sense of *terminating*: `Failed` has no terminal "abandoned"
  state and no supported way to clear it (hazard 4).
- *Can a target be deleted while referenced, or preserved while unreferenced,
  under a concurrent `TurnRunner` insert?* No. The turn holds `exclusive` for its
  entire `_run` (`src/blackrose/turns.py::TurnRunner.run`), blob bytes and
  attachment rows are written only inside that scope
  (`src/blackrose/turns.py::TurnRunner._begin_turn`,
  `src/blackrose/chat.py::ChatController._persist_attachments`), and `attach()`
  writes nothing to disk (`src/blackrose/chat.py::ChatController.attach`;
  `tests/test_gui_images.py::ImageTests.test_attachment_shows_a_preview_before_send_and_after_reopen`
  asserts the store does not exist after `attach`). The `protected` snapshot and the unlink therefore cannot
  interleave with an attachment insert. Preservation can only be *stale in the
  conservative direction*: a job is preserved if any row still references the
  bytes at snapshot time, and it is re-queued by the next
  `delete_conversation` (`src/blackrose/storage/repository.py::Repository.delete_conversation`).
- *Is `reconciliation` exhaustive?* Over files, yes (I11). Over directories, no:
  only files and symlinks are reported, so an unexpected empty directory is
  invisible (hazard 6). It never acts on an unrecognized layout: it reports it,
  and a symlinked `illustration-drafts` or ledger sidecar is refused rather than
  followed (`src/blackrose/artifacts.py::reconciliation`,
  `src/blackrose/artifacts.py::recovery_path`).
- *Does `exclusive()` really exclude turn work?* Yes —
  `src/blackrose/turns.py::TurnRunner.run` holds it across the whole turn, and
  every other application writer of `message_attachments`
  (`src/blackrose/chat.py::ChatController._save_output`, reached from
  `recover_output` and `review_edit`) holds it too. Chat *creation* is not covered
  (`TurnRunner.run` calls `ensure_conversation` before taking the lock), but it
  inserts no attachment rows, so it cannot create a reference the drain missed.
- *What happens if `finish_cleanup` fails after the file was deleted?* The row
  survives; the drain then tries to record the failure and either stores a
  database error on a completed effect or aborts (hazards 2 and 3). The next drain treats
  the absent file as success and clears the row (demonstrated by
  `tests/test_sm_12_artifact_cleanup.py::QueueTests.test_failed_acknowledgement_after_unlink_is_reported_and_retried`).

**1. `fragile` (fixed) — `src/blackrose/artifacts.py::ArtifactLifecycle.retry`, the
`blob`/`partial` branch — a still-referenced noncanonical
target became a permanently failed obligation.** `retry()` validated the layout
before consulting the protected set, so a `delete_conversation` obligation
carrying a damaged alias (which `Repository.delete_conversation` inserts verbatim) became
`Failed` with `"Unrecognized blob layout"` even though a surviving attachment row
still resolved to the same bytes. No retry can clear it and `repair` never queues
it, so the drain logs a pending obligation at every start
(`src/blackrose/chat.py::ChatController.retry_artifact_cleanup`),
Session shows it after the next delete
(`src/blackrose/gui/session.py::Session.delete_conversation`), and
`blackrose artifacts repair` exits 1 forever. Demonstrated by the regression test
`tests/test_sm_12_artifact_cleanup.py::QueueTests.test_referenced_noncanonical_target_is_preserved_not_failed`
(fails before the change, passes after). Fixed by deciding protection on the canonical path and
before the layout check; safety is unchanged because bytes whose canonical path is
referenced were never deletable anyway.

**2. `fragile` — `src/blackrose/artifacts.py::ArtifactLifecycle.retry`, the `except`
around each job — an unmodelled exception aborts the whole
drain.** The `except` covers `OSError`, `ValueError` and `sqlite3.Error` only, and
a failure of the *failure* acknowledgement propagates out of the loop. Every
remaining row keeps its last state (no loss), but a persistent fault in the
`blob` class (ordered first, `src/blackrose/storage/repository.py::Repository.cleanup_jobs`)
defers `drafts` and `recovery` at every startup, and the caller
`src/blackrose/chat.py::ChatController._delete_current_conversation` then raises
after the chat rows were already deleted. Not demonstrated: it needs an exception outside the caught
set (for example a `RuntimeError` from a store or recovery collaborator). Would
confirm by injecting `RuntimeError` from `AttachmentStore.delete` and observing
that later rows are untouched and `delete_current_conversation` raises.

**3. `none-found` — `src/blackrose/artifacts.py::ArtifactLifecycle.retry`, the
completed append and the acknowledgements — a failed acknowledgement
after a successful effect is self-healing.** The row is recorded with a database
error while the bytes are already gone, so `completed` and `pending` name the same
obligation for one drain; the next drain clears it because `store.delete` treats
an absent file as success (`src/blackrose/attachments/store.py::AttachmentStore.delete`).
Demonstrated by the test named in the focus-question answer. The
[interruption and retry contract](../artifact-cleanup.md#interruption-and-retry-contract)
states this.

**4. `documentation-gap` — the `artifact_cleanup.error` column
(`src/blackrose/storage/migrations.py::MIGRATIONS`, migration 4) — `Failed` is terminal but has no
operator path.** There is no attempt counter, no "abandoned" state and no command
to drop an obligation that no retry can satisfy (for example an unreferenced file
with an unrecognized name, or a damaged alias that lost its last reference). The
guide says to fix the cause and rerun
([explicit conservative repair](../artifact-cleanup.md#explicit-conservative-repair)), which
does not apply to a row whose only "cause" is a corrupt path. Demonstrated:
`tests/test_sm_12_artifact_cleanup.py::QueueTests.test_unrecognized_unreferenced_target_is_refused_and_retained`
shows a refusal that repeats identically forever, and the
[retry contract](../artifact-cleanup.md#interruption-and-retry-contract) correctly
notes there is no background retry loop — but the row is still re-attempted at
every start (`src/blackrose/runtime.py::Runtime.open`) and every delete, rewriting `error`.

**5. `documentation-gap` — `src/blackrose/artifacts.py::reconciliation`, the file
loop of the walk — pending output-recovery
journals are reported as `unrecognized`.** `src/blackrose/chat.py::ChatController._save_output`
and `src/blackrose/chat.py::ChatController.recover_output` keep
`<store>/output-recovery/<64>.json` inside the attachment store root; the walk
reports those files as an unknown layout because the report has no state for
"saved output whose metadata is waiting for recovery". Not demonstrated; would
confirm by writing such a journal and running `blackrose artifacts`. Harmless
(repair retains them) but misleading in a diagnostic report.

**6. `documentation-gap` — `src/blackrose/artifacts.py::reconciliation`, the walk —
unexpected *directories* are
not classified.** Only symlinked directories and draft ids are reported; any other
directory is traversed and, if empty, never mentioned. Demonstrated by
`tests/test_sm_12_artifact_cleanup.py::ReconciliationTests.test_buckets_are_exhaustive_disjoint_and_repair_only_touches_its_own`
(`unexpected-empty-directory` is absent from `unrecognized`). Nothing can be
deleted from it, so this is a report gap, not a risk.

**7. `fragile` — `src/blackrose/artifacts.py::reconciliation`, the reference loop
and the `unreferenced` set — protection compares spellings,
not filesystem identity.** The protected set is built from the row's spelling
resolved with `Path.resolve()`, while `unreferenced` is compared with on-disk
names from `os.walk`. On a case-insensitive or Unicode-normalizing filesystem a
hand-edited row that differs in case from the stored name would make a referenced
blob look unreferenced, and `retry` would unlink it (the same spelling is used for
`canonical_key`, so the protection check misses too). Unreachable on Linux, the
supported platform (`docs/architecture.md`), and all stored paths come from one
computed string (`src/blackrose/attachments/store.py::AttachmentStore.write`), so this needs a corrupted row plus a
case-insensitive filesystem. Not demonstrated. Confirmation would be a macOS test
with a case-mangled row; the fix (compare `os.path.normcase` + NFC keys on both
sides) was deliberately not made — see Formalization decisions.

**8. `none-found` — `src/blackrose/artifacts.py::ArtifactLifecycle.exclusive` —
`exclusive()`'s check-then-acquire is
not atomic by construction.** It is atomic in practice: `_lock` is only ever
acquired inside `exclusive()`, so no waiter can exist, and
`asyncio.Lock.acquire()` returns without suspending when uncontended — there is no
await between `locked()` and `__aenter__`.
`tests/test_sm_12_artifact_cleanup.py::QueueTests.test_exclusive_refuses_a_second_lifecycle_command`
pins the observable contract (second acquisition refused, not queued).

**9. `none-found` — `src/blackrose/storage/repository.py::Repository.orphaned_blobs` —
`orphaned_blobs` is superseded and
unused.** It has no production caller (only
`tests/test_storage.py::StorageTests.test_delete_cascades_turn_attachments_and_evidence`);
the queue plus `inventory` is the single source of truth for ownership. It is shared
code, so it is recorded here rather than removed.

## Formalization decisions

**Source change (one, small, tested).** `artifacts.py`:

- extracted `canonical_key(store, path)` from the inline canonicalisation that
  `retry()` already performed for references, used it for both sides, and moved
  the protected check ahead of the layout check in the `blob`/`partial` branch.

Why: hazard 1 is a stranded obligation reachable from the supported
conversation-delete path with a damaged row, and the fix is four lines with no
change in the trust boundary — it only ever *preserves* more. The denial side is
unchanged and pinned: an unreferenced unrecognized target is still refused
(`tests/test_sm_12_artifact_cleanup.py::QueueTests.test_unrecognized_unreferenced_target_is_refused_and_retained`),
and an escaping path is still refused
(`tests/test_artifacts.py::ArtifactTests.test_symlinked_owned_directories_and_escaped_rows_never_delete_external_files`).

**No change proposed** for hazards 2 and 7 (both wider than the demonstrated
problem: 2 needs a drain-level error policy and a decision about the caller's
post-delete behaviour in `chat.py`; 7 needs a platform-dependent path identity
rule), nor for 4-6 and 9 (documentation/shared-file changes). Follow-ups worth a
bead: an explicit "refused" acknowledgement for permanent failures (attempt
counter plus a CLI action), a report section for pending output-recovery journals,
and deleting or documenting `orphaned_blobs`.

## Verification

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_12_artifact_cleanup.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_artifacts.py
.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
.venv/bin/python scripts/check_unit_tests.py -p test_animation_recovery_store.py
```

The first suite pins this machine's guards, refusals and reconciliation buckets;
the others cover the lifecycle, the queue's storage transitions and the recovery
ledger. `scripts/check_recovery.py` (the restore drill in the health gate)
snapshots pending cleanup and verifies that cleanup resumes after restore; it
needs the built Motion Canvas runtime ([checkout setup](../../CONTRIBUTING.md#set-up-a-checkout)).
