# Blackrose Bloom engineering standards

**Status: ratified 2026-11-05 (`bra-6t9`).** Proposed 2026-11-03, then reviewed
and amended by the maintainer. It is linked from [CONTRIBUTING.md](../CONTRIBUTING.md) and the
[documentation index](README.md). The standards are applied in review. Ratifying
them did not change Ruff, mypy or CI, and tooling enforcement of any standard
needs its own decision.

This is not a generic style guide. Every normative statement below is grounded in
Blackrose Bloom's actual architecture, constraints or audit findings.

Bead IDs (`bra-…`), the decision log (`DECISIONS.MD`) and the dated audit cited as
"Audit §n" are the maintainer's private records, kept outside this repository.
Contributors without access can follow the standards without them.

**How to read the labels.** Each section states which of three kinds of
statement it contains; the [appendix](#appendix-status-of-each-convention) lists
them together.

- **Established** — implemented today and backed by code, tests or a recorded
  decision. Breaking one is a defect unless a new decision supersedes it.
- **Standard** — the expectation for new and changed code. It applies when you
  touch an area; it is not a claim that existing code is noncompliant, and it is
  enforced by review, not by tooling.
- **Proposed** — an accepted direction that is not implemented yet, with the
  Bead that tracks it. Do not describe it as current architecture or gate on it.

Companion documents, which remain authoritative for their subjects. If this
document disagrees with one of them, the companion describes what the code does;
correct this document rather than the code:

- [Architecture and runtime ownership](architecture.md) — module ownership map.
- [Contributing](../CONTRIBUTING.md) — setup, checks, repository layout.
- [Workspaces](workspaces.md), [citation rigor](citation-rigor.md),
  [memory](memory.md), [interactive answers](interactive-ui.md) — feature
  constraints.

## 1. Core principles

*Standard.*

1. **Clarity over cleverness.** Write the version a maintainer can understand at
   2 a.m. without running the code. Prefer explicit data flow and explicit
   failure over compact cleverness.
2. **Simplicity over unnecessary sophistication.** Add structure when it removes
   a demonstrated problem. Do not add a registry, dependency-injection container,
   plugin system or generic framework because it might be useful later.
3. **Consistency over personal preference.** Match the surrounding module,
   naming, error style and test style. A different but equally good style is not
   a reason to rewrite working code.
4. **Cohesion over line count.** A long, linear function with one job can be
   better than five fragments that share hidden state. Split when a unit has
   multiple reasons to change, not when it crosses an arbitrary length.
5. **Explicit ownership.** Every durable fact and every policy should have one
   owner. If two modules can mutate the same truth, the design is not finished.
6. **No more complicated than necessary — and no less.** Reliability, security
   and data integrity are requirements, not polish. Simplicity is not an excuse
   to skip a transaction, a regression test or an authorization check.
7. **Preserve the project's identity.** Blackrose Bloom is a local-first desktop
   assistant with a native GTK/WebKit shell, in-process Python policy and
   deliberately small provider adapters. Do not import an architecture from a
   different kind of product.

The test for a change is: *does it make the next change easier, safer or more
understandable without weakening an existing guarantee?*

## 2. Architecture

### 2.1 Established conventions to preserve

*Established.* These are implemented today. Treat them as constraints, not
suggestions. References name a module docstring or symbol rather than line
numbers, which drift.

**Three-thread desktop model.** GTK/WebKit runs on the main thread; application
work runs on one asyncio loop; page events are delivered by the emitter thread.
The page renders session state and must never own durable turn or tool policy
(`gui/bridge.py` module docstring). New page-facing methods go through the `Api` facade,
accept plain values, validate them, and must do nothing the user could not do by
typing a command.

**One conversation, one turn, one active reply.** Navigation is bound to
`ChatController.selected_conversation`; each turn runs under a `ContextVar` with a
stale-view token. Renderer recovery settles work but never resubmits a turn. Do
not introduce a second source of "current conversation" or a parallel reply loop.

**System message is application policy only.** Untrusted text — skills, memory,
citation evidence, local references, tool output — belongs on the request message
in a delimited block (`prompts.py`: `SKILL_POLICY`, `MEMORY_POLICY` and their block
delimiters). Context admission runs before every model round. Any new context source must follow this rule and must not be appended to
the system message.

**One system-message representation.** `Prompt.instructions` holds application
policy once; `messages[0]` is rendered from it and the host-written attachment
manifest. Change policy only with `Prompt.add_instruction`/`remove_instruction`
or the capability methods built on them; `instructions` and `manifest` are
read-only (`prompts.py` module docstring, `bra-alv`).

**Host-owned security decisions.** Citation evidence is captured by the host; the
model selects source/span identities and never supplies supporting excerpt text.
Outbound web and remote-image requests are authorized by `OutboundPolicy` per
reply. Workspace access is confined by `WorkspaceFiles` descriptors and no-follow
opens. External application launch is confined to `gui/external.py`. Do not
reimplement these checks in a caller.

**Tool handlers see a narrow host.** `ToolExecutor` dispatches through an
explicit handler mapping (`HANDLERS`, `FILE_TOOL_NAMES`, `CONTROLLER_TOOLS`,
`FAILURE_HOOKS` in `tool_execution.py`) and holds a `ToolHost`
(`tool_host.py`), never the `ChatController`. Handlers reach only the named host
capabilities; the outbound policy is reachable only as `outbound_active` and
`preauthorize`. Add a tool by adding its handler and, if it needs one, a named
host capability; do not reach controller privates. `tests/test_tool_handlers.py`
guards the mapping and the host surface (`bra-8wp`).

**Terminal state is durable and explicit.** A turn commits its user message and
assistant placeholder atomically; cancellation settlement is shielded at commit
boundaries; partial answers are saved and reported as partial. A reply whose
complete write has committed stays complete even if Stop lands during that
write (`bra-1hs`). A change that can
leave a half-written turn, a duplicate effect or a silently swallowed terminal
failure violates this convention.

**Turns run as named phases.** `TurnRunner._run` is a short orchestration of
prepare, begin, start-reply, answer (one admit/stream/check/tools sequence per
round) and complete, with shielded failure settlement. Phases pass data
explicitly; the single-use runner holds only the state settlement must see when
a failure or Stop lands. Put new turn behavior in the phase that owns it, not in
`_run` (`turns.py` module docstring, `bra-yj0`).

**Serialized persistence.** All SQLite access goes through the `Repository` and
the one-connection worker in `storage/db.py`. Migrations run atomically with the
schema version. Do not open a second writable connection or perform ad-hoc SQL
from a feature module.

**Provider adapters stay small.** Local and cloud providers sit behind
`ChatProviders`; protocol-specific continuation details stay in `backend/`.
Do not leak provider-specific fields into prompts, tools or the page.

### 2.2 Proposed architectural improvements

*Proposed.* These are accepted directions from the 2026-11-03 audit, not yet
implemented. Do not describe them as current architecture. (The audit's tool
handler boundary has since shipped; see §2.1.)

- **One provider-selection source.** `ChatProviders` should derive the provider
  consistently from the model or store model and provider atomically. Tracked by
  `bra-rr1`.

### 2.3 Dependency direction

*Standard; removing the two existing cycles is Proposed (`bra-lae`).*

Prefer module-level imports. Use a function-local import only when:

- the dependency is optional or heavy and importing it at module load would slow
  startup or require an unavailable platform binding (for example `webview` or a
  GPU library), or
- a genuine cycle cannot be removed without a larger refactor.

When you use a function-local import for cycle avoidance, leave the cycle better
than you found it or record why it remains. Do not add a new cycle. The two cycles
that were hidden behind function-local imports under `bra-lae` are removed:

- `tools.py` ↔ `animations.py`: the animation-create parameters now live in
  `animations.py`;
- `citations.py` → `tool_execution.py` (constant only): the result-size budget
  now lives in `tools.py`.

`tests/test_import_direction.py` keeps these edges from returning.

Higher-level policy modules may import lower-level data/utility modules. A lower
level module must not import the controller, the session or the page bridge to
reach policy.

## 3. Code organization and readability

*Standard.*

**Module responsibility.** A module should have a name that describes its
ownership, and its docstring should state the invariant or trust boundary it
enforces. `outbound.py`, `workspace.py`, `storage/db.py` and `prompts.py` are the
model: a narrow job, explicit inputs, and a documented reason for the boundary.

**Classes and functions.** A class may be large if it is cohesive. Split when it
has multiple unrelated reasons to change, when tests need to mock half of it, or
when a new feature repeatedly edits unrelated sections. `ChatController` and
`Session` currently carry concentrated responsibilities;
changes to them should leave the touched area clearer, not add another unrelated
branch. Do not launch a broad split without a scoped plan and characterization
tests.

**Naming.** Name things after the domain (`Conversation`, `Turn`, `Evidence`,
`WorkspaceFiles`, `OutboundPolicy`), not after their implementation
(`Manager`, `Helper`, `Util`, `Processor`). A boolean should read as a claim
(`is_usable_history`, `has_images`). Prefer explicit parameter names over
positional booleans.

**Control flow.** Prefer early returns and linear phases over deep nesting. A
function that mixes validation, I/O, policy and rendering should be split by
phase. A function that is long because it enumerates a protocol or a schema may
be fine; do not fragment it mechanically.

**Duplication.** Duplicated *business logic* is a defect; duplicated *boilerplate*
is often acceptable. If two paths enforce the same security or state invariant,
extract the invariant. If two tests or two schema declarations look similar,
prefer a shared helper only when the shared concept is stable.

**Comments and docstrings.** Explain intent, constraints and "why", not line-by-
line mechanics. Document a non-obvious invariant, a security decision, a bound, a
cancellation rule or a compatibility reason. Do not add noise comments that
repeat the code.

**Dead code and compatibility paths.** Remove unreachable branches, unused
parameters and abandoned compatibility paths when you touch them, unless a
documented compatibility guarantee requires them. Prove a branch is dead before
removing it: the audit reported an unreachable `OutboundDenied` branch in
`ToolExecutor.execute`, but `bra-8wp` found it was a duplicate guarding the
image job's not-started notice, so it was merged rather than deleted.

## 4. Error handling, state and asynchronous work

*Standard. The exception types, `Repository` transactions and cancellation
settlement it builds on are Established (§2.1).*

**Fail predictably.** Use the existing exception types (`StorageError`,
`FileAccessError`, `OutboundDenied`, `InputRejected`, `ChatBusy`,
`VisionUnsupported`, `MemoryUnavailable`) and preserve their user-facing
messages. A new failure mode should have a named error or an explicit result, not
a generic `Exception` that the UI cannot explain.

**Do not silently swallow.** A broad `except Exception` is acceptable at a
best-effort cleanup or recovery boundary, but it must log the failure or carry a
comment explaining why silence is correct. `bra-z41` reviewed the existing
swallowed handlers; do not add new ones without the same justification.

**State ownership.** A durable fact has one owner. UI state may be cached, but the
durable source is the database or the controller. Do not create a second
in-memory "selected conversation", "active model" or "pending approval" without
defining how it is invalidated.

**Transactions and settlement.** If a change performs more than one durable
write, define the settlement rule: what is committed together, what is retried,
what is reported as partial, and what happens on cancellation. Use the existing
`Repository` transaction patterns; do not invent a second transaction mechanism.

**Cancellation.** Treat `CancelledError` as a normal control path at commit
boundaries. Shield the commit, then re-raise. Do not swallow cancellation into a
success state. Keep the existing "settle, do not resubmit" rule for renderer
termination.

**Do not block the loop.** Synchronous parsing, rendering, hashing, image work or
large database scans belong in `asyncio.to_thread` or an existing worker. Keep
bounds on work: stream budgets, tool-result caps, import limits, page sizes and
timeouts are part of the contract, not optional polish.

**Cleanup.** A resource acquired in a request (provider stream, file descriptor,
temporary stage, worker future) must be released on success, failure and
cancellation. Prefer context managers and existing lifecycle owners over manual
flags. Test the failure path, not only the success path.

## 5. Testing

*Standard. Behavior-focused tests with real SQLite are Established; the coverage
and mutation baseline is Proposed (`bra-ot9`).*

**Test behavior and invariants, not implementation choreography.** Assert what a
user or a caller can observe: persisted rows, returned payloads, emitted events,
files on disk, authorization decisions, terminal state. Avoid asserting that a
private method was called unless the call *is* the contract. The existing suite is
a good model: the 2026-11-03 audit counted 999 test functions and only 33
mock-call assertions.

**Every behavior change needs a regression test.** The test should fail on the
old behavior and pass on the new one. State the scenario in the test name. If a
bug is fixed, include the reproduced input or a minimal equivalent.

**Use the right level.**

- Unit tests for pure policy, validation, parsing and state transitions.
- Integration tests with real temporary SQLite for persistence, migrations,
  rollback and recovery.
- Contract tests for provider protocols, tool schemas and bridge payloads.
- Browser probes for shipped page behavior; native `--gui` probes for GTK/WebKit
  behavior.
- Failure-injection tests for security boundaries, cancellation and settlement.

**Security changes need boundary tests.** A change to outbound authorization,
workspace confinement, path handling, approval lifecycle, skill review or
citation evidence belongs in `tests/test_security_boundaries.py` (or a focused
suite with the same style) and should cover denial as well as success.

**Tests must be deterministic and isolated.** Use temporary directories and
databases; never touch the user's real chat database, network or GPU. Use the
existing scripted provider streams and fake transports. A test that depends on a
live model, a paid API or a real GPU belongs in an explicit operational probe, not
the offline suite.

**Do not couple tests to internal structure.** Refactoring should not require
rewriting behavior tests. If a test breaks because a private method moved, ask
whether it was testing behavior or implementation.

**Coverage and mutation are diagnostics, not gates.** A coverage percentage can
reward trivial lines; a mutation score can reward equivalent mutants. Use them to
find weak security tests, not to enforce a number. The proposed baseline is
`bra-ot9`.

**When in doubt, add the smaller test.** A focused regression test is cheaper to
maintain than a broad fixture. A large end-to-end test is appropriate when the
interaction *is* the behavior (turn settlement, renderer recovery, approval
lifecycle).

## 6. Security and data integrity

*Standard. The boundaries it protects are Established (§2.1).*

Security and data integrity are foundational design concerns. This section states
engineering expectations; it does not replace the dedicated security audits.

**Authorization stays central.** Model-proposed web requests and remote image
jobs go through `OutboundPolicy`. Do not add a direct HTTP call, a new image
egress path or a tool handler that bypasses the policy. A handler that needs
egress uses the `ToolHost` capabilities, which leave the policy with the
controller; never give a handler the policy object or a raw network client.

**Filesystem access goes through `WorkspaceFiles`.** New file tools must use its
descriptor-confined operations, no-follow opens, bounded reads and approved
replacement path. Do not use `open()`, `Path.write_text()` or `shutil` directly on
user paths. Symlinks are refused by design. Approved edit backups live outside
project folders.

**Untrusted text never becomes policy.** Skills, memory, web pages, tool results,
local file contents and model output are untrusted. Keep them on the request
message in delimited blocks; do not interpolate them into the system message, tool
schema or approval UI as instructions. Neutralize control/bidirectional text when
displaying untrusted content.

**The page is not an authority.** Every `Api` method is reachable from page script.
Validate plain values, re-check conversation/model ownership and never expose a
capability the user could not invoke by typing. The page may render; it may not
decide durable policy.

**Persistence integrity.** Use migrations, transactions and the existing rollback
patterns. Never leave a partially written turn, attachment, evidence snapshot or
edit backup. If a multi-step operation can fail midway, define recovery or
settlement before shipping it.

**Privacy.** Do not log URLs, queries, message bodies, file contents or secrets.
Log decisions, hosts, identifiers and error classes. Memory is opt-in; recalled
text is untrusted request text; forgetting must follow the recorded provenance.

**When a security boundary must change,** write it down: the threat model, the
new enforcement point, the denial behavior and the tests. Update
`docs/architecture.md` and the relevant feature guide, and create or reference a
security Bead. Do not quietly broaden an existing grant.

## 7. Documentation and architectural decisions

*Standard. The `DECISIONS.MD` entry format and the current-versus-preserved split
in `docs/README.md` are Established.*

**Update documentation with the change.** At minimum:

- `docs/architecture.md` when module ownership, an invariant or a trust boundary
  changes. The ownership table is selective: add an entry when a utility acquires
  durable policy or state, not for every helper.
- The relevant feature guide when user-visible behavior, limits or configuration
  changes.
- `docs/README.md` when adding a new guide or preserved record.
- `DECISIONS.MD` when a significant decision is made or amended.

**Proportionality.** A typo fix, a small bug fix or an internal rename needs no
new document. A new subsystem, a new trust boundary, a new persistence schema or a
change to a public bridge contract needs documentation. Use judgment.

**When to write an ADR.** Use `docs/adr/` for a decision that:

- has multiple viable options with long-term consequences;
- changes ownership, trust boundaries or persistence identity;
- is expensive to reverse; or
- future maintainers will reasonably ask "why was it done this way?"

Small decisions belong in `DECISIONS.MD` as dated entries with the existing
format: decision, why, consequences, reconsider conditions. Reference the Bead or
audit that motivated it. Do not rewrite history; amend with a dated note.

**Document intent, not just mechanics.** A module docstring should say what the
module owns and what it refuses to do. A design note should say which constraint
drove the choice. A test should say which invariant it protects. Avoid
documentation that merely restates the code.

**Keep the index honest.** `docs/README.md` distinguishes current guides from
preserved evidence. New guides go in the current table; dated investigations go
under "Preserved reviews and evidence". Do not present a historical measurement as
a current guarantee.

## 8. Dependencies and abstractions

*Standard.*

**Justify every dependency.** Before adding a package, answer:

- What concrete problem does it solve that the standard library or an existing
  dependency cannot?
- Is it maintained, licensed compatibly and available on the supported Python
  versions and platforms?
- What is the install, startup, security and maintenance cost?
- Can it be confined to one module or an optional extra?

Record the decision. The project already pins security floors (for example the
Certifi advisory note in `pyproject.toml`) and maintains `THIRD_PARTY_NOTICES.md`;
follow that pattern.

**Prefer the smallest abstraction that solves the demonstrated problem.** An
interface is justified when there are at least two real implementations or a
test seam that cannot be achieved otherwise. A registry is justified when the set
of things is open-ended and repeatedly extended. A plugin system is justified only
when third parties must extend the product without changing it. None of these
should be introduced speculatively.

**Do not add a layer to hide a dependency.** If a module needs `Repository`, pass
it explicitly. If a handler needs one controller capability, pass that capability,
not the whole controller; `ToolHost` is the model. Explicit parameters are clearer
than a service locator.

**Keep provider and platform seams narrow.** `ChatProviders` is intentionally
small. `backend/service.py` owns router lifecycle. `gui/external.py` owns external
launch. Do not grow these into general-purpose frameworks.

**Remove dependencies deliberately.** An unused dependency is maintenance and
supply-chain cost. Remove it with the lockfile and notices update, and run the
distribution check.

## 9. Refactoring principles

*Standard.*

**Preserve behavior first.** A refactor changes structure, not observable
behavior. If you need both, land them separately: add the regression test and the
behavior change, then refactor, or refactor first with characterization tests.

**Characterize before extracting.** If a function or class has no behavior tests,
add tests that pin its current observable behavior before moving code. Do not
"clean up" and change semantics in the same commit.

**Keep changes reviewable.** Prefer a sequence of small, independently verifiable
steps over one large rewrite. Each step should leave the application working and
the test suite passing.

**Do not mix unrelated changes.** A bug fix should not reformat a file, rename
unrelated symbols or upgrade a dependency. A refactor should not add a feature.
Reviewers should be able to explain the change in one sentence.

**Leave the touched area clearer.** If you must edit a concentrated class, extract
the piece you are changing or add the missing test. Do not add another unrelated
responsibility just because the class is already large.

**Do not refactor security boundaries casually.** Outbound authorization,
workspace confinement, skill review, citation evidence and persistence settlement
need boundary tests and explicit review. A "small cleanup" in these areas can
silently widen authority.

**Measure before and after when performance motivates the change.** The project
has a culture of bounded measurements (`docs/audits/`, `scripts/check_health.py`).
Use it. Do not claim a performance improvement from a line-count reduction.

## 10. Agent-assisted development

*Standard.*

Blackrose Bloom is developed extensively with coding agents. These expectations
apply to agents and to humans reviewing agent work.

**Understand before changing.** Read `docs/architecture.md`, the relevant module
docstring and the tests for the area. Search for the symbol and its callers. Do not
assume a conventional architecture; this project has deliberate constraints that
may look unusual.

**Respect ownership and invariants.** The page does not own policy; the system
message is policy-only; authorization is central; workspace access is confined;
turns settle durably. If a request seems to require violating one of these, stop
and explain the conflict rather than working around it.

**Prefer focused, reviewable changes.** One coherent change per unit of work. Do
not reformat files, rename unrelated symbols, upgrade dependencies or add
speculative abstractions. Do not "fix" code you were not asked to touch unless it
blocks the change, and then call it out.

**Avoid speculative abstraction.** Do not add an interface, registry, factory,
plugin system or dependency-injection layer because it might help later. Solve the
demonstrated problem with the smallest structure that fits the existing design.

**Preserve security and data integrity.** Never widen an approval, bypass a
policy, add a direct network or filesystem path, or move untrusted text into the
system message. Add the denial test as well as the success test.

**Validate important behavior.** Run the relevant tests, then
`scripts/check_health.py` for the full offline gate. Run `--gui` or browser probes
when the change affects native or shipped page behavior. If a probe cannot run,
say so explicitly and describe the residual risk.

**Update the documentation.** If ownership, invariants, limits or user-visible
behavior changed, update the architecture map or feature guide in the same change.
If you made a significant decision, record it in `DECISIONS.MD` and consider an ADR.

**Review your own diff for accidental complexity.** Before finishing, reread the
diff: Did you add a branch that cannot happen? A second source of truth? A
deferred import that hides a cycle? A broad catch without a reason? A test that
asserts a mock call instead of behavior? Remove it or explain it.

**Record uncertainty.** If you could not verify a behavior, say which claim is
unverified and what would verify it. Do not present an inference as a measurement.

**Proportionality.** A one-line fix does not need an ADR, a new Bead or a full
audit. A new subsystem does. Match the process to the risk; do not perform
bureaucracy for its own sake.

## 11. Keeping the standards honest

*Standard (process).*

Engineering experience should inform the standards, but standards changes must
remain deliberate and reviewable. The existing workflow already supports this:

1. **Work leaves evidence.** Beads close with root cause, resolution,
   verification and an engineering takeaway.
2. **Observations become proposals.** A recurring lesson, a successful pattern or
   a repeated failure can produce a proposed standards amendment or a technical-
   debt Bead. It must cite the evidence (Bead IDs, audit record, measurement).
3. **Proposals are reviewed.** The maintainer decides whether to accept,
   amend or reject. Accepted changes are dated in `DECISIONS.MD`; rejected ones
   remain as evidence, not as policy.
4. **Only ratified text is normative.** Do not auto-edit this document from
   generated memories. Do not enforce a draft standard in CI or agent
   instructions. Keep the distinction between an observation, a proposed
   improvement and an approved standard.

A lightweight review cadence is enough: when a Bead closes with a reusable
takeaway, ask whether it contradicts or extends this document. If yes, open a
small standards amendment rather than changing behavior silently. If no, leave the
document alone.

## Appendix: status of each convention

| Convention | Status | Evidence / source |
| --- | --- | --- |
| Core principles (§1) | Standard | Audit §4, this document |
| Code organization, naming, control flow, duplication, comments (§3) | Standard | Audit §5.1, this document |
| Named errors, logged broad catches, state ownership, settlement, non-blocking loop, cleanup (§4) | Standard | `bra-z41`, audit §4.1, §4.4 |
| Regression test per behavior change; test at the right level (§5) | Standard | Audit §4.6, §5.6 |
| Security rules for new code (§6) | Standard | Audit §7 |
| Documentation proportionality and ADR threshold (§7) | Standard | Audit §4.7, §5.7 |
| Dependency justification; smallest abstraction (§8) | Standard | `pyproject.toml`, `THIRD_PARTY_NOTICES.md` |
| Refactoring separately from behavior change; characterization first (§9) | Standard | Audit §9 |
| Agent-assisted development expectations (§10) | Standard | Audit §1 |
| Standards amendment loop (§11) | Standard (process) | `DECISIONS.MD`, Beads close reasons |
| Three-thread desktop model; page renders, never owns policy | Established | `gui/bridge.py`, `docs/architecture.md` |
| System message is policy-only; untrusted text on request message | Established | `prompts.py`, `docs/architecture.md`, `DECISIONS.MD` |
| One system-message representation | Established — `bra-alv` | `prompts.py`, `tests/test_prompts.py` |
| Context admission before every model round | Established | `prompts.py`, `turns.py` |
| One active reply; view tokens; no stream rebinding | Established | `gui/session.py`, `turns.py` |
| Atomic turn creation and cancellation-safe settlement | Established | `turns.py`, `storage/repository.py` |
| `TurnRunner._run` runs as named phases | Established — `bra-yj0` | `turns.py`, `tests/test_turns.py` |
| Serialized SQLite worker, rollback, atomic migrations | Established | `storage/db.py` |
| Descriptor-confined workspace, symlink refusal, atomic replacement | Established | `workspace.py`, `docs/workspaces.md` |
| Central outbound authorization and privacy-preserving logging | Established | `outbound.py`, `chat.py` |
| Host-owned citation evidence | Established | `citations.py`, `docs/citation-rigor.md` |
| Skills reviewed as third-party text; digest-gated | Established | `skills.py`, `DECISIONS.MD` |
| Behavior-focused tests; real SQLite failure injection | Established | `tests/`, `test_storage.py` |
| Targeted mypy and correctness-first Ruff gate | Established | `pyproject.toml`, `docs/architecture.md` |
| Dated decisions with reconsider conditions | Established | `DECISIONS.MD` |
| Explicit tool handler mapping behind a narrow `ToolHost` | Established — `bra-8wp` | `tool_execution.py`, `tool_host.py`, `tests/test_tool_handlers.py` |
| A committed complete reply outlasts a late Stop | Established — `bra-1hs` | `turns.py`, `DECISIONS.MD` 2026-11-05 |
| One provider-selection source | Proposed — `bra-rr1` | Audit §5.4 |
| Coverage + targeted mutation baseline | Proposed — `bra-ot9` | Audit §5.6 |
| Import-direction rule and cycle removal | Proposed — `bra-lae` | Audit §5.5 |
| Ratification of this document | Established — `bra-6t9` | `DECISIONS.MD` 2026-11-05 |
