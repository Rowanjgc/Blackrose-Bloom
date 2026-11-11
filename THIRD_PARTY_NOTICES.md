# Licenses and provenance

Blackrose Bloom's project code and documentation are available under the
[MIT license](LICENSE), copyright 2026 Azalea Evergreen. Separately attributed
components retain the terms and notices below.

## Project origins

- BlackroseAI: the author's earlier project, base commit
  `c5c69c89254cde8db2f42a679932a98e53801580`. Its original `pyproject.toml`
  declares MIT. The fork also incorporated then-current working-tree changes,
  as recorded in [the user guide](docs/user-guide.md#source-provenance).
- Retriever: the author's earlier project, base commit
  `1c3d85e40574e9f87d85151cbfc079c2e187fd5f`, plus the working-tree integration.
  No upstream license declaration was found. The author of both projects released
  them as open source; this fork applies MIT to that project code.
- Dragon Rose artwork: `src/blackrose/gui/static/blackrose-bloom.png` and
  `blackrose-bloom-logo.png`. The artwork was generated with
  ChatGPT, and its author shares it under MIT. The original
  icon was introduced in `6c10d3b`; the transparent logo in `019c7bb`.

## Bundled skill

The Ian/Xiaohei illustration instructions derive from
[tojileon/ian-xiaohei-illustrations-en](https://github.com/tojileon/ian-xiaohei-illustrations-en),
reviewed at local checkpoint `18280fc475fbda00efc0da6608860d69028deb6a`.
The source is MIT licensed, copyright 2026 Ian. This fork adapts the instructions
and resources for its host tools. The original full notice is preserved at
[skills/ian-xiaohei-illustrations/LICENSE](skills/ian-xiaohei-illustrations/LICENSE)
and travels with the skill in wheels and source distributions.

## Fonts

Libron v0.31 by Nico Verbruggen is distributed under the SIL Open Font License
1.1, with Reserved Font Name Libron. Its copyright record also identifies the
Readerly and Newsreader origins. Unmodified font files and their notices remain
together under `src/blackrose/gui/static/fonts/libron/`:

- [Source and version](src/blackrose/gui/static/fonts/libron/README.md)
- [Full license](src/blackrose/gui/static/fonts/libron/LICENSE)
- [Copyright and reserved name](src/blackrose/gui/static/fonts/libron/COPYRIGHT)

## JavaScript and Python dependencies

The Motion Canvas browser bundle is generated from the locked npm dependencies.
The build preserves inline license comments and writes
`src/blackrose/gui/static/motion-canvas-NOTICES.txt` from the license/notice files
of packages actually included in that bundle. Release archives include both
generated files. Motion Canvas itself is MIT licensed. The installed Node
compiler dependencies retain their own license files, including TypeScript's
Apache-2.0 notice and esbuild's MIT notice.

Playwright is a development dependency under Apache-2.0. It and Chromium are
installed separately for browser checks, not bundled in the application wheel.
Python dependencies are installed separately according to `uv.lock`; this
project's MIT license does not replace their individual terms. Model weights
are not included in release archives and retain their provider's terms.

## Interactive answer runtime

The optional interactive answer renderer bundles OpenUI React Lang and Lang Core
1.0.1 (MIT), React/React DOM 19.2.0 (MIT), Zod (MIT), and the transitive packages
listed in `ui-runtime/package-lock.json`. `ui-runtime/build.mjs` preserves inline
notices and collects the licenses of all included packages in
`src/blackrose/gui/static/ui-NOTICES.txt`. Release archives include that generated
notice alongside the runtime. Model-generated text is never bundled as code.

## Mermaid diagrams

The local diagram renderer bundles Mermaid 12.3.0 (MIT) and transitive packages
locked in `diagram-runtime/package-lock.json`. The build collects licenses for
the 65 packages actually included into `src/blackrose/gui/static/mermaid-NOTICES.txt`
and preserves inline notices. Release archives include those notices, the full
sandbox host, and an integrity manifest. No CDN or remote diagram service is used.
