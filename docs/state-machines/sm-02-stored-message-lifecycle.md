# SM-02: Persisted stored-message lifecycle and usable history

## Scope and ownership

- `src/blackrose/storage/records.py`: `MessageState` (36–50), `StoredMessage.state`/
  `error` (130–131), `StoredMessage.is_usable_history` (145–158).
- `src/blackrose/storage/repository.py`: `begin_turn` (619–651), `add_message`
  (653–677), `checkpoint_content` (750–765), `finish_message` (767–819),
  `reap_pending` (884–917), `save_evidence` (1099–1104), `save_audit` (1129–1143).
- `src/blackrose/storage/migrations.py`: migration 1 `messages` (8–15), migration 5
  `message_audits` (66–70), migration 9 `memory_outbox` (125–133), migration 11
  indexes (140–145).
- Coupled tables: `message_attachments` (migrations.py:18–25), `evidence`
  (26–29), `message_blocks`/`ui_state`/`ui_events` (34–57).

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[ADR 0001](../adr/0001-desktop-ownership.md):

- Exactly one desktop process owns the chat database. All repository work is
  submitted through `Database.run`, which holds an `asyncio.Lock` around a
  single worker thread and rolls back a failed callback
  (`storage/db.py:69–95`). Rows are therefore mutated in submission order;
  there is no cross-process writer while the desktop is running.
- `TurnRunner` (application asyncio loop) owns turn settlement: it holds
  `self.completed` as the local proof that the complete write landed
  (`turns.py:235–236`, 358–365) and shields the final write from cancellation
  (`turns.py:736–758`).
- `Session` owns startup recovery ordering: it awaits
  `controller.recover_interrupted()` before publishing the transcript
  (`gui/session.py:330`), which calls `Repository.reap_pending()`
  (`chat.py:229–232`).
- The repository stores state; it does not decide policy. `prompts.py` and
  `citations.py` decide what a stored state *means* for model input.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `messages.state` | `migrations.py:13`; `records.py:130` | `TEXT CHECK(state IN ('pending','complete','cancelled','error'))` / `MessageState` | Lifecycle position of one row | inserts `repository.py:633–638`, `669`; terminal `repository.py:791–794`; recovery `repository.py:905–913` |
| `messages.error` | `migrations.py:14`; `records.py:131` | `TEXT NULL` | Human-readable stop/failure text for the transcript | same two statements (`:792`, `:906`) |
| `messages.role` | `migrations.py:11` | `TEXT CHECK(role IN ('system','user','assistant'))` | Author of the row | inserts only |
| `messages.content` | `migrations.py:12` | `TEXT NOT NULL DEFAULT ''` | Checkpointed prefix, then final text | `checkpoint_content` `:758–762`; `finish_message` `:791–794`; `save_audit` `:1139` |
| `messages.reply_to` | `migrations.py:14` | `INTEGER NULL REFERENCES messages(id)` | Assistant row → its request | `begin_turn` `:636–638` |
| `messages.refs` | `migrations.py:32` | JSON text | Selected file/skill pointers | `begin_turn` `:635` |
| `message_audits.report` | `migrations.py:66–70` | JSON text (`AnswerAudit`) | Citation audit checkpoint (`status='pending'`) or final report | `save_audit` `:1140`; `finish_message` `:789`; `reap_pending` `:896` |
| `evidence.sources` | `migrations.py:26–29` | JSON text | Captured retrieval sources for a reply | `finish_message` `:800–802`; `save_evidence` `:1099–1104` |
| `memory_outbox` row (`kind='turn'`) | `migrations.py:125–133` | Row presence | Durable duty to send a completed turn to Honcho | `finish_message` `:803–810` only |
| `conversations.memory_used` | `migrations.py:136` | `INTEGER` | Chat may have a Honcho session to delete | `finish_message` `:808–810` |
| `message_blocks.payload.status` | `migrations.py:39` | JSON field | `streaming` → `ready`/`invalid`/`cancelled` | `save_blocks`; `reap_pending` `:898–903` |
| `ui_events.status` | `migrations.py:47–55` | `TEXT` | `accepted` → `started` → `completed`/`interrupted` | `begin_turn` `:639–643`; `finish_ui_event` `:1092–1097`; `reap_pending` `:904` |
| `StoredMessage.is_usable_history` | `records.py:145–158` | Derived `bool` | Whether this row may be replayed to a model | Computed on read; never persisted |

`MessageState.is_finished` (`records.py:48–50`) is a convenience predicate with no
callers in `src/`, `tests/` or `scripts/`; it is not part of the runtime contract.

## States

The lifecycle has four mutually exclusive states. `state` is the only persisted
discriminator; `error` and the coupled tables are consequences, not inputs.

| State | Predicate | Content/error shape in product paths |
| --- | --- | --- |
| `PENDING` | `state='pending'` | `content` is the last checkpoint (may be `''`); `error IS NULL`; an audit row may exist with `status` `pending`/`incomplete`; blocks may be `streaming` |
| `COMPLETE` | `state='complete'` | `error IS NULL`; final `content`, final audit (if citation rigor ran) and evidence written in the settling transaction |
| `CANCELLED` | `state='cancelled'` | `error` is the stop reason (`TurnRunner` writes `'Stopped'`, `turns.py:764–765`); `content` is the partial reply; blocks are finalised as `cancelled` before settlement (`turns.py:778–780`) |
| `ERROR` | `state='error'` | `error` is the exception class/message, or the fixed crash string `"interrupted: Blackrose exited during generation"` (`repository.py:905–913`) |

Derived projections (not states of the row, but of what the rest of the system does
with it):

| Projection | Predicate | Consumers |
| --- | --- | --- |
| `USABLE_HISTORY` | `is_usable_history` (`records.py:145–158`) | prompt construction (`prompts.py:455–461`), image-capability gate (`turns.py:404–406`), citation inheritance (`citations.py:744–756`) |
| `NOT_USABLE_HISTORY` | negation of the above | same |
| `DISPLAY_STOPPED` | `state is CANCELLED` (`gui/render.py:643`) | transcript payload; `error` is shown only as text (`render.py:642`) |
| `DISPLAY_FAILED` | `state is ERROR` and/or non-null `error` | transcript payload |

`is_usable_history` in full (`records.py:154–158`):

- `role='user'` → `content.strip()` or attachments. State is *not* consulted.
- assistant with `PENDING` or `ERROR` → `False`.
- assistant with `COMPLETE` or `CANCELLED` → `content.strip()` or attachments or blocks.

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Start a turn | `TurnRunner._begin_turn` → `Repository.begin_turn` (`turns.py:471–496`, `repository.py:619`) |
| E2 | Persist an app-created row | `Repository.add_message` (`repository.py:653`), used by scripts/probes only |
| E3 | Content checkpoint tick | `TurnRunner._checkpoint_content` (`turns.py:238–257`, 525–526) → `checkpoint_content` |
| E4 | Audit checkpoint | `CitationAudit.checkpoint` (`citations.py:756–764`) → `save_audit`; called before streaming (`turns.py:523`) and on each audit phase |
| E5 | Generation finished successfully | `TurnRunner._complete` (`turns.py:722–760`) → `finish_message(COMPLETE)` |
| E6 | User Stop | `asyncio.CancelledError` in `_run` (`turns.py:358–365`) → `_settle_interrupted` → `finish_message(CANCELLED)` (`turns.py:762–801`) |
| E7 | Generation failure | any other exception in `_run` (`turns.py:358–365`) → `finish_message(ERROR)` |
| E8 | Settlement retry | `_save_interrupted` retries once without evidence after a failed atomic write (`turns.py:790–801`) |
| E9 | Startup recovery | `Session.boot` → `ChatController.recover_interrupted` → `reap_pending` (`gui/session.py:330`, `chat.py:229–232`) |
| E10 | Conversation deletion | `ON DELETE CASCADE` from `messages` (`migrations.py:10`, 20, 27, 37, 49, 68) |
| E11 | Read for transcript / history | `Repository.messages`/`message_page` (`repository.py:821–855`) → `_load_messages` → `_attach_all` |

No other statement writes `messages.state`: a source-wide search finds only
`:633–638`, `:669`, `:791–794`, `:905–913`.

## Transition table

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| — | E1 | `begin_turn` runs on an existing conversation; `ui_event_id` (if any) must still be `accepted` and is consumed with `rowcount==1` (`repository.py:639–643`) | Insert request row `COMPLETE` (`:633–634`), insert reply row `PENDING` with `content=''`, `reply_to=user.id` (`:636–638`), bump `conversations.updated_at` (`:644`); all in one transaction | request `COMPLETE`, reply `PENDING` |
| — | E2 | none (`add_message`) | Insert row at the caller's `state`, default `COMPLETE` (`repository.py:653–677`, `records.py:130`) | any of the four |
| `PENDING` | E3 | assistant, `state='pending'`, no `message_audits` row (`:758–762`); statement updates 1 row | Replace `content` only; returns `True`/`False` (`:763`) | `PENDING` |
| `PENDING` | E4 | assistant, `state='pending'` (`:1132–1134`); optional body must hash-match the report (`:1136–1138`) | Replace `content` (when given) and `message_audits.report` in one transaction (`:1139–1142`) | `PENDING` |
| `PENDING` | E5 | audit, when supplied, must hash-match `content` (`:785–788`) | Insert/replace audit (`:789–790`), write `content/state/error` (`:791–794`), bump `updated_at` (`:795–799`), replace evidence (`:800–802`), queue `memory_outbox('turn')` when `remember` and the chat is not excluded (`:803–810`) | `COMPLETE` |
| `PENDING` | E6 | `TurnRunner.completed is None` (`turns.py:358–365`) | Same write as E5 with `state='cancelled'`, `error='Stopped'`; no outbox row | `CANCELLED` |
| `PENDING` | E7 | same | Same write with `state='error'`, `error` = exception text | `ERROR` |
| `PENDING` | E8 | first attempt raised; row is still `pending` | Repeat E6/E7 without `evidence` so the original reason is retained (`turns.py:790–801`) | `CANCELLED`/`ERROR` |
| `PENDING` | E9 | row is `pending` at startup (`repository.py:905–913`) | Rewrite any pending audit as `incomplete` with recomputed `body_hash` (`:892–897`), cancel every `streaming` block (`:898–903`), interrupt `accepted`/`started` events (`:904`), write `ERROR` + fixed crash text; returns `rowcount` | `ERROR` |
| `COMPLETE` | E9 | filtered out by `WHERE state='pending'` (`:907`) | none; `reap_pending` returns 0 | `COMPLETE` |
| `CANCELLED`/`ERROR` | E9 | same | none | unchanged |
| `COMPLETE`/`CANCELLED`/`ERROR` | E3 | `state='pending'` in the guard | 0 rows; returns `False` (`:763`) | unchanged |
| `COMPLETE`/`CANCELLED`/`ERROR` | E4 | `state='pending'` check raises | `ValueError('Audit checkpoints require a pending assistant message')` (`:1134`) | unchanged |
| `COMPLETE`/`CANCELLED`/`ERROR` | E5/E6/E7 | **no guard** (`:791–794`) | Overwrites `content/state/error` (and audit/evidence/outbox) of a settled row | any terminal state |
| `COMPLETE`/`CANCELLED`/`ERROR` | E2 | none | A second, independent row | — |
| any | E10 | conversation deleted | cascades the row, its attachments, evidence, audit and blocks | row gone |

The only *refused* transitions are E3 and E4 from a terminal state; E5–E8 are
unguarded at the repository boundary (hazard H1).

## Invariants

1. **Atomic turn creation.** A turn is either one `COMPLETE` request plus one
   `PENDING` reply linked by `reply_to`, or nothing. Enforced by one transaction
   in `begin_turn` (`repository.py:631–649`); pinned by
   `test_begin_turn_commits_request_attachments_and_pending_reply` and
   `test_begin_turn_rolls_back_request_if_pending_reply_fails`
   (`tests/test_storage.py`), and by
   `test_begin_turn_creates_one_complete_request_and_one_pending_reply`.
2. **Closed state vocabulary.** `messages.state` is one of `pending`, `complete`,
   `cancelled`, `error`, and `role` one of `system`, `user`, `assistant`
   (`migrations.py:11–13`). Pinned by
   `test_schema_accepts_only_the_four_lifecycle_states_and_three_roles`.
3. **Requests are terminal at birth.** Application code creates `user` rows with
   `COMPLETE` only (`repository.py:633–634`), and no product writer ever moves a
   user row (only the unguarded `finish_message` of H1 can).
4. **Pending content is written only by the two guarded checkpoints.** While
   `state='pending'`, `content` changes only through `checkpoint_content`
   (`:758–762`) or `save_audit` (`:1139`), and `checkpoint_content` refuses once
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
   (`repository.py:803–810`). Pinned by
   `test_memory_outbox_is_queued_only_for_a_complete_remembered_turn` and
   `test_failed_replies_and_form_submissions_are_not_sent` (`tests/test_memory.py`).
7. **No pending row survives startup recovery.** After `reap_pending` returns, no
   `messages` row is `pending`, and a second call returns 0. Pinned by
   `test_reap_pending_is_idempotent_and_never_rewrites_terminal_rows` and
   `test_reaping_an_audited_pending_row_is_stable_across_reopen`, plus the
   existing crash probe in `tests/test_reply_checkpoints.py`.
8. **Recovery never invents or erases content.** `reap_pending` keeps the last
   checkpointed `content` and the audit's captured `sources`, only rewriting the
   report `status`/`body_hash`/claims (`citations.py:331–345`). Pinned by
   `test_reap_pending_settles_the_message_its_audit_blocks_and_events` and
   `test_crash_reaps_pending_audit_and_preserves_captured_text`
   (`tests/test_citations.py`).
9. **Recovery never rewrites a terminal row.** The message statement filters on
   `state='pending'` (`repository.py:905–913`); audits and events are only
   rewritten for pending/in-flight rows, and block rewriting is limited to
   `status='streaming'`. Pinned by
   `test_reap_pending_is_idempotent_and_never_rewrites_terminal_rows`.
10. **In-flight and failed replies are not history.** `is_usable_history` is
    `False` for assistant `PENDING` and `ERROR` rows; `COMPLETE`/`CANCELLED`
    rows are usable iff they carry text, attachments or blocks. Pinned by
    `test_usable_history_truth_table`.
11. **History assembly consumes exactly the usable rows.** `PromptBuilder.build`
    maps `history` through `is_usable_history` (`prompts.py:455–461`), and
    `wire_message` serialises blocks in place of `content` when blocks exist
    (`prompts.py:206–213`), so a block-only cancelled reply contributes its block
    sources. Pinned by `test_history_wire_reads_what_usable_history_admits`.
12. **Terminal immutability is a caller invariant, not a repository invariant.**
    `TurnRunner.completed` (`turns.py:358–365`) and the `pending` filter in
    `reap_pending` are the only things preventing a second settlement. Not
    enforced by `finish_message`; see H1.

## Hazards and open questions

- **H1 — `fragile` — `repository.py:791–794`.** `finish_message` accepts any
  target id and any state, with no role check and no `state='pending'` guard. A
  second call rewrites a settled row; on a `user` row it rewrites content too.
  Demonstrated by
  `test_finish_message_can_rewrite_a_terminal_row_documented_follow_up`. No
  product path reaches it: `TurnRunner` settles only while `completed` is unset,
  the final write is shielded, and `reap_pending` filters on `pending`. But
  `tests/test_diagram_repairs.py:147` already relies on re-settling a row that
  `session.submit` left `COMPLETE` (verified: state after submit is `complete`),
  so a strict guard changes existing behavior. Confirm with a grep for a second
  `finish_message` on one id in product code (none today) and with a trigger that
  logs duplicate settlements.
- **H2 — `documentation-gap` — `repository.py:791–794`, `migrations.py:13–14`.**
  Nothing couples `error` to `state`: `COMPLETE` with error text and `ERROR`
  with `NULL` both persist. Only `TurnRunner` discipline keeps `COMPLETE`
  error-free and failures named (`turns.py:764–765`, 791–792). Demonstrated by
  `test_finish_message_does_not_couple_error_text_to_state_documented_follow_up`.
  Display is driven by `state` and `error` independently (`render.py:642–643`),
  so a schema/API coupling would be a user-visible change, not just a cleanup.
- **H3 — `fragile` — `repository.py:1099–1104`.** `save_evidence` writes evidence
  for any message id with no role or state check, unlike its siblings. It has no
  callers in `src/` (only tests). Not a corruption risk today: evidence on an
  unusable row is only read by `_private_context` (`turns.py:327`) and
  `citations.py:752–755` (which filters on `is_usable_history`). Confirm with a
  grep in future code; either delete the method or give it the same
  pending-assistant guard as `save_audit`.
- **H4 — `none-found` — `repository.py:884–917`.** Recovery covers every table
  that can reference a pending message: `message_audits`, `message_blocks`,
  `ui_events`, and `messages`. `memory_outbox` cannot reference a pending row
  (written only on `COMPLETE`). `evidence` can (only via H3) and is left in
  place; `reap_pending` marks the audit `incomplete` but keeps its captured
  `sources` (`citations.py:331–345`). `message_attachments` and `ui_state` are
  keyed by rows that survive; nothing dangles.
- **H5 — `fragile` — `records.py:154–155`.** `is_usable_history` ignores `refs`
  and `ui_context`, which `wire_message` does emit (`prompts.py:219–223`). A
  user row with references but no text and no attachments is therefore dropped
  from later history even though it was sent with the turn. Unreachable through
  the GUI, which refuses an empty send with no attachments
  (`gui/session.py:933`), but reachable through `ChatController.send('', refs=...)`.
  Confirm by sending an empty prompt with one file reference and inspecting the
  next prompt's history.
- **H6 — `documentation-gap` — `migrations.py:11–13`.** The schema permits
  role/state combinations the application never writes (`user`/`pending`,
  `system`/`error`) and has no trigger tying state to role. `add_message`
  (`repository.py:653–677`) is the only API that can create them, and no product
  caller passes a non-default state. Consider a partial index or trigger if a
  future feature needs `pending` user rows.
- **H7 — `fragile` — `repository.py:884–917`.** `reap_pending` assumes the
  single-owner database (ADR 0001). Any second writer running during generation
  would convert a live turn to `ERROR` with the crash text. There is no lock
  file, no ownership check and no "started at" column to tell a live turn from a
  dead one. The existing crash probe (`tests/test_reply_checkpoints.py`) kills
  the process before reopening, so it does not test the concurrent case.

## Formalization decisions

**No source change.** H1 is real but the smallest correct fix is not local: a
`state='pending'`/role guard changes behavior that `tests/test_diagram_repairs.py`
depends on for fixture setup, and `repository.py` is shared with other in-flight
work. Proposed patch, for the owner of `repository.py`:

```python
# finish_message, before the audit insert at repository.py:785
changed = conn.execute(
    "UPDATE messages SET content = ?, state = ?, error = ? "
    "WHERE id = ? AND role = 'assistant' AND state = 'pending'",
    (content, state.value, error, message_id),
)
if changed.rowcount != 1:
    raise ValueError('Only a pending assistant message can be resolved')
```

and move the audit/evidence/outbox writes after it so a refused settle writes
nothing. Follow-ups this requires: rewrite `tests/test_diagram_repairs.py:147`
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
# Ran 14 tests ... OK

.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
# Ran 17 tests ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
# Ran 6 tests ... OK  (includes the process-kill crash probe)
.venv/bin/python scripts/check_unit_tests.py -p test_citations.py
# Ran 64 tests ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_memory.py
# Ran 47 tests ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_terminal_evidence.py
# Ran 8 tests ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
# Ran 28 tests ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_diagram_repairs.py
# Ran 11 tests ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_prompts.py
# Ran 3 tests ... OK
```

Environment-limited, unrelated to this change: `test_session_lifecycle.py` and
`test_ui_documents.py` fail in this checkout before touching message state,
because the generated `src/blackrose/gui/static/ui-contract.json` is absent
(`ui_documents._fingerprint`, `ui_documents.py:55`). They need the runtime build
(`npm --prefix ui-runtime`) and were not used as evidence here.

Unverified: H5's refs-only send was reasoned from `session.py:933` and
`prompts.py:455–461`, not executed; confirming it needs one empty-text, one-ref
turn through `ChatController.send`. H7's concurrent-writer case was not
exercised (doing so requires a second process with a live turn).
