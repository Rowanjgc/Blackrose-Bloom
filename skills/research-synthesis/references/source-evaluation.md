# Source evaluation and synthesis

## Judging a source

Ask these questions quickly; note the answers that matter.

- **Kind of evidence:** primary data or experiment, systematic review,
  narrative review, official documentation or standard, expert commentary,
  journalism, marketing, forum post.
- **Origin:** is this the original source, or a report of one? Prefer the
  original when it is accessible, and read it when the claim matters.
- **Method:** sample size, controls, measurement, and whether the method can
  support the claim being made (correlation versus causation, lab versus field).
- **Currency:** publication date, software version or standard revision. For
  fast-moving topics, an old source may be accurate for its time and wrong now.
- **Incentives:** who funded or published it, and what they gain from the
  conclusion. Incentives are a reason to check, not proof of error.
- **Agreement:** does it fit with independent sources, and if not, why?

## Handling conflicting evidence

1. State each position and its best supporting source.
2. Check whether they really conflict: different definitions, populations,
   time periods or versions often explain apparent disagreement.
3. Compare method quality and directness to the question.
4. Say which position the evidence currently favors, how strongly, and what
   finding would change that.
5. If the conflict cannot be resolved with available sources, say so and leave
   it open.

## Technical sources

- For protocols, formats and APIs, prefer the specification or official
  documentation for the relevant version, then implementation source code or
  changelogs, then third-party articles.
- Benchmarks are evidence about a configuration; note hardware, versions and
  workload before generalizing.
- Claims in competing architecture write-ups often depend on unstated workloads
  or constraints; name them.

## Separating evidence from interpretation

Use wording that marks the layer:

- Evidence: "The 2023 trial found…", "The RFC requires…"
- Inference: "Taken together, this suggests…", "This likely means…"
- Assistant judgment: "My reading is…", "I would weigh X more heavily because…"
- Unknown: "None of the sources consulted measured…"

## Research brief outline

Use when the user wants a written brief. Adapt the sections to the question.

1. **Question and scope:** the question as investigated, and any
   interpretation chosen.
2. **Answer:** the conclusion in a few sentences, with confidence.
3. **Key findings:** each with inline citations.
4. **Where sources disagree:** positions, likely reasons, current weight.
5. **Limitations:** gaps, currency, source types not covered, avenues not
   checked.
6. **Open questions and next steps:** what would settle remaining uncertainty.
