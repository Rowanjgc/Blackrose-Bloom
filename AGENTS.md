# Agent instructions

This file points coding agents at the maintained project guidance. It adds no
policy of its own: if it ever disagrees with a linked document, the linked
document wins and this file should be corrected.

## Before changing code

- [Architecture and runtime ownership](docs/architecture.md): module ownership,
  invariants and trust boundaries.
- [Engineering standards](docs/engineering-standards.md), ratified 2026-11-05,
  especially [§10 Agent-assisted development](docs/engineering-standards.md#10-agent-assisted-development).
  Sections are labelled Established, Standard or Proposed; only the first two
  describe current expectations.
- [Contributing](CONTRIBUTING.md): checkout setup, where changes belong,
  dependencies and generated files. [CONTEXT.md](CONTEXT.md) defines domain
  terms.
- [Branches](CONTRIBUTING.md#branches): start each unit of work on a new branch
  from `main`.

## Verify a change

After [checkout setup](CONTRIBUTING.md#set-up-a-checkout), the offline health
gate is:

```sh
.venv/bin/python scripts/check_health.py
git diff --check
```

[Verify a change](CONTRIBUTING.md#verify-a-change) covers the native `--gui`
and browser probes, and when they apply. Live model and GPU probes are separate
and may send paid provider requests; read their documentation before running one.
