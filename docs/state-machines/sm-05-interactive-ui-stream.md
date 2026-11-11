# SM-05: Interactive UI stream parser, block lifecycle and widget state sequence

## Scope and ownership

- `src/blackrose/ui_documents.py::UIStream`, `src/blackrose/ui_documents.py::apply_repair`, `src/blackrose/ui_documents.py::validate_document`,
  `src/blackrose/ui_documents.py::repaired_source`, and the limits `src/blackrose/ui_documents.py::MAX_SOURCE` and
  `src/blackrose/ui_documents.py::MAX_BLOCKS`.
- `src/blackrose/storage/records.py::UIBlock`.
- `src/blackrose/storage/repository.py::Repository.save_blocks`, `src/blackrose/storage/repository.py::Repository.repair_block`, `src/blackrose/storage/repository.py::Repository.reap_pending`,
  `src/blackrose/storage/repository.py::Repository.ui_block`, `src/blackrose/storage/repository.py::Repository.save_ui_state`, and the sanctioned diagram-rewrite
  exception `src/blackrose/storage/repository.py::Repository.claim_diagram_repair`/`src/blackrose/storage/repository.py::Repository.finish_diagram_repair`.
- `src/blackrose/turns.py::TurnRunner._repair_ui`, `src/blackrose/turns.py::TurnRunner._append`, `src/blackrose/turns.py::TurnRunner._answer`, `src/blackrose/turns.py::TurnRunner._stream_round`,
  `src/blackrose/turns.py::TurnRunner._close_ui_segment`, `src/blackrose/turns.py::TurnRunner._save_interrupted`,
  `src/blackrose/turns.py::require_normal_finish`.
- Coupled boundaries: `src/blackrose/gui/session.py::Session._ui_payload`, `src/blackrose/gui/session.py::Session._check_ui_view`,
  `src/blackrose/gui/session.py::Session._owned_ui`, `src/blackrose/gui/session.py::Session.ui_state`, `src/blackrose/gui/session.py::Session._send.on_ui`;
  `src/blackrose/interactive_answers.py::InteractiveAnswers.owned_block`, `src/blackrose/interactive_answers.py::InteractiveAnswers.save_state`; the `message_blocks` and `ui_state`
  tables in migration 3 of `src/blackrose/storage/migrations.py::MIGRATIONS`; and `ui-runtime/host.jsx::flush`, `ui-runtime/host.jsx::mount`, `ui-runtime/host.jsx::update`. Ownership per
  [architecture](../architecture.md#modules-and-invariants): `ui_documents.py`
  owns "streaming block assembly and continuation serialization";
  `storage/` owns "immutable finalized UI blocks, sequenced saved state".

Three state machines share one set of rows and must be read together:

1. **The parser** (`UIStream`), owned by `TurnRunner`, one instance per turn,
   mutated only on the application asyncio loop: `_append` is called from the
   stream loop (`src/blackrose/turns.py::TurnRunner._stream_round`) and from tool callbacks, and `_close_ui_segment`
   runs between rounds. No other task touches it.
2. **The persisted block** (`message_blocks.payload`), owned by `Repository` and
   mutated only inside `Database.run`, which serialises on one thread and rolls
   back a failed callback (`src/blackrose/storage/db.py::Database.run`). The only desktop process owns
   the database ([ADR 0001](../adr/0001-desktop-ownership.md)).
3. **The widget state row** (`ui_state`), owned by `InteractiveAnswers`
   (`src/blackrose/interactive_answers.py::InteractiveAnswers.save_state`), which is the only caller of
   `save_ui_state`; the page supplies `state_sequence`, and the repository is the
   authority that rejects it (`src/blackrose/storage/repository.py::Repository.save_ui_state`).

The Python side decides what a block *is*; `ui-runtime/host.jsx` only renders it
and re-reads `source_sequence`/`state_sequence` from the payloads Python sends.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `UIStream.profile` | `src/blackrose/ui_documents.py::UIStream.__init__` | `str \| None` | `None` = plain markdown round; a contract profile = NDJSON round | `begin`; `end` |
| `UIStream.active` | `src/blackrose/ui_documents.py::UIStream.__init__` | `UIBlock \| None` | Markdown block currently receiving text | `begin`; `append`; `_markdown`; `end` |
| `UIStream.pending` | `src/blackrose/ui_documents.py::UIStream.__init__` | `str` | Bytes after the last newline in a UI round | `begin`; `append`; `end` |
| `UIStream.prose` | `src/blackrose/ui_documents.py::UIStream.__init__` | `bool` | Sticky per-round "this round degraded to prose" latch | `begin` clears; `_line` sets |
| `UIStream.used` | `src/blackrose/ui_documents.py::UIStream.__init__` | `bool` | Turn emitted (or promised) semantic content; gates publish/persist | `begin` (`used or profile is not None`) |
| `UIStream.source_bytes` | `src/blackrose/ui_documents.py::UIStream.__init__` | `int` | Cumulative bytes seen in UI-profile rounds of this turn | `append` |
| `UIStream.blocks` | `src/blackrose/ui_documents.py::UIStream.__init__` | `list[UIBlock]` | Ordered live blocks, append-only | `_new` |
| `block.position` | `src/blackrose/ui_documents.py::UIStream._new` | `int` | `len(blocks)` at creation; the transcript order | never rewritten (`src/blackrose/storage/repository.py::Repository.save_blocks`) |
| `block.kind` | `src/blackrose/ui_documents.py::UIStream._new` | `'markdown' \| 'interactive_ui'` | Rendering/repair class | `_new` only |
| `block.status` | `src/blackrose/ui_documents.py::UIStream._new`, `src/blackrose/ui_documents.py::UIStream._line`, `src/blackrose/ui_documents.py::UIStream.end`; `src/blackrose/storage/repository.py::Repository.reap_pending` | `'streaming' \| 'ready' \| 'invalid' \| 'cancelled'` | Lifecycle of the block's source | see transition table |
| `block.source_revision` | `src/blackrose/ui_documents.py::UIStream._new` | `int` | 1 at creation, +1 per accepted repair | `apply_repair`; `finish_diagram_repair` (markdown only) |
| `block.source_sequence` | `src/blackrose/ui_documents.py::UIStream._new` | `int` | Monotone redraw/ordering counter; the page drops a lower one (`ui-runtime/host.jsx::update`) | `append`; `_markdown`; `apply_repair`; `finish_diagram_repair` |
| `block.source`, `source_hash`, `document`, `fallback_text`, `error` | `src/blackrose/ui_documents.py::UIStream._new`, `src/blackrose/ui_documents.py::UIStream._line`, `src/blackrose/ui_documents.py::apply_repair` | JSON text / dict | Validated source and its renderable fallbacks | `_line`, `apply_repair` |
| `block.diagram_promoted`, `diagram_repairs` | `src/blackrose/storage/records.py::UIBlock`; `src/blackrose/storage/repository.py::Repository.claim_diagram_repair`, `src/blackrose/storage/repository.py::Repository.finish_diagram_repair` | JSON fields | Markdown-only diagram state, unrelated to UI blocks | diagram repair only |
| `message_blocks.payload` | migration 3 of `src/blackrose/storage/migrations.py::MIGRATIONS` | JSON text | Whole `UIBlock` | `save_blocks`; `repair_block`; `reap_pending` |
| `ui_state.sequence` | migration 3 of `src/blackrose/storage/migrations.py::MIGRATIONS` | `INTEGER` | Last accepted widget-state sequence, 0 when no row | `save_ui_state` |
| `ui_state.state` | migration 3 of `src/blackrose/storage/migrations.py::MIGRATIONS` | JSON text | Last accepted widget values | same statement |
| `saved_state`, `state_sequence` | `src/blackrose/storage/repository.py::Repository.ui_block`, `src/blackrose/storage/repository.py::_attach_batch` | injected on read | Join of `ui_state` onto the block shape | reads only |
| `view_id` | `src/blackrose/gui/session.py::Session._ui_payload` | session-only token | Rotated per redraw; a stale token is refused (`src/blackrose/gui/session.py::Session._check_ui_view`) | `_ui_payload`, never persisted |

## States

### Parser (`UIStream`, one round at a time)

| State | Predicate | Meaning |
| --- | --- | --- |
| `IDLE` | `profile is None`, `pending == ''`, `active is None` | Between rounds, or inside a plain-markdown round: the object has no open/closed flag, so these two are indistinguishable (`end` and `begin(None)` leave the same field values) |
| `MARKDOWN` | `profile is None` and a round is streaming | Every chunk is appended to one `kind='markdown'`, `status='streaming'` block (`src/blackrose/ui_documents.py::UIStream.append`) |
| `BUFFERING` | `profile is not None and not prose` | Line buffering; only a non-blank line whose first non-space character is `{` may become an interactive block (`src/blackrose/ui_documents.py::UIStream._line`) |
| `PROSE` | `profile is not None and prose` | Latched degraded mode; every line, valid JSON or not, becomes markdown (`src/blackrose/ui_documents.py::UIStream._line`) |

`prose` is the only round-scoped field that outlives `end()`, and `begin()` is
the only thing that clears it (`src/blackrose/ui_documents.py::UIStream.begin`). Within a round it cannot be un-latched.

### Persisted block

| State | Predicate | Notes |
| --- | --- | --- |
| `STREAMING` | `payload.status == 'streaming'` | Only state in which `save_blocks` may replace source (`src/blackrose/storage/repository.py::Repository.save_blocks`) |
| `READY` | `'ready'` | Interactive, actionable; the page renders controls only for `ready`/`streaming` and enables them only for `ready` (`ui-runtime/host.jsx::render`) |
| `INVALID` | `'invalid'` | Created by failed validation (`src/blackrose/ui_documents.py::UIStream._line`), by error settlement (`src/blackrose/turns.py::TurnRunner._save_interrupted`), or by the reaper (`src/blackrose/storage/repository.py::Repository.reap_pending`). Repairable exactly once |
| `CANCELLED` | `'cancelled'` | Stop settlement (`src/blackrose/turns.py::TurnRunner._save_interrupted`) or crash reaping (`src/blackrose/storage/repository.py::Repository.reap_pending`); not repair-eligible |

Two transient, non-persisted states mattered for H1: a `STAGED` candidate dict
(produced by `apply_repair`, not yet committed) and the divergence
`MEMORY_STALE` (memory says `INVALID` revision *n* while the row is `READY`
revision *n+1*). Since the H1 fix, memory takes the candidate before the commit
is awaited, so `MEMORY_STALE` is no longer reachable from a Stop.

### Widget state

| State | Predicate | Meaning |
| --- | --- | --- |
| `UNSET` | no `ui_state` row; `ui_block` reports `state_sequence == 0` | Page renders contract defaults (`ui-runtime/host.jsx::mount`) |
| `SAVED(n)` | row `sequence = n ≥ 1` | Page state is authoritative and the next accepted edit is `n+1` |

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Round begins | `TurnRunner._answer` → `UIStream.begin(prompt.ui_profile)` (`src/blackrose/turns.py::TurnRunner._answer`) |
| E2 | Visible text delta | provider stream → `_append` → `UIStream.append` (`src/blackrose/turns.py::TurnRunner._stream_round`, `src/blackrose/turns.py::TurnRunner._append`) |
| E3 | New block appears | `_stream_round` saves all blocks the moment the list grows (`src/blackrose/turns.py::TurnRunner._stream_round`) |
| E4 | Round end | `_close_ui_segment` → `UIStream.end()` (`src/blackrose/turns.py::TurnRunner._close_ui_segment`) |
| E5 | Round normal-finish check | `require_normal_finish` (called from `src/blackrose/turns.py::TurnRunner._answer`; `src/blackrose/turns.py::require_normal_finish`); refuses before E4 runs, so blocks stay `streaming` in memory |
| E6 | Round blocks persisted | `save_blocks` (`src/blackrose/turns.py::TurnRunner._close_ui_segment`) |
| E7 | One-shot repair request | `_repair_ui` → `repair_text` (`src/blackrose/turns.py::TurnRunner._repair_ui`) |
| E8 | Repair commit | `apply_repair` then `repair_block` (`src/blackrose/turns.py::TurnRunner._repair_ui`) |
| E9 | Stop | `_run` catch → `_settle_interrupted` (`src/blackrose/turns.py::TurnRunner._settle_interrupted`) → `_save_interrupted` (`src/blackrose/turns.py::TurnRunner._save_interrupted`) |
| E10 | Failure | same path with `state='error'` |
| E11 | Startup recovery | `Session.boot` → `recover_interrupted` → `reap_pending` (`src/blackrose/chat.py::ChatController.recover_interrupted`; `src/blackrose/storage/repository.py::Repository.reap_pending`) |
| E12 | Widget state edit | page `flush` (`ui-runtime/host.jsx::flush`) → `Session.ui_state` → `InteractiveAnswers.save_state` → `save_ui_state` |
| E13 | Redraw / reload | full transcript or `ui_block` read (`src/blackrose/storage/repository.py::_attach_batch`, `src/blackrose/storage/repository.py::Repository.ui_block`) |
| E14 | View rotation | `Session._ui_payload` issues a new `view_id`; `_check_ui_view` refuses any other (`src/blackrose/gui/session.py::Session._ui_payload`, `src/blackrose/gui/session.py::Session._check_ui_view`) |
| E15 | Conversation deleted | `ON DELETE CASCADE` from `messages` (migration 3 of `src/blackrose/storage/migrations.py::MIGRATIONS`) |

No other statement writes `payload.status`: a source-wide search finds only
`src/blackrose/ui_documents.py::UIStream._line`, `src/blackrose/ui_documents.py::UIStream.end`, `src/blackrose/storage/repository.py::Repository.reap_pending`, `src/blackrose/storage/repository.py::Repository.repair_block` and
`src/blackrose/turns.py::TurnRunner._save_interrupted`.

## Transition table

### Parser

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | E1 `begin(p)` | — | `active=None`; `profile=p`; `pending=''`; `prose=False`; `used \|= p is not None` (`src/blackrose/ui_documents.py::UIStream.begin`) | `p is None` → `MARKDOWN`, else `BUFFERING` |
| `MARKDOWN` | E2 | — | create `markdown` block at `position=len(blocks)` if `active` is not one; append; `source_sequence += 1` (`src/blackrose/ui_documents.py::UIStream.append`, `src/blackrose/ui_documents.py::UIStream._markdown`) | `MARKDOWN` |
| `MARKDOWN` | E4 `end(s)` | `pending == ''` (always) | no flush; `s` applies to `streaming` blocks only; `profile=None`; `active=None`; `publish(force=True)` (`src/blackrose/ui_documents.py::UIStream.end`) | `IDLE` |
| `BUFFERING` | E2 | `source_bytes + len(part) ≤ 65536` | `source_bytes += len`; `pending += part`; each complete line → `_line`; throttled publish (`src/blackrose/ui_documents.py::UIStream.append`) | `BUFFERING` |
| `BUFFERING` | E2 | budget exceeded | `ValueError('UI source exceeds the 64 KiB turn budget')` | `BUFFERING` (round fails) |
| `BUFFERING` | line | blank (`.strip()` empty) | ignored; no block (`src/blackrose/ui_documents.py::UIStream._line`) | `BUFFERING` |
| `BUFFERING` | line | first non-space char is `{` and `interactive_ui` count `< 12` | new `interactive_ui` block `status='streaming'`; validate → `document`/`fallback_text`, else `status='invalid'` + `error` + readable fallback | `BUFFERING` |
| `BUFFERING` | line | 12 interactive blocks already | `ValueError('UI reply exceeds 12 blocks')` | `BUFFERING` (round fails) |
| `BUFFERING` | line | non-blank and not `{`-started | `prose=True`; new/extended `markdown` block with `line+'\n'` | `PROSE` |
| `PROSE` | line | any line | blank → `'\n'` appended to markdown; otherwise `line+'\n'` | `PROSE` |
| `PROSE` | line | — | a valid JSON line is markdown, never a block | `PROSE` |
| `BUFFERING`/`PROSE` | E4 `end('ready')` | leftover `pending.strip()` | flush through `_line`; a `ValueError` propagates, leaving the round open (`src/blackrose/ui_documents.py::UIStream.end`) | `IDLE` only on success |
| `BUFFERING`/`PROSE` | E4 `end(s≠'ready')` | leftover `pending.strip()` | flush through `_line`, `ValueError` swallowed | `IDLE` |
| any open | E4 | — | every `status == 'streaming'` block becomes `s`; terminal blocks untouched; `pending=''`; `profile=None`; `active=None`; publish | `IDLE` |
| `IDLE` | E4 again | — | no-op: `profile is None` and no streaming blocks | `IDLE` |

### Persisted block

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| — | `save_blocks` | owner is an assistant message, ids match (`src/blackrose/storage/repository.py::Repository.save_blocks`) | INSERT at the caller's status — a first write is `streaming` or `invalid`, never terminal by policy | `STREAMING`/`INVALID` |
| `STREAMING` | `save_blocks` | identity keys equal, `source_sequence ≥ prior` | replace payload | `STREAMING` |
| `STREAMING` | `save_blocks` | identity differs | `ValueError('UI block identity is immutable')`; the whole call rolls back | `STREAMING` |
| `STREAMING` | `save_blocks` | lower `source_sequence` | `ValueError('Stale UI source sequence')` | `STREAMING` |
| `STREAMING` | E4+E6 ready round | — | `status='ready'` written at E6 | `READY` |
| `STREAMING` | E9 Stop | — | `end('cancelled')` then save (`src/blackrose/turns.py::TurnRunner._save_interrupted`) | `CANCELLED` |
| `STREAMING` | E10 failure | — | `end('invalid')` then save | `INVALID` |
| `STREAMING` | E11 crash | row still `streaming` (`src/blackrose/storage/repository.py::Repository.reap_pending`) | status rewritten to `cancelled`, payload otherwise byte-identical | `CANCELLED` |
| `INVALID` | E8 `repair_block` | prior `invalid`, new `ready`, identity equal, revision exactly +1 (`src/blackrose/storage/repository.py::Repository.repair_block`) | replace payload; `source_sequence` +1 by `apply_repair` (`src/blackrose/ui_documents.py::apply_repair`) | `READY` |
| `INVALID` | `save_blocks` | prior not `streaming` (`src/blackrose/storage/repository.py::Repository.save_blocks`) | identical payload → no write; any change → `ValueError('Finalized UI source is immutable')` | `INVALID` |
| `READY` | `save_blocks` | as above | identical → no-op; change → refused | `READY` |
| `READY`/`CANCELLED` | E8 | prior not `invalid` (`src/blackrose/storage/repository.py::Repository.repair_block`) | `ValueError('Only an invalid UI block can be repaired')` | unchanged |
| `INVALID` | E8 | revision ≠ prior+1 | `ValueError('UI repair must advance the source revision once')` | `INVALID` |
| `READY` (markdown only) | `claim`/`finish_diagram_repair` | `kind == 'markdown'` (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`), claim still `repairing` and source matches (`src/blackrose/storage/repository.py::Repository.finish_diagram_repair`) | rewrite `source`, `source_revision += 1`, `source_sequence += 1` | `READY` |
| `READY` | E13 | — | read only; `saved_state`/`state_sequence` merged onto the returned dict (`src/blackrose/storage/repository.py::_attach_batch`) | `READY` |
| any | E15 | — | row deleted | gone |

`finish_diagram_repair` is the one sanctioned rewrite of a finalised block, and it
is confined to `markdown`; no path rewrites an `interactive_ui` block except
`repair_block`, which requires `invalid`.

### Widget state

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNSET` | E12 | `sequence == 1`, block row exists | INSERT (`src/blackrose/storage/repository.py::Repository.save_ui_state`) | `SAVED(1)` |
| `SAVED(n)` | E12 | `sequence == n+1` | replace `sequence`, `state` | `SAVED(n+1)` |
| `SAVED(n)` | E12 | any other `sequence` | `ValueError('Stale UI state sequence')` | `SAVED(n)` |
| `UNSET` | E12 | block_id absent | `sqlite3.IntegrityError` from the foreign key (migration 3 of `src/blackrose/storage/migrations.py::MIGRATIONS`) | `UNSET` |
| `SAVED(n)` | E8 repair | block stays the same `block_id` | no `ui_state` write; the row is untouched | `SAVED(n)` |
| `SAVED(n)` | E13 redraw | — | join returns `n` and the saved values | `SAVED(n)` |
| `SAVED(n)` | E14 view rotation | — | the page gets a new `view_id` and the same `state_sequence` | `SAVED(n)` |

## Invariants

1. **The prose latch is per round and irreversible inside one.** Once a
   non-blank non-`{` line appears, no later line in that round can create an
   interactive block (`src/blackrose/ui_documents.py::UIStream._line`); only `begin` clears it (`src/blackrose/ui_documents.py::UIStream.begin`).
   Pinned by `test_prose_latch_survives_valid_ui_lines_until_the_next_round`.
2. **Block order is creation order and never renumbered.** `position` is
   `len(blocks)` at creation (`src/blackrose/ui_documents.py::UIStream._new`), blocks are append-only, and
   `save_blocks`/`repair_block` refuse a `position` change
   (`src/blackrose/storage/repository.py::Repository.save_blocks`, `src/blackrose/storage/repository.py::Repository.repair_block`). Pinned by
   `test_block_positions_are_creation_order_across_rounds`,
   `test_save_blocks_is_atomic_and_checks_ownership`.
3. **The parser never mutates a finalised block.** Both `begin` and
   `end` clear `active`, and `_markdown` only reuses `active` when it is
   a markdown block (`src/blackrose/ui_documents.py::UIStream._markdown`), so a `ready` block is never appended to. Pinned by
   `test_markdown_rounds_create_no_semantic_blocks_and_never_publish` (a second
   round creates a new block rather than growing the closed one).
4. **`end()` is the only finaliser, and it never reclassifies a terminal
   block.** `src/blackrose/ui_documents.py::UIStream.end` filters on `streaming`, so an `invalid` block keeps its
   status even when the abort status is `cancelled`. Pinned by
   `test_end_applies_the_abort_status_only_to_streaming_blocks`.
5. **No pending byte survives a round.** `end` flushes among any status and
   clears `pending` (`src/blackrose/ui_documents.py::UIStream.end`); whitespace-only pending is dropped. Pinned by
   `test_round_end_flushes_pending_for_every_status`.
6. **A ready round fails loudly on overflow; an aborting round degrades
   quietly.** The 13th block raises on the `ready` path and is dropped on the
   abnormal path (`src/blackrose/ui_documents.py::UIStream.end`). Pinned by
   `test_over_limit_final_block_fails_a_ready_round_and_is_dropped_on_abort`.
7. **`source_sequence` never decreases for a block.** `save_blocks` refuses a
   lower value (`src/blackrose/storage/repository.py::Repository.save_blocks`); `apply_repair` and diagram repair add
   exactly one (`src/blackrose/ui_documents.py::apply_repair`, `src/blackrose/storage/repository.py::Repository.finish_diagram_repair`). Pinned by
   `test_finalized_source_is_immutable_including_the_source_sequence`.
8. **A finalised block is byte-identical to what was accepted.** While
   `prior.status != 'streaming'`, only a payload equal to the stored one is
   accepted (`src/blackrose/storage/repository.py::Repository.save_blocks`); this is what covers `source_sequence`,
   `source`, `document`, `fallback_text` and `position` together. Pinned by
   `test_finalized_source_is_immutable_including_the_source_sequence`.
9. **`save_blocks` is atomic per call.** The loop runs inside `with conn`
   (`src/blackrose/storage/repository.py::Repository.save_blocks`), so one refused block writes nothing — a new block
   listed before the refused one is not inserted. Pinned by
   `test_save_blocks_is_atomic_and_checks_ownership`.
10. **Repair is one-shot and forward-only.** Only `invalid → ready` is accepted,
    with exactly `revision+1` and unchanged identity (`src/blackrose/storage/repository.py::Repository.repair_block`), so
    a second repair of the same block is refused at storage, and
    `apply_repair` refuses to stage it as well. Pinned by
    `test_repair_is_one_shot_from_invalid_only`,
    `test_repair_guards_revision_identity_and_cancelled_blocks`.
11. **A `cancelled` block is never repaired.** Same guard; repair only
    runs inside a completed UI segment (`src/blackrose/turns.py::TurnRunner._close_ui_segment`), which cannot be
    reached after Stop. Pinned by
    `test_repair_guards_revision_identity_and_cancelled_blocks`.
12. **`ui_state.sequence` is strictly `+1`, per immutable `block_id`, and is
    untouched by repair and redraw.** `block_id` never changes
    (`src/blackrose/storage/repository.py::Repository.repair_block`), `save_ui_state` only writes `current+1`
    (`src/blackrose/storage/repository.py::Repository.save_ui_state`), and `repair_block` never touches `ui_state`. Pinned by
    `test_ui_state_sequence_is_monotonic_across_repair_and_redraw`.
13. **`ui_state` cannot reference a missing block.** Foreign key with cascade
    (migration 3 of `src/blackrose/storage/migrations.py::MIGRATIONS`). Pinned by `test_ui_state_cannot_orphan_a_missing_block`.
14. **An invalid block has no document and no widget state.** `document` is set
    only after validation (`src/blackrose/ui_documents.py::UIStream._line`), and the policy gate
    accepts state/actions only for `ready`/`streaming` with a supported runtime
    (`src/blackrose/interactive_answers.py::InteractiveAnswers.owned_block`); `save_state` also refuses a null document
    (`src/blackrose/interactive_answers.py::InteractiveAnswers.save_state`). Therefore `state_sequence == 0` for every invalid block and a
    repair re-initialises the page widget from the repaired document
    (`ui-runtime/host.jsx::mount`, `ui-runtime/host.jsx::update`). Pinned by
    `test_invalid_blocks_are_refused_by_the_answers_policy_until_repaired`.
15. **A crash leaves no streaming block.** Startup reaping rewrites every
    `streaming` payload to `cancelled` and is idempotent (`src/blackrose/storage/repository.py::Repository.reap_pending`);
    a second reap returns 0 and rewrites nothing else. Pinned by
    `test_reap_cancels_streaming_blocks_once_and_leaves_terminal_blocks_alone`.
16. **A streaming block implies a pending reply.** Every round closes its blocks
    (`src/blackrose/turns.py::TurnRunner._close_ui_segment`) before `_complete` can write `COMPLETE`
    (`src/blackrose/turns.py::TurnRunner._complete`, called from `src/blackrose/turns.py::TurnRunner._run`; `src/blackrose/storage/repository.py::Repository.finish_message`), so a `streaming`
    block on a settled reply is only reachable through the unguarded first insert
    in `save_blocks`.

## Hazards and open questions

- **H1 — `defect` — `src/blackrose/turns.py::TurnRunner._repair_ui` (with `src/blackrose/storage/db.py::run_to_completion`).**
  Resolved: `src/blackrose/turns.py::TurnRunner._repair_ui` now copies the staged
  candidate into the stream block before awaiting `repair_block` and restores the
  prior block only when storage refuses it; pinned by
  `tests/test_sm_05_interactive_ui_stream.py::RepairInterruptionTests.test_stop_after_the_repair_commit_still_settles_the_reply`.
  As found: Stop can
  land inside `await self.repo.repair_block(...)`. `run_to_completion` keeps
  waiting for the worker thread after cancellation and only then re-raises
  `CancelledError` (`src/blackrose/storage/db.py::run_to_completion`), so the repair **is committed** while the
  caller never reaches `block.clear(); block.update(candidate)`.
  The interrupted settlement then calls `end('cancelled')` and `save_blocks`
  (`src/blackrose/turns.py::TurnRunner._save_interrupted`) with the block still `invalid` revision 1 while the row is
  `ready` revision 2; the identity check refuses it
  (`src/blackrose/storage/repository.py::Repository.save_blocks`), the raise happens **before** `finish_message`
  (in `src/blackrose/turns.py::TurnRunner._save_interrupted`, `save_blocks` precedes it), and the reply row stays `pending`. The
  `ValueError` replaces the `CancelledError` escaping `_run`, so the window
  reports the reply as an *error* whose text is the internal message
  `UI block identity is immutable` (the `except Exception` branch of `src/blackrose/gui/session.py::Session._send`), and the next
  start reaps the pending row as a crash (reaper error text,
  `src/blackrose/storage/repository.py::Repository.reap_pending`). The original
  characterization drove the real `_repair_ui` and `_save_interrupted`: the
  committed row was `ready` revision 2, the stream block still `invalid`
  revision 1, `_save_interrupted` raised, and the reply row stayed `PENDING`.
  The window was one local write wide, but deterministic when reached.
- **H2 — `fragile` — `src/blackrose/ui_documents.py::apply_repair`.** Resolved
  for status: `src/blackrose/ui_documents.py::apply_repair` now raises unless the
  block is `invalid`; pinned by
  `tests/test_sm_05_interactive_ui_stream.py::BlockLifecycleTests.test_repair_is_one_shot_from_invalid_only`
  (both staging and storage refuse a second repair). As found: `apply_repair`
  staged a repair onto any dict without checking `status`, so it produced
  `ready`/`revision+1` from a `ready`, `cancelled` or already-repaired block.
  Still open: it accepts a lower `source_sequence` than storage holds, and
  `save_blocks`'s monotonicity check
  (`src/blackrose/storage/repository.py::Repository.save_blocks`) has no
  counterpart in `src/blackrose/storage/repository.py::Repository.repair_block`.
  The only caller takes the sequence from the block it read, so this needs a
  second caller to matter.
- **H3 — `fragile` — `src/blackrose/storage/repository.py::Repository.ui_block`/`src/blackrose/storage/repository.py::_attach_batch` vs `src/blackrose/storage/repository.py::Repository.save_blocks`.** The
  repository injects `saved_state`/`state_sequence` when it reads a block, then
  treats the whole payload as the immutable unit when it writes one. A block
  round-tripped through `ui_block` or `message()` can therefore never be written
  back, and the failure reads as `Finalized UI source is immutable`. Existing
  tests work around it by deleting those keys
  (`tests/test_ui_documents.py::UITests.test_malformed_block_preserves_valid_content_and_immutable_source`). Demonstrated by
  `test_the_repository_read_shape_is_not_accepted_again_documented_follow_up`.
  No product path re-saves a block it read, so this is a trap for future code,
  not a live corruption.
- **H4 — `documentation-gap` — `src/blackrose/turns.py::TurnRunner._save_interrupted` with `src/blackrose/ui_documents.py::UIStream.end`.**
  On error settlement every `streaming` block becomes `invalid`, including
  blocks whose document validated and rendered. `invalid` therefore means both
  "source failed validation" and "the reply failed after this block"; the
  latter blocks keep a non-null `document` and no `error`, and can never be
  repaired (repair requires a UI segment round, `src/blackrose/turns.py::TurnRunner._close_ui_segment`), so they stay
  permanently non-interactive while rendering normally. Demonstrated by
  `test_error_settlement_labels_valid_blocks_invalid`. Whether to add a fifth
  status or to use `cancelled` for error settlement is a product decision, not
  a mechanical fix.
- **H5 — `fragile` — `src/blackrose/storage/repository.py::Repository.save_ui_state` with `ui-runtime/host.jsx::flush`.**
  `save_ui_state` is deliberately not idempotent: a repeated sequence is
  `Stale`. The page advances `record.sequence` only from a successful reply
  (`ui-runtime/host.jsx::flush`) and retries the *same* `sequence + 1` after any failure
  (the `catch` in `flush` sets `dirty` again), and `update()` never refreshes `record.sequence` from an incoming
  payload (`ui-runtime/host.jsx::update`). A committed-but-unacknowledged save (cancellation after
  commit is possible for the same `run_to_completion` reason as H1's original window) therefore wedges that
  widget's saves until the page re-mounts. Not executed: the window needs a
  bridge reply lost after the row was written. Confirm with a probe that
  cancels `Session.ui_state` after `save_ui_state` returns and then retries.
  Either side can be fixed: make the retry idempotent for an identical
  `(sequence, state)`, or have `update()` set `record.sequence` from
  `block.state_sequence`.
- **H6 — `documentation-gap` — `src/blackrose/storage/repository.py::Repository.save_blocks`.** `save_blocks` does not
  validate the status vocabulary, nor that a first insert is `streaming`, nor
  that a stream is finalised before the owning message settles. The model is
  enforced by `turns.py` discipline plus the equality check, not by storage.
  Unreachable from product code (the only caller is `TurnRunner`), so no fix is
  proposed.
- **H7 — `documentation-gap` — `src/blackrose/storage/repository.py::Repository.save_blocks` with
  the `message_blocks` table in migration 3 of `src/blackrose/storage/migrations.py::MIGRATIONS`.** Two blocks claiming the same `position` surface as
  `sqlite3.IntegrityError` from `UNIQUE(message_id, position)`, not as a
  `ValueError`; `save_ui_state` likewise leaks `IntegrityError` for a missing
  block (pinned by `test_ui_state_cannot_orphan_a_missing_block`) even though
  every other refusal in this area is a `ValueError` that `Session` catches.
  Unreachable: positions come from `len(blocks)` and block ids from
  `secrets.token_hex` (`src/blackrose/ui_documents.py::UIStream._new`), and `save_ui_state` is only
  called after `owned_block` resolved the row.
- **H8 — `fragile` — `src/blackrose/turns.py::TurnRunner._repair_ui`.** The repair budget
  (`config.interactive_ui_repairs`, default 1, maximum 2 —
  `src/blackrose/config.py::Config.interactive_ui_repairs`, validated in `src/blackrose/config.py::Config.__post_init__`) is consumed per *attempt*, not per success, and the
  loop scans every block in the stream. Only one UI segment exists per turn
  (`src/blackrose/turns.py::TurnRunner._answer` clears `prompt.ui_profile` after the first round), so no
  block can be retried by a later round; if a future change allows a second UI
  segment, the budget semantics would need revisiting. Pinned by
  `test_repair_budget_targets_only_invalid_interactive_blocks`.
- **H9 — `none-found` — `src/blackrose/storage/repository.py::Repository.reap_pending`.** The reaper's block query is
  not scoped to pending messages, but no product path settles a message while a
  block is `streaming` (invariant 16), so the wider scope cannot convert a live
  turn's blocks.

### Answers to the focus questions

- **Can `prose=True` be un-latched inside a round?** No. Only `begin` clears it
  (`src/blackrose/ui_documents.py::UIStream.begin`), and `begin` runs once per round (`src/blackrose/turns.py::TurnRunner._answer`).
  `end` deliberately leaves it set (`src/blackrose/ui_documents.py::UIStream.end`); the next `begin` clears it.
- **What happens to `pending` at round end and on abort?** Flushed through the
  same `_line` path for any status (`src/blackrose/ui_documents.py::UIStream.end`), so a truncated final line
  becomes an `invalid` block rather than vanishing, then cleared.
  Whitespace-only pending is dropped. On the abnormal path an over-limit flush
  is dropped instead of raising; on the ready path it raises and fails the round.
- **Can an `invalid` block be repaired twice, or repaired after cancellation?**
  Not through storage: `invalid → ready` exactly once, revision `+1`
  (`src/blackrose/storage/repository.py::Repository.repair_block`), and `apply_repair` refuses to stage one
  from any status but `invalid` (H2). Nothing in the repository refuses a repair purely because the reply
  was cancelled — the guard is that `_repair_ui` never runs after Stop
  (`src/blackrose/turns.py::TurnRunner._close_ui_segment`) and that `cancelled` is not `invalid`.
- **Does `save_blocks` make finalised blocks immutable, including
  `source_sequence`?** Yes, by full-payload equality (`src/blackrose/storage/repository.py::Repository.save_blocks`),
  with two caveats: the read shape cannot be written back (H3), and
  `finish_diagram_repair` rewrites finalised `markdown` source, revision and
  sequence (`src/blackrose/storage/repository.py::Repository.finish_diagram_repair`) as a sanctioned exception.
- **Is `ui_state.sequence` monotonic across a repair and a redraw?** Yes, per
  `block_id`: strictly `+1` (`src/blackrose/storage/repository.py::Repository.save_ui_state`), never reset by repair (which touches
  `message_blocks` only) or by a redraw (the join returns the stored value).
  The page's in-memory copy is the weak link (H5).
- **What happens when a round is cancelled while a block is `streaming`?** Stop
  and failure settlement finalise it to `cancelled`/`invalid`
  (`src/blackrose/turns.py::TurnRunner._save_interrupted`); a crash leaves it `streaming` until `reap_pending`
  rewrites it to `cancelled` (`src/blackrose/storage/repository.py::Repository.reap_pending`). Either way the block is
  terminal before any further read.
- **Are block indices stable between streaming saves and final save?** Yes.
  `position` is assigned once from `len(blocks)` (`src/blackrose/ui_documents.py::UIStream._new`), the
  stream never removes a block, and both write paths refuse a `position` change
  (`src/blackrose/storage/repository.py::Repository.save_blocks`, `src/blackrose/storage/repository.py::Repository.repair_block`), so the transcript order seen by the
  reload join (`ORDER BY position`, `src/blackrose/storage/repository.py::_attach_batch`) matches the streaming order.

## Formalization decisions

**H1 and H2 fixed; H3/H4/H5 need their owners.** H1 is fixed in `turns.py`
and H2's status guard in `ui_documents.py`. H3 and H5 belong to the storage
layer and the renderer.

1. **H1, `src/blackrose/turns.py::TurnRunner._repair_ui` — make memory follow the attempted commit.**

   ```python
   candidate = dict(block)
   try:
       ui_documents.apply_repair(candidate, source, profile or 'mixed')
       prior = dict(block)
       block.clear()
       block.update(candidate)          # memory matches the row we are about to write
       try:
           await self.repo.repair_block(assistant.id, candidate)
       except ValueError:
           block.clear()
           block.update(prior)          # storage refused the repair: restore staging
           raise
   except ValueError as error:
       log.debug('UI repair source rejected (%s)', type(error).__name__)
       continue
   ui_stream.publish(force=True)
   ```

   With this, a `CancelledError` raised after the commit leaves memory and row
   agreeing (`ready`, revision 2), so the interrupted settlement's `save_blocks`
   is a no-op instead of a refusal and `finish_message(CANCELLED)` runs; the H1
   test asserts exactly that.
   `test_stop_during_the_repair_request_commits_nothing_and_settles` keeps the
   earlier outcome (`invalid` + `cancelled`, row `CANCELLED`). The rejected
   alternative was to have `_save_interrupted` reconcile the conflicting block by
   re-reading the row — wider and slower.

2. **H2, `src/blackrose/ui_documents.py::apply_repair` — one-line defence in depth.**
   `if block.get('status') != 'invalid': raise ValueError('Only an invalid UI block can be repaired')`
   now runs before `validate_document`. It makes the staging layer agree with
   `src/blackrose/storage/repository.py::Repository.repair_block`, costs nothing, and removes the confusing
   "identity is immutable" error at settlement time. The `source_sequence` half
   of H2 is not addressed.

3. **H3, `src/blackrose/storage/repository.py::Repository.save_blocks` — compare the immutable payload without the read
   projection.** Pop `saved_state`/`state_sequence` from `prior` (or compare
   `{k: v for k, v in prior.items() if k not in ('saved_state', 'state_sequence')}`)
   so a block read from `ui_block`/`message()` can be written back unchanged.
   Invert `test_the_repository_read_shape_is_not_accepted_again_documented_follow_up`.

4. **H5** — either make `save_ui_state` accept an identical repeat
   (`if sequence == current and state == stored: return sequence`) or set
   `record.sequence = block.state_sequence` in `ui-runtime/host.jsx::update`. The
   first changes a storage guarantee, the second is a renderer change; both need
   their owner's decision.

5. **H4** — product decision (new status, or `cancelled` for error settlement);
   not mechanical, deliberately left open.

## Verification

Offline, from the repository root:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_05_interactive_ui_stream.py
.venv/bin/python scripts/check_unit_tests.py -p test_ui_documents.py
.venv/bin/python scripts/check_unit_tests.py -p test_session_lifecycle.py
.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
.venv/bin/python scripts/check_unit_tests.py -p test_sm_02_stored_message_lifecycle.py
.venv/bin/python scripts/check_unit_tests.py -p test_diagram_repairs.py
.venv/bin/python -m ruff check tests/test_sm_05_interactive_ui_stream.py
```

The first command runs the tests that pin this machine's transitions,
invariants and hazards; the interactive-answer policy tests live in
`tests/test_ui_documents.py` (class `UITests`). `test_ui_documents.py` and
`test_session_lifecycle.py` need the generated `ui-runtime` contract from
[checkout setup](../../CONTRIBUTING.md#set-up-a-checkout).

Unverified: H5's lost-reply window (needs a bridge reply cancelled after the row
is written; reasoned from `src/blackrose/storage/db.py::run_to_completion` and
`ui-runtime/host.jsx::flush`/`ui-runtime/host.jsx::update`). H1's cancellation
of the repair commit is driven with a wrapper that awaits the real
`repair_block` and then raises `CancelledError`, which is exactly `run_to_completion`'s
post-cancellation contract; the settlement half runs the real
`_save_interrupted`, and the row outcome was asserted directly. A GUI Stop was
not raced through the real turn to hit the same instruction.
