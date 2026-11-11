# SM-01: Turn orchestration and settlement

## Scope and ownership

| Path | Owns |
| --- | --- |
| `src/blackrose/turns.py` (`TurnRunner`, 218–825) | Request admission, the five turn phases, the generation reservation, the reply's settlement state (`assistant`, `completed`, `content`), the checkpoint task, the citation audit attachment, the final write and the interruption fallback |
| `src/blackrose/chat.py:124–139` | The four controller-level flags the turn hands to its caller: `is_generating`, `last_request_stored`, `last_completed_turn`, `outbound`; `send` (1462–1471) is a one-line delegation to `TurnRunner(...).run(...)` |
| `src/blackrose/storage/repository.py:619–651, 750–819, 884–917` | Durable turn creation (`begin_turn`), the two guarded pending writers (`checkpoint_content`, `save_audit`), the one terminal writer (`finish_message`), crash reaping (`reap_pending`) |
| `src/blackrose/citations.py:719–1035` (`CitationAudit`) | The audit's own phases; `run` (956–1035) is awaited inline by `_complete` |
| `src/blackrose/gui/session.py:1414–1456, 1507–1537` | The only reader of `last_completed_turn` and of `last_request_stored` after a turn ends |

One `TurnRunner` is constructed per send (`chat.py:1466`) and is single-use
(`turns.py:3–23`). Every transition below runs on the caller's asyncio task
except the plain-reply checkpoint task (`turns.py:238–257`), which is created in
`_start_reply` and drained before any terminal write. Repository work is
serialized through `Database.run` (`storage/db.py:82–94`): an `asyncio.Lock`
around one worker thread, with rollback on failure and a worker that keeps the
lock until the thread finishes even when its awaiting task is cancelled
(`db.py:20–35`). That is what makes the shield-and-wait idioms in
`_complete` (740–758) and `_settle_interrupted` (766–773) meaningful: a
cancelled waiter does not cancel the write.

Ownership from [architecture](../architecture.md#modules-and-invariants):
`turns.py` owns "Request admission, atomic turn creation and cancellation-safe
identity handoff, streaming …, terminal reply state and evidence attribution";
`gui/session.py` owns "active reply state, complete reload snapshots,
cancellation recovery before persistence". `chat.py` owns nothing about the turn
beyond storing the flags for that caller. `TurnRunner` binds the originating
conversation in `ContextVar` `_turn_conversation` for the whole turn
(`turns.py:296–306`, `chat.py:119–151`), so navigation cannot redirect a turn's
history, tools or outbound grants.

`is_generating` is *not* private to turns. The same boolean is the controller's
generation reservation for `select_model` (`chat.py:302,321`), reviewed-edit
commit (`1167,1192`), backup restore (`1223,1237`), library approval
(`1282,1301`) and diagram repair (`diagram_repairs.py:26,61`). A turn is the
longest holder, not the only one.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `ChatController.is_generating` | `chat.py:124` | `bool` | Global generation reservation. `True` from turn admission until the turn's `finally`. | `turns.py:338` (set), `368` (clear); also `chat.py:302/321`, `1167/1192`, `1223/1237`, `1282/1301`, `diagram_repairs.py:26/61` |
| `ChatController.last_request_stored` | `chat.py:127` | `bool` | Whether *the last admitted send* committed its request row, so its text is not a lost draft. Never cleared when a turn ends; it describes the previous turn until the next `_run`. | `turns.py:339` (reset), `493` and `495` (set) — no other writer |
| `ChatController.last_completed_turn` | `chat.py:129` | `Turn \| None` | The reply this send committed, when Stop landed after the complete write; `None` on every other exit. | `turns.py:296` (reset), `753` and `759` (set) — no other writer |
| `ChatController.outbound` | `chat.py:139` | `OutboundPolicy \| None` | This reply's web/remote-image authorization; none means every outbound tool refuses (`chat.py:730–740`, `1442–1445`). | `turns.py:438` (install), `367` (clear) |
| `TurnRunner.assistant` | `turns.py:228` | `StoredMessage \| None` | The pending reply row this turn owns. Exists only after `begin_turn` committed. | `turns.py:483`, `492` — no other writer |
| `TurnRunner.completed` | `turns.py:236` | `Turn \| None` | Proof that the `COMPLETE` row was written and committed. Settlement never runs once it is set. | `turns.py:753`, `759` — no other writer |
| `TurnRunner.content` | `turns.py:227` | `str` | Visible answer text accumulated from model and tool deltas; what a terminal write stores. | `turns.py:533` (append), `728` (audited final), `784` (revert to audit draft) |
| `TurnRunner.checkpoint_task` | `turns.py:233` | `asyncio.Task \| None` | Background plain-reply checkpoint loop (`_checkpoint_content`, 238–257), created only when citation rigor is `off`. | `turns.py:525` (create); never cancelled, only drained (`803–808`) |
| `TurnRunner.checkpoint_stop` | `turns.py:234` | `asyncio.Event` | Stop flag for that loop; set by `_stop_checkpoints` (`804`). | `turns.py:804` |
| `TurnRunner.citation_audit` | `turns.py:231` | `CitationAudit \| None` | The audit for this reply, created only when rigor is not `off`. | `turns.py:517–523` |
| `TurnRunner.ui_stream` / `executor` | `turns.py:230`, `229` | objects | Streaming/block and tool services; context, not discriminators. | `turns.py:506`, `511` |
| `messages.state` etc. | `sm-02` | persisted | The durable side of the same machine | `repository.py` (see SM-02) |

`TurnRunner` is discarded after `run` returns, so `assistant`/`completed`/
`checkpoint_task`/`citation_audit` are only observable through instrumentation of
a live runner. The three controller flags and `outbound` are observable at rest.

## States

The 6-tuple used throughout is
`(is_generating, last_request_stored, assistant, completed, checkpoint_task,
citation_audit)`, with the last two read as *not `None`*. `outbound` is a seventh,
derived bit: it is installed late in phase 1 (`turns.py:403,438`) and cleared in
the `finally` (`367`), so `outbound != None ⇒ is_generating` and it is `None`
everywhere else. One turn is in exactly one of the states below.

The matrix is a property of a **live `TurnRunner`**. `is_generating` is shared
with four non-turn reservations (see *Scope*), so a controller-level snapshot
taken while `select_model`, an edit approval, a backup restore or a diagram
repair holds the reservation has `assistant`/`completed`/services absent and an
arbitrary `last_request_stored` — including the shape of row 4 below. Those
snapshots are not turn states and are what H5 is about.

| State | Predicate | Meaning |
| --- | --- | --- |
| `RESTING` | `¬G ∧ ¬A` | No turn is running and no reservation is held. `S`/`C` describe the previous send. |
| `PREPARING` | `G ∧ ¬A` | Phase 1: `_prepare` (374–428). Nothing is stored; `outbound` appears mid-phase. |
| `COMMITTED` | `G ∧ A ∧ ¬C ∧ ¬K ∧ ¬U` | Phase 2 returned and phase 3 has not attached a reply service yet (the post-commit prompt rebuild, `352–353`). |
| `ANSWERING_PLAIN` | `G ∧ A ∧ ¬C ∧ K` | Phases 3–5 with rigor `off`: model rounds run, checkpoint task writes. |
| `ANSWERING_AUDITED` | `G ∧ A ∧ ¬C ∧ U` | Phases 3–5 with rigor on: model rounds run, audit attached (and later running). |
| `COMPLETED` | `A ∧ C` | The `COMPLETE` write committed. Reached only in `_complete`; the runner is inside its cancel path, its `finally`, or already returned |
| `SETTLED` | `¬G ∧ A ∧ ¬C` | A terminal write (`CANCELLED` or `ERROR`) finished and the reservation was released; the runner is in `finally` or already dropped |

The seven predicates are mutually exclusive and cover all 13 reachable rows:
`RESTING` 1–2, `PREPARING` 3, `COMMITTED` 5, `ANSWERING_PLAIN` 6,
`ANSWERING_AUDITED` 7, `COMPLETED` 8–9 and 13–14, `SETTLED` 10–12.

`ANSWERING_PLAIN`/`ANSWERING_AUDITED` are also the states during `_complete`
before the final write (the audit or the drain has not yet released the
service), so "answering" here means "reply services attached", not "streaming".

The rigor branch is decided once per turn (`turns.py:343`, `516–526`): the two
services are alternatives, not layers. Precisely one is attached when
`_start_reply` returns, and the plain-reply task is *finished* (not merely
unattached) before any terminal write because both terminal paths drain it first
(`735`, `776`).

### Exhaustiveness of the 6-tuple

Three mechanical rules exclude 48 of the 64 combinations:

- **R1 (28 rows) `¬A ∧ (C ∨ K ∨ U)`.** `completed` is built from
  `assistant_message` (`753`), the checkpoint task from `assistant.id` (`525–526`),
  and the audit from `assistant` (`517`); none exists before `_begin_turn`.
- **R2 (16 rows) `A ∧ ¬S`.** `_begin_turn` assigns `self.assistant` and
  `c.last_request_stored` in the same non-await window (`483–495`), and `S` is
  reset only at `_run` entry before `A` can exist (`339`).
- **R3 (4 rows) `K ∧ U`.** `_start_reply` attaches one *or* the other
  (`516–526`), never both.

The remaining 16 are tabulated. Two further rules, visible only inside that set,
exclude 3 rows, leaving 13 reachable for a live turn (48 + 3 = 51 impossible):

- **R4 (2 rows: 15–16) `A ∧ S ∧ C ∧ ¬K ∧ ¬U`.** `_complete` is only reachable
  after `_start_reply` returned (`357`), and it always attaches one service.
- **R5 (1 row: 4) `G ∧ S ∧ ¬A`.** `G` is raised only in `_run`, which resets `S`
  in the same non-await window (`338–339`); `S` is raised only together with `A`.

| # | `(G,S,A,C,K,U)` | Status | Where it occurs |
| --- | --- | --- | --- |
| 1 | `(0,0,0,0,0,0)` | reachable, stable | Idle before a send; also the end state of a phase-1 failure (`test_turns.py::test_cancel_during_prompt_build_stores_nothing_and_releases_the_turn`) |
| 2 | `(0,1,0,0,0,0)` | reachable, **hazardous** | At rest after a stored turn, and — the hazard — during a send whose `_run` has not started (H1) |
| 3 | `(1,0,0,0,0,0)` | reachable, transient | `PREPARING` |
| 4 | `(1,1,0,0,0,0)` | **impossible for a turn (R5)** | `G` is only raised in `_run`, which resets `S` first (`338–339`); `S` is only raised with `A`. A controller-level snapshot during a non-turn reservation can match this shape (H5) |
| 5 | `(1,1,1,0,0,0)` | reachable, transient | `COMMITTED`; also what a Stop during the post-commit prompt rebuild or during `ToolExecutor` construction settles from (`test_cancel_after_the_commit_but_before_any_service_settles_an_empty_reply`) |
| 6 | `(1,1,1,0,1,0)` | reachable, stable | `ANSWERING_PLAIN` |
| 7 | `(1,1,1,0,0,1)` | reachable, stable | `ANSWERING_AUDITED` |
| 8 | `(1,1,1,1,1,0)` | reachable, transient | `COMPLETED`, rigor `off`: between the committed write (`753`) and the `finally` (`368`) |
| 9 | `(1,1,1,1,0,1)` | reachable, transient | `COMPLETED`, rigor on (same window) |
| 10 | `(0,1,1,0,1,0)` | reachable, transient | `SETTLED`, rigor `off`, inside `finally` |
| 11 | `(0,1,1,0,0,1)` | reachable, transient | `SETTLED`, rigor on, inside `finally` |
| 12 | `(0,1,1,0,0,0)` | reachable, transient | `SETTLED` when `_start_reply` failed before attaching a service; also inside `finally` |
| 13 | `(0,1,1,1,1,0)` | reachable, transient | returned or raised after a complete plain reply |
| 14 | `(0,1,1,1,0,1)` | reachable, transient | returned or raised after a complete audited reply |
| 15 | `(1,1,1,1,0,0)` | **impossible (R4)** | `_complete` is only reachable after `_start_reply` returned, which always attaches one service (`357`, `500–529`) |
| 16 | `(0,1,1,1,0,0)` | **impossible (R4)** | same |

Rows 8, 9, 13 and 14 are the only ones in which `completed` is set; every one of
them also has `A` and `S`, which is invariant 3.

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Send | `ChatController.send` → `TurnRunner.run` (`chat.py:1462–1471`, `turns.py:293`) |
| E2 | Admit | `_prepare` passes attachment, model, reference, vision and admission checks (`turns.py:374–428`) |
| E3 | Commit | `_worker(_persist_attachments)` then shielded `repo.begin_turn` (`turns.py:478–495`) |
| E4 | Attach services | `_start_reply` (`turns.py:500–529`) |
| E5 | Rounds | `_answer` → `_stream_round`/`_run_tools` (`544–581`) |
| E6 | Complete | `_complete`: audit (optional), drain, shielded `finish_message(COMPLETE)` (`722–760`) |
| E7 | Stop | `asyncio.CancelledError` anywhere in `_run` (`358–365`) |
| E8 | Failure | any other `BaseException` in `_run` (`358–365`) |
| E9 | Settle | `_settle_interrupted` → shielded `_save_interrupted` (`762–801`) |
| E10 | Settle retry | `_save_interrupted` repeats the terminal write without evidence after a failed first attempt (`790–801`) |
| E11 | Checkpoint tick | `_checkpoint_content` every `CONTENT_CHECKPOINT_SECONDS = 1.0` (`turns.py:53`, `238–257`) |
| E12 | Drain | `_stop_checkpoints` (`803–808`) |
| E13 | Audit checkpoint | `CitationAudit.checkpoint` → `save_audit` (`citations.py:756–764`), called at attach and per audit phase |
| E14 | Second send | refused by `artifacts.exclusive` (`turns.py:303`) or by `_run` (`336–337`) |
| E15 | Startup recovery | `Session.boot` → `recover_interrupted` → `reap_pending` (`chat.py:229–232`, `repository.py:884–917`) |

There is no external IPC into a running turn: `Stop` is task cancellation, and
every other controller command either refuses while `is_generating` or mutates
state the running turn does not read (for example `set_reasoning_effort`,
`chat.py:208–221`, explicitly allows mid-reply changes that a running turn has
already snapshotted at `turns.py:514–515`).

## Transition table

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| — | E1 | `ChatBusy` if `artifacts.exclusive()` is held (`303`, `artifacts.py:173–178`) | Clears `last_completed_turn` (`296`); binds the conversation context (`297`) | `RESTING` (still) |
| `RESTING` | E1 (inside lock) | `not is_generating` (`336–337`) | `is_generating = True`, `last_request_stored = False` (`338–339`); snapshots the rigor mode (`343`) | `PREPARING` |
| `PREPARING` | E2 | attachment budget, `cancel_title`, model load, reference/skill resolution, vision check, `builder.admit` (`382–427`) | Installs `outbound` (`403`, `438`), marks local data exposed when private (`434–437`); stores nothing | `PREPARING` |
| `PREPARING` | E7/E8 | any failure or Stop before E3 | `finally` clears `outbound` and the reservation (`367–368`); no row exists (E7: pinned by `test_cancel_during_prompt_build_stores_nothing_and_releases_the_turn`) | `RESTING` |
| `PREPARING` | E3 | `begin_turn` transaction (`repository.py:631–649`) | Sets `assistant` + `last_request_stored = True` (`483–495`) | `COMMITTED` |
| `COMMITTED` | E7 | cancellation while awaiting the shielded commit | Waits for the identity, sets both fields, re-raises (`484–494`) | `COMMITTED` → settlement |
| `COMMITTED` | E4 | rigor `off` | Creates the checkpoint task (`525–526`) | `ANSWERING_PLAIN` |
| `COMMITTED` | E4 | rigor on, UI off/on | Creates `CitationAudit`, inherits history, checkpoints a pending report (`516–523`) | `ANSWERING_AUDITED` |
| `COMMITTED` | E7/E8 | failure in the post-commit prompt rebuild or in service construction | Settlement writes `CANCELLED`/`ERROR` with `content=''` (`775–801`) | `COMMITTED` (settling) |
| `ANSWERING_*` | E5 | each round admitted and bounded (`583–718`) | Appends visible text and checkpoint content (`531–540`); audit draft tracks `content` (`534–535`) | same |
| `ANSWERING_PLAIN` | E11 | stop not set, snapshot changed (`240–257`) | `repo.checkpoint_content` guarded to `pending` rows with no audit (`repository.py:758–762`) | same |
| `ANSWERING_AUDITED` | E13 | row still `pending` (`repository.py:1132–1134`) | Replaces `content` (hash-checked) and the report in one transaction (`1139–1142`) | same |
| `ANSWERING_*` | E6 | `_answer` returned a tool-free visible answer | Audit runs (`728–731`), evidence deduped (`734`, `810–815`), checkpoints drained (`735`) | `COMPLETED` (write in flight) |
| `COMPLETED`-in-flight | E6 | `finish_message` commits | Sets `completed` and `last_completed_turn` (`759`) | `COMPLETED` |
| `COMPLETED`-in-flight | E7 | Stop while the shielded write runs | Waits for the outcome without raising its failure (`744–750`); on success sets both fields (`753`) | `COMPLETED` → raise |
| `COMPLETED`-in-flight | E7 | Stop and the write failed | Logs (`756–757`), leaves `completed = None`, re-raises | `COMPLETED`-in-flight → settling |
| `COMPLETED`-in-flight | E8 | the write raised | `shield` re-raises the storage error; `completed` stays `None` | settling |
| `ANSWERING_*` | E7 | — | `state = CANCELLED`, `error = 'Stopped'` (`764–765`) | `ANSWERING_*` (settling) |
| `ANSWERING_*` | E8 | — | `state = ERROR`, `error = str(error) or type name` (`764–765`) | `ANSWERING_*` (settling) |
| any with `A` | E7 (during the audit) | `audit.run` is inline, not shielded (`728`) | `audit.run` converts its own report to `incomplete` (`citations.py:1014–1020`); settlement reverts `content` to `audit.draft_text` and writes an `unchecked_report` (`turns.py:783–788`) | `ANSWERING_AUDITED` (settling) |
| settling | E9 | `assistant is not None ∧ completed is None` (`359–364`) | Shields the terminal write (`766–773`); `_save_interrupted` drains, finalises blocks, reverts an audited draft, retries without evidence (`775–801`) | `SETTLED` after the `finally` |
| `COMPLETED` | E7 | `completed is not None` (`359`) | Schedules the title/memory follow-ups for the stored reply (`361–362`); no settlement | `COMPLETED` after the `finally` |
| settling | E9 (failed) | `cleanup.result()` raised (`773`) | The retry without evidence (`799–801`) already failed, so the storage error propagates in place of the original Stop/failure; the row stays `pending` | `SETTLED` with a pending row |
| any | E14 | another send arrives | `ChatBusy`; the live turn's flags and rows are untouched | unchanged |
| `RESTING` (row pending) | E15 | process restarted with a pending row | `reap_pending` writes `ERROR` with the crash text, cancels streaming blocks, interrupts UI events (`repository.py:884–917`) | `ERROR` (durable) |

`E1`-with-lock and `E1`-without-lock both exist: `artifacts.exclusive` refuses
immediately instead of queueing (`artifacts.py:173–178`), so the `_run` guard at
`336` is only reached when the lock is free but another reservation (model
select, edit approval, library approval) holds `is_generating`.

Settling is not a distinct 6-tuple. The terminal write runs while
`is_generating` is still held and after the reply services were attached, so the
runner stays in `COMMITTED`/`ANSWERING_PLAIN`/`ANSWERING_AUDITED` (rows 5–7)
until the `finally` drops the reservation; only then is it `SETTLED` (rows
10–12) or `COMPLETED` (rows 8–9 and 13–14). "Settling" above names that
sub-phase, not a row of the matrix. The same is true of `_complete`: rows 6–7 are
also the states while the final write is in flight.

## Invariants

1. **One turn at a time per controller.** `is_generating` is checked and raised
   in one non-await window (`turns.py:336–339`), and the artifact lock is taken
   before it (`303`). A second send cannot enter `_run`, cannot write the turn's
   rows, and cannot change `is_generating` or `last_request_stored`. It does
   clear `last_completed_turn` (`296`), which a running turn only sets in its
   final step before returning or raising. Pinned by
   `test_a_refused_second_send_does_not_disturb_the_live_turn`.
2. **Reply identity and `last_request_stored` are set atomically.** Both fields
   are assigned in the same statement pair with no `await` between them
   (`483–495`); therefore `assistant is not None ⇒ last_request_stored` for the
   lifetime of a runner. Pinned by the matrix test (no observed row has
   `A ∧ ¬S`) and by `test_cancelling_the_commit_still_yields_the_stored_identity`.
3. **`completed` implies a committed `COMPLETE` row.** `completed` is assigned
   only from the result of a successful `finish_message` (`753`, `759`), whose
   transaction writes `state='complete'` and commits before returning
   (`repository.py:791–794`, `816`); `Database.run` rolls back on any exception
   (`db.py:87–92`). There is no reachable state with `completed` set and the row
   still `pending`. Pinned by
   `test_a_committed_final_save_is_never_followed_by_settlement`.
4. **Settlement never follows a committed write.** `_run` consults `completed`
   first and, when it is set, only schedules follow-ups (`358–362`).
   `_settle_interrupted` has exactly one call site (`364`). Pinned by
   `test_a_committed_final_save_is_never_followed_by_settlement` (a spy observes
   zero settlement calls).
5. **Reply services are mutually exclusive.** `_start_reply` takes exactly one
   branch (`516–526`). While answering, exactly one of `checkpoint_task` (live)
   and `citation_audit` is present; neither is present only before
   `_start_reply` or after it failed before the branch. Pinned by the matrix test.
6. **At most one terminal write per turn.** Exactly one of `_complete`'s shield
   result, `_save_interrupted`'s first attempt or its retry can apply, and the
   drain precedes all of them (`735`, `776`). Pinned by
   `test_checkpoint_drain_is_idempotent_across_complete_and_settlement`
   (three `finish_message` calls, one row, still `pending` because all were
   rejected).
7. **Checkpoint writes are ordered before the terminal write and are
   idempotent to drain.** `_stop_checkpoints` sets the flag and awaits the task
   (`803–808`); both terminal paths call it first, and the failure path calls it
   twice. Pinned by the same test (`order == ['checkpoint', 'finish', 'finish',
   'finish']`), `test_reply_checkpoints.py::test_cancellation_drains_inflight_checkpoint_before_terminal_write`,
   and `test_turns.py::test_stop_before_the_final_save_starts_settles_the_reply_as_cancelled`.
8. **A late checkpoint can never rewrite a terminal row.** Both pending writers
   require `state='pending'` (`repository.py:758–762`, `1132–1134`), and
   `finish_message` is the only terminal writer. Pinned by
   `test_reply_checkpoints.py::test_checkpoint_guards_terminal_user_missing_and_audited_rows`.
9. **Every exit path releases the reservation and the outbound policy.**
   `finally: c.outbound = None; c.is_generating = False` (`366–368`) runs for
   success, Stop and failure, including failures in `_prepare` and in
   settlement. Asserted in every terminal test.
10. **Follow-ups belong to stored replies only.** `_schedule_follow_ups` is
    called on the success path (`369`) and on the committed-during-Stop path
    (`361`), never from settlement. `test_cancelling_the_commit...`,
    `test_cancel_after_the_commit...` and
    `test_a_provider_failure_settles_error_and_releases_the_reservation` assert
    `schedule_title` is not called.
11. **A row left pending by a failed settlement is repaired at startup, once.**
    `reap_pending` filters on `state='pending'` (`repository.py:905–913`), so it
    is idempotent and never rewrites a terminal row. Pinned by SM-02's tests and
    the crash probe in `test_reply_checkpoints.py`.
12. **Stop cannot be prevented from settling by a second Stop.** `_settle_interrupted`
    waits on `shield(cleanup)` in a loop (`766–773`), so the terminal write is
    never cancelled by the caller's cancellations. Pinned by
    `test_turns.py::stop_during_final_save` (two cancels),
    `test_reply_checkpoints.py::test_cancellation_drains_inflight_checkpoint_before_terminal_write`
    (two cancels), and `test_settlement_failure_masks_the_stop_with_the_storage_error`
    (the failure itself surfaces, rather than being swallowed).

## Hazards and open questions

### Answers to the bead's questions

- **Every reachable combination** is enumerated above: 13 of 64 for a live turn.
  Rules R1–R3 exclude 48 rows; R4–R5 exclude rows 4, 15 and 16. Only row 2 is
  hazardous. `is_generating` outside a turn can reproduce row 4's shape; that is
  H5, not a turn state.
- **Cancellation during each shielded await** is answered in the table below.
- **`last_request_stored` with `assistant is None`?** Within a runner: no
  (invariant 2). At controller level: yes and it is the normal resting value
  after any stored turn — and it is exactly the hazardous window when a new
  send is cancelled before `_run` begins (H1).
- **`assistant` set while `last_request_stored` is `False`?** No path: the two
  are assigned together (`483–495`) and nothing else writes either.
- **`completed` set while the row is still `pending`?** No: `completed` is only
  assigned from a returned `finish_message`, which commits the terminal state in
  the same transaction.
- **Both `checkpoint_task` and `citation_audit` live?** Never (single `if/else`,
  `516–526`). **Neither?** Only before `_start_reply`, or if it failed before the
  branch; the resulting settlement is state 12 and writes an empty `CANCELLED`
  row.
- **Does `_settle_interrupted` ever run after `_complete` partially succeeded?**
  It runs only when `completed is None` (`359–364`), and `completed` is set iff
  the write returned. `finish_message` is one transaction, so "partially
  succeeded" does not exist: it either commits all of
  content/state/error/audit/evidence/outbox or rolls back. The only theoretical
  exception is event-loop shutdown cancelling the `saving` task
  (`751`), in which case the row is left pending and startup recovery decides.
- **What does the caller do with `last_completed_turn` when settlement failed?**
  `ChatController.send` ignores it (`chat.py:1462–1471`). The GUI reads it once,
  in the `except CancelledError` handler (`session.py:1443–1446`); when it is
  `None` Session renders a `cancelled` (or, for a storage error, `error`)
  outcome and `_interrupted_answer` re-reads the *latest assistant row* from the
  database (`1507–1517`) — a row that is still `pending` with its last
  checkpoint — then `reap_pending` converts it to `ERROR` with the crash text on
  the next start.

| Shielded boundary | Site | What a Stop does |
| --- | --- | --- |
| `builder.build` in `_prepare` | `421` (not shielded; `to_thread`) | Raises at once; the build thread's result is discarded; nothing stored; reservation released (`367–368`) |
| `_worker(_persist_attachments)` | `478` (worker shielded, caller not) | `_worker` waits for the thread, then raises `CancelledError`; blobs exist, no rows; H4 |
| `asyncio.shield(repo.begin_turn)` | `483–494` | The handler waits for the identity, latches `assistant` + `last_request_stored`, re-raises → `CANCELLED` row |
| `audit.inherit` / `audit.checkpoint('')` | `520`, `523` (not shielded) | Raises; the audit is already attached (`519`), so settlement writes an `unchecked_report` over `draft_text` (still `''`) |
| `asyncio.shield(checkpoint_task)` | `808` (single shot) | Raises out of `_complete`; the task keeps running (shield protects it) and settlement drains it again → H2 |
| `CitationAudit.run` in `_complete` | `728` (not shielded) | `run` converts its report to `incomplete` and re-raises; settlement reverts to `draft_text` and writes `CANCELLED` — pinned by `test_cancelling_the_audit_checkpoint_settles_an_unchecked_report` |
| `asyncio.shield(finish_message)` | `740–758` | Waits for the outcome; commits → `completed` + follow-ups; fails → logs and settles `CANCELLED` |
| `asyncio.shield(cleanup)` in `_settle_interrupted` | `767–773` | Cannot cancel the terminal write; repeat Stops are absorbed |

- **H1 — `defect` — `turns.py:296` vs `338–339`.** `run` clears
  `last_completed_turn` before its first await but resets `last_request_stored`
  only inside `_run`, after `ensure_conversation()` (`297`) and the keyed-provider
  credential lookup (`300–301`, which awaits a thread for `opencode-go` models,
  `backend/opencode.py:46`). A Stop in that window leaves the previous turn's
  `True` in place. The GUI then concludes this send stored its request
  (`session.py:1452`, `1469`, `1520`): it neither restores the composer text nor
  records the unsaved outcome, and `_draft_sending` is consumed, so the typed
  text exists nowhere. Reproduced deterministically by
  `test_a_send_cancelled_before_admission_inherits_the_last_stored_flag_documented_follow_up`
  (the test asserts current behaviour; applying the patch in *Formalization
  decisions* inverts its one `assertTrue`). Confirmation: the test plus a GUI
  probe that presses Stop immediately after Enter with a keyed provider.
- **H2 — `fragile` — `turns.py:803–808`.** `_stop_checkpoints` shields the drain
  once, not in a loop, unlike `_complete` (744–750) and `_settle_interrupted`
  (767–773). A repeat Stop while it is awaiting raises out of `_complete`, so the
  drain is abandoned; correctness is restored only because settlement calls
  `_stop_checkpoints` again inside the shielded cleanup task, and because the
  repositories refuse to write a non-pending row. No user-visible failure was
  found; pinned by the existing two-Stop tests. A loop would make the guarantee
  local rather than emergent.
- **H3 — `documentation-gap` — `turns.py:766–773`, `799–801`.** When settlement
  itself fails, `cleanup.result()` replaces the original Stop (or provider error)
  with the storage error, so the caller reports `error`, not `cancelled`, and the
  row stays `pending` until startup recovery. This is deliberate ("Failure to
  settle the terminal row still propagates for startup recovery", 795–797) but
  the state change is not documented anywhere else. Demonstrated by
  `test_settlement_failure_masks_the_stop_with_the_storage_error` and, for the
  failure path, `test_terminal_evidence.py::test_failure_to_settle_terminal_state_still_propagates`.
- **H4 — `documentation-gap` — `turns.py:478`.** Attachment blobs are persisted
  (and content-addressed on disk) before the turn row is created, so a Stop in
  that thread leaves a blob no message references. Artifact cleanup reconciles
  recorded obligations only, so nothing in the turn path reclaims it.
  Demonstrated by
  `test_cancel_while_persisting_attachments_leaves_no_turn_documented_follow_up`.
  Confirming the leak's size needs the orphan report from
  `ArtifactLifecycle` over a store that went through this path.
- **H5 — `fragile` — `chat.py:302, 1167, 1223, 1282`; `diagram_repairs.py:26`.**
  `is_generating` is a shared reservation, so `is_generating == True` does not
  imply a turn is running, and a second send's refusal message depends on which
  holder is active: `artifacts.exclusive` ("Wait for the current reply or
  artifact cleanup to finish", `artifacts.py:176`) when a turn or repair holds
  the lock, `_run`'s "A reply is already in progress" (`turns.py:337`) when
  `select_model` or a library/edit approval does. Nothing reads the flag as a
  phase discriminator, so this is not a correctness defect; it is the reason the
  6-tuple, not `is_generating`, is the machine.
- **H6 — `none-found` — the rest of the machine.** The writer is exclusive, the
  identity handoff is shielded, both terminal paths drain before writing, the
  repository refuses late checkpoints, and the only two reachable-but-wrong
  tuples/behaviours are H1 and the H2 emergency path. No lost update, stranded
  obligation or duplicated settlement was found in a product path.

## Formalization decisions

**H1 fixed in `turns.py`; the rest is unchanged.** The two files in scope
(`src/blackrose/turns.py`, `src/blackrose/chat.py`) are shared with concurrent
work, but the demonstrated defect (H1) was applied by the orchestrator once the
concurrent beads had landed: `TurnRunner.run` now takes the refusal guard before
its first await and clears `last_request_stored` beside `last_completed_turn`.
The characterization test was inverted to
`test_a_send_cancelled_before_admission_reports_nothing_stored`. The patch is
kept below for review.

```python
    async def run(self, *args, **kwargs):
        # A refused send owns nothing: leave the live turn's flags alone.
        if self.chat.is_generating:
            raise ChatBusy('A reply is already in progress')
        # Cleared before the first await, so a Stop that lands before this turn
        # begins cannot report an earlier turn's reply or request as this one's.
        self.chat.last_completed_turn = None
        self.chat.last_request_stored = False
        token = self.chat._turn_conversation.set(await self.chat.ensure_conversation())
```

Why here and not line 339: clearing the flag earlier without the guard would let
a `ChatBusy`-refused send erase the live turn's flag, turning H1's lost draft
into a restored draft on top of an already-stored request. The guard must move
with it. `ChatBusy` is already imported (`turns.py:38`), and `_run`'s own check
(`336–337`) stays as the admission point's guard — it is also the path taken when
`select_model`, an edit approval or a library approval holds the reservation.

Follow-up after the patch: invert the single `assertTrue(c.last_request_stored)`
in `test_a_send_cancelled_before_admission_inherits_the_last_stored_flag_documented_follow_up`
and drop the `_documented_follow_up` suffix.

Two smaller items are deliberately left alone:

- **H2** could be made local by looping the drain (`turns.py:803–808`) exactly as
  `_settle_interrupted` does. It is not a defect today, and the repository guards
  are what actually prevent a late write; a local loop is a robustness
  improvement, not a fix.
- **H3** already propagates by design. If the product wants a stopped turn whose
  settlement failed to still read as `cancelled`, the change belongs in
  `Session._send`, not in the turn machine.

Nothing in this machine needs a new explicit phase enum. The 13 reachable tuples
are a strict function of `(is_generating, last_request_stored, assistant,
completed, checkpoint_task, citation_audit)`, every impossible combination is
excluded by a structural rule, and the only guard the code lacks is H1's
pre-admission flag reset.

## Verification

Offline, from the repository root, with `.venv/bin/python` on Python 3.13:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_01_turn_orchestration.py -v
# Ran 13 tests in 0.266s
# OK

.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
# Ran 28 tests in 0.891s
# OK

.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
# Ran 6 tests in 3.225s
# OK   (includes the process-kill crash probe)

.venv/bin/python scripts/check_unit_tests.py -p test_terminal_evidence.py
# Ran 8 tests in 0.354s
# OK
```

`test_sm_01_turn_orchestration.py` was run three times to check determinism
(12 tests before the H4 case was added, then 13): `OK` each time. `ruff check`
on the new test file and the repository-wide `mypy` both pass.

The full offline gate was also run, and its failures are environmental and
pre-existing in this checkout, not related to this machine:

```sh
.venv/bin/python scripts/check_health.py
# passed: python -m ruff check src tests scripts
# passed: mypy
# passed: node --check (all five page scripts)
# failed: scripts/check_unit_tests.py  (1378 tests, 3 errors) — test_motion_canvas /
#         test_motion_plan: missing generated gui/static/motion-canvas-runtime.js
#         ("run blackrose setup-animations")
# failed: scripts/check_recovery.py    — same missing Motion Canvas toolchain
# failed: npm test --prefix ui-runtime — esbuild not installed in this checkout
```

Not run: the `--gui` probes (`scripts/check_gui_session.py`) and the live model
probes; neither is needed for this machine, which is entirely in-process. The
H1 window's user-visible consequence was reasoned from
`gui/session.py:1441–1470, 1519–1537` and reproduced at the controller level, not
through the native window; a GUI probe would need a keyed provider and a Stop
tapped within the credential lookup.
