# Contributing to Blackrose Bloom

## Set up a checkout

Use Python 3.13 (the version in `.python-version`), uv, and Node.js 22.19+.
Python 3.12 and 3.13 are supported and covered by the CI matrix.
Linux desktop runs also need GTK/WebKit and its development dependencies; see
[Requirements](README.md#requirements).

Run from the repository root:

```sh
uv sync --locked
npm ci --prefix animation-runtime
npm run build --prefix animation-runtime
npm ci --prefix ui-runtime
npm run build --prefix ui-runtime
npm ci --prefix diagram-runtime
npm run build --prefix diagram-runtime
```

The builds create the ignored Motion Canvas bundle, notices and export map, plus
the interactive-answer bundle, schema/prompt contract and notices, and the full
Mermaid sandbox host, integrity manifest and notices. These builds
are prerequisites for the full health suite, even when interactive answers are
disabled in the application. Repeat the affected build after changing its source,
dependencies or build script. Missing optional bundles leave ordinary chat
available; that fallback does not satisfy tests that directly read the generated
contract. See [distribution](docs/distribution.md) for wheel setup and release
archive verification.

## Where changes belong

| Location | Purpose |
| --- | --- |
| `src/blackrose/` | Desktop application, provider adapters, tools, and storage |
| `src/local_knowledge/` | Native Retriever library and CLI/MCP entry points |
| `src/blackrose/gui/static/` | Shipped page, styles, scripts, fonts, and artwork |
| `animation-runtime/` | Trusted Motion Canvas build and scene compiler |
| `ui-runtime/` | Trusted semantic-answer registry, generated contract, compiler and renderer |
| `diagram-runtime/` | Full local Mermaid renderer, sandbox host build and dependency notices |
| `skills/` | Bundled skill instructions and reference resources |
| `tests/` | Deterministic regression tests and browser interpreter fixtures |
| `scripts/` | Maintained development checks, installation, export, and audit tools |
| `docs/` | Maintained guides and explicitly preserved review evidence |

Read [architecture](docs/architecture.md) before changing module ownership.
The [engineering standards](docs/engineering-standards.md) set review expectations
for design, error handling, tests, security, documentation and refactoring.
[CONTEXT.md](CONTEXT.md) defines domain terms. Record architecture decisions in
`docs/adr/`. The maintainer's issue tracker, dated decision log and review
records are kept in a private companion repository; bead IDs such as `bra-6t9`
in the docs refer to it. The [documentation index](docs/README.md) lists the
current guides. [AGENTS.md](AGENTS.md) points coding agents at these
documents and the health gate below; `CLAUDE.md` imports it for Claude Code. It
links rather than restates, so change the linked document, not AGENTS.md.

## Verify a change

```sh
.venv/bin/python scripts/check_health.py
git diff --check
```

The health command's list in `scripts/check_health.py` is the source of what it
runs: offline unittests, the synthetic
[backup/restore drill](docs/recovery.md#repeatable-synthetic-drill), the
[documentation reference check](scripts/check_docs.py), Ruff, targeted mypy, page JavaScript syntax checks, the interactive-answer runtime tests and
Python lockfile validation. CI also installs and builds the locked Node
dependencies. Keep tests focused on behavior and
regressions. Follow existing file style; broad formatting changes belong in a
separate change.

To run some of the unittests, use the same runner with `unittest discover`
arguments, for example
`.venv/bin/python scripts/check_unit_tests.py -p test_local_images.py -v`. It
gives the tests empty scratch XDG directories and fails if any test leaves files
there. A test that saves preferences or data must point the XDG directories at
its own temporary root; run without this runner, such a test overwrites your
real settings.

Native desktop checks require a display and an explicit command:

```sh
.venv/bin/python scripts/check_health.py --gui
```

This uses scratch storage and scripted providers. The native probes it runs are
the `GUI_PROBES` list in `scripts/check_health.py`; CI runs the same list under
Xvfb. Routine health checks must not open desktop windows. To run just the native checks on a machine with Xvfb and dbus installed:

```sh
xvfb-run -a dbus-run-session -- .venv/bin/python scripts/check_health.py --gui-only --report-dir artifacts/native
```

Each command has a deadline. The report directory retains a separate output log
per command and a JSON result summary, including failures and timeouts. CI also
uploads the screenshots/HTML emitted by probes that provide them. A local pass
does not establish that the remote workflow has run successfully.

The optional growth and accessibility harnesses (`scripts/measure_housekeeping.py`,
`scripts/check_gui_housekeeping.py`) record synthetic scale and native AT-SPI
observations separately from routine health checks.

The browser probes are the `test:browser` script in the root `package.json`.
They use a locked development dependency at the repository root:

```sh
npm ci
npx --no-install playwright install chromium
npm run test:browser
```

CI installs Chromium's Linux dependencies with `--with-deps` and runs all browser
probes headlessly. The edit-review probe also runs `python3` (or `PYTHON`) with the
standard library to build its comparison. The pinned Playwright version selects its matching browser revision;
see the [browser installation reference](https://playwright.dev/docs/browsers).
Existing installations can still be supplied through `PLAYWRIGHT_MODULE` and
`CHROMIUM_PATH`. Optional `READING_ROOM_SCREENSHOTS` and
`MODEL_PICKER_SCREENSHOTS` directories capture review images. Interactive-answer
screenshots are written under `artifacts/browser/interactive-ui/`.

Live model/image probes are separate from offline and scripted GUI checks. Read
their module documentation and arguments before running them: they may contact
configured services, use the GPU, or send paid provider requests.

## Dependencies and generated files

Commit `uv.lock`, root `package-lock.json`,
`animation-runtime/package-lock.json`, `ui-runtime/package-lock.json`, and
`diagram-runtime/package-lock.json`.
Use uv/npm to update them, and review
manifest and lock changes together. Make upgrades explicit and
run the relevant checks afterward; do not regenerate locks as incidental cleanup.
See uv's [locking and syncing reference](https://docs.astral.sh/uv/concepts/projects/sync/)
for the distinction between `--locked` validation and dependency upgrades.

Keep virtual environments, `node_modules`, tool caches, generated runtime bundles,
and coverage output untracked. Store ad hoc screenshots, traces, and model output
in `/tmp` or the ignored root `artifacts/` directory. Promote only useful review
evidence into a named `docs/` directory with its purpose, date, reproduction
instructions, and limitations. Check the resulting diff for private content.

Local `.env` files and root `config.toml` are ignored. Keep shareable examples in
`config.example.toml` or `.env.example`; never put credentials in those examples.
Application databases, attachment stores, model caches, and credentials belong
in their configured user-data locations, outside the checkout.

Project code uses MIT. Keep the [third-party notices](THIRD_PARTY_NOTICES.md),
skill attribution, and font licenses with their resources when moving or shipping
them. Update the provenance record when introducing externally sourced assets.

Before committing, inspect `git status --short` and `git diff`. Keep changes
focused and include the relevant verification results in the review description.
Move or remove files only after updating their callers and documentation links.
Review completed TODO entries periodically and move lengthy historical notes into
the relevant dated record, preserving useful verification evidence.

## Branches

Start each unit of work from an up-to-date `main`, not from the branch you
were just on:

```sh
git switch main
git switch -c fix/<issue-id>-<short-description>
```

Keep one issue's work per branch. Before committing, check
`git branch --show-current` names the branch for that work. Branching from the
previous topic branch, or committing a second fix onto it, stacks unrelated
commits that then have to be split apart and re-verified before they can land.

## Maintenance and project memory

Record each completion's date, commit, commands, outcomes and limits in the
issue that tracked it. Keep historical counts and evaluations dated, record
consequential ownership or replay changes as ADRs, and keep current guides in
the [documentation index](docs/README.md). Check local file links and section anchors
when moving or adding documents. Raw experiment output stays in ignored artifacts;
preserve useful compact evidence and runnable reproduction instructions separately.

Review dependencies and CI [monthly](docs/dependency-maintenance.md), including all
four npm trees, the Python lock, full action pins, notices and runtime provenance.
Use the [troubleshooting guide](docs/troubleshooting.md) for logs, runtime/cache
repair and latched recovery errors. Release/remote gates remain explicit even
when every local check passes.
