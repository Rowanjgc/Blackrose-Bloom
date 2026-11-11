# Stabilization and architecture changes — 2026-10-15

The review at `eeef117` identified eleven primary reliability defects and several
narrower lifecycle issues. This change completes the bounded stabilization phase
and the targeted refactors that support it.

| Finding | Resolution | Maintained verification |
| --- | --- | --- |
| F01 Public fetches could target local endpoints | Resolve and validate public unicast addresses inside connection establishment, dial numeric IPs, retain HTTPS origin, disable environment proxies | `test_web.py`: loopback aliases, private/mixed DNS, reserved addresses, rebinding redirects, Host/SNI, timeout and cancellation |
| F02 Failed writes could commit later | Storage rolls back failed callbacks; multi-statement mutations have explicit transactions | `test_storage.py`: injected attachment failures and unrelated later commits |
| F03 Failed migrations left partial schemas/connections | DDL and schema version commit atomically; failed initialization closes the connection; future versions are refused | `test_storage.py`: failure/retry and future-schema cases |
| F04 Stop before saving lost drafts | Session retains request ownership and restores text/attachments when persistence did not occur | `test_session_lifecycle.py`: model-loading Stop, immediate Stop and close |
| F05 Concurrent imports bypassed limits | Validate and append on the session loop without yielding; revalidate the complete request before saving | `test_session_lifecycle.py`, `test_turns.py`: concurrent drops and aggregate admission |
| F06 Document evidence was silently sliced again | Prompt assembly includes the full accepted extraction, with an explicit extraction-limit disclosure | `test_turns.py`: evidence after character 24,000 |
| F07 Context guard ignored most request content | Admit the assembled request before saving and before every continuation, with output reserve | `test_turns.py`: oversized documents and tool evidence |
| F08 Cancellation stranded pending replies | Create request and pending reply atomically; settle and receive their IDs before cancellation cleanup | `test_storage.py`, `test_turns.py`: injected reply failure and repeated cancellation after commit |
| F09 A second desktop recovered another live reply | Enforce one desktop owner per database through startup and shutdown | `test_instance.py`: independent processes and rejection before runtime startup |
| F10 Image failure retry rules were advisory | Host-owned attempt/failure budget removes further execution after a failed effect | `test_turns.py`: repeated calls, validation repair and parallel image budget |
| F11 Cleanup failures lost completed images | Return completed artifacts with recovery/cancellation outcomes; save them before ending the turn; block unsafe inference and model selection | `test_image_recovery.py`, `test_turns.py`: release/reload failures and post-download cancellation |

Additional work completed:

- Retriever initializes vector schema and index identity coherently; legacy empty
  dimension mismatches are reported and safely repaired on import. Search embeds
  before opening its read snapshot, then rechecks identity in the actual snapshot.
- Retriever tests cover import, unchanged/revised sources, removal, rebuild,
  source preservation, provenance, citation opening and consistency of derived
  projections with a deterministic embedder.
- Session snapshots restore active reply text, reasoning, tool state, artifacts,
  draft attachments and busy controls after a page reload. Native GTK/WebKit
  smoke checks exercise an actual navigation while streaming.
- Document path imports retain their validated extraction rather than parsing
  twice. Controller reuse loads the conversation before reading its history.
- Modal backgrounds become inert, and reply completion has an accessible live
  announcement. The graphical smoke checks exercise these browser behaviors;
  they are not a full screen-reader audit.
- The growing send loop is separated into prompt assembly, tool execution and
  durable turn orchestration. Provider session/continuation details stay in the
  existing provider adapter. [Architecture](architecture.md) documents ownership.
- CI and `scripts/check_health.py` run the offline contracts, Ruff, targeted mypy,
  Node syntax and dependency-lock validation. Native graphical probes remain
  available through the optional `--gui` check.

The implementation retains the desktop technology, SQLite storage, native
Retriever, explicit provider selection and bounded data-only animation format.
No bulk data migration, model downloads, live inference or paid image generation
is required for the maintained tests. CI workflow execution still requires the
changes to be pushed to GitHub.

## Practical limits

Local validation: **184 tests pass**, up from the original 104. Ruff, targeted
mypy, JavaScript syntax, lockfile validation and diff whitespace checks pass.
All five native GTK/WebKit smoke scripts pass: attachments, generated images,
animations/export, provider selection and active-session reload/accessibility.

Context admission currently uses conservative text and image estimates; provider
tokenizers can make it more precise later. GPU memory thresholds remain the
existing measured local configuration. An unresolved image recovery failure
requires correcting that local process/memory condition and restarting the
desktop. The public transport has offline connection-level tests; no external
production endpoints are required to test its isolation policy.
