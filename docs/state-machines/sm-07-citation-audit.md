# SM-07: Citation rigor and claim-verification audit

## Scope and ownership

| Path | Owns |
| --- | --- |
| `src/blackrose/citations.py` | Evidence capture, the audit state machine (`CitationAudit`), claim assessment and downgrade (`validate_assessment`, `reconcile_claims`, `_review_contradictions`), read-time normalization (`display_report`, `audit_payload`, `annotated_copy`) |
| `src/blackrose/storage/repository.py` | Durable audit checkpoints (`save_audit`), atomic final answer + audit + evidence (`finish_message`), crash conversion (`reap_pending`) |
| `src/blackrose/turns.py` | Drives the audit inside one reply, owns cancellation, timeouts, final commit and the interruption fallback (`_save_interrupted`) |

One `CitationAudit` instance exists per rigor-enabled reply. It is created by
`src/blackrose/turns.py::TurnRunner._start_reply` and awaited by `src/blackrose/turns.py::TurnRunner._complete` on the `TurnRunner` task, so every transition below runs
on the reply's own asyncio task; there is no second writer. The *persisted*
report is written on that task through the repository's serialized database
worker (`storage/db.py::run_to_completion`), so a checkpoint is either wholly before or
wholly after a terminal write.

Ownership boundaries from `docs/architecture.md`: `citations.py` owns
"bounded claim assessment/revision and exact supporting excerpts"; `turns.py`
owns "the shared audit deadline, selected provider, cancellation and atomic
final answer/report publication" ([admission and effects](../architecture.md#admission-and-effects)).
`src/blackrose/storage/repository.py::Repository.save_audit` is deliberately restricted to a *pending* assistant, so a
report checkpoint can never follow the terminal write.

## State variables

Unqualified names below are in `src/blackrose/citations.py`: `run`, `checkpoint`,
`_assess`, `_retry_coverage`, `_review_contradictions` and `_research` are
`CitationAudit` methods; `new_report`, `unchecked_report`, `validate_assessment`,
`merge_assessments`, `reconcile_claims`, `display_report`, `audit_payload` and
`annotated_copy` are module functions. `save_audit`, `finish_message`,
`reap_pending` and `audit_for` are `Repository` methods.

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `AnswerAudit['status']` | `src/blackrose/citations.py::AnswerAudit` | `str` | Audit phase: `pending`, `checking`, `researching`, `revising`, `complete`, `incomplete`, `not_applicable` | `new_report`, `checkpoint`, `validate_assessment`, `unchecked_report` |
| `AnswerAudit['body_hash']` | `src/blackrose/citations.py::AnswerAudit` | `str` | SHA-256 of the answer text this report assessed | `new_report`, `unchecked_report` |
| `AnswerAudit['claims']` | `src/blackrose/citations.py::AnswerAudit` | `list[ClaimAssessment]` | Per-span verdicts with host-computed positions | `validate_assessment`, `unchecked_report`, `display_report` |
| `AnswerAudit['revision']` | `src/blackrose/citations.py::AnswerAudit` | `dict` (`text`, `body_hash`, `claims`) | Provisional assessments of the pre-revision draft | `run` (`provisional`), `run`, `run`, `run` |
| `AnswerAudit['coverage']` | `src/blackrose/citations.py::AnswerAudit` | `dict[str,int]` | Derived status/exclusion counts | `validate_assessment`, `display_report`, `unchecked_report` |
| `ClaimAssessment['status']` | `src/blackrose/citations.py::ClaimAssessment` | `str` | `supported`, `partially_supported`, `unsupported`, `contradicted`, `inference`, `unchecked` | `validate_assessment`, `_review_contradictions`, `display_report` |
| `ClaimAssessment['references']` | `src/blackrose/citations.py::ClaimAssessment` | `list[EvidenceReference]` | Source/span identities only; excerpts are sliced by the host | `validate_assessment` |
| `CitationAudit.report` | `src/blackrose/citations.py::CitationAudit.__init__` | `AnswerAudit \| None` | Latest report in memory; what settlement falls back to | `checkpoint`, `run` |
| `CitationAudit.initial_report` | `src/blackrose/citations.py::CitationAudit.__init__` | `AnswerAudit \| None` | In-memory copy of the first assessment; read only by `scripts/evaluate_citations.py::live` for the false-support metric, never persisted | `run` |
| `CitationAudit.draft_text` | `src/blackrose/citations.py::CitationAudit.__init__` | `str` | Answer text the checkpoints are bound to | `checkpoint`, `src/blackrose/turns.py::TurnRunner._append`, read by `src/blackrose/turns.py::TurnRunner._save_interrupted` |
| `CitationAudit.prior_leads` | `src/blackrose/citations.py::CitationAudit.__init__` | `list[dict]` | Historical URLs/document ids that justify auditing an evidence-free reply | `src/blackrose/citations.py::CitationAudit.inherit` |
| `EvidenceRegistry.attempted` | `src/blackrose/citations.py::EvidenceRegistry.__init__` | `bool` | This reply called an evidence tool (success or failure) | `src/blackrose/citations.py::EvidenceRegistry.capture` |
| `EvidenceRegistry.sources` | `src/blackrose/citations.py::EvidenceRegistry.__init__` | `list[EvidenceSnapshot]` | Immutable captured text with `sha256`/`spans` | `src/blackrose/citations.py::EvidenceRegistry.capture`, `src/blackrose/citations.py::EvidenceRegistry.inherit` |
| `messages.state` | `src/blackrose/storage/records.py::MessageState` | `MessageState` | `pending` until the terminal write | `src/blackrose/storage/repository.py::Repository.finish_message`, `src/blackrose/storage/repository.py::Repository.reap_pending` |
| `messages.content` | schema 1 | `str` | Draft while pending; final answer after the terminal write | `save_audit`, `finish_message`, `reap_pending` |
| `message_audits.report` | `src/blackrose/storage/migrations.py::MIGRATIONS` (schema 5) | JSON text | The persisted `AnswerAudit` | `save_audit`, `finish_message`, `reap_pending` |

## States

### Audit lifecycle (persisted `status`, derived from `status` + `messages.state`)

| Name | Predicate |
| --- | --- |
| `absent` | No `message_audits` row. Either rigor was off (`src/blackrose/turns.py::TurnRunner._start_reply`) or `run` returned `None` because nothing was attempted and no leads existed |
| `pending` | `status='pending'` (written by the empty checkpoint in `src/blackrose/turns.py::TurnRunner._start_reply`) |
| `checking` | `status='checking'` (`run`, `run`) |
| `researching` | `status='researching'` (`_research`) |
| `revising` | `status='revising'` (`run`) |
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
`unchecked` (`src/blackrose/citations.py::STATUSES`). They are recomputed from the model payload on
every assessment pass; they are not accumulated. `unchecked` is the bottom of
the ladder: nothing in the pipeline promotes a claim out of it.

## Events

| Event | Source |
| --- | --- |
| `checkpoint(text?, phase)` | Internal; `src/blackrose/turns.py::TurnRunner._start_reply`, `src/blackrose/citations.py::CitationAudit.run`, `src/blackrose/citations.py::CitationAudit._research` |
| Initial assessment returns / raises | `_assess` |
| Coverage retry returns / fails | `_retry_coverage` |
| Contradiction review returns / fails | `_review_contradictions` |
| Evidence tool succeeds / fails / is denied | `_research` |
| Revision request returns / is empty / raises | `run` |
| Audit deadline (`asyncio.timeout`) fires | `run` → `TimeoutError` |
| `asyncio.CancelledError` (Stop) | `src/blackrose/citations.py::CitationAudit.run`, `src/blackrose/citations.py::CitationAudit._research`, `src/blackrose/turns.py::TurnRunner._settle_interrupted` |
| Turn commits the reply | `src/blackrose/turns.py::TurnRunner._complete` → `src/blackrose/storage/repository.py::Repository.finish_message` |
| Settlement of an interrupted reply | `src/blackrose/turns.py::TurnRunner._save_interrupted` → `src/blackrose/storage/repository.py::Repository.finish_message` |
| Startup recovery | `src/blackrose/chat.py::ChatController.recover_interrupted` → `src/blackrose/storage/repository.py::Repository.reap_pending` |
| Checkpoint attempted after settlement | `save_audit` raises |
| Display/read of a stored report | `src/blackrose/citations.py::audit_payload`, `src/blackrose/citations.py::annotated_copy`, `src/blackrose/gui/render.py::audited_html` |

## Transition table

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `absent` | reply starts with `mode != 'off'` | `mode != 'off'` in `src/blackrose/turns.py::TurnRunner._start_reply` | `checkpoint('')` writes `pending` | `pending` |
| `pending` | `run` entry | `registry.attempted or registry.sources or prior_leads` (`run`) | `checkpoint(text,'checking')` (`run`) | `checking` |
| `pending` | `run` entry | no attempt, no sources, no leads (`run`) | return `(text, None)`; no row | `absent`; turn writes `not_applicable` (`src/blackrose/turns.py::TurnRunner._complete`) |
| `checking` | assessment validated | `payload.source_based is False` and `not registry.attempted` (`validate_assessment`) | report has zero claims | `not_applicable` (`run`) |
| `checking` | coverage gap found | one bounded retry (`_retry_coverage`) | merged payload revalidated; verdicts for already-assessed units are re-derived, not re-declared (`merge_assessments`) | `checking` (self-transition) |
| `checking` | assessment completes | no `unchecked` claim | `validate_assessment`; persisted only at commit | `complete` |
| `checking` | assessment completes | any `unchecked` claim / malformed batch (`unchecked_report`) | `validate_assessment` | `incomplete` |
| `checking` | mode is balanced/strict and (research requested or a claim is not `supported` or carries a quality caveat) | `run` | latch `provisional` (`run`), run research, `checkpoint(text,'revising')` (`run`) | `revising` |
| `checking` | mode is advisory, or every claim supported with no caveats | `run` false | no provisional, no revision | `complete`/`incomplete` |
| `researching` | each accepted evidence call | call validated and captured (`_research`) | `checkpoint(phase='researching')` | `researching` (self-transition) |
| `researching` | evidence call fails or is denied | `_research` | limitation recorded only; no checkpoint | `researching` |
| `researching` | budget exhausted or queue drained | `_research` | return to `run` | `revising` (`run`) |
| `revising` | revision request returns non-empty text | `run` | `final_text = revised`, then `checkpoint(revised,'checking')` — no await between the two | `checking` |
| `revising` | revision request returns empty | `run` raises | `run`'s `except Exception` → `unchecked_report` with the pre-revision text | `incomplete` |
| `checking` (after revision) | final assessment returns | status is terminal (`validate_assessment`) | `candidate['revision'] = provisional`, `final_text=revised` | `complete`/`incomplete` |
| `checking` (after revision) | final assessment says `not_applicable` | `run` | `ValueError` → `run`'s `except Exception`, revised text kept | `incomplete` |
| `researching`/`revising`/`checking` | `CancelledError` | `run` | `unchecked_report(final_text)` + `revision` if latched, re-raise | `incomplete` (memory), then settlement (`src/blackrose/turns.py::TurnRunner._save_interrupted`) |
| `researching`/`revising`/`checking` | audit deadline | `run` → `TimeoutError` caught by its `except Exception` | `unchecked_report(final_text)` + `revision`; returns normally | `incomplete` |
| any transient | any other exception | `run` | `unchecked_report(final_text)` + `revision` | `incomplete` |
| any transient | turn settles after Stop/error | `src/blackrose/turns.py::TurnRunner._save_interrupted` | content := `audit.draft_text`; `unchecked_report`; `finish_message` (validates the hash) | terminal row |
| any transient | process crash | message still `pending` | `reap_pending` rebuilds from `messages.content` | `incomplete` (`messages.state='error'`) |
| `complete`/`incomplete`/`not_applicable` | `save_audit` | row is not a pending assistant (`Repository.save_audit`) | `ValueError`; nothing written | unchanged |
| `complete`/`incomplete`/`not_applicable` | `finish_message` with mismatched `body_hash` | `Repository.finish_message` | `ValueError` inside one transaction; row stays pending | unchanged |
| `complete` | stored report read | `body_hash != digest(text)` (`display_report`) | whole report converted to `unchecked_report` | `identity_mismatch` (`incomplete`) |
| `complete` | stored claim overlaps another | `reconcile_claims` | overlapping group replaced by one `unchecked` claim | `incomplete` (`display_report`) |

Claim transitions inside one assessment pass (`validate_assessment`):

| Declared | Trigger | To |
| --- | --- | --- |
| any | unsupported claim fields, unusable selector, invalid/duplicate span references or an undistinguished multi-claim unit, explanation quoting unmatched evidence | `unchecked` |
| `supported`/`partially_supported`/`inference` | no valid captured reference | `unsupported` |
| `contradicted` | no valid captured reference | `unchecked` |
| `supported`/`partially_supported`/`inference` | direct quotation absent from the selected spans | `unsupported` |
| any | two claims/exclusion overlap the same text | `unchecked` (`src/blackrose/citations.py::reconcile_claims`) |
| any | alphanumeric text classified by neither | `unchecked` gap, unless it is punctuation or `and/but/or/nor/yet/so` between assessed clauses |
| `contradicted` | review: `insufficient` / `unclear` / unavailable | `unsupported` / `unchecked` / `unchecked` (`CitationAudit._review_contradictions`) |
| `contradicted` | review: `incompatible` | `contradicted` |

## Invariants

1. **Single writer while pending.** A checkpoint can only be written while
   `messages.state='pending'` (`src/blackrose/storage/repository.py::Repository.save_audit`), so no checkpoint can
   follow the terminal write. Pinned by
   `test_save_audit_requires_a_pending_assistant_and_matching_bytes`.
2. **Settled replies carry terminal statuses only.** `run` returns `complete`,
   `incomplete` or `not_applicable` (`CitationAudit.run`), settlement always writes
   `unchecked_report` (`src/blackrose/turns.py::TurnRunner._save_interrupted`), and `src/blackrose/turns.py::TurnRunner._complete` writes
   `not_applicable` for a skipped audit. Pinned by
   `test_run_returns_only_terminal_statuses`.
3. **Answer/audit identity.** Every stored audit row satisfies
   `report['body_hash'] == digest(messages.content)`. Enforced on write by
   `save_audit` (when `content` is passed; `checkpoint` always passes it,
   `src/blackrose/citations.py::CitationAudit.checkpoint`) and unconditionally by `finish_message`; on read by
   `display_report`, `audit_payload`, `annotated_copy` and
   `src/blackrose/gui/render.py::audited_html`; repaired by `reap_pending`. Not enforced by the raw
   accessors `src/blackrose/storage/repository.py::Repository.audit_for` / `StoredMessage.audit` (`src/blackrose/storage/repository.py::_load_messages`),
   which are display-unaware. Pinned by
   `test_body_hash_mismatch_fails_closed_on_every_read_path`,
   `test_reap_repairs_a_stale_body_hash_against_the_stored_answer`.
4. **`complete` excludes `unchecked`.** `validate_assessment` derives the
   status from the reconciled coverage, and `display_report` re-derives
   it from the claims it actually returns. `not_applicable` has zero claims.
   Pinned by `test_complete_status_never_coexists_with_an_unchecked_claim`,
   `test_display_report_derives_status_instead_of_trusting_stored_labels`.
5. **Claim downgrades are one-way within a pass.** A claim's rank never rises
   once the assessment is being validated: every rule either keeps or lowers it,
   and the contradiction review only ever maps a `contradicted` claim to
   `contradicted`/`unsupported`/`unchecked` (`CitationAudit._review_contradictions`). Pinned by
   `test_claim_downgrades_are_monotonic_within_one_pass`,
   `test_review_can_only_keep_or_weaken_a_contradiction`.
6. **Revised text is latched before it can be observed.** `final_text = revised`
   (`run`) and `draft_text = revised` (`checkpoint`, called by `run` immediately after)
   are separated by no await, and every later failure builds its report from
   `final_text`. Therefore settlement's fallback `self.content =
   audit.draft_text` (`src/blackrose/turns.py::TurnRunner._save_interrupted`) writes the revised text, never the
   erroneous draft. Pinned by
   `test_revised_text_is_latched_when_the_final_assessment_fails`,
   `test_empty_revision_cannot_replace_the_answer`.
7. **Provisional history is never rewritten into the final claims.** The
   provisional assessment is stored under `revision` (`run`) and the final
   claims are separate. Pinned by `test_unchecked_report_is_idempotent_and_preserves_provisional_history`.
8. **`not_applicable` requires an unattempted reply.** The guard is
   `payload['source_based'] is False and not registry.attempted`
   (`validate_assessment`); `src/blackrose/citations.py::EvidenceRegistry.inherit` loads sources without setting
   `attempted`, so inheritance alone does not force an audit. Pinned by
   `test_retrieval_attempt_forbids_not_applicable`,
   `test_inherited_evidence_alone_allows_a_declined_assessment`.
9. **Transient phases are escapable.** Every persisted transient status leaves
   through commit, cancellation (`run`), timeout/error (`run`), or
   startup reaping (`src/blackrose/storage/repository.py::Repository.reap_pending`). Pinned by
   `test_cancellation_mid_research_keeps_text_and_provisional_record`,
   `test_timeout_mid_research_returns_an_incomplete_report`,
   `test_reap_converts_every_transient_checkpoint_to_incomplete`.
10. **Recovery is idempotent and non-destructive.** `unchecked_report` replaces
    only `status`, `body_hash`, `limitations`, `claims`, `exclusions` and
    `coverage`, preserving `sources` and `revision` (`src/blackrose/citations.py::unchecked_report`); reaping twice
    changes nothing because the row leaves `pending` (`src/blackrose/storage/repository.py::Repository.reap_pending`).
    Pinned by `test_unchecked_report_is_idempotent_and_preserves_provisional_history`,
    `test_reap_converts_every_transient_checkpoint_to_incomplete`.

## Hazards and open questions

**H1 — `fragile` — `src/blackrose/citations.py::CitationAudit.run`, `src/blackrose/storage/repository.py::Repository.reap_pending`.**
The provisional claim assessments are never checkpointed. `checkpoint` rebuilds
the report from `src/blackrose/citations.py::new_report`, so the `revising` and
post-revision `checking` rows carry no `revision`; only the in-memory final
report and `run`'s exception paths attach it.
Graceful Stop and timeout preserve it; a crash between the `revising`
checkpoint and `finish_message` loses it, and `reap_pending` then reports only
fresh `unchecked` claims. [Citation rigor](../citation-rigor.md#what-each-level-does) states the provisional
assessments "remain inspectable after revision", which is true for normal
completion, Stop and timeout, but not after a crash in that window.
*Demonstrated:* yes, `test_checkpoints_never_carry_the_provisional_revision`
(all checkpoint writes lack `revision` while the returned report has it) and
`test_reap_keeps_a_checkpointed_provisional_revision` (reaping preserves a
`revision` only if a writer put one there). *Confirmation of loss:* run the
scripted audit, stop after the `revising` checkpoint, restart, compare the
reaped report's `claims` with `initial_report`.

**H2 — `fragile` — `src/blackrose/citations.py::display_report`.**
`display_report` reads `revision['text']` and iterates `revision['claims']`
without validating the saved shape. A stored revision missing `text` raises
`KeyError`, which propagates out of `audit_payload`/`annotated_copy` into
transcript rendering instead of failing closed. Every current writer constructs
all three keys (`run` builds it; `run`'s exception paths and `src/blackrose/turns.py::TurnRunner._save_interrupted` copy it), so
this is reachable only from a corrupted or hand-migrated row. *Demonstrated:* no
— a test would have to seed a malformed row, which would pin the behavior I
propose to change.
*Confirmation:* write `{'claims': []}` as `revision` and call
`display_report`; observed `KeyError: 'text'`.

**H3 — `documentation-gap` — `src/blackrose/storage/repository.py::Repository.save_audit`.**
`save_audit` enforces `body_hash ⟷ content` only when `content` is passed. With
`content=None` a report bound to different bytes is accepted, so the identity
invariant depends on the caller rather than the writer. The only production
caller passes content (`src/blackrose/citations.py::CitationAudit.checkpoint`), and an accepted desync is still
normalized on display (invariant 3). *Demonstrated:* no (the test would enshrine
the gap). *Confirmation:* `save_audit(id, report_for_other_text)` on a pending
row then `audit_for` returns the mismatched hash. Proposed fix in
"Formalization decisions".

**H4 — `documentation-gap` — `src/blackrose/storage/repository.py::Repository.reap_pending`.**
`reap_pending` selects audits by `messages.state='pending'`, not by report
status. Transient statuses are cleaned up because settlement only ever writes
terminal ones (invariants 2, 9), not because recovery checks them. A terminal
row carrying `researching` would be displayed with a live-progress badge
forever; no current writer can produce that pairing. *Demonstrated:* no.
*Confirmation:* store a `researching` report on a `complete` message and render
the transcript — `audit_payload` returns the stored status unchanged.

**H5 — `documentation-gap` — `src/blackrose/citations.py::validate_assessment`, `src/blackrose/citations.py::CitationAudit.run`.**
`not_applicable` is decided by this reply's `registry.attempted` and the model's
`source_based` flag, not by the presence of inherited evidence. In a chat whose
earlier replies captured sources, a follow-up that declares `source_based=false`
is exempt from assessment even though the registry already holds snapshots
(`EvidenceRegistry.inherit` does not set `attempted`). This matches the documented rule
that rigor covers answers *dependent on* evidence and lets a genuinely
unrelated follow-up pass, but it means the untrusted model chooses its own
coverage. *Demonstrated:* yes,
`test_inherited_evidence_alone_allows_a_declined_assessment`. *Would confirm a
stronger rule is intended:* product decision, not code archaeology.

**H6 — `none-found` — `src/blackrose/citations.py::CitationAudit.run`, `src/blackrose/turns.py::TurnRunner._save_interrupted`.**
No path was found that sets `final_text`/`self.content` to a revision and then
restores the draft. `draft_text` is the single fallback source, and it is
updated before any await following the latch. Cancellation, timeout and
settlement each produce an `incomplete` report whose hash matches the stored
text (tests under `AuditPhaseTests`).

**H7 — `none-found` — `src/blackrose/citations.py::reconcile_claims`, `src/blackrose/citations.py::validate_assessment`, `src/blackrose/citations.py::CitationAudit._review_contradictions`.**
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
   the `revising` checkpoint in `run` (and at the post-revision `checking`
   checkpoint so a crash during the final assessment
   keeps the pre-revision verdicts). Report positions already index
   `revision['text']`, so the read path needs no change; the display decision is
   whether live replay should show provisional badges before the final report.
   Regression test: full scripted run → assert the `revising` checkpoint
   contains `revision`; crash after it → reaped report keeps those claims.
2. **Fail closed on a malformed saved `revision` (H2).** In
   `src/blackrose/citations.py::display_report`, treat a revision that is not
   `{'text': str, 'body_hash': str, 'claims': list}` as absent and append a
   limitation, instead of raising `KeyError`. Test: a report with
   `revision={'claims': []}` yields an `incomplete` display report.
3. **Check identity in `save_audit` even without `content` (H3).** Compare
   `report['body_hash']` with the stored assistant content when `content is
   None` (`Repository.save_audit`). Test: a mismatched report is refused and
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
.venv/bin/python scripts/check_unit_tests.py -p test_citations.py
.venv/bin/python scripts/check_unit_tests.py -p test_citation_grading.py
.venv/bin/python scripts/check_unit_tests.py -p test_reply_checkpoints.py
```

The SM-07 tests are deterministic and offline: the audit-phase tests use a
scripted backend and an in-memory checkpoint recorder, and the storage tests use
a temporary `chats.db` under `tempfile.TemporaryDirectory()`. The live evaluation
(`scripts/evaluate_citations.py --live`) and the GTK/browser citation probes
(`scripts/check_gui_citations.py`, `scripts/check_citations.cjs`) exercise the
same machine against a real model and the shipped page.
