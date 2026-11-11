# Thinking Partner techniques

Use one technique when it helps the current question. None is a required
procedure.

## Assumption map

List what must be true for the idea to work. For each assumption, note:

- **Consequence if false:** fatal, costly, or minor.
- **Current support:** observed, reported by someone, inferred, or hoped.
- **Cheapest check:** what would confirm or refute it.

Address the fatal assumption with the weakest support first. Two or three
well-chosen assumptions beat a list of fifteen.

## Steelman, then test

Before criticizing, state the strongest version of the idea, including the
problem it solves well. Then test that version. This prevents critiques of a
straw proposal and shows the user that you understood it.

## Generate alternatives that differ in kind

When comparing options, include:

- **Do less:** the smallest change that captures most of the benefit.
- **Do nothing yet:** what waiting costs and what it would let you learn.
- **Change the frame:** a different way to meet the underlying goal, not just a
  different implementation of the proposal.

Options that differ only in degree rarely help the decision.

## Decision criteria

Ask, or infer and state, what matters most: cost, time, reversibility, risk,
learning, quality, or the user's own values. Make the weights explicit only when
the choice is genuinely close. Prefer reversible steps when uncertainty is high.
If criteria conflict on values, lay out the tradeoff and leave the weighing to the
user.

## Pre-mortem

"It is a year later and this failed. What is the most likely story?" Use it to
find failure paths the user has not named. Keep the stories concrete and
plausible, and attach each to an early warning sign.

## What would change my mind

For each major judgment, state the specific observation that would reverse or
weaken it. For example: "If more than a third of the pilot group stops using it
within two weeks, the onboarding cost is higher than this plan assumes." This
turns disagreement into something testable.

## Small experiments

A useful experiment has:

1. **Question:** the single uncertainty it resolves.
2. **Method:** the cheapest action that produces real signal (a prototype, a
   survey of five users, a spreadsheet model, a timed spike).
3. **Success signal and threshold,** decided before running it.
4. **Budget and stop condition:** time or money, and when to abandon it.
5. **Decision link:** what you will do differently depending on the result.

## Technical tradeoffs

For architecture and engineering choices:

- Name the forces: latency, throughput, consistency, operability, cost, team
  familiarity, migration path and failure modes.
- Ask what happens at ten times the expected load or data, and when a dependency
  fails or is slow.
- Separate reversible choices (internal module boundaries) from expensive ones
  (data formats, public APIs, storage engines).
- A **feasibility spike** answers one risky technical question with throwaway
  code in a bounded time, for example "can the parser handle our largest real
  file in under 2 s?". Define the input, the measurement and the threshold first.
- Benchmarks and anecdotes from other systems are evidence about those systems;
  say what would need to be true for them to transfer.
