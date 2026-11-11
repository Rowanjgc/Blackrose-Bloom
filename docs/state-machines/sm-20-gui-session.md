# SM-20: Desktop session lifecycle, generation slot, draft recovery and view tokens

## Scope and ownership

| Surface | Location |
| --- | --- |
| Boot, availability and status projection | `src/blackrose/gui/session.py::Session.__init__` (`_ready`, `_boot_task`, `_quit_requested`, `_status_state`, `_status_detail`), `src/blackrose/gui/session.py::Session.status`, `src/blackrose/gui/session.py::Session._status_payload`, `src/blackrose/gui/session.py::Session.boot`, `src/blackrose/gui/session.py::Session.select_model` |
| The single generation slot | `src/blackrose/gui/session.py::Session.submit`, `src/blackrose/gui/session.py::Session.retry`, `src/blackrose/gui/session.py::Session.ui_action`, `src/blackrose/gui/session.py::Session._wait_for_repair_slot`, `src/blackrose/gui/session.py::Session._repair_diagrams`, `src/blackrose/gui/session.py::Session._repair_animations`, `src/blackrose/gui/session.py::Session._send` |
| Reply epoch and transcript snapshots | `src/blackrose/gui/session.py::Session._transcript_payload`, `src/blackrose/gui/session.py::Session._redraw`, `src/blackrose/gui/session.py::Session._load_draft` |
| Composer draft, debounce and sending envelope | `src/blackrose/gui/session.py::Session.update_draft`, `src/blackrose/gui/session.py::Session._schedule_draft_save`, `src/blackrose/gui/session.py::Session._start_draft_save`, `src/blackrose/gui/session.py::Session._flush_draft`, `src/blackrose/gui/session.py::Session._save_draft`, `src/blackrose/gui/session.py::Session._load_draft`, `src/blackrose/gui/session.py::Session.submit`, `src/blackrose/gui/session.py::Session._restore_unstored_draft`, and the `finally` of `src/blackrose/gui/session.py::Session._send` |
| Durable draft rows | `src/blackrose/drafts.py::ComposerDrafts`; `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 6) |
| Native permission interlock | `src/blackrose/gui/session.py::Session.submit` (directory approval), `src/blackrose/gui/session.py::Session._authorize_outbound`, `src/blackrose/gui/session.py::Session.settle_after_renderer_termination` |
| UI view tokens and action acceptance | `src/blackrose/gui/session.py::Session._ui_payload`, `src/blackrose/gui/session.py::Session._check_ui_view`, `src/blackrose/gui/session.py::Session._owned_ui`, `src/blackrose/gui/session.py::Session.ui_state`, `src/blackrose/gui/session.py::Session.ui_action`, `src/blackrose/gui/session.py::Session.render_transcript`, `src/blackrose/gui/session.py::Session._redraw` |
| Window shutdown | `src/blackrose/gui/session.py::Session.close`; `src/blackrose/gui/bridge.py::Api._shutdown`; the `finally` of `src/blackrose/gui/app.py::_run_gui` |

One `Session` per window (`src/blackrose/gui/app.py::_run_gui` builds the `Api`, whose
`src/blackrose/gui/bridge.py::Api.__init__` constructs it), constructed on the window
thread and driven entirely from one asyncio loop
(`src/blackrose/gui/bridge.py::LoopThread`). Every page call is a coroutine submitted
to that loop (`src/blackrose/gui/bridge.py::LoopThread.submit`,
`src/blackrose/gui/bridge.py::LoopThread.run`); `update_draft`, `stop` and
`remove_attachment` are plain functions run on it through
`src/blackrose/gui/bridge.py::LoopThread.call` (`src/blackrose/gui/bridge.py::Api.update_draft`,
`src/blackrose/gui/bridge.py::Api.stop`, `src/blackrose/gui/bridge.py::Api.remove_attachment`).
GTK/WebKit stays on its own thread and the emitter delivers batches from a third
(`src/blackrose/gui/bridge.py::Emitter`). Therefore:

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
belongs to the conversation being left (the busy guard at the top of
`src/blackrose/gui/session.py::Session._open_conversation`): the reservation is
global, the transcript is per conversation, and one `ActiveReply` records its own
`conversation_id` (`src/blackrose/gui/session.py::ActiveReply`,
`src/blackrose/gui/session.py::Session._visible_reply`).

## State variables

Every in-memory variable is initialised in
`src/blackrose/gui/session.py::Session.__init__`. The writers named below are methods of
`src/blackrose/gui/session.py::Session`; the two durable columns are created by
`src/blackrose/storage/migrations.py::MIGRATIONS` (migration 6) and written by
`src/blackrose/drafts.py::ComposerDrafts.save`.

| Variable | Type | Meaning | Who writes |
| --- | --- | --- | --- |
| `_ready` | `bool` | a provider model is loaded; the only admitting state | `boot` after `controller.start()` returns; `select_model` on success and on `PreferenceSaveError`; never reset |
| `_boot_task` | `asyncio.Task \| None` | the running `boot()` coroutine; the "startup is still in progress" flag | `boot` on entry and in its `finally` |
| `_quit_requested` | `bool` | window is closing; refuses new page work | `close` (first statement); never cleared |
| `_status_state`, `_status_detail` | `str`, `str \| None` | last display line; `'Backend unavailable'` is the one value read semantically (the `availability` field of `_status_payload`) | `status()` through `_status_payload`, and ~20 call sites |
| `_generation` | `asyncio.Task \| None` | the one admitted turn, retry or repair drain | set by `ui_action`, `submit`, `retry`, `_wait_for_repair_slot`; cleared by the `finally` of `_retry_latest`, `_wait_for_repair_slot` (a done owner), the `finally` of `_repair_diagrams` and `_repair_animations`, and the `finally` of `_send` |
| `_active_reply` | `ActiveReply \| None` | the streaming reply and its renderer/blocks | `_send` (prologue and `finally`); `_restore_unstored_draft` |
| `_reply_key` | `int` | monotonic reply identity carried by page events | `_send` prologue |
| `_reply_epoch` | `int` | "a reply started, settled or was restored" counter for snapshot readers | `_send` prologue and `finally`; `_restore_unstored_draft` |
| `_request_state` | `str \| None` | `pending` while a reply streams, else the settled/unsaved outcome | `_send` prologue and `finally`; `_transcript_payload`; `_remember_unsaved_outcome` |
| `_unsaved_outcomes` | `dict[int, {state, error}]` | pre-admission refusals that have no durable turn | `_remember_unsaved_outcome`; `_send`'s `CancelledError` and `Exception` branches; popped in `_send`'s prologue |
| `_permission_pending` | `bool` | a native approval dialog is open; **every** admission refuses | `submit` (approval branch and its `finally`); `_authorize_outbound` (set, and cleared by the `closed` done callback) |
| `_can_retry` | `bool` | the shown chat has a stored user request | `_transcript_payload`; `_send`'s `finally` |
| `_draft_owner` | `int \| None` | the conversation the in-memory composer belongs to | `_load_draft` (cleared on entry, installed at the end); `delete_conversation` |
| `_draft_text`, `_draft_refs`, `_attachments` | `str`, `list[dict]`, `list[PendingAttachment]` | the composer | `update_draft`; `_load_draft`; `_restore_unstored_draft`; `_send` prologue |
| `_draft_sending` | `{text, refs, attachments, baseline} \| None` | a submitted request whose turn may not be stored yet | `submit`; `_load_draft` (re-armed from a live reply); cleared by `_send`'s `finally`, `_restore_unstored_draft` and `delete_conversation` |
| `_draft_save` | `TimerHandle \| None` | the 0.5 s debounce (`src/blackrose/gui/session.py::DRAFT_SAVE_SECONDS`) | `_schedule_draft_save`; `_start_draft_save` |
| `_draft_save_task` | `asyncio.Task \| None` | the save in flight | `_start_draft_save` |
| `_draft_lock` | `asyncio.Lock` | serialises saves so the last write is the newest state | held by `_save_draft` |
| `_draft_save_failed` | `bool` | latched warning so one failure is announced once | `_save_draft` (cleared on success, latched in the `except`) |
| `_ui_views` | `dict[block_id, hex token]` | the view identity the page must echo | `_ui_payload`; cleared by `render_transcript`, `_transcript_payload`, `_redraw` |
| `_ui_action_pending` | `bool` | interlock covering acceptance through task creation | `ui_action` (set after the entry guard, cleared in its `finally`) |
| `_diagram_repair_task`, `_animation_repair_task` | `asyncio.Task \| None` | the two deferred drains | `diagram_failed`; `animation_failed`; cleared in each drain's `finally` |
| `_pending_diagram_failures`, `_pending_animation_failures` | dicts | queued page-reported failures, bounded at 8 | intake in `diagram_failed`, `animation_failed`; cleared by `stop`; drains |
| `_animation_recovery_states`, `_repairing_animation_id` | `dict`, `int \| None` | mirror of the durable claim for the page | `animation_failed`; `_repair_animations` (claim, start, finish and `finally`) |
| `composer_drafts.text/refs/attachments/sending` | `TEXT`/JSON | the persisted composer and envelope | `ComposerDrafts.save` |
| `composer_draft_files.data` | `BLOB` keyed by digest | attachment copies; cascade with the row | `ComposerDrafts.save` |

`_status_state` is a free string. Only one comparison is semantic — `availability`
reports "Unavailable" only for the exact literal `'Backend unavailable'`
(the `availability` expression in `src/blackrose/gui/session.py::Session._status_payload`);
any other unready status reads as "Loading".

## States

### A. Availability (single writer: `boot`/`select_model`/`close`)

| State | Predicate |
| --- | --- |
| `Starting` | `_boot_task is not None and not _ready` |
| `Ready` | `_ready` |
| `Unavailable` | `_boot_task is None and not _ready and _status_state == 'Backend unavailable'` |
| `Aborted` | `_boot_task is None and not _ready and _status_state != 'Backend unavailable'` — boot ended without either outcome (cancelled by `close`, or raised outside the caught region) |

`Closing` (`_quit_requested`) is an orthogonal, absorbing gate rather than a state;
`_ready` is never cleared, so `Closing ∧ Ready` is reachable; every admission path,
`submit` and `retry` included, refuses in it (H4, resolved).

### B. Generation slot (`_generation`, `_active_reply`, `controller.is_generating`)

| State | Predicate |
| --- | --- |
| `Idle` | `_generation is None` |
| `Settled` | `_generation` is a finished task; treated as idle by `_busy`, `stop` and `settle_after_renderer_termination` |
| `Reserved` | live task, `_active_reply is None` — `retry` before `_send`, or `_send`'s prologue |
| `Streaming` | live task, `_active_reply` set, `_request_state == 'pending'` |
| `Settling` | live task, `_active_reply` cleared (`_send`'s `finally`), `_generation` not yet cleared (the `manage_generation` branch at the end of that `finally`) |
| `Repairing` | live task is `_repair_diagrams`/`_repair_animations` (owner acquired by `_wait_for_repair_slot`) |
| `Stopping` | a cancelled task still unwinding through its recovery path |

### C. Composer draft

| State | Predicate |
| --- | --- |
| `Unowned` | `_draft_owner is None` — the composer is being replaced (`_load_draft` on entry, `delete_conversation`) |
| `Clean` | owner set, `_draft_save is None`, `_draft_save_task` done or absent |
| `Dirty` | `_draft_save` armed (the 0.5 s debounce) |
| `Saving` | `_draft_save_task` running, or `_draft_lock` held |
| `SendPending` | `_draft_sending is not None` and `not last_request_stored` |
| `SendAdmitted` | `_draft_sending is not None` and `last_request_stored` — cleared by the first branch of `_send`'s `finally` |
| `SaveFailed` | `_draft_save_failed` latched; the composer still works, only durability is lost |

### D. UI view tokens

| State | Predicate |
| --- | --- |
| `Unissued` | no `_ui_views` entry for the block |
| `Issued` | `_ui_views[block_id] == token` for the token the page last received |
| `Rotated` | the snapshot was rebuilt, so every token the page holds is stale |

### E. Permission

`Clear`; `Pending` (`_permission_pending`), entered from `submit`'s directory prompt
and from `_authorize_outbound`, and left only by the `finally` of `submit`'s approval
branch or the executor future's done callback (`closed` in
`src/blackrose/gui/session.py::Session._authorize_outbound`).

## Events

| Id | Event | Origin | Note |
| --- | --- | --- | --- |
| E1 | Page ready → first boot | `src/blackrose/gui/bridge.py::Api.ready` | once per window (`_booted`) |
| E2 | Local recovery and transcript published | `src/blackrose/gui/session.py::Session.boot` (recovery, sidebar, transcript, draft) | before the provider loads |
| E3 | Provider load succeeds / fails | `src/blackrose/gui/session.py::Session.boot` (inner `try` around `controller.start()`) | failure is caught, not raised |
| E4 | Model selected from the Models dialog | `src/blackrose/gui/session.py::Session.select_model` | the only exit from `Unavailable` |
| E5 | Submit text and/or attachments | `src/blackrose/gui/session.py::Session.submit` | slash commands return before admission |
| E6 | Retry the latest stored request | `src/blackrose/gui/session.py::Session.retry` | reserves the slot synchronously |
| E7 | UI action accepted by the page | `src/blackrose/gui/session.py::Session.ui_action` | durable acceptance first, turn second |
| E8 | Stop (button, key, renderer termination) | `src/blackrose/gui/session.py::Session.stop`, `src/blackrose/gui/session.py::Session.settle_after_renderer_termination` | also cancels queued repairs |
| E9 | Close / window closed | `src/blackrose/gui/session.py::Session.close`; `src/blackrose/gui/bridge.py::Api._shutdown` | cancels boot, repairs, generation, then flushes |
| E10 | The generation task settles | `src/blackrose/gui/session.py::Session._send` (its `try`/`except`/`finally`) | complete, refused, cancelled, exception |
| E11 | Navigation or redraw | `src/blackrose/gui/session.py::Session._open_conversation`, `src/blackrose/gui/session.py::Session.new_conversation`, `src/blackrose/gui/session.py::Session.delete_conversation`, `src/blackrose/gui/session.py::Session.redraw` | redraws rotate every view token |
| E12 | Draft debounce fires / flush | `src/blackrose/gui/session.py::Session._start_draft_save`, `src/blackrose/gui/session.py::Session._flush_draft` | flush before an owner change and at close |
| E13 | Directory approval dialog returns | `src/blackrose/gui/session.py::Session.submit` (the `asyncio.to_thread` call to `_approve_directories`) | runs off-loop; the answer is revalidated |
| E14 | Outbound approval dialog returns | `src/blackrose/gui/session.py::Session._authorize_outbound` | executor future + done callback |
| E15 | Browser reports a diagram/animation failure | `src/blackrose/gui/session.py::Session.diagram_failed`, `src/blackrose/gui/session.py::Session.animation_failed` | queued; never takes the slot directly |
| E16 | A repair drain starts, loops or finishes | `src/blackrose/gui/session.py::Session._repair_diagrams`, `src/blackrose/gui/session.py::Session._repair_animations` | waits for boot and then for the slot |
| E17 | A draft load completes (possibly into a moved composer) | `src/blackrose/gui/session.py::Session._load_draft` | re-reads the selected conversation |
| E18 | Process death | — | the only recovery is `composer_drafts` plus the on-disk turn rows |

## Transition table

All methods named in these tables are on `src/blackrose/gui/session.py::Session`
unless a path is given; guards are named by the statement that implements them.

### A. Availability

Owned by `src/blackrose/gui/session.py::Session.boot`,
`src/blackrose/gui/session.py::Session.select_model` and
`src/blackrose/gui/session.py::Session.close`.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| — | E1 | — | `_boot_task := current_task()` (first statement of `boot`); `_status_state` was `'Starting…'` (its `__init__` value) | `Starting` |
| `Starting` | E2 | `recover_interrupted`/`ensure_conversation`/`refresh_conversations`/`render_transcript`/`_load_draft` all return (the first five awaits of `boot`) | transcript and sidebar emitted; the composer owner is set before `_ready` | `Starting` (recovered) |
| `Starting` (recovered) | E2 | — | `status('Loading default model…')` | `Starting` (loading) |
| `Starting` (loading) | E3 success | `controller.start()` returns | `_ready := True`; `status('Ready')` | `Ready` |
| `Starting` (loading) | E3 failure | the inner `except Exception` | notice; `status('Backend unavailable', detail)`; `_ready` stays `False` | `Unavailable` |
| any | E2/E3 | boot body raises outside the inner `try` (the recovery awaits, or `_show_router_warnings`) | `finally` clears `_boot_task`; the exception propagates to the caller (`src/blackrose/gui/bridge.py::Api.ready` ignores the future) | `Aborted` |
| `Starting` | E9 | boot task not done (the `_boot_task` branch of `close`) | `cancel()` and await; `finally` clears `_boot_task`; `_ready` stays `False` | `Aborted` |
| `Unavailable` | E4 success | `controller.select_model` returns | `_ready := True`; `status('Ready')` (in `select_model`'s `finally`) | `Ready` |
| `Unavailable` | E4 `PreferenceSaveError` | model is loaded, only the default failed (the `PreferenceSaveError` branch) | `_ready := True`; error returned to the page | `Ready` |
| `Unavailable` | E4 `OSError`/other | the `OSError` and `Exception` branches | error returned; `status('Backend unavailable')` (in the `finally`) | `Unavailable` |
| `Ready` | E4 entry | `self._busy() or controller.is_generating` (`select_model`'s entry guard) | refusal, nothing written | `Ready` |
| `Ready` | E4 | — | `status('Loading …')` but `_ready` is unchanged, so admission stays open while the switch runs | `Ready` |
| `Ready`/`Unavailable` | E9 | — | `_quit_requested := True` (first statement of `close`); `_ready` unchanged | `Closing` |
| `Aborted` | E4 | — | `select_model` still runs and can reach `Ready`; nothing prevents it | `Ready` |

### B. Generation slot

Owned by `src/blackrose/gui/session.py::Session.submit`,
`src/blackrose/gui/session.py::Session.retry`,
`src/blackrose/gui/session.py::Session._retry_latest`,
`src/blackrose/gui/session.py::Session.ui_action`,
`src/blackrose/gui/session.py::Session._send`,
`src/blackrose/gui/session.py::Session.stop`, the two intakes
(`src/blackrose/gui/session.py::Session.diagram_failed`,
`src/blackrose/gui/session.py::Session.animation_failed`) and the two drains with
`src/blackrose/gui/session.py::Session._wait_for_repair_slot`.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Idle` | E5 | slash command (`submit`'s first branch) | draft text cleared, `run_command`; **no slot** | `Idle` |
| `Idle` | E5 | nothing to send (no text and no attachments) | `{'clear': False}` | `Idle` |
| `Idle` | E5 | `not _ready or _draft_owner is None or _quit_requested or is_generating or _busy()` (`submit`'s busy guard) | bell | `Idle` |
| `Idle` | E5 | `_permission_pending` (`submit`'s silent permission check) | silent `{'clear': False}` | `Idle` |
| `Idle` | E5 | approval branch (`_approve_directories is not None`) | `_permission_pending := True`; revalidate conversation+workspace after the dialog; refuse if busy returned (the busy check after `approve_directories`) | `Idle` (permission `Pending` in between) |
| `Idle` | E5 | all pass | `_draft_sending := {…}`, composer cleared, `_generation := create_task(_send)`, immediate save (`_schedule_draft_save(0)`) | `Reserved` |
| `Idle` | E6 | `not _ready or _busy() or _quit_requested or is_generating or _permission_pending` (`retry`'s guard) | bell | `Idle` |
| `Idle` | E6 | — | `_generation := create_task(_retry_latest())` **before any read** | `Reserved` |
| `Idle` | E7 | `_ui_action_pending or _busy() or is_generating or not _ready or _permission_pending or _quit_requested` (`ui_action`'s entry guard) | `{'ok': False, 'error': 'Wait for the current reply to finish'}` | `Idle` |
| `Reserved` | `_send` enters | conversation selected (`_send`'s first check, else raises before the `try`) | `reply_start`, `_active_reply`, `_reply_epoch += 1`, `_request_state := 'pending'` (`_send`'s prologue) | `Streaming` |
| `Reserved` (retry) | reads finish | request found, `ui_context is None`, attachments readable, grants okay (`_retry_latest`'s checks) | `_send(..., preserve_draft=True)` | `Streaming` |
| `Reserved` (retry) | no request / form request / refusal | `_retry_latest`'s early returns and its `except Exception` | flash/notice; `finally` clears the slot (when `_generation is current_task()`) | `Settled` |
| `Reserved` | E7 | acceptance succeeded, but the view rotated, quit, permission, or a live slot appeared (`ui_action`'s post-acceptance re-check) | `finish(event_id, 'interrupted')`, refusal raised; `finally` clears `_ui_action_pending` | `Idle` |
| `Reserved` | E7 | all pass | `_generation := create_task(_send(..., ui_event_id))` | `Reserved`→`Streaming` |
| `Reserved` | E8 | — | the cancel is queued with `call_soon` so `_send` reaches its recovery scope first (the last statements of `stop`) | `Stopping` |
| `Streaming` | E10 stored | `controller.send` returns a turn | `reply_end`, outcome from the message state (`end_stored`) | `Settling` |
| `Streaming` | E8 | — | `renderer.close()`, cancelled reply_end; if `last_completed_turn` is set the stored answer is shown instead (`_send`'s `CancelledError` branch); `_unsaved_outcomes` only when not stored; `_restore_unstored_draft` | `Settling` |
| `Streaming` | E10 exception | `_send`'s `except Exception` branch | error reply_end, `_unsaved_outcomes` (only when not stored), `_restore_unstored_draft` | `Settling` |
| `Settling` | — | owner matches and the request was stored (first branch of `_send`'s `finally`) | `_draft_sending := None`, save | `Settling` |
| `Settling` | — | `ui_event_id` | `interactive_answers.finish(event_id, outcome)` | `Settling` |
| `Settling` | — | — | `_active_reply := None`; `_request_state := outcome` for the visible chat; `_reply_epoch += 1` | `Settling` |
| `Settling` | — | — | summary read, metrics decode speed cleared (the nested `try`/`finally` of `_send`'s `finally`); `manage_generation` → `_generation := None`, `status('Ready')` | `Settled` |
| `Settling` | navigation completes first | `selected.id != active.conversation_id` (the `_request_state` check in `_send`'s `finally`) | `_request_state` keeps the other chat's value; the next redraw repairs it (the `_request_state` recomputation in `_transcript_payload`) | `Settled` |
| `Idle` | E15 diagram | `config.diagram_repairs != 0`, payload bounds, `_quit_requested`, `(not _ready and _boot_task is None)`, `_permission_pending` (`diagram_failed`'s guards) | queue key (≤ 8), `setdefault`, spawn drain if none live | `Idle` + queued |
| `Reserved`/`Streaming`/`Settling` | E15 diagram | same guards pass | queued only; the drain waits for the slot (`_repair_diagrams` → `_wait_for_repair_slot`) | unchanged |
| `Idle` | E15 animation | cloud repair available, bounds, `_quit_requested`, `_ready`, `_permission_pending`, scope revalidated (`animation_failed`'s guards and `read_animation` check) | queue, `status: 'queued'`, spawn drain if none live | `Idle` + queued |
| any | E16 start | boot task live and not the drain itself (`_repair_diagrams`'s first check) | `await shield(boot)` | unchanged |
| any | E16 claim | `_generation` live and not this task (`_wait_for_repair_slot`'s loop) | `await shield(owner)`; then clear it if still current and done | unchanged |
| any | E16 claim | `not _ready or _quit_requested or _permission_pending` (`_wait_for_repair_slot`'s refusal) | `return False`; diagrams emit `unavailable` unless quitting (`_repair_diagrams`); animations `unavailable` in their `finally` (`_repair_animations`) | queue drained |
| `Idle`/`Settled` | E16 claim | guards pass | `_generation := current_task` (`_wait_for_repair_slot`'s assignment) | `Repairing` |
| `Repairing` | E16 loop | failures queued | repair/claim/appended turn; animations call `_send(..., manage_generation=False)` | `Repairing` |
| `Repairing` | E16 end | — | clear queue, clear `_repairing_animation_id`, `_generation := None` only if it is still this task (the `finally` of `_repair_diagrams` and `_repair_animations`) | `Settled` |
| `Repairing` | E8 | `_generation` is the drain | cancel queued with `call_soon`; the drain's `finally` runs | `Stopping`→`Settled` |
| `Repairing` (queued only) | E8 | drain not yet started | `_pending_*` cleared with terminal page updates (`stop`'s queue-clearing loops), drain cancelled | `Idle` |
| any | E11 while `Streaming` | `_busy() and not (_active_reply and is_generating)` (`_open_conversation`'s guard) | navigation refused with a bell | unchanged |

The five slot writers all sit behind the same admission triple — `_ready`,
`not is_generating`, `not _busy()` — and `_wait_for_repair_slot` performs its
test-and-set with **no `await` between the done-owner clear and the
`self._generation = current` assignment**, so two drains waking from the same
`shield` serialise instead of both claiming. This is the invariant pinned by
`test_claimants_take_the_single_slot_one_at_a_time`.

### C. Composer draft

Owned by `src/blackrose/gui/session.py::Session.update_draft`,
`src/blackrose/gui/session.py::Session._schedule_draft_save`,
`src/blackrose/gui/session.py::Session._start_draft_save`,
`src/blackrose/gui/session.py::Session._flush_draft`,
`src/blackrose/gui/session.py::Session._save_draft`,
`src/blackrose/gui/session.py::Session._load_draft`,
`src/blackrose/gui/session.py::Session._restore_unstored_draft` and the `finally` of
`src/blackrose/gui/session.py::Session._send`.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unowned` | boot recovery completes | — | `_draft_owner := conversation.id` (end of `_load_draft`) **before** `_ready` (set in `boot` after `controller.start()`) | `Clean` |
| any | E5 changed text | `refs` given | `_selected_refs` filter, `_schedule_draft_save()` (`update_draft`) | `Dirty` |
| `Dirty` | debounce fires | — | `_draft_save := None`, `_draft_save_task := create_task(_save_draft)` (`_start_draft_save`) | `Saving` |
| `Dirty` | E12 flush | — | timer cancelled, `_save_draft` awaited (`_flush_draft`) | `Saving` |
| `Saving` | row written | `_draft_owner` set (`_save_draft`'s owner check) | `drafts.save(owner, text, refs, attachments, sending)` | `Clean` |
| `Saving` | `_draft_owner is None` | `_save_draft`'s owner check | **no write at all**, silently | `Unowned` |
| `Saving` | `OSError`/`sqlite3.Error`/`StorageError` | `_save_draft`'s `except` | one warning notice, latched | `SaveFailed` |
| `SaveFailed` | next successful save | `_save_draft` after `drafts.save` returns | latch cleared | `Clean` |
| `Clean`/`Dirty` | E5 admission | all guards pass | `_draft_sending := envelope` with `baseline = _last_message_id`; composer text cleared (`submit`) | `SendPending` |
| `SendPending` | E10 stored | `_draft_owner == active.conversation_id` and `last_request_stored` (first branch of `_send`'s `finally`) | `_draft_sending := None`, save | `Clean` |
| `SendPending` | E10 refused/cancelled | `not last_request_stored` and owner matches (`_restore_unstored_draft`'s two early checks) | attachments restored, `_draft_sending := None`, sent text prepended to later typing | `Clean` |
| `SendPending` | E10 refused/cancelled | owner is a different chat (`_restore_unstored_draft`'s owner check) | no in-memory restore; the row is the recovery point | unchanged |
| `SendPending` | E8 with `preserve_draft` | retry, UI action or animation repair (the `preserve` branch of `_restore_unstored_draft`) | transcript redrawn, composer untouched, `_draft_sending` not cleared by the restore | unchanged |
| `SendPending` | E17 the chat is reopened | `saved['sending']` present and no live reply and `not request_was_stored` (`_load_draft` via `_draft_was_admitted`) | envelope merged in front of the saved text/refs/attachments | `Clean`→`Dirty` |
| `SendPending` | E17 the chat is reopened | `request_was_stored` (`_draft_was_admitted`) | envelope dropped, row rewritten as shown (`_load_draft`'s `_schedule_draft_save(0)`) | `Clean` |
| `SendPending` | E17 the live reply is visible again | `live and saved['sending']` (`_load_draft`'s `_draft_sending` assignment) | envelope re-armed from the reply's own text | `SendPending` |
| any | E11/E9 | `_flush_draft` before the switch (`_open_conversation`, `new_conversation`, `close`) | the current composer is saved under its owner | `Clean` |
| any | E11 delete | — | `_draft_owner, _draft_sending := None, None` (`delete_conversation`); the row cascaded | `Unowned` |
| `Unowned` | E17 completes | conversation unchanged (`_load_draft`'s selection re-check) | owner/text/refs/attachments installed, row rewritten | `Clean`/`Dirty` |
| `Unowned` | E17, the selection moved | `_load_draft`'s selection re-check | return without installing anything; the newer load owns the composer | `Unowned` |
| any | E17 returned while a reply settled | `epoch != _reply_epoch and saved['sending']` (`_load_draft`'s epoch re-check) | the whole load restarts so the envelope decision is re-made | restart |

### D. UI view tokens

Owned by `src/blackrose/gui/session.py::Session._ui_payload`,
`src/blackrose/gui/session.py::Session._check_ui_view`,
`src/blackrose/gui/session.py::Session._owned_ui`,
`src/blackrose/gui/session.py::Session.ui_state` and
`src/blackrose/gui/session.py::Session.ui_action`.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unissued` | `_ui_payload` for a non-markdown block | — | `_ui_views.setdefault(block_id, token_hex(16))` | `Issued` |
| `Issued` | E11 complete redraw | `render_transcript`, `_transcript_payload`, `_redraw` (each clears `_ui_views`) | the whole map is cleared, then re-issued for the new snapshot | `Rotated` |
| `Rotated` | page replays an old token | `_ui_views.get(block_id) != view_id` (`_check_ui_view`) | `ValueError('UI view is obsolete; reload this conversation')` | `Rotated` |
| `Issued` | page event for another chat | `selected.id != envelope.conversation_id` (`_check_ui_view`) | `ValueError('UI event belongs to another conversation')` | unchanged |
| `Issued` | `ui_state` | token valid before and after the read (`_owned_ui` checks twice) | state persisted with the page's sequence | unchanged |
| `Issued` | `ui_action` entry | `_ui_action_pending or _busy() or is_generating or not _ready or _permission_pending or _quit_requested` (`ui_action`'s entry guard) | refusal | unchanged |
| `Issued` | `ui_action` accepted | token valid after acceptance and no quit/permission/generation appeared (`ui_action`'s post-acceptance re-check) | `_generation := create_task(_send(...))` | `Issued` |
| `Issued` | `ui_action` view rotated or state changed during acceptance | `ui_action`'s post-acceptance re-check | `finish(event_id, 'interrupted')`, refusal; no task | `Issued` |

`_ui_payload` issues ids with `setdefault`, so within one snapshot a block keeps one id;
only a complete redraw rotates it. `earlier_messages` deliberately does not
clear the map (`src/blackrose/gui/session.py::Session.earlier_messages`): paged-in
history accumulates beside the current window until the next complete redraw replaces
both.

### E. Permission

Owned by `src/blackrose/gui/session.py::Session.submit` (directory approval branch) and
`src/blackrose/gui/session.py::Session._authorize_outbound`.

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Clear` | E5 with `_approve_directories` | slot free (`submit`'s busy guard) | `_permission_pending := True` | `Pending` |
| `Pending` | E13 declines | `not approved` | `{'clear': False}` in the `finally` | `Clear` |
| `Pending` | E13 accepts | conversation and workspace unchanged (the post-dialog re-check) | grants recorded, then the busy check | `Clear` |
| `Pending` | E13 accepts, the world moved | the post-dialog re-check fails | notice "Workspace changed while permission was requested" | `Clear` |
| `Clear` | outbound request during a reply | `_approve_outbound` configured (the `approve_outbound` argument `_send` passes to `controller.send`) | `_permission_pending := True`, status "Waiting for your approval…" | `Pending` |
| `Pending` | E14 returns (any answer, any exception) | executor future done (`closed` done callback) | callback clears `_permission_pending`; the awaiting reply then sets `status('Generating…')` unless Stop cancelled it | `Clear` |
| `Pending` | E8 | — | the reply is cancelled; the dialog stays open and `_permission_pending` stays `True` | `Pending` |
| `Pending` | E14 | — | the late answer is discarded with the cancelled reply | `Clear` |

## Invariants

1. **Admission requires `Ready`.** Every creator of `_generation` runs after
   `_ready` was observed true, and `_ready` is only set by a successful boot or model
   selection. Pinned by `test_nothing_is_admitted_until_a_transcript_and_model_are_ready`.
2. **The composer owner exists before the first admission.** `boot` installs
   `_draft_owner` (through `src/blackrose/gui/session.py::Session._load_draft`) before
   `_ready` (set in `src/blackrose/gui/session.py::Session.boot` after
   `controller.start()`), so a booting session can never accept a submit. Pinned by the
   same test.
3. **One slot, one owner.** At most one live task is ever `_generation`, and no
   admission path overwrites a live one. Pinned by
   `test_claimants_take_the_single_slot_one_at_a_time`,
   `test_a_queued_repair_waits_for_the_reply_and_never_shares_the_slot` and
   `test_the_action_interlock_closes_the_other_admissions`.
4. **A refusal writes nothing.** `_wait_for_repair_slot` returns `False` without
   touching `_generation` (its refusal branch precedes the assignment in
   `src/blackrose/gui/session.py::Session._wait_for_repair_slot`). Pinned by
   `test_refusals_never_take_or_clobber_the_slot` and
   `test_a_refusal_after_waiting_leaves_the_slot_free`.
5. **Cancelling a waiter never cancels the owner.** The wait is `shield`ed
   (`src/blackrose/gui/session.py::Session._wait_for_repair_slot`). Pinned by
   `test_a_cancelled_claimant_does_not_cancel_the_owner`.
6. **A finished task left in the field is inert.** `_busy`, `stop`, `settle` and the
   next claimant all treat `done()` as free (`src/blackrose/gui/session.py::Session._busy`,
   `src/blackrose/gui/session.py::Session.stop`,
   `src/blackrose/gui/session.py::Session.settle_after_renderer_termination`,
   `src/blackrose/gui/session.py::Session._wait_for_repair_slot`).
   Pinned by `test_a_finished_slot_is_cleared_before_the_next_claim` and
   `test_busy_covers_the_action_interlock_and_a_live_task`.
7. **`_active_reply` is cleared before the slot is released.** In the `finally` of
   `src/blackrose/gui/session.py::Session._send`, `self._active_reply = None` precedes
   the `manage_generation` release, and each drain clears its own task in a `finally`,
   so `Streaming` never coexists with `Idle`. Asserted throughout `GenerationSlotTests`.
8. **Stop reaches the task before the task runs.** `submit`/`retry` create the task
   and return; `stop` queues the cancel with `call_soon`
   (`src/blackrose/gui/session.py::Session.stop`) and `close` yields once (the
   `asyncio.sleep(0)` before `generation.cancel()` in
   `src/blackrose/gui/session.py::Session.close`), so `_send`/`_retry_latest` always
   enter their recovery scope first. Pinned by
   `test_retry_reservation_is_stoppable_before_it_reads_anything` and
   `test_stop_immediately_after_submit_recovers_draft` (existing).
9. **Stop is a no-op on an idle session.** `stop` rings the bell and returns `False`
   (its idle branch in `src/blackrose/gui/session.py::Session.stop`). Pinned by
   `test_stop_with_no_work_only_rings_the_bell`.
10. **Queued repairs never outlive their conversation's session.** `stop` and `close`
    clear both queues and emit a terminal update for every diagram key it drops
    (`src/blackrose/gui/session.py::Session.stop`). Pinned by
    `test_stop_cancels_a_queued_repair_and_releases_the_slot` and
    `test_close_cancels_queued_repairs_and_marks_the_session_closed`.
11. **A refusal before admission never consumes the composer.** The envelope exists
    from `submit` until the turn is stored or restored
    (`src/blackrose/gui/session.py::Session.submit`, the first branch of the `finally`
    in `src/blackrose/gui/session.py::Session._send`,
    `src/blackrose/gui/session.py::Session._restore_unstored_draft`).
    Pinned by the existing `test_composer_drafts.test_rejected_send_keeps_request_and_later_typing`
    and `test_stop_before_persistence_restores_question_and_attachment`.
12. **A submitted request survives navigation.** Leaving a streaming reply flushes
    the envelope to that chat's row (`_flush_draft` in
    `src/blackrose/gui/session.py::Session._open_conversation`, then
    `src/blackrose/gui/session.py::Session._save_draft`) and the in-memory copy is
    dropped (the `_draft_sending` assignment at the end of
    `src/blackrose/gui/session.py::Session._load_draft`); reopening re-validates and
    merges it. Pinned by `test_leaving_a_reply_parks_its_envelope_on_its_own_chat`.
13. **Save order cannot regress.** The newest state is only read inside
    `_draft_lock` (`src/blackrose/gui/session.py::Session._save_draft`), and
    `_flush_draft` takes the same lock, so a late timer or a stray task writes the
    current composer. Pinned by `test_the_debounced_save_always_writes_the_newest_state`.
14. **No owner, no write.** `_save_draft` refuses without `_draft_owner`
    (its owner check in `src/blackrose/gui/session.py::Session._save_draft`). Pinned by
    `test_a_save_is_refused_while_the_composer_has_no_owner`.
15. **A view token is valid only for its snapshot.** Any complete redraw invalidates
    every issued token, and both `ui_state` and `ui_action` verify before and after
    their awaits (`src/blackrose/gui/session.py::Session._owned_ui` and the
    post-acceptance re-check in `src/blackrose/gui/session.py::Session.ui_action`).
    Pinned by `test_every_complete_redraw_rotates_the_token_and_refuses_the_old_one`,
    `test_a_token_from_another_conversation_is_refused` and
    `test_an_obsolete_token_cannot_start_a_ui_action`.
16. **An accepted action either starts a turn or settles the event.** The post-
    acceptance re-check in `src/blackrose/gui/session.py::Session.ui_action` settles
    `interrupted`; `_send` settles the rest in its `finally` (the `ui_event_id` branch
    of `src/blackrose/gui/session.py::Session._send`). (SM-06 invariant 8/10; SM-06 H1
    is the one cancellation gap.)
17. **A pending native decision blocks every admission and no cancellation.** All
    six admission predicates include `_permission_pending`
    (`src/blackrose/gui/session.py::Session.ui_action`,
    `src/blackrose/gui/session.py::Session.submit`,
    `src/blackrose/gui/session.py::Session.retry`,
    `src/blackrose/gui/session.py::Session.animation_failed`,
    `src/blackrose/gui/session.py::Session._wait_for_repair_slot`,
    `src/blackrose/gui/session.py::Session.diagram_failed`), and
    `settle_after_renderer_termination` waits instead of deciding (its
    `while self._permission_pending` loop). Pinned by
    `test_a_pending_native_decision_refuses_every_admission`,
    `test_renderer_settlement_waits_for_the_native_decision` and
    `test_stop_is_not_blocked_by_a_pending_decision`.
18. **`_permission_pending` is always released.** Both setters have exactly one
    release path: the `finally` of `submit`'s approval branch
    (`src/blackrose/gui/session.py::Session.submit`) and the executor done callback
    `closed` in `src/blackrose/gui/session.py::Session._authorize_outbound`, which runs
    for a value and for an exception.
19. **A stale renderer callback cannot reach the page.** `emit_reply` requires
    `_visible_reply() is active` and `on_images`/`on_ui` repeat the identity check
    (closures in `src/blackrose/gui/session.py::Session._send`); a settled reply's
    renderer is closed (`renderer.close()` on the stored, cancelled and exception paths
    of `_send`). `_reply_epoch` is *not* what guards callbacks — it guards snapshot
    readers. Pinned by `test_a_settled_reply_cannot_emit_a_late_renderer_callback`.
20. **A snapshot is internally consistent with one reply epoch.** `_transcript_payload`
    loops until the epoch and the selected conversation are unchanged across the read
    (`src/blackrose/gui/session.py::Session._transcript_payload`), and `_redraw`
    repeats the whole loop (`src/blackrose/gui/session.py::Session._redraw`). Pinned by
    `test_a_transcript_read_restarts_when_a_reply_settles_mid_read`.
21. **The draft's attachment budget never overflows the store.** At most 4 images and
    4 documents per message
    (`src/blackrose/attachments/images.py::MAX_IMAGE_ATTACHMENTS_PER_MESSAGE`,
    `src/blackrose/attachments/documents.py::MAX_DOCUMENT_ATTACHMENTS_PER_MESSAGE`),
    so a draft plus its envelope is at most 16 items — exactly the `[:16]` read cap in
    `src/blackrose/drafts.py::ComposerDrafts.load`.

## Hazards and open questions

- **H1 — `fragile` — `src/blackrose/gui/session.py::Session.delete_conversation` with
  `src/blackrose/gui/session.py::Session.submit` and
  `src/blackrose/gui/session.py::Session._restore_unstored_draft`.** *Reachable,
  demonstrated.* `delete_conversation` clears `_draft_owner` and then awaits a full
  redraw before `_load_draft` reinstalls the owner. Inside that window a page submit
  is admitted (`_ready` is true, `_generation` is idle, `_permission_pending` is
  false), yet `_save_draft` refuses to write (its owner check) so the envelope exists
  only in memory. If the turn is then refused before admission — an ordinary
  context-admission refusal — `_restore_unstored_draft` sees
  `_draft_owner is None != conversation_id` and returns `True`, assuming a saved
  envelope that was never written (its owner check); the following `_load_draft` then
  overwrites `_draft_sending` with `None`. The user's text is in neither the composer
  nor `composer_drafts`; only `_unsaved_outcomes[...]['error']` survives, pointing at a
  draft that is gone. The same window exists for the whole of `_load_draft` (from
  clearing `_draft_owner` on entry to installing it at the end) during any
  conversation switch. Demonstrated by
  `test_a_submit_in_the_ownerless_redraw_window_is_refused`, which asserts the
  submission is now refused inside the window. **Fixed**: `submit` refuses while
  `_draft_owner is None` (see
  [Formalization decisions](#formalization-decisions)).
- **H2 — `fragile` — `src/blackrose/gui/session.py::Session._wait_for_repair_slot`,
  `src/blackrose/gui/session.py::Session._repair_diagrams`,
  `src/blackrose/gui/session.py::Session._repair_animations`.** A drain
  that is cancelled *before its first step* never executes its `finally`, so
  `_diagram_repair_task`/`_animation_repair_task` keep pointing at a cancelled task.
  This is benign because every reader treats `done()` as absent (the spawn checks in
  `src/blackrose/gui/session.py::Session.animation_failed` and
  `src/blackrose/gui/session.py::Session.diagram_failed`) and the queues were already
  cleared by `stop`/`close`. Pinned as `assertTrue(...done())` in
  `test_stop_cancels_a_queued_repair_and_releases_the_slot` and
  `test_close_cancels_queued_repairs_and_marks_the_session_closed`. If a future
  reader ever tests `is not None` instead of `done()`, this becomes a lost repair.
- **H3 — `documentation-gap` — `src/blackrose/gui/session.py::Session._send`.** `_send`
  reads the selected conversation and raises `ValueError('No conversation is open')`
  *before* its `try:`, so such a failure would skip both the `ui_event_id` settlement
  and the draft restore, leaving an accepted (unsettled) event and a finished
  `_generation` field that nothing cleared. Every
  current caller prevents it (`submit` requires `_ready`; `ensure_conversation` runs
  at boot and at delete-end; `_retry_latest` checks for a selected conversation before
  reading; repairs hold the slot so no delete can run), and `delete_current_conversation`
  always ends with `ensure_conversation` (the final `return` of
  `src/blackrose/chat.py::ChatController._delete_current_conversation`). Reasoned only:
  no product path reaches it. A defensive `except` around the prologue, or moving the
  prologue inside the `try`, would make it unrepresentable.
- **H4 — `fragile` — `src/blackrose/gui/session.py::Session.submit`,
  `src/blackrose/gui/session.py::Session.retry`.** Resolved:
  `src/blackrose/gui/session.py::Session.submit` and
  `src/blackrose/gui/session.py::Session.retry` now include `_quit_requested` in
  their admission guards. No test pins the refusal of these two paths yet; the
  slot waiter's equivalent is pinned by
  `tests/test_sm_20_gui_session.py::GenerationSlotPlannerTests.test_refusals_never_take_or_clobber_the_slot`.
  As found: `submit` and `retry` did not check
  `_quit_requested`, although `ui_action`, both repair intakes and the slot waiter do
  (`src/blackrose/gui/session.py::Session.ui_action`,
  `src/blackrose/gui/session.py::Session.animation_failed`,
  `src/blackrose/gui/session.py::Session.diagram_failed`,
  `src/blackrose/gui/session.py::Session._wait_for_repair_slot`). `close()` sets the
  flag first (`src/blackrose/gui/session.py::Session.close`) but keeps
  awaiting afterwards (generation cancellation, draft flush), and `_ready` is never
  cleared, so a page call delivered in that window was admitted and its task never
  awaited; the loop then shut the runtime down under it. The window needs a page call
  during shutdown — the emitter is already closed (`src/blackrose/gui/bridge.py::Api._shutdown`
  closes it before `Session.close`) but a pywebview worker thread can still dispatch.
  Reasoned only, not demonstrated.
- **H5 — `fragile` — the `finally` of `src/blackrose/gui/session.py::Session._send` with
  `src/blackrose/gui/session.py::Session._status_payload`.** `_request_state` is only
  advanced to the outcome when the reply's own conversation is selected. After a
  navigation during the reply the field keeps `'pending'` until the next redraw
  recomputes it from the newest message
  (`src/blackrose/gui/session.py::Session._transcript_payload`). `_status_payload`
  prefers the live reply (its `request_state` field), so the stale value is not emitted
  for a chat that has a visible reply; it is emitted only if a status is sent between
  the settle and the next redraw. Reasoned only.
- **H6 — `documentation-gap` — `src/blackrose/drafts.py::ComposerDrafts.load`.** `load`
  truncates saved attachments to 16 with no notice. That number is exactly two
  messages' worth of the current budget (4 images + 4 documents each), so it cannot
  bite today, but it is an undocumented coupling: raising
  `MAX_IMAGE_ATTACHMENTS_PER_MESSAGE` or `MAX_DOCUMENT_ATTACHMENTS_PER_MESSAGE` would
  start dropping attachments silently.
- **H7 — `fragile` — `src/blackrose/gui/session.py::Session._authorize_outbound`.**
  Nothing bounds how long `_permission_pending` may stay set: it is released only when
  the native dialog's future resolves. A dialog that never returns (a window killed
  while modal, a wedged GIO thread) refuses every turn, action and repair for the rest
  of the process, and `settle_after_renderer_termination` reports
  `permission_pending` instead of reloading. This is the documented policy ("left to
  the user", the docstring of
  `src/blackrose/gui/session.py::Session.settle_after_renderer_termination`) and the
  same behaviour
  `tests/test_gui_renderer_recovery.py::SessionRendererSettlementTests.test_pending_permission_is_not_cancelled_or_decided_by_host`
  pins. Reasoned only: no timeout exists and none is proposed — auto-deciding a trust
  prompt would be worse.
- **H8 — `none-found` — `src/blackrose/gui/session.py::Session._wait_for_repair_slot`.**
  The slot is genuinely exclusive. The five writers all gate on the same triple, the
  drain's test-and-set is await-free, `shield` keeps a waiter's cancellation away from
  the owner, and every drain clears only its own task (the `finally` of
  `src/blackrose/gui/session.py::Session._repair_diagrams`,
  `src/blackrose/gui/session.py::Session._repair_animations` and
  `src/blackrose/gui/session.py::Session._send`). The one asymmetry —
  `_send(manage_generation=False)` for animation repair, whose slot is cleared by the
  drain instead — is consistent because the drain sets and clears both sides.
- **H9 — `none-found` — `src/blackrose/gui/session.py::Session._check_ui_view`,
  `src/blackrose/gui/session.py::Session._owned_ui`,
  `src/blackrose/gui/session.py::Session.ui_action`.** A rotated or foreign view
  token cannot start an action: the token is checked before the read, after the read,
  after acceptance, and the acceptance is refused if the session moved. The only
  accepted risk is inherent: a token stays valid until the next *complete* redraw,
  so a page that never redraws keeps the token it has. sm-05/sm-06 cover the
  persisted side.
- **H10 — `none-found` — `src/blackrose/gui/session.py::Session.boot`.** `boot` has no
  re-entry guard of its own: a second call would overwrite `_boot_task` (its first
  statement) and the first call's `finally` would clear the second one's flag. The only
  caller is gated by `bridge._booted` (`src/blackrose/gui/bridge.py::Api.ready`), so
  the contract is stated at the bridge. A guard in `Session.boot` would localise it;
  none is proposed.

## Formalization decisions

**H1 and H4 fixed.** `submit` refuses while `_draft_owner is None` or `_quit_requested` is set, and
`retry` also checks `_quit_requested` like every other admission path. `drafts.py`
needs no change: its save is an upsert that cannot resurrect a deleted conversation
(`src/blackrose/drafts.py::ComposerDrafts.save`), its reads revalidate everything
(`src/blackrose/gui/session.py::Session._valid_draft_refs`,
`src/blackrose/gui/session.py::Session._valid_draft_attachments`), and its
`[:16]` cap is exactly the current budget (H6).

### The H1 guard

The composer cannot accept a submission while it has no owner. This makes
"`_draft_sending` set" imply "the envelope can be persisted", which is the assumption
`_restore_unstored_draft` documents in its owner-check comment
(`src/blackrose/gui/session.py::Session._restore_unstored_draft`).

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
        # No owner means the composer is mid-replacement (`_load_draft`, delete): a
        # write would be refused and an unstored request would have no recovery.
        if (not self._ready or self._draft_owner is None or self._quit_requested
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

`tests/test_sm_20_gui_session.py::DraftRecoveryTests.test_a_submit_in_the_ownerless_redraw_window_is_refused`
asserts the refusal inside the window. `tests/test_composer_drafts.py` and
`tests/test_session_lifecycle.py` submit only with an owner installed, so the guard
does not affect them. The `_quit_requested` term in the same guard, and in
`src/blackrose/gui/session.py::Session.retry`, is the H4 fix.

## Verification

Offline, from the repository root, after
[checkout setup](../../CONTRIBUTING.md#set-up-a-checkout). The focused suite for this
machine:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_20_gui_session.py -v
```

Pre-existing focused tests for this module, each run as
`.venv/bin/python scripts/check_unit_tests.py -p <file>`:

- `test_composer_drafts.py`
- `test_session_lifecycle.py`
- `test_chat_navigation.py`
- `test_gui_renderer_recovery.py`
- `test_sm_06_ui_action_events.py`
- `test_sm_08_diagram_repair.py`

The offline health gate covers the whole suite plus lint and type checks:

```sh
.venv/bin/python scripts/check_health.py
git diff --check
```

The native probe for this area is `scripts/check_gui_session.py`; like the other live
`--gui` probes it needs GTK/WebKit and a display.

No test writes outside its own temporary root; the suite points `XDG_*` at a scratch
directory and `check_unit_tests.py` fails if anything leaks into the shared ones.
