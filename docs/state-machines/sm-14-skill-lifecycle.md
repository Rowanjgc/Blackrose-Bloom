# SM-14: Skill package staging, review digest and enablement

## Scope and ownership

- `src/blackrose/skills.py`: `_digest`, `_read_entry`, `_parse`,
  `_package_files`, `_review`, `_valid_review`, `src/blackrose/skills.py::StagedSkill`,
  `src/blackrose/skills.py::SkillManager`: registry health (`_state`, `_reviews`,
  their `_warn_*` and `_require_valid_*` helpers), `_review_status`, `_path`,
  discovery (`_records`, `list`), `stage_install`, `publish_install`, `install`,
  `review`, `approve`, `set_enabled`, `remove`, `read`, `catalog`.
- `src/blackrose/gui/bridge.py`: `Api._skill_import` (set in `src/blackrose/gui/bridge.py::Api.__init__`),
  `skills`, `set_skill`, `remove_skill`, `_change_skill`, `review_skill`,
  `approve_skill`, `import_skill`, `finish_skill_import`, `_discard_skill_import`,
  `_finish_skill_import`, `_stage_skill_import`.
- `src/blackrose/config.py::write_json`: the only registry writer
  (mkstemp in the target directory, then `os.replace`).
- Coupled, not owned: `src/blackrose/runtime.py::build_runtime` constructs the single
  `SkillManager(data_dir() / 'skills')`; `src/blackrose/chat.py::ChatController._call_tool`
  runs `read_skill` as `asyncio.to_thread(self.skills.read, ...)`;
  `src/blackrose/prompts.py::PromptBuilder.build` consumes `catalog`, `list` and
  `read`; `src/blackrose/main.py::main` (`install-skill`) installs bundled packages;
  `src/blackrose/gui/static/bloom.js::openSkills` is the only page caller of
  `remove_skill`.

Ownership, per [architecture.md](../architecture.md#skill-and-animation-activation):

- **One manager per process, one directory on disk.** The package tree
  `<root>/<key>/` and two JSON files, `<root>/state.json` and
  `<root>/reviews.json`, are the durable state (`src/blackrose/skills.py::SkillManager.__init__`). There is
  no database column and no cache: every `list`, `catalog`, `review` and `read`
  reads the files and re-hashes `SKILL.md`.
- **The loop thread owns writes.** `src/blackrose/gui/bridge.py::Api._change_skill`,
  `src/blackrose/gui/bridge.py::Api._finish_skill_import` and `publish_install` run as plain functions
  submitted to the asyncio loop (`src/blackrose/gui/bridge.py::LoopThread.call`,
  `src/blackrose/gui/bridge.py::LoopThread.run`), so
  `set_enabled`, `approve`, `remove` and publication are serialized inside the
  app. `install-skill` is a second process (`src/blackrose/main.py::main`) with no lock.
- **Worker threads only read.** `read_skill`
  (`src/blackrose/chat.py::ChatController._call_tool`) and `review_skill`
  (`src/blackrose/gui/bridge.py::Api.review_skill`) call the manager from `asyncio.to_thread`
  while the loop serves page calls; staging (`stage_install`) also runs in a
  worker (`src/blackrose/gui/bridge.py::Api._stage_skill_import`). The registry-health decision is therefore per read,
  not per process (I6).
- **The page and the model own nothing.** The page chooses a folder and a
  token; `approve` re-hashes the current `SKILL.md` and refuses a mismatch
  (`src/blackrose/skills.py::SkillManager.approve`); the model can only read what `catalog` and `read` already
  approve. Skill text is untrusted third-party content (the `skills.py` module
  docstring; [skill and animation activation](../architecture.md#skill-and-animation-activation)).
- **Approval is a digest, not a switch.** `reviews.json` holds
  `key -> {digest, source, origin}` and `state.json` holds `key -> bool`; the
  two files are deliberately separate (the comment in `src/blackrose/skills.py::SkillManager.__init__`). Only their
  conjunction makes a skill available (I2).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `state.json` | `src/blackrose/skills.py::SkillManager.__init__` | JSON object `str -> bool` | The user's switch per installed skill ID. | `publish_install`, `approve`, `set_enabled`, `remove`; only through `write_json` (atomic). |
| `reviews.json` | `src/blackrose/skills.py::SkillManager.__init__` | JSON object `str -> record`; record = `{digest: 64-hex \| null, source: str \| null, origin: 'imported' \| 'bundled'}` (`_valid_review`) | The `SKILL.md` bytes the user approved. `digest: null` records an install that was never approved. | `publish_install`, `approve`, `remove`. |
| `<root>/<key>/SKILL.md` | `_path`, `_entry` | bytes ≤ 64 KB (`_read_entry`) | Identity, description and instructions; its SHA-256 is the approval subject. | The user, an import (`stage_install`), or any local process after install. |
| `StagedSkill.key` | `src/blackrose/skills.py::StagedSkill` | `str`, `[a-z0-9][a-z0-9_-]{0,79}` | Destination ID derived from frontmatter `name`, sanitized (`stage_install`). | `stage_install`. |
| `StagedSkill.digest` | `src/blackrose/skills.py::StagedSkill` | 64-hex `str` | SHA-256 of the staged `SKILL.md` at staging time (`stage_install`). This is the value the approval is bound to at publication. | `stage_install` only. |
| `StagedSkill.package` | `src/blackrose/skills.py::StagedSkill` | `Path` inside a `TemporaryDirectory(dir=root, prefix='.import-')` (`stage_install`) | Private 0700 copy of the package; `package/` is one level below the discovery glob. | `stage_install`; cleaned by `publish_install` or `cleanup`. |
| `StagedSkill.metadata` | `src/blackrose/skills.py::StagedSkill` | `dict` (`name`, `description`) | Frontmatter parsed at staging time; returned by `publish_install`. | `stage_install`. |
| `SkillManager._last_state_warning` | `src/blackrose/skills.py::SkillManager.__init__` | `str \| None` | Display/last-observation string for `list()` (`_warnings`); no longer decides a call's default (I6). | `_state`, `_warn_state`. |
| `SkillManager._last_review_warning` | `src/blackrose/skills.py::SkillManager.__init__` | `str \| None` | Same, for `reviews.json`. | `_reviews`, `_warn_reviews`. |
| `_logged_state_warning`, `_logged_review_warning` | created by `getattr` in `_warn_state`, `_warn_reviews` | `str \| None` | Deduplicates the warning log line. | `_warn_*`. |
| `Api._skill_import` | `src/blackrose/gui/bridge.py::Api.__init__` | `(token: str, staged: StagedSkill) \| None` | The one open import session; token is `secrets.token_hex(16)` (`_stage_skill_import`). | `_stage_skill_import`, `_discard_skill_import`, `_finish_skill_import`. |
| `.import-*` | `src/blackrose/skills.py::SkillManager.stage_install` | directory, mode 0700 | In-flight package copy; invisible to `*/SKILL.md` discovery. | `stage_install` / `TemporaryDirectory.cleanup`. |
| returned `enabled` | `_records` | `bool` | Derived only: `switched_on and review == 'approved'`. | `_records`; never persisted. |
| returned `review` | `_records`, `review` | `'required' \| 'approved' \| 'changed'` | Derived from the record and the current digest. | `_review_status`. |

## States

**S1 — installed package** (per key, derived from disk). Mutually exclusive:

| State | Predicate |
| --- | --- |
| `MISSING` | no `<root>/<key>/SKILL.md` in `root.glob('*/SKILL.md')` (`_records`). |
| `PRESENT` | the entry exists, is not a symlink, and `_read_entry`/`_parse` succeed (`_records`). |
| `EXCLUDED` | `SKILL.md` or its package directory is a symlink; skipped entirely (`_records`). |
| `UNREADABLE` | the entry exists but `OSError`, `ValueError` (including size/UTF-8/frontmatter) or `yaml.YAMLError` is raised; listed as invalid, disabled (`_records`). |

**S2 — review status** (per key, `src/blackrose/skills.py::SkillManager._review_status`). Total over filtered
records (`_reviews`) and a 64-hex current digest:

| State | Predicate | Code label |
| --- | --- | --- |
| `UNREVIEWED` | no record, an invalid record, or `record['digest'] is None` | `required` |
| `APPROVED` | `record['digest'] == sha256(current SKILL.md bytes)` | `approved` |
| `CHANGED` | a valid 64-hex digest that differs from the current bytes | `changed` |

**S3 — switch** (per key, from `state.json` and whether *this read* was
degraded):

| State | Predicate | Effective on? |
| --- | --- | --- |
| `ON` | `state[key] is True` | yes |
| `OFF` | `state[key] is False` | no |
| `DEFAULT_ON` | key absent and the state read was healthy (`_records`) | yes |
| `UNCERTAIN_OFF` | key absent or non-boolean and the state read was degraded (`_records`) | no |

**S4 — effective availability** (per key). Mutually exclusive by construction:

| State | Predicate |
| --- | --- |
| `ENABLED` | `S1 == PRESENT` and `S2 == APPROVED` and `S3 ∈ {ON, DEFAULT_ON}` (`_records`; re-derived for access at `read`). |
| `DISABLED` | everything else. |

`UNREADABLE` and `EXCLUDED` can never be `ENABLED`; `CHANGED` and
`UNREVIEWED` outrank the switch.

**S5 — `state.json` health** (per read):

| State | Predicate | Grants returned |
| --- | --- | --- |
| `HEALTHY` | absent, or a JSON object with only boolean values (`src/blackrose/skills.py::SkillManager._state`) | all, empty if absent |
| `PARTIAL` | a JSON object with at least one non-boolean value (`_state`) | only definite booleans |
| `UNREADABLE` | unreadable file, invalid JSON, or a non-object (`_state`) | none |

**S6 — `reviews.json` health** (per read): `HEALTHY` (all records valid or
absent), `PARTIAL` (invalid records dropped), `UNREADABLE`
(unreadable/invalid JSON/non-object); all decided in `src/blackrose/skills.py::SkillManager._reviews`.

**S7 — import session** (bridge, at most one):

| State | Predicate |
| --- | --- |
| `IDLE` | `_skill_import is None` and no staging task |
| `STAGING` | `_stage_skill_import` is awaiting the shielded `to_thread(stage)` (`src/blackrose/gui/bridge.py::Api._stage_skill_import`) |
| `STAGED` | `_skill_import == (token, staged)`; a review has been returned (`_stage_skill_import`) |
| `ENDED` | the token was consumed by a finish, replaced by a newer import, or the staging was cancelled/failed; the `StagedSkill` was cleaned |

**S8 — publication commit** (per `src/blackrose/skills.py::SkillManager.publish_install`, in its statement
order): `STAGED → RENAMED` `→ REVIEWS_COMMITTED` `→ STATE_COMMITTED`; any
exception reaches `ROLLED_BACK` (its `except`). Process death between `RENAMED`
and `STATE_COMMITTED` leaves an `ORPHANED` package: it has no matching digest
(or only the user's genuine approval if reviews committed), so I2 keeps it
unavailable; see T2 and I9.

## Events

1. `import_skill()` — folder dialog (`src/blackrose/gui/bridge.py::Api.import_skill`); no window or no folder →
   cancelled with no session.
2. Staging completion, staging exception, or task cancellation
   (`_stage_skill_import`); each cleans the private tree.
3. `finish_skill_import(token, approve)` — publish or discard
   (`_finish_skill_import`).
4. A newer import request succeeds (`_stage_skill_import` calls `_discard_skill_import`) or fails
   (old session untouched).
5. `install(source, approved=..., origin=...)` — CLI/bundled or programmatic
   `stage_install` + `publish_install` (`src/blackrose/skills.py::SkillManager.install`).
6. `set_enabled(key, bool)` (`src/blackrose/skills.py::SkillManager.set_enabled`); `approve(key, digest)`
   (`src/blackrose/skills.py::SkillManager.approve`); `remove(key)` (`src/blackrose/skills.py::SkillManager.remove`).
7. `read(skill_id, path, offset)` from `read_skill`
   (`src/blackrose/chat.py::ChatController._call_tool`).
8. Discovery: `list()`, `catalog()`, `review(key)` (`src/blackrose/skills.py::SkillManager.list`,
   `src/blackrose/skills.py::SkillManager.catalog`, `src/blackrose/skills.py::SkillManager.review`).
9. External edit of `<root>/<key>/SKILL.md` (any process; no watcher).
10. External edit/repair/deletion of `state.json` or `reviews.json`; the warning
    text explicitly invites the user to repair them (`_warn_state`, `_warn_reviews`).
11. A registry write failure (ENOSPC, read-only filesystem, permissions).
12. Process death during or after packaging.
13. App/CLI startup: no migration and no recovery pass; state is read on
    demand (`__init__`).

## Transition table

### T1 — import session (S7, bridge)

Guards are in `src/blackrose/gui/bridge.py::Api.import_skill`,
`src/blackrose/gui/bridge.py::Api._stage_skill_import`,
`src/blackrose/gui/bridge.py::Api.finish_skill_import` and
`src/blackrose/gui/bridge.py::Api._finish_skill_import`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | `import_skill`, `_window is None` | `import_skill`'s window check | `{'cancelled': True}`, no dialog | `IDLE` |
| `IDLE` | dialog returns nothing | `if not paths` | cancelled | `IDLE` |
| `IDLE` | staging requested while generating | `_stage_skill_import`'s `is_generating` check | `ValueError`, no staging | `IDLE` |
| `IDLE` | staging requested | `to_thread(stage)` task created | private `.import-` tree begins | `STAGING` |
| `STAGING` | worker returns staged + review | `staging.result()` succeeds, not cancelled | previous session discarded; token issued | `STAGED` |
| `STAGING` | worker raises | `stage()`'s `except`, then `staging.result()` re-raises | staged tree cleaned in `stage()`; error propagates | `IDLE` |
| `STAGING` | task cancelled (repeatedly) | the shielded wait loop sets `cancelled` | keep awaiting the unkillable thread; then cleanup + `CancelledError` | `IDLE` |
| `STAGED` | finish, token mismatch/absent | `secrets.compare_digest` fails or no session | `ValueError('expired')`; current session intact | `STAGED` |
| `STAGED` | finish, token ok, `approve is not True` | `finish_skill_import` passes `approve is True` | `staged.cleanup()`, `{'cancelled': True}` | `ENDED` |
| `STAGED` | finish, token ok, approve, generating | `is_generating` after the session is cleared | token cleared, cleanup, `ValueError('Wait for the reply')` | `ENDED` |
| `STAGED` | finish, token ok, approve | session cleared, then `publish_install` | token cleared before publish; `publish_install(approved=True)` | `PUBLISHED` / `ENDED` |
| `STAGED` | newer import succeeds | `_discard_skill_import` before the new token | old `cleanup()` then replace | `STAGED` (new token) |
| `STAGING` | newer import starts while one is pending | only after success | pending session survives a failed newer import | `STAGED` |

### T2 — publication (S8, `publish_install`)

All guards and effects are statements of
`src/blackrose/skills.py::SkillManager.publish_install`, in table order.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `STAGED` | publish | `origin not in ORIGINS` | `ValueError`; `finally` cleanup | `ROLLED_BACK` |
| `STAGED` | publish | this read's state or reviews degraded | `ValueError`; cleanup | `ROLLED_BACK` |
| `STAGED` | publish | destination exists or `key in state` | `ValueError`; cleanup | `ROLLED_BACK` |
| `STAGED` | publish, `approved=True` | current staged digest ≠ `staged.digest` | `ValueError`; cleanup | `ROLLED_BACK` |
| `STAGED` | rename | all checks above passed | `os.rename` of the whole directory (same filesystem) | `RENAMED` |
| `RENAMED` | reviews write | first `write_json` | `reviews[key] = {digest or null, source, origin}` | `REVIEWS_COMMITTED` |
| `RENAMED` | reviews write fails | the inner `except`, `reviewed` false | destination removed; nothing else written | `ROLLED_BACK` |
| `REVIEWS_COMMITTED` | state write | second `write_json` | `state[key] = approved` | `STATE_COMMITTED` |
| `REVIEWS_COMMITTED` | state write fails | the inner `except`, `reviewed` true | destination removed; previous reviews restored, or logged if that also fails | `ROLLED_BACK` |
| `RENAMED`/`REVIEWS_COMMITTED` | process death | — | package remains with no/partial registry | `ORPHANED` (inert: review `required` or switch/approval present only if the user actually approved) |

### T3 — review status (S2)

Guards are in `src/blackrose/skills.py::SkillManager.publish_install`,
`src/blackrose/skills.py::SkillManager.approve`,
`src/blackrose/skills.py::SkillManager._review_status` and
`src/blackrose/skills.py::SkillManager._reviews`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNREVIEWED` | `publish_install(approved=True)` | digest verified | record digest = staged digest | `APPROVED` |
| `UNREVIEWED` | `publish_install(approved=False)` | — | record digest `None`, switch false | `UNREVIEWED` |
| `UNREVIEWED` | `approve(key, digest)` | state+reviews healthy; current bytes hash to `digest` | digest recorded, switch true | `APPROVED` |
| `UNREVIEWED`/`CHANGED` | `approve(key, wrong)` | digest mismatch | `ValueError`, no write | unchanged |
| `APPROVED` | `SKILL.md` edited | digest differs | none (derived) | `CHANGED` |
| `CHANGED` | `SKILL.md` restored | digest equals record | none (derived) | `APPROVED` |
| any | reviews record becomes malformed | `_valid_review` fails | record dropped, warning | `UNREVIEWED` |
| any | `reviews.json` unreadable | `_reviews`' JSON/object checks | `{}` + warning | `UNREVIEWED` (all) |

### T4 — switch and availability (S3/S4)

Guards are in `src/blackrose/skills.py::SkillManager.set_enabled`,
`src/blackrose/skills.py::SkillManager._records` and
`src/blackrose/skills.py::SkillManager.read`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | `set_enabled(key, True)` | `type(enabled) is bool`; `SKILL.md` exists; this read's state healthy; reviews healthy; status `approved` | `state[key] = True` | `ON` |
| any | `set_enabled(key, True)` | any guard above fails | `ValueError`; nothing written | unchanged |
| any | `set_enabled(key, False)` | bool, `SKILL.md` exists, state healthy | `state[key] = False`; reviews not needed | `OFF` |
| `DEFAULT_ON` | `state.json` deleted | `_state` returns `{}`, healthy | missing key defaults on | `DEFAULT_ON` (unchanged; H3) |
| `ON`/`DEFAULT_ON` | `SKILL.md` changes | derived `CHANGED` | switch unchanged, `enabled` false; `read` refuses | `DISABLED` |
| `ON`/`DEFAULT_ON` | state read becomes degraded | `_state` returns a warning | definite `True` still honored, uncertain keys off | `ON` or `UNCERTAIN_OFF` |
| `ENABLED` | `read` | state on, digest approved | resource checks below | `ENABLED` |
| `ENABLED` | `read` with traversal/absolute path | resolved outside root | `ValueError` | `ENABLED` |
| `ENABLED` | `read` a symlinked component | any symlink between the root and the path | `ValueError` | `ENABLED` |
| `ENABLED` | `read` resource > 64 KB | `st_size > 64_000` | `ValueError` | `ENABLED` |
| `ENABLED` | `read` SKILL.md | `target == entry` | returns the already-digested bytes, not a second read | `ENABLED` |

### T5 — registry health and recovery (S5/S6)

Guards are in `src/blackrose/skills.py::SkillManager._state` and
`src/blackrose/skills.py::SkillManager._reviews`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `HEALTHY` | file unreadable/invalid |, | warning; `{}` | `UNREADABLE` |
| `HEALTHY` | some values invalid |, | warning; valid subset returned | `PARTIAL` |
| `HEALTHY`/`PARTIAL` | any mutator | `_require_valid_state`, `_require_valid_reviews` use **this read's** warning | `ValueError`; no write, no delete | unchanged |
| `PARTIAL` | read/list/catalog | `_records`, `read` | definite grants/records honoured; uncertain off | unchanged |
| `UNREADABLE`/`PARTIAL` | repair to valid JSON | next read | warning cleared, empty or full grants | `HEALTHY` |
| `UNREADABLE` | delete the file | next read | treated as empty and healthy | `HEALTHY` |
| `PARTIAL` | concurrent repair by another thread | another read clears the shared display warning | the first read keeps its own warning (I6) | unchanged |

### T6 — removal

All guards and effects are statements of
`src/blackrose/skills.py::SkillManager.remove` unless named.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `PRESENT` | `remove(key)` | valid key; state+reviews healthy; `SKILL.md` exists or key in either registry | `rmtree`; pop state; pop reviews | `MISSING` |
| `MISSING` + records | `remove(key)` (retry) | key in `state` or `reviews` | records cleared, nothing to delete | `MISSING` (clean) |
| `MISSING`, no records | `remove(key)` | no `SKILL.md` and key in neither registry | `ValueError('Skill not found')` | `MISSING` |
| `PRESENT` | `remove`, state write fails | `write_json(state_path)` raises | package gone, records remain | `MISSING` + stale records (retryable, I15) |
| `PRESENT` | `remove`, reviews write fails | `write_json(reviews_path)` raises | package+state cleared, stale review record | `MISSING`; a later install overwrites the record |
| `EXCLUDED`/any | `remove` | `_path` | `ValueError('Invalid skill path')` | unchanged (H5) |
| any | `remove` with degraded registry | `_require_valid_state`/`_require_valid_reviews` | `ValueError` before any delete | unchanged |

### T7 — read access (`read`)

All guards are statements of `src/blackrose/skills.py::SkillManager.read`, in
table order.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | `read` | effective switch off | `ValueError('Skill is disabled')` | — |
| any | `read` | `SKILL.md` missing/symlink | `ValueError('Skill not found')` | — |
| any | `read` | review `changed` | `ValueError('changed after it was approved')` | — |
| any | `read` | review `required` | `ValueError('until the user reviews it')` | — |
| `ENABLED` | `read` directory | `target.is_dir()` | paginated names only (`_resources`, ≤25, ≤2000 bytes) | — |
| `ENABLED` | `read` file | inside root, no symlink, ≤64 KB | text; `SKILL.md` from the digested read, others | — |

## Invariants

1. **The review label is total.** `_review_status` maps any filtered record
   (absent, `digest is None`, 64-hex) and any 64-hex current digest to exactly
   one of `required`, `approved`, `changed`. Pinned by
   `test_every_switch_and_review_combination_has_one_named_state` and
   `test_review_status_never_returns_anything_else`.
2. **`enabled` is derived, never stored, and requires both the switch and the
   approval.** The only computation is `switched_on and review == 'approved'`
   (`src/blackrose/skills.py::SkillManager._records`); `read` re-derives the same conjunction before serving
   anything (`src/blackrose/skills.py::SkillManager.read`). A switch flag alone cannot enable unreviewed text. Pinned by
   `test_a_switch_written_without_a_review_does_not_enable_text` and the grid.
3. **Availability is recomputed from disk at every load.** There is no cache:
   `_records` re-reads both registries and re-hashes `SKILL.md` per call
   (`_records`); `catalog`, `list`, `PromptBuilder` (`src/blackrose/prompts.py::PromptBuilder.build`)
   and `read_skill` all go through that path. Pinned by
   `test_approval_binds_the_exact_bytes_and_a_change_revokes_availability`
   and `tests/test_skills.py::SkillTests::test_availability_tracks_changes_even_with_preserved_timestamp`.
4. **Approval binds the exact `SKILL.md` bytes.** `approve` re-hashes the file
   read at approval time and refuses a mismatch (`approve`);
   `publish_install(approved=True)` re-hashes the staged file immediately before
   activation and persists the staging-time digest, not a post-check re-read
   (both in `publish_install`). Bytes changed between verification and activation are
   therefore recorded as `changed` and fail the load-time check. Pinned by
   `test_bytes_changed_between_digest_check_and_rename_stay_disabled`.
5. **A content change revokes availability without touching the switch.**
   `_review_status` reports `changed`; `read` refuses; restoring the approved bytes re-enables. Pinned by the same
   test, which asserts `state.json` still says `true` while the file is
   unreviewed.
6. **Registry degradation is per read, not shared state.** `_state` and
   `_reviews` return this read's grants/records and warning; every mutator
   raises from that warning
   (`_require_valid_state`, `_require_valid_reviews`), and
   discovery/read default missing keys from it (`_records`, `read`). A concurrent
   repair cannot turn a degraded read into a grant or a partial rewrite. Pinned
   by the three `SkillRegistryConcurrencyTests`.
7. **A malformed registry fails closed for reads and blocks every mutation
   before any write or delete.** `list`/`catalog` show disabled skills plus the
   path; `read` refuses; `stage_install`, `publish_install`, `approve`,
   `set_enabled` and `remove` all use a validating read first. Pinned by
   `SkillRegistryHealthTests` (six malformed state shapes and five malformed
   review shapes, package intact, file byte-identical).
8. **Registry files are replaced atomically.** `src/blackrose/config.py::write_json` writes
   a temporary file in the target directory and `os.replace`s it; a reader sees
   the old or the new document, never a partial one, and a failed write leaves
   the old file byte-identical. Pinned by
   `test_remove_after_a_failed_state_write_is_retryable` (old content asserted)
   and both publication-failure tests.
9. **Publication is all-or-nothing for package and records within the
   process.** Order is rename → reviews → state (`publish_install`); its
   `except` removes the destination and restores the previous reviews. A failed state write cannot leave the package visible with a
   new approval, and a failed reviews write cannot leave a record. Pinned by
   `test_failed_state_write_removes_the_package_and_restores_the_review` and
   `test_failed_review_write_removes_the_package_and_leaves_no_record`.
10. **An import publishes disabled unless approved.** `reviews[key].digest` is
    `None` and `state[key]` is `False` for `approved=False` (`publish_install`).
    Pinned by `tests/test_skills.py::SkillReviewTests::test_import_installs_disabled_until_the_reviewed_text_is_approved`.
11. **Staging is private and invisible to discovery.** The copy lives in a 0700
    `.import-*` directory with the package one level below `*/SKILL.md`
    (`stage_install` vs the glob in `_records`), and every exit path removes it
    (`stage_install`'s failure path, `publish_install`'s `finally`;
    `src/blackrose/gui/bridge.py::Api._discard_skill_import`, the `stage()` helper and the cancellation path in
    `src/blackrose/gui/bridge.py::Api._stage_skill_import`). Pinned by
    `test_a_staging_tree_is_private_and_invisible_to_discovery`,
    `test_nested_symlinks_are_refused_and_leave_no_staging`,
    `test_special_files_are_refused_and_leave_no_staging`, and
    `tests/test_gui_skill_import.py`.
12. **The import token is single-use, and only literal `True` approves.**
    `_finish_skill_import` compares the token and clears the session before
    publishing (`src/blackrose/gui/bridge.py::Api._finish_skill_import`); `src/blackrose/gui/bridge.py::Api.finish_skill_import`
    passes `approve is True`. A wrong token leaves the session intact; a
    publish failure consumes it and cleans the staging tree. Pinned by
    `test_a_published_token_is_single_use`, `test_only_the_literal_true_approves`,
    `test_a_new_import_replaces_the_previous_session_and_its_staging`,
    `test_a_failed_publish_consumes_the_token_and_cleans_up_staging`, and
    `tests/test_gui_skill_import.py::…test_only_the_latest_review_token_can_publish`.
13. **Staged descriptor walks cannot traverse or follow links.** Every entry is
    opened relative to its parent directory fd with `O_NOFOLLOW`, its
    `(st_dev, st_ino)` is compared with the enumerated entry, and only regular
    files and directories are accepted (`src/blackrose/skills.py::SkillManager.stage_install`'s descriptor walk);
    totals are bounded at 1000 entries and 20 MB
    (`src/blackrose/skills.py::_MAX_IMPORT_ENTRIES`,
    `src/blackrose/skills.py::_MAX_IMPORT_BYTES`). Pinned by the nested
    symlink and FIFO tests plus
    `tests/test_skills.py::SkillTests::test_symlink_and_failed_copy_leave_no_partial_install`,
    `…test_ancestor_swapped_for_symlink_is_not_followed`,
    `…test_growth_during_copy_is_bounded_by_actual_read_bytes`.
14. **Skill IDs are never paths.** `_path` requires `[a-z0-9][a-z0-9_-]{0,79}`,
    refuses a symlink, and requires the resolved parent to be the managed root
    (`src/blackrose/skills.py::SkillManager._path`); every entry point (`set_enabled`, `review`, `approve`,
    `remove`, `read`, `publish_install`, `stage_install`) calls it. Pinned by
    `test_skill_ids_that_are_paths_are_refused_by_every_entry_point`.
15. **Removal forgets both records and is retryable after a partial removal.**
    `remove` clears the state and review entries even when the package is
    already gone (`remove`), while an unknown key still refuses. Pinned by
    `SkillRemovalRecoveryTests`.
16. **No third-party text reaches the model without `ENABLED`.** `catalog`
    emits only `enabled` records inside the delimited JSON block
    (`src/blackrose/skills.py::SkillManager.catalog`); prompt assembly uses `catalog`/`list`/`read`
    (`src/blackrose/prompts.py::PromptBuilder.build`); `read_skill` re-checks. Pinned by
    `tests/test_skills.py::SkillPromptChannelTests` and
    `tests/test_skill_authority.py`.

## Hazards and open questions

1. **fragile (found, fixed) — `src/blackrose/skills.py::SkillManager.remove`, its not-found check (pre-fix).**
   `remove` required `<root>/<key>/SKILL.md` before it would clear the
   registries. If `write_json(state.json)` failed after `shutil.rmtree`, the
   package was gone but `state.json` still held `key: true`; a retry raised
   `Skill not found`, and `stage_install`/`publish_install` then refused the ID
   (`stage_install`, `publish_install`), so the skill ID was blocked until the user hand-edited
   `state.json`. Demonstrated: `test_remove_after_a_failed_state_write_is_retryable`
   and `test_remove_forgets_records_when_the_package_is_already_gone` both
   errored with `ValueError: Skill not found` before the fix. Fixed by clearing
   the records when they are the only thing left (I15).
2. **fragile — `src/blackrose/skills.py::SkillManager.approve`, its two writes.** `approve` commits the review digest
   before the switch. If the state write fails, the approval is already durable;
   with no explicit flag and no `state.json`, `_records` defaults the switch
   on, so the skill is enabled even though `approve` raised. It is not a
   widening — the user approved exactly those bytes and the digest was recorded
   — but the reported failure is false. Characterized by
   `test_an_approval_whose_switch_write_fails_is_already_effective`. Would
   confirm with an injected `write_json` failure (done). A fix would need to
   report the partial commit or reorder the writes; reordering is safe but
   inverts the deliberate reviews-before-switch order, so it is left as a
   follow-up.
3. **fragile — `src/blackrose/skills.py::SkillManager._state` with `_records`.** A missing `state.json` is
   "healthy with no explicit grants", and a missing key defaults the switch on.
   The recovery text (`_warn_state`) tells the user to repair the file but not
   that *deleting* it re-enables every approved skill. Characterized by
   `test_deleting_state_json_restores_the_implicit_on_switch`. The current
   default is pinned by
   `tests/test_skills.py::SkillTests::test_missing_flag_defaults_on_only_for_reviewed_skills`;
   changing it is a user-visible decision, so no change here.
4. **fragile (found, fixed) — `src/blackrose/skills.py::SkillManager._state`, `src/blackrose/skills.py::SkillManager._reviews` with
   `_records` and `read` (pre-fix).** The degraded-read decision used the shared
   `_last_state_warning`/`_last_review_warning`, while `read`/`review` run in
   worker threads (`src/blackrose/chat.py::ChatController._call_tool`,
   `src/blackrose/gui/bridge.py::Api.review_skill`). A degraded read followed by
   another thread's repair+read cleared the flag; the first read then treated
   missing keys as on (`_records`) and a mutator could proceed with the partial
   dict, rewriting `state.json` with only the definite grants it saw (other
   flags vanish, then default on) or `reviews.json` with only the valid records
   it saw (other approvals vanish). All three interleavings were demonstrated
   against the pre-fix code: `test_list_does_not_reinterpret_a_degraded_state_read`
   (`enabled` was `True`), `test_a_mutation_cannot_write_a_partial_state_after_a_concurrent_repair`
   (`set_enabled` did not raise), and
   `test_approval_cannot_drop_other_records_after_a_concurrent_repair`
   (`approve` did not raise). Fixed by returning each read's own warning (I6).
5. **fragile — `src/blackrose/skills.py::SkillManager._records` (the symlink skip) with `_path`.** A package directory
   whose entry or directory is replaced by a symlink is `EXCLUDED`: it is not
   listed, and `remove` refuses it as `Invalid skill path`, so the page
   (`src/blackrose/gui/static/bloom.js::openSkills` only removes listed rows) cannot clean it up. Excluding it
   is the security-correct choice; the operational dead end is real. No test;
   would confirm by symlinking `<root>/<key>` and observing an empty `list()`
   plus a refusing `remove`. Recommended follow-up: an "unmanageable entries"
   recovery action (delete a validated `root/<key>` symlink without following
   it) — wider than this investigation.
6. **documentation-gap — `_review` with `_package_files` and
   `read`.** The review shows resource paths, kinds and sizes, but the
   approval digest covers only `SKILL.md`; resource bodies are served later
   without any digest binding and may change after approval. This is the
   deliberate progressive-disclosure design (the module docstring binds
   approval to `SKILL.md`), but `_review`'s "the package's contents" overstates
   what is bound, and the review's `bytes` column is not re-checked on read.
   Only the importing user (or another local process) can change the installed
   tree; imports are copied once and reject symlinks, so it is not a remote
   path. Would confirm by editing a resource after approval and reading it.
   Recommendation: reword the docstring; binding every resource would break
   on-demand loading.
7. **fragile — `src/blackrose/gui/bridge.py::Api._stage_skill_import` (its `stage()` helper) with `src/blackrose/skills.py::StagedSkill.review` and
   `publish_install`.** The review payload's digest is recomputed when
   shown, while publication checks the staging-time `staged.digest`. Inside the
   app both happen sequentially on one worker, so they agree; a same-user
   process that rewrites the staged file between them could show text T2, then
   restore T1 before publication, enabling T1 unseen. Same-user precondition,
   outside the privilege boundary, and the staging directory is 0700. Would
   confirm with a barrier around `StagedSkill.review` plus a swap. Proposed
   bridge patch (not applied): pass `review['digest']` into
   `publish_install(..., digest=...)` and refuse unless it equals both
   `staged.digest` and the current file.
8. **fragile — the registry writes in `publish_install`, `approve`, `set_enabled` and `remove`.** Every registry
   mutation is read-modify-write with no lock. `blackrose install-skill`
   (`src/blackrose/main.py::main`) can run as a second process while the app is open; the
   last writer's `os.replace` wins and silently drops the other process's keys
   (atomic replacement prevents corruption, not lost updates). Not tested (it
   needs two processes). Recommendation: a lock file around the read-modify-write,
   or a CLI refusal while an app owns the directory.
9. **documentation-gap — `src/blackrose/skills.py::_parse` with `src/blackrose/main.py::main`.** Malformed YAML
   frontmatter raises `yaml.YAMLError`, which is not a `ValueError`.
   `_records` catches it and lists the skill as invalid/disabled, but
   `review`, `read` and `stage_install` let it propagate, and `install-skill`
   catches only `(OSError, ValueError, subprocess.SubprocessError)`, so that CLI
   prints a traceback instead of a message. Fails closed; error type only.
   Would confirm with `name: [a`. Deferred: it changes error types rather than
   state behavior.
10. **none-found — TOCTOU inside `read`.** `entry.is_symlink()` is
    checked before `_read_entry` opens the path, and `target` is
    resolved before it is read (all in `read`). A same-user swap can change
    which file is read, but `SKILL.md` is served from the bytes already digested
    and the package tree is user-owned; imports reject symlinks.
    The residual capability (read a different same-user file) is one the user
    already has. No test.
11. **none-found — `stage_install`.** A path to a regular file imports
    its *parent* directory. All in-app callers pass directories (the FOLDER dialog in
    `src/blackrose/gui/bridge.py::Api.import_skill`; the bundled directory in
    `src/blackrose/main.py::main`), and the copy is bounded
    (1000 entries / 20 MB). Not reachable from the page without the user
    choosing a file.
12. **documentation-gap — `_last_*_warning` is display-only but shared.**
    After I6 the gate never depends on it, yet `list()` can show a warning that
    a concurrent `read` cleared, and `review`/`read` can clear it between a
    mutation's read and its error message. Cosmetic; no test.

## Formalization decisions

**Two source changes, both in `src/blackrose/skills.py` (this investigation's file).**

1. **`remove` clears a partially removed skill's records (H1).** Previously it
   refused when `SKILL.md` was absent, so a registry write failure after the
   package deletion stranded `state.json`/`reviews.json` entries that blocked
   the same skill ID forever. Now `remove` proceeds when the key appears in
   either registry (`remove`), deletes the directory when one exists, and
   clears both entries. Unknown keys still raise `Skill not found`; a degraded
   registry still refuses before any delete; a non-directory path still
   refuses. Nothing widens: this path only removes grants for a package that is
   already gone. Regression tests:
   `test_remove_after_a_failed_state_write_is_retryable` (fails pre-fix) and
   `test_remove_forgets_records_when_the_package_is_already_gone`.
2. **Registry health is per read (H4).** `_state` and `_reviews` now return
   `(dict, warning | None)` instead of relying on the instance's last warning;
   `_warn_state`/`_warn_reviews` return the warning string;
   `_require_valid_state`/`_require_valid_reviews`, `_records`, `read` and
   `review` use the value from their own read. `_last_state_warning` and
   `_last_review_warning` remain for `list()`'s display text and the log
   dedupe. Single-threaded behavior is identical; the change removes the
   shared-flag race that could enable a skill from a degraded read or rewrite a
   partial registry. It never widens an approval: on the contrary, the
   interleavings that previously wrote now raise. Regression tests: the three
   `SkillRegistryConcurrencyTests`, all three of which fail against the pre-fix
   code.

**No source change proposed for the bridge (shared file):**

- H7: carry the reviewed digest into `publish_install` so the approved payload
  and the published bytes are the same value even if the private staging tree is
  mutated between review and finish. Small bridge patch, but `bridge.py` is a
  shared file; recorded here as a follow-up.
- H5: a recovery action for `EXCLUDED` (symlinked) entries so the page can
  delete them without following them. Wider than this investigation.
- `finish_skill_import` already validates the token before consuming it, clears
  the session before publishing, treats only literal `True` as approval and
  relies on `publish_install`'s `finally` for cleanup; no change is needed for
  I12.

**No change to `config.write_json`, `prompts.py`, `chat.py` or `main.py`:**

- Atomic replacement (`src/blackrose/config.py::write_json`) already gives readers a
  consistent document and makes a failed write a no-op on disk; the two fixes
  above build on that.
- `PromptBuilder` and `ToolExecutor` already consume only `catalog`/`list`/
  `read`, so load-time enforcement is in the manager (I3, I16) and needs no
  caller change.
- The `install-skill` YAMLError case (H9) is an error-type polish, not a state
  defect.

## Verification

Run from the repository root, offline. `scripts/check_unit_tests.py` runs each
unit in its own temporary XDG tree.

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_14_skill_lifecycle.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_skills.py -q
.venv/bin/python scripts/check_unit_tests.py -p test_gui_skill_import.py -q
.venv/bin/python scripts/check_unit_tests.py -p test_skill_authority.py -q
.venv/bin/python scripts/check_unit_tests.py -p test_prompts.py -q
.venv/bin/python scripts/check_unit_tests.py -p test_resources.py -q
.venv/bin/python -m ruff check src/blackrose/skills.py tests/test_sm_14_skill_lifecycle.py
```

`tests/test_sm_14_skill_lifecycle.py` pins the transitions and hazard fixes
above; the other suites cover the existing skill, import, authority, prompt and
resource behavior those fixes must preserve.

Regression evidence: before the two fixes, the new tests failed as follows.

- `test_remove_after_a_failed_state_write_is_retryable` and
  `test_remove_forgets_records_when_the_package_is_already_gone` raised
  `ValueError: Skill not found` from the not-found check in
  `src/blackrose/skills.py::SkillManager.remove`.
- `test_list_does_not_reinterpret_a_degraded_state_read` failed with
  `AssertionError: True is not false` (the degraded read defaulted the switch
  on after another thread repaired the file).
- `test_a_mutation_cannot_write_a_partial_state_after_a_concurrent_repair` and
  `test_approval_cannot_drop_other_records_after_a_concurrent_repair` failed
  with `AssertionError: None is not an instance of <class 'ValueError'>`
  (the mutators proceeded).

The cross-process lost-update hazard (H8) is unverified by design: it needs two
processes, and no deterministic single-process test can pin it.
