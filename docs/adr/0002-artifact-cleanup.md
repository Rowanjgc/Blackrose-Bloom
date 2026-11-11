# Durable conversation artifact cleanup

Status: accepted, 2026-10-17.

Conversation rows and attachment files have different commit boundaries.
Deleting rows first used to lose the list of files on interruption; animation
recovery cleanup lived in Session and was omitted by controller-only deletion.
File deletion failures were ignored.

The controller now owns this lifecycle through `ArtifactLifecycle`. Chat schema
4 adds a small cleanup journal: the repository queues original/extracted-text
paths plus private draft and recovery obligations atomically with row deletion.
The journal outlives the conversation. The existing application loop drains it
at deletion and runtime startup. Successful or already-absent work is acknowledged;
failures remain with errors and are visible to the caller and Session.

One controller lock excludes complete turns and approved artifact writes from
cleanup. Thread workers settle before cancellation releases that lock. Explicit
orphan repair also requires active ownership of this database's `desktop.lock`,
extending [ADR 0001](0001-desktop-ownership.md). Read-only offline inspection uses
the existing lock without creating files and never migrates source storage.

Reconciliation checks both blob reference columns. Illustration directories and
the auxiliary recovery ledger retain distinct ownership; neither is interpreted
as a blob tree. Unknown layouts and symlinks are preserved. Workspace/Downloads
outputs and knowledge-library captures keep their independent lifetimes.

This adds no execution loop or scheduled garbage collector. Unrecorded orphans
from failed linkage require an explicit repair after a backup and report. Older
applications refuse schema 4; downgrade by restoring a complete earlier snapshot,
not by editing schema metadata. See the [operator guide](../artifact-cleanup.md)
and [recovery runbook](../recovery.md).

Revisit if multiple cooperating chat writers or concurrent desktop windows become
a supported requirement. They would need shared writer exclusion covering file
publication, database linkage and deletion together.
