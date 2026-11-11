# SM-18: Conversation naming, `TitleSource` and the background title task

## Scope and ownership

- `src/blackrose/storage/records.py`: `TitleSource` (53–62), `Conversation.title_source`
  (71), `Conversation.is_unnamed` (74–77).
- `src/blackrose/storage/repository.py`: `DEFAULT_TITLE` (20), `derive_title` (89–111),
  `_conversation` (114–122), `create_conversation` (279–308), `rename_conversation`
  (398–423).
- `src/blackrose/storage/migrations.py`: the `conversations` table, migration 1 (2–7).
- `src/blackrose/chat.py`: `_title_task` (140), `shutdown` (246–247), `select_model`
  (298–321), `rename_conversation` (406–413), `cancel_title` (415–425),
  `schedule_title` (427–430), `_name_conversation` (472–498), conversation deletion
  (521–539).
- `src/blackrose/titles.py`: `TITLE_TIMEOUT` (8), `TITLE_INSTRUCTIONS` (9–16),
  `title_messages` (19–29), `clean_title` (32–46), `generate_title` (49–64).
- Call sites: `turns.py:392` (`_prepare` cancels), `turns.py:368–380` (schedules after a
  stored reply, including the Stop-after-commit path), `turns.py:835–843`
  (`_schedule_follow_ups`), `diagram_repairs.py:30` (cancels), `gui/session.py:377`
  (close), `:379–390` (sidebar refresh), `:640–649` (new chat), `:662–684` (delete),
  `:686–702` (`/rename`), `:1429–1437` (`on_title=self.refresh_conversations`).

Ownership, per [architecture](../architecture.md#modules-and-invariants) and
[ADR 0001](../adr/0001-desktop-ownership.md):

- **The persisted title belongs to the one owning desktop process.** Every read and
  write goes through `Repository` on the serialized `Database` worker
  (`storage/db.py:82–94`), so the guarded UPDATE at `repository.py:413–421` is atomic
  with respect to every other writer in the process; no migration, trigger or second
  process writes `conversations.title`.
- **The in-flight task belongs to the application asyncio loop.** `ChatController` holds
  the only `_title_task`. `titles.py` owns only prompt text, output cleanup and early
  stream closing; `chat.py` owns scheduling, preemption and guarded persistence, as the
  architecture map states (line 24). `gui/session.py` never touches the task; it only
  passes `refresh_conversations` as the `on_title` callback and cancels on close.
- **Naming is downstream of a durable reply.** Scheduling happens in
  `TurnRunner._schedule_follow_ups`, which `turns.py` calls only after a `COMPLETE`
  reply is stored (`turns.py:367–379`, and the `except` branch at `:368–372` for a Stop
  that landed after the final save). Nothing in this machine can change a reply's
  outcome; `_name_conversation` writes only the title column.

There are two orthogonal machines: the persisted `title_source` value and the
process-local title task.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `conversations.title` | insert `repository.py:297–300`; update `:414`; read `:116–121` | `TEXT NOT NULL` | Sidebar text | `create_conversation`, `chat.rename_conversation` (through the repo), `_name_conversation` |
| `conversations.title_source` | `migrations.py:6` (default `'placeholder'`); insert `repository.py:298–300`; update `:414–418`; read `:120` | `TEXT NOT NULL`, **no CHECK** | Provenance: `placeholder`, `auto`, `manual` | `create_conversation` (either the argument or the equality inference at `:291–294`), `rename_conversation` (the caller's `source`) |
| `Conversation.is_unnamed` | `records.py:74–77` | derived `bool` | `title_source is PLACEHOLDER`; the only gate on automatic naming | computed |
| `ChatController._title_task` | init `chat.py:140`; cleared `:417`; set `:429` | `asyncio.Task \| None` | The one tracked naming task | `schedule_title`, `cancel_title` (never by the task itself) |
| model captured for the title | `chat.py:430` (argument), used `:483` | `str` | The model of the reply that just settled | `schedule_title` at call time |
| `model_name` | `chat.py:307` | `str` | The selected chat model; the capture source | `select_model` only, which refuses while a reply is generating (`:299–300`) |
| `conversations.title` of a *deleted* row | `repository.py` delete path via `chat.py:528` | absent | Terminal; ids are `AUTOINCREMENT` (`migrations.py:4`) and never reused | `delete_conversation` |
| `TITLE_TIMEOUT` | `titles.py:8`, applied at `chat.py:482` | `float` seconds | Deadline for the provider title round trip (not for `generate_title` itself) | constant |

## States

Persisted, mutually exclusive (`title_source`, with the text as a second discriminator):

| State | Predicate |
| --- | --- |
| `PLACEHOLDER` | `title_source = 'placeholder'` and `title = DEFAULT_TITLE`; `is_unnamed` true |
| `AUTO` | `title_source = 'auto'` and `title != DEFAULT_TITLE`; named by the background task |
| `AUTO_PLACEHOLDER` | `title_source = 'auto'` and `title = DEFAULT_TITLE`; nothing named it, yet naming is over (H1) |
| `MANUAL` | `title_source = 'manual'`; the user chose the text, including a manual `DEFAULT_TITLE` |
| `DELETED` | no row; terminal for this machine |

`AUTO` and `MANUAL` are named states; the state machine has no transition back to
`PLACEHOLDER` (only the INSERT writes that value). The dataclass default is `AUTO`
(`records.py:71`), so an in-memory `Conversation` built without provenance is never
auto-namable — fail-safe by construction.

Process-local task states:

| State | Predicate |
| --- | --- |
| `IDLE` | `_title_task is None` |
| `RUNNING` | `_title_task` set, not done |
| `DONE_TRACKED` | `_title_task` set and done — the resting state after every completed naming, since only `cancel_title` clears the pointer (`chat.py:415–417`) |
| `DETACHED` | `_title_task is None` while the previously tracked task is still settling its cancellation; exists only inside `cancel_title` between `:417` and `:422` |
| `ABANDONED` | a second `schedule_title` replaced the pointer while the first task is still running and untracked (H2) |

## Events

| Event | Source | Effect on this machine |
| --- | --- | --- |
| A chat is created without a title | `chat.new_conversation` (`chat.py:393`), `ensure_conversation` (`:349`) | row inserted as `PLACEHOLDER` |
| A chat is created with a custom title | `Repository.create_conversation(title=...)` (`repository.py:291–294`) | row inserted as `MANUAL`; no controller caller passes a title |
| A reply settles `COMPLETE` | `turns.py:367`, `:379` | `_schedule_follow_ups` computes the first exchange and calls `schedule_title` |
| Stop lands after the final save | `turns.py:368–372` | the same scheduling from the `except` branch, then the error is re-raised |
| `schedule_title(conversation, request, answer, on_title)` | `turns.py:841` | creates the task when `conversation.is_unnamed` (`chat.py:428`) |
| A turn starts | `turns.py:392` | `await cancel_title()` before any new work |
| A model is selected | `chat.py:304` | `await cancel_title()` before the old model may be released |
| A diagram repair starts | `diagram_repairs.py:30` | `await cancel_title()` under the artifact lock |
| The user renames | `chat.rename_conversation` (`chat.py:406–413`) from `/rename` (`session.py:696`) | unconditional `source=MANUAL`; no task interaction |
| The conversation is deleted | `chat._delete_current_conversation` (`chat.py:528`) | row gone; the task is **not** cancelled and observes `None` at `:474–476` |
| The user navigates to another chat | `chat.open_conversation` (`chat.py:398–404`) | no title event; a pending task keeps naming its originating id and refreshes `selected_conversation` only if it is still that chat (`chat:493`) |
| The window closes | `session.py:377`, then `controller.shutdown` (`chat.py:247`) | `cancel_title()` twice; idempotent |
| The process restarts | — | no task exists; the stored `title_source` and text are read back (`repo:114–122`), and a `PLACEHOLDER` is retried on the next completed turn |
| The provider returns a title | `chat.py:483`, `titles.py:49–64` | `AUTO` through the guarded UPDATE |
| The provider fails, times out or returns nothing usable | `chat.py:484–487`, `titles.py:55–63` | local `derive_title` fallback (`chat.py:488`) |
| `TITLE_TIMEOUT` elapses | `chat.py:482` | `TimeoutError` → the same fallback |
| The provider stream is cancelled | `chat.py:419–422` | `CancelledError` propagates out of `_name_conversation`; nothing is written |

## Transition table

### Persisted title state

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| — | `create_conversation()` (no title) | `title == DEFAULT_TITLE` and no explicit source (`repo:291–294`) | INSERT `('placeholder', DEFAULT_TITLE)` (`:297–306`) | `PLACEHOLDER` |
| — | `create_conversation(custom)` | no explicit source (`repo:291–294`) | INSERT `('manual', custom)` | `MANUAL` |
| — | `create_conversation(DEFAULT_TITLE, title_source=MANUAL)` | explicit source (`repo:291`) | INSERT `('manual', DEFAULT_TITLE)` | `MANUAL` |
| `PLACEHOLDER` | provider title | row still a placeholder at `chat:474–476`; UPDATE matches at `repo:416–419` | title and `'auto'` written (`repo:414`) | `AUTO` |
| `PLACEHOLDER` | provider failure/timeout/empty | same | `derive_title(request.content)` written with `'auto'` (`chat:488–490`) | `AUTO` |
| `PLACEHOLDER` | provider failure, unnameable request (H1) | same; `derive_title` returns `DEFAULT_TITLE` (`repo:95–96`, `106–107`) | `DEFAULT_TITLE` written with `'auto'` — no text change | `AUTO_PLACEHOLDER` |
| `PLACEHOLDER` | `/rename` | non-empty after strip and 120-char clip (`chat:407–409`) | unconditional UPDATE (`repo:414`, source default `MANUAL` at `:403`) | `MANUAL` |
| `PLACEHOLDER` | stale automatic rename after a manual one | `AND title_source = 'placeholder'` fails (`repo:416–418`) | `rowcount == 0`; no write, no `on_title` (`chat:491–496`) | `PLACEHOLDER` (unchanged) |
| `PLACEHOLDER` | title task on a missing row | `current is None` (`chat:475`) | return; no provider call | `PLACEHOLDER` |
| `AUTO` | `/rename` | non-empty (`chat:407–409`) | unconditional UPDATE | `MANUAL` |
| `AUTO` | a second automatic name | guard fails (`repo:417–418`) | no write | `AUTO` (self) |
| `AUTO_PLACEHOLDER` | `/rename` | non-empty | unconditional UPDATE | `MANUAL` |
| `AUTO_PLACEHOLDER` | a later turn | `is_unnamed` false at `chat:428` | no task scheduled | `AUTO_PLACEHOLDER` (self) |
| `MANUAL` | `/rename` | non-empty | unconditional UPDATE; SQLite counts the row even for identical text | `MANUAL` (self) |
| `MANUAL` | automatic name | guard fails (`repo:417–418`) | no write | `MANUAL` (self) |
| any | `/rename` with blank text | `chat:407–409` | `ValueError`; no write | unchanged (self) |
| any | conversation delete | `chat:524–528` | row deleted; grant/proposal maps cleaned (`:529–533`) | `DELETED` |
| `DELETED` | late automatic name | `current is None` (`chat:475`); ids are never reused (`migrations.py:4`) | return; no INSERT exists on this path | `DELETED` (self) |

### Title task lifecycle

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | `schedule_title` | `conversation.is_unnamed` (`chat:428`) | `create_task(_name_conversation(..., self.model_name, ...))` (`:429–430`) | `RUNNING` |
| `IDLE` | `schedule_title` | not `is_unnamed` (`chat:428`) | nothing; pinned by `test_a_named_conversation_is_never_scheduled` | `IDLE` |
| `RUNNING` | row became named or vanished | re-read at `chat:474–476` | return before any provider request | `DONE_TRACKED` |
| `RUNNING` | provider title accepted | `clean_title` / `generate_title` (`titles:49–64`) | guarded UPDATE (`chat:489–490`) | `DONE_TRACKED` (+ `AUTO`) |
| `RUNNING` | provider failure, timeout, tool call, multi-line, over-long | `chat:484–487`, `titles:55–63` | fallback title, guarded UPDATE | `DONE_TRACKED` |
| `RUNNING` | guarded UPDATE refused | `chat:491` | no refresh, no `on_title` | `DONE_TRACKED` |
| `RUNNING` | UPDATE succeeded | `chat:491–496` | refresh `selected_conversation` when it is the same chat; `await on_title()` | `DONE_TRACKED` |
| `RUNNING` | any unexpected error | `chat:497–498` | logged without content; task ends | `DONE_TRACKED` |
| `RUNNING` | `cancel_title` | `chat:419–420` | pointer cleared (`:417`), `task.cancel()` | `DETACHED` |
| `DETACHED` | task settles | `chat:421–425` | `await task`; `CancelledError` suppressed unless the *caller* is being cancelled (`:424`) | `IDLE` |
| `DONE_TRACKED` | `cancel_title` | `chat:418–422` | pointer cleared; awaiting a finished task returns | `IDLE` |
| `RUNNING` | second `schedule_title` | none (`chat:427–430`) | pointer replaced; first task neither cancelled nor tracked | `ABANDONED` + `RUNNING` (new) |
| `ABANDONED` | it finishes | none | still writes through the same guarded UPDATE and may call `on_title` | untracked done |
| `DONE_TRACKED`/`IDLE` | window close | `session:377`, `chat:247` | `cancel_title` twice | `IDLE` |

### Recovery after cancellation

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `DETACHED` | next completed turn | `is_unnamed` still true (`chat:428`) | a new task names from the *first* stored exchange (`turns:838–841`) | `RUNNING` |
| `DETACHED` | no further turn | — | the chat stays `PLACEHOLDER` with `DEFAULT_TITLE` | `PLACEHOLDER` (unchanged) |
| `DETACHED` | diagram repair finished, no reschedule | `diagram_repairs.py:30` has no counterpart | the chat stays unnamed until the next completed turn | `PLACEHOLDER` (unchanged) |
| `ABANDONED` | window close | `chat:417` sees only the latest task | the abandoned task can still run into shutdown | untracked done |

## Invariants

1. **`title_source` moves away from `placeholder` at most once and never returns.**
   The INSERT (`repository.py:296–300`) is the only writer of `'placeholder'`; the
   UPDATE writes a caller-supplied source (`:414`), and the only callers pass `AUTO`
   (guarded) or `MANUAL`. Pinned by
   `test_automatic_naming_replaces_the_placeholder_exactly_once`
   and `test_a_manual_title_is_terminal_for_automatic_naming`.
2. **Automatic naming may replace a placeholder and nothing else.** The guard is the
   `AND title_source = ?` clause inside the UPDATE (`repository.py:416–418`), not a
   caller check, so a stale `Conversation` cannot defeat it; the task also re-reads the
   row before spending a provider request (`chat.py:474–476`). Pinned by
   `test_a_stale_placeholder_object_cannot_name_a_renamed_conversation` and
   `test_an_inflight_title_cannot_replace_a_manual_title`.
3. **The title text is never a state input after creation.** `is_unnamed` is exactly
   `title_source is PLACEHOLDER` (`records.py:74–77`); only the INSERT infers the
   source from text equality (`repository.py:291–294`). A manual title equal to
   `DEFAULT_TITLE` is `MANUAL`. Pinned by
   `test_a_manual_title_equal_to_the_placeholder_text_is_still_named`.
4. **A rename never creates a row.** Pinned by
   `test_renaming_a_missing_conversation_changes_nothing_and_creates_no_row`.
5. **A late title cannot resurrect or rename a deleted chat.** The task re-reads the
   row (`chat.py:474–476`) and the UPDATE matches no row; ids are `AUTOINCREMENT` and
   never reused (`migrations.py:4`). Pinned by
   `test_a_late_title_cannot_resurrect_a_deleted_conversation` and
   `test_a_deleted_conversation_id_is_never_reused`.
6. **Every path that starts new work cancels the tracked task first.** Turn start
   (`turns.py:392`), model switch (`chat.py:304`), diagram repair
   (`diagram_repairs.py:30`), window close (`session.py:377`) and shutdown
   (`chat.py:247`). Pinned by `test_cancel_title_awaits_the_generation_and_is_idempotent`
   and `test_close_cancels_the_title_before_the_session_is_torn_down`.
7. **`cancel_title()` clears the pointer before awaiting and returns only after the
   task has settled.** A cancelled provider generator has run its `finally` when the
   call returns; a caller that is not itself cancelled does not receive the task's
   `CancelledError` (`chat.py:415–425`). Pinned by the same two tests.
8. **A title is named by the model captured at scheduling.** `chat.py:430` passes
   `self.model_name`; `_name_conversation` uses that argument (`:483`), never the
   current selection. The capture is race-free: the successful path schedules right
   after `is_generating = False` with no intervening `await` (`turns.py:376–379`), and
   the Stop-after-commit path (`:368–372`) schedules while `is_generating` is still
   true, when `select_model` refuses (`chat.py:299–300`). Pinned by
   `test_naming_uses_the_model_captured_when_the_title_was_scheduled` and
   `test_titles.test_model_change_preempts_title_and_naming_uses_new_selected_model`.
9. **Naming never changes a reply's outcome or the transcript.** Scheduling follows
   the stored turn (`turns.py:367–379`); every provider error is swallowed
   (`chat.py:481–487`, `:497–498`); only the title column is written. Pinned by
   `test_titles.test_reply_finishes_while_title_is_pending_and_sidebar_updates_later`
   and `test_a_nameable_request_is_named_from_the_fallback_when_generation_fails`.
10. **`on_title` runs only when a row changed.** `chat.py:491–496`. Pinned by
    `test_an_inflight_title_cannot_replace_a_manual_title`.
11. **The title request is bounded, tool-free and content-limited.** 1600 characters of
    request, 1000 of reply, 4 filenames of 180 characters (`titles.py:24–28`); no
    document bodies, tool results, skills, system prompt or image bytes, and no tool
    schemas (`titles.py:52`). Pinned by
    `test_the_title_payload_is_bounded_and_excludes_document_and_prompt_context` and
    `test_titles.test_naming_input_is_small_and_excludes_document_body_and_chat_instructions`.
12. **A refused or unusable title never changes the stored text.** The guarded UPDATE
    either writes the intended title or nothing; there is no way to write an empty
    string (`chat:408–409`, `491`). Pinned by the manual-title and late-title tests.
13. **A cancelled title leaves `PLACEHOLDER`, so a later turn retries.** Cancellation
    happens before the UPDATE (`chat.py:419–422`), and the next turn re-derives the
    first exchange (`turns.py:838–841`). Pinned by
    `test_titles.test_followup_preempts_title_and_retries_with_the_original_exchange`
    and by the `PLACEHOLDER` assertion in
    `test_cancel_title_awaits_the_generation_and_is_idempotent`.

## Hazards and open questions

| # | Severity | Location | Hazard | Test |
| --- | --- | --- | --- | --- |
| H1 | `defect` | `chat.py:488–490`; `repository.py:89–96`, `:106–107` | An exchange with no nameable text writes `DEFAULT_TITLE` with `source='auto'` (`AUTO_PLACEHOLDER`). Reachable from the GUI: an image-only send (`session.py:933–934` allows empty text with attachments) or markup-only text such as `---` (`derive_title` strips to `''`). `is_unnamed` is then false, so the sidebar keeps “New conversation” unless the user renames it, and no later turn retries. Automatic naming ends with nothing named, contradicting the `TitleSource` docstring (`records.py:54–58`). | `test_an_unnameable_first_request_marks_the_chat_named_and_stops_retrying` |
| H2 | `fragile` | `chat.py:427–430` | `schedule_title` overwrites `_title_task` without cancelling the previous task. A second schedule leaves the first running untracked (`ABANDONED`): it can still write the title and call `on_title`, and `cancel_title`/`shutdown` can no longer cancel it. Not reachable from today's call sites — one schedule per completed turn, and `_prepare` cancels first — so this is an API-level hazard, demonstrated by calling the public method twice. | `test_a_second_schedule_abandons_the_first_task_uncancelled` |
| H3 | `fragile` | `chat.py:489–494` | The task assigns `selected_conversation` from a read taken after its UPDATE but before the user's manual rename is visible to it: the *read* can resolve, the user's rename can run and commit, and the assigning task can then install the older `AUTO` object. The database and the sidebar (refreshed by the user's own rename) are correct; only `selected_conversation.title` is stale until the next navigation or rename, which shows up in the `/rename` hint (`session.py:692`) and the delete notice (`:668`). Reasoned only: forcing it needs a suspension point between the DB read resolving and its use, which no public seam provides. | not demonstrated (reasoned) |
| H4 | `fragile` | `chat.py:406–413` | `rename_conversation` ignores the repository's `False`: it returns the requested title and reports success for a conversation that no longer exists (`repo:413–421`). The selection-refresh branch can also assign `None` when the missing id was the selected chat. Unreachable from the GUI, which reads and re-checks `selected_conversation` first (`session.py:686–696`), and from the controller's own delete path (`chat:524–539`), so only an API caller can see it. | `test_renaming_a_deleted_conversation_reports_success_without_a_row` |
| H5 | `fragile` | `migrations.py:4–6`; `repository.py:120` | `title_source` has a default but no CHECK, and `_conversation` converts with `TitleSource(...)`. One unknown value (hand edit, forward-version database, future writer) makes every `get/list/page` call raise `ValueError`, so the whole sidebar fails rather than one row being quarantined. Demonstrated by writing an unknown value and calling `list_conversations`. | `test_an_unknown_stored_title_source_fails_the_whole_listing` |
| H6 | `fragile` | `chat.py:415–425`, `:482`; `titles.py:8` | `TITLE_TIMEOUT` bounds `suggest_title`, not cancellation: `cancel_title` awaits the task with no bound, so a provider whose generator ignores `CancelledError` and hangs in `aclose()` would block the next turn start, `select_model` and window close forever. The real `RouterClient`/`OpenCodeGoClient` close promptly (`titles.py:52` uses `aclosing`), so this is a containment gap rather than an observed hang. Reasoned only. | not demonstrated (needs a provider that ignores cancellation) |
| H7 | `documentation-gap` | `turns.py:835–841` | “The first exchange” is the first `COMPLETE` assistant reply's request (`:838–840`). That request can be attachment-only, host-generated (interactive-UI action or animation repair) or markup-only, so naming may be derived from text the user never wrote. Combined with H1 it strands the chat; alone it produces a misleading title. Reasoned only beyond the empty-text case. | `test_an_unnameable_first_request_marks_the_chat_named_and_stops_retrying` (the empty-text half) |
| H8 | `documentation-gap` | `repository.py:291–294` | Creation infers `PLACEHOLDER` from `title == DEFAULT_TITLE`. A repository caller that deliberately names a chat “New conversation” gets a placeholder that automatic naming may overwrite. `chat.new_conversation` never passes a title (`chat.py:393`) and `/rename` is `MANUAL` (`chat:410`), so the GUI cannot reach it. | `test_creation_classifies_the_default_title_as_unnamed_and_a_custom_one_as_manual` |
| H9 | `none-found` | `chat.py:472–498`; `titles.py:19–29`, `:49–64` | The title request carries only a bounded excerpt of the same exchange that the same provider already received, plus attachment *labels*; it adds no document body, tool result, skill text, system prompt, image byte or tool schema, and the model is the one captured before any switch (`chat:430`). The cloud `session_id` is the conversation's existing stable identity (`providers.py:59–65`), not new state. | invariants 11 and 8; the three payload tests |
| H10 | `documentation-gap` | `titles.py:26` | Attachment filenames are sent to the provider. A filename can carry sensitive text the user never typed into the message (`payroll-2026-Q3.xlsx`), and the omission is deliberate but undocumented. Reasoned only. | not demonstrated (a privacy policy question, not a code path) |
| H11 | `fragile` | `diagram_repairs.py:30`; `chat.py:415–425` | A cancelled title schedules no replacement. A diagram repair immediately after the first reply therefore leaves the chat unnamed until the user sends another turn; the same is true for a model switch or a Stop just before naming. Recovery is automatic on the next completed turn (invariant 13), so this is a delay, not a loss. | `test_cancel_title_awaits_the_generation_and_is_idempotent` (state) and `test_titles.test_followup_preempts_title_and_retries_with_the_original_exchange` (recovery) |

## Formalization decisions

**H1 and its follow-through were applied by the orchestrator** after the
investigation (the investigation itself did not edit `chat.py` or `turns.py`).
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
   notice (`session.py:695–700`), so the user sees the failure instead of a false
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
   `_conversation` (`repository.py:114–122`) would contain the damage but hides real
   corruption; the CHECK is the correct boundary. Not made here: it needs a migration
   number and a decision about rewriting user data.

7. **H7 — document that the retried exchange may be host-generated**, or extend patch 2
   to skip host-generated requests. That needs a durable marker on the user message
   (schema), so it is out of scope for this bead.

## Verification

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_18_conversation_titles.py -v
# Ran 21 tests in 0.259s
# OK

.venv/bin/python scripts/check_unit_tests.py -p test_titles.py
# Ran 15 tests in 0.306s
# OK

.venv/bin/python scripts/check_unit_tests.py -p test_storage.py
# Ran 17 tests in 0.208s
# OK

.venv/bin/python scripts/check_unit_tests.py -p test_turns.py
# Ran 28 tests in 0.539s
# OK

.venv/bin/python scripts/check_unit_tests.py -p test_conversation_paging.py
# Ran 11 tests in 0.187s
# OK

.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
# Ran 6 tests in 2.774s
# OK

.venv/bin/python scripts/check_unit_tests.py -p test_chat_navigation.py
# Ran 7 tests in 0.333s
# OK

.venv/bin/python -m ruff check tests/test_sm_18_conversation_titles.py
# All checks passed!
```

The tests named for H1, H2 and H4 pin the current, hazardous behaviour and pass today;
each must be rewritten to assert the desired behaviour when its patch lands. H5's test
flips from a read-time `ValueError` to a refused write when patch 6 lands. The other 17
tests assert guards that must survive every patch.

The full `scripts/check_health.py` gate was run and exited 1 for two reasons outside this
bead:

```
timeout: .../scripts/check_unit_tests.py
failed: .../python -m ruff check src tests scripts
```

- The unit-suite step exceeded the script's 180 s per-command timeout in this shared
  worktree, where other agents' in-flight tests are present. This bead's file runs in
  0.26 s and every focused suite listed above passes.
- The only Ruff findings are `ASYNC110` (`tests/test_sm_20_gui_session.py:197`) and
  `F841` (`:270`), both in another agent's untracked file. `ruff check` on this bead's
  test file passes.

The gate's other steps passed: `mypy` reported "Success: no issues found in 19 source
files", `check_recovery.py` passed its drill, and `npm test --prefix ui-runtime` and
`uv lock --check --offline` passed.

Not run: the `--gui` probe (needs GTK/WebKit and a display) and any live model request.
H3 and H6 have no deterministic seam without injecting a suspension point or a
misbehaving provider, so they remain reasoned-only claims.
