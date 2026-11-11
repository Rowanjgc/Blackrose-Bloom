# SM-09: Animation playback recovery and the Motion Canvas probe

## Scope and ownership

- `src/blackrose/animation_recovery.py`: the durable ledger. `AnimationRecovery`
  (21–24), `_connect`/schema (26–41), `states` (43–56), `claim` (58–78),
  `finish` (80–104), `delete` (106–112), `MAX_REPAIRS = 2` (16).
- `src/blackrose/gui/session.py`: the in-memory half. Fields (127–132),
  `close` (353–376), `_transcript_payload` (422–453; projection at 439–442),
  `_attachment_payload` (706–746; recovery in the payload at 727–728), `stop`
  (1060–1087), `animation_failed` (1126–1161), `_wait_for_repair_slot`
  (1163–1173), `_repair_animations` (1238–1300),
  `_finish_failed_animation_repair` (1302–1308).
- `src/blackrose/tool_execution.py`: records the replacement link at save time
  (107, 162–165, 270–271, 364–381; link at 374–378) and holds the one-reply
  plan state (309–361).
- `src/blackrose/tool_host.py`: the narrow `finish_animation_repair` capability
  (68–71).
- `src/blackrose/motion_canvas.py`: `PLAYBACK_TIMEOUT` (28), `validate_scene`
  (108–129), `compile_source` (132–157), `probe_document`/cache (160–170),
  `_run_probe` (173–212), `motion_report` (211–265), `prepare_scene` (268–304).
- `src/blackrose/motion_canvas_probe.py`: the disposable child. GTK setup
  (12–35), the probe document and its lockstep script (37–80), the polling
  driver (82–107).
- `src/blackrose/gui/static/motion-canvas-frame.js`: the trusted adapter inside
  the opaque-origin frame (51–137; commands 142–163).
- `src/blackrose/gui/static/motion-canvas-player.js`: page-side recovery label
  and the failure report (58–115, 149–160, 185–187); `bloom.js` mounts it with
  `recovery` and `onFailure` (494–505) and routes `animation_recovery` events
  (777).
- `src/blackrose/motion_plan.py`: `validate_plan` (56–103), `element_report`
  (129–189), `assemble_source` (192–229).

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[animations](../animations.md) / [Motion Canvas](../motion-canvas.md):

- The **ledger** is auxiliary SQLite under the attachment store
  (`chat.py:103–107`), owned by the one desktop process (ADR 0001). Every
  operation is a function run on a worker thread through `_worker`
  (`storage/db.py:20–35`), which finishes the thread's work before it re-raises
  a caller cancellation. `AnimationRecovery._lock` serializes those operations
  *and* is the writer exclusion that lets an artifact report open the ledger
  read-only with `immutable=1` (`artifacts.py:180–183`,
  `storage/snapshot.py:10–20`).
- **`Session`** (GUI asyncio loop) owns the failure queue, the recovery cache,
  the single `_animation_repair_task`, `_repairing_animation_id` and the
  reservation of `_generation` for the repair turn. It never owns the durable
  status: it only calls `claim`/`finish`.
- **`ToolExecutor`** (inside the repair turn) owns the one write that links a new
  attachment to the chain, before the attachment is published (`tool_execution.py:374–378`).
- The **page** owns only presentation. It reports one failure per mount and
  never mutates recovery state (`motion-canvas-player.js:98–115`).
- **`motion_canvas`** owns compilation and the probe *parent*: the deadline, the
  process group, and every guard on the returned result. The **probe child** owns
  the disposable GTK/WebKit process; its caches, HSTS list and storage salts are
  ephemeral and in memory (`motion_canvas_probe.py:29–33`). Generated TSX runs
  only in an opaque `sandbox="allow-scripts"` frame with no bridge
  (`motion_canvas_probe.py:37–40`; `motion_canvas.py:312–330`).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `repairs.conversation_id`, `attachment_id` | `animation_recovery.py:32` | `INTEGER`, composite PK (36) | Chain member identity; one row per attachment forever | `claim` insert only (73) |
| `repairs.root_id` | `:33` | `INTEGER NOT NULL` | First attachment of the chain | `claim` (`:70`, or `attachment_id` for a new root) |
| `repairs.attempt` | `:33` | `INTEGER NOT NULL` | Position in the chain, `1..MAX_REPAIRS+1` | `claim` (`:71`) |
| `repairs.status` | `:34` | `TEXT NOT NULL`, no CHECK | `repairing` / `exhausted` / `replaced` / `failed` / `stopped` | `claim` (72), `finish` (83–85) |
| `repairs.error` | `:34` | `TEXT NOT NULL` | First bounded diagnostic (untrusted scene output) | `claim`, truncated to 1000 (`:74`) |
| `repairs.replacement_attachment_id` | `:35` | `INTEGER` nullable | The saved attachment that now supersedes this one; the *parent link* | `finish` (83–85); `NULL` at insert (73) |
| `_pending_animation_failures` | `session.py:130` | `dict[(conversation_id, attachment_id), str]` | Accepted but unclaimed failures | `animation_failed` (1156), `stop` (1075), `close` (360), `_repair_animations` (1247, 1295) |
| `_animation_recovery_states` | `session.py:131` | `dict[attachment_id, dict]` | Cache/dedup and the value sent to the page | `animation_failed` (1154, 1158), `stop` (1073), `_repair_animations` (1250, 1254, 1261, 1278), `_finish_failed_animation_repair` (1307); wholesale replaced by `_transcript_payload` (439) |
| `_repairing_animation_id` | `session.py:132` | `int \| None` | The attachment whose `repairing` row this process owns | `_repair_animations` (1266, 1281, 1296) |
| `_animation_repair_task` | `session.py:127` | `asyncio.Task \| None` | The one repair loop | `animation_failed` (1160); cleared in its own `finally` (1300) |
| `_generation` | `session.py`, see 1030–1031, 1172, 1297 | `asyncio.Task \| None` | Global turn reservation, held by the repair turn | `_wait_for_repair_slot` (1172); released at 1297/1503 |
| `controller.automatic_animation_repair` | `tool_host.py:27–29`; read at `session.py:1128`, `1252`, `turns.py:384` | `bool` | Cloud-model policy gate for automatic repair | configuration/provider state |
| `figure.dataset.recovery`, `options.recovery` | `motion-canvas-player.js:60–76` | `string` | Page label and the "already reported" marker | `setRecovery` only |
| `PLAYBACK_TIMEOUT` | `motion_canvas.py:28` | `int` seconds | The only execution bound for the probe | module constant |
| `window.probeResult`, `targets`, `index`, `samples` | `motion_canvas_probe.py:43–44` | JS null/array/int | Probe protocol state | the probe document only |
| frame `ready`, `failed`, `inspection` | `motion-canvas-frame.js:4` | JS bool/null | Frame lifecycle and the single pending seek | frame only |
| `result['duration']`, `samples` | `motion_canvas.py:192–201` | dict | Validated probe output | parent, after guards |
| `scene['duration']`, `scene['cues']` | `motion_canvas.py:282, 298–302` | number/list | Measured duration replaces metadata; cues clamp to it | `prepare_scene` |
| `motion` dict | `motion_canvas.py:283–293`; overridden at `chat.py:1337–1344` | dict | `assessment`/`last_motion`/`duration`/`notice`/`elements`/`reviewed` | `prepare_scene`, visual review |

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
| `QUEUED` | failure accepted, cached as `{'status': 'queued'}`, not yet claimed | `animation_failed` (1156–1161) |
| `UNAVAILABLE` | repair disabled at dequeue time, or boot/quit/permission refusal | 1252–1256, 1291–1294; also a refused report (1130–1132) |
| `INTERRUPTED` | a durable `REPAIRING` row whose attachment is not `_repairing_animation_id` | `_transcript_payload` (441–442), `animation_failed` (1152–1153); never written to the ledger (H1) |

Probe pipeline:

| State | Predicate |
| --- | --- |
| `REJECTED` | `validate_scene`/`compile_source`/`_run_probe`/`validate_scene` raised `ValueError`; no artifact is saved |
| `COMPILED` | `normalized['compiled']` set from the pinned compiler |
| `PROBING` | the child process group is alive; JS `window.probeResult` is `null` |
| `PROBED` | child exited 0 with a JSON result that passed every guard |
| `ASSESSED` | `motion_report` and, for plan-first, `element_report` merged into `motion` |

Assessment values are a closed set: `unknown`, `blank`, `static`, `limited`,
`moving` (`motion_canvas.py:211–265`), and elements `unknown`, `visible`,
`incomplete` (`motion_plan.py:188–189`).

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Page reports a playback failure | `motion-canvas-player.js:98–113` → `call('animation_failed', …)` (`bloom.js:503–504`) → `Session.animation_failed` |
| E2 | Duplicate report for the same attachment | the same page path; deduplicated at `session.py:1146–1155` |
| E3 | Repair loop starts or resumes | `asyncio.create_task(_repair_animations)` (`session.py:1160`); `_wait_for_repair_slot` (1244) |
| E4 | Claim | `animation_recovery.claim` (`session.py:1260`) |
| E5 | Repair turn starts | `Session._send(..., animation_repair_id=…)` (1273–1274) → `TurnRunner._prepare` (`turns.py:384–424`) |
| E6 | Replacement saved in the repair turn | `ToolExecutor._animation_create` (`tool_execution.py:364–381`) → `finish(..., 'replaced', id)` |
| E7 | Repair turn ended | `_send` returned (1275–1279) or a turn-level failure (1286–1289) |
| E8 | Stop or window close | `Session.stop` (1060–1087), `close` (353–376) |
| E9 | Repair raised after the claim | `_repair_animations` `except Exception` (1286–1289) |
| E10 | Transcript render | `_transcript_payload` (422–453), `_attachment_payload` (727–728) |
| E11 | Conversation deleted | cleanup journal `recovery` obligation (`artifacts.py:244–245`, 225–228) → `AnimationRecovery.delete` |
| E12 | Scene compile/probe/assessment | `prepare_scene` (268–304), called from `animations.py:252` and `animation_visual_review.py:131, 183` |
| E13 | Plan accepted or rejected | `ToolExecutor` `animation_plan`/`animation_create_planned` (`tool_execution.py:309–361`) |

## Transition table

Durable chain (`animation_recovery.py`):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | E4 | no row; parent row with `(conversation_id, replacement_attachment_id=attachment_id)` exists (67–69) | `INSERT` with `root_id=parent.root_id`, `attempt=parent.attempt+1`, `status='repairing'` (70–74) | `REPAIRING` |
| `ABSENT` | E4 | no parent (67–69) | `INSERT` with `root_id=attachment_id`, `attempt=1` | `REPAIRING` |
| `ABSENT` | E4 | derived `attempt > MAX_REPAIRS` (72) | `INSERT` with `status='exhausted'`; `claimed=False` | `EXHAUSTED` |
| `ABSENT` | E4 | `error` longer than 1000 characters (74) | stored truncated to 1000 | as above |
| `ABSENT` | E4 | claim is cancelled while waiting for the lock (77) | nothing written; a later claim still gets attempt 1 | `ABSENT` |
| `REPAIRING` | E6 | `animation_repair_id` set and the creation completed (374–378); the row is still `repairing` | `UPDATE ... status='replaced', replacement_attachment_id=<saved id> WHERE status='repairing'` (83–85) | `REPLACED` |
| `REPAIRING` | E7 | turn returned at least one `animation` attachment but E6 did not run (1275–1276) | same `UPDATE` with `replacement_id=None` | `REPLACED` unlinked (H2) |
| `REPAIRING` | E7 | turn returned no animation attachment (false success) | `UPDATE ... status='failed'` | `FAILED` |
| `REPAIRING` | E7 | `finish` cancelled while waiting for the lock (H2 before this bead) | with the shield (91–104) the write still completes before `CancelledError` | as E6/E7 |
| `REPAIRING` | E8 | Stop after the claim; `CancelledError` in `_repair_animations` (1282–1285) | `finish(...,'stopped')`, re-read, emit | `STOPPED` |
| `REPAIRING` | E9 | any other exception after the claim (1286–1289) | `finish(...,'failed')`, notice | `FAILED` |
| `REPAIRING` | — | process dies or is killed after the claim | nothing writes; the row is durable evidence of the spent attempt | `REPAIRING` (stale, H1) |
| `REPAIRING` | E11 | conversation deleted | `DELETE ... WHERE conversation_id` (106–112) | row gone |
| `REPLACED`/`FAILED`/`STOPPED`/`EXHAUSTED` | E6/E7/E8/E9 | `WHERE status='repairing'` matches nothing | no change | unchanged |
| any | E4 | row exists | `SELECT` returns it with `claimed=False` (65–66); no new attempt | unchanged |
| any | E10 | `REPAIRING` and `attachment_id != _repairing_animation_id` | `dict(state, status='interrupted')` in memory only (441–442) | `INTERRUPTED` projection |
| same attachment, new `ABSENT` row | E4 | a second conversation | independent row (PK includes `conversation_id`) | independent chain |

Session (in-memory, `gui/session.py`):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| none | E1 | cloud model, `_ready`, not quitting, no permission prompt, int id, `0 < len(error) <= 1000`, selected conversation, the attachment is a Motion Canvas scene, selection unchanged (1128–1144) | cache `queued`, enqueue, create the task if idle | `QUEUED` |
| none | E1 | any guard fails | `{'status': 'unavailable'}`; nothing queued | none |
| `QUEUED` | E2 | in-memory or durable state already known (1146–1155) | returns the cached/durable state; a durable `repairing` is returned as `interrupted` | unchanged |
| `QUEUED` | E3 | slot refused (not ready, quitting, permission prompt) (1244–1245) | `finally` emits `unavailable` for every pending item (1291–1294) | `UNAVAILABLE` |
| `QUEUED` | E3 | the conversation changed while waiting (1248–1251) | state dropped, no ledger row | dropped |
| `QUEUED` | E3 | repair disabled after the wait (1252–1256) | emit `unavailable` | `UNAVAILABLE` |
| `QUEUED` | E3 | `read_animation` raised (1258) | notice; the popped item keeps its `queued` cache entry (H3) | `QUEUED` (stuck) |
| `QUEUED` | E4 | claim refused (`claimed=False`, 1263–1265) | the durable state replaces the cache and is emitted | durable state |
| `QUEUED` | E4 | claim accepted | cache the claim, set `_repairing_animation_id` (1260–1266), run one turn | `REPAIRING` |
| `REPAIRING` | E6/E7 | see the durable table | cache replaced from a re-read, emitted (1277–1279) | durable terminal state |
| `QUEUED` | E8 | pending item never claimed (1071–1075) | cache and emit `stopped`; no ledger row exists | `STOPPED` (memory only) |
| `QUEUED` | E4 | `claim` raised (e.g. corrupt ledger); `claimed` was set first (1259) | `_finish_failed_animation_repair('failed')`; its `finish` is suppressed, the empty read falls back to a local `failed` dict (1303–1307) | `FAILED` (memory only) |

Probe (`motion_canvas.py`, `motion_canvas_probe.py`, `motion-canvas-frame.js`):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any scene | E12 | `validate_scene` (108–129): size, schema, cue order/range | `ValueError` before any child runs | `REJECTED` |
| validated | E12 | compiler/runtime present; 20 s Node deadline (132–157) | `compiled` set | `COMPILED` |
| compiled | E12 | `Popen(..., start_new_session=True)` (180–182) | child group created | `PROBING` |
| `PROBING` | ready message | measured `0.5..120`; every cue `<= duration + 1/30 + 1e-6` (53–58) | `targets` quantized to frames, backward seeks appended (59–68) | `PROBING` |
| `PROBING` | `inspected` with `id == index` | `abs(time - targets[index]) <= 1/30 + 1e-6` (70–73) | append the sample; next `inspect` | `PROBING` |
| `PROBING` | `index == targets.length` | — | `window.probeResult = {duration, samples}` (76) | result ready |
| `PROBING` | error message, duration guard, cue guard, seek mismatch | — | `probeResult = {error}` (50, 54, 57, 72) | `REJECTED` result |
| `PROBING` | deadline | `PLAYBACK_TIMEOUT` (185–188) | `ValueError`; `finally` `SIGKILL` to the group and reap (202–212) | `REJECTED` |
| `PROBING` | child exit non-zero, junk JSON, `error`, bad duration, no samples (189–201) | — | `ValueError`; same `finally` | `REJECTED` |
| result ready | E12 | all guards passed | returned and cached unless `capture` (160–170) | `PROBED` |
| `PROBED` | E12 | — | `duration` replaces metadata; `motion_report`; optional `element_report` (282–293) | `ASSESSED` |
| `ASSESSED` | E12 | cues clamped to the measured duration | dedupe by clamped time, keep the last at each time (294–302) | `ASSESSED` |
| `ASSESSED` | E12 | `validate_scene` again (303) | `ValueError` if the postcondition is violated (no reachable case after the fix, H6) | `REJECTED` |
| plan | E13 | `validate_plan` (56–103) | canonical detached plan and a finite duration | accepted |
| plan rejected | E13 | any schema/timeline/final-state disagreement | `ValueError`; the executor keeps `rejected_motion_plan` and falls back once | `REJECTED` |

## Invariants

1. **One row, one claim per attachment.** The primary key is
   `(conversation_id, attachment_id)` (`animation_recovery.py:32, 36`), and
   `claim` returns the existing row with `claimed=False` instead of inserting
   (`:65–66`). Pinned by
   `test_the_recorded_replacement_link_continues_the_chain_after_reopen` and
   `test_animation_recovery_store.py:test_concurrent_claims_spend_one_attempt_and_finish_is_terminal`.
2. **The attempt is derived from the durable parent, not counted.** `claim`
   computes `root_id`/`attempt` from the row whose
   `replacement_attachment_id` equals the new attachment (`:67–71`), so a
   restart cannot reset or double-spend. Pinned by invariant 1's test (three
   fresh `AnimationRecovery` instances, attempts 1→2→3, root preserved).
3. **At most two claimed attempts per chain.** Attempt 3 is inserted as
   `exhausted` with `claimed=False` (`:72, :76`); no later claim advances it.
   Pinned by `test_the_recorded_replacement_link_continues_the_chain_after_reopen`.
4. **Terminal immutability.** `finish` updates only rows whose status is
   `repairing` (`:83–85`), so a late `replaced`, `failed` or `stopped` cannot
   rewrite a settled row and an unknown attachment creates no row. Pinned by
   `test_terminal_states_are_immutable_and_unknown_rows_are_ignored`.
5. **The link is written before the replacement is published.**
   `_animation_create` awaits `finish_animation_repair` (374–378) before
   `on_animations` (379–380), and the session's later `finish` (1276) hits the
   `repairing` guard, so the recorded replacement id and status come from the
   save itself. Observed by
   `test_two_attempt_chain_is_durable_and_does_not_restart_on_reopen`, which
   claims the recorded `replacement_attachment_id` and receives attempt 2.
6. **A cancelled settlement still records the link.** `finish` runs its lock
   section as a shielded task and waits for it before re-raising cancellation
   (91–104). Pinned by
   `test_a_cancelled_finish_still_records_the_replacement_link` (fails against
   the previous code with `('repairing', None)`).
7. **A cancelled claim spends nothing.** `claim` never begins its write until it
   holds the lock, so a cancellation in that window leaves `ABSENT`; a retry
   still gets attempt 1. Pinned by `test_a_cancelled_claim_spends_no_attempt`.
8. **A live repair is never projected as interrupted.** `_repairing_animation_id`
   is assigned in the same task step as the claim result (1260–1266, no
   suspension between), and every settlement path awaits `finish` before the id
   is cleared (1276–1281, 1302–1308, 1296), so a `repairing` row with a live
   owner always matches the field. The projection at 441–442 therefore only
   fires for a row left by a dead process (H1).
9. **The page never asks for a repair it already has a state for.**
   `showError` prefers `options.recovery` over `onFailure`
   (`motion-canvas-player.js:108–113`), and a `queued` result cannot overwrite a
   non-queued label (`:61`). This is what makes replay/reopen unable to spend an
   attempt (`motion-canvas.md`, "Replay/reopen does not reset the budget").
10. **Recovery is scoped.** Only the selected conversation, only a Motion Canvas
    attachment, and only a bounded error string are admitted
    (1128–1144; 1000 characters again at `:74`), and the repair turn is offered
    only `animation_read`/`animation_create` for that one id (`turns.py:463–464`,
    `tools.py:138`).
11. **The replacement revision cannot target another scene.** The repair turn's
    `animation_create.revises_attachment_id` is an enum of `[animation_repair_id]`
    (`tools.py:138`) and `_normalized_arguments` supplies it when the model omits
    it (`tool_execution.py:270–271`), so a save in a repair turn really is the
    claimed attachment's revision.
12. **Probe output is validated before it is used.** Duration must be numeric in
    `0.5..120` and `samples` must be non-empty (`motion_canvas.py:197–201`), and
    an `error` field is refused (`:195–196`). Pinned by
    `test_result_guards_reject_errors_empty_samples_and_bad_durations`.
13. **A failed probe is not cached.** Only `probe_document(capture=False)`
    results go through `lru_cache` (`:160–170`), and a raising call caches
    nothing. Pinned by `test_a_failed_probe_is_not_cached_so_a_retry_can_succeed`
    and `test_capture_probes_are_not_cached`.
14. **Every probe exit path kills the group and reaps the child.**
    `os.killpg(pid, SIGKILL)` runs in a `finally` (`:202–208`) for success,
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
    (`motion_plan.py:154–155`) and at most 12 issues (`:188–189`). Pinned by
    `test_missing_elements_are_bounded_and_unknown_needs_an_inventory`.

## Hazards and open questions

- **H1 — `fragile` — `gui/session.py:439–442`, `1152–1153`.** `interrupted` is a
  display projection, never a stored value: no statement in `src/` writes it to
  `repairs.status`. A crash or kill between `claim` and `finish` leaves a durable
  `repairing` row that no later process settles; the session re-labels it in
  memory on every render and on every repeated failure report, and the attempt is
  correctly spent, but the ledger keeps a non-terminal state forever. The only
  settlement available is conversation deletion through the cleanup journal
  (`artifacts.py:225–228`). Demonstrated at the ledger level by
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
- **H2 — `fragile` — `animation_recovery.py:80–104`, `session.py:1275–1276`,
  `tool_execution.py:374–378`.** `finish` accepts `status='replaced'` with no
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
  attachment id from the turn to `finish` (1276). Only after that, `finish` could
  reject `replaced` without an id; doing it first would turn the fallback into a
  stuck `repairing` row.
- **H3 — `fragile` — `gui/session.py:1247–1258`.** After the pending item is
  popped (1247), a `read_animation` failure at 1258 (the attachment file or
  scene disappeared while the repair waited for the reply slot) leaves
  `_animation_recovery_states[attachment_id] == 'queued'` with no durable row
  and no `unavailable` emit, because the `finally` only covers items still in
  the dict. `animation_failed` then returns the cached `queued` forever in this
  process, until any transcript render replaces the cache (439). Not
  demonstrated: it needs a session-level injection of a failing read. Proposal:
  wrap the loop body in `try/except` so the popped item is cached and emitted as
  `unavailable` (the same shape as 1252–1256).
- **H4 — `documentation-gap` — `motion_canvas.py:180–182, 202–208`.** The probe's
  lifecycle is bounded only while the parent lives: `start_new_session=True`
  detaches the child into its own session, so if the desktop process is killed
  mid-probe the probe and its WebKit children keep a hidden window and a 25 ms
  polling loop with nothing left to bound them. There is no `PR_SET_PDEATHSIG`
  and the child has no self-deadline (`motion_canvas_probe.py:82–107`, whose
  docstring states that the parent owns the deadline). Not demonstrated;
  confirming it means killing the parent during a probe and watching the child
  group.
- **H5 — `fragile` — `motion_canvas.py:208`.** The `finally` kills the group and
  then calls `process.communicate()` without a timeout. A descendant that leaves
  the group (for example by calling `setsid`) while keeping the inherited
  stdout/stderr descriptors open would block that call after the error is already
  determined. WebKit children inherit the group, so no product path is known to
  do this; `test_the_deadline_kills_the_process_group_and_raises` shows the
  ordinary path returns promptly. A bounded second `communicate(timeout=…)` with
  a close of the pipes would make the `finally` total.
- **H6 — `fragile` (fixed) — `motion_canvas.py:294–303`,
  `motion_canvas_probe.py:56`.** The probe accepts cue times up to one frame past
  the measured end, and the clamp used to map two such times onto the same last
  frame, so the second `validate_scene` raised
  `Caption times must increase and fit within duration` after a successful
  compile and probe. Demonstrated before the fix by
  `test_two_cues_inside_the_endpoint_tolerance_keep_the_last_one` (`ValueError`);
  fixed by deduplicating on the clamped time and keeping the last cue — the one
  the player displays there (`motion-canvas-player.js:86`). A related nit found
  while testing: `_run_probe`'s `isinstance(duration, (float, int))` accepts
  `True` as `1.0` (`motion_canvas.py:197`); our own probe never emits it, and
  `validate_scene` then refuses the scene, so nothing is saved. Not fixed.
- **H7 — `none-found` — the rest of the chain is closed.** For the live process
  the chain is total and idempotent: every accepted failure is either claimed,
  refused as `unavailable`, dropped for a changed conversation, or settled by
  `finish`; every claimed attempt reaches a terminal state on return, cancellation
  or exception (1282–1299); a repeated report spends nothing (1146–1155); the
  page cannot re-request a recorded chain (`motion-canvas-player.js:61,
  108–113`); and the repair turn cannot widen its scope (`turns.py:463–464`).
  For the probe, every parent-side exit is bounded, kills the group and returns
  a `ValueError` (invariants 12–14), and the assessment is total (invariant 15).
  The honest exceptions are H1 (a crash strands a non-terminal row) and H2 (an
  unlinked replacement resets the chain).

## Formalization decisions

Two small source changes, each with a regression test that fails on the previous
code:

1. `motion_canvas.prepare_scene` (294–302) now deduplicates cues by their clamped
   time, keeping the last. The post-measurement `validate_scene` could previously
   fail on a scene the probe had just accepted (H6). Keeping the last cue changes
   no rendered frame: the player shows only the last cue whose time has been
   reached (`motion-canvas-player.js:86`), and cues past the measured end were
   unreachable. All cue times remain strictly increasing, so the stored scene
   still satisfies the input schema.
2. `AnimationRecovery.finish` (80–104) now runs its lock section as a shielded
   task and waits for it before re-raising cancellation. The link is what makes a
   replacement join the chain, and the comment at `tool_execution.py:375–376`
   states the intent ("a crash/cancellation after save must not regenerate it").
   `_worker` already survives cancellation, but the `asyncio.Lock` wait did not,
   and that lock can be held for a whole artifact report (`artifacts.py:180–183`).
   The lock itself is kept: it is the writer exclusion that lets that report open
   the ledger with `immutable=1` (`storage/snapshot.py:11–20`), so simply dropping
   it would have broken the read-only snapshot contract. A Stop can therefore
   wait for an in-flight report before the repair task unwinds; `claim` already
   had that property (`animation_recovery.py:77`).

No change to `gui/session.py`: it is shared with other beads, and both fixes it
needs (H1's boot settlement, H2's replacement id, H3's `unavailable` emit) change
the session/collaborator contract. They are proposals above, for that file's
owner. No schema change: `status` still has no CHECK, and the vocabulary is
enforced by the writers (H7).

## Verification

Offline, from the repository root, after `scripts/check_unit_tests.py`'s scratch
XDG setup:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_09_animation_recovery.py -v
# Ran 18 tests in 0.776s ... OK        (repeated 5x, no flakes)
.venv/bin/python scripts/check_unit_tests.py -p test_animation_recovery_store.py -v
# Ran 3 tests in 0.020s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas.py -v
# Ran 11 tests in 2.770s ... OK (skipped=3)
.venv/bin/python scripts/check_unit_tests.py -p test_motion_plan.py -v
# Ran 12 tests in 1.686s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas_validation.py
# Ran 10 tests in 6.45s ... OK (skipped=6)
.venv/bin/python scripts/check_unit_tests.py -p test_animations.py
# Ran 35 tests in 1.71s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_animation_visual_review.py
# Ran 11 tests in 0.23s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_artifacts.py
# OK        (the ledger lock is also the artifact-report writer exclusion)
.venv/bin/python -m ruff check src/blackrose/animation_recovery.py \
    src/blackrose/motion_canvas.py tests/test_sm_09_animation_recovery.py
# All checks passed!
```

The native playback suites (real GTK/WebKit, `DISPLAY=:0`):

```sh
BLACKROSE_TEST_WEBKIT=1 .venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas.py
# Ran 11 tests in 13.71s ... OK
BLACKROSE_TEST_WEBKIT=1 .venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas_validation.py
# Ran 10 tests in 43.07s ... OK
BLACKROSE_TEST_WEBKIT=1 .venv/bin/python scripts/check_unit_tests.py -p test_animation_recovery.py
# Ran 9 tests in 5.25s ... OK
```

Regression demonstration: with the two source files replaced by their previous
versions, `test_a_cancelled_finish_still_records_the_replacement_link` fails with
`('repairing', None) != ('replaced', 101)` and
`test_two_cues_inside_the_endpoint_tolerance_keep_the_last_one` errors with
`Caption times must increase and fit within duration`. Both pass with the
changes.

Not run: `scripts/check_gui_motion_canvas.py --recovery` (an interactive desktop
probe; it drives the real window). The session-level questions H1 and H3 are
answered from code and from the ledger-level tests; confirming the user-visible
label needs that native probe or a session-level failure injection, which is the
follow-up recommended for the `session.py` owner.
