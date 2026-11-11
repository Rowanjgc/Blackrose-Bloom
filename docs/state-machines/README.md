# State machines

Blackrose has no single explicit state machine. Its behaviour is carried by
coupled booleans, nullable references, task handles, SQLite `CHECK` columns and
`dataset`/`data-*` attributes spread across modules. That is workable while the
code is small and hostile to review once it is not: a combination that cannot
occur is indistinguishable from one nobody has thought about, and the legal
transitions live in whoever happens to call the function next.

This directory records one investigation per machine. Each document has the same
shape: the state variables and their owners, a predicate for every state, the
events, a transition table that cites the code by symbol, numbered invariants that
can be falsified, and the hazards the investigation demonstrated or merely
reasoned about. Each has a companion suite under `tests/test_sm_*.py` that pins
the cheap part of the machine: guards, terminal immutability, idempotent
recovery, ordering and refusal paths.

These are investigations, not a refactor. A document may describe an awkward but
sound design, and most do. Where a hazard was demonstrated and the fix was small,
the fix landed with the document; where the fix would cross a shared central file
or change a contract, the patch is written out in the document's "Formalization
decisions" section instead. Read a document as the specification of the code as it
is, plus the evidence for what should change next.

## Method

1. Read the owning module and every caller that writes the coupled fields.
2. Name the states so that each is a predicate over the fields.
3. Enumerate the reachable combinations, and say which are excluded and by what.
4. Walk the events through the transition table, including cancellation, crash,
   timeout and refusal.
5. Try to falsify each invariant with a small deterministic test.
6. Record hazards by severity: `defect` (wrong behaviour, demonstrated),
   `fragile` (right today by convention or ordering), `documentation-gap` (the
   rule exists only in prose), `none-found` (the interesting hazard was checked
   and does not exist).
7. Change source only for a demonstrated hazard, and only with a denial or
   failure test that fails before the change.

## Hazards by severity

Counts are hazards recorded across the 22 investigations, not open defects. Many
`fragile` items are deliberate trade-offs; each document says why.

| Severity | Meaning |
| --- | --- |
| `defect` | Demonstrated wrong behaviour with a regression test. |
| `fragile` | Correct today because of ordering, a single writer, or a convention that is not enforced. |
| `documentation-gap` | A rule exists only in prose or in a caller's discipline. |
| `none-found` | Checked, and the machine holds. |

## Index

Turn execution, streaming and persistence:

| Id | Document | Machine |
| --- | --- | --- |
| SM-01 | [Turn orchestration](sm-01-turn-orchestration.md) | `TurnRunner` phases from prepare through commit, answer, audit and settlement |
| SM-02 | [Stored message lifecycle](sm-02-stored-message-lifecycle.md) | `messages.state`, usable history, crash reaping |
| SM-03 | [Round admission](sm-03-round-admission.md) | Provider rounds, UI segments, the free activation round, capability latches |
| SM-04 | [SSE stream decoder](sm-04-sse-stream-decoder.md) | Line framing, per-block accumulation and terminal reasons across four protocols |

Interactive UI, citations, diagrams and animations:

| Id | Document | Machine |
| --- | --- | --- |
| SM-05 | [Interactive UI stream](sm-05-interactive-ui-stream.md) | Parser modes, block status and revision, widget state sequence |
| SM-06 | [UI action events](sm-06-ui-action-events.md) | `ui_events` acceptance, consumption and settlement, inflight lock |
| SM-07 | [Citation audit](sm-07-citation-audit.md) | Audit phases, claim downgrades, answer identity |
| SM-08 | [Diagram repair](sm-08-diagram-repair.md) | Failure-driven syntax repair, promotion, session queue |
| SM-09 | [Animation recovery](sm-09-animation-recovery.md) | Playback repair chain and the WebKit scene probe |

Tools, workspace, artifacts and policy:

| Id | Document | Machine |
| --- | --- | --- |
| SM-10 | [Tool obligations](sm-10-tool-obligations.md) | Effect budgets, artifact obligations, illustration drafts |
| SM-11 | [Workspace files](sm-11-workspace-files.md) | Selection, edit and library proposals, output journal, atomic backups |
| SM-12 | [Artifact cleanup](sm-12-artifact-cleanup.md) | Cleanup obligations and reconciliation |
| SM-13 | [Outbound policy](sm-13-outbound-policy.md) | Privacy latch, site grants, approval gates |
| SM-14 | [Skill lifecycle](sm-14-skill-lifecycle.md) | Staging, review digest, switch and effective availability |

Local services, memory and knowledge:

| Id | Document | Machine |
| --- | --- | --- |
| SM-15 | [Local image GPU](sm-15-local-image-gpu.md) | GPU handoff, keep-warm window, ComfyUI ownership |
| SM-16 | [Router supervision](sm-16-router-supervision.md) | Desktop lock, router adoption/launch/stop, model residency |
| SM-17 | [Memory outbox](sm-17-memory-outbox.md) | Honcho readiness, profile staleness, durable outbox |
| SM-18 | [Conversation titles](sm-18-conversation-titles.md) | Placeholder, automatic and manual naming |
| SM-19 | [Knowledge index](sm-19-knowledge-index.md) | Schema and identity, atomic rebuild, lazy embedder |

Desktop shell and front end:

| Id | Document | Machine |
| --- | --- | --- |
| SM-20 | [GUI session](sm-20-gui-session.md) | Boot, generation slot, draft recovery, UI view tokens |
| SM-21 | [Renderer recovery](sm-21-renderer-recovery.md) | WebKit crash recovery, consent sheet, external handoff, emitter |
| SM-22 | [Frontend runtimes](sm-22-frontend-runtimes.md) | Animation players, Mermaid rendering, UI host, page controller |

## Working on a machine

Pin the invariant before changing the code, and keep the test in the same commit
as the change. If an investigation and the code disagree, the code is the bug or
the document is stale; do not leave the disagreement in place. Bounded live or
native probes belong in the document's verification section with their
prerequisites, because the unit suite deliberately stays offline.

Where a document's prose and its `tests/test_sm_*.py` suite disagree, the suite is
authoritative: it is run on every change and the prose is not. Correct the
document in the same change that exposes the disagreement.

Cite code by symbol, never by line number, which moves with every edit above it.
Use `path::Qualified.name` (for example
`src/blackrose/turns.py::TurnRunner._prepare` or
`tests/test_turns.py::TurnTests.test_stop_before_the_final_save_starts_settles_the_reply_as_cancelled`);
`scripts/check_docs.py` checks that each dotted part is still defined. When the
exact statement matters, cite its function and name the statement in words ("the
`finally` that clears `is_generating`"). Link other documents by heading anchor.

A verification section records how to reproduce the evidence: the commands, their
prerequisites and what they establish. It does not record the state of the
checkout or session that first ran them.
