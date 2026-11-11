# Dependency and CI maintenance

Use a **manual monthly review**, owned by the maintainer reviewing the change;
the next review is due **2026-11-17**. Also review before releases and when an
advisory affects a deployed path. Do not enable unattended merging. Keep the
dated disposition here with lock identities and verification; raw reports belong
in ignored `artifacts/` and must not include user data.

## Monthly procedure

1. Review `pyproject.toml`/`uv.lock`, all four npm manifests/locks, supported
   Python 3.12–3.13 and Node 22.19+ (CI uses Node 22), and upstream advisories.
   A package's presence alone does not establish exploitability. Record affected
   functionality, the chosen fix or reason for deferral, and the next review date.
2. Check npm's full locked trees, including build/development dependencies:

   ```sh
   mkdir -p artifacts/dependency-review
   npm audit --package-lock-only --json > artifacts/dependency-review/npm-root.json
   npm audit --package-lock-only --prefix animation-runtime --json > artifacts/dependency-review/npm-animation.json
   npm audit --package-lock-only --prefix ui-runtime --json > artifacts/dependency-review/npm-ui.json
   npm audit --package-lock-only --prefix diagram-runtime --json > artifacts/dependency-review/npm-diagrams.json
   ```

3. Audit the Python lock without loading models or downloading its large wheels:

   ```sh
   uv export --locked --all-groups --no-emit-project --no-hashes \
     --output-file artifacts/dependency-review/requirements.txt
   uv tool run pip-audit==2.10.1 \
     -r artifacts/dependency-review/requirements.txt --no-deps --disable-pip \
     --format json --output artifacts/dependency-review/python.json
   ```

   `pip-audit --locked` does not consume uv.lock in this tool version. The export
   supplies exact pins; the audit installs/resolves nothing. Review skipped pins
   and platform markers explicitly. PyPI does not index the `+cpu` torch wheels:
   for advisory matching only, copy the export and replace `torch==2.14.1+cpu`
   with `torch==2.14.1` and `torchvision==0.29.1+cpu` with `torchvision==0.29.1`
   (use the versions actually in that month's lock), then audit the copy with
   the same flags. Preserve the exact lock and CPU-index wheel hashes. This
   checks public-version advisories, not the CPU wheel's binary contents. The
   current audit covers active Linux/Python 3.13 markers; inspect other supported
   environments when their dependencies differ. A nonzero audit exit or missing
   report is not a clean audit.
4. Review every external action's full commit SHA and its upstream release.
   Resolve the tag from the original repository with `git ls-remote`, including
   `refs/tags/VERSION^{}` for annotated tags; verify the commit belongs to that
   upstream release before changing the pin. Keep a readable version comment.
   Current pins were resolved from checkout v4, setup-python v5, setup-node v4,
   upload-artifact v4 and setup-uv v6 on 2026-10-17. Follow
   [GitHub's full-SHA guidance](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions).
   All jobs have explicit deadlines and repository permissions remain read-only.
5. Make one bounded trial update. Review generated lock changes, including marker
   normalization, with its manifest. Use `uv sync --locked --inexact` in a shared
   development environment to preserve extra experiment tools. Rebuild all three
   trusted runtimes. Review generated schema/bundle hashes, Motion Canvas exports,
   third-party notices and [provenance](../THIRD_PARTY_NOTICES.md); generated
   resources are ignored but required in releases.
6. Run health, all browser probes, affected native probes under isolated Xvfb,
   and fresh archive/installed-wheel checks using [contributor setup](../CONTRIBUTING.md)
   and [release instructions](distribution.md). Record failures and follow-up
   fixes, not just the final green result. A local run does not verify GitHub
   branch protection, required checks or remote workflow success. Inspect those
   settings separately before relying on them.
7. Review the drift allowances that the health gate tolerates. Each should
   shrink or carry a reason that still holds:
   - `scripts/check_docs_baseline.json`: line-number references kept by documents
     that predate the rule. Record the total; a count that has not fallen for two
     reviews means the conversion has stalled (`DECISIONS.MD` 2026-11-11).

## 2026-10-17 review and trial update

Base commit: `7040328`; changes remain in the working tree. A
machine-readable record preserved lock and report hashes, package counts,
action pins and the disposition. Next review:
2026-11-17. Review the final diff before committing/releasing this checkpoint.

All three npm trees reported zero advisories. The original Linux Python export
had one finding, **CVE-2026-59951 / GHSA-84m7-p3x7-pcfv** in MCP 2.0.0. The
repository exposes [stdio tools](../src/local_knowledge/mcp_server.py), so the
affected stateful HTTP transport is not used. The
[upstream advisory](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-84m7-p3x7-pcfv)
lists 2.2.0 as fixed. The trial upgrades **mcp and mcp-types 2.0.0 → 2.2.0**;
other package versions are unchanged. uv also narrows platform markers for
existing macOS/Windows-only dependencies; `uv lock --check --offline` passes.
The final public-version audit covers 91 active dependencies, with zero findings
and no skipped packages after the explicit CPU-version mapping.

MCP 2.3.0, chardet 7.7.0, pypdf 6.21.1 and transformers 5.20.1 were available
but deferred to keep the security trial focused. Broader updates, including Pillow 13,
readability-lxml 0.9 and ReportLab 5 need separate extraction/rendering review.
No applicable remaining advisory was found for the retained active versions.
Reconsider these updates at the next monthly review; this is a dated advisory
snapshot, not a continuing vulnerability guarantee.

A deterministic real-SDK client check exercises tool discovery/read-only
annotations, synthetic search and citation opening without model inference.
Validation and local native limitations were recorded with that work. Current
remote jobs and repository settings were not inspected; H02 retains its remote
verification gate.

## 2026-10-30 advisory-triggered update: Certifi

Base commit: `3239e2d`; tracked as `bra-upf`. This review ran outside the monthly
schedule because the advisory affects every default-context outbound HTTPS
client: web fetching, cloud chat and remote images. **GHSA-6q48-6c7w-9gw2 /
PYSEC-2026-4320** covers Certifi releases before 2026.10.18, which ship the
`emSign ECC Root CA - G3` root. Eight intermediates under that root have
compromised keys. The
[upstream advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)
says 2026.10.18 removed all emSign roots and 2026.10.19 restored the roots that
are not affected. The update moves **certifi 2026.7.22 → 2026.10.23** with
`uv lock --upgrade-package certifi`; no other package version changes.
`pyproject.toml` now declares a direct floor of `certifi>=2026.10.19`, so
fresh wheel installs that resolve without `uv.lock` cannot pick an affected
bundle. OSV reports no advisories for 2026.10.23.

The bundle installed by `uv sync --locked --inexact` does not contain
`emSign ECC Root CA - G3`; `emSign Root CA - G1` remains, as upstream intended.
TLS verification is unchanged. `httpx.create_ssl_context()` still requires
certificates and checks hostnames. A live request to pypi.org succeeded, and a
self-signed endpoint was rejected. `scripts/check_health.py` passed.
Resulting `uv.lock` SHA-256: `6aecbd00c15b735410b081513fe0c58ef6fac608b0afdd32cfb445b8b63782df`.

The public-version audit (92 dependencies, none skipped) is clean for Certifi.
It reports open findings for filelock 4.2.0 (fixed 4.2.1), Pillow 12.3.0
(fixed 13.0.0, which is outside the current `<13` pin), pypdf 6.20.1 (fixed
6.21.x) and setuptools 84.0.0 (fixed 84.1.0). This update leaves them
untouched; each needs its own exposure review.

## 2026-10-30 advisory-triggered update: Pillow

Base commit: `c889d46`; tracked as `bra-siw`. Pillow 12.3.0 had 39 public
advisories (decoder memory-safety, bundled libopenjp2, parser DoS,
EPS/Ghostscript command construction), all fixed only in 13.0.0. The direct pin
moves from `pillow>=11,<13` to `pillow>=13,<14`, and
`uv lock --upgrade-package pillow` moves **Pillow 12.3.0 → 13.0.0** with no
other package version changes. ReportLab 4.5.1 and torchvision 0.29.1 declare
no Pillow upper bound and import cleanly against 13.0.0.

Extraction/rendering review: `src/` has no direct `PIL` import, and no
untrusted path reaches a Pillow decoder. Image attachments, local images and
generated images are recognised from fixed header bytes in
`blackrose/attachments/images.py` and are never decoded. That module limits
them to PNG, JPEG and WebP. PDF export escapes every model string before
ReportLab sees it, so model output cannot name an image, URL or font. PDF
attachments go through pypdf `extract_text` only; pypdf never extracts
embedded images, which is the step that would decode them with Pillow.
Retriever loads text-embedding models only, so torchvision never
decodes images. No new Pillow format restriction is needed. Treat any future
direct Pillow decode as a new exposure needing its own review, with formats
restricted explicitly via `Image.open(..., formats=...)`.

`scripts/check_health.py` passed. Resulting `uv.lock` SHA-256:
`fa7c5cbdb4f64a7fc0ac9aad94d47210f5c0b6e4a7f9164fdd82575d977c6d38`. The
public-version audit (92 dependencies, none skipped, `+cpu` suffixes stripped
for matching only) is clean for Pillow. Open findings remain for filelock 4.2.0,
pypdf 6.20.1 and setuptools 84.0.0, tracked as `bra-oid`, `bra-wc4` and
`bra-p22`.

## 2026-10-30 advisory-triggered update: pypdf

Base commit: `7e47b5d`; tracked as `bra-wc4`. pypdf 6.20.1 had 7 public
advisories: a crafted PDF reaching arbitrary image parsers (fixed 6.21.1),
memory exhaustion (fixed 6.21.0) and long runtimes (fixed 6.21.2). The direct
pin moves from `pypdf>=6,<7` to `pypdf>=6.21.2,<7`, so installs that resolve
without `uv.lock` cannot pick an affected release.
`uv lock --upgrade-package pypdf` moves **pypdf 6.20.1 → 6.21.4** with no other
package version changes.

Extraction review: both PDF paths call `extract_text` only and never extract
embedded images, so the image-parser advisory does not reach Pillow here.
Knowledge ingestion (`local_knowledge/extraction.py`) parses in a subprocess
worker with a 768 MiB address-space limit, 20 s of CPU and a 30 s timeout.
Chat attachments (`blackrose/attachments/documents.py:_extract_pdf`) parse
in-process with no time or memory bound, and the page limit applies only after
parsing. Moving that path to a bounded worker is tracked as `bra-12w`.
Encrypted, malformed and truncated PDFs still fail with the same
`InvalidDocument` and worker messages on both paths.

`scripts/check_health.py` passed. Resulting `uv.lock` SHA-256:
`5e13719542f940eb796fa3d184c08fec26bf711b40c42fe256a798bbb144b9e3`. pip-audit
reports no known vulnerabilities for pypdf 6.21.4. Open findings remain for
filelock 4.2.0 and setuptools 84.0.0, tracked as `bra-oid` and `bra-p22`.

## 2026-10-30 advisory-triggered update: filelock

Base commit: `b019477`; tracked as `bra-oid`. filelock 4.2.0 had 17 public
advisories, all fixed in 4.2.1. These include `UnixFileLock` truncating an
existing regular file. Most need another local user who can write to the lock
directory, or affect only soft, read-write or Windows locks. filelock is
transitive only, through huggingface-hub and torch; `src/` does not import it.
`uv lock --upgrade-package filelock` moves **filelock 4.2.0 → 4.2.5** with no
other package version changes. No manifest floor is added for a transitive
package.

Exposure review: filelock takes its locks in the model cache, which defaults to
`$XDG_CACHE_HOME/local-knowledge` (`~/.cache/local-knowledge`) and is owned by
the user with mode `0700` on this install. No other local user can plant files
there, so the multi-user advisories did not apply in practice. A `cache_dir`
configured on a shared or world-writable path would have been exposed.

`scripts/check_health.py` passed, and the pinned embedding model loaded offline
from the local cache and produced a 768-dimension query vector. Resulting
`uv.lock` SHA-256:
`c2ef686a9f52bc09d42588785b827068133cb9cdeb85882a6e3d0542342ed01c`. pip-audit
reports no known vulnerabilities for filelock 4.2.5.

## 2026-10-30 advisory-triggered update: setuptools

Base commit: `8a19d9c`; tracked as `bra-p22`. setuptools 84.0.0 had public
advisory GHSA-xmj3-9gf7-h52w, fixed in 84.1.0. On Windows,
attacker-controlled `.dist-info/namespace_packages.txt` content in a wheel can
cause harmful behaviour. `uv lock --upgrade-package setuptools` moves
**setuptools 84.0.0 → 84.2.1** with no other package version changes.

Exposure review: the bead assumed setuptools was installed only on macOS. That
is wrong. Both torch entries in `uv.lock` depend on it: the macOS build under
`sys_platform == 'darwin'`, and the `+cpu` build under
`sys_platform != 'darwin'`. The `+cpu` build ships `win_amd64` and `win_arm64`
wheels, so a locked Windows install did get the affected release. The project
build does not use setuptools: `[build-system]` requires only `hatchling`, and
CI runs on Ubuntu only. setuptools is a runtime dependency of torch here and
never handles third-party wheels, so the risk was low. A deferral would still
have rested on a false platform claim, so the lock is upgraded instead.

`scripts/check_health.py` passed. Resulting `uv.lock` SHA-256:
`1f6700c47798b3da93ba23555e678023f6e487485cc391ced1f2293c44eaf5e0`. The
public-version audit of the full exported lock (`+cpu` suffixes stripped for
matching only) reports no known vulnerabilities. The 2026-10-30 findings for
Pillow, pypdf, filelock and setuptools are all resolved.
