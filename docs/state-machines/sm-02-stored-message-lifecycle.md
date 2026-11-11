# SM-02: Persisted stored-message lifecycle and usable history

## Scope and ownership

- `src/blackrose/storage/records.py`: `src/blackrose/storage/records.py::MessageState`, `StoredMessage.state`/
  `error` (fields of `src/blackrose/storage/records.py::StoredMessage`), `src/blackrose/storage/records.py::StoredMessage.is_usable_history`.
- `src/blackrose/storage/repository.py`: `begin_turn`, `add_message`,
  `checkpoint_content`, `finish_message`, `reap_pending`, `save_evidence` and
  `save_audit` on `src/blackrose/storage/repository.py::Repository`.
- `src/blackrose/storage/migrations.py::MIGRATIONS`: migration 1 `messages`, migration 5
  `message_audits`, migration 9 `memory_outbox`, migration 11
  indexes.
- Coupled tables: `message_attachments` and `evidence` (migration 1),
  `message_blocks`/`ui_state`/`ui_events` (migration 3).

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[ADR 0001](../adr/0001-desktop-ownership.md):

- Exactly one desktop process owns the chat database. All repository work is
  submitted through `Database.run`, which holds an `asyncio.Lock` around a
  single worker thread and rolls back a failed callback
  (`src/blackrose/storage/db.py::Database.run`). Rows are therefore mutated in submission order;
  there is no cross-process writer while the desktop is running.
- `TurnRunner` (application asyncio loop) owns turn settlement: it holds
  `self.completed` as the local proof that the complete write landed
  (`src/blackrose/turns.py::TurnRunner.__init__`, `src/blackrose/turns.py::TurnRunner._run`) and shields the final write from cancellation
  (`src/blackrose/turns.py::TurnRunner._complete`).
- `Session` owns startup recovery ordering: it awaits
  `controller.recover_interrupted()` before publishing the transcript
  (`src/blackrose/gui/session.py::Session.boot`), which calls `Repository.reap_pending()`
  (`src/blackrose/chat.py::ChatController.recover_interrupted`).
- The repository stores state; it does not decide policy. `prompts.py` and
  `citations.py` decide what a stored state *means* for model input.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `messages.state` | migration 1; `src/blackrose/storage/records.py::StoredMessage` | `TEXT CHECK(state IN ('pending','complete','cancelled','error'))` / `MessageState` | Lifecycle position of one row | inserts `src/blackrose/storage/repository.py::Repository.begin_turn`, `src/blackrose/storage/repository.py::Repository.add_message`; terminal `src/blackrose/storage/repository.py::Repository.finish_message`; recovery `src/blackrose/storage/repository.py::Repository.reap_pending` |
| `messages.error` | migration 1; `src/blackrose/storage/records.py::StoredMessage` | `TEXT NULL` | Human-readable stop/failure text for the transcript | same two statements (the `UPDATE` in `finish_message` and in `reap_pending`) |
| `messages.role` | migration 1 | `TEXT CHECK(role IN ('system','user','assistant'))` | Author of the row | inserts only |
| `messages.content` | migration 1 | `TEXT NOT NULL DEFAULT ''` | Checkpointed prefix, then final text | `checkpoint_content`; `finish_message`; `save_audit` |
| `messages.reply_to` | migration 1 | `INTEGER NULL REFERENCES messages(id)` | Assistant row → its request | `begin_turn` (the reply insert) |
| `messages.refs` | migration 2 | JSON text | Selected file/skill pointers | `begin_turn` (the request insert) |
| `message_audits.report` | migration 5 | JSON text (`AnswerAudit`) | Citation audit checkpoint (`status='pending'`) or final report | `save_audit`; `finish_message`; `reap_pending` |
| `evidence.sources` | migration 1 | JSON text | Captured retrieval sources for a reply | `finish_message`; `src/blackrose/storage/repository.py::Repository.save_evidence` |
| `memory_outbox` row (`kind='turn'`) | migration 9 | Row presence | Durable duty to send a completed turn to Honcho | `finish_message` only |
| `conversations.memory_used` | migration 9 | `INTEGER` | Chat may have a Honcho session to delete | `finish_message` (with the outbox insert) |
| `message_blocks.payload.status` | migration 3 | JSON field | `streaming` → `ready`/`invalid`/`cancelled` | `src/blackrose/storage/repository.py::Repository.save_blocks`; `reap_pending` |
| `ui_events.status` | migration 3 | `TEXT` | `accepted` → `started` → `completed`/`interrupted` | `begin_turn`; `src/blackrose/storage/repository.py::Repository.finish_ui_event`; `reap_pending` |
| `StoredMessage.is_usable_history` | `src/blackrose/storage/records.py::StoredMessage.is_usable_history` | Derived `bool` | Whether this row may be replayed to a model | Computed on read; never persisted |

`src/blackrose/storage/records.py::MessageState.is_finished` is a convenience predicate with no
callers in `src/`, `tests/` or `scripts/`; it is not part of the runtime contract.

## States

The lifecycle has four mutually exclusive states. `state` is the only persisted
discriminator; `error` and the coupled tables are consequences, not inputs.

| State | Predicate | Content/error shape in product paths |
| --- | --- | --- |
| `PENDING` | `state='pending'` | `content` is the last checkpoint (may be `''`); `error IS NULL`; an audit row may exist with `status` `pending`/`incomplete`; blocks may be `streaming` |
| `COMPLETE` | `state='complete'` | `error IS NULL`; final `content`, final audit (if citation rigor ran) and evidence written in the settling transaction |
| `CANCELLED` | `state='cancelled'` | `error` is the stop reason (`TurnRunner` writes `'Stopped'`, `src/blackrose/turns.py::TurnRunner._settle_interrupted`); `content` is the partial reply; blocks are finalised as `cancelled` before settlement (`src/blackrose/turns.py::TurnRunner._save_interrupted`) |
| `ERROR` | `state='error'` | `error` is the exception class/message, or the fixed crash string `"interrupted: Blackrose exited during generation"` (`src/blackrose/storage/repository.py::Repository.reap_pending`) |

Derived projections (not states of the row, but of what the rest of the system does
with it):

| Projection | Predicate | Consumers |
| --- | --- | --- |
| `USABLE_HISTORY` | `is_usable_history` | prompt construction (`src/blackrose/prompts.py::PromptBuilder.build`), image-capability gate (`src/blackrose/turns.py::TurnRunner._prepare`), citation inheritance (`src/blackrose/citations.py::CitationAudit.inherit`) |
| `NOT_USABLE_HISTORY` | negation of the above | same |
| `DISPLAY_STOPPED` | `state is CANCELLED` (`src/blackrose/gui/render.py::message_payload`) | transcript payload; `error` is shown only as text (same function) |
| `DISPLAY_FAILED` | `state is ERROR` and/or non-null `error` | transcript payload |

`is_usable_history` in full (`src/blackrose/storage/records.py::StoredMessage.is_usable_history`):

- `role='user'` → `content.strip()` or attachments. State is *not* consulted.
- assistant with `PENDING` or `ERROR` → `False`.
- assistant with `COMPLETE` or `CANCELLED` → `content.strip()` or attachments or blocks.

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Start a turn | `src/blackrose/turns.py::TurnRunner._begin_turn` → `src/blackrose/storage/repository.py::Repository.begin_turn` |
| E2 | Persist an app-created row | `src/blackrose/storage/repository.py::Repository.add_message`, used by scripts/probes only |
| E3 | Content checkpoint tick | `src/blackrose/turns.py::TurnRunner._checkpoint_content` (started in `src/blackrose/turns.py::TurnRunner._start_reply`) → `checkpoint_content` |
| E4 | Audit checkpoint | `src/blackrose/citations.py::CitationAudit.checkpoint` → `save_audit`; called before streaming (`src/blackrose/turns.py::TurnRunner._start_reply`) and on each audit phase |
| E5 | Generation finished successfully | `src/blackrose/turns.py::TurnRunner._complete` → `finish_message(COMPLETE)` |
| E6 | User Stop | `asyncio.CancelledError` in `src/blackrose/turns.py::TurnRunner._run` → `_settle_interrupted` → `finish_message(CANCELLED)` (`src/blackrose/turns.py::TurnRunner._save_interrupted`) |
| E7 | Generation failure | any other exception in `src/blackrose/turns.py::TurnRunner._run` → `finish_message(ERROR)` |
| E8 | Settlement retry | `_save_interrupted` retries once without evidence after a failed atomic write (`src/blackrose/turns.py::TurnRunner._save_interrupted`) |
| E9 | Startup recovery | `Session.boot` → `ChatController.recover_interrupted` → `reap_pending` (`src/blackrose/gui/session.py::Session.boot`, `src/blackrose/chat.py::ChatController.recover_interrupted`) |
| E10 | Conversation deletion | `ON DELETE CASCADE` from `messages` (the foreign keys in migrations 1, 3 and 5 of `src/blackrose/storage/migrations.py::MIGRATIONS`) |
| E11 | Read for transcript / history | `src/blackrose/storage/repository.py::Repository.messages`/`src/blackrose/storage/repository.py::Repository.message_page` → `_load_messages` → `_attach_all` |

No other statement writes `messages.state`: a source-wide search finds only
the inserts in `begin_turn` and `add_message` and the updates in `finish_message`
and `reap_pending`.

## Transition table

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| — | E1 | `begin_turn` runs on an existing conversation; `ui_event_id` (if any) must still be `accepted` and is consumed with `rowcount==1` (`src/blackrose/storage/repository.py::Repository.begin_turn`) | Insert request row `COMPLETE`, insert reply row `PENDING` with `content=''`, `reply_to=user.id`, bump `conversations.updated_at`; all in one transaction | request `COMPLETE`, reply `PENDING` |
| — | E2 | none (`add_message`) | Insert row at the caller's `state`, default `COMPLETE` (`src/blackrose/storage/repository.py::Repository.add_message`, `src/blackrose/storage/records.py::StoredMessage`) | any of the four |
| `PENDING` | E3 | assistant, `state='pending'`, no `message_audits` row (`src/blackrose/storage/repository.py::Repository.checkpoint_content`); statement updates 1 row | Replace `content` only; returns `True`/`False` | `PENDING` |
| `PENDING` | E4 | assistant, `state='pending'` (`src/blackrose/storage/repository.py::Repository.save_audit`); optional body must hash-match the report | Replace `content` (when given) and `message_audits.report` in one transaction | `PENDING` |
| `PENDING` | E5 | audit, when supplied, must hash-match `content` (`src/blackrose/storage/repository.py::Repository.finish_message`) | Insert/replace audit, write `content/state/error`, bump `updated_at`, replace evidence, queue `memory_outbox('turn')` when `remember` and the chat is not excluded (in that order, one transaction) | `COMPLETE` |
| `PENDING` | E6 | `TurnRunner.completed is None` (`src/blackrose/turns.py::TurnRunner._run`) | Same write as E5 with `state='cancelled'`, `error='Stopped'`; no outbox row | `CANCELLED` |
| `PENDING` | E7 | same | Same write with `state='error'`, `error` = exception text | `ERROR` |
| `PENDING` | E8 | first attempt raised; row is still `pending` | Repeat E6/E7 without `evidence` so the original reason is retained (`src/blackrose/turns.py::TurnRunner._save_interrupted`) | `CANCELLED`/`ERROR` |
| `PENDING` | E9 | row is `pending` at startup (`src/blackrose/storage/repository.py::Repository.reap_pending`) | Rewrite any pending audit as `incomplete` with recomputed `body_hash`, cancel every `streaming` block, interrupt `accepted`/`started` events, write `ERROR` + fixed crash text; returns `rowcount` | `ERROR` |
| `COMPLETE` | E9 | filtered out by `WHERE state='pending'` | none; `reap_pending` returns 0 | `COMPLETE` |
| `CANCELLED`/`ERROR` | E9 | same | none | unchanged |
| `COMPLETE`/`CANCELLED`/`ERROR` | E3 | `state='pending'` in the guard | 0 rows; returns `False` | unchanged |
| `COMPLETE`/`CANCELLED`/`ERROR` | E4 | `state='pending'` check raises | `ValueError('Audit checkpoints require a pending assistant message')` (`save_audit`) | unchanged |
| `COMPLETE`/`CANCELLED`/`ERROR` | E5/E6/E7 | **no guard** (`finish_message`) | Overwrites `content/state/error` (and audit/evidence/outbox) of a settled row | any terminal state |
| `COMPLETE`/`CANCELLED`/`ERROR` | E2 | none | A second, independent row | — |
| any | E10 | conversation deleted | cascades the row, its attachments, evidence, audit and blocks | row gone |

The only *refused* transitions are E3 and E4 from a terminal state; E5–E8 are
unguarded at the repository boundary (hazard H1).

## Invariants

1. **Atomic turn creation.** A turn is either one `COMPLETE` request plus one
   `PENDING` reply linked by `reply_to`, or nothing. Enforced by one transaction
   in `src/blackrose/storage/repository.py::Repository.begin_turn`; pinned by
   `test_begin_turn_commits_request_attachments_and_pending_reply` and
   `test_begin_turn_rolls_back_request_if_pending_reply_fails`
   (`tests/test_storage.py`), and by
   `test_begin_turn_creates_one_complete_request_and_one_pending_reply`.
2. **Closed state vocabulary.** `messages.state` is one of `pending`, `complete`,
   `cancelled`, `error`, and `role` one of `system`, `user`, `assistant`
   (migration 1). Pinned by
   `test_schema_accepts_only_the_four_lifecycle_states_and_three_roles`.
3. **Requests are terminal at birth.** Application code creates `user` rows with
   `COMPLETE` only (`begin_turn`), and no product writer ever moves a
   user row (only the unguarded `finish_message` of H1 can).
4. **Pending content is written only by the two guarded checkpoints.** While
   `state='pending'`, `content` changes only through `checkpoint_content`
   or `save_audit`, and `checkpoint_content` refuses once
   an audit exists. Pinned by
   `test_checkpoint_content_only_writes_pending_unaudited_assistant_text` and
   `test_checkpoint_guards_terminal_user_missing_and_audited_rows`.
5. **Terminal writes are all-or-nothing.** `finish_message` resolves content,
   state, error, audit, evidence and the `memory_outbox` row in one transaction;
   a mismatched audit hash writes nothing. Pinned by
   `test_finish_message_rejects_an_audit_that_does_not_match_the_answer` and
   `test_final_answer_and_audit_roll_back_together` (`tests/test_citations.py`).
6. **Memory duty follows success only.** A `memory_outbox(kind='turn')` row exists
   only for a `COMPLETE` turn in a chat with `memory_excluded=0`
   (`finish_message`). Pinned by
   `test_memory_outbox_is_queued_only_for_a_complete_remembered_turn` and
   `test_failed_replies_and_form_submissions_are_not_sent` (`tests/test_memory.py`).
7. **No pending row survives startup recovery.** After `reap_pending` returns, no
   `messages` row is `pending`, and a second call returns 0. Pinned by
   `test_reap_pending_is_idempotent_and_never_rewrites_terminal_rows` and
   `test_reaping_an_audited_pending_row_is_stable_across_reopen`, plus the
   existing crash probe in `tests/test_reply_checkpoints.py`.
8. **Recovery never invents or erases content.** `reap_pending` keeps the last
   checkpointed `content` and the audit's captured `sources`, only rewriting the
   report `status`/`body_hash`/claims (`src/blackrose/citations.py::unchecked_report`). Pinned by
   `test_reap_pending_settles_the_message_its_audit_blocks_and_events` and
   `test_crash_reaps_pending_audit_and_preserves_captured_text`
   (`tests/test_citations.py`).
9. **Recovery never rewrites a terminal row.** The message statement filters on
   `state='pending'` (`reap_pending`); audits and events are only
   rewritten for pending/in-flight rows, and block rewriting is limited to
   `status='streaming'`. Pinned by
   `test_reap_pending_is_idempotent_and_never_rewrites_terminal_rows`.
10. **In-flight and failed replies are not history.** `is_usable_history` is
    `False` for assistant `PENDING` and `ERROR` rows; `COMPLETE`/`CANCELLED`
    rows are usable iff they carry text, attachments or blocks. Pinned by
    `test_usable_history_truth_table`.
11. **History assembly consumes exactly the usable rows.** `PromptBuilder.build`
    maps `history` through `is_usable_history` (`src/blackrose/prompts.py::PromptBuilder.build`), and
    `wire_message` serialises blocks in place of `content` when blocks exist
    (`src/blackrose/prompts.py::wire_message`), so a block-only cancelled reply contributes its block
    sources. Pinned by `test_history_wire_reads_what_usable_history_admits`.
12. **Terminal immutability is a caller invariant, not a repository invariant.**
    `TurnRunner.completed` (`src/blackrose/turns.py::TurnRunner._run`) and the `pending` filter in
    `reap_pending` are the only things preventing a second settlement. Not
    enforced by `finish_message`; see H1.

## Hazards and open questions

- **H1 — `fragile` — `src/blackrose/storage/repository.py::Repository.finish_message`.** `finish_message` accepts any
  target id and any state, with no role check and no `state='pending'` guard. A
  second call rewrites a settled row; on a `user` row it rewrites content too.
  Demonstrated by
  `test_finish_message_can_rewrite_a_terminal_row_documented_follow_up`. No
  product path reaches it: `TurnRunner` settles only while `completed` is unset,
  the final write is shielded, and `reap_pending` filters on `pending`. But
  `tests/test_diagram_repairs.py::DiagramRepairTests.test_original_citation_controls_survive_diagram_revision` already relies on re-settling a row that
  `session.submit` left `COMPLETE` (verified: state after submit is `complete`),
  so a strict guard changes existing behavior. Confirm with a grep for a second
  `finish_message` on one id in product code (none today) and with a trigger that
  logs duplicate settlements.
- **H2 — `documentation-gap` — `src/blackrose/storage/repository.py::Repository.finish_message`, migration 1 of `src/blackrose/storage/migrations.py::MIGRATIONS`.**
  Nothing couples `error` to `state`: `COMPLETE` with error text and `ERROR`
  with `NULL` both persist. Only `TurnRunner` discipline keeps `COMPLETE`
  error-free and failures named (`src/blackrose/turns.py::TurnRunner._settle_interrupted`, `src/blackrose/turns.py::TurnRunner._save_interrupted`). Demonstrated by
  `test_finish_message_does_not_couple_error_text_to_state_documented_follow_up`.
  Display is driven by `state` and `error` independently (`src/blackrose/gui/render.py::message_payload`),
  so a schema/API coupling would be a user-visible change, not just a cleanup.
- **H3 — `fragile` — `src/blackrose/storage/repository.py::Repository.save_evidence`.** `save_evidence` writes evidence
  for any message id with no role or state check, unlike its siblings. It has no
  callers in `src/` (only tests). Not a corruption risk today: evidence on an
  unusable row is only read by `src/blackrose/turns.py::TurnRunner._private_context` and
  `src/blackrose/citations.py::CitationAudit.inherit` (which filters on `is_usable_history`). Confirm with a
  grep in future code; either delete the method or give it the same
  pending-assistant guard as `save_audit`.
- **H4 — `none-found` — `src/blackrose/storage/repository.py::Repository.reap_pending`.** Recovery covers every table
  that can reference a pending message: `message_audits`, `message_blocks`,
  `ui_events`, and `messages`. `memory_outbox` cannot reference a pending row
  (written only on `COMPLETE`). `evidence` can (only via H3) and is left in
  place; `reap_pending` marks the audit `incomplete` but keeps its captured
  `sources` (`src/blackrose/citations.py::unchecked_report`). `message_attachments` and `ui_state` are
  keyed by rows that survive; nothing dangles.
- **H5 — `fragile` — `src/blackrose/storage/records.py::StoredMessage.is_usable_history`.** `is_usable_history` ignores `refs`
  and `ui_context`, which `wire_message` does emit (`src/blackrose/prompts.py::wire_message`). A
  user row with references but no text and no attachments is therefore dropped
  from later history even though it was sent with the turn. Unreachable through
  the GUI, which refuses an empty send with no attachments
  (`src/blackrose/gui/session.py::Session.submit`), but reachable through `ChatController.send('', refs=...)`.
  Confirm by sending an empty prompt with one file reference and inspecting the
  next prompt's history.
- **H6 — `documentation-gap` — migration 1 of `src/blackrose/storage/migrations.py::MIGRATIONS`.** The schema permits
  role/state combinations the application never writes (`user`/`pending`,
  `system`/`error`) and has no trigger tying state to role. `add_message`
  (`src/blackrose/storage/repository.py::Repository.add_message`) is the only API that can create them, and no product
  caller passes a non-default state. Consider a partial index or trigger if a
  future feature needs `pending` user rows.
- **H7 — `fragile` — `src/blackrose/storage/repository.py::Repository.reap_pending`.** `reap_pending` assumes the
  single-owner database (ADR 0001). Any second writer running during generation
  would convert a live turn to `ERROR` with the crash text. There is no lock
  file, no ownership check and no "started at" column to tell a live turn from a
  dead one. The existing crash probe (`tests/test_reply_checkpoints.py`) kills
  the process before reopening, so it does not test the concurrent case.

## Formalization decisions

**No source change.** H1 is real but the smallest correct fix is not local: a
`state='pending'`/role guard changes behavior that `tests/test_diagram_repairs.py`
depends on for fixture setup. Proposed patch, for the owner of `repository.py`:

```python
# Repository.finish_message, before the audit insert
changed = conn.execute(
    "UPDATE messages SET content = ?, state = ?, error = ? "
    "WHERE id = ? AND role = 'assistant' AND state = 'pending'",
    (content, state.value, error, message_id),
)
if changed.rowcount != 1:
    raise ValueError('Only a pending assistant message can be resolved')
```

and move the audit/evidence/outbox writes after it so a refused settle writes
nothing. Follow-ups this requires: rewrite
`tests/test_diagram_repairs.py::DiagramRepairTests.test_original_citation_controls_survive_diagram_revision`
to seed its audit through `save_audit` before settlement (or an explicit
`add_message` fixture) instead of re-finishing a completed reply; keep the two
`documented_follow_up` tests in `tests/test_sm_02_stored_message_lifecycle.py`
and invert them. For H2, if enforcement is wanted, add
`CHECK((state='error') OR error IS NULL)`-style coupling only together with a
UI decision, since `CANCELLED` currently carries `'Stopped'` as visible text.
For H3, delete `save_evidence` (no `src/` callers) or guard it; both are
independent of this specification.

H4 needed no change: recovery is already complete for every table that can
reference a pending message, and the retained evidence is intentional.

## Verification

Offline, from the repository root:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_02_stored_message_lifecycle.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
.venv/bin/python scripts/check_unit_tests.py -p test_citations.py
.venv/bin/python scripts/check_unit_tests.py -p test_memory.py
.venv/bin/python scripts/check_unit_tests.py -p test_terminal_evidence.py
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
.venv/bin/python scripts/check_unit_tests.py -p test_diagram_repairs.py
.venv/bin/python scripts/check_unit_tests.py -p test_prompts.py
```

`test_reply_checkpoints.py` includes the process-kill crash probe. The
interactive-answer suites (`test_session_lifecycle.py`, `test_ui_documents.py`)
need the generated `ui-runtime` contract from [checkout
setup](../../CONTRIBUTING.md#set-up-a-checkout).

Unverified: H5's refs-only send was reasoned from `src/blackrose/gui/session.py::Session.submit` and
`src/blackrose/prompts.py::PromptBuilder.build`, not executed; confirming it needs one empty-text, one-ref
turn through `ChatController.send`. H7's concurrent-writer case was not
exercised (doing so requires a second process with a live turn).
