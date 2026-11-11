# Runtime ownership and code health

The desktop uses GTK/WebKit on the main thread, one asyncio loop for application
work, and an emitter thread for page delivery. The page renders session state;
it does not own durable turn or tool execution policy.

## Modules and invariants

| Module | Owns |
| --- | --- |
| `gui/session.py` | Drafts, attachment admission, active reply state, complete reload snapshots, cancellation recovery before persistence, UI view tokens, transient action/busy guards and animation-repair scheduling |
| `gui/renderer_recovery.py` | GTK renderer-termination handling, bounded host reloads and native recovery controls; Session retains cancellation and durable turn settlement |
| `chat.py` | Conversation commands, selected model, validated attachments, durable artifact operations and review/commit of library proposals |
| `artifacts.py`, `artifact_cli.py` | Durable cleanup retries, shared-blob protection, read-only reconciliation and explicit orphan repair under desktop ownership |
| `turns.py` | Request admission, atomic turn creation and cancellation-safe identity handoff, streaming (including semantic blocks/repair), terminal reply state and evidence attribution |
| `citations.py` | Opt-in evidence snapshots, host-owned source/span identities, bounded claim assessment/revision and exact supporting excerpts |
| `ui_documents.py` | Typed bridge envelope, bounded semantic documents, schema identity, state validation, readable fallback, streaming block assembly and continuation serialization |
| `interactive_answers.py` | Controller-owned persisted UI identity/runtime gate, validated state persistence, action acceptance/cancellation settlement and outcomes |
| `diagnostics.py` | Bounded rotating app/router logs and fixed-chunk router output draining |
| `ui-runtime/` | Trusted registry generating schema/prompts/fixtures; semantic-to-OpenUI compilation and renderer |
| `library.py` | Bounded source captures, conversation-scoped proposal tokens and review cards; no indexing before controller approval |
| `animation_recovery.py` | Separate durable repair claims, replacement chains and the two-attempt bound |
| `motion_canvas.py`, `motion_canvas_probe.py` | Pinned TSX compilation, supervised sandbox playback, deadlines and measured validation |
| `titles.py` | Bounded topic-title prompts, output cleanup and early closing of title streams; `chat.py` owns background scheduling, preemption and guarded persistence |
| `prompts.py` | Usable history, complete accepted document evidence, skills, artifact catalogs and context admission on every model round |
| `workspace.py` | Descriptor-confined filesystem operations, bounded discovery, Downloads selection, collision handling, approved replacements and the move of displaced originals into the private backup store |
| `edit_backups.py` | Durable schema-7 records of displaced originals, crash settlement from named stages, guarded restore, explicit removal and conflict-preserving age cleanup |
| `edit_review.py` | Bounded line comparison and line-ending/final-newline summary shown before an approved replacement |
| `documents.py` | In-process Markdown-to-PDF rendering without external resources or script execution |
| `skills.py` | Managed packages, staged import review, reviewed-digest and enabled-state checks, frontmatter discovery, confined text reads and paginated resource indexes |
| `skill_execution.py`, `skill_script_launcher.py` | Whole-package execution snapshots and digests, mandatory namespace/syscall confinement, bounded JSON jobs, cancellation and shutdown reaping; controller owns report publication |
| `tool_execution.py` | Argument validation against the turn's advertised schemas, the explicit handler mapping (`HANDLERS`, `FILE_TOOL_NAMES`, `CONTROLLER_TOOLS`) under which an unlisted tool fails before any effect, bounded effects, result serialization, per-tool failed-attempt policy (`FAILURE_HOOKS`) and artifact failure outcomes; holds a `ToolHost`, never the controller |
| `tool_host.py` | The only controller surface tool handlers can reach: named capabilities that delegate to `ChatController` at call time, so effects, persistence and configuration stay with the controller; the outbound policy is exposed only as `outbound_active` and `preauthorize`, never as an object |
| `backend/providers.py` | Explicit provider selection, stable provider session identity and continuation details |
| `backend/service.py` | Local router launch/adoption/shutdown, its per-launch API key file and launch hardening, and adoption exposure warnings that Session shows as notices |
| `storage/` | Serialized workers, mutation rollback, atomic migrations, user/pending-reply creation, immutable finalized UI blocks, sequenced saved state and deduplicated action events |
| `outbound.py` | Per-reply authorization of model-proposed web requests and remote image jobs: user-typed or search-result URLs, search queries and remote images before local data, and exact-request native approval otherwise; enforced in `ChatController._call_tool` and `_generate_image` |
| `gui/external.py`, `gui/frame_isolation.py` | The only path from the window to other applications: link/file/folder validation, GIO default-handler resolution, a native confirmation naming destination and handler (Cancel default, warning for active HTML) and launch on acceptance; no frame may navigate away, open a window or download |
| `gui/announcements.py` | Native speech for page live regions: WebKitGTK raises no accessibility event when a live region changes, so `bloom.js` forwards each region's settled text through `Api.announce`, which emits a bounded ATK notification on the window (assertive only for `role=alert`/`aria-live=assertive`) |
| `web/transport.py` | Resolution validation tied to numeric public-IP connections, retaining the original HTTPS origin |
| `local_images.py` | GPU handoff sessions, keep-warm record and idle release, round batching, chat-load preemption, bounded jobs, cleanup settlement and explicit recovery failure state |
| `memory.py` | Optional loopback Honcho client: idempotent `blackrose` workspace/peer setup and failure backoff, bounded recall as untrusted request text, the durable outbox (redaction, pasted-text omission, session forgetting with the dream conclusions built on it) and the `/memory` review operations; `turns.py` calls recall, `storage/` queues turns and forget jobs, the bridge confirms forget-all natively; see [memory](memory.md) |
| `local_knowledge/` | Index identity and coherent documents/chunks/FTS/vector/snapshot mutation, with deterministic embedding adapters in tests |

The [desktop ownership decision](adr/0001-desktop-ownership.md) records why only
one desktop may own a chat database. Existing SQLite and native Retriever seams
remain in-process. Legacy animation JSON remains validated data. New animation
authoring uses generated Motion Canvas TSX: the host compiles against pinned
imports, then supervises execution in an opaque-origin `sandbox="allow-scripts"`
frame without a desktop bridge. Trusted runtime/compiler code is distinct from
that generated source. Type, timeline, caption and playback checks do not establish
explanation quality. See [animations.md](animations.md) and
[motion-canvas.md](motion-canvas.md).

## Interactive answers, library review and recovery

Mermaid remains ordinary Markdown and introduces no saved block type or provider
tool. The Python fence parser records explicit closure, and the page preserves
diagram figures across streaming redraws. `diagram-runtime/` builds a full local
Mermaid host; one lazy opaque-origin sandbox frame renders serially with no bridge
or network. The trusted page receives bounded SVG strings and displays them as
passive images rather than inserting generated SVG into its DOM. Visibility
scheduling, bounded caching and stale-view checks keep work local to readable
content. See [diagrams](diagrams.md) for limits, fallback behavior and probes.

`DiagramRepairs` uses the shared tool-free `repairs.py` stream for syntax failures
reported by the browser after a reply settles. Session shares deferred scheduling
with animation repair and owns Stop, scope and patch delivery. Repository claims
and Markdown revisions use existing `message_blocks`; the raw assistant message
stays immutable, the attempt survives reload, and future model history uses the
corrected block source. Promoted plain replies render corrections within the
original Markdown tokens so citation offsets and surrounding prose stay intact.
Resource/policy/runtime failures do not spend repair requests. The normal prompt
and successful rendering path carry no repair instructions or extra model calls.

Navigation changes `ChatController.selected_conversation`. `TurnRunner` binds
the originating conversation in a controller-local `ContextVar` for the turn
and its child tasks, resetting it on exit. Controller history, workspace tools,
privacy marks and outbound grants therefore keep using that owner while another
chat is displayed. Session reads the explicit selection for page state, retains
one active reply with its conversation ID, and replays it only in its own chat.
Navigation and reload snapshots serialize; transcript reads retry if the owner
or turn version changes. Scoped stream events are also checked by the page
after emitter batching. The generation reservation remains global, including
Stop; viewing another chat does not admit another turn. Verification:
[navigation regressions](../tests/test_chat_navigation.py),
[browser probe](../scripts/check_chat_navigation.cjs), and the
[native session probe](../scripts/check_gui_session.py).

Interactive answers are opt-in semantic data, not generated browser code.
`ui_documents.py` validates source against the generated contract; trusted
`ui-runtime/` code compiles accepted documents into the pinned renderer.
Session checks and rotates view tokens, retains drafts, guards busy/permission
state and schedules replies. The controller's `InteractiveAnswers` collaborator
owns the persisted assistant/revision/runtime compatibility gate and validated
state/action persistence. Typed envelopes, saved blocks, state/action results and
a repository protocol make that boundary explicit. Session accesses it without
private repository calls for interactive operations. The repository enforces
immutable finalized source, state sequencing and event uniqueness; `begin_turn`
atomically links an accepted event to its visible user message. Cancellation
settles acceptance and records interruption before returning. Session rechecks
its view after reads/acceptance, and startup reaps unfinished events. TurnRunner
streams, persists and repairs blocks through the normal provider path.
Session boot completes local interruption recovery before publishing the first
transcript, then loads the selected provider. Saved interruption states remain
visible while the provider loads or if it is unavailable.
[ADR 0003](adr/0003-interactive-answer-ownership.md) records ownership and
compatibility; this policy boundary is included in targeted mypy. Verification:
[UI tests](../tests/test_ui_documents.py), `npm test --prefix ui-runtime`, and
[native replay/state/action probe](../scripts/check_gui_interactive_ui.py).
The [interactive-answer guide](interactive-ui.md) records protocol and limits.

`LibraryProposals` retains bounded fetched-page or saved-artifact captures and
review tokens in memory. `ChatController` checks source ownership and review
approval before invoking native Retriever capture. Session snapshots replay
pending cards during page reload; proposals are not durable across process
restart. Capture completion settles before cancellation propagates. Verification:
[library tests](../tests/test_library.py) and
[native review/reload probe](../scripts/check_gui_library.py).

Session queues playback failures after the active reply, revalidates attachment
ownership and cloud availability, then commits an `AnimationRecovery` claim before
starting a normal repair turn. The auxiliary database under the attachment store
retains replacement chains and attempts across restarts. Controller conversation
deletion owns recovery and illustration cleanup through the schema-4 cleanup
journal; Session reports partial failures. Complete turns and approved file edits
share writer exclusion with artifact reconciliation and cleanup. Startup retries
recorded obligations; unrecorded orphans need explicit repair after a read-only
report and backup. See the [cleanup guide](artifact-cleanup.md) and
[ownership ADR](adr/0002-artifact-cleanup.md).
Verification: [offline recovery persistence](../tests/test_animation_recovery_store.py),
[artifact lifecycle tests](../tests/test_artifacts.py), the
[restore drill](../scripts/check_recovery.py), and the playback-dependent
[session recovery tests](../tests/test_animation_recovery.py) /
[native Motion Canvas probe](../scripts/check_gui_motion_canvas.py) with `--recovery`.

## Skill and animation activation

Skills follow three disclosure stages through the existing in-process tool path:
enabled metadata in the initial request, selected `SKILL.md` instructions in a tool
result, and individual reference bodies only when requested. Package discovery
parses frontmatter from the bounded `SKILL.md`; resource indexes list paths without
opening their contents.

A skill is available only when it is switched on and its current `SKILL.md` bytes
match the SHA-256 digest the user approved (`reviews.json` beside `state.json`).
GUI imports stage a private copy, return its review through the bridge and publish
only on approval; cancelling discards staging. A changed or malformed review record
fails closed. `blackrose install-skill` records bundled skills as approved. Skill
names, descriptions and bodies never enter the system message: `SkillManager.catalog`
returns one delimited, JSON-encoded untrusted block and `PromptBuilder` appends it,
with any explicitly selected instructions, to the transient request message. The
system message carries only the fixed skill policy. Every provider protocol therefore
serializes skill text as user content, and admission counts it there.
`read_skill` accepts directory paths for paginated discovery as well as file paths
for text. `SkillManager` owns package access; `PromptBuilder` explains selection
and performs admission; `ToolExecutor` returns results through the normal tool
continuation. Skill bodies and indexes are transient to the reply and never add
executable tools. No Retriever indexing, provider adapter, or storage migration
is involved. The host's Xiaohei validation contract remains active for explicit
requests or a successful instruction read, without preloading its references.
Host contracts follow skill identity only: reading or selecting the named bundled
`explanatory-animations` or `ian-xiaohei-illustrations` skill loads its contract,
while matching text in any other skill changes no tools or instructions.

Empty enabled-skill catalogs contribute no catalog or `read_skill` schema. The
default prompt's reader guidance is omitted in that case; custom system prompts
remain verbatim. Availability is snapshotted for a reply and recomputed for the
next request, while package access still checks enabled state at read time.

Animation authoring instructions and schemas are disclosed per reply. Obvious
animation requests, selected/read animation instructions and saved conversation
animations activate them immediately; other requests advertise a compact
`activate_tools(capability="animations")` discovery tool. Animation discovery and
execution require an active cloud provider; local requests cannot activate them,
even through skills, historical attachments, or queued automatic repairs. `Prompt` owns the
activation state and unchanged authoring contract, `ToolExecutor` validates and
enables the capability, and `TurnRunner` allows one successful activation-only
round separately from the normal tool-round budget. Activation does not change
filesystem grants or artifact/effect limits. Context admission includes the
expanded request before the next inference call.

## Admission and effects

Optional script execution is a separate permission: `execution_digest` in the
existing review records binds every package file, and only the native bridge
confirmation can grant it. `run_skill_script` routes through `ToolHost` to the
controller, which retains conversation ownership and publishes validated `.txt`
reports through its existing durable output path. The child receives an immutable
snapshot, JSON stdin and no workspace/outbound grant. Mandatory Bubblewrap
namespaces, a default-deny syscall filter and hard limits apply before package
code runs; unavailable confinement never falls back to host execution.
Instruction-only behavior is preserved. See [execution contract](skill-scripts.md)
and [ADR 0004](adr/0004-skill-script-execution.md).

Tool dispatch is an explicit boundary. `ToolExecutor` validates each call against
the schemas advertised for that turn, then routes it through one entry in its
handler tables; a name outside them fails before any effect. Handlers hold a
`ToolHost`, not the `ChatController`. Each host capability delegates to the
controller at call time, so effects, persistence and configuration stay there,
and web and remote-image requests still pass `OutboundPolicy` in `_call_tool` and
`_generate_image`. Python privacy is only a convention, so a test parses
`tool_execution.py` and rejects private-attribute access on anything but the
executor itself. Verification:
[handler contract and host boundary tests](../tests/test_tool_handlers.py).

Citation rigor is off by default. When enabled, ToolExecutor captures only accepted
retrieval output into a per-answer evidence registry before delivery; model-supplied
quotation text cannot enter it. The audit selects existing source/span identities,
and application code slices saved source text to construct supporting excerpts.
TurnRunner owns the shared audit deadline, selected provider, cancellation and
atomic final answer/report publication. Schema 5 stores reports and evidence inside
the chat database; Session renders safe citation controls and replays pending/final
audit states. Old evidence metadata and populated schemas 1–4 remain compatible.
See [citation rigor](citation-rigor.md) for scope, limits and verification.

Document extraction owns its 40,000-character limit and reports omitted source
text. Prompt construction includes that complete accepted extraction and
discloses its truncation. The model never receives a silent second slice.

Context admission accounts for the assembled history, system/clock instructions,
skills, tool definitions, tool results, continuation metadata and embedded image
estimates. It runs before storing a new turn and before every model round. It
reserves output space without changing server inference presets. Until provider
tokenizers are available, text uses a conservative UTF-8-byte token bound and
images use a 14-pixel patch estimate plus framing overhead. This can refuse a
request a particular tokenizer would accept; it is not a billing estimate or a
guarantee about every provider's multimodal tokenizer.

Each reply can execute at most four image jobs. Correctable validation failures
do not start a job. An execution failure disables image execution for that reply
and marks the reply failed. Instructions to the model explain this policy; the
host enforces it even if the model repeats calls. Animations retain their own
four-artifact cap and validation-repair loop.

Completed images are saved before cancellation or memory-recovery warnings end
the reply. Inference does not resume after an unresolved local GPU recovery
failure. The diagnostic is retained and later requests are refused before model
loading; address the local process/memory problem and restart the desktop.

Local image jobs follow the chat model the window has selected when the job starts,
not a configured constant. The router's status for that model decides what is
released and restored: a loaded model is unloaded only when the image job cannot
share its memory, and it is then restored by name; a sleeping, unloaded or cloud
model is never unloaded or woken. Sharing is allowed only for the model the
headroom was measured with. If memory is still short after Blackrose releases its
own model, the job is refused; another client's model is never unloaded.

After a job on the sharing path the image model stays loaded for 120 seconds,
unless less system RAM is free than sharing needs, in which case it is released. A
warm record in the state directory, written only under the handoff lock, lets any
window's next job reuse it and restart the window; the owning window releases it
when the window ends. The record also carries the duty to stop a `comfyui.service`
that Blackrose started. A job the warm model cannot serve, including one that
finds less system RAM free than sharing needs, frees it before its VRAM checks. Before a chat model is loaded or switched, and before each model round,
the warm model is released unless that chat model is the loaded model it shares
with. Several `image_gen` calls in one round run under one handoff, so the
exclusive path restores the chat model once. The Models dialog shows the state
and offers Release now; the page only displays Python's state.

## Illustration drafts

Xiaohei authoring uses `illustration_draft(changes, draft_id?, revision?)`.
Five content fields are required: core_idea, facts, scene, action, and labels.
Object descriptions, directed connections, placement, and review notes are optional.
The host supplies fixed character/style constraints and standalone layout defaults.
The tool envelope is validated first, while incomplete or structurally invalid
content is saved and receives the complete structural issue list with field paths.
Unknown fields and conflicting aliases are never silently dropped. Only recognized
field names with redundant literal quotes are normalized.

Nonblocking review findings flag potential crowding and remaining verification needs.
Drafts are scoped to the assistant reply, persisted under the attachment store's
`illustration-drafts/<conversation>/<message>/` directory, and removed when the
conversation is deleted. Current draft files and immutable revision snapshots
retain the specification independently of inference context. Updates use optimistic
revision checks and replace only submitted top-level fields (including whole arrays).
Each reply can create four drafts, each allowing two repair updates.

The specialized `image_gen` accepts only a draft ID, current revision, aspect
ratio and optional reference attachments. It reloads and revalidates that exact
revision before compilation or image execution. Missing, invalid, foreign-reply,
and stale drafts cannot start a job. Generic image requests retain their prompt
contract. The rendered draft is streamed/persisted for the user but is not inserted
again into the model continuation. Tool responses return compact status and issues,
not a duplicate plan. References are loaded selectively by the updated skill.

Structural validity does not verify factual claims, the metaphor, language, or
rendered pixels. Draft responses explicitly mark these review items unverified;
generation results separately report file/dimension checks and unreviewed visual
criteria. The language check establishes Latin script only. No self-written
semantic-check field acts as a factual certificate. The live probe
`scripts/check_illustration_model.py` tests real local-model draft authoring in
isolated storage with image execution disabled.

## Verification

Follow [checkout setup](../CONTRIBUTING.md#set-up-a-checkout), including both
runtime builds, then run:

```sh
.venv/bin/python scripts/check_health.py
.venv/bin/python scripts/check_health.py --gui
```

The first runs the offline unittest suite, Ruff correctness checks, targeted mypy
checks, Node syntax checks and dependency lock validation. It uses temporary
databases, deterministic embedders, controlled network backends and scripted model
streams. Node also runs the actual animation interpreter through the tests.

The optional graphical run requires GTK/WebKit and a display. Its hidden-window
checks use temporary XDG directories, fake providers and mock image transports;
they do not use the user's chat database or send paid requests. Live model and
GPU generation checks remain separate operational scripts.

CI runs the offline checks on Python 3.12 and 3.13 without installing GTK bindings.
Ruff deliberately starts with correctness/unused-import rules; typing focuses on
stored records, prompt admission, effect state and turn orchestration. Grow these
checks around meaningful contracts rather than applying a formatting rewrite.

## Conversation workspaces

Workspace grants (canonical path plus device/inode), structured message references,
and artifact output paths extend the existing SQLite records in migration 2.
`ChatController` owns workspace commands, reference admission, output persistence,
and session-scoped edit proposals. `ToolExecutor` dispatches file operations to
that controller through `ToolHost.file_tool` and emits artifacts through the
existing attachment callback.
`TurnRunner` retains successive calls, cancellation, and durable terminal state;
there is no additional agent loop or mode. Document writes settle disk and attachment
persistence before cancellation propagates. `PromptBuilder` serializes reference
pointers and artifact metadata without ingesting local file bodies.

Explicit skill references call the existing `SkillManager.read` before admission.
Only selected instructions are loaded, into a delimited user-selected block on the
request message rather than the system message; automatic metadata discovery and later
`read_skill` resource reads remain intact. The host image contract still applies
for explicitly selected Xiaohei instructions. File grants never come from skill
instructions or model tool arguments.

The native directory picker is the authorization gesture. Filesystem operations
walk directories with descriptors and no-follow opens; root identity, traversal,
regular-file checks, overwrite review, backups, and bounded reads live in Python.
Only the output folder `Downloads/Blackrose`, the default launch workspace,
accepts unreviewed file creation; without an active workspace only conversation
outputs may be read. The page owns autocomplete presentation and safe
preview surfaces; the backend revalidates selected paths and skill availability.
The existing session snapshot includes draft references, compact tool history,
and pending edit tokens. Durable composer drafts (`drafts.py`) save references
as pointers only; Session revalidates them, and attachment copies, when a
conversation's draft is restored. [Workspace documentation](workspaces.md) records user
behavior, permission details, limits, and deferred integrations.

Optional plan-first Motion Canvas authoring validates bounded plan data and emits
the numeric timeline in `motion_plan.py`; `ToolExecutor` keeps the accepted plan
within the active reply and falls back to direct authoring on failure. Both paths
converge on the existing compiler, isolated playback probe and artifact store.
Model-authored visual TSX remains untrusted and has no additional capabilities.
See [Motion Canvas](motion-canvas.md#experimental-plan-first-authoring) for the
contract, conformance limits and configuration.
