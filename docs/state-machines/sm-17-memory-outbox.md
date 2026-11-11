# SM-17: Honcho memory client backoff, peer-card staleness and the durable outbox

## Scope and ownership

- `src/blackrose/memory.py`: `HonchoMemory` (187–457) — one loopback HTTP client for the
  few Honcho v3 operations Blackrose uses; `MemoryOutbox` (460–536) — the durable send
  queue; module constants `BACKOFF_SECONDS` (23), `CARD_STALE` (45).
- `src/blackrose/storage/repository.py`: `_forget_in_memory` (229–234),
  `delete_conversation` (458–476), `set_memory_excluded` (510–518), `memory_jobs`
  (520–523), `start_memory_job` (525–529), `finish_memory_job` (531–535),
  `drop_memory_turns` (537–542), `memory_turn` (544–556), the queueing branch of
  `finish_message` (803–810).
- `src/blackrose/storage/migrations.py`: migration 9, `memory_outbox` and the two
  conversation flags (120–139).
- `src/blackrose/chat.py`: `MemoryOutbox` construction (113–116), `schedule_memory_sync`
  (432–438), `_sync_memory` (440–451), `set_memory_excluded` (453–463), `forget_memory`
  (465–470), deletion scheduling (533), shutdown cancellation (246–252).
- `src/blackrose/gui/session.py`: the explicit surfaces — toggle (1597–1608), exclusion (1610–1617), review
  sheet reads and writes (1632–1673); `turns.py:345`/`402` gate recall, `turns.py:824–825`
  schedule a sync after a stored reply.

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[ADR 0001](../adr/0001-desktop-ownership.md):

- **One desktop process owns `chats.db`.** Every repository call is submitted through
  `Database.run`, which serialises callbacks on one worker thread
  (`storage/db.py`). Rows therefore mutate in submission order and there is no
  cross-process writer while the desktop runs.
- **The application asyncio loop owns the client and the outbox.** `ChatController`
  holds the only `HonchoMemory` and the only `MemoryOutbox`. `_memory_task`
  (chat.py:115) is the only drain task; `MemoryOutbox._lock` (memory.py:472) serialises
  drains against `forget_everything`. `_ready`/`_retry_at` are touched only from this
  loop, so they need no lock of their own beyond `_ready_lock` (memory.py:197).
- **Honcho's peer metadata is external state with a second owner.** `card_stale_since`
  lives in the user peer's metadata (`memory.py:45`); Blackrose sets it and only
  an external maintenance job clears it after rebuilding the profile
  ([memory.md](../memory.md#the-profile-after-forgetting)). This document treats that
  timer as part of the state machine, because recall depends on it.
- **Memory is an enhancement, never a precondition.** Every failure path either keeps a
  durable row or degrades to no memory ([architecture](../architecture.md#modules-and-invariants)).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `_retry_at` | `memory.py:195`, read `:201` | `float` (monotonic seconds) | Instant before which background requests are refused | `request` on a 2xx (`:222`), on an answered `missing_ok` 404 (`:216`), on any transport/HTTP/JSON failure to `now + 300` (`:225`) |
| `_ready` | `memory.py:196`, `:233`, `:246` | `bool` | Workspace and peers were created this process | `ensure_ready` only; never reset |
| `_ready_lock` | `memory.py:197` | `asyncio.Lock` | Single-flight setup | `ensure_ready` (`:235`) |
| `available` | `memory.py:199–201` | derived `bool` | `clock() >= _retry_at`; the only gate on background requests (`:209`) | computed |
| `_clock` | `memory.py:194` | callable | Test seam for monotonic time | constructor |
| peer metadata `card_stale_since` | set `memory.py:342`, read `:335`/`:268`, cleared `:452` | ISO-8601 UTC `str` in JSON | Profile may state what has been forgotten; recall omits the profile while set | `mark_card_stale` for every forget path; cleared by `forget_everything` here and by the external maintenance job |
| peer card (`peer_card`) | `memory.py:292–294`, `:296–299`, `:447–449` | JSON list of `str` | The profile Honcho shows and recall prefers | `save_card`, `forget_everything`, Honcho dreams |
| `conversations.memory_excluded` | `migrations.py:135`; `repository.py:510–518` | `INTEGER 0/1` | Chat contributes nothing and is not recalled | `set_memory_excluded`; read by `memory_turn` (`:548`) and `finish_message` (`:807`) |
| `conversations.memory_used` | `migrations.py:136`; `repository.py:808–810` | `INTEGER 0/1` | A turn was queued, so a Honcho session may exist to delete | `finish_message`, only when the queue insert changed a row |
| `memory_outbox` row | `migrations.py:125–133` | `(kind, conversation_id, message_id, attempts, created_at)` | One durable obligation; `UNIQUE(kind, conversation_id, message_id)` | inserted `repository.py:804–807` and `:232–234`; `attempts+1` `:528`; deleted `:534`, `:231`, `:541` |
| `MemoryOutbox._lock` | `memory.py:472` | `asyncio.Lock` | A drain and a forget-everything never interleave | `drain` (`:474`), `forget_everything` (`:478`) |
| `_memory_task` | `chat.py:115`, `:437–438`, `:248–252` | `asyncio.Task \| None` | The running drain task, if any | `schedule_memory_sync`, `shutdown` |
| `_memory_pending` | `chat.py:116`, `:436`, `:442–443` | `bool` | A sync was requested while a drain was running | `schedule_memory_sync`, `_sync_memory` |
| installation id | `migrations.py:138–139`; read `repository.py:500–501` | 12 hex chars | Names this database's sessions: `bb-<id>-<conversation>` (`memory.py:490`, `:496`) | migration 9 only |

## States

The client has two orthogonal variables, so four states; `available` is the gate
background requests see (`memory.py:209`).

| State | Predicate |
| --- | --- |
| `COLD` | `not _ready and available` |
| `COLD_PAUSED` | `not _ready and not available` |
| `WARM` | `_ready and available` |
| `WARM_PAUSED` | `_ready and not available` |

The profile (`recall`'s view, `memory.py:268–269`):

| State | Predicate |
| --- | --- |
| `FRESH` | `card_stale_since` absent from peer metadata |
| `STALE` | `card_stale_since` present; recall omits `peer_card` but serves conclusions |

An outbox row (`kind='turn'` or `kind='forget'`) has three persisted states and one
process-local phase; there is no persisted *failed* state.

| State | Predicate |
| --- | --- |
| `QUEUED` | row present with `attempts = 0` |
| `RETRYING` | row present with `attempts >= 1`, no drain working on it |
| `IN_FLIGHT` | process-local: between `start_memory_job` and `finish_memory_job` for that row |
| `SETTLED` | row absent — the only terminal state |

The coalescer (`chat.py:432–451`):

| State | Predicate |
| --- | --- |
| `IDLE` | task is `None` or done |
| `RUNNING` | task running, `_memory_pending` false |
| `RUNNING_PENDING` | task running, `_memory_pending` true; the loop drains again |
| `STRANDED` | `_memory_pending` true with no running task — reachable only through the failure return at `chat.py:446–451`; the next `schedule_memory_sync` repairs it |

## Events

| Event | Source | Effect on this machine |
| --- | --- | --- |
| A reply settles `COMPLETE` with `remember=True` | `turns.py:737`, `repository.py:803–810` | inserts a `turn` row, sets `memory_used = 1`, then `schedule_memory_sync` (`turns.py:825`) |
| `schedule_memory_sync()` | turn settle, conversation delete (`chat.py:533`), memory switched off (`session.py:1608`), startup (`runtime.py:17`) | coalesces into `_memory_task` |
| `drain()` reaches the queue | `chat.py:445` | drops turns if disabled, lists jobs, ensures readiness, settles rows |
| `ensure_ready()` | `recall`, `save_card`, `rebuild_card`, `_drain` (`memory.py:491`), status (`session.py:1687`) | creates workspace/peers once |
| Honcho 2xx | any request | `_retry_at = 0` |
| Honcho `missing_ok` 404 | `card`, `delete_conclusion`, list endpoints | treated as success; `_retry_at = 0` |
| Honcho 400/404/409/413/422 | any request | `MemoryRejected` — the *request* is terminal, no pause |
| Transport error, 5xx, bad JSON | any request | `MemoryUnavailable`, `_retry_at = now + 300` |
| User excludes a chat / deletes it | `chat.py:458`, `repository.py:473` | deletes its `turn` rows, inserts a `forget` row when `memory_used = 1` |
| Memory switched off | `chat.py:226`, `session.py:1608` | next drain drops all `turn` rows |
| Review sheet: forget one note / rebuild profile | `memory.py:371–381` | sets `card_stale_since`, then deletes the note (forget) or does nothing else (rebuild) |
| Forget everything (native confirmation) | `chat.py:465–470` → `memory.py:478–480` | takes the lock, drops `turn` rows, deletes sessions, conclusions, profile, mark |
| Shutdown | `chat.py:246–252` | cancels `_memory_task`; rows stay queued |
| Backoff window elapses | clock | `WARM_PAUSED → WARM`, `COLD_PAUSED → COLD` with no request |
| External profile rebuild | external process | `STALE → FRESH` without any Blackrose code running |

## Transition table

### Client readiness and backoff

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `COLD` | `ensure_ready` | `not _ready` (`:233`) | workspace, config, peers created; `_ready = True` (`:239–246`) | `COLD`→`WARM` |
| `COLD` | background request during an outage | `not available` (`:209`) | raises `MemoryUnavailable`, no HTTP call | `COLD_PAUSED` |
| `COLD` | rejection status | `missing_ok` false (`:218`) | `MemoryRejected`; `_retry_at` untouched (`:219–220`) | `COLD` |
| `WARM` | transport error, 5xx, bad JSON | any request (`:224–229`) | `_retry_at = now + 300`; `MemoryUnavailable` | `WARM_PAUSED` |
| `WARM_PAUSED` | background request | `:209` | refused without an HTTP call | `WARM_PAUSED` (self) |
| `WARM_PAUSED` | explicit request (`explicit=True`) | `:209` skipped | request is made | `WARM_PAUSED` |
| `WARM_PAUSED` | any 2xx | `:222` | `_retry_at = 0` | `WARM` |
| `WARM_PAUSED` | `missing_ok` 404 | `:213` | `_retry_at = 0`; `None` returned | `WARM` |
| `WARM_PAUSED` | rejection status | `:218` | `MemoryRejected`; no pause change | `WARM_PAUSED` |
| `WARM_PAUSED` | 300 s elapse | `clock() >= _retry_at` (`:201`) | availability only | `WARM` |
| `WARM` | Honcho loses the workspace (404) | `:218` | `MemoryRejected`; `_ready` stays `True` (`:233`) | `WARM` |

### Profile staleness

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `FRESH` | `forget_note` | valid conclusion id (`:372`) | metadata read, `card_stale_since` written (`:341–344`), then delete | `STALE` |
| `FRESH` | `forget_session` finds notes | `own` non-empty (`:412`) | mark written *before* the first delete (`:414`) | `STALE` |
| `FRESH` | `forget_session` finds no notes | `own` empty (`:412`) | no mark, session deleted only (`:427`) | `FRESH` |
| `FRESH` | `rebuild_card` | none (`:378–381`) | `ensure_ready`, then mark set | `STALE` |
| `STALE` | `forget_note` / `forget_session` / `rebuild_card` | `:414`, `:342` | new timestamp written; nothing cleared | `STALE` (self) |
| `STALE` | `save_card` | none | notes and profile replaced; mark untouched (`:304–329`) | `STALE` (self) |
| `STALE` | `forget_everything` completes | profile emptied (`:447–449`) | `metadata.pop(CARD_STALE)` (`:452`) | `FRESH` |
| `STALE` | the maintenance job rebuilds and clears | external, value-checked | nothing in Blackrose runs | `FRESH` |
| any | `forget_everything` fails mid-way | `:446` | mark is *not* set by this path; profile and remaining notes stay | unchanged |

### Outbox rows and the drain

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `SETTLED` | `finish_message(COMPLETE, remember=True)` | `c.memory_excluded = 0` (`:807`) | insert row, `memory_used = 1` (`:808–810`) | `QUEUED` |
| `SETTLED` | `finish_message` on an excluded chat | `:807` | no row | `SETTLED` |
| `QUEUED` | drain starts on it | row listed (`:487`) | `attempts → 1` (`:495`, repo `:528`), then send | `IN_FLIGHT` |
| `RETRYING` | drain starts on it | row listed | `attempts + 1`, duplicate check because `job['attempts']` was non-zero (`:521–526`) | `IN_FLIGHT` |
| `IN_FLIGHT` | turn accepted | `:532–536` | `finish_memory_job` deletes the row (`:505`, repo `:534`) | `SETTLED` |
| `IN_FLIGHT` | duplicate found remotely | `:525–526` | return without sending; `finish_memory_job` | `SETTLED` |
| `IN_FLIGHT` | turn unusable | `memory_turn` None, form/repair, pasted-only (`:511–519`) | return; `finish_memory_job` | `SETTLED` |
| `IN_FLIGHT` | `MemoryRejected` | `:502–504` | swallowed; `finish_memory_job` | `SETTLED` (dropped) |
| `IN_FLIGHT` | `MemoryUnavailable` | none — it escapes (`:497`, `:501`) | row keeps `attempts`; the whole drain aborts | `RETRYING` |
| `IN_FLIGHT` | unexpected exception | none | same as above; `_sync_memory` logs only its kind (`chat.py:448–451`) | `RETRYING` |
| `IN_FLIGHT` | task cancelled (shutdown, `chat.py:250`) | none | row keeps `attempts`; next start checks for its own copy | `RETRYING` |
| `QUEUED`/`RETRYING` | exclusion, or conversation delete | `_forget_in_memory` (`repo:231`) | `turn` rows deleted; `forget` inserted if `memory_used = 1` (`:232–234`) | `SETTLED` + new `forget` `QUEUED` |
| `forget` `QUEUED` | drain starts on it | row listed | `attempts + 1`; `forget_session` (`:498–499`) | `IN_FLIGHT` |
| `forget` `IN_FLIGHT` | `forget_session` finished or rejected | `:499`, `:502` | `finish_memory_job` | `SETTLED` |
| `forget` `IN_FLIGHT` | `forget_session` raises `MemoryUnavailable` | none | row kept | `RETRYING` |
| `QUEUED`/`RETRYING` | memory disabled | `:485–486` | all `turn` rows deleted; `forget` rows still drained (`:487–506`) | `SETTLED` / unchanged |
| any | `forget_everything` (outbox) | lock held (`:478`) | `drop_memory_turns`, then remote wipe | empty |
| `SETTLED` | same reply settled again | `:804–807` | new row with `attempts = 0`, so no duplicate check | `QUEUED` |

### Coalescer

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | `schedule_memory_sync` | outbox present (`:434`) | `_memory_pending = True`, task created | `RUNNING` |
| `RUNNING` | `schedule_memory_sync` | task not done (`:437`) | `_memory_pending = True` only | `RUNNING_PENDING` |
| `RUNNING_PENDING` | drain returns | loop condition (`:442`) | pending cleared, drain again | `RUNNING` |
| `RUNNING` | drain returns | pending false | task ends | `IDLE` |
| `RUNNING` | drain raises `MemoryUnavailable` | `:446` | `_sync_memory` returns; a request made during the drain stays true | `STRANDED` |

## Invariants

1. **The pause gates background work only.** `request` refuses a non-explicit call
   without any HTTP traffic (`memory.py:209`), and an explicit review call is always
   attempted; any answered request ends the pause (`:216`, `:222`). Pinned by
   `test_an_object_honcho_says_is_missing_ends_the_pause` and
   `test_memory.ForgettingTests.test_review_actions_are_tried_while_background_memory_is_paused`.
2. **Readiness is monotone for the life of the process.** `_ready` is only ever set
   (`memory.py:246`); no failure resets it, so a workspace that disappears is not
   recreated until restart. Pinned by
   `test_a_workspace_honcho_no_longer_has_drops_a_turn_for_good`.
3. **A row's attempt is counted before its first remote call.** `start_memory_job`
   runs at `memory.py:495`, before `forget_session`/`_send_turn` at `:499–501`.
   Pinned by `test_a_failed_send_counts_its_attempt_and_keeps_the_row`.
4. **Every attempt after the first looks for its own copy before sending.**
   `job['attempts']` is the value from before the increment, so attempt 1 always
   queries and attempt 0 never does (`memory.py:521–526`); the query key is the same
   `blackrose_message` identity written at `:527–528`. Pinned by
   `test_a_drain_cancelled_mid_send_retries_with_a_duplicate_check`.
5. **A row leaves the table only by settling, exclusion, or deletion of its chat.**
   `finish_memory_job` (`repository.py:531–535`), `_forget_in_memory` (`:231`) and
   `drop_memory_turns` (`:541`) are the only deletes.
6. **A `forget` row always sorts after the `turn` rows it must cover.** Rows are
   drained in `id` order (`repository.py:523`) and a drain holds `_lock` for the whole
   pass (`memory.py:472–476`), so a turn whose row is converted while it is being sent
   is delivered before, never after, the forget that removes it. Pinned by
   `test_a_turn_a_drain_is_sending_is_removed_by_the_forget_that_follows`.
7. **Exclusion is re-checked at send time.** `memory_turn` joins
   `conversations.memory_excluded = 0` and `state = complete`
   (`repository.py:547–549`), so a turn fetched before a delete or exclusion is not
   sent afterwards unless it was already past that check.
8. **The profile mark precedes the first destructive delete on every forget path.**
   `forget_note` marks then deletes (`memory.py:371–373`); `forget_session` marks
   before its first delete when the session has notes (`:411–414`). Pinned by
   `StaleProfileStateTests.test_a_forget_interrupted_after_the_mark_keeps_the_profile_hidden`
   and `test_memory.ForgettingTests.test_forgetting_a_conversation_leaves_the_profile_out_until_rebuilt`.
9. **Recall omits the profile exactly when the mark is set.** `memory.py:268–269`.
   Pinned by the same tests, which clear the mark and see the profile return.
10. **Blackrose clears the mark only when it empties the profile**, in
    `forget_everything` (`memory.py:447–452`); `rebuild_card` only sets it
    (`:378–381`). Pinned by `test_a_rebuild_request_only_sets_the_mark`.
11. **A settled remembered turn always has a row or a completed send.** `finish_message`
    inserts inside the settling transaction (`repository.py:804–807`); the coalescer
    only postpones work because each drain re-reads the table
    (`memory.py:487`, `:506`). Pinned by
    `test_a_failed_sync_leaves_every_settled_turn_for_the_next_one`.
12. **Two siblings complete after the first.** A request made while a drain runs is
    served by the same task's next loop iteration (`chat.py:442–443`). Pinned by
    `test_a_turn_settled_during_a_drain_is_sent_by_the_same_sync`.
13. **Nothing is sent while memory is wiped.** `forget_everything` holds `_lock`
    (`memory.py:478–482`) and drops unsent turns first (`:481`).

## Hazards and open questions

| # | Severity | Location | Hazard | Test |
| --- | --- | --- | --- | --- |
| H1 | `fragile` | `memory.py:218–220`, `:491`; `chat.py:446` | A rejection sets no pause because "repeating cannot succeed", but `_drain` calls `ensure_ready` outside the per-row `try`, so a setup that is permanently refused is retried once per drain forever and **no queued row ever settles**. One HTTP round trip per turn, no backoff, no user-visible signal. | `test_a_rejected_setup_repeats_without_a_backoff_and_settles_nothing` |
| H2 | `fragile` | `memory.py:218`, `:233`, `:246` | If Honcho loses the workspace (404 on the session POST), the turn is `MemoryRejected` and dropped while `_ready` stays `True`, so later turns are dropped too until the process restarts: silent loss, not a pause. | `test_a_workspace_honcho_no_longer_has_drops_a_turn_for_good` |
| H3 | `fragile` | `memory.py:495`, `:497–506`; `repository.py:528` | `attempts` is unbounded and never used as a cap, and a `MemoryUnavailable` aborts the pass, so a permanently failing head row (401/403/5xx) is retried forever and every later row waits behind it. The pause bounds the retry rate (one per 300 s), not the queue. | `test_a_failed_send_counts_its_attempt_and_keeps_the_row` (row retention); no bound exists to test |
| H4 | `fragile` | `memory.py:501`; `chat.py:448–451` | Any exception that is not `MemoryRejected`/`MemoryUnavailable` escapes `_drain` and skips `finish_memory_job` for the current row and every later row in the pass; `_sync_memory` logs only its kind. The rows are durable, so this is containment, not loss. | `test_an_unexpected_error_stops_the_drain_and_keeps_every_row` |
| H5 | `documentation-gap` (fixed here) | `memory.py:213–217` | A `missing_ok` 404 — an answer from a reachable Honcho — used to return before clearing `_retry_at`, so opening the review sheet during a pause left background memory paused for the rest of the window. | `test_an_object_honcho_says_is_missing_ends_the_pause` (fails before the fix, passes after) |
| H6 | `fragile` | `memory.py:304–329` | `save_card` writes the profile the user just edited but never clears the mark, so an explicit profile save is invisible to recall until an external rebuild. Whether an explicit save is itself approval is a policy decision, so no change was made. | `test_a_saved_profile_stays_out_of_recall_while_the_mark_holds` |
| H7 | `documentation-gap` | `memory.py:378–381`, `:452`; `docs/memory.md` | `STALE` is absorbing inside Blackrose: only `forget_everything` clears it here, `rebuild_card` sets it. If no maintenance job runs, the profile is never recalled again. The bead's premise that `overview()` rebuilds and clears it is not this codebase. | `test_a_rebuild_request_only_sets_the_mark` |
| H8 | `fragile` | `chat.py:442–451` | `_memory_pending` is cleared at the top of the loop and a failing drain returns without re-reading it, so a turn settled during a failing drain is sent by the *next* sync, not this one (`STRANDED`). No data is lost because the row is durable. | `test_a_failed_sync_leaves_every_settled_turn_for_the_next_one` |
| H9 | `documentation-gap` | `memory.py:485–487` | With memory switched off, a drain still contacts Honcho to settle `forget` rows (privacy wins over "off means no traffic"). Deliberate per `forget_in_memory`, but not stated in `docs/memory.md`; the background path retries it forever with no notice. | `test_memory_off_drops_turns_but_still_forgets_an_excluded_chat` |
| H10 | `none-found` | `memory.py:472–476`, `:494–506`; `repository.py:523` | The in-flight exclusion window (a drain holds a turn the user then converts to `forget`) is safe: the turn may be delivered, and the later forget deletes the session. | `test_a_turn_a_drain_is_sending_is_removed_by_the_forget_that_follows` |
| H11 | `fragile` | `memory.py:434–452` | `forget_everything` deletes sessions and conclusions before it empties the profile and never sets the mark, so a failure in the conclusion rounds leaves the old profile served by recall while its sources are gone — and `stale` stays false. | `test_a_forget_everything_that_fails_leaves_the_old_profile_in_recall` |
| H12 | `fragile` | `repository.py:804–807`; `memory.py:521` | The duplicate check keys on `attempts > 0`, which only exists while the row does. Settling the same reply again (the retry at `turns.py:790–800` after a failed write, or any second `finish_message`) re-inserts the row with `attempts = 0` and skips the check, so the turn is sent twice. | `test_settling_the_same_turn_twice_sends_it_twice` |
| H13 | `documentation-gap` | `memory.py:341–344` | `mark_card_stale` reads metadata and writes it back whole, so a concurrent maintenance job that clears the mark between the read and the write is clobbered; the card stays out of recall after a rebuild. Last-writer-wins across two processes; no in-process test can force it. | not demonstrated (needs a concurrent external writer) |

## Formalization decisions

Changed (smallest fix for a demonstrated defect):

- `memory.py:213–217`: an answered `missing_ok` 404 now clears `_retry_at`, because the
  state machine's own rule is "any answered request ends the pause" and this path
  answered while leaving the client `WARM_PAUSED`. Regression test
  `test_an_object_honcho_says_is_missing_ends_the_pause` fails without the change.

Proposed, not made (each touches a shared file or a policy question):

1. **Workspace loss (H2).** On a 404/410 for a session or workspace path, clear `_ready`
   and re-run `ensure_ready` once before settling the row. This belongs in `_drain`
   (`memory.py`), but the decision "retry once, then drop" changes the durable-outbox
   contract, so it is proposed rather than taken here.
2. **A bounded attempt policy (H3).** Add `memory_outbox.error`/a cap in a migration and
   drop or quarantine a row after N attempts, with the count surfaced in the memory
   status notice. Needs `migrations.py` + `repository.py` (shared).
3. **Per-row containment (H4/H1).** Split setup failure from row failure: catch
   `MemoryRejected` around `ensure_ready` and either pause (a workspace-level refusal is
   not a per-request refusal) or disable the outbox for the process with a logged
   reason. Needs `chat.py` for the notice.
4. **Clearing the mark on an explicit save (H6).** `save_card` would clear
   `card_stale_since` because the user approved every line they kept; that is a product
   decision about whether a curated profile outranks a pending rebuild.
5. **Empty the profile before deleting sources (H11).** Hoist the `:447–449` branch
   above the session loop in `forget_everything`, so a partial wipe cannot leave a
   profile in recall. Deliberately not done here: it changes the documented order
   ("notes before profile") without an ADR.
6. **Always deduplicate (H12).** Query `messages/list` on the first attempt too, or
   record a sent marker on the message row. Costs one request per turn; needs a
   decision on whether that is worth it.
7. **Re-check the coalescer flag (H8).** Have `_sync_memory` re-read `_memory_pending`
   after a caught failure instead of returning; `chat.py` is shared.
8. **Document H9** in `docs/memory.md`: switching memory off discards unsent turns but
   does not cancel a pending forget.

No other source change: the drain's at-least-once behaviour, the mark-before-delete
rule and the lock/ordering argument all hold as written.

## Verification

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_17_memory_outbox.py -v
# Ran 16 tests in 0.208s — OK

.venv/bin/python scripts/check_unit_tests.py -p test_memory.py
# Ran 47 tests in 0.582s — OK

.venv/bin/python scripts/check_unit_tests.py -p test_sm_02_stored_message_lifecycle.py
# Ran 14 tests in 0.124s — OK

.venv/bin/python scripts/check_unit_tests.py -p test_saved_compatibility.py
# Ran 6 tests in 0.756s — OK

.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
# Ran 28 tests in 0.530s — OK

.venv/bin/python scripts/check_unit_tests.py -p test_prompt_history.py
# Ran 2 tests in 0.023s — OK
```

The regression test for the change was confirmed to fail without it: reverting
`memory.py:213–217` and running
`scripts/check_unit_tests.py -p test_sm_17_memory_outbox.py -k ends_the_pause` gives
`AssertionError: False is not true` at `self.assertTrue(memory.available)`.

Not run: the full `scripts/check_health.py` gate (outside this bead's scope and the
shared working tree carries other in-flight changes), the `--gui` probe (needs
GTK/WebKit), and any live Honcho request. H13 needs a real concurrent maintenance job
to reproduce.
