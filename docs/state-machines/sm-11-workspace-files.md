# SM-11: Workspace selection, review tokens, output recovery journal and edit backups

## Scope and ownership

Four independent machines in one user workflow: authorizing a directory for a
chat, reviewing model-proposed file and library changes, recording an output
whose attachment metadata failed, and settling the displaced original of an
approved replacement.

- `src/blackrose/chat.py`: `ChatController._workspace_selection` and
  `_navigation_epoch` (set in
  `src/blackrose/chat.py::ChatController.__init__`),
  `begin_workspace_selection`, `_check_workspace_selection`, `set_workspace`,
  the `file_tool` proposals, `_save_output`, `recover_output`,
  `review_edit`/`_review_edit`, `edit_backup_action`, `_restore_edit_backup`,
  `propose_library_add`, `review_library_add`, `recover_interrupted`, and the
  shutdown/deletion purges (`shutdown`, `_delete_current_conversation`).
- `src/blackrose/edit_backups.py`:
  `src/blackrose/edit_backups.py::EditBackups` — durable records, crash
  settlement, guarded restore, explicit removal and age cleanup.
- `src/blackrose/workspace.py`:
  `src/blackrose/workspace.py::WorkspaceFiles.write`,
  `src/blackrose/workspace.py::WorkspaceFiles._store_displaced`, and the
  module functions `store_entry`, `keep_beside`, `recover_replacement_stage`,
  `find_stored_backup`, `remove_stored_backup`, `_cleanup_abandoned_stages`,
  `replacement_stage` and `backup_filename`.
- `src/blackrose/library.py`: `src/blackrose/library.py::LibraryProposals`,
  `src/blackrose/library.py::import_capture`.
- `src/blackrose/storage/repository.py`: the `Repository` methods
  `begin_edit_backup`, `settle_edit_backup`, `edit_backups`, `edit_backup`,
  `orphaned_edit_backups`, `mark_edit_backup_restored` and
  `delete_edit_backup`. Schema is migration 7 in
  `src/blackrose/storage/migrations.py::MIGRATIONS`.
- Coupled, not owned here: `src/blackrose/gui/bridge.py::Api.pick_workspace`
  runs the native picker between `begin_workspace_selection` and
  `set_workspace`, and `src/blackrose/gui/bridge.py::Api.review_edit`
  serializes review calls on the GUI thread;
  `src/blackrose/artifacts.py::ArtifactLifecycle.exclusive` supplies the
  writer-exclusion lock; `turns.py` runs tool calls (`file_tool`) inside a
  reply; `attachments/` owns blobs.

Ownership per [architecture.md](../architecture.md):

- **One application asyncio loop, one desktop.** `ChatController` owns every
  variable here; the page only renders cards and sends review decisions (the
  `chat.py` row of [modules and invariants](../architecture.md#modules-and-invariants),
  the library paragraph of
  [interactive answers, library review and recovery](../architecture.md#interactive-answers-library-review-and-recovery),
  and [conversation workspaces](../architecture.md#conversation-workspaces)).
  Two desktops cannot share the database
  ([ADR 0001](../adr/0001-desktop-ownership.md)), which is why no machine in
  this document has a cross-process lock.
- **Workspace grants are one per conversation, persisted.**
  `WorkspaceFiles` re-checks the stored device/inode on every operation
  (`src/blackrose/workspace.py::WorkspaceFiles._root`), refuses the backup
  store as a workspace path (`src/blackrose/workspace.py::WorkspaceFiles.parent`)
  and lets only the output folder create unreviewed files (the `automatic`
  check in `_root`). `files.readable` is the set of this conversation's output
  paths (`src/blackrose/chat.py::ChatController.workspace_files`).
- **Review tokens are session-only.** `pending_edits` and
  `LibraryProposals.pending` are plain dictionaries; `workspaces.md` states the
  edit proposals "expire on application restart or workspace change" and the
  library proposals are "not durable across process restart"
  ([architecture](../architecture.md#interactive-answers-library-review-and-recovery)).
- **Edit backups are durable and outlive their conversation.** Migration 7 has
  no conversation foreign key on purpose (its leading SQL comment says so);
  only an explicit removal deletes a row, and age cleanup never removes
  conflict content (`src/blackrose/edit_backups.py::EditBackups.prune`).
- **Output recovery is a journal outside the database.** It sits in the
  attachment store root (`output-recovery/`, built in
  `src/blackrose/chat.py::ChatController._save_output`) and is described in
  [troubleshooting.md](../troubleshooting.md#output-saved-but-conversation-artifact-pending).

## State variables

Unqualified methods are on `ChatController` in `src/blackrose/chat.py` unless
the row names another owner.

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `_workspace_selection` | `ChatController.__init__` | `tuple[object, Conversation, int] \| None` | Last picker token, its owning conversation and the navigation epoch it saw | `begin_workspace_selection` only |
| `_navigation_epoch` | `ChatController.__init__` | `int` | Bumped when the selection moves to another chat; refreshing the same chat's metadata is not navigation | `conversation` setter only |
| `_workspace_lock` | `ChatController.__init__` | `asyncio.Lock` | Serializes `set_workspace` bodies | `set_workspace` |
| `is_generating` | `ChatController.__init__` | `bool` | Single-writer latch for replies, approvals and restores | `_review_edit`, `review_library_add`, `_restore_edit_backup` (set, then cleared in `finally`), `turns.py` |
| `pending_edits` | `ChatController.__init__` | `dict[str, dict]`, ≤ 8 | File proposals bound to conversation, workspace, reply, path, bytes, digest | `file_tool` (edit and create proposals); removed by `_review_edit` (reject, `file_changed_published`, success) and `_delete_current_conversation`; filtered by `set_workspace` |
| `directory_grants` | `ChatController.__init__` | `dict[int, dict]` | Extra in-memory read grants per conversation | `approve_directories`; popped by `set_workspace` and, for the deleted conversation, by `ChatController._delete_current_conversation` |
| `library_proposals` | `ChatController.__init__` | `LibraryProposals` | In-memory capture proposals | `__init__` only; mutated in place (`LibraryProposals.propose`, `review_library_add` reject and commit, `LibraryProposals.discard_conversation`, `shutdown`) |
| `LibraryProposals.pending` | `src/blackrose/library.py::LibraryProposals.__init__` | `dict[str, Proposal]`, ≤ 8 | Token → frozen capture/title/tags | `propose` (new token, or the existing one on a dedup hit); read by `get`; pops above |
| `Proposal` | `src/blackrose/library.py::Proposal` | frozen dataclass | The reviewed snapshot; `review()` renders a card | constructed in `propose` |
| `_reference_index` | `ChatController.__init__` | cache \| `None` | Reference suggestions invalidated by writes and grants | `set_workspace`, `_save_output`, `_restore_edit_backup` |
| `output-recovery/<key>.json` | `_save_output` | JSON `{result, assistant_id, workspace, conversation_id, requested_path, options, digest}` | The bridge from a written file to its attachment row | `write_json` in `_save_output`; unlinked there after the attachment row |
| `key` | `_save_output` | 64 hex chars | `sha256(json([conversation_id, path, sha256(data), options]))` | computed per save |
| `edit_backups.state` | `MIGRATIONS` (migration 7) | `'pending' \| 'saved'` (CHECK) | Record lifecycle | `Repository.begin_edit_backup`, `Repository.settle_edit_backup` |
| `edit_backups.stored` | `MIGRATIONS` (migration 7) | `0 \| 1` | Location is inside the private store (1) or a project path (0) | `settle` |
| `edit_backups.conflict` | `MIGRATIONS` (migration 7) | `0 \| 1` | Displaced bytes differ from the reviewed digest | `settle` (`EditBackups._recover`, `WorkspaceFiles._store_displaced`) |
| `edit_backups.location`/`digest`/`size`/`mode` | `MIGRATIONS` (migration 7) | text/int | Where the original is, and its facts | `settle` |
| `edit_backups.approved_digest`/`published_digest` | `MIGRATIONS` (migration 7) | text | Digest the user reviewed, and the bytes the replacement published | `begin` only |
| `edit_backups.restored_at` | `MIGRATIONS` (migration 7) | text \| `NULL` | This backup was put back; informational only | `mark_edit_backup_restored` |
| `.blackrose-stage-<id>-edit` | `src/blackrose/workspace.py::replacement_stage` | directory entry | Same-folder carrier of the replacement, then of the displaced original | created by `WorkspaceFiles.write`; removed/renamed by its `except BaseException` branch (unlink, or `keep_beside` once published), `_store_displaced`, `recover_replacement_stage` (unlink or `store_entry`) |
| `<store>/<group>/<id>-<label>` | `src/blackrose/workspace.py::backup_filename` | file in `data_dir()/edit-backups/<conversation>` | The retained original | `store_entry` (rename, or `_copy_entry` across filesystems) |
| `<name>.backup`, `<name> (n).backup` | `src/blackrose/workspace.py::keep_beside` | visible project file | Fallback when the store cannot take the entry | `keep_beside` |
| `published` | `WorkspaceFiles.write` | local `bool` | Set immediately **before** the `_exchange` call, cleared if it fails | that block only |

## States

**S1 — workspace selection** (evaluated by `_check_workspace_selection`, one
token at a time):

| State | Predicate |
| --- | --- |
| `UNSELECTED` | `_workspace_selection is None` |
| `CURRENT` | token is `_workspace_selection`, `token[2] == _navigation_epoch`, a conversation is selected with its id equal to `token[1].id`, `not is_generating` |
| `SUPERSEDED` | a newer `begin_workspace_selection` replaced the token |
| `DETACHED` | no conversation is selected, or its id is not `token[1].id` (opened another chat, or deleted) |
| `NAVIGATED` | `token[2] != _navigation_epoch`: another chat was selected after the picker opened, even if navigation returned to `token[1]` |
| `BUSY` | a reply or approval started after the picker |

Every state but `CURRENT` raises `ChatBusy('Workspace selection expired; choose
the directory again')` (`src/blackrose/chat.py::ChatController._check_workspace_selection`).
There is no `CONSUMED` state: the field is never cleared, so a successful
selection is `CURRENT` again. A title rename replaces the selected
`Conversation` object for the same id
(`src/blackrose/chat.py::ChatController._name_conversation` assigns
`selected_conversation` directly) without touching the epoch, so it leaves the
picker `CURRENT`; that refresh is why the comparison is by id rather than by
object (bra-lfou).

**S2 — pending edit proposal** (per token):

| State | Predicate |
| --- | --- |
| `ABSENT` | token not in `pending_edits` |
| `EDIT_REVIEW` | token present, `operation != 'create'`, `old`+`digest` recorded |
| `CREATE_REVIEW` | token present, `operation == 'create'`, `text` recorded, `digest is None` |
| `CONSUMED` | popped after a publication (with or without `artifact_pending`) |
| `REJECTED` | popped by `reject=True` |
| `EXPIRED` | dropped by `set_workspace` or conversation deletion, or a `file_changed_published` raise |

Review (`approve=False, reject=False`) is stateless: it renders `pending['old']`
against `pending['data']`/`pending['text']` (the `not approve` branch of
`_review_edit`) and changes nothing.

**S3 — library proposal** (per token):

| State | Predicate |
| --- | --- |
| `ABSENT` | token not in `pending` |
| `PENDING` | token present, no commit running |
| `LATCHED` | token present and `is_generating` is `True` — this token's commit, or any reply/approval, because the latch is controller-wide |
| `CONSUMED` | popped after `import_capture` returned |
| `REJECTED` | popped by `reject=True` |
| `EXPIRED` | `discard_conversation`, `shutdown`, process exit, or a foreign conversation |

A commit failure returns the token to `PENDING` (the `knowledge.call('capture')`
inside `review_library_add`'s `commit` runs before the pop), so the machine is
retryable; a success is terminal because the token is gone.

**S4 — output recovery journal** (per key):

| State | Predicate |
| --- | --- |
| `UNWRITTEN` | no journal file; the disk write for this identity has not succeeded yet |
| `WRITTEN` | journal exists; the output file exists with `record['digest']` |
| `ATTACHED` | journal removed after `append_attachments` found or created the row |
| `UNRECORDABLE` | the journal write itself failed; the result carries `recovery_id: None` (the `except Exception` branch of `_save_output`) |
| `ORPHAN_FILE` | no journal file and the output exists — the gap between `files.write` and the journal `write_json` in `_save_output` made permanent by process death; no code can observe it |

**S5 — edit backup** (`edit_backups` row plus its disk counterpart). Ten states:

| State | Row | Disk |
| --- | --- | --- |
| `E0 ABSENT` | none | nothing named for this id |
| `E1 PENDING_UNSTAGED` | `pending` | no stage, no store file; target still holds the original |
| `E2 PENDING_STAGED` | `pending` | stage digest == `published_digest` (never published) |
| `E3 PENDING_EXCHANGED` | `pending` | stage digest != `published_digest`, no store file |
| `E4 PENDING_COPIED` | `pending` | stage present **and** store file with the same bytes (copy published, source left) |
| `E5 PENDING_STORED` | `pending` | no stage, store file present |
| `E6 SAVED_STORED` | `saved`, `stored=1` | store file matches `location`/`digest` |
| `E7 SAVED_BESIDE` | `saved`, `stored=0` | project `.backup` or a retained `-edit` stage at `location` |
| `E8 SAVED_MISSING` | `saved` | `location` unreachable or its bytes changed |
| `E9 REMOVED` | none | store file deleted (stored=1) or project copy retained (stored=0) |

`restored_at` is a marker on E6/E7, not a separate state; it changes neither
`state` nor `location`, so a restored backup remains listable and re-checkable.

**S6 — replacement stage** (the disk component of S5, per `(directory, id)`):
`STAGE_ABSENT`, `STAGE_STAGED` (replacement bytes), `STAGE_EXCHANGED`
(displaced bytes), `STAGE_GONE` (moved into the store), `STAGE_BESIDE`
(renamed `.backup`), `STAGE_LEFT` (a copy was published but the source entry
remains). S5's predicates are written in terms of S6.

## Events

1. The native picker: `begin_workspace_selection` then
   `set_workspace(path, selection=...)` (`Api.pick_workspace`); clearing the
   workspace calls `set_workspace(None)` without a selection
   (`src/blackrose/gui/bridge.py::Api.clear_workspace`), as do the
   launch-directory paths (`ensure_conversation`, `new_conversation`,
   `configure_launch_workspace`).
2. Navigation: `open_conversation`, `new_conversation`,
   `delete_current_conversation`; each passes through the `conversation` setter
   (`src/blackrose/chat.py::ChatController.conversation`), which bumps
   `_navigation_epoch` for a different id.
3. A reply starting or ending (`is_generating`, `turns.py`), including the
   `file_tool` calls that create proposals.
4. `review_edit(token, approve|reject|neither)` from the page
   (`Api.review_edit`); `edit_backup_action('restore'|'remove'|'prune'|…)`.
5. `propose_library_add` (a `knowledge_add` tool call), then
   `review_library_add(token, approve|reject|neither)`.
6. `WorkspaceFiles.write` in either mode, including the exchange syscall, the
   store move, the `keep_beside` fallback and every exception on those paths.
7. Process death at any instruction boundary, and the startup
   `recover_interrupted` (`src/blackrose/chat.py::ChatController.recover_interrupted`
   → `EditBackups.recover`).
8. Metadata failure inside `_save_output` (blob write, `append_attachments`,
   journal unlink), a cancelled save (the shielded task at the end of
   `_save_output`), and a later `_save_output`/`recover_output` for the same
   journal key.
9. Conversation deletion
   (`src/blackrose/chat.py::ChatController._delete_current_conversation`) and
   `shutdown` (`src/blackrose/chat.py::ChatController.shutdown`).

## Transition table

### T1 — workspace selection token

Guards are in `begin_workspace_selection` and `set_workspace`; "check 1/2/3"
are the three `_check_workspace_selection` calls in `set_workspace`, in order.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | picker opened | `not is_generating` | new token stored with the ensured conversation and the current epoch | `CURRENT` (previous token `SUPERSEDED`) |
| `CURRENT` | automatic title rename of the same chat | id and epoch unchanged | the selected object is replaced (`_name_conversation`); nothing expires | `CURRENT` |
| `CURRENT` | `set_workspace` entered | `_workspace_lock` acquired | check 1 | `CURRENT` |
| `CURRENT` | `authorize_directory` returned | check 2 | nothing persisted yet; `grant` computed off-loop | `CURRENT` |
| `CURRENT` | `_repo.set_workspace` returned | — | grant durable for the captured conversation id | `CURRENT` |
| `CURRENT` | `get_conversation` returned | check 3 | pending edits dropped, extra grants popped, selection updated, cache cleared | `CURRENT` (consumed) |
| `CURRENT` | navigation/generation during `authorize_directory` | check 2 | `ChatBusy`; nothing persisted | `NAVIGATED`/`DETACHED`/`BUSY` |
| `CURRENT` | navigation/generation during the database write or read | check 3 | `ChatBusy`; the grant **is** already persisted (H3) | `NAVIGATED`/`DETACHED`/`BUSY` |
| `CURRENT` | captured conversation row is `None` | `conversation is None` after check 3 | `ChatBusy` | `DETACHED` |
| `CURRENT` | `set_workspace(None)` | `authorize_directory` skipped (`path` is falsy) | `grant=None` persisted, edits dropped | `CURRENT` (no grant) |

### T2 — pending edit proposal

Proposals are created in `ChatController.file_tool`; review guards are in
`ChatController.review_edit` and `ChatController._review_edit`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | `file_edit` tool call | `len(pending_edits) < 8`; text/binary checks | old text+digest captured; token stored | `EDIT_REVIEW` |
| `ABSENT` | `document_create` outside the output folder | `_save_output` raised `review_required`; `< 8`; destination resolved | token stored | `CREATE_REVIEW` |
| `ABSENT` | `file_edit` with 8 already pending | `len(pending_edits) >= 8` | `ValueError('Review pending edits before requesting more')` | `ABSENT` |
| `EDIT_REVIEW`/`CREATE_REVIEW` | `review_edit(token)` (no flags) | token present, same conversation, same workspace | comparison rendered; no mutation | unchanged |
| `EDIT_REVIEW` | `approve=True` | `not is_generating`; `exclusive()` in `review_edit` | `is_generating=True`; `metadata` re-check; `EditBackups.replace` | `CONSUMED` (token popped after the write) |
| `CREATE_REVIEW` | `approve=True` | same guards | `_save_output(reviewed=True, collision='error')` (refuses an appeared destination) | `CONSUMED` |
| either | approval raises `file_changed_published` | `error.code` check in `_review_edit` | token popped, then re-raised, so the card cannot be retried | `EXPIRED` |
| either | approval raises `file_changed`/`already_exists`/other | same `except` | token kept; the user may retry after restoring the reviewed state | unchanged |
| either | `reject=True` | token present, same conversation/workspace only | token popped | `REJECTED` |
| either | workspace changed | workspace comparison in `_review_edit` | `ValueError('Workspace changed; request a new edit')`; `set_workspace` also drops the token | `EXPIRED` |
| either | other conversation selected | conversation comparison in `_review_edit` | `ValueError('Edit approval expired')` | `EXPIRED` |
| either | conversation deleted | `_delete_current_conversation` | token filtered out | `EXPIRED` |
| either | process restart | — | dictionary is gone | `ABSENT` |

### T3 — library proposal

Guards are in `ChatController.propose_library_add`,
`ChatController.review_library_add` and the `LibraryProposals` methods in
`src/blackrose/library.py`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | `knowledge_add` with a known `source_ref` | `knowledge_enabled`, conversation matches the reply; capture found (`propose_library_add`) | capture frozen (`Capture` is frozen) | `ABSENT` |
| `ABSENT` | `propose` accepted | title/1..500, ≤20 tags ≤100 chars, metadata valid; no identical open proposal; `< 8` (`LibraryProposals.propose`) | token minted | `PENDING` |
| `PENDING` | identical `propose` again | same conversation, title, tags, identity, text, completeness | returns the existing token | `PENDING` |
| `PENDING` | ninth distinct proposal | `len(pending) >= 8` | `ValueError('Review pending library proposals before requesting more')` | `PENDING` |
| `PENDING` | `review_library_add(token)` (no flags) | `LibraryProposals.get` matches conversation | card with the full frozen text | `PENDING` |
| `PENDING` | `approve=True` | `not is_generating`; `knowledge_enabled` | latch; shielded `knowledge.call('capture')`; token popped on success inside `commit` | `CONSUMED` |
| `LATCHED` | approve or reject | `is_generating` | `ChatBusy` | `LATCHED` |
| `LATCHED` | commit raised | — | `finally` clears the latch; token was not popped | `PENDING` |
| `LATCHED` | task cancelled | `asyncio.shield` around `commit` | commit finishes, token popped, `CancelledError` re-raised | `CONSUMED` |
| `PENDING` | `reject=True` | `not is_generating` | token deleted | `REJECTED` |
| `PENDING` | conversation deleted / shutdown | `_delete_current_conversation`, `shutdown` | token dropped | `EXPIRED` |
| `PENDING` | foreign conversation selected | `LibraryProposals.get` | `ValueError('Library approval expired or belongs to another conversation')` | `EXPIRED` (token survives for its owner) |

### T4 — output recovery journal

All steps are in `ChatController._save_output` unless named.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNWRITTEN` | `files.write` returned | — | bytes are on disk; the journal is not written yet | `UNWRITTEN` (`ORPHAN_FILE` if the process dies here) |
| `UNWRITTEN` | journal written | `write_json` | record holds result, owner reply, workspace, requested path, options, digest | `WRITTEN` |
| `WRITTEN` | attachment row found or appended | `attachments_for(owner)` match on path+digest | blob written only when absent | `WRITTEN` |
| `WRITTEN` | journal unlinked | — | — | `ATTACHED` |
| `WRITTEN` | blob/metadata/unlink raised | `except Exception` | result carries `artifact_pending`, `recovery_id` if the journal exists, a warning and a notice | `WRITTEN` (`UNRECORDABLE` when the journal write itself failed) |
| `WRITTEN` | same identity saved again | `journal.exists()` | workspace equality; bytes re-read and compared; owner taken from the record; **no** disk write | `ATTACHED` after the attachment and unlink steps |
| `WRITTEN` | `recover_output(id)` | id shape; `exclusive()`; conversation; workspace; digest; reply still exists (`ChatController.recover_output`) | replays `_save_output` with the recorded options | `ATTACHED` |
| `UNWRITTEN`/`ORPHAN_FILE` | process death | — | no record; nothing replays | unchanged |
| `ATTACHED` | `recover_output(old id)` | journal gone | `FileNotFoundError` from the read | `ATTACHED` |

### T5 — approved replacement (S5/S6)

`replace`, `_settle`, `remove`, `prune` and `read` are `EditBackups` methods in
`src/blackrose/edit_backups.py`; `write` and `_store_displaced` are
`WorkspaceFiles` methods in `src/blackrose/workspace.py`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `E0` | `EditBackups.replace` entered | — | `begin_edit_backup` row written **before** any file effect | `E1` |
| `E1` | `write` pre-checks pass | regular file, digest equal, size budget | mode captured, stage created 0600 | `E2` |
| `E1` | digest differs | first digest check in `write` | `FileAccessError('file_changed')`; no stage | `E0` after settle |
| `E2` | stage written and synced | — | bytes are durable | `E2` |
| `E2` | second digest check fails | "File changed before replacement" | stage unlinked in `except` | `E0` after settle |
| `E2` | `_exchange` returns | `published=True` before the call | target holds the replacement, stage holds the displaced original | `E3` |
| `E2` | `_exchange` raises | `published=False` | stage unlinked | `E0` after settle |
| `E2`/`E3` | `BaseException` after `published` became true | `except BaseException` in `write` | stage renamed `.backup` (`keep_beside`) so the displaced inode stays reachable | `E7`-shaped, row still pending |
| `E3` | `store_entry` moved the stage | — | store file has the displaced bytes, stage gone | `E5` |
| `E3` | store failed | `except` in `_store_displaced` | `keep_beside` renames the stage; warning names it | `E7` after settle (row pending meanwhile) |
| `E3` | cross-filesystem copy published, source unlinked | `src/blackrose/workspace.py::_copy_entry` | store file, no stage | `E5` |
| `E3` | cross-filesystem copy published, source left | `_copy_entry` returns `False`; `_store_displaced` warning | store file **and** stage | `E4` |
| `E3`/`E4`/`E5` | displaced digest != `expected_digest` | `backup_conflict` in `_store_displaced` | `file_changed_published` with `details` | settle from `details` |
| `E3`/`E5` | settle succeeded | `_settle` with details | `state='saved'`, facts recorded | `E6` (or `E7`) |
| `E1`..`E5` | `write` raised `FileAccessError` with details | `except FileAccessError` in `replace` | settle from the details, re-raise | `E6`/`E7` |
| `E1`..`E5` | any other exception/cancellation | `except BaseException` in `replace` | shielded settle from disk (`_settle(None)` → `_recover`) | whatever recovery derives |
| `E1`..`E5` | settle failed (database) | — | exception propagates; row stays pending | `E1`..`E5` until start |
| `E6` | `remove` | owned + `saved` (`EditBackups.owned`, `EditBackups.remove`) | store file deleted, row deleted | `E9` |
| `E7` | `remove` | same | row deleted, project copy left and named | `E9` |
| `E6`/`E7` | `prune` past `RETENTION_DAYS` | `conflict` and `stored=0` excluded (`EditBackups.prune`) | store file + row deleted | `E9` |
| `E8` | `read`/`preview`/`restore` | digest or reachability (`EditBackups.read`) | refusal; row unchanged | `E8` |
| `E8` | `remove`/`prune` | stored file already absent | row deleted (H2 fix) | `E9` |

### T6 — startup recovery of pending rows

`src/blackrose/edit_backups.py::EditBackups.recover` selects `state='pending'`
rows and applies `src/blackrose/edit_backups.py::EditBackups._recover` to each:

| From | Event | Guard / decision | Effect | To |
| --- | --- | --- | --- | --- |
| `E1` | `recover()` | store empty (`EditBackups._recover_files`); stage absent (`recover_replacement_stage`) | `delete_edit_backup`; returns 0 | `E0` |
| `E2` | `recover()` | stage digest == `published_digest` (`recover_replacement_stage`) | stage unlinked, row deleted | `E0` |
| `E3` | `recover()` | stage digest != `published_digest` | `store_entry` moves it (`recover_replacement_stage`) | `E6` (`conflict` from `_recover`) |
| `E4` | `recover()` | store already holds the same bytes | stage removed via the stored digest (H1 fix), settle from the store | `E6` |
| `E5` | `recover()` | stage absent, `src/blackrose/workspace.py::find_stored_backup` finds it | settle with `stored=True` (`_recover`) | `E6` |
| `E3`/`E4` | `recover()` | store raises (`ENOSPC`, `EEXIST`, non-regular stage) | warning in `_recover`; row and stage kept for the next start | unchanged |
| `E6`/`E7`/`E8` | `recover()` | not selected (`state != 'pending'`) | untouched | unchanged |
| any | `recover()` again | row no longer pending | no effect; no second store entry | unchanged |

## Invariants

1. **A selection is usable only while it is the newest, for the conversation it
   was created for, in the navigation epoch it saw, outside a reply.**
   `_check_workspace_selection` is the single predicate and is evaluated before
   the persist, after the authorize and after the database reads (its three
   calls in `src/blackrose/chat.py::ChatController.set_workspace`). The
   conversation is matched by id plus `_navigation_epoch`, so a title rename's
   refreshed object for the same chat stays `CURRENT` while leaving and
   returning does not. Pinned by
   `test_expiry_before_persist_leaves_no_grant_behind`, the existing
   `test_workspace_selection_rejects_navigation_delete_and_replacement`,
   `test_workspace_newer_selection_supersedes_pending_picker` and
   `test_a_title_rename_does_not_expire_an_open_picker`.
2. **A persisted grant is only written for the captured conversation id.**
   `conversation_id = selection[1].id` is read before the awaits and used for
   the write (`set_workspace`), so navigation cannot redirect the write. Pinned
   by `test_expiry_before_persist_leaves_no_grant_behind`.
3. **No grant is persisted when the selection expires before the write.**
   Checks 1 and 2 precede `_repo.set_workspace` with no intervening `await`
   after check 2. Pinned by
   `test_expiry_before_persist_leaves_no_grant_behind` and
   `test_workspace_navigation_during_authorization`.
4. **A workspace change invalidates this conversation's file proposals and extra
   grants, whether or not the change completes.** The `pending_edits` filter
   and `directory_grants.pop` run before check 3. Pinned by
   `test_workspace_change_expires_pending_edits_and_directory_grants`.
5. **A token is bound to its conversation and workspace.** `_review_edit` checks
   both and `set_workspace` filters by conversation id. Pinned by
   `test_a_token_belongs_to_its_conversation` and
   `test_workspace_change_expires_pending_edits_and_directory_grants`.
6. **One approval runs at a time.** `review_edit` takes `artifacts.exclusive()`,
   which refuses a second caller
   (`src/blackrose/artifacts.py::ArtifactLifecycle.exclusive`), and
   `_review_edit` sets `is_generating` before any await. Pinned by
   `test_a_second_approval_is_refused_while_the_first_writes` (exactly one
   `WorkspaceFiles.write` call, one backup, token gone).
7. **An approval never writes bytes the user did not review.** The replacement
   digest is checked twice, before staging and after it
   (`src/blackrose/workspace.py::WorkspaceFiles.write`), and the token's digest
   is what `edit_backups.replace` records as `approved_digest`
   (`_review_edit`). A mismatch leaves the file untouched and the token
   pending. Pinned by
   `test_an_external_change_refuses_approval_and_keeps_the_token`.
8. **A consumed token is dead; a refusable approval keeps it.** `CONSUMED` and
   `file_changed_published` pop the token; `file_changed` and other refusals
   do not (`_review_edit`). Pinned by
   `test_an_external_change_refuses_approval_and_keeps_the_token`,
   `test_a_second_approval_is_refused_while_the_first_writes`, and the existing
   `test_published_edit_conflict_expires_approval`.
9. **An output is written at most once per journal key, and metadata is
   idempotent per `(reply, path, digest)`.** The journal is read before the
   write, and the attachment lookup skips an existing row
   (`src/blackrose/chat.py::ChatController._save_output`). Pinned by
   `test_a_journal_replays_metadata_without_writing_the_file_again` (the write
   is patched to fail if it runs) and
   `test_a_replay_does_not_duplicate_an_attachment_that_already_exists`.
10. **A journal belongs to one conversation, one workspace identity and one
    digest.** The workspace check in `_save_output`, and the conversation,
    workspace and digest checks in `recover_output`. Pinned by
    `test_recovery_is_bound_to_its_conversation_and_workspace` and
    `test_recovery_refuses_changed_bytes_and_keeps_the_record`.
11. **A finished save leaves no journal.** `_save_output` unlinks it after the
    attachment row; the only surviving journals are `WRITTEN`/`UNRECORDABLE`
    outcomes. Pinned by
    `test_a_successful_output_leaves_one_attachment_and_no_journal`.
12. **A backup row exists before the bytes move, and is settled only from what is
    on disk.** `begin_edit_backup` precedes `write`
    (`src/blackrose/edit_backups.py::EditBackups.replace`); `_settle` without
    details re-derives the state (`src/blackrose/edit_backups.py::EditBackups._settle`).
    Pinned by every case in `EditBackupCrashTests` (rows are created without a
    writer running).
13. **`recover()` handles every pending pair without touching a saved one.**
    `recover_replacement_stage` distinguishes never-published from displaced by
    digest (`src/blackrose/workspace.py::recover_replacement_stage`);
    `find_stored_backup` covers the completed move; the query filters
    `state='pending'` (`EditBackups.recover`). Pinned by the twelve
    `EditBackupCrashTests`, in particular
    `test_saved_records_are_never_revised_by_startup_recovery`.
14. **Recovery is idempotent.** A settled row is no longer selected and no store
    entry is created twice. Pinned by
    `test_recovery_is_idempotent_and_never_duplicates_a_stored_backup`.
15. **`conflict` means "the displaced bytes are not the reviewed bytes", computed
    identically on both paths** (`WorkspaceFiles._store_displaced`,
    `EditBackups._recover`, with `approved_digest == expected_digest` from the
    `edit_backups.replace` call in `_review_edit`). Pinned by
    `test_displaced_content_that_changed_before_the_crash_is_a_conflict` and the
    existing `test_published_edit_conflict_expires_approval`.
16. **Conflict content and project copies are never removed by age; store copies
    are only removed explicitly.** `prune` skips `conflict`/`stored=0`
    (`EditBackups.prune`); `remove` only forgets a project copy
    (`EditBackups.remove`). Pinned by the existing
    `test_retention_keeps_conflicts_and_removal_is_explicit` plus
    `test_prune_forgets_records_whose_files_are_already_gone`.
17. **A library token commits exactly once and the reviewed bytes are the frozen
    capture.** `Capture`/`Proposal` are frozen dataclasses
    (`src/blackrose/library.py::Capture`, `src/blackrose/library.py::Proposal`);
    `import_capture` derives the file name and `expected_sha256` from that
    text; the token is popped only after the commit returns (`commit` in
    `review_library_add`). Pinned by `test_approval_commits_the_snapshot_once`
    and the existing
    `test_disk_changes_during_ingestion_cannot_change_approved_content`.
18. **A failed library commit is retryable and cannot leave the writer latched.**
    `finally` clears `is_generating` (`review_library_add`); the token survives
    a raise. Pinned by
    `test_a_failed_commit_keeps_the_token_and_clears_the_latch`.
19. **No machine in this document can be driven by model output alone.** A
    proposal only becomes an effect through `review_*`; the model cannot supply
    the approval flag
    (`tests/test_library.py::LibraryTests.test_old_web_refs_expire_and_model_cannot_supply_content_or_approval`
    pins the schema refusal) and `WorkspaceFiles.write` refuses an unreviewed
    replacement (`collision == 'replace' and not reviewed`).
20. **The private store is not reachable through file tools.**
    `WorkspaceFiles.parent` refuses any path under `self.backups`; the store is
    0700 (`src/blackrose/workspace.py::store_entry`). Pinned by the existing
    `test_file_tools_cannot_reach_the_store`.

## Hazards and open questions

| # | Severity | Where | Finding | Demonstrated |
| --- | --- | --- | --- | --- |
| H1 | fragile (fixed) | `src/blackrose/edit_backups.py::EditBackups._recover_files`, `src/blackrose/workspace.py::store_entry` | A cross-filesystem copy can publish the store file and leave its source under the stage name (E4). Recovery then tried to move the stage onto the same store name, got `EEXIST` for ever: the row stayed `pending`, the backup was never listed, and the displaced original stayed hidden in the project — the module docstring's promise ("never left hidden in the project or untracked"). Recovery now looks up the stored copy first and passes its digest, so a stage holding exactly those bytes is removed (content-checked) and the row settles from the store. Differing bytes still fail closed. | `test_leftover_stage_from_a_published_copy_is_removed_not_moved_again` (failed before the change: `0 != 1`), `test_a_differing_leftover_stage_is_not_discarded` |
| H2 | fragile (fixed) | `src/blackrose/edit_backups.py::EditBackups._delete`, `src/blackrose/workspace.py::_stored_backup` | If the whole `<store>/<group>` directory is gone, `remove_stored_backup` raised `FileNotFoundError` from the descriptor walk before it reached the tolerant `unlink`. `remove()` and `prune()` therefore aborted, leaving rows that could never be deleted, and `prune` stopped partway through its list. `_delete` now treats a missing group as already deleted (matching the project's "absent is a successful delete" convention). | `test_missing_store_group_does_not_block_removal`, `test_prune_forgets_records_whose_files_are_already_gone` (both errored before the change) |
| H3 | fragile | `src/blackrose/chat.py::ChatController.set_workspace` (persist before check 3) | The last selection check runs *after* `_repo.set_workspace`. If navigation or a reply starts during the database write/read, the caller gets "Workspace selection expired", the bridge shows an error and does not refresh, yet the grant is durable and this conversation's pending edits and extra grants have already been discarded. The conversation is then granted a directory the UI reported as not applied. | `test_expiry_after_the_persist_reports_failure_for_an_applied_grant` |
| H4 | fragile | `src/blackrose/chat.py::ChatController._review_edit` (reject branch) vs `src/blackrose/chat.py::ChatController.review_library_add` | The edit machine rejects a token without checking `is_generating`, while the library machine refuses a reject with `ChatBusy`. A reject that arrives while an approval is writing returns `{'rejected': True}` and pops the token, but the approval still publishes the file: the user is told the edit was rejected and finds it applied. Two concurrent callers are needed; the GUI bridge serializes review calls on one thread (`Api.review_edit`), so this is a latent inconsistency rather than a reachable UI bug. | `test_reject_during_an_approval_loses_the_rejection` vs `test_reject_while_committing_is_busy` |
| H5 | fragile | `src/blackrose/chat.py::ChatController._review_edit`, `src/blackrose/chat.py::ChatController._restore_edit_backup` | `_review_edit` binds the proposal to `self.conversation` at the guard, then re-reads it after two awaits for `workspace_files()` and for the backup's `conversation_id`. Opening an existing chat with an equal grant in that window publishes the edit and records its backup under the newly selected chat, so the originating chat cannot restore it ("Open the conversation that made this edit") and the other chat's list shows a file it never touched. `_restore_edit_backup` has the same shape. The GUI thread serializes calls, so this needs a programmatic caller. | `test_navigation_during_an_approval_files_the_backup_under_the_new_chat` |
| H6 | documentation-gap | `src/blackrose/chat.py::ChatController._save_output` (`files.write` before the journal) | The journal is written *after* the file. Process death between the write and the journal leaves an output with no journal, no attachment and no tool result; nothing replays it, and a model retry creates a second copy. The limitation is stated for users ([troubleshooting](../troubleshooting.md#output-saved-but-conversation-artifact-pending): "not a process crash between publication and recording"), not in the module or the architecture map. Closing it needs a pre-write intent record (a planned stage name), i.e. a design change, not a guard. | Not committed; the window is between two statements and needs process death to observe. Would confirm with a subprocess that `os._exit`s between them. |
| H7 | documentation-gap | `src/blackrose/chat.py::ChatController.recover_output` | `recover_output` has no shipped caller: `bridge.py` exposes no action for it, so the real recovery path is repeating the identical request (same journal key). It also assumes the originating reply still exists (its `next(...)` over `current_messages()` raises `StopIteration`, surfacing as `RuntimeError`), and a journal whose conversation was deleted is unreachable and never cleaned (the same gap sm-12 records as an `unrecognized` layout). | `test_recovery_is_bound_to_its_conversation_and_workspace` pins the guards; the missing-reply and orphan-journal cases are read-only findings. |
| H8 | fragile | `src/blackrose/storage/repository.py::Repository.settle_edit_backup` | `settle_edit_backup` updates every column with only `WHERE id=?`, and `mark_edit_backup_restored` has no state guard either. Correctness rests on `recover()` selecting `state='pending'` and on one desktop owning the database, so a delayed settle (a shielded task that outlives its caller) could rewrite an already saved row. | `test_saved_records_are_never_revised_by_startup_recovery` pins the recovery half only; the unguarded update itself needs a concurrent caller to demonstrate. |
| H9 | fragile | `src/blackrose/edit_backups.py::EditBackups.replace` (final settle) | If the settle write fails after a successful publication, `replace` raises with the row still `pending`. The backup is on disk and the file is published, but in the same process the token is now unretryable (`file_changed` on the next approval) until `recover()` runs at the next start. The user sees a storage error for an edit that happened. | Not committed; would need `settle_edit_backup` to raise while the process stays alive. Its recovery half is `test_exchanged_stage_is_moved_to_the_store_and_settled`. |
| H10 | documentation-gap | `src/blackrose/workspace.py::keep_beside`, `src/blackrose/workspace.py::WorkspaceFiles._store_displaced` | Two fallback shapes are not representable. (a) `keep_beside` falling through every candidate returns the *stage* path, so `location` names a hidden `-edit` entry that only that record can read. (b) Process death after `keep_beside` but before the settle leaves a visible `name.backup` with no row; recovery deletes the pending row, so the copy is neither listed nor restorable (visible and untracked). Both preserve the bytes, which is the priority, but neither is described in `workspaces.md`. | (a) is pinned by the existing `test_store_and_rename_failure_still_names_the_retained_entry`; (b) is a crash window, not demonstrated. |
| H11 | documentation-gap | `src/blackrose/workspace.py::read_stored_backup` | `EditBackups.read` promises "no longer matches the saved original", but a missing store file or group surfaces as `FileNotFoundError` naming a *relative* path (`store`), not the location the user knows. Restore and preview therefore report an OS error instead of the documented refusal. Removal of such a row is fixed under H2. | `test_missing_store_group_does_not_block_removal` pins the current error before removal. |
| H12 | none-found | `src/blackrose/workspace.py::_cleanup_abandoned_stages` | The abandoned-stage sweep is bounded correctly: it only touches names without `-edit`, regular files, mode 0600, the process's uid and older than 24 h, at most once per directory per process. A live write is bounded by 2.56 MB, so a stage cannot age 24 h unless the filesystem stalls; `-edit` stages are never collected, which is required because one may hold the user's original. The per-process memo can skip one sweep when a directory inode is reused. | Existing `test_old_abandoned_stage_is_collected_before_new_output` and `test_process_death_during_write_leaves_no_partial_final_and_stage_is_hidden`. |
| H13 | none-found | `src/blackrose/chat.py::ChatController.begin_workspace_selection`, `src/blackrose/chat.py::ChatController._check_workspace_selection` | The selection token is never cleared after use, so a consumed selection is `CURRENT` again and the same `(path, selection)` call can be replayed. It cannot widen access: the grant is recomputed from the path, persisted for the same conversation, and any newer picker or `set_workspace(None)` supersedes it. The token is matched by conversation id plus `_navigation_epoch`; an earlier identity comparison failed closed for an automatically retitled chat (bra-lfou). | Covered by the existing `test_workspace_newer_selection_supersedes_pending_picker` and `test_navigation_still_expires_a_picker_whose_chat_was_renamed`. |

### Answers to the bead's focus questions

- **Which on-disk/DB pairs does `recover()` handle?** S5's E1→`E0`, E2→`E0`,
  E3→`E6`, E4→`E6`, E5→`E6`; E6–E9 are not selected. The discriminator is the
  digest of the named stage (`recover_replacement_stage`) plus the store lookup
  (`find_stored_backup`), which is exactly what distinguishes "never
  published" from "displaced original waiting".
- **Process death between `RENAME_EXCHANGE` and `settle_edit_backup`?** The row
  is `pending`, the stage holds the displaced original, the target holds the
  replacement. Startup moves the stage to the store, settles `saved` and sets
  `conflict` when those bytes differ from `approved_digest`
  (`test_exchanged_stage_is_moved_to_the_store_and_settled`,
  `test_displaced_content_that_changed_before_the_crash_is_a_conflict`). If the
  store move had already completed, `find_stored_backup` settles from the store
  (`test_row_whose_copy_already_reached_the_store_settles_from_it`); if it had
  completed *by copy*, the source stage is the leftover this bead fixed (H1).
- **Process death between writing the stage and exchanging?** The stage holds
  exactly the replacement, so recovery unlinks it and deletes the row: the
  target was never touched, so there is nothing to keep
  (`test_stage_holding_the_approved_replacement_is_removed_as_unpublished`). The
  same branch is correct when the displaced bytes happened to equal the
  replacement, because the target already holds those bytes.
- **Can the selection token be stale after each of the three awaits?** Yes after
  the authorize (check 2, nothing persisted: safe) and after the database
  read/write (check 3, grant already persisted: H3). Between check 2 and the
  persist there is no await. After check 3 there is no await before the
  in-memory assignment, so the display update itself cannot interleave.
- **Can `pending_edits` be approved twice, or approved after the file changed?**
  Not twice: `artifacts.exclusive()` refuses a concurrent caller and the token is
  popped on success. After a change the approval is attempted and refused by the
  digest guard, leaving the token pending; it stays usable only if the file
  returns to the reviewed bytes (`test_an_external_change_refuses_approval_and_keeps_the_token`).
  The two real gaps are H4 (a concurrent reject is lost) and H5 (the owner is
  re-read after the guard).
- **Does the output-recovery journal survive a crash, and is it replayed exactly
  once?** It survives a crash after it was written (it is a plain file under the
  attachment store and no startup code deletes it); the disk write is not
  repeated because the identity key is found before `files.write`, and metadata
  is not duplicated because the attachment lookup matches `(reply, path,
  digest)`. It does **not** cover a crash between the write and the journal (H6),
  and `recover_output` is not wired to the UI (H7).

## Formalization decisions

- **No explicit state enum or new abstraction.** Each machine is already a small
  set of fields whose predicates are one-line checks (`_check_workspace_selection`,
  `pending_edits.get`, `LibraryProposals.get`, `journal.exists()`,
  `state='pending'` + digest). Converting them to enums or a shared helper would
  add indirection without adding a guard, and three of the five files are shared
  with other work in this epic. The bead's "formalize explicitly" goal is met by
  this document plus the `test_sm_11_workspace_files.py` suite.
- **Source change 1 — recovery of a store copy whose source stage was left
  behind (H1).** `EditBackups._recover_files` now looks up the stored backup
  first and passes its digest to `recover_replacement_stage`, so a stage holding
  exactly those bytes is removed (the existing content check in
  `recover_replacement_stage` decides) and the row settles from the store.
  Differing bytes keep the previous fail-closed pending outcome. No policy or
  approval changes; only the previously unreachable E4→E6 transition is added.
  Regression tests: `test_leftover_stage_from_a_published_copy_is_removed_not_moved_again`
  (fails before the change) and `test_a_differing_leftover_stage_is_not_discarded`
  (guards the refusal).
- **Source change 2 — removal of a record whose store group is gone (H2).**
  `EditBackups._delete` treats `FileNotFoundError` from `remove_stored_backup` as
  already removed. This makes `remove` and `prune` total over E8. Regression
  tests: `test_missing_store_group_does_not_block_removal` and
  `test_prune_forgets_records_whose_files_are_already_gone` (both errored before
  the change).
- **Deferred: H3 (chat.py, shared).** The smallest honest fix is to stop
  reporting expiry after the grant is durable: keep the check before the persist,
  and after it, if the selection is stale, still return the grant while leaving
  `self.conversation`/the page untouched (with a notice that the directory was
  granted to the conversation that asked). Reverting the row instead would race
  a newer grant. Needs a decision about the user-facing wording, and the file is
  shared with other agents, so it is a follow-up.
- **Deferred: H4 (chat.py, shared).** Move the reject branch inside the
  `is_generating` guard (`raise ChatBusy` like `review_library_add`), or take
  `artifacts.exclusive()` for reject too. Both are one-line changes but alter
  Stop/reject settlement, so they belong with the review-loop work rather than
  this audit. `test_reject_during_an_approval_loses_the_rejection` will need
  inverting.
- **Deferred: H5 (chat.py, shared).** Capture the owner once — `owner =
  self.conversation` before the first await — and use `owner.id` for
  `workspace_files`, `replace` and `mark_edit_backup_restored`; refuse if the
  selection changed. Same for `_restore_edit_backup`. This is the same shape as
  the workspace-selection token and should probably reuse a single captured-owner
  helper rather than three separate copies.
- **Deferred: H8 (repository.py, shared).** Add `AND state='pending'` to
  `settle_edit_backup` and make `mark_edit_backup_restored` a no-op for unknown
  ids, so a late settle cannot rewrite a saved row. Requires a decision about
  whether a late settle should log instead of silently doing nothing.
- **Not changed: the workspace write path.** The stage/exchange/store sequence,
  the non-cooperating-writer caveat and the sweep are sound for the stated
  threat model; H10/H11 are documentation and diagnostic gaps, and fixing the
  `FileNotFoundError` wording would belong to `workspace.py`, which is shared.

## Verification

Run from the repository root (the runner provides scratch XDG directories and
reports any write outside them):

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_11_workspace_files.py -v
```

Four classes: `EditBackupCrashTests` (the S5/S6 pairs and T6),
`WorkspaceSelectionTests` (S1/S2 and T1/T2), `OutputJournalTests` (S4 and T4)
and `LibraryProposalTests` (S3 and T3).

The pre-existing focused tests for the same code, each run with the same runner
(`-p <file>`): `test_workspace.py`, `test_workspace_atomic_publish.py`,
`test_workspace_suggestions.py`, `test_edit_backups.py`, `test_library.py` and
`test_directory_access.py`.

Static checks for the source changes:

```sh
.venv/bin/ruff check src/blackrose/edit_backups.py tests/test_sm_11_workspace_files.py
.venv/bin/python -m mypy
```

The H1 and H2 regression tests fail against the previous `EditBackups`:
`test_leftover_stage_from_a_published_copy_is_removed_not_moved_again`
(`0 != 1`), and `test_missing_store_group_does_not_block_removal` and
`test_prune_forgets_records_whose_files_are_already_gone` (both
`FileNotFoundError`). Both changes are branches inside `EditBackups`'s recovery
and removal helpers, exercised by the offline suite; the `--gui` and native
workspace/library probes add no coverage for them.
