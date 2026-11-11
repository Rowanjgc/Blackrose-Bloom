# Conversation and artifact cleanup

Deleting a conversation removes its chat rows, private illustration drafts and
animation recovery rows through `ChatController`. Shared originals and extracted
text survive until their last reference disappears. Workspace files, Downloads
exports and knowledge-library captures have separate lifetimes; conversation
deletion does not remove them.

Chat schema **4** adds `artifact_cleanup`. The repository records blob paths and
conversation-scoped draft/recovery obligations in the same transaction that
deletes the chat rows. These records deliberately have no conversation foreign
key. Filesystem or auxiliary-database failures leave an obligation and its latest
error. The controller returns them in `cleanup_result['pending']`; Session shows
a notice when deletion leaves cleanup unfinished. Runtime startup retries recorded
obligations before opening a session. It does not scan for or silently remove
unrecorded orphans.

## Inspect before repairing

Use the same XDG environment as the desktop. Close Bloom first, and take the
[complete stopped-application backup](recovery.md). From a source checkout use
`.venv/bin/python -m blackrose.main` in place of `blackrose` below.

```sh
blackrose artifacts
```

This prints a JSON report and acquires the existing desktop lock read-only. It
does not construct inference/knowledge services, migrate the database, retry jobs,
or change attachment/database files. A clean database is read as immutable; if a
WAL or rollback journal remains, SQLite reads a private temporary copy so even
source sidecar files stay unchanged. The report includes:

- `blobs`: original and extracted-text references, unreferenced blobs, missing
  bytes and noncanonical references.
- `partials`: interrupted store writes with the exact hidden temporary naming
  convention.
- `drafts`: conversation directories and those without a live conversation.
- `recovery`: conversation IDs in the auxiliary ledger, orphaned IDs and any
  read error. The ledger and its sidecars are distinct from blobs.
- `pending`: durable cleanup obligations and the latest failure for each.
- `unrecognized`: files, unexpected layouts and symlinks requiring inspection.

A missing chat database is refused without creating one. A live desktop is also
refused. For old or copied data without `desktop.lock`, explicitly create an empty
file at that path with all owners stopped, then retain it. Never unlink or replace
a desktop lock file. The report is local diagnostic data; errors can contain
absolute paths.

## Explicit conservative repair

After reviewing the report and preserving a backup:

```sh
blackrose artifacts repair
```

Repair reacquires desktop ownership and computes a fresh inventory. It records
candidates before removing them, then prints `before`, `cleanup` and `after`.
Both attachment reference columns are checked again under writer exclusion.
Referenced bytes, including canonical targets of malformed reference aliases,
are preserved. The controller excludes cleanup from complete turns and approved
file-edit persistence; cancelled file workers settle before ownership is released.
Recovery-state reads/writes also serialize with its inventory scan.

Candidates are limited to the store's two-level SHA-256 naming convention
(including extensionless and Unicode-suffix artifacts), its exact hidden partial
files, orphaned numeric illustration directories and orphaned recovery rows.
Repair retains the shared recovery database itself. It refuses symlinked paths
and paths outside app-owned storage. Unknown files are reported and retained.

`repair` exits nonzero if cleanup remains pending or the recovery ledger cannot
be read. Correct a concrete filesystem/SQLite availability problem and rerun the
command to retry. A successful report command means the report was produced;
inspect its missing/invalid/error fields. Repair does not reconstruct missing
referenced bytes or reset a corrupt ledger, because that would lose saved content
or repair-attempt limits. Use the recovery runbook for those cases.

## Interruption and retry contract

| Interruption | Durable result and next action |
| --- | --- |
| Attachment write succeeds but turn linkage fails, or a batch write stops partway | Unreferenced bytes remain; the read-only report identifies them for explicit repair. |
| Conversation-delete transaction fails | Rows and cleanup obligations roll back together; retry the conversation command. |
| Process exits after deleting rows but before deleting files/recovery rows | Obligations survive; startup or explicit repair retries them. |
| File is removed but completion is not acknowledged | Retry treats an already absent file as success. |
| A blob still belongs to another message, including extracted text | Bytes are retained and this obligation is acknowledged. Deleting the final owner records a new obligation. |
| File, draft or recovery deletion fails | Other obligations continue; the failed obligation and error remain for retry. |

There is no additional execution loop, timer or unbounded background retry.
Controller embeddings and scratch runtimes must honor the same desktop ownership
contract as the GUI. Direct repository/store mutation is a persistence primitive,
not a concurrent application writer API.

Verification is offline in [artifact lifecycle tests](../tests/test_artifacts.py)
and [recovery persistence tests](../tests/test_animation_recovery_store.py).
The [restore drill](../scripts/check_recovery.py) also snapshots pending cleanup,
restores it twice and verifies cleanup resumes without changing the backup or
retained conversations. The ownership decision is recorded in
[ADR 0002](adr/0002-artifact-cleanup.md).
