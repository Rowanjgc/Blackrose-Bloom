# SM-14: Skill package staging, review digest and enablement

## Scope and ownership

- `src/blackrose/skills.py`: `_digest` (38), `_read_entry` (42–48), `_parse`
  (51–66), `_package_files` (69–91), `_review` (94–101), `_valid_review`
  (104–109), `StagedSkill` (112–127), `SkillManager` (130–551): registry health
  (140–216), `_review_status` (218–222), `_path` (224–230), discovery
  (237–265), `stage_install` (267–364), `publish_install` (366–406), `install`
  (408–411), `review` (422–428), `approve` (430–442), `set_enabled` (444–456),
  `remove` (458–474), `read` (493–533), `catalog` (535–551).
- `src/blackrose/gui/bridge.py`: `Api._skill_import` (188), `skills` (615),
  `set_skill` (618), `remove_skill` (621), `_change_skill` (624–628),
  `review_skill` (630–633), `approve_skill` (635–637), `import_skill`
  (639–647), `finish_skill_import` (649–651), `_discard_skill_import`
  (653–657), `_finish_skill_import` (659–671), `_stage_skill_import`
  (673–709).
- `src/blackrose/config.py:75–85`: `write_json`, the only registry writer
  (mkstemp in the target directory, then `os.replace`).
- Coupled, not owned: `runtime.py:58` constructs the single
  `SkillManager(data_dir() / 'skills')`; `chat.py:718–719` runs `read_skill` as
  `asyncio.to_thread(self.skills.read, ...)`; `prompts.py:371, 387, 391` consume
  `catalog`, `list` and `read`; `main.py:83–90` installs bundled packages;
  `gui/static/bloom.js:2948` is the only page caller of `remove_skill`.

Ownership, per [architecture.md](../architecture.md#skill-and-animation-activation)
(136–165):

- **One manager per process, one directory on disk.** The package tree
  `<root>/<key>/` and two JSON files, `<root>/state.json` and
  `<root>/reviews.json`, are the durable state (`skills.py:131–136`). There is
  no database column and no cache: every `list`, `catalog`, `review` and `read`
  reads the files and re-hashes `SKILL.md`.
- **The loop thread owns writes.** `Api._change_skill` (bridge 624–628),
  `_finish_skill_import` (659–671) and `publish_install` run as plain functions
  submitted to the asyncio loop (`LoopThread.call/run`, bridge 58–65), so
  `set_enabled`, `approve`, `remove` and publication are serialized inside the
  app. `install-skill` is a second process (`main.py:83–90`) with no lock.
- **Worker threads only read.** `read_skill` (`chat.py:719`) and
  `review_skill` (`bridge.py:633`) call the manager from `asyncio.to_thread`
  while the loop serves page calls; staging (`stage_install`) also runs in a
  worker (`bridge.py:687`). The registry-health decision is therefore per read,
  not per process (I6).
- **The page and the model own nothing.** The page chooses a folder and a
  token; `approve` re-hashes the current `SKILL.md` and refuses a mismatch
  (`:435–436`); the model can only read what `catalog` and `read` already
  approve. Skill text is untrusted third-party content
  (module docstring 1–6; architecture 144–153).
- **Approval is a digest, not a switch.** `reviews.json` holds
  `key -> {digest, source, origin}` and `state.json` holds `key -> bool`; the
  two files are deliberately separate (`skills.py:135`). Only their conjunction
  makes a skill available (I2).

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `state.json` | `skills.py:134` | JSON object `str -> bool` | The user's switch per installed skill ID. | `publish_install:393`, `approve:441`, `set_enabled:456`, `remove:472`; only through `write_json` (atomic). |
| `reviews.json` | `skills.py:136` | JSON object `str -> record`; record = `{digest: 64-hex \| null, source: str \| null, origin: 'imported' \| 'bundled'}` (`_valid_review:104–109`) | The `SKILL.md` bytes the user approved. `digest: null` records an install that was never approved. | `publish_install:391`, `approve:439`, `remove:473`. |
| `<root>/<key>/SKILL.md` | `_path:224–230`, `_entry:413–420` | bytes ≤ 64 KB (`_read_entry:42–48`) | Identity, description and instructions; its SHA-256 is the approval subject. | The user, an import (`stage_install`), or any local process after install. |
| `StagedSkill.key` | `skills.py:114` | `str`, `[a-z0-9][a-z0-9_-]{0,79}` | Destination ID derived from frontmatter `name`, sanitized (`:357`). | `stage_install`. |
| `StagedSkill.digest` | `:119` | 64-hex `str` | SHA-256 of the staged `SKILL.md` at staging time (`:355, :361`). This is the value the approval is bound to at publication. | `stage_install` only. |
| `StagedSkill.package` | `:117` | `Path` inside a `TemporaryDirectory(dir=root, prefix='.import-')` (`:275–277`) | Private 0700 copy of the package; `package/` is one level below the discovery glob. | `stage_install`; cleaned by `publish_install:405–406` or `cleanup:126–127`. |
| `StagedSkill.metadata` | `:115` | `dict` (`name`, `description`) | Frontmatter parsed at staging time; returned by `publish_install:403`. | `stage_install`. |
| `SkillManager._last_state_warning` | `:137` | `str \| None` | Display/last-observation string for `list()` (`_warnings:215–216`); no longer decides a call's default (I6). | `_state:141–159`, `_warn_state:161–171`. |
| `SkillManager._last_review_warning` | `:138` | `str \| None` | Same, for `reviews.json`. | `_reviews:180–197`, `_warn_reviews:199–207`. |
| `_logged_state_warning`, `_logged_review_warning` | created by `getattr` at `:168, :204` | `str \| None` | Deduplicates the warning log line. | `_warn_*`. |
| `Api._skill_import` | `bridge.py:188` | `(token: str, staged: StagedSkill) \| None` | The one open import session; token is `secrets.token_hex(16)` (`:707`). | `_stage_skill_import:708`, `_discard_skill_import:655`, `_finish_skill_import:664`. |
| `.import-*` | `skills.py:275` | directory, mode 0700 | In-flight package copy; invisible to `*/SKILL.md` discovery. | `stage_install` / `TemporaryDirectory.cleanup`. |
| returned `enabled` | `:253` | `bool` | Derived only: `switched_on and review == 'approved'`. | `_records`; never persisted. |
| returned `review` | `:250`, `:427` | `'required' \| 'approved' \| 'changed'` | Derived from the record and the current digest. | `_review_status:218–222`. |

## States

**S1 — installed package** (per key, derived from disk). Mutually exclusive:

| State | Predicate |
| --- | --- |
| `MISSING` | no `<root>/<key>/SKILL.md` in `root.glob('*/SKILL.md')` (`:242`). |
| `PRESENT` | the entry exists, is not a symlink, and `_read_entry`/`_parse` succeed (`:246–253`). |
| `EXCLUDED` | `SKILL.md` or its package directory is a symlink; skipped entirely (`:243–244`). |
| `UNREADABLE` | the entry exists but `OSError`, `ValueError` (including size/UTF-8/frontmatter) or `yaml.YAMLError` is raised; listed as invalid, disabled (`:254–256`). |

**S2 — review status** (per key, `_review_status:218–222`). Total over filtered
records (`_reviews:192–196`) and a 64-hex current digest:

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
| `DEFAULT_ON` | key absent and the state read was healthy (`:251`) | yes |
| `UNCERTAIN_OFF` | key absent or non-boolean and the state read was degraded (`:251`) | no |

**S4 — effective availability** (per key). Mutually exclusive by construction:

| State | Predicate |
| --- | --- |
| `ENABLED` | `S1 == PRESENT` and `S2 == APPROVED` and `S3 ∈ {ON, DEFAULT_ON}` (`:250–253`; re-derived for access at `read:494–508`). |
| `DISABLED` | everything else. |

`UNREADABLE` and `EXCLUDED` can never be `ENABLED`; `CHANGED` and
`UNREVIEWED` outrank the switch.

**S5 — `state.json` health** (per read):

| State | Predicate | Grants returned |
| --- | --- | --- |
| `HEALTHY` | absent, or a JSON object with only boolean values (`:142–160`) | all, empty if absent |
| `PARTIAL` | a JSON object with at least one non-boolean value (`:153–159`) | only definite booleans |
| `UNREADABLE` | unreadable file, invalid JSON, or a non-object (`:146–152`) | none |

**S6 — `reviews.json` health** (per read): `HEALTHY` (all records valid or
absent), `PARTIAL` (invalid records dropped, `:192–196`), `UNREADABLE`
(unreadable/invalid JSON/non-object, `:185–191`).

**S7 — import session** (bridge, at most one):

| State | Predicate |
| --- | --- |
| `IDLE` | `_skill_import is None` and no staging task |
| `STAGING` | `_stage_skill_import` is awaiting the shielded `to_thread(stage)` (`:687–695`) |
| `STAGED` | `_skill_import == (token, staged)`; a review has been returned (`:707–708`) |
| `ENDED` | the token was consumed by a finish, replaced by a newer import, or the staging was cancelled/failed; the `StagedSkill` was cleaned |

**S8 — publication commit** (per `publish_install`): `STAGED → RENAMED`
(`:384`) `→ REVIEWS_COMMITTED` (`:391`) `→ STATE_COMMITTED` (`:393`); any
exception reaches `ROLLED_BACK` (`:394–402`). Process death between `RENAMED`
and `STATE_COMMITTED` leaves an `ORPHANED` package: it has no matching digest
(or only the user's genuine approval if reviews committed), so I2 keeps it
unavailable; see T2 and I9.

## Events

1. `import_skill()` — folder dialog (bridge 639–647); no window or no folder →
   cancelled with no session.
2. Staging completion, staging exception, or task cancellation
   (`_stage_skill_import:687–704`); each cleans the private tree.
3. `finish_skill_import(token, approve)` — publish or discard
   (`_finish_skill_import:659–671`).
4. A newer import request succeeds (`_discard_skill_import:706`) or fails
   (old session untouched).
5. `install(source, approved=..., origin=...)` — CLI/bundled or programmatic
   `stage_install` + `publish_install` (`:408–411`).
6. `set_enabled(key, bool)` (`:444–456`); `approve(key, digest)` (`:430–442`);
   `remove(key)` (`:458–474`).
7. `read(skill_id, path, offset)` from `read_skill` (`chat.py:719`).
8. Discovery: `list()`, `catalog()`, `review(key)` (`:259–265, 422–428, 535–551`).
9. External edit of `<root>/<key>/SKILL.md` (any process; no watcher).
10. External edit/repair/deletion of `state.json` or `reviews.json`; the warning
    text explicitly invites the user to repair them (`:163–166, :201–203`).
11. A registry write failure (ENOSPC, read-only filesystem, permissions).
12. Process death during or after packaging.
13. App/CLI startup: no migration and no recovery pass; state is read on
    demand (`__init__:131–138`).

## Transition table

### T1 — import session (S7, bridge)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | `import_skill`, `_window is None` | `:641–642` | `{'cancelled': True}`, no dialog | `IDLE` |
| `IDLE` | dialog returns nothing | `:644–646` | cancelled | `IDLE` |
| `IDLE` | staging requested while generating | `:675–676` | `ValueError`, no staging | `IDLE` |
| `IDLE` | staging requested | `:680, 687` | private `.import-` tree begins | `STAGING` |
| `STAGING` | worker returns staged + review | `:696–701, 706–708` | previous session discarded; token issued | `STAGED` |
| `STAGING` | worker raises | `:681–685, 696–701` | staged tree cleaned in `stage()`; error propagates | `IDLE` |
| `STAGING` | task cancelled (repeatedly) | `:689–695` | keep awaiting the unkillable thread; then `:702–704` cleanup + `CancelledError` | `IDLE` |
| `STAGED` | finish, token mismatch/absent | `:661–663` | `ValueError('expired')`; current session intact | `STAGED` |
| `STAGED` | finish, token ok, `approve is not True` | `:651, 666–670` | `staged.cleanup()`, `{'cancelled': True}` | `ENDED` |
| `STAGED` | finish, token ok, approve, generating | `:664–669` | token cleared, cleanup, `ValueError('Wait for the reply')` | `ENDED` |
| `STAGED` | finish, token ok, approve | `:664, 671` | token cleared before publish; `publish_install(approved=True)` | `PUBLISHED` / `ENDED` |
| `STAGED` | newer import succeeds | `:706–708` | old `cleanup()` then replace | `STAGED` (new token) |
| `STAGING` | newer import starts while one is pending | `:706` only after success | pending session survives a failed newer import | `STAGED` |

### T2 — publication (S8, `publish_install`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `STAGED` | publish | `origin not in ORIGINS` `:373–374` | `ValueError`; `finally` cleanup `:405–406` | `ROLLED_BACK` |
| `STAGED` | publish | this read's state or reviews degraded `:376–377` | `ValueError`; cleanup | `ROLLED_BACK` |
| `STAGED` | publish | destination exists or `key in state` `:379–380` | `ValueError`; cleanup | `ROLLED_BACK` |
| `STAGED` | publish, `approved=True` | current staged digest ≠ `staged.digest` `:381–382` | `ValueError`; cleanup | `ROLLED_BACK` |
| `STAGED` | rename | `:383–384` | `os.rename` of the whole directory (same filesystem) | `RENAMED` |
| `RENAMED` | reviews write | `:391` | `reviews[key] = {digest or null, source, origin}` | `REVIEWS_COMMITTED` |
| `RENAMED` | reviews write fails | `:394–402` | destination removed; nothing else written | `ROLLED_BACK` |
| `REVIEWS_COMMITTED` | state write | `:393` | `state[key] = approved` | `STATE_COMMITTED` |
| `REVIEWS_COMMITTED` | state write fails | `:394–402` | destination removed; previous reviews restored (`:398`), or logged if that also fails (`:399–401`) | `ROLLED_BACK` |
| `RENAMED`/`REVIEWS_COMMITTED` | process death | — | package remains with no/partial registry | `ORPHANED` (inert: review `required` or switch/approval present only if the user actually approved) |

### T3 — review status (S2)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNREVIEWED` | `publish_install(approved=True)` | digest verified `:381` | record digest = staged digest `:386` | `APPROVED` |
| `UNREVIEWED` | `publish_install(approved=False)` | — | record digest `None`, switch false `:386–388` | `UNREVIEWED` |
| `UNREVIEWED` | `approve(key, digest)` | state+reviews healthy `:432–433`; current bytes hash to `digest` `:434–436` | digest recorded, switch true `:438–441` | `APPROVED` |
| `UNREVIEWED`/`CHANGED` | `approve(key, wrong)` | digest mismatch `:435–436` | `ValueError`, no write | unchanged |
| `APPROVED` | `SKILL.md` edited | digest differs `:219–222` | none (derived) | `CHANGED` |
| `CHANGED` | `SKILL.md` restored | digest equals record | none (derived) | `APPROVED` |
| any | reviews record becomes malformed | `_valid_review` fails `:104–109, 192–196` | record dropped, warning | `UNREVIEWED` |
| any | `reviews.json` unreadable | `:185–191` | `{}` + warning | `UNREVIEWED` (all) |

### T4 — switch and availability (S3/S4)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | `set_enabled(key, True)` | `type(enabled) is bool` `:445`; `SKILL.md` exists `:447`; this read's state healthy `:449`; reviews healthy `:451`; status `approved` `:452–454` | `state[key] = True` `:455–456` | `ON` |
| any | `set_enabled(key, True)` | any guard above fails | `ValueError`; nothing written | unchanged |
| any | `set_enabled(key, False)` | bool, `SKILL.md` exists, state healthy | `state[key] = False` `:455–456`; reviews not needed | `OFF` |
| `DEFAULT_ON` | `state.json` deleted | `_state:142–144` returns `{}`, healthy | missing key defaults on `:251` | `DEFAULT_ON` (unchanged; H3) |
| `ON`/`DEFAULT_ON` | `SKILL.md` changes | derived `CHANGED` | switch unchanged, `enabled` false `:250–253`; `read` refuses `:504–508` | `DISABLED` |
| `ON`/`DEFAULT_ON` | state read becomes degraded | `:153–159` | definite `True` still honored, uncertain keys off `:251` | `ON` or `UNCERTAIN_OFF` |
| `ENABLED` | `read` | state on, digest approved `:494–508` | resource checks below | `ENABLED` |
| `ENABLED` | `read` with traversal/absolute path | resolved outside root `:511–513` | `ValueError` | `ENABLED` |
| `ENABLED` | `read` a symlinked component | `:515–517` | `ValueError` | `ENABLED` |
| `ENABLED` | `read` resource > 64 KB | `:531–532` | `ValueError` | `ENABLED` |
| `ENABLED` | `read` SKILL.md | `:526–530` | returns the already-digested bytes, not a second read | `ENABLED` |

### T5 — registry health and recovery (S5/S6)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `HEALTHY` | file unreadable/invalid | `:146–152`, `:185–191` | warning; `{}` | `UNREADABLE` |
| `HEALTHY` | some values invalid | `:153–159`, `:192–196` | warning; valid subset returned | `PARTIAL` |
| `HEALTHY`/`PARTIAL` | any mutator | `_require_valid_state:173–177`, `_require_valid_reviews:209–213` use **this read's** warning | `ValueError`; no write, no delete | unchanged |
| `PARTIAL` | read/list/catalog | `_records:239–251`, `read:494–508` | definite grants/records honoured; uncertain off | unchanged |
| `UNREADABLE`/`PARTIAL` | repair to valid JSON | next read | warning cleared, empty or full grants | `HEALTHY` |
| `UNREADABLE` | delete the file | next read | treated as empty and healthy `:142–144, :181–183` | `HEALTHY` |
| `PARTIAL` | concurrent repair by another thread | another read clears the shared display warning | the first read keeps its own warning (I6) | unchanged |

### T6 — removal

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `PRESENT` | `remove(key)` | valid key `:461`; state+reviews healthy `:459–460`; `SKILL.md` exists or key in either registry `:465` | `rmtree` `:467–468`; pop state `:471–472`; pop reviews `:473–474` | `MISSING` |
| `MISSING` + records | `remove(key)` (retry) | key in `state` or `reviews` `:465` | records cleared, nothing to delete | `MISSING` (clean) |
| `MISSING`, no records | `remove(key)` | `:465–466` | `ValueError('Skill not found')` | `MISSING` |
| `PRESENT` | `remove`, state write fails | `:472` | package gone, records remain | `MISSING` + stale records (retryable, I15) |
| `PRESENT` | `remove`, reviews write fails | `:474` | package+state cleared, stale review record | `MISSING`; a later install overwrites the record |
| `EXCLUDED`/any | `remove` | `_path:228–229` | `ValueError('Invalid skill path')` | unchanged (H5) |
| any | `remove` with degraded registry | `:459–460` | `ValueError` before any delete | unchanged |

### T7 — read access (`read`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | `read` | effective switch off `:494–496` | `ValueError('Skill is disabled')` | — |
| any | `read` | `SKILL.md` missing/symlink `:498–500` | `ValueError('Skill not found')` | — |
| any | `read` | review `changed` `:504–506` | `ValueError('changed after it was approved')` | — |
| any | `read` | review `required` `:507–508` | `ValueError('until the user reviews it')` | — |
| `ENABLED` | `read` directory | `:519–521` | paginated names only (`_resources:477–491`, ≤25, ≤2000 bytes) | — |
| `ENABLED` | `read` file | inside root, no symlink, ≤64 KB | text; `SKILL.md` from the digested read `:526–530`, others `:533` | — |

## Invariants

1. **The review label is total.** `_review_status` maps any filtered record
   (absent, `digest is None`, 64-hex) and any 64-hex current digest to exactly
   one of `required`, `approved`, `changed` (`:218–222`). Pinned by
   `test_every_switch_and_review_combination_has_one_named_state` and
   `test_review_status_never_returns_anything_else`.
2. **`enabled` is derived, never stored, and requires both the switch and the
   approval.** The only computation is `switched_on and review == 'approved'`
   (`:250–253`); `read` re-derives the same conjunction before serving anything
   (`:494–508`). A switch flag alone cannot enable unreviewed text. Pinned by
   `test_a_switch_written_without_a_review_does_not_enable_text` and the grid.
3. **Availability is recomputed from disk at every load.** There is no cache:
   `_records` re-reads both registries and re-hashes `SKILL.md` per call
   (`:239–253`); `catalog`, `list`, `PromptBuilder` (`prompts.py:371, 387, 391`)
   and `read_skill` all go through that path. Pinned by
   `test_approval_binds_the_exact_bytes_and_a_change_revokes_availability`
   and `tests/test_skills.py::SkillTests::test_availability_tracks_changes_even_with_preserved_timestamp`.
4. **Approval binds the exact `SKILL.md` bytes.** `approve` re-hashes the file
   read at approval time and refuses a mismatch (`:434–436`);
   `publish_install(approved=True)` re-hashes the staged file immediately before
   activation (`:381–382`) and persists the staging-time digest (`:386`), not a
   post-check re-read. Bytes changed between verification and activation are
   therefore recorded as `changed` and fail the load-time check. Pinned by
   `test_bytes_changed_between_digest_check_and_rename_stay_disabled`.
5. **A content change revokes availability without touching the switch.**
   `_review_status` reports `changed` (`:219–222`); `read` refuses
   (`:504–506`); restoring the approved bytes re-enables. Pinned by the same
   test, which asserts `state.json` still says `true` while the file is
   unreviewed.
6. **Registry degradation is per read, not shared state.** `_state` and
   `_reviews` return this read's grants/records and warning (`:140–159`,
   `:179–197`); every mutator raises from that warning
   (`_require_valid_state:173–177`, `_require_valid_reviews:209–213`), and
   discovery/read default missing keys from it (`:251`, `:495`). A concurrent
   repair cannot turn a degraded read into a grant or a partial rewrite. Pinned
   by the three `SkillRegistryConcurrencyTests`.
7. **A malformed registry fails closed for reads and blocks every mutation
   before any write or delete.** `list`/`catalog` show disabled skills plus the
   path; `read` refuses; `stage_install` (`:269`), `publish_install`
   (`:376–377`), `approve` (`:432–433`), `set_enabled` (`:449–451`) and
   `remove` (`:459–460`) all use a validating read first. Pinned by
   `SkillRegistryHealthTests` (six malformed state shapes and five malformed
   review shapes, package intact, file byte-identical).
8. **Registry files are replaced atomically.** `config.write_json:77–82` writes
   a temporary file in the target directory and `os.replace`s it; a reader sees
   the old or the new document, never a partial one, and a failed write leaves
   the old file byte-identical. Pinned by
   `test_remove_after_a_failed_state_write_is_retryable` (old content asserted)
   and both publication-failure tests.
9. **Publication is all-or-nothing for package and records within the
   process.** Order is rename → reviews → state (`:384–393`); any exception
   removes the destination (`:395`) and restores the previous reviews
   (`:396–401`). A failed state write cannot leave the package visible with a
   new approval, and a failed reviews write cannot leave a record. Pinned by
   `test_failed_state_write_removes_the_package_and_restores_the_review` and
   `test_failed_review_write_removes_the_package_and_leaves_no_record`.
10. **An import publishes disabled unless approved.** `reviews[key].digest` is
    `None` and `state[key]` is `False` for `approved=False` (`:386–388`).
    Pinned by `tests/test_skills.py::SkillReviewTests::test_import_installs_disabled_until_the_reviewed_text_is_approved`.
11. **Staging is private and invisible to discovery.** The copy lives in a 0700
    `.import-*` directory with the package one level below `*/SKILL.md`
    (`:275–277` vs `:242`), and every exit path removes it (`:362–364`,
    `:405–406`; bridge `:653–657, 681–685, 702–704`). Pinned by
    `test_a_staging_tree_is_private_and_invisible_to_discovery`,
    `test_nested_symlinks_are_refused_and_leave_no_staging`,
    `test_special_files_are_refused_and_leave_no_staging`, and
    `tests/test_gui_skill_import.py`.
12. **The import token is single-use, and only literal `True` approves.**
    `_finish_skill_import` compares the token and clears the session before
    publishing (`bridge.py:661–665`); `finish_skill_import` passes
    `approve is True` (`:651`). A wrong token leaves the session intact; a
    publish failure consumes it and cleans the staging tree. Pinned by
    `test_a_published_token_is_single_use`, `test_only_the_literal_true_approves`,
    `test_a_new_import_replaces_the_previous_session_and_its_staging`,
    `test_a_failed_publish_consumes_the_token_and_cleans_up_staging`, and
    `tests/test_gui_skill_import.py::…test_only_the_latest_review_token_can_publish`.
13. **Staged descriptor walks cannot traverse or follow links.** Every entry is
    opened relative to its parent directory fd with `O_NOFOLLOW`, its
    `(st_dev, st_ino)` is compared with the enumerated entry, and only regular
    files and directories are accepted (`:282–346`); totals are bounded at 1000
    entries and 20 MB (`:296–297, 326–327, 335–336`). Pinned by the nested
    symlink and FIFO tests plus
    `tests/test_skills.py::SkillTests::test_symlink_and_failed_copy_leave_no_partial_install`,
    `…test_ancestor_swapped_for_symlink_is_not_followed`,
    `…test_growth_during_copy_is_bounded_by_actual_read_bytes`.
14. **Skill IDs are never paths.** `_path` requires `[a-z0-9][a-z0-9_-]{0,79}`,
    refuses a symlink, and requires the resolved parent to be the managed root
    (`:224–230`); every entry point (`set_enabled`, `review`, `approve`,
    `remove`, `read`, `publish_install`, `stage_install`) calls it. Pinned by
    `test_skill_ids_that_are_paths_are_refused_by_every_entry_point`.
15. **Removal forgets both records and is retryable after a partial removal.**
    `remove` clears the state and review entries even when the package is
    already gone (`:465–474`), while an unknown key still refuses. Pinned by
    `SkillRemovalRecoveryTests`.
16. **No third-party text reaches the model without `ENABLED`.** `catalog`
    emits only `enabled` records inside the delimited JSON block
    (`:541–551`); prompt assembly uses `catalog`/`list`/`read`
    (`prompts.py:371–396`); `read_skill` re-checks. Pinned by
    `tests/test_skills.py::SkillPromptChannelTests` and
    `tests/test_skill_authority.py`.

## Hazards and open questions

1. **fragile (found, fixed) — `skills.py:465–466` (pre-fix `:462–463`).**
   `remove` required `<root>/<key>/SKILL.md` before it would clear the
   registries. If `write_json(state.json)` failed after `shutil.rmtree`, the
   package was gone but `state.json` still held `key: true`; a retry raised
   `Skill not found`, and `stage_install`/`publish_install` then refused the ID
   (`:359`, `:379`), so the skill ID was blocked until the user hand-edited
   `state.json`. Demonstrated: `test_remove_after_a_failed_state_write_is_retryable`
   and `test_remove_forgets_records_when_the_package_is_already_gone` both
   errored with `ValueError: Skill not found` before the fix. Fixed by clearing
   the records when they are the only thing left (I15).
2. **fragile — `skills.py:439–441`.** `approve` commits the review digest
   before the switch. If the state write fails, the approval is already durable;
   with no explicit flag and no `state.json`, `_records:251` defaults the switch
   on, so the skill is enabled even though `approve` raised. It is not a
   widening — the user approved exactly those bytes and the digest was recorded
   — but the reported failure is false. Characterized by
   `test_an_approval_whose_switch_write_fails_is_already_effective`. Would
   confirm with an injected `write_json` failure (done). A fix would need to
   report the partial commit or reorder the writes; reordering is safe but
   inverts the deliberate reviews-before-switch order, so it is left as a
   follow-up.
3. **fragile — `skills.py:142–144` with `:251`.** A missing `state.json` is
   "healthy with no explicit grants", and a missing key defaults the switch on.
   The recovery text (`:163–166`) tells the user to repair the file but not
   that *deleting* it re-enables every approved skill. Characterized by
   `test_deleting_state_json_restores_the_implicit_on_switch`. The current
   default is pinned by
   `tests/test_skills.py::SkillTests::test_missing_flag_defaults_on_only_for_reviewed_skills`;
   changing it is a user-visible decision, so no change here.
4. **fragile (found, fixed) — `skills.py:140–159, 179–197` with `:251, :495`
   (pre-fix).** The degraded-read decision used the shared
   `_last_state_warning`/`_last_review_warning`, while `read`/`review` run in
   worker threads (`chat.py:719`, `bridge.py:633`). A degraded read followed by
   another thread's repair+read cleared the flag; the first read then treated
   missing keys as on (`:251`) and a mutator could proceed with the partial
   dict, rewriting `state.json` with only the definite grants it saw (other
   flags vanish, then default on) or `reviews.json` with only the valid records
   it saw (other approvals vanish). All three interleavings were demonstrated
   against the pre-fix code: `test_list_does_not_reinterpret_a_degraded_state_read`
   (`enabled` was `True`), `test_a_mutation_cannot_write_a_partial_state_after_a_concurrent_repair`
   (`set_enabled` did not raise), and
   `test_approval_cannot_drop_other_records_after_a_concurrent_repair`
   (`approve` did not raise). Fixed by returning each read's own warning (I6).
5. **fragile — `skills.py:243–244` with `_path:228–229`.** A package directory
   whose entry or directory is replaced by a symlink is `EXCLUDED`: it is not
   listed, and `remove` refuses it as `Invalid skill path`, so the page
   (`bloom.js:2948` only removes listed rows) cannot clean it up. Excluding it
   is the security-correct choice; the operational dead end is real. No test;
   would confirm by symlinking `<root>/<key>` and observing an empty `list()`
   plus a refusing `remove`. Recommended follow-up: an "unmanageable entries"
   recovery action (delete a validated `root/<key>` symlink without following
   it) — wider than this bead.
6. **documentation-gap — `_review:94–101` with `_package_files:69–91` and
   `read:523–533`.** The review shows resource paths, kinds and sizes, but the
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
7. **fragile — `bridge.py:680–685` with `StagedSkill.review:122–124` and
   `publish_install:381–382`.** The review payload's digest is recomputed when
   shown, while publication checks the staging-time `staged.digest`. Inside the
   app both happen sequentially on one worker, so they agree; a same-user
   process that rewrites the staged file between them could show text T2, then
   restore T1 before publication, enabling T1 unseen. Same-user precondition,
   outside the privilege boundary, and the staging directory is 0700. Would
   confirm with a barrier around `StagedSkill.review` plus a swap. Proposed
   bridge patch (not applied): pass `review['digest']` into
   `publish_install(..., digest=...)` and refuse unless it equals both
   `staged.digest` and the current file.
8. **fragile — `skills.py:391–393, 439–441, 455–456, 471–474`.** Every registry
   mutation is read-modify-write with no lock. `blackrose install-skill`
   (`main.py:83–90`) can run as a second process while the app is open; the
   last writer's `os.replace` wins and silently drops the other process's keys
   (atomic replacement prevents corruption, not lost updates). Not tested (it
   needs two processes). Recommendation: a lock file around the read-modify-write,
   or a CLI refusal while an app owns the directory.
9. **documentation-gap — `_parse:51–66` with `main.py:93`.** Malformed YAML
   frontmatter raises `yaml.YAMLError`, which is not a `ValueError`.
   `_records:254` catches it and lists the skill as invalid/disabled, but
   `review`, `read` and `stage_install` let it propagate, and `install-skill`
   catches only `(OSError, ValueError, subprocess.SubprocessError)`, so that CLI
   prints a traceback instead of a message. Fails closed; error type only.
   Would confirm with `name: [a`. Deferred: it changes error types rather than
   state behavior.
10. **none-found — TOCTOU inside `read`.** `entry.is_symlink()` (`:499`) is
    checked before `_read_entry` opens the path (`:42–48`), and `target` is
    resolved (`:511`) before it is read (`:533`). A same-user swap can change
    which file is read, but `SKILL.md` is served from the bytes already digested
    (`:526–530`) and the package tree is user-owned; imports reject symlinks.
    The residual capability (read a different same-user file) is one the user
    already has. No test.
11. **none-found — `stage_install:270–273`.** A path to a regular file imports
    its *parent* directory. All in-app callers pass directories (FOLDER dialog
    `bridge.py:644`; bundled directory `main.py:89`), and the copy is bounded
    (1000 entries / 20 MB). Not reachable from the page without the user
    choosing a file.
12. **documentation-gap — `_last_*_warning` is display-only but shared.**
    After I6 the gate never depends on it, yet `list()` can show a warning that
    a concurrent `read` cleared, and `review`/`read` can clear it between a
    mutation's read and its error message. Cosmetic; no test.

## Formalization decisions

**Two source changes, both in `src/blackrose/skills.py` (this bead's file).**

1. **`remove` clears a partially removed skill's records (H1).** Previously it
   refused when `SKILL.md` was absent, so a registry write failure after the
   package deletion stranded `state.json`/`reviews.json` entries that blocked
   the same skill ID forever. Now `remove` proceeds when the key appears in
   either registry (`:465–474`), deletes the directory when one exists, and
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
  mutated between review and finish. Small bridge patch, but `bridge.py` is
  shared with other agents; recorded here as a follow-up.
- H5: a recovery action for `EXCLUDED` (symlinked) entries so the page can
  delete them without following them. Wider than this bead.
- `finish_skill_import` already validates the token before consuming it, clears
  the session before publishing, treats only literal `True` as approval and
  relies on `publish_install`'s `finally` for cleanup; no change is needed for
  I12.

**No change to `config.write_json`, `prompts.py`, `chat.py` or `main.py`:**

- Atomic replacement (`config.write_json:77–82`) already gives readers a
  consistent document and makes a failed write a no-op on disk; the two fixes
  above build on that.
- `PromptBuilder` and `ToolExecutor` already consume only `catalog`/`list`/
  `read`, so load-time enforcement is in the manager (I3, I16) and needs no
  caller change.
- The `install-skill` YAMLError case (H9) is an error-type polish, not a state
  defect.

## Verification

All commands from the repository root, offline, each unit run in its own
temporary XDG tree (`scripts/check_unit_tests.py`).

| Command | Result |
| --- | --- |
| `.venv/bin/python scripts/check_unit_tests.py -p test_sm_14_skill_lifecycle.py -v` | `Ran 28 tests in 0.058s` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_skills.py -q` | `Ran 31 tests in 0.159s` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_gui_skill_import.py -q` | `Ran 7 tests in 0.015s` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_skill_authority.py -q` | `Ran 1 test in 0.060s` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_prompts.py -q` | `Ran 3 tests in 0.020s` — `OK` |
| `.venv/bin/python scripts/check_unit_tests.py -p test_resources.py -q` | `Ran 5 tests in 0.006s` — `OK` |
| `.venv/bin/python -m ruff check src/blackrose/skills.py tests/test_sm_14_skill_lifecycle.py` | `All checks passed!` |
| `git diff --check` | no output (exit 0) |
| `.venv/bin/python scripts/check_unit_tests.py -q` (whole suite) | `Ran 1492 tests in 48.720s` — `OK (skipped=18)` |

Regression evidence, gathered by running the new tests before the two fixes:

- `test_remove_after_a_failed_state_write_is_retryable` and
  `test_remove_forgets_records_when_the_package_is_already_gone` errored with
  `ValueError: Skill not found` at `skills.py:463` (pre-fix).
- `test_list_does_not_reinterpret_a_degraded_state_read` failed with
  `AssertionError: True is not false` (the degraded read defaulted the switch
  on after another thread repaired the file).
- `test_a_mutation_cannot_write_a_partial_state_after_a_concurrent_repair` and
  `test_approval_cannot_drop_other_records_after_a_concurrent_repair` failed
  with `AssertionError: None is not an instance of <class 'ValueError'>`
  (the mutators proceeded).
- After the fixes all 28 tests pass, and the 47 pre-existing skill/prompt tests
  above still pass.

An earlier whole-suite run reported three failures in
`tests/test_sm_03_round_admission.py`, a file another agent was adding to this
shared worktree at the time. That file passes in isolation (`Ran 26 tests —
OK`) and the final whole-suite run above passes; the failures were not caused by
this change.

Not run: `scripts/check_health.py` (the whole-repo gate also runs CLI and
distribution probes unrelated to this change; the module suites above are the
relevant set) and the native/browser probes, which this manager-only change
does not touch. The cross-process lost-update hazard (H8) is unverified by
design: it needs two processes, and no deterministic single-process test can
pin it.
