# SM-12: Durable artifact cleanup queue and reconciliation

## Scope and ownership

| Surface | Location |
| --- | --- |
| `ArtifactLifecycle` (`exclusive`, `report`, `retry`, `repair`), `inventory`, `reconciliation`, `settle`, path predicates | `src/blackrose/artifacts.py:24-246` |
| Cleanup rows and their transitions | `src/blackrose/storage/repository.py:458-496`, schema `src/blackrose/storage/migrations.py:58-65` |
| Command entry points and the controller's last result | `src/blackrose/chat.py:500-539` |
| Startup retry | `src/blackrose/runtime.py:14-17` |
| Offline inspection/repair CLI | `src/blackrose/artifact_cli.py:27-57` |
| User-facing contract | `docs/artifact-cleanup.md` |

One `ArtifactLifecycle` exists per `ChatController` (a process-level object); the
drain is owned by the single asyncio loop that runs turns
(`docs/architecture.md:3-5`). Three different owners meet here:

- **The obligation rows** are owned by `storage/repository.py`. Only
  `delete_conversation`, `queue_cleanup` and `finish_cleanup` write them, and
  `delete_conversation` writes the blob obligations in the same transaction that
  deletes the chat rows (`repository.py:460-474`), so an obligation cannot exist
  without its effect having been requested, and rows cannot be deleted without
  their obligations.
- **The bytes** are owned by `AttachmentStore`, whose names are content
  addresses and which treats an already-absent file as a successful delete
  (`attachments/store.py:104-110`).
- **The right to act** is the in-process `_lock` plus the cross-process desktop
  lock. `exclusive()` is taken by every complete turn (`turns.py:303`), every
  lifecycle command (`chat.py:500-519`), output recovery (`chat.py:1125`),
  approved file-edit persistence (`chat.py:1144`), backup actions
  (`chat.py:1202`), diagram repair (`diagram_repairs.py:29`) and the CLI repair
  (`artifact_cli.py:48`). The CLI
  inspection path takes desktop ownership read-only in name only — it is still
  `LOCK_EX` (`instance.py:35`) — so it cannot race a live desktop either.

`ArtifactLifecycle.report()` itself does not acquire `exclusive()`
(`artifacts.py:180-183`); it is safe only because both callers already hold it or
hold desktop ownership. Nothing in the module prevents a future caller from
reporting without ownership; `reconciliation` is read-only (`artifacts.py:93`),
so that remains a report-consistency risk, not a deletion risk.

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `artifact_cleanup.kind` | `migrations.py:61` | TEXT, CHECK in `blob,partial,drafts,recovery` | what class of target the row owns | repository |
| `artifact_cleanup.target` | `migrations.py:62` | TEXT | store-relative path, or the decimal conversation id | repository |
| `artifact_cleanup.error` | `migrations.py:63` | TEXT NULL | `NULL` = never failed; text = last failure | `finish_cleanup` |
| PK `(kind,target)` | `migrations.py:64` | — | one obligation per class and target; makes queueing idempotent | repository |
| `message_attachments.storage_path` | `migrations.py:22` | TEXT | canonical blob path; also read as a raw alias | turns/approvals |
| `message_attachments.text_path` | `migrations.py:23` | TEXT NULL | extracted-text blob; second ownership column | turns/approvals |
| `conversations.id` | `migrations.py:2` | INTEGER PK AUTOINCREMENT | chat identity; never reused, so a live id always means the same chat | repository |
| store layout | `attachments/store.py:65-88`, `artifacts.py:21`, `artifacts.py:44-50` | filesystem | `aa/bb/<sha256><suffix>`, `.<name>.partial`, `illustration-drafts/<cid>/…`, `animation-recovery.sqlite3{,-wal,-shm,-journal}` | store, tool paths, SQLite |
| `ArtifactLifecycle._lock` | `artifacts.py:166` | `asyncio.Lock` | the single writer/cleanup exclusion | `exclusive()` |
| `busy` | `artifacts.py:168-170` | bool (derived) | `_lock.locked()` | — |
| `cleanup_result` | `chat.py:108` | dict of three lists | last drain outcome shown by Session | `chat.py:510,518,534` |
| `is_generating` | `chat.py:522` | bool | a turn or approved edit holds the effect budget | turns, review, backups |
| `DesktopOwnership.active` | `instance.py:16-25` | bool | the process still holds the desktop flock | `desktop_instance` |
| `AnimationRecovery.path` | `animation_recovery.py:22-24` | Path | the auxiliary ledger and its own `_lock` | recovery store |
| report dict | `artifacts.py:154-160` | dict | derived snapshot: `blobs`, `partials`, `drafts`, `recovery`, `pending`, `unrecognized` | never persisted |

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
| `Held(turn)` | locked by `TurnRunner.run` (`turns.py:303`) |
| `Held(command)` | locked by `chat.py:500-519`, `1125`, `1144`, `1202`, `diagram_repairs.py:29`, `artifact_cli.py:48` |
| `Held(command, settling)` | the command's task was cancelled while `settle` waits for its worker (`chat.py:502`, `artifacts.py:24-36`) |

**Classification states** (derived per path by `reconciliation`):

| State | Predicate |
| --- | --- |
| `ReferencedPresent` | in `storage_path`/`text_path`, `is_file()`, canonical and blob-shaped |
| `ReferencedMissing` | referenced, `not is_file()` |
| `ReferencedInvalid` | referenced but not blob-shaped or not the resolved path, or unresolvable |
| `UnreferencedBlob` | on disk, blob-shaped, canonical path not in the protected set (`artifacts.py:155`) |
| `Partial` | basename `.<blob>.partial` with a valid blob candidate (`artifacts.py:44-50`) |
| `Unrecognized` | symlink, or a file that is neither a blob, a partial, nor a root recovery file (`artifacts.py:120-132`) |
| `DraftLive` / `DraftOrphan` | numeric top-level `illustration-drafts/<cid>` whose id is / is not in `conversations` (`artifacts.py:133-142`) |
| `LedgerLive` / `LedgerOrphan` / `LedgerError` | ids in `repairs` with a live / dead conversation, or a read error (`artifacts.py:144-153`) |
| `NotClassified` | an unexpected **directory**; traversed, never listed |

## Events

- `ChatController.delete_current_conversation` → `exclusive` → `settle` → repository
  delete + immediate `retry()` (`chat.py:500-539`).
- `ChatController.retry_artifact_cleanup` (`chat.py:508-513`), called by
  `Runtime.open` at startup (`runtime.py:16`).
- `ChatController.repair_artifacts(ownership)` → `ArtifactLifecycle.repair`
  (`chat.py:515-519`, `artifacts.py:236-246`).
- `ChatController.artifact_report` (`chat.py:504-506`); `blackrose artifacts`
  / `blackrose artifacts repair` (`artifact_cli.py:27-57`).
- A complete turn starting or ending (`turns.py:303`), an approved edit
  (`chat.py:1144`), a backup restore/remove/prune (`chat.py:1202`), diagram repair.
- Per-row failures: `OSError`, `ValueError`, `sqlite3.Error`, a `False` return
  from `AttachmentStore.delete`, an unreadable ledger, a symlinked or noncanonical
  path, an escaping target (`artifacts.py:199-233`).
- Interruptions: cancellation (`asyncio.CancelledError`, not caught), process death
  between effect and acknowledgement, SQLite unavailability.
- `finish_cleanup` idempotent re-queue (`INSERT OR IGNORE`) and error overwrite.

## Transition table

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Absent` | delete chat `c` with attachment paths | chat row exists; `BEGIN IMMEDIATE` (`repository.py:461-463`) | `INSERT OR IGNORE ('blob', storage_path)` and `('blob', text_path)` `(464-470)`, then rows deleted `(474)` | `Queued` ×N |
| `Absent` | delete chat `c` | same transaction | `('drafts', str(c))`, `('recovery', str(c))` `(471-472)` | `Queued` ×2 |
| `Absent` | `repair()` | `DesktopOwnership` for this DB + `exclusive` (`artifacts.py:236-239`) | `queue_cleanup` of unreferenced blobs, partials, orphan drafts and orphan ledger ids `(241-245)` | `Queued` |
| `Queued`/`Failed` | re-queue same `(kind,target)` | PK + `INSERT OR IGNORE` (`repository.py:485`) | none (error text preserved) | unchanged |
| `Queued` | `retry()`, `blob`/`partial`, canonical target protected | snapshot `(artifacts.py:190-197)`, `canonical_key(target) in protected` `(205)` | `preserved.append` `(206)`; `finish_cleanup(kind,target)` `(231)` | `Absent` (preserved) |
| `Queued` | `retry()`, `blob`, recognized, unreferenced | `blob_name(target)` `(207)`, `owned_path` `(210)`, `store.delete` → `True` `(211)` | `completed.append` `(213)`; `finish_cleanup` `(231)` | `Absent` (completed) |
| `Queued` | `retry()`, `partial`, recognized, unreferenced | `partial_blob(target)` `(207)` | same as above | `Absent` (completed) |
| `Queued` | `retry()`, blob/partial, `store.delete` → `False` | `(211)` | `raise OSError('Attachment deletion failed; retry required.')` `(212)` | `Failed` |
| `Queued` | `retry()`, blob/partial, unrecognized layout | `(205)` false, `(207)` false | `ValueError('Unrecognized blob layout…')` → `finish_cleanup(error)` `(232-233)` | `Failed` |
| `Queued` | `retry()`, drafts/recovery, `target` not the canonical decimal of a positive int | `artifacts.py:215-217` | `ValueError` → `finish_cleanup(error)` | `Failed` |
| `Queued` | `retry()`, drafts/recovery, conversation live | `get_conversation(id) is not None` `(218)` | `finish_cleanup` `(231)` | `Absent` (preserved) |
| `Queued` | `retry()`, drafts, orphan | `(220-222)` | `delete_conversation_drafts` (rmtree when present), `completed` `(224)` | `Absent` (completed) |
| `Queued` | `retry()`, recovery, orphan | `recovery_path` non-symlink check, then `AnimationRecovery.delete` `(225-227)` | `DELETE FROM repairs` if the ledger exists, `completed` `(228)` | `Absent` (completed) |
| `Queued` | `retry()`, any row, `OSError`/`ValueError`/`sqlite3.Error` | `artifacts.py:232` | `finish_cleanup(kind,target,str(error))` `(233)` | `Failed` |
| `Failed` | `retry()` and the obstruction is gone | guards above | verified effect + `finish_cleanup` | `Absent` |
| `Failed` | `retry()` and it fails again | `artifacts.py:232` | `UPDATE artifact_cleanup SET error=?` `(repository.py:494)` | `Failed` (new text) |
| `Queued` | cancellation between effect and acknowledgement | `_worker` finishes the thread, then re-raises `CancelledError` (`storage/db.py:20-35`) | no acknowledgement | `Queued` |
| `Queued`/`Failed` | `finish_cleanup` succeeds for an already-absent file | `store.delete` is `missing_ok` (`attachments/store.py:104-110`) | `completed` | `Absent` |
| `Queued`/`Failed` | process death before acknowledgement | rows committed with the delete transaction | none | unchanged at next start |
| `Queued`/`Failed` | startup `Runtime.open` | `exclusive` via `chat.py:508-510` | `retry()` | per rows above |

Ownership transitions:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Idle` | turn starts | — | `async with exclusive()` around the whole `_run` (`turns.py:303`) | `Held(turn)` |
| `Idle` | lifecycle command | — | acquire `_lock` (`artifacts.py:173-178`) | `Held(command)` |
| `Held(*)` | another turn or command | `_lock.locked()` `(175)` | `raise ChatBusy` `(176)`; nothing queues | unchanged |
| `Held(command)` | command returns/raises | — | context-manager exit | `Idle` |
| `Held(command)` | cancel `delete_current_conversation` | operation wrapped by `settle` (`chat.py:502`) | wait for the worker, then re-raise `CancelledError` | `Held(command, settling)` → `Idle` |
| `Held(command)` | cancel `report`/`retry`/`repair` | not wrapped by `settle`; each `_worker` still settles | next await raises; rows keep their last state | `Idle` (partial drain) |
| `Held(turn)` | turn completes, fails or is stopped | `TurnRunner` settles its own persistence | unlock | `Idle` |

## Invariants

1. **Acknowledgement follows effect.** `finish_cleanup(kind, target)` with
   `error=None` is called only after the effect was verified: `store.delete`
   returned true, the conversation was absent, the draft tree was removed, or the
   ledger rows were deleted (`artifacts.py:205-231`).
2. **Protection is decided on the canonical path.** A `blob`/`partial` target
   whose resolved store-relative path is any `message_attachments.storage_path` or
   `text_path` is never unlinked (`artifacts.py:193-206`). Both columns are read in
   one snapshot (`artifacts.py:77-89`).
3. **Deletion is confined.** No unlink happens through a symlink, a noncanonical
   path, or a path outside the store root: `owned_path` runs before
   `store.delete` (`artifacts.py:210`, `artifacts.py:53-59`, `attachments/store.py:53-63`).
4. **Unrecognized layouts are never deleted.** Neither `reconciliation` nor
   `repair` queues `unrecognized` entries; `retry` refuses a target that is not
   blob- or partial-shaped (`artifacts.py:207-208`, `241-245`).
5. **Conversation identities are canonical.** `drafts`/`recovery` targets must
   satisfy `str(int(target)) == target` and `int(target) > 0`
   (`artifacts.py:215-217`); `'007'`, `'0'`, `'-1'`, `'7.0'` are refused.
6. **A live chat protects its auxiliary state.** Drafts and ledger rows are
   preserved while `get_conversation(id)` returns a row (`artifacts.py:218-219`);
   `AUTOINCREMENT` ids (`migrations.py:2`) mean a live id cannot belong to a
   different chat.
7. **Nothing is lost when an acknowledgement fails.** Every row that survives a
   drain has non-null `error`; every repeatable effect is idempotent
   (`store.delete` missing-ok, `rmtree` only when the root exists,
   `DELETE FROM repairs` only when the ledger exists). `completed` and `pending`
   may name the same obligation in that case, which is the honest report.
8. **Obligations outlive their rows and are drained exactly once per event.**
   `artifact_cleanup` has no foreign key (`migrations.py:59-65`); `retry` runs
   only under `exclusive` and returns `pending` from a fresh read
   (`artifacts.py:188-189`, `234`).
9. **One holder.** `exclusive()` admits at most one turn or command and refuses
   the next immediately with `ChatBusy` rather than queueing it
   (`artifacts.py:173-178`).
10. **Reconciliation is read-only.** `report()` performs no write to the store or
    the ledger: the walk is `followlinks=False` and the ledger is opened through
    `readonly_snapshot` (`artifacts.py:110`, `149`).
11. **File classification is total and disjoint** outside the pruned
    `illustration-drafts` subtree and the root recovery sidecars: each regular
    file is a referenced blob, an unreferenced blob, a partial, or unrecognized
    (`artifacts.py:120-132`).
12. **Repair queues only what it classified.** The job list is exactly
    unreferenced blobs, partials, orphan draft ids and orphan ledger ids
    (`artifacts.py:241-245`); nothing else is ever created by repair.

## Hazards and open questions

Answers to the bead's focus questions, then the numbered findings.

- *Is the row lifecycle total?* Yes in the sense that a row is only removed by a
  verified effect and never disappears silently; `Queued` and `Failed` are the
  only persistent states, and every write to them is a row-level decision. It is
  not total in the sense of *terminating*: `Failed` has no terminal "abandoned"
  state and no supported way to clear it (hazard 4).
- *Can a target be deleted while referenced, or preserved while unreferenced,
  under a concurrent `TurnRunner` insert?* No. The turn holds `exclusive` for its
  entire `_run` (`turns.py:303`), blob bytes and attachment rows are written only
  inside that scope (`turns.py:478-483`, `chat.py:583-597`), and `attach()`
  writes nothing to disk (`chat.py:578-581`; `tests/test_gui_images.py:80`
  asserts the store does not exist after `attach`). The `protected` snapshot and the unlink therefore cannot
  interleave with an attachment insert. Preservation can only be *stale in the
  conservative direction*: a job is preserved if any row still references the
  bytes at snapshot time, and it is re-queued by the next
  `delete_conversation` (`repository.py:464-470`).
- *Is `reconciliation` exhaustive?* Over files, yes (I11). Over directories, no:
  only files and symlinks are reported, so an unexpected empty directory is
  invisible (hazard 6). It never acts on an unrecognized layout: it reports it,
  and a symlinked `illustration-drafts` or ledger sidecar is refused rather than
  followed (`artifacts.py:112-119`, `70-75`, `144-153`).
- *Does `exclusive()` really exclude turn work?* Yes — `turns.py:303` holds it
  across the whole turn, and every other application writer of
  `message_attachments` (`_save_output` at `chat.py:1094`, reached from
  `recover_output` and `review_edit`) holds it too. Chat *creation* is not covered
  (`turns.py:297` runs before the lock), but it inserts no attachment rows, so it
  cannot create a reference the drain missed.
- *What happens if `finish_cleanup` fails after the file was deleted?* The row
  survives; the drain then tries to record the failure and either stores a
  database error on a completed effect or aborts (hazards 2 and 3). The next drain treats
  the absent file as success and clears the row (demonstrated by
  `test_failed_acknowledgement_after_unlink_is_reported_and_retried`).

**1. `fragile` (fixed) — `artifacts.py:201-208` — a still-referenced noncanonical
target became a permanently failed obligation.** `retry()` validated the layout
before consulting the protected set, so a `delete_conversation` obligation
carrying a damaged alias (which `repository.py:464-470` inserts verbatim) became
`Failed` with `"Unrecognized blob layout"` even though a surviving attachment row
still resolved to the same bytes. No retry can clear it and `repair` never queues
it, so the drain logs a pending obligation at every start (`chat.py:511-512`),
Session shows it after the next delete (`session.py:681-683`), and
`blackrose artifacts repair` exits 1 forever. Demonstrated by the regression test
`test_referenced_noncanonical_target_is_preserved_not_failed` (fails before the
change, passes after). Fixed by deciding protection on the canonical path and
before the layout check; safety is unchanged because bytes whose canonical path is
referenced were never deletable anyway.

**2. `fragile` — `artifacts.py:232-233` — an unmodelled exception aborts the whole
drain.** The `except` covers `OSError`, `ValueError` and `sqlite3.Error` only, and
a failure of the *failure* acknowledgement propagates out of the loop. Every
remaining row keeps its last state (no loss), but a persistent fault in the
`blob` class (ordered first, `repository.py:480`) defers `drafts` and `recovery` at
every startup, and the caller in `chat.py:534` then raises after the chat rows
were already deleted. Not demonstrated: it needs an exception outside the caught
set (for example a `RuntimeError` from a store or recovery collaborator). Would
confirm by injecting `RuntimeError` from `AttachmentStore.delete` and observing
that later rows are untouched and `delete_current_conversation` raises.

**3. `none-found` — `artifacts.py:213` and `231-233` — a failed acknowledgement
after a successful effect is self-healing.** The row is recorded with a database
error while the bytes are already gone, so `completed` and `pending` name the same
obligation for one drain; the next drain clears it because `store.delete` treats
an absent file as success (`attachments/store.py:107`). Demonstrated by the test
named in the focus-question answer. `docs/artifact-cleanup.md:86` states this
contract.

**4. `documentation-gap` — `migrations.py:63` — `Failed` is terminal but has no
operator path.** There is no attempt counter, no "abandoned" state and no command
to drop an obligation that no retry can satisfy (for example an unreferenced file
with an unrecognized name, or a damaged alias that lost its last reference). The
guide says to fix the cause and rerun (`docs/artifact-cleanup.md:73-76`), which
does not apply to a row whose only "cause" is a corrupt path. Demonstrated:
`test_unrecognized_unreferenced_target_is_refused_and_retained` shows a refusal
that repeats identically forever, and `docs/artifact-cleanup.md:90` correctly
notes there is no background retry loop — but the row is still re-attempted at
every start (`runtime.py:16`) and every delete, rewriting `error`.

**5. `documentation-gap` — `artifacts.py:120-132` — pending output-recovery
journals are reported as `unrecognized`.** `chat.py:1064` and `1126` keep
`<store>/output-recovery/<64>.json` inside the attachment store root; the walk
reports those files as an unknown layout because the report has no state for
"saved output whose metadata is waiting for recovery". Not demonstrated; would
confirm by writing such a journal and running `blackrose artifacts`. Harmless
(repair retains them) but misleading in a diagnostic report.

**6. `documentation-gap` — `artifacts.py:110-132` — unexpected *directories* are
not classified.** Only symlinked directories and draft ids are reported; any other
directory is traversed and, if empty, never mentioned. Demonstrated by
`test_buckets_are_exhaustive_disjoint_and_repair_only_touches_its_own`
(`unexpected-empty-directory` is absent from `unrecognized`). Nothing can be
deleted from it, so this is a report gap, not a risk.

**7. `fragile` — `artifacts.py:96-105` and `155` — protection compares spellings,
not filesystem identity.** The protected set is built from the row's spelling
resolved with `Path.resolve()`, while `unreferenced` is compared with on-disk
names from `os.walk`. On a case-insensitive or Unicode-normalizing filesystem a
hand-edited row that differs in case from the stored name would make a referenced
blob look unreferenced, and `retry` would unlink it (the same spelling is used for
`canonical_key`, so the protection check misses too). Unreachable on Linux, the
supported platform (`docs/architecture.md`), and all stored paths come from one
computed string (`attachments/store.py:75`), so this needs a corrupted row plus a
case-insensitive filesystem. Not demonstrated. Confirmation would be a macOS test
with a case-mangled row; the fix (compare `os.path.normcase` + NFC keys on both
sides) was deliberately not made — see Formalization decisions.

**8. `none-found` — `artifacts.py:173-178` — `exclusive()`'s check-then-acquire is
not atomic by construction.** It is atomic in practice: `_lock` is only ever
acquired inside `exclusive()`, so no waiter can exist, and
`asyncio.Lock.acquire()` returns without suspending when uncontended — there is no
await between `locked()` and `__aenter__`. `test_exclusive_refuses_a_second_lifecycle_command`
pins the observable contract (second acquisition refused, not queued).

**9. `none-found` — `repository.py:697-724` — `orphaned_blobs` is superseded and
unused.** It has no production caller (only `tests/test_storage.py:140`); the
queue plus `inventory` is the single source of truth for ownership. It is shared
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
(`test_unrecognized_unreferenced_target_is_refused_and_retained`), and an escaping
path is still refused (`tests/test_artifacts.py:296-308`).

**No change proposed** for hazards 2 and 7 (both wider than the demonstrated
problem: 2 needs a drain-level error policy and a decision about the caller's
post-delete behaviour in `chat.py`; 7 needs a platform-dependent path identity
rule), nor for 4-6 and 9 (documentation/shared-file changes). Follow-ups worth a
bead: an explicit "refused" acknowledgement for permanent failures (attempt
counter plus a CLI action), a report section for pending output-recovery journals,
and deleting or documenting `orphaned_blobs`.

## Verification

```
.venv/bin/python scripts/check_unit_tests.py -p test_sm_12_artifact_cleanup.py -v
    Ran 12 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_artifacts.py
    Ran 24 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
    Ran 17 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_animation_recovery_store.py
    Ran 3 tests … OK
.venv/bin/python -m ruff check src tests scripts
    All checks passed!
.venv/bin/python -m mypy
    Success: no issues found in 19 source files
.venv/bin/python scripts/check_unit_tests.py
    Ran 1261 tests … FAILED (failures=4, errors=53, skipped=23)
```

The whole-suite failures are confined to the interactive-UI/ui-runtime and
Motion Canvas areas of this checkout and are unrelated to this change:
`src/blackrose/gui/static/ui-contract.json` is a generated build output that is
absent here (`FileNotFoundError`, 32 errors in `test_ui_documents.py` alone;
`test_saved_compatibility` fails with "Interactive UI runtime unavailable; rebuild
ui-runtime"; `test_motion_plan` needs the Node toolchain). No test touching
artifacts, storage or recovery fails.

`scripts/check_recovery.py` (the restore drill, part of the health gate) cannot
run here for the same pre-existing reason: it stops at
`ValueError: Motion Canvas needs setup: run blackrose setup-animations (requires
Node.js 22+ and npm)`. That drill snapshots pending cleanup and verifies that
cleanup resumes after restore; it should be run where the animation toolchain is
installed. The new code path only changes which obligations are *preserved*, and
the preserved list is not part of the drill's assertions beyond "cleanup resumes",
so the residual risk is low but unverified here.
