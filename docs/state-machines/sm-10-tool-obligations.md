# SM-10: Tool effect budgets, artifact obligations and illustration draft revisions

## Scope and ownership

- `src/blackrose/tool_execution.py`: `ToolInvocation` (60–65), `EffectBudget`
  (68–82), `ToolExecutor` latches (100–122), `definitions` (161–172),
  `require_successful_artifacts` (178–193), `execute` (212–251),
  `_record_attempt` (253–262), `_illustration_draft` (303–307),
  `_animation_plan` (309–321), `_animation_create_planned` (323–362),
  `_animation_create` (364–381), `_file_tool` (383–389), `_image_gen` (391–425),
  `_failure_result`/`FAILURE_HOOKS` (48–56, 448–454), `_fail_motion_plan`
  (455–469), `_fail_document` (480–481), `_fail_animation_create` (483–517),
  `_fail_image_gen` (519–536).
- `src/blackrose/illustrations.py`: `IllustrationDrafts` (150–216),
  `MAX_DRAFTS`/`MAX_REPAIRS`/`MAX_DRAFT_BYTES` (15–17), `validate_draft`
  (101–147), `delete_conversation_drafts` (258–265).
- `src/blackrose/image_generation.py`: `GeneratedImage` (21–34),
  `OpenRouterImages.generate` (45–87).
- Coupled, not owned here: `turns.py` creates one executor per reply (507–510),
  runs calls sequentially in one round (691–718) and calls the acceptance
  predicate once (687); `chat.py` performs the image effect and persists it
  before honoring flags (1378–1436) and returns `document_create` proposals
  (1036–1053); `local_images.py` attaches recovery state to a downloaded image
  (726–737).

Ownership follows the [architecture map](../architecture.md#modules-and-invariants)
(`tool_execution.py` owns effects, result serialization and artefact failure
outcomes; `session.py` owns no tool policy):

- **One executor per reply.** `TurnRunner._start_reply` constructs it
  (`turns.py:507–510`); every latch is in-memory and starts at its initial value
  (`tool_execution.py:100–122`). Nothing in this machine is persisted, so no
  state crosses replies. A repair reply starts with `animation_attempted = True`
  (`:107`) because `TurnRunner` created the executor with a non-null
  `animation_repair_id` (`turns.py:384–385`, `:507–510`).
- **One application asyncio loop, one tool call at a time.** `_run_tools`
  awaits `executor.execute` in a `for` loop (`turns.py:707–711`); there is no
  concurrent mutation of these fields. `image_round` may hold one local-GPU
  handoff across the round's image calls (`chat.py:333–339`), which changes GPU
  residency, not this state.
- **`IllustrationDrafts` is reply-scoped but on disk.** Its root is
  `illustration-drafts/<conversation>/<message>` (`tool_execution.py:101`,
  `tool_host.py:39–41`), so a re-created executor for the same message (restart,
  resumed reply) re-derives `created` from the files
  (`illustrations.py:152–154`). Conversation deletion removes the directory
  (`illustrations.py:258–265`).
- **`GeneratedImage` is a frozen value.** It carries downloaded bytes
  (`image_generation.py:21–34`); it does not own the job, the budget or the
  persistence decision. `ChatController._generate_image` persists first and then
  reports `cancelled` (`chat.py:1408–1426`).
- **The page never owns any of this.** Tool execution, effects and artefact
  policy are Python (`architecture.md:182–193`); the four-image cap,
  no-job-on-validation-failure and reply-fails-on-execution-failure policy is
  stated at `architecture.md:218–222`, and "completed images are saved before
  cancellation or memory-recovery warnings end the reply" at 224–227. Draft
  revision behaviour is described at 249–282.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `ToolExecutor.effects` | `tool_execution.py:100` | `EffectBudget` | This reply's image budget and failure latch | `__init__` only |
| `EffectBudget.image_jobs` | `:71` | `int`, 0–4 | Started image jobs (not attempts) | `start_image` `:82`, reached from `_image_gen.image_start` `:402–404` ← `chat.py:1403–1404` |
| `EffectBudget.image_error` | `:72` | `str \| None` | Diagnostic of the first started-job failure | `_fail_image_gen` `:531` only |
| `ToolInvocation.image_started` | `:64` | `bool` | This call reached `on_start` | `_image_gen` `:404` |
| `ToolInvocation.generated` | `:65` | `GeneratedImage \| None` | Image handed to `_run_tools` for vision inspection | `_image_gen` `:409` |
| `draft_attempted` | `:102` | `bool` | An `illustration_draft` call was dispatched | `_record_attempt` `:260`; `_illustration_draft` `:304` |
| `draft_valid` | `:103` | `bool` | Validity of the **last saved** revision | `_illustration_draft` `:306` only |
| `illustration_requested` | `:104` | `bool` | An `image_gen` was dispatched while `prompt.xiaohei` | `_record_attempt` `:262` |
| `illustration_created` | `:105` | `bool` | A Xiaohei image was persisted | `_image_gen` `:411` |
| `animation_attempted` | `:107` | `bool` | An animation tool was dispatched, or this is a repair reply | `__init__` `:107`; `_record_attempt` `:256`; `_animation_create_planned` `:328` |
| `animation_created` | `:108` | `bool` | An animation artefact was saved | `_animation_create_planned` `:341`; `_animation_create` `:369` |
| `document_attempted` | `:109` | `bool` | `document_create` was dispatched | `_record_attempt` `:258` |
| `document_created` | `:110` | `bool` | `host.file_tool` returned without raising | `_file_tool` `:386` |
| `document_error` | `:111` | `str \| None` | Last `document_create` diagnostic | `_fail_document` `:481` |
| `animation_error` | `:112` | `str \| None` | Last animation diagnostic (last write wins) | `_fail_motion_plan` `:469`; `_fail_animation_create` `:485`, `:503` |
| `motion_plan` | `:113` | `dict \| None` | Accepted canonical plan | `_animation_plan` `:317`; cleared `:458` |
| `motion_plan_started` | `:114` | `float \| None` | Plan timing for one log line | `:314` |
| `motion_plan_disabled` | `:115` | `bool` | Plan-first fallback latch | `:339`, `:457` |
| `rejected_motion_plan` | `:116` | `dict \| None` | Copy of the rejected plan (debugging) | `:315` (clear), `:462` |
| `motion_plan_error` | `:117` | `str \| None` | Exact validator diagnostic for that plan | `:316` (clear), `:464` |
| `prompt.xiaohei` | `prompts.py:109` | `bool` | Whether the Xiaohei contract is active for this request | `Prompt` construction; `_controller_tool` `:444` |
| `IllustrationDrafts.created` | `illustrations.py:154` | `int`, 0–4 | Draft slots used, derived from `draft-[1-4].json` | `save` `:193–194`; recomputed on construction |
| `draft-<1-4>.json` | `:192` (write), `:161–168` (read) | JSON `{draft_id, revision, plan, valid, issues}` | Current pointer per draft slot | `save` only |
| `draft-<1-4>-r<1-3>.json` | `:191` | same shape | Immutable snapshot of one revision | `save` only, once per revision |
| `saved['revision']` | `:188` | `int`, 1–3 | Optimistic-concurrency token | `save` `:183` |
| `saved['plan']` | `:188` | JSON object | Merged draft content (`DEFAULTS` + submitted fields) | `save` `:184` |
| `saved['valid']`/`saved['issues']` | `:188–189` | `bool`, `list` | Structural validity only | `validate_draft` `:187` |
| `GeneratedImage.cancelled` | `image_generation.py:33` | `bool` | Artefact saved; the reply must end as cancelled | `chat.py:1426`; `local_images.py:737` |
| `GeneratedImage.cleanup_warning` | `:32` | `str \| None` | Artefact saved; the GPU needs recovery | `local_images.py:730` |

## States

Four independent machines, all derived from the variables above.

**S1 — obligation instance** (one per artefact: draft, illustration, animation,
document). Mutually exclusive per instance:

| State | Predicate |
| --- | --- |
| `UNATTEMPTED` | `attempted == False` (and therefore `created == False`) |
| `OPEN` | `attempted == True and created == False` (draft: `attempted and not valid`) |
| `MET` | `created == True` (draft: `valid == True`) |

Only the draft instance can leave `MET`: `draft_valid` is last-write-wins
(`:306`), so a later invalid repair moves `MET → OPEN`. No other instance can go
backwards (see I3).

**S2 — reply acceptance** (derived; mutually exclusive by precedence
`IMAGE_BLOCKED > PENDING > SATISFIED > EMPTY`):

| State | Predicate |
| --- | --- |
| `IMAGE_BLOCKED` | `effects.image_error is not None` |
| `PENDING` | no `image_error`; some instance is `OPEN` |
| `SATISFIED` | no `image_error`; no instance is `OPEN`; some instance is `MET` |
| `EMPTY` | every instance is `UNATTEMPTED` |

`require_successful_artifacts` (`:178–193`) raises exactly in
`IMAGE_BLOCKED`/`PENDING`; `PENDING` raises in the fixed order animation →
document → illustration → draft, so only the first open instance is reported.

**S3 — image effect budget** (per reply):

| State | Predicate |
| --- | --- |
| `AVAILABLE` | `image_error is None and image_jobs < 4` |
| `EXHAUSTED` | `image_error is None and image_jobs == 4` |
| `BLOCKED` | `image_error is not None` |

`images_available` (`:74–76`) is false in both `EXHAUSTED` and `BLOCKED`;
`image_error` is what distinguishes "cap reached" from "execution failed".

**S4 — plan-first** (per reply):

| State | Predicate |
| --- | --- |
| `UNPLANNED` | `motion_plan is None and not motion_plan_disabled` |
| `PLANNED` | `motion_plan is not None and not motion_plan_disabled` |
| `DISABLED` | `motion_plan_disabled` (always with `motion_plan is None`) |

**S5 — draft slot** (per draft id, plus the slot counter):

| State | Predicate |
| --- | --- |
| `ABSENT` | no `draft-<n>.json` |
| `INVALID@r` | current file exists, `revision == r`, `valid == false` |
| `VALID@r` | current file exists, `revision == r`, `valid == true` |

`r ∈ 1..3`; `r == 3` is terminal for that draft (two repairs are the maximum,
`MAX_REPAIRS = 2`, `:16`). `created ∈ 0..4` is the number of non-`ABSENT` slots
(`MAX_DRAFTS = 4`, `:15`).

**S6 — image continuation** (per downloaded `GeneratedImage`):

| State | Predicate | Executor outcome |
| --- | --- | --- |
| `CLEAN` | `not cancelled and cleanup_warning is None` | normal tool result (`:425`) |
| `CANCELLED` | `cancelled and cleanup_warning is None` | `CancelledError` (`:421–422`) |
| `RECOVERY` | `not cancelled and cleanup_warning` | `RecoveryRequired` (`:423–424`) |
| `CANCELLED_RECOVERY` | both | `CancelledError` wins (`:421` before `:423`) |

Both flags can be set: `local_images.py:726–737` attaches the cleanup warning
and then, when the failure is a `CancelledError`, returns the same value with
`cancelled=True`.

## Events

1. A tool-free model round reaching `_require_final_answer`
   (`turns.py:678–689`): the only event that reads S2.
2. `TurnRunner._run_tools` dispatching each call of one round sequentially
   (`turns.py:691–718`); `execute` first latches the attempt (`:214`), then
   emits `start`, parses/normalizes/validates arguments (`:219–227`), then runs
   the handler (`:228`), then emits `complete`/`error` and appends the tool
   result (`:230–250`).
3. Any exception inside a call: `_failure_result` shapes the error and
   `FAILURE_HOOKS` updates turn state (`:48–56`, `:448–454`).
4. `illustration_draft` save result (valid or invalid) → `draft_valid`
   (`:306`).
5. `image_gen` `on_start` → `EffectBudget.start_image` (`:402–404`);
   `host.generate_image` returning or raising (`:405–410`).
6. A skill read inside the round that turns `prompt.xiaohei` on
   (`_controller_tool` `:427–446`, set at `:444`); it changes the schemas
   returned by `definitions()` (`:166–172`) for later calls in the same round.
7. Plan-first calls: accepted plan (`:309–321`), planned creation
   (`:323–362`), direct creation (`:364–381`).
8. Image download settlement: `on_start` before the provider call, persistence
   before flag honouring (`chat.py:1403–1426`), recovery attachment
   (`local_images.py:726–737`).
9. Cancellation of the reply task at any `await`; `RecoveryRequired` raised by
   GPU state (`chat.py:1378` path, `local_images.py`) or by `_fail_image_gen`
   (`:535–536`).
10. Executor creation for a repair reply (`turns.py:507–510`,
    `tool_execution.py:107`).
11. `document_create` cancellation (`:231–234`) and `RecoveryRequired`
    (`:235–237`).

## Transition table

### T1 — obligation latches (dispatch and completion)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| draft `UNATTEMPTED`/`OPEN`/`MET` | `illustration_draft` dispatched | name match `:259–260` | `draft_attempted = True` | unchanged until the save returns |
| draft `OPEN`/`MET` | `save` returned | — | `draft_valid = result['valid']` `:306` | `MET` if valid, else `OPEN` |
| draft any | `save` raised (stale, absent, limit, size) | `illustrations.py:166–167`, `:175–186` | no write; `draft_valid` keeps its previous value | unchanged |
| illustration `UNATTEMPTED`/`OPEN` | `image_gen` dispatched | `prompt.xiaohei` `:261–262` | `illustration_requested = True` | `OPEN` |
| illustration `OPEN` | image persisted | `prompt.xiaohei` `:410–411` | `illustration_created = True` | `MET` |
| illustration `UNATTEMPTED` | `image_gen` dispatched | `not prompt.xiaohei` | no latch (generic image) | `UNATTEMPTED` |
| animation `UNATTEMPTED`/`OPEN` | animation tool dispatched | name ∈ {animation_create, animation_plan, animation_create_planned} `:255–256` | `animation_attempted = True` | `OPEN` |
| animation `OPEN` | `create_animation` returned | planned `:341`; direct `:369` | `animation_created = True` | `MET` |
| document `UNATTEMPTED`/`OPEN` | `document_create` dispatched | `:257–258` | `document_attempted = True` | `OPEN` |
| document `OPEN` | `host.file_tool` returned | `:385–386` | `document_created = True` | `MET` |
| any `OPEN` | handler raised | failure hook `:448–454` | error text stored; no `created` write | `OPEN` |

### T2 — reply acceptance

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `EMPTY`/`SATISFIED` | tool-free round | `require_successful_artifacts` returns `turns.py:687` | reply may persist complete | `ACCEPTED` |
| `PENDING` | tool-free round | animation open `:179` | `RuntimeError`, reply settles `error` `turns.py:364–365` | `FAILED` |
| `PENDING` | tool-free round | document open `:182` | `RuntimeError` | `FAILED` |
| `PENDING` | tool-free round | illustration open `:184` | `RuntimeError` | `FAILED` |
| `PENDING` | tool-free round | draft open `:186–191` | `RuntimeError` (message names the saved artefact when one exists) | `FAILED` |
| `IMAGE_BLOCKED` | tool-free round | `:192–193` | `RuntimeError('Image generation failed: …')` | `FAILED` |
| any | reply cancelled/failed before the tool-free round | — | predicate never runs (`turns.py:360–365`) | `INTERRUPTED`/`FAILED` |
| any | model answers without a visible answer | `turns.py:688–689` | `RuntimeError` after the predicate | `FAILED` |

### T3 — image effect budget

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `AVAILABLE` | `on_start` called | `jobs < 4`, no error `:74–82` | `image_jobs += 1` | `AVAILABLE` or `EXHAUSTED` at 4 |
| `AVAILABLE`/`EXHAUSTED`/`BLOCKED` | call reaches `execute` | `definitions()` hides `image_gen` when `not images_available` `:170–171` | `validate_call` fails "unavailable" `tools.py:205–207`; no job | unchanged |
| `AVAILABLE` | handler raised before `on_start` | `not inv.image_started` `:525–529` | no `image_error`; notice "no image job has started" | unchanged |
| `AVAILABLE` | handler raised after `on_start` | `:530–536` | `image_error = result['error']`; notice blocks retry; may raise `RecoveryRequired` | `BLOCKED` |
| `BLOCKED` | later `image_gen` dispatched | schema hidden, or `start_image` refuses `:78–82` | no second job; error result only | `BLOCKED` |

### T4 — plan-first

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNPLANNED` | `animation_plan` | `not disabled` `:312–313`; `validate_plan` ok `:317` | canonical plan stored; timing set `:314` | `PLANNED` |
| `PLANNED` | `animation_plan` | same | accepted plan replaced | `PLANNED` |
| `UNPLANNED`/`PLANNED` | `animation_plan` | `validate_plan` raised | disabled `:457`, plan cleared `:458`, rejected copy + diagnostic `:462–464` | `DISABLED` |
| `PLANNED` | `animation_create_planned` | plan present, not disabled `:326–327`; assembly+creation ok | `animation_created = True` `:341` | `PLANNED` (+ animation `MET`) |
| `PLANNED` | `animation_create_planned` | `ValueError`/`ValidationError` (assembly or creation) | disabled `:339`, then hook clears plan `:458` | `DISABLED` |
| `PLANNED`/`DISABLED` | `animation_create_planned` | plan missing or disabled `:326–327` | `ValueError` → hook latches disabled again | `DISABLED` |
| `DISABLED` | `animation_plan` | `:312–313` | `ValueError` → hook overwrites `animation_error` `:469` | `DISABLED` |
| `DISABLED` | `animation_create` | normal handler | `animation_created = True` `:369` | `DISABLED` (+ animation `MET`) |
| any | new reply | — | fresh executor `:113–115` | `UNPLANNED` |

### T5 — illustration drafts

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | `save(changes)` | `created < 4` `:175–176` | `DEFAULTS` + changes; snapshot+pointer written `:191–192`; `created += 1` `:193–194` | `INVALID@1` or `VALID@1` |
| `INVALID@r`/`VALID@r` | `save(changes, id, r)` | `read` requires exact `int` `r` `:166–167`; `r <= 2` `:181` | merged plan; snapshot `@r+1`; pointer replaced | `INVALID@r+1` or `VALID@r+1` |
| any | `save(..., id, wrong)` | `read` raised | no write at all | unchanged |
| `INVALID@3`/`VALID@3` | `save(..., id, 3)` | `saved['revision'] > 2` `:181–182` | `ValueError('Draft repair limit reached…')` | unchanged |
| any | `save` with `draft_id=None, revision≠None` | `:173–174` | `ValueError('revision requires draft_id.')` | unchanged |
| `ABSENT` | `save(changes)` | `created == 4` `:175–176` | `ValueError('Draft limit reached…')` | unchanged |
| any | `save` plan too large / bad id | `:185–186`, `:156–158` | `ValueError` before any write | unchanged |
| `VALID@r` | `validated(id, r)` | issues re-validated `:211–216` | returns the plan; only then may `image_gen` run | `VALID@r` |
| `INVALID@r`/stale | `validated(id, r)` | issues or `read` raise | `ValueError`; no image job | unchanged |

### T6 — image continuation

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| job running | provider returned | `_generate_image` returns `GeneratedImage` | `on_images(saved)` `:419`; `inv.generated` set `:409` | `CLEAN` |
| `CLEAN` | reply cancelled during persistence | `chat.py:1418–1426` | image saved first; `replace(…, cancelled=True)` | `CANCELLED` |
| `CLEAN` | GPU recovery after download | `local_images.py:726–730` | image saved first; `cleanup_warning` | `RECOVERY` |
| `CLEAN`/`RECOVERY` | cancellation after download | `local_images.py:732–737` | both flags kept | `CANCELLED_RECOVERY` |
| `CANCELLED`/`CANCELLED_RECOVERY` | executor reads the value | `:421–422` | `illustration_created` already `True` `:411`; raises `CancelledError` | reply cancelled |
| `RECOVERY` | executor reads the value | `:423–424` | `illustration_created` already `True`; raises `RecoveryRequired` | reply failed |

## Invariants

1. **Creation implies attempt.** `illustration_created ⇒ illustration_requested`
   (`:261–262` and `:410–411` read the same `prompt.xiaohei` in the same
   synchronous prefix of `execute`), `animation_created ⇒ animation_attempted`
   (`:255–256` vs `:341`/`:369`), `document_created ⇒ document_attempted`
   (`:258` vs `:386`), `draft_valid ⇒ draft_attempted` (`:260`/`:304` vs
   `:306`). Enforced at those sites; the acceptance predicate itself is pinned
   by `test_acceptance_predicate_covers_every_reachable_combination`.
2. **Acceptance is exactly "no open obligation and no image failure".**
   `require_successful_artifacts` raises iff one of the five disjuncts in
   `:179–192` holds. Enforced there; pinned by the same test.
3. **All latches are monotone except `draft_valid` and `motion_plan`.**
   `animation_created`, `document_created`, `illustration_created`,
   `*_attempted`, `illustration_requested` and `image_error` are only ever
   assigned `True`/a value after construction (no `= False`/`= None` after
   `:100–122`). `draft_valid` can go `True → False` (`:306`); `motion_plan` can
   go `dict → None` (`:458`). The monotone latches are pinned by
   `test_later_failed_attempts_do_not_revoke_created_latches`; the
   `draft_valid` reversal by
   `test_invalid_repair_after_a_saved_illustration_keeps_the_artifact_claim`,
   the plan replacement by
   `test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan`.
4. **A later failing retry cannot revoke a saved artefact.** The three `created`
   bits are written only on their success paths above; failure hooks write only
   `*_error` (`:480–481`, `:483–517`, `:519–536`) plus the plan latches.
   `draft_valid` is the exception described in I3: it tracks the last saved
   revision and is not a creation latch. Pinned by
   `test_later_failed_attempts_do_not_revoke_created_latches`.
5. **The predicate runs once, only on the normal path.** `turns.py:687` is its
   only call site; cancelled or failed replies settle through
   `_settle_interrupted` (`turns.py:360–365`, `:762–773`) and never evaluate it.
6. **A started job is counted before the provider runs.** `on_start` is invoked
   at `chat.py:1403–1404`, before `provider.generate` at `:1405`; the counter
   only advances through `start_image` (`:78–82`). Pinned by
   `test_four_started_jobs_hide_the_tool_without_an_error_latch`.
7. **`image_error` is set only for a started job.** `_fail_image_gen` sets it
   solely in the `else` branch after `inv.image_started` (`:530–531`); the
   `OutboundDenied` and pre-start branches never touch it (`:520–529`). Pinned
   by `test_pre_start_image_failure_does_not_fail_the_turn` and
   `test_started_image_failure_fails_the_turn_after_an_earlier_success`.
8. **A spent or failed budget is enforced twice.** `definitions()` omits
   `image_gen` when `not images_available` (`:170–171`) and `start_image`
   refuses (`:78–82`). The second guard is not reachable through `execute`
   because calls are sequential and each recomputes `definitions()`; it is
   defensive. Pinned by
   `test_four_started_jobs_hide_the_tool_without_an_error_latch`.
9. **Every draft mutation is revision-guarded.** `save` is the only mutator; with
   a `draft_id` it must pass `read` (`illustrations.py:180–181`), which requires
   `type(revision) is int` and exact equality (`:166–167`). `validated` uses the
   same gate (`:211–216`). The only other writer is conversation deletion, which
   removes the whole reply directory atomically (`:258–265`) and therefore needs
   no per-draft token. Pinned by
   `test_every_mutating_path_requires_the_current_revision`.
10. **A rejected draft write writes nothing.** All raising checks
    (`:156–158`, `:166–167`, `:173–176`, `:181–182`, `:185–186`) precede both
    `write_json` calls (`:191–192`); `write_json` itself replaces atomically
    (`config.py:75–85`). Pinned by the same test and
    `test_foreign_draft_ids_cannot_be_read_or_written`.
11. **Revision snapshots are immutable and the slot counter ignores them.**
    `draft-<n>-r<m>.json` is written once, at revision `m`; the glob that derives
    `created` matches only `draft-[1-4].json` (`:154`). Pinned by
    `test_two_repairs_are_allowed_and_snapshots_stay_immutable` and
    `test_new_drafts_count_current_files_not_revision_snapshots`.
12. **Generation re-validates the exact revision.** `_image_gen` calls
    `drafts.validated(draft_id, revision)` before compiling (`:393–394`), so the
    `draft_valid` latch is not the only gate; a stale, missing, foreign or
    invalid revision cannot start a job even when `draft_valid` is `True`.
    Pinned indirectly by `test_invalid_draft_without_an_image_reports_no_image`
    (no job, request latched) and existing `test_illustrations.py`
    coverage.
13. **Plan-first cannot run without an accepted plan, and `DISABLED` implies no
    plan.** `_animation_create_planned` refuses when
    `motion_plan is None or motion_plan_disabled` (`:326–327`); the only writer
    of `motion_plan_disabled = True` also clears `motion_plan`
    (`:457–458`, `:338–339` + hook `:458`). Pinned by
    `test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan` and
    `test_planned_creation_failure_falls_back_without_keeping_the_plan`.
14. **A downloaded image is persisted before `cancelled`/`cleanup_warning` are
    honoured.** `_generate_image` appends the attachment before returning
    (`chat.py:1408–1424`); the executor sets `illustration_created` and calls
    `on_images` before it reads the flags (`:411–424`). The `GeneratedImage`
    docstring states the same contract (`image_generation.py:23–27`). Pinned by
    `test_cancelled_download_keeps_the_artifact_and_latches_no_error`,
    `test_cleanup_warning_after_a_saved_image_requires_recovery` and existing
    `test_image_recovery.py`.

## Hazards and open questions

| # | Severity | Where | Finding | Demonstrated |
| --- | --- | --- | --- | --- |
| H1 | defect (fixed) | `tool_execution.py:186–191` | A later invalid draft repair after a saved illustration made `require_successful_artifacts` fail with "no image was generated" although the image existed and was attached. The failure itself is deliberate fail-closed behaviour; the claim was false. Now reports that the saved illustration predates the rejected revision. | `test_invalid_repair_after_a_saved_illustration_keeps_the_artifact_claim` (failed before the change; `test_invalid_draft_without_an_image_reports_no_image` keeps the original wording for the genuinely image-less case) |
| H2 | documentation-gap | `tool_execution.py:386` with `chat.py:1036–1053` | `document_created` means "`file_tool` returned without raising", not "a file exists". A new file outside the output folder returns `approval_required` with "No file was created" and still satisfies the obligation. The acceptance is arguably correct (the model's part succeeded and a review card is pending), but the name overstates the effect. | `test_document_proposal_counts_as_created_without_a_file` |
| H3 | fragile | `tool_execution.py:455–469`, `:312–313` | `animation_error` is last-write-wins. After a rejected plan, a second `animation_plan` (refused because plan-first is latched off) overwrites the root diagnostic, so the reply error says "Plan-first is unavailable for this reply" instead of the validator's `unknown target 'missing'`. The root cause survives only in `motion_plan_error`/`rejected_motion_plan`, which are not shown to the user. A later failed plan also discards an earlier accepted plan (`:457–458`) — fail-safe for artefacts, but the accepted work is lost. | `test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan` |
| H4 | fragile | `tool_execution.py:231–234` | Inside `except asyncio.CancelledError`, a cancelled `document_create` awaits `host.attachments_for`. If that read raises, the new exception replaces the `CancelledError`, so the reply settles as `error` ("database is locked") instead of `cancelled`: the user's Stop is lost and the transcript claims a failure. Proposed fix below; not applied. | `test_cancelled_document_creation_becomes_a_storage_error` (with `test_cancelled_document_creation_still_propagates_cancellation` as the passing counterpart) |
| H5 | fragile | `tool_execution.py:261–262`, `:393`, `:410`, `:444` | The Xiaohei gate is keyed on the mutable `prompt.xiaohei` at dispatch time, and reading the skill mid-round turns it on (`:444`). A round whose calls are `image_gen(prompt)` then `read_skill` therefore runs the first call under the generic schema: no draft obligation is created and the reply can end `SATISFIED` with an ungated image, while the reverse order is refused. Model-controlled call order decides whether the contract applies. Not a trust boundary (paid images still pass `OutboundPolicy`), but undocumented. | Not committed; the opposite order is covered by `test_illustrations.py::test_skill_read_mid_round_activates_draft_gate`. Confirming needs a round with `image_gen` before `read_skill`. |
| H6 | none-found | `tool_execution.py:411–424` | `illustration_created` is set before the `cancelled`/`cleanup_warning` checks, so a cancelled or recovery-failing reply counts the image as created. Harmless: both paths re-raise before the predicate can run. It does mean the latch is "image persisted", not "reply continued". | `test_cancelled_download_keeps_the_artifact_and_latches_no_error`, `test_cleanup_warning_after_a_saved_image_requires_recovery` |
| H7 | fragile | `tool_execution.py:369` vs `:377` | `animation_created` is set before `host.finish_animation_repair`. If that auxiliary write raises (SQLite error), the reply fails but the durable claim stays `status='repairing'`; `AnimationRecovery.claim` then reports `claimed=False` for the same attachment forever (`animation_recovery.py:58–78`), so the failed animation can never be repaired again by the app. Requires a failing recovery-store write. | No; would need `animation_recovery.finish` to raise. Cross-check with the animation-recovery bead. |
| H8 | none-found | `tool_execution.py:78–82` vs `:170–171` | The `start_image` refusal is unreachable through `execute` (the schema is already hidden and calls are sequential); it is a defensive second guard, not dead policy. | `test_four_started_jobs_hide_the_tool_without_an_error_latch` |
| H9 | none-found | `tool_execution.py:107`, `:457–458` | `motion_plan_disabled` is a safe fallback latch: once set it is never cleared in the reply, it clears the accepted plan, refuses further planning, and direct `animation_create` still satisfies the animation obligation. It cannot hide a lost artefact. | `test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan` |

### Answers to the bead's focus questions

- **Reachable combinations of the eight obligation bits.** The reachable set is
  the product of four independent instances (`UNATTEMPTED`/`OPEN`/`MET`),
  constrained by I1 and by `prompt.xiaohei` for the illustration pair, plus the
  independent `image_error` bit. The acceptance predicate is a pure function of
  that tuple: reject iff `(A_a ∧ ¬A_c) ∨ (K_a ∧ ¬K_c) ∨ (I_r ∧ ¬I_c) ∨
  (D_a ∧ ¬D_v) ∨ image_error`, evaluated in that order (`:179–193`). The
  thirteen representative reachable tuples and their verdicts are pinned by
  `test_acceptance_predicate_covers_every_reachable_combination`;
  `IMAGE_BLOCKED` wins over `PENDING` only when no earlier disjunct fires,
  which is why the ordering is observable in the message.
- **Any wrong accepted combination?** "Created without attempt" is unreachable
  (I1); "attempted but never created" is always rejected. The two semantic soft
  spots are H2 (`document_created` covers a review proposal with no file) and H6
  (`illustration_created` covers a saved-but-not-continued image). The reverse
  asymmetry — an accepted artefact plus a later invalid repair is rejected even
  though the image exists — is deliberate fail-closed behaviour; H1 fixed only
  the false wording.
- **Does a later round clear or contradict an earlier obligation?** Only two
  registers are non-monotone: `draft_valid` (`MET → OPEN` on a later invalid
  repair) and `motion_plan` (`PLANNED → DISABLED` on a later failed plan).
  Neither can produce an accepted turn with a missing artefact; both can turn a
  turn that already produced its artefact into an error reply (H1, H3). No
  `created` latch is ever cleared (I3, I4).
- **Is `motion_plan_disabled` a safe fallback latch?** Yes (H9): monotone per
  reply, clears the plan, refuses planning, and the direct path remains
  available. Its only cost is diagnostic (H3).
- **Can `image_error` be set after a successful job and wrongly fail the turn?**
  It is set only by a *started* job that then fails (`:530–531`), which
  includes a second job after a successful first — the documented policy
  (`architecture.md:218–222`, "an execution failure … marks the reply failed"),
  so not wrong. Pre-start rejections (`:525–529`), cancellation and
  cleanup-warning continuations (`:421–424`) never set it, and nothing clears it
  within a reply. The one unverified corner is a failure inside the `on_images`
  callback after a saved image (`:419` before `:531`); not demonstrated.
- **Is the `IllustrationDrafts` optimistic revision check enforced on every
  mutating path?** Yes (I9, I10): `save` is the only mutator, a repair save must
  pass `read`'s exact-int revision equality, `validated` uses the same gate, and
  every raising check precedes both writes. A `draft_id=None` create needs no
  revision but is bounded by `created`. Reopening the store re-derives `created`
  from the current pointers only, so it cannot reuse or overwrite a slot.

## Formalization decisions

- **No structural change to the obligation tracker.** The tracker is already an
  explicit set of per-reply latches with a single predicate, and each instance
  has a different creation path, error hook and wording. Converting it to a
  state enum or a shared helper would add indirection without a new guard, and
  the four instances are already documented as a product machine above. The
  bead's "formalize into an explicit state structure" goal is met by this
  document plus the matrix test; a code-level enum is not proposed.
- **One source change: truthful failure text (H1).**
  `require_successful_artifacts` now says the saved illustration predates the
  rejected draft revision when `illustration_created` is true, instead of
  claiming no image was generated. No state or policy changed. Regression test:
  `test_invalid_repair_after_a_saved_illustration_keeps_the_artifact_claim`;
  the image-less wording is retained and pinned by
  `test_invalid_draft_without_an_image_reports_no_image`.
- **Deferred: H4.** The fix is to wrap the attachment refresh in
  `try/except Exception` and log it before `raise`, preserving the
  `CancelledError`. It is small but changes Stop settlement on a storage-failure
  path that this bead cannot exercise end to end; it should be reviewed with
  the turn-settlement behaviour rather than folded into an obligation audit.
  The characterization test above records the current behaviour and will need
  inverting with the fix.
- **Deferred: H3, H5, H7.** H3 needs a decision about whether the refusal should
  suppress the diagnostic update, or whether the reply error should prefer
  `motion_plan_error`; that is user-facing wording policy. H5 needs a decision
  about whether the Xiaohei contract should be decided per round (snapshot)
  rather than per call. H7 is an ordering/durability trade-off in the
  recovery-store protocol owned by the animation-recovery area.
- **No change to the draft store.** The revision gate, snapshot immutability and
  slot counter are sound; the only oddity is that `MAX_REPAIRS` is compared to
  the *current* revision rather than counting repairs (`illustrations.py:181`),
  which is equivalent because revisions only increase by one from 1.

## Verification

Run from the repository root:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_10_tool_obligations.py -v
```

Result: `Ran 18 tests` — `OK` (18/18). Two classes cover the draft
machine (`DraftRevisionStateTests`) and the obligation/budget/plan/image
machines (`ObligationStateTests`).

Pre-existing focused tests, each run with the same runner:

| Command (`-p …`) | Result |
| --- | --- |
| `test_tool_execution.py` | `Ran 3 tests` — `OK` |
| `test_illustrations.py` | `Ran 19 tests` — `OK` |
| `test_image_generation.py` | `Ran 14 tests` — `OK` |
| `test_image_recovery.py` | `Ran 7 tests` — `OK` |
| `test_local_images.py` | `Ran 43 tests` — `OK` |
| `test_gui_images.py` | `Ran 9 tests` — `OK` |
| `test_tool_handlers.py` | `Ran 10 tests` — `OK` |
| `test_motion_plan.py` | `Ran 12 tests` — `FAILED (errors=2)`: `test_source_compiles_with_real_motion_canvas_toolchain` and `test_blood_circulation_sequence_compiles` need the Motion Canvas runtime (`compile_source` raises "Motion Canvas needs setup: run blackrose setup-animations"); `animation-runtime/node_modules` is absent in this checkout. Unrelated to this change; the plan/fallback tests in that module pass. |

Static checks: `.venv/bin/ruff check src/blackrose/tool_execution.py
tests/test_sm_10_tool_obligations.py` — `All checks passed!`.

Not run: `scripts/check_health.py` (full offline gate), the `--gui` probe and any
live image/GPU probe. The changed line is an error-message branch inside the
acceptance predicate; the GUI probe was not needed to verify it.
