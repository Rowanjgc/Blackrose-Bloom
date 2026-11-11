# SM-15: Local image GPU handoff, keep-warm window and ComfyUI service ownership

## Scope and ownership

| Surface | Location |
| --- | --- |
| `ComfyService` (`owned`, `ensure_running`, `release`, `free`, `_systemctl`) | `src/blackrose/local_images.py:80-148` |
| `WarmRecord` (durable warm model + stop duty) | `src/blackrose/local_images.py:164-177` |
| `_Resident`, `_Session` | `src/blackrose/local_images.py:180-199` |
| `LocalImages` handoff: `_lock`, record I/O, `_adopt`, `_release_record`, `_expire`, `_keep_warm` | `src/blackrose/local_images.py:212-420` |
| Session lifecycle: `_job`, `_prepare_memory`, `_settle`, `_finish`, `_run_shielded`, `batch`, `generate` | `src/blackrose/local_images.py:557-810` |
| Chat-model preemption and the recovery refusal | `src/blackrose/chat.py:242,305,323-342`, `src/blackrose/turns.py:566` |
| Picker status / orphan reap / Release now | `src/blackrose/gui/session.py:1753-1818` |
| Shutdown | `src/blackrose/chat.py:255-256` |
| User-facing contract | `docs/architecture.md:227-247` |

Two different owners meet in this module, and the architecture page is explicit
about both (`docs/architecture.md:229-247`):

- **The GPU** is owned by whoever holds `local-images.lock` (`fcntl.flock`,
  `local_images.py:247-267`). The lock is cross-window, not cross-user, and is
  held through job execution, GPU cleanup and chat-model restoration. Every
  mutation of the warm record and every `ComfyService.owned` transition happens
  inside it. The unlocked readers are `status()` (`:394`), `make_room_for_chat`'s
  first look (`:373`), `reap_orphan` (`:416`) and `_release_record`'s fast path
  (`:324`); each decision is re-read under the lock before it acts (`:327`).
- **The `comfyui.service` unit** is owned by *the process that started it*, and
  that duty is transferable: it moves from `ComfyService.owned` to
  `WarmRecord.started_service` when the model is left warm (`:616-622`) and back
  on adoption (`:353-367`) or release (`:317-351`). The record docstring states
  the rule: "carries the duty to stop comfyui.service with the record, whichever
  window releases it" (`:164-171`).

There is exactly one `LocalImages` and one `asyncio` loop per desktop window
(`src/blackrose/runtime.py:64`); the flock is what serializes windows.

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `ComfyService.owned` | `local_images.py:85` | bool | this process started the unit and owes a stop; false means an external unit (release = `/free`) | `ensure_running:114`, `release:131`, `_keep_warm:621`, `_release_record:338`, `_adopt:363` |
| `local-images.lock` | `:222`, `:254-267` | `flock` on an fd | the handoff: one window may execute a job, adopt or release | `_lock` |
| `local-images.warm.json.token` | `:172`, `:617` | hex uuid | identity of one warm window; `'invalid'` from `:279` | `_keep_warm` under the lock |
| `.pid` | `:173`, `:617` | int | the window that last kept it warm (`0` for the sentinel) | same |
| `.until` | `:174` | float epoch | deadline of the warm window; `0.0` for the sentinel | same |
| `.headroom` | `:175` | int bytes | the measured headroom the resident model was checked against | same |
| `.chat_model` | `:176` | str | the chat model resident beside it (`''` when unknown) | same |
| `.started_service` | `:177`, `:618` | bool | **the stop duty**: true means the record, not a process, owes the stop | same |
| `LocalImages._phase` | `:231` | `None \| 'loading' \| 'generating' \| 'releasing'` | what this process is doing to the GPU, for display | `_job:687,708,721`, `_release_record:335,347`, `_release_service:610,614` |
| `LocalImages._timer` | `:230` | `asyncio.Task \| None` | the idle expiry worker for this window's record | `_arm_timer:298-301`, `_cancel_timer:291-296`, `_adopt:359-361`, `_release_record:348-350` |
| `LocalImages.recovery_error` | `:227` | `str \| None` | sticky in-process latch: GPU memory recovery failed | `:342`, `:635`, `:640` |
| `_Session.ready` | `:193` | bool | a job reached the service (queue checked empty) | `_job:696,725` |
| `.jobs` | `:194` | int | how many jobs this hold ran | `_job:681` |
| `.resident` | `:195` | `_Resident \| None` | the image model this session has loaded / adopted | `_adopt:365,367`, `_prepare_memory:655`, `_job:738` |
| `.restore` | `:196` | `str \| None` | a chat model this session displaced and must reload | `_prepare_memory:670` |
| `.failed` / `.cancelled` | `:197-198` | bool | the job reached the service and then failed / was stopped | `_job:735,740`, `batch:775-776` |
| `_Resident.shared` | `:186` | bool | the chat model stayed loaded beside the image model | `_adopt:365,367`, `_job:738` |
| `_Resident.until` | `:186` | `float \| None` | the original deadline of an adopted record | `_adopt:365` |
| record on disk | `:281-285` | file | written to `<name>.<pid>.tmp` then `os.replace` (atomic) | `_write_record` |
| `SHARING_MODELS`, `IDLE_RELEASE_SECONDS`, `SHARING_RAM` | `:41-52` | constants | who may share, for how long, with how much RAM | — |

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
| `Adopted` | `session.resident` set by `_adopt` (`:365` valid, `:367` sentinel) |
| `Ready` | `session.ready == True` — a prompt may be submitted |
| `Displaced` | `session.restore is not None` — a chat model was unloaded (`:670`) |
| `Settling` | inside the shielded `_settle` (`:731-733`) |
| `Finishing` | inside the shielded `_finish` (`:780`, `:808`) |
| `Tainted` | `recovery_error` is set; process-scoped, survives every session |

**Display states** (`status():389-399`, checked in this order — phase, then
record, then lock):

| State | Predicate |
| --- | --- |
| `loading` | `_phase == 'loading'` |
| `in_use` | `_phase == 'generating'` (`:391`), or `_phase is None`, no record, lock held (`:399`) |
| `releasing` | `_phase == 'releasing'` (wins over an existing record, because the record is removed in the same block) |
| `loaded` | `_phase is None`, record present, with `releases_at`/`seconds_left`/`this_window` |
| `not_loaded` | `_phase is None`, no record, lock free |

## Events

- `generate()` with no batch: acquire the lock, adopt, one job, finish
  (`:789-810`).
- `generate()` inside `batch()`: reuse the session, no lock, no finish
  (`:798-801`); `batch()` enter/exit (`:758-787`), opened by
  `ChatController.image_round` (`chat.py:333-339`) and used by
  `turns.py:707-710`.
- `release_now()` from the picker (`:382-387`, `gui/session.py:1814-1818`).
- `make_room_for_chat(model)` before a chat model load or switch
  (`:371-380`, `chat.py:242,305,323-330`, `turns.py:566`).
- `reap_orphan()` before the picker's status read (`:414-420`,
  `gui/session.py:1802-1810`).
- `close()` at shutdown (`:233-245`, `chat.py:255-256`).
- Idle timer expiry (`_expire:303-315`) and its 5 s retry on a busy lock (`:309-312`).
- Cancellation (Stop) at any await: captured into `_Session` (`:718-719`,
  `:775-776`), and absorbed by `_run_shielded` around `_settle`/`_finish`
  (`:624-642`).
- Process death (window closed by SIGKILL/crash) and a recycled pid
  (`_alive:202-209`).
- Record damage or a record from another version (`_read_record:271-279`).

## Transition table

Service duty (`owned` = this process's flag, `r` = the record on disk):

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `External` | `ensure_running`, service answers | `/system_stats` 200 (`:103-105`) | none | `External` |
| `External` | `ensure_running`, nothing listening | `httpx.ConnectError` (`:106-108`) | probe `systemctl is-active` (`:110-112`) | `External` |
| `External` | `ensure_running`, unit inactive | `is-active` exit ≠ 0 (`:113`) | `owned = True` **then** `systemctl start` (`:114-115`), poll `/system_stats` (`:116-125`) | `ProcessOwned` |
| `External` | `ensure_running`, unit already active | `is-active` exit 0 (`:112`) | none — a starting unit keeps its original owner (`:107-108`) | `External` |
| `ProcessOwned` | `_finish` keeps warm | `idle_release>0`, `resident.shared`, chat model, not failed/cancelled (`:592-593`), RAM free (`:596-597`) | write `r(started_service=owned)` (`:618-620`), `owned = False` (`:621`), arm timer (`:622`) | `RecordOwned` |
| `ProcessOwned` | `_finish` releases | keep false (`:602`) | `_release_service` → `service.release` → `systemctl stop`, `owned = False` (`:129-131`) | `External` |
| `ProcessOwned` | job failed before submission | `session.ready or owned` forced ready (`:722-725`) then `_finish` release | stop (`:129-131`) | `External` |
| `RecordOwned` | a job adopts | record readable (`:355`) | `_remove_record` (`:358`), cancel this window's timer (`:359-361`), `owned = True` (`:362-363`) | `ProcessOwned` |
| `RecordOwned` | `make_room_for_chat` / `release_now` / timer / reap | record present, lock free (`:326`), token matches (`:328`), `until` past for `expired_only` (`:332`), pid dead for reap (`:417`) | `owned = True` from `r` (`:337-338`), `stop` or `/free` (`:340`), record removed (`:345`) | `External` |
| `RecordExternal` | same events | `record.started_service == False` | `service.release` → `free` (`:133,135-148`), record removed (`:345`) | `External` |
| `RecordOwned`/`RecordExternal` | `_release_record` with a newer token | `r.token != token` (`:328`) | none; `False` | unchanged |
| `RecordOwned`/`RecordExternal` | `_release_record(expired_only)` on a live window | `r.until > now` (`:332`) | re-arm the timer (`:333`) | unchanged |
| `RecordOwned`/`RecordExternal` | `close` of another window | `owner_only` and `r.pid != os.getpid()` (`:330`) | none | unchanged |
| `RecordOwned`/`RecordExternal` | `_finish` after a job, shared keep | as `ProcessOwned` keep row | new record: new token, `pid = this`, `owned = False` (`:616-622`) | same duty state, new owner window |
| `RecordOwned`/`RecordExternal` | `_finish` with no job reached the service | resident re-describable (`until` and `chat_model`, `:584`) | put back with the same `until`, new token (`:585`) | same duty state |
| `RecordOwned`/`RecordExternal` | same, resident not re-describable | `:586` (this change) | `_release_service` | `External` |
| `any` | record write raises | `_write_record` (`:620`) | `_run_shielded` latches `recovery_error` (`:640`); `owned` keeps the duty, no record | `ProcessOwned` (stranded) |
| `any` | release raises | `:340-343` | latch `recovery_error` (`:342`), record still removed (`:345`), `owned` stays set (`:131` not reached) | `ProcessOwned` + `Tainted` (hazard 1) |
| `ProcessOwned` | process death before any record write | — | flag dies with the process; no record exists | `External` with the unit still running (hazard 2) |
| `RecordOwned` | owner process death | — | record survives, still adoptable; `reap_orphan` waits for `until` (`:417`) | `RecordOwned` |

Session lifecycle:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Idle` | `generate` (no batch) | `_batch.get() is None` (`:798`) | lock (`:802`), re-check `recovery_error` (`:803-804`), `_adopt` (`:806`) | `Adopted` |
| `Idle` | `batch()` | no session in context (`:761-763`) | lock (`:766`), `_adopt` (`:768`) | `Adopted` |
| `Adopted` | `_job` starts | — | `_phase = 'loading'` (`:687`) | `Adopted` |
| `Adopted` | service reached, queue empty | `ensure_running` returned, `_queue()` empty (`:692-696`) | `session.ready = True` (`:696`) | `Ready` |
| `Ready` | memory can be shared | `_can_share` (`:664-666`) | `resident = _Resident(..., shared=True)` (`:736-738`) | `Ready` |
| `Ready` | memory must be exclusive | `holds_memory` and no share (`:668`) | `session.restore = chat_model` (`:670`) **before** `backend.unload` (`:671`) | `Displaced` |
| `Ready` | prompt submitted, downloaded | `/prompt` accepted (`:703-707`) | `_phase = 'generating'` (`:708`), `resident.headroom = max(required, previous)` (`:738`) | `Ready` |
| `Ready` | prepare refused (foreign VRAM) | `:672-675` | failure, nothing submitted | `Ready` (then `Finishing`) |
| any | job ends (success, failure, Stop) | — | `_phase = None` (`:721`); settle only when a prompt exists or the service was reached (`:722-733`) | `Settling` |
| `Settling` | queue drained | `_queue()` empty (`:573-575`) | `_phase` untouched | `Finishing` |
| `Finishing` | keep | `:592-601` | record written, timer armed | `Idle` |
| `Finishing` | release | `:602-603` | `_release_service` | `Idle` |
| `Finishing` | restore | `session.restore` (`:604-606`) | one `backend.load`, after the release | `Idle` |
| any | cancellation at an await | `_run_shielded` (`:624-642`) | worker completes; the cancellation is returned and re-raised after recovery | same state until `Idle` |
| `Idle` | `generate`/`batch` with a latched error | `:764-765`, `:792-793` | `RuntimeError(recovery_error)` before any request or model load | `Tainted` |

## Invariants

1. **One duty.** In the absence of a recorded failure, at most one of
   `{record.started_service == True, some process's owned == True}` holds.
   Enforced because every writer holds the lock (`:248-267`): `_keep_warm` writes
   the record *then* clears the flag (`:620-621`), `_adopt` removes the record
   *then* sets the flag (`:358`, `:362-363`), and neither window can interleave
   (no await between the pair; the lock excludes the other window).
   Pinned by `test_a_warm_handoff_moves_the_stop_duty_to_the_record`,
   `test_adoption_removes_the_record_before_this_process_owes_the_stop`,
   `test_preempting_an_owned_warm_model_leaves_no_owner_at_all`.
2. **Every record mutation is under the lock.** `_write_record`/`_remove_record`
   are called only from `_keep_warm` (reached from `_finish` at `:780`/`:808`),
   `_release_record` (`:326-345`) and `_adopt` (`:353-358`). `_read_record`
   (`:271`) is also called without the lock from `status:394` and `reap_orphan:416`;
   every decision is taken again under it (`:327`).
3. **A record is either the old one or the new one.** Writes are
   `<name>.<pid>.tmp` + `os.replace` (`:281-285`), so a reader never parses a
   half-written record; a damaged record is read as the sentinel (`:279`) rather
   than as a plausible one.
4. **While a record exists, it is the only account of the stop.** `release()`
   stops the unit only when `owned` is true (`:129`), and `_release_record` takes
   `owned` from `record.started_service` (`:337-338`) before acting; `_adopt`
   does the same (`:362-363`). Pinned by
   `test_preempting_an_external_service_only_frees_its_models`,
   `test_an_orphan_reap_stops_the_service_the_record_owes`.
5. **A record is adopted at most once.** Adoption removes it under the lock and
   every release path re-checks the token (`:328`). Pinned by
   `test_a_release_for_another_token_is_refused` and
   `test_closing_a_window_never_releases_another_windows_model`.
6. **The idle timer cannot release a window it does not describe.** `_expire`
   passes its token (`:307`) and `expired_only` re-arms when the deadline moved
   (`:332-334`). Pinned by `test_an_unexpired_record_is_rearmed_instead_of_released`.
7. **A dead window's model is reaped only after its deadline.** `reap_orphan`
   requires `until <= now` *and* a dead pid (`:417`). Pinned by
   `test_a_dead_owner_keeps_its_warm_model_until_the_deadline`.
8. **No job runs outside the handoff lock.** `generate` (`:802`) and `batch`
   (`:766`) are the only callers of `_job`. Pinned indirectly by
   `test_the_handoff_lock_is_free_after_a_failed_submission` and
   `test_cancellation_restores_the_chat_model_once`.
9. **GPU recovery is cancellation-proof and runs once.** `_run_shielded` awaits a
   shielded task in a loop and only abandons it when the worker itself was
   cancelled (`:624-642`). Pinned by `test_cancellation_restores_the_chat_model_once`
   and `tests/test_image_recovery.py:66-84`.
10. **The chat model is restored at most once per session, after the image
    memory.** `session.restore` is written once, before the unload (`:670`), and
    consumed once in `_finish` (`:604-606`); `_finish` is called exactly once per
    session (`:780`, `:808`) and `_job` converts every job failure into a return
    value (`:712-719`). Pinned by
    `test_a_batch_restores_the_chat_model_once_after_its_last_job`,
    `test_a_failed_job_inside_the_batch_still_restores_the_chat_model`,
    `test_cancellation_restores_the_chat_model_once`.
11. **A failed release is not followed by a restore.** `_release_service` lets the
    exception out of `_finish` before `:604`, and `_run_shielded` latches it.
    Pinned by `tests/test_image_recovery.py:28-38` and
    `test_a_failed_release_latches_recovery_and_refuses_later_work`.
12. **`recovery_error` is monotone.** It is assigned `None` only in the
    constructor (`:227`) and a message only at `:342`, `:635`, `:640`; nothing
    clears it. Every entry point refuses before contacting a provider
    (`:764-765`, `:792-793`). Pinned by
    `test_a_failed_release_latches_recovery_and_refuses_later_work` (unchanged
    after `release_now`, `generate` and `batch`).
13. **`_phase` is written only by a lock holder and always cleared in a
    `finally`.** `:335/:347`, `:610/:614`, `:687/:721`. Pinned by
    `test_the_phase_follows_the_job_from_loading_to_generating`,
    `test_the_releasing_phase_is_reported_while_the_release_is_in_flight`,
    and the `_phase is None` assertions after cancellation and failure.
14. **Display precedence is phase, record, lock** (`:389-399`), so a window never
    reports a stale `loaded` while it is releasing. Pinned by the releasing-phase
    test and by `test_the_phase_follows_the_job_from_loading_to_generating`
    (`loaded` with `this_window` and a fresh deadline).
15. **Nothing of ours is held after a failed submission.** The lock is released
    by the context manager (`:264-267`), and no prompt is cancelled when no prompt
    id was assigned (`:559-563`); a submitted prompt with a lost response is still
    interrupted by its known id (`:559-566`). Pinned by
    `test_the_handoff_lock_is_free_after_a_failed_submission` and
    `tests/test_local_images.py:185-191`.

## Hazards and open questions

Answers to the bead's focus questions, then the numbered findings.

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
  so `_systemctl` (`:98-99`) does not turn a duplicate stop into an error. Leaks
  exist only after a failure or a crash (hazards 1-3).
- *What if the process dies between writing the warm record and releasing the
  flock?* Safe. The record is durable and self-describing before the flag is
  cleared (`:620-621`), and the OS drops the flock. Another window may adopt it
  and reuse it; `reap_orphan` stops the unit it owes once `until` passes. The
  model stays loaded until then — that is the warm window's purpose.
- *What if it dies between `systemctl start` and writing the record?* A leak:
  `owned` is set before the start (`:114`) and only becomes durable at `:620`.
  Nothing on disk mentions the start, so the unit keeps running unowned
  (hazard 2).
- *Is `_phase` consistent with the session record at every await?* Yes. Phase is
  only written while holding the handoff lock (`:335`, `:610`, `:687`), and each
  writer clears it in a `finally`, so a session that is waiting in `_settle`,
  `_finish` or the shielded recovery has either the correct phase or no record
  and a held lock (`in_use`, `:399`). The one precedence quirk is deliberate:
  during `_release_record` the record still exists while the phase says
  `releasing` (`:335` before `:345`), and `status` reports the phase.
- *Can `recovery_error` be cleared?* No — nothing writes `None` after
  construction (`:227` vs `:342`, `:635`, `:640`). It is process-scoped, so
  restarting the desktop is the documented escape
  (`docs/architecture.md:224-227`) and `require_recovered_memory`
  (`chat.py:335-342`) also blocks model switches meanwhile.
- *Does the chat-model restore run exactly once on every exit path, including
  cancellation?* Yes: `_finish` is shielded and called once per session, and
  `session.restore` is set before the unload (`:670`). Two caveats are by
  design: no restore when the release failed (invariant 11), and no restore when
  `_adopt` itself raises before the session's `try/finally` (only an unlink
  failure at `:287-289`, hazard 3).

**1. `fragile` — `local_images.py:340-345` — a failed release destroys the durable
stop duty.** `_release_record` removes the record in the `finally` even when
`service.release()` raised, and `ComfyService.release` leaves `owned = True`
because `:131` was not reached. After that, every retry surface needs a record:
`_release_record` returns at `:324-325` when there is none, so `release_now`
(`:384-387`) and `close` (`:239`) do nothing, and `_finish` is unreachable
because `:342-343` latched `recovery_error` and jobs refuse at `:764`/`:792`. Net
effect: a unit Blackrose started keeps running, owed only by a flag no code path
reads, until the process exits — and then by nobody. Not demonstrated with a
test (a test would have to pin the leak). Would confirm by raising from
`service.release`, then calling `release_now()` and `close()` and observing no
second `release` call and `service.owned == True`. The fix is a coupled pair:
remove the record only after a successful release, and make
`record.started_service` authoritative for `owned` at both `:337-338` and
`:362-363` (so a stale in-memory flag cannot stop an externally started unit
later). Left as a proposal because it changes what a record means during a
failure (today the record is a claim that the model is warm).

**2. `fragile` — `local_images.py:114-115` and `:616-622` — a crash before the
first record write leaves a started unit with no owner.** `owned = True` precedes
`systemctl start`; the duty only becomes durable when `_finish` writes the
record. A death between the start and that write (a long job, an OOM kill, a
desktop crash) loses the duty with the process. `reap_orphan` has no record to
read (`:414-420`), and the next window's `ensure_running` finds the unit active
and treats it as external (`:109-112`), so it will never stop it — only `/free`
its models. Not demonstrated (needs process death); would confirm by SIGKILL in
`_job` after `ensure_running` and checking that the state directory has no record
while `systemctl --user is-active comfyui.service` is still true. A fix makes the
start durable before it happens (a separate owner marker, or a record written at
start) — a protocol change, so not made.

**3. `fragile` — `local_images.py:358-363` — `_adopt` removes the record before
taking the flag.** There is no await between the two statements, so cancellation
cannot land there, but process death can: the record is gone and no process holds
the duty (same leak as hazard 2, one file operation wide). Not demonstrated. A
one-line swap would narrow it, but if `_remove_record` then raised
(`contextlib.suppress` covers only `FileNotFoundError`, `:287-289`) `_adopt`
would leave `owned = True` with the record still on disk — a `DoubleDuty` — and
`_adopt` is called before the session's `try/finally` (`:768`, `:806`), so the
raise would also skip `_finish`. Documented rather than half-fixed.

**4. `defect` (fixed) — `local_images.py:581-591` — a resident that cannot be
described again was silently dropped.** `_adopt` models an unreadable record as
`_Resident(0, None, False)` whose comment says "freed at first use" (`:367`),
but the no-job path only re-kept residents with both `until` and `chat_model`
(`:584`) and otherwise returned — with the record already removed (`:358`) and no
timer. Nothing then owned the still-loaded model, and `close()` could not release
it (`_release_record` returns at `:324`). The record can be unreadable through
`_read_record`'s sentinel (`:279`: truncated, missing key, or a key renamed by a
version-skew write),
and a blank `chat_model` reaches the same branch. Now the fall-through releases
it (`:586-590`). Regression test:
`test_a_record_nobody_can_describe_is_released_instead_of_stranded` (fails
before, passes after); success side:
`test_an_adopted_record_that_can_be_described_is_put_back_unchanged`.

**5. `defect` (fixed) — `local_images.py:722-733` — a start failure latched the
sticky recovery error.** `_job` settled whenever it owned the service, and
`_settle` polls `GET /queue` (`:573-575`). After `systemctl start` fails the
service is not listening, so that poll raised `httpx.ConnectError`;
`_run_shielded` latched "Local image memory recovery failed" (`:640`) although
nothing was submitted, no recovery was needed, and the failure had already been
cleaned up by the stop in `_finish`. From then on `require_recovered_memory`
(`chat.py:335-342`) refused every image job *and* every chat-model switch until
the desktop restarted, with a message unrelated to the real cause. Now the
settle is skipped when no prompt was submitted and the service was never reached
(`:722-733`), while the normal, lost-response and owned-service paths still
settle. Regression test: `test_a_failed_start_does_not_latch_recovery` (fails
before: latched error and the next job refused; passes after).

**6. `fragile` — `local_images.py:303-315` and `:414-420` — reaping is lazy.**
An expired record is released by the writing window's timer, by a picker status
read (`gui/session.py:1807`), or by a later job's adoption. If the owner died and
no window opens the picker, `comfyui.service` keeps its model resident well past
the 120 s window. Bounded by user action, not by the code; the architecture page
says "any window's next job reuses it" but not who sweeps it
(`docs/architecture.md:237-241`). Pinned as *intended* by
`test_a_dead_owner_keeps_its_warm_model_until_the_deadline`; the follow-up is a
sweep at startup (`Runtime.open`) or in `image_providers`, which today only runs
when the dialog opens.

**7. `documentation-gap` — `local_images.py:271-279` — the record is read more
leniently than it is written.** `_read_record` copies the six known keys with no
type or range validation, ignores extra keys, and returns the sentinel only for
a missing key, bad JSON or an unreadable file. `"until": null` therefore becomes
`until=None`, which lands in the "cannot be described" branch and now *releases*
the user's warm model (fix 4) — safe, but a version-skew write can silently
discard a warm model. The sentinel's `pid=0` also makes `_alive(0)` true
(`os.kill(0, 0)` addresses the process group), so `reap_orphan` never clears a
sentinel; only adoption does. Not demonstrated; would confirm by writing
`{"token": "warm-token", "pid": 0, "until": null, "headroom": 0, "chat_model": "",
"started_service": false}` and watching the next batch free the model. A
`WarmRecord` schema version is the obvious follow-up.

**8. `none-found` — `local_images.py:401-412` — `_lock_held_elsewhere` also
answers "in use" for this process's own lock.** `flock` on a second open file
description conflicts even inside one process, which is exactly what `status`
wants while a session holds the lock between awaits. No action.

**9. `fragile` (theoretical) — `local_images.py:228-229` — the batch
contextvar is inherited by child tasks.** `_batch` is a `ContextVar`, so an
`asyncio.create_task`/`gather` started inside `batch()` would inherit the
session, and two `_job` calls would mutate one `_Session` (`resident`, `restore`,
`jobs`) concurrently with no lock, because `generate` skips the lock when a
session is present (`:798-801`). Today unreachable: `turns.py:707-710` executes a
round's calls in a plain `for` loop. Would confirm by gathering two `generate`
calls inside `batch()`; the fix (a per-session lock or a "one job at a time"
assertion) is not needed for the current callers.

## Formalization decisions

**Two source changes, each with a regression test.** Both are 3-6 lines in the
module this bead owns, neither touches the trust boundary or the record format.

- `_finish` (`:586-590`): release an adopted resident that cannot be put back
  faithfully, instead of returning with the record already gone. Fixes hazard 4
  (a model running with no record, no timer and no owner).
- `_job` (`:688`, `:722-733`): settle only when a prompt was submitted or the
  service was actually reached. Fixes hazard 5 (a plain start failure latching
  the sticky, misleading `recovery_error`).
- `_release_service` (`:608-614`): a two-line extraction so the phase-managed
  release exists once rather than twice. No behaviour change; the existing
  release path (`:603`) is unchanged.

**No change proposed** for hazard 1 (needs a coordinated change to what a record
means on failure and to both `owned` assignments), hazard 2 (needs a durable
marker written before `systemctl start` — a protocol and documentation change),
hazard 3 (a swap that would trade the crash window for a `DoubleDuty` if the
unlink fails), hazard 6 (a startup sweep belongs to `runtime.py`), hazard 7 (a
record schema version), or hazard 9 (unreachable from the current callers).
Follow-ups worth beads: make the start durable (owner marker), acknowledge a
release only after its effect, and sweep expired records at startup.

## Verification

```
.venv/bin/python scripts/check_unit_tests.py -p test_sm_15_local_image_gpu.py -v
    Ran 21 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_local_images.py
    Ran 43 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_local_image_warm.py
    Ran 20 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_image_recovery.py
    Ran 7 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_gui_images.py
    Ran 9 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_local_image_preflight.py
    Ran 5 tests … OK
.venv/bin/python -m ruff check src/blackrose/local_images.py tests/test_sm_15_local_image_gpu.py
    All checks passed!
.venv/bin/python -m mypy
    Success: no issues found in 19 source files
git diff --check
    (clean)
```

Both regression tests were confirmed to fail against the unmodified module and
pass with the change (`Ran 21 tests … FAILED (failures=2)` before). The whole
suite (`.venv/bin/python scripts/check_health.py`) is dominated by other beads'
in-flight files, so its counts move between runs; the last run of
`.venv/bin/python scripts/check_unit_tests.py` reported `Ran 1378 tests … FAILED
(errors=5, skipped=23)` with every error in `test_motion_canvas`/`test_motion_plan`
(the Motion Canvas toolchain is not installed in this checkout) and
`scripts/check_recovery.py` stops at the same missing toolchain. No test touching
local images, the handoff lock or the warm record fails.
