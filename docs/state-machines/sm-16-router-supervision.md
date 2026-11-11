# SM-16: Local router supervision, model residency and the desktop instance lock

## Scope and ownership

| Surface | Location |
| --- | --- |
| `DesktopOwnership` (`path`, `handle`, `active`, `require`) | `src/blackrose/instance.py::DesktopOwnership` |
| `desktop_instance` (`flock`, context manager) | `src/blackrose/instance.py::desktop_instance` |
| Desktop call site: lock acquired before runtime and router | `src/blackrose/gui/app.py::run_gui` |
| Artifact CLI call sites (read-only and repair) | `src/blackrose/artifact_cli.py::run` |
| `RouterKey` (`path`, `owned`, `read`, `create`, `remove`, `auth_flow`) | `src/blackrose/backend/service.py::RouterKey` |
| Key path derivation (endpoint hash) | `src/blackrose/backend/service.py::router_key`, `src/blackrose/backend/service.py::_endpoint_id` |
| `RouterService` (`_pid`, `_handle`, `_process`, `_output`, `_operation`) | `src/blackrose/backend/service.py::RouterService` |
| `_adopt` (/proc scan, identity checks) | `src/blackrose/backend/service.py::RouterService._adopt` |
| `_launch` (key, spawn, pidfd) | `src/blackrose/backend/service.py::RouterService._launch` |
| `ensure_running` (startup flock, bounded readiness poll) | `src/blackrose/backend/service.py::RouterService.ensure_running` |
| `close` (SIGTERM/SIGKILL, reap, key removal) | `src/blackrose/backend/service.py::RouterService.close` |
| `RouterClient.load` (residency poll) | `src/blackrose/backend/router.py::RouterClient.load` |
| `RouterClient.unload` | `src/blackrose/backend/router.py::RouterClient.unload` |
| `RouterClient._inspect` and `capabilities` cache | `src/blackrose/backend/router.py::RouterClient._inspect`, `src/blackrose/backend/router.py::RouterClient.capabilities` (set in `__init__`, written in `load`), `src/blackrose/backend/router.py::RouterClient.reasoning_levels` |
| Capability consumers | `src/blackrose/backend/effort.py::local_levels`, `src/blackrose/backend/effort.py::local_fields`, `src/blackrose/chat.py::ChatController.effort_levels`, `src/blackrose/backend/router.py::RouterClient.stream` |
| Callers of `ensure_running` / `close` | `src/blackrose/backend/router.py::RouterClient.models`, `src/blackrose/backend/router.py::RouterClient.aclose`, `src/blackrose/chat.py::ChatController.start`, `src/blackrose/chat.py::ChatController.load_model`, `src/blackrose/chat.py::ChatController.shutdown` |
| Decision record | [ADR 0001](../adr/0001-desktop-ownership.md) |

Unqualified method names in the Router supervision tables below are members of
`src/blackrose/backend/service.py::RouterService`; in the Model residency tables,
of `src/blackrose/backend/router.py::RouterClient`; in the Desktop ownership
table, `src/blackrose/instance.py::desktop_instance` and
`src/blackrose/instance.py::DesktopOwnership.require`. Each citation names the
statement it means.

Three owners meet here, and the architecture page states them separately
([Modules and invariants](../architecture.md#modules-and-invariants): the
`backend/service.py` row and the desktop ownership paragraph):

- **The chat database is owned by one desktop at a time**, via an advisory
  `fcntl.flock` on `<database dir>/desktop.lock`
  (`src/blackrose/instance.py::desktop_instance`). The lock is held for the whole
  life of the window — through storage, session and router shutdown
  (`src/blackrose/gui/app.py::run_gui`) — and is released by the OS on process
  death ([ADR 0001](../adr/0001-desktop-ownership.md), the decision paragraph).
- **The router process is owned by whichever desktop launched it**, and that
  ownership is *transferable* through `/proc` discovery: a router that survives a
  crashed desktop is adopted by the next one and stopped on its exit (the module
  docstring of `src/blackrose/backend/service.py`, the
  `src/blackrose/backend/service.py::RouterKey` docstring, and
  `src/blackrose/backend/service.py::RouterService._adopt`). Ownership is
  expressed as a pidfd plus a per-endpoint key file, not as an in-memory flag the
  next process can read.
- **Each model's residency** (`unloaded`/`loading`/`loaded`/`sleeping`/`failed`)
  is owned by the router (`llama-server`), not by Blackrose. `RouterClient` only
  observes it and caches the one derived fact it needs: whether the model's chat
  template supports `enable_thinking` and `supports_reasoning_effort`
  (`src/blackrose/backend/router.py::RouterClient._inspect`).

There is one `RouterService` and one `RouterClient` per runtime
(`src/blackrose/runtime.py::build_runtime`), and one runtime per desktop window
(`src/blackrose/gui/app.py::_run_gui`). The only cross-process serialization for a
router is the per-endpoint startup flock (`lock_path` in
`RouterService.__init__`, taken in `RouterService.ensure_running`), which is a
*startup* lock, not a lifetime lock; see hazard 3.

## State variables

Desktop ownership:

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `path` | `src/blackrose/instance.py::DesktopOwnership.path` | `Path` | the `desktop.lock` this ownership covers | constructor |
| `handle` | `src/blackrose/instance.py::DesktopOwnership.handle` | text `IO` | the open file description holding the `flock` | `desktop_instance` (the `with path.open(...)`) |
| `active` | `src/blackrose/instance.py::DesktopOwnership.active` | bool | this context still grants ownership | constructor (`True`), `desktop_instance`'s `finally` (`False`) |
| `desktop.lock` inode | `desktop_instance` (open and `flock`) | file on disk | the lock object; never unlinked (the "Do not unlink" comment) | `desktop_instance` |

Router supervision (`RouterService` and `RouterKey`):

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `endpoint`, `host`, `port` | `RouterService.__init__` | str/int | the configured router address | constructor |
| `local` | `RouterService.__init__` | bool | `http` on a loopback host: adoption and launch are allowed | constructor |
| `key.path` | `src/blackrose/backend/service.py::RouterKey.__init__`, `src/blackrose/backend/service.py::router_key` | `Path` | `<state>/llama-router-<sha256(endpoint)[:16]>.key` (or the configured file) | `router_key` |
| `key.owned` | `src/blackrose/backend/service.py::RouterKey.__init__` | bool | true: Blackrose generated this key and may delete it; false: the user's file | `router_key` |
| `_pid` | `RouterService.__init__` | `int \| None` | the router pid we hold a handle for | `_adopt` (the paired assignment), `_launch` (after `Popen`), cleared in `_adopt` and `_launch` (stale handle), `_launch`'s post-spawn `except` and `close`'s `finally` |
| `_handle` | `RouterService.__init__` | `int \| None` | pidfd for `_pid`; the only thing ever signalled | same assignments |
| `_process` | `RouterService.__init__` | `Popen \| None` | set only when *this* process spawned the router | `_launch` (`Popen`), cleared in `_launch`'s post-spawn `except` and `close`'s `finally` |
| `_output` | `RouterService.__init__` | `RouterOutput \| None` | the drain thread for the child's stdout/stderr | `_launch` (last statement), cleared in `_launch` (after joining the previous one) and `close`'s outer `finally` |
| `_operation` | `RouterService.__init__` | `asyncio.Lock` | serializes `ensure_running` and `close` | both |
| `lock_path` | `RouterService.__init__` | `Path` | `<state>/llama-router-<sha256(endpoint)[:16]>.lock` (`src/blackrose/backend/service.py::_endpoint_id`), a startup-only `flock` | `ensure_running` (taken in the startup section, released in its `finally`) |
| `start_timeout`, `stop_timeout` | `RouterService.__init__` | float | 30 s startup bound, 60 s SIGTERM grace (`src/blackrose/config.py::Config.router_start_timeout`, `src/blackrose/config.py::Config.router_stop_timeout`) | constructor |
| `_warnings` | `RouterService.__init__` | `list[str]` | undelivered adoption warnings | `_check_adopted` (append), drained by `take_warnings` |
| router key file | `src/blackrose/backend/service.py::RouterKey.create` | 0600 file | the credential the router requires on `/v1/models` | `RouterKey.create` (atomic replace) |
| router log | `log_path` in `RouterService.__init__` | 0600 file | bounded stdout/stderr (`src/blackrose/diagnostics.py::BoundedLogHandler`) | drain thread |

Model residency and the capability cache (`RouterClient`):

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `status.value` | router, read in `load` (before and inside its poll loop) and `unload` | `'unloaded' \| 'loading' \| 'loaded' \| 'sleeping' \| 'failed' \| other \| absent` | the router's residency for one model | the router only |
| `capabilities` | `src/blackrose/backend/router.py::RouterClient.capabilities` | `dict[str, dict]` | per-model `{'thinking': bool, 'reasoning_effort': bool}` derived from `/props` | `load` (set after a successful `_inspect`; dropped before a management load) |
| `timeout` | `RouterClient.__init__` | float | the poll deadline (`config.request_timeout`, 1..3600 s, 600 default) and the HTTP timeout | constructor |
| `budgets`, `stream_bounds` | `RouterClient.__init__` | config | reasoning budgets and SSE bounds, not residency state | constructor |

## States

**Desktop ownership** (mutually exclusive, derived from `active` and `handle`):

| State | Predicate |
| --- | --- |
| `Unlocked` | no open file description holds `LOCK_EX` on the path |
| `Held(owner)` | this process holds it; `ownership.active is True` and `not handle.closed` |
| `Released` | `active is False`, the handle may still be open until the `with` block exits |
| `Closed` | `handle.closed` — the OS has dropped the lock |

Only `Held` satisfies `src/blackrose/instance.py::DesktopOwnership.require`; the
three non-`Held` states are indistinguishable to every consumer, so ownership is
total.

**Router supervision** (per `RouterService`, orthogonal to `_output`/`_warnings`):

| State | Predicate |
| --- | --- |
| `Unattached` | `_handle is None and _pid is None and _process is None` |
| `Adopted` | `_handle is not None`, `_pid` is a process we did not spawn (`_process is None`) |
| `OwnedRunning` | `_handle is not None`, `_process is not None`, `_process.poll() is None` |
| `Exited` | `_handle is not None and _exited(_handle)`; `_process` may be an unreaped `Popen` |
| `Stopping` | `_operation` held inside `close`, handles already signalled (`close`'s signal and wait block) |
| `Relaunching` | transient inside `_launch`/`_adopt` while a stale handle is closed (the `_handle is not None` block at the top of each) |
| `Unsupervised` | forbidden: `_process` running with no `_handle` (was reachable before this change; see Formalization decisions) |

`Adopted` and `OwnedRunning` are distinguished by `_process`, which is why
`close()` reaps only a child it spawned (its `_process.poll()`) while it signals
either kind through the pidfd (its `pidfd_send_signal` calls).

**Model residency** (per model id, as reported by the router):

| State | Predicate |
| --- | --- |
| `Unlisted` | no catalog row with that id (`load`'s `row is None` check) |
| `Incompatible` | `status` is absent/not a dict/no `value` (`load`'s and `unload`'s `status_value is None` checks, and `unload`'s 404) |
| `Unloaded` | `status.value == 'unloaded'` |
| `Loading` | `status.value == 'loading'` |
| `Loaded` | `status.value == 'loaded'` |
| `Sleeping` | `status.value == 'sleeping'` |
| `Failed` | `status.value == 'failed'` |
| `Unknown` | any other string |

Readiness is the joint predicate `Loaded or Sleeping` (`load`'s
`status in ('loaded', 'sleeping')`). The capability cache has two states per
model: `Cached` (`model in capabilities`) and `Uncached`. A failed `/props` read
now leaves the model `Uncached` instead of recording `Cached({})` (`load` caches
only a non-`None` `_inspect` result, and `_inspect` returns `None` on failure).

## Events

- Desktop start: `run_gui` enters `desktop_instance` before building the runtime
  (`src/blackrose/gui/app.py::run_gui`); `main` turns `AlreadyRunning` into exit
  code 1 (`src/blackrose/main.py::main`).
- Desktop exit, including a raised startup error: the context manager's
  `finally` runs (`src/blackrose/gui/app.py::_run_gui`'s `finally`,
  `src/blackrose/instance.py::desktop_instance`'s `finally`).
- Artifact CLI: read-only inventory under `create=False`, repair under
  `create=True` (`src/blackrose/artifact_cli.py::run`).
- Any catalog read: `RouterClient.models()` calls `service.ensure_running()`
  (`src/blackrose/backend/router.py::RouterClient.models`); reached from
  `ChatController.start/load_model/select_model`
  (`src/blackrose/chat.py::ChatController.start`,
  `src/blackrose/chat.py::ChatController.load_model`,
  `src/blackrose/chat.py::ChatController.select_model`), `refresh_models`
  (`src/blackrose/chat.py::ChatController.refresh_models`), `local_images` restore
  (`src/blackrose/local_images.py::LocalImages._finish`) and the GUI picker.
- Router shutdown: `ChatController.shutdown` → `ChatProviders.aclose` →
  `RouterClient.aclose` → `RouterService.close`
  (`src/blackrose/chat.py::ChatController.shutdown`,
  `src/blackrose/backend/providers.py::ChatProviders.aclose`,
  `src/blackrose/backend/router.py::RouterClient.aclose`).
- Another Blackrose starting a router on the same endpoint: startup flock
  contention, then a re-check for readiness
  (`src/blackrose/backend/service.py::RouterService.ensure_running`).
- Router death, connection refusal, `/v1/models` 503 (still warming) or 401
  (key rejected) (`src/blackrose/backend/service.py::RouterService._ready`,
  `src/blackrose/backend/service.py::RouterService.ensure_running`,
  `src/blackrose/backend/router.py::RouterClient.models`).
- `/proc` scan finding zero or several candidate routers
  (`src/blackrose/backend/service.py::RouterService._adopt`).
- SIGTERM grace expiry and escalation to SIGKILL
  (`src/blackrose/backend/service.py::RouterService.close`).
- A model load accepted, rejected as busy (`500`), raced (`400`), missing route
  (`404`) or failed (`success: false`)
  (`src/blackrose/backend/router.py::RouterClient.load`, its `/models/load` loop).
- A model going `unloaded`/`loading` again while `load()` polls (`load`'s final
  poll loop), and `unload()` observing `failed` or a vanished row (`unload`'s poll
  loop).
- A `/props` read failing (`src/blackrose/backend/router.py::RouterClient._inspect`).

## Transition table

Desktop ownership (statements in `desktop_instance` and `DesktopOwnership.require`):

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unlocked` | `desktop_instance(path, create=True)` | parent created (`mkdir`), file opened `a` | `flock(LOCK_EX\|LOCK_NB)` | `Held` |
| `Unlocked` | `desktop_instance(path, create=False)` | file missing | `FileNotFoundError` from the `open`; caller reports it (`src/blackrose/artifact_cli.py::run`'s `except`) | `Unlocked` |
| `Held(other)` | `desktop_instance` | `BlockingIOError` | `AlreadyRunning`; the failed attempt closes its own fd | `Unlocked` |
| `Held` | body returns or raises | — | `active = False` **then** `LOCK_UN` (the `finally`), then the `with` closes the fd | `Released` → `Closed` |
| `Held` | `require(db)` | `db == ':memory:'` or lock not beside `db` | `RuntimeError` | `Held` |
| `Released`/`Closed` | `require(db)` | `not active or handle.closed` | `RuntimeError` | unchanged |

Router supervision (statements in `ensure_running`, `_ready`, `_adopt`, `_launch`
and `close`):

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unattached` | `ensure_running`, router answers with our key | `_ready()` True (the first probe) | `_adopt()` finds a matching pid and takes a pidfd (`_adopt`'s `pidfd_open` and paired assignment) | `Adopted` |
| `Unattached` | `ensure_running`, router answers but no pid matches | `_ready()` True, `_adopt` exhausts `/proc` | `RuntimeError` (`_adopt`'s final `raise`); nothing signalled; fields stay `None` | `Unattached` |
| `Unattached` | `ensure_running`, nothing listening | `ConnectError` (the first probe) | take startup flock, bounded by `start_timeout` | `Unattached` (inside the startup section) |
| startup | re-check after the lock, another window is ready | `_ready()` True (the re-check) | skip the poll loop and adopt | `Adopted` |
| startup | re-check after the lock, still refused | `ConnectError` (the re-check) | `_launch()`: key, spawn, pidfd | `OwnedRunning` |
| startup | router answers 503 (warming) | `_ready()` False (`_ready`'s 503 branch) | poll every 0.1 s, never launch (the `while not ready` loop) | `Unattached` until ready |
| `OwnedRunning`/`Adopted` | router becomes ready | `_ready()` True (inside the poll loop) | `_adopt()`; our own handle is alive, so it returns at its live-handle check | unchanged |
| `OwnedRunning` | our child exits during startup | `_process.poll() is not None` (inside the poll loop) | `RuntimeError` with the exit code; handle and key retained for `close()` | `Exited` |
| `OwnedRunning`/`Adopted` | router exits at any time | `_exited(_handle)` (`_adopt`, `_launch`, `close`) | next `_adopt`/`_launch` closes the pidfd and clears `_pid` (their stale-handle block) | `Unattached` (`_process` may stay as an unreaped `Popen`) |
| `Exited` | `close()` | `_exited` True | skip signals, `_process.poll()`, `key.remove()`, close handle (`close`'s `finally`) | `Unattached` |
| `OwnedRunning`/`Adopted` | `close()` | handle alive | collect descendant pidfds (`_descendant_handles`), SIGTERM, wait `stop_timeout`, SIGKILL survivors | `Stopping` |
| `Stopping` | all handles report exit | every `_exited` (the `asyncio.timeout(5)` wait) | poll child, remove owned key, close handles and clear fields (`close`'s `finally`) | `Unattached` |
| `Stopping` | SIGKILL survivors after 5 s | `asyncio.timeout(5)` | `TimeoutError` escapes; handles closed and fields cleared (`close`'s `finally`), key **kept** (`key.remove()` not reached) | `Unattached` with a surviving router (hazard 4) |
| `OwnedRunning` | post-spawn supervision fails | `output.start`/`pidfd_open` raises (`_launch`'s second `try`) | kill and wait the child, clear `_process`/`_pid`, remove the owned key, re-raise (fix 1) | `Unattached` |
| `Unattached` | `close()` | `_handle is None` | return; join `_output` and close HTTP (`close`'s outer `finally`) | `Unattached` |
| any | `ensure_running` or `close` re-entered | `_operation` (`async with self._operation` in both) | the second caller waits | unchanged |

Model residency and capabilities (statements in `load`, `unload` and `_inspect`):

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unlisted` | `load` | catalog lacks the id (`row is None`) | `ValueError` | `Unlisted` |
| `Incompatible` | `load`/`unload` | no `status.value` (`status_value is None`) | `_management_required` | unchanged |
| `Unloaded`/`Failed`/`Unknown` | `load` | status not in `loaded/sleeping/loading` | drop the cache (`capabilities.pop`), `POST /models/load` | `Loading` |
| `Loading` | `load` | POST returns busy `500` (`busy`) | re-read the catalog, wait, retry until the deadline | `Loading` |
| `Loading` | `load` | POST returns raced `400` (`raced`) | re-read; if the row is now `loaded/sleeping/loading`, stop posting | `Loading` |
| `Loading` | `load` | POST 404 / other error / `success: false` (the `not busy and not raced` branch) | `_management_required`, HTTP error or `RuntimeError` | `Failed`/unchanged |
| `Loading` | poll reads `loaded`/`sleeping` | `status in ('loaded', 'sleeping')` | inspect once if uncached, return the fresh catalog | `Loaded`/`Sleeping`, `Cached` |
| `Loading` | poll reads `loading`/`unloaded` | `status in ('loading', 'unloaded')` | sleep ≤0.25 s, re-read | unchanged |
| `Loading` | deadline passes | `remaining <= 0` in the poll loop (and in the POST loop) | `TimeoutError` | unchanged |
| `Loading` | poll reads `failed` or an unknown value | `status not in ('loading', 'unloaded')` | `RuntimeError` with the status | `Failed`/`Unknown` |
| `Loaded`/`Sleeping` | `load` | cached (`model in self.capabilities`) | no `/props` call, return the catalog | unchanged |
| `Cached` | `load` starts a management load | `capabilities.pop` | entry dropped | `Uncached` |
| `Uncached` | `/props` answers | `_inspect`'s `try` | cache `{'thinking', 'reasoning_effort'}` (`load`) | `Cached` |
| `Uncached` | `/props` fails | `_inspect`'s `except` | cache stays unset (fix 2) | `Uncached` |
| `Loaded`/`Sleeping`/`Loading` | `unload` | status not `unloaded` | `POST /models/unload` | `Unloaded` after polling |
| `Unloaded` | `unload` | status already `unloaded` | return without a request | `Unloaded` |
| `unloading` | poll reads `unloaded` | `unload`'s poll loop | return | `Unloaded` |
| `unloading` | poll reads `failed` or the row vanished | `row is None` or `failed` | `RuntimeError` | unchanged |

## Invariants

1. **One holder per lock path.** A cooperating process cannot enter twice; a second
   open file description conflicts even inside one process
   (`src/blackrose/instance.py::desktop_instance`). Pinned by
   `tests/test_instance.py::InstanceTests.test_nested_acquisition_fails_and_exception_releases_lock`
   and `test_ownership_is_valid_only_while_the_context_holds_the_lock`.
2. **Ownership is valid only inside the context, and never revived.**
   `active` is written `True` once (`src/blackrose/instance.py::DesktopOwnership.active`)
   and `False` once (`desktop_instance`'s `finally`); `require` rejects either
   `not active` or a closed handle. Pinned by the same test.
3. **`active` is cleared before the lock is released.** In `desktop_instance`'s
   `finally`, `ownership.active = False` precedes `LOCK_UN`, so a consumer that
   observes `active` cannot then act after the release. Assumed by
   `src/blackrose/artifacts.py::ArtifactLifecycle.repair`.
4. **The lock file is never unlinked** (`desktop_instance`'s "Do not unlink"
   comment; [ADR 0001](../adr/0001-desktop-ownership.md), the decision paragraph),
   so a waiting process never locks an orphaned inode.
5. **`require` accepts only the lock beside this database**
   (`src/blackrose/instance.py::DesktopOwnership.require`). Pinned by
   `test_require_accepts_only_the_lock_beside_this_database` and
   `tests/test_artifacts.py::ArtifactTests.test_repair_requires_active_ownership_of_the_same_database`.
6. **A spawned router always has a pidfd.** Every statement after `Popen` is inside
   the transactional tail (`src/blackrose/backend/service.py::RouterService._launch`,
   its second `try`); a failure kills and reaps the child and removes the launch
   key, so `_process` running with `_handle is None` is unreachable. Pinned by
   `test_a_spawn_that_cannot_be_supervised_is_stopped_and_its_key_removed` (fails
   before the change).
7. **`_pid` and `_handle` are both set or both unset.** They are assigned together
   (`_adopt`'s paired assignment, `_launch` after `Popen`) and cleared together
   (the stale-handle blocks in `_adopt` and `_launch`, `_launch`'s post-spawn
   `except`, `close`'s `finally`). Assumed by `close()`'s
   `handles = [self._handle]` and
   `src/blackrose/backend/service.py::RouterService._descendant_handles`.
8. **Nothing is signalled without an identified pidfd.** Every signal goes through
   `_handle` (`close`'s `pidfd_send_signal` calls), which comes from `pidfd_open`
   of a `/proc` entry that matched uid, executable, `--models-preset`, port and host
   (`src/blackrose/backend/service.py::RouterService._adopt`), or from our own
   `Popen` (`_launch`). Pinned by
   `test_adoption_refuses_a_process_serving_another_port` and
   `test_adoption_requires_separate_port_and_host_arguments`.
9. **The owned key is removed only after its router exited.** `key.remove()` runs
   inside `close`'s `try` after the wait
   (`src/blackrose/backend/service.py::RouterService.close`) and on every failed
   launch (`_launch`'s two `except` blocks); a router that survives SIGKILL keeps
   its key (the "a surviving router keeps its key" comment in `close`). Pinned by
   `test_close_reaps_the_owned_router_and_is_idempotent` and
   `tests/test_router_service.py::RouterServiceTests.test_next_desktop_adopts_a_surviving_key_protected_router_silently`.
10. **A configured key file is never removed.** Only `key.owned` is unlinked
    (`src/blackrose/backend/service.py::RouterKey.remove`). Pinned by
    `test_a_failed_launch_never_removes_a_configured_key_file` and
    `tests/test_router_service.py::RouterServiceTests.test_configured_key_file_must_be_private_and_is_never_removed`.
11. **Startup is bounded.** The lock wait, launch and readiness poll are all inside
    `asyncio.timeout(start_timeout)`
    (`src/blackrose/backend/service.py::RouterService.ensure_running`), so a
    warming or crashing router cannot hold the caller past the configured 30 s.
    Pinned by
    `tests/test_router_service.py::RouterServiceTests.test_startup_timeout_retains_handle_for_shutdown`,
    `test_a_router_that_is_still_warming_up_is_waited_for_not_duplicated` and
    `test_an_exited_router_is_relaunched_with_a_fresh_key`.
12. **A warming router is waited for, not duplicated.** `_ready()` returning
    `False` for 503 enters the poll loop; only `ConnectError` reaches `_launch()`
    (`src/blackrose/backend/service.py::RouterService.ensure_running`, the
    re-check after the lock). Pinned by
    `test_a_router_that_is_still_warming_up_is_waited_for_not_duplicated`.
13. **One router operation at a time per service.** `_operation` wraps the whole of
    `ensure_running` and `close` (`async with self._operation` in each); no
    interleaving can observe a half-launched or half-stopped service.
14. **Adoption is all-or-nothing.** `_adopt` writes `_pid` and `_handle` in one
    statement (`src/blackrose/backend/service.py::RouterService._adopt`) or raises,
    and a rejected candidate is closed and skipped (its `_exited(handle)` check).
    `test_adoption_refuses_a_process_serving_another_port` asserts the refused
    process is still alive.
15. **Adoption warnings are delivered once.** `take_warnings` swaps the list
    (`src/blackrose/backend/service.py::RouterService.take_warnings`); pinned by
    `tests/test_router_service.py::RouterServiceTests.test_adopting_an_exposed_unauthenticated_router_warns_once`.
16. **Readiness is only `loaded` or `sleeping`.** `loading` and `unloaded` keep
    polling (`src/blackrose/backend/router.py::RouterClient.load`, its final poll
    loop), so a model that falls back to `unloaded` during a poll is never reported
    ready. Pinned by `test_a_model_that_falls_back_to_unloaded_is_waited_for`.
17. **Every residency wait is bounded.** `load` computes one deadline (`deadline`)
    and checks it before each sleep in both loops (`remaining <= 0`); `unload`
    wraps its poll in `asyncio.timeout(timeout)`. Each individual HTTP call is
    still bounded by the client's `request_timeout`, so the worst case is the
    deadline plus one request. Pinned by
    `test_a_model_that_never_becomes_ready_is_bounded`.
18. **The capability cache is per model id.** Keys are the model id only
    (`src/blackrose/backend/router.py::RouterClient.capabilities`, written in
    `load` and read in `reasoning_levels`), so a switch cannot reuse another
    model's entry; a management reload of the same model drops it first (`load`'s
    `capabilities.pop`). Pinned by
    `test_capabilities_are_per_model_across_a_switch` and
    `test_capabilities_are_dropped_and_reread_before_a_reload`.
19. **A cached capability is a successful `/props` observation.** `_inspect`
    returns `None` on failure and `load` caches only a dict
    (`src/blackrose/backend/router.py::RouterClient.load`,
    `src/blackrose/backend/router.py::RouterClient._inspect`). Pinned by
    `test_a_failed_property_read_is_not_cached_as_missing_support` (fails before
    the change).
20. **A management load sends no generation overrides.** The POST body is exactly
    `{'model': model}` (`load`'s `/models/load` request); sampling, context and GPU
    settings stay with the server preset, and `local_fields` only adds a reasoning
    request when the user explicitly chose an effort
    (`src/blackrose/backend/effort.py::local_fields`).
21. **`stream` never invents capabilities.** It reads `capabilities.get(model)`
    (`src/blackrose/backend/router.py::RouterClient.stream`); an uncached model
    therefore sends no reasoning fields (`local_fields`' first guard), which is
    the same wire result as "no support".

## Hazards and open questions

Answers to the investigation's focus questions first.

- *Is the supervision state total (unattached / adopted / owned-launched / exited /
  stopping)?* Yes, with one former hole now closed. `_pid`/`_handle` move together
  (invariant 7), `_process` distinguishes owned from adopted, and `close` is the
  only `Stopping` state. The hole was `_launch`: `Popen` succeeded but
  `output.start`/`os.pidfd_open` could raise, leaving a live `_process` with no
  handle that `close()` refused to touch (its `_handle is None` return). That is
  now transactional (`_launch`'s second `try`), so the state table above has no
  `Unsupervised` row.
- *Can adoption pick a PID that is not our router?* It cannot pick one that does
  not answer the endpoint with our key (`_ready()` runs first, in
  `ensure_running`), and it verifies uid, `/proc/<pid>/exe`, `--models-preset`,
  port and host before taking the pidfd (`_adopt`). Two gaps remain: the identity
  checks happen *before* `pidfd_open`, so a pid recycled in between would be
  adopted (hazard 1), and a router started with `--port=NNNN` cannot be adopted at
  all (hazard 2). Every refusal leaves the candidate unsignalled (invariant 14).
- *What happens if the process crashes between `key.prepare()` and `key.remove()`?*
  There is no `prepare()`; the sequence is `key.create()` → `Popen` →
  `key.remove()` (`src/blackrose/backend/service.py::RouterService._launch`). A
  crash leaves the 0600 key at the deterministic endpoint path
  (`src/blackrose/backend/service.py::router_key`). It is not adopted *as a
  credential*: the next desktop either uses it to adopt the surviving router it
  belongs to (`ensure_running`'s first probe and `_adopt`, pinned by
  `tests/test_router_service.py::RouterServiceTests.test_next_desktop_adopts_a_surviving_key_protected_router_silently`)
  or replaces it atomically with `key.create()` before spawning a new router
  (`_launch`; the `os.replace` in `RouterKey.create`). A crash between the
  temporary write and `os.replace` leaks a `.<name>.<hex>` file (hazard 5); no
  stale key can authenticate against a router it was not generated for.
- *Does `stop()` always reap the child and remove the key it owns?* `close()` is the
  stop path. For any child with a pidfd: yes — SIGTERM, wait `stop_timeout`,
  SIGKILL, `_process.poll()`, then `key.remove()`
  (`src/blackrose/backend/service.py::RouterService.close`). For a surviving
  router (hazard 4) no: the key is deliberately kept and the pidfd is dropped. For
  an owned key whose launch failed, yes (`_launch`'s two `except` blocks). The one
  residue is an already-exited `Popen` that `_adopt` detached (its stale-handle
  block), which `close()` does not `poll()` because it returns at its
  `_handle is None` check (hazard 6 — a zombie, no live resource).
- *Can two desktop instances both believe they own the router?* Yes, when they have
  different chat database directories: `desktop.lock` is per database directory
  (`src/blackrose/gui/app.py::run_gui`, the `lock` path), and the endpoint lock is
  released when startup finishes (`ensure_running`'s `finally`). Both then hold
  pidfds for the same router and either close stops it (hazard 3, pinned). Same
  database directory: no — the second window never builds a runtime
  (`src/blackrose/gui/app.py::run_gui`).
- *Is `ensure_loaded` polling bounded, and does it handle `loaded -> unloaded`?*
  There is no `ensure_loaded`; the call is `RouterClient.load`
  (`src/blackrose/backend/router.py::RouterClient.load`). It is bounded by one
  deadline derived from `request_timeout` (its `deadline` and the `remaining <= 0`
  checks) plus at most one in-flight request. A model that returns to `unloaded`
  during the poll is tolerated and polled again (the final poll loop), never
  reported ready; it ends in `Loaded`, `Failed` or `TimeoutError`
  (`test_a_model_that_falls_back_to_unloaded_is_waited_for`,
  `test_a_model_that_never_becomes_ready_is_bounded`).
- *Are capabilities cached past a model switch?* No per-model leakage: the cache is
  keyed by model id (invariant 18) and re-read after a management reload
  (`load`'s `capabilities.pop` and its cache write). Two staleness cases remain: a
  failed `/props` is no longer cached (fixed), but a router *restart* under the
  same model id keeps the old entry, and `unload()` does not drop it (hazard 7).

**1. `fragile` — `src/blackrose/backend/service.py::RouterService._adopt` —
adoption verifies the pid before opening its pidfd.** The identity evidence is
strong (uid, executable, preset flag, port, host, plus the endpoint answering with
our key), but the pidfd is taken last, so a pid recycled between
`(entry / 'cmdline').read_bytes()` and `os.pidfd_open` would yield a handle to an
unrelated process that `close()` would later SIGTERM/SIGKILL. Requires a pid wrap
inside that window (two `/proc` reads), so it is theoretical here, but the
consequence is signalling a foreign process. Not demonstrated: it needs interposed
syscalls or a fake `/proc`. Would confirm by patching `os.pidfd_open` to return a
pidfd for a second live process and checking that `_adopt` accepts it. Follow-up:
read `/proc/<pid>/stat` field 22 (start time) before and after `pidfd_open` and
reject a mismatch — four lines, but only worth adding with a deterministic test
seam.

**2. `fragile` — `src/blackrose/backend/service.py::RouterService._adopt` — the
`/proc` parser only understands space-separated options.**
`arguments[arguments.index('--port') + 1]` raises `ValueError` for `--port=NNNN`,
which is caught and the candidate skipped (`_adopt`'s `except`), so a router
Blackrose can talk to (`_ready` True) cannot be adopted and startup fails with
"Cannot identify the local llama-router process". Characterised by
`test_adoption_requires_separate_port_and_host_arguments`. Accepting
`--port=`/`--host=` forms is a small change but needs its own refusal tests for
the ambiguous `--host=0.0.0.0` warning path.

**3. `fragile` — `src/blackrose/backend/service.py::RouterService.ensure_running`
— the endpoint lock is a startup lock, not an ownership lock.** `desktop.lock` is
per chat database (`src/blackrose/gui/app.py::run_gui`), so two windows with
different database directories, a scratch runtime, or the artifact CLI may each
adopt the same router; the first `close()` SIGTERMs the router the other is still
using (`src/blackrose/backend/service.py::RouterService.close`). Pinned as current
behaviour by `test_the_endpoint_lock_is_not_a_lifetime_ownership_lock`.
[ADR 0001](../adr/0001-desktop-ownership.md) (its scope paragraph) scopes the
desktop lock to one database directory, so this is a known gap rather than an
accident, and the adoption warning only mentions the exit-stop contract for
anonymous or exposed routers
(`src/blackrose/backend/service.py::RouterService._check_adopted`). A fix is a
protocol change: hold the endpoint lock for the service lifetime and add a
"borrowed, do not stop" state, or refuse to adopt a router another live Blackrose
holds. Not attempted.

**4. `fragile` — `src/blackrose/backend/service.py::RouterService.close` — a
router that survives SIGKILL makes `close()` raise and is then unreachable.** The
5 s post-SIGKILL wait is inside `asyncio.timeout`; on expiry the `finally` closes
the pidfds and clears `_pid`/`_handle`/`_process`, `key.remove()` is skipped, and
the `TimeoutError` propagates through `RouterClient.aclose` and
`ChatController.shutdown` (`src/blackrose/chat.py::ChatController.shutdown`) into
the window's shutdown log (`src/blackrose/gui/app.py::_run_gui`, the "runtime
shutdown failed" handler). Only an uninterruptible (D-state) router reaches it.
Not demonstrated; SIGKILL is unblockable in practice. A retryable "stop owed" flag
would be the fix, which is new state.

**5. `fragile` — `src/blackrose/backend/service.py::RouterKey.create` and
`src/blackrose/backend/service.py::RouterService._launch` — crash windows around
the key file leak a file, not a credential.** `create` writes `.<name>.<hex>` then
`os.replace`; a SIGKILL between them leaves the temporary file, and a crash after
`key.create()` before `Popen`/after the router exited leaves the endpoint key.
Both are 0600 and are replaced on the next launch (`_launch`'s `key.create()`);
neither can authenticate against a router it was not created for. Not
demonstrated (needs a killed process). A sweep of `.<key name>.*` under the state
lock at startup would clean it up.

**6. `fragile` — `src/blackrose/backend/service.py::RouterService.close` —
`close()` skips the `Popen.poll()` when the handle is already gone.**
`_adopt`/`_launch` clear `_handle`/`_pid` for an exited process without clearing
`_process` (their stale-handle blocks), so `close()` returns at its
`_handle is None` check before the reap (`_process.poll()`). The child is already
dead; the zombie is reaped when the interpreter exits. Harmless, but it means
"close reaps every child it spawned" is not literally true. No change: moving one
`poll()` above the early return would fix it, but no code reads the exit status
afterwards.

**7. `fragile` (theoretical) — `src/blackrose/backend/router.py::RouterClient.capabilities`,
`src/blackrose/backend/router.py::RouterClient.load` and
`src/blackrose/backend/router.py::RouterClient.unload` — a router restart keeps
the capability cache.** Nothing invalidates `capabilities` when the endpoint is
relaunched (owned by `RouterService`, which the client does not observe), and
`unload` does not drop the entry either. While the launch command is fixed for
the desktop's lifetime (`src/blackrose/backend/service.py::RouterService._command`)
and a relaunch uses the same preset, a re-inspection would return the same values,
so this is benign today. If presets became hot-reloadable, a stale
`{'thinking': False}` would silently suppress reasoning effort. Would confirm by
loading a model, restarting the mock router with a different template, and
calling `load` again (no new `/props` request). Follow-up: a router generation
counter exposed by the service and compared by the client.

**8. `documentation-gap` — `src/blackrose/backend/router.py::RouterClient.load` —
the recorded limits do not match the code.** The original task description
describes `ensure_loaded()` polling for 180 s and requiring `n_ctx > 0`; the
method is `load()`, the bound is `request_timeout` (600 s default,
`src/blackrose/config.py::Config.request_timeout`, range-checked in
`src/blackrose/config.py::Config.__post_init__`), and `n_ctx` is never read.
`capabilities` stores only `thinking` and `reasoning_effort`
(`src/blackrose/backend/router.py::RouterClient._inspect`).

**9. `documentation-gap` — `src/blackrose/backend/service.py::RouterKey` and
`src/blackrose/backend/service.py::router_key` — "the key is generated for each
launch and removed after that router stops" is per endpoint, not per desktop.**
The path is `llama-router-<sha256(endpoint)[:16]>.key` (`router_key`), so every
Blackrose configured for that endpoint shares one key file; `RouterKey.owned` is
true even for an adopted router, so `close()` deletes the key a *previous* desktop
generated (`close`'s `key.remove()`). That is necessary for the documented
adopt-and-stop contract (the warning text in
`src/blackrose/backend/service.py::RouterService._check_adopted`) and is only
surprising combined with hazard 3.

**10. `none-found` — `src/blackrose/instance.py::desktop_instance` and
`src/blackrose/instance.py::DesktopOwnership` — the ownership state is total and
monotone.** `active` cannot be revived, the lock is held for every `Held`
observation, the file is never unlinked, and `require` is the single gate
(`src/blackrose/artifacts.py::ArtifactLifecycle.repair`). The only assumption is
cooperativeness: `flock` is advisory, so a non-cooperating process can share the
database. That is stated in [ADR 0001](../adr/0001-desktop-ownership.md) (the
decision paragraph).

**11. `none-found` — `src/blackrose/backend/router.py::RouterClient.models` —
`models()` ensures the router on every call, including each poll inside
`load()`.** A router killed mid-load is therefore relaunched (bounded by
`start_timeout`) instead of being polled forever; the residency deadline (`load`'s
`deadline`) is not extended by a launch, so a relaunch can consume it. No action:
the alternative (poll a dead port) is worse.

**12. `none-found` — `src/blackrose/backend/service.py::RouterService._ready` and
`src/blackrose/backend/service.py::RouterService._check_adopted` — adoption can
only warn about the two exposures it can detect.** A router protected by a key
Blackrose does not have is refused at `_ready` (401 → `key_rejected`) and never
adopted or stopped. A loopback router that accepts the endpoint's key file is
adopted and stopped silently; an anonymous or non-loopback one is adopted, stopped
and warned about (`_check_adopted`). The silent case is the configured endpoint's
router, and the warning text states the exit-stop contract for the cases the user
cannot infer. Judged intended, worth knowing.

## Formalization decisions

**Two source changes, each with a regression test that fails before it.**

- `src/blackrose/backend/service.py::RouterService._launch` — `_launch` is
  transactional after `Popen`. `output.start` and `os.pidfd_open` are now inside
  the failure path: on any exception the child is killed and reaped,
  `_process`/`_pid` are cleared and the owned key is removed before re-raising.
  Before the change, an fd-exhaustion `OSError` from `pidfd_open` left a detached
  router (its own session, so it survives the desktop) holding the port and the
  key, and `close()` returned at its `_handle is None` check without touching
  either. Closes the `Unsupervised` state above. Regression:
  `test_a_spawn_that_cannot_be_supervised_is_stopped_and_its_key_removed`
  (fails before: live process and key left). Denial:
  `test_a_failed_launch_never_removes_a_configured_key_file` — the same failure
  with `router_api_key_file` set must not unlink the user's file.
- `src/blackrose/backend/router.py::RouterClient.load` and
  `src/blackrose/backend/router.py::RouterClient._inspect` — `_inspect` returns
  `None` when `/props` fails and `load` caches only a real answer. Before, a
  single transient `/props` failure cached `{}`, which `local_levels`/`local_fields`
  read as "no thinking support" for the rest of the session
  (`src/blackrose/backend/effort.py::local_levels`,
  `src/blackrose/backend/effort.py::local_fields`), silently dropping reasoning
  effort. Regression:
  `test_a_failed_property_read_is_not_cached_as_missing_support` (fails before:
  cache contains `{}` and `/props` is never retried).

**No change proposed** for hazard 1 (a start-time guard needs a deterministic
`/proc` seam to test), hazard 2 (option-form parsing plus new warning tests),
hazard 3 (a lifetime endpoint ownership/lending protocol; `ADR 0001` scopes the
desktop lock to one database), hazard 4 (retryable stop obligation), hazard 5 (a
startup sweep), hazard 6 (no reader of the exit status), hazard 7 (a router
generation counter). Follow-ups worth tracking: hold router ownership for the
service lifetime or refuse a second adopter; accept `--port=`/`--host=`; guard
adoption with `/proc/<pid>/stat` start time; sweep abandoned key temporaries.

## Verification

Run from the repository root, offline. `scripts/check_unit_tests.py` runs each
unit in its own temporary XDG tree.

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_16_router_supervision.py -v
.venv/bin/python scripts/check_unit_tests.py -p test_router_service.py
.venv/bin/python scripts/check_unit_tests.py -p test_router_management_contract.py
.venv/bin/python scripts/check_unit_tests.py -p test_router_child_auth_probe.py
.venv/bin/python scripts/check_unit_tests.py -p test_router_deployed_probe.py
.venv/bin/python scripts/check_unit_tests.py -p test_instance.py
.venv/bin/python scripts/check_unit_tests.py -p test_effort.py
.venv/bin/python scripts/check_unit_tests.py -p test_local_images.py
.venv/bin/python scripts/check_unit_tests.py -p test_image_recovery.py
.venv/bin/python scripts/check_unit_tests.py -p test_gui_images.py
.venv/bin/python -m ruff check src/blackrose/backend/service.py src/blackrose/backend/router.py tests/test_sm_16_router_supervision.py
.venv/bin/python -m mypy
```

`tests/test_sm_16_router_supervision.py` pins the transitions, invariants and the
two fixes above; `test_router_service.py` and the router probes cover launch,
adoption, key handling and shutdown, and the remaining suites cover the
capability, ownership and local-image callers those fixes must preserve. Both
regression tests
(`test_a_spawn_that_cannot_be_supervised_is_stopped_and_its_key_removed` and
`test_a_failed_property_read_is_not_cached_as_missing_support`) fail against the
modules without the fixes and pass with them.
