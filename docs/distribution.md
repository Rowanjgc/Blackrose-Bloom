# Release archives and installed resources

Blackrose Bloom supports Python 3.12 and 3.13. Linux desktop runs still require
the GTK/WebKit system packages described in [Requirements](../README.md#requirements).
The wheel includes the desktop assets, Retriever package, bundled skill resources,
and pinned Motion Canvas compiler scripts/manifests. Model weights, Node.js, and
`node_modules` are installed separately.

Before upgrading durable data, take a [complete backup](recovery.md) and verify
the isolated restore drill. Chat schemas 1, 2 and 3 migrate to 4; Retriever schema 1
migrates to 2. The [populated chat compatibility matrix](../tests/fixtures/upgrades/README.md)
checks preserved identities/content, failure/retry at each pending step and
idempotent reopen. Retriever's populated upgrade preserves vectors and backfills
source-aware search in [its regression suite](../tests/test_retriever.py).
Older applications refuse newer schemas: restore the complete pre-upgrade
snapshot into an empty location or reinstall a compatible application, following
the recovery guide. Never downgrade schema metadata by hand.

## Build from a checkout

Before a release:

- Take a complete pre-upgrade snapshot and verify isolated restoration.
- Run health (including populated upgrades, saved-answer compatibility and the
  restore drill), browser probes, and the maintained native checks.
- Record successful remote jobs for the release commit; local evidence alone
  does not establish remote success.
- Build the source archive and its wheel, then run the isolated installed-wheel
  check below. Compatibility source tests do not replace archive verification.
- On a rollback, stop all writers and restore the complete pre-upgrade snapshot
  into an empty location as described in [recovery](recovery.md#restore-into-isolation-first).
  Keep the newer data separately; an older application must refuse its schema.

```sh
uv sync --locked
npm ci --prefix animation-runtime
npm run build --prefix animation-runtime
npm ci --prefix ui-runtime
npm run build --prefix ui-runtime
npm ci --prefix diagram-runtime
npm run build --prefix diagram-runtime
uv build --out-dir artifacts/dist
.venv/bin/python scripts/check_distribution.py artifacts/dist
```

`uv build` creates a source archive, then builds the wheel from that archive.
The wheel also includes the local interactive UI bundle, shared schema/prompts,
CSS and third-party notices. Semantic UI replay requires no Node runtime or CDN.
See [interactive answers](interactive-ui.md) for experimental feature flags.
Mermaid diagrams use a bundled sandbox renderer and passive SVG images; rendering
requires no Node runtime or network. Both archives include its integrity manifest
and collected dependency notices. See [diagrams](diagrams.md).

The build hook refuses release archives missing the generated browser runtimes,
third-party notices, or compiler export map. Editable development installs do not
require those generated files. Do not reuse an output directory containing other
release versions for the distribution check; it requires one wheel and one source
archive.

The source archive includes the generated files so rebuilding its wheel needs
Python build tools only. The wheel maps skill resources and compiler files into
`blackrose/_bundled/` using Hatch's
[file inclusion configuration](https://hatch.pypa.io/dev/config/build/#forced-inclusion).
Both archives preserve the project license, third-party notices, skill license,
and font notices. Temporary caches, `node_modules`, and scratch output are excluded.

The distribution check installs the wheel without the heavyweight model/GUI
dependencies in a fresh virtual environment outside the checkout. It installs
cached YAML/JSON validation dependencies, prepares the actual Node compiler,
imports a bundled skill, compiles the gear example, and creates its standalone
player document. Python installation is offline; npm may fetch the pinned Node
packages. No model inference, graphical windows, or paid requests are involved.
This verifies package/resource boundaries; the separate offline and WebKit CI
jobs verify application behavior and native rendering. The distribution check
also removes generated Node build files from a temporary source copy and confirms
that an editable Python install still succeeds.

## Install a built wheel

For example, using an isolated environment and the CPU PyTorch packages:

```sh
uv venv --python 3.13 /tmp/bloom-install
uv pip install --python /tmp/bloom-install/bin/python --torch-backend cpu artifacts/dist/blackrose_bloom-0.2.0-py3-none-any.whl
/tmp/bloom-install/bin/blackrose install-skill explanatory-animations
/tmp/bloom-install/bin/blackrose setup-animations
/tmp/bloom-install/bin/blackrose
```

The full install needs the system development packages required by GTK bindings.
Node.js 22+ and npm must be available when running `setup-animations`; this command
explicitly downloads the locked compiler dependencies. It does not run on app
startup. The same command works in a source checkout, where it installs and builds
`animation-runtime/` in place.

For a wheel, the compiler is staged atomically under
`$XDG_CACHE_HOME/blackrose-bloom/animation-runtime/<content-hash>/` (default
`~/.cache/blackrose-bloom/animation-runtime/`). Package files remain read-only.
An interrupted setup never publishes its partial staging directory. A package
update changing compiler resources selects a new cache; run setup again for that
version. If the cache is cleared, rerun the command before authoring animations.

`blackrose install-skill <id>` copies one bundled skill into the managed user-data
directory and records it as approved: bundled skills are reviewed as part of the
application source, and naming one is the explicit choice. Run it without an ID to
see available choices. It refuses to overwrite
an existing skill; use the existing app controls to remove/import an updated copy
when intended. Installing a wheel does not enable skills automatically.
The release check requires every file under `skills/` in the wheel and installs
the `research-synthesis` starter skill as well as `explanatory-animations`.

The wheel also contains the optional `numeric-summary` example and trusted script
launcher. `blackrose install-skill numeric-summary` approves only its instructions;
execution requires a separate native permission in Skills. System Python 3.12/3.13,
Bubblewrap and libseccomp remain optional OS dependencies, supplied separately
from the wheel. No installation command executes package scripts or installs
their dependencies. See [skill script setup and limits](skill-scripts.md).

The source-checkout launcher installer remains `scripts/install_desktop.py`.
Wheel users can run the installed `blackrose` entry point directly.
