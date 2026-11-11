# User guide

The full reference for Blackrose Bloom's behavior, setup and configuration. For
an overview and quick start, see the [project README](../README.md).

## Models

The application starts with the model named by `default_model` (the shipped
default is the `qwen36-35b-a3b` preset) on the llama-router at `endpoint`
(`http://127.0.0.1:18080` by default). It reuses the loaded model or explicitly
loads its preset. The model picker's **Provider** dropdown switches between
local answering models and **OpenCode Go**. Go's models appear in a second
dropdown, with one model's details at a time. Browsing providers does not change
the active model. For either provider,
**Use model** selects one for this session, while **Make default** also saves
it for future launches. With `router_autostart = true`, closing Blackrose stops
the local router and its loaded models, including an adopted router. For a shared
router managed elsewhere (for example by systemd), set `router_autostart = false`: closing
Blackrose disconnects without stopping the router or unloading shared models.

There is no classifier, automatic model switching, escalation ladder, routing
mode, benchmark dashboard, or routing telemetry.

Before every model request, including continuations after tool calls, the harness
adds a fresh clock snapshot to the system instructions: local date/time, UTC
offset, IANA time zone and UTC date/time. It reads the system clock and the
system time zone (`TZ`, otherwise `/etc/localtime`), so daylight-saving transitions
are handled automatically. Clock context is regenerated
throughout long conversations and is not stored as a chat message.

## Interface

The Reading Room interface uses Libron throughout, a framed reading column, and
quiet toolbar controls. **Conversations** in the top toolbar shows or hides the
resizable conversation list; **Ctrl \** does the same. Its width and desktop
visibility are remembered. On narrow windows the list opens as a drawer beneath
the toolbar. The list starts with the 200 most recent chats; **Load older conversations**
below it adds the next 200 and states how many remain. Live context usage and tokens/sec remain beneath the composer. **Retry** beside
Send, or **Ctrl+R**, resends the latest prompt as a new turn using its original
attachments and references. Previous replies and your current draft are kept.
You can open and scroll another saved chat while a reply continues in its
original chat. Return to see its live progress or saved result. One reply runs
at a time; **Stop** and **Ctrl+C** stop that reply from either chat.
**Ctrl+M** opens the model picker. Loading a model refreshes the picker only
while it is open. **Shift+Tab** in the message box (or the **Reasoning** button
beside Citation rigor) cycles reasoning effort: Default → Off → Low → Medium →
High → XHigh. Default sends nothing, so the model's preset decides. Each model
remembers its own level, and a change applies from the next prompt without
reloading the model. Models that don't expose the setting show the button
disabled. Elsewhere, Shift+Tab still moves focus backwards. Reply phases and terminal outcomes appear separately from
model availability; failures remain visible after reopening a saved chat.
Reasoning is labeled neutrally, tool diagnostics stay in their disclosure, and
generation speed clears when the request ends.

Completed `mermaid` code fences render as diagrams in chat and saved replies,
with **Expand**, **Source**, and **Copy source** controls. Models receive a short
hint to use diagrams selectively for useful flows and relationships. Rendering
is local and lazy. Successful diagrams add no model calls; syntax failures get
one bounded, source-only repair per answer. See [diagrams](diagrams.md)
for supported behavior, limits and development setup.

New conversations name themselves after the first completed reply. A small
background request uses the selected model and a bounded excerpt of the exchange
to produce a short topic title. The answer finishes independently; the sidebar
and window title update when the name arrives. New messages and model changes
cancel naming so interactive work takes priority. Naming has a 15-second deadline
and falls back to a local prompt excerpt if unavailable. `/rename <title>` always
takes precedence. With OpenCode Go, naming is one additional request to the same
provider; local chats stay local.

Conversation deletion also cleans private drafts and animation recovery state,
retains shared attachments, and retries interrupted cleanup at startup.
`blackrose artifacts` reports storage discrepancies with Bloom closed;
`blackrose artifacts repair` explicitly repairs conservative orphan candidates.
See the [cleanup and backup instructions](artifact-cleanup.md) first.

## Launch

```sh
blackrose
```

From a source checkout, use `uv run --locked blackrose`. `blackrose-bloom` is an
equivalent entry point.

`blackrose` opens its private output folder, `Downloads/Blackrose`, as the
workspace: new files land there without review, and the model can read nothing
else until you allow it. Use `blackrose .` for your current
directory or `blackrose ~/Dev/` for a specific directory. New chats inherit that
launch directory. Mention another directory in chat (for example, “look in
`~/Documents`”) to approve access without changing the workspace. Quote paths
containing spaces. Additional access lasts for that conversation until the app
closes or its workspace changes; replacing existing files still needs approval.

Add Blackrose Bloom and its Dragon Rose icon to your application launcher with
`.venv/bin/python scripts/install_desktop.py`. This installs a per-user desktop
entry pointing to this checkout; rerun it if you move the project.

For a fresh installation, use Python 3.12 or 3.13 and `uv`:

```sh
uv sync --locked --python 3.13
uv run --locked blackrose doctor
uv run --locked blackrose
```

GTK WebKit is required on Linux. On Fedora, the system dependencies are
`webkit2gtk4.1`, `gobject-introspection-devel`, and `cairo-gobject-devel`.
The app reuses llama-router at the configured endpoint, or starts it automatically
when it is absent. It runs `llama_server_path` (default `~/.local/bin/llama-server`)
with the presets file named by `router_preset` (default
`~/.config/llama-router/models.ini`), one model slot, and explicit model
loading. The presets continue to supply all tuning. Startup waits for the router
and selected model to be ready. Closing the window, `/quit`, Ctrl+Q or a normal
termination signal stops the identified local router and its model children.
Router output is saved to `~/.local/state/blackrose-bloom/llama-router.log`.

One desktop instance owns each chat database. A second launch exits with a
message to close the existing window; this prevents crash recovery or router
shutdown from interrupting another live window. The lock is released after
shutdown or a process exit. Retriever CLI/MCP access remains independent.

A router the app launches requires a random API key generated for that launch.
The key is kept in a `0600` file beside the router log, never on the command line,
and removed once the router stops; a router that survives a crash keeps it so the
next launch can adopt it. Launched routers also disable the `/slots` monitor and
answer browsers with an origin no web page has. Adopting a router that answers
without a key, or listens on all interfaces, shows a warning.

`llama_server_path`, `router_preset`, `router_start_timeout`, and
`router_stop_timeout` are configurable in the startup TOML. Set
`router_autostart = false` to connect to an externally managed service without
starting or stopping it. Remote endpoints are externally managed. For a router you
manage (or share with another client), start it with `--api-key-file` and set
`router_api_key_file` to the same `0600` file; Blackrose sends its first key and
never deletes that file. `doctor` checks
model availability, library reads and the GUI environment
without generating an answer or sending a public search query.

For a shared router, use a persistent key, loading on demand, and preset idle
sleep to release model/KV memory independently of client window lifetimes.
Blackrose accepts sleeping models and lets inference wake them. If another client
is using the only model slot, a transient management-load refusal is retried
within the request timeout; Stop cancels the wait. See the
[shared-router guide](shared-router.md) for ownership, configuration and
the measured concurrency behavior.

## OpenCode Go

Store your Go API key in the same Linux desktop keyring used for OpenRouter:

```sh
blackrose opencode-go-key set
blackrose opencode-go-key status
blackrose opencode-go-key delete
```

Key entry is hidden and requires an interactive terminal. The app uses
`secret-tool` (libsecret/Secret Service), with `service=blackrose-bloom` and
`account=opencode-go-api-key`, separate from the OpenRouter credential. Keys
are never saved in preferences, configuration, or chats. Unlock the desktop
keyring before using Go.

Open **Models → Provider → OpenCode Go**, choose a model in its dropdown, then
click **Use model** or **Make default**. Go defaults survive restarts and work
without starting the local router. Local and Go catalogs are fetched separately;
a failure in one provider leaves the other available in the picker.

The catalog comes from `https://opencode.ai/inference/go/v1/models`.
Chat Completions, Messages, Responses, and Gemini streaming requests use the
[Go API endpoints](https://opencode.ai/v2/docs/console/go/#endpoints), an app
user agent, and a stable conversation session ID. Tool continuations retain
reasoning/signatures needed by their API format. Requests are not automatically
retried. Go model IDs saved in preferences are prefixed with `opencode-go/`;
local preset IDs keep their existing format.

Selecting Go sends the conversation, enabled tool definitions, and tool results
to OpenCode Go under your subscription. Image generation continues to use the
image provider shown in the model details; the Go key is only for chat models.

## Knowledge and web access

Retriever is included as native Python code under `src/local_knowledge`.
The app calls its retrieval and ingestion functions directly; it does not
spawn Retriever CLI or MCP processes. It preserves the CPU FP32 EmbeddingGemma 2
configuration, hybrid FTS5/vector retrieval, reciprocal rank fusion, original
passages, provenance metadata, and resolvable document/chunk citations.

By default it uses the Retriever configuration at
`~/.config/local-knowledge/config.toml` (`knowledge_config`) and that
configuration's index and embedding cache.
The embedding model loads lazily and stays available during the session.

- **Library** browses sources, searches candidate passages, opens full indexed
  passages, imports selected files, and removes indexed sources after confirmation.
  Removing a source keeps the original file. Library imports affect the shared
  Retriever index.
- **Local knowledge** controls whether the model can call `knowledge_search`,
  `knowledge_open`, and `knowledge_list`. The Library remains available for manual use.
- **Web search** is optional and initially enabled. The model chooses when to
  use `web_search` and `fetch_content`; ordinary conversation needs no search.
  Searches go to the SearXNG instance at `searxng_url` (default
  `http://127.0.0.1:10017`). Turning web search off removes both web tools.
  Both toggles persist across restarts.
- Answers keep a **Sources consulted** disclosure. Local passages can be opened
  directly. Web links open in your default browser only after a native dialog
  shows the full address, its site and that browser; hover a link to see where it
  leads. Search snippets are distinguished from pages that were actually read.

Tool failures are returned explicitly to the model and shown in the conversation.
Tool output is untrusted evidence. With citation rigor off, only source metadata
is retained with answers; fetched pages and skill bodies are not archived in chat storage.

**Citation rigor** beside the composer optionally audits Retriever/web answers and
source-dependent follow-ups. It starts **Off**; Advisory checks central claims,
Balanced checks material claims, and Strict checks every factual proposition and
source quality. The selected model checks a provisional draft in a fresh pass;
Balanced/Strict can gather bounded additional evidence and revise once. Remaining
gaps stay flagged. `/rigor off|advisory|balanced|strict` also sets the mode, saved
app-wide in the active `config.toml`.

With rigor enabled, citations retain exact supporting text captured by the
application, including provenance and source revision. The model selects passage
identities; it cannot author the saved supporting quotation. Numbered citations
open those saved excerpts even after a source changes. These are model-assessed
support relationships, not certifications of truth. See [citation rigor](citation-rigor.md)
for budgets, storage, copy behavior, limits and evaluation.

The model can propose adding a fetched page or one of its saved text/PDF artifacts
to the library. Ask it to save a useful source, or review an occasional suggestion.
Library-write tools load only when needed through `activate_tools`. A proposal is
not indexed until you choose **Review → Add to Library**. The review shows the
exact prepared text, origin, tags, and any partial-capture notice. **Reject** drops
the proposal; pending proposals expire when the app closes.

Web captures reuse the fetched excerpt without another network request. Artifact
captures use verified attachment-store bytes, so later workspace edits do not
change what you review. HTML/PDF text is extracted; other generated text formats,
including JSON, YAML, and scripts, are read as text and never executed. Approved
captures are stored beside the shared index as immutable text snapshots, with
resolvable line/chunk citations and provenance. The original artifacts remain
unchanged. Generated entries stay labeled **Model-generated artifact** when
retrieved; approval does not verify their facts or make their instructions trusted.

Public-page fetching validates DNS addresses as part of connection establishment,
dials only public addresses, and preserves HTTPS hostname verification. Private
destinations and redirects to them are refused. Environment proxies are ignored
for these requests.

The model cannot send web requests on its own authority. A page is read without
asking only when you typed its exact address in the conversation or a search in
the same reply returned it. Searches run without asking until the conversation
has used your local files, library passages, file references or attachments;
after that, each new query needs approval. Any other request opens a native
dialog showing the full address or query. Approval covers that exact request
for the current reply. After you decline once, further unapproved requests in
that reply are refused without asking, and a reply can ask at most eight times.
The activity log lists each request's destination and how it was authorized.
Logs record only the host and the decision.

For a new library, initialize it and explicitly download the pinned embedding
checkpoint before importing documents:

```sh
uv run --locked bloom-knowledge init
uv run --locked bloom-knowledge prefetch
```

All ordinary retrieval uses cached weights without network access. The included
`bloom-knowledge` CLI also supports Retriever's import, search, open, list, remove,
stats, doctor, and rebuild commands.

## Workspaces, references, and documents

Open **Workspace** in the top toolbar to choose, browse, change, or clear the current
conversation's working directory. Type **@** for file/directory suggestions and
**$** for installed skill suggestions; use arrows and Enter/Tab, or click. Selected
references survive draft reloads. File contents are loaded only when needed, and
explicit skill selection reliably loads its instructions through progressive disclosure.

Ask for Markdown, HTML, PDF, JSON, or a script in ordinary chat. New files save
immediately only in the app-owned output folder, `Downloads/Blackrose`, with
numbered names on collision. A new file anywhere else, including the workspace,
waits on a review card showing its destination and exact text, and nothing is
written until you approve it. Artifact cards provide previews, open, and reveal
actions. HTML previews are sandboxed; PDFs are rendered as real documents.
Filesystem tools operate directly without RAG indexing. Existing-file edits
require review/approval; the previous version moves to a private backup store
outside the project, restorable from **Workspace → Edit backups**. Scripts are created without
executing them.

See [workspace behavior, permissions, tools, and limits](workspaces.md).
No additional configuration is required; PDF creation adds the in-process ReportLab
dependency. Workspace grants and references use the existing SQLite persistence.

## Skills

Open **Skills → Import skill…** and select a folder containing `SKILL.md`.
The app copies the package into private staging and shows a review: the skill ID,
name, full description, the complete `SKILL.md` text, every other file with its
size, and the source folder. **Approve and install** copies it into the app's
managed library, enabled. **Cancel** discards the copy; nothing is installed.
The approval covers exactly that `SKILL.md`, which includes the description. If
the file later changes on disk, the skill is unavailable to the assistant (no
catalog entry, `$` suggestion or `read_skill` access) until you choose **Review…**
and approve the new text. Skills installed before reviews existed need one review.
`blackrose install-skill <id>` installs a bundled skill already approved, because
bundled skills are reviewed as part of the application source.
This approves instructions only. Optional [skill scripts](skill-scripts.md)
require a separate package review and native execution permission in Skills.

Five instruction-only starter skills ship with the application. None is installed
or activated automatically:

| Skill ID | Use it to |
| --- | --- |
| [`thinking-partner`](../skills/thinking-partner/SKILL.md) | Pressure-test an idea, proposal or decision; surface assumptions and design small experiments |
| [`learning-partner`](../skills/learning-partner/SKILL.md) | Understand and apply a subject, with direct explanations or optional hinted practice |
| [`research-synthesis`](../skills/research-synthesis/SKILL.md) | Investigate a question across supplied, library and web sources and report a traceable synthesis |
| [`writing-revision`](../skills/writing-revision/SKILL.md) | Turn notes into documents and edit at the requested depth while keeping the author's voice |
| [`creative-workshop`](../skills/creative-workshop/SKILL.md) | Explore, develop and critique fiction, worldbuilding, design and other creative work |

Each keeps its example prompts and review criteria in `references/examples.md`.
They use only the tools already available for a reply and change no permissions.
`scripts/check_starter_skills_model.py` is a manual live-model probe for reviewing
their replies.

A standard skill begins with optional YAML frontmatter:

```markdown
---
name: concise-reviews
description: Review a proposal with concrete, concise feedback.
---
Explain the strongest concern first. End with a practical next step.
```

Skills use progressive disclosure: automatic discovery initially receives only enabled skill
IDs, names, and descriptions. Selecting a `$` suggestion explicitly loads that
skill's instructions before inference; its resource bodies remain lazy. The assistant selects relevant skills and calls
`read_skill` to load their `SKILL.md`, then reads only the resources needed for the
task. Instruction reads include a resource index without loading resource bodies.
Directory paths (for example, `path="references"`, or `path="."` for the package
root) return up to 25 entries; pass a returned `next_offset` as `offset` to continue.
File paths return that file's text. Paths stay inside the enabled managed package.
Loaded text participates in context admission on the next model round and is not
archived or automatically carried into later replies.

Skill text never enters the system message, which holds only application policy.
The catalog (descriptions shortened to 200 characters) and any `$`-selected
instructions are appended to the current request as delimited blocks labelled as
third-party text that ranks below application policy. Enabling or selecting a
skill adds only the `read_skill` tool. It grants no file or web access and approves
nothing, and URLs in skill text still need your approval before a fetch. Only the
identity of the bundled `explanatory-animations` or `ian-xiaohei-illustrations`
skill loads its host contract; other skills' text cannot.

Enable and disable take effect on the next reply and persist across launches.
Remove deletes the managed copy after
confirmation; the original folder stays unchanged. Imports, toggles, and removals
wait until a reply has finished. Separately approved script packages can run only
through the explicit `run_skill_script` tool, under Linux namespace and syscall
confinement with no network, credentials, user files or child processes. Package
changes invalidate execution permission. The optional `numeric-summary` example
computes statistics and saves a plain-text report; see [setup, limits and review](skill-scripts.md).

## Experimental interactive answers

The OpenUI integration can present forms, filterable comparisons, steps, local
calculations and small charts. It is disabled by default; see
[setup, feature flags and validation](interactive-ui.md).

## Animated explanations

Ask **“Animate how a RAG pipeline works”** to generate a short playable diagram
directly in chat. Play/pause, replay, seek, and **Export HTML** are built in.
Animations persist with the conversation; follow up with **“Slow down retrieval
and show the selected documents”** to create a revised version from saved source.
The model authors standard Motion Canvas TSX through `animation_create` and
`animation_read`. Source compiles locally and
plays in a sandboxed frame; existing JSON timelines still play. Set up this source
checkout once with `npm ci --prefix animation-runtime` followed by
`npm run build --prefix animation-runtime` (Node.js 22+).
Wheel installs use `blackrose setup-animations`; see
[installed resources](distribution.md) for setup and bundled skill imports.
Playback failures trigger bounded automatic source repair; earlier versions and
the composer draft are preserved. New scenes receive type and supervised playback checks.
Local models carry no animation tools by default and author or revise only when
the animation skill is selected; saved playback and export remain available, and
automatic playback repair stays cloud-only.

The optional [`explanatory-animations` skill](../skills/explanatory-animations/SKILL.md)
adds brief design guidance and a procedural gear example. See [animation documentation](animations.md)
for the format, architecture, security limits, offline export, and validation commands.

## Image generation

With **Gemma 4 E4B IT** (`gemma-4-e4b-it`) selected, `image_gen` automatically
uses a local **Qwen Image 2.1** ComfyUI setup, reading its workflows from
`local_image_workflows` (default `~/.config/blackrose-bloom/image-workflows`). Generation and
edits run locally, without an API key or API charges. Choose Gemma in **Models**
and ask for an image; the same previews, saved attachments, and edit references
work with both image providers. The Models panel identifies each provider, and
chat shows startup, generation, and memory-management progress.

The app starts a `comfyui.service` systemd user unit on demand, using that
service's own environment and memory flags. It reads the text-to-image and edit
workflows without modifying them. Its private workflow copies bypass the old LM Studio
unload node. **All chat inference and model loading use llama-router.**

Gemma stays loaded when there is enough headroom: at least 9 GiB of actual free
GPU memory for generation, 10.5 GiB for single-reference edits, and 18 GiB of
available system memory. GPU availability includes Vulkan allocations, using
the kernel's counters.
When memory is tighter, or an edit uses multiple references, the app unloads
Gemma through llama-router and restores its same preset after generation. The
image service releases its models afterward; a service started by this app is
stopped, while an already running service stays available. Stop cancels only the
app's own image job and waits for cleanup before chat can continue.

Images use approximately one megapixel, with dimensions rounded for the model;
edits preserve the first reference's aspect ratio. The workflows retain their
existing 25-step sampler and model files. Local failures are shown in chat and
never automatically switch to paid generation.

`local_image_workflows` and `local_image_endpoint` customize the installed
workflows and local ComfyUI address. Set `local_image_keep_chat_loaded = false`
to always use exclusive GPU handoff. `local_image_enabled = false` explicitly
selects OpenRouter for Gemma too. Other chat models use OpenRouter by default.

For OpenRouter, store your API key in the Linux desktop keychain (Secret Service)
from a terminal:


```sh
blackrose openrouter-key set
blackrose openrouter-key status
```

The setup prompt hides input. The key goes to `secret-tool` over stdin, never a
command argument, config file, environment variable, chat message, or application
log. Install Fedora's `libsecret` package if `secret-tool` is missing, and unlock
the desktop keychain. There is no plaintext fallback. The keychain entry uses
`service=blackrose-bloom` and `account=openrouter-api-key`. Remove it with
`blackrose openrouter-key delete`.

Ask the assistant to generate an image, or import and invoke
`ian-xiaohei-illustrations` for article illustrations. With a chat model other
than Gemma selected, `image_gen` sends one full visual prompt per request to
OpenRouter's Image API. Its default model
is `bytedance-seed/seedream-5-0-pro`, with 16:9 output at the 1K base tier.
[OpenRouter lists](https://openrouter.ai/bytedance-seed/seedream-5-0-pro)
$0.045 per base-tier output image and $0.003 per reference image for edits;
the app explicitly selects 1K to avoid the $0.09 high-resolution tier.
Returned PNG, JPEG, and WebP files use their actual formats. The model can also
edit images using up to four selected image attachment IDs in the current chat.
The assistant receives those IDs and can inspect generated images when the
selected chat model supports vision. With a text-only model, it must not claim
visual QA. Preserve the requested handwritten labels and check both their
spelling and the absence of invented text visually.

The Blackrose-adapted Ian/Xiaohei package is in
[`skills/ian-xiaohei-illustrations`](../skills/ian-xiaohei-illustrations/SKILL.md).
Import and approve that folder through **Skills → Import skill…** on a new
installation, or run `blackrose install-skill ian-xiaohei-illustrations`.
It uses the app's actual tool arguments, conversation attachment IDs for edits,
automatic attachment storage, and visual QA only when image context is available.
It preserves the original style, character, and composition guidance.

Xiaohei also accepts standalone topics (for example, the water cycle); an
article is optional. Explicit Xiaohei requests activate the host's structured image
contract while skill instructions and references load on demand through `read_skill`.
Reading its `SKILL.md` also activates that contract when the assistant selects the
skill during a reply; browsing directories or reading a reference alone does not.
Before an image job starts, the tool
requires detailed art direction: literal relationships, metaphor mappings and
misreading prevention, object positions/sizes, character pose/contact points,
directed connections, exact English label targets/locations, omissions, and a
semantic check. The full plan appears before generation and every visual
decision is compiled into the provider prompt. Xiaohei keeps a fixed single
bean-shaped silhouette, eye placement, and limb proportions across prompts.
The plan should specify a sparse picture precisely, without adding visual
complexity. Broken object references and non-Latin labels are rejected before
generation; rendered relationships and lettering still need visual QA. Other
image styles keep the standard tool interface.

Images display in the assistant reply and remain available when reopening the
chat, including images generated before a later interruption or failure. Files
live in the app's content-addressed attachment store under
`~/.local/share/blackrose-bloom/attachments/`; the tool returns each absolute
saved path. **Show folder…** reveals saved images after confirming the folder and
file manager. Image and animation destinations
retain their existing attachment-store behavior; automatic copies into a skill's
requested workspace `assets/` folder remain a manual export.

Only the visual prompt and explicitly selected reference images are sent to
OpenRouter when that provider is selected; ordinary chat continues to use the
selected local model. OpenRouter generation is paid and requests are not
automatically retried. Set
`image_generation_enabled = false` to remove the tool, or change `image_model`
in the startup TOML to try another image model. No API key belongs in TOML.

## Configuration and storage

Copy `config.example.toml` to `~/.config/blackrose-bloom/config.toml` to change
endpoints or startup settings. `blackrose-bloom --config /path/config.toml` selects
another startup configuration. GUI preferences override startup values for the
default model, the two retrieval toggles and per-model reasoning effort.

| Location | Contents |
| --- | --- |
| `~/.config/blackrose-bloom/` | Startup TOML and saved preferences |
| `~/.local/share/blackrose-bloom/` | Chat database, attachment blobs, managed skills |
| `~/.local/state/blackrose-bloom/` | Application log and window layout |
| Existing `local-knowledge` locations | Shared Retriever index, config, cache, diagnostics |

See [backup and recovery](recovery.md) for a complete stopped-application
snapshot and isolated restore. Chat rows, attachments and the separately
configured Retriever index/captures must be preserved together.

The app's directories respect XDG overrides. Images and documents can still be attached to
chat; these are separate from importing a source into the shared Library.

The chat client sends no temperature, top-k, top-p, min-p, repetition, context,
GPU, cache, template, or output-token overrides, and no reasoning override unless
you choose a reasoning effort other than Default. Your router preset supplies all
other tuning.

Host-side stream safeguards are separate from model sampling and idle request
timeouts. Defaults allow 16 MiB of visible reply text, 8 MiB of reasoning, 64 MiB
of streamed response data across a reply, 1 MiB per SSE line, and 30 minutes of
total stream time (including tool rounds and silent UI repair streams). Change
`stream_reply_bytes`, `stream_reasoning_bytes`, `stream_response_bytes`,
`stream_sse_line_bytes`, or `stream_duration_seconds` in startup TOML if a trusted
local workload needs more room. Exceeding a limit stops generation and saves the
accepted visible answer as a partial result.

Reasoning effort is sent per request, never as a load option:

- **Local models.** A model qualifies when its loaded chat template reads
  `enable_thinking` (checked through `/props`). Off sends
  `chat_template_kwargs: {enable_thinking: false}`. Low to XHigh send
  `thinking_budget_tokens`, a ceiling of 1,024 / 4,096 / 16,384 / 32,768 tokens by
  default; set a `[reasoning_budgets]` table in the startup TOML to change them.
  Templates that report `supports_reasoning_effort` also receive `reasoning_effort`.
- **OpenCode Go.** GPT models use `reasoning.effort` (Off is `none`). Claude
  models use `output_config.effort` from Low to XHigh; current Claude models
  cannot turn thinking off. Other families keep Default until verified.

Title generation and the citation claim audit always use Default.

Operational help: [troubleshooting and bounded diagnostics](troubleshooting.md).
Maintainers: [dependency/CI review](dependency-maintenance.md) and the
[documentation index](README.md).

## Development checks

```sh
uv sync --locked
npm ci --prefix animation-runtime
npm run build --prefix animation-runtime
npm ci --prefix ui-runtime
npm run build --prefix ui-runtime
npm ci --prefix diagram-runtime
npm run build --prefix diagram-runtime
.venv/bin/python scripts/check_health.py
.venv/bin/python scripts/check_health.py --gui
```

The offline checks cover persistence failures, Retriever lifecycle invariants,
prompt admission, effect budgets, provider protocols, web destination isolation
and session concurrency. Graphical checks require GTK/WebKit and a display; they
use temporary storage and scripted providers. Neither command calls a live model
or sends paid image requests. See [architecture and health checks](architecture.md)
and the [stabilization record](stabilization.md).

See [CONTRIBUTING.md](../CONTRIBUTING.md) for repository layout, dependency updates,
generated-file policy, and the development workflow. The
[documentation index](README.md) links current guides and preserved reviews.
Supported Python versions are 3.12 and 3.13. Browser checks use the locked root
Node dependencies and run in CI; release archives and installed animation/skill
setup are described in [distribution](distribution.md).

## Source provenance

Blackrose Bloom began as a standalone fork of two earlier projects: BlackroseAI (including the Gothic Bloom GUI) at base commit
`c5c69c89254cde8db2f42a679932a98e53801580`, and Retriever at base commit
`1c3d85e40574e9f87d85151cbfc079c2e187fd5f`.

The initial fork omitted upstream experiments, benchmark datasets, tests, and
scratch output. This repository now maintains its own regression tests, development
checks, bundled skills and design previews. See the
[documentation index](README.md) for the current guides.

Project code and the shared Dragon Rose artwork use the [MIT license](../LICENSE).
See [licenses and provenance](../THIRD_PARTY_NOTICES.md) for the original skill
attribution, Libron font license, and bundled dependency notices.
