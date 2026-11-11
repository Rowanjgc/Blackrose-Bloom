# Model-authored Motion Canvas

The selected model authors standard Motion Canvas TSX, guided by the
explanatory-animation skill. Cloud models receive discovery, creation, source
reads for revision, and automatic repairs. Local models carry no standing
animation tools, activation capability, or instructions; they receive them only
when the user selects the skill for that reply. Automatic playback-failure repair
stays cloud-only. Saved playback and
offline HTML export work with either provider. No request silently changes
providers.

This repository includes no composition packs or expressive-motion authoring DSL.

## Setup

Use Node.js 22+ and the pinned dependencies:

```sh
npm ci --prefix animation-runtime
npm run build --prefix animation-runtime
./run
```

Import and approve `skills/explanatory-animations` through the app, or run
`blackrose install-skill explanatory-animations` for the bundled copy. With a local
model, select the skill from the `$` menu or type `$explanatory-animations` in the
message to enable authoring for that reply.
If an older copy is installed, reimport it to update its instructions. The skill
loads progressively; its procedural gear example is optional. Installed wheels
include the skill and compiler resources; run `blackrose setup-animations` to
install the pinned Node dependencies in the user cache. See
[distribution](distribution.md) for release builds and installed-resource checks.

## Authoring and playback

`animation_create` accepts a complete `scene` with `title`, `description`, expected
`duration`, TSX `source`, optional time/text `cues`, and a top-level `loop` boolean.
The host supplies version 2 and the Motion Canvas engine. Scenes export default
`makeScene2D(function* (view) {...})`. Imports are limited to `@motion-canvas/2d`
and `@motion-canvas/core`. The canvas is 960×540 with a centered origin.

Use library nodes, paths, groups, signals, and finite generator timelines. The
player handles looping. Compilation uses pinned TypeScript diagnostics and
esbuild without evaluating generated source or project configuration. A disposable
WebKit process renders samples with a 25-second hard deadline, measures actual
duration, and checks caption timing. Captured samples and type checks cannot
certify physical correctness, instructional clarity, or every intermediate frame.

Type diagnostics also reject any generator-returning call in statement position,
such as `ring().opacity(1, 0.2);`, `all(...)`, or `waitFor(...)`. Motion Canvas
only schedules those generators through `yield*` or a yielded `all(...)`; a bare
call would silently do nothing. `spawn(...)` is intentionally auto-starting and
remains allowed. After playback, a deterministic motion check compares coarse
per-cell frame fingerprints outside the bottom caption band. It reports `moving`,
`limited`, `static`, `blank`, or `unknown`; limited, static, and blank results are
published as warnings rather than blocking the save. The check does not identify
which object moved, judge the mechanism, or inspect the top or middle text bands.

Saving uses measured duration. Each revision reads the existing source and submits
a complete replacement with its conversation-scoped `revises_attachment_id`.
Previous versions remain available. Legacy v1 JSON scenes retain compatibility
without advertising their schema to new authoring requests.

Playback starts paused and supports play/pause, replay, seeking, captions,
reduced-motion preferences, and offline HTML export. Generated code executes in
an opaque iframe with no native desktop bridge, network, or external assets.
Compiled scenes have size limits; compilation has a 20-second timeout and a Node
heap limit. The preflight worker is supervised; the inline player is browser
isolation, not a general hard CPU/memory sandbox for arbitrary JavaScript.

A runtime failure can request an automatic source repair only with a cloud model
active. The repair waits for the current reply, rechecks the provider, preserves the
composer draft, and exposes only read/create tools for the failed attachment.
The durable ledger limits each chain to two automatic attempts. Stop cancels
repairs. Replay/reopen does not reset the budget. Local playback never causes an
automatic cloud switch.

## Verification

The default unittest command does not open playback windows. Native rendering
tests require `BLACKROSE_TEST_WEBKIT=1`; use Xvfb to isolate them from the desktop.
The explicit GUI scripts below open windows. All use scripted providers and
scratch storage without paid requests:

```sh
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python scripts/check_gui_motion_canvas.py
.venv/bin/python scripts/check_gui_motion_canvas.py --recovery
```

The desktop check covers inline controls, seeking, captions, resolution, reopen,
revision, offline export, and frame isolation. Use `--demo` for a persistent preview.

The optional live checker requires an explicit `--model opencode-go/<model>` and
configured credentials. It sends cloud requests and is not part of offline
verification. No live cloud quality evaluation has been run for this change.

## Optional visual review

Set `motion_visual_review = true` in the startup config and restart to review
new Motion Canvas animations before saving. Review runs only when the selected
model's catalog explicitly advertises image input. It uses the selected provider
and may incur that provider's normal inference charges; it never switches models.
`motion_visual_repair = true` additionally permits one tool-free source repair
per animation after a high-confidence, actionable failure. Both settings default
to false. Playback-failure recovery remains independent.

The existing supervised, opaque-origin renderer captures at most eight ordered
frames. Planned transition boundaries are preferred, with remaining samples
spread across the measured duration. A bounded timestamped JPEG contact sheet,
the original request and the available plan go to the model without tools. The
review contains an assessment and bounded, timestamped findings, including their
category, object, explanation, suggested correction and confidence. Invalid,
or timed-out reviews do not prevent saving the technically valid original.
Models without advertised image support skip review. A non-passing or unavailable
review is reported in the reply. Sampled
frames cannot prove continuous motion or physical correctness; a pass is an
advisory model judgment, not certification.

Repairs use the shared tool-free source-repair stream. The candidate must compile,
pass supervised playback validation and receive a passing second visual review;
otherwise the original is saved with an uncertain assessment and the findings.
The host never recursively repairs the candidate. The second review receives the
original findings, but a model can still miss an unchanged defect. When a planned
scene is repaired, its measured duration must remain within the existing plan
tolerance; the repaired source is no longer guaranteed to retain host-generated
timeline scaffolding.

Tool results include capture, image preparation, inference, revalidation and total
creation timings when available. Capture is an additional supervised render pass;
repair adds another validation and capture pass. Image preparation on a synthetic
three-frame solid-color fixture averaged 0.018 seconds over ten runs on the
development machine; this is not a model or renderer benchmark. Actual render,
inference and end-to-end latency depend on the animation and selected model.

The animation-enabled reply log records whether creation was attempted and how
many tool rounds were used when a reply ends without an artifact. This distinguishes
no animation call from an attempted tool failure, but does not establish why a
particular model omitted the call. No representative Qwen animation failure trace
was found in the checked repository artifacts; the cause remains unconfirmed.

## Experimental plan-first authoring

Set `motion_plan_first = true` in the Blackrose startup config and restart to
try plan-first authoring. It is off by default. The selected conversation model
calls `animation_plan` and then `animation_create_planned`; no other model or
service is loaded. Revisions and playback repairs continue to use direct source.
For unsupported motion, invalid plans, or planned-source failures, the model is
instructed to make one direct-generation fallback through `animation_create`.
The normal tool-round limit still applies. Tool results identify the strategy;
application logs record the exact validation error, fallback and elapsed time, and
the executor retains the rejected plan in memory for debugging. The plan is not
persisted or added to the model's context beyond the normal error text.

The version 1 plan is a JSON object with `version`, `objective`, `scene`,
`constraints`, `elements`, `timeline`, and `final_hold`. Each element has a
unique `id`, `description`, optional `label` and `relationship`, `initial`
(with numeric x and y and optional opacity, rotation, scale), and `final`
(the expected numeric properties to check). Coordinates are relative to the
node's parent. A timeline entry is exactly one of `wait` (seconds), `transition`
(a target ID, property, numeric value and duration), or `parallel` (at least two
such transitions). Entries are sequential; parallel duration is the longest
member, followed by the explicit final hold. Supported properties are x, y,
opacity, rotation and uniform scale. The host checks final values against the
initial values and ordered transitions, rejects conflicting concurrent writes,
unknown targets and durations outside 0.5–120 seconds, then generates the
Motion Canvas generator and applies initial properties. Version `"1"` is
canonicalized to integer `1`; ambiguous or unsupported fields are rejected.

The model supplies a TSX module exporting `build(view)`, which adds nodes and
returns an ID-to-node map. The host appends the default scene and timeline. The
module remains untrusted code under the existing pinned compiler and isolated
playback boundaries. Missing nodes cause playback validation to fail. The host
also exposes the planned node map to the trusted probe, which records rendered
opacity, local position, absolute position, size, rotation and scale at each
sample. It warns when a planned element that should appear never becomes visible,
is outside the frame, or never changes a property the plan moves. This is a
presence-and-change check, not a geometry, occlusion or legibility verdict; it
does not prove that labels are visible, nodes match their descriptions, relative
coordinates yield the intended layout, or the explanation is correct. Model-authored
code can also change nodes outside the planned schedule; this experiment does not
enforce visual-code purity. Use direct generation for complex paths, linked
signals, nonnumeric properties, easing, or other unsupported motion. The existing
pre-save checks and cloud-only post-save playback repair remain in place; repair
produces a direct source revision and does not retain a structured plan.
