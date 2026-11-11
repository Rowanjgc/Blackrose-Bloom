# Citation rigor

Citation rigor is **off by default**. Select Off, Advisory, Balanced or Strict
beside the composer, or use `/rigor off|advisory|balanced|strict`. The choice is
saved app-wide as `citation_rigor` in the active startup `config.toml`, including
a file selected with `--config`. Other settings and comments are retained. A
failed save leaves the old selection active; changes wait until a reply ends.
Sending and retrying wait for an in-progress mode save to finish.

## What each level does

| Mode | Coverage | Work after drafting |
| --- | --- | --- |
| Off | Existing answer behavior | No audit requests or evidence snapshots |
| Advisory | Central factual claims | One assessment and focused review of contradiction verdicts; no research or rewrite |
| Balanced | All material factual claims | Assessment, up to two evidence calls, one revision and final assessment |
| Strict | Every externally checkable factual proposition | Assessment, up to six evidence calls, one revision and final assessment; also examine source quality and counterevidence |

An evidence call is one search or passage/page read. Additional research uses
only enabled Retriever/web tools. An audit web search may schedule a read of its
first candidate page, charged to the same budget; search snippets never establish
support. The entire post-draft audit shares the
configured request timeout; each model request passes context admission.
Exhausted budgets, missing context and malformed responses leave visible gaps.

Each assessment that labels a claim **contradicted** receives one focused logical
review in a fresh context using the same selected model. The review distinguishes
incompatible facts from missing, unmeasured, untested or stale evidence. Failure
to complete that check leaves the claim unchecked. This adds at most one model
request per assessment. When an assessment leaves answer units unclassified, one
bounded coverage follow-up re-asks only those units and merges their verdicts
back into the same report; a structurally invalid assessment counts as leaving
every unit unclassified, so the same follow-up gets one chance to recover it. A
failing follow-up never discards the partial
results. With that follow-up, advisory uses at most three; balanced/strict use at
most seven assessment/revision requests, plus their bounded evidence calls. All
work shares the existing request timeout. This is a consistency check by the
selected model, not independent certification.

These levels apply to Retriever/web answers and follow-ups using their evidence.
A fresh assessment distinguishes source-dependent follow-ups from unrelated chat.
Rigor does not force searching for every ordinary conversational answer.
The currently selected model performs the assessment in a separate context;
there is no automatic verifier/provider switch. Enabling rigor adds latency and,
with a paid provider, additional requests to that provider.

The draft streams under **Provisional · claims not yet checked**. Checking,
research and revision progress remain visible. Final text and its annotations
replace the draft together. Unsupported claims are retained and qualified;
contradicted claims can be corrected. The provisional claim assessments remain
inspectable after revision, so a rewrite cannot silently erase the record of an
unsupported statement. Interruptions retain available text under
**Audit incomplete**. A stopped/crashed request cannot reopen with a successful
audit label.

If a revision succeeds but its final assessment fails or is interrupted, the
useful revised text is retained with an incomplete audit. The interface, saved
message and copied answer use that same text; the original provisional claim
history remains inspectable. An initial malformed assessment can still use the
existing single revision allowance in balanced/strict mode.

## What a citation establishes

Numbered controls open the claim, its support assessment, and the original saved
passage. Supported claims use compact numbers; other verdicts and source caveats
remain labeled in the answer. Completed assessments sit below the answer, while
provisional and incomplete notices stay above it. **Review evidence** opens all
claims with text search and verdict filters; each claim also offers previous/next
navigation. **About this assessment** retains exclusions and provisional history.
Citation views keep the claim, assessment and saved excerpts in separate sections.
Full captured context and source details expand on demand. Source details show
labeled provenance fields, with exact metadata and snapshot hashes under
**Technical record**. The title and Close control remain visible while evidence
scrolls. The **?** beside citation rigor compares the four modes.

Gothic Bloom colors reinforce the written verdicts: green for supported, amber
for partial or missing support and source caveats, coral for contradiction, blue
for inference, and silver for unchecked claims. Blue also identifies saved source
text and ordinary source references, without implying that they support a claim.
The assessment color key explains every verdict; labels remain the primary cue.

Outside rigor, exact local citation references become compact **Source** controls
when they match retrieved material; code blocks remain unchanged. The full citation
is available in Source details. Sources consulted keep passage access and original
URLs, with project/version context above expandable provenance.

Assessments distinguish supported, partially supported, unsupported,
contradicted, inference and unchecked. Quality caveats and omitted coverage are
visible separately. Strict examines authority, primary evidence, dates,
version/applicability, independent corroboration where appropriate, and conflicts.
These are **model-assessed relationships to sources**, not certifications of truth.

Supporting passage text is never written by the model:

1. The application captures successful retrieved/extracted text after tool-output
   checks, before delivering the tool result to the model. Search snippets are
   discovery leads and cannot become supporting passages.
2. It assigns a source identity and exact span boundaries, and saves the text,
   SHA-256, locations, times, revision and provenance. Truncation remains explicit.
3. The model selects source/span identities. Evidence references cannot supply
   quotation text, replacement metadata, URLs or titles.
4. Application code validates those identities and constructs excerpts by slicing
   the captured text. Adjacent spans coalesce; separate excerpts show omissions.
   Direct quotations must occur verbatim in selected evidence. Explicit `[…]` or
   `[...]` omissions may select ordered exact pieces from one source; they cannot
   stitch different sources or reorder wording. No fuzzy matching
   or reconstruction from model memory is permitted.
   If one direct quotation contains separately assessed propositions, its complete
   wording may be checked against the combined selected spans touching that
   quotation **within one saved source**. Each claim retains its own references;
   this establishes quotation fidelity, not another claim's semantic support.
   An explicitly disputed assertion quoted from the user's question or original
   draft is not presented as a source quotation. A narrow, application-checked
   rejection frame recognizes that distinction; the model cannot supply its own
   exemption or anchor. Positive source attributions still require exact captured
   text. An ambiguous frame keeps the conservative quotation check.
   Direct source attributions in assessment reasons/caveats are checked as well:
   unmatched quotations are replaced by an unchecked warning rather than displayed
   as supporting source wording. The auditor is instructed to write explanations
   in its own prose; exact supporting quotations are supplied by the application.
5. Opening a citation displays the immutable saved extraction, highlighting the
   selected spans. Opening the current original is a separate action. Reindexing,
   editing or removing the source does not replace the saved supporting text.

Answer positions are application-owned too. The application supplies smaller
sentence units, preserving quotations, version numbers and common abbreviations.
The model selects exact, unambiguous proposition text; code computes its position.
A whole-unit assessment is appropriate only when all its propositions share the
same verdict and evidence. Multiple claims require separate selectors. Duplicate
assessments collapse; ambiguous overlapping claims or claim/exclusion conflicts
become unchecked instead of contradictory badges. A single unusable claim,
exclusion or selector is isolated to its own span or ignored with a recorded
limitation; it never discards the other verdicts in the answer. Unclassified gaps
stay separate so they cannot swallow valid claims between them. Reference markers
and connecting words between assessed claims are recorded as nonfactual
scaffolding; negation and inference language are never silently excluded.
Model-supplied character offsets and invented draft selectors are rejected.
Historical/provisional views apply the overlap safeguards without rewriting the
stored record.

Missing evidence is not disproof: “survival was not measured” leaves an improvement
unestablished; it does not contradict the improvement itself. A correction can
accurately explain that limitation and receive a supported assessment. Revisions
preserve supported facts, qualify unresolved portions, and reserve direct source
quotations for authentic passages.

PDF extraction and HTML cleanup yield exact **extracted text**, rather than
original page layout. An authentic excerpt can still be irrelevant, misleading,
outdated or false; source-support assessment remains a separate step.

**Sources consulted** lists retrieved material separately from claim support.
**Copy answer with assessments** includes claim labels, references, exact excerpts
and provenance. Selecting text from an audited answer also attaches its assessment
appendix, so copied prose does not silently lose its evidence qualifications.

## Storage and boundaries

Schema 5 adds `message_audits`, owned by assistant messages. Snapshots and pending
drafts checkpoint before auditing; final answer text and its hash-bound report
commit together. Conversation deletion removes these records. Normal stopped-app
backups include them in `chats.db`; no independent source-snapshot directory exists.
Earlier metadata-only sources must be reread before they can support a follow-up.
Advisory does no additional research, so unavailable historical evidence stays
unsupported. Local snapshots never become shared-library entries automatically.

The registry caps evidence at 64 snapshots / 256,000 characters per answer. Audits
allow 160 answer units and 512 assessed claims, with a 160,000-character response
limit per assessment/revision. Uncovered content that the one bounded coverage
follow-up still leaves unclassified stays unchecked; over-budget content stays
unchecked; evidence is not silently summarized to fit. Code blocks are outside
factual prose; nested list items and tables are included.

Rigor-enabled replies use Markdown in v1. Generated files, images, animations and
interactive views are outside this audit boundary. Citation rigor does not add
formal APA/Chicago/IEEE bibliography generation or independently validate calculations.

## Verification and evaluation

Deterministic checks exercise malicious auditor output, exact quotation fidelity,
Unicode and omitted spans, source changes, coverage gaps, budgets, disabled/effectful
research, timeouts/cancellation, settings and saved-answer compatibility:

```sh
PYTHONPATH=tests .venv/bin/python -m unittest test_citations test_saved_compatibility -v
.venv/bin/python scripts/evaluate_citations.py --output artifacts/citation-integrity.json
node scripts/check_citations.cjs
xvfb-run -a dbus-run-session -- .venv/bin/python scripts/check_gui_citations.py
```

The browser probe covers desktop/mobile controls, provisional/final/incomplete
states, safe passage rendering, copy and focus restoration. The native GTK/WebKit
probe uses scratch storage and scripted providers to check source highlighting and
active/saved reload, including removal of the live source. DOM labels and keyboard
checks do not establish assistive-technology behavior with a real screen reader.

The 22 synthetic cases in `tests/fixtures/citation-evaluation.json` cover invented
citations/quotes, scope, negation, causality, mixed claims, conflicts, versions,
dates, tables, source quality and source-text prompt injection. The default evaluator
checks five seeded source-integrity attacks, including an auditor falsely asserting
support. It does **not** measure a model's semantic judgment.

With the configured model already available, run:

```sh
.venv/bin/python scripts/evaluate_citations.py --live --output artifacts/citation-evaluation.json
```

This uses the configured model/provider with fixture evidence only; it never starts,
stops or switches models and never searches the public web. Results include initial
missed claims, false support, unnecessary flags, per-mode latency, research calls,
and final answers/reports. Missing the model is a recorded failure, not a passing
evaluation. Seeded unsupported claims and fabricated citations must never receive
supported labels in the acceptance set. Human review/sign-off and live model
results remain necessary before making academic-accuracy claims about a model.

The initial evaluation covered the host-integrity gate and three live
balanced-mode Gemma regressions using exact draft selectors; later assessment
fixes covered the full Gemma fixture matrix and longer paired tasks. Human sign-off and
acceptance checks for other intended models remain open.

Independent usefulness grading used
DeepSeek V4.1 Flash through OpenCode Go to assess 60 saved Gemma answers, with
mode/model/cohort identities hidden from the judge. Three answers use the current
contract; 57 come from the earlier numeric-offset prototype and are reported
separately. The current answers received 5/5 for body usefulness, but two had
annotation defects, including a compound false-support badge. Audit completion
and exact saved passages do not establish correct semantic support labels.
That initial grading preceded the assessment fixes. It is a single-judge
synthetic evaluation, not human sign-off.

With those saved artifacts present, reproduce or resume the grading run with:

```sh
.venv/bin/python scripts/grade_citation_answers.py --prepare  # inputs only, no API calls
.venv/bin/python scripts/grade_citation_answers.py            # authenticated OpenCode Go requests
```

The script uses the desktop-keyring Go credential, the exact catalog model ID
`opencode-go/deepseek-v4.1-flash`, and saved fixture evidence only. It does not
generate new answers or change the selected application model. Results, blinded
inputs, raw responses, rejected grades and a request checkpoint are saved in
`artifacts/citation-usefulness-grading*`. Re-running reuses valid grades only
when the input, rubric and grader identity match; failed grades are requested
again and retained in the request history. Issue quotations must occur exactly
in the supplied answer/annotation text or the grade is rejected.

For the extended tasks, generate one shared draft per task and compare modes:

```sh
.venv/bin/python scripts/evaluate_citations.py --live \
  --fixtures tests/fixtures/citation-academic-evaluation.json \
  --mode off --mode advisory --mode balanced --mode strict \
  --output artifacts/citation-academic-paired.json
```

`--reassess-from PATH` can evaluate saved final answer bodies against their original
snapshots without rewriting the bodies or searching again. Evaluation checkpoints
reuse complete outputs only for matching fixtures, implementation and selected
model. The false-support metric examines initial seeded assertions; semantic
judgment of revised prose still needs independent review.

The grader accepts repeatable `--input COHORT=PATH` and `--fixtures PATH` arguments.
Generated paired tasks must include exactly one off control with identical question,
sources, draft and answer model. The separate review gate additionally rejects any
major/blocking issue, even when the numeric usefulness/fidelity scores are high.
