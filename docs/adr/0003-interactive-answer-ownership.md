# ADR 0003: Interactive-answer contracts and durable acceptance

Date: 2026-10-17. Status: accepted. Base: `7040328`; implementation is in
the housekeeping working tree until a completion commit is recorded.

## Decision

`Session` owns current view tokens, drafts, presentation, busy/permission guards
and reply scheduling. `ChatController` owns an `InteractiveAnswers` collaborator
for persisted assistant ownership, source revision and runtime compatibility,
state validation/persistence, and action validation/acceptance/outcomes.
`UIEnvelope`, `UIBlock`, `SavedUIState`, `AcceptedUIAction` and `AnswerRepository`
describe that boundary. The repository retains immutable source, atomic sequence
checks, event uniqueness and transactional event-to-user-message linkage;
`TurnRunner` retains admission, provider continuation and durable turn identity.

The collaborator concentrates the runtime/identity gate and the state/event
policy previously spread through Session. It also settles a cancelled acceptance
before recording an interrupted outcome. Removing it would return those policies
and persistence dependencies to presentation code. It is not a general capability
framework. Targeted mypy includes this module, semantic validation/streaming,
persisted records and the repository. Session as a whole is not yet a typing gate.

Session checks its view before and after the owned-record read and again after
action acceptance. Reload, conversation change or an intervening busy/permission
state interrupts acceptance before scheduling a visible turn. Cancellation during
the commit waits for its outcome and interrupts only a newly accepted event;
a duplicate event cannot overwrite an earlier outcome. Startup reaps accepted
or started events left by process failure. Retrying uses a new event identity.

## Compatibility and product constraints

No storage migration or wire-shape change is needed. Unknown protocol/library/
schema identities and missing/corrupt installed runtimes retain readable fallback
and owned source export while refusing effects. Generation and follow-up flags
remain independent; disabling generation preserves saved replay/local controls.
State saves remain sequenced, with the existing debounce-loss limit on abrupt exit.

Model-authored content is bounded semantic data; the trusted registry supplies
browser code. Python remains authoritative for effects, and provider adapters
remain unchanged. Historical user fields are included in model context only by
explicit submission or an explicit request to use saved values.

The feature stays disabled by default. The local evaluation's 95% schema-valid
rate does not establish useful, faithful answers. Promotion needs fresh content
and visual review against the rollout gate in the [current guide](../interactive-ui.md).

## Verification

[Authority/lifecycle tests](../../tests/test_ui_documents.py) cover stale views
and revisions, sequenced state and reload, duplicate actions, cancellation during
acceptance, reload between acceptance and scheduling, and startup interruption.
[Populated compatibility tests](../../tests/test_saved_compatibility.py), the
browser probe and native replay/state/action probe cover saved replay and fallback.
Run health and `npm run test:browser`; run native checks under an isolated Xvfb
display using the [contributor instructions](../../CONTRIBUTING.md).
