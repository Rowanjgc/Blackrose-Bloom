# Experiments

Evaluation harnesses kept beside the code they measure. They are linted with the
rest of the repository and listed here; `tests/test_script_index.py` fails when a
file is missing from this index. They are not part of the health gate. Results
are evidence for a dated record, not a current guarantee. Closed experiments move
to the maintainer's private records rather than staying here.

## Interactive answers

Measures whether opt-in interactive answers help, against plain Markdown. See
[interactive answers](../docs/interactive-ui.md#verification-and-remaining-gates) for the protocol and
limits. Live runs contact the configured provider and may send paid requests;
every run uses isolated storage.

| File | Purpose |
| --- | --- |
| [`evaluate.py`](interactive-ui/evaluate.py) | Live paired evaluation of interactive and Markdown answers |
| [`spontaneous.py`](interactive-ui/spontaneous.py) | Live evaluation where no prompt names the feature |
| [`usefulness.py`](interactive-ui/usefulness.py) | Bounded local usefulness audit on synthetic tasks |
| [`fidelity.py`](interactive-ui/fidelity.py) | Offline exact checks of answers against the supplied dataset |
| [`summarize.py`](interactive-ui/summarize.py) | Summarizes measured provider work from a run |
| [`review.cjs`](interactive-ui/review.cjs) | Offline gallery of captured outputs with the shipped renderer |
| [`usefulness.cjs`](interactive-ui/usefulness.cjs) | Offline task and geometry audit against the shipped page |
| [`usefulness-review.cjs`](interactive-ui/usefulness-review.cjs) | Renders every captured reply in the real transcript at both widths |
