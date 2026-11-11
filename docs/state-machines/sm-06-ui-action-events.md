# SM-06: Interactive UI action submission and the `ui_events` in-flight lock

## Scope and ownership

- `src/blackrose/interactive_answers.py`: `AnswerRepository` protocol,
  `InteractiveAnswers.owned_block`, `save_state`, `accept_action`, `finish`.
- `src/blackrose/storage/repository.py`: `Repository.begin_turn`,
  `reap_pending`, `accept_ui_event`, `ui_event_context`, `finish_ui_event`; the
  read that attaches `ui_context` to a stored request (`src/blackrose/storage/repository.py::_attach_batch`,
  reached through `_attach_all`).
- `src/blackrose/storage/migrations.py::MIGRATIONS`: migration 3, which creates the `ui_events` table and the partial unique
  index `idx_ui_inflight`.
- `src/blackrose/gui/session.py`: `Session._ui_payload`, `_check_ui_view`,
  `_owned_ui`, `ui_state`, `ui_action`, `_busy`, `stop`, `close`, `boot`,
  `_send` (settlement in its `finally`).
- Coupled: `src/blackrose/ui_documents.py::continuation` and `src/blackrose/ui_documents.py::event_id`,
  `src/blackrose/turns.py::TurnRunner._prepare` and `src/blackrose/turns.py::TurnRunner._begin_turn`,
  `src/blackrose/chat.py::ChatController.recover_interrupted`,
  `src/blackrose/artifacts.py::settle`, `src/blackrose/storage/db.py::Database.run`
  and `src/blackrose/storage/db.py::_worker`.

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[ADR 0003](../adr/0003-interactive-answer-ownership.md):

- The **page** owns nothing durable. It echoes an `event_id` and the session's
  view token; `UIEnvelope.parse` and `Session._check_ui_view` reject anything
  else.
- **`Session`** owns the ephemeral part: `_ui_views` tokens, the
  `_ui_action_pending` interlock, the single `_generation` task, and the
  after-acceptance re-check of its own view in `Session.ui_action`.
- **`InteractiveAnswers`** owns the persisted policy gate (conversation, block
  kind/revision, runtime availability, `status='ready'`, protocol/library/schema
  hashes; `InteractiveAnswers.owned_block`) and validated acceptance/settlement
  (`accept_action`, `finish`).
- **`Repository`** owns atomicity. All writes go through `Database.run`, which
  holds one `asyncio.Lock` around one worker thread
  (`src/blackrose/storage/db.py::Database.run`) and rolls back a failed callback;
  `src/blackrose/storage/db.py::_worker` waits for the thread even after the
  caller is cancelled. One desktop owns the database (ADR 0001), so there is
  exactly one writer process.
- **`TurnRunner`** owns the turn: it reads the event's context while it is still
  `accepted` (`src/blackrose/turns.py::TurnRunner._prepare`), consumes it inside the same transaction that
  inserts the user request and pending reply (`src/blackrose/turns.py::TurnRunner._begin_turn`), and
  `Session._send` settles it in its `finally`.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `ui_events.event_id` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 3) | `TEXT PRIMARY KEY` | Submission identity; validated by `src/blackrose/ui_documents.py::event_id` | the `accept_ui_event` insert only (`src/blackrose/storage/repository.py::Repository.accept_ui_event`) |
| `ui_events.block_id` | `src/blackrose/storage/migrations.py::MIGRATIONS` | `TEXT NOT NULL REFERENCES message_blocks(block_id) ON DELETE CASCADE` | The form/comparison block that offered the action | same insert |
| `ui_events.action_id` | `src/blackrose/storage/migrations.py::MIGRATIONS` | `TEXT NOT NULL` | Second column of `idx_ui_inflight`; the only writer passes the constant `'submit'` (`persist` inside `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action`, required again by `src/blackrose/ui_documents.py::continuation`) | same insert |
| `ui_events.status` | `src/blackrose/storage/migrations.py::MIGRATIONS` | `TEXT NOT NULL DEFAULT 'accepted'`, **no CHECK** | The discriminator | default on insert (`Repository.accept_ui_event`); `Repository.begin_turn`; `Repository.finish_ui_event`; `Repository.reap_pending` |
| `ui_events.user_message_id` | `src/blackrose/storage/migrations.py::MIGRATIONS` | `INTEGER REFERENCES messages(id) ON DELETE SET NULL` | The visible request the action produced | `Repository.begin_turn` and `finish_ui_event`'s `COALESCE(?, user_message_id)`; no caller passes the optional argument |
| `ui_events.payload` | `src/blackrose/storage/migrations.py::MIGRATIONS` | `TEXT NOT NULL` (JSON) | The validated submission values | same insert |
| `ui_events.created_at` | `src/blackrose/storage/migrations.py::MIGRATIONS` | `TEXT NOT NULL` | Ordering only; nothing reads it | same insert |
| `idx_ui_inflight` membership | `src/blackrose/storage/migrations.py::MIGRATIONS` | Partial unique index on `(block_id, action_id) WHERE status IN ('accepted','started')` | The lock that makes a block's submission exclusive | SQLite, on every status write |
| `Session._ui_views[block_id]` | `src/blackrose/gui/session.py::Session._ui_payload` | `dict[str, str]` (hex token) | View identity the page must echo | `_ui_payload` `setdefault`; cleared by `Session._redraw` |
| `Session._ui_action_pending` | `src/blackrose/gui/session.py::Session.ui_action` | `bool` | Admission interlock covering acceptance through task creation | `ui_action` |
| `Session._generation` | `src/blackrose/gui/session.py::Session.ui_action` | `asyncio.Task \| None` | The turn that owns a `started` event | `ui_action`, `submit`, `retry`, repair paths; cleared when the owning task ends |
| `config.interactive_ui_actions_enabled` | `config.py`; read in `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action` and `src/blackrose/gui/session.py::Session._ui_payload` | `bool` | Policy gate for offering and accepting actions | preferences |
| `message_blocks.payload.status` | `src/blackrose/storage/migrations.py::MIGRATIONS`; read in `src/blackrose/interactive_answers.py::InteractiveAnswers.owned_block` | JSON field | `'ready'` is required before an action may be accepted | `Repository.save_blocks`, `Repository.reap_pending` |

## States

`status` is the only durable discriminator; `user_message_id` and index
membership are consequences.

| State | Predicate |
| --- | --- |
| `ABSENT` | no row with this `event_id` |
| `ACCEPTED` | `status='accepted'` (and `user_message_id IS NULL`) |
| `STARTED` | `status='started'` (and `user_message_id IS NOT NULL`) |
| `COMPLETE` | `status='complete'` |
| `CANCELLED` | `status='cancelled'` |
| `ERROR` | `status='error'` |
| `INTERRUPTED` | `status='interrupted'` |
| `OUT_OF_VOCABULARY` | any other string (reachable only through `Repository.finish_ui_event`; H2) |

Derived projections:

| Projection | Predicate | Consumers |
| --- | --- | --- |
| `LOCKED` | `status IN ('accepted','started')` | `idx_ui_inflight` (`src/blackrose/storage/migrations.py::MIGRATIONS`), `Repository.reap_pending` |
| `SETTLED` | `status` is one of the six above and not `LOCKED` | `Repository.ui_event_context` refuses it; `reap_pending` skips it |
| `BLOCK_FREE` | no `LOCKED` row with `(block_id, 'submit')` | the next `accept_ui_event` insert |
| `CONTEXT_READABLE` | `status='accepted'` and the block's message belongs to the conversation | `src/blackrose/storage/repository.py::Repository.ui_event_context`, read by `src/blackrose/turns.py::TurnRunner._prepare` |
| `USER_TURN_LINKED` | `user_message_id IS NOT NULL` | `_attach_all` attaches `ui_context` to that stored request (`src/blackrose/storage/repository.py::_attach_batch`) |

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Accept a submission | `src/blackrose/gui/session.py::Session.ui_action` → `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action` → `src/blackrose/storage/repository.py::Repository.accept_ui_event` |
| E2 | Persistence cancelled during acceptance | `asyncio.CancelledError` around `settle(persist())` in `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action` |
| E3 | Session state changed after acceptance | second `_check_ui_view` and the busy/quit/permission re-check in `src/blackrose/gui/session.py::Session.ui_action` |
| E4 | Turn preparation and context read | `src/blackrose/turns.py::TurnRunner._prepare` (its `ui_event_context` read) |
| E5 | Consume the event into a turn | `src/blackrose/turns.py::TurnRunner._begin_turn` → `src/blackrose/storage/repository.py::Repository.begin_turn` |
| E6 | Turn completes | `_send`'s `end_stored` → its `finally` (`src/blackrose/gui/session.py::Session._send`) |
| E7 | Turn cancelled | `_send`'s `CancelledError` branch → `finally`; `src/blackrose/gui/session.py::Session.stop` is the only in-session canceller |
| E8 | Turn fails before or during generation | `_send`'s `Exception` branch → `finally` |
| E9 | Startup recovery | `src/blackrose/gui/session.py::Session.boot` → `src/blackrose/chat.py::ChatController.recover_interrupted` → `src/blackrose/storage/repository.py::Repository.reap_pending` |
| E10 | Conversation deleted | `message_blocks` cascade → `ui_events.block_id` `ON DELETE CASCADE` (`src/blackrose/storage/migrations.py::MIGRATIONS`) |
| E11 | Read `ui_events` for display | `src/blackrose/storage/repository.py::_attach_batch` (through `_attach_all`), joining on `user_message_id` |

No other statement writes `status`: a source-wide search finds only the insert in
`Repository.accept_ui_event`, the consuming `UPDATE` in `Repository.begin_turn`,
the `UPDATE` in `Repository.finish_ui_event` and the reap in
`Repository.reap_pending`.

## Transition table

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | E1 | conversation, block kind, `source_revision`, `status='ready'`, protocol/library/schema hashes (`src/blackrose/interactive_answers.py::InteractiveAnswers.owned_block`); then, in `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action`: actions enabled, `action_type='continue_conversation'`, event identity regex (`src/blackrose/ui_documents.py::event_id`); `submit` declared and values valid (`src/blackrose/ui_documents.py::continuation`) | `INSERT` with the schema default status and `created_at` (`src/blackrose/storage/repository.py::Repository.accept_ui_event`) | `ACCEPTED` |
| `ABSENT` | E1 | any guard above fails | `ValueError` before the insert; no row | `ABSENT` |
| `ABSENT` | E1 | `idx_ui_inflight` already holds `(block_id,'submit')` (`src/blackrose/storage/migrations.py::MIGRATIONS`) | `IntegrityError` → `ValueError('Duplicate or in-flight UI submission')` (`src/blackrose/storage/repository.py::Repository.accept_ui_event`); write rolled back | unchanged (self-transition) |
| `ABSENT` | E1 | `event_id` already exists (primary key) | same refusal as above; a retry needs a new identity | unchanged |
| `ACCEPTED` | E2 | the insert completed before the cancellation was observed (`src/blackrose/artifacts.py::settle` waits for the task; the `if accepted:` branch of `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action`) | `finish(event,'interrupted')` under `settle`, then `CancelledError` is re-raised | `INTERRUPTED` |
| `ACCEPTED` | E2 | the insert never completed | `accepted` stays `False`; nothing is settled | `ABSENT` |
| `ACCEPTED` | E3 | view token rotated, `_quit_requested`, `_permission_pending`, `is_generating`, or a live `_generation` (the re-check in `src/blackrose/gui/session.py::Session.ui_action`) | `finish(event,'interrupted')` and the refusal is returned to the page; no task is created | `INTERRUPTED` |
| `ACCEPTED` | E5 | `ui_event_context` was readable (`src/blackrose/turns.py::TurnRunner._prepare`) and the row is still `accepted`; `rowcount == 1` | One transaction: insert `COMPLETE` user request + `PENDING` reply, set `user_message_id` and `status='started'`, bump `conversations.updated_at` (`_begin` inside `src/blackrose/storage/repository.py::Repository.begin_turn`) | `STARTED` |
| `ACCEPTED` | E5 | the row is no longer `accepted` (`rowcount != 1`) | `ValueError('UI event was already consumed')` (`src/blackrose/storage/repository.py::Repository.begin_turn`); the whole transaction rolls back, including the two message inserts | `ACCEPTED` |
| `ACCEPTED` | E6/E8 | the turn failed before `E5` (prepare/admission/provider error) | `_send`'s `finally` calls `finish(event,'error')` | `ERROR` |
| `ACCEPTED` | E7 | the turn was cancelled before `E5` | `_send`'s `finally` calls `finish(event,'cancelled')`; the draft is restored in the `CancelledError` branch | `CANCELLED` |
| `ACCEPTED` | E9 | row is `accepted` at startup | `UPDATE ... WHERE status IN ('accepted','started')` (`src/blackrose/storage/repository.py::Repository.reap_pending`) | `INTERRUPTED` |
| `STARTED` | E6 | the stored reply settled (`end_stored`) | `finish(event, <message state>)` → `complete` in `_send`'s `finally` | `COMPLETE` |
| `STARTED` | E7 | Stop; the assistant row was settled `cancelled` by `TurnRunner` | `finish(event,'cancelled')`; `user_message_id` is preserved by `COALESCE` in `Repository.finish_ui_event` | `CANCELLED` |
| `STARTED` | E8 | generation raised | `finish(event,'error')` | `ERROR` |
| `STARTED` | E9 | row is `started` at startup | same statement as above; `reap_pending` also sets the reply row to `ERROR` with the crash text | `INTERRUPTED` |
| `COMPLETE`/`CANCELLED`/`ERROR`/`INTERRUPTED` | E9 | filtered out by `WHERE status IN ('accepted','started')` | none | unchanged |
| `COMPLETE`/`CANCELLED`/`ERROR`/`INTERRUPTED` | E5 | `rowcount == 0` | `ValueError('UI event was already consumed')`; no messages written | unchanged |
| any settled | E1 | a *new* identity, no `LOCKED` row for the block | a second, independent row | `ACCEPTED` |
| any | E6/E7/E8 | **no guard** (`src/blackrose/storage/repository.py::Repository.finish_ui_event`) | `status` and `COALESCE(user_message_id)` are overwritten, whatever the current state | any status string (H2) |
| any | E10 | conversation deleted | row removed with the block | row gone |

`InteractiveAnswers.finish` rejects any outcome outside
`complete|cancelled|error|interrupted` (`src/blackrose/interactive_answers.py::InteractiveAnswers.finish`); the
repository does not (H2).

## Invariants

1. **One in-flight submission per block.** The partial unique index covers
   `(block_id, action_id)` for `accepted`/`started` rows
   (`src/blackrose/storage/migrations.py::MIGRATIONS`), and the only writer passes `action_id='submit'`
   (`persist` in `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action`). Pinned by
   `test_inflight_index_refuses_a_second_action_on_the_same_block`.
2. **The lock is released only by settlement.** No `LOCKED` row can be reused:
   the next acceptance for the block needs a distinct `event_id` and a settled
   predecessor. Pinned by
   `test_settled_event_frees_the_block_and_a_new_identity_is_required`.
3. **Rejection writes nothing.** Every guard in `accept_action` runs before the
   insert, so a refused submission leaves no row and consumes no identity.
   Pinned by `test_accept_action_refuses_inputs_before_writing_any_row`.
4. **`accepted` means "not yet linked to a visible request".**
   `begin_turn` is the only writer of `user_message_id` (`src/blackrose/storage/repository.py::Repository.begin_turn`);
   `status='accepted'` therefore implies `user_message_id IS NULL`. Pinned by
   the row assertions in tests 1, 3 and 6.
5. **Context is read before consumption.** `TurnRunner._prepare` reads
   `ui_event_context`, which requires `status='accepted'`
   (`src/blackrose/storage/repository.py::Repository.ui_event_context`); `src/blackrose/turns.py::TurnRunner._run` calls `_begin_turn` afterwards. Pinned by
   `test_acceptance_reads_as_accepted_before_begin_turn_and_started_after`.
6. **Consumption is atomic with turn creation.** The status change, the request
   and the pending reply commit or roll back together (`src/blackrose/storage/repository.py::Repository.begin_turn`),
   so a failed `begin_turn` leaves the event `accepted` and stored no messages.
   Pinned by `test_failed_begin_turn_does_not_consume_the_accepted_event`.
7. **An identity is consumed once.** A second `begin_turn` for the same event is
   refused and writes nothing. Pinned by
   `test_consumed_event_cannot_start_a_second_turn`.
8. **A rotated view cannot start a turn.** The post-acceptance re-check
   in `src/blackrose/gui/session.py::Session.ui_action` settles `interrupted` and creates no task. Pinned by
   `test_view_rotation_between_acceptance_and_turn_start_settles_interrupted`.
9. **Settlement reaches a started row after cancellation.** `Database._worker`
   finishes the write before it re-raises `CancelledError`
   (`src/blackrose/storage/db.py::_worker`), so a
   Stop between `begin_turn` and `finish` still records `cancelled` against the
   consumed row. Pinned by
   `test_turn_cancelled_after_consumption_settles_the_event`.
10. **Every turn that fails before creating the row releases the event.**
    `_send` settles in a `finally`, so prepare failures, `ChatBusy`, provider
    failures and cancellations all reach the row. Pinned by
    `test_pre_turn_failure_releases_the_event_as_error`.
11. **Recovery is idempotent and never touches a settled row.** `reap_pending`
    filters on `accepted`/`started`. Pinned by
    `test_a_cancelled_caller_between_acceptance_and_turn_start_strands_the_lock`
    (second call returns 0) and by
    `tests/test_storage.py::StorageTests.test_recovery_only_parses_streaming_blocks_and_uses_indexes`.
12. **Terminal immutability is a caller invariant, not a repository one.**
    `src/blackrose/storage/repository.py::Repository.finish_ui_event` has no transition guard; only
    `InteractiveAnswers.finish` restricts the vocabulary. See H2.

## Hazards and open questions

- **H1 — `fragile` — `src/blackrose/gui/session.py::Session.ui_action`.** The one live-session leak
  found. `ui_action` accepts (`accept_action`), then creates the turn task
  (`asyncio.create_task(self._send(...))`) with no suspension point in between; a
  cancellation delivered after persistence and before `create_task` unwinds
  through its `finally` without settling the row,
  leaving it `accepted` with no in-session releaser. The block's submit is then
  refused — `idx_ui_inflight` still holds `(block_id,'submit')` and the page sees
  `Duplicate or in-flight UI submission` — until startup `reap_pending`.
  *User Stop cannot reach it*: `stop()` finds `_generation is None` in that
  window and returns `False` after a bell (`src/blackrose/gui/session.py::Session.stop`), which is the behaviour
  `test_stop_during_action_validation_has_no_missing_generation` pins; once
  `_generation` exists, `stop()` queues the cancel with `call_soon` so the task
  enters its recovery scope first, and `close()` yields once for the
  same reason (`src/blackrose/gui/session.py::Session.close`). The window needs a caller that cancels the submitted
  coroutine; the GTK bridge runs it with `run_coroutine_threadsafe` and never
  cancels that future (`src/blackrose/gui/bridge.py::LoopThread.submit`,
  `src/blackrose/gui/bridge.py::Api.ui_action`). Demonstrated by
  `test_a_cancelled_caller_between_acceptance_and_turn_start_strands_the_lock`.
  Confirmation that it is unreachable in the product: no `future.cancel()` or
  `task.cancel()` path on a `ui_action` call exists in `src/`.
- **H2 — `fragile` — `src/blackrose/storage/repository.py::Repository.finish_ui_event`.** `finish_ui_event`
  updates any row, from any state, to any string. Two consequences: a settled
  row is rewritten (`complete → cancelled`), and a status outside the vocabulary
  is accepted, producing a row that is neither `LOCKED` (lock silently released)
  nor reapable (`reap_pending`'s filter never matches) nor readable by
  `ui_event_context`. Only `src/blackrose/interactive_answers.py::InteractiveAnswers.finish` validates the
  vocabulary, and no caller passes `user_message_id`.
  Demonstrated by
  `test_finish_rewrites_a_settled_row_and_accepts_out_of_vocabulary_status_documented_follow_up`
  and `test_finish_refuses_outcomes_outside_the_terminal_vocabulary`. No
  product path passes a bad status.
- **H3 — `documentation-gap` — `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action`, `src/blackrose/storage/migrations.py::MIGRATIONS`.**
  `action_id` is always `'submit'`, so the index's second column is constant and
  its semantics are "one in-flight submission per block", not "per action". Two
  *different* actions offered by the same block can never be in flight together:
  the second is refused, and because the message is the shared
  `'Duplicate or in-flight UI submission'` (`Repository.accept_ui_event`) the
  page cannot tell a duplicate identity from a busy block. Matching
  `src/blackrose/ui_documents.py::continuation` supports exactly one declared action today, so the
  index is correct for the current protocol; a future multi-action block would
  need per-action identities. Demonstrated by
  `test_inflight_index_refuses_a_second_action_on_the_same_block`.
- **H4 — `documentation-gap` — `src/blackrose/storage/repository.py::Repository.begin_turn`.** `begin_turn`
  consumes any `accepted` event without checking that its block belongs to
  `conversation_id`; the only ownership check is `ui_event_context`'s join on the
  block's message (`Repository.ui_event_context`), which runs earlier in
  `_prepare`. The product path is safe because
  `src/blackrose/chat.py::ChatController.conversation` is bound per turn by a
  `ContextVar` (`_turn_conversation`, created in `ChatController.__init__`), so
  navigation cannot redirect the
  insert. Demonstrated by
  `test_begin_turn_links_an_event_from_another_conversation_documented_follow_up`.
  Confirming it needs a repository caller that skips `_prepare` — none exists in
  `src/`.
- **H5 — `fragile` — `src/blackrose/storage/repository.py::Repository.reap_pending`.** `reap_pending`'s
  `interrupted` transition is unconditional for `accepted`/`started` rows, so it
  cannot distinguish a crash leftover from a live turn. The only caller is
  `src/blackrose/chat.py::ChatController.recover_interrupted` from
  `src/blackrose/gui/session.py::Session.boot`, i.e. before the first transcript is
  published, and one desktop owns the database (ADR 0001). A second writer, or a
  direct call during a live turn, marks the live row `interrupted`; a context read
  in that window fails `'stale'` (`Repository.ui_event_context`), and the turn's own unguarded
  `finish` (H2) then rewrites it to its real outcome. Demonstrated by
  `test_reap_during_a_live_turn_flips_a_started_row_and_settlement_overwrites_it`.
  A `started_at` column or an owner token would make the two distinguishable.
- **H6 — `fragile` — `src/blackrose/storage/db.py::Database.run`.** The cancellation-safe write
  guarantee starts when `_worker` owns the operation. A cancellation delivered
  while `Database.run` is still waiting on `Database._lock` (its
  `async with self._lock`) drops the
  settlement entirely, so a *second* cancellation landing inside `_send`'s
  `finally` while another writer holds the lock would leave the row inflight.
  Two in-process cancellations of `_generation` need two `stop()` calls in the
  recovery window; one stop was verified to settle correctly
  (`test_turn_cancelled_after_consumption_settles_the_event`). Demonstrated at
  the boundary by
  `test_settlement_cancelled_while_queued_on_the_writer_is_lost`, which also
  shows that a cancellation after the worker starts loses nothing.
- **H7 — `none-found` — `src/blackrose/interactive_answers.py::InteractiveAnswers.accept_action`,
  `src/blackrose/turns.py::TurnRunner._begin_turn`, `src/blackrose/gui/session.py::Session._send`.** Apart from H1 and H6, the lifecycle is closed:
  validation precedes the insert (invariant 3), the insert is shielded and
  settled on cancellation (E2), the context read precedes consumption
  (invariant 5), consumption is atomic (invariant 6), `_send` settles in a
  `finally` on all three exit paths, and startup reaps anything a
  crash left (`Repository.reap_pending`). The bead's question — can a row be stranded during a live
  session? — is answered *no for user-driven cancellation*, and *yes only* for a
  caller cancellation in the acceptance gap (H1) or a double cancellation on the
  writer lock (H6).

## Formalization decisions

**No source change.** `interactive_answers.py` (the file this bead owns) is
sound as written: its guards run before persistence, the shielded `settle`
guarantees that a cancellation either happens before the row exists or is
observed and settled (`settle(persist())` in `accept_action`), and `finish` restricts
the vocabulary. The two demonstrated gaps live in shared central files
(`repository.py`, `session.py`) that other beads are editing, so they are
proposals only.

Proposed patches, for the owner of `storage/repository.py`:

```python
# Repository.finish_ui_event — terminal immutability, and the
# vocabulary check the repository currently trusts its caller for.
async def finish_ui_event(self, event_id, status, user_message_id=None):
    if status not in ('complete', 'cancelled', 'error', 'interrupted'):
        raise ValueError('Invalid UI event outcome')
    def save(conn):
        with conn:
            conn.execute(
                "UPDATE ui_events SET status=?, user_message_id=COALESCE(?,user_message_id) "
                "WHERE event_id=? AND status IN ('accepted','started')",
                (status, user_message_id, event_id))
    await self._db.run(save)
```

A zero-row result must stay silent rather than raise: `_send`'s `finally`
and `ui_action`'s refusal path may both settle after a reap has
already closed the row, and turning that into an exception would abort the rest
of `_send`'s cleanup. Follow-ups this requires: rewrite
`test_finish_rewrites_a_settled_row_and_accepts_out_of_vocabulary_status_documented_follow_up`
to assert the refusal, keep
`test_finish_refuses_outcomes_outside_the_terminal_vocabulary` as-is, and check
`tests/test_session_lifecycle.py::StartupRecoveryTests.asyncSetUp`, which seeds
`ui_events` rows directly before reopening (unaffected, it never calls `finish_ui_event`).

For the schema, add a vocabulary constraint in a new migration (`status TEXT
NOT NULL DEFAULT 'accepted' CHECK(status IN (...))`); SQLite cannot add a CHECK
to an existing table, so this needs a table rebuild or a `BEFORE UPDATE` trigger,
and it must accept the legacy rows reaped by migration-compatible code.

For H1, the smallest fix that stays inside `interactive_answers.py` would be for
`accept_action` to return the row and for `Session.ui_action` to hand ownership
to a finally that settles an unconsumed `accepted` event; that changes the
`Session`/collaborator contract, so it belongs with the `gui/session.py` owner.
For H4, add `AND b.message_id IN (SELECT id FROM messages WHERE conversation_id=?)`
to `begin_turn`'s `ui_events` UPDATE, which turns a programming error into a
refusal without changing the product path.

## Verification

Prerequisite: the interactive-answer contract built by the [checkout
setup](../../CONTRIBUTING.md#set-up-a-checkout) (`npm run build --prefix
ui-runtime`), because the UI document tests read it.

Offline, from the repository root:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_06_ui_action_events.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_ui_documents.py
.venv/bin/python scripts/check_unit_tests.py -p test_session_lifecycle.py
.venv/bin/python scripts/check_unit_tests.py -p test_saved_compatibility.py
.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
.venv/bin/python scripts/check_unit_tests.py -p test_security_boundaries.py
.venv/bin/python scripts/check_unit_tests.py -p test_transcript_history.py
.venv/bin/python scripts/check_unit_tests.py -p test_gui_renderer_recovery.py
.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
```

The first suite pins the invariants and demonstrates the hazards above; the
others are the pre-existing coverage of the coupled code.
`tests/test_ui_documents.py` holds the `InteractiveAnswers` cases (cancellation
during acceptance, reload during acceptance, startup reaping, stale, tampered and
cross-conversation refusal), and `test_reply_checkpoints.py` includes the
process-kill crash probe.

The native probe `scripts/check_gui_interactive_ui.py` (GTK/WebKit and a display)
exercises the desktop path. H1 and H6 are demonstrated by cancellation injection,
not by driving the desktop: confirming H1 in the product would need a GTK-level
cancel of a submitted bridge coroutine, which the bridge has no path to;
confirming H6 would need two Stop events inside the recovery window, observable
only as a missing `status` write.
