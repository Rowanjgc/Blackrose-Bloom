# Scripts

Every file in this directory, grouped by what running it costs. Read a script's
module documentation and arguments before running anything outside the first
three groups. `tests/test_script_index.py` fails when a file is missing from this
index, when an entry names a file that does not exist, and when the native and
browser probe groups differ from `GUI_PROBES` in `check_health.py` or the
`test:browser` script in the root `package.json`.

## Offline health gate

Run by `check_health.py`; no display, network, model or user data.

| Script | Purpose |
| --- | --- |
| [`check_health.py`](check_health.py) | The gate itself; `--gui` and `--gui-only` add the native probes below |
| [`check_unit_tests.py`](check_unit_tests.py) | Offline unittests in scratch XDG directories; fails if a test writes outside them |
| [`check_recovery.py`](check_recovery.py) | Synthetic stopped-application backup and restore drill |
| [`check_docs.py`](check_docs.py) | Markdown links, anchors, repository paths and symbol references; no line-number references |
| [`check_docs_baseline.json`](check_docs_baseline.json) | Line-number references older documents may keep; may only shrink |

## Native probes in the health gate

Run by `check_health.py --gui` and in CI under Xvfb. Each opens hidden GTK/WebKit
windows over scratch storage with scripted providers and mocked transports.

| Script | Purpose |
| --- | --- |
| [`check_gui_images.py`](check_gui_images.py) | Attachment and paste rendering |
| [`check_gui_generation.py`](check_gui_generation.py) | Generated-image display against a mocked paid API |
| [`check_gui_animations.py`](check_gui_animations.py) | Legacy inline animation playback and offline export |
| [`check_gui_motion_canvas.py`](check_gui_motion_canvas.py) | Motion Canvas playback; `--recovery` exercises playback repair |
| [`check_gui_opencode.py`](check_gui_opencode.py) | Provider browsing and the compact Go model picker |
| [`check_gui_session.py`](check_gui_session.py) | Session reload and accessibility |
| [`check_gui_workspace.py`](check_gui_workspace.py) | Workspace, autocomplete, artifact and approval UI |
| [`check_gui_workspace_autocomplete.py`](check_gui_workspace_autocomplete.py) | Incomplete workspace autocomplete and its recovery |
| [`check_gui_library.py`](check_gui_library.py) | Library review cards and reload |
| [`check_gui_memory.py`](check_gui_memory.py) | The `/memory` review sheet against a scripted Honcho |
| [`check_gui_interactive_ui.py`](check_gui_interactive_ui.py) | Interactive-answer streaming, state, actions and replay |
| [`check_gui_skill_scripts.py`](check_gui_skill_scripts.py) | Skill-script review and native consent |
| [`check_gui_citations.py`](check_gui_citations.py) | Citation replay and source integrity |
| [`check_gui_diagrams.py`](check_gui_diagrams.py) | Mermaid rendering |

## Browser probes

`npm run test:browser` and CI's browser job: the shipped page in headless
Chromium with a scripted bridge. Needs the root `npm ci` and Playwright's Chromium.

| Script | Purpose |
| --- | --- |
| [`check_reading_room.cjs`](check_reading_room.cjs) | Reading Room page at real sizes |
| [`check_gui_models.cjs`](check_gui_models.cjs) | Model picker over an illustrative catalog |
| [`check_gui_toolbar_scale.cjs`](check_gui_toolbar_scale.cjs) | Toolbar reach at ordinary and 200% text |
| [`check_answer_layout.cjs`](check_answer_layout.cjs) | Readable answer content without hidden clipping |
| [`check_interactive_ui.cjs`](check_interactive_ui.cjs) | Interactive answers with the trusted runtime at desktop and mobile sizes |
| [`check_citations.cjs`](check_citations.cjs) | Citation controls, evidence, copy and reload |
| [`check_sources_popover.cjs`](check_sources_popover.cjs) | The composer's Sources panel |
| [`check_help_sheet.cjs`](check_help_sheet.cjs) | Keys and commands sheet |
| [`check_effort.cjs`](check_effort.cjs) | Reasoning-effort control |
| [`check_conversation_paging.cjs`](check_conversation_paging.cjs) | Sidebar paging, focus and selection |
| [`check_edit_review.cjs`](check_edit_review.cjs) | File-replacement review; also runs `python3` to build the comparison |
| [`check_history_paging.cjs`](check_history_paging.cjs) | Earlier-history pages and scroll anchoring |
| [`check_chat_navigation.cjs`](check_chat_navigation.cjs) | Live-chat replay while another chat is read |
| [`check_diagrams.cjs`](check_diagrams.cjs) | Markdown and full Mermaid rendering offline |

## Optional native observations

Scratch data and no network, but they need a private display or accessibility
bus and record observations rather than pass/fail gates.

| Script | Purpose |
| --- | --- |
| [`run_private_atspi.sh`](run_private_atspi.sh) | Runs a probe with a private Xvfb and AT-SPI bus |
| [`check_gui_accessibility_tasks.py`](check_gui_accessibility_tasks.py) | Task-based keyboard and accessibility journeys |
| [`check_gui_orca.py`](check_gui_orca.py) | Orca speech captured while an accessibility probe runs |
| [`check_gui_housekeeping.py`](check_gui_housekeeping.py) | Timing, keyboard and AT-SPI observations at synthetic scale |
| [`check_native_contrast_motion.py`](check_native_contrast_motion.py) | Rendered contrast samples and reduced motion |
| [`check_gui_web_process_recovery.py`](check_gui_web_process_recovery.py) | Kills the scratch window's WebKit renderer and checks host recovery |
| [`measure_gui_memory.py`](measure_gui_memory.py) | WebKit memory during repeated synthetic navigation |
| [`check_provider_keychain.py`](check_provider_keychain.py) | libsecret in a disposable private bus and keyring; needs gnome-keyring |

## Local service probes

Start an isolated CPU llama-router with a scratch model. No paid requests; they
need the installed router build.

| Script | Purpose |
| --- | --- |
| [`check_router_deployed_contract.py`](check_router_deployed_contract.py) | The installed router build's management contract |
| [`check_router_child_auth.py`](check_router_child_auth.py) | What a model child exposes on its loopback port |
| [`check_shared_router.py`](check_shared_router.py) | Shared-router sleep, credentials and contention ([guide](../docs/shared-router.md)) |

## Live model, GPU or paid probes

Contact a configured provider or use the GPU. Cloud runs may send paid requests.
Each keeps its chat storage isolated.

| Script | Purpose |
| --- | --- |
| [`accept_opencode_go.py`](accept_opencode_go.py) | Prepares, or explicitly runs, the OpenCode Go protocol acceptance matrix |
| [`check_animation_model.py`](check_animation_model.py) | Cloud-model animation authoring and revision |
| [`check_illustration_model.py`](check_illustration_model.py) | Local-model illustration draft authoring, image execution disabled |
| [`check_xiaohei_generation.py`](check_xiaohei_generation.py) | Topic-only planning and local Xiaohei image generation (GPU) |
| [`check_starter_skills_model.py`](check_starter_skills_model.py) | Qualitative starter-skill replies from a live model |
| [`evaluate_citations.py`](evaluate_citations.py) | Synthetic citation evaluation; offline integrity gate by default, `--live` uses a model ([guide](../docs/citation-rigor.md)) |
| [`grade_citation_answers.py`](grade_citation_answers.py) | Blind usefulness grading of saved citation answers via OpenCode Go |

## Offline measurements

Synthetic data on scratch storage; they report numbers, not pass/fail.

| Script | Purpose |
| --- | --- |
| [`measure_housekeeping.py`](measure_housekeeping.py) | Growth baselines at synthetic scale |
| [`measure_reply_checkpoints.py`](measure_reply_checkpoints.py) | Checkpoint traffic and contention ([recovery](../docs/recovery.md)) |
| [`measure_retriever_embedder.py`](measure_retriever_embedder.py) | The pinned Retriever embedder on temporary indexes |

## Tools

| Script | Purpose |
| --- | --- |
| [`check_distribution.py`](check_distribution.py) | Verifies release archives and a wheel outside the checkout; CI runs it after `uv build` ([distribution](../docs/distribution.md)) |
| [`install_desktop.py`](install_desktop.py) | Installs this checkout in the current user's application launcher |
| [`export_animation.py`](export_animation.py) | Validates an animation scene and exports a portable player |
