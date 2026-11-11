# SM-01: Turn orchestration and settlement

## Scope and ownership

| Path | Owns |
| --- | --- |
| `src/blackrose/turns.py::TurnRunner` | Request admission, the five turn phases, the generation reservation, the reply's settlement state (`assistant`, `completed`, `content`), the checkpoint task, the citation audit attachment, the final write and the interruption fallback |
| `src/blackrose/chat.py::ChatController.__init__` | The four controller-level flags the turn hands to its caller: `is_generating`, `last_request_stored`, `last_completed_turn`, `outbound`; `src/blackrose/chat.py::ChatController.send` is a one-line delegation to `TurnRunner(...).run(...)` |
| `src/blackrose/storage/repository.py::Repository` | Durable turn creation (`begin_turn`), the two guarded pending writers (`checkpoint_content`, `save_audit`), the one terminal writer (`finish_message`), crash reaping (`reap_pending`) |
| `src/blackrose/citations.py::CitationAudit` | The audit's own phases; `CitationAudit.run` is awaited inline by `_complete` |
| `src/blackrose/gui/session.py::Session._send`, `src/blackrose/gui/session.py::Session._interrupted_answer` | The only reader of `last_completed_turn` and of `last_request_stored` after a turn ends |

One `TurnRunner` is constructed per send (`src/blackrose/chat.py::ChatController.send`) and is single-use
(the `src/blackrose/turns.py` module docstring). Every transition below runs on the caller's asyncio task
except the plain-reply checkpoint task (`src/blackrose/turns.py::TurnRunner._checkpoint_content`), which is created in
`_start_reply` and drained before any terminal write. Repository work is
serialized through `src/blackrose/storage/db.py::Database.run`: an `asyncio.Lock`
around one worker thread, with rollback on failure and a worker that keeps the
lock until the thread finishes even when its awaiting task is cancelled
(`src/blackrose/storage/db.py::_worker`). That is what makes the shield-and-wait idioms in
`_complete` and `_settle_interrupted` meaningful: a
cancelled waiter does not cancel the write.

Ownership from [architecture](../architecture.md#modules-and-invariants):
`turns.py` owns "Request admission, atomic turn creation and cancellation-safe
identity handoff, streaming …, terminal reply state and evidence attribution";
`gui/session.py` owns "active reply state, complete reload snapshots,
cancellation recovery before persistence". `chat.py` owns nothing about the turn
beyond storing the flags for that caller. `TurnRunner` binds the originating
conversation in `ContextVar` `_turn_conversation` for the whole turn
(`src/blackrose/turns.py::TurnRunner.run`, `src/blackrose/chat.py::ChatController.conversation`), so navigation cannot redirect a turn's
history, tools or outbound grants.

`is_generating` is *not* private to turns. The same boolean is the controller's
generation reservation for `src/blackrose/chat.py::ChatController.select_model`, reviewed-edit
commit (`src/blackrose/chat.py::ChatController._review_edit`), backup restore (`src/blackrose/chat.py::ChatController._restore_edit_backup`), library approval
(`src/blackrose/chat.py::ChatController.review_library_add`) and diagram repair (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`). A turn is the
longest holder, not the only one.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `ChatController.is_generating` | `src/blackrose/chat.py::ChatController.__init__` | `bool` | Global generation reservation. `True` from turn admission until the turn's `finally`. | `src/blackrose/turns.py::TurnRunner._run` (set at admission, cleared in its `finally`); also `select_model`, `_review_edit`, `_restore_edit_backup`, `review_library_add` and `DiagramRepairs.repair`, each setting and clearing it |
| `ChatController.last_request_stored` | `src/blackrose/chat.py::ChatController.__init__` | `bool` | Whether *the last admitted send* committed its request row, so its text is not a lost draft. Never cleared when a turn ends; it describes the previous turn until the next `_run`. | `src/blackrose/turns.py::TurnRunner._run` (reset), `src/blackrose/turns.py::TurnRunner._begin_turn` (set, on both its cancelled and normal paths) — no other writer |
| `ChatController.last_completed_turn` | `src/blackrose/chat.py::ChatController.__init__` | `Turn \| None` | The reply this send committed, when Stop landed after the complete write; `None` on every other exit. | `src/blackrose/turns.py::TurnRunner.run` (reset), `src/blackrose/turns.py::TurnRunner._complete` (set, on both its Stop and normal paths) — no other writer |
| `ChatController.outbound` | `src/blackrose/chat.py::ChatController.__init__` | `OutboundPolicy \| None` | This reply's web/remote-image authorization; none means every outbound tool refuses (`src/blackrose/chat.py::ChatController._call_tool`, `src/blackrose/chat.py::ChatController._authorize_remote_image`). | `src/blackrose/turns.py::TurnRunner._install_outbound_policy` (install), the `finally` of `src/blackrose/turns.py::TurnRunner._run` (clear) |
| `TurnRunner.assistant` | `src/blackrose/turns.py::TurnRunner.__init__` | `StoredMessage \| None` | The pending reply row this turn owns. Exists only after `begin_turn` committed. | `src/blackrose/turns.py::TurnRunner._begin_turn` — no other writer |
| `TurnRunner.completed` | `src/blackrose/turns.py::TurnRunner.__init__` | `Turn \| None` | Proof that the `COMPLETE` row was written and committed. Settlement never runs once it is set. | `src/blackrose/turns.py::TurnRunner._complete` — no other writer |
| `TurnRunner.content` | `src/blackrose/turns.py::TurnRunner.__init__` | `str` | Visible answer text accumulated from model and tool deltas; what a terminal write stores. | `src/blackrose/turns.py::TurnRunner._append` (append), `src/blackrose/turns.py::TurnRunner._complete` (audited final), `src/blackrose/turns.py::TurnRunner._save_interrupted` (revert to audit draft) |
| `TurnRunner.checkpoint_task` | `src/blackrose/turns.py::TurnRunner.__init__` | `asyncio.Task \| None` | Background plain-reply checkpoint loop (`src/blackrose/turns.py::TurnRunner._checkpoint_content`), created only when citation rigor is `off`. | `src/blackrose/turns.py::TurnRunner._start_reply` (create); never cancelled, only drained (`src/blackrose/turns.py::TurnRunner._stop_checkpoints`) |
| `TurnRunner.checkpoint_stop` | `src/blackrose/turns.py::TurnRunner.__init__` | `asyncio.Event` | Stop flag for that loop; set by `_stop_checkpoints`. | `src/blackrose/turns.py::TurnRunner._stop_checkpoints` |
| `TurnRunner.citation_audit` | `src/blackrose/turns.py::TurnRunner.__init__` | `CitationAudit \| None` | The audit for this reply, created only when rigor is not `off`. | `src/blackrose/turns.py::TurnRunner._start_reply` |
| `TurnRunner.ui_stream` / `executor` | `src/blackrose/turns.py::TurnRunner.__init__` | objects | Streaming/block and tool services; context, not discriminators. | `src/blackrose/turns.py::TurnRunner._start_reply` |
| `messages.state` etc. | `sm-02` | persisted | The durable side of the same machine | `repository.py` (see SM-02) |

`TurnRunner` is discarded after `run` returns, so `assistant`/`completed`/
`checkpoint_task`/`citation_audit` are only observable through instrumentation of
a live runner. The three controller flags and `outbound` are observable at rest.

## States

The 6-tuple used throughout is
`(is_generating, last_request_stored, assistant, completed, checkpoint_task,
citation_audit)`, with the last two read as *not `None`*. `outbound` is a seventh,
derived bit: it is installed late in phase 1 (`src/blackrose/turns.py::TurnRunner._install_outbound_policy`, called from `_prepare`) and cleared in
the `finally` of `_run`, so `outbound != None ⇒ is_generating` and it is `None`
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
| `PREPARING` | `G ∧ ¬A` | Phase 1: `src/blackrose/turns.py::TurnRunner._prepare`. Nothing is stored; `outbound` appears mid-phase. |
| `COMMITTED` | `G ∧ A ∧ ¬C ∧ ¬K ∧ ¬U` | Phase 2 returned and phase 3 has not attached a reply service yet (the post-commit prompt rebuild in `src/blackrose/turns.py::TurnRunner._run`). |
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

The rigor branch is decided once per turn (`mode` is read in `src/blackrose/turns.py::TurnRunner._run` and branched on in `src/blackrose/turns.py::TurnRunner._start_reply`): the two
services are alternatives, not layers. Precisely one is attached when
`_start_reply` returns, and the plain-reply task is *finished* (not merely
unattached) before any terminal write because both terminal paths drain it first
(`_complete` and `_save_interrupted` each call `_stop_checkpoints` before writing).

### Exhaustiveness of the 6-tuple

Three mechanical rules exclude 48 of the 64 combinations:

- **R1 (28 rows) `¬A ∧ (C ∨ K ∨ U)`.** `completed` is built from
  `assistant_message` (`src/blackrose/turns.py::TurnRunner._complete`), the checkpoint task from `assistant.id` and the
  audit from `assistant` (both in `src/blackrose/turns.py::TurnRunner._start_reply`); none exists before `_begin_turn`.
- **R2 (16 rows) `A ∧ ¬S`.** `_begin_turn` assigns `self.assistant` and
  `c.last_request_stored` in the same non-await window, and `S` is
  reset only at `_run` entry before `A` can exist.
- **R3 (4 rows) `K ∧ U`.** `_start_reply` attaches one *or* the other
  (a single `if`/`else` on the rigor mode), never both.

The remaining 16 are tabulated. Two further rules, visible only inside that set,
exclude 3 rows, leaving 13 reachable for a live turn (48 + 3 = 51 impossible):

- **R4 (2 rows: 15–16) `A ∧ S ∧ C ∧ ¬K ∧ ¬U`.** `_complete` is only reachable
  after `_start_reply` returned (the phase order in `_run`), and it always attaches one service.
- **R5 (1 row: 4) `G ∧ S ∧ ¬A`.** `G` is raised only in `_run`, which resets `S`
  in the same non-await window; `S` is raised only together with `A`.

| # | `(G,S,A,C,K,U)` | Status | Where it occurs |
| --- | --- | --- | --- |
| 1 | `(0,0,0,0,0,0)` | reachable, stable | Idle before a send; also the end state of a phase-1 failure (`test_turns.py::test_cancel_during_prompt_build_stores_nothing_and_releases_the_turn`) |
| 2 | `(0,1,0,0,0,0)` | reachable, **hazardous** | At rest after a stored turn, and — the hazard — during a send whose `_run` has not started (H1) |
| 3 | `(1,0,0,0,0,0)` | reachable, transient | `PREPARING` |
| 4 | `(1,1,0,0,0,0)` | **impossible for a turn (R5)** | `G` is only raised in `_run`, which resets `S` in the same statement pair; `S` is only raised with `A`. A controller-level snapshot during a non-turn reservation can match this shape (H5) |
| 5 | `(1,1,1,0,0,0)` | reachable, transient | `COMMITTED`; also what a Stop during the post-commit prompt rebuild or during `ToolExecutor` construction settles from (`test_cancel_after_the_commit_but_before_any_service_settles_an_empty_reply`) |
| 6 | `(1,1,1,0,1,0)` | reachable, stable | `ANSWERING_PLAIN` |
| 7 | `(1,1,1,0,0,1)` | reachable, stable | `ANSWERING_AUDITED` |
| 8 | `(1,1,1,1,1,0)` | reachable, transient | `COMPLETED`, rigor `off`: between the committed write in `_complete` and the `finally` of `_run` |
| 9 | `(1,1,1,1,0,1)` | reachable, transient | `COMPLETED`, rigor on (same window) |
| 10 | `(0,1,1,0,1,0)` | reachable, transient | `SETTLED`, rigor `off`, inside `finally` |
| 11 | `(0,1,1,0,0,1)` | reachable, transient | `SETTLED`, rigor on, inside `finally` |
| 12 | `(0,1,1,0,0,0)` | reachable, transient | `SETTLED` when `_start_reply` failed before attaching a service; also inside `finally` |
| 13 | `(0,1,1,1,1,0)` | reachable, transient | returned or raised after a complete plain reply |
| 14 | `(0,1,1,1,0,1)` | reachable, transient | returned or raised after a complete audited reply |
| 15 | `(1,1,1,1,0,0)` | **impossible (R4)** | `_complete` is only reachable after `_start_reply` returned, which always attaches one service (`src/blackrose/turns.py::TurnRunner._run`, `src/blackrose/turns.py::TurnRunner._start_reply`) |
| 16 | `(0,1,1,1,0,0)` | **impossible (R4)** | same |

Rows 8, 9, 13 and 14 are the only ones in which `completed` is set; every one of
them also has `A` and `S`, which is invariant 3.

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Send | `src/blackrose/chat.py::ChatController.send` → `src/blackrose/turns.py::TurnRunner.run` |
| E2 | Admit | `src/blackrose/turns.py::TurnRunner._prepare` passes attachment, model, reference, vision and admission checks |
| E3 | Commit | `_worker(_persist_attachments)` then shielded `repo.begin_turn` (`src/blackrose/turns.py::TurnRunner._begin_turn`) |
| E4 | Attach services | `src/blackrose/turns.py::TurnRunner._start_reply` |
| E5 | Rounds | `src/blackrose/turns.py::TurnRunner._answer` → `_stream_round`/`_run_tools` |
| E6 | Complete | `src/blackrose/turns.py::TurnRunner._complete`: audit (optional), drain, shielded `finish_message(COMPLETE)` |
| E7 | Stop | `asyncio.CancelledError` anywhere in `_run` (its `except BaseException` handler, `src/blackrose/turns.py::TurnRunner._run`) |
| E8 | Failure | any other `BaseException` in `_run` (the same handler) |
| E9 | Settle | `src/blackrose/turns.py::TurnRunner._settle_interrupted` → shielded `src/blackrose/turns.py::TurnRunner._save_interrupted` |
| E10 | Settle retry | `_save_interrupted` repeats the terminal write without evidence after a failed first attempt (the `except` at the end of `_save_interrupted`) |
| E11 | Checkpoint tick | `src/blackrose/turns.py::TurnRunner._checkpoint_content` every `CONTENT_CHECKPOINT_SECONDS = 1.0` (module constant in `src/blackrose/turns.py`) |
| E12 | Drain | `src/blackrose/turns.py::TurnRunner._stop_checkpoints` |
| E13 | Audit checkpoint | `src/blackrose/citations.py::CitationAudit.checkpoint` → `save_audit`, called at attach and per audit phase |
| E14 | Second send | refused by `artifacts.exclusive` (entered in `src/blackrose/turns.py::TurnRunner.run`) or by `src/blackrose/turns.py::TurnRunner._run` |
| E15 | Startup recovery | `Session.boot` → `recover_interrupted` → `reap_pending` (`src/blackrose/chat.py::ChatController.recover_interrupted`, `src/blackrose/storage/repository.py::Repository.reap_pending`) |

There is no external IPC into a running turn: `Stop` is task cancellation, and
every other controller command either refuses while `is_generating` or mutates
state the running turn does not read (for example `src/blackrose/chat.py::ChatController.set_reasoning_effort`
explicitly allows mid-reply changes that a running turn has
already snapshotted in `src/blackrose/turns.py::TurnRunner._start_reply`).

## Transition table

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| — | E1 | `ChatBusy` if `artifacts.exclusive()` is held (`src/blackrose/turns.py::TurnRunner.run`, `src/blackrose/artifacts.py::ArtifactLifecycle.exclusive`) | Clears `last_completed_turn`; binds the conversation context (`src/blackrose/turns.py::TurnRunner.run`) | `RESTING` (still) |
| `RESTING` | E1 (inside lock) | `not is_generating` (`src/blackrose/turns.py::TurnRunner._run`) | `is_generating = True`, `last_request_stored = False`; snapshots the rigor mode (`src/blackrose/turns.py::TurnRunner._run`) | `PREPARING` |
| `PREPARING` | E2 | attachment budget, `cancel_title`, model load, reference/skill resolution, vision check, `builder.admit` (`src/blackrose/turns.py::TurnRunner._prepare`) | Installs `outbound` and marks local data exposed when private (`src/blackrose/turns.py::TurnRunner._install_outbound_policy`); stores nothing | `PREPARING` |
| `PREPARING` | E7/E8 | any failure or Stop before E3 | `finally` of `_run` clears `outbound` and the reservation; no row exists (E7: pinned by `test_cancel_during_prompt_build_stores_nothing_and_releases_the_turn`) | `RESTING` |
| `PREPARING` | E3 | `begin_turn` transaction (`src/blackrose/storage/repository.py::Repository.begin_turn`) | Sets `assistant` + `last_request_stored = True` (`src/blackrose/turns.py::TurnRunner._begin_turn`) | `COMMITTED` |
| `COMMITTED` | E7 | cancellation while awaiting the shielded commit | Waits for the identity, sets both fields, re-raises (the `CancelledError` handler in `_begin_turn`) | `COMMITTED` → settlement |
| `COMMITTED` | E4 | rigor `off` | Creates the checkpoint task (`src/blackrose/turns.py::TurnRunner._start_reply`) | `ANSWERING_PLAIN` |
| `COMMITTED` | E4 | rigor on, UI off/on | Creates `CitationAudit`, inherits history, checkpoints a pending report (`src/blackrose/turns.py::TurnRunner._start_reply`) | `ANSWERING_AUDITED` |
| `COMMITTED` | E7/E8 | failure in the post-commit prompt rebuild or in service construction | Settlement writes `CANCELLED`/`ERROR` with `content=''` (`src/blackrose/turns.py::TurnRunner._save_interrupted`) | `COMMITTED` (settling) |
| `ANSWERING_*` | E5 | each round admitted and bounded (`src/blackrose/turns.py::TurnRunner._admit_round`, `src/blackrose/turns.py::TurnRunner._stream_round`, `src/blackrose/turns.py::TurnRunner._run_tools`) | Appends visible text and checkpoint content; audit draft tracks `content` (`src/blackrose/turns.py::TurnRunner._append`) | same |
| `ANSWERING_PLAIN` | E11 | stop not set, snapshot changed (`src/blackrose/turns.py::TurnRunner._checkpoint_content`) | `repo.checkpoint_content` guarded to `pending` rows with no audit (`src/blackrose/storage/repository.py::Repository.checkpoint_content`) | same |
| `ANSWERING_AUDITED` | E13 | row still `pending` (`src/blackrose/storage/repository.py::Repository.save_audit`) | Replaces `content` (hash-checked) and the report in one transaction (same method) | same |
| `ANSWERING_*` | E6 | `_answer` returned a tool-free visible answer | Audit runs, evidence deduped (`src/blackrose/turns.py::TurnRunner._evidence`), checkpoints drained (all in `src/blackrose/turns.py::TurnRunner._complete`) | `COMPLETED` (write in flight) |
| `COMPLETED`-in-flight | E6 | `finish_message` commits | Sets `completed` and `last_completed_turn` (`src/blackrose/turns.py::TurnRunner._complete`) | `COMPLETED` |
| `COMPLETED`-in-flight | E7 | Stop while the shielded write runs | Waits for the outcome without raising its failure (the `asyncio.wait` loop in `_complete`); on success sets both fields | `COMPLETED` → raise |
| `COMPLETED`-in-flight | E7 | Stop and the write failed | Logs, leaves `completed = None`, re-raises (`_complete`) | `COMPLETED`-in-flight → settling |
| `COMPLETED`-in-flight | E8 | the write raised | `shield` re-raises the storage error; `completed` stays `None` | settling |
| `ANSWERING_*` | E7 | — | `state = CANCELLED`, `error = 'Stopped'` (`src/blackrose/turns.py::TurnRunner._settle_interrupted`) | `ANSWERING_*` (settling) |
| `ANSWERING_*` | E8 | — | `state = ERROR`, `error = str(error) or type name` (`src/blackrose/turns.py::TurnRunner._settle_interrupted`) | `ANSWERING_*` (settling) |
| any with `A` | E7 (during the audit) | `audit.run` is inline, not shielded (`src/blackrose/turns.py::TurnRunner._complete`) | `audit.run` converts its own report to `incomplete` (the `CancelledError` handler in `src/blackrose/citations.py::CitationAudit.run`); settlement reverts `content` to `audit.draft_text` and writes an `unchecked_report` (`src/blackrose/turns.py::TurnRunner._save_interrupted`) | `ANSWERING_AUDITED` (settling) |
| settling | E9 | `assistant is not None ∧ completed is None` (`src/blackrose/turns.py::TurnRunner._run`) | Shields the terminal write (`src/blackrose/turns.py::TurnRunner._settle_interrupted`); `src/blackrose/turns.py::TurnRunner._save_interrupted` drains, finalises blocks, reverts an audited draft, retries without evidence | `SETTLED` after the `finally` |
| `COMPLETED` | E7 | `completed is not None` (`src/blackrose/turns.py::TurnRunner._run`) | Schedules the title/memory follow-ups for the stored reply (`src/blackrose/turns.py::TurnRunner._schedule_follow_ups`); no settlement | `COMPLETED` after the `finally` |
| settling | E9 (failed) | `cleanup.result()` raised (`_settle_interrupted`) | The retry without evidence (`_save_interrupted`) already failed, so the storage error propagates in place of the original Stop/failure; the row stays `pending` | `SETTLED` with a pending row |
| any | E14 | another send arrives | `ChatBusy`; the live turn's flags and rows are untouched | unchanged |
| `RESTING` (row pending) | E15 | process restarted with a pending row | `reap_pending` writes `ERROR` with the crash text, cancels streaming blocks, interrupts UI events (`src/blackrose/storage/repository.py::Repository.reap_pending`) | `ERROR` (durable) |

`E1`-with-lock and `E1`-without-lock both exist: `artifacts.exclusive` refuses
immediately instead of queueing (`src/blackrose/artifacts.py::ArtifactLifecycle.exclusive`), so the `_run` guard
is only reached when the lock is free but another reservation (model
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
   in one non-await window (`src/blackrose/turns.py::TurnRunner._run`), and the artifact lock is taken
   before it (`src/blackrose/turns.py::TurnRunner.run`). A second send cannot enter `_run`, cannot write the turn's
   rows, and cannot change `is_generating` or `last_request_stored`. It does
   clear `last_completed_turn` (`run`), which a running turn only sets in its
   final step before returning or raising. Pinned by
   `test_a_refused_second_send_does_not_disturb_the_live_turn`.
2. **Reply identity and `last_request_stored` are set atomically.** Both fields
   are assigned in the same statement pair with no `await` between them
   (`src/blackrose/turns.py::TurnRunner._begin_turn`); therefore `assistant is not None ⇒ last_request_stored` for the
   lifetime of a runner. Pinned by the matrix test (no observed row has
   `A ∧ ¬S`) and by `test_cancelling_the_commit_still_yields_the_stored_identity`.
3. **`completed` implies a committed `COMPLETE` row.** `completed` is assigned
   only from the result of a successful `finish_message` (`_complete`), whose
   transaction writes `state='complete'` and commits before returning
   (`src/blackrose/storage/repository.py::Repository.finish_message`); `Database.run` rolls back on any exception
   (`src/blackrose/storage/db.py::Database.run`). There is no reachable state with `completed` set and the row
   still `pending`. Pinned by
   `test_a_committed_final_save_is_never_followed_by_settlement`.
4. **Settlement never follows a committed write.** `_run` consults `completed`
   first and, when it is set, only schedules follow-ups (its `except BaseException` handler).
   `_settle_interrupted` has exactly one call site, in that handler. Pinned by
   `test_a_committed_final_save_is_never_followed_by_settlement` (a spy observes
   zero settlement calls).
5. **Reply services are mutually exclusive.** `_start_reply` takes exactly one
   branch. While answering, exactly one of `checkpoint_task` (live)
   and `citation_audit` is present; neither is present only before
   `_start_reply` or after it failed before the branch. Pinned by the matrix test.
6. **At most one terminal write per turn.** Exactly one of `_complete`'s shield
   result, `_save_interrupted`'s first attempt or its retry can apply, and the
   drain precedes all of them (`_stop_checkpoints` is called first on each path). Pinned by
   `test_checkpoint_drain_is_idempotent_across_complete_and_settlement`
   (three `finish_message` calls, one row, still `pending` because all were
   rejected).
7. **Checkpoint writes are ordered before the terminal write and are
   idempotent to drain.** `_stop_checkpoints` sets the flag and awaits the task
   (`src/blackrose/turns.py::TurnRunner._stop_checkpoints`); both terminal paths call it first, and the failure path calls it
   twice. Pinned by the same test (`order == ['checkpoint', 'finish', 'finish',
   'finish']`), `tests/test_reply_checkpoints.py::test_cancellation_drains_inflight_checkpoint_before_terminal_write`,
   and `tests/test_turns.py::TurnTests.test_stop_before_the_final_save_starts_settles_the_reply_as_cancelled`.
8. **A late checkpoint can never rewrite a terminal row.** Both pending writers
   require `state='pending'` (`Repository.checkpoint_content`, `Repository.save_audit`), and
   `finish_message` is the only terminal writer. Pinned by
   `tests/test_reply_checkpoints.py::test_checkpoint_guards_terminal_user_missing_and_audited_rows`.
9. **Every exit path releases the reservation and the outbound policy.**
   `finally: c.outbound = None; c.is_generating = False` (`src/blackrose/turns.py::TurnRunner._run`) runs for
   success, Stop and failure, including failures in `_prepare` and in
   settlement. Asserted in every terminal test.
10. **Follow-ups belong to stored replies only.** `_schedule_follow_ups` is
    called on the success path and on the committed-during-Stop path
    (both in `_run`), never from settlement. `test_cancelling_the_commit...`,
    `test_cancel_after_the_commit...` and
    `test_a_provider_failure_settles_error_and_releases_the_reservation` assert
    `schedule_title` is not called.
11. **A row left pending by a failed settlement is repaired at startup, once.**
    `reap_pending` filters on `state='pending'` (`src/blackrose/storage/repository.py::Repository.reap_pending`), so it
    is idempotent and never rewrites a terminal row. Pinned by SM-02's tests and
    the crash probe in `test_reply_checkpoints.py`.
12. **Stop cannot be prevented from settling by a second Stop.** `_settle_interrupted`
    waits on `shield(cleanup)` in a loop (`src/blackrose/turns.py::TurnRunner._settle_interrupted`), so the terminal write is
    never cancelled by the caller's cancellations. Pinned by
    `tests/test_turns.py::stop_during_final_save` (two cancels),
    `tests/test_reply_checkpoints.py::test_cancellation_drains_inflight_checkpoint_before_terminal_write`
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
  are assigned together (`_begin_turn`) and nothing else writes either.
- **`completed` set while the row is still `pending`?** No: `completed` is only
  assigned from a returned `finish_message`, which commits the terminal state in
  the same transaction.
- **Both `checkpoint_task` and `citation_audit` live?** Never (single `if/else`
  in `_start_reply`). **Neither?** Only before `_start_reply`, or if it failed before the
  branch; the resulting settlement is state 12 and writes an empty `CANCELLED`
  row.
- **Does `_settle_interrupted` ever run after `_complete` partially succeeded?**
  It runs only when `completed is None` (`_run`'s handler), and `completed` is set iff
  the write returned. `finish_message` is one transaction, so "partially
  succeeded" does not exist: it either commits all of
  content/state/error/audit/evidence/outbox or rolls back. The only theoretical
  exception is event-loop shutdown cancelling the `saving` task
  (in `_complete`), in which case the row is left pending and startup recovery decides.
- **What does the caller do with `last_completed_turn` when settlement failed?**
  `src/blackrose/chat.py::ChatController.send` ignores it. The GUI reads it once,
  in the `except CancelledError` handler of `src/blackrose/gui/session.py::Session._send`; when it is
  `None` Session renders a `cancelled` (or, for a storage error, `error`)
  outcome and `_interrupted_answer` re-reads the *latest assistant row* from the
  database (`src/blackrose/gui/session.py::Session._interrupted_answer`) — a row that is still `pending` with its last
  checkpoint — then `reap_pending` converts it to `ERROR` with the crash text on
  the next start.

| Shielded boundary | Site | What a Stop does |
| --- | --- | --- |
| `builder.build` in `_prepare` | `src/blackrose/turns.py::TurnRunner._prepare` (not shielded; `to_thread`) | Raises at once; the build thread's result is discarded; nothing stored; reservation released (`finally` of `_run`) |
| `_worker(_persist_attachments)` | `src/blackrose/turns.py::TurnRunner._begin_turn` (worker shielded, caller not) | `_worker` waits for the thread, then raises `CancelledError`; blobs exist, no rows; H4 |
| `asyncio.shield(repo.begin_turn)` | `src/blackrose/turns.py::TurnRunner._begin_turn` | The handler waits for the identity, latches `assistant` + `last_request_stored`, re-raises → `CANCELLED` row |
| `audit.inherit` / `audit.checkpoint('')` | `src/blackrose/turns.py::TurnRunner._start_reply` (not shielded) | Raises; the audit is already attached, so settlement writes an `unchecked_report` over `draft_text` (still `''`) |
| `asyncio.shield(checkpoint_task)` | `src/blackrose/turns.py::TurnRunner._stop_checkpoints` (single shot) | Raises out of `_complete`; the task keeps running (shield protects it) and settlement drains it again → H2 |
| `CitationAudit.run` in `_complete` | `src/blackrose/turns.py::TurnRunner._complete` (not shielded) | `run` converts its report to `incomplete` and re-raises; settlement reverts to `draft_text` and writes `CANCELLED` — pinned by `test_cancelling_the_audit_checkpoint_settles_an_unchecked_report` |
| `asyncio.shield(finish_message)` | `src/blackrose/turns.py::TurnRunner._complete` | Waits for the outcome; commits → `completed` + follow-ups; fails → logs and settles `CANCELLED` |
| `asyncio.shield(cleanup)` in `_settle_interrupted` | `src/blackrose/turns.py::TurnRunner._settle_interrupted` | Cannot cancel the terminal write; repeat Stops are absorbed |

- **H1 — `defect` — `src/blackrose/turns.py::TurnRunner.run` vs `src/blackrose/turns.py::TurnRunner._run`.** `run` clears
  `last_completed_turn` before its first await but resets `last_request_stored`
  only inside `_run`, after `ensure_conversation()` and the keyed-provider
  credential lookup (which awaits a thread for `opencode-go` models,
  `src/blackrose/backend/opencode.py::OpenCodeGoClient.turn_credentials`). A Stop in that window leaves the previous turn's
  `True` in place. The GUI then concludes this send stored its request
  (the `last_request_stored` checks in `Session._send` and `Session._interrupted_answer`): it neither restores the composer text nor
  records the unsaved outcome, and `_draft_sending` is consumed, so the typed
  text exists nowhere. Reproduced deterministically by
  `test_a_send_cancelled_before_admission_inherits_the_last_stored_flag_documented_follow_up`
  (the test asserts current behaviour; applying the patch in *Formalization
  decisions* inverts its one `assertTrue`). Confirmation: the test plus a GUI
  probe that presses Stop immediately after Enter with a keyed provider.
- **H2 — `fragile` — `src/blackrose/turns.py::TurnRunner._stop_checkpoints`.** `_stop_checkpoints` shields the drain
  once, not in a loop, unlike `_complete` and `_settle_interrupted`. A repeat Stop while it is awaiting raises out of `_complete`, so the
  drain is abandoned; correctness is restored only because settlement calls
  `_stop_checkpoints` again inside the shielded cleanup task, and because the
  repositories refuse to write a non-pending row. No user-visible failure was
  found; pinned by the existing two-Stop tests. A loop would make the guarantee
  local rather than emergent.
- **H3 — `documentation-gap` — `src/blackrose/turns.py::TurnRunner._settle_interrupted`, `src/blackrose/turns.py::TurnRunner._save_interrupted`.** When settlement
  itself fails, `cleanup.result()` replaces the original Stop (or provider error)
  with the storage error, so the caller reports `error`, not `cancelled`, and the
  row stays `pending` until startup recovery. This is deliberate ("Failure to
  settle the terminal row still propagates for startup recovery", in `_save_interrupted`) but
  the state change is not documented anywhere else. Demonstrated by
  `test_settlement_failure_masks_the_stop_with_the_storage_error` and, for the
  failure path, `tests/test_terminal_evidence.py::test_failure_to_settle_terminal_state_still_propagates`.
- **H4 — `documentation-gap` — `src/blackrose/turns.py::TurnRunner._begin_turn`.** Attachment blobs are persisted
  (and content-addressed on disk) before the turn row is created, so a Stop in
  that thread leaves a blob no message references. Artifact cleanup reconciles
  recorded obligations only, so nothing in the turn path reclaims it.
  Demonstrated by
  `test_cancel_while_persisting_attachments_leaves_no_turn_documented_follow_up`.
  Confirming the leak's size needs the orphan report from
  `ArtifactLifecycle` over a store that went through this path.
- **H5 — `fragile` — `src/blackrose/chat.py::ChatController.select_model`, `_review_edit`, `_restore_edit_backup`, `review_library_add`; `src/blackrose/diagram_repairs.py::DiagramRepairs.repair`.**
  `is_generating` is a shared reservation, so `is_generating == True` does not
  imply a turn is running, and a second send's refusal message depends on which
  holder is active: `artifacts.exclusive` ("Wait for the current reply or
  artifact cleanup to finish", `src/blackrose/artifacts.py::ArtifactLifecycle.exclusive`) when a turn or repair holds
  the lock, `_run`'s "A reply is already in progress" (`src/blackrose/turns.py::TurnRunner._run`) when
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

Why here and not beside `_run`'s reset: clearing the flag earlier without the guard would let
a `ChatBusy`-refused send erase the live turn's flag, turning H1's lost draft
into a restored draft on top of an already-stored request. The guard must move
with it. `ChatBusy` is already imported in `src/blackrose/turns.py`, and `_run`'s own check
stays as the admission point's guard — it is also the path taken when
`select_model`, an edit approval or a library approval holds the reservation.

Follow-up after the patch: invert the single `assertTrue(c.last_request_stored)`
in `test_a_send_cancelled_before_admission_inherits_the_last_stored_flag_documented_follow_up`
and drop the `_documented_follow_up` suffix.

Two smaller items are deliberately left alone:

- **H2** could be made local by looping the drain (`src/blackrose/turns.py::TurnRunner._stop_checkpoints`) exactly as
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

Offline, from the repository root:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_01_turn_orchestration.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
.venv/bin/python scripts/check_unit_tests.py -p test_terminal_evidence.py
```

`test_sm_01_turn_orchestration.py` pins the matrix, the guards and the hazards
above; it is deterministic and needs no provider, display or GPU.
`test_reply_checkpoints.py` includes the process-kill crash probe.

The machine is entirely in-process, so no `--gui` or live model probe is needed.
H1's user-visible consequence was reasoned from `src/blackrose/gui/session.py::Session._send`
and `src/blackrose/gui/session.py::Session._interrupted_answer` and reproduced at the controller level, not
through the native window; a GUI probe would need a keyed provider and a Stop
tapped within the credential lookup.
