# SM-11: Workspace selection, review tokens, output recovery journal and edit backups

## Scope and ownership

Four independent machines in one user workflow: authorizing a directory for a
chat, reviewing model-proposed file and library changes, recording an output
whose attachment metadata failed, and settling the displaced original of an
approved replacement.

- `src/blackrose/chat.py`: `ChatController._workspace_selection` (125) and
  `_navigation_epoch` (124), `begin_workspace_selection` (772–777),
  `_check_workspace_selection` (779–789), `set_workspace` (791–812),
  `file_tool` proposals (1010–1052), `_save_output`
  (1054–1118), `recover_output` (1120–1140), `review_edit`/`_review_edit`
  (1142–1192), `edit_backup_action` (1194–1209), `_restore_edit_backup`
  (1211–1240), `propose_library_add` (1242–1266), `review_library_add`
  (1268–1301), `recover_interrupted` (229–232), startup/deletion purges
  (253, 531–532).
- `src/blackrose/edit_backups.py`: `EditBackups` (33–177) — durable records,
  crash settlement, guarded restore, explicit removal and age cleanup.
- `src/blackrose/workspace.py`: `WorkspaceFiles.write` (632–729),
  `_store_displaced` (598–630), `store_entry` (159–193), `keep_beside`
  (196–209), `recover_replacement_stage` (212–230), `find_stored_backup`
  (259–270), `remove_stored_backup` (273–279), `_cleanup_abandoned_stages`
  (45–70), `replacement_stage` (38–42), `backup_filename` (282–285).
- `src/blackrose/library.py`: `LibraryProposals` (70–139), `import_capture`
  (142–186).
- `src/blackrose/storage/repository.py`: `begin_edit_backup` (560–568),
  `settle_edit_backup` (570–578), `edit_backups` (580–589), `edit_backup`
  (591–595), `orphaned_edit_backups` (597–603), `mark_edit_backup_restored`
  (605–609), `delete_edit_backup` (611–615). Schema in
  `storage/migrations.py:93–110` (migration 7).
- Coupled, not owned here: `gui/bridge.py:362–385` runs the native picker
  between `begin_workspace_selection` and `set_workspace`, and
  `bridge.py:416–431` serializes review calls on the GUI thread;
  `artifacts.py:173–178` supplies the writer-exclusion lock; `turns.py` runs
  tool calls (`file_tool`) inside a reply; `attachments/` owns blobs.

Ownership per [architecture.md](../architecture.md):

- **One application asyncio loop, one desktop.** `ChatController` owns every
  variable here; the page only renders cards and sends review decisions
  (`architecture.md:13`, `:112–118`, `:309–340`). Two desktops cannot share the
  database ([ADR 0001](../adr/0001-desktop-ownership.md)), which is why no
  machine in this document has a cross-process lock.
- **Workspace grants are one per conversation, persisted.**
  `WorkspaceFiles` re-checks the stored device/inode on every operation
  (`workspace.py:444–447`), refuses the backup store as a workspace path
  (`:468–469`) and lets only the output folder create unreviewed files
  (`:439–442`). `files.readable` is the set of this conversation's output paths
  (`chat.py:833–834`).
- **Review tokens are session-only.** `pending_edits` and
  `LibraryProposals.pending` are plain dictionaries; `workspaces.md` states the
  edit proposals "expire on application restart or workspace change" and the
  library proposals are "not durable across process restart"
  (`architecture.md:112–118`).
- **Edit backups are durable and outlive their conversation.** Migration 7 has
  no conversation foreign key on purpose (`migrations.py:89–92`); only an
  explicit removal deletes a row, and age cleanup never removes conflict
  content (`edit_backups.py:162–177`).
- **Output recovery is a journal outside the database.** It sits in the
  attachment store root (`chat.py:1064`) and is described in
  [troubleshooting.md](../troubleshooting.md#output-saved-but-conversation-artifact-pending).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `_workspace_selection` | `chat.py:125` | `tuple[object, Conversation, int] \| None` | Last picker token, its owning conversation and the navigation epoch it saw | `begin_workspace_selection` `:772` only |
| `_navigation_epoch` | `chat.py:124` | `int` | Bumped when the selection moves to another chat; refreshing the same chat's metadata is not navigation | `conversation` setter `:158` only |
| `_workspace_lock` | `chat.py:126` | `asyncio.Lock` | Serializes `set_workspace` bodies | `set_workspace` `:795` |
| `is_generating` | `chat.py:127` | `bool` | Single-writer latch for replies, approvals and restores | `_review_edit` `:1167`/`:1192`, `review_library_add` `:1282`/`:1301`, `_restore_edit_backup` `:1223`/`:1237`, `turns.py` |
| `pending_edits` | `chat.py:133` | `dict[str, dict]`, ≤ 8 | File proposals bound to conversation, workspace, reply, path, bytes, digest | `file_tool` `:1019`, `:1047`; removed `:1155`, `:1187`, `:1189`, `:531`, filtered `:804` |
| `directory_grants` | `chat.py:138` | `dict[int, dict]` | Extra in-memory read grants per conversation | `approve_directories` `:824`; popped `:806` |
| `library_proposals` | `chat.py:132` | `LibraryProposals` | In-memory capture proposals | `__init__` only; mutated in place (`library.py:126`, `:1276`, `:1287`, `:136`, `chat.py:253`) |
| `LibraryProposals.pending` | `library.py:72` | `dict[str, Proposal]`, ≤ 8 | Token → frozen capture/title/tags | `propose` `:126`, `:122` (dedup hit); `get` `:129`; pops above |
| `Proposal` | `library.py:48–67` | frozen dataclass | The reviewed snapshot; `review()` renders a card | constructed in `propose` `:116` |
| `_reference_index` | `chat.py:133` | cache \| `None` | Reference suggestions invalidated by writes and grants | `:792`, `:1078`, `:1238` |
| `output-recovery/<key>.json` | `chat.py:1064` | JSON `{result, assistant_id, workspace, conversation_id, requested_path, options, digest}` | The bridge from a written file to its attachment row | `write_json` `:1081–1084`; unlinked `:1096` |
| `key` | `chat.py:1061–1063` | 64 hex chars | `sha256(json([conversation_id, path, sha256(data), options]))` | computed per save |
| `edit_backups.state` | `migrations.py:100` | `'pending' \| 'saved'` (CHECK) | Record lifecycle | `begin` `:564`, `settle` `:575` |
| `edit_backups.stored` | `migrations.py:102` | `0 \| 1` | Location is inside the private store (1) or a project path (0) | `settle` |
| `edit_backups.conflict` | `migrations.py:106` | `0 \| 1` | Displaced bytes differ from the reviewed digest | `settle` (`edit_backups.py:90`, `workspace.py:624`) |
| `edit_backups.location`/`digest`/`size`/`mode` | `migrations.py:101–105` | text/int | Where the original is, and its facts | `settle` |
| `edit_backups.approved_digest`/`published_digest` | `migrations.py:98–99` | text | Digest the user reviewed, and the bytes the replacement published | `begin` only |
| `edit_backups.restored_at` | `migrations.py:108` | text \| `NULL` | This backup was put back; informational only | `mark_edit_backup_restored` |
| `.blackrose-stage-<id>-edit` | `workspace.py:38–42` | directory entry | Same-folder carrier of the replacement, then of the displaced original | `write` `:668`; removed/renamed by `:688`, `:692`, `_store_displaced` `:607–609`, `recover_replacement_stage` `:228`/`:230` |
| `<store>/<group>/<id>-<label>` | `workspace.py:282–285` | file in `data_dir()/edit-backups/<conversation>` | The retained original | `store_entry` `:180`/`:184` |
| `<name>.backup`, `<name> (n).backup` | `workspace.py:196–209` | visible project file | Fallback when the store cannot take the entry | `keep_beside` |
| `published` | `workspace.py:669` | local `bool` | Set immediately **before** the exchange syscall (`:679`), cleared if it fails (`:684`) | that block only |

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
the directory again')` (`chat.py:779–789`). There is no `CONSUMED` state: the
field is never cleared, so a successful selection is `CURRENT` again. A title
rename replaces the selected `Conversation` object for the same id
(`chat.py:507`) without touching the epoch, so it leaves the picker `CURRENT`;
that refresh is why the comparison is by id rather than by object (bra-lfou).

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
against `pending['data']`/`pending['text']` (`:1159–1164`) and changes nothing.

**S3 — library proposal** (per token):

| State | Predicate |
| --- | --- |
| `ABSENT` | token not in `pending` |
| `PENDING` | token present, no commit running |
| `LATCHED` | token present and `is_generating` is `True` — this token's commit, or any reply/approval, because the latch is controller-wide |
| `CONSUMED` | popped after `import_capture` returned |
| `REJECTED` | popped by `reject=True` |
| `EXPIRED` | `discard_conversation`, `shutdown`, process exit, or a foreign conversation |

A commit failure returns the token to `PENDING` (`chat.py:1285–1288` runs before
the pop), so the machine is retryable; a success is terminal because the token
is gone.

**S4 — output recovery journal** (per key):

| State | Predicate |
| --- | --- |
| `UNWRITTEN` | no journal file; the disk write for this identity has not succeeded yet |
| `WRITTEN` | journal exists; the output file exists with `record['digest']` |
| `ATTACHED` | journal removed after `append_attachments` found or created the row |
| `UNRECORDABLE` | the journal write itself failed; the result carries `recovery_id: None` (`:1097–1105`) |
| `ORPHAN_FILE` | no journal file and the output exists — the gap between `:1076` and `:1081` made permanent by process death; no code can observe it |

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

1. The native picker: `begin_workspace_selection` (`chat.py:772`) then
   `set_workspace(path, selection=...)` (`bridge.py:366–379`); clearing the
   workspace calls `set_workspace(None)` without a selection (`bridge.py:383`),
   as do the launch-directory paths (`chat.py:359`, `:403`, `:816`).
2. Navigation: `open_conversation`, `new_conversation`,
   `delete_current_conversation`; each passes through the `conversation` setter
   (`chat.py:150–159`), which bumps `_navigation_epoch` for a different id.
3. A reply starting or ending (`is_generating`, `turns.py`), including the
   `file_tool` calls that create proposals.
4. `review_edit(token, approve|reject|neither)` from the page
   (`bridge.py:416–431`); `edit_backup_action('restore'|'remove'|'prune'|…)`.
5. `propose_library_add` (a `knowledge_add` tool call), then
   `review_library_add(token, approve|reject|neither)`.
6. `WorkspaceFiles.write` in either mode, including the exchange syscall, the
   store move, the `keep_beside` fallback and every exception on those paths.
7. Process death at any instruction boundary, and the startup
   `recover_interrupted` (`chat.py:229–232` → `EditBackups.recover`).
8. Metadata failure inside `_save_output` (blob write, `append_attachments`,
   journal unlink), a cancelled save (`:1108–1118`), and a later
   `_save_output`/`recover_output` for the same journal key.
9. Conversation deletion (`chat.py:521–533`) and `shutdown` (`:246–253`).

## Transition table

### T1 — workspace selection token

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | picker opened | `not is_generating` `:773–774` | new token stored with the ensured conversation and the current epoch `:775–777` | `CURRENT` (previous token `SUPERSEDED`) |
| `CURRENT` | automatic title rename of the same chat | id and epoch unchanged `:780–783` | the selected object is replaced (`:507`); nothing expires | `CURRENT` |
| `CURRENT` | `set_workspace` entered | lock acquired `:795` | first check `:796` | `CURRENT` |
| `CURRENT` | `authorize_directory` returned | check `:799` | nothing persisted yet; `grant` computed off-loop `:798` | `CURRENT` |
| `CURRENT` | `_repo.set_workspace` returned | — | grant durable for the captured conversation id `:802` | `CURRENT` |
| `CURRENT` | `get_conversation` returned | check `:807` | pending edits dropped `:804–805`, extra grants popped `:806`, selection updated `:810`, cache cleared `:811` | `CURRENT` (consumed) |
| `CURRENT` | navigation/generation during `authorize_directory` | check `:799` | `ChatBusy`; nothing persisted | `NAVIGATED`/`DETACHED`/`BUSY` |
| `CURRENT` | navigation/generation during the database write or read | check `:807` | `ChatBusy`; the grant **is** already persisted (H3) | `NAVIGATED`/`DETACHED`/`BUSY` |
| `CURRENT` | captured conversation row is `None` | `:808–809` | `ChatBusy` | `DETACHED` |
| `CURRENT` | `set_workspace(None)` | `authorize_directory` skipped `:798` | `grant=None` persisted, edits dropped | `CURRENT` (no grant) |

### T2 — pending edit proposal

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | `file_edit` tool call | `len(pending_edits) < 8` `:1011`; text/binary checks `:1013–1017` | old text+digest captured; token stored `:1018–1022` | `EDIT_REVIEW` |
| `ABSENT` | `document_create` outside the output folder | `_save_output` raised `review_required` `:1036–1040`; `< 8` `:1043`; destination resolved `:1045` | token stored `:1046–1049` | `CREATE_REVIEW` |
| `ABSENT` | `file_edit` with 8 already pending | `:1011` | `ValueError('Review pending edits before requesting more')` | `ABSENT` |
| `EDIT_REVIEW`/`CREATE_REVIEW` | `review_edit(token)` (no flags) | token present, same conversation, same workspace `:1149–1153` | comparison rendered; no mutation | unchanged |
| `EDIT_REVIEW` | `approve=True` | `not is_generating` `:1165`; `exclusive()` `:1144` | `is_generating=True` `:1167`; `metadata` re-check `:1178`; `EditBackups.replace` `:1179–1184` | `CONSUMED` `:1189` |
| `CREATE_REVIEW` | `approve=True` | same guards | `_save_output(reviewed=True, collision='error')` `:1174–1175` (refuses an appeared destination) | `CONSUMED` |
| either | approval raises `file_changed_published` | `:1186–1187` | token popped, then re-raised, so the card cannot be retried | `EXPIRED` |
| either | approval raises `file_changed`/`already_exists`/other | `:1185–1188` | token kept; the user may retry after restoring the reviewed state | unchanged |
| either | `reject=True` | token present, same conversation/workspace `:1149–1153` only | token popped `:1155` | `REJECTED` |
| either | workspace changed | `:1152` | `ValueError('Workspace changed; request a new edit')`; `set_workspace` also drops the token `:785` | `EXPIRED` |
| either | other conversation selected | `:1150–1151` | `ValueError('Edit approval expired')` | `EXPIRED` |
| either | conversation deleted | `chat.py:531` | token filtered out | `EXPIRED` |
| either | process restart | — | dictionary is gone | `ABSENT` |

### T3 — library proposal

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | `knowledge_add` with a known `source_ref` | `knowledge_enabled`, conversation matches the reply `:1243–1246`; capture found `:1247–1265` | capture frozen (`Capture` is frozen) | `ABSENT` |
| `ABSENT` | `propose` accepted | title/1..500, ≤20 tags ≤100 chars, metadata valid `library.py:110–115`; no identical open proposal `:117–122`; `< 8` `:123–124` | token minted `:125–126` | `PENDING` |
| `PENDING` | identical `propose` again | same conversation, title, tags, identity, text, completeness `:118–121` | returns the existing token | `PENDING` |
| `PENDING` | ninth distinct proposal | `:123–124` | `ValueError('Review pending library proposals before requesting more')` | `PENDING` |
| `PENDING` | `review_library_add(token)` (no flags) | `get` matches conversation `:1271`, `:129–133` | card with the full frozen text | `PENDING` |
| `PENDING` | `approve=True` | `not is_generating` `:1272–1274`; `knowledge_enabled` `:1280–1281` | latch `:1282`; shielded `knowledge.call('capture')` `:1289–1298`; token popped on success `:1287` | `CONSUMED` |
| `LATCHED` | approve or reject | `:1272–1274` | `ChatBusy` | `LATCHED` |
| `LATCHED` | commit raised | — | `finally` clears the latch `:1301`; token was not popped | `PENDING` |
| `LATCHED` | task cancelled | shield `:1293` | commit finishes, token popped, `CancelledError` re-raised `:1297–1298` | `CONSUMED` |
| `PENDING` | `reject=True` | `not is_generating` `:1272–1274` | token deleted `:1276` | `REJECTED` |
| `PENDING` | conversation deleted / shutdown | `:532`, `:253` | token dropped | `EXPIRED` |
| `PENDING` | foreign conversation selected | `get` `:131–132` | `ValueError('Library approval expired or belongs to another conversation')` | `EXPIRED` (token survives for its owner) |

### T4 — output recovery journal

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNWRITTEN` | `files.write` returned | — | bytes are on disk `:1076`; the journal is not written yet | `UNWRITTEN` (`ORPHAN_FILE` if the process dies here) |
| `UNWRITTEN` | journal written | `write_json` `:1081–1084` | record holds result, owner reply, workspace, requested path, options, digest | `WRITTEN` |
| `WRITTEN` | attachment row found or appended | `attachments_for(owner)` match on path+digest `:1085–1088` | blob written only when absent `:1089–1095` | `WRITTEN` |
| `WRITTEN` | journal unlinked | `:1096` | — | `ATTACHED` |
| `WRITTEN` | blob/metadata/unlink raised | `except Exception` `:1097` | result carries `artifact_pending`, `recovery_id` if the journal exists, a warning and a notice | `WRITTEN` (`UNRECORDABLE` when the journal write itself failed) |
| `WRITTEN` | same identity saved again | journal matches `:1065–1074` | workspace equality `:1068`; bytes re-read and compared `:1071–1073`; owner taken from the record `:1074`; **no** disk write | `ATTACHED` after `:1085–1096` |
| `WRITTEN` | `recover_output(id)` | id shape `:1122–1124`; `exclusive()` `:1125`; conversation `:1128`; workspace `:1131`; digest `:1135`; reply still exists `:1137–1138` | replays `_save_output` with the recorded options `:1139–1140` | `ATTACHED` |
| `UNWRITTEN`/`ORPHAN_FILE` | process death | — | no record; nothing replays | unchanged |
| `ATTACHED` | `recover_output(old id)` | journal gone | `FileNotFoundError` from the read `:1127` | `ATTACHED` |

### T5 — approved replacement (S5/S6)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `E0` | `EditBackups.replace` entered | — | `begin_edit_backup` row written **before** any file effect `edit_backups.py:41–42` | `E1` |
| `E1` | `write` pre-checks pass | regular file, digest equal, size budget `workspace.py:647–663` | mode captured, stage created 0600 `:666–668` | `E2` |
| `E1` | digest differs | `:662–663` | `FileAccessError('file_changed')`; no stage | `E0` after settle |
| `E2` | stage written and synced | `:671–675` | bytes are durable | `E2` |
| `E2` | second digest check fails | `:677–678` | stage unlinked in `except` `:687–688` | `E0` after settle |
| `E2` | `_exchange` returns | `published=True` before the call `:679` | target holds the replacement, stage holds the displaced original | `E3` |
| `E2` | `_exchange` raises | `published=False` `:684` | stage unlinked `:688` | `E0` after settle |
| `E2`/`E3` | `BaseException` after `published` became true | `:686–693` | stage renamed `.backup` so the displaced inode stays reachable `:692` | `E7`-shaped, row still pending |
| `E3` | `store_entry` moved the stage | `:180` | store file has the displaced bytes, stage gone | `E5` |
| `E3` | store failed | `:608–612` | `keep_beside` renames the stage `:609`; warning names it | `E7` after settle (row pending meanwhile) |
| `E3` | cross-filesystem copy published, source unlinked | `workspace.py:148–154` | store file, no stage | `E5` |
| `E3` | cross-filesystem copy published, source left | `:151–156` returns `False`; `:620–622` warning | store file **and** stage | `E4` |
| `E3`/`E4`/`E5` | displaced digest != `expected_digest` | `:624`, `:628–629` | `file_changed_published` with `details` | settle from `details` |
| `E3`/`E5` | settle succeeded | `edit_backups.py:55–60` | `state='saved'`, facts recorded | `E6` (or `E7`) |
| `E1`..`E5` | `write` raised `FileAccessError` with details | `:45–47` | settle from the details, re-raise | `E6`/`E7` |
| `E1`..`E5` | any other exception/cancellation | `:48–50` | shielded settle from disk (`_settle(None)` → `_recover`) | whatever recovery derives |
| `E1`..`E5` | settle failed (database) | — | exception propagates; row stays pending | `E1`..`E5` until start |
| `E6` | `remove` | owned + `saved` `edit_backups.py:105–111`, `:154–160` | store file deleted, row deleted | `E9` |
| `E7` | `remove` | same | row deleted, project copy left and named | `E9` |
| `E6`/`E7` | `prune` past `RETENTION_DAYS` | `:169–176`; `conflict` and `stored=0` excluded `:172–174` | store file + row deleted | `E9` |
| `E8` | `read`/`preview`/`restore` | digest or reachability `:113–121` | refusal; row unchanged | `E8` |
| `E8` | `remove`/`prune` | stored file already absent | row deleted (H2 fix) | `E9` |

### T6 — startup recovery of pending rows

`EditBackups.recover()` (`:95–100`) selects `state='pending'` rows and applies
`_recover` (`:80–93`) to each:

| From | Event | Guard / decision | Effect | To |
| --- | --- | --- | --- | --- |
| `E1` | `recover()` | store empty `:67–78`; stage absent `workspace.py:224–226` | `delete_edit_backup` `:88–89`; returns 0 | `E0` |
| `E2` | `recover()` | stage digest == `published_digest` `workspace.py:227–229` | stage unlinked, row deleted | `E0` |
| `E3` | `recover()` | stage digest != `published_digest` | `store_entry` moves it `workspace.py:230` | `E6` (`conflict` from `:92`) |
| `E4` | `recover()` | store already holds the same bytes | stage removed via the stored digest (H1 fix), settle from the store | `E6` |
| `E5` | `recover()` | stage absent, `find_stored_backup` finds it `workspace.py:259–270` | settle with `stored=True` `edit_backups.py:90–92` | `E6` |
| `E3`/`E4` | `recover()` | store raises (`ENOSPC`, `EEXIST`, non-regular stage) | warning `:83–86`; row and stage kept for the next start | unchanged |
| `E6`/`E7`/`E8` | `recover()` | not selected (`state != 'pending'`) | untouched | unchanged |
| any | `recover()` again | row no longer pending | no effect; no second store entry | unchanged |

## Invariants

1. **A selection is usable only while it is the newest, for the conversation it
   was created for, in the navigation epoch it saw, outside a reply.**
   `_check_workspace_selection` is the single predicate (`chat.py:779–789`) and
   is evaluated before the persist, after the authorize and after the database
   reads (`:796`, `:799`, `:807`). The conversation is matched by id plus
   `_navigation_epoch`, so a title rename's refreshed object for the same chat
   stays `CURRENT` while leaving and returning does not. Pinned by
   `test_expiry_before_persist_leaves_no_grant_behind`, the existing
   `test_workspace_selection_rejects_navigation_delete_and_replacement`,
   `test_workspace_newer_selection_supersedes_pending_picker` and
   `test_a_title_rename_does_not_expire_an_open_picker`.
2. **A persisted grant is only written for the captured conversation id.**
   `conversation_id = selection[1].id` is read before the awaits (`:797`) and
   used for the write (`:802`), so navigation cannot redirect the write. Pinned
   by `test_expiry_before_persist_leaves_no_grant_behind`.
3. **No grant is persisted when the selection expires before the write.**
   Checks `:796`/`:799` precede `:802` with no intervening `await`. Pinned by
   `test_expiry_before_persist_leaves_no_grant_behind` and
   `test_workspace_navigation_during_authorization`.
4. **A workspace change invalidates this conversation's file proposals and extra
   grants, whether or not the change completes.** `:804–806` run before the last
   check. Pinned by
   `test_workspace_change_expires_pending_edits_and_directory_grants`.
5. **A token is bound to its conversation and workspace.** `_review_edit` checks
   both (`:1150–1153`) and `set_workspace` filters by conversation id (`:804`).
   Pinned by `test_a_token_belongs_to_its_conversation` and
   `test_workspace_change_expires_pending_edits_and_directory_grants`.
6. **One approval runs at a time.** `review_edit` takes `artifacts.exclusive()`,
   which refuses a second caller (`artifacts.py:174–177`), and sets
   `is_generating` before any await (`:1165–1167`). Pinned by
   `test_a_second_approval_is_refused_while_the_first_writes` (exactly one
   `WorkspaceFiles.write` call, one backup, token gone).
7. **An approval never writes bytes the user did not review.** The replacement
   digest is checked twice, before staging and after it (`workspace.py:662`,
   `:677`), and the token's digest is what `edit_backups.replace` records as
   `approved_digest` (`chat.py:1180`). A mismatch leaves the file untouched and
   the token pending. Pinned by
   `test_an_external_change_refuses_approval_and_keeps_the_token`.
8. **A consumed token is dead; a refusable approval keeps it.** `CONSUMED` and
   `file_changed_published` pop the token (`:1189`, `:1187`); `file_changed` and
   other refusals do not (`:1185–1188`). Pinned by
   `test_an_external_change_refuses_approval_and_keeps_the_token`,
   `test_a_second_approval_is_refused_while_the_first_writes`, and the existing
   `test_published_edit_conflict_expires_approval`.
9. **An output is written at most once per journal key, and metadata is
   idempotent per `(reply, path, digest)`.** The journal is read before the write
   (`chat.py:1065–1076`), and the attachment lookup skips an existing row
   (`:1085–1088`). Pinned by
   `test_a_journal_replays_metadata_without_writing_the_file_again` (the write is
   patched to fail if it runs) and
   `test_a_replay_does_not_duplicate_an_attachment_that_already_exists`.
10. **A journal belongs to one conversation, one workspace identity and one
    digest.** `:1068`, `:1128`, `:1131`, `:1135`. Pinned by
    `test_recovery_is_bound_to_its_conversation_and_workspace` and
    `test_recovery_refuses_changed_bytes_and_keeps_the_record`.
11. **A finished save leaves no journal.** `:1096` removes it; the only surviving
    journals are `WRITTEN`/`UNRECORDABLE` outcomes. Pinned by
    `test_a_successful_output_leaves_one_attachment_and_no_journal`.
12. **A backup row exists before the bytes move, and is settled only from what is
    on disk.** `begin_edit_backup` precedes `write` (`edit_backups.py:41–44`);
    `_settle` without details re-derives the state (`:61–65`). Pinned by every
    case in `EditBackupCrashTests` (rows are created without a writer running).
13. **`recover()` handles every pending pair without touching a saved one.**
    `recover_replacement_stage` distinguishes never-published from displaced by
    digest (`workspace.py:227–230`); `find_stored_backup` covers the completed
    move; the query filters `state='pending'` (`edit_backups.py:98`). Pinned by
    the twelve `EditBackupCrashTests`, in particular
    `test_saved_records_are_never_revised_by_startup_recovery`.
14. **Recovery is idempotent.** A settled row is no longer selected and no store
    entry is created twice. Pinned by
    `test_recovery_is_idempotent_and_never_duplicates_a_stored_backup`.
15. **`conflict` means "the displaced bytes are not the reviewed bytes", computed
    identically on both paths** (`workspace.py:624`, `edit_backups.py:92`, with
    `approved_digest == expected_digest` from `chat.py:1180–1184`). Pinned by
    `test_displaced_content_that_changed_before_the_crash_is_a_conflict` and the
    existing `test_published_edit_conflict_expires_approval`.
16. **Conflict content and project copies are never removed by age; store copies
    are only removed explicitly.** `prune` skips `conflict`/`stored=0`
    (`edit_backups.py:172–174`); `remove` only forgets a project copy
    (`:154–160`). Pinned by the existing
    `test_retention_keeps_conflicts_and_removal_is_explicit` plus
    `test_prune_forgets_records_whose_files_are_already_gone`.
17. **A library token commits exactly once and the reviewed bytes are the frozen
    capture.** `Capture`/`Proposal` are frozen dataclasses (`library.py:39–53`);
    `import_capture` derives the file name and `expected_sha256` from that text
    (`:146–148`, `:186`); the token is popped only after the commit returns
    (`chat.py:1287`). Pinned by `test_approval_commits_the_snapshot_once` and the
    existing `test_disk_changes_during_ingestion_cannot_change_approved_content`.
18. **A failed library commit is retryable and cannot leave the writer latched.**
    `finally` clears `is_generating` (`chat.py:1300–1301`); the token survives a
    raise. Pinned by `test_a_failed_commit_keeps_the_token_and_clears_the_latch`.
19. **No machine in this document can be driven by model output alone.** A
    proposal only becomes an effect through `review_*`; the model cannot supply
    the approval flag (`test_library.py:229–243` pins the schema refusal) and
    `WorkspaceFiles.write` refuses an unreviewed replacement
    (`workspace.py:649–650`).
20. **The private store is not reachable through file tools.**
    `WorkspaceFiles.parent` refuses any path under `self.backups`
    (`workspace.py:468–469`); the store is 0700 (`:169`, `:177`). Pinned by the
    existing `test_file_tools_cannot_reach_the_store`.

## Hazards and open questions

| # | Severity | Where | Finding | Demonstrated |
| --- | --- | --- | --- | --- |
| H1 | fragile (fixed) | `edit_backups.py:67–78`, `workspace.py:180–184` | A cross-filesystem copy can publish the store file and leave its source under the stage name (E4). Recovery then tried to move the stage onto the same store name, got `EEXIST` for ever: the row stayed `pending`, the backup was never listed, and the displaced original stayed hidden in the project — the module docstring's promise ("never left hidden in the project or untracked"). Recovery now looks up the stored copy first and passes its digest, so a stage holding exactly those bytes is removed (content-checked) and the row settles from the store. Differing bytes still fail closed. | `test_leftover_stage_from_a_published_copy_is_removed_not_moved_again` (failed before the change: `0 != 1`), `test_a_differing_leftover_stage_is_not_discarded` |
| H2 | fragile (fixed) | `edit_backups.py:144–152`, `workspace.py:234–243` | If the whole `<store>/<group>` directory is gone, `remove_stored_backup` raised `FileNotFoundError` from the descriptor walk before it reached the tolerant `unlink`. `remove()` and `prune()` therefore aborted, leaving rows that could never be deleted, and `prune` stopped partway through its list. `_delete` now treats a missing group as already deleted (matching the project's "absent is a successful delete" convention). | `test_missing_store_group_does_not_block_removal`, `test_prune_forgets_records_whose_files_are_already_gone` (both errored before the change) |
| H3 | fragile | `chat.py:802–807` | The last selection check runs *after* `_repo.set_workspace`. If navigation or a reply starts during the database write/read, the caller gets "Workspace selection expired", the bridge shows an error and does not refresh, yet the grant is durable and this conversation's pending edits and extra grants have already been discarded. The conversation is then granted a directory the UI reported as not applied. | `test_expiry_after_the_persist_reports_failure_for_an_applied_grant` |
| H4 | fragile | `chat.py:1154–1156` vs `1272–1277` | The edit machine rejects a token without checking `is_generating`, while the library machine refuses a reject with `ChatBusy`. A reject that arrives while an approval is writing returns `{'rejected': True}` and pops the token, but the approval still publishes the file: the user is told the edit was rejected and finds it applied. Two concurrent callers are needed; the GUI bridge serializes review calls on one thread (`bridge.py:416–431`), so this is a latent inconsistency rather than a reachable UI bug. | `test_reject_during_an_approval_loses_the_rejection` vs `test_reject_while_committing_is_busy` |
| H5 | fragile | `chat.py:1150`, `:1170`, `:1180`, `:1220`, `:1226`, `:1234`/`:1239` | `_review_edit` binds the proposal to `self.conversation` at the guard, then re-reads it after two awaits for `workspace_files()` and for the backup's `conversation_id`. Opening an existing chat with an equal grant in that window publishes the edit and records its backup under the newly selected chat, so the originating chat cannot restore it ("Open the conversation that made this edit") and the other chat's list shows a file it never touched. `_restore_edit_backup` has the same shape. The GUI thread serializes calls, so this needs a programmatic caller. | `test_navigation_during_an_approval_files_the_backup_under_the_new_chat` |
| H6 | documentation-gap | `chat.py:1076–1084` | The journal is written *after* the file. Process death between the write and the journal leaves an output with no journal, no attachment and no tool result; nothing replays it, and a model retry creates a second copy. The limitation is stated for users (`docs/troubleshooting.md:156–157`), not in the module or the architecture map. Closing it needs a pre-write intent record (a planned stage name), i.e. a design change, not a guard. | Not committed; the window is between two statements and needs process death to observe. Would confirm with a subprocess that `os._exit`s between them. |
| H7 | documentation-gap | `chat.py:1120–1140` | `recover_output` has no shipped caller: `bridge.py` exposes no action for it, so the real recovery path is repeating the identical request (same journal key). It also assumes the originating reply still exists (`next(...)` at `:1137–1138` raises `StopIteration`, surfacing as `RuntimeError`), and a journal whose conversation was deleted is unreachable and never cleaned (the same gap sm-12 records as an `unrecognized` layout). | `test_recovery_is_bound_to_its_conversation_and_workspace` pins the guards; the missing-reply and orphan-journal cases are read-only findings. |
| H8 | fragile | `repository.py:570–578` | `settle_edit_backup` updates every column with only `WHERE id=?`, and `mark_edit_backup_restored` has no state guard either. Correctness rests on `recover()` selecting `state='pending'` and on one desktop owning the database, so a delayed settle (a shielded task that outlives its caller) could rewrite an already saved row. | `test_saved_records_are_never_revised_by_startup_recovery` pins the recovery half only; the unguarded update itself needs a concurrent caller to demonstrate. |
| H9 | fragile | `edit_backups.py:51–52` | If the settle write fails after a successful publication, `replace` raises with the row still `pending`. The backup is on disk and the file is published, but in the same process the token is now unretryable (`file_changed` on the next approval) until `recover()` runs at the next start. The user sees a storage error for an edit that happened. | Not committed; would need `settle_edit_backup` to raise while the process stays alive. Its recovery half is `test_exchanged_stage_is_moved_to_the_store_and_settled`. |
| H10 | documentation-gap | `workspace.py:196–209`, `:598–630` | Two fallback shapes are not representable. (a) `keep_beside` falling through every candidate returns the *stage* path, so `location` names a hidden `-edit` entry that only that record can read. (b) Process death after `keep_beside` but before the settle leaves a visible `name.backup` with no row; recovery deletes the pending row, so the copy is neither listed nor restorable (visible and untracked). Both preserve the bytes, which is the priority, but neither is described in `workspaces.md`. | (a) is pinned by the existing `test_store_and_rename_failure_still_names_the_retained_entry`; (b) is a crash window, not demonstrated. |
| H11 | documentation-gap | `workspace.py:246–256` | `EditBackups.read` promises "no longer matches the saved original", but a missing store file or group surfaces as `FileNotFoundError` naming a *relative* path (`store`), not the location the user knows. Restore and preview therefore report an OS error instead of the documented refusal. Removal of such a row is fixed under H2. | `test_missing_store_group_does_not_block_removal` pins the current error before removal. |
| H12 | none-found | `workspace.py:45–70` | The abandoned-stage sweep is bounded correctly: it only touches names without `-edit`, regular files, mode 0600, the process's uid and older than 24 h, at most once per directory per process. A live write is bounded by 2.56 MB, so a stage cannot age 24 h unless the filesystem stalls; `-edit` stages are never collected, which is required because one may hold the user's original. The per-process memo can skip one sweep when a directory inode is reused. | Existing `test_old_abandoned_stage_is_collected_before_new_output` and `test_process_death_during_write_leaves_no_partial_final_and_stage_is_hidden`. |
| H13 | none-found | `chat.py:772`, `:779–789` | The selection token is never cleared after use, so a consumed selection is `CURRENT` again and the same `(path, selection)` call can be replayed. It cannot widen access: the grant is recomputed from the path, persisted for the same conversation, and any newer picker or `set_workspace(None)` supersedes it. The token is matched by conversation id plus `_navigation_epoch`; an earlier identity comparison failed closed for an automatically retitled chat (bra-lfou). | Covered by the existing `test_workspace_newer_selection_supersedes_pending_picker` and `test_navigation_still_expires_a_picker_whose_chat_was_renamed`. |

### Answers to the bead's focus questions

- **Which on-disk/DB pairs does `recover()` handle?** S5's E1→`E0`, E2→`E0`,
  E3→`E6`, E4→`E6`, E5→`E6`; E6–E9 are not selected. The discriminator is the
  digest of the named stage (`workspace.py:227–230`) plus the store lookup
  (`:259–270`), which is exactly what distinguishes "never published" from
  "displaced original waiting".
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
  the authorize (`:780`, nothing persisted: safe) and after the database
  read/write (`:788`, grant already persisted: H3). Between `:780` and the
  persist there is no await. After `:788` there is no await before the in-memory
  assignment, so the display update itself cannot interleave.
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
  this document plus the 32 tests.
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

Run from the repository root:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_11_workspace_files.py -v
```

Result: `Ran 32 tests` — `OK` (32/32). Four classes:
`EditBackupCrashTests` (12, the S5/S6 pairs and T6), `WorkspaceSelectionTests`
(9, S1/S2 and T1/T2), `OutputJournalTests` (6, S4 and T4),
`LibraryProposalTests` (5, S3 and T3). The runner also reports no writes outside
its scratch XDG directories.

Pre-existing focused tests, each with the same runner:

| Command (`-p …`) | Result |
| --- | --- |
| `test_workspace.py` | `Ran 48 tests` — `OK` |
| `test_workspace_atomic_publish.py` | `Ran 5 tests` — `OK` |
| `test_workspace_suggestions.py` | `Ran 4 tests` — `OK` |
| `test_edit_backups.py` | `Ran 11 tests` — `OK` |
| `test_library.py` | `Ran 17 tests` — `OK` |
| `test_directory_access.py` | `Ran 9 tests` — `OK` |

Static checks: `.venv/bin/ruff check src/blackrose/edit_backups.py
tests/test_sm_11_workspace_files.py` — `All checks passed!`;
`.venv/bin/python -m mypy` — `Success: no issues found in 19 source files`.

The two source changes were verified to fail before them:
`test_leftover_stage_from_a_published_copy_is_removed_not_moved_again`
(`0 != 1`), `test_missing_store_group_does_not_block_removal` and
`test_prune_forgets_records_whose_files_are_already_gone` (both
`FileNotFoundError`), with the original `edit_backups.py` restored from a copy.

Not run: `scripts/check_health.py` (full offline gate), the `--gui` probe and the
native workspace/library probes. The changes are two branches inside
`EditBackups`'s recovery and removal helpers, already exercised by the offline
suite; the GUI probes would add no coverage for them.
