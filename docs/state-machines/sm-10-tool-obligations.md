# SM-10: Tool effect budgets, artifact obligations and illustration draft revisions

## Scope and ownership

- `src/blackrose/tool_execution.py`: `ToolInvocation`, `EffectBudget`, the
  `ToolExecutor` latches set in
  `src/blackrose/tool_execution.py::ToolExecutor.__init__`, and the
  `ToolExecutor` methods `definitions`, `require_successful_artifacts`,
  `execute`, `_record_attempt`, `_illustration_draft`, `_animation_plan`,
  `_animation_create_planned`, `_animation_create`, `_file_tool`, `_image_gen`,
  `_failure_result` with `src/blackrose/tool_execution.py::FAILURE_HOOKS`,
  `_fail_motion_plan`, `_fail_document`, `_fail_animation_create` and
  `_fail_image_gen`.
- `src/blackrose/illustrations.py`: `IllustrationDrafts`,
  `MAX_DRAFTS`/`MAX_REPAIRS`/`MAX_DRAFT_BYTES`,
  `src/blackrose/illustrations.py::validate_draft` and
  `src/blackrose/illustrations.py::delete_conversation_drafts`.
- `src/blackrose/image_generation.py`:
  `src/blackrose/image_generation.py::GeneratedImage` and
  `src/blackrose/image_generation.py::OpenRouterImages.generate`.
- Coupled, not owned here:
  `src/blackrose/turns.py::TurnRunner._start_reply` creates one executor per
  reply, `src/blackrose/turns.py::TurnRunner._run_tools` runs calls
  sequentially in one round, and
  `src/blackrose/turns.py::TurnRunner._require_final_answer` calls the
  acceptance predicate once.
  `src/blackrose/chat.py::ChatController._generate_image` performs the image
  effect and persists it before honoring flags, and
  `src/blackrose/chat.py::ChatController.file_tool` returns `document_create`
  proposals. `src/blackrose/local_images.py::LocalImages._outcome` attaches
  recovery state to a downloaded image.

Ownership follows the [architecture map](../architecture.md#modules-and-invariants)
(`tool_execution.py` owns effects, result serialization and artefact failure
outcomes; `session.py` owns no tool policy):

- **One executor per reply.** `TurnRunner._start_reply` constructs it; every
  latch is in-memory and starts at its initial value in
  `ToolExecutor.__init__`. Nothing in this machine is persisted, so no state
  crosses replies. A repair reply starts with `animation_attempted = True`
  because `TurnRunner._run` passes a non-null `animation_repair_id` to
  `_start_reply`, which hands it to the executor.
- **One application asyncio loop, one tool call at a time.** `_run_tools`
  awaits `executor.execute` in a `for` loop; there is no concurrent mutation of
  these fields. `src/blackrose/chat.py::ChatController.image_round` may hold one
  local-GPU handoff across the round's image calls, which changes GPU
  residency, not this state.
- **`IllustrationDrafts` is reply-scoped but on disk.** Its root is
  `illustration-drafts/<conversation>/<message>` (built in
  `ToolExecutor.__init__` from
  `src/blackrose/tool_host.py::ToolHost.illustration_drafts_root`), so a
  re-created executor for the same message (restart, resumed reply) re-derives
  `created` from the files in
  `src/blackrose/illustrations.py::IllustrationDrafts.__init__`. Conversation
  deletion removes the directory (`delete_conversation_drafts`).
- **`GeneratedImage` is a frozen value.** It carries downloaded bytes; it does
  not own the job, the budget or the persistence decision.
  `ChatController._generate_image` persists first and then reports
  `cancelled`.
- **The page never owns any of this.** Tool execution, effects and artefact
  policy are Python
  ([admission and effects](../architecture.md#admission-and-effects)). The same
  section states the four-image cap, no-job-on-validation-failure and
  reply-fails-on-execution-failure policy, and that "completed images are saved
  before cancellation or memory-recovery warnings end the reply". Draft
  revision behaviour is described under
  [illustration drafts](../architecture.md#illustration-drafts).

## State variables

Unqualified methods are on `ToolExecutor` in `src/blackrose/tool_execution.py`;
draft rows are in `src/blackrose/illustrations.py`.

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `ToolExecutor.effects` | `ToolExecutor.__init__` | `EffectBudget` | This reply's image budget and failure latch | `__init__` only |
| `EffectBudget.image_jobs` | `EffectBudget` | `int`, 0–4 | Started image jobs (not attempts) | `EffectBudget.start_image`, reached from the `image_start` callback in `_image_gen` ← `on_start` in `ChatController._generate_image` |
| `EffectBudget.image_error` | `EffectBudget` | `str \| None` | Diagnostic of the first started-job failure | `_fail_image_gen` only |
| `ToolInvocation.image_started` | `ToolInvocation` | `bool` | This call reached `on_start` | `_image_gen` (`image_start`) |
| `ToolInvocation.generated` | `ToolInvocation` | `GeneratedImage \| None` | Image handed to `_run_tools` for vision inspection | `_image_gen` |
| `draft_attempted` | `ToolExecutor.__init__` | `bool` | An `illustration_draft` call was dispatched | `_record_attempt`; `_illustration_draft` |
| `draft_valid` | `ToolExecutor.__init__` | `bool` | Validity of the **last saved** revision | `_illustration_draft` only |
| `illustration_requested` | `ToolExecutor.__init__` | `bool` | An `image_gen` was dispatched while `prompt.xiaohei` | `_record_attempt` |
| `illustration_created` | `ToolExecutor.__init__` | `bool` | A Xiaohei image was persisted | `_image_gen` |
| `animation_attempted` | `ToolExecutor.__init__` | `bool` | An animation tool was dispatched, or this is a repair reply | `__init__`; `_record_attempt`; `_animation_create_planned` |
| `animation_created` | `ToolExecutor.__init__` | `bool` | An animation artefact was saved | `_animation_create_planned`; `_animation_create` |
| `document_attempted` | `ToolExecutor.__init__` | `bool` | `document_create` was dispatched | `_record_attempt` |
| `document_created` | `ToolExecutor.__init__` | `bool` | `host.file_tool` returned without raising | `_file_tool` |
| `document_error` | `ToolExecutor.__init__` | `str \| None` | Last `document_create` diagnostic | `_fail_document` |
| `animation_error` | `ToolExecutor.__init__` | `str \| None` | Last animation diagnostic (last write wins) | `_fail_motion_plan`; `_fail_animation_create` (the error, then the joined validation detail) |
| `motion_plan` | `ToolExecutor.__init__` | `dict \| None` | Accepted canonical plan | `_animation_plan`; cleared by `_fail_motion_plan` |
| `motion_plan_started` | `ToolExecutor.__init__` | `float \| None` | Plan timing for one log line | `_animation_plan` |
| `motion_plan_disabled` | `ToolExecutor.__init__` | `bool` | Plan-first fallback latch | `_animation_create_planned`, `_fail_motion_plan` |
| `rejected_motion_plan` | `ToolExecutor.__init__` | `dict \| None` | Copy of the rejected plan (debugging) | `_animation_plan` (clear), `_fail_motion_plan` |
| `motion_plan_error` | `ToolExecutor.__init__` | `str \| None` | Exact validator diagnostic for that plan | `_animation_plan` (clear), `_fail_motion_plan` |
| `prompt.xiaohei` | `src/blackrose/prompts.py::Prompt.xiaohei` | `bool` | Whether the Xiaohei contract is active for this request | `Prompt` construction; `_controller_tool` |
| `IllustrationDrafts.created` | `IllustrationDrafts.__init__` | `int`, 0–4 | Draft slots used, derived from `draft-[1-4].json` | `save`; recomputed on construction |
| `draft-<1-4>.json` | `IllustrationDrafts.save` (write), `IllustrationDrafts.read` (read) | JSON `{draft_id, revision, plan, valid, issues}` | Current pointer per draft slot | `save` only |
| `draft-<1-4>-r<1-3>.json` | `IllustrationDrafts.save` | same shape | Immutable snapshot of one revision | `save` only, once per revision |
| `saved['revision']` | `IllustrationDrafts.save` | `int`, 1–3 | Optimistic-concurrency token | `save` |
| `saved['plan']` | `IllustrationDrafts.save` | JSON object | Merged draft content (`DEFAULTS` + submitted fields) | `save` |
| `saved['valid']`/`saved['issues']` | `IllustrationDrafts.save` | `bool`, `list` | Structural validity only | `validate_draft` |
| `GeneratedImage.cancelled` | `src/blackrose/image_generation.py::GeneratedImage.cancelled` | `bool` | Artefact saved; the reply must end as cancelled | `ChatController._generate_image`; `LocalImages._outcome` |
| `GeneratedImage.cleanup_warning` | `src/blackrose/image_generation.py::GeneratedImage.cleanup_warning` | `str \| None` | Artefact saved; the GPU needs recovery | `LocalImages._outcome` |

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
(`_illustration_draft`), so a later invalid repair moves `MET → OPEN`. No other
instance can go backwards (see I3).

**S2 — reply acceptance** (derived; mutually exclusive by precedence
`IMAGE_BLOCKED > PENDING > SATISFIED > EMPTY`):

| State | Predicate |
| --- | --- |
| `IMAGE_BLOCKED` | `effects.image_error is not None` |
| `PENDING` | no `image_error`; some instance is `OPEN` |
| `SATISFIED` | no `image_error`; no instance is `OPEN`; some instance is `MET` |
| `EMPTY` | every instance is `UNATTEMPTED` |

`require_successful_artifacts` raises exactly in
`IMAGE_BLOCKED`/`PENDING`; `PENDING` raises in the fixed order animation →
document → illustration → draft, so only the first open instance is reported.

**S3 — image effect budget** (per reply):

| State | Predicate |
| --- | --- |
| `AVAILABLE` | `image_error is None and image_jobs < 4` |
| `EXHAUSTED` | `image_error is None and image_jobs == 4` |
| `BLOCKED` | `image_error is not None` |

`src/blackrose/tool_execution.py::EffectBudget.images_available` is false in
both `EXHAUSTED` and `BLOCKED`; `image_error` is what distinguishes "cap
reached" from "execution failed".

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
`src/blackrose/illustrations.py::MAX_REPAIRS` is 2). `created ∈ 0..4` is the
number of non-`ABSENT` slots (`src/blackrose/illustrations.py::MAX_DRAFTS` is
4).

**S6 — image continuation** (per downloaded `GeneratedImage`):

| State | Predicate | Executor outcome |
| --- | --- | --- |
| `CLEAN` | `not cancelled and cleanup_warning is None` | normal tool result from `_image_gen` |
| `CANCELLED` | `cancelled and cleanup_warning is None` | `CancelledError` |
| `RECOVERY` | `not cancelled and cleanup_warning` | `RecoveryRequired` |
| `CANCELLED_RECOVERY` | both | `CancelledError` wins (`_image_gen` checks `cancelled` before `cleanup_warning`) |

Both flags can be set: `LocalImages._outcome` attaches the cleanup warning and
then, when the failure is a `CancelledError`, returns the same value with
`cancelled=True`.

## Events

1. A tool-free model round reaching `TurnRunner._require_final_answer`: the
   only event that reads S2.
2. `TurnRunner._run_tools` dispatching each call of one round sequentially;
   `execute` first latches the attempt (`_record_attempt`), then emits `start`,
   parses/normalizes/validates arguments, then runs the handler (`_run`), then
   emits `complete`/`error` and appends the tool result.
3. Any exception inside a call: `_failure_result` shapes the error and
   `FAILURE_HOOKS` updates turn state.
4. `illustration_draft` save result (valid or invalid) → `draft_valid`
   (`_illustration_draft`).
5. `image_gen` `on_start` → `EffectBudget.start_image` (the `image_start`
   callback in `_image_gen`); `host.generate_image` returning or raising.
6. A skill read inside the round that turns `prompt.xiaohei` on
   (`_controller_tool`, on a `read_skill` of the Xiaohei skill's `SKILL.md`); it
   changes the schemas returned by `definitions()` for later calls in the same
   round.
7. Plan-first calls: accepted plan (`_animation_plan`), planned creation
   (`_animation_create_planned`), direct creation (`_animation_create`).
8. Image download settlement: `on_start` before the provider call, persistence
   before flag honouring (`ChatController._generate_image`), recovery
   attachment (`LocalImages._outcome`).
9. Cancellation of the reply task at any `await`; `RecoveryRequired` raised by
   GPU state (the `ChatController._generate_image` path, `local_images.py`) or
   by `_fail_image_gen`.
10. Executor creation for a repair reply (`TurnRunner._start_reply`,
    `ToolExecutor.__init__`).
11. `document_create` cancellation and `RecoveryRequired` (the
    `except asyncio.CancelledError` and `except RecoveryRequired` branches of
    `execute`).

## Transition table

Unqualified methods are on `ToolExecutor` unless they belong to
`IllustrationDrafts` (`save`, `read`, `validated`).

### T1 — obligation latches (dispatch and completion)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| draft `UNATTEMPTED`/`OPEN`/`MET` | `illustration_draft` dispatched | name match in `_record_attempt` | `draft_attempted = True` | unchanged until the save returns |
| draft `OPEN`/`MET` | `save` returned | — | `draft_valid = result['valid']` in `_illustration_draft` | `MET` if valid, else `OPEN` |
| draft any | `save` raised (stale, absent, limit, size) | `IllustrationDrafts.read` revision check; `save` limit and size checks | no write; `draft_valid` keeps its previous value | unchanged |
| illustration `UNATTEMPTED`/`OPEN` | `image_gen` dispatched | `prompt.xiaohei` in `_record_attempt` | `illustration_requested = True` | `OPEN` |
| illustration `OPEN` | image persisted | `prompt.xiaohei` in `_image_gen` | `illustration_created = True` | `MET` |
| illustration `UNATTEMPTED` | `image_gen` dispatched | `not prompt.xiaohei` | no latch (generic image) | `UNATTEMPTED` |
| animation `UNATTEMPTED`/`OPEN` | animation tool dispatched | name ∈ `ANIMATION_ATTEMPT_TOOLS` = {animation_create, animation_plan, animation_create_planned} in `_record_attempt` | `animation_attempted = True` | `OPEN` |
| animation `OPEN` | `create_animation` returned | planned: `_animation_create_planned`; direct: `_animation_create` | `animation_created = True` | `MET` |
| document `UNATTEMPTED`/`OPEN` | `document_create` dispatched | `_record_attempt` | `document_attempted = True` | `OPEN` |
| document `OPEN` | `host.file_tool` returned | `_file_tool` | `document_created = True` | `MET` |
| any `OPEN` | handler raised | failure hook via `_failure_result` | error text stored; no `created` write | `OPEN` |

### T2 — reply acceptance

All guards are branches of `require_successful_artifacts`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `EMPTY`/`SATISFIED` | tool-free round | `require_successful_artifacts` returns in `TurnRunner._require_final_answer` | reply may persist complete | `ACCEPTED` |
| `PENDING` | tool-free round | animation open | `RuntimeError`, reply settles `error` through `TurnRunner._run` | `FAILED` |
| `PENDING` | tool-free round | document open | `RuntimeError` | `FAILED` |
| `PENDING` | tool-free round | illustration open | `RuntimeError` | `FAILED` |
| `PENDING` | tool-free round | draft open | `RuntimeError` (message names the saved artefact when one exists) | `FAILED` |
| `IMAGE_BLOCKED` | tool-free round | `effects.image_error` | `RuntimeError('Image generation failed: …')` | `FAILED` |
| any | reply cancelled/failed before the tool-free round | — | predicate never runs (`TurnRunner._run` settles through `_settle_interrupted`) | `INTERRUPTED`/`FAILED` |
| any | model answers without a visible answer | the content check after the predicate in `TurnRunner._require_final_answer` | `RuntimeError` after the predicate | `FAILED` |

### T3 — image effect budget

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `AVAILABLE` | `on_start` called | `jobs < 4`, no error (`EffectBudget.start_image`) | `image_jobs += 1` | `AVAILABLE` or `EXHAUSTED` at 4 |
| `AVAILABLE`/`EXHAUSTED`/`BLOCKED` | call reaches `execute` | `definitions()` hides `image_gen` when `not images_available`, and also when `prompt.xiaohei and prompt.plan_only` | `src/blackrose/tools.py::validate_call` fails "unavailable"; no job | unchanged |
| `AVAILABLE` | handler raised before `on_start` | `not inv.image_started` in `_fail_image_gen` | no `image_error`; notice "no image job has started" | unchanged |
| `AVAILABLE` | handler raised after `on_start` | `inv.image_started` in `_fail_image_gen` | `image_error = result['error']`; notice blocks retry; may raise `RecoveryRequired` | `BLOCKED` |
| `BLOCKED` | later `image_gen` dispatched | schema hidden, or `EffectBudget.start_image` refuses | no second job; error result only | `BLOCKED` |

### T4 — plan-first

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNPLANNED` | `animation_plan` | `not disabled` and `validate_plan` ok in `_animation_plan` | canonical plan stored; timing set | `PLANNED` |
| `PLANNED` | `animation_plan` | same | accepted plan replaced | `PLANNED` |
| `UNPLANNED`/`PLANNED` | `animation_plan` | `validate_plan` raised | `_fail_motion_plan`: disabled, plan cleared, rejected copy + diagnostic | `DISABLED` |
| `PLANNED` | `animation_create_planned` | plan present, not disabled; assembly+creation ok | `animation_created = True` | `PLANNED` (+ animation `MET`) |
| `PLANNED` | `animation_create_planned` | `ValueError`/`ValidationError` (assembly or creation) | disabled in `_animation_create_planned`, then the hook clears the plan | `DISABLED` |
| `PLANNED`/`DISABLED` | `animation_create_planned` | plan missing or disabled | `ValueError` → hook latches disabled again | `DISABLED` |
| `DISABLED` | `animation_plan` | `motion_plan_disabled` | `ValueError` → hook overwrites `animation_error` | `DISABLED` |
| `DISABLED` | `animation_create` | normal handler | `animation_created = True` in `_animation_create` | `DISABLED` (+ animation `MET`) |
| any | new reply | — | fresh executor (`ToolExecutor.__init__`) | `UNPLANNED` |

### T5 — illustration drafts

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | `save(changes)` | `created < MAX_DRAFTS` | `DEFAULTS` + changes; snapshot+pointer written; `created += 1` | `INVALID@1` or `VALID@1` |
| `INVALID@r`/`VALID@r` | `save(changes, id, r)` | `read` requires exact `int` `r`; `r <= MAX_REPAIRS` | merged plan; snapshot `@r+1`; pointer replaced | `INVALID@r+1` or `VALID@r+1` |
| any | `save(..., id, wrong)` | `read` raised | no write at all | unchanged |
| `INVALID@3`/`VALID@3` | `save(..., id, 3)` | `saved['revision'] > MAX_REPAIRS` | `ValueError('Draft repair limit reached…')` | unchanged |
| any | `save` with `draft_id=None, revision≠None` | first check in `save` | `ValueError('revision requires draft_id.')` | unchanged |
| `ABSENT` | `save(changes)` | `created == MAX_DRAFTS` | `ValueError('Draft limit reached…')` | unchanged |
| any | `save` plan too large / bad id | `MAX_DRAFT_BYTES` check in `save`; `IllustrationDrafts._path` | `ValueError` before any write | unchanged |
| `VALID@r` | `validated(id, r)` | issues re-validated in `IllustrationDrafts.validated` | returns the plan; only then may `image_gen` run | `VALID@r` |
| `INVALID@r`/stale | `validated(id, r)` | issues or `read` raise | `ValueError`; no image job | unchanged |

### T6 — image continuation

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| job running | provider returned | `_generate_image` returns `GeneratedImage` | `inv.generated` set; `on_images(saved)` in `_image_gen` | `CLEAN` |
| `CLEAN` | reply cancelled during persistence | the shielded save in `ChatController._generate_image` | image saved first; `replace(…, cancelled=True)` | `CANCELLED` |
| `CLEAN` | GPU recovery after download | `LocalImages._outcome` | image saved first; `cleanup_warning` | `RECOVERY` |
| `CLEAN`/`RECOVERY` | cancellation after download | `LocalImages._outcome` | both flags kept | `CANCELLED_RECOVERY` |
| `CANCELLED`/`CANCELLED_RECOVERY` | executor reads the value | `cancelled` check in `_image_gen` | `illustration_created` already `True`; raises `CancelledError` | reply cancelled |
| `RECOVERY` | executor reads the value | `cleanup_warning` check in `_image_gen` | `illustration_created` already `True`; raises `RecoveryRequired` | reply failed |

## Invariants

1. **Creation implies attempt.** `illustration_created ⇒ illustration_requested`
   (`_record_attempt` and `_image_gen` read the same `prompt.xiaohei` in the
   same synchronous prefix of `execute`), `animation_created ⇒
   animation_attempted` (`_record_attempt` vs `_animation_create_planned`/
   `_animation_create`), `document_created ⇒ document_attempted`
   (`_record_attempt` vs `_file_tool`), `draft_valid ⇒ draft_attempted`
   (`_record_attempt`/`_illustration_draft` set the attempt before the save
   result is assigned). Enforced at those sites; the acceptance predicate
   itself is pinned by
   `test_acceptance_predicate_covers_every_reachable_combination`.
2. **Acceptance is exactly "no open obligation and no image failure".**
   `require_successful_artifacts` raises iff one of its five disjuncts holds.
   Enforced there; pinned by the same test.
3. **All latches are monotone except `draft_valid` and `motion_plan`.**
   `animation_created`, `document_created`, `illustration_created`,
   `*_attempted`, `illustration_requested` and `image_error` are only ever
   assigned `True`/a value after construction (no `= False`/`= None` outside
   `ToolExecutor.__init__`). `draft_valid` can go `True → False`
   (`_illustration_draft`); `motion_plan` can go `dict → None`
   (`_fail_motion_plan`). The monotone latches are pinned by
   `test_later_failed_attempts_do_not_revoke_created_latches`; the
   `draft_valid` reversal by
   `test_invalid_repair_after_a_saved_illustration_keeps_the_artifact_claim`,
   the plan replacement by
   `test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan`.
4. **A later failing retry cannot revoke a saved artefact.** The three `created`
   bits are written only on their success paths above; failure hooks write only
   `*_error` (`_fail_document`, `_fail_animation_create`, `_fail_image_gen`)
   plus the plan latches. `draft_valid` is the exception described in I3: it
   tracks the last saved revision and is not a creation latch. Pinned by
   `test_later_failed_attempts_do_not_revoke_created_latches`.
5. **The predicate runs once, only on the normal path.**
   `TurnRunner._require_final_answer` is its only call site; cancelled or
   failed replies settle through
   `src/blackrose/turns.py::TurnRunner._settle_interrupted` (from the
   `except BaseException` branch of `TurnRunner._run`) and never evaluate it.
6. **A started job is counted before the provider runs.** `on_start` is invoked
   in `ChatController._generate_image` before `provider.generate`; the counter
   only advances through `EffectBudget.start_image`. Pinned by
   `test_four_started_jobs_hide_the_tool_without_an_error_latch`.
7. **`image_error` is set only for a started job.** `_fail_image_gen` sets it
   solely in the `else` branch after `inv.image_started`; the `OutboundDenied`
   and pre-start branches never touch it. Pinned by
   `test_pre_start_image_failure_does_not_fail_the_turn` and
   `test_started_image_failure_fails_the_turn_after_an_earlier_success`.
8. **A spent or failed budget is enforced twice.** `definitions()` omits
   `image_gen` when `not images_available` and `EffectBudget.start_image`
   refuses. (`definitions()` also omits it, independent of the budget, while
   `prompt.xiaohei and prompt.plan_only`.) The second guard is not reachable through `execute` because calls
   are sequential and each recomputes `definitions()`; it is defensive. Pinned
   by `test_four_started_jobs_hide_the_tool_without_an_error_latch`.
9. **Every draft mutation is revision-guarded.** `save` is the only mutator;
   with a `draft_id` it must pass `read`, which requires `type(revision) is int`
   and exact equality (`src/blackrose/illustrations.py::IllustrationDrafts.read`).
   `validated` uses the same gate. The only other writer is conversation
   deletion, which removes the whole reply directory atomically
   (`delete_conversation_drafts`) and therefore needs no per-draft token.
   Pinned by `test_every_mutating_path_requires_the_current_revision`.
10. **A rejected draft write writes nothing.** All raising checks (`_path`,
    `read`, the `revision requires draft_id` and draft-limit checks, the repair
    limit and the size limit) precede both `write_json` calls in
    `src/blackrose/illustrations.py::IllustrationDrafts.save`;
    `src/blackrose/config.py::write_json` itself replaces atomically. Pinned by
    the same test and `test_foreign_draft_ids_cannot_be_read_or_written`.
11. **Revision snapshots are immutable and the slot counter ignores them.**
    `draft-<n>-r<m>.json` is written once, at revision `m`; the glob that
    derives `created` in `IllustrationDrafts.__init__` matches only
    `draft-[1-4].json`. Pinned by
    `test_two_repairs_are_allowed_and_snapshots_stay_immutable` and
    `test_new_drafts_count_current_files_not_revision_snapshots`.
12. **Generation re-validates the exact revision.** `_image_gen` calls
    `drafts.validated(draft_id, revision)` before compiling, so the
    `draft_valid` latch is not the only gate; a stale, missing, foreign or
    invalid revision cannot start a job even when `draft_valid` is `True`.
    Pinned indirectly by `test_invalid_draft_without_an_image_reports_no_image`
    (no job, request latched) and existing `test_illustrations.py`
    coverage.
13. **Plan-first cannot run without an accepted plan, and `DISABLED` implies no
    plan.** `_animation_create_planned` refuses when
    `motion_plan is None or motion_plan_disabled`; the only writers of
    `motion_plan_disabled = True` are `_fail_motion_plan`, which also clears
    `motion_plan`, and `_animation_create_planned`'s `except` branch, which
    re-raises into that hook. Pinned by
    `test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan` and
    `test_planned_creation_failure_falls_back_without_keeping_the_plan`.
14. **A downloaded image is persisted before `cancelled`/`cleanup_warning` are
    honoured.** `ChatController._generate_image` appends the attachment before
    returning; the executor sets `illustration_created` and calls `on_images`
    before it reads the flags (`_image_gen`). The `GeneratedImage` docstring
    states the same contract. Pinned by
    `test_cancelled_download_keeps_the_artifact_and_latches_no_error`,
    `test_cleanup_warning_after_a_saved_image_requires_recovery` and existing
    `test_image_recovery.py`.

## Hazards and open questions

| # | Severity | Where | Finding | Demonstrated |
| --- | --- | --- | --- | --- |
| H1 | defect (fixed) | `src/blackrose/tool_execution.py::ToolExecutor.require_successful_artifacts` (draft branch) | A later invalid draft repair after a saved illustration made `require_successful_artifacts` fail with "no image was generated" although the image existed and was attached. The failure itself is deliberate fail-closed behaviour; the claim was false. Now reports that the saved illustration predates the rejected revision. | `test_invalid_repair_after_a_saved_illustration_keeps_the_artifact_claim` (failed before the change; `test_invalid_draft_without_an_image_reports_no_image` keeps the original wording for the genuinely image-less case) |
| H2 | documentation-gap | `src/blackrose/tool_execution.py::ToolExecutor._file_tool` with `src/blackrose/chat.py::ChatController.file_tool` | `document_created` means "`file_tool` returned without raising", not "a file exists". A new file outside the output folder returns `approval_required` with "No file was created" and still satisfies the obligation. The acceptance is arguably correct (the model's part succeeded and a review card is pending), but the name overstates the effect. | `test_document_proposal_counts_as_created_without_a_file` |
| H3 | fragile | `src/blackrose/tool_execution.py::ToolExecutor._fail_motion_plan`, `src/blackrose/tool_execution.py::ToolExecutor._animation_plan` | `animation_error` is last-write-wins. After a rejected plan, a second `animation_plan` (refused because plan-first is latched off) overwrites the root diagnostic, so the reply error says "Plan-first is unavailable for this reply" instead of the validator's `unknown target 'missing'`. The root cause survives only in `motion_plan_error`/`rejected_motion_plan`, which are not shown to the user. A later failed plan also discards an earlier accepted plan (`_fail_motion_plan` clears it) — fail-safe for artefacts, but the accepted work is lost. | `test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan` |
| H4 | fragile | `src/blackrose/tool_execution.py::ToolExecutor.execute` (`except asyncio.CancelledError`) | Inside `except asyncio.CancelledError`, a cancelled `document_create` awaits `host.attachments_for`. If that read raises, the new exception replaces the `CancelledError`, so the reply settles as `error` ("database is locked") instead of `cancelled`: the user's Stop is lost and the transcript claims a failure. Proposed fix below; not applied. | `test_cancelled_document_creation_becomes_a_storage_error` (with `test_cancelled_document_creation_still_propagates_cancellation` as the passing counterpart) |
| H5 | fragile | `src/blackrose/tool_execution.py::ToolExecutor._record_attempt`, `src/blackrose/tool_execution.py::ToolExecutor._image_gen`, `src/blackrose/tool_execution.py::ToolExecutor._controller_tool` | The Xiaohei gate is keyed on the mutable `prompt.xiaohei` at dispatch time, and reading the skill mid-round turns it on (`_controller_tool`). A round whose calls are `image_gen(prompt)` then `read_skill` therefore runs the first call under the generic schema: no draft obligation is created and the reply can end `SATISFIED` with an ungated image, while the reverse order is refused. Model-controlled call order decides whether the contract applies. Not a trust boundary (paid images still pass `OutboundPolicy`), but undocumented. | Not committed; the opposite order is covered by `tests/test_illustrations.py::IllustrationTests.test_skill_read_mid_round_activates_draft_gate`. Confirming needs a round with `image_gen` before `read_skill`. |
| H6 | none-found | `src/blackrose/tool_execution.py::ToolExecutor._image_gen` | `illustration_created` is set before the `cancelled`/`cleanup_warning` checks, so a cancelled or recovery-failing reply counts the image as created. Harmless: both paths re-raise before the predicate can run. It does mean the latch is "image persisted", not "reply continued". | `test_cancelled_download_keeps_the_artifact_and_latches_no_error`, `test_cleanup_warning_after_a_saved_image_requires_recovery` |
| H7 | fragile | `src/blackrose/tool_execution.py::ToolExecutor._animation_create` | `animation_created` is set before `host.finish_animation_repair`. If that auxiliary write raises (SQLite error), the reply fails but the durable claim stays `status='repairing'`; `src/blackrose/animation_recovery.py::AnimationRecovery.claim` then reports `claimed=False` for the same attachment forever, so the failed animation can never be repaired again by the app. Requires a failing recovery-store write. | No; would need `animation_recovery.finish` to raise. Cross-check with the animation-recovery bead. |
| H8 | none-found | `src/blackrose/tool_execution.py::EffectBudget.start_image` vs `src/blackrose/tool_execution.py::ToolExecutor.definitions` | The `start_image` refusal is unreachable through `execute` (the schema is already hidden and calls are sequential); it is a defensive second guard, not dead policy. | `test_four_started_jobs_hide_the_tool_without_an_error_latch` |
| H9 | none-found | `src/blackrose/tool_execution.py::ToolExecutor.__init__`, `src/blackrose/tool_execution.py::ToolExecutor._fail_motion_plan` | `motion_plan_disabled` is a safe fallback latch: once set it is never cleared in the reply, it clears the accepted plan, refuses further planning, and direct `animation_create` still satisfies the animation obligation. It cannot hide a lost artefact. | `test_plan_failure_disables_plan_first_and_revokes_the_accepted_plan` |

### Answers to the bead's focus questions

- **Reachable combinations of the eight obligation bits.** The reachable set is
  the product of four independent instances (`UNATTEMPTED`/`OPEN`/`MET`),
  constrained by I1 and by `prompt.xiaohei` for the illustration pair, plus the
  independent `image_error` bit. The acceptance predicate is a pure function of
  that tuple: reject iff `(A_a ∧ ¬A_c) ∨ (K_a ∧ ¬K_c) ∨ (I_r ∧ ¬I_c) ∨
  (D_a ∧ ¬D_v) ∨ image_error`, evaluated in that order
  (`require_successful_artifacts`). The thirteen representative reachable
  tuples and their verdicts are pinned by
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
  It is set only by a *started* job that then fails (`_fail_image_gen`), which
  includes a second job after a successful first — the documented policy
  ([admission and effects](../architecture.md#admission-and-effects): "An
  execution failure disables image execution for that reply and marks the reply
  failed"), so not wrong. Pre-start rejections, cancellation and
  cleanup-warning continuations (the flag checks at the end of `_image_gen`)
  never set it, and nothing clears it within a reply. The one unverified corner
  is a failure inside the `on_images` callback after a saved image (in
  `_image_gen`, before any `_fail_image_gen`); not demonstrated.
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
  the *current* revision rather than counting repairs (the repair-limit check
  in `IllustrationDrafts.save`), which is equivalent because revisions only
  increase by one from 1.

## Verification

Run from the repository root:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_10_tool_obligations.py -v
```

Two classes cover the draft machine (`DraftRevisionStateTests`) and the
obligation/budget/plan/image machines (`ObligationStateTests`).

The pre-existing focused tests for the same code, each run with the same runner
(`-p <file>`): `test_tool_execution.py`, `test_illustrations.py`,
`test_image_generation.py`, `test_image_recovery.py`, `test_local_images.py`,
`test_gui_images.py`, `test_tool_handlers.py` and `test_motion_plan.py`. Two
`test_motion_plan.py` tests (`test_source_compiles_with_real_motion_canvas_toolchain`
and `test_blood_circulation_sequence_compiles`) need the Motion Canvas runtime
(`blackrose setup-animations`); without it `compile_source` raises "Motion
Canvas needs setup" and they error, while the plan/fallback tests in that module
still run.

Static check for the source change:

```sh
.venv/bin/ruff check src/blackrose/tool_execution.py tests/test_sm_10_tool_obligations.py
```

The H1 change is an error-message branch inside the acceptance predicate; the
`--gui` probe and live image/GPU probes do not exercise it.
