# SM-15: Local image GPU handoff, keep-warm window and ComfyUI service ownership

## Scope and ownership

| Surface | Location |
| --- | --- |
| `ComfyService` (`owned`, `ensure_running`, `release`, `free`, `_systemctl`) | `src/blackrose/local_images.py::ComfyService` |
| `WarmRecord` (durable warm model + stop duty) | `src/blackrose/local_images.py::WarmRecord` |
| `_Resident`, `_Session` | `src/blackrose/local_images.py::_Resident`, `src/blackrose/local_images.py::_Session` |
| `LocalImages` handoff: `_lock`, record I/O, `_adopt`, `_release_record`, `_expire`, `_keep_warm` | `src/blackrose/local_images.py::LocalImages` (`_lock` through `reap_orphan`, plus `_keep_warm`) |
| Session lifecycle: `_job`, `_prepare_memory`, `_settle`, `_finish`, `_run_shielded`, `batch`, `generate` | `src/blackrose/local_images.py::LocalImages` (`_settle` through `generate`) |
| Chat-model preemption and the recovery refusal | `src/blackrose/chat.py::ChatController.load_model`, `src/blackrose/chat.py::ChatController.select_model`, `src/blackrose/chat.py::ChatController.make_room_for_chat`, `src/blackrose/chat.py::ChatController.image_round`, `src/blackrose/chat.py::ChatController.require_recovered_memory`, `src/blackrose/turns.py::TurnRunner._answer` |
| Picker status / orphan reap / Release now | `src/blackrose/gui/session.py::Session.image_providers`, `src/blackrose/gui/session.py::Session._local_image_memory`, `src/blackrose/gui/session.py::Session.release_image_memory` |
| Shutdown | `src/blackrose/chat.py::ChatController.shutdown` |
| User-facing contract | [Admission and effects](../architecture.md#admission-and-effects) |

Unqualified method names below are members of
`src/blackrose/local_images.py::LocalImages`, or of
`src/blackrose/local_images.py::ComfyService` where the text says so; each
citation names the statement it means.

Two different owners meet in this module, and the architecture page is explicit
about both ([Admission and effects](../architecture.md#admission-and-effects)):

- **The GPU** is owned by whoever holds `local-images.lock` (`fcntl.flock`,
  `src/blackrose/local_images.py::LocalImages._lock`). The lock is cross-window,
  not cross-user, and is held through job execution, GPU cleanup and chat-model
  restoration. Every mutation of the warm record and every `ComfyService.owned`
  transition happens inside it. The unlocked readers are `status()`,
  `make_room_for_chat`'s first look, `reap_orphan` and `_release_record`'s fast
  path (the `_read_record()` before the lock); each decision is re-read under the
  lock before it acts (the second `_read_record()` in `_release_record`).
- **The `comfyui.service` unit** is owned by *the process that started it*, and
  that duty is transferable: it moves from `ComfyService.owned` to
  `WarmRecord.started_service` when the model is left warm (`_keep_warm`) and back
  on adoption (`_adopt`) or release (`_release_record`). The record docstring
  states the rule: "carries the duty to stop comfyui.service with the record,
  whichever window releases it" (`src/blackrose/local_images.py::WarmRecord`).

There is exactly one `LocalImages` and one `asyncio` loop per desktop window
(`src/blackrose/runtime.py::build_runtime`); the flock is what serializes windows.

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `ComfyService.owned` | `ComfyService.__init__` | bool | this process started the unit and owes a stop; false means an external unit (release = `/free`) | `ComfyService.ensure_running` (before `start`), `ComfyService.release` (after `stop`), `_keep_warm`, `_release_record` (from the record), `_adopt` (from the record) |
| `local-images.lock` | `lock_path` in `__init__`; `_lock` | `flock` on an fd | the handoff: one window may execute a job, adopt or release | `_lock` |
| `local-images.warm.json.token` | `WarmRecord.token`; set in `_keep_warm` | hex uuid | identity of one warm window; `'invalid'` from `_read_record`'s sentinel | `_keep_warm` under the lock |
| `.pid` | `WarmRecord.pid`; set in `_keep_warm` | int | the window that last kept it warm (`0` for the sentinel) | same |
| `.until` | `WarmRecord.until` | float epoch | deadline of the warm window; `0.0` for the sentinel | same |
| `.headroom` | `WarmRecord.headroom` | int bytes | the measured headroom the resident model was checked against | same |
| `.chat_model` | `WarmRecord.chat_model` | str | the chat model resident beside it (`''` when unknown) | same |
| `.started_service` | `WarmRecord.started_service`; set in `_keep_warm` from `service.owned` | bool | **the stop duty**: true means the record, not a process, owes the stop | same |
| `LocalImages._phase` | `__init__` | `None \| 'loading' \| 'generating' \| 'releasing'` | what this process is doing to the GPU, for display | `_job` (`'loading'`, `'generating'`, cleared in its `finally`), `_release_record` (`'releasing'`, cleared in its `finally`), `_release_service` (same) |
| `LocalImages._timer` | `__init__` | `asyncio.Task \| None` | the idle expiry worker for this window's record | `_arm_timer`, `_cancel_timer`, `_adopt`, `_release_record` |
| `LocalImages.recovery_error` | `__init__` | `str \| None` | sticky in-process latch: GPU memory recovery failed | `_release_record`'s `except`, `_run_shielded`'s two latches |
| `_Session.ready` | `src/blackrose/local_images.py::_Session` | bool | a job reached the service (queue checked empty) | `_job` (after the queue check, and forced when the service is owned) |
| `.jobs` | same | int | how many jobs this hold ran | `_job` |
| `.resident` | same | `_Resident \| None` | the image model this session has loaded / adopted | `_adopt` (valid record or sentinel), `_prepare_memory` (cleared), `_job` (after a download) |
| `.restore` | same | `str \| None` | a chat model this session displaced and must reload | `_prepare_memory` |
| `.failed` / `.cancelled` | same | bool | the job reached the service and then failed / was stopped | `_job`, `batch`'s `except` |
| `_Resident.shared` | `src/blackrose/local_images.py::_Resident` | bool | the chat model stayed loaded beside the image model | `_adopt`, `_job` |
| `_Resident.until` | same | `float \| None` | the original deadline of an adopted record | `_adopt` |
| record on disk | `_write_record` | file | written to `<name>.<pid>.tmp` then `os.replace` (atomic) | `_write_record` |
| `SHARING_MODELS`, `IDLE_RELEASE_SECONDS`, `SHARING_RAM` | `src/blackrose/local_images.py::SHARING_MODELS`, `src/blackrose/local_images.py::IDLE_RELEASE_SECONDS`, `src/blackrose/local_images.py::SHARING_RAM` | constants | who may share, for how long, with how much RAM | — |

## States

**Service-duty states** (derived from `record.started_service` and every
process's `owned`; mutually exclusive barring the recorded hazards):

| State | Predicate |
| --- | --- |
| `External` | no record, every `owned == False`; the unit is whatever systemd says |
| `ProcessOwned(P)` | no record, exactly one process `P` with `owned == True` |
| `RecordOwned` | a readable record with `started_service == True`, every process `owned == False` |
| `RecordExternal` | a readable record with `started_service == False`, every process `owned == False` |
| `DoubleDuty` | forbidden: a record with `started_service == True` **and** a process with `owned == True` |

**Session states** (in-process, one per hold of the lock):

| State | Predicate |
| --- | --- |
| `Idle` | no lock holder, `_batch.get() is None` |
| `Adopted` | `session.resident` set by `_adopt` (a valid record, or the sentinel branch) |
| `Ready` | `session.ready == True` — a prompt may be submitted |
| `Displaced` | `session.restore is not None` — a chat model was unloaded (`_prepare_memory`) |
| `Settling` | inside the shielded `_settle` (`_job`'s `_run_shielded` call) |
| `Finishing` | inside the shielded `_finish` (`batch`'s `finally`, or `generate` after `_job`) |
| `Tainted` | `recovery_error` is set; process-scoped, survives every session |

**Display states** (`status()`, checked in this order — phase, then record, then
lock):

| State | Predicate |
| --- | --- |
| `loading` | `_phase == 'loading'` |
| `in_use` | `_phase == 'generating'` (the phase map), or `_phase is None`, no record, lock held (`_lock_held_elsewhere`) |
| `releasing` | `_phase == 'releasing'` (wins over an existing record, because the record is removed in the same block) |
| `loaded` | `_phase is None`, record present, with `releases_at`/`seconds_left`/`this_window` |
| `not_loaded` | `_phase is None`, no record, lock free |

## Events

- `generate()` with no batch: acquire the lock, adopt, one job, finish
  (`generate`).
- `generate()` inside `batch()`: reuse the session, no lock, no finish
  (`generate`'s `session is not None` branch); `batch()` enter/exit (`batch`),
  opened by `src/blackrose/chat.py::ChatController.image_round` and used by
  `src/blackrose/turns.py::TurnRunner._run_tools`.
- `release_now()` from the picker (`release_now`,
  `src/blackrose/gui/session.py::Session.release_image_memory`).
- `make_room_for_chat(model)` before a chat model load or switch
  (`make_room_for_chat`; `src/blackrose/chat.py::ChatController.load_model`,
  `src/blackrose/chat.py::ChatController.select_model`,
  `src/blackrose/chat.py::ChatController.make_room_for_chat`,
  `src/blackrose/turns.py::TurnRunner._answer`).
- `reap_orphan()` before the picker's status read (`reap_orphan`,
  `src/blackrose/gui/session.py::Session._local_image_memory`).
- `close()` at shutdown (`close`, `src/blackrose/chat.py::ChatController.shutdown`).
- Idle timer expiry (`_expire`) and its 5 s retry on a busy lock (`_expire`'s
  `except TimeoutError`).
- Cancellation (Stop) at any await: captured into `_Session` (`_job`'s
  `except BaseException`, `batch`'s `except`), and absorbed by `_run_shielded`
  around `_settle`/`_finish`.
- Process death (window closed by SIGKILL/crash) and a recycled pid
  (`src/blackrose/local_images.py::_alive`).
- Record damage or a record from another version (`_read_record`).

## Transition table

Service duty (`owned` = this process's flag, `r` = the record on disk). Guards
and effects name statements in `ComfyService.ensure_running`,
`ComfyService.release`, `ComfyService.free`, `_finish`, `_keep_warm`, `_job`,
`_adopt`, `_release_record` and `reap_orphan`:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `External` | `ensure_running`, service answers | `/system_stats` 200 (the first probe) | none | `External` |
| `External` | `ensure_running`, nothing listening | `httpx.ConnectError` | probe `systemctl is-active` | `External` |
| `External` | `ensure_running`, unit inactive | `is-active` exit ≠ 0 | `owned = True` **then** `systemctl start`, poll `/system_stats` | `ProcessOwned` |
| `External` | `ensure_running`, unit already active | `is-active` exit 0 | none — a starting unit keeps its original owner (the comment in the `ConnectError` branch) | `External` |
| `ProcessOwned` | `_finish` keeps warm | `idle_release>0`, `resident.shared`, chat model, not failed/cancelled (the `keep` expression), RAM free (`_ram_headroom`) | write `r(started_service=owned)`, `owned = False`, arm timer (`_keep_warm`) | `RecordOwned` |
| `ProcessOwned` | `_finish` releases | keep false | `_release_service` → `service.release` → `systemctl stop`, `owned = False` (`ComfyService.release`) | `External` |
| `ProcessOwned` | job failed before submission | `session.ready or owned` forced ready (`_job`, after its `finally`) then `_finish` release | stop (`ComfyService.release`) | `External` |
| `RecordOwned` | a job adopts | record readable (`_adopt`'s `record is None` check) | `_remove_record`, cancel this window's timer, `owned = True` (`_adopt`, in that order) | `ProcessOwned` |
| `RecordOwned` | `make_room_for_chat` / `release_now` / timer / reap | record present, lock free (`_lock(patience=0)`), token matches, `until` past for `expired_only`, pid dead for reap (`reap_orphan`'s `_alive` check) | `owned = True` from `r`, `stop` or `/free` (`service.release()`), record removed (the `finally`) — all in `_release_record` | `External` |
| `RecordExternal` | same events | `record.started_service == False` | `service.release` → `free` (`ComfyService.free`), record removed (`_release_record`'s `finally`) | `External` |
| `RecordOwned`/`RecordExternal` | `_release_record` with a newer token | `r.token != token` | none; `False` | unchanged |
| `RecordOwned`/`RecordExternal` | `_release_record(expired_only)` on a live window | `r.until > now` | re-arm the timer | unchanged |
| `RecordOwned`/`RecordExternal` | `close` of another window | `owner_only` and `r.pid != os.getpid()` | none | unchanged |
| `RecordOwned`/`RecordExternal` | `_finish` after a job, shared keep | as `ProcessOwned` keep row | new record: new token, `pid = this`, `owned = False` (`_keep_warm`) | same duty state, new owner window |
| `RecordOwned`/`RecordExternal` | `_finish` with no job reached the service | resident re-describable (`until` and `chat_model`, `_finish`'s `not session.ready` branch) | put back with the same `until`, new token (`_keep_warm`) | same duty state |
| `RecordOwned`/`RecordExternal` | same, resident not re-describable | the `elif` in that branch (this change) | `_release_service` | `External` |
| `any` | record write raises | `_write_record` (in `_keep_warm`) | `_run_shielded` latches `recovery_error` (its `except Exception`); `owned` keeps the duty, no record | `ProcessOwned` (stranded) |
| `any` | release raises | `service.release()` in `_release_record` | latch `recovery_error` (the `except`), record still removed (the `finally`), `owned` stays set (`ComfyService.release`'s `owned = False` not reached) | `ProcessOwned` + `Tainted` (hazard 1) |
| `ProcessOwned` | process death before any record write | — | flag dies with the process; no record exists | `External` with the unit still running (hazard 2) |
| `RecordOwned` | owner process death | — | record survives, still adoptable; `reap_orphan` waits for `until` | `RecordOwned` |

Session lifecycle (statements in `generate`, `batch`, `_job`, `_prepare_memory`,
`_settle`, `_finish` and `_run_shielded`):

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Idle` | `generate` (no batch) | `_batch.get() is None` | lock, re-check `recovery_error`, `_adopt` (`generate`) | `Adopted` |
| `Idle` | `batch()` | no session in context (`batch`'s first check) | lock, `_adopt` (`batch`) | `Adopted` |
| `Adopted` | `_job` starts | — | `_phase = 'loading'` | `Adopted` |
| `Adopted` | service reached, queue empty | `ensure_running` returned, `_queue()` empty | `session.ready = True` | `Ready` |
| `Ready` | memory can be shared | `_can_share` (`_prepare_memory`) | `resident = _Resident(..., shared=True)` (`_job`, after the download) | `Ready` |
| `Ready` | memory must be exclusive | `holds_memory` and no share (`_prepare_memory`) | `session.restore = chat_model` **before** `backend.unload` | `Displaced` |
| `Ready` | prompt submitted, downloaded | `/prompt` accepted (the `prompt_id`/`node_errors` check) | `_phase = 'generating'`, `resident.headroom = max(required, previous)` | `Ready` |
| `Ready` | prepare refused (foreign VRAM) | `_prepare_memory`'s `_has_free_vram` check | failure, nothing submitted | `Ready` (then `Finishing`) |
| any | job ends (success, failure, Stop) | — | `_phase = None` (`_job`'s `finally`); settle only when a prompt exists or the service was reached (the `prompt_id is not None or reached` guard) | `Settling` |
| `Settling` | queue drained | `_queue()` empty (`_settle`'s poll) | `_phase` untouched | `Finishing` |
| `Finishing` | keep | the `keep` expression and `_ram_headroom` in `_finish` | record written, timer armed | `Idle` |
| `Finishing` | release | `keep` false | `_release_service` | `Idle` |
| `Finishing` | restore | `session.restore` (`_finish`'s last branch) | one `backend.load`, after the release | `Idle` |
| any | cancellation at an await | `_run_shielded` | worker completes; the cancellation is returned and re-raised after recovery | same state until `Idle` |
| `Idle` | `generate`/`batch` with a latched error | the `recovery_error` checks at the top of `batch` and `generate` | `RuntimeError(recovery_error)` before any request or model load | `Tainted` |

## Invariants

1. **One duty.** In the absence of a recorded failure, at most one of
   `{record.started_service == True, some process's owned == True}` holds.
   Enforced because every writer holds the lock (`_lock`): `_keep_warm` writes
   the record *then* clears the flag, `_adopt` removes the record *then* sets the
   flag, and neither window can interleave (no await between the pair; the lock
   excludes the other window).
   Pinned by `test_a_warm_handoff_moves_the_stop_duty_to_the_record`,
   `test_adoption_removes_the_record_before_this_process_owes_the_stop`,
   `test_preempting_an_owned_warm_model_leaves_no_owner_at_all`.
2. **Every record mutation is under the lock.** `_write_record`/`_remove_record`
   are called only from `_keep_warm` (reached from `_finish` in `batch` and
   `generate`), `_release_record` and `_adopt`. `_read_record` is also called
   without the lock from `status` and `reap_orphan`; every decision is taken
   again under it (the second read in `_release_record`).
3. **A record is either the old one or the new one.** Writes are
   `<name>.<pid>.tmp` + `os.replace` (`_write_record`), so a reader never parses
   a half-written record; a damaged record is read as the sentinel (`_read_record`'s
   `except`) rather than as a plausible one.
4. **While a record exists, it is the only account of the stop.** `release()`
   stops the unit only when `owned` is true (`ComfyService.release`), and
   `_release_record` takes `owned` from `record.started_service` before acting;
   `_adopt` does the same. Pinned by
   `test_preempting_an_external_service_only_frees_its_models`,
   `test_an_orphan_reap_stops_the_service_the_record_owes`.
5. **A record is adopted at most once.** Adoption removes it under the lock and
   every release path re-checks the token (`_release_record`). Pinned by
   `test_a_release_for_another_token_is_refused` and
   `test_closing_a_window_never_releases_another_windows_model`.
6. **The idle timer cannot release a window it does not describe.** `_expire`
   passes its token and `expired_only` re-arms when the deadline moved
   (`_release_record`). Pinned by `test_an_unexpired_record_is_rearmed_instead_of_released`.
7. **A dead window's model is reaped only after its deadline.** `reap_orphan`
   requires `until <= now` *and* a dead pid. Pinned by
   `test_a_dead_owner_keeps_its_warm_model_until_the_deadline`.
8. **No job runs outside the handoff lock.** `generate` and `batch` (each under
   `async with self._lock()`) are the only callers of `_job`. Pinned indirectly by
   `test_the_handoff_lock_is_free_after_a_failed_submission` and
   `test_cancellation_restores_the_chat_model_once`.
9. **GPU recovery is cancellation-proof and runs once.** `_run_shielded` awaits a
   shielded task in a loop and only abandons it when the worker itself was
   cancelled. Pinned by `test_cancellation_restores_the_chat_model_once`
   and `tests/test_image_recovery.py::ImageRecoveryTests.test_cancellation_after_download_returns_artifact_after_recovery`.
10. **The chat model is restored at most once per session, after the image
    memory.** `session.restore` is written once, before the unload
    (`_prepare_memory`), and consumed once in `_finish`; `_finish` is called
    exactly once per session (`batch`'s `finally`, or `generate`) and `_job`
    converts every job failure into a return value (its `except` clauses). Pinned
    by `test_a_batch_restores_the_chat_model_once_after_its_last_job`,
    `test_a_failed_job_inside_the_batch_still_restores_the_chat_model`,
    `test_cancellation_restores_the_chat_model_once`.
11. **A failed release is not followed by a restore.** `_release_service` lets the
    exception out of `_finish` before the `session.restore` branch, and
    `_run_shielded` latches it. Pinned by
    `tests/test_image_recovery.py::ImageRecoveryTests.test_download_survives_release_failure_and_blocks_new_jobs`
    and `test_a_failed_release_latches_recovery_and_refuses_later_work`.
12. **`recovery_error` is monotone.** It is assigned `None` only in the
    constructor (`__init__`) and a message only in `_release_record`'s `except`
    and `_run_shielded`'s two latches; nothing clears it. Every entry point refuses
    before contacting a provider (the checks at the top of `batch` and `generate`).
    Pinned by `test_a_failed_release_latches_recovery_and_refuses_later_work`
    (unchanged after `release_now`, `generate` and `batch`).
13. **`_phase` is written only by a lock holder and always cleared in a
    `finally`.** `_release_record`, `_release_service` and `_job`. Pinned by
    `test_the_phase_follows_the_job_from_loading_to_generating`,
    `test_the_releasing_phase_is_reported_while_the_release_is_in_flight`,
    and the `_phase is None` assertions after cancellation and failure.
14. **Display precedence is phase, record, lock** (`status`), so a window never
    reports a stale `loaded` while it is releasing. Pinned by the releasing-phase
    test and by `test_the_phase_follows_the_job_from_loading_to_generating`
    (`loaded` with `this_window` and a fresh deadline).
15. **Nothing of ours is held after a failed submission.** The lock is released
    by the context manager (`_lock`'s `finally`), and no prompt is cancelled when
    no prompt id was assigned (`_settle`'s `prompt_id and not completed` guard); a
    submitted prompt with a lost response is still interrupted by its known id
    (`_settle`). Pinned by
    `test_the_handoff_lock_is_free_after_a_failed_submission` and
    `tests/test_local_images.py::LocalProviderTests.test_lost_submission_response_still_cancels_our_known_uuid`.

## Hazards and open questions

Answers to the investigation's focus questions, then the numbered findings.

- *Enumerate the ownership pairs across a handoff, an adoption, a preemption and
  an orphan reap.*
  - Handoff, A started the unit: `ProcessOwned(A)` → A writes
    `r(started_service=True, pid=A)` → A clears `owned` → `RecordOwned`. If A
    never started it, the same sequence lands in `RecordExternal`.
  - Adoption by B: `RecordOwned` → B removes `r` → B sets `owned` → `ProcessOwned(B)`.
    On session end B either writes `r'` (`RecordOwned`) or stops the unit
    (`External`).
  - Preemption by C (chat load, Release now, timer): `RecordOwned` → C takes
    `owned` from `r` → stops → removes `r` → `External`.
  - Orphan reap by C: `RecordOwned` with a dead pid and a past deadline → same as
    preemption → `External`. A live pid or a live deadline does nothing.
  No pair leaves the unit running with two owners, and none leaves a *recorded*
  model with no owner. The exceptions are failure windows, below.
- *Is there a pair where nobody owns a running service (leak) or two actors both
  think they own it (double stop)?* Under normal operation, no: the lock is held
  across each pair of writes, so a second actor cannot observe or act on the
  intermediate state, and a repeated `systemctl stop` of an inactive unit succeeds,
  so `ComfyService._systemctl` (its exit-code check) does not turn a duplicate
  stop into an error. Leaks exist only after a failure or a crash (hazards 1-3).
- *What if the process dies between writing the warm record and releasing the
  flock?* Safe. The record is durable and self-describing before the flag is
  cleared (`_keep_warm`), and the OS drops the flock. Another window may adopt it
  and reuse it; `reap_orphan` stops the unit it owes once `until` passes. The
  model stays loaded until then — that is the warm window's purpose.
- *What if it dies between `systemctl start` and writing the record?* A leak:
  `owned` is set before the start (`ComfyService.ensure_running`) and only becomes
  durable in `_keep_warm`. Nothing on disk mentions the start, so the unit keeps
  running unowned (hazard 2).
- *Is `_phase` consistent with the session record at every await?* Yes. Phase is
  only written while holding the handoff lock (`_release_record`,
  `_release_service`, `_job`), and each writer clears it in a `finally`, so a
  session that is waiting in `_settle`, `_finish` or the shielded recovery has
  either the correct phase or no record and a held lock (`in_use`, from
  `_lock_held_elsewhere`). The one precedence quirk is deliberate: during
  `_release_record` the record still exists while the phase says `releasing`
  (the phase is set before the `finally` that removes the record), and `status`
  reports the phase.
- *Can `recovery_error` be cleared?* No — nothing writes `None` after
  construction (`__init__` vs the latches in `_release_record` and
  `_run_shielded`). It is process-scoped, so restarting the desktop is the
  documented escape ([Admission and effects](../architecture.md#admission-and-effects))
  and `src/blackrose/chat.py::ChatController.require_recovered_memory` also blocks
  model switches meanwhile.
- *Does the chat-model restore run exactly once on every exit path, including
  cancellation?* Yes: `_finish` is shielded and called once per session, and
  `session.restore` is set before the unload (`_prepare_memory`). Two caveats are
  by design: no restore when the release failed (invariant 11), and no restore
  when `_adopt` itself raises before the session's `try/finally` (only an unlink
  failure in `_remove_record`, hazard 3).

**1. `fragile` — `src/blackrose/local_images.py::LocalImages._release_record` —
a failed release destroys the durable stop duty.** `_release_record` removes the
record in the `finally` even when `service.release()` raised, and
`ComfyService.release` leaves `owned = True` because its `owned = False` was not
reached. After that, every retry surface needs a record: `_release_record`
returns from its unlocked fast path when there is none, so `release_now` and
`close` do nothing, and `_finish` is unreachable because `_release_record`'s
`except` latched `recovery_error` and jobs refuse at the top of `batch` and
`generate`. Net effect: a unit Blackrose started keeps running, owed only by a
flag no code path reads, until the process exits — and then by nobody. Not
demonstrated with a test (a test would have to pin the leak). Would confirm by
raising from `service.release`, then calling `release_now()` and `close()` and
observing no second `release` call and `service.owned == True`. The fix is a
coupled pair: remove the record only after a successful release, and make
`record.started_service` authoritative for `owned` in both `_release_record` and
`_adopt` (so a stale in-memory flag cannot stop an externally started unit
later). Left as a proposal because it changes what a record means during a
failure (today the record is a claim that the model is warm).

**2. `fragile` — `src/blackrose/local_images.py::ComfyService.ensure_running`
and `src/blackrose/local_images.py::LocalImages._keep_warm` — a crash before the
first record write leaves a started unit with no owner.** `owned = True` precedes
`systemctl start`; the duty only becomes durable when `_finish` writes the
record. A death between the start and that write (a long job, an OOM kill, a
desktop crash) loses the duty with the process. `reap_orphan` has no record to
read, and the next window's `ensure_running` finds the unit active and treats it
as external (its `is-active` probe), so it will never stop it — only `/free` its
models. Not demonstrated (needs process death); would confirm by SIGKILL in
`_job` after `ensure_running` and checking that the state directory has no record
while `systemctl --user is-active comfyui.service` is still true. A fix makes the
start durable before it happens (a separate owner marker, or a record written at
start) — a protocol change, so not made.

**3. `fragile` — `src/blackrose/local_images.py::LocalImages._adopt` — `_adopt`
removes the record before taking the flag.** There is no await between the two
statements, so cancellation cannot land there, but process death can: the record
is gone and no process holds the duty (same leak as hazard 2, one file operation
wide). Not demonstrated. A one-line swap would narrow it, but if `_remove_record`
then raised (`contextlib.suppress` covers only `FileNotFoundError`, in
`_remove_record`) `_adopt` would leave `owned = True` with the record still on
disk — a `DoubleDuty` — and `_adopt` is called before the session's
`try/finally` (in `batch` and `generate`), so the raise would also skip
`_finish`. Documented rather than half-fixed.

**4. `defect` (fixed) — `src/blackrose/local_images.py::LocalImages._finish` — a
resident that cannot be described again was silently dropped.** `_adopt` models
an unreadable record as `_Resident(0, None, False)` whose comment says "freed at
first use" (its sentinel branch), but the no-job path only re-kept residents with
both `until` and `chat_model` (`_finish`'s `not session.ready` branch) and
otherwise returned — with the record already removed (`_adopt`) and no timer.
Nothing then owned the still-loaded model, and `close()` could not release it
(`_release_record` returns from its fast path). The record can be unreadable
through `_read_record`'s sentinel (its `except`: truncated, missing key, or a key
renamed by a version-skew write),
and a blank `chat_model` reaches the same branch. Now the fall-through releases
it (the `elif` that calls `_release_service`). Regression test:
`test_a_record_nobody_can_describe_is_released_instead_of_stranded` (fails
before, passes after); success side:
`test_an_adopted_record_that_can_be_described_is_put_back_unchanged`.

**5. `defect` (fixed) — `src/blackrose/local_images.py::LocalImages._job` — a
start failure latched the sticky recovery error.** `_job` settled whenever it
owned the service, and `_settle` polls `GET /queue` (its final loop). After
`systemctl start` fails the service is not listening, so that poll raised
`httpx.ConnectError`; `_run_shielded` latched "Local image memory recovery
failed" (its `except Exception`) although nothing was submitted, no recovery was
needed, and the failure had already been cleaned up by the stop in `_finish`.
From then on `src/blackrose/chat.py::ChatController.require_recovered_memory`
refused every image job *and* every chat-model switch until the desktop
restarted, with a message unrelated to the real cause. Now the settle is skipped
when no prompt was submitted and the service was never reached (`_job`'s
`prompt_id is not None or reached` guard), while the normal, lost-response and
owned-service paths still settle. Regression test:
`test_a_failed_start_does_not_latch_recovery` (fails before: latched error and
the next job refused; passes after).

**6. `fragile` — `src/blackrose/local_images.py::LocalImages._expire` and
`src/blackrose/local_images.py::LocalImages.reap_orphan` — reaping is lazy.**
An expired record is released by the writing window's timer, by a picker status
read (`src/blackrose/gui/session.py::Session._local_image_memory`), or by a later
job's adoption. If the owner died and no window opens the picker,
`comfyui.service` keeps its model resident well past the 120 s window. Bounded by
user action, not by the code; the architecture page says "any window's next job
reuses it" but not who sweeps it
([Admission and effects](../architecture.md#admission-and-effects)). Pinned as
*intended* by `test_a_dead_owner_keeps_its_warm_model_until_the_deadline`; the
follow-up is a sweep at startup (`src/blackrose/runtime.py::Runtime.open`) or in
`image_providers`, which today only runs when the dialog opens.

**7. `documentation-gap` — `src/blackrose/local_images.py::LocalImages._read_record`
— the record is read more leniently than it is written.** `_read_record` copies
the six known keys with no type or range validation, ignores extra keys, and
returns the sentinel only for a missing key, bad JSON or an unreadable file.
`"until": null` therefore becomes `until=None`, which lands in the "cannot be
described" branch and now *releases* the user's warm model (fix 4) — safe, but a
version-skew write can silently discard a warm model. The sentinel's `pid=0` also
makes `_alive(0)` true (`os.kill(0, 0)` addresses the process group), so
`reap_orphan` never clears a sentinel; only adoption does. Not demonstrated;
would confirm by writing `{"token": "warm-token", "pid": 0, "until": null,
"headroom": 0, "chat_model": "", "started_service": false}` and watching the next
batch free the model. A `WarmRecord` schema version is the obvious follow-up.

**8. `none-found` — `src/blackrose/local_images.py::LocalImages._lock_held_elsewhere`
— `_lock_held_elsewhere` also answers "in use" for this process's own lock.**
`flock` on a second open file description conflicts even inside one process,
which is exactly what `status` wants while a session holds the lock between
awaits. No action.

**9. `fragile` (theoretical) — `src/blackrose/local_images.py::LocalImages.__init__`
— the batch contextvar is inherited by child tasks.** `_batch` is a `ContextVar`,
so an `asyncio.create_task`/`gather` started inside `batch()` would inherit the
session, and two `_job` calls would mutate one `_Session` (`resident`, `restore`,
`jobs`) concurrently with no lock, because `generate` skips the lock when a
session is present (its `session is not None` branch). Today unreachable:
`src/blackrose/turns.py::TurnRunner._run_tools` executes a round's calls in a
plain `for` loop. Would confirm by gathering two `generate` calls inside
`batch()`; the fix (a per-session lock or a "one job at a time" assertion) is not
needed for the current callers.

## Formalization decisions

**Two source changes, each with a regression test.** Both are 3-6 lines in the
module this investigation owns, neither touches the trust boundary or the record
format.

- `_finish` (the `elif` in its `not session.ready` branch): release an adopted
  resident that cannot be put back faithfully, instead of returning with the
  record already gone. Fixes hazard 4 (a model running with no record, no timer
  and no owner).
- `_job` (the `reached` flag and the `prompt_id is not None or reached` guard):
  settle only when a prompt was submitted or the service was actually reached.
  Fixes hazard 5 (a plain start failure latching the sticky, misleading
  `recovery_error`).
- `_release_service`: a two-line extraction so the phase-managed release exists
  once rather than twice. No behaviour change; the existing release path in
  `_finish` is unchanged.

**No change proposed** for hazard 1 (needs a coordinated change to what a record
means on failure and to both `owned` assignments), hazard 2 (needs a durable
marker written before `systemctl start` — a protocol and documentation change),
hazard 3 (a swap that would trade the crash window for a `DoubleDuty` if the
unlink fails), hazard 6 (a startup sweep belongs to `runtime.py`), hazard 7 (a
record schema version), or hazard 9 (unreachable from the current callers).
Follow-ups worth tracking: make the start durable (owner marker), acknowledge a
release only after its effect, and sweep expired records at startup.

## Verification

Run from the repository root, offline. `scripts/check_unit_tests.py` runs each
unit in its own temporary XDG tree.

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_15_local_image_gpu.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_local_images.py
.venv/bin/python scripts/check_unit_tests.py -p test_local_image_warm.py
.venv/bin/python scripts/check_unit_tests.py -p test_image_recovery.py
.venv/bin/python scripts/check_unit_tests.py -p test_gui_images.py
.venv/bin/python scripts/check_unit_tests.py -p test_local_image_preflight.py
.venv/bin/python -m ruff check src/blackrose/local_images.py tests/test_sm_15_local_image_gpu.py
.venv/bin/python -m mypy
```

`tests/test_sm_15_local_image_gpu.py` pins the transitions, invariants and the
two fixes above; the other suites cover the local image provider, warm record,
recovery and picker behavior those fixes must preserve. Both regression tests
(`test_a_record_nobody_can_describe_is_released_instead_of_stranded` and
`test_a_failed_start_does_not_latch_recovery`) fail against the module without
the fixes and pass with them.
