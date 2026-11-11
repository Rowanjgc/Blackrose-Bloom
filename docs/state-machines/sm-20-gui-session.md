# SM-20: Desktop session lifecycle, generation slot, draft recovery and view tokens

## Scope and ownership

| Surface | Location |
| --- | --- |
| Boot, availability and status projection | `src/blackrose/gui/session.py:99-101`, `116-117`, `144-177`, `327-345`, `1844-1861` |
| The single generation slot | `session.py:97`, `973`, `983`, `593`, `1163-1173`, `1204-1236`, `1238-1300`, `1310-1505` |
| Reply epoch and transcript snapshots | `session.py:103`, `422-453`, `501-529`, `227-293` |
| Composer draft, debounce and sending envelope | `session.py:106-115`, `179-225`, `227-293`, `924-975`, `1468-1472`, `1519-1537` |
| Durable draft rows | `src/blackrose/drafts.py:43-139`; `src/blackrose/storage/migrations.py:71-87` |
| Native permission interlock | `session.py:125`, `940-966`, `1037-1058`, `1089-1124` |
| UI view tokens and action acceptance | `session.py:94-95`, `531-559`, `565-599`, `405-420`, `501-529` |
| Window shutdown | `session.py:353-377`; `src/blackrose/gui/bridge.py:800-802`; `gui/app.py:107-119` |

One `Session` per window (`gui/app.py:69-98`), constructed on the window thread and
driven entirely from one asyncio loop (`gui/bridge.py:41-69`). Every page call is a
coroutine submitted to that loop (`bridge.py:55-65`); `update_draft`, `stop` and
`remove_attachment` are plain functions run on it (`bridge.py:325-329`). GTK/WebKit
stays on its own thread and the emitter delivers batches from a third
(`bridge.py:72-133`). Therefore:

* every attribute below is written **only** on the loop thread, so a read-modify-write
  without an `await` in between is atomic with respect to every other page call;
* two page calls are genuinely concurrent — each occupies its own coroutine and they
  interleave at `await` points — so every multi-`await` guard is a real window;
* the page owns nothing durable. It echoes view tokens and event ids; Python decides
  admission, persistence and settlement.

Per [architecture](../architecture.md#modules-and-invariants) `gui/session.py` owns
"drafts, attachment admission, active reply state, complete reload snapshots,
cancellation recovery before persistence, UI view tokens, transient action/busy
guards and animation-repair scheduling". The turn itself belongs to `turns.py`, the
durable `ui_events` row to `interactive_answers.py`/`storage/` (see SM-06), and the
two repair claims to `diagram_repairs.py` / `animation_recovery.py` (SM-08, SM-09).

Navigation is deliberately *allowed* while a reply is generating as long as the reply
belongs to the conversation being left (`session.py:620-625`): the reservation is
global, the transcript is per conversation, and one `ActiveReply` records its own
`conversation_id` (`session.py:66-80`, `400-403`).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `_ready` | `session.py:99` | `bool` | a provider model is loaded; the only admitting state | `boot` `:338`; `select_model` `:1850`, `:1853`; never reset |
| `_boot_task` | `:100` | `asyncio.Task \| None` | the running `boot()` coroutine; the "startup is still in progress" flag | `boot` `:328`, `:345` |
| `_quit_requested` | `:101` | `bool` | window is closing; refuses new page work | `close` `:354`; never cleared |
| `_status_state`, `_status_detail` | `:116-117` | `str`, `str \| None` | last display line; `'Backend unavailable'` is the one value read semantically (`:174-175`) | `status()` `:144-149` and ~20 call sites |
| `_generation` | `:97` | `asyncio.Task \| None` | the one admitted turn, retry or repair drain | set `:593`, `:973`, `:983`, `:1172`; cleared `:1031`, `:1169`, `:1234`, `:1298`, `:1503` |
| `_active_reply` | `:102` | `ActiveReply \| None` | the streaming reply and its renderer/blocks | `_send` `:1333`, `:1475`; `_restore_unstored_draft` `:1525` |
| `_reply_key` | `:98` | `int` | monotonic reply identity carried by page events | `_send` `:1322` |
| `_reply_epoch` | `:103` | `int` | "a reply started, settled or was restored" counter for snapshot readers | `:1335`, `:1479`, `:1526` |
| `_request_state` | `:118` | `str \| None` | `pending` while a reply streams, else the settled/unsaved outcome | `:1340`, `:1478`, `:446`, `:611` |
| `_unsaved_outcomes` | `:121` | `dict[int, {state, error}]` | pre-admission refusals that have no durable turn | `:614`, `:1453`, `:1465`; popped `:1334` |
| `_permission_pending` | `:125` | `bool` | a native approval dialog is open; **every** admission refuses | `submit` `:941`/`:966`; `_authorize_outbound` `:1046`/`1045` |
| `_can_retry` | `:126` | `bool` | the shown chat has a stored user request | `:443`, `:1493` |
| `_draft_owner` | `:110` | `int \| None` | the conversation the in-memory composer belongs to | `_load_draft` `:239`, `:272`; `delete_conversation` `:676` |
| `_draft_text`, `_draft_refs`, `_attachments` | `:106-107`, `:92` | `str`, `list[dict]`, `list[PendingAttachment]` | the composer | `update_draft` `:180-182`; `_load_draft` `:273`; `_restore_unstored_draft` `:1530-1534`; `_send` `:1318` |
| `_draft_sending` | `:111` | `{text, refs, attachments, baseline} \| None` | a submitted request whose turn may not be stored yet | `submit` `:969`; `_load_draft` `:274-278`; cleared `:1471`, `:1531`, `:676` |
| `_draft_save` | `:112` | `TimerHandle \| None` | the 0.5 s debounce | `_schedule_draft_save` `:194-198`; `_start_draft_save` `:201` |
| `_draft_save_task` | `:113` | `asyncio.Task \| None` | the save in flight | `:202` |
| `_draft_lock` | `:114` | `asyncio.Lock` | serialises saves so the last write is the newest state | `_save_draft` `:214` |
| `_draft_save_failed` | `:115` | `bool` | latched warning so one failure is announced once | `:220`, `:224` |
| `_ui_views` | `:94` | `dict[block_id, hex token]` | the view identity the page must echo | `_ui_payload` `:540`; cleared `:406`, `:449`, `:502` |
| `_ui_action_pending` | `:95` | `bool` | interlock covering acceptance through task creation | `ui_action` `:581`, `:599` |
| `_diagram_repair_task`, `_animation_repair_task` | `:127-128` | `asyncio.Task \| None` | the two deferred drains | `diagram_failed` `:1192`; `animation_failed` `:1160`; cleared in each drain's `finally` |
| `_pending_diagram_failures`, `_pending_animation_failures` | `:129-130` | dicts | queued page-reported failures, bounded at 8 | intake `:1190`, `:1156`; `stop` `:1068`, `:1075`; drains |
| `_animation_recovery_states`, `_repairing_animation_id` | `:131-132` | `dict`, `int \| None` | mirror of the durable claim for the page | `:1154`, `:1266`, `:1278`, `:1296` |
| `composer_drafts.text/refs/attachments/sending` | `migrations.py:74-81` | `TEXT`/JSON | the persisted composer and envelope | `drafts.py:83-104` |
| `composer_draft_files.data` | `migrations.py:82-87` | `BLOB` keyed by digest | attachment copies; cascade with the row | `drafts.py:98-103` |

`_status_state` is a free string. Only one comparison is semantic — `availability`
reports "Unavailable" only for the exact literal `'Backend unavailable'`
(`session.py:174-176`); any other unready status reads as "Loading".

## States

### A. Availability (single writer: `boot`/`select_model`/`close`)

| State | Predicate |
| --- | --- |
| `Starting` | `_boot_task is not None and not _ready` |
| `Ready` | `_ready` |
| `Unavailable` | `_boot_task is None and not _ready and _status_state == 'Backend unavailable'` |
| `Aborted` | `_boot_task is None and not _ready and _status_state != 'Backend unavailable'` — boot ended without either outcome (cancelled by `close`, or raised outside the caught region) |

`Closing` (`_quit_requested`) is an orthogonal, absorbing gate rather than a state;
`_ready` is never cleared, so `Closing ∧ Ready` is reachable and is the state in which
`submit`/`retry` are still admitted (H4).

### B. Generation slot (`_generation`, `_active_reply`, `controller.is_generating`)

| State | Predicate |
| --- | --- |
| `Idle` | `_generation is None` |
| `Settled` | `_generation` is a finished task; treated as idle by `_busy` `:1035`, `stop` `:1078`, `settle` `:1105` |
| `Reserved` | live task, `_active_reply is None` — `retry` before `_send`, or `_send`'s prologue |
| `Streaming` | live task, `_active_reply` set, `_request_state == 'pending'` |
| `Settling` | live task, `_active_reply` cleared (`:1475`), `_generation` not yet cleared (`:1503`) |
| `Repairing` | live task is `_repair_diagrams`/`_repair_animations` (owner acquired at `:1172`) |
| `Stopping` | a cancelled task still unwinding through its recovery path |

### C. Composer draft

| State | Predicate |
| --- | --- |
| `Unowned` | `_draft_owner is None` — the composer is being replaced (`:239`, `:676`) |
| `Clean` | owner set, `_draft_save is None`, `_draft_save_task` done or absent |
| `Dirty` | `_draft_save` armed (the 0.5 s debounce) |
| `Saving` | `_draft_save_task` running, or `_draft_lock` held |
| `SendPending` | `_draft_sending is not None` and `not last_request_stored` |
| `SendAdmitted` | `_draft_sending is not None` and `last_request_stored` — cleared by `_send`'s `finally` `:1469-1472` |
| `SaveFailed` | `_draft_save_failed` latched; the composer still works, only durability is lost |

### D. UI view tokens

| State | Predicate |
| --- | --- |
| `Unissued` | no `_ui_views` entry for the block |
| `Issued` | `_ui_views[block_id] == token` for the token the page last received |
| `Rotated` | the snapshot was rebuilt, so every token the page holds is stale |

### E. Permission

`Clear`; `Pending` (`_permission_pending`), entered from `submit`'s directory prompt
and from `_authorize_outbound`, and left only by the `finally` (`:966`) or the
executor future's done callback (`:1044-1045`).

## Events

| Id | Event | Origin | Note |
| --- | --- | --- | --- |
| E1 | Page ready → first boot | `bridge.py:206-211` | once per window (`_booted`) |
| E2 | Local recovery and transcript published | `session.py:330-334` | before the provider loads |
| E3 | Provider load succeeds / fails | `:337-342` | failure is caught, not raised |
| E4 | Model selected from the Models dialog | `:1844` | the only exit from `Unavailable` |
| E5 | Submit text and/or attachments | `:924` | slash commands return before admission |
| E6 | Retry the latest stored request | `:977` | reserves the slot synchronously |
| E7 | UI action accepted by the page | `:577` | durable acceptance first, turn second |
| E8 | Stop (button, key, renderer termination) | `:1060`, `:1107` | also cancels queued repairs |
| E9 | Close / window closed | `:353`; `bridge.py:800-802` | cancels boot, repairs, generation, then flushes |
| E10 | The generation task settles | `:1427-1505` | complete, refused, cancelled, exception |
| E11 | Navigation or redraw | `:620`, `:640`, `:662`, `:496` | redraws rotate every view token |
| E12 | Draft debounce fires / flush | `:201`, `:204` | flush before an owner change and at close |
| E13 | Directory approval dialog returns | `:951` | runs off-loop; the answer is revalidated |
| E14 | Outbound approval dialog returns | `:1050-1052` | executor future + done callback |
| E15 | Browser reports a diagram/animation failure | `:1175`, `:1126` | queued; never takes the slot directly |
| E16 | A repair drain starts, loops or finishes | `:1204`, `:1238` | waits for boot and then for the slot |
| E17 | A draft load completes (possibly into a moved composer) | `:227-293` | re-reads the selected conversation |
| E18 | Process death | — | the only recovery is `composer_drafts` plus the on-disk turn rows |

## Transition table

### A. Availability

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| — | E1 | — | `_boot_task := current_task()` (`:328`); `_status_state` was `'Starting…'` (`:116`) | `Starting` |
| `Starting` | E2 | `recover_interrupted`/`ensure_conversation`/`refresh_conversations`/`render_transcript`/`_load_draft` all return (`:330-334`) | transcript and sidebar emitted; the composer owner is set before `_ready` | `Starting` (recovered) |
| `Starting` (recovered) | E2 | — | `status('Loading default model…')` (`:335`) | `Starting` (loading) |
| `Starting` (loading) | E3 success | `controller.start()` returns (`:337-338`) | `_ready := True`; `status('Ready')` | `Ready` |
| `Starting` (loading) | E3 failure | `except Exception` (`:340-342`) | notice; `status('Backend unavailable', detail)`; `_ready` stays `False` | `Unavailable` |
| any | E2/E3 | boot body raises outside the inner `try` (`:330-334`, `:343`) | `finally` clears `_boot_task`; the exception propagates to the caller (`bridge.py:211` ignores the future) | `Aborted` |
| `Starting` | E9 | boot task not done (`:365-368`) | `cancel()` and await; `finally` clears `_boot_task`; `_ready` stays `False` | `Aborted` |
| `Unavailable` | E4 success | `controller.select_model` returns (`:1849-1851`) | `_ready := True`; `status('Ready')` (`:1860`) | `Ready` |
| `Unavailable` | E4 `PreferenceSaveError` | model is loaded, only the default failed (`:1852-1854`) | `_ready := True`; error returned to the page | `Ready` |
| `Unavailable` | E4 `OSError`/other | `:1855-1858` | error returned; `status('Backend unavailable')` (`:1860`) | `Unavailable` |
| `Ready` | E4 entry | `self._busy() or controller.is_generating` (`:1845`) | refusal, nothing written | `Ready` |
| `Ready` | E4 | — | `status('Loading …')` but `_ready` is unchanged, so admission stays open while the switch runs (`:1847-1849`) | `Ready` |
| `Ready`/`Unavailable` | E9 | — | `_quit_requested := True` (`:354`); `_ready` unchanged | `Closing` |
| `Aborted` | E4 | — | `select_model` still runs and can reach `Ready`; nothing prevents it | `Ready` |

### B. Generation slot

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Idle` | E5 | slash command (`:926-931`) | draft text cleared, `run_command`; **no slot** | `Idle` |
| `Idle` | E5 | nothing to send (`:933`) | `{'clear': False}` | `Idle` |
| `Idle` | E5 | `not _ready or is_generating or _busy()` (`:935`) | bell | `Idle` |
| `Idle` | E5 | `_permission_pending` (`:938`) | silent `{'clear': False}` | `Idle` |
| `Idle` | E5 | approval branch (`:940-966`) | `_permission_pending := True`; revalidate conversation+workspace after the dialog (`:954-959`); refuse if busy returned (`:960`) | `Idle` (permission `Pending` in between) |
| `Idle` | E5 | all pass | `_draft_sending := {…}`, composer cleared (`:969-972`), `_generation := create_task(_send)` (`:973`), immediate save (`:974`) | `Reserved` |
| `Idle` | E6 | `not _ready or _busy() or is_generating or _permission_pending` (`:979`) | bell | `Idle` |
| `Idle` | E6 | — | `_generation := create_task(_retry_latest())` **before any read** (`:983`) | `Reserved` |
| `Idle` | E7 | `_ui_action_pending or _busy() or is_generating or not _ready or _permission_pending or _quit_requested` (`:578-580`) | `{'ok': False, 'error': 'Wait for the current reply to finish'}` | `Idle` |
| `Reserved` | `_send` enters | conversation selected (`:1312-1315`, else raises before the `try`) | `reply_start`, `_active_reply`, `_reply_epoch += 1`, `_request_state := 'pending'` (`:1320-1341`) | `Streaming` |
| `Reserved` (retry) | reads finish | request found, `ui_context is None`, attachments readable, grants okay (`:990-1020`) | `_send(..., preserve_draft=True)` | `Streaming` |
| `Reserved` (retry) | no request / form request / refusal | `:992-998`, `:1003`, `:1019` | flash/notice; `finally` clears the slot (`:1030-1032`) | `Settled` |
| `Reserved` | E7 | acceptance succeeded, but the view rotated, quit, permission, or a live slot appeared (`:586-589`) | `finish(event_id, 'interrupted')`, refusal raised (`:590-592`); `finally` clears `_ui_action_pending` | `Idle` |
| `Reserved` | E7 | all pass | `_generation := create_task(_send(..., ui_event_id))` (`:593-594`) | `Reserved`→`Streaming` |
| `Reserved` | E8 | — | the cancel is queued with `call_soon` so `_send` reaches its recovery scope first (`:1084-1086`) | `Stopping` |
| `Streaming` | E10 stored | `controller.send` returns a turn (`:1438-1440`) | `reply_end`, outcome from the message state | `Settling` |
| `Streaming` | E8 | — | `renderer.close()`, cancelled reply_end; if `last_completed_turn` is set the stored answer is shown instead (`:1441-1446`); `_unsaved_outcomes` only when not stored (`:1452-1453`); `_restore_unstored_draft` (`:1454`) | `Settling` |
| `Streaming` | E10 exception | `:1457-1467` | error reply_end, `_unsaved_outcomes` (`:1464-1465`), `_restore_unstored_draft` (`:1466`) | `Settling` |
| `Settling` | — | owner matches and the request was stored (`:1469`) | `_draft_sending := None`, save (`:1471-1472`) | `Settling` |
| `Settling` | — | `ui_event_id` | `interactive_answers.finish(event_id, outcome)` (`:1473-1474`) | `Settling` |
| `Settling` | — | — | `_active_reply := None` (`:1475`); `_request_state := outcome` for the visible chat (`:1476-1478`); `_reply_epoch += 1` (`:1479`) | `Settling` |
| `Settling` | — | — | summary read, metrics decode speed cleared (`:1480-1501`); `manage_generation` → `_generation := None`, `status('Ready')` (`:1502-1504`) | `Settled` |
| `Settling` | navigation completes first | `selected.id != active.conversation_id` (`:1477`) | `_request_state` keeps the other chat's value; the next redraw repairs it (`:444-446`) | `Settled` |
| `Idle` | E15 diagram | `config.diagram_repairs != 0`, payload bounds, `_quit_requested`, `(not _ready and _boot_task is None)`, `_permission_pending` (`:1176-1183`) | queue key (≤ 8, `:1188`), `setdefault`, spawn drain if none live (`:1190-1192`) | `Idle` + queued |
| `Reserved`/`Streaming`/`Settling` | E15 diagram | same guards pass | queued only; the drain waits for the slot (`:1207-1210`) | unchanged |
| `Idle` | E15 animation | cloud repair available, bounds, `_quit_requested`, `_ready`, `_permission_pending`, scope revalidated (`:1128-1144`) | queue, `status: 'queued'`, spawn drain if none live (`:1156-1161`) | `Idle` + queued |
| any | E16 start | boot task live and not the drain itself (`:1207-1209`) | `await shield(boot)` | unchanged |
| any | E16 claim | `_generation` live and not this task (`:1165-1167`) | `await shield(owner)`; then clear it if still current and done (`:1168-1169`) | unchanged |
| any | E16 claim | `not _ready or _quit_requested or _permission_pending` (`:1170`) | `return False`; diagrams emit `unavailable` unless quitting (`:1211-1214`); animations `unavailable` in their `finally` (`:1291-1294`) | queue drained |
| `Idle`/`Settled` | E16 claim | guards pass | `_generation := current_task` (`:1172`) | `Repairing` |
| `Repairing` | E16 loop | failures queued | repair/claim/appended turn; animations call `_send(..., manage_generation=False)` (`:1273`) | `Repairing` |
| `Repairing` | E16 end | — | clear queue, clear `_repairing_animation_id`, `_generation := None` only if it is still this task (`:1233-1236`, `:1297-1300`) | `Settled` |
| `Repairing` | E8 | `_generation` is the drain | cancel queued with `call_soon`; the drain's `finally` runs | `Stopping`→`Settled` |
| `Repairing` (queued only) | E8 | drain not yet started | `_pending_*` cleared with terminal page updates (`:1063-1077`), drain cancelled | `Idle` |
| any | E11 while `Streaming` | `_busy() and not (_active_reply and is_generating)` (`:622`) | navigation refused with a bell | unchanged |

The five slot writers all sit behind the same admission triple — `_ready`,
`not is_generating`, `not _busy()` — and `_wait_for_repair_slot` performs its
test-and-set with **no `await` between `:1169` and `:1172`**, so two drains waking
from the same `shield` serialise instead of both claiming. This is the invariant
pinned by `test_claimants_take_the_single_slot_one_at_a_time`.

### C. Composer draft

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unowned` | boot recovery completes | — | `_draft_owner := conversation.id` (`:272`) **before** `_ready` (`:338`) | `Clean` |
| any | E5 changed text | `refs` given | `_selected_refs` filter (`:182`, `:186-190`), `_schedule_draft_save()` (`:183`) | `Dirty` |
| `Dirty` | debounce fires | — | `_draft_save := None`, `_draft_save_task := create_task(_save_draft)` (`:200-202`) | `Saving` |
| `Dirty` | E12 flush | — | timer cancelled, `_save_draft` awaited (`:204-209`) | `Saving` |
| `Saving` | row written | `_draft_owner` set (`:215`) | `drafts.save(owner, text, refs, attachments, sending)` (`:218-219`) | `Clean` |
| `Saving` | `_draft_owner is None` | `:215-216` | **no write at all**, silently | `Unowned` |
| `Saving` | `OSError`/`sqlite3.Error`/`StorageError` | `:221-225` | one warning notice, latched | `SaveFailed` |
| `SaveFailed` | next successful save | `:220` | latch cleared | `Clean` |
| `Clean`/`Dirty` | E5 admission | all guards pass | `_draft_sending := envelope` with `baseline = _last_message_id` (`:969-970`); composer text cleared (`:971-972`) | `SendPending` |
| `SendPending` | E10 stored | `_draft_owner == active.conversation_id` and `last_request_stored` (`:1469`) | `_draft_sending := None`, save (`:1471-1472`) | `Clean` |
| `SendPending` | E10 refused/cancelled | `not last_request_stored` and owner matches (`:1520-1522`) | attachments restored, `_draft_sending := None`, sent text prepended to later typing (`:1530-1535`) | `Clean` |
| `SendPending` | E10 refused/cancelled | owner is a different chat (`:1522-1524`) | no in-memory restore; the row is the recovery point | unchanged |
| `SendPending` | E8 with `preserve_draft` | retry, UI action or animation repair (`:1454`, `:1466`) | transcript redrawn, composer untouched, `_draft_sending` not cleared by the restore | unchanged |
| `SendPending` | E17 the chat is reopened | `saved['sending']` present and no live reply and `not request_was_stored` (`:256-258`) | envelope merged in front of the saved text/refs/attachments (`:257-258`) | `Clean`→`Dirty` |
| `SendPending` | E17 the chat is reopened | `request_was_stored` (`:295-298`) | envelope dropped, row rewritten as shown (`:281-283`) | `Clean` |
| `SendPending` | E17 the live reply is visible again | `live and saved['sending']` (`:274-278`) | envelope re-armed from the reply's own text | `SendPending` |
| any | E11/E9 | `_flush_draft` before the switch (`:629`, `:644`, `:376`) | the current composer is saved under its owner | `Clean` |
| any | E11 delete | — | `_draft_owner, _draft_sending := None, None` (`:676`); the row cascaded | `Unowned` |
| `Unowned` | E17 completes | conversation unchanged (`:264`) | owner/text/refs/attachments installed (`:272-273`), row rewritten (`:281-283`) | `Clean`/`Dirty` |
| `Unowned` | E17, the selection moved | `:264-265` | return without installing anything; the newer load owns the composer | `Unowned` |
| any | E17 returned while a reply settled | `epoch != _reply_epoch and saved['sending']` (`:266`) | the whole load restarts so the envelope decision is re-made | restart |

### D. UI view tokens

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unissued` | `_ui_payload` for a non-markdown block | — | `_ui_views.setdefault(block_id, token_hex(16))` (`:540`) | `Issued` |
| `Issued` | E11 complete redraw | `render_transcript` `:406`, `_transcript_payload` `:449`, `_redraw` `:502` | the whole map is cleared, then re-issued for the new snapshot | `Rotated` |
| `Rotated` | page replays an old token | `_ui_views.get(block_id) != view_id` (`:549`) | `ValueError('UI view is obsolete; reload this conversation')` | `Rotated` |
| `Issued` | page event for another chat | `selected.id != envelope.conversation_id` (`:547-548`) | `ValueError('UI event belongs to another conversation')` | unchanged |
| `Issued` | `ui_state` | token valid before and after the read (`:555-558`) | state persisted with the page's sequence | unchanged |
| `Issued` | `ui_action` entry | `_ui_action_pending or _busy() or is_generating or not _ready or _permission_pending or _quit_requested` (`:578-580`) | refusal | unchanged |
| `Issued` | `ui_action` accepted | token valid after acceptance and no quit/permission/generation appeared (`:586-589`) | `_generation := create_task(_send(...))` | `Issued` |
| `Issued` | `ui_action` view rotated or state changed during acceptance | `:586-592` | `finish(event_id, 'interrupted')`, refusal; no task | `Issued` |

`_ui_payload` issues ids with `setdefault`, so within one snapshot a block keeps one id
(`:540`); only a complete redraw rotates it. `earlier_messages` deliberately does not
clear the map (`:477-494`): paged-in history accumulates beside the current window
until the next complete redraw replaces both.

### E. Permission

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Clear` | E5 with `_approve_directories` | slot free (`:935`) | `_permission_pending := True` (`:941`) | `Pending` |
| `Pending` | E13 declines | `:952-953` | `{'clear': False}` in the `finally` | `Clear` |
| `Pending` | E13 accepts | conversation and workspace unchanged (`:954-956`) | grants recorded, then the busy check (`:960`) | `Clear` |
| `Pending` | E13 accepts, the world moved | `:955-958` | notice "Workspace changed while permission was requested" | `Clear` |
| `Clear` | outbound request during a reply | `_approve_outbound` configured (`:1433`) | `_permission_pending := True` (`:1046`), status "Waiting for your approval…" | `Pending` |
| `Pending` | E14 returns (any answer, any exception) | executor future done (`:1044-1051`) | callback clears `_permission_pending`; the awaiting reply then sets `status('Generating…')` unless Stop cancelled it (`:1052-1053`) | `Clear` |
| `Pending` | E8 | — | the reply is cancelled; the dialog stays open and `_permission_pending` stays `True` | `Pending` |
| `Pending` | E14 | — | the late answer is discarded with the cancelled reply | `Clear` |

## Invariants

1. **Admission requires `Ready`.** Every creator of `_generation` runs after
   `_ready` was observed true, and `_ready` is only set by a successful boot or model
   selection. Pinned by `test_nothing_is_admitted_until_a_transcript_and_model_are_ready`.
2. **The composer owner exists before the first admission.** `boot` installs
   `_draft_owner` (`:272`) before `_ready` (`:338`), so a booting session can never
   accept a submit. Pinned by the same test.
3. **One slot, one owner.** At most one live task is ever `_generation`, and no
   admission path overwrites a live one. Pinned by
   `test_claimants_take_the_single_slot_one_at_a_time`,
   `test_a_queued_repair_waits_for_the_reply_and_never_shares_the_slot` and
   `test_the_action_interlock_closes_the_other_admissions`.
4. **A refusal writes nothing.** `_wait_for_repair_slot` returns `False` without
   touching `_generation` (`:1170-1171`). Pinned by
   `test_refusals_never_take_or_clobber_the_slot` and
   `test_a_refusal_after_waiting_leaves_the_slot_free`.
5. **Cancelling a waiter never cancels the owner.** The wait is `shield`ed
   (`:1167`). Pinned by `test_a_cancelled_claimant_does_not_cancel_the_owner`.
6. **A finished task left in the field is inert.** `_busy`, `stop`, `settle` and the
   next claimant all treat `done()` as free (`:1035`, `:1078`, `:1105`, `:1168`).
   Pinned by `test_a_finished_slot_is_cleared_before_the_next_claim` and
   `test_busy_covers_the_action_interlock_and_a_live_task`.
7. **`_active_reply` is cleared before the slot is released.** `:1475` precedes
   `:1503`, and each drain clears its own task in a `finally`, so `Streaming` never
   coexists with `Idle`. Asserted throughout `GenerationSlotTests`.
8. **Stop reaches the task before the task runs.** `submit`/`retry` create the task
   and return; `stop` queues the cancel with `call_soon` (`:1084-1086`) and `close`
   yields once (`:371-372`), so `_send`/`_retry_latest` always enter their recovery
   scope first. Pinned by
   `test_retry_reservation_is_stoppable_before_it_reads_anything` and
   `test_stop_immediately_after_submit_recovers_draft` (existing).
9. **Stop is a no-op on an idle session.** `stop` rings the bell and returns `False`
   (`:1078-1082`). Pinned by `test_stop_with_no_work_only_rings_the_bell`.
10. **Queued repairs never outlive their conversation's session.** `stop` and `close`
    clear both queues and emit a terminal update for every diagram key it drops
    (`:1063-1077`). Pinned by
    `test_stop_cancels_a_queued_repair_and_releases_the_slot` and
    `test_close_cancels_queued_repairs_and_marks_the_session_closed`.
11. **A refusal before admission never consumes the composer.** The envelope exists
    from `submit` until the turn is stored or restored (`:969`, `:1469`, `:1531`).
    Pinned by the existing `test_composer_drafts.test_rejected_send_keeps_request_and_later_typing`
    and `test_stop_before_persistence_restores_question_and_attachment`.
12. **A submitted request survives navigation.** Leaving a streaming reply flushes
    the envelope to that chat's row (`:629`, `:218-219`) and the in-memory copy is
    dropped (`:274-278`); reopening re-validates and merges it. Pinned by
    `test_leaving_a_reply_parks_its_envelope_on_its_own_chat`.
13. **Save order cannot regress.** The newest state is only read inside
    `_draft_lock` (`:214-219`), and `_flush_draft` takes the same lock, so a late
    timer or a stray task writes the current composer. Pinned by
    `test_the_debounced_save_always_writes_the_newest_state`.
14. **No owner, no write.** `_save_draft` refuses without `_draft_owner`
    (`:215-216`). Pinned by `test_a_save_is_refused_while_the_composer_has_no_owner`.
15. **A view token is valid only for its snapshot.** Any complete redraw invalidates
    every issued token, and both `ui_state` and `ui_action` verify before and after
    their awaits (`:555`, `:558`, `:586`). Pinned by
    `test_every_complete_redraw_rotates_the_token_and_refuses_the_old_one`,
    `test_a_token_from_another_conversation_is_refused` and
    `test_an_obsolete_token_cannot_start_a_ui_action`.
16. **An accepted action either starts a turn or settles the event.** The post-
    acceptance re-check (`:586-592`) settles `interrupted`; `_send` settles the rest
    in its `finally` (`:1473-1474`). (SM-06 invariant 8/10; SM-06 H1 is the one
    cancellation gap.)
17. **A pending native decision blocks every admission and no cancellation.** All
    six admission predicates include `_permission_pending` (`:578`, `:935`, `:979`,
    `:1131`, `:1170`, `:1182`), and `settle_after_renderer_termination` waits instead
    of deciding (`:1098-1102`). Pinned by
    `test_a_pending_native_decision_refuses_every_admission`,
    `test_renderer_settlement_waits_for_the_native_decision` and
    `test_stop_is_not_blocked_by_a_pending_decision`.
18. **`_permission_pending` is always released.** Both setters have exactly one
    release path: the `finally` at `:965-966` and the executor done callback at
    `:1044-1045`, which runs for a value and for an exception.
19. **A stale renderer callback cannot reach the page.** `emit_reply` requires
    `_visible_reply() is active` (`:1326-1328`) and `on_images`/`on_ui` repeat the
    identity check (`:1395`, `:1406`); a settled reply's renderer is closed
    (`:1438`, `:1442`, `:1458`). `_reply_epoch` is *not* what guards callbacks — it
    guards snapshot readers. Pinned by
    `test_a_settled_reply_cannot_emit_a_late_renderer_callback`.
20. **A snapshot is internally consistent with one reply epoch.** `_transcript_payload`
    loops until the epoch and the selected conversation are unchanged across the read
    (`:425-436`), and `_redraw` repeats the whole loop (`:504-509`). Pinned by
    `test_a_transcript_read_restarts_when_a_reply_settles_mid_read`.
21. **The draft's attachment budget never overflows the store.** At most 4 images and
    4 documents per message (`attachments/images.py:34`, `attachments/documents.py:78`),
    so a draft plus its envelope is at most 16 items — exactly the read cap at
    `drafts.py:127`.

## Hazards and open questions

- **H1 — `fragile` — `session.py:676-679` with `:935` and `:1522`.** *Reachable,
  demonstrated.* `delete_conversation` clears `_draft_owner` and then awaits a full
  redraw before `_load_draft` reinstalls the owner. Inside that window a page submit
  is admitted (`_ready` is true, `_generation` is idle, `_permission_pending` is
  false), yet `_save_draft` refuses to write (`:215`) so the envelope exists only in
  memory. If the turn is then refused before admission — an ordinary context-admission
  refusal — `_restore_unstored_draft` sees `_draft_owner is None != conversation_id`
  and returns `True`, assuming a saved envelope that was never written (`:1522-1524`);
  the following `_load_draft` then overwrites `_draft_sending` with `None`. The user's
  text is in neither the composer nor `composer_drafts`; only
  `_unsaved_outcomes[...]['error']` survives, pointing at a draft that is gone.
  The same window exists for the whole of `_load_draft` (`:239-272`) during any
  conversation switch. Demonstrated by
  `test_a_submit_in_the_ownerless_redraw_window_is_refused`, which asserts the
  submission is now refused inside the window. **Fixed**: `submit` and `retry` refuse
  while `_draft_owner is None` or `_quit_requested` is set (see
  [Formalization decisions](#formalization-decisions)).
- **H2 — `fragile` — `session.py:1170-1172`, `:1231-1236`, `:1290-1300`.** A drain
  that is cancelled *before its first step* never executes its `finally`, so
  `_diagram_repair_task`/`_animation_repair_task` keep pointing at a cancelled task.
  This is benign because every reader treats `done()` as absent (`:1159`, `:1191`)
  and the queues were already cleared by `stop`/`close`. Pinned as
  `assertTrue(...done())` in `test_stop_cancels_a_queued_repair_and_releases_the_slot`
  and `test_close_cancels_queued_repairs_and_marks_the_session_closed`. If a future
  reader ever tests `is not None` instead of `done()`, this becomes a lost repair.
- **H3 — `documentation-gap` — `session.py:1310-1315`.** `_send` reads the selected
  conversation and raises `ValueError('No conversation is open')` *before* its
  `try:`, so such a failure would skip both the `ui_event_id` settlement and the
  draft restore, leaving an accepted (unsettled) event and a finished `_generation`
  field that nothing cleared. Every
  current caller prevents it (`submit` requires `_ready`; `ensure_conversation` runs
  at boot and at delete-end; `_retry_latest` checks `:991`; repairs hold the slot so
  no delete can run), and `delete_current_conversation` always ends with
  `ensure_conversation` (`chat.py:539`). Reasoned only: no product path reaches it.
  A defensive `except` around the prologue, or moving the prologue inside the `try`,
  would make it unrepresentable.
- **H4 — `fragile` — `session.py:935`, `:979`.** `submit` and `retry` do not check
  `_quit_requested`, although `ui_action`, both repair intakes and the slot waiter do
  (`:579`, `:1131`, `:1182`, `:1170`). `close()` sets the flag first (`:354`) but keeps
  awaiting afterwards (generation cancellation, draft flush), and `_ready` is never
  cleared, so a page call delivered in that window is admitted and its task is never
  awaited; the loop then shuts the runtime down under it. The window needs a page call
  during shutdown — the emitter is already closed (`bridge.py:801`) but a pywebview
  worker thread can still dispatch. Reasoned only, not demonstrated.
- **H5 — `fragile` — `session.py:1477-1478` with `:176`.** `_request_state` is only
  advanced to the outcome when the reply's own conversation is selected. After a
  navigation during the reply the field keeps `'pending'` until the next redraw
  recomputes it from the newest message (`:444-446`). `_status_payload` prefers the
  live reply (`:176`), so the stale value is not emitted for a chat that has a
  visible reply; it is emitted only if a status is sent between the settle and the
  next redraw. Reasoned only.
- **H6 — `documentation-gap` — `drafts.py:127`.** `load` truncates saved attachments
  to 16 with no notice. That number is exactly two messages' worth of the current
  budget (4 images + 4 documents each), so it cannot bite today, but it is an
  undocumented coupling: raising `MAX_IMAGE_ATTACHMENTS_PER_MESSAGE` or
  `MAX_DOCUMENT_ATTACHMENTS_PER_MESSAGE` would start dropping attachments silently.
- **H7 — `fragile` — `session.py:125`, `:1037-1058`.** Nothing bounds how long
  `_permission_pending` may stay set: it is released only when the native dialog's
  future resolves. A dialog that never returns (a window killed while modal, a wedged
  GIO thread) refuses every turn, action and repair for the rest of the process, and
  `settle_after_renderer_termination` reports `permission_pending` instead of reloading.
  This is the documented policy ("left to the user", `:1091-1094`) and the same
  behaviour `tests/test_gui_renderer_recovery.py:154-173` pins. Reasoned only: no
  timeout exists and none is proposed — auto-deciding a trust prompt would be worse.
- **H8 — `none-found` — `session.py:1163-1173`.** The slot is genuinely exclusive.
  The five writers all gate on the same triple, the drain's test-and-set is
  await-free, `shield` keeps a waiter's cancellation away from the owner, and every
  drain clears only its own task (`:1233`, `:1297`, `:1502`). The one asymmetry —
  `_send(manage_generation=False)` for animation repair, whose slot is cleared by the
  drain instead — is consistent because the drain sets and clears both sides.
- **H9 — `none-found` — `session.py:545-559`, `:586`.** A rotated or foreign view
  token cannot start an action: the token is checked before the read, after the read,
  after acceptance, and the acceptance is refused if the session moved. The only
  accepted risk is inherent: a token stays valid until the next *complete* redraw,
  so a page that never redraws keeps the token it has. sm-05/sm-06 cover the
  persisted side.
- **H10 — `none-found` — `session.py:327-345`.** `boot` has no re-entry guard of its
  own: a second call would overwrite `_boot_task` (`:328`) and the first call's
  `finally` (`:345`) would clear the second one's flag. The only caller is gated by
  `bridge._booted` (`bridge.py:209-211`), so the contract is stated at the bridge.
  A guard in `Session.boot` would localise it; none is proposed.

## Formalization decisions

**H1 (and optional H4) applied.** `gui/session.py` is a shared central file, so the
investigation first specified the fix as a patch; the orchestrator has since applied
it: `submit` refuses while `_draft_owner is None` or `_quit_requested` is set, and
`retry` also checks `_quit_requested` like every other admission path. `drafts.py`
needs no change: its save is an upsert that cannot resurrect a deleted conversation
(`drafts.py:90-97`), its reads revalidate everything (`session.py:300-321`), and its
`[:16]` cap is exactly the current budget (H6).

### Applied patch for H1

The composer cannot accept a submission while it has no owner. This makes
"`_draft_sending` set" imply "the envelope can be persisted", which is the assumption
`_restore_unstored_draft` documents at `:1520-1524`.

```python
    async def submit(self, text, refs=None):
        stripped = text.strip()
        if (stripped.startswith('/') and '/' not in stripped.split()[0][1:]
                and not await asyncio.to_thread(Path(stripped.split()[0]).is_dir)):
            # Clear first: a command can switch conversations and load another draft.
            self._draft_text = ''
            self._schedule_draft_save()
            await self.run_command(stripped)
            return {'clear': True}
        if not stripped and not self._attachments:
            return {'clear': False}
        # No owner means the composer is mid-replacement (`_load_draft`, delete):
        # a write would be refused and an unstored request would have no recovery.
        if (not self._ready or self._draft_owner is None
                or self.controller.is_generating or self._busy()):
            self.emit('bell')
            return {'clear': False}
```

Why not a narrower fix inside `_restore_unstored_draft`? Because the same window also
lets a turn be *stored* while the composer has no owner; the envelope then survives in
memory until the reload discards it, which is harmless only by accident. Refusing the
submission removes the state instead of compensating for it. If the owner prefers to
keep admission open, the alternative is a `_draft_sending_saved` flag set by
`_save_draft` and required by `_restore_unstored_draft`'s early return — more state for
a narrower guarantee.

Follow-ups this requires:

- `test_a_send_settling_before_the_reloaded_owner_strands_the_request` must be
  rewritten to assert the refusal (bell, composer unchanged, no task) once the guard
  lands; `test_a_rejection_in_the_ownerless_redraw_window_is_restored` should then
  assert that `submit` returns `{'clear': False}` inside the window instead of
  `{'clear': True}`.
- `tests/test_composer_drafts.py` and `tests/test_session_lifecycle.py` submit only
  with an owner installed, so they are unaffected.
- Optional companion (H4): add `self._quit_requested` to the same guard at `:935`
  and to `retry`'s guard at `:979`, matching every other admission path.

## Verification

Offline, from the repository root. Exact commands and the result lines:

```
.venv/bin/python scripts/check_unit_tests.py -p test_sm_20_gui_session.py -v
  Ran 31 tests in 1.245s
  OK
```

Pre-existing focused tests for this module, re-run after adding the suite:

| Command (`scripts/check_unit_tests.py -p …`) | Result |
| --- | --- |
| `test_composer_drafts.py` | `Ran 11 tests` — `OK` |
| `test_session_lifecycle.py` | `Ran 26 tests` — `OK` |
| `test_chat_navigation.py` | `Ran 7 tests` — `OK` |
| `test_gui_renderer_recovery.py` | `Ran 11 tests` — `OK` |
| `test_sm_06_ui_action_events.py` | `Ran 15 tests` — `OK` |
| `test_sm_08_diagram_repair.py` | `Ran 10 tests` — `OK` |

Health gate:

```
.venv/bin/python scripts/check_health.py
  Ran 1571 tests in 69.895s
  OK (skipped=18)
  passed: … -m ruff check src tests scripts
  passed: … -m mypy
git diff --check
  (no output)
```

No source file was changed, so the wider module run is the whole offline suite above.
Live `--gui` probes were not run: they need GTK/WebKit and a display, and this bead
added no window behaviour to probe. The native probe for this area is
`scripts/check_gui_session.py` (same prerequisites).

No test writes outside its own temporary root; the suite points `XDG_*` at a scratch
directory and `check_unit_tests.py` fails if anything leaks into the shared ones. The
only new files are this document and `tests/test_sm_20_gui_session.py`.
