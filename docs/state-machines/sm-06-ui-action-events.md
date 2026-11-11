# SM-06: Interactive UI action submission and the `ui_events` in-flight lock

## Scope and ownership

- `src/blackrose/interactive_answers.py`: `AnswerRepository` protocol (20–25),
  `InteractiveAnswers.owned_block` (45–59), `save_state` (61–70),
  `accept_action` (72–92), `finish` (94–97).
- `src/blackrose/storage/repository.py`: `begin_turn` (619–651),
  `reap_pending` (884–917), `accept_ui_event` (1070–1079),
  `ui_event_context` (1081–1090), `finish_ui_event` (1092–1097); the read that
  attaches `ui_context` to a stored request (`_attach_all`, 191–194).
- `src/blackrose/storage/migrations.py`: migration 3 (34), `ui_events`
  (47–55), partial unique index `idx_ui_inflight` (56–57).
- `src/blackrose/gui/session.py`: `_ui_payload` (531–543), `_check_ui_view`
  (545–551), `_owned_ui` (553–559), `ui_state` (565–575), `ui_action`
  (577–599), `_busy` (1034–1035), `stop` (1060–1087), `close` (353–376),
  `boot` (327–345), `_send` (1310–1505; settlement at 1473–1474).
- Coupled: `ui_documents.continuation` (312–328) and `event_id` (434–437),
  `TurnRunner._prepare`/`_begin_turn` (`turns.py:374–428`, 471–496),
  `ChatController.recover_interrupted` (`chat.py:229–232`),
  `artifacts.settle` (`artifacts.py:24–36`), `Database.run`/`_worker`
  (`storage/db.py:82–94`, 20–35).

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[ADR 0003](../adr/0003-interactive-answer-ownership.md):

- The **page** owns nothing durable. It echoes an `event_id` and the session's
  view token; `UIEnvelope.parse` and `_check_ui_view` (545–551) reject anything
  else.
- **`Session`** owns the ephemeral part: `_ui_views` tokens, the
  `_ui_action_pending` interlock, the single `_generation` task, and the
  after-acceptance re-check of its own view (586–589).
- **`InteractiveAnswers`** owns the persisted policy gate (conversation, block
  kind/revision, runtime availability, `status='ready'`, protocol/library/schema
  hashes; 45–59) and validated acceptance/settlement (72–97).
- **`Repository`** owns atomicity. All writes go through `Database.run`, which
  holds one `asyncio.Lock` around one worker thread (`db.py:82–94`) and rolls
  back a failed callback; `_worker` waits for the thread even after the caller is
  cancelled (20–35). One desktop owns the database (ADR 0001), so there is
  exactly one writer process.
- **`TurnRunner`** owns the turn: it reads the event's context while it is still
  `accepted` (420), consumes it inside the same transaction that inserts the
  user request and pending reply (350, 471–496), and `Session._send` settles it
  in a `finally` (1468–1474).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `ui_events.event_id` | `migrations.py:48` | `TEXT PRIMARY KEY` | Submission identity; validated by `ui_documents.event_id` (`ui_documents.py:434–437`) | `accept_ui_event` insert only (`repository.py:1075`) |
| `ui_events.block_id` | `migrations.py:49` | `TEXT NOT NULL REFERENCES message_blocks(block_id) ON DELETE CASCADE` | The form/comparison block that offered the action | same insert |
| `ui_events.action_id` | `migrations.py:50` | `TEXT NOT NULL` | Second column of `idx_ui_inflight`; the only writer passes the constant `'submit'` (`interactive_answers.py:83`, required again by `ui_documents.py:316`) | same insert |
| `ui_events.status` | `migrations.py:52` | `TEXT NOT NULL DEFAULT 'accepted'`, **no CHECK** | The discriminator | default on insert (`repository.py:1075`); `begin_turn` (640–641); `finish_ui_event` (1094); `reap_pending` (904) |
| `ui_events.user_message_id` | `migrations.py:53` | `INTEGER REFERENCES messages(id) ON DELETE SET NULL` | The visible request the action produced | `begin_turn` (640) and `finish_ui_event`'s `COALESCE(?, user_message_id)` (1094); no caller passes the optional argument |
| `ui_events.payload` | `migrations.py:51` | `TEXT NOT NULL` (JSON) | The validated submission values | same insert |
| `ui_events.created_at` | `migrations.py:54` | `TEXT NOT NULL` | Ordering only; nothing reads it | same insert |
| `idx_ui_inflight` membership | `migrations.py:56–57` | Partial unique index on `(block_id, action_id) WHERE status IN ('accepted','started')` | The lock that makes a block's submission exclusive | SQLite, on every status write |
| `Session._ui_views[block_id]` | `session.py:540` | `dict[str, str]` (hex token) | View identity the page must echo | `_ui_payload` `setdefault` (540); cleared by `_redraw` (502) |
| `Session._ui_action_pending` | `session.py:581`, 599 | `bool` | Admission interlock covering acceptance through task creation | `ui_action` |
| `Session._generation` | `session.py:593` | `asyncio.Task \| None` | The turn that owns a `started` event | `ui_action`, `submit`, `retry`, repair paths; cleared at 1030–1031, 1297, 1502 |
| `config.interactive_ui_actions_enabled` | `config.py`; read at `interactive_answers.py:73`, `session.py:541` | `bool` | Policy gate for offering and accepting actions | preferences |
| `message_blocks.payload.status` | `migrations.py:39`; read at `interactive_answers.py:55` | JSON field | `'ready'` is required before an action may be accepted | `save_blocks`, `reap_pending` (898–903) |

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
| `LOCKED` | `status IN ('accepted','started')` | `idx_ui_inflight` (`migrations.py:56–57`), `reap_pending` (904) |
| `SETTLED` | `status` is one of the six above and not `LOCKED` | `ui_event_context` refuses it (1085); `reap_pending` skips it (904) |
| `BLOCK_FREE` | no `LOCKED` row with `(block_id, 'submit')` | the next `accept_ui_event` insert |
| `CONTEXT_READABLE` | `status='accepted'` and the block's message belongs to the conversation | `ui_event_context` (1083–1086), read by `_prepare` (420) |
| `USER_TURN_LINKED` | `user_message_id IS NOT NULL` | `_attach_all` attaches `ui_context` to that stored request (`repository.py:191–194`) |

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Accept a submission | `Session.ui_action` (577) → `InteractiveAnswers.accept_action` (72) → `accept_ui_event` (1070) |
| E2 | Persistence cancelled during acceptance | `asyncio.CancelledError` around `settle(persist())` (`interactive_answers.py:86–91`) |
| E3 | Session state changed after acceptance | second `_check_ui_view` and the busy/quit/permission re-check (`session.py:585–592`) |
| E4 | Turn preparation and context read | `TurnRunner._prepare` (`turns.py:374–428`, `ui_event_context` at 420) |
| E5 | Consume the event into a turn | `TurnRunner._begin_turn` (471–496) → `Repository.begin_turn` (619) |
| E6 | Turn completes | `_send` `end_stored` (1414–1425) → `finally` (1473–1474) |
| E7 | Turn cancelled | `_send`'s `CancelledError` branch (1441–1456) → `finally`; `Session.stop` (1060–1087) is the only in-session canceller |
| E8 | Turn fails before or during generation | `_send`'s `Exception` branch (1457–1467) → `finally` |
| E9 | Startup recovery | `Session.boot` (330) → `ChatController.recover_interrupted` (`chat.py:231`) → `reap_pending` (884) |
| E10 | Conversation deleted | `message_blocks` cascade → `ui_events.block_id` `ON DELETE CASCADE` (`migrations.py:49`) |
| E11 | Read `ui_events` for display | `_attach_all` (191–194), joining on `user_message_id` |

No other statement writes `status`: a source-wide search finds only
`1075`, `640–641`, `1094` and `904`.

## Transition table

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | E1 | conversation, block kind, `source_revision`, `status='ready'`, protocol/library/schema hashes (`interactive_answers.py:45–59`); actions enabled (73); `action_type='continue_conversation'` (75); event identity regex (77, `ui_documents.py:434–437`); `submit` declared and values valid (`ui_documents.py:312–328`) | `INSERT` with the schema default status and `created_at` (`repository.py:1075–1076`) | `ACCEPTED` |
| `ABSENT` | E1 | any guard above fails | `ValueError` before the insert; no row | `ABSENT` |
| `ABSENT` | E1 | `idx_ui_inflight` already holds `(block_id,'submit')` (`migrations.py:56–57`) | `IntegrityError` → `ValueError('Duplicate or in-flight UI submission')` (`repository.py:1077–1078`); write rolled back | unchanged (self-transition) |
| `ABSENT` | E1 | `event_id` already exists (primary key) | same refusal as above; a retry needs a new identity | unchanged |
| `ACCEPTED` | E2 | the insert completed before the cancellation was observed (`artifacts.settle` 24–36 waits for the task; `interactive_answers.py:89–90`) | `finish(event,'interrupted')` under `settle`, then `CancelledError` is re-raised | `INTERRUPTED` |
| `ACCEPTED` | E2 | the insert never completed | `accepted` stays `False`; nothing is settled | `ABSENT` |
| `ACCEPTED` | E3 | view token rotated, `_quit_requested`, `_permission_pending`, `is_generating`, or a live `_generation` (`session.py:586–589`) | `finish(event,'interrupted')` (591) and the refusal is returned to the page; no task is created | `INTERRUPTED` |
| `ACCEPTED` | E5 | `ui_event_context` was readable (`turns.py:420`) and the row is still `accepted`; `rowcount == 1` | One transaction: insert `COMPLETE` user request + `PENDING` reply, set `user_message_id` and `status='started'`, bump `conversations.updated_at` (`repository.py:632–649`) | `STARTED` |
| `ACCEPTED` | E5 | the row is no longer `accepted` (`rowcount != 1`) | `ValueError('UI event was already consumed')` (`repository.py:642–643`); the whole transaction rolls back, including the two message inserts | `ACCEPTED` |
| `ACCEPTED` | E6/E8 | the turn failed before `E5` (prepare/admission/provider error) | `_send`'s `finally` calls `finish(event,'error')` (1474) | `ERROR` |
| `ACCEPTED` | E7 | the turn was cancelled before `E5` | `_send`'s `finally` calls `finish(event,'cancelled')`; the draft is restored (1454) | `CANCELLED` |
| `ACCEPTED` | E9 | row is `accepted` at startup | `UPDATE ... WHERE status IN ('accepted','started')` (`repository.py:904`) | `INTERRUPTED` |
| `STARTED` | E6 | the stored reply settled (`end_stored`, 1415) | `finish(event, <message state>)` → `complete` (1474) | `COMPLETE` |
| `STARTED` | E7 | Stop; the assistant row was settled `cancelled` by `TurnRunner` | `finish(event,'cancelled')`; `user_message_id` is preserved by `COALESCE` (1094) | `CANCELLED` |
| `STARTED` | E8 | generation raised | `finish(event,'error')` | `ERROR` |
| `STARTED` | E9 | row is `started` at startup | same statement as above (904); the reply row is set to `ERROR` with the crash text (905–913) | `INTERRUPTED` |
| `COMPLETE`/`CANCELLED`/`ERROR`/`INTERRUPTED` | E9 | filtered out by `WHERE status IN ('accepted','started')` | none | unchanged |
| `COMPLETE`/`CANCELLED`/`ERROR`/`INTERRUPTED` | E5 | `rowcount == 0` | `ValueError('UI event was already consumed')`; no messages written | unchanged |
| any settled | E1 | a *new* identity, no `LOCKED` row for the block | a second, independent row | `ACCEPTED` |
| any | E6/E7/E8 | **no guard** (`repository.py:1092–1097`) | `status` and `COALESCE(user_message_id)` are overwritten, whatever the current state | any status string (H2) |
| any | E10 | conversation deleted | row removed with the block | row gone |

`InteractiveAnswers.finish` rejects any outcome outside
`complete|cancelled|error|interrupted` (`interactive_answers.py:95–96`); the
repository does not (H2).

## Invariants

1. **One in-flight submission per block.** The partial unique index covers
   `(block_id, action_id)` for `accepted`/`started` rows
   (`migrations.py:56–57`), and the only writer passes `action_id='submit'`
   (`interactive_answers.py:83`). Pinned by
   `test_inflight_index_refuses_a_second_action_on_the_same_block`.
2. **The lock is released only by settlement.** No `LOCKED` row can be reused:
   the next acceptance for the block needs a distinct `event_id` and a settled
   predecessor. Pinned by
   `test_settled_event_frees_the_block_and_a_new_identity_is_required`.
3. **Rejection writes nothing.** Every guard in `accept_action` runs before the
   insert, so a refused submission leaves no row and consumes no identity.
   Pinned by `test_accept_action_refuses_inputs_before_writing_any_row`.
4. **`accepted` means "not yet linked to a visible request".**
   `begin_turn` is the only writer of `user_message_id` (`repository.py:640`);
   `status='accepted'` therefore implies `user_message_id IS NULL`. Pinned by
   the row assertions in tests 1, 3 and 6.
5. **Context is read before consumption.** `TurnRunner._prepare` reads
   `ui_event_context`, which requires `status='accepted'` (1085), at
   `turns.py:420`; `_begin_turn` runs afterwards at 350. Pinned by
   `test_acceptance_reads_as_accepted_before_begin_turn_and_started_after`.
6. **Consumption is atomic with turn creation.** The status change, the request
   and the pending reply commit or roll back together (`repository.py:632–649`),
   so a failed `begin_turn` leaves the event `accepted` and stored no messages.
   Pinned by `test_failed_begin_turn_does_not_consume_the_accepted_event`.
7. **An identity is consumed once.** A second `begin_turn` for the same event is
   refused and writes nothing. Pinned by
   `test_consumed_event_cannot_start_a_second_turn`.
8. **A rotated view cannot start a turn.** The post-acceptance re-check
   (`session.py:586`) settles `interrupted` and creates no task. Pinned by
   `test_view_rotation_between_acceptance_and_turn_start_settles_interrupted`.
9. **Settlement reaches a started row after cancellation.** `Database._worker`
   finishes the write before it re-raises `CancelledError` (`db.py:20–35`), so a
   Stop between `begin_turn` and `finish` still records `cancelled` against the
   consumed row. Pinned by
   `test_turn_cancelled_after_consumption_settles_the_event`.
10. **Every turn that fails before creating the row releases the event.**
    `_send` settles in a `finally`, so prepare failures, `ChatBusy`, provider
    failures and cancellations all reach the row. Pinned by
    `test_pre_turn_failure_releases_the_event_as_error`.
11. **Recovery is idempotent and never touches a settled row.** `reap_pending`
    filters on `accepted`/`started` (904). Pinned by
    `test_a_cancelled_caller_between_acceptance_and_turn_start_strands_the_lock`
    (second call returns 0) and by `tests/test_storage.py:191–235`.
12. **Terminal immutability is a caller invariant, not a repository one.**
    `finish_ui_event` has no transition guard (1092–1097); only
    `InteractiveAnswers.finish` restricts the vocabulary. See H2.

## Hazards and open questions

- **H1 — `fragile` — `gui/session.py:584–593`.** The one live-session leak
  found. `ui_action` accepts (584), then creates the turn task (593) with no
  suspension point in between; a cancellation delivered after persistence and
  before `create_task` unwinds through `finally` (598) without settling the row,
  leaving it `accepted` with no in-session releaser. The block's submit is then
  refused — `idx_ui_inflight` still holds `(block_id,'submit')` and the page sees
  `Duplicate or in-flight UI submission` — until startup `reap_pending`.
  *User Stop cannot reach it*: `stop()` finds `_generation is None` in that
  window and returns `False` after a bell (1078–1082), which is the behaviour
  `test_stop_during_action_validation_has_no_missing_generation` pins; once
  `_generation` exists, `stop()` queues the cancel with `call_soon` so the task
  enters its recovery scope first (1084–1086), and `close()` yields once for the
  same reason (371–373). The window needs a caller that cancels the submitted
  coroutine; the GTK bridge runs it with `run_coroutine_threadsafe` and never
  cancels that future (`gui/bridge.py:55–65`, `271–272`). Demonstrated by
  `test_a_cancelled_caller_between_acceptance_and_turn_start_strands_the_lock`.
  Confirmation that it is unreachable in the product: no `future.cancel()` or
  `task.cancel()` path on a `ui_action` call exists in `src/`.
- **H2 — `fragile` — `storage/repository.py:1092–1097`.** `finish_ui_event`
  updates any row, from any state, to any string. Two consequences: a settled
  row is rewritten (`complete → cancelled`), and a status outside the vocabulary
  is accepted, producing a row that is neither `LOCKED` (lock silently released)
  nor reapable (904 never matches) nor readable by `ui_event_context` (1085).
  Only `InteractiveAnswers.finish` validates the vocabulary
  (`interactive_answers.py:95–96`), and no caller passes `user_message_id`.
  Demonstrated by
  `test_finish_rewrites_a_settled_row_and_accepts_out_of_vocabulary_status_documented_follow_up`
  and `test_finish_refuses_outcomes_outside_the_terminal_vocabulary`. No
  product path passes a bad status.
- **H3 — `documentation-gap` — `interactive_answers.py:83`, `migrations.py:56`.**
  `action_id` is always `'submit'`, so the index's second column is constant and
  its semantics are "one in-flight submission per block", not "per action". Two
  *different* actions offered by the same block can never be in flight together:
  the second is refused, and because the message is the shared
  `'Duplicate or in-flight UI submission'` (1078) the page cannot tell a
  duplicate identity from a busy block. Matching `ui_documents.continuation`
  (`ui_documents.py:312–328`) supports exactly one declared action today, so the
  index is correct for the current protocol; a future multi-action block would
  need per-action identities. Demonstrated by
  `test_inflight_index_refuses_a_second_action_on_the_same_block`.
- **H4 — `documentation-gap` — `storage/repository.py:640–643`.** `begin_turn`
  consumes any `accepted` event without checking that its block belongs to
  `conversation_id`; the only ownership check is `ui_event_context`'s join on the
  block's message (1083–1086), which runs earlier in `_prepare`. The product path
  is safe because `ChatController.conversation` is bound per turn by a
  `ContextVar` (`chat.py:121`, 143–151), so navigation cannot redirect the
  insert. Demonstrated by
  `test_begin_turn_links_an_event_from_another_conversation_documented_follow_up`.
  Confirming it needs a repository caller that skips `_prepare` — none exists in
  `src/`.
- **H5 — `fragile` — `storage/repository.py:904`.** `reap_pending`'s
  `interrupted` transition is unconditional for `accepted`/`started` rows, so it
  cannot distinguish a crash leftover from a live turn. The only caller is
  `chat.py:231` from `Session.boot` (330), i.e. before the first transcript is
  published, and one desktop owns the database (ADR 0001). A second writer, or a
  direct call during a live turn, marks the live row `interrupted`; a context read
  in that window fails `'stale'` (1088), and the turn's own unguarded
  `finish` (H2) then rewrites it to its real outcome. Demonstrated by
  `test_reap_during_a_live_turn_flips_a_started_row_and_settlement_overwrites_it`.
  A `started_at` column or an owner token would make the two distinguishable.
- **H6 — `fragile` — `storage/db.py:82–94`.** The cancellation-safe write
  guarantee starts when `_worker` owns the operation. A cancellation delivered
  while `Database.run` is still waiting on `Database._lock` (`db.py:84`) drops the
  settlement entirely, so a *second* cancellation landing inside `_send`'s
  `finally` while another writer holds the lock would leave the row inflight.
  Two in-process cancellations of `_generation` need two `stop()` calls in the
  recovery window; one stop was verified to settle correctly
  (`test_turn_cancelled_after_consumption_settles_the_event`). Demonstrated at
  the boundary by
  `test_settlement_cancelled_while_queued_on_the_writer_is_lost`, which also
  shows that a cancellation after the worker starts loses nothing.
- **H7 — `none-found` — `interactive_answers.py:72–92`, `turns.py:471–496`,
  `session.py:1468–1474`.** Apart from H1 and H6, the lifecycle is closed:
  validation precedes the insert (invariant 3), the insert is shielded and
  settled on cancellation (E2), the context read precedes consumption
  (invariant 5), consumption is atomic (invariant 6), `_send` settles in a
  `finally` on all three exit paths (1414–1474), and startup reaps anything a
  crash left (904). The bead's question — can a row be stranded during a live
  session? — is answered *no for user-driven cancellation*, and *yes only* for a
  caller cancellation in the acceptance gap (H1) or a double cancellation on the
  writer lock (H6).

## Formalization decisions

**No source change.** `interactive_answers.py` (the file this bead owns) is
sound as written: its guards run before persistence, the shielded `settle`
guarantees that a cancellation either happens before the row exists or is
observed and settled (`interactive_answers.py:86–91`), and `finish` restricts
the vocabulary. The two demonstrated gaps live in shared central files
(`repository.py`, `session.py`) that other beads are editing, so they are
proposals only.

Proposed patches, for the owner of `storage/repository.py`:

```python
# finish_ui_event (repository.py:1092) — terminal immutability, and the
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
(1474) and `ui_action`'s refusal path (591) may both settle after a reap has
already closed the row, and turning that into an exception would abort the rest
of `_send`'s cleanup. Follow-ups this requires: rewrite
`test_finish_rewrites_a_settled_row_and_accepts_out_of_vocabulary_status_documented_follow_up`
to assert the refusal, keep
`test_finish_refuses_outcomes_outside_the_terminal_vocabulary` as-is, and check
`tests/test_session_lifecycle.py:40–44`, which seeds `ui_events` rows directly
before reopening (unaffected, it never calls `finish_ui_event`).

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

Environment setup (the generated UI contract is absent from a fresh worktree
and is gitignored; the build is the documented checkout step):

```sh
ln -s <main-checkout>/ui-runtime/node_modules ui-runtime/node_modules
npm --prefix ui-runtime run build
# Built interactive UI: 96 modules, 615871 bytes; schema b2136b8b…30bf3b
```

Offline, from the repository root:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_06_ui_action_events.py -v
# Ran 15 tests in 1.134s ... OK        (repeated 3x, no flakes)
.venv/bin/python scripts/check_unit_tests.py -p test_ui_documents.py
# Ran 37 tests in 1.176s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_session_lifecycle.py
# Ran 26 tests in 1.008s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_saved_compatibility.py
# Ran 6 tests in 1.284s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
# Ran 17 tests in 0.270s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
# Ran 28 tests in 0.885s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_security_boundaries.py
# Ran 73 tests in 3.811s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_transcript_history.py
# Ran 12 tests in 0.681s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_gui_renderer_recovery.py
# Ran 11 tests in 0.211s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_interactive_ui_evaluation.py
# Ran 5 tests in 0.004s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
# OK (includes the process-kill crash probe)
```

There is no `tests/test_interactive_answers.py` in this checkout; the
pre-existing coverage for `InteractiveAnswers` lives in
`tests/test_ui_documents.py` (cancellation during acceptance, reload during
acceptance, startup reaping, stale/tampered/cross-conversation refusal) and is
listed above.

Not run: the native probe `scripts/check_gui_interactive_ui.py` (needs GTK/WebKit
and a display). H1 and H6 were demonstrated by cancellation injection, not by
driving the desktop: confirming H1 in the product would need a GTK-level cancel
of a submitted bridge coroutine, which the bridge has no path to; confirming H6
would need two Stop events inside the recovery window, observable only as a
missing `status` write.
