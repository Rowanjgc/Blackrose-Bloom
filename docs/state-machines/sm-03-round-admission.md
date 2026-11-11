# SM-03: Provider round admission, capability locks and the finish-reason latch

## Scope and ownership

- `src/blackrose/turns.py`: `require_normal_finish` (159–183), `collect_tool_call`
  (135–156), `_prepare` (374–428), `_initial_tools` (457–467), `_start_reply`
  (500–529), `_answer` (544–581), `_admit_round` (583–614), `_stream_round`
  (616–663), `_close_ui_segment` (665–676), `_require_final_answer` (678–689),
  `_run_tools` (691–718).
- `src/blackrose/prompts.py`: `Prompt` (103–195), `PromptBuilder.build`
  (363–499), `PromptBuilder.admit` (501–520), `_ui_contract` (98–100).
- `src/blackrose/tool_execution.py`: `ToolExecutor.definitions` (161–172),
  `execute` (212–251), `_record_attempt` (253–262), `_activate_tools` (284–297),
  `_controller_tool` (427–446), `require_successful_artifacts` (178–193),
  `activation_rounds` (118).
- Coupled boundaries, not owned here: `chat.py` `tool_definitions` (675–708) is
  the offering function; `config.py` `max_tool_rounds` (174, validated 240–241)
  sets the budget; `ui_documents.instructions` supplies the UI contract text
  (`prompts.py:98–100`).

Ownership and threading, per [architecture](../architecture.md#modules-and-invariants):

- **One reply, one `TurnRunner`, one `Prompt`, one `ToolExecutor`.** `chat.send`
  builds the runner per turn (`chat.py:1462–1466`); `_prepare` builds the prompt
  and admits it once (374–428); `_start_reply` builds the executor and attaches
  streaming/audit services (500–529). Every flag in this document is in-memory
  and starts fresh for the next reply, so no capability state crosses turns.
- **One asyncio loop.** `_answer` awaits `_stream_round` and then `_run_tools`
  sequentially (544–581); tool calls run one at a time in a `for` loop
  (707–711). No other task mutates `Prompt`, `RoundOutput` or the executor's
  counters during a round. Gui/tool callbacks can only *observe*.
- **Durable state is only the reply row.** Settlement is owned by `TurnRunner`
  and SM-01/SM-02; this machine decides which request is sent and whether the
  reply may complete. The policy it implements is stated at
  `architecture.md:175–180` ("allows one successful activation-only round
  separately from the normal tool-round budget") and `architecture.md:209–216`
  (admission on every model round).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `round_number` | `turns.py:553` (local) | `int`, 0..`max_tool_rounds` | Paid tool rounds spent | `_answer:575–578` |
| `request_number` | `turns.py:554` (loop var) | `int`, 0..`max+1` | Provider requests made; fallback tool-call ID salt | `for` loop; `assign_call_ids:201` |
| `exhausted` | `turns.py:555` | `bool` | `round_number >= max_tool_rounds`; tools withdrawn | derived each iteration |
| `executor.activation_rounds` | `tool_execution.py:118` | `int`, 0..1 | Free activation-only rounds spent | `_answer:575–576` |
| `prompt.ui_profile` | `prompts.py:121` | `str \| None` | Profile of the *next* round, which will be a UI segment | `enable_ui:165`; `disable_ui:173`; `_answer:562` |
| `prompt.ui_used` | `prompts.py:122` | `bool` | One-way per-reply latch: interactive UI was armed | `enable_ui:166` only |
| `prompt.animations_available` | `prompts.py:113` | `bool` | Authoring is allowed for this request | `build:448` |
| `prompt.animations_enabled` | `prompts.py:112` | `bool` | Authoring contract is loaded | `build:497–498`; `enable_animations:187` |
| `prompt.library_write_enabled` | `prompts.py:120` | `bool` | `knowledge_add` is being offered | `enable_library_write:179` |
| `prompt.xiaohei` | `prompts.py:109` | `bool` | Xiaohei image contract is active | `build:386–407`, gated; `_controller_tool:444` |
| `prompt.plan_only` | `prompts.py:110` | `bool` | Image generation withheld for the reply | `build:447` |
| `prompt.counts` | `prompts.py:123` | `PromptCounts \| None` | Admission estimate for the next round | `_prepare:425`; `_admit_round:598, 610`; cleared by `admit:504` |
| `output.finish_reason` | `turns.py:120` | `str \| None` | Latched terminal reason | `_stream_round:656–660` |
| `output.calls` | `turns.py:121` | `dict[int, dict]` | Streamed tool calls, by fragment index | `collect_tool_call:140–156` |
| `preparing_arguments` | `turns.py:623` (local) | `bool` | UI-event latch for the "arguments" phase | `_stream_round:651–653` |
| `executor.animation_attempted`/`animation_created` | `tool_execution.py:107–108` | `bool` | Authoring obligation | `_record_attempt:255–256`; `_animation_*` |
| `executor.illustration_requested`/`illustration_created` | `:104–105` | `bool` | Illustration obligation | `_record_attempt:261–262`; `_image_gen:411` |
| `max_tool_rounds` | `config.py:174` | `int`, 1..16 | Paid tool-round budget | config load (`:240–241`) |

Not persisted: there is no column, JSON file or IPC message for any of these.
The page never sees them; it only receives tool/reply events.

## States

Three machines. They are read together: admission decides the *request*,
the meter decides *how many* requests are allowed, and the locks decide what
the request may *offer*.

### M1 — Admission mode (one state per provider request)

| State | Predicate (at `_admit_round`) | `offered` |
| --- | --- | --- |
| `NORMAL` | `ui_profile is None and not exhausted`, first admission succeeds | `executor.definitions()` (`turns.py:590`) |
| `UI_SEGMENT` | `ui_profile is not None`, admission succeeds | `[]` (`591–592`) |
| `EXHAUSTED` | `ui_profile is None and exhausted` | `[]` + budget notice (`593–597`) |
| `UI_FALLBACK` | `ui_profile was not None`, the first `admit` raised `InputRejected`, the retry succeeded | `definitions()` if `not exhausted` else `[]` (`603–613`) |
| `REJECTED` (terminal) | any other `InputRejected`, or the retry raises | no request is sent; the turn settles `error` |

`UI_SEGMENT` outranks `EXHAUSTED`: a UI segment admitted at the budget edge is
`UI_SEGMENT` with `exhausted is True` and *no* budget notice (pinned by
`test_a_paid_activation_round_admits_the_ui_segment_at_the_budget_edge`).
`UI_FALLBACK` at the edge keeps tools withdrawn and still appends no notice
(`test_the_fallback_at_the_budget_edge_keeps_tools_withdrawn`).

### M2 — Round meter (one state per iteration of `_answer`)

| State | Predicate | Meaning |
| --- | --- | --- |
| `ADMITTED` | iteration start, `_admit_round` returned | a request will stream |
| `FREE` | last tool round: single `activate_tools`, triple changed, `activation_rounds == 0` | `activation_rounds += 1`, `round_number` unchanged (`575–576`) |
| `PAID` | every other tool round | `round_number += 1` (`578`) |
| `ANSWER` | streamed round with `output.calls` empty and `_require_final_answer` accepted | the turn completes |
| `ABORTED` | any refusal below | the turn settles `error`/`cancelled` |

The meter's counters admit exactly one `FREE` per reply: `activation_rounds`
only ever moves 0→1, and the `FREE` predicate requires it to be 0
(`_run_tools:712–715`).

### M3 — Capability locks (one per capability, per reply)

| Capability | States | Predicate |
| --- | --- | --- |
| interactive UI | `LOCKED` → `OPEN` → `ARMED` → `SPENT` | `LOCKED`: not offered. `OPEN`: offered by `activate_tools` (`chat.py:682–685`). `ARMED`: `ui_profile is not None and ui_used`. `SPENT`: `ui_profile is None and ui_used` (`prompts.py:121–122`) |
| animations | `OFF` → `ACTIVE` (or `LOCKED`) | `LOCKED` when unavailable/manual-only; `OFF` when not enabled; `ACTIVE` when `animations_enabled` (`prompts.py:112,183–195`) |
| library write | `OFF` → `ACTIVE` | `knowledge_add` offered only while `knowledge_enabled and not library_write_enabled` (`chat.py:689–690`) |
| Xiaohei | `INACTIVE` → `ACTIVE` | `prompt.xiaohei` (`prompts.py:109`) |

`SPENT` is reachable in exactly two ways, both from `ARMED`: the segment round
begins (`turns.py:560–562`) or the contract cannot be admitted
(`prompts.py:169–173`). `SPENT` never returns to `OPEN`/`ARMED` in the same
reply.

### M4 — Stream latch (inside one round)

| State | Predicate | Meaning |
| --- | --- | --- |
| `OPEN` | `output.finish_reason is None` | content/reasoning/tool-call fragments accumulate |
| `LATCHED(reason)` | `output.finish_reason == reason` | any later content, reasoning or tool-call delta raises (`632–635`) |
| `CONFLICT` | a later, different `finish_reason` | raises (`657–659`); terminal for the round |
| `CLOSED` | the stream iterator ended | `require_normal_finish` decides the round's fate |

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Turn prepared, first prompt admitted | `_prepare:421–427` |
| E2 | Reply services attached | `_start_reply:505–515` |
| E3 | Round admitted | `_answer:556` → `_admit_round` |
| E4 | UI segment begins | `_answer:557–558` → `UIStream.begin` |
| E5 | Round streams | `_stream_round:628–660` |
| E6 | Terminal pair checked | `_answer:568` → `require_normal_finish` |
| E7 | UI segment closed | `_answer:569` → `_close_ui_segment` |
| E8 | Tool round executed | `_answer:575` → `_run_tools` |
| E9 | Free round charged | `_answer:576` |
| E10 | Paid round charged | `_answer:578` |
| E11 | Tool-free round accepted | `_answer:570–572` → `_require_final_answer` |
| E12 | Loop bound reached | `_answer:581` |
| E13 | Admit refuses the prompt | `builder.admit` raises `InputRejected` (`prompts.py:509–520`) |
| E14 | `activate_tools` call | `tool_execution.py:284–297` |
| E15 | `read_skill` names a bundled contract skill | `tool_execution.py:438–445` |
| E16 | Tool call validation | `execute:227` → `validate_call(self.definitions(), …)` |
| E17 | Stop/failure settlement | `_run:358–365` → `_settle_interrupted` (SM-01) |

No other event changes these variables: `Prompt.enable_ui`/`disable_ui` have one
caller each (`_activate_tools` and `_admit_round`); `ui_profile` has one clearer
(`_answer:562`); `activation_rounds` has one incrementer (`_answer:576`).

## Transition table

### M1 — Admission

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | E3 | `ui_profile is None`, `not exhausted` | `offered = definitions()` (`:590`) | `NORMAL` |
| `IDLE` | E3 | `ui_profile is not None` | `offered = []` (`:591–592`) | `UI_SEGMENT` |
| `IDLE` | E3 | `ui_profile is None`, `exhausted` | `offered = []`; append the budget notice (`:593–597`) | `EXHAUSTED` |
| `UI_SEGMENT` | E13 | `ui_profile is not None` (`:604–605`) | `disable_ui()` (`:606`); overwrite `messages[-1]` (`:607–608`); `offered = definitions() if not exhausted else []` (`:609`); recount (`:610–612`); re-admit (`:613`) | `UI_FALLBACK` |
| `UI_SEGMENT` | E13 | retry refuses | exception propagates | `REJECTED` |
| `NORMAL`/`EXHAUSTED` | E13 | — | exception propagates (`:604–605`) | `REJECTED` |
| `NORMAL`/`EXHAUSTED`/`UI_FALLBACK` | E4 | — | `UIStream.begin(ui_profile)`; profile captured and cleared (`:557–562`) | same mode, streaming |

Note the admission function is total and prioritised: UI first (`591`), then
exhaustion (`593`), else normal (`590`). The fallback is entered only from the
UI branch on a specific exception event.

### M2 — Round meter and round outcome

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ADMITTED` | E5/E6 | `reason in {stop,end_turn}` and no calls | — | `ANSWER` |
| `ADMITTED` | E5/E6 | `reason == 'tool_calls'` and calls | — | tool path |
| `ADMITTED` | E5/E6 | `reason` and calls disagree (`:179–181`) | raise ("inconsistent with its finish reason") | `ABORTED` |
| `ADMITTED` | E5/E6 | `reason ∉ {stop,end_turn,tool_calls}` (`:165–178`) | raise with the reason's message | `ABORTED` |
| `ADMITTED` | E5/E6 | `ui_segment` and (calls or `reason ∉ {stop,end_turn}`) (`:182–183`) | raise ("Interactive UI segment did not finish normally") | `ABORTED` |
| `ADMITTED` | E5 | output after `LATCHED` (`:632–635`) | raise | `ABORTED` |
| `ADMITTED` | E5 | conflicting finish reason (`:657–659`) | raise | `ABORTED` |
| `ADMITTED` | E5 | stream ends without a reason | `require_normal_finish:176–178` raises | `ABORTED` |
| tool path | E8 | `offered` non-empty (`:573`) | execute; append assistant wire + tool results (`:696–717`) | `FREE` or `PAID` |
| tool path | E8 | `offered` empty (`:573–574`) | raise ("exceeded the tool-call limit"); no call runs | `ABORTED` |
| `FREE` | E8 returns | single call, `activate_tools`, triple changed, `activation_rounds == 0` (`:712–715`) | `activation_rounds = 1` (`:576`) | next `ADMITTED` |
| `PAID` | E8 returns | otherwise | `round_number += 1` (`:578`) | next `ADMITTED` |
| `ANSWER` | E11 | `exhausted` and `'<tool_call>'` in content (`:685–686`) | raise | `ABORTED` |
| `ANSWER` | E11 | open artifact obligation (`:687`, `tool_execution.py:178–193`) | raise | `ABORTED` |
| `ANSWER` | E11 | no visible content and no attachments (`:688–689`) | raise | `ABORTED` |
| `ANSWER` | E11 | otherwise | return from `_answer` | `DONE` |
| any | E17 | — | `_settle_interrupted` writes the terminal row | terminal |

The fall-off raise at `:581` is unreachable: `PAID ≤ max_tool_rounds`,
`FREE ≤ 1`, `ANSWER` consumes a request, so `max+2` iterations always contain
the answer or abort first (see I5, H6).

### M3 — Capability locks

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `LOCKED`/`OFF` (animations) | E1 `build` | `animations_available` and (selected animation skill, or the request mentions animation and not manual-only, or saved animations) (`:495–498`) | `enable_animations()` adds the contract | `ACTIVE` |
| `OFF` (animations) | E14 `activate_tools("animations")` | capability offered ⇒ available, not manual-only, not enabled, no saved ids (`chat.py:686–688`) | `enable_animations()` (`tool_execution.py:287–288`) | `ACTIVE` |
| `OFF` (animations) | E15 `read_skill` | animation skill id, `SKILL.md`, `not animations_manual_only` (`:438–441`) | `enable_animations()` | `ACTIVE` |
| any | E1/E14/E15 | unavailable / manual-only / already enabled (`prompts.py:185–186`) | returns `False`, no text added | unchanged |
| `OFF` (library) | E14 `activate_tools("library_write")` | knowledge enabled and not yet enabled (`chat.py:689–690`) | `enable_library_write()` adds the contract (`:175–181`) | `ACTIVE` |
| `OFF` (UI) | E14 `activate_tools("interactive_ui", profile)` | config enabled, citation rigor off, bundle available, `not ui_used` (`chat.py:682–685`) | `enable_ui(profile)`: add contract, set both flags (`prompts.py:161–167`) | `ARMED` |
| `ARMED` | E4 | the next round is admitted | profile captured and cleared before streaming (`turns.py:560–562`) | `SPENT` |
| `ARMED` | E13 | the UI round cannot be admitted (`:603–606`) | `disable_ui()` removes the contract, clears the profile, keeps `ui_used` | `SPENT` |
| `SPENT` | E14 | — | the capability is not advertised (`chat.py:682`), so validation fails first (`tool_execution.py:227`); a direct call would raise (`prompts.py:162–163`) | `SPENT` |
| `INACTIVE` (Xiaohei) | E1 | "xiaohei" in the request and the skill enabled, or a selected skill ref (`prompts.py:386–407`) | contract added to the system message | `ACTIVE` |
| `INACTIVE` (Xiaohei) | E15 | Xiaohei skill id + `SKILL.md` (`tool_execution.py:442–445`) | `prompt.xiaohei = True`; the contract travels in the tool result | `ACTIVE` |

### M4 — Stream latch

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `OPEN` | content delta | — | append to `output.content` and `self.content` (`638–644`) | `OPEN` |
| `OPEN` | reasoning delta | — | append to `output.reasoning` (`645–649`) | `OPEN` |
| `OPEN` | tool-call fragment | count/identity/argument bounds (`collect_tool_call:137–156`) | merge; `preparing_arguments = True` once (`650–654`) | `OPEN` |
| `OPEN` | `finish_reason` | first reason | latch (`655–660`) | `LATCHED(reason)` |
| `LATCHED` | identical `finish_reason` | — | no-op (protocols repeat stop reasons) | `LATCHED` |
| `LATCHED` | different `finish_reason` | — | raise (`657–659`) | `CONFLICT` |
| `LATCHED` | content/reasoning/tool-call delta | any truthy value (`632–635`) | raise ("output after its finish reason") | `ABORTED` |
| `LATCHED` | `_provider_content` only | — | overwrite continuation metadata (`636–637`) | `LATCHED` |
| `OPEN`/`LATCHED` | iterator ends | — | `metrics.publish(force=True)` (`661–662`) | `CLOSED` |

## Invariants

1. **Every provider request has exactly one admission mode.** `_admit_round`
   assigns `offered` on exactly one of three exclusive branches, and only the
   UI branch can be replaced by `UI_FALLBACK` (`turns.py:590–613`). Pinned by
   `test_a_normal_round_offers_tools_and_no_budget_notice`,
   `test_the_exhausted_round_offers_nothing_and_appends_the_notice_once`,
   `test_a_ui_segment_offers_no_tools_and_ends_the_turn`,
   `test_the_ui_fallback_restores_tools_and_keeps_the_ui_latch`.
2. **A UI segment offers no tools and no budget notice.** `:591–592` precedes
   the `elif exhausted` at `:593`, and `:591` unconditionally clears `offered`.
   Pinned by the segment tests; the budget-edge test shows the ordering.
3. **`ui_used` is monotone within a reply.** Only `enable_ui` writes it and it
   writes `True`; `disable_ui` deliberately leaves it (`prompts.py:161–173`).
   Pinned by `test_the_ui_contract_can_be_armed_once_and_never_again`,
   `test_the_fallback_state_is_profile_none_and_used_true`.
4. **Interactive UI is offered only while it can take effect.**
   `not ui_used` gates the capability (`chat.py:682–685`), so a spent latch is
   invisible to the model's schema. Pinned by
   `test_activation_capabilities_disappear_once_their_flag_is_set` and
   `test_ui_cannot_be_reactivated_after_the_fallback`.
5. **`round_number` never exceeds `max_tool_rounds`, and each tool round
   increments exactly one counter.** `_answer:575–578` is the only place either
   counter moves; `exhausted` is derived from `round_number` alone (`:555`).
   Pinned by `test_one_activation_only_round_does_not_spend_the_tool_budget`
   and `test_the_free_discovery_round_is_granted_only_once`.
6. **A reply executes at most `max_tool_rounds + 1` tool rounds and makes at
   most `max_tool_rounds + 2` provider requests.** `PAID ≤ max`,
   `FREE ≤ 1`, plus one answer round. Pinned by the same two tests and by the
   `range(max_tool_rounds + 2)` bound (`:554`).
7. **The free round is activation-only and is granted once.** The predicate
   requires one call, the literal name `activate_tools`, an observed change in
   `(animations_enabled, library_write_enabled, ui_used)`, and
   `activation_rounds == 0` (`_run_tools:704, 712–715`). Two *successful*
   activations in different rounds yield `[True, False]` (same test); a repeated
   identical activation is refused by the offer (`chat.py:682–690`) and spends a
   paid round (existing `test_capability_disclosure.py` covers that variant).
8. **A round's `finish_reason` and calls must agree for every protocol.**
   Validation happens after decoding, so it is protocol-independent
   (`require_normal_finish:176–181`). Pinned by
   `test_finish_reason_and_calls_must_agree`,
   `test_every_normal_terminal_pair_is_accepted`,
   `test_abnormal_and_missing_reasons_reject_the_round`.
9. **A UI segment is a tool-free normal finish.** `:182–183`; pinned by
   `test_a_ui_segment_must_be_tool_free_and_normal` and
   `test_a_ui_segment_that_returns_tool_calls_runs_nothing` (no call executes).
10. **Once the finish reason is latched, no content, reasoning or tool call is
    accepted.** `bounded_choices`/`_stream_round` bound the stream before any of
    it is used (`:632–660`); pinned by
    `test_output_after_the_finish_latch_is_rejected`.
11. **A conflicting reason fails the round; an identical repeat does not.**
    `:657–659`; pinned by `test_conflicting_finish_reasons_are_rejected` and
    `test_an_identical_repeated_finish_reason_is_accepted`.
12. **A round without a finish reason is never a finished answer.** The latch
    stays `None` and `require_normal_finish:176–178` refuses it; pinned by
    `test_a_round_without_a_finish_reason_is_never_a_finished_answer` and, at
    the protocol layer, `test_stream_outcomes.test_bare_done_cannot_complete_any_protocol`.
13. **`ui_profile` is cleared before its round streams.** `_answer:560–562`
    captures then clears it; the recorder observes `ui_profile is None` at
    `_stream_round` entry in every test that asserts `streams[...]`.
14. **The armed contract is removed from the system message on fallback.**
    `disable_ui` (`prompts.py:169–173`) restores the pre-arm bytes; pinned by
    `test_the_ui_fallback_restores_tools_and_keeps_the_ui_latch`
    (`system` contains the contract, `system_after` does not) and by
    `test_the_ui_contract_can_be_armed_once_and_never_again`.
15. **A capability flip between two calls of one round can only reject, never
    widen, an effect.** Every call is validated against the live
    `definitions()` at execution (`tool_execution.py:227`), so an already
    advertised call is refused rather than reinterpreted. Pinned by
    `test_a_skill_read_switches_the_image_contract_inside_the_same_round`.
16. **Capability flags are per reply.** A fresh `Prompt` and `ToolExecutor` are
    built in `_prepare`/`_start_reply`; the existing
    `test_capability_disclosure.py::test_activation_resets_between_turns_without_saved_artifacts`
    pins the reset.
17. **`ui_profile` and `ui_used` agree except in `SPENT`.** `enable_ui` is the
    only writer of both and writes them together (`prompts.py:161–167`); the
    only way to observe `profile is None, used is True` is the two transitions
    in M3. Pinned by the latch tests.
18. **A capability that is `ACTIVE` always has its tools in the next round's
    offer — except at exhaustion.** `chat.py:705–707` includes animation tools
    whenever `animations_enabled`, but `_admit_round:590` withholds *all* tools
    when `exhausted`. **Violated** by H2's `read_skill` path; the code only logs
    (`turns.py:682–684`).

## Hazards and open questions

- **H1 — `fragile` (near-defect) — `turns.py:607–608`.** The UI fallback
  assumes `prompt.messages[-1]` is the `activate_tools` result. That is only
  true when the activation was the round's last call. A round may legally
  contain `[activate_tools(interactive_ui), <other tool>]` (multi-call rounds are
  executed in order, `_run_tools:708–711`); the following UI round then rewrites
  the *other* call's accepted tool result with the UI-fit error, silently
  dropping its evidence, while the activation result keeps its stale
  "emit NDJSON" claim. Demonstrated by
  `test_the_fallback_overwrites_the_last_tool_result_documented_follow_up`
  (the `file_read` result is replaced and `Fixture evidence.` is absent from
  the request). Fix in "Formalization decisions".
- **H2 — `documentation-gap` — `tool_execution.py:438–441` with
  `turns.py:554, 590, 682–684`.** `read_skill` of the bundled animation skill
  enables the authoring contract in whatever round it is read. If that is the
  last paid round, the next request is `EXHAUSTED` with `offered == []`: the
  system message advertises the authoring contract and the model can never call
  `animation_create`. The turn still completes; `_require_final_answer` only
  logs. Demonstrated by
  `test_a_skill_read_in_the_last_paid_round_discloses_an_unreachable_contract`.
- **H3 — `fragile` — `tool_execution.py:444` with `:227` and `:253–262`.**
  `read_skill` of `ian-xiaohei-illustrations` sets `prompt.xiaohei` *between*
  the calls of one round. An `image_gen` authored under the round's advertised
  (non-Xiaohei) schema is then validated against the Xiaohei schema and
  rejected — safe, because validation precedes the effect. But
  `_record_attempt` runs before validation (`:214` vs `:227`), so it also
  records `illustration_requested` from that rejected call, and
  `require_successful_artifacts` fails the reply with "Illustration generation
  did not complete" even though no draft could exist. Demonstrated by
  `test_a_skill_read_switches_the_image_contract_inside_the_same_round`. No
  image job starts (asserted), so this is a confusing failure, not a silent
  effect.
- **H4 — `fragile` — `prompts.py:169–173`.** `disable_ui` removes the contract
  by re-deriving its bytes from `ui_documents.instructions(profile)` instead of
  remembering what `enable_ui` added. The derivation is deterministic today
  (module-level bundle/contract caches), so the removal is exact; a cache
  clear or reload between arming and fallback would leave the contract in the
  system message while `ui_profile` is `None` — the model would be told to emit
  NDJSON in a normal, tool-bearing round. Not demonstrated: confirming it needs
  `ui_documents.instructions` to return different bytes for the same profile
  mid-turn (patch it to a call counter and assert the contract survives).
  Cheapest hardening is in "Formalization decisions".
- **H5 — `documentation-gap` — `turns.py:591–597 vs 603–613` with
  `prompts.py:169–173`.** At the budget edge the UI branch outranks the
  exhaustion branch, so no budget notice is appended for that reply (the UI-fit
  notice is the only guidance the model receives). The model is told to answer
  in Markdown, not that its tools are gone. Pinned as current behaviour by
  `test_the_fallback_at_the_budget_edge_keeps_tools_withdrawn`; harmless
  (tools are still withheld) but the two notices are not composable.
- **H6 — `none-found` — `turns.py:554, 575–578, 581`.** The loop bound is
  exactly consistent with the counters: at most `max` paid rounds, at most one
  free round and one answer round = `max+2`. The raise at `:581` is therefore
  unreachable. It is a useful backstop, but it is coupled to "at most one free
  round": a future second activation round would silently turn into
  "Tool limit reached without a final answer" rather than a budget bypass.
- **H7 — `none-found` — `_run_tools:712–715`.** A free round cannot be granted
  twice: the counter guard, the disappearing capability and the loop bound each
  block it independently. Tested with two successful activations.
- **H8 — `none-found` — `_stream_round:632–660` with `require_normal_finish`.**
  The latch and the terminal-pair check are protocol-independent, so no
  provider decoder can smuggle a call past a `stop` or a narrative past
  `tool_calls`. `tests/test_stream_outcomes.py` (12 tests, five protocols) and
  the unit matrix here agree.
- **H9 — `documentation-gap` — `_stream_round:636–637`.** The post-latch guard
  covers `content`, `reasoning_content` and `tool_calls` but not
  `_provider_content`, which a post-latch packet may still overwrite. The
  bundled decoder emits it on the same choice as the finish reason
  (`backend/opencode_protocols.py:156–162`), so no product path reaches the
  gap; a future decoder that splits them would change continuation metadata
  after the terminal pair was latched.

### Answers to the focus questions

- **Which admission modes exist, and are they exclusive and exhaustive?**
  `NORMAL`, `UI_SEGMENT`, `UI_FALLBACK`, `EXHAUSTED` (M1). The first assignment
  is a total, prioritised function of `(ui_profile, exhausted)`
  (`turns.py:590–593`), and the fallback replaces only the UI branch on a
  specific exception event (`:603–613`); every other refusal sends no request.
  `UI_SEGMENT` can co-occur with `exhausted` (a paid activation round at the
  edge), which is why the code orders UI first — the mode is still unique.
- **Can `ui_profile` and `ui_used` disagree harmfully (UI re-enabled after a
  rejection)?** The only reachable disagreement is `SPENT` (`profile None`,
  `used True`), which is the intended latch. Re-arming is refused three ways:
  `enable_ui` raises (`prompts.py:162–163`); the capability is not advertised
  once `ui_used` (`chat.py:682–685`), so a model call fails validation
  (`tool_execution.py:227`) before any effect; and a direct call would become an
  error result. Tested directly and end to end.
- **Is the free `activate_tools` round reachable twice?** No. Three independent
  guards: `activation_rounds == 0` in the predicate, the capability disappears
  from `activate_tools` once its flag is set, and the loop bound only ever pays
  for one extra round. Two successful activations produce one free and one paid
  round.
- **Can `round_number` and `activation_rounds` drift, giving more tool rounds
  than `max_tool_rounds`?** They cannot drift: every tool round increments
  exactly one counter (`_answer:575–578`), and `exhausted` is derived from
  `round_number` alone. A reply gets at most `max_tool_rounds + 1` tool rounds,
  and the extra one is exactly the documented free activation-only round
  (`architecture.md:177–178`). The request bound `max+2` is tight and
  consistent.
- **Are `finish_reason`/`calls` validated for every protocol, including a UI
  segment returning tool calls?** Yes. Validation is after decoding, so all
  five protocol adapters share it; a UI segment is additionally required to be
  `stop`/`end_turn` with no calls, and the segment's calls never execute
  (asserted by the absence of a tool continuation).
- **Does `read_skill` mutating `animations_enabled`/`xiaohei` mid-round create a
  mid-turn capability change the prompt contract did not disclose?** The
  animation contract *is* added to the live system message and the next
  admission recounts and re-admits it, so the next request discloses both the
  contract and the tools — except at exhaustion (H2), where the contract is
  disclosed without the tools. The Xiaohei flip is disclosed only through the
  tool result's `generation_contract` (`tool_execution.py:445`), by design
  (`architecture.md:160–163`); the same-round schema change is *not* in the
  model's context, so a call it already wrote can be rejected (H3). No flip ever
  grants an effect that was not offered at admission.

## Formalization decisions

**H1 fixed in `turns.py`; no `prompts.py` change.** H1 was applied by the
orchestrator once the concurrent beads had landed: the UI-fit fallback now
corrects the `activate_tools` result rather than `messages[-1]`, and the
characterization test was inverted to
`test_the_fallback_corrects_the_activation_result_not_the_last_call`. The patch
is kept below for review.

1. **H1, `turns.py:603–613` — correct the activation result, never another
   call's result.** Replace the unconditional `messages[-1]` rewrite:

   ```python
   except InputRejected:
       if not prompt.ui_profile:
           raise
       prompt.disable_ui()
       notice = json.dumps({'error': 'Interactive UI contract could not fit. '
                                   'Answer the original request in ordinary Markdown.'})
       # The armed contract has exactly one activation result; correct that
       # message instead of whatever call happened to be last.
       for message in reversed(prompt.messages):
           if message.get('role') == 'tool' and message.get('name') == 'activate_tools':
               message['content'] = notice
               break
       else:
           prompt.messages.append({'role': 'user', 'content': notice})
       offered = reply.executor.definitions() if not exhausted else []
       prompt.counts = count_prompt(prompt.messages, offered)
       metrics = GenerationMetrics(reply.model, prompt.messages, offered, self.events.on_metrics,
                                   counts=prompt.counts)
       reply.builder.admit(prompt, offered)
   ```

   The reversed search finds the most recent activation result, which is always
   the one that armed the UI contract. When the activation was the last call the
   bytes sent are unchanged. Invert
   `test_the_fallback_overwrites_the_last_tool_result_documented_follow_up` to
   assert that the `file_read` result survives and the activation result carries
   the notice; `test_the_ui_fallback_restores_tools_and_keeps_the_ui_latch`
   keeps passing.

2. **H4, `prompts.py:161–173` — remember the bytes that were added.** Add an
   `init=False, repr=False, compare=False` field `_ui_contract_text`, set it in
   `enable_ui` and pass it to `remove_instruction` in `disable_ui`. Not applied:
   the non-determinism it defends against was not demonstrated, and the extra
   field would need care to stay out of `Prompt` equality (used by
   `test_capability_disclosure.py:49`).

3. **H2/H3 — product/owner decisions, not mechanical fixes.** H2 would need
   either a post-tool admission that refuses to add a contract no remaining
   round can honour, or an explicit provenance note in the tool result. H3 would
   need `_record_attempt` to run after validation (so a rejected call creates no
   obligation) or the Xiaohei flip to be deferred to the next round. Both widen
   behaviour beyond this bead's evidence.

## Verification

Offline, from the repository root, with `.venv/bin/python`:

```sh
scripts/check_unit_tests.py -p test_sm_03_round_admission.py -v
# Ran 26 tests in 0.354s
# OK
scripts/check_unit_tests.py -p test_prompts.py
# Ran 3 tests ... OK
scripts/check_unit_tests.py -p test_turns.py
# Ran 28 tests ... OK
scripts/check_unit_tests.py -p test_tool_execution.py
# Ran 3 tests ... OK
scripts/check_unit_tests.py -p test_stream_outcomes.py
# Ran 12 tests ... OK
scripts/check_unit_tests.py -p test_capability_disclosure.py
# Ran 21 tests ... OK
scripts/check_unit_tests.py -p test_stream_limits.py
# Ran 13 tests ... OK
scripts/check_unit_tests.py -p test_prompt_history.py
# Ran 2 tests ... OK
scripts/check_unit_tests.py -p test_prompt_counts.py
# Ran 3 tests ... OK
.venv/bin/ruff check tests/test_sm_03_round_admission.py
# All checks passed!
git diff --check
# (no output: no source file was modified)
```

No source file was modified, so no wider module suite was re-run. The full
`scripts/check_health.py` gate was not run because sibling agents are editing
shared files in this worktree during the same session.

Unverified: H4 (needs `ui_documents.instructions` to change between arming and
fallback; reasoned from `prompts.py:169–173` and the process-wide caches in
`ui_documents.py:99–105`). H1 is demonstrated only through the injected
`InputRejected`; the fallback branch itself is the real code, and the clobber
only needs the real order of `messages` in a two-call round.
