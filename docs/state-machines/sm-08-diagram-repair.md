# SM-08: Failure-driven diagram syntax repair

## Scope and ownership

| Surface | Location |
| --- | --- |
| `DiagramRepairs.repair` (claim → inference → settle) | `src/blackrose/diagram_repairs.py::DiagramRepairs.repair` |
| Durable claim and finish | `src/blackrose/storage/repository.py::Repository.claim_diagram_repair`, `src/blackrose/storage/repository.py::Repository.finish_diagram_repair` |
| Browser report intake and deferred scheduling | `src/blackrose/gui/session.py::Session.diagram_failed`, `src/blackrose/gui/session.py::Session._repair_diagrams`, `src/blackrose/gui/session.py::Session._wait_for_repair_slot` |
| Stop/close cancellation and their updates | `src/blackrose/gui/session.py::Session.close`, `src/blackrose/gui/session.py::Session.stop` |
| Figure failure detection and one-shot report | `src/blackrose/gui/static/diagrams.js::mount`, `src/blackrose/gui/static/diagrams.js::settled`, `src/blackrose/gui/static/diagrams.js::repairResult` |
| Bridge call and result dispatch | the `failed` callback in `src/blackrose/gui/static/bloom.js::renderHTML`, `src/blackrose/gui/static/bloom.js::on.diagram_update`; `src/blackrose/gui/bridge.py::Api.diagram_failed` |
| Promotion rendering | `src/blackrose/gui/render.py::_render_fence`, `src/blackrose/gui/render.py::message_payload`; `src/blackrose/gui/session.py::Session._message_payloads`, `src/blackrose/gui/session.py::Session._ui_payload` |
| Config bound | `src/blackrose/config.py::Config.diagram_repairs`, checked in `Config.__post_init__` |
| User-facing contract | [automatic syntax repair](../diagrams.md#automatic-syntax-repair); [architecture](../architecture.md#interactive-answers-library-review-and-recovery) |

One `DiagramRepairs` per `ChatController` (created in
`src/blackrose/chat.py::ChatController.__init__`); one `Session` per window and one
GTK/WebKit page ([architecture](../architecture.md#runtime-ownership-and-code-health)). The single asyncio
loop owns the claim, the inference and the Session queue; the SQLite worker
(`src/blackrose/storage/db.py::Database.run`) serializes all payload reads and writes, and the desktop
flock (`src/blackrose/instance.py::desktop_instance`) forbids a second process on the same database
(`docs/adr/0001-desktop-ownership.md`).

The shape of the ownership: the *page* decides only that a render failed (it does
not know Mermaid validity in Python); the *Session* owns schedule, scope and
delivery; the *repository* owns the durable claim, the budget and the source
revision; `DiagramRepairs` owns the one inference and its settlement. The raw
assistant message (`messages.content`) is never rewritten — only the promoted or
existing `markdown` block (`message_blocks.payload['source']`), which is what
future history uses
(`tests/test_diagram_repairs.py::DiagramRepairTests.test_failure_repairs_only_source_preserves_draft_and_original_and_updates_history`).

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `payload['diagram_repairs'][str(index)]` | `src/blackrose/storage/repository.py::Repository.claim_diagram_repair`, `src/blackrose/storage/repository.py::Repository.finish_diagram_repair` | JSON object `{status, source}` | per-diagram attempt record inside one block | `claim_diagram_repair`, `finish_diagram_repair` |
| `payload['diagram_repairs'][i].status` | `src/blackrose/storage/repository.py::Repository.claim_diagram_repair`, `src/blackrose/storage/repository.py::Repository.finish_diagram_repair` | enum `repairing\|updated\|failed\|stopped\|interrupted` | durable attempt state | claim, finish |
| `payload['diagram_repairs'][i].source` | `src/blackrose/storage/repository.py::Repository.claim_diagram_repair` | str ≤ 20 000 | the exact fence body that failed; binds a finish to its claim | claim |
| `payload['block_id']` | `src/blackrose/storage/repository.py::Repository.claim_diagram_repair` | str (≤128 through the bridge) | block identity; `diagram-{message_id}` when promoted | promotion (claim), turn |
| `payload['diagram_promoted']` | `src/blackrose/storage/repository.py::Repository.claim_diagram_repair` | bool | this block is a plain reply promoted for repair | claim only |
| `payload['source']` | `src/blackrose/storage/repository.py::Repository.finish_diagram_repair` | str | Markdown with the fence bodies; the display/history source | `finish(updated)`; turn streaming |
| `payload['source_hash']`, `['fallback_text']` | `src/blackrose/storage/repository.py::Repository.finish_diagram_repair` | str | hash and readable fallback of `source` | `finish(updated)` |
| `payload['source_revision']`, `['source_sequence']` | `src/blackrose/storage/repository.py::Repository.finish_diagram_repair` | int | monotonic revision and UI sequence; +1 per applied repair | `finish(updated)` |
| `messages.state` | read in `src/blackrose/storage/repository.py::Repository.claim_diagram_repair`; written by `src/blackrose/storage/repository.py::Repository.finish_message` | `MessageState` | only `complete` is claimable | `finish_message`, `_reap` |
| `Session._pending_diagram_failures` | `src/blackrose/gui/session.py::Session.__init__` | dict `(cid, mid, block, index, source) → error`, ≤ 8 | queued reports not yet claimed | `diagram_failed`, `stop`, `close`, `_repair_diagrams` |
| `Session._diagram_repair_task` | `src/blackrose/gui/session.py::Session.__init__` | `asyncio.Task \| None` | the single drain task | `diagram_failed` (spawn), `_repair_diagrams` (clear) |
| `Session._generation` | `src/blackrose/gui/session.py::Session._wait_for_repair_slot`, `src/blackrose/gui/session.py::Session._repair_diagrams` | `asyncio.Task \| None` | the shared reply/repair slot | `_wait_for_repair_slot`, Session |
| `ChatController.is_generating` | `src/blackrose/chat.py::ChatController.__init__`; `src/blackrose/diagram_repairs.py::DiagramRepairs.repair` | bool | in-process one-effect gate | turn, repair |
| `ArtifactLifecycle._lock` / `.busy` | `src/blackrose/artifacts.py::ArtifactLifecycle.busy`, `src/blackrose/artifacts.py::ArtifactLifecycle.exclusive` | async lock | excludes lifecycle commands from a repair | context manager |
| `Config.diagram_repairs` | `src/blackrose/config.py::Config.diagram_repairs`, `Config.__post_init__` | int 0..2 | attempts allowed per *message* | user config |
| figure `reported`, `failure` | `src/blackrose/gui/static/diagrams.js::mount` | JS closure | one report per DOM node; last render error | `mount` |
| figure `_diagramRepairSettled`, `_diagramMounted` | `src/blackrose/gui/static/diagrams.js::mount` | JS | terminal text already shown; mount latch | `repairResult`, `mount` |
| renderer `broken`, cache | `src/blackrose/gui/static/diagrams.js::renderer`, `src/blackrose/gui/static/diagrams.js::remember`, `src/blackrose/gui/static/diagrams.js::render` | JS | latched renderer failure; source → SVG cache | renderer |

## States

**Persisted diagram-attempt states** (one per `(block, index)`, mutually
exclusive by `status`):

| State | Predicate |
| --- | --- |
| `Absent` | no `diagram_repairs[str(index)]` entry |
| `Repairing` | entry with `status == 'repairing'` |
| `Updated` | `status == 'updated'` (a replacement was applied) |
| `Failed` | `status == 'failed'` (exception, unchanged output, or the repaired source also failed) |
| `Stopped` | `status == 'stopped'` (cancelled through `settle`) |
| `Interrupted` | `status == 'interrupted'` (re-claimed while `repairing`, i.e. crash/duplicate recovery) |

**Ephemeral claim outcomes** (`claim_diagram_repair` return, never persisted):
`repairing`, `unavailable`, `stale`, `disabled`, `exhausted`, and the terminal
status echoed back by a re-claim (`updated` is never echoed: re-claiming an
`updated` entry yields `failed`).

**Session queue states**: `Empty`; `Queued(n)` (keys held, no claim);
`Draining(key)` (the task owns `_generation` and is inside `repair`).

**Browser figure states** (`diagrams.js`): `PendingRender`; `Rendered`;
`RenderFailed` (repairable error, not yet reported); `Reported` (call in flight
or `queued` text shown); `Settled` (terminal text shown); `Unrepairable`
(non-repairable error, no message id, or message not `complete`).

**Renderer states**: `Warm`; `Broken` (a timeout or frame error latches
`broken` in `src/blackrose/gui/static/diagrams.js::renderer`, and `src/blackrose/gui/static/diagrams.js::render` rejects with it).

## Events

- A visible figure's render rejects with `error.repairable === true`
  (`src/blackrose/gui/static/diagrams.js::mount`; `mermaid-host.html` decides repairability).
- `settled(root)` after a reply settles (`src/blackrose/gui/static/bloom.js::finishReply` → `src/blackrose/gui/static/diagrams.js::settled`),
  which gives a not-yet-visible or earlier-failed figure a second report chance.
- Bridge `diagram_failed(message_id, block_id, index, source, error)`
  (`src/blackrose/gui/bridge.py::Api.diagram_failed` → `src/blackrose/gui/session.py::Session.diagram_failed`).
- Session drain start, boot completion, reply-slot acquisition
  (`src/blackrose/gui/session.py::Session._repair_diagrams`).
- Repository claim (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`) and finish (`src/blackrose/storage/repository.py::Repository.finish_diagram_repair`).
- Inference completion, exception, cancellation, unchanged replacement
  (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`); `settle` keeps a cancellation from interrupting
  the settlement (`src/blackrose/artifacts.py::settle`).
- Stop (`src/blackrose/gui/session.py::Session.stop`), close (`src/blackrose/gui/session.py::Session.close`), navigation,
  page reload, process death.
- Config change; message deletion.

## Transition table

Durable attempt machine. Claim guards and effects are in
`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`; finish
guards and effects in `Repository.finish_diagram_repair`:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Absent` | claim, empty `block_id` | message is `assistant`+`complete` in this conversation (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`) and has **no** blocks | synthesize `diagram-{message_id}`, position 0, `diagram_promoted: True`, source = message content | claim continues |
| `Absent` | claim, empty `block_id`, message already has blocks | the no-blocks test is false | `target is None` → `unavailable` | `Absent` |
| `Absent` | claim | block missing, not `markdown`, or index out of range / fence body ≠ reported source | `unavailable` / `stale` | `Absent` |
| `Absent` | claim on a promoted block with empty `block_id` | exactly one block and `diagram_promoted` — resolved by `src/blackrose/diagram_repairs.py::DiagramRepairs.repair` before the claim | claim as if the page had named `diagram-{id}` | `Repairing` or refusal |
| `Absent` | claim | `used = Σ len(repairs)` over all blocks of the message, `used >= limit` | `disabled` when `limit == 0`, else `exhausted`; no write | `Absent` |
| `Absent` | claim, all guards pass | — | `repairs[index] = {repairing, source}`; whole payload upserted | `Repairing` |
| `Repairing` | claim, same index, same source | entry exists | `status := interrupted`; payload written | `Interrupted` |
| `Repairing` | finish(`updated`, replacement) | row exists, `status == 'repairing'`, `source` equals the claim | `replace_diagram` body only; hash/fallback; revision+1, sequence+1; `status := updated` | `Updated` |
| `Repairing` | finish(`failed`) — exception, invalid output (`repaired_source` raises), or unchanged output (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`) | same guard | `status := failed`; source untouched | `Failed` |
| `Repairing` | finish(`stopped`) — `CancelledError` (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`) | same guard | `status := stopped`; source untouched | `Stopped` |
| `Repairing` | finish after a re-claim, block deletion, or a different claim source | guard fails | **silent no-op**, caller receives `None` | unchanged |
| `Repairing` | process death | — | entry stays committed | `Repairing` (until the next claim) |
| `Updated` | claim, same index, source = the **new** fence body | token matches | `status := failed`, no inference | `Failed` |
| `Failed` / `Stopped` / `Interrupted` | claim, same index, same source | entry exists | payload rewritten with the same status, same value | unchanged (terminal) |
| `Failed` / `Stopped` / `Interrupted` | claim, same index, different source | the fence-body comparison fails first | `stale` | unchanged |

`Updated → Failed` is the only non-terminal→terminal transition besides a
finish; it happens when the browser renders the repaired source, fails again and
reports that new source at the same fence index. It runs **before** the budget
check in `claim_diagram_repair`, so it costs no attempt and no request
(`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`). A third failure re-claims `Failed → Failed` and
still spends nothing
(`test_persisted_states_are_closed_and_every_terminal_state_stays_put`).

Session queue. Intake guards are in `Session.diagram_failed`; draining is
`Session._repair_diagrams`:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Empty` | `diagram_failed` | config ≠ 0, types/bounds, ready, not quitting, no permission prompt (`src/blackrose/gui/session.py::Session.diagram_failed`), selected chat | `setdefault(key, error)`; spawn task if none running | `Queued(1)` |
| `Queued(n)` | same key (same chat, message, block, index, **source**) | — | `setdefault` no-op; error text of the first report kept | unchanged |
| `Queued(8)` | new key | `len >= 8` | `unavailable`; report dropped, page shows unavailable | unchanged |
| `Queued(n)` | task starts | `_boot_task` pending | await it | unchanged |
| `Queued(n)` | boot settled | `_wait_for_repair_slot` false (not ready, quitting, permission pending) | emit `unavailable` for every key | `Empty` |
| `Queued(n)` | slot acquired | — | `_generation = task`; head key stays in the map while `repair` runs | `Draining(head)` |
| `Draining(key)` | `repair` returns | — | pop key, emit `diagram_update(..., repair=result)` | `Queued(n-1)` / `Empty` |
| `Draining(key)` | navigation | selected chat ≠ key chat | pop key, **no** update | `Queued(n-1)` |
| `Draining(key)` | `repair` raises (`ChatBusy` from a busy lifecycle, or any unexpected error) | `_repair_diagrams`'s `except Exception` | log; `finally` clears **all** keys and the slot; no update | `Empty` |
| `Draining(key)` | Stop | `Session.stop` | emit `stopped` for every queued key (including the head, still in the map), clear, cancel the task | `Empty` |
| `Draining(key)` | cancellation at any await | `_repair_diagrams`'s `except CancelledError` | re-raise; `finally` clears, releases `_generation`, sets `_diagram_repair_task = None` | `Empty` |

Browser figure. Rows are in `src/blackrose/gui/static/diagrams.js::mount` unless
noted; terminal text is chosen by `repairResult`:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `PendingRender` | figure intersects the viewport | `_diagramMounted` false | serial `render(source, isConnected)` in the shared sandbox | `Rendered` / `RenderFailed` |
| `PendingRender` | renderer already `broken` | `diagrams.js::render` | reject with the latched error | `RenderFailed` (non-repairable) |
| `RenderFailed` | notify | `error.repairable`, not `reported`, message id present, `dataset.messageState === 'complete'` | `reported = true` **before** the call; `diagram_failed` | `Reported` |
| `RenderFailed` | notify | message not complete, no id, or `reported` | nothing; `settled()` can retry after the reply settles | unchanged |
| `Reported` | result `queued` | `_diagramRepairSettled` false | `"Repairing diagram…"` | `Reported` |
| `Reported` | result terminal (`failed`, `exhausted`, `stopped`, `interrupted`, `unavailable`) | status text visible | terminal text, `_diagramRepairSettled = true` | `Settled` |
| `Reported` | re-render with a **new** source | `renderHTML`/`renderBlocks` build a new figure | new node, `reported` false | `PendingRender` |
| `Reported` | re-render with the **same** source | figure node reused (`src/blackrose/gui/static/bloom.js::renderHTML`) | `reported` stays true | `Reported` |

## Invariants

1. **Claim before inference.** `claim_diagram_repair` commits the `repairing`
   entry before `repair_text` is awaited (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`), and a
   crash therefore consumes the attempt ([automatic syntax repair](../diagrams.md#automatic-syntax-repair)). Pinned while
   inference is held open: `test_a_duplicate_failure_during_inference_cannot_claim_again`.
2. **The persisted status set is closed.** The only writes are the status literals in
   `src/blackrose/storage/repository.py::Repository.claim_diagram_repair` and
   `src/blackrose/storage/repository.py::Repository.finish_diagram_repair`, and
   every `finish` argument in
   `src/blackrose/diagram_repairs.py::DiagramRepairs.repair` is a literal. `finish` does not validate
   `status`; closure is by call-site convention. Pinned by
   `test_persisted_states_are_closed_and_every_terminal_state_stays_put`.
3. **Every non-`repairing` state is terminal for claims.** A re-claim returns the
   same status, writes no new attempt and starts no inference
   (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`). Pinned for `failed`, `stopped`, `interrupted`.
4. **A claim must match the current fence body.** `tokens[index].content` must
   equal the reported `source` (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`); a stale page or a
   changed block gets `stale`, never a spend. Pinned by
   `test_claim_refuses_stale_sources_foreign_owners_and_unknown_blocks`.
5. **Only a complete assistant message is claimable.** `role='assistant' AND
   state='complete'` in the named conversation (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`).
   Pinned by `test_claim_requires_a_complete_assistant_message`.
6. **The budget is per message and counted by attempt entries.** `used` sums
   `diagram_repairs` lengths over **all** blocks of the message
   (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`); a re-claim and the `updated→failed` transition skip
   the check because they start no inference. Pinned by
   `test_budget_is_counted_per_message_across_all_diagram_blocks`.
7. **Promotion is deterministic and never guessed.** The synthesized block is
   `diagram-{message_id}` at position 0 with `diagram_promoted: True`, and only
   when the message has no blocks at all (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`). An empty
   report against a message that has a non-promoted block stays `unavailable`
   (denial: `test_an_unpromoted_block_is_never_guessed_from_an_empty_report`);
   a queued empty report is resolved to the single promoted block
   (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`).
8. **`finish` is bound to its claim.** Block id + message id must exist, the
   entry must still be `repairing`, and its stored `source` must equal the
   claim's (`src/blackrose/storage/repository.py::Repository.finish_diagram_repair`). Pinned by
   `test_finish_requires_the_claim_it_started_with`.
9. **An update advances the source exactly once and never rewrites the answer.**
   `source_revision` and `source_sequence` increase by exactly 1 per applied
   repair (`src/blackrose/storage/repository.py::Repository.finish_diagram_repair`); `messages.content` is unchanged
   (`tests/test_diagram_repairs.py::DiagramRepairTests.test_failure_repairs_only_source_preserves_draft_and_original_and_updates_history`, and the two-repair case reaches revision 3 in
   `test_every_queued_failure_of_a_promoted_plain_reply_is_resolved`).
10. **One in-flight repair per process.** The `is_generating or artifacts.busy`
    check and the `is_generating = True` set have no `await` between them
    (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`), `_db.run` serializes every claim/finish
    (`src/blackrose/storage/db.py::Database.run`), Session keeps one task (`src/blackrose/gui/session.py::Session.diagram_failed`), and the
    desktop lock excludes a second process. Duplicate reports deduplicate in the
    queue (`test_a_duplicate_failure_during_inference_cannot_claim_again`).
11. **The page reports each failed figure at most once per DOM node**, and only
    for `complete` messages with a message id (`src/blackrose/gui/static/diagrams.js::mount`).
12. **A terminal status never causes a model request.** `repair` returns before
    building the prompt when the claim is not `repairing`
    (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`); the update/re-claim tests hold the request
    count at 1 across three reports.

## Hazards and open questions

Answers to the bead's focus questions, then the numbered findings.

- *Is the persisted state set closed, and is every state escapable?* Closed by
  convention (invariant 2). `Repairing` is escapable by a finish or by a re-claim;
  every other state is terminal and can never return to inference (invariant 3).
  `Repairing` is escape-**less** after a crash until the same `(block, index)` is
  reported again (hazard 6).
- *Can two repairs claim the same message block concurrently?* Not in one
  process: the controller gate, the single Session task and the serialized DB
  worker make claim and inference mutually exclusive (invariant 10). A second
  process is excluded by the desktop lock. Two *different indices* in one block
  are processed serially by the same queue and accumulate revisions; pinned by the
  two-repair promotion test.
- *Is the `updated → failed` re-claim reachable, and what happens on a third
  failure?* Yes: the page re-renders the repaired source; if Mermaid still
  rejects it, the figure reports the new source and the claim flips `updated` to
  `failed` with no request. A third report (only possible from a new page node or
  a reload, because `reported` is latched) is a terminal `failed` no-op. Pinned
  by the `updated→failed→failed` sequence in the state test.
- *Does `claim_diagram_repair` verify the block still contains the reported
  source?* Yes (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`), against `diagram_tokens` of the block,
  which also rejects an out-of-range index. Fence index stability relies on
  `replace_diagram` replacing only a body (`src/blackrose/diagrams.py::replace_diagram`), so the order and
  count of fences never change.
- *Can a repair run against a message that is no longer `complete`?* The claim
  cannot (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`); the finish does not re-check
  (`src/blackrose/storage/repository.py::Repository.finish_diagram_repair`). No production path moves a `complete` message to
  another state: `finish_message` is only called for the active pending reply,
  and `_reap` only rewrites `pending` rows (`src/blackrose/storage/repository.py::Repository.reap_pending`). A synthetic
  `finish_message(..., CANCELLED)` followed by a finish would still apply the
  repair; not demonstrated because it is unreachable from the UI.
- *Is the budget counted per block or per message?* Per message, summed over all
  blocks (invariant 6). Promotion cannot change block identity mid-repair: the
  promoted id is a pure function of the message id and is created once, at the
  claim; later repairs target the same row. It *can* invalidate a queued report
  that carried an empty block id before promotion — hazard 1.
- *Does the browser report a failure more than once, and is the server idempotent
  if it does?* The page latches `reported` before the call and never clears it for
  that node (invariant 11), so a duplicate is only possible from a second page
  node, a reload, or another Session. The queue deduplicates identical keys
  (`src/blackrose/gui/session.py::Session.diagram_failed`) and the repository refuses or echoes terminal states
  (invariant 3), so a duplicate costs at most one claim; a duplicate that arrives
  while the entry is `repairing` (two Sessions) is treated as crash recovery and
  interrupts the live claim (hazard 3).

**1. `defect` (fixed) — `src/blackrose/storage/repository.py::Repository.claim_diagram_repair`, `src/blackrose/diagram_repairs.py::DiagramRepairs.repair` — a
queued failure of a plain reply became `unavailable` once another diagram
promoted the reply.** Both reports of a two-diagram plain reply carry
`block_id = ''` because the page has no block yet. The first claim promotes the
message to `diagram-{id}`; the second claim then sees `blocks != []` with
`block_id == ''`, `target is None`, and returns `unavailable`
(`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`) — the page says "Automatic diagram repair is
unavailable" although a repair was possible and the budget allowed it. Fixed by
resolving a single promoted block for an empty report before the claim
(`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`), mirroring the render condition
(`src/blackrose/gui/render.py::message_payload`, `src/blackrose/gui/session.py::Session._message_payloads`). Regression test
`test_every_queued_failure_of_a_promoted_plain_reply_is_resolved` fails before
the change (1 request, second result `unavailable`) and passes after (2 requests,
both `updated`). Denial is unchanged: a non-promoted block is never guessed
(`test_an_unpromoted_block_is_never_guessed_from_an_empty_report`). The clean
home for this resolution is `claim_diagram_repair` itself; `repository.py` is
shared with other work, so the fix lives at the caller and the move is a
follow-up.

**2. `fragile` — `src/blackrose/gui/session.py::Session._repair_diagrams` — a busy lifecycle silently discards the
whole queue.** `_wait_for_repair_slot` waits only for `_generation`, but
`DiagramRepairs.repair` also refuses while `artifacts.busy` (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`)
or while another Session owns `is_generating`. The resulting `ChatBusy`
propagates to the generic `except`, which logs, and `finally` clears every
remaining key (`src/blackrose/gui/session.py::Session._repair_diagrams`). The page already set `reported = true`
(`src/blackrose/gui/static/diagrams.js::mount`), so it never re-reports that node. Demonstrated: with a
lifecycle command holding `exclusive()`, a queued report produces no request, no
claim, no `diagram_update` and no retry —
`test_busy_lifecycle_refusal_drops_the_queued_failure_without_feedback`.
Proposed patch (Session is shared, not changed here): catch `ChatBusy` around the
per-key `repair` call, keep the key (or re-queue it with a small delay) and emit
`{'status': 'unavailable'}` only if it stays refused; never clear unrelated keys
on a single key's failure.

**3. `fragile` — `src/blackrose/diagram_repairs.py::DiagramRepairs.repair`, `src/blackrose/storage/repository.py::Repository.finish_diagram_repair` — a no-op
finish is reported to the page as `updated`.** `finish_diagram_repair` returns
`None` on every path, including the guard failure when the entry is no longer
`repairing` (re-claimed by another Session or process) or the block row is gone.
`repair` then returns `{'status': 'updated'}` and `_diagram_update` emits an
unchanged payload; the page's `repairResult` has no `updated` message
(`src/blackrose/gui/static/diagrams.js::repairResult`), so the figure keeps "Repairing diagram…" and `reported`
stays true. The no-op itself is demonstrated
(`test_persisted_states_are_closed_and_every_terminal_state_stays_put`,
interrupted-claim case); the lying status is code-read, because a second claimant
needs a second Session or process. Proposed patch (repository + caller):
`finish_diagram_repair` returns `True`/`False`, and `repair` returns
`{'status': 'failed'}` (or `stale`) when nothing was applied. Cheap and strictly
more honest; deferred because both files are shared.

**4. `fragile` — `src/blackrose/gui/session.py::Session._repair_diagrams` — navigation silently drops a queued
failure with no retry for that page.** The key is popped without a
`diagram_update` on purpose (the chat is not visible), but when the user returns
the figure node is reused with `reported = true`, so the page never reports it
again until a reload. Not demonstrated; confirmation needs the browser probe
(`scripts/check_diagrams.cjs`) or a DOM test: report, switch chats before the
drain, switch back, observe no repair. The durable state is untouched, so a page
reload recovers.

**5. `documentation-gap` — `src/blackrose/gui/static/diagrams.js::mount` — the one-shot latch has no
failure path.** `reported = true` is set before `failed()` resolves and the
promise has no `catch`; a rejected bridge call (window closing, app shutting
down) permanently suppresses the report for that node, and the status text stays
"Diagram unavailable: …". Not demonstrated; a rejected `call()` in the probe would
confirm. Low impact because the next page load re-reports.

**6. `documentation-gap` — `src/blackrose/storage/repository.py::Repository.claim_diagram_repair` — a crash leaves `repairing`
forever and keeps consuming the budget.** There is no startup reaper for diagram
attempts (contrast `_reap` for messages and blocks, `src/blackrose/storage/repository.py::Repository.reap_pending`). The
entry flips to `interrupted` only when the same `(block, index)` is reported
again, and it counts toward `used` until then. This is deliberate
("attempts commit before inference", [automatic syntax repair](../diagrams.md#automatic-syntax-repair)) but the permanent
budget cost of a crash is not stated.

**7. `documentation-gap` — `src/blackrose/gui/static/diagrams.js::repairResult` — `repairResult` has no
`disabled` entry.** A config change to 0 while a report is queued makes the claim
return `disabled` (`src/blackrose/storage/repository.py::Repository.claim_diagram_repair`), which `repairResult` ignores, so
the figure keeps "Repairing diagram…". `diagram_failed` itself short-circuits
`disabled` (`src/blackrose/gui/session.py::Session.diagram_failed`), so this only occurs through a mid-queue
config change. Not demonstrated.

**8. `none-found` — `src/blackrose/diagram_repairs.py::DiagramRepairs.repair`, `src/blackrose/storage/db.py::Database.run`, `src/blackrose/gui/session.py::Session.diagram_failed`
— concurrent claims of one block are prevented by three independent layers** (the
effect gate, the Session task, the serialized DB worker; the desktop flock for a
second process). The `is_generating` check/set is atomic because no `await`
separates them.

**9. `none-found` — `src/blackrose/storage/repository.py::Repository.claim_diagram_repair` — the budget check is correctly
after the re-claim branch.** It is what makes `updated → failed` free and what
prevents a terminal state from being re-charged, and the denial direction (a new
index at the limit) is `exhausted`, not `disabled`.

## Formalization decisions

**Source change (one, small, tested).** `src/blackrose/diagram_repairs.py::DiagramRepairs.repair`:
when the report has no block id, read the message and, if it is exactly one
promoted block, claim that block. Why: hazard 1 is a stranded failure reachable
from the normal two-diagram-plain-reply path, the resolution mirrors the two
places that already treat a single promoted block as "the message" (`src/blackrose/gui/render.py::message_payload`,
`src/blackrose/gui/session.py::Session._message_payloads`), it adds no new state, and it cannot widen anything — the
repository still validates ownership, completeness, the fence body and the
budget. The read is inside `artifacts.exclusive()` and scoped by conversation and
message. Regression and denial tests are named in hazard 1.

**No source change made** for hazards 2 and 3: both touch shared central files
(`gui/session.py`, `storage/repository.py`) and change a cross-owner contract
(the `ChatBusy` policy and the `finish` return value) that other work in flight
may rely on. They are recorded as concrete follow-ups with suggested shapes.
Hazards 4-7 and 9 are documentation or browser-only. Recommended follow-up beads:
(a) report `ChatBusy`/unavailable per key in `_repair_diagrams` without dropping
the rest of the queue; (b) return a bool from `finish_diagram_repair` and stop
reporting `updated` for a no-op; (c) move the promoted-block resolution into
`claim_diagram_repair`; (d) add `disabled` to `repairResult` and a `catch` on the
report promise; (e) a startup reaper for `repairing` entries, or an explicit
statement in `docs/diagrams.md` that a crash spends the attempt permanently.

## Verification

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_08_diagram_repair.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_diagram_repairs.py
.venv/bin/python scripts/check_unit_tests.py -p test_diagrams.py
```

`test_every_queued_failure_of_a_promoted_plain_reply_is_resolved` pins the
hazard 1 fix: with the promoted-block resolution in `DiagramRepairs.repair`
removed, it fails (one request instead of two).

The browser probe `scripts/check_diagrams.cjs` (needs the `diagram-runtime`
build) and the native probe `scripts/check_gui_diagrams.py` (needs a WebKit
window) cover the page behavior; hazards 4, 5 and 7 are JavaScript-only and only
those probes can confirm them.
