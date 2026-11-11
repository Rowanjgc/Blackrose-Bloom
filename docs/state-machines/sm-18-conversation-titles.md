# SM-18: Conversation naming, `TitleSource` and the background title task

## Scope and ownership

- `src/blackrose/storage/records.py`: `src/blackrose/storage/records.py::TitleSource`,
  `src/blackrose/storage/records.py::Conversation.title_source`,
  `src/blackrose/storage/records.py::Conversation.is_unnamed`.
- `src/blackrose/storage/repository.py`: `src/blackrose/storage/repository.py::DEFAULT_TITLE`,
  `src/blackrose/storage/repository.py::derive_title`,
  `src/blackrose/storage/repository.py::_conversation`,
  `src/blackrose/storage/repository.py::Repository.create_conversation`,
  `src/blackrose/storage/repository.py::Repository.rename_conversation`.
- `src/blackrose/storage/migrations.py`: the `conversations` table, migration 1 in
  `src/blackrose/storage/migrations.py::MIGRATIONS`.
- `src/blackrose/chat.py`: `_title_task` (`src/blackrose/chat.py::ChatController.__init__`),
  `src/blackrose/chat.py::ChatController.shutdown`, `src/blackrose/chat.py::ChatController.select_model`,
  `src/blackrose/chat.py::ChatController.rename_conversation`,
  `src/blackrose/chat.py::ChatController.cancel_title`,
  `src/blackrose/chat.py::ChatController.schedule_title`,
  `src/blackrose/chat.py::ChatController._name_conversation`, conversation deletion
  (`src/blackrose/chat.py::ChatController._delete_current_conversation`).
- `src/blackrose/titles.py`: `src/blackrose/titles.py::TITLE_TIMEOUT`,
  `src/blackrose/titles.py::TITLE_INSTRUCTIONS`, `src/blackrose/titles.py::title_messages`,
  `src/blackrose/titles.py::clean_title`, `src/blackrose/titles.py::generate_title`.
- Call sites: `src/blackrose/turns.py::TurnRunner._prepare` (cancels),
  `src/blackrose/turns.py::TurnRunner._run` (schedules after a stored reply, including the
  Stop-after-commit path), `src/blackrose/turns.py::TurnRunner._schedule_follow_ups`,
  `src/blackrose/diagram_repairs.py::DiagramRepairs.repair` (cancels),
  `src/blackrose/gui/session.py::Session.close` (close),
  `src/blackrose/gui/session.py::Session.refresh_conversations` (sidebar refresh),
  `src/blackrose/gui/session.py::Session.new_conversation` (new chat),
  `src/blackrose/gui/session.py::Session.delete_conversation` (delete),
  `src/blackrose/gui/session.py::Session.rename` (`/rename`), and
  `src/blackrose/gui/session.py::Session._send` (`on_title=self.refresh_conversations`).

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[ADR 0001](../adr/0001-desktop-ownership.md):

- **The persisted title belongs to the one owning desktop process.** Every read and
  write goes through `Repository` on the serialized `Database` worker
  (`src/blackrose/storage/db.py::Database.run`), so the guarded UPDATE in
  `src/blackrose/storage/repository.py::Repository.rename_conversation` is atomic
  with respect to every other writer in the process; no migration, trigger or second
  process writes `conversations.title`.
- **The in-flight task belongs to the application asyncio loop.** `ChatController` holds
  the only `_title_task`. `titles.py` owns only prompt text, output cleanup and early
  stream closing; `chat.py` owns scheduling, preemption and guarded persistence, as the
  [architecture map](../architecture.md#modules-and-invariants) states. `gui/session.py`
  never touches the task; it only passes `refresh_conversations` as the `on_title`
  callback and cancels on close.
- **Naming is downstream of a durable reply.** Scheduling happens in
  `TurnRunner._schedule_follow_ups`, which `turns.py` calls only after a `COMPLETE`
  reply is stored (the last statement of `src/blackrose/turns.py::TurnRunner._run`, and
  its `except` branch for a Stop that landed after the final save). Nothing in this
  machine can change a reply's outcome; `_name_conversation` writes only the title column.

There are two orthogonal machines: the persisted `title_source` value and the
process-local title task.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `conversations.title` | insert `src/blackrose/storage/repository.py::Repository.create_conversation`; update `src/blackrose/storage/repository.py::Repository.rename_conversation`; read `src/blackrose/storage/repository.py::_conversation` | `TEXT NOT NULL` | Sidebar text | `create_conversation`, `chat.rename_conversation` (through the repo), `_name_conversation` |
| `conversations.title_source` | migration 1 in `src/blackrose/storage/migrations.py::MIGRATIONS` (default `'placeholder'`); insert `src/blackrose/storage/repository.py::Repository.create_conversation`; update `src/blackrose/storage/repository.py::Repository.rename_conversation`; read `src/blackrose/storage/repository.py::_conversation` | `TEXT NOT NULL`, **no CHECK** | Provenance: `placeholder`, `auto`, `manual` | `create_conversation` (either the argument or the equality inference at its start), `rename_conversation` (the caller's `source`) |
| `Conversation.is_unnamed` | `src/blackrose/storage/records.py::Conversation.is_unnamed` | derived `bool` | `title_source is PLACEHOLDER`; the only gate on automatic naming | computed |
| `ChatController._title_task` | init `src/blackrose/chat.py::ChatController.__init__`; cleared `src/blackrose/chat.py::ChatController.cancel_title`; set `src/blackrose/chat.py::ChatController.schedule_title` | `asyncio.Task \| None` | The one tracked naming task | `schedule_title`, `cancel_title` (never by the task itself) |
| model captured for the title | `src/blackrose/chat.py::ChatController.schedule_title` (argument), used in `src/blackrose/chat.py::ChatController._name_conversation` | `str` | The model of the reply that just settled | `schedule_title` at call time |
| `model_name` | `src/blackrose/chat.py::ChatController.select_model` | `str` | The selected chat model; the capture source | `select_model` only, which refuses while a reply is generating (its `is_generating` check) |
| `conversations.title` of a *deleted* row | `repository.py` delete path via `src/blackrose/chat.py::ChatController._delete_current_conversation` | absent | Terminal; ids are `AUTOINCREMENT` (migration 1) and never reused | `delete_conversation` |
| `TITLE_TIMEOUT` | `src/blackrose/titles.py::TITLE_TIMEOUT`, applied in `src/blackrose/chat.py::ChatController._name_conversation` | `float` seconds | Deadline for the provider title round trip (not for `generate_title` itself) | constant |

## States

Persisted, mutually exclusive (`title_source`, with the text as a second discriminator):

| State | Predicate |
| --- | --- |
| `PLACEHOLDER` | `title_source = 'placeholder'` and `title = DEFAULT_TITLE`; `is_unnamed` true |
| `AUTO` | `title_source = 'auto'` and `title != DEFAULT_TITLE`; named by the background task |
| `AUTO_PLACEHOLDER` | `title_source = 'auto'` and `title = DEFAULT_TITLE`; nothing named it, yet naming is over (H1). Since the H1 fix only a row written before it can be in this state |
| `MANUAL` | `title_source = 'manual'`; the user chose the text, including a manual `DEFAULT_TITLE` |
| `DELETED` | no row; terminal for this machine |

`AUTO` and `MANUAL` are named states; the state machine has no transition back to
`PLACEHOLDER` (only the INSERT writes that value). The dataclass default is `AUTO`
(`src/blackrose/storage/records.py::Conversation.title_source`), so an in-memory
`Conversation` built without provenance is never auto-namable — fail-safe by construction.

Process-local task states:

| State | Predicate |
| --- | --- |
| `IDLE` | `_title_task is None` |
| `RUNNING` | `_title_task` set, not done |
| `DONE_TRACKED` | `_title_task` set and done — the resting state after every completed naming, since only `cancel_title` clears the pointer (`src/blackrose/chat.py::ChatController.cancel_title`) |
| `DETACHED` | `_title_task is None` while the previously tracked task is still settling its cancellation; exists only inside `cancel_title` between clearing the pointer and the `await task` |
| `ABANDONED` | a second `schedule_title` replaced the pointer while the first task is still running and untracked (H2) |

## Events

| Event | Source | Effect on this machine |
| --- | --- | --- |
| A chat is created without a title | `src/blackrose/chat.py::ChatController.new_conversation`, `src/blackrose/chat.py::ChatController.ensure_conversation` | row inserted as `PLACEHOLDER` |
| A chat is created with a custom title | `Repository.create_conversation(title=...)` (`src/blackrose/storage/repository.py::Repository.create_conversation`) | row inserted as `MANUAL`; no controller caller passes a title |
| A reply settles `COMPLETE` | `src/blackrose/turns.py::TurnRunner._run` | `_schedule_follow_ups` computes the first exchange and calls `schedule_title` |
| Stop lands after the final save | the `except` branch of `src/blackrose/turns.py::TurnRunner._run` | the same scheduling from the `except` branch, then the error is re-raised |
| `schedule_title(conversation, request, answer, on_title)` | `src/blackrose/turns.py::TurnRunner._schedule_follow_ups` | creates the task when `conversation.is_unnamed` (`src/blackrose/chat.py::ChatController.schedule_title`) |
| A turn starts | `src/blackrose/turns.py::TurnRunner._prepare` | `await cancel_title()` before any new work |
| A model is selected | `src/blackrose/chat.py::ChatController.select_model` | `await cancel_title()` before the old model may be released |
| A diagram repair starts | `src/blackrose/diagram_repairs.py::DiagramRepairs.repair` | `await cancel_title()` under the artifact lock |
| The user renames | `src/blackrose/chat.py::ChatController.rename_conversation` from `/rename` (`src/blackrose/gui/session.py::Session.rename`) | unconditional `source=MANUAL`; no task interaction |
| The conversation is deleted | `src/blackrose/chat.py::ChatController._delete_current_conversation` | row gone; the task is **not** cancelled and observes `None` at its re-read in `_name_conversation` |
| The user navigates to another chat | `src/blackrose/chat.py::ChatController.open_conversation` | no title event; a pending task keeps naming its originating id and refreshes `selected_conversation` only if it is still that chat (the `if changed` branch of `_name_conversation`) |
| The window closes | `src/blackrose/gui/session.py::Session.close`, then `src/blackrose/chat.py::ChatController.shutdown` | `cancel_title()` twice; idempotent |
| The process restarts | — | no task exists; the stored `title_source` and text are read back (`src/blackrose/storage/repository.py::_conversation`), and a `PLACEHOLDER` is retried on the next completed turn |
| The provider returns a title | `src/blackrose/chat.py::ChatController._name_conversation`, `src/blackrose/titles.py::generate_title` | `AUTO` through the guarded UPDATE |
| The provider fails, times out or returns nothing usable | the `except Exception` around `suggest` in `_name_conversation`, `src/blackrose/titles.py::generate_title` | local `derive_title` fallback (`title = title or fallback`) |
| `TITLE_TIMEOUT` elapses | the `asyncio.timeout(TITLE_TIMEOUT)` in `_name_conversation` | `TimeoutError` → the same fallback |
| The provider stream is cancelled | `src/blackrose/chat.py::ChatController.cancel_title` | `CancelledError` propagates out of `_name_conversation`; nothing is written |

## Transition table

### Persisted title state

`create`/`rename` below are `src/blackrose/storage/repository.py::Repository.create_conversation`
and `src/blackrose/storage/repository.py::Repository.rename_conversation`; `name` is
`src/blackrose/chat.py::ChatController._name_conversation`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| — | `create_conversation()` (no title) | `title == DEFAULT_TITLE` and no explicit source (the inference at the start of `create`) | INSERT `('placeholder', DEFAULT_TITLE)` | `PLACEHOLDER` |
| — | `create_conversation(custom)` | no explicit source | INSERT `('manual', custom)` | `MANUAL` |
| — | `create_conversation(DEFAULT_TITLE, title_source=MANUAL)` | explicit source | INSERT `('manual', DEFAULT_TITLE)` | `MANUAL` |
| `PLACEHOLDER` | provider title | row still a placeholder at the re-read in `name`; UPDATE matches the `only_if_unnamed` clause in `rename` | title and `'auto'` written | `AUTO` |
| `PLACEHOLDER` | provider failure/timeout/empty | same | `derive_title(request.content)` written with `'auto'` (the `fallback` in `name`) | `AUTO` |
| `PLACEHOLDER` | unnameable request (H1) | same; `derive_title` returns `DEFAULT_TITLE` (`src/blackrose/storage/repository.py::derive_title`) | return before the provider call; no write (`src/blackrose/chat.py::ChatController._name_conversation`). Before the H1 fix, a provider failure here wrote `DEFAULT_TITLE` with `'auto'` and reached `AUTO_PLACEHOLDER` | `PLACEHOLDER` (unchanged) |
| `PLACEHOLDER` | `/rename` | non-empty after strip and 120-char clip (`src/blackrose/chat.py::ChatController.rename_conversation`) | unconditional UPDATE (source default `MANUAL` in `rename`) | `MANUAL` |
| `PLACEHOLDER` | stale automatic rename after a manual one | `AND title_source = 'placeholder'` fails in `rename` | `rowcount == 0`; no write, no `on_title` (the `if changed` branch of `name`) | `PLACEHOLDER` (unchanged) |
| `PLACEHOLDER` | title task on a missing row | `current is None` (the re-read in `name`) | return; no provider call | `PLACEHOLDER` |
| `AUTO` | `/rename` | non-empty (`src/blackrose/chat.py::ChatController.rename_conversation`) | unconditional UPDATE | `MANUAL` |
| `AUTO` | a second automatic name | guard fails in `rename` | no write | `AUTO` (self) |
| `AUTO_PLACEHOLDER` | `/rename` | non-empty | unconditional UPDATE | `MANUAL` |
| `AUTO_PLACEHOLDER` | a later turn | `is_unnamed` false in `src/blackrose/chat.py::ChatController.schedule_title` | no task scheduled | `AUTO_PLACEHOLDER` (self) |
| `MANUAL` | `/rename` | non-empty | unconditional UPDATE; SQLite counts the row even for identical text | `MANUAL` (self) |
| `MANUAL` | automatic name | guard fails in `rename` | no write | `MANUAL` (self) |
| any | `/rename` with blank text | `src/blackrose/chat.py::ChatController.rename_conversation` | `ValueError`; no write | unchanged (self) |
| any | conversation delete | `src/blackrose/chat.py::ChatController._delete_current_conversation` | row deleted; grant/proposal maps cleaned in the same method | `DELETED` |
| `DELETED` | late automatic name | `current is None` (the re-read in `name`); ids are never reused (migration 1) | return; no INSERT exists on this path | `DELETED` (self) |

### Title task lifecycle

Guards are in `src/blackrose/chat.py::ChatController.schedule_title`,
`src/blackrose/chat.py::ChatController._name_conversation` and
`src/blackrose/chat.py::ChatController.cancel_title` as named.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | `schedule_title` | `conversation.is_unnamed` (`schedule_title`) | `create_task(_name_conversation(..., self.model_name, ...))` | `RUNNING` |
| `IDLE` | `schedule_title` | not `is_unnamed` | nothing; pinned by `test_a_named_conversation_is_never_scheduled` | `IDLE` |
| `RUNNING` | row became named or vanished | the re-read at the start of `_name_conversation` | return before any provider request | `DONE_TRACKED` |
| `RUNNING` | provider title accepted | `clean_title` / `generate_title` (`src/blackrose/titles.py::generate_title`) | guarded UPDATE | `DONE_TRACKED` (+ `AUTO`) |
| `RUNNING` | provider failure, timeout, tool call, multi-line, over-long | the `except Exception` around `suggest`, `src/blackrose/titles.py::generate_title` | fallback title, guarded UPDATE | `DONE_TRACKED` |
| `RUNNING` | guarded UPDATE refused | `if changed` false | no refresh, no `on_title` | `DONE_TRACKED` |
| `RUNNING` | UPDATE succeeded | the `if changed` branch | refresh `selected_conversation` when it is the same chat; `await on_title()` | `DONE_TRACKED` |
| `RUNNING` | any unexpected error | the outer `except Exception` of `_name_conversation` | logged without content; task ends | `DONE_TRACKED` |
| `RUNNING` | `cancel_title` | task not done (`cancel_title`) | pointer cleared, `task.cancel()` | `DETACHED` |
| `DETACHED` | task settles | `cancel_title` awaits it | `await task`; `CancelledError` suppressed unless the *caller* is being cancelled (the `cancelling()` check) | `IDLE` |
| `DONE_TRACKED` | `cancel_title` | `cancel_title` | pointer cleared; awaiting a finished task returns | `IDLE` |
| `RUNNING` | second `schedule_title` | none (`schedule_title`) | pointer replaced; first task neither cancelled nor tracked | `ABANDONED` + `RUNNING` (new) |
| `ABANDONED` | it finishes | none | still writes through the same guarded UPDATE and may call `on_title` | untracked done |
| `DONE_TRACKED`/`IDLE` | window close | `src/blackrose/gui/session.py::Session.close`, `src/blackrose/chat.py::ChatController.shutdown` | `cancel_title` twice | `IDLE` |

### Recovery after cancellation

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `DETACHED` | next completed turn | `is_unnamed` still true (`src/blackrose/chat.py::ChatController.schedule_title`) | a new task names from the *first* stored exchange (`src/blackrose/turns.py::TurnRunner._schedule_follow_ups`) | `RUNNING` |
| `DETACHED` | no further turn | — | the chat stays `PLACEHOLDER` with `DEFAULT_TITLE` | `PLACEHOLDER` (unchanged) |
| `DETACHED` | diagram repair finished, no reschedule | `src/blackrose/diagram_repairs.py::DiagramRepairs.repair` has no counterpart | the chat stays unnamed until the next completed turn | `PLACEHOLDER` (unchanged) |
| `ABANDONED` | window close | `cancel_title` sees only the latest task | the abandoned task can still run into shutdown | untracked done |

## Invariants

1. **`title_source` moves away from `placeholder` at most once and never returns.**
   The INSERT (`src/blackrose/storage/repository.py::Repository.create_conversation`) is the
   only writer of `'placeholder'`; the UPDATE writes a caller-supplied source
   (`src/blackrose/storage/repository.py::Repository.rename_conversation`), and the only
   callers pass `AUTO` (guarded) or `MANUAL`. Pinned by
   `test_automatic_naming_replaces_the_placeholder_exactly_once`
   and `test_a_manual_title_is_terminal_for_automatic_naming`.
2. **Automatic naming may replace a placeholder and nothing else.** The guard is the
   `AND title_source = ?` clause inside the UPDATE
   (`src/blackrose/storage/repository.py::Repository.rename_conversation`), not a
   caller check, so a stale `Conversation` cannot defeat it; the task also re-reads the
   row before spending a provider request (`src/blackrose/chat.py::ChatController._name_conversation`).
   Pinned by `test_a_stale_placeholder_object_cannot_name_a_renamed_conversation` and
   `test_an_inflight_title_cannot_replace_a_manual_title`.
3. **The title text is never a state input after creation.** `is_unnamed` is exactly
   `title_source is PLACEHOLDER` (`src/blackrose/storage/records.py::Conversation.is_unnamed`);
   only the INSERT infers the source from text equality
   (`src/blackrose/storage/repository.py::Repository.create_conversation`). A manual title
   equal to `DEFAULT_TITLE` is `MANUAL`. Pinned by
   `test_a_manual_title_equal_to_the_placeholder_text_is_still_named`.
4. **A rename never creates a row.** Pinned by
   `test_renaming_a_missing_conversation_changes_nothing_and_creates_no_row`.
5. **A late title cannot resurrect or rename a deleted chat.** The task re-reads the
   row (`src/blackrose/chat.py::ChatController._name_conversation`) and the UPDATE matches
   no row; ids are `AUTOINCREMENT` and never reused (migration 1 in
   `src/blackrose/storage/migrations.py::MIGRATIONS`). Pinned by
   `test_a_late_title_cannot_resurrect_a_deleted_conversation` and
   `test_a_deleted_conversation_id_is_never_reused`.
6. **Every path that starts new work cancels the tracked task first.** Turn start
   (`src/blackrose/turns.py::TurnRunner._prepare`), model switch
   (`src/blackrose/chat.py::ChatController.select_model`), diagram repair
   (`src/blackrose/diagram_repairs.py::DiagramRepairs.repair`), window close
   (`src/blackrose/gui/session.py::Session.close`) and shutdown
   (`src/blackrose/chat.py::ChatController.shutdown`). Pinned by
   `test_cancel_title_awaits_the_generation_and_is_idempotent`
   and `test_close_cancels_the_title_before_the_session_is_torn_down`.
7. **`cancel_title()` clears the pointer before awaiting and returns only after the
   task has settled.** A cancelled provider generator has run its `finally` when the
   call returns; a caller that is not itself cancelled does not receive the task's
   `CancelledError` (`src/blackrose/chat.py::ChatController.cancel_title`). Pinned by the
   same two tests.
8. **A title is named by the model captured at scheduling.**
   `src/blackrose/chat.py::ChatController.schedule_title` passes `self.model_name`;
   `_name_conversation` uses that argument, never the current selection. The capture is
   race-free: the successful path schedules right after `is_generating = False` with no
   intervening `await` (the end of `src/blackrose/turns.py::TurnRunner._run`), and the
   Stop-after-commit path (its `except` branch) schedules while `is_generating` is still
   true, when `select_model` refuses (`src/blackrose/chat.py::ChatController.select_model`).
   Pinned by `test_naming_uses_the_model_captured_when_the_title_was_scheduled` and
   `test_titles.test_model_change_preempts_title_and_naming_uses_new_selected_model`.
9. **Naming never changes a reply's outcome or the transcript.** Scheduling follows
   the stored turn (`src/blackrose/turns.py::TurnRunner._run`); every provider error is
   swallowed (the two `except Exception` handlers in
   `src/blackrose/chat.py::ChatController._name_conversation`); only the title column is
   written. Pinned by
   `test_titles.test_reply_finishes_while_title_is_pending_and_sidebar_updates_later`
   and `test_a_nameable_request_is_named_from_the_fallback_when_generation_fails`.
10. **`on_title` runs only when a row changed.** The `if changed` branch of
    `src/blackrose/chat.py::ChatController._name_conversation`. Pinned by
    `test_an_inflight_title_cannot_replace_a_manual_title`.
11. **The title request is bounded, tool-free and content-limited.** 1600 characters of
    request, 1000 of reply, 4 filenames of 180 characters
    (`src/blackrose/titles.py::title_messages`); no document bodies, tool results, skills,
    system prompt or image bytes, and no tool schemas (the empty tool tuple passed to
    `backend.stream` in `src/blackrose/titles.py::generate_title`). Pinned by
    `test_the_title_payload_is_bounded_and_excludes_document_and_prompt_context` and
    `test_titles.test_naming_input_is_small_and_excludes_document_body_and_chat_instructions`.
12. **A refused or unusable title never changes the stored text.** The guarded UPDATE
    either writes the intended title or nothing; there is no way to write an empty
    string (the blank-text check in `src/blackrose/chat.py::ChatController.rename_conversation`,
    and `if changed` in `_name_conversation`). Pinned by the manual-title and late-title tests.
13. **A cancelled title leaves `PLACEHOLDER`, so a later turn retries.** Cancellation
    happens before the UPDATE (`src/blackrose/chat.py::ChatController.cancel_title`), and
    the next turn re-derives the first exchange
    (`src/blackrose/turns.py::TurnRunner._schedule_follow_ups`). Pinned by
    `test_titles.test_followup_preempts_title_and_retries_with_the_original_exchange`
    and by the `PLACEHOLDER` assertion in
    `test_cancel_title_awaits_the_generation_and_is_idempotent`.

## Hazards and open questions

| # | Severity | Location | Hazard | Test |
| --- | --- | --- | --- | --- |
| H1 | `defect` | `src/blackrose/chat.py::ChatController._name_conversation`; `src/blackrose/storage/repository.py::derive_title` | Resolved: `src/blackrose/chat.py::ChatController._name_conversation` now returns before writing when the derived title is `DEFAULT_TITLE`, and `src/blackrose/turns.py::TurnRunner._schedule_follow_ups` names from the current exchange when the first is unnameable. As found: an exchange with no nameable text wrote `DEFAULT_TITLE` with `source='auto'` (`AUTO_PLACEHOLDER`). Reachable from the GUI: an image-only send (`src/blackrose/gui/session.py::Session.submit` allows empty text with attachments) or markup-only text such as `---` (`derive_title` strips to `''`). `is_unnamed` was then false, so the sidebar kept “New conversation” unless the user renamed it, and no later turn retried. Automatic naming ended with nothing named, contradicting the `TitleSource` docstring (`src/blackrose/storage/records.py::TitleSource`). | `tests/test_sm_18_conversation_titles.py::NamingTaskTests.test_an_unnameable_first_exchange_waits_for_a_later_nameable_turn` |
| H2 | `fragile` | `src/blackrose/chat.py::ChatController.schedule_title` | `schedule_title` overwrites `_title_task` without cancelling the previous task. A second schedule leaves the first running untracked (`ABANDONED`): it can still write the title and call `on_title`, and `cancel_title`/`shutdown` can no longer cancel it. Not reachable from today's call sites — one schedule per completed turn, and `_prepare` cancels first — so this is an API-level hazard, demonstrated by calling the public method twice. | `test_a_second_schedule_abandons_the_first_task_uncancelled` |
| H3 | `fragile` | `src/blackrose/chat.py::ChatController._name_conversation` (the `if changed` refresh) | The task assigns `selected_conversation` from a read taken after its UPDATE but before the user's manual rename is visible to it: the *read* can resolve, the user's rename can run and commit, and the assigning task can then install the older `AUTO` object. The database and the sidebar (refreshed by the user's own rename) are correct; only `selected_conversation.title` is stale until the next navigation or rename, which shows up in the `/rename` hint (`src/blackrose/gui/session.py::Session.rename`) and the delete notice (`src/blackrose/gui/session.py::Session.delete_conversation`). Reasoned only: forcing it needs a suspension point between the DB read resolving and its use, which no public seam provides. | not demonstrated (reasoned) |
| H4 | `fragile` | `src/blackrose/chat.py::ChatController.rename_conversation` | `rename_conversation` ignores the repository's `False`: it returns the requested title and reports success for a conversation that no longer exists (`src/blackrose/storage/repository.py::Repository.rename_conversation`). The selection-refresh branch can also assign `None` when the missing id was the selected chat. Unreachable from the GUI, which reads and re-checks `selected_conversation` first (`src/blackrose/gui/session.py::Session.rename`), and from the controller's own delete path (`src/blackrose/chat.py::ChatController._delete_current_conversation`), so only an API caller can see it. | `test_renaming_a_deleted_conversation_reports_success_without_a_row` |
| H5 | `fragile` | migration 1 in `src/blackrose/storage/migrations.py::MIGRATIONS`; `src/blackrose/storage/repository.py::_conversation` | `title_source` has a default but no CHECK, and `_conversation` converts with `TitleSource(...)`. One unknown value (hand edit, forward-version database, future writer) makes every `get/list/page` call raise `ValueError`, so the whole sidebar fails rather than one row being quarantined. Demonstrated by writing an unknown value and calling `list_conversations`. | `test_an_unknown_stored_title_source_fails_the_whole_listing` |
| H6 | `fragile` | `src/blackrose/chat.py::ChatController.cancel_title`, `src/blackrose/chat.py::ChatController._name_conversation`; `src/blackrose/titles.py::TITLE_TIMEOUT` | `TITLE_TIMEOUT` bounds `suggest_title`, not cancellation: `cancel_title` awaits the task with no bound, so a provider whose generator ignores `CancelledError` and hangs in `aclose()` would block the next turn start, `select_model` and window close forever. The real `RouterClient`/`OpenCodeGoClient` close promptly (`src/blackrose/titles.py::generate_title` uses `aclosing`), so this is a containment gap rather than an observed hang. Reasoned only. | not demonstrated (needs a provider that ignores cancellation) |
| H7 | `documentation-gap` | `src/blackrose/turns.py::TurnRunner._schedule_follow_ups` | “The first exchange” is the first `COMPLETE` assistant reply's request (the `first_answer`/`first_request` lookups). That request can be attachment-only, host-generated (interactive-UI action or animation repair) or markup-only, so naming may be derived from text the user never wrote. Combined with H1 it stranded the chat until the H1 fix; alone it produces a misleading title. Reasoned only beyond the empty-text case. | `test_an_unnameable_first_exchange_waits_for_a_later_nameable_turn` (the empty-text half) |
| H8 | `documentation-gap` | `src/blackrose/storage/repository.py::Repository.create_conversation` | Creation infers `PLACEHOLDER` from `title == DEFAULT_TITLE`. A repository caller that deliberately names a chat “New conversation” gets a placeholder that automatic naming may overwrite. `chat.new_conversation` never passes a title (`src/blackrose/chat.py::ChatController.new_conversation`) and `/rename` is `MANUAL` (`src/blackrose/chat.py::ChatController.rename_conversation`), so the GUI cannot reach it. | `test_creation_classifies_the_default_title_as_unnamed_and_a_custom_one_as_manual` |
| H9 | `none-found` | `src/blackrose/chat.py::ChatController._name_conversation`; `src/blackrose/titles.py::title_messages`, `src/blackrose/titles.py::generate_title` | The title request carries only a bounded excerpt of the same exchange that the same provider already received, plus attachment *labels*; it adds no document body, tool result, skill text, system prompt, image byte or tool schema, and the model is the one captured before any switch (`src/blackrose/chat.py::ChatController.schedule_title`). The cloud `session_id` is the conversation's existing stable identity (`src/blackrose/backend/providers.py::ChatProviders.request_options`), not new state. | invariants 11 and 8; the three payload tests |
| H10 | `documentation-gap` | `src/blackrose/titles.py::title_messages` | Attachment filenames are sent to the provider. A filename can carry sensitive text the user never typed into the message (`payroll-2026-Q3.xlsx`), and the omission is deliberate but undocumented. Reasoned only. | not demonstrated (a privacy policy question, not a code path) |
| H11 | `fragile` | `src/blackrose/diagram_repairs.py::DiagramRepairs.repair`; `src/blackrose/chat.py::ChatController.cancel_title` | A cancelled title schedules no replacement. A diagram repair immediately after the first reply therefore leaves the chat unnamed until the user sends another turn; the same is true for a model switch or a Stop just before naming. Recovery is automatic on the next completed turn (invariant 13), so this is a delay, not a loss. | `test_cancel_title_awaits_the_generation_and_is_idempotent` (state) and `test_titles.test_followup_preempts_title_and_retries_with_the_original_exchange` (recovery) |

## Formalization decisions

**H1 and its follow-through were applied** after the investigation (the
investigation itself did not edit `chat.py` or `turns.py`).
`ChatController._name_conversation` now returns before the provider call when
`derive_title` yields `DEFAULT_TITLE`, so an unnameable exchange stays a
placeholder, and `TurnRunner._schedule_follow_ups` prefers the current exchange
when the first one is unnameable. The characterization test became
`test_an_unnameable_first_exchange_waits_for_a_later_nameable_turn`, which now
asserts the placeholder, the zero provider calls, and the later automatic name.
H2, H3, H4 and H6 remain proposals below.

1. **H1 — do not name a chat from an unnameable exchange** (`src/blackrose/chat.py`).
   The import gains `DEFAULT_TITLE`; the fallback is computed before the provider call
   so no paid request is spent on a chat that cannot be named.

   ```diff
   -from .storage.repository import derive_title
   +from .storage.repository import DEFAULT_TITLE, derive_title
   ```

   ```diff
        async def _name_conversation(self, conversation, request, answer, model, on_title):
            try:
                current = await self._repo.get_conversation(conversation.id)
                if current is None or not current.is_unnamed:
                    return
   +            fallback = derive_title(request.content)
   +            if fallback == DEFAULT_TITLE:
   +                # Nothing in this exchange can name the chat. Stay a placeholder so
   +                # a later turn can name it, and do not spend a provider request.
   +                return
                title = None
                suggest = getattr(self.backend, 'suggest_title', None)
                if suggest is not None:
                    options = self.backend.request_options(conversation, model) if hasattr(self.backend, 'request_options') else {}
                    try:
                        async with asyncio.timeout(TITLE_TIMEOUT):
                            title = await suggest(model, title_messages(request, answer), **options)
                    except Exception:
                        # This optional request must never change a reply's outcome.
                        # Do not log provider diagnostics that may echo chat content.
                        log.debug('Chat naming unavailable; using a local fallback')
   -            title = title or derive_title(request.content)
   +            title = title or fallback
                changed = await self._repo.rename_conversation(
                    conversation.id, title, source=TitleSource.AUTO, only_if_unnamed=True)
   ```

2. **H1 follow-through — stop retrying an unnameable first exchange**
   (`src/blackrose/turns.py`). Without this, patch 1 leaves the chat a placeholder and
   every later turn re-examines the same unnameable exchange (no provider cost after
   patch 1, but no name either).

   ```diff
    from .storage.records import MessageState
   +from .storage.repository import DEFAULT_TITLE, derive_title
   ```

   ```diff
        def _schedule_follow_ups(self, history, request, answer, on_title, remember: bool) -> None:
            """Name the chat from its first exchange and sync memory; only after a stored reply."""
            c = self.chat
            first_answer = next((m for m in history if m.role == 'assistant'
                                 and m.state == MessageState.COMPLETE), answer)
            first_request = next((m for m in history if m.id == first_answer.reply_to), request)
   +        if derive_title(first_request.content) == DEFAULT_TITLE:
   +            # The first exchange has no nameable text (an image-only send, markup-only
   +            # text, or a host-generated repair request); prefer this reply.
   +            first_request, first_answer = request, answer
            c.schedule_title(c.conversation, first_request, first_answer, on_title)
   ```

3. **H2 — never abandon a live task** (`src/blackrose/chat.py`). `schedule_title` is
   synchronous, so the smallest fix cancels and detaches rather than awaiting; a
   cancelled task cannot write after its own cancellation point (invariant 13).

   ```diff
        def schedule_title(self, conversation, request, answer, on_title=None):
            if conversation.is_unnamed:
   +            previous = self._title_task
   +            if previous is not None and not previous.done():
   +                previous.cancel()
                self._title_task = asyncio.create_task(
                    self._name_conversation(conversation, request, answer, self.model_name, on_title))
   ```

4. **H4 — a rename that changed no row must not report success**
   (`src/blackrose/chat.py`). `session.rename` already catches `Exception` and shows a
   notice (`src/blackrose/gui/session.py::Session.rename`), so the user sees the failure instead of a false
   confirmation.

   ```diff
   -        await self._repo.rename_conversation(conversation_id, title)
   +        if not await self._repo.rename_conversation(conversation_id, title):
   +            raise ValueError('Conversation not found')
   ```

   A manual rename to the text the chat already has still reports one changed row
   (SQLite counts an UPDATE that rewrites identical values; verified against the test
   database), so only a missing id raises.

5. **H3 — never install a stale `AUTO` object** (`src/blackrose/chat.py`). Only a row
   that is still `AUTO` may replace the selected conversation; a manual rename that
   won the race leaves the user's object in place.

   ```diff
                if changed:
                    refreshed = await self._repo.get_conversation(conversation.id)
   -                if self.selected_conversation and self.selected_conversation.id == conversation.id and refreshed:
   +                if (refreshed and refreshed.title_source is TitleSource.AUTO
   +                        and self.selected_conversation
   +                        and self.selected_conversation.id == conversation.id):
                        self.selected_conversation = refreshed
   ```

6. **H5 — enforce the stored domain.** Prefer a migration that rebuilds
   `conversations` with `CHECK(title_source IN ('placeholder','auto','manual'))` plus a
   backfill of unknown values to `'manual'` (never auto-name a row whose provenance is
   unknown), because SQLite cannot add a CHECK in place. A per-row fallback in
   `_conversation` (`src/blackrose/storage/repository.py::_conversation`) would contain the damage but hides real
   corruption; the CHECK is the correct boundary. Not made here: it needs a migration
   number and a decision about rewriting user data.

7. **H7 — document that the retried exchange may be host-generated**, or extend patch 2
   to skip host-generated requests. That needs a durable marker on the user message
   (schema), so it is out of scope for this investigation.

## Verification

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_18_conversation_titles.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_titles.py
.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
.venv/bin/python scripts/check_unit_tests.py -p test_conversation_paging.py
.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
.venv/bin/python scripts/check_unit_tests.py -p test_chat_navigation.py
```

The H1 test asserts the fixed behaviour. The tests named for H2 and H4 pin the current,
hazardous behaviour; each must be rewritten to assert the desired behaviour when its
patch lands. H5's test flips from a
read-time `ValueError` to a refused write when patch 6 lands. The other tests assert
guards that must survive every patch.

The suites are offline and use scripted providers. H3 and H6 have no deterministic seam
without injecting a suspension point or a misbehaving provider, so they remain
reasoned-only claims.
