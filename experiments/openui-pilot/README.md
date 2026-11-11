# OpenUI Phase 0 pilot

Preparation branch: `evaluation/openui-pilot`. This experiment does not enable
display generation in Bloom.

## Question and scope

Can the explicitly selected, already resident `qwen36-35b-a3b` produce useful
informational displays from supplied evidence within acceptable turnaround time?
Compare two authoring representations with the same nine components and renderer:

- `openui`: the trimmed library's generated positional-language prompt.
- `json`: a named-property component tree, deterministically converted to the
  same component calls and validated by the same parser.

This comparison measures the whole prompt/representation package. Prompt lengths
differ; it does not isolate syntax alone or compare OpenUI with every possible
JSON display design. Neither arm uses constrained decoding.

The subset is Stack, Card, CardHeader, TextContent, Table, Col, BarChart,
LineChart and Series. Four scenarios use fixed synthetic evidence, including
source labels and exact chart/table values. No application tools, private
conversation data, actions, forms or live queries are involved.

## Prerequisites and offline checks

Use Node 22.19+ and a built OpenUI checkout at commit
`78844d2d065e8ed3ea335d5e089c1e21fbd502e8`. The source is
[thesysdev/openui](https://github.com/thesysdev/openui/tree/78844d2d065e8ed3ea335d5e089c1e21fbd502e8).
Installation and builds belong in that separate checkout; use its locked dependencies and
documented build commands. This experiment adds no Bloom runtime dependency.

Required outputs: `packages/lang-core/dist/index.mjs`,
`packages/react-ui/dist/genui-lib/index.mjs`, and, for the review builder,
`packages/browser-bundle/dist/openui-styles.css` plus that package's installed
esbuild/React/OpenUI dependencies. Tracked OpenUI source must be clean. Untracked
historical benchmark files are permitted. Actual built module, schema and prompt
hashes are recorded, since a source revision alone does not identify a build.

From the Bloom repository root:

```bash
export OPENUI_CHECKOUT="/path/to/openui"
node experiments/openui-pilot/run.mjs --checkout "$OPENUI_CHECKOUT"
node --test experiments/openui-pilot/pilot.test.mjs
```

Preflight imports local modules and verifies the Git revision. It sends no
router or generation requests and changes no model configuration. Tests exercise
the real parser with deterministic fixtures and a fake streaming transport.
The evaluation's original prompt was 28,774 characters; local preflight for this
subset produces 7,659 characters for OpenUI and 4,466 for the JSON arm. Character
counts are not tokenizer measurements or latency predictions.

## Original-result audit

The original check was `Boolean(root) && errors.length === 0`. A streaming parser
can satisfy that condition while dropping unresolved children or auto-closing an
unfinished program. The revised gate also rejects incomplete programs,
unresolved/orphaned names, unsupported or dynamic nodes, and empty displays.

```bash
node experiments/openui-pilot/audit.mjs \
  --checkout "$OPENUI_CHECKOUT" \
  --results "$OPENUI_CHECKOUT/benchmarks/viewer/repair-results.json" \
  --out /tmp/openui-baseline-audit.json
```

The output path must be new. The original baseline audit reported Qwen36
15/16, Qwen38 8/8 and Tiel 15/16 after graph/completeness checks. It does not
re-run inference or apply the new evidence fixtures to old tasks. New repair
feedback may recover the flagged programs.

## Smoke run and decision cohort

Select Qwen36 explicitly in the local router before running. The harness checks
`/v1/models` before each display and refuses to load or switch models. It accepts
only a loopback HTTP endpoint. Run while other model work is idle: sharing the
resident model does not establish contention-free operation.

```bash
# Eight displays, each with at most two repairs. Up to eight minutes at the cap.
node experiments/openui-pilot/run.mjs --checkout "$OPENUI_CHECKOUT" \
  --run --trials 1 --out artifacts/openui-pilot/smoke

# Only after inspecting smoke outputs: 200 displays, 100 per arm.
node experiments/openui-pilot/run.mjs --checkout "$OPENUI_CHECKOUT" \
  --run --trials 25 --out artifacts/openui-pilot/cohort-01
```

Each output directory must be new; runs are never merged or resumed. Defaults
are ten trials per scenario per arm, not the decision cohort size. At the old
15 s mean, 200 displays would take roughly 50 minutes; the 60 s/display cap makes
the upper bound roughly 200 minutes plus status checks and local overhead.

Requests contain only the model ID, messages and streaming/usage flags. Presets
own tuning: no sampling, context, token-limit, grammar, GPU or template overrides.
Repair is at most two turns with the original evidence retained. A 60 s deadline
spans inference and repairs. An incomplete stream, token-limit finish, transport
error or timeout is a failure; transport failures do not trigger fresh sampling.
Arm order alternates by trial to reduce a fixed warm-cache advantage. Cache
effects remain part of this practical comparison.

Output contains metadata and source hashes, both exact prompts, the full fixture
snapshot, per-trial JSON including every response/error/usage record, final `.oui`
programs where available, and an incrementally written `summary.json`. Latency
includes all repair calls and local validation; status checks before each trial
are outside it. Cold loading, UI rendering and user-visible chat scheduling are
not measured. `first_ok`/`final_ok` include structural and fixture-fidelity checks;
the summary also exposes raw structural counts. Missing usage is unknown, not
zero cost. All artifact output stays under ignored `artifacts/` or `/tmp`.

## Render and review during Phase 0

Build a standalone review artifact from a completed or interrupted run:

```bash
node experiments/openui-pilot/review.mjs --checkout "$OPENUI_CHECKOUT" \
  --results artifacts/openui-pilot/smoke
python3 -m http.server 10032 --bind 127.0.0.1 \
  --directory artifacts/openui-pilot/smoke/review
```

Open `http://127.0.0.1:10032` when ready. No command opens a desktop window. The
review builds the same trimmed library with OpenUI's React renderer, checks
generation/review module and schema hashes, and displays accepted programs at
480 px and 960 px. Network connections are disabled in the review page. Failed
programs remain visible as errors and raw attempts. A renderer exception is a
quality failure even when structural checks passed.

Record a row per trial in a separate `quality.json` or CSV alongside the output:
trial filename, readable, complete, faithful, useful (each yes/no), notes, reviewer
and review date. Inspect every accepted final output in the decision cohort at
both widths. A quality pass requires all four checks:

1. Readable: no clipping, overlapping labels, unusable axes or missing content
   from responsive layout.
2. Complete: the requested components and explanation are present, including
   metric cards for the informational scenario.
3. Faithful: values, units, source labels and relationships match supplied
   evidence, with no invented claims. The automated gate checks selected exact
   fields; it cannot prove the absence of invented extra text or data.
4. Useful: the display makes comparison or understanding easier than ordinary
   prose/table output. Record whether the additional latency was worthwhile.

Quality starts as `pending`; scripts never grant integration acceptance. Assess
both arms without treating parser success as a preference signal. Browser review
is not GTK/WebKit verification.

## Decision gate and next integration work

For the nominated arm require at least 95/100 final gate passes, at least 23/25
per scenario, median latency ≤20 s and p95 ≤45 s. Count timeouts and transport
errors in the denominator and latency summary. Report the maximum and both
structural and evidence-fidelity failures. These are observed pilot thresholds,
not a statistical guarantee about future requests.

At least 90% of accepted outputs must pass all four quality checks. Any material
numeric/citation error blocks integration until addressed and re-evaluated.
Choose the representation using latency, repair rate, fidelity, usefulness and
maintenance burden; an OpenUI win is not assumed.

Before an opt-in desktop pilot, resolve and verify:

- Selected-model/provider availability; no automatic switches or hidden fallback.
- Dedicated display-turn ownership, cancellation and queue behavior while chat
  is active, preserving ordinary prompts and requests.
- Evidence snapshot and citations carried through repairs; clear prose fallback
  after a failed display without discarding an existing answer.
- Actual GTK/WebKit rendering, native bridge isolation, and normal-chat checks.
- Whether to persist artifacts; if yes, version the library/schema and evidence,
  and claim repair attempts durably before inference.

No desktop integration, model inference or quality approval is implied by the
preparation checks. Grammar hardening remains a later experiment.
