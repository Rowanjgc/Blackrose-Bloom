# SM-17: Honcho memory client backoff, peer-card staleness and the durable outbox

## Scope and ownership

- `src/blackrose/memory.py`: `src/blackrose/memory.py::HonchoMemory` — one loopback HTTP client for the
  few Honcho v3 operations Blackrose uses; `src/blackrose/memory.py::MemoryOutbox` — the durable send
  queue; module constants `src/blackrose/memory.py::BACKOFF_SECONDS` and `src/blackrose/memory.py::CARD_STALE`.
- `src/blackrose/storage/repository.py`: `src/blackrose/storage/repository.py::_forget_in_memory`,
  `src/blackrose/storage/repository.py::Repository.delete_conversation`, `src/blackrose/storage/repository.py::Repository.set_memory_excluded`, `src/blackrose/storage/repository.py::Repository.memory_jobs`,
  `src/blackrose/storage/repository.py::Repository.start_memory_job`, `src/blackrose/storage/repository.py::Repository.finish_memory_job`,
  `src/blackrose/storage/repository.py::Repository.drop_memory_turns`, `src/blackrose/storage/repository.py::Repository.memory_turn`, and the queueing branch of
  `src/blackrose/storage/repository.py::Repository.finish_message` (the `remember and state == MessageState.COMPLETE` insert).
- `src/blackrose/storage/migrations.py`: migration 9 in `src/blackrose/storage/migrations.py::MIGRATIONS`, which creates
  `memory_outbox`, the two conversation flags and the installation id.
- `src/blackrose/chat.py`: `MemoryOutbox` construction in `src/blackrose/chat.py::ChatController.__init__`, `src/blackrose/chat.py::ChatController.schedule_memory_sync`,
  `src/blackrose/chat.py::ChatController._sync_memory`, `src/blackrose/chat.py::ChatController.set_memory_excluded`, `src/blackrose/chat.py::ChatController.forget_memory`,
  deletion scheduling in `src/blackrose/chat.py::ChatController._delete_current_conversation`, shutdown cancellation in `src/blackrose/chat.py::ChatController.shutdown`.
- `src/blackrose/gui/session.py`: the explicit surfaces — toggle (`src/blackrose/gui/session.py::Session.set_memory`), exclusion
  (`src/blackrose/gui/session.py::Session.set_memory_excluded`), review sheet reads and writes (`src/blackrose/gui/session.py::Session.memory_review`,
  `src/blackrose/gui/session.py::Session.set_memory_card`, `src/blackrose/gui/session.py::Session.delete_memory_note`, `src/blackrose/gui/session.py::Session.rebuild_memory_profile`,
  `src/blackrose/gui/session.py::Session.forget_memory`). Recall is gated by the `remember` flag computed in
  `src/blackrose/turns.py::TurnRunner._run` and checked in `src/blackrose/turns.py::TurnRunner._prepare`;
  `src/blackrose/turns.py::TurnRunner._schedule_follow_ups` schedules a sync after a stored reply.

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[ADR 0001](../adr/0001-desktop-ownership.md):

- **One desktop process owns `chats.db`.** Every repository call is submitted through
  `Database.run`, which serialises callbacks on one worker thread
  (`storage/db.py`). Rows therefore mutate in submission order and there is no
  cross-process writer while the desktop runs.
- **The application asyncio loop owns the client and the outbox.** `ChatController`
  holds the only `HonchoMemory` and the only `MemoryOutbox`. `_memory_task`
  (set in `src/blackrose/chat.py::ChatController.schedule_memory_sync`) is the only drain task; `MemoryOutbox._lock` (`src/blackrose/memory.py::MemoryOutbox.__init__`) serialises
  drains against `forget_everything`. `_ready`/`_retry_at` are touched only from this
  loop, so they need no lock of their own beyond `_ready_lock` (`src/blackrose/memory.py::HonchoMemory.__init__`).
- **Honcho's peer metadata is external state with a second owner.** `card_stale_since`
  lives in the user peer's metadata (`src/blackrose/memory.py::CARD_STALE`); Blackrose sets it and only
  an external maintenance job clears it after rebuilding the profile
  ([memory.md](../memory.md#the-profile-after-forgetting)). This document treats that
  timer as part of the state machine, because recall depends on it.
- **Memory is an enhancement, never a precondition.** Every failure path either keeps a
  durable row or degrades to no memory ([architecture](../architecture.md#modules-and-invariants)).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `_retry_at` | `src/blackrose/memory.py::HonchoMemory.__init__`, read by `src/blackrose/memory.py::HonchoMemory.available` | `float` (monotonic seconds) | Instant before which background requests are refused | `src/blackrose/memory.py::HonchoMemory.request`: cleared on a 2xx and on an answered `missing_ok` 404, set to `now + 300` on any transport/HTTP/JSON failure |
| `_ready` | `src/blackrose/memory.py::HonchoMemory.__init__`, `src/blackrose/memory.py::HonchoMemory.ensure_ready` | `bool` | Workspace and peers were created this process | `ensure_ready` only; never reset |
| `_ready_lock` | `src/blackrose/memory.py::HonchoMemory.__init__` | `asyncio.Lock` | Single-flight setup | `src/blackrose/memory.py::HonchoMemory.ensure_ready` |
| `available` | `src/blackrose/memory.py::HonchoMemory.available` | derived `bool` | `clock() >= _retry_at`; the only gate on background requests (the first check in `src/blackrose/memory.py::HonchoMemory.request`) | computed |
| `_clock` | `src/blackrose/memory.py::HonchoMemory.__init__` | callable | Test seam for monotonic time | constructor |
| peer metadata `card_stale_since` | set `src/blackrose/memory.py::HonchoMemory.mark_card_stale`, read `src/blackrose/memory.py::HonchoMemory.card_stale`/`src/blackrose/memory.py::HonchoMemory.recall`, cleared `src/blackrose/memory.py::HonchoMemory.forget_everything` | ISO-8601 UTC `str` in JSON | Profile may state what has been forgotten; recall omits the profile while set | `mark_card_stale` for every forget path; cleared by `forget_everything` here and by the external maintenance job |
| peer card (`peer_card`) | `src/blackrose/memory.py::HonchoMemory.card`, `src/blackrose/memory.py::HonchoMemory.set_card`, `src/blackrose/memory.py::HonchoMemory.forget_everything` | JSON list of `str` | The profile Honcho shows and recall prefers | `save_card`, `forget_everything`, Honcho dreams |
| `conversations.memory_excluded` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 9); `src/blackrose/storage/repository.py::Repository.set_memory_excluded` | `INTEGER 0/1` | Chat contributes nothing and is not recalled | `set_memory_excluded`; read by `src/blackrose/storage/repository.py::Repository.memory_turn` and `src/blackrose/storage/repository.py::Repository.finish_message` |
| `conversations.memory_used` | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 9); `src/blackrose/storage/repository.py::Repository.finish_message` | `INTEGER 0/1` | A turn was queued, so a Honcho session may exist to delete | `finish_message`, only when the queue insert changed a row |
| `memory_outbox` row | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 9) | `(kind, conversation_id, message_id, attempts, created_at)` | One durable obligation; `UNIQUE(kind, conversation_id, message_id)` | inserted by `src/blackrose/storage/repository.py::Repository.finish_message` and `src/blackrose/storage/repository.py::_forget_in_memory`; `attempts+1` by `src/blackrose/storage/repository.py::Repository.start_memory_job`; deleted by `src/blackrose/storage/repository.py::Repository.finish_memory_job`, `src/blackrose/storage/repository.py::_forget_in_memory`, `src/blackrose/storage/repository.py::Repository.drop_memory_turns` |
| `MemoryOutbox._lock` | `src/blackrose/memory.py::MemoryOutbox.__init__` | `asyncio.Lock` | A drain and a forget-everything never interleave | `src/blackrose/memory.py::MemoryOutbox.drain`, `src/blackrose/memory.py::MemoryOutbox.forget_everything` |
| `_memory_task` | `src/blackrose/chat.py::ChatController.__init__`, `src/blackrose/chat.py::ChatController.schedule_memory_sync`, `src/blackrose/chat.py::ChatController.shutdown` | `asyncio.Task \| None` | The running drain task, if any | `schedule_memory_sync`, `shutdown` |
| `_memory_pending` | `src/blackrose/chat.py::ChatController.__init__`, `src/blackrose/chat.py::ChatController.schedule_memory_sync`, `src/blackrose/chat.py::ChatController._sync_memory` | `bool` | A sync was requested while a drain was running | `schedule_memory_sync`, `_sync_memory` |
| installation id | `src/blackrose/storage/migrations.py::MIGRATIONS` (migration 9); read `src/blackrose/storage/repository.py::Repository.installation_id` | 12 hex chars | Names this database's sessions: `bb-<id>-<conversation>` (`src/blackrose/memory.py::MemoryOutbox._drain`) | migration 9 only |

## States

The client has two orthogonal variables, so four states; `available` is the gate
background requests see (the first check in `src/blackrose/memory.py::HonchoMemory.request`).

| State | Predicate |
| --- | --- |
| `COLD` | `not _ready and available` |
| `COLD_PAUSED` | `not _ready and not available` |
| `WARM` | `_ready and available` |
| `WARM_PAUSED` | `_ready and not available` |

The profile (`recall`'s view, the `CARD_STALE` check in `src/blackrose/memory.py::HonchoMemory.recall`):

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

The coalescer (`src/blackrose/chat.py::ChatController.schedule_memory_sync` and `src/blackrose/chat.py::ChatController._sync_memory`):

| State | Predicate |
| --- | --- |
| `IDLE` | task is `None` or done |
| `RUNNING` | task running, `_memory_pending` false |
| `RUNNING_PENDING` | task running, `_memory_pending` true; the loop drains again |
| `STRANDED` | `_memory_pending` true with no running task — reachable only through a failure return in `src/blackrose/chat.py::ChatController._sync_memory`; the next `schedule_memory_sync` repairs it |

## Events

| Event | Source | Effect on this machine |
| --- | --- | --- |
| A reply settles `COMPLETE` with `remember=True` | `src/blackrose/turns.py::TurnRunner._complete`, `src/blackrose/storage/repository.py::Repository.finish_message` | inserts a `turn` row, sets `memory_used = 1`, then `schedule_memory_sync` (`src/blackrose/turns.py::TurnRunner._schedule_follow_ups`) |
| `schedule_memory_sync()` | turn settle, conversation delete (`src/blackrose/chat.py::ChatController._delete_current_conversation`), memory switched off (`src/blackrose/gui/session.py::Session.set_memory`), startup (`src/blackrose/runtime.py::Runtime.open`) | coalesces into `_memory_task` |
| `drain()` reaches the queue | `src/blackrose/chat.py::ChatController._sync_memory` | drops turns if disabled, lists jobs, ensures readiness, settles rows |
| `ensure_ready()` | `recall`, `save_card`, `rebuild_card`, `src/blackrose/memory.py::MemoryOutbox._drain`, status (`src/blackrose/gui/session.py::Session._memory_status`) | creates workspace/peers once |
| Honcho 2xx | any request | `_retry_at = 0` |
| Honcho `missing_ok` 404 | `card`, `delete_conclusion`, list endpoints | treated as success; `_retry_at = 0` |
| Honcho 400/404/409/413/422 | any request | `MemoryRejected` — the *request* is terminal, no pause |
| Transport error, 5xx, bad JSON | any request | `MemoryUnavailable`, `_retry_at = now + 300` |
| User excludes a chat / deletes it | `src/blackrose/storage/repository.py::Repository.set_memory_excluded`, `src/blackrose/storage/repository.py::Repository.delete_conversation` (both through `_forget_in_memory`) | deletes its `turn` rows, inserts a `forget` row when `memory_used = 1` |
| Memory switched off | `src/blackrose/chat.py::ChatController.set_memory_enabled`, `src/blackrose/gui/session.py::Session.set_memory` | next drain drops all `turn` rows |
| Review sheet: forget one note / rebuild profile | `src/blackrose/memory.py::HonchoMemory.forget_note`, `src/blackrose/memory.py::HonchoMemory.rebuild_card` | sets `card_stale_since`, then deletes the note (forget) or does nothing else (rebuild) |
| Forget everything (native confirmation) | `src/blackrose/chat.py::ChatController.forget_memory` → `src/blackrose/memory.py::MemoryOutbox.forget_everything` | takes the lock, drops `turn` rows, deletes sessions, conclusions, profile, mark |
| Shutdown | `src/blackrose/chat.py::ChatController.shutdown` | cancels `_memory_task`; rows stay queued |
| Backoff window elapses | clock | `WARM_PAUSED → WARM`, `COLD_PAUSED → COLD` with no request |
| External profile rebuild | external process | `STALE → FRESH` without any Blackrose code running |

## Transition table

### Client readiness and backoff

Guards and effects are in `src/blackrose/memory.py::HonchoMemory.request` unless the
row names `src/blackrose/memory.py::HonchoMemory.ensure_ready` or
`src/blackrose/memory.py::HonchoMemory.available`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `COLD` | `ensure_ready` | `not self._ready` (both checks in `ensure_ready`) | workspace, config, peers created; `_ready = True` at the end of `ensure_ready` | `COLD`→`WARM` |
| `COLD` | background request during an outage | `not available` (the first check in `request`) | raises `MemoryUnavailable`, no HTTP call | `COLD_PAUSED` |
| `COLD` | rejection status | `missing_ok` false (the rejection-status branch) | `MemoryRejected`; `_retry_at` untouched | `COLD` |
| `WARM` | transport error, 5xx, bad JSON | any request (the `except (httpx.HTTPError, ValueError)` handler) | `_retry_at = now + 300`; `MemoryUnavailable` | `WARM_PAUSED` |
| `WARM_PAUSED` | background request | the `not explicit and not self.available` check | refused without an HTTP call | `WARM_PAUSED` (self) |
| `WARM_PAUSED` | explicit request (`explicit=True`) | that check skipped | request is made | `WARM_PAUSED` |
| `WARM_PAUSED` | any 2xx | after `raise_for_status` | `_retry_at = 0` | `WARM` |
| `WARM_PAUSED` | `missing_ok` 404 | the `missing_ok and status_code == 404` branch | `_retry_at = 0`; `None` returned | `WARM` |
| `WARM_PAUSED` | rejection status | the rejection-status branch | `MemoryRejected`; no pause change | `WARM_PAUSED` |
| `WARM_PAUSED` | 300 s elapse | `clock() >= _retry_at` (`src/blackrose/memory.py::HonchoMemory.available`) | availability only | `WARM` |
| `WARM` | Honcho loses the workspace (404) | the rejection-status branch | `MemoryRejected`; `_ready` stays `True` (`ensure_ready` returns early) | `WARM` |

### Profile staleness

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `FRESH` | `forget_note` | valid conclusion id (`src/blackrose/memory.py::HonchoMemory.forget_note`) | metadata read, `card_stale_since` written (`src/blackrose/memory.py::HonchoMemory.mark_card_stale`), then delete | `STALE` |
| `FRESH` | `forget_session` finds notes | `own` non-empty (`src/blackrose/memory.py::HonchoMemory.forget_session`) | mark written *before* the first delete | `STALE` |
| `FRESH` | `forget_session` finds no notes | `own` empty | no mark, session deleted only (the final `DELETE` of the session) | `FRESH` |
| `FRESH` | `rebuild_card` | none (`src/blackrose/memory.py::HonchoMemory.rebuild_card`) | `ensure_ready`, then mark set | `STALE` |
| `STALE` | `forget_note` / `forget_session` / `rebuild_card` | the mark in `forget_session`, `mark_card_stale` | new timestamp written; nothing cleared | `STALE` (self) |
| `STALE` | `save_card` | none | notes and profile replaced; mark untouched (`src/blackrose/memory.py::HonchoMemory.save_card`) | `STALE` (self) |
| `STALE` | `forget_everything` completes | profile emptied (the `if await self.card()` branch of `src/blackrose/memory.py::HonchoMemory.forget_everything`) | `metadata.pop(CARD_STALE)` at the end of `forget_everything` | `FRESH` |
| `STALE` | the maintenance job rebuilds and clears | external, value-checked | nothing in Blackrose runs | `FRESH` |
| any | `forget_everything` fails mid-way | the `for … else: raise MemoryUnavailable` of its conclusion rounds | mark is *not* set by this path; profile and remaining notes stay | unchanged |

### Outbox rows and the drain

The drain is `src/blackrose/memory.py::MemoryOutbox._drain` and the send is
`src/blackrose/memory.py::MemoryOutbox._send_turn`; repository rows change in the
named `Repository` methods.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `SETTLED` | `finish_message(COMPLETE, remember=True)` | `c.memory_excluded = 0` in the queue insert of `src/blackrose/storage/repository.py::Repository.finish_message` | insert row, `memory_used = 1` when the insert changed a row | `QUEUED` |
| `SETTLED` | `finish_message` on an excluded chat | the same join | no row | `SETTLED` |
| `QUEUED` | drain starts on it | row listed by `memory_jobs` | `attempts → 1` (`src/blackrose/storage/repository.py::Repository.start_memory_job`), then send | `IN_FLIGHT` |
| `RETRYING` | drain starts on it | row listed | `attempts + 1`, duplicate check because `job['attempts']` was non-zero (the `if job['attempts']` query in `_send_turn`) | `IN_FLIGHT` |
| `IN_FLIGHT` | turn accepted | the session and message `POST`s at the end of `_send_turn` | `finish_memory_job` deletes the row (`src/blackrose/storage/repository.py::Repository.finish_memory_job`) | `SETTLED` |
| `IN_FLIGHT` | duplicate found remotely | the duplicate query returns items | return without sending; `finish_memory_job` | `SETTLED` |
| `IN_FLIGHT` | turn unusable | `memory_turn` None, form/repair, pasted-only (the early returns in `_send_turn`) | return; `finish_memory_job` | `SETTLED` |
| `IN_FLIGHT` | `MemoryRejected` | the `except MemoryRejected` in `_drain` | swallowed; `finish_memory_job` | `SETTLED` (dropped) |
| `IN_FLIGHT` | `MemoryUnavailable` | none — it escapes `_drain` from `forget_session` or `_send_turn` | row keeps `attempts`; the whole drain aborts | `RETRYING` |
| `IN_FLIGHT` | unexpected exception | none | same as above; `_sync_memory` logs only its kind (its `except Exception`, `src/blackrose/chat.py::ChatController._sync_memory`) | `RETRYING` |
| `IN_FLIGHT` | task cancelled (shutdown, `src/blackrose/chat.py::ChatController.shutdown`) | none | row keeps `attempts`; next start checks for its own copy | `RETRYING` |
| `QUEUED`/`RETRYING` | exclusion, or conversation delete | `src/blackrose/storage/repository.py::_forget_in_memory` | `turn` rows deleted; `forget` inserted if `memory_used = 1` | `SETTLED` + new `forget` `QUEUED` |
| `forget` `QUEUED` | drain starts on it | row listed | `attempts + 1`; `forget_session` | `IN_FLIGHT` |
| `forget` `IN_FLIGHT` | `forget_session` finished or rejected | `forget_session` returns, or the `except MemoryRejected` | `finish_memory_job` | `SETTLED` |
| `forget` `IN_FLIGHT` | `forget_session` raises `MemoryUnavailable` | none | row kept | `RETRYING` |
| `QUEUED`/`RETRYING` | memory disabled | `not self._config.memory_enabled` at the top of `_drain` | all `turn` rows deleted; `forget` rows still drained by the rest of `_drain` | `SETTLED` / unchanged |
| any | `forget_everything` (outbox) | lock held (`src/blackrose/memory.py::MemoryOutbox.forget_everything`) | `drop_memory_turns`, then remote wipe | empty |
| `SETTLED` | same reply settled again | the queue insert in `finish_message` | new row with `attempts = 0`, so no duplicate check | `QUEUED` |

### Coalescer

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | `schedule_memory_sync` | outbox present (`src/blackrose/chat.py::ChatController.schedule_memory_sync`) | `_memory_pending = True`, task created | `RUNNING` |
| `RUNNING` | `schedule_memory_sync` | task not done | `_memory_pending = True` only | `RUNNING_PENDING` |
| `RUNNING_PENDING` | drain returns | the `while self._memory_pending` loop in `src/blackrose/chat.py::ChatController._sync_memory` | pending cleared, drain again | `RUNNING` |
| `RUNNING` | drain returns | pending false | task ends | `IDLE` |
| `RUNNING` | drain raises `MemoryUnavailable` | the `except MemoryUnavailable: return` in `_sync_memory` | `_sync_memory` returns; a request made during the drain stays true | `STRANDED` |

## Invariants

1. **The pause gates background work only.** `request` refuses a non-explicit call
   without any HTTP traffic (the first check in `src/blackrose/memory.py::HonchoMemory.request`),
   and an explicit review call is always attempted; any answered request ends the pause
   (the `missing_ok` 404 branch and the 2xx path both clear `_retry_at`). Pinned by
   `test_an_object_honcho_says_is_missing_ends_the_pause` and
   `test_memory.ForgettingTests.test_review_actions_are_tried_while_background_memory_is_paused`.
2. **Readiness is monotone for the life of the process.** `_ready` is only ever set
   (`src/blackrose/memory.py::HonchoMemory.ensure_ready`); no failure resets it, so a workspace
   that disappears is not recreated until restart. Pinned by
   `test_a_workspace_honcho_no_longer_has_drops_a_turn_for_good`.
3. **A row's attempt is counted before its first remote call.** `start_memory_job`
   runs first in each iteration of `src/blackrose/memory.py::MemoryOutbox._drain`, before
   `forget_session`/`_send_turn`. Pinned by
   `test_a_failed_send_counts_its_attempt_and_keeps_the_row`.
4. **Every attempt after the first looks for its own copy before sending.**
   `job['attempts']` is the value from before the increment, so attempt 1 always
   queries and attempt 0 never does (the `if job['attempts']` query in
   `src/blackrose/memory.py::MemoryOutbox._send_turn`); the query key is the same
   `blackrose_message` identity written into the sent messages' metadata. Pinned by
   `test_a_drain_cancelled_mid_send_retries_with_a_duplicate_check`.
5. **A row leaves the table only by settling, exclusion, or deletion of its chat.**
   `src/blackrose/storage/repository.py::Repository.finish_memory_job`,
   `src/blackrose/storage/repository.py::_forget_in_memory` and
   `src/blackrose/storage/repository.py::Repository.drop_memory_turns` are the only deletes.
6. **A `forget` row always sorts after the `turn` rows it must cover.** Rows are
   drained in `id` order (`src/blackrose/storage/repository.py::Repository.memory_jobs`) and a
   drain holds `_lock` for the whole pass (`src/blackrose/memory.py::MemoryOutbox.drain`), so a
   turn whose row is converted while it is being sent is delivered before, never after,
   the forget that removes it. Pinned by
   `test_a_turn_a_drain_is_sending_is_removed_by_the_forget_that_follows`.
7. **Exclusion is re-checked at send time.** `memory_turn` joins
   `conversations.memory_excluded = 0` and `state = complete`
   (`src/blackrose/storage/repository.py::Repository.memory_turn`), so a turn fetched before a
   delete or exclusion is not sent afterwards unless it was already past that check.
8. **The profile mark precedes the first destructive delete on every forget path.**
   `forget_note` marks then deletes (`src/blackrose/memory.py::HonchoMemory.forget_note`);
   `forget_session` marks before its first delete when the session has notes
   (`src/blackrose/memory.py::HonchoMemory.forget_session`). Pinned by
   `StaleProfileStateTests.test_a_forget_interrupted_after_the_mark_keeps_the_profile_hidden`
   and `test_memory.ForgettingTests.test_forgetting_a_conversation_leaves_the_profile_out_until_rebuilt`.
9. **Recall omits the profile exactly when the mark is set.** The `CARD_STALE` check in
   `src/blackrose/memory.py::HonchoMemory.recall`. Pinned by the same tests, which clear the
   mark and see the profile return.
10. **Blackrose clears the mark only when it empties the profile**, in
    `src/blackrose/memory.py::HonchoMemory.forget_everything`; `rebuild_card` only sets it
    (`src/blackrose/memory.py::HonchoMemory.rebuild_card`). Pinned by
    `test_a_rebuild_request_only_sets_the_mark`.
11. **A settled remembered turn always has a row or a completed send.** `finish_message`
    inserts inside the settling transaction
    (`src/blackrose/storage/repository.py::Repository.finish_message`); the coalescer only
    postpones work because each drain re-reads the table (the `memory_jobs` calls in
    `src/blackrose/memory.py::MemoryOutbox._drain`). Pinned by
    `test_a_failed_sync_leaves_every_settled_turn_for_the_next_one`.
12. **Two siblings complete after the first.** A request made while a drain runs is
    served by the same task's next loop iteration
    (`src/blackrose/chat.py::ChatController._sync_memory`). Pinned by
    `test_a_turn_settled_during_a_drain_is_sent_by_the_same_sync`.
13. **Nothing is sent while memory is wiped.** `forget_everything` holds `_lock`
    (`src/blackrose/memory.py::MemoryOutbox.forget_everything`) and drops unsent turns first.

## Hazards and open questions

| # | Severity | Location | Hazard | Test |
| --- | --- | --- | --- | --- |
| H1 | `fragile` | `src/blackrose/memory.py::HonchoMemory.request` (rejection branch), `src/blackrose/memory.py::MemoryOutbox._drain` (`ensure_ready`); `src/blackrose/chat.py::ChatController._sync_memory` | A rejection sets no pause because "repeating cannot succeed", but `_drain` calls `ensure_ready` outside the per-row `try`, so a setup that is permanently refused is retried once per drain forever and **no queued row ever settles**. One HTTP round trip per turn, no backoff, no user-visible signal. | `test_a_rejected_setup_repeats_without_a_backoff_and_settles_nothing` |
| H2 | `fragile` | `src/blackrose/memory.py::HonchoMemory.request` (rejection branch), `src/blackrose/memory.py::HonchoMemory.ensure_ready` | If Honcho loses the workspace (404 on the session POST), the turn is `MemoryRejected` and dropped while `_ready` stays `True`, so later turns are dropped too until the process restarts: silent loss, not a pause. | `test_a_workspace_honcho_no_longer_has_drops_a_turn_for_good` |
| H3 | `fragile` | `src/blackrose/memory.py::MemoryOutbox._drain`; `src/blackrose/storage/repository.py::Repository.start_memory_job` | `attempts` is unbounded and never used as a cap, and a `MemoryUnavailable` aborts the pass, so a permanently failing head row (401/403/5xx) is retried forever and every later row waits behind it. The pause bounds the retry rate (one per 300 s), not the queue. | `test_a_failed_send_counts_its_attempt_and_keeps_the_row` (row retention); no bound exists to test |
| H4 | `fragile` | `src/blackrose/memory.py::MemoryOutbox._drain`; `src/blackrose/chat.py::ChatController._sync_memory` | Any exception that is not `MemoryRejected`/`MemoryUnavailable` escapes `_drain` and skips `finish_memory_job` for the current row and every later row in the pass; `_sync_memory` logs only its kind. The rows are durable, so this is containment, not loss. | `test_an_unexpected_error_stops_the_drain_and_keeps_every_row` |
| H5 | `documentation-gap` (fixed here) | `src/blackrose/memory.py::HonchoMemory.request` (`missing_ok` 404 branch) | A `missing_ok` 404 — an answer from a reachable Honcho — used to return before clearing `_retry_at`, so opening the review sheet during a pause left background memory paused for the rest of the window. | `test_an_object_honcho_says_is_missing_ends_the_pause` (fails before the fix, passes after) |
| H6 | `fragile` | `src/blackrose/memory.py::HonchoMemory.save_card` | `save_card` writes the profile the user just edited but never clears the mark, so an explicit profile save is invisible to recall until an external rebuild. Whether an explicit save is itself approval is a policy decision, so no change was made. | `test_a_saved_profile_stays_out_of_recall_while_the_mark_holds` |
| H7 | `documentation-gap` | `src/blackrose/memory.py::HonchoMemory.rebuild_card`, `src/blackrose/memory.py::HonchoMemory.forget_everything`; `docs/memory.md` | `STALE` is absorbing inside Blackrose: only `forget_everything` clears it here, `rebuild_card` sets it. If no maintenance job runs, the profile is never recalled again. The premise that `overview()` rebuilds and clears it does not hold in this codebase. | `test_a_rebuild_request_only_sets_the_mark` |
| H8 | `fragile` | `src/blackrose/chat.py::ChatController._sync_memory` | `_memory_pending` is cleared at the top of the loop and a failing drain returns without re-reading it, so a turn settled during a failing drain is sent by the *next* sync, not this one (`STRANDED`). No data is lost because the row is durable. | `test_a_failed_sync_leaves_every_settled_turn_for_the_next_one` |
| H9 | `documentation-gap` | `src/blackrose/memory.py::MemoryOutbox._drain` (the `memory_enabled` check) | With memory switched off, a drain still contacts Honcho to settle `forget` rows (privacy wins over "off means no traffic"). Deliberate per `forget_in_memory`, but not stated in `docs/memory.md`; the background path retries it forever with no notice. | `test_memory_off_drops_turns_but_still_forgets_an_excluded_chat` |
| H10 | `none-found` | `src/blackrose/memory.py::MemoryOutbox.drain`, `src/blackrose/memory.py::MemoryOutbox._drain`; `src/blackrose/storage/repository.py::Repository.memory_jobs` | The in-flight exclusion window (a drain holds a turn the user then converts to `forget`) is safe: the turn may be delivered, and the later forget deletes the session. | `test_a_turn_a_drain_is_sending_is_removed_by_the_forget_that_follows` |
| H11 | `fragile` | `src/blackrose/memory.py::HonchoMemory.forget_everything` | `forget_everything` deletes sessions and conclusions before it empties the profile and never sets the mark, so a failure in the conclusion rounds leaves the old profile served by recall while its sources are gone — and `stale` stays false. | `test_a_forget_everything_that_fails_leaves_the_old_profile_in_recall` |
| H12 | `fragile` | `src/blackrose/storage/repository.py::Repository.finish_message`; `src/blackrose/memory.py::MemoryOutbox._send_turn` | The duplicate check keys on `attempts > 0`, which only exists while the row does. Settling the same reply again (the retry in `src/blackrose/turns.py::TurnRunner._save_interrupted` after a failed write, or any second `finish_message`) re-inserts the row with `attempts = 0` and skips the check, so the turn is sent twice. | `test_settling_the_same_turn_twice_sends_it_twice` |
| H13 | `documentation-gap` | `src/blackrose/memory.py::HonchoMemory.mark_card_stale` | `mark_card_stale` reads metadata and writes it back whole, so a concurrent maintenance job that clears the mark between the read and the write is clobbered; the card stays out of recall after a rebuild. Last-writer-wins across two processes; no in-process test can force it. | not demonstrated (needs a concurrent external writer) |

## Formalization decisions

Changed (smallest fix for a demonstrated defect):

- `src/blackrose/memory.py::HonchoMemory.request`: an answered `missing_ok` 404 now clears
  `_retry_at`, because the state machine's own rule is "any answered request ends the
  pause" and this path answered while leaving the client `WARM_PAUSED`. Regression test
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
5. **Empty the profile before deleting sources (H11).** Hoist the profile-emptying
   branch of `forget_everything` (the `if await self.card()` `PUT`) above the session
   loop, so a partial wipe cannot leave a profile in recall. Deliberately not done here:
   it changes the documented order ("notes before profile") without an ADR.
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
.venv/bin/python scripts/check_unit_tests.py -p test_memory.py
.venv/bin/python scripts/check_unit_tests.py -p test_sm_02_stored_message_lifecycle.py
.venv/bin/python scripts/check_unit_tests.py -p test_saved_compatibility.py
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
.venv/bin/python scripts/check_unit_tests.py -p test_prompt_history.py
```

To confirm the regression test detects the defect, revert the `_retry_at` reset in the
`missing_ok` 404 branch of `src/blackrose/memory.py::HonchoMemory.request` and run
`scripts/check_unit_tests.py -p test_sm_17_memory_outbox.py -k ends_the_pause`: it fails
at `self.assertTrue(memory.available)`.

The suites are offline and use fake Honcho transports. H13 needs a real concurrent
maintenance job to reproduce.
