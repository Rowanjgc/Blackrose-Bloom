# SM-05: Interactive UI stream parser, block lifecycle and widget state sequence

## Scope and ownership

- `src/blackrose/ui_documents.py`: `UIStream` (331–431), `apply_repair`
  (303–309), `validate_document` (136–183), `repaired_source` (284–300),
  `MAX_SOURCE`/`MAX_BLOCKS` (25–27).
- `src/blackrose/storage/records.py`: `UIBlock` (11–33).
- `src/blackrose/storage/repository.py`: `save_blocks` (919–945), `repair_block`
  (947–970), `reap_pending` (884–916), `ui_block` (1045–1055), `save_ui_state`
  (1057–1068), and the sanctioned diagram-rewrite exception
  `claim_diagram_repair`/`finish_diagram_repair` (972–1043).
- `src/blackrose/turns.py`: `_repair_ui` (259–291), `_append` (531–539),
  `_answer` (544–581), `_stream_round` (616–663), `_close_ui_segment` (665–676),
  `_save_interrupted` (775–801), `require_normal_finish` (159–183).
- Coupled boundaries: `gui/session.py` (`_ui_payload` 531–543, `_check_ui_view`
  545–551, `_owned_ui` 553–559, `ui_state` 565–575, `on_ui` 1400–1407),
  `interactive_answers.py` (`owned_block` 45–59, `save_state` 61–70),
  `storage/migrations.py` (`message_blocks` 35–41, `ui_state` 42–46),
  `ui-runtime/host.jsx` (67–97). Ownership per
  [architecture](../architecture.md#modules-and-invariants): `ui_documents.py`
  owns "streaming block assembly and continuation serialization";
  `storage/` owns "immutable finalized UI blocks, sequenced saved state".

Three state machines share one set of rows and must be read together:

1. **The parser** (`UIStream`), owned by `TurnRunner`, one instance per turn,
   mutated only on the application asyncio loop: `_append` is called from the
   stream loop (`turns.py:642`) and from tool callbacks, and `_close_ui_segment`
   runs between rounds. No other task touches it.
2. **The persisted block** (`message_blocks.payload`), owned by `Repository` and
   mutated only inside `Database.run`, which serialises on one thread and rolls
   back a failed callback (`storage/db.py:82–94`). The only desktop process owns
   the database ([ADR 0001](../adr/0001-desktop-ownership.md)).
3. **The widget state row** (`ui_state`), owned by `InteractiveAnswers`
   (`interactive_answers.py:61–70`), which is the only caller of
   `save_ui_state`; the page supplies `state_sequence`, and the repository is the
   authority that rejects it (`repository.py:1060–1063`).

The Python side decides what a block *is*; `ui-runtime/host.jsx` only renders it
and re-reads `source_sequence`/`state_sequence` from the payloads Python sends.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `UIStream.profile` | `ui_documents.py:338` | `str \| None` | `None` = plain markdown round; a contract profile = NDJSON round | `begin` `:347`; `end` `:421` |
| `UIStream.active` | `:337` | `UIBlock \| None` | Markdown block currently receiving text | `begin` `:346`; `append` `:364–365`; `_markdown` `:402–403`; `end` `:422` |
| `UIStream.pending` | `:339` | `str` | Bytes after the last newline in a UI round | `begin` `:348`; `append` `:372–374`; `end` `:408–417` |
| `UIStream.prose` | `:341` | `bool` | Sticky per-round "this round degraded to prose" latch | `begin` `:349` clears; `_line` `:387` sets |
| `UIStream.used` | `:340` | `bool` | Turn emitted (or promised) semantic content; gates publish/persist | `begin` `:350` (`used or profile is not None`) |
| `UIStream.source_bytes` | `:342` | `int` | Cumulative bytes seen in UI-profile rounds of this turn | `append` `:369` |
| `UIStream.blocks` | `:336` | `list[UIBlock]` | Ordered live blocks, append-only | `_new` `:359` |
| `block.position` | `:355` | `int` | `len(blocks)` at creation; the transcript order | never rewritten (`repository.py:933–935`) |
| `block.kind` | `:355` | `'markdown' \| 'interactive_ui'` | Rendering/repair class | `_new` only |
| `block.status` | `:357`, `:398`, `:420`; `repository.py:901` | `'streaming' \| 'ready' \| 'invalid' \| 'cancelled'` | Lifecycle of the block's source | see transition table |
| `block.source_revision` | `:356` | `int` | 1 at creation, +1 per accepted repair | `apply_repair` `:308`; `finish_diagram_repair` `:1038` (markdown only) |
| `block.source_sequence` | `:356` | `int` | Monotone redraw/ordering counter; the page drops a lower one (`host.jsx:93`) | `append` `:367`; `_markdown` `:405`; `apply_repair` `:308`; `finish_diagram_repair` `:1039` |
| `block.source`, `source_hash`, `document`, `fallback_text`, `error` | `:357–358`, `:393–399`, `apply_repair:306–307` | JSON text / dict | Validated source and its renderable fallbacks | `_line`, `apply_repair` |
| `block.diagram_promoted`, `diagram_repairs` | `records.py:32–33`; `repository.py:988`, `1031` | JSON fields | Markdown-only diagram state, unrelated to UI blocks | diagram repair only |
| `message_blocks.payload` | `migrations.py:39` | JSON text | Whole `UIBlock` | `save_blocks` `:942–944`; `repair_block` `:968–969`; `reap_pending` `:898–903` |
| `ui_state.sequence` | `migrations.py:44` | `INTEGER` | Last accepted widget-state sequence, 0 when no row | `save_ui_state` `:1064–1066` |
| `ui_state.state` | `:45` | JSON text | Last accepted widget values | same statement |
| `saved_state`, `state_sequence` | `repository.py:1053–1054`, `:182–190` | injected on read | Join of `ui_state` onto the block shape | reads only |
| `view_id` | `gui/session.py:540` | session-only token | Rotated per redraw; a stale token is refused (`:549`) | `_ui_payload`, never persisted |

## States

### Parser (`UIStream`, one round at a time)

| State | Predicate | Meaning |
| --- | --- | --- |
| `IDLE` | `profile is None`, `pending == ''`, `active is None` | Between rounds, or inside a plain-markdown round: the object has no open/closed flag, so these two are indistinguishable (`end` `:421–422` and `begin(None)` `:346–348` leave the same field values) |
| `MARKDOWN` | `profile is None` and a round is streaming | Every chunk is appended to one `kind='markdown'`, `status='streaming'` block (`:362–368`) |
| `BUFFERING` | `profile is not None and not prose` | Line buffering; only a non-blank line whose first non-space character is `{` may become an interactive block (`:383–399`) |
| `PROSE` | `profile is not None and prose` | Latched degraded mode; every line, valid JSON or not, becomes markdown (`:383–389`) |

`prose` is the only round-scoped field that outlives `end()`, and `begin()` is
the only thing that clears it (`:349`). Within a round it cannot be un-latched.

### Persisted block

| State | Predicate | Notes |
| --- | --- | --- |
| `STREAMING` | `payload.status == 'streaming'` | Only state in which `save_blocks` may replace source (`repository.py:938–941`) |
| `READY` | `'ready'` | Interactive, actionable; the page renders controls only for `ready`/`streaming` and enables them only for `ready` (`host.jsx:21–26`) |
| `INVALID` | `'invalid'` | Created by failed validation (`ui_documents.py:397–399`), by error settlement (`turns.py:779`), or by the reaper (`repository.py:901`). Repairable exactly once |
| `CANCELLED` | `'cancelled'` | Stop settlement (`turns.py:779`) or crash reaping (`repository.py:901`); not repair-eligible |

Two transient, non-persisted states matter for H1: a `STAGED` candidate dict
(produced by `apply_repair`, not yet committed) and the divergence
`MEMORY_STALE` (memory says `INVALID` revision *n* while the row is `READY`
revision *n+1*).

### Widget state

| State | Predicate | Meaning |
| --- | --- | --- |
| `UNSET` | no `ui_state` row; `ui_block` reports `state_sequence == 0` | Page renders contract defaults (`host.jsx:85–86`) |
| `SAVED(n)` | row `sequence = n ≥ 1` | Page state is authoritative and the next accepted edit is `n+1` |

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Round begins | `TurnRunner._answer` → `UIStream.begin(prompt.ui_profile)` (`turns.py:557–558`) |
| E2 | Visible text delta | provider stream → `_append` → `UIStream.append` (`turns.py:642`, `531–539`) |
| E3 | New block appears | `_stream_round` saves all blocks the moment the list grows (`turns.py:640–644`) |
| E4 | Round end | `_close_ui_segment` → `UIStream.end()` (`turns.py:671`) |
| E5 | Round normal-finish check | `require_normal_finish` (`turns.py:568`, `159–183`); refuses before E4 runs, so blocks stay `streaming` in memory |
| E6 | Round blocks persisted | `save_blocks` (`turns.py:672–673`) |
| E7 | One-shot repair request | `_repair_ui` → `repair_text` (`turns.py:267–281`) |
| E8 | Repair commit | `apply_repair` then `repair_block` (`turns.py:282–288`) |
| E9 | Stop | `_run` catch → `_settle_interrupted` (`turns.py:762`) → `_save_interrupted` (`:775–780`) |
| E10 | Failure | same path with `state='error'` (`turns.py:779`) |
| E11 | Startup recovery | `Session.boot` → `recover_interrupted` → `reap_pending` (`chat.py:229–232`; `repository.py:884`) |
| E12 | Widget state edit | page `flush` (`host.jsx:67–80`) → `Session.ui_state` → `InteractiveAnswers.save_state` → `save_ui_state` |
| E13 | Redraw / reload | full transcript or `ui_block` read (`repository.py:182–190`, `1045–1055`) |
| E14 | View rotation | `Session._ui_payload` issues a new `view_id`; `_check_ui_view` refuses any other (`gui/session.py:540`, `545–551`) |
| E15 | Conversation deleted | `ON DELETE CASCADE` from `messages` (`migrations.py:37`, `43`, `49`) |

No other statement writes `payload.status`: a source-wide search finds only
`ui_documents.py:398`, `:420`, `repository.py:901`, `:968`, `turns.py:779`.

## Transition table

### Parser

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | E1 `begin(p)` | — | `active=None`; `profile=p`; `pending=''`; `prose=False`; `used \|= p is not None` (`:345–350`) | `p is None` → `MARKDOWN`, else `BUFFERING` |
| `MARKDOWN` | E2 | — | create `markdown` block at `position=len(blocks)` if `active` is not one; append; `source_sequence += 1` (`:362–368`, `:401–405`) | `MARKDOWN` |
| `MARKDOWN` | E4 `end(s)` | `pending == ''` (always) | no flush; `s` applies to `streaming` blocks only; `profile=None`; `active=None`; `publish(force=True)` (`:407–423`) | `IDLE` |
| `BUFFERING` | E2 | `source_bytes + len(part) ≤ 65536` | `source_bytes += len`; `pending += part`; each complete line → `_line`; throttled publish (`:369–376`) | `BUFFERING` |
| `BUFFERING` | E2 | budget exceeded | `ValueError('UI source exceeds the 64 KiB turn budget')` (`:369–371`) | `BUFFERING` (round fails) |
| `BUFFERING` | line | blank (`.strip()` empty) | ignored; no block (`:379–382`) | `BUFFERING` |
| `BUFFERING` | line | first non-space char is `{` and `interactive_ui` count `< 12` | new `interactive_ui` block `status='streaming'`; validate → `document`/`fallback_text`, else `status='invalid'` + `error` + readable fallback (`:390–399`) | `BUFFERING` |
| `BUFFERING` | line | 12 interactive blocks already | `ValueError('UI reply exceeds 12 blocks')` (`:390–391`) | `BUFFERING` (round fails) |
| `BUFFERING` | line | non-blank and not `{`-started | `prose=True`; new/extended `markdown` block with `line+'\n'` (`:383–389`) | `PROSE` |
| `PROSE` | line | any line | blank → `'\n'` appended to markdown; otherwise `line+'\n'` (`:380–388`) | `PROSE` |
| `PROSE` | line | — | a valid JSON line is markdown, never a block (`:383`) | `PROSE` |
| `BUFFERING`/`PROSE` | E4 `end('ready')` | leftover `pending.strip()` | flush through `_line`; a `ValueError` propagates, leaving the round open (`:408–410`) | `IDLE` only on success |
| `BUFFERING`/`PROSE` | E4 `end(s≠'ready')` | leftover `pending.strip()` | flush through `_line`, `ValueError` swallowed (`:411–416`) | `IDLE` |
| any open | E4 | — | every `status == 'streaming'` block becomes `s`; terminal blocks untouched (`:418–420`); `pending=''`; `profile=None`; `active=None`; publish | `IDLE` |
| `IDLE` | E4 again | — | no-op: `profile is None` and no streaming blocks | `IDLE` |

### Persisted block

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| — | `save_blocks` | owner is an assistant message, ids match (`:923–928`) | INSERT at the caller's status — a first write is `streaming` or `invalid`, never terminal by policy | `STREAMING`/`INVALID` |
| `STREAMING` | `save_blocks` | identity keys equal (`:933–935`), `source_sequence ≥ prior` (`:936–937`) | replace payload (`:942–944`) | `STREAMING` |
| `STREAMING` | `save_blocks` | identity differs | `ValueError('UI block identity is immutable')`; the whole call rolls back (`:921–922`) | `STREAMING` |
| `STREAMING` | `save_blocks` | lower `source_sequence` | `ValueError('Stale UI source sequence')` | `STREAMING` |
| `STREAMING` | E4+E6 ready round | — | `status='ready'` written at E6 | `READY` |
| `STREAMING` | E9 Stop | — | `end('cancelled')` then save (`turns.py:779–780`) | `CANCELLED` |
| `STREAMING` | E10 failure | — | `end('invalid')` then save | `INVALID` |
| `STREAMING` | E11 crash | row still `streaming` (`repository.py:898–903`) | status rewritten to `cancelled`, payload otherwise byte-identical | `CANCELLED` |
| `INVALID` | E8 `repair_block` | prior `invalid`, new `ready`, identity equal, revision exactly +1 (`:959–965`) | replace payload; `source_sequence` +1 by `apply_repair` (`ui_documents.py:308`) | `READY` |
| `INVALID` | `save_blocks` | prior not `streaming` (`:938`) | identical payload → no write; any change → `ValueError('Finalized UI source is immutable')` (`:939–941`) | `INVALID` |
| `READY` | `save_blocks` | as above | identical → no-op; change → refused | `READY` |
| `READY`/`CANCELLED` | E8 | prior not `invalid` (`:959`) | `ValueError('Only an invalid UI block can be repaired')` | unchanged |
| `INVALID` | E8 | revision ≠ prior+1 | `ValueError('UI repair must advance the source revision once')` | `INVALID` |
| `READY` (markdown only) | `claim`/`finish_diagram_repair` | `kind == 'markdown'` (`:993`), claim still `repairing` and source matches (`:1032`) | rewrite `source`, `source_revision += 1`, `source_sequence += 1` (`:1034–1039`) | `READY` |
| `READY` | E13 | — | read only; `saved_state`/`state_sequence` merged onto the returned dict (`:188–190`) | `READY` |
| any | E15 | — | row deleted | gone |

`finish_diagram_repair` is the one sanctioned rewrite of a finalised block, and it
is confined to `markdown`; no path rewrites an `interactive_ui` block except
`repair_block`, which requires `invalid`.

### Widget state

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNSET` | E12 | `sequence == 1`, block row exists | INSERT (`repository.py:1064–1066`) | `SAVED(1)` |
| `SAVED(n)` | E12 | `sequence == n+1` | replace `sequence`, `state` | `SAVED(n+1)` |
| `SAVED(n)` | E12 | any other `sequence` | `ValueError('Stale UI state sequence')` (`:1062–1063`) | `SAVED(n)` |
| `UNSET` | E12 | block_id absent | `sqlite3.IntegrityError` from the foreign key (`migrations.py:43`) | `UNSET` |
| `SAVED(n)` | E8 repair | block stays the same `block_id` | no `ui_state` write; the row is untouched | `SAVED(n)` |
| `SAVED(n)` | E13 redraw | — | join returns `n` and the saved values | `SAVED(n)` |
| `SAVED(n)` | E14 view rotation | — | the page gets a new `view_id` and the same `state_sequence` | `SAVED(n)` |

## Invariants

1. **The prose latch is per round and irreversible inside one.** Once a
   non-blank non-`{` line appears, no later line in that round can create an
   interactive block (`ui_documents.py:383–389`); only `begin` clears it (`:349`).
   Pinned by `test_prose_latch_survives_valid_ui_lines_until_the_next_round`.
2. **Block order is creation order and never renumbered.** `position` is
   `len(blocks)` at creation (`:355`), blocks are append-only, and
   `save_blocks`/`repair_block` refuse a `position` change
   (`repository.py:933–935`, `961–963`). Pinned by
   `test_block_positions_are_creation_order_across_rounds`,
   `test_save_blocks_is_atomic_and_checks_ownership`.
3. **The parser never mutates a finalised block.** Both `begin` (`:346`) and
   `end` (`:422`) clear `active`, and `_markdown` only reuses `active` when it is
   a markdown block (`:402`), so a `ready` block is never appended to. Pinned by
   `test_markdown_rounds_create_no_semantic_blocks_and_never_publish` (a second
   round creates a new block rather than growing the closed one).
4. **`end()` is the only finaliser, and it never reclassifies a terminal
   block.** `:418–420` filters on `streaming`, so an `invalid` block keeps its
   status even when the abort status is `cancelled`. Pinned by
   `test_end_applies_the_abort_status_only_to_streaming_blocks`.
5. **No pending byte survives a round.** `end` flushes among any status and
   clears `pending` (`:408–417`); whitespace-only pending is dropped. Pinned by
   `test_round_end_flushes_pending_for_every_status`.
6. **A ready round fails loudly on overflow; an aborting round degrades
   quietly.** The 13th block raises on the `ready` path and is dropped on the
   abnormal path (`:408–416`). Pinned by
   `test_over_limit_final_block_fails_a_ready_round_and_is_dropped_on_abort`.
7. **`source_sequence` never decreases for a block.** `save_blocks` refuses a
   lower value (`repository.py:936–937`); `apply_repair` and diagram repair add
   exactly one (`ui_documents.py:308`, `repository.py:1039`). Pinned by
   `test_finalized_source_is_immutable_including_the_source_sequence`.
8. **A finalised block is byte-identical to what was accepted.** While
   `prior.status != 'streaming'`, only a payload equal to the stored one is
   accepted (`repository.py:938–941`); this is what covers `source_sequence`,
   `source`, `document`, `fallback_text` and `position` together. Pinned by
   `test_finalized_source_is_immutable_including_the_source_sequence`.
9. **`save_blocks` is atomic per call.** The loop runs inside `with conn`
   (`repository.py:921–922`), so one refused block writes nothing — a new block
   listed before the refused one is not inserted. Pinned by
   `test_save_blocks_is_atomic_and_checks_ownership`.
10. **Repair is one-shot and forward-only.** Only `invalid → ready` is accepted,
    with exactly `revision+1` and unchanged identity (`repository.py:959–965`), so
    a second repair of the same block is refused at storage even though
    `apply_repair` would stage it. Pinned by
    `test_repair_is_one_shot_from_invalid_only`,
    `test_repair_guards_revision_identity_and_cancelled_blocks`.
11. **A `cancelled` block is never repaired.** Same guard (`:959`); repair only
    runs inside a completed UI segment (`turns.py:674–676`), which cannot be
    reached after Stop. Pinned by
    `test_repair_guards_revision_identity_and_cancelled_blocks`.
12. **`ui_state.sequence` is strictly `+1`, per immutable `block_id`, and is
    untouched by repair and redraw.** `block_id` never changes
    (`repository.py:961–963`), `save_ui_state` only writes `current+1`
    (`:1060–1066`), and `repair_block` never touches `ui_state`. Pinned by
    `test_ui_state_sequence_is_monotonic_across_repair_and_redraw`.
13. **`ui_state` cannot reference a missing block.** Foreign key with cascade
    (`migrations.py:43`). Pinned by `test_ui_state_cannot_orphan_a_missing_block`.
14. **An invalid block has no document and no widget state.** `document` is set
    only after validation (`ui_documents.py:394–396`), and the policy gate
    accepts state/actions only for `ready`/`streaming` with a supported runtime
    (`interactive_answers.py:52–58`); `save_state` also refuses a null document
    (`:65–67`). Therefore `state_sequence == 0` for every invalid block and a
    repair re-initialises the page widget from the repaired document
    (`host.jsx:85–86,93–95`). Pinned by
    `test_invalid_blocks_are_refused_by_the_answers_policy_until_repaired`.
15. **A crash leaves no streaming block.** Startup reaping rewrites every
    `streaming` payload to `cancelled` and is idempotent (`repository.py:898–903`);
    a second reap returns 0 and rewrites nothing else. Pinned by
    `test_reap_cancels_streaming_blocks_once_and_leaves_terminal_blocks_alone`.
16. **A streaming block implies a pending reply.** Every round closes its blocks
    (`turns.py:671–673`) before `_complete` can write `COMPLETE`
    (`turns.py:722`, called at `:357`; `repository.py:767`), so a `streaming`
    block on a settled reply is only reachable through the unguarded first insert
    in `save_blocks`.

## Hazards and open questions

- **H1 — `defect` — `turns.py:282–291` (with `storage/db.py:20–35`).** Stop can
  land inside `await self.repo.repair_block(...)`. `Database._worker` keeps
  waiting for the worker thread after cancellation and only then re-raises
  `CancelledError` (`db.py:24–35`), so the repair **is committed** while the
  caller never reaches `block.clear(); block.update(candidate)` (`turns.py:289–290`).
  The interrupted settlement then calls `end('cancelled')` and `save_blocks`
  (`turns.py:778–780`) with the block still `invalid` revision 1 while the row is
  `ready` revision 2; the identity check refuses it
  (`repository.py:933–935`), the raise happens **before** `finish_message`
  (`turns.py:780` precedes `:791`), and the reply row stays `pending`. The
  `ValueError` replaces the `CancelledError` escaping `_run`, so the window
  reports the reply as an *error* whose text is the internal message
  `UI block identity is immutable` (`gui/session.py:1457–1463`), and the next
  start reaps the pending row as a crash (reaper error text,
  `repository.py:905–913`). Demonstrated by
  `test_stop_after_the_repair_commit_breaks_settlement_documented_follow_up`,
  which drives the real `_repair_ui` and `_save_interrupted`: the committed row
  is `ready` revision 2, the stream block is still `invalid` revision 1,
  `_save_interrupted` raises, and the reply row is still `PENDING`. Window is one
  local write wide, but deterministic when reached. Fix in "Formalization
  decisions".
- **H2 — `fragile` — `ui_documents.py:303–309`.** `apply_repair` stages a repair
  onto any dict: it does not check `status`, so it happily produces
  `ready`/`revision+1` from a `ready`, `cancelled` or already-repaired block, and
  also accepts a lower `source_sequence` than storage holds. The authoritative
  guards live one layer away (`repository.py:959–965`) and `save_blocks`'s
  monotonicity check (`:936`) has no counterpart in `repair_block`. Today the
  only caller filters on `status == 'invalid'` (`turns.py:265`), so this is a
  caller-only invariant; the split is visible in
  `test_repair_is_one_shot_from_invalid_only` (staging a second repair succeeds,
  storage refuses it). Confirming a real impact needs a second caller; the
  cheapest hardening is the one-line status guard in "Formalization decisions".
- **H3 — `fragile` — `repository.py:1045–1055`/`182–190` vs `938–941`.** The
  repository injects `saved_state`/`state_sequence` when it reads a block, then
  treats the whole payload as the immutable unit when it writes one. A block
  round-tripped through `ui_block` or `message()` can therefore never be written
  back, and the failure reads as `Finalized UI source is immutable`. Existing
  tests work around it by deleting those keys
  (`tests/test_ui_documents.py:523–524`). Demonstrated by
  `test_the_repository_read_shape_is_not_accepted_again_documented_follow_up`.
  No product path re-saves a block it read, so this is a trap for future code,
  not a live corruption.
- **H4 — `documentation-gap` — `turns.py:779` with `ui_documents.py:418–420`.**
  On error settlement every `streaming` block becomes `invalid`, including
  blocks whose document validated and rendered. `invalid` therefore means both
  "source failed validation" and "the reply failed after this block"; the
  latter blocks keep a non-null `document` and no `error`, and can never be
  repaired (repair requires a UI segment round, `turns.py:674–676`), so they stay
  permanently non-interactive while rendering normally. Demonstrated by
  `test_error_settlement_labels_valid_blocks_invalid`. Whether to add a fifth
  status or to use `cancelled` for error settlement is a product decision, not
  a mechanical fix.
- **H5 — `fragile` — `repository.py:1057–1068` with `ui-runtime/host.jsx:67–80`.**
  `save_ui_state` is deliberately not idempotent: a repeated sequence is
  `Stale`. The page advances `record.sequence` only from a successful reply
  (`host.jsx:77`) and retries the *same* `sequence + 1` after any failure
  (`:79`), and `update()` never refreshes `record.sequence` from an incoming
  payload (`:93–95`). A committed-but-unacknowledged save (cancellation after
  commit is possible for the same `_worker` reason as H1) therefore wedges that
  widget's saves until the page re-mounts. Not executed: the window needs a
  bridge reply lost after the row was written. Confirm with a probe that
  cancels `Session.ui_state` after `save_ui_state` returns and then retries.
  Either side can be fixed: make the retry idempotent for an identical
  `(sequence, state)`, or have `update()` set `record.sequence` from
  `block.state_sequence`.
- **H6 — `documentation-gap` — `repository.py:919–945`.** `save_blocks` does not
  validate the status vocabulary, nor that a first insert is `streaming`, nor
  that a stream is finalised before the owning message settles. The model is
  enforced by `turns.py` discipline plus the equality check, not by storage.
  Unreachable from product code (the only caller is `TurnRunner`), so no fix is
  proposed.
- **H7 — `documentation-gap` — `repository.py:942–944` with
  `migrations.py:40`.** Two blocks claiming the same `position` surface as
  `sqlite3.IntegrityError` from `UNIQUE(message_id, position)`, not as a
  `ValueError`; `save_ui_state` likewise leaks `IntegrityError` for a missing
  block (pinned by `test_ui_state_cannot_orphan_a_missing_block`) even though
  every other refusal in this area is a `ValueError` that `Session` catches.
  Unreachable: positions come from `len(blocks)` and block ids from
  `secrets.token_hex` (`ui_documents.py:353–355`), and `save_ui_state` is only
  called after `owned_block` resolved the row.
- **H8 — `fragile` — `turns.py:259–291`.** The repair budget
  (`config.interactive_ui_repairs`, default 1, maximum 2 —
  `config.py:180`, `:242`) is consumed per *attempt*, not per success, and the
  loop scans every block in the stream. Only one UI segment exists per turn
  (`turns.py:559–562` clears `prompt.ui_profile` after the first round), so no
  block can be retried by a later round; if a future change allows a second UI
  segment, the budget semantics would need revisiting. Pinned by
  `test_repair_budget_targets_only_invalid_interactive_blocks`.
- **H9 — `none-found` — `repository.py:884–916`.** The reaper's block query is
  not scoped to pending messages, but no product path settles a message while a
  block is `streaming` (invariant 16), so the wider scope cannot convert a live
  turn's blocks.

### Answers to the focus questions

- **Can `prose=True` be un-latched inside a round?** No. Only `begin` clears it
  (`ui_documents.py:349`), and `begin` runs once per round (`turns.py:557–558`).
  `end` deliberately leaves it set (`:407–423`); the next `begin` clears it.
- **What happens to `pending` at round end and on abort?** Flushed through the
  same `_line` path for any status (`:408–416`), so a truncated final line
  becomes an `invalid` block rather than vanishing, then cleared (`:417`).
  Whitespace-only pending is dropped. On the abnormal path an over-limit flush
  is dropped instead of raising; on the ready path it raises and fails the round.
- **Can an `invalid` block be repaired twice, or repaired after cancellation?**
  Not through storage: `invalid → ready` exactly once, revision `+1`
  (`repository.py:959–965`). `apply_repair` itself would stage a second repair
  (H2), and nothing in the repository refuses a repair purely because the reply
  was cancelled — the guard is that `_repair_ui` never runs after Stop
  (`turns.py:674–676`) and that `cancelled` is not `invalid`.
- **Does `save_blocks` make finalised blocks immutable, including
  `source_sequence`?** Yes, by full-payload equality (`repository.py:938–941`),
  with two caveats: the read shape cannot be written back (H3), and
  `finish_diagram_repair` rewrites finalised `markdown` source, revision and
  sequence (`:1034–1039`) as a sanctioned exception.
- **Is `ui_state.sequence` monotonic across a repair and a redraw?** Yes, per
  `block_id`: strictly `+1` (`:1060–1066`), never reset by repair (which touches
  `message_blocks` only) or by a redraw (the join returns the stored value).
  The page's in-memory copy is the weak link (H5).
- **What happens when a round is cancelled while a block is `streaming`?** Stop
  and failure settlement finalise it to `cancelled`/`invalid`
  (`turns.py:777–780`); a crash leaves it `streaming` until `reap_pending`
  rewrites it to `cancelled` (`repository.py:898–903`). Either way the block is
  terminal before any further read.
- **Are block indices stable between streaming saves and final save?** Yes.
  `position` is assigned once from `len(blocks)` (`ui_documents.py:355`), the
  stream never removes a block, and both write paths refuse a `position` change
  (`repository.py:933–935`, `961–963`), so the transcript order seen by the
  reload join (`ORDER BY position`, `:182–190`) matches the streaming order.

## Formalization decisions

**H1 and H2 fixed; H3/H4/H5 need their owners.** H1 (in `turns.py`) and H2 (in
`ui_documents.py`) were applied by the orchestrator once the concurrent beads
had landed; the H1 characterization test was inverted to
`test_stop_after_the_repair_commit_still_settles_the_reply` and H2 now has its
own refusal test. H3/H5 remain one layer away from the file this bead owns.
Patches are kept below for review.

1. **H1, `turns.py:282–291` — make memory follow the attempted commit.**

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
   is a no-op instead of a refusal and `finish_message(CANCELLED)` runs. Invert
   `test_stop_after_the_repair_commit_breaks_settlement_documented_follow_up`
   (it must then assert `save_blocks` succeeds and the row settles
   `CANCELLED`); `test_stop_during_the_repair_request_commits_nothing_and_settles`
   keeps today's outcome (`invalid` + `cancelled`, row `CANCELLED`). Alternative,
   if `turns.py` is not available: have `_save_interrupted` reconcile the
   conflicting block by re-reading the row — wider and slower, not recommended.

2. **H2, `ui_documents.py:303` — one-line defence in depth.** Add
   `if block.get('status') != 'invalid': raise ValueError('Only an invalid UI block can be repaired')`
   before `validate_document`. It makes the staging layer agree with
   `repository.py:959`, costs nothing, and removes the confusing
   "identity is immutable" error at settlement time. Not applied here because
   no current caller can reach the unguarded path, and this bead's rule is to
   change source only for a demonstrated hazard.

3. **H3, `repository.py:938` — compare the immutable payload without the read
   projection.** Pop `saved_state`/`state_sequence` from `prior` (or compare
   `{k: v for k, v in prior.items() if k not in ('saved_state', 'state_sequence')}`)
   so a block read from `ui_block`/`message()` can be written back unchanged.
   Invert `test_the_repository_read_shape_is_not_accepted_again_documented_follow_up`.

4. **H5** — either make `save_ui_state` accept an identical repeat
   (`if sequence == current and state == stored: return sequence`) or set
   `record.sequence = block.state_sequence` in `ui-runtime/host.jsx:93–95`. The
   first changes a storage guarantee, the second is a renderer change; both need
   their owner's decision.

5. **H4** — product decision (new status, or `cancelled` for error settlement);
   not mechanical, deliberately left open.

## Verification

Offline, from the repository root, with `.venv/bin/python`:

```sh
scripts/check_unit_tests.py -p test_sm_05_interactive_ui_stream.py -v
# Ran 20 tests ... OK
scripts/check_unit_tests.py -p test_ui_documents.py
# Ran 37 tests ... OK
scripts/check_unit_tests.py -p test_session_lifecycle.py
# Ran 26 tests ... OK
scripts/check_unit_tests.py -p test_storage.py
# Ran 17 tests ... OK
scripts/check_unit_tests.py -p test_turns.py
# Ran 28 tests ... OK
scripts/check_unit_tests.py -p test_sm_02_stored_message_lifecycle.py
# Ran 14 tests ... OK
scripts/check_unit_tests.py -p test_diagram_repairs.py
# Ran 11 tests ... OK
.venv/bin/ruff check tests/test_sm_05_interactive_ui_stream.py
# All checks passed!
```

`tests/test_interactive_answers.py` named by the bead does not exist in this
checkout; the interactive-answer policy tests live in `tests/test_ui_documents.py`
(class `UITests`, 37 tests, run above). No source file was modified, so no other
module's suite was re-run beyond the focused set above; the full
`scripts/check_health.py` gate was not run because sibling agents are editing
shared files in this worktree during the same session.

Unverified: H5's lost-reply window (needs a bridge reply cancelled after the row
is written; reasoned from `db.py:20–35` and `host.jsx:67–97`). H1's cancellation
of the repair commit is reproduced with a wrapper that awaits the real
`repair_block` and then raises `CancelledError`, which is exactly `_worker`'s
post-cancellation contract (`db.py:24–35`); the settlement half runs the real
`_save_interrupted`, and the row outcome was asserted directly. A GUI Stop was
not raced through the real turn to hit the same instruction.
