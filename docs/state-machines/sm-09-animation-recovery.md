# SM-09: Animation playback recovery and the Motion Canvas probe

## Scope and ownership

- `src/blackrose/animation_recovery.py`: the durable ledger. `AnimationRecovery`,
  its `_connect`/schema, `states`, `claim`, `finish`, `delete`; `MAX_REPAIRS = 2`.
- `src/blackrose/gui/session.py`: the in-memory half. Fields set in
  `Session.__init__`, `close`, `_transcript_payload` (the recovery projection),
  `_attachment_payload` (recovery in the payload), `stop`, `animation_failed`,
  `_wait_for_repair_slot`, `_repair_animations`,
  `_finish_failed_animation_repair`.
- `src/blackrose/tool_execution.py`: records the replacement link at save time
  (`src/blackrose/tool_execution.py::ToolExecutor._animation_create`, with `_normalized_arguments` and `definitions`) and
  holds the one-reply plan state (`src/blackrose/tool_execution.py::ToolExecutor._animation_plan`,
  `src/blackrose/tool_execution.py::ToolExecutor._animation_create_planned`).
- `src/blackrose/tool_host.py`: the narrow
  `src/blackrose/tool_host.py::ToolHost.finish_animation_repair` capability.
- `src/blackrose/motion_canvas.py`: `PLAYBACK_TIMEOUT`, `validate_scene`,
  `compile_source`, `probe_document` and its cache, `_run_probe`,
  `motion_report`, `prepare_scene`.
- `src/blackrose/motion_canvas_probe.py`: the disposable child. `main` sets up
  GTK, loads the probe document with its lockstep script, and runs the polling
  driver (`evaluated`).
- `src/blackrose/gui/static/motion-canvas-frame.js`: the trusted adapter inside
  the opaque-origin frame and its command handler.
- `src/blackrose/gui/static/motion-canvas-player.js`: page-side recovery label
  and the failure report (`setRecovery`, `showError` inside `mount`, and the
  exported `recovery`); `src/blackrose/gui/static/bloom.js::attachmentChips`
  mounts it with `recovery` and `onFailure`, and
  `src/blackrose/gui/static/bloom.js::on.animation_recovery` routes
  `animation_recovery` events.
- `src/blackrose/motion_plan.py`: `validate_plan`, `element_report`,
  `assemble_source`.

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[animations](../animations.md) / [Motion Canvas](../motion-canvas.md):

- The **ledger** is auxiliary SQLite under the attachment store
  (created in `src/blackrose/chat.py::ChatController.__init__`), owned by the one
  desktop process (ADR 0001). Every operation is a function run on a worker
  thread through `src/blackrose/storage/db.py::run_to_completion`, which finishes the thread's work before it re-raises
  a caller cancellation. `AnimationRecovery._lock` serializes those operations
  *and* is the writer exclusion that lets an artifact report open the ledger
  read-only with `immutable=1` (`src/blackrose/artifacts.py::ArtifactLifecycle.report`,
  `src/blackrose/storage/snapshot.py::readonly_snapshot`).
- **`Session`** (GUI asyncio loop) owns the failure queue, the recovery cache,
  the single `_animation_repair_task`, `_repairing_animation_id` and the
  reservation of `_generation` for the repair turn. It never owns the durable
  status: it only calls `claim`/`finish`.
- **`ToolExecutor`** (inside the repair turn) owns the one write that links a new
  attachment to the chain, before the attachment is published (`src/blackrose/tool_execution.py::ToolExecutor._animation_create`).
- The **page** owns only presentation. It reports one failure per mount and
  never mutates recovery state (`src/blackrose/gui/static/motion-canvas-player.js::showError`).
- **`motion_canvas`** owns compilation and the probe *parent*: the deadline, the
  process group, and every guard on the returned result. The **probe child** owns
  the disposable GTK/WebKit process; its caches, HSTS list and storage salts are
  ephemeral and in memory (`src/blackrose/motion_canvas_probe.py::main`).
  Generated TSX runs only in an opaque `sandbox="allow-scripts"` frame with no
  bridge (the probe document in `main`; `src/blackrose/motion_canvas.py::player_document`).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `repairs.conversation_id`, `attachment_id` | `src/blackrose/animation_recovery.py::AnimationRecovery._connect` | `INTEGER`, composite PK | Chain member identity; one row per attachment forever | `claim` insert only |
| `repairs.root_id` | `src/blackrose/animation_recovery.py::AnimationRecovery._connect` | `INTEGER NOT NULL` | First attachment of the chain | `claim` (from the parent, or `attachment_id` for a new root) |
| `repairs.attempt` | `src/blackrose/animation_recovery.py::AnimationRecovery._connect` | `INTEGER NOT NULL` | Position in the chain, `1..MAX_REPAIRS+1` | `claim` |
| `repairs.status` | `src/blackrose/animation_recovery.py::AnimationRecovery._connect` | `TEXT NOT NULL`, no CHECK | `repairing` / `exhausted` / `replaced` / `failed` / `stopped` | `claim`, `finish` |
| `repairs.error` | `src/blackrose/animation_recovery.py::AnimationRecovery._connect` | `TEXT NOT NULL` | First bounded diagnostic (untrusted scene output) | `claim`, truncated to 1000 |
| `repairs.replacement_attachment_id` | `src/blackrose/animation_recovery.py::AnimationRecovery._connect` | `INTEGER` nullable | The saved attachment that now supersedes this one; the *parent link* | `finish`; `NULL` at insert |
| `_pending_animation_failures` | `src/blackrose/gui/session.py::Session.__init__` | `dict[(conversation_id, attachment_id), str]` | Accepted but unclaimed failures | `animation_failed`, `stop`, `close`, `_repair_animations` |
| `_animation_recovery_states` | `src/blackrose/gui/session.py::Session.__init__` | `dict[attachment_id, dict]` | Cache/dedup and the value sent to the page | `animation_failed`, `stop`, `_repair_animations`, `_finish_failed_animation_repair`; wholesale replaced by `_transcript_payload` |
| `_repairing_animation_id` | `src/blackrose/gui/session.py::Session.__init__` | `int \| None` | The attachment whose `repairing` row this process owns | `_repair_animations` |
| `_animation_repair_task` | `src/blackrose/gui/session.py::Session.__init__` | `asyncio.Task \| None` | The one repair loop | `animation_failed`; cleared in its own `finally` |
| `_generation` | `src/blackrose/gui/session.py::Session.__init__` | `asyncio.Task \| None` | Global turn reservation, held by the repair turn | `_wait_for_repair_slot`; released by `_repair_animations` and `_send` |
| `controller.automatic_animation_repair` | `src/blackrose/tool_host.py::ToolHost.automatic_animation_repair`; read in `src/blackrose/gui/session.py::Session.animation_failed`, `src/blackrose/gui/session.py::Session._repair_animations`, `src/blackrose/turns.py::TurnRunner._prepare` | `bool` | Cloud-model policy gate for automatic repair | configuration/provider state |
| `figure.dataset.recovery`, `options.recovery` | `src/blackrose/gui/static/motion-canvas-player.js::setRecovery` | `string` | Page label and the "already reported" marker | `setRecovery` only |
| `PLAYBACK_TIMEOUT` | `src/blackrose/motion_canvas.py::PLAYBACK_TIMEOUT` | `int` seconds | The only execution bound for the probe | module constant |
| `window.probeResult`, `targets`, `index`, `samples` | the probe document in `src/blackrose/motion_canvas_probe.py::main` | JS null/array/int | Probe protocol state | the probe document only |
| frame `ready`, `failed`, `inspection` | `src/blackrose/gui/static/motion-canvas-frame.js` | JS bool/null | Frame lifecycle and the single pending seek | frame only |
| `result['duration']`, `samples` | `src/blackrose/motion_canvas.py::_run_probe` | dict | Validated probe output | parent, after guards |
| `scene['duration']`, `scene['cues']` | `src/blackrose/motion_canvas.py::prepare_scene` | number/list | Measured duration replaces metadata; cues clamp to it | `prepare_scene` |
| `motion` dict | `src/blackrose/motion_canvas.py::prepare_scene`; overridden in `src/blackrose/chat.py::ChatController._create_animation` | dict | `assessment`/`last_motion`/`duration`/`notice`/`elements`/`reviewed` | `prepare_scene`, visual review |

## States

Durable chain, exactly one per row (`status` is the only discriminator):

| State | Predicate |
| --- | --- |
| `ABSENT` | no row for `(conversation_id, attachment_id)` |
| `REPAIRING` | `status='repairing'` (implies `attempt <= 2`, `replacement_attachment_id IS NULL`) |
| `EXHAUSTED` | `status='exhausted'` (implies `attempt = 3`) |
| `REPLACED` | `status='replaced'`; `replacement_attachment_id` should be non-null (H2) |
| `FAILED` | `status='failed'` |
| `STOPPED` | `status='stopped'` |
| `OUT_OF_VOCABULARY` | any other string; no writer in `src/` produces one |

Derived projections (no row, or a display-only rewrite):

| Projection | Predicate | Consumer |
| --- | --- | --- |
| `QUEUED` | failure accepted, cached as `{'status': 'queued'}`, not yet claimed | `animation_failed` |
| `UNAVAILABLE` | repair disabled at dequeue time, or boot/quit/permission refusal | `_repair_animations`; also a refused report in `animation_failed` |
| `INTERRUPTED` | a durable `REPAIRING` row whose attachment is not `_repairing_animation_id` | `_transcript_payload`, `animation_failed`; never written to the ledger (H1) |

Probe pipeline:

| State | Predicate |
| --- | --- |
| `REJECTED` | `validate_scene`/`compile_source`/`_run_probe`/`validate_scene` raised `ValueError`; no artifact is saved |
| `COMPILED` | `normalized['compiled']` set from the pinned compiler |
| `PROBING` | the child process group is alive; JS `window.probeResult` is `null` |
| `PROBED` | child exited 0 with a JSON result that passed every guard |
| `ASSESSED` | `motion_report` and, for plan-first, `element_report` merged into `motion` |

Assessment values are a closed set: `unknown`, `blank`, `static`, `limited`,
`moving` (`src/blackrose/motion_canvas.py::motion_report`), and elements `unknown`, `visible`,
`incomplete` (`src/blackrose/motion_plan.py::element_report`).

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Page reports a playback failure | `src/blackrose/gui/static/motion-canvas-player.js::showError` → `call('animation_failed', …)` (`src/blackrose/gui/static/bloom.js::attachmentChips`) → `Session.animation_failed` |
| E2 | Duplicate report for the same attachment | the same page path; deduplicated in `src/blackrose/gui/session.py::Session.animation_failed` |
| E3 | Repair loop starts or resumes | `asyncio.create_task(_repair_animations)` in `src/blackrose/gui/session.py::Session.animation_failed`; `src/blackrose/gui/session.py::Session._wait_for_repair_slot` |
| E4 | Claim | `animation_recovery.claim`, called by `src/blackrose/gui/session.py::Session._repair_animations` |
| E5 | Repair turn starts | `Session._send(..., animation_repair_id=…)` → `src/blackrose/turns.py::TurnRunner._prepare` |
| E6 | Replacement saved in the repair turn | `src/blackrose/tool_execution.py::ToolExecutor._animation_create` → `finish(..., 'replaced', id)` |
| E7 | Repair turn ended | `_send` returned, or a turn-level failure, in `_repair_animations` |
| E8 | Stop or window close | `src/blackrose/gui/session.py::Session.stop`, `src/blackrose/gui/session.py::Session.close` |
| E9 | Repair raised after the claim | `_repair_animations`'s `except Exception` |
| E10 | Transcript render | `src/blackrose/gui/session.py::Session._transcript_payload`, `src/blackrose/gui/session.py::Session._attachment_payload` |
| E11 | Conversation deleted | cleanup journal `recovery` obligation (`src/blackrose/artifacts.py::ArtifactLifecycle.repair`, `src/blackrose/artifacts.py::ArtifactLifecycle.retry`) → `AnimationRecovery.delete` |
| E12 | Scene compile/probe/assessment | `src/blackrose/motion_canvas.py::prepare_scene`, called from `src/blackrose/animations.py::encode_scene` and `src/blackrose/animation_visual_review.py::inspect_and_repair` |
| E13 | Plan accepted or rejected | `src/blackrose/tool_execution.py::ToolExecutor._animation_plan` and `src/blackrose/tool_execution.py::ToolExecutor._animation_create_planned` |

## Transition table

Durable chain. Claim guards and effects are in `src/blackrose/animation_recovery.py::AnimationRecovery.claim`, settlement in
`src/blackrose/animation_recovery.py::AnimationRecovery.finish`, deletion in `src/blackrose/animation_recovery.py::AnimationRecovery.delete`:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | E4 | no row; parent row with `(conversation_id, replacement_attachment_id=attachment_id)` exists | `INSERT` with `root_id=parent.root_id`, `attempt=parent.attempt+1`, `status='repairing'` | `REPAIRING` |
| `ABSENT` | E4 | no parent | `INSERT` with `root_id=attachment_id`, `attempt=1` | `REPAIRING` |
| `ABSENT` | E4 | derived `attempt > MAX_REPAIRS` | `INSERT` with `status='exhausted'`; `claimed=False` | `EXHAUSTED` |
| `ABSENT` | E4 | `error` longer than 1000 characters | stored truncated to 1000 | as above |
| `ABSENT` | E4 | claim is cancelled while waiting for the lock | nothing written; a later claim still gets attempt 1 | `ABSENT` |
| `REPAIRING` | E6 | `animation_repair_id` set and the creation completed (`ToolExecutor._animation_create`); the row is still `repairing` | `UPDATE ... status='replaced', replacement_attachment_id=<saved id> WHERE status='repairing'` | `REPLACED` |
| `REPAIRING` | E7 | turn returned at least one `animation` attachment but E6 did not run | same `UPDATE` with `replacement_id=None` | `REPLACED` unlinked (H2) |
| `REPAIRING` | E7 | turn returned no animation attachment (false success) | `UPDATE ... status='failed'` | `FAILED` |
| `REPAIRING` | E7 | `finish` cancelled while waiting for the lock (H2 before this bead) | with the shield in `finish` the write still completes before `CancelledError` | as E6/E7 |
| `REPAIRING` | E8 | Stop after the claim; `CancelledError` in `_repair_animations` | `finish(...,'stopped')`, re-read, emit | `STOPPED` |
| `REPAIRING` | E9 | any other exception after the claim | `finish(...,'failed')`, notice | `FAILED` |
| `REPAIRING` | — | process dies or is killed after the claim | nothing writes; the row is durable evidence of the spent attempt | `REPAIRING` (stale, H1) |
| `REPAIRING` | E11 | conversation deleted | `DELETE ... WHERE conversation_id` | row gone |
| `REPLACED`/`FAILED`/`STOPPED`/`EXHAUSTED` | E6/E7/E8/E9 | `WHERE status='repairing'` matches nothing | no change | unchanged |
| any | E4 | row exists | `SELECT` returns it with `claimed=False`; no new attempt | unchanged |
| any | E10 | `REPAIRING` and `attachment_id != _repairing_animation_id` | `dict(state, status='interrupted')` in memory only (`_transcript_payload`) | `INTERRUPTED` projection |
| same attachment, new `ABSENT` row | E4 | a second conversation | independent row (PK includes `conversation_id`) | independent chain |

Session (in-memory). Intake guards are in `src/blackrose/gui/session.py::Session.animation_failed`; the queue
and settlement are `src/blackrose/gui/session.py::Session._repair_animations`:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| none | E1 | cloud model, `_ready`, not quitting, no permission prompt, int id, `0 < len(error) <= 1000`, selected conversation, the attachment is a Motion Canvas scene, selection unchanged | cache `queued`, enqueue, create the task if idle | `QUEUED` |
| none | E1 | any guard fails | `{'status': 'unavailable'}`; nothing queued | none |
| `QUEUED` | E2 | in-memory or durable state already known | returns the cached/durable state; a durable `repairing` is returned as `interrupted` | unchanged |
| `QUEUED` | E3 | slot refused (not ready, quitting, permission prompt) | `finally` emits `unavailable` for every pending item | `UNAVAILABLE` |
| `QUEUED` | E3 | the conversation changed while waiting | state dropped, no ledger row | dropped |
| `QUEUED` | E3 | repair disabled after the wait | emit `unavailable` | `UNAVAILABLE` |
| `QUEUED` | E3 | `read_animation` raised | notice; the popped item keeps its `queued` cache entry (H3) | `QUEUED` (stuck) |
| `QUEUED` | E4 | claim refused (`claimed=False`) | the durable state replaces the cache and is emitted | durable state |
| `QUEUED` | E4 | claim accepted | cache the claim, set `_repairing_animation_id`, run one turn | `REPAIRING` |
| `REPAIRING` | E6/E7 | see the durable table | cache replaced from a re-read, emitted | durable terminal state |
| `QUEUED` | E8 | pending item never claimed (`Session.stop`) | cache and emit `stopped`; no ledger row exists | `STOPPED` (memory only) |
| `QUEUED` | E4 | `claim` raised (e.g. corrupt ledger); `claimed` was set first | `_finish_failed_animation_repair('failed')`; its `finish` is suppressed, the empty read falls back to a local `failed` dict (`_finish_failed_animation_repair`) | `FAILED` (memory only) |

Probe. Parent guards are in `src/blackrose/motion_canvas.py::_run_probe` and `src/blackrose/motion_canvas.py::prepare_scene`; the
child's lockstep script is the probe document in
`src/blackrose/motion_canvas_probe.py::main`:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any scene | E12 | `src/blackrose/motion_canvas.py::validate_scene`: size, schema, cue order/range | `ValueError` before any child runs | `REJECTED` |
| validated | E12 | compiler/runtime present; 20 s Node deadline (`src/blackrose/motion_canvas.py::compile_source`) | `compiled` set | `COMPILED` |
| compiled | E12 | `Popen(..., start_new_session=True)` | child group created | `PROBING` |
| `PROBING` | ready message | measured `0.5..120`; every cue `<= duration + 1/30 + 1e-6` | `targets` quantized to frames, backward seeks appended | `PROBING` |
| `PROBING` | `inspected` with `id == index` | `abs(time - targets[index]) <= 1/30 + 1e-6` | append the sample; next `inspect` | `PROBING` |
| `PROBING` | `index == targets.length` | — | `window.probeResult = {duration, samples}` | result ready |
| `PROBING` | error message, duration guard, cue guard, seek mismatch | — | `probeResult = {error}` | `REJECTED` result |
| `PROBING` | deadline | `PLAYBACK_TIMEOUT` | `ValueError`; `finally` `SIGKILL` to the group and reap | `REJECTED` |
| `PROBING` | child exit non-zero, junk JSON, `error`, bad duration, no samples | — | `ValueError`; same `finally` | `REJECTED` |
| result ready | E12 | all guards passed | returned and cached unless `capture` (`src/blackrose/motion_canvas.py::probe_document`) | `PROBED` |
| `PROBED` | E12 | — | `duration` replaces metadata; `motion_report`; optional `element_report` | `ASSESSED` |
| `ASSESSED` | E12 | cues clamped to the measured duration | dedupe by clamped time, keep the last at each time | `ASSESSED` |
| `ASSESSED` | E12 | `validate_scene` again | `ValueError` if the postcondition is violated (no reachable case after the fix, H6) | `REJECTED` |
| plan | E13 | `src/blackrose/motion_plan.py::validate_plan` | canonical detached plan and a finite duration | accepted |
| plan rejected | E13 | any schema/timeline/final-state disagreement | `ValueError`; the executor keeps `rejected_motion_plan` and falls back once | `REJECTED` |

## Invariants

1. **One row, one claim per attachment.** The primary key is
   `(conversation_id, attachment_id)` (`src/blackrose/animation_recovery.py::AnimationRecovery._connect`), and
   `claim` returns the existing row with `claimed=False` instead of inserting.
   Pinned by
   `test_the_recorded_replacement_link_continues_the_chain_after_reopen` and
   `tests/test_animation_recovery_store.py::RecoveryStoreTests.test_concurrent_claims_spend_one_attempt_and_finish_is_terminal`.
2. **The attempt is derived from the durable parent, not counted.** `claim`
   computes `root_id`/`attempt` from the row whose
   `replacement_attachment_id` equals the new attachment, so a
   restart cannot reset or double-spend. Pinned by invariant 1's test (three
   fresh `AnimationRecovery` instances, attempts 1→2→3, root preserved).
3. **At most two claimed attempts per chain.** Attempt 3 is inserted as
   `exhausted` with `claimed=False`; no later claim advances it.
   Pinned by `test_the_recorded_replacement_link_continues_the_chain_after_reopen`.
4. **Terminal immutability.** `finish` updates only rows whose status is
   `repairing`, so a late `replaced`, `failed` or `stopped` cannot
   rewrite a settled row and an unknown attachment creates no row. Pinned by
   `test_terminal_states_are_immutable_and_unknown_rows_are_ignored`.
5. **The link is written before the replacement is published.**
   `_animation_create` awaits `finish_animation_repair` before calling
   `on_animations`, and the session's later `finish` in `_repair_animations` hits the
   `repairing` guard, so the recorded replacement id and status come from the
   save itself. Observed by
   `test_two_attempt_chain_is_durable_and_does_not_restart_on_reopen`, which
   claims the recorded `replacement_attachment_id` and receives attempt 2.
6. **A cancelled settlement still records the link.** `finish` runs its lock
   section as a shielded task and waits for it before re-raising cancellation.
   Pinned by
   `test_a_cancelled_finish_still_records_the_replacement_link` (fails against
   the previous code with `('repairing', None)`).
7. **A cancelled claim spends nothing.** `claim` never begins its write until it
   holds the lock, so a cancellation in that window leaves `ABSENT`; a retry
   still gets attempt 1. Pinned by `test_a_cancelled_claim_spends_no_attempt`.
8. **A live repair is never projected as interrupted.** `_repairing_animation_id`
   is assigned in the same task step as the claim result (no suspension
   between, in `_repair_animations`), and every settlement path awaits `finish`
   before the id is cleared (`_repair_animations`,
   `_finish_failed_animation_repair`), so a `repairing` row with a live owner
   always matches the field. The projection in `_transcript_payload` therefore only
   fires for a row left by a dead process (H1).
9. **The page never asks for a repair it already has a state for.**
   `src/blackrose/gui/static/motion-canvas-player.js::showError` prefers `options.recovery` over `onFailure`,
   and a `queued` result cannot overwrite a non-queued label
   (`src/blackrose/gui/static/motion-canvas-player.js::setRecovery`). This is what makes replay/reopen unable to spend an
   attempt ([Motion Canvas](../motion-canvas.md), "Replay/reopen does not reset the budget").
10. **Recovery is scoped.** Only the selected conversation, only a Motion Canvas
    attachment, and only a bounded error string are admitted
    (`src/blackrose/gui/session.py::Session.animation_failed`; 1000 characters again in `AnimationRecovery.claim`),
    and the repair turn is offered only `animation_read`/`animation_create` for
    that one id (`src/blackrose/tool_execution.py::ToolExecutor.definitions`, `src/blackrose/tools.py::animation_tools`).
11. **The replacement revision cannot target another scene.** The repair turn's
    `animation_create.revises_attachment_id` is an enum of `[animation_repair_id]`
    (`src/blackrose/tools.py::animation_tools`) and `_normalized_arguments`
    supplies it when the model omits it (`src/blackrose/tool_execution.py::ToolExecutor._normalized_arguments`), so a save in a repair turn really is the
    claimed attachment's revision.
12. **Probe output is validated before it is used.** Duration must be numeric in
    `0.5..120` and `samples` must be non-empty (`src/blackrose/motion_canvas.py::_run_probe`), and
    an `error` field is refused. Pinned by
    `test_result_guards_reject_errors_empty_samples_and_bad_durations`.
13. **A failed probe is not cached.** Only `probe_document(capture=False)`
    results go through `lru_cache` (`src/blackrose/motion_canvas.py::probe_document`, `src/blackrose/motion_canvas.py::_cached_probe`), and a raising call caches
    nothing. Pinned by `test_a_failed_probe_is_not_cached_so_a_retry_can_succeed`
    and `test_capture_probes_are_not_cached`.
14. **Every probe exit path kills the group and reaps the child.**
    `os.killpg(pid, SIGKILL)` runs in `src/blackrose/motion_canvas.py::_run_probe`'s `finally` for success,
    deadline, non-zero exit, junk output and every guard. Pinned by
    `test_the_deadline_kills_the_process_group_and_raises`,
    `test_failed_workers_are_bounded_refusals` (both assert the group is gone).
15. **Assessment is total and closed.** `motion_report` returns exactly one of
    `unknown`, `blank`, `static`, `limited`, `moving`; `unknown` means fewer than
    three distinct sampled times or no usable grids; `blank` requires every
    sample's foreground count to be zero; the first render of a repeated seek
    time is the compared one; `limited` uses the supplied measured duration.
    Pinned by `test_the_assessment_vocabulary_is_closed`,
    `test_repeated_seek_targets_compare_the_first_render_of_each_time`,
    `test_limited_is_decided_against_the_supplied_measured_duration`.
16. **Measured duration is authoritative and cues stay strictly ordered.** Cue
    times are clamped to the measured duration and deduplicated by the clamped
    time, so the second `validate_scene` cannot fail on the clamp. Pinned by
    `test_two_cues_inside_the_endpoint_tolerance_keep_the_last_one` and
    `test_cues_inside_the_measured_timeline_keep_their_times_and_order`.
17. **Element checks are presence-and-change only, with a bounded report.**
    `element_report` returns `unknown` without any observed inventory
    and at most 12 issues (`src/blackrose/motion_plan.py::element_report`). Pinned by
    `test_missing_elements_are_bounded_and_unknown_needs_an_inventory`.

## Hazards and open questions

- **H1 — `fragile` — `src/blackrose/gui/session.py::Session._transcript_payload`, `src/blackrose/gui/session.py::Session.animation_failed`.** `interrupted` is a
  display projection, never a stored value: no statement in `src/` writes it to
  `repairs.status`. A crash or kill between `claim` and `finish` leaves a durable
  `repairing` row that no later process settles; the session re-labels it in
  memory on every render and on every repeated failure report, and the attempt is
  correctly spent, but the ledger keeps a non-terminal state forever. The only
  settlement available is conversation deletion through the cleanup journal
  (`src/blackrose/artifacts.py::ArtifactLifecycle.retry`). Demonstrated at the ledger level by
  `test_a_restarted_ledger_never_settles_a_repairing_row`; the projection itself
  is session-level. Confirmation of the user-visible effect: kill the desktop
  during a repair, reopen, and watch `states()` still report `repairing` while
  the page says “interrupted”. Proposal for the `session.py` owner: settle the
  projection once at boot (after `ensure_conversation`, before the first
  transcript) with
  `finish(conversation_id, attachment_id, 'interrupted')` for every `repairing`
  row found in the selected conversation, guarded by `_repairing_animation_id`
  being `None`. That makes the durable vocabulary match the documented one and
  removes the stale non-terminal state without ever retrying inference.
- **H2 — `fragile` — `src/blackrose/animation_recovery.py::AnimationRecovery.finish`, `src/blackrose/gui/session.py::Session._repair_animations`,
  `src/blackrose/tool_execution.py::ToolExecutor._animation_create`.** `finish` accepts `status='replaced'` with no
  `replacement_attachment_id`, and the store cannot know the saved id. The
  session's fallback path (a repair turn that returned an animation attachment)
  passes no id. If the tool-side `finish` ever fails — a transient SQLite error,
  a full disk, or (before this bead) a cancellation while the ledger lock was
  held by another operation — the row becomes `replaced` with a `NULL` link, and
  `claim` then treats the saved replacement as a new root with a fresh two-attempt
  budget. Demonstrated by
  `test_a_replaced_row_without_a_recorded_link_starts_a_new_chain`; the
  cancellation window that made it plausible is closed and pinned by
  `test_a_cancelled_finish_still_records_the_replacement_link`, and the error
  window remains. Proposal for the `session.py` owner: pass the last `animation`
  attachment id from the turn to `finish` in `_repair_animations`. Only after that, `finish` could
  reject `replaced` without an id; doing it first would turn the fallback into a
  stuck `repairing` row.
- **H3 — `fragile` — `src/blackrose/gui/session.py::Session._repair_animations`.** After the pending item is
  popped, a `read_animation` failure (the attachment file or
  scene disappeared while the repair waited for the reply slot) leaves
  `_animation_recovery_states[attachment_id] == 'queued'` with no durable row
  and no `unavailable` emit, because the `finally` only covers items still in
  the dict. `animation_failed` then returns the cached `queued` forever in this
  process, until any transcript render replaces the cache. Not
  demonstrated: it needs a session-level injection of a failing read. Proposal:
  wrap the loop body in `try/except` so the popped item is cached and emitted as
  `unavailable` (the same shape as the disabled-after-wait branch).
- **H4 — `documentation-gap` — `src/blackrose/motion_canvas.py::_run_probe`.** The probe's
  lifecycle is bounded only while the parent lives: `start_new_session=True`
  detaches the child into its own session, so if the desktop process is killed
  mid-probe the probe and its WebKit children keep a hidden window and a 25 ms
  polling loop with nothing left to bound them. There is no `PR_SET_PDEATHSIG`
  and the child has no self-deadline (`src/blackrose/motion_canvas_probe.py::main`, whose
  docstring states that the parent owns the deadline). Not demonstrated;
  confirming it means killing the parent during a probe and watching the child
  group.
- **H5 — `fragile` — `src/blackrose/motion_canvas.py::_run_probe`.** The `finally` kills the group and
  then calls `process.communicate()` without a timeout. A descendant that leaves
  the group (for example by calling `setsid`) while keeping the inherited
  stdout/stderr descriptors open would block that call after the error is already
  determined. WebKit children inherit the group, so no product path is known to
  do this; `test_the_deadline_kills_the_process_group_and_raises` shows the
  ordinary path returns promptly. A bounded second `communicate(timeout=…)` with
  a close of the pipes would make the `finally` total.
- **H6 — `fragile` (fixed) — `src/blackrose/motion_canvas.py::prepare_scene`,
  `src/blackrose/motion_canvas_probe.py::main`.** The probe accepts cue times up to one frame past
  the measured end, and the clamp used to map two such times onto the same last
  frame, so the second `validate_scene` raised
  `Caption times must increase and fit within duration` after a successful
  compile and probe. Demonstrated before the fix by
  `test_two_cues_inside_the_endpoint_tolerance_keep_the_last_one` (`ValueError`);
  fixed by deduplicating on the clamped time and keeping the last cue — the one
  the player displays there (`src/blackrose/gui/static/motion-canvas-player.js::update`). A related nit found
  while testing: `_run_probe`'s `isinstance(duration, (float, int))` accepts
  `True` as `1.0`; our own probe never emits it, and
  `validate_scene` then refuses the scene, so nothing is saved. Not fixed.
- **H7 — `none-found` — the rest of the chain is closed.** For the live process
  the chain is total and idempotent: every accepted failure is either claimed,
  refused as `unavailable`, dropped for a changed conversation, or settled by
  `finish`; every claimed attempt reaches a terminal state on return, cancellation
  or exception (`_repair_animations`); a repeated report spends nothing
  (`animation_failed`); the page cannot re-request a recorded chain
  (`src/blackrose/gui/static/motion-canvas-player.js::setRecovery`, `src/blackrose/gui/static/motion-canvas-player.js::showError`); and the repair turn cannot widen its
  scope (`src/blackrose/tool_execution.py::ToolExecutor.definitions`).
  For the probe, every parent-side exit is bounded, kills the group and returns
  a `ValueError` (invariants 12–14), and the assessment is total (invariant 15).
  The honest exceptions are H1 (a crash strands a non-terminal row) and H2 (an
  unlinked replacement resets the chain).

## Formalization decisions

Two small source changes, each with a regression test that fails on the previous
code:

1. `src/blackrose/motion_canvas.py::prepare_scene` now deduplicates cues by their clamped
   time, keeping the last. The post-measurement `validate_scene` could previously
   fail on a scene the probe had just accepted (H6). Keeping the last cue changes
   no rendered frame: the player shows only the last cue whose time has been
   reached (`src/blackrose/gui/static/motion-canvas-player.js::update`), and cues past the measured end were
   unreachable. All cue times remain strictly increasing, so the stored scene
   still satisfies the input schema.
2. `src/blackrose/animation_recovery.py::AnimationRecovery.finish` now runs its lock section as a shielded
   task and waits for it before re-raising cancellation. The link is what makes a
   replacement join the chain, and the comment in `src/blackrose/tool_execution.py::ToolExecutor._animation_create`
   states the intent ("a crash/cancellation after save must not regenerate it").
   `run_to_completion` already survives cancellation, but the `asyncio.Lock` wait did not,
   and that lock can be held for a whole artifact report
   (`src/blackrose/artifacts.py::ArtifactLifecycle.report`).
   The lock itself is kept: it is the writer exclusion that lets that report open
   the ledger with `immutable=1` (`src/blackrose/storage/snapshot.py::readonly_snapshot`), so simply dropping
   it would have broken the read-only snapshot contract. A Stop can therefore
   wait for an in-flight report before the repair task unwinds; `claim` already
   had that property (`src/blackrose/animation_recovery.py::AnimationRecovery.claim`).

No change to `gui/session.py`: it is shared with other beads, and both fixes it
needs (H1's boot settlement, H2's replacement id, H3's `unavailable` emit) change
the session/collaborator contract. They are proposals above, for that file's
owner. No schema change: `status` still has no CHECK, and the vocabulary is
enforced by the writers (H7).

## Verification

Offline, from the repository root (the runner provides scratch XDG
directories):

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_09_animation_recovery.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_animation_recovery_store.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_motion_plan.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas_validation.py
.venv/bin/python scripts/check_unit_tests.py -p test_animations.py
.venv/bin/python scripts/check_unit_tests.py -p test_animation_visual_review.py
.venv/bin/python scripts/check_unit_tests.py -p test_artifacts.py
```

`test_artifacts.py` is included because the ledger lock is also the
artifact-report writer exclusion. Motion Canvas tests that need the compiler or
WebKit skip without them; the native playback suites run with real GTK/WebKit and
a display:

```sh
BLACKROSE_TEST_WEBKIT=1 .venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas.py
BLACKROSE_TEST_WEBKIT=1 .venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas_validation.py
BLACKROSE_TEST_WEBKIT=1 .venv/bin/python scripts/check_unit_tests.py -p test_animation_recovery.py
```

The two source changes are pinned by tests that fail on the previous code:
`test_a_cancelled_finish_still_records_the_replacement_link` (the link stays
`('repairing', None)`) and
`test_two_cues_inside_the_endpoint_tolerance_keep_the_last_one` (`Caption times
must increase and fit within duration`).

`scripts/check_gui_motion_canvas.py --recovery` drives the real window. H1 and H3
are answered from code and from the ledger-level tests; confirming the
user-visible label needs that native probe or a session-level failure injection.
