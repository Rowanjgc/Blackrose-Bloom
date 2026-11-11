# SM-08: Failure-driven diagram syntax repair

## Scope and ownership

| Surface | Location |
| --- | --- |
| `DiagramRepairs.repair` (claim → inference → settle) | `src/blackrose/diagram_repairs.py:19-68` |
| Durable claim and finish | `src/blackrose/storage/repository.py:972-1043` |
| Browser report intake and deferred scheduling | `src/blackrose/gui/session.py:1175-1193`, `1204-1236` |
| Stop/close cancellation and their updates | `src/blackrose/gui/session.py:353-366`, `1060-1070` |
| Figure failure detection and one-shot report | `src/blackrose/gui/static/diagrams.js:114-205` |
| Bridge call and result dispatch | `src/blackrose/gui/static/bloom.js:293-298`, `800-809`; `src/blackrose/gui/bridge.py:268-269` |
| Promotion rendering | `src/blackrose/gui/render.py:412-425`, `645-655`; `src/blackrose/gui/session.py:460`, `531-543` |
| Config bound | `src/blackrose/config.py:181`, `244-245` |
| User-facing contract | `docs/diagrams.md:39-64`; `docs/architecture.md:65-75` |

One `DiagramRepairs` per `ChatController` (`chat.py:105-106`); one `Session` per
window and one GTK/WebKit page (`docs/architecture.md:3-5`). The single asyncio
loop owns the claim, the inference and the Session queue; the SQLite worker
(`storage/db.py:82-94`) serializes all payload reads and writes, and the desktop
flock (`instance.py:35`) forbids a second process on the same database
(`docs/adr/0001-desktop-ownership.md`).

The shape of the ownership: the *page* decides only that a render failed (it does
not know Mermaid validity in Python); the *Session* owns schedule, scope and
delivery; the *repository* owns the durable claim, the budget and the source
revision; `DiagramRepairs` owns the one inference and its settlement. The raw
assistant message (`messages.content`) is never rewritten — only the promoted or
existing `markdown` block (`message_blocks.payload['source']`), which is what
future history uses (`test_diagram_repairs.py:84-88`).

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `payload['diagram_repairs'][str(index)]` | `repository.py:998-1011`, `1031-1040` | JSON object `{status, source}` | per-diagram attempt record inside one block | `claim_diagram_repair`, `finish_diagram_repair` |
| `payload['diagram_repairs'][i].status` | `repository.py:1001-1004`, `1011`, `1040` | enum `repairing\|updated\|failed\|stopped\|interrupted` | durable attempt state | claim, finish |
| `payload['diagram_repairs'][i].source` | `repository.py:1011` | str ≤ 20 000 | the exact fence body that failed; binds a finish to its claim | claim |
| `payload['block_id']` | `repository.py:987`, `1013` | str (≤128 through the bridge) | block identity; `diagram-{message_id}` when promoted | promotion (claim), turn |
| `payload['diagram_promoted']` | `repository.py:992` | bool | this block is a plain reply promoted for repair | claim only |
| `payload['source']` | `repository.py:1035` | str | Markdown with the fence bodies; the display/history source | `finish(updated)`; turn streaming |
| `payload['source_hash']`, `['fallback_text']` | `repository.py:1036-1037` | str | hash and readable fallback of `source` | `finish(updated)` |
| `payload['source_revision']`, `['source_sequence']` | `repository.py:1038-1039` | int | monotonic revision and UI sequence; +1 per applied repair | `finish(updated)` |
| `messages.state` | `repository.py:979-980` read; `767-...` write | `MessageState` | only `complete` is claimable | `finish_message`, `_reap` |
| `Session._pending_diagram_failures` | `session.py:129` | dict `(cid, mid, block, index, source) → error`, ≤ 8 | queued reports not yet claimed | `diagram_failed`, `stop`, `close`, `_repair_diagrams` |
| `Session._diagram_repair_task` | `session.py:128` | `asyncio.Task \| None` | the single drain task | `diagram_failed` (spawn), `_repair_diagrams` (clear) |
| `Session._generation` | `session.py:1210`, `1233-1234` | `asyncio.Task \| None` | the shared reply/repair slot | `_wait_for_repair_slot`, Session |
| `ChatController.is_generating` | `chat.py:124`, `302`, `321`; `diagram_repairs.py:21-26`, `67-68` | bool | in-process one-effect gate | turn, repair |
| `ArtifactLifecycle._lock` / `.busy` | `artifacts.py:168-170`, `173-178` | async lock | excludes lifecycle commands from a repair | context manager |
| `Config.diagram_repairs` | `config.py:181`, `244-245` | int 0..2 | attempts allowed per *message* | user config |
| figure `reported`, `failure` | `diagrams.js:130-141` | JS closure | one report per DOM node; last render error | `mount` |
| figure `_diagramRepairSettled`, `_diagramMounted` | `diagrams.js:138`, `116-117` | JS | terminal text already shown; mount latch | `repairResult`, `mount` |
| renderer `broken`, cache | `diagrams.js:7`, `37`, `84-110` | JS | latched renderer failure; source → SVG cache | renderer |

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
`broken`, `diagrams.js:37`, `94`).

## Events

- A visible figure's render rejects with `error.repairable === true`
  (`diagrams.js:176-181`, `mermaid-host.html` decides repairability).
- `settled(root)` after a reply settles (`bloom.js:575-576` → `diagrams.js:188-190`),
  which gives a not-yet-visible or earlier-failed figure a second report chance.
- Bridge `diagram_failed(message_id, block_id, index, source, error)`
  (`bridge.py:268-269` → `session.py:1175`).
- Session drain start, boot completion, reply-slot acquisition
  (`session.py:1207-1214`).
- Repository claim (`repository.py:972`) and finish (`repository.py:1020`).
- Inference completion, exception, cancellation, unchanged replacement
  (`diagram_repairs.py:51-66`); `settle` keeps a cancellation from interrupting
  the settlement (`artifacts.py:24-36`).
- Stop (`session.py:1060-1070`), close (`session.py:353-366`), navigation,
  page reload, process death.
- Config change; message deletion.

## Transition table

Durable attempt machine:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Absent` | claim, empty `block_id` | message is `assistant`+`complete` in this conversation (`repository.py:979-982`) and has **no** blocks (`986`) | synthesize `diagram-{message_id}`, position 0, `diagram_promoted: True`, source = message content (`987-992`) | claim continues |
| `Absent` | claim, empty `block_id`, message already has blocks | `986` false | `target is None` → `unavailable` (`993-994`) | `Absent` |
| `Absent` | claim | block missing, not `markdown` (`993`), or index out of range / fence body ≠ reported source (`995-997`) | `unavailable` / `stale` | `Absent` |
| `Absent` | claim on a promoted block with empty `block_id` | exactly one block and `diagram_promoted` — resolved by `diagram_repairs.py:31-37` before the claim | claim as if the page had named `diagram-{id}` | `Repairing` or refusal |
| `Absent` | claim | `used = Σ len(repairs)` over all blocks of the message (`1008`), `used >= limit` (`1009`) | `disabled` when `limit == 0`, else `exhausted`; no write | `Absent` |
| `Absent` | claim, all guards pass | — | `repairs[index] = {repairing, source}`; whole payload upserted (`1011-1014`) | `Repairing` |
| `Repairing` | claim, same index, same source | entry exists (`999`) | `status := interrupted`; payload written (`1001-1006`) | `Interrupted` |
| `Repairing` | finish(`updated`, replacement) | row exists, `status == 'repairing'`, `source` equals the claim (`1026-1033`) | `replace_diagram` body only (`1035`); hash/fallback (`1036-1037`); revision+1, sequence+1 (`1038-1039`); `status := updated` (`1040`) | `Updated` |
| `Repairing` | finish(`failed`) — exception, invalid output (`repaired_source` raises), or unchanged output (`diagram_repairs.py:53-54`, `62-66`) | same guard | `status := failed`; source untouched | `Failed` |
| `Repairing` | finish(`stopped`) — `CancelledError` (`diagram_repairs.py:58-61`) | same guard | `status := stopped`; source untouched | `Stopped` |
| `Repairing` | finish after a re-claim, block deletion, or a different claim source | guard fails (`1028-1033`) | **silent no-op**, caller receives `None` | unchanged |
| `Repairing` | process death | — | entry stays committed | `Repairing` (until the next claim) |
| `Updated` | claim, same index, source = the **new** fence body | token matches (`996`) | `status := failed` (`1003-1004`), no inference | `Failed` |
| `Failed` / `Stopped` / `Interrupted` | claim, same index, same source | entry exists | payload rewritten with the same status, same value (`1005-1007`) | unchanged (terminal) |
| `Failed` / `Stopped` / `Interrupted` | claim, same index, different source | `996` fails first | `stale` | unchanged |

`Updated → Failed` is the only non-terminal→terminal transition besides a
finish; it happens when the browser renders the repaired source, fails again and
reports that new source at the same fence index. It runs **before** the budget
check (`1008-1010`), so it costs no attempt and no request
(`diagram_repairs.py:40-41`). A third failure re-claims `Failed → Failed` and
still spends nothing
(`test_persisted_states_are_closed_and_every_terminal_state_stays_put`).

Session queue:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Empty` | `diagram_failed` | config ≠ 0, types/bounds, ready, not quitting, no permission prompt (`session.py:1176-1183`), selected chat (`1184-1186`) | `setdefault(key, error)`; spawn task if none running (`1190-1192`) | `Queued(1)` |
| `Queued(n)` | same key (same chat, message, block, index, **source**) | — | `setdefault` no-op; error text of the first report kept | unchanged |
| `Queued(8)` | new key | `len >= 8` (`1188`) | `unavailable`; report dropped, page shows unavailable | unchanged |
| `Queued(n)` | task starts | `_boot_task` pending | await it (`1207-1209`) | unchanged |
| `Queued(n)` | boot settled | `_wait_for_repair_slot` false (not ready, quitting, permission pending) | emit `unavailable` for every key (`1211-1213`) | `Empty` |
| `Queued(n)` | slot acquired | — | `_generation = task`; head key stays in the map while `repair` runs | `Draining(head)` |
| `Draining(key)` | `repair` returns | — | pop key (`1225`), emit `diagram_update(..., repair=result)` (`1226`) | `Queued(n-1)` / `Empty` |
| `Draining(key)` | navigation | selected chat ≠ key chat (`1218-1219`) | pop key, **no** update | `Queued(n-1)` |
| `Draining(key)` | `repair` raises (`ChatBusy` from a busy lifecycle, or any unexpected error) | `1229-1230` | log; `finally` clears **all** keys and the slot (`1231-1236`); no update | `Empty` |
| `Draining(key)` | Stop | `1063-1070` | emit `stopped` for every queued key (including the head, still in the map), clear, cancel the task | `Empty` |
| `Draining(key)` | cancellation at any await | `1227-1228` | re-raise; `finally` clears, releases `_generation`, sets `_diagram_repair_task = None` | `Empty` |

Browser figure:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `PendingRender` | figure intersects the viewport | `_diagramMounted` false (`116`) | serial `render(source, isConnected)` in the shared sandbox | `Rendered` / `RenderFailed` |
| `PendingRender` | renderer already `broken` | `94` | reject with the latched error | `RenderFailed` (non-repairable) |
| `RenderFailed` | notify | `error.repairable`, not `reported`, message id present, `dataset.messageState === 'complete'` (`133-134`) | `reported = true` **before** the call (`135`); `diagram_failed` | `Reported` |
| `RenderFailed` | notify | message not complete, no id, or `reported` | nothing; `settled()` can retry after the reply settles (`188-190`) | unchanged |
| `Reported` | result `queued` | `_diagramRepairSettled` false (`138`) | `"Repairing diagram…"` | `Reported` |
| `Reported` | result terminal (`failed`, `exhausted`, `stopped`, `interrupted`, `unavailable`) | status text visible (`199-204`) | terminal text, `_diagramRepairSettled = true` | `Settled` |
| `Reported` | re-render with a **new** source | `renderHTML`/`renderBlocks` build a new figure | new node, `reported` false | `PendingRender` |
| `Reported` | re-render with the **same** source | figure node reused (`bloom.js:274-281`) | `reported` stays true | `Reported` |

## Invariants

1. **Claim before inference.** `claim_diagram_repair` commits the `repairing`
   entry before `repair_text` is awaited (`diagram_repairs.py:38-51`), and a
   crash therefore consumes the attempt (`docs/diagrams.md:52-56`). Pinned while
   inference is held open: `test_a_duplicate_failure_during_inference_cannot_claim_again`.
2. **The persisted status set is closed.** The only writes are the literals at
   `repository.py:1002`, `1004`, `1011`, `1040`, and every `finish` argument is a
   literal (`diagram_repairs.py:54`, `56`, `60`, `65`). `finish` does not validate
   `status`; closure is by call-site convention. Pinned by
   `test_persisted_states_are_closed_and_every_terminal_state_stays_put`.
3. **Every non-`repairing` state is terminal for claims.** A re-claim returns the
   same status, writes no new attempt and starts no inference
   (`repository.py:999-1007`). Pinned for `failed`, `stopped`, `interrupted`.
4. **A claim must match the current fence body.** `tokens[index].content` must
   equal the reported `source` (`repository.py:995-997`); a stale page or a
   changed block gets `stale`, never a spend. Pinned by
   `test_claim_refuses_stale_sources_foreign_owners_and_unknown_blocks`.
5. **Only a complete assistant message is claimable.** `role='assistant' AND
   state='complete'` in the named conversation (`repository.py:979-980`).
   Pinned by `test_claim_requires_a_complete_assistant_message`.
6. **The budget is per message and counted by attempt entries.** `used` sums
   `diagram_repairs` lengths over **all** blocks of the message
   (`repository.py:1008`); a re-claim and the `updated→failed` transition skip
   the check because they start no inference. Pinned by
   `test_budget_is_counted_per_message_across_all_diagram_blocks`.
7. **Promotion is deterministic and never guessed.** The synthesized block is
   `diagram-{message_id}` at position 0 with `diagram_promoted: True`, and only
   when the message has no blocks at all (`repository.py:986-992`). An empty
   report against a message that has a non-promoted block stays `unavailable`
   (denial: `test_an_unpromoted_block_is_never_guessed_from_an_empty_report`);
   a queued empty report is resolved to the single promoted block
   (`diagram_repairs.py:31-37`).
8. **`finish` is bound to its claim.** Block id + message id must exist, the
   entry must still be `repairing`, and its stored `source` must equal the
   claim's (`repository.py:1026-1033`). Pinned by
   `test_finish_requires_the_claim_it_started_with`.
9. **An update advances the source exactly once and never rewrites the answer.**
   `source_revision` and `source_sequence` increase by exactly 1 per applied
   repair (`repository.py:1038-1039`); `messages.content` is unchanged
   (`test_diagram_repairs.py:84-88`, and the two-repair case reaches revision 3 in
   `test_every_queued_failure_of_a_promoted_plain_reply_is_resolved`).
10. **One in-flight repair per process.** The `is_generating or artifacts.busy`
    check and the `is_generating = True` set have no `await` between them
    (`diagram_repairs.py:21-26`), `_db.run` serializes every claim/finish
    (`db.py:82-94`), Session keeps one task (`session.py:1191-1192`), and the
    desktop lock excludes a second process. Duplicate reports deduplicate in the
    queue (`test_a_duplicate_failure_during_inference_cannot_claim_again`).
11. **The page reports each failed figure at most once per DOM node**, and only
    for `complete` messages with a message id (`diagrams.js:130-135`).
12. **A terminal status never causes a model request.** `repair` returns before
    building the prompt when the claim is not `repairing`
    (`diagram_repairs.py:40-41`); the update/re-claim tests hold the request
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
  source?* Yes (`repository.py:995-997`), against `diagram_tokens` of the block,
  which also rejects an out-of-range index. Fence index stability relies on
  `replace_diagram` replacing only a body (`diagrams.py:50-61`), so the order and
  count of fences never change.
- *Can a repair run against a message that is no longer `complete`?* The claim
  cannot (`repository.py:979-980`); the finish does not re-check
  (`repository.py:1026-1033`). No production path moves a `complete` message to
  another state: `finish_message` is only called for the active pending reply,
  and `_reap` only rewrites `pending` rows (`repository.py:884-917`). A synthetic
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
  (`session.py:1190`) and the repository refuses or echoes terminal states
  (invariant 3), so a duplicate costs at most one claim; a duplicate that arrives
  while the entry is `repairing` (two Sessions) is treated as crash recovery and
  interrupts the live claim (hazard 3).

**1. `defect` (fixed) — `repository.py:986`, `diagram_repairs.py:31-37` — a
queued failure of a plain reply became `unavailable` once another diagram
promoted the reply.** Both reports of a two-diagram plain reply carry
`block_id = ''` because the page has no block yet. The first claim promotes the
message to `diagram-{id}`; the second claim then sees `blocks != []` with
`block_id == ''`, `target is None`, and returns `unavailable`
(`repository.py:985-994`) — the page says "Automatic diagram repair is
unavailable" although a repair was possible and the budget allowed it. Fixed by
resolving a single promoted block for an empty report before the claim
(`diagram_repairs.py:31-37`), mirroring the render condition
(`render.py:648`, `session.py:460`). Regression test
`test_every_queued_failure_of_a_promoted_plain_reply_is_resolved` fails before
the change (1 request, second result `unavailable`) and passes after (2 requests,
both `updated`). Denial is unchanged: a non-promoted block is never guessed
(`test_an_unpromoted_block_is_never_guessed_from_an_empty_report`). The clean
home for this resolution is `claim_diagram_repair` itself; `repository.py` is
shared with other work, so the fix lives at the caller and the move is a
follow-up.

**2. `fragile` — `session.py:1223-1230` — a busy lifecycle silently discards the
whole queue.** `_wait_for_repair_slot` waits only for `_generation`, but
`DiagramRepairs.repair` also refuses while `artifacts.busy` (`diagram_repairs.py:21`)
or while another Session owns `is_generating`. The resulting `ChatBusy`
propagates to the generic `except`, which logs, and `finally` clears every
remaining key (`session.py:1231-1232`). The page already set `reported = true`
(`diagrams.js:135`), so it never re-reports that node. Demonstrated: with a
lifecycle command holding `exclusive()`, a queued report produces no request, no
claim, no `diagram_update` and no retry —
`test_busy_lifecycle_refusal_drops_the_queued_failure_without_feedback`.
Proposed patch (Session is shared, not changed here): catch `ChatBusy` around the
per-key `repair` call, keep the key (or re-queue it with a small delay) and emit
`{'status': 'unavailable'}` only if it stays refused; never clear unrelated keys
on a single key's failure.

**3. `fragile` — `diagram_repairs.py:56`, `repository.py:1028-1033` — a no-op
finish is reported to the page as `updated`.** `finish_diagram_repair` returns
`None` on every path, including the guard failure when the entry is no longer
`repairing` (re-claimed by another Session or process) or the block row is gone.
`repair` then returns `{'status': 'updated'}` and `_diagram_update` emits an
unchanged payload; the page's `repairResult` has no `updated` message
(`diagrams.js:192-198`), so the figure keeps "Repairing diagram…" and `reported`
stays true. The no-op itself is demonstrated
(`test_persisted_states_are_closed_and_every_terminal_state_stays_put`,
interrupted-claim case); the lying status is code-read, because a second claimant
needs a second Session or process. Proposed patch (repository + caller):
`finish_diagram_repair` returns `True`/`False`, and `repair` returns
`{'status': 'failed'}` (or `stale`) when nothing was applied. Cheap and strictly
more honest; deferred because both files are shared.

**4. `fragile` — `session.py:1218-1221` — navigation silently drops a queued
failure with no retry for that page.** The key is popped without a
`diagram_update` on purpose (the chat is not visible), but when the user returns
the figure node is reused with `reported = true`, so the page never reports it
again until a reload. Not demonstrated; confirmation needs the browser probe
(`scripts/check_diagrams.cjs`) or a DOM test: report, switch chats before the
drain, switch back, observe no repair. The durable state is untouched, so a page
reload recovers.

**5. `documentation-gap` — `diagrams.js:135-141` — the one-shot latch has no
failure path.** `reported = true` is set before `failed()` resolves and the
promise has no `catch`; a rejected bridge call (window closing, app shutting
down) permanently suppresses the report for that node, and the status text stays
"Diagram unavailable: …". Not demonstrated; a rejected `call()` in the probe would
confirm. Low impact because the next page load re-reports.

**6. `documentation-gap` — `repository.py:1011` — a crash leaves `repairing`
forever and keeps consuming the budget.** There is no startup reaper for diagram
attempts (contrast `_reap` for messages and blocks, `repository.py:890-917`). The
entry flips to `interrupted` only when the same `(block, index)` is reported
again (`1001-1002`), and it counts toward `used` until then. This is deliberate
("attempts commit before inference", `docs/diagrams.md:52-56`) but the permanent
budget cost of a crash is not stated.

**7. `documentation-gap` — `diagrams.js:192-198` — `repairResult` has no
`disabled` entry.** A config change to 0 while a report is queued makes the claim
return `disabled` (`repository.py:1009-1010`), which `repairResult` ignores, so
the figure keeps "Repairing diagram…". `diagram_failed` itself short-circuits
`disabled` (`session.py:1176-1177`), so this only occurs through a mid-queue
config change. Not demonstrated.

**8. `none-found` — `diagram_repairs.py:21-26`, `db.py:82-94`, `session.py:1190-1192`
— concurrent claims of one block are prevented by three independent layers** (the
effect gate, the Session task, the serialized DB worker; the desktop flock for a
second process). The `is_generating` check/set is atomic because no `await`
separates them.

**9. `none-found` — `repository.py:1008-1010` — the budget check is correctly
after the re-claim branch.** It is what makes `updated → failed` free and what
prevents a terminal state from being re-charged, and the denial direction (a new
index at the limit) is `exhausted`, not `disabled`.

## Formalization decisions

**Source change (one, small, tested).** `src/blackrose/diagram_repairs.py:31-37`:
when the report has no block id, read the message and, if it is exactly one
promoted block, claim that block. Why: hazard 1 is a stranded failure reachable
from the normal two-diagram-plain-reply path, the resolution mirrors the two
places that already treat a single promoted block as "the message" (`render.py:648`,
`session.py:460`), it adds no new state, and it cannot widen anything — the
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

```
.venv/bin/python scripts/check_unit_tests.py -p test_sm_08_diagram_repair.py -v
    Ran 10 tests in 0.375s
    OK
.venv/bin/python scripts/check_unit_tests.py -p test_diagram_repairs.py
    Ran 11 tests in 0.343s
    OK
.venv/bin/python scripts/check_unit_tests.py -p test_diagrams.py
    Ran 6 tests in 0.068s
    OK
.venv/bin/python -m ruff check src/blackrose/diagram_repairs.py tests/test_sm_08_diagram_repair.py
    All checks passed!
.venv/bin/python -m mypy
    Success: no issues found in 19 source files
git diff --check
    (clean)
```

The regression test was also run against the pre-change source (the resolution
block removed by hand, then restored): 2 of 10 tests failed, including
`test_every_queued_failure_of_a_promoted_plain_reply_is_resolved`
(`AssertionError: 1 != 2`), confirming it pins the fix.

Not run here: `node scripts/check_diagrams.cjs` and
`scripts/check_gui_diagrams.py`. The first needs the `diagram-runtime` npm build,
the second a visible WebKit window; neither was modified in this change, so the
residual risk is confined to the JS behavior in hazards 4, 5 and 7, which only
those probes can confirm.
