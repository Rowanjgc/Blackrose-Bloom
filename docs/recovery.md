# Backup and recovery

During ordinary reply generation, Bloom checkpoints changed reply text about once
per second, coalescing chunks into one database write per tick. A paused stream's
latest text is included on the next tick. With a responsive loop and healthy
storage, newly accepted text waits at most roughly one timer interval, plus any
in-flight checkpoint, scheduling, queue and write time before it is committed.
The timer starts again after each write; this is a cadence, not a hard deadline
or a maximum number of bytes that can be lost. After a process crash, startup retains
the last committed prefix and marks the reply interrupted; it does not resume or
duplicate the turn. Text since that checkpoint can be lost. A busy database,
stalled application loop or failed write can extend this window; failed
checkpoints are logged and retried on the next tick. This protects against process
termination, not power loss. Citation-rigor replies retain their separate atomic
answer/report checkpoints.

Completion, Stop and provider errors drain any checkpoint in flight, then save all
accepted text with its terminal state. Finalized replies reject late checkpoints.
The deterministic regression in `tests/test_reply_checkpoints.py` kills a synthetic
streaming subprocess, reopens its scratch database twice, and checks the saved
prefix, interruption label, turn identity and checkpoint cadence:

```sh
.venv/bin/python -m unittest discover -s tests -p test_reply_checkpoints.py -v
```

### Reply checkpoint write measurement

On 2026-11-03, the existing full-prefix strategy was measured with
`.venv/bin/python scripts/measure_reply_checkpoints.py`.
It runs 600 growing snapshots ending at 100,000 ASCII bytes, plus 60 draft
saves and six turn creations against a separate conversation. These are the
snapshots of a ten-minute reply at one per second, compressed into a stress run;
competing operations are deliberately queued alongside checkpoints. A control
runs the same competing work without checkpoints. Linux process I/O counters
cover the main SQLite database and WAL together, including competing operations;
they are not per-file or physical device measurements. The sample used the
checkout's Btrfs filesystem, SQLite WAL and the application's normal settings.

| Measurement | Control | With checkpoints |
| --- | ---: | ---: |
| Submitted reply bytes | 0 | 30,049,800 |
| Write syscall bytes (database + WAL) | 420,240 | 65,794,984 |
| Kernel-accounted storage write bytes | 421,888 | 68,780,032 |
| Checkpoint median / p95 / max | — | 0.232 / 0.459 / 3.440 ms |
| Draft median / p95 / max | 0.104 / 0.295 / 0.445 ms | 0.374 / 1.126 / 3.921 ms |
| Turn median / p95 / max | 0.219 / 0.292 / 0.313 ms | 0.532 / 0.633 / 3.827 ms |
| Queue median / p95 / max | 0.001 / 0.001 / 0.003 ms | 0.273 / 1.026 / 2.961 ms |

These results confirm substantial write amplification but show small absolute
latencies on this machine even under deliberately coincident operations. The
roughly 68 MB of additional kernel-accounted writes over a hypothetical ten
minutes is not sufficient evidence here to justify a chunk schema, recovery
reassembly and compaction protocol. The existing storage design is retained;
different hardware, longer answers or concurrent workloads may warrant another
measurement. The regression asserts submitted volume, not a filesystem-independent
limit on SQLite writes.

The supported procedure is a **stopped-application snapshot** of the databases,
their linked files, and configuration. Close Bloom and quiesce every CLI, MCP
server or other desktop sharing its Retriever index before copying. A copy of
`chats.db` alone is incomplete. Do not copy a live SQLite main file independently
of its WAL, or treat a workspace edit's `.backup` file as an application backup.

## Find the actual locations

Run this in the same environment used to launch the application. If the launcher
uses `--config`, first set `BLOOM_RECOVERY_CONFIG` to that exact startup TOML.
This prints selected paths only, without dumping settings or credentials:

```sh
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
from blackrose.config import config_dir, data_dir, state_dir, load_config
from local_knowledge.config import Config as KnowledgeConfig

startup = Path(os.environ.get('BLOOM_RECOVERY_CONFIG', config_dir() / 'config.toml')).expanduser().absolute()
config = load_config(startup)
knowledge_path = Path(config.knowledge_config).expanduser().absolute()
knowledge = KnowledgeConfig.load(knowledge_path)
paths = {
    'startup_toml': startup,
    'app_config': config_dir().absolute(),
    'app_data': data_dir().absolute(),
    'app_state': state_dir().absolute(),
    'retriever_config': knowledge_path,
    'retriever_data': Path(knowledge.data_dir),
    'retriever_database': Path(knowledge.database_path),
    'retriever_captures': Path(knowledge.database_path).parent / 'captures',
    'retriever_embedding_cache': Path(knowledge.cache_dir),
    'router_preset': Path(config.router_preset).expanduser().absolute(),
    'local_image_workflows': Path(config.local_image_workflows).expanduser().absolute(),
}
print(json.dumps({key: {'path': str(path), 'exists': path.exists()}
                  for key, path in paths.items()}, indent=2))
PY
```

Save this inventory with the backup, together with the application version,
source commit if applicable, Python version, and backup date. A missing config
can mean the defaults are in use; record that absence. Do not create a replacement
config merely to take a backup. Record launcher/environment XDG overrides too.

Bloom's config/data/state roots append `blackrose-bloom` to their respective XDG
roots, defaulting to `~/.config`, `~/.local/share`, and `~/.local/state`.
The desktop's default `knowledge_config` is the literal
`~/.config/local-knowledge/config.toml`; changing XDG alone does **not** redirect
that setting. Retriever's configured `database_path`, `data_dir` and `cache_dir`
can each be elsewhere. Captures live beside the **database**, not necessarily
inside `data_dir`.

| Preserve together | Contents and recovery significance |
| --- | --- |
| Entire Bloom data directory | `chats.db` (including schema-4 pending artifact-cleanup obligations and schema-7 edit-backup records) and any SQLite sidecars; `attachments/`; `edit-backups/`, originals displaced by approved edits, which may hold secrets; managed `skills/` including `state.json`, `reviews.json` (approved `SKILL.md` digests) and resource files |
| Entire attachment directory | Content-addressed originals and separately referenced extracted text; `illustration-drafts/<conversation>/<message>/` current/revision JSON; `animation-recovery.sqlite3` and any sidecars. Recovery claims enforce attempt limits and must travel with their animations. |
| Bloom config directory and selected startup TOML | `preferences.json` overrides model/favorites/retrieval defaults; an external `--config` file is an additional backup item |
| Bloom state directory | `window.json`, useful recent app/router diagnostics; copied lock files do not preserve a running process or lock ownership |
| Selected Retriever config, database and its sidecars, sibling `captures/` | Index identity, documents/chunks, FTS/vector data, retained extraction, metadata and immutable approved captures. Preserve IDs and citations. Record any additional files in a custom Retriever data directory. |
| External router presets and image workflow configuration | Needed to reconstruct the configured services; referenced model files and image models have separate storage/installation requirements |

SQLite sidecars can include `-wal`, `-shm` and `-journal`. After clean shutdown
they may be absent; preserve any that remain rather than deleting them.

Workspace directories, Downloads exports and original imported knowledge sources
have separate lifetimes. Chat rows retain pointers and the app may retain a blob
copy of an export, but that does not back up the rest of the external directory.
Back up those sources/outputs separately if needed. In-memory drafts, pending
library/edit approval tokens and edits still inside the 250 ms UI-state debounce
are not a durable recovery promise. Flush/save edits and finish or reject pending
reviews before shutdown.

## Take a consistent snapshot

1. Close Bloom normally and wait for shutdown, including any Retriever writer.
   Stop other applications, CLI imports/rebuilds and MCP processes sharing the
   index. Keep them stopped through copying. Do not kill an active SQLite writer
   just to shorten a backup.
2. Confirm ownership is available without unlinking lock files. Using the actual
   paths from the inventory, hold both locks for the entire copy:

   ```sh
   # Set these to the inventory's actual absolute paths first.
   flock -n "$BLOOM_DATA/desktop.lock" flock -n "$KNOWLEDGE_DATABASE.lock" bash
   ```

   Continue the remaining steps in that shell; exit it after publishing the
   backup. An unavailable lock means a cooperating owner is still active. Locks
   are additional protection, not a substitute for stopping writers that do not
   participate in this locking scheme. Never remove a live `desktop.lock` inode.
3. Create a **new**, private staging directory on the backup destination
   (`umask 077`). Copy the inventory items with a metadata-preserving tool such
   as `cp -a -- SOURCE NEW_DESTINATION`. Keep separate labeled destinations for
   app data/config/state, external startup TOML, Retriever config, database and
   captures, and external service configuration. Preserve symlinks as symlinks;
   inventory their targets separately if they contain required data. Never
   follow a symlink as an implicit expansion of backup scope.
4. Record every copied regular file's relative path, byte count and SHA-256 in a
   manifest. Compare source and staged manifests while writers remain stopped;
   require matching file sets and hashes. Retain the original-location mapping,
   absent optional items, versions and schema identities with the manifest.
5. Publish the completed staging directory by renaming it on the same filesystem.
   Keep incomplete snapshots labeled as incomplete. Preserve at least the last
   known-good snapshot independently; do not overwrite it with an unverified
   backup. Release the locks and restart the services only after copying ends.

The drill below exercises this copy/hash/publish procedure with closed database
connections and both locks. It is not a general-purpose tool for backing up live
user data. A future live-backup feature would need SQLite's supported backup API
**and** coordination of linked files; it is outside this recovery contract.

## Restore into isolation first

1. Install the same application version and dependencies used by the snapshot.
   Use a new empty restore directory with private permissions. Verify its copied
   file set and hashes against the manifest **before opening a database**; reject
   missing, extra or corrupt files. Keep the original backup unchanged.
2. Map app config/data/state into new XDG roots. Restore the external startup TOML
   and Retriever config too. On the restore copy, explicitly update
   `knowledge_config`, Retriever `database_path`, `data_dir` and `cache_dir` to
   point inside the restore environment. Merely changing XDG can still select
   the live index. Copy the captures beside the restored database.
3. Retain indexed document IDs, chunk IDs, provenance paths and chat references.
   Relocated citation reads use retained index text, but indexed absolute source
   paths still name the old locations. Re-importing a capture or source at a
   different path may create a new document identity. For normal production
   recovery, restore sources/captures at their original absolute paths where
   possible. There is no automatic index-path relocation command.
4. Open the restore through `build_runtime(config)` / `Runtime.open`, controller
   conversation methods and `Session.render_transcript`, without `Session.boot`
   or `ChatController.start`. These reads do not load a chat model. Use native
   `KnowledgeService` list/open methods to resolve retained citations. The
   synthetic drill demonstrates these exact interfaces without networking.
5. Check recent history and evidence; original/extracted attachment hashes; image
   previews and saved animation source/compiled bytes; UI state and completed
   actions; illustration revisions; recovery attempts/replacement chains;
   managed skills/enabled flags; preferences and layout. Run SQLite integrity
   and foreign-key checks and Retriever's `doctor` checks, then reopen again.
   Only a separate explicit native GUI check establishes playback/rendering.
6. Reauthorize relocated workspaces with the native directory chooser: grants
   include device/inode identity, so copying their contents does not transfer
   authorization. Restore external outputs/original sources from their separate
   backups. Review service endpoints before deliberately starting a model or
   opening the restored desktop.

After isolated verification, schedule production replacement with all writers
stopped and ownership held. Keep the old installation/data and original backup
available until verification succeeds. Do not merge database rows or attachment
trees from different snapshot times. If the installed application refuses a newer
schema, reinstall the matching/newer application or restore the complete older
snapshot into a separate empty location. Do not lower schema metadata manually.

Credentials live in the desktop Secret Service, not in app config/chats.
Unlock or reprovision them with `blackrose opencode-go-key set` and
`blackrose openrouter-key set`; never copy a key into the manifest or shell
arguments. A file snapshot does not restore a keyring session.

Python/Node dependencies and generated source-checkout bundles can be rebuilt
from the recorded locks and version. Installed wheels include trusted browser
bundles, but animation authoring also needs `blackrose setup-animations` after its
compiler cache is removed. Embedding weights are a separate explicit
`bloom-knowledge --config PATH prefetch` download; local chat/image weights,
llama-server, image services and their workflows also need their own installation
or backup. Preserve caches/weights separately when offline recovery is required.
Do not discard the index, captures, extracted text or managed skills as caches.

## Repeatable synthetic drill

After [checkout setup](../CONTRIBUTING.md#set-up-a-checkout):

```sh
.venv/bin/python scripts/check_recovery.py --output artifacts/recovery-drill-NEW-NAME
```

`--output` must not already exist. The script accepts no live config or backup
input, creates only synthetic data, blocks Python network connections, opens no
windows and uses fixed test vectors without model inference. Without `--output`
it removes its temporary files after the check. It retains a source fixture,
immutable backup/manifest, damaged-snapshot examples, restored tree and
`report.json` when an output directory is given.

It uses an external startup TOML, preferences that override it, a custom shared
index location, document/image attachments, compiled saved animation fixtures,
interactive state/events, illustration revisions, repair chains, pending
conversation cleanup, managed skills,
an original knowledge document and an approved immutable web capture. After
snapshotting it moves the original application and external-input directories
away, then verifies two fresh runtime opens against the restore. Missing and
tampered files must be rejected by the manifest before reopening. Restored startup
retries the queued blob/draft/recovery cleanup while retaining live conversations
and leaving the backup unchanged. See [artifact cleanup](artifact-cleanup.md)
before any real-data reconciliation or repair.

The recorded recovery time covers copying, hash verification and application
checks; fixture creation is reported separately in total time. This is a small
synthetic baseline, not a production recovery-time promise. It does not exercise
keyring provisioning, model/service restoration, native playback or screen
readers. Run it again after lifecycle incidents or schema/storage changes.

### Recorded rehearsal — 2026-10-17

A machine-readable result recorded a passing
run on Linux x86_64, Python 3.13.16, Node 22.22.2 and SQLite 3.50.2. It used
application 0.2.0, chat schema 3, Retriever schema 2 and the existing unversioned
animation-repair table, from base commit `7040328` plus the uncommitted
housekeeping changes. The record includes the drill script's SHA-256.

The fixture contained 21 backed-up files (761,012 bytes), four messages, seven
attachments and two knowledge documents. Both restored runtime opens passed all
listed checks. Copying, hashing and verification took 0.095 seconds; total fixture
creation plus recovery took 1.073 seconds. These timings describe this tiny local
fixture only. The final retained run used:

```sh
.venv/bin/python scripts/check_recovery.py --output artifacts/recovery-drill-2026-10-17-completed
```

The complete offline health command also passed after including the drill:
375 unit tests (18 skipped), the restore check, Ruff, targeted mypy, JavaScript
syntax, UI runtime tests and lock validation. Execution required leaving the
command sandbox: a minimal temporary-database/asyncio example also stalled inside
it and passed outside it. The drill's network guards and synthetic-only scope
remained active. No native desktop checks or real-data backup were run in this
pass.

### Schema-4 cleanup rehearsal — 2026-10-17

A second rehearsal reran the drill with chat
schema 4 and three pending cleanup obligations at snapshot time. Restored startup
removes the deleted conversation's blob, illustration draft and recovery rows,
while retaining four messages, seven attachments, interactive state and two
knowledge documents. A second fresh open is idempotent and the backup stays
unchanged. The fixture contains 23 files; recovery took 0.119 seconds and total
fixture/verification time was 1.250 seconds on the recorded environment. These
small synthetic timings do not estimate production recovery time.

The retained run used:

```sh
.venv/bin/python scripts/check_recovery.py --output artifacts/recovery-drill-h04-2026-10-17
```

Use a new output name to reproduce. The H04 health run passed 402 tests (18
skipped) plus the drill and all other offline checks. Native playback, real
models/keyring/services and live-data repair remain outside this rehearsal.
