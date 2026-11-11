# Experimental interactive answers

Integrated at `7040328` after development on `experimental/openui-integration`.
The design accepts bounded semantic content, compiles it through a trusted
registry, keeps Python authoritative over state/actions, and provides readable
fallback. This guide and the [ownership guide](architecture.md) describe the
maintained implementation. This remains an opt-in experiment. A
configured-local-model evaluation completed 120 paired/repeated turns. Its 95% schema-valid rate did not establish
the proposed [useful-answer rollout threshold](#rollout-evidence).

## Enable in a source checkout

```sh
npm ci --prefix ui-runtime
npm run build --prefix ui-runtime
```

Set these independent flags in Blackrose's `config.toml`, then restart:

```toml
interactive_ui_enabled = true
interactive_ui_actions_enabled = true
interactive_ui_repairs = 1
```

All default to false except `interactive_ui_repairs` (1). The first allows model
activation; the second allows explicit follow-up buttons on saved views; the
third bounds silent in-place repair of invalid blocks (0 disables, maximum 2). Replay and local controls remain
available when generation/actions are disabled. Missing build artifacts leave
ordinary Markdown available; unavailable/unsupported views retain readable text.
Node is needed to build the trusted bundle, not to validate or replay semantic
answers on an installed desktop.

## Supported answers

The model can activate `interactive_ui` with a focused `comparison`, `form`,
`steps`, `calculation`, or `metrics` profile, or `mixed`. Only that reply receives
the expanded schema/example contract. All providers use the same text protocol;
there is no cloud-only gate for this feature.

- Comparisons filter and sort supplied rows locally; an optional labeled button
  sends the selected row as a new user message. If a filter hides the selection,
  the button is disabled with an explanation until the row is visible again or
  a visible row is selected. Saved filters and selections follow the same rule.
  Authoring instructions preserve per-row units and distinguish supplied inputs,
  actual supplied measurements and conditional qualitative expectations. A
  measurement's trial scope must come from the supplied evidence. Host schema
  validation does not verify these semantic distinctions.
- Forms accept text, numbers, choices and checkboxes. Required fields and numeric
  bounds are checked in Python before a labeled follow-up becomes a normal turn.
  The authoring contract leaves unknown preferences blank: a default is allowed
  only when the user supplies that value or asks for defaults. This is model
  guidance; schema validation alone cannot establish a default's provenance.
  Optional dropdowns offer “Not specified”; clearing removes the saved/submitted
  field, including after reopening. Required dropdowns still require a choice.
- Steps provide local Previous/Next controls.
- Calculations provide independent quantity inputs for every supplied item,
  including items initially at zero, and one combined total. Quantities are
  0–1,000,000 and may be fractional. The separate **Quantity multiplier**
  (0–1000, initially 1) scales every entered quantity; each subtotal is quantity
  × multiplier × supplied unit value. Edits stay local and survive reopening.
  Blank quantities or a blank multiplier leave the total unavailable until
  filled; blanks are saved separately from zero. Older scale-only snapshots
  retain their multiplier and start with the original supplied quantities.
  The host supplies the multiplier label even for older documents whose
  `scale_label` was misleading. Ordinary results display up to four decimal
  places, while nonzero magnitudes below 0.0001 use scientific notation with
  up to six significant digits. Metrics use the same numeric formatting.
- Metrics show a bounded dataset as bars and compute its sum, average or count.
  Bar lengths represent absolute values relative to the largest magnitude in the
  dataset, including datasets below one; signed values remain visible as text.
  Count includes zero-valued observations and labels the aggregate as items;
  each observation retains the document's original measurement unit. The
  authoring contract preserves source membership, signs and decimals and keeps
  derived totals out of the input dataset.
- Text blocks provide prose inside mixed answers.

Numbers and claims supplied by a model remain model-authored. Arithmetic and
layout do not verify evidence. Read-only retrieval adapters and evidence-ID
resolution belong to M7; there are no runtime queries, backend mutations, maps,
remote media, arbitrary formulas, generated JavaScript or raw OpenUI input.

## Streaming and authority

After activation, the next generation segment emits one complete JSON block per
line. Python validates each completed line and sends full accumulated snapshots.
Completed blocks can appear before the segment finishes; a single unfinished JSON
object is not painted speculatively. A final line need not end with a newline.
The segment receives no tools. Ordinary Markdown and code fences are never
interpreted as an activation or executable UI.

The common path uses named content documents, as recommended by the refinement.
Trusted code compiles them into six high-level components inside `Answer` and
renders them with `@openuidev/react-lang`/`lang-core` **1.0.1**, React **19.2.0**.
Each block owns a stable React root. Markdown updates cannot replace its parent;
source appends and finalization retain edited inputs. The shared registry generates
the Python JSON Schema, profile prompts, compiler fixtures and renderer schema.
Build checks parse every compiled fixture and package checks verify the bundle hash.
The optional raw OpenUI/Node validator path is deliberately not exposed.

Migration 3 adds ordered message blocks, independently sequenced saved state and
deduplicated action events. Finalized source, identity, schema hash and fallback
are immutable. The original message text remains available for diagnosis; typed
blocks own transcript rendering and bounded semantic history. Historical fields
are not automatically sent to the model. Visible follow-ups show field labels and
selected values. Bounded program context and typed submitted values are attached
only during model serialization, in the user channel. Refinement currently
creates a new answer rather than mutating a finalized document.

Python checks the current conversation, persisted assistant owner, revision,
installed contract/bundle integrity, schema identity, terminal status, current view token, static `submit` action,
field types/ranges, byte budgets and event ID. Session owns view tokens/drafts/scheduling; the controller-owned
`InteractiveAnswers` policy owns persisted validation and state/event writes
through a typed repository contract. See [ADR 0003](adr/0003-interactive-answer-ownership.md).
The accepted event is recorded
before the normal Session/TurnRunner path starts. Linking its visible user message
and marking it started happen in the turn-creation transaction. Busy replies
disable follow-up buttons in both layers; local inspection remains available.
Duplicate event IDs and repeat in-flight submissions fail. Crash recovery marks
unfinished documents/actions interrupted. Cancellation during acceptance settles
the commit and records interruption; reload during acceptance prevents a new turn. Reopening rotates view identities;
late state writes fail, and state sequences prevent stale overwrites.

Saved views with an unknown protocol, library or schema identity retain their
text fallback and source export. They cannot save edits or trigger follow-ups.
The renderer does not inspect unfamiliar document fields to initialize controls.
Missing or corrupt runtime resources also refuse state/actions while retaining
ordinary chat and saved fallback text. Rebuild `ui-runtime` from the locked
dependencies to restore a damaged installation; saved records are unchanged.
The [compatibility fixtures](../tests/fixtures/upgrades/README.md) and browser
probe cover these paths independently of model generation flags.

State saves are debounced 250 ms and flushed on blur, submit and ordinary page
navigation. Abrupt process termination can lose edits still inside that debounce.
Source is limited to 64 KiB per UI turn, 12 semantic blocks, depth 12 and 200 rows;
state is limited to 16 KiB. Unknown keys, non-finite numbers and malformed blocks
fail to readable fallback. An invalid block may be repaired silently in place:
Python re-asks the model with only the broken block, its validation error and the
matching schema, then validates the reply with the same gate. A successful repair
advances the block's `source_revision`, keeps its identity and position, replaces
the fallback with the corrected view and repaints it; it never becomes a visible
user turn. Repair is bounded per reply (`interactive_ui_repairs`, default one
block) and any remaining failure keeps the readable fallback. If the expanded
contract cannot fit context admission, the turn falls back to ordinary Markdown.

## Verification and remaining gates

```sh
npm test --prefix ui-runtime
.venv/bin/python -m unittest discover -s tests -p test_ui_documents.py -v
.venv/bin/python scripts/check_gui_interactive_ui.py
```

The native fixture probe uses an isolated database and a scripted provider. It
checks progressive completed-block rendering, stable input identity, local
arithmetic/steps, saved state after reopening, one visible continuation, replay
with generation disabled, and layout. It uses the real GTK/WebKit desktop and
writes `/tmp/blackrose-interactive-ui-desktop.png`. The browser probe
`node scripts/check_interactive_ui.cjs` additionally checks 1280- and 390-pixel
layouts, literal hostile text, filtering, state flush before submission, busy
guards, reduced motion and unmounting, and writes screenshots under
`artifacts/browser/interactive-ui/`.

`node scripts/check_answer_layout.cjs` checks metric labels, units and totals
against their cells and answer bounds across 320–1280px widths and 16/32px text.
It also exercises sidebar fresh load/resize at 760/768/800/1024px with
16/24/32px text, drawer controls, focus and saved layout preferences. Both
ordinary Markdown and interactive answers keep a readable column. The sidebar
uses a drawer when its minimum width would leave less than a 32rem main pane;
this responds to root text-size changes as well as window resizing. Metric
rows stack in narrow answer containers, and long labels/units and totals wrap.

The production bundle is about 613 kB uncompressed. One local deterministic
fixture run measured about 0.11 seconds to the first form; this includes the
scripted activation/bridge/render path and is **not model latency evidence**.
Coarse native-process memory measurements are recorded in the evaluation report.
Dedicated per-view memory profiling, cloud-model evaluation, broader provider
eligibility and the 95% useful-first-pass rollout gate remain open. Do not
enable this by default based on parser or fixture tests alone.

For explicit model evaluation, use the 30-task paired UI/Markdown harness in
`experiments/interactive-ui/evaluate.py`. It records exact prompts, provider metrics,
first accepted block and total time, documents/fallback and errors in scratch
storage. Run repeated trials per chosen local/cloud model, then manually assess
factual accuracy, content completeness, usability and rendered results; schema
validity is only one gate. Cloud runs use the selected provider and its normal
account/billing. No live inference is part of the deterministic checks.
Each turn has a configurable `--timeout` (120 seconds by default). Capture stores
structured snapshots of stream budgets while forwarding the original objects
to the provider. `schema_valid` requires an actual ready UI document; ordinary
Markdown controls are recorded separately as `markdown_control_valid`.
`summary.json` reports completed turns and errors, and any execution error gives
the process a nonzero exit status. A completed run still needs content review.

For targeted regressions, `experiments/interactive-ui/usefulness.py` declares the
task set, repetitions, task hash, reasoning effort and runtime artifact before
inference. `fidelity.py results.json` reads the sibling `run.json` (or
`--manifest`) and rejects incomplete, duplicated or mismatched task/repeat sets,
execution errors, structural failures and failed bounded numeric checks. Missing
manifests cannot establish a passing cohort. Numeric oracles score observation
units separately from aggregation and dataset membership; comparison prose and
usefulness still require explicit review. `usefulness-review.cjs results.json`
replays both widths and exits nonzero for runtime errors or incorrect golden
totals on the tasks with explicitly requested operations. These checks do not
make a rollout decision.

## Rollout evidence

The earlier external plan called this gate M6. Its required context is preserved
here: at least 95% of first answers in a fresh, declared cohort must be useful,
faithful to the supplied evidence and visibly complete, without relying on repair
to turn structural success into a quality score. Record the model/preset, task
set, repeats, repair policy, failures and human review at desktop/mobile widths.
Schema acceptance, content fidelity, controls/grouping, visual quality and latency
are separate results. Material numeric/citation errors block promotion. Native
scripted fixtures establish host behavior only; they do not substitute for model
quality evidence. Broader provider/cloud evaluation remains open.

A 2026-11-03 usefulness audit
tested 60 fresh local-model turns, 120 reply renders and 140 fixture layouts.
It found coherent basic grouping alongside material numeric display, preference,
dataset-fidelity and interaction issues, tracked in `bra-s4y.1`–`bra-s4y.13`.
Its structural passes do not establish the useful-answer gate. A follow-up added
blank-preference authoring, optional dropdown clearing and restored paired
evaluation, verified with a fresh repeated form cohort.

The authoring contract also separates observation units from count totals, keeps
zero/signed/decimal observations in the original dataset, excludes derived totals
from aggregate inputs, and distinguishes qualitative comparisons from measured
evidence. Comparisons preserve per-row units without unrequested conversions.
These are model instructions; schema acceptance cannot enforce request fidelity.
`experiments/interactive-ui/usefulness.py` declares the selected cohort and records
separate schema/content results. Run `fidelity.py RESULTS_JSON` from
`experiments/interactive-ui/` with Python for bounded metrics checks and comparison review
criteria. It exits nonzero for measured dataset failures or execution errors;
unscored comparisons, prose claims, usefulness and renderer usability require
separate review even when it exits zero. Replay with `usefulness-review.cjs` to
inspect the actual transcript at desktop/mobile widths.
Wide comparison and calculator tables scroll inside labeled keyboard-focusable
regions, checked for arrow keys, focus and table semantics at narrow and
enlarged-text sizes.

Reproduce a fresh cohort with the maintained harness; earlier evaluation outputs
are not part of this repository. Enabling by default
requires an explicit reviewed promotion decision after this evidence is collected.
