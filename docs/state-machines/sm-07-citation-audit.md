# SM-07: Citation rigor and claim-verification audit

## Scope and ownership

| Path | Owns |
| --- | --- |
| `src/blackrose/citations.py` | Evidence capture, the audit state machine (`CitationAudit`), claim assessment and downgrade (`validate_assessment`, `reconcile_claims`, `_review_contradictions`), read-time normalization (`display_report`, `audit_payload`, `annotated_copy`) |
| `src/blackrose/storage/repository.py` | Durable audit checkpoints (`save_audit`), atomic final answer + audit + evidence (`finish_message`), crash conversion (`reap_pending`) |
| `src/blackrose/turns.py` | Drives the audit inside one reply, owns cancellation, timeouts, final commit and the interruption fallback (`_save_interrupted`) |

One `CitationAudit` instance exists per rigor-enabled reply. It is created and
awaited by the `TurnRunner` task (`turns.py:517`), so every transition below runs
on the reply's own asyncio task; there is no second writer. The *persisted*
report is written on that task through the repository's serialized database
worker (`storage/db.py::_worker`), so a checkpoint is either wholly before or
wholly after a terminal write.

Ownership boundaries from `docs/architecture.md`: `citations.py` owns
"bounded claim assessment/revision and exact supporting excerpts"; `turns.py`
owns "the shared audit deadline, selected provider, cancellation and atomic
final answer/report publication" (`architecture.md:195-202`). `save_audit` is
deliberately restricted to a *pending* assistant (`repository.py:1133`), so a
report checkpoint can never follow the terminal write.

## State variables

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `AnswerAudit['status']` | `citations.py:68` | `str` | Audit phase: `pending`, `checking`, `researching`, `revising`, `complete`, `incomplete`, `not_applicable` | `new_report:236`, `checkpoint:761`, `validate_assessment:641`, `unchecked_report:333` |
| `AnswerAudit['body_hash']` | `citations.py:69` | `str` | SHA-256 of the answer text this report assessed | `new_report:237`, `unchecked_report:334` |
| `AnswerAudit['claims']` | `citations.py:70` | `list[ClaimAssessment]` | Per-span verdicts with host-computed positions | `validate_assessment:567-597`, `unchecked_report:340`, `display_report:1056` |
| `AnswerAudit['revision']` | `citations.py:76` | `dict` (`text`, `body_hash`, `claims`) | Provisional assessments of the pre-revision draft | `run:977` (`provisional`), `run:1009`, `run:1018`, `run:1024` |
| `AnswerAudit['coverage']` | `citations.py:74` | `dict[str,int]` | Derived status/exclusion counts | `validate_assessment:639-640`, `display_report:1061-1062`, `unchecked_report:344` |
| `ClaimAssessment['status']` | `citations.py:58` | `str` | `supported`, `partially_supported`, `unsupported`, `contradicted`, `inference`, `unchecked` | `validate_assessment:540-569`, `_review_contradictions:914-915`, `display_report:1052` |
| `ClaimAssessment['references']` | `citations.py:59` | `list[EvidenceReference]` | Source/span identities only; excerpts are sliced by the host | `validate_assessment:576-579` |
| `CitationAudit.report` | `citations.py:731` | `AnswerAudit \| None` | Latest report in memory; what settlement falls back to | `checkpoint:760`, `run:973,1012,1015,1021` |
| `CitationAudit.initial_report` | `citations.py:732` | `AnswerAudit \| None` | In-memory copy of the first assessment; read only by `scripts/evaluate_citations.py:211` for the false-support metric, never persisted | `run:971` |
| `CitationAudit.draft_text` | `citations.py:735` | `str` | Answer text the checkpoints are bound to | `checkpoint:758`, `turns.py:535`, read by `turns.py:784` |
| `CitationAudit.prior_leads` | `citations.py:734` | `list[dict]` | Historical URLs/document ids that justify auditing an evidence-free reply | `inherit:753` |
| `EvidenceRegistry.attempted` | `citations.py:97` | `bool` | This reply called an evidence tool (success or failure) | `capture:124` |
| `EvidenceRegistry.sources` | `citations.py:95` | `list[EvidenceSnapshot]` | Immutable captured text with `sha256`/`spans` | `capture:150`, `inherit:107` |
| `messages.state` | `storage/records.py:36-48` | `MessageState` | `pending` until the terminal write | `repository.finish_message`, `reap_pending` |
| `messages.content` | schema 1 | `str` | Draft while pending; final answer after the terminal write | `save_audit:1139`, `finish_message:791`, `reap_pending` |
| `message_audits.report` | `storage/migrations.py:67-70` | JSON text | The persisted `AnswerAudit` | `save_audit:1140`, `finish_message:789`, `reap_pending:896` |

## States

### Audit lifecycle (persisted `status`, derived from `status` + `messages.state`)

| Name | Predicate |
| --- | --- |
| `absent` | No `message_audits` row. Either rigor was off (`turns.py:516`) or `run:957` returned `None` because nothing was attempted and no leads existed |
| `pending` | `status='pending'` (written by the empty checkpoint `turns.py:523`) |
| `checking` | `status='checking'` (`run:960`, `run:1004`) |
| `researching` | `status='researching'` (`_research:944`) |
| `revising` | `status='revising'` (`run:979`) |
| `complete` | `status='complete'` and every claim non-`unchecked` |
| `incomplete` | `status='incomplete'`, at least one `unchecked` claim or a recorded failure |
| `not_applicable` | `status='not_applicable'`, zero claims |
| `settled` | `messages.state ∈ {complete, cancelled, error}`; only terminal report statuses may exist here |
| `identity_mismatch` | Stored `body_hash != digest(messages.content)`; a read-time state, normalized to `incomplete` |

`pending`/`checking`/`researching`/`revising` are *transient*: the only persisted
statuses a settled reply may carry are `complete`, `incomplete`,
`not_applicable`. `absent` and those seven status values are mutually exclusive
and total — exactly one holds for any assistant message. `settled` and
`identity_mismatch` are orthogonal derived predicates used by the guarantees
below, not additional lifecycle phases.

### Claim states

`supported`, `partially_supported`, `unsupported`, `contradicted`, `inference`,
`unchecked` (`citations.py:25`). They are recomputed from the model payload on
every assessment pass; they are not accumulated. `unchecked` is the bottom of
the ladder: nothing in the pipeline promotes a claim out of it.

## Events

| Event | Source |
| --- | --- |
| `checkpoint(text?, phase)` | Internal; `turns.py:523`, `run:960,979,1004`, `_research:944` |
| Initial assessment returns / raises | `_assess:850-860` |
| Coverage retry returns / fails | `_retry_coverage:829-840` |
| Contradiction review returns / fails | `_review_contradictions:877-920` |
| Evidence tool succeeds / fails / is denied | `_research:930-954` |
| Revision request returns / is empty / raises | `run:980-1000` |
| Audit deadline (`asyncio.timeout`) fires | `run:964` → `TimeoutError` |
| `asyncio.CancelledError` (Stop) | `run:1014`, `_research:951`, `turns.py:766-773` |
| Turn commits the reply | `turns.py:726-740` → `finish_message` |
| Settlement of an interrupted reply | `turns.py:775-801` → `finish_message` |
| Startup recovery | `chat.py:231` → `reap_pending` (`repository.py:884-897`) |
| Checkpoint attempted after settlement | `save_audit:1133` raises |
| Display/read of a stored report | `audit_payload:1080`, `annotated_copy:1112`, `gui/render.py:181` |

## Transition table

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `absent` | reply starts with `mode != 'off'` | `turns.py:516` | `checkpoint('')` writes `pending` (`turns.py:523`) | `pending` |
| `pending` | `run` entry | `registry.attempted or registry.sources or prior_leads` (`run:957`) | `checkpoint(text,'checking')` (`run:960`) | `checking` |
| `pending` | `run` entry | no attempt, no sources, no leads (`run:957-958`) | return `(text, None)`; no row | `absent`; turn writes `not_applicable` (`turns.py:729-731`) |
| `checking` | assessment validated | `payload.source_based is False` and `not registry.attempted` (`validate_assessment:434`) | report has zero claims (`435-436`) | `not_applicable` (`run:972-974`) |
| `checking` | coverage gap found | one bounded retry (`_retry_coverage:823-835`) | merged payload revalidated; verdicts for already-assessed units are re-derived, not re-declared (`merge_assessments:417-418`) | `checking` (self-transition) |
| `checking` | assessment completes | no `unchecked` claim | `validate_assessment:641`; persisted only at commit | `complete` |
| `checking` | assessment completes | any `unchecked` claim / malformed batch (`unchecked_report:333`) | `validate_assessment:641` | `incomplete` |
| `checking` | mode is balanced/strict and (research requested or a claim is not `supported` or carries a quality caveat) | `run:975-976` | latch `provisional` (`run:977`), run research, `checkpoint(text,'revising')` (`run:979`) | `revising` |
| `checking` | mode is advisory, or every claim supported with no caveats | `run:975-976` false | no provisional, no revision | `complete`/`incomplete` |
| `researching` | each accepted evidence call | call validated and captured (`_research:930-941`) | `checkpoint(phase='researching')` (`944`) | `researching` (self-transition) |
| `researching` | evidence call fails or is denied | `_research:953-954` | limitation recorded only; no checkpoint | `researching` |
| `researching` | budget exhausted or queue drained | `_research:927` | return to `run` | `revising` (`run:979`) |
| `revising` | revision request returns non-empty text | `run:999` | `final_text = revised` (`1003`), then `checkpoint(revised,'checking')` (`1004`) — no await between the two | `checking` |
| `revising` | revision request returns empty | `run:999-1000` raises | `except Exception:1020` → `unchecked_report` with the pre-revision text | `incomplete` |
| `checking` (after revision) | final assessment returns | status is terminal (`validate_assessment:641`) | `candidate['revision'] = provisional` (`1009`), `final_text=revised` (`1010`) | `complete`/`incomplete` |
| `checking` (after revision) | final assessment says `not_applicable` | `run:1006-1007` | `ValueError` → `except Exception:1020`, revised text kept | `incomplete` |
| `researching`/`revising`/`checking` | `CancelledError` | `run:1014` | `unchecked_report(final_text)` + `revision` if latched (`1015-1018`), re-raise | `incomplete` (memory), then settlement (`turns.py:787`) |
| `researching`/`revising`/`checking` | audit deadline | `run:964` → `TimeoutError` at `1020` | `unchecked_report(final_text)` + `revision`; returns normally | `incomplete` |
| any transient | any other exception | `run:1020-1025` | `unchecked_report(final_text)` + `revision` | `incomplete` |
| any transient | turn settles after Stop/error | `turns.py:783-788` | content := `audit.draft_text`; `unchecked_report`; `finish_message` (validates hash, `repository.py:787`) | terminal row |
| any transient | process crash | message still `pending` | `reap_pending:892-897` rebuilds from `messages.content` | `incomplete` (`messages.state='error'`) |
| `complete`/`incomplete`/`not_applicable` | `save_audit` | row is not a pending assistant (`repository.py:1133`) | `ValueError`; nothing written | unchanged |
| `complete`/`incomplete`/`not_applicable` | `finish_message` with mismatched `body_hash` | `repository.py:787` | `ValueError` inside one transaction; row stays pending | unchanged |
| `complete` | stored report read | `body_hash != digest(text)` (`display_report:1042`) | whole report converted to `unchecked_report` | `identity_mismatch` (`incomplete`) |
| `complete` | stored claim overlaps another | `reconcile_claims:356-375` | overlapping group replaced by one `unchecked` claim | `incomplete` (`display_report:1059`) |

Claim transitions inside one assessment pass (`validate_assessment:540-597`):

| Declared | Trigger | To |
| --- | --- | --- |
| any | unsupported claim fields (`514-518`), unusable selector (`519-524`), invalid/duplicate span references or an undistinguished multi-claim unit (`534-544`), explanation quoting unmatched evidence (`567-569`) | `unchecked` |
| `supported`/`partially_supported`/`inference` | no valid captured reference | `unsupported` (`545-547`) |
| `contradicted` | no valid captured reference | `unchecked` (`546`) |
| `supported`/`partially_supported`/`inference` | direct quotation absent from the selected spans | `unsupported` (`557-560`) |
| any | two claims/exclusion overlap the same text | `unchecked` (`372-375`, `590-593`) |
| any | alphanumeric text classified by neither | `unchecked` gap (`602-627`), unless it is punctuation or `and/but/or/nor/yet/so` between assessed clauses (`617-623`) |
| `contradicted` | review: `insufficient` / `unclear` / unavailable | `unsupported` / `unchecked` / `unchecked` (`914-920`) |
| `contradicted` | review: `incompatible` | `contradicted` (`914`) |

## Invariants

1. **Single writer while pending.** A checkpoint can only be written while
   `messages.state='pending'` (`repository.py:1132-1134`), so no checkpoint can
   follow the terminal write. Pinned by
   `test_save_audit_requires_a_pending_assistant_and_matching_bytes`.
2. **Settled replies carry terminal statuses only.** `run` returns `complete`,
   `incomplete` or `not_applicable` (`956-1025`), settlement always writes
   `unchecked_report` (`turns.py:787`), and `turns.py:729-731` writes
   `not_applicable` for a skipped audit. Pinned by
   `test_run_returns_only_terminal_statuses`.
3. **Answer/audit identity.** Every stored audit row satisfies
   `report['body_hash'] == digest(messages.content)`. Enforced on write by
   `save_audit:1137` (when `content` is passed; `checkpoint` always passes it,
   `citations.py:762`) and unconditionally by `finish_message:787`; on read by
   `display_report:1042`, `audit_payload:1080`, `annotated_copy:1110` and
   `gui/render.py:182`; repaired by `reap_pending:894`. Not enforced by the raw
   accessors `audit_for:1145` / `StoredMessage.audit` (`repository.py:214`),
   which are display-unaware. Pinned by
   `test_body_hash_mismatch_fails_closed_on_every_read_path`,
   `test_reap_repairs_a_stale_body_hash_against_the_stored_answer`.
4. **`complete` excludes `unchecked`.** `validate_assessment:641` derives the
   status from the reconciled coverage, and `display_report:1059-1060` re-derives
   it from the claims it actually returns. `not_applicable` has zero claims.
   Pinned by `test_complete_status_never_coexists_with_an_unchecked_claim`,
   `test_display_report_derives_status_instead_of_trusting_stored_labels`.
5. **Claim downgrades are one-way within a pass.** A claim's rank never rises
   once the assessment is being validated: every rule either keeps or lowers it,
   and the contradiction review only ever maps a `contradicted` claim to
   `contradicted`/`unsupported`/`unchecked` (`914-920`). Pinned by
   `test_claim_downgrades_are_monotonic_within_one_pass`,
   `test_review_can_only_keep_or_weaken_a_contradiction`.
6. **Revised text is latched before it can be observed.** `final_text = revised`
   (`run:1003`) and `draft_text = revised` (`checkpoint:758`, called at `1004`)
   are separated by no await, and every later failure builds its report from
   `final_text`. Therefore settlement's fallback `self.content =
   audit.draft_text` (`turns.py:784`) writes the revised text, never the
   erroneous draft. Pinned by
   `test_revised_text_is_latched_when_the_final_assessment_fails`,
   `test_empty_revision_cannot_replace_the_answer`.
7. **Provisional history is never rewritten into the final claims.** The
   provisional assessment is stored under `revision` (`run:1009`) and the final
   claims are separate. Pinned by `test_unchecked_report_is_idempotent_and_preserves_provisional_history`.
8. **`not_applicable` requires an unattempted reply.** The guard is
   `payload['source_based'] is False and not registry.attempted`
   (`validate_assessment:434`); `inherit:101-107` loads sources without setting
   `attempted`, so inheritance alone does not force an audit. Pinned by
   `test_retrieval_attempt_forbids_not_applicable`,
   `test_inherited_evidence_alone_allows_a_declined_assessment`.
9. **Transient phases are escapable.** Every persisted transient status leaves
   through commit, cancellation (`run:1014`), timeout/error (`run:1020`), or
   startup reaping (`repository.py:892`). Pinned by
   `test_cancellation_mid_research_keeps_text_and_provisional_record`,
   `test_timeout_mid_research_returns_an_incomplete_report`,
   `test_reap_converts_every_transient_checkpoint_to_incomplete`.
10. **Recovery is idempotent and non-destructive.** `unchecked_report` replaces
    only `status`, `body_hash`, `limitations`, `claims`, `exclusions` and
    `coverage`, preserving `sources` and `revision` (`331-345`); reaping twice
    changes nothing because the row leaves `pending` (`repository.py:905-912`).
    Pinned by `test_unchecked_report_is_idempotent_and_preserves_provisional_history`,
    `test_reap_converts_every_transient_checkpoint_to_incomplete`.

## Hazards and open questions

**H1 — `fragile` — `citations.py:971-979`, `citations.py:1004`, `citations.py:1009`, `repository.py:892`.**
The provisional claim assessments are never checkpointed. `checkpoint` rebuilds
the report from `new_report` (`citations.py:760`), so the `revising` and
post-revision `checking` rows carry no `revision`; only the in-memory final
report (`1009`) and the exception paths (`1017-1018`, `1023-1024`) attach it.
Graceful Stop and timeout preserve it; a crash between the `revising`
checkpoint and `finish_message` loses it, and `reap_pending` then reports only
fresh `unchecked` claims. `docs/citation-rigor.md:50-51` states the provisional
assessments "remain inspectable after revision", which is true for normal
completion, Stop and timeout, but not after a crash in that window.
*Demonstrated:* yes, `test_checkpoints_never_carry_the_provisional_revision`
(all checkpoint writes lack `revision` while the returned report has it) and
`test_reap_keeps_a_checkpointed_provisional_revision` (reaping preserves a
`revision` only if a writer put one there). *Confirmation of loss:* run the
scripted audit, stop after the `revising` checkpoint, restart, compare the
reaped report's `claims` with `initial_report`.

**H2 — `fragile` — `citations.py:1066-1068`.**
`display_report` reads `revision['text']` and iterates `revision['claims']`
without validating the saved shape. A stored revision missing `text` raises
`KeyError`, which propagates out of `audit_payload`/`annotated_copy` into
transcript rendering instead of failing closed. Every current writer constructs
all three keys (`run:977` builds it; `run:1024` and `turns.py:787` copy it), so
this is reachable only from a corrupted or hand-migrated row. *Demonstrated:* no
— a test would have to seed a malformed row, which would pin the behavior I
propose to change.
*Confirmation:* write `{'claims': []}` as `revision` and call
`display_report`; observed `KeyError: 'text'`.

**H3 — `documentation-gap` — `repository.py:1135-1140`.**
`save_audit` enforces `body_hash ⟷ content` only when `content` is passed. With
`content=None` a report bound to different bytes is accepted, so the identity
invariant depends on the caller rather than the writer. The only production
caller passes content (`citations.py:762`), and an accepted desync is still
normalized on display (invariant 3). *Demonstrated:* no (the test would enshrine
the gap). *Confirmation:* `save_audit(id, report_for_other_text)` on a pending
row then `audit_for` returns the mismatched hash. Proposed fix in
"Formalization decisions".

**H4 — `documentation-gap` — `repository.py:892`.**
`reap_pending` selects audits by `messages.state='pending'`, not by report
status. Transient statuses are cleaned up because settlement only ever writes
terminal ones (invariants 2, 9), not because recovery checks them. A terminal
row carrying `researching` would be displayed with a live-progress badge
forever; no current writer can produce that pairing. *Demonstrated:* no.
*Confirmation:* store a `researching` report on a `complete` message and render
the transcript — `audit_payload` returns the stored status unchanged.

**H5 — `documentation-gap` — `citations.py:434`, `citations.py:957`.**
`not_applicable` is decided by this reply's `registry.attempted` and the model's
`source_based` flag, not by the presence of inherited evidence. In a chat whose
earlier replies captured sources, a follow-up that declares `source_based=false`
is exempt from assessment even though the registry already holds snapshots
(`inherit:101-107` does not set `attempted`). This matches the documented rule
that rigor covers answers *dependent on* evidence and lets a genuinely
unrelated follow-up pass, but it means the untrusted model chooses its own
coverage. *Demonstrated:* yes,
`test_inherited_evidence_alone_allows_a_declined_assessment`. *Would confirm a
stronger rule is intended:* product decision, not code archaeology.

**H6 — `none-found` — `citations.py:1014-1025`, `turns.py:783-788`.**
No path was found that sets `final_text`/`self.content` to a revision and then
restores the draft. `draft_text` is the single fallback source, and it is
updated before any await following the latch. Cancellation, timeout and
settlement each produce an `incomplete` report whose hash matches the stored
text (tests under `AuditPhaseTests`).

**H7 — `none-found` — `citations.py:348-376`, `citations.py:540-569`, `citations.py:914-920`.**
No upgrade path was found for claim status: validation only downgrades, overlap
reconciliation only downgrades, and the contradiction review has no mapping to
`supported`/`inference`.

## Formalization decisions

**No source change made.** The editable source module for this bead
(`citations.py`) contains no demonstrated defect: the latch, the downgrade
ladder, the `complete`/`unchecked` exclusion and the escape-from-transient
guarantees all hold, and each is now pinned by a test. The hazards are
data-loss and fail-closed questions whose fix shapes are policy, so they are
recorded rather than implemented.

Proposals, smallest first, for a follow-up bead:

1. **Persist the provisional record with the `revising` checkpoint (H1).**
   Give `checkpoint` an optional `revision` argument and set it at
   `citations.py:979` (and at `1004` so a crash during the final assessment
   keeps the pre-revision verdicts). Report positions already index
   `revision['text']`, so the read path needs no change; the display decision is
   whether live replay should show provisional badges before the final report.
   Regression test: full scripted run → assert the `revising` checkpoint
   contains `revision`; crash after it → reaped report keeps those claims.
2. **Fail closed on a malformed saved `revision` (H2).** In
   `display_report:1063`, treat a revision that is not
   `{'text': str, 'body_hash': str, 'claims': list}` as absent and append a
   limitation, instead of raising `KeyError`. Test: a report with
   `revision={'claims': []}` yields an `incomplete` display report.
3. **Check identity in `save_audit` even without `content` (H3).** Compare
   `report['body_hash']` with the stored assistant content when `content is
   None` (`repository.py:1135-1139`). Test: a mismatched report is refused and
   the row keeps its prior report.
4. **Normalize transient statuses on read (H4, optional).** Make
   `display_report` convert `pending`/`checking`/`researching`/`revising` to an
   `incomplete` report when the message is settled. This duplicates state
   knowledge in the display layer; only do it if a writer other than
   `TurnRunner` ever stores audits.

Proposals 3 and 4 touch `repository.py`/shared read paths and were out of this
agent's edit scope; proposal 1 changes what a checkpoint means for live replay,
which is a product decision.

## Verification

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_07_citation_audit.py -v
# Ran 27 tests ... OK

.venv/bin/python scripts/check_unit_tests.py -p test_citations.py
# Ran 64 tests ... OK

.venv/bin/python scripts/check_unit_tests.py -p test_citation_grading.py
# Ran 5 tests ... OK

.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
# Ran 6 tests ... OK
```

The new tests are deterministic and offline: the audit-phase tests use a
scripted backend and an in-memory checkpoint recorder, and the storage tests use
a temporary `chats.db` under `tempfile.TemporaryDirectory()` (the runner's XDG
leak check passed). Not run: the live evaluation
(`scripts/evaluate_citations.py --live`), the GTK/browser citation probes, and
`scripts/check_health.py` — this change adds one test module and one document
and touches no source, so the focused suites above are the applicable gate.
