# SM-16: Local router supervision, model residency and the desktop instance lock

## Scope and ownership

| Surface | Location |
| --- | --- |
| `DesktopOwnership` (`path`, `handle`, `active`, `require`) | `src/blackrose/instance.py:15-24` |
| `desktop_instance` (`flock`, context manager) | `src/blackrose/instance.py:27-43` |
| Desktop call site: lock acquired before runtime and router | `src/blackrose/gui/app.py:43-53` |
| Artifact CLI call sites (read-only and repair) | `src/blackrose/artifact_cli.py:34-49` |
| `RouterKey` (`path`, `owned`, `read`, `create`, `remove`, `auth_flow`) | `src/blackrose/backend/service.py:44-108` |
| Key path derivation (endpoint hash) | `src/blackrose/backend/service.py:111-115` |
| `RouterService` (`_pid`, `_handle`, `_process`, `_output`, `_operation`) | `src/blackrose/backend/service.py:118-373` |
| `_adopt` (/proc scan, identity checks) | `src/blackrose/backend/service.py:157-193` |
| `_launch` (key, spawn, pidfd) | `src/blackrose/backend/service.py:231-273` |
| `ensure_running` (startup flock, bounded readiness poll) | `src/blackrose/backend/service.py:275-316` |
| `close` (SIGTERM/SIGKILL, reap, key removal) | `src/blackrose/backend/service.py:338-373` |
| `RouterClient.load` (residency poll) | `src/blackrose/backend/router.py:69-129` |
| `RouterClient.unload` | `src/blackrose/backend/router.py:131-159` |
| `RouterClient._inspect` and `capabilities` cache | `src/blackrose/backend/router.py:24,117-121,161-177` |
| Capability consumers | `src/blackrose/backend/effort.py:16-38`, `src/blackrose/chat.py:200-206`, `src/blackrose/backend/router.py:183-189` |
| Callers of `ensure_running` / `close` | `src/blackrose/backend/router.py:35-37,240-245`, `src/blackrose/chat.py:239-259` |
| Decision record | `docs/adr/0001-desktop-ownership.md` |

Three owners meet here, and the architecture page states them separately
(`docs/architecture.md:34`, `:44-52`):

- **The chat database is owned by one desktop at a time**, via an advisory
  `fcntl.flock` on `<database dir>/desktop.lock` (`instance.py:28-43`). The lock
  is held for the whole life of the window — through storage, session and router
  shutdown (`gui/app.py:47-53`) — and is released by the OS on process death
  (`ADR 0001:9-13`).
- **The router process is owned by whichever desktop launched it**, and that
  ownership is *transferable* through `/proc` discovery: a router that survives a
  crashed desktop is adopted by the next one and stopped on its exit
  (`service.py:1`, `:46-51`, `:157-193`). Ownership is expressed as a pidfd plus
  a per-endpoint key file, not as an in-memory flag the next process can read.
- **Each model's residency** (`unloaded`/`loading`/`loaded`/`sleeping`/`failed`)
  is owned by the router (`llama-server`), not by Blackrose. `RouterClient` only
  observes it and caches the one derived fact it needs: whether the model's chat
  template supports `enable_thinking` and `supports_reasoning_effort`
  (`router.py:161-174`).

There is one `RouterService` and one `RouterClient` per runtime
(`runtime.py:49-52`), and one runtime per desktop window (`gui/app.py:68`). The
only cross-process serialization for a router is the per-endpoint startup flock
(`service.py:130,287-295`), which is a *startup* lock, not a lifetime lock; see
hazard 3.

## State variables

Desktop ownership:

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `path` | `instance.py:17` | `Path` | the `desktop.lock` this ownership covers | constructor |
| `handle` | `instance.py:18` | text `IO` | the open file description holding the `flock` | `desktop_instance:33` |
| `active` | `instance.py:19` | bool | this context still grants ownership | constructor (`True`), `:42` (`False`) |
| `desktop.lock` inode | `instance.py:33-35` | file on disk | the lock object; never unlinked (`:32`) | `desktop_instance` |

Router supervision (`RouterService` and `RouterKey`):

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `endpoint`, `host`, `port` | `service.py:120-123` | str/int | the configured router address | constructor |
| `local` | `service.py:124` | bool | `http` on a loopback host: adoption and launch are allowed | constructor |
| `key.path` | `service.py:53`, `111-115` | `Path` | `<state>/llama-router-<sha256(endpoint)[:16]>.key` (or the configured file) | `router_key` |
| `key.owned` | `service.py:55` | bool | true: Blackrose generated this key and may delete it; false: the user's file | `router_key` |
| `_pid` | `service.py:134` | `int \| None` | the router pid we hold a handle for | `_adopt:187`, `_launch:263`, cleared `:165,237,270,368` |
| `_handle` | `service.py:135` | `int \| None` | pidfd for `_pid`; the only thing ever signalled | same assignments |
| `_process` | `service.py:136` | `Popen \| None` | set only when *this* process spawned the router | `_launch:255`, cleared `:270,368` |
| `_output` | `service.py:137` | `RouterOutput \| None` | the drain thread for the child's stdout/stderr | `_launch:273`, cleared `:247,372` |
| `_operation` | `service.py:132` | `asyncio.Lock` | serializes `ensure_running` and `close` | both |
| `lock_path` | `service.py:130` | `Path` | `<state>/llama-router-<endpoint>.lock`, a startup-only `flock` | `ensure_running:287-295,316` |
| `start_timeout`, `stop_timeout` | `service.py:127-128` | float | 30 s startup bound, 60 s SIGTERM grace (`config.py:170-171`) | constructor |
| `_warnings` | `service.py:138` | `list[str]` | undelivered adoption warnings | `_check_adopted:220`, drained `take_warnings:140-143` |
| router key file | `service.py:85-97` | 0600 file | the credential the router requires on `/v1/models` | `RouterKey.create` (atomic replace) |
| router log | `service.py:129` | 0600 file | bounded stdout/stderr (`diagnostics.py:34-96`) | drain thread |

Model residency and the capability cache (`RouterClient`):

| Variable | Location | Type | Meaning | Writer |
| --- | --- | --- | --- | --- |
| `status.value` | router, read at `router.py:75-78,116-117,138-141` | `'unloaded' \| 'loading' \| 'loaded' \| 'sleeping' \| 'failed' \| other \| absent` | the router's residency for one model | the router only |
| `capabilities` | `router.py:24` | `dict[str, dict]` | per-model `{'thinking': bool, 'reasoning_effort': bool}` derived from `/props` | `load:117-121` (set), `load:83` (drop) |
| `timeout` | `router.py:20` | float | the poll deadline (`config.request_timeout`, 1..3600 s, 600 default) and the HTTP timeout | constructor |
| `budgets`, `stream_bounds` | `router.py:22-23` | config | reasoning budgets and SSE bounds, not residency state | constructor |

## States

**Desktop ownership** (mutually exclusive, derived from `active` and `handle`):

| State | Predicate |
| --- | --- |
| `Unlocked` | no open file description holds `LOCK_EX` on the path |
| `Held(owner)` | this process holds it; `ownership.active is True` and `not handle.closed` |
| `Released` | `active is False`, the handle may still be open until the `with` block exits |
| `Closed` | `handle.closed` — the OS has dropped the lock |

Only `Held` satisfies `DesktopOwnership.require` (`instance.py:21-24`); the three
non-`Held` states are indistinguishable to every consumer, so ownership is total.

**Router supervision** (per `RouterService`, orthogonal to `_output`/`_warnings`):

| State | Predicate |
| --- | --- |
| `Unattached` | `_handle is None and _pid is None and _process is None` |
| `Adopted` | `_handle is not None`, `_pid` is a process we did not spawn (`_process is None`) |
| `OwnedRunning` | `_handle is not None`, `_process is not None`, `_process.poll() is None` |
| `Exited` | `_handle is not None and _exited(_handle)`; `_process` may be an unreaped `Popen` |
| `Stopping` | `_operation` held inside `close`, handles already signalled (`service.py:340-360`) |
| `Relaunching` | transient inside `_launch`/`_adopt` while a stale handle is closed (`:160-165`, `:232-237`) |
| `Unsupervised` | forbidden: `_process` running with no `_handle` (was reachable before this change; see Formalization decisions) |

`Adopted` and `OwnedRunning` are distinguished by `_process`, which is why
`close()` reaps only a child it spawned (`:361-362`) while it signals either kind
through the pidfd (`:348,355`).

**Model residency** (per model id, as reported by the router):

| State | Predicate |
| --- | --- |
| `Unlisted` | no catalog row with that id (`router.py:72-74`) |
| `Incompatible` | `status` is absent/not a dict/no `value` (`:75-78`, `:99-100,147-148`) |
| `Unloaded` | `status.value == 'unloaded'` |
| `Loading` | `status.value == 'loading'` |
| `Loaded` | `status.value == 'loaded'` |
| `Sleeping` | `status.value == 'sleeping'` |
| `Failed` | `status.value == 'failed'` |
| `Unknown` | any other string |

Readiness is the joint predicate `Loaded or Sleeping` (`:117`). The capability
cache has two states per model: `Cached` (`model in capabilities`) and
`Uncached`. A failed `/props` read now leaves the model `Uncached` instead of
recording `Cached({})` (`:117-121,173-174`).

## Events

- Desktop start: `run_gui` enters `desktop_instance` before building the runtime
  (`gui/app.py:43-53`); `main` turns `AlreadyRunning` into exit code 1
  (`main.py:168-173`).
- Desktop exit, including a raised startup error: the context manager's
  `finally` runs (`gui/app.py:110-119`, `instance.py:39-43`).
- Artifact CLI: read-only inventory under `create=False` (`artifact_cli.py:34-40`),
  repair under `create=True` (`:41-49`).
- Any catalog read: `RouterClient.models()` calls `service.ensure_running()`
  (`router.py:35-37`); reached from `ChatController.start/load_model/select_model`
  (`chat.py:239-243,306`), `refresh_models` (`:253-256`), `local_images` restore
  (`local_images.py:606`) and the GUI picker.
- Router shutdown: `ChatController.shutdown` → `ChatProviders.aclose` →
  `RouterClient.aclose` → `RouterService.close` (`chat.py:246-259`,
  `providers.py:82-86`, `router.py:240-245`).
- Another Blackrose starting a router on the same endpoint: startup flock
  contention, then a re-check for readiness (`service.py:287-302`).
- Router death, connection refusal, `/v1/models` 503 (still warming) or 401
  (key rejected) (`service.py:146-155,279-312`, `router.py:38-40`).
- `/proc` scan finding zero or several candidate routers (`service.py:170-193`).
- SIGTERM grace expiry and escalation to SIGKILL (`service.py:349-356`).
- A model load accepted, rejected as busy (`500`), raced (`400`), missing route
  (`404`) or failed (`success: false`) (`router.py:85-113`).
- A model going `unloaded`/`loading` again while `load()` polls
  (`router.py:114-129`), and `unload()` observing `failed` or a vanished row
  (`:152-158`).
- A `/props` read failing (`router.py:167-174`).

## Transition table

Desktop ownership:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unlocked` | `desktop_instance(path, create=True)` | parent created (`:30-31`), file opened `a` (`:33`) | `flock(LOCK_EX\|LOCK_NB)` (`:35`) | `Held` |
| `Unlocked` | `desktop_instance(path, create=False)` | file missing | `FileNotFoundError` from `:33`; caller reports it (`artifact_cli.py:54-56`) | `Unlocked` |
| `Held(other)` | `desktop_instance` | `BlockingIOError` (`:36`) | `AlreadyRunning` (`:37`); the failed attempt closes its own fd | `Unlocked` |
| `Held` | body returns or raises | — | `active = False` (`:42`) **then** `LOCK_UN` (`:43`), then the `with` closes the fd (`:33`) | `Released` → `Closed` |
| `Held` | `require(db)` | `db == ':memory:'` or lock not beside `db` (`:22-23`) | `RuntimeError` | `Held` |
| `Released`/`Closed` | `require(db)` | `not active or handle.closed` (`:22`) | `RuntimeError` | unchanged |

Router supervision:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unattached` | `ensure_running`, router answers with our key | `_ready()` True (`:280`) | `_adopt()` (`:281`) finds a matching pid and takes a pidfd (`:183-187`) | `Adopted` |
| `Unattached` | `ensure_running`, router answers but no pid matches | `_ready()` True, `_adopt` exhausts `/proc` | `RuntimeError` (`:192-193`); nothing signalled; fields stay `None` | `Unattached` |
| `Unattached` | `ensure_running`, nothing listening | `ConnectError` (`:283`) | take startup flock, bounded by `start_timeout` (`:287-295`) | `Unattached` (inside the startup section) |
| startup | re-check after the lock, another window is ready | `_ready()` True (`:298`) | skip the poll loop (`:302`) and adopt (`:312`) | `Adopted` |
| startup | re-check after the lock, still refused | `ConnectError` (`:299`) | `_launch()`: key, spawn, pidfd (`:300`, `:248-273`) | `OwnedRunning` |
| startup | router answers 503 (warming) | `_ready()` False (`:147-148`) | poll every 0.1 s, never launch (`:302-311`) | `Unattached` until ready |
| `OwnedRunning`/`Adopted` | router becomes ready | `_ready()` True (`:308`) | `_adopt()` (`:312`); our own handle is alive, so it returns at `:160-162` | unchanged |
| `OwnedRunning` | our child exits during startup | `_process.poll() is not None` (`:303`) | `RuntimeError` with the exit code (`:304-305`); handle and key retained for `close()` | `Exited` |
| `OwnedRunning`/`Adopted` | router exits at any time | `_exited(_handle)` (`:161,233,345`) | next `_adopt`/`_launch` closes the pidfd and clears `_pid` (`:163-165,235-237`) | `Unattached` (`_process` may stay as an unreaped `Popen`) |
| `Exited` | `close()` | `_exited` True (`:345`) | skip signals, `_process.poll()` (`:361-362`), `key.remove()` (`:364`), close handle (`:367`) | `Unattached` |
| `OwnedRunning`/`Adopted` | `close()` | handle alive (`:345`) | collect descendant pidfds (`:346`), SIGTERM (`:348`), wait `stop_timeout` (`:349-357`), SIGKILL survivors (`:352-356`) | `Stopping` |
| `Stopping` | all handles report exit | every `_exited` (`:358`) | poll child, remove owned key (`:361-364`), close handles and clear fields (`:366-368`) | `Unattached` |
| `Stopping` | SIGKILL survivors after 5 s | `asyncio.timeout(5)` (`:358`) | `TimeoutError` escapes; handles closed and fields cleared (`:365-368`), key **kept** (`:364` not reached) | `Unattached` with a surviving router (hazard 4) |
| `OwnedRunning` | post-spawn supervision fails | `output.start`/`pidfd_open` raises (`:265-272`) | kill and wait the child, clear `_process`/`_pid`, remove the owned key, re-raise (fix 1) | `Unattached` |
| `Unattached` | `close()` | `_handle is None` (`:341`) | return; join `_output` and close HTTP (`:369-372`) | `Unattached` |
| any | `ensure_running` or `close` re-entered | `_operation` (`:276,340`) | the second caller waits | unchanged |

Model residency and capabilities:

| from | event | guard | effect | to |
| --- | --- | --- | --- | --- |
| `Unlisted` | `load` | catalog lacks the id (`:72-74`) | `ValueError` | `Unlisted` |
| `Incompatible` | `load`/`unload` | no `status.value` (`:75-78`, `:138-139`) | `_management_required` (`:60-67`) | unchanged |
| `Unloaded`/`Failed`/`Unknown` | `load` | status not in `loaded/sleeping/loading` (`:82`) | drop the cache (`:83`), `POST /models/load` (`:86`) | `Loading` |
| `Loading` | `load` | POST returns busy `500` (`:96`) | re-read the catalog, wait, retry until the deadline (`:106-113`) | `Loading` |
| `Loading` | `load` | POST returns raced `400` (`:97`) | re-read; if the row is now `loaded/sleeping/loading`, stop posting (`:110-113`) | `Loading` |
| `Loading` | `load` | POST 404 / other error / `success: false` (`:99-103`) | `_management_required`, HTTP error or `RuntimeError` | `Failed`/unchanged |
| `Loading` | poll reads `loaded`/`sleeping` | `:117` | inspect once if uncached (`:118-121`), return the fresh catalog | `Loaded`/`Sleeping`, `Cached` |
| `Loading` | poll reads `loading`/`unloaded` | `:123` | sleep ≤0.25 s, re-read (`:125-129`) | unchanged |
| `Loading` | deadline passes | `:125-127` (and `:106-108`) | `TimeoutError` | unchanged |
| `Loading` | poll reads `failed` or an unknown value | `:123-124` | `RuntimeError` with the status | `Failed`/`Unknown` |
| `Loaded`/`Sleeping` | `load` | cached (`:118`) | no `/props` call, return the catalog | unchanged |
| `Cached` | `load` starts a management load | `:83` | entry dropped | `Uncached` |
| `Uncached` | `/props` answers | `:167-172` | cache `{'thinking', 'reasoning_effort'}` (`:117-121`) | `Cached` |
| `Uncached` | `/props` fails | `:173-174` | cache stays unset (fix 2) | `Uncached` |
| `Loaded`/`Sleeping`/`Loading` | `unload` | status not `unloaded` (`:141-142`) | `POST /models/unload` (`:144`) | `Unloaded` after polling |
| `Unloaded` | `unload` | status already `unloaded` (`:141-142`) | return without a request | `Unloaded` |
| `unloading` | poll reads `unloaded` | `:155-156` | return | `Unloaded` |
| `unloading` | poll reads `failed` or the row vanished | `:157-158` | `RuntimeError` | unchanged |

## Invariants

1. **One holder per lock path.** A cooperating process cannot enter twice; a second
   open file description conflicts even inside one process (`instance.py:33-37`).
   Pinned by `tests/test_instance.py:24-32` and
   `test_ownership_is_valid_only_while_the_context_holds_the_lock`.
2. **Ownership is valid only inside the context, and never revived.**
   `active` is written `True` once (`instance.py:19`) and `False` once (`:42`);
   `require` rejects either `not active` or a closed handle (`:22`). Pinned by the
   same test.
3. **`active` is cleared before the lock is released.** `:42` precedes `:43`, so a
   consumer that observes `active` cannot then act after the release. Assumed by
   `artifacts.py:236-238`.
4. **The lock file is never unlinked** (`instance.py:32`; `ADR 0001:11-13`), so a
   waiting process never locks an orphaned inode.
5. **`require` accepts only the lock beside this database** (`instance.py:22-24`).
   Pinned by `test_require_accepts_only_the_lock_beside_this_database` and
   `tests/test_artifacts.py:390-399`.
6. **A spawned router always has a pidfd.** Every statement after `Popen` is inside
   the transactional tail (`service.py:265-272`); a failure kills and reaps the
   child and removes the launch key, so `_process` running with `_handle is None`
   is unreachable. Pinned by
   `test_a_spawn_that_cannot_be_supervised_is_stopped_and_its_key_removed` (fails
   before the change).
7. **`_pid` and `_handle` are both set or both unset.** They are assigned together
   (`service.py:187,263-264`) and cleared together (`:164-165,236-237,270,368`).
   Assumed by `close()`'s `handles = [self._handle]` (`:343`) and `_descendant_handles`
   (`:318-336`).
8. **Nothing is signalled without an identified pidfd.** Every signal goes through
   `_handle` (`service.py:348,355`), which comes from `pidfd_open` of a `/proc`
   entry that matched uid, executable, `--models-preset`, port and host
   (`:173-187`), or from our own `Popen` (`:263-264`). Pinned by
   `test_adoption_refuses_a_process_serving_another_port` and
   `test_adoption_requires_separate_port_and_host_arguments`.
9. **The owned key is removed only after its router exited.** `key.remove()` runs
   inside `close`'s `try` after the wait (`service.py:361-364`) and on every failed
   launch (`:259,271`); a router that survives SIGKILL keeps its key
   (`:363` comment). Pinned by `test_close_reaps_the_owned_router_and_is_idempotent`
   and `tests/test_router_service.py:214-222`.
10. **A configured key file is never removed.** Only `key.owned` is unlinked
    (`service.py:99-102`). Pinned by
    `test_a_failed_launch_never_removes_a_configured_key_file` and
    `tests/test_router_service.py:140-166`.
11. **Startup is bounded.** The lock wait, launch and readiness poll are all inside
    `asyncio.timeout(start_timeout)` (`service.py:289-316`), so a warming or
    crashing router cannot hold the caller past the configured 30 s. Pinned by
    `tests/test_router_service.py:239-248`,
    `test_a_router_that_is_still_warming_up_is_waited_for_not_duplicated` and
    `test_an_exited_router_is_relaunched_with_a_fresh_key`.
12. **A warming router is waited for, not duplicated.** `_ready()` returning
    `False` for 503 enters the poll loop; only `ConnectError` reaches `_launch()`
    (`service.py:297-311`). Pinned by
    `test_a_router_that_is_still_warming_up_is_waited_for_not_duplicated`.
13. **One router operation at a time per service.** `_operation` wraps the whole of
    `ensure_running` (`service.py:276`) and `close` (`:340`); no interleaving can
    observe a half-launched or half-stopped service.
14. **Adoption is all-or-nothing.** `_adopt` writes `_pid` and `_handle` in one
    statement (`service.py:187`) or raises, and a rejected candidate is closed and
    skipped (`:183-186`). `test_adoption_refuses_a_process_serving_another_port`
    asserts the refused process is still alive.
15. **Adoption warnings are delivered once.** `take_warnings` swaps the list
    (`service.py:140-143`); pinned by `tests/test_router_service.py:185-201`.
16. **Readiness is only `loaded` or `sleeping`.** `loading` and `unloaded` keep
    polling (`router.py:117-128`), so a model that falls back to `unloaded` during
    a poll is never reported ready. Pinned by
    `test_a_model_that_falls_back_to_unloaded_is_waited_for`.
17. **Every residency wait is bounded.** `load` computes one deadline (`:79`) and
    checks it before each sleep in both loops (`:106-108,125-127`); `unload` wraps
    its poll in `asyncio.timeout(timeout)` (`:152`). Each individual HTTP call is
    still bounded by the client's `request_timeout`, so the worst case is the
    deadline plus one request. Pinned by
    `test_a_model_that_never_becomes_ready_is_bounded`.
18. **The capability cache is per model id.** Keys are the model id only
    (`router.py:24,118-121,176-177`), so a switch cannot reuse another model's
    entry; a management reload of the same model drops it first (`:83`). Pinned by
    `test_capabilities_are_per_model_across_a_switch` and
    `test_capabilities_are_dropped_and_reread_before_a_reload`.
19. **A cached capability is a successful `/props` observation.** `_inspect`
    returns `None` on failure and `load` caches only a dict
    (`router.py:117-121,161-174`). Pinned by
    `test_a_failed_property_read_is_not_cached_as_missing_support` (fails before
    the change).
20. **A management load sends no generation overrides.** The POST body is exactly
    `{'model': model}` (`router.py:84-86`); sampling, context and GPU settings stay
    with the server preset, and `local_fields` only adds a reasoning request when
    the user explicitly chose an effort (`effort.py:29-38`).
21. **`stream` never invents capabilities.** It reads `capabilities.get(model)`
    (`router.py:189`); an uncached model therefore sends no reasoning fields
    (`effort.py:30`), which is the same wire result as "no support".

## Hazards and open questions

Answers to the bead's focus questions first.

- *Is the supervision state total (unattached / adopted / owned-launched / exited /
  stopping)?* Yes, with one former hole now closed. `_pid`/`_handle` move together
  (invariant 7), `_process` distinguishes owned from adopted, and `close` is the
  only `Stopping` state. The hole was `_launch`: `Popen` succeeded but
  `output.start`/`os.pidfd_open` could raise, leaving a live `_process` with no
  handle that `close()` refused to touch (`:341-342`). That is now transactional
  (`:265-272`), so the state table above has no `Unsupervised` row.
- *Can adoption pick a PID that is not our router?* It cannot pick one that does
  not answer the endpoint with our key (`_ready()` runs first, `:280`), and it
  verifies uid, `/proc/<pid>/exe`, `--models-preset`, port and host before taking
  the pidfd (`:170-187`). Two gaps remain: the identity checks happen *before*
  `pidfd_open`, so a pid recycled in between would be adopted (hazard 1), and a
  router started with `--port=NNNN` cannot be adopted at all (hazard 2). Every
  refusal leaves the candidate unsignalled (invariant 14).
- *What happens if the process crashes between `key.prepare()` and `key.remove()`?*
  There is no `prepare()`; the sequence is `key.create()` → `Popen` →
  `key.remove()` (`service.py:248-273`). A crash leaves the 0600 key at the
  deterministic endpoint path (`:111-115`). It is not adopted *as a credential*:
  the next desktop either uses it to adopt the surviving router it belongs to
  (`:280-281`, pinned by `tests/test_router_service.py:203-212`) or replaces it
  atomically with `key.create()` before spawning a new router (`:250`,
  `os.replace` at `:93`). A crash between the temporary write and `os.replace`
  leaks a `.<name>.<hex>` file (hazard 5); no stale key can authenticate against a
  router it was not generated for.
- *Does `stop()` always reap the child and remove the key it owns?* `close()` is the
  stop path. For any child with a pidfd: yes — SIGTERM, wait `stop_timeout`,
  SIGKILL, `_process.poll()`, then `key.remove()` (`:345-364`). For a surviving
  router (hazard 4) no: the key is deliberately kept and the pidfd is dropped. For
  an owned key whose launch failed, yes (`:259,271`). The one residue is an
  already-exited `Popen` that `_adopt` detached (`:163-165`), which `close()` does
  not `poll()` because it returns at `:341` (hazard 6 — a zombie, no live
  resource).
- *Can two desktop instances both believe they own the router?* Yes, when they have
  different chat database directories: `desktop.lock` is per database directory
  (`gui/app.py:50-52`), and the endpoint lock is released when startup finishes
  (`service.py:315-316`). Both then hold pidfds for the same router and either
  close stops it (hazard 3, pinned). Same database directory: no — the second
  window never builds a runtime (`gui/app.py:43-53`).
- *Is `ensure_loaded` polling bounded, and does it handle `loaded -> unloaded`?*
  There is no `ensure_loaded`; the call is `RouterClient.load` (`router.py:69`).
  It is bounded by one deadline derived from `request_timeout` (`:79`,
  `:106-108,125-127`) plus at most one in-flight request. A model that returns to
  `unloaded` during the poll is tolerated and polled again (`:123-128`), never
  reported ready; it ends in `Loaded`, `Failed` or `TimeoutError`
  (`test_a_model_that_falls_back_to_unloaded_is_waited_for`,
  `test_a_model_that_never_becomes_ready_is_bounded`).
- *Are capabilities cached past a model switch?* No per-model leakage: the cache is
  keyed by model id (invariant 18) and re-read after a management reload
  (`:83,117-121`). Two staleness cases remain: a failed `/props` is no longer
  cached (fixed), but a router *restart* under the same model id keeps the old
  entry, and `unload()` does not drop it (hazard 7).

**1. `fragile` — `service.py:170-187` — adoption verifies the pid before opening
its pidfd.** The identity evidence is strong (uid, executable, preset flag, port,
host, plus the endpoint answering with our key), but the pidfd is taken last, so a
pid recycled between `(entry / 'cmdline').read_bytes()` (`:176`) and
`os.pidfd_open` (`:183`) would yield a handle to an unrelated process that
`close()` would later SIGTERM/SIGKILL. Requires a pid wrap inside that window
(two `/proc` reads), so it is theoretical here, but the consequence is signalling
a foreign process. Not demonstrated: it needs interposed syscalls or a fake
`/proc`. Would confirm by patching `os.pidfd_open` to return a pidfd for a second
live process and checking that `_adopt` accepts it. Follow-up: read
`/proc/<pid>/stat` field 22 (start time) before and after `pidfd_open` and reject a
mismatch — four lines, but only worth adding with a deterministic test seam.

**2. `fragile` — `service.py:179-181` — the `/proc` parser only understands
space-separated options.** `arguments[arguments.index('--port') + 1]` raises
`ValueError` for `--port=NNNN`, which is caught and the candidate skipped
(`:189-191`), so a router Blackrose can talk to (`_ready` True) cannot be adopted
and startup fails with "Cannot identify the local llama-router process".
Characterised by `test_adoption_requires_separate_port_and_host_arguments`.
Accepting `--port=`/`--host=` forms is a small change but needs its own refusal
tests for the ambiguous `--host=0.0.0.0` warning path.

**3. `fragile` — `service.py:130,287-316` — the endpoint lock is a startup lock,
not an ownership lock.** `desktop.lock` is per chat database (`gui/app.py:50-52`),
so two windows with different database directories, a scratch runtime, or the
artifact CLI may each adopt the same router; the first `close()` SIGTERMs the
router the other is still using (`service.py:345-355`). Pinned as current behaviour
by `test_the_endpoint_lock_is_not_a_lifetime_ownership_lock`. `ADR 0001:15-18`
scopes the desktop lock to one database directory, so this is a known gap rather
than an accident, and the adoption warning only mentions the exit-stop contract for
anonymous or exposed routers (`:206-218`). A fix is a protocol change: hold the
endpoint lock for the service lifetime and add a "borrowed, do not stop" state, or
refuse to adopt a router another live Blackrose holds. Not attempted.

**4. `fragile` — `service.py:358-364` — a router that survives SIGKILL makes
`close()` raise and is then unreachable.** The 5 s post-SIGKILL wait is inside
`asyncio.timeout`; on expiry the `finally` closes the pidfds and clears
`_pid`/`_handle`/`_process` (`:365-368`), `key.remove()` (`:364`) is skipped, and the
`TimeoutError` propagates through `RouterClient.aclose` and
`ChatController.shutdown` (`chat.py:246-259`) into the window's shutdown log
(`gui/app.py:113-117`). Only an uninterruptible (D-state) router reaches it. Not
demonstrated; SIGKILL is unblockable in practice. A retryable "stop owed" flag
would be the fix, which is new state.

**5. `fragile` — `service.py:85-102,248-259` — crash windows around the key file
leak a file, not a credential.** `create` writes `.<name>.<hex>` then
`os.replace` (`:88-93`); a SIGKILL between them leaves the temporary file, and a
crash after `key.create()` before `Popen`/after the router exited leaves the
endpoint key. Both are 0600 and are replaced on the next launch (`:250`); neither
can authenticate against a router it was not created for. Not demonstrated (needs
a killed process). A sweep of `.<key name>.*` under the state lock at startup would
clean it up.

**6. `fragile` — `service.py:341-342` — `close()` skips the `Popen.poll()` when the
handle is already gone.** `_adopt`/`_launch` clear `_handle`/`_pid` for an exited
process without clearing `_process` (`:163-165,235-237`), so `close()` returns at
`:341` before the reap at `:361-362`. The child is already dead; the zombie is
reaped when the interpreter exits. Harmless, but it means "close reaps every child
it spawned" is not literally true. No change: moving one `poll()` above the early
return would fix it, but no code reads the exit status afterwards.

**7. `fragile` (theoretical) — `router.py:24,117-121,131-159` — a router restart
keeps the capability cache.** Nothing invalidates `capabilities` when the endpoint
is relaunched (owned by `RouterService`, which the client does not observe), and
`unload` does not drop the entry either. While the launch command is fixed for the
desktop's lifetime (`service.py:222-229`) and a relaunch uses the same preset, a
re-inspection would return the same values, so this is benign today. If presets
became hot-reloadable, a stale `{'thinking': False}` would silently suppress
reasoning effort. Would confirm by loading a model, restarting the mock router
with a different template, and calling `load` again (no new `/props` request).
Follow-up: a router generation counter exposed by the service and compared by the
client.

**8. `documentation-gap` — `router.py:69-129` — the recorded limits do not match
the code.** The bead describes `ensure_loaded()` polling for 180 s and requiring
`n_ctx > 0`; the method is `load()`, the bound is `request_timeout` (600 s default,
`config.py:161,246-247`), and `n_ctx` is never read. `capabilities` stores only
`thinking` and `reasoning_effort` (`:171-172`).

**9. `documentation-gap` — `service.py:46-51,111-115` — "the key is generated for
each launch and removed after that router stops" is per endpoint, not per
desktop.** The path is `llama-router-<sha256(endpoint)[:16]>.key`
(`:111-115`), so every Blackrose configured for that endpoint shares one key file;
`RouterKey.owned` is true even for an adopted router, so `close()` deletes the key
a *previous* desktop generated (`:364`). That is necessary for the documented
adopt-and-stop contract (`:211-218`) and is only surprising combined with
hazard 3.

**10. `none-found` — `instance.py:15-43` — the ownership state is total and
monotone.** `active` cannot be revived, the lock is held for every `Held`
observation, the file is never unlinked, and `require` is the single gate
(`artifacts.py:236-238`). The only assumption is cooperativeness: `flock` is
advisory, so a non-cooperating process can share the database. That is stated in
`ADR 0001:9-13`.

**11. `none-found` — `router.py:36-37` — `models()` ensures the router on every
call, including each poll inside `load()`.** A router killed mid-load is therefore
relaunched (bounded by `start_timeout`) instead of being polled forever; the
residency deadline (`router.py:79`) is not extended by a launch, so a relaunch can
consume it. No action: the alternative (poll a dead port) is worse.

**12. `none-found` — `service.py:145-155,195-220` — adoption can only warn about
the two exposures it can detect.** A router protected by a key Blackrose does not
have is refused at `_ready` (401 → `key_rejected`) and never adopted or stopped.
A loopback router that accepts the endpoint's key file is adopted and stopped
silently; an anonymous or non-loopback one is adopted, stopped and warned about
(`:205-218`). The silent case is the configured endpoint's router, and the warning
text states the exit-stop contract for the cases the user cannot infer. Judged
intended, worth knowing.

## Formalization decisions

**Two source changes, each with a regression test that fails before it.**

- `service.py:253-273` — `_launch` is transactional after `Popen`. `output.start`
  and `os.pidfd_open` are now inside the failure path: on any exception the child
  is killed and reaped, `_process`/`_pid` are cleared and the owned key is
  removed before re-raising. Before the change, an fd-exhaustion
  `OSError` from `pidfd_open` left a detached router (its own session, so it
  survives the desktop) holding the port and the key, and `close()` returned at
  `:341` without touching either. Closes the `Unsupervised` state above. Regression:
  `test_a_spawn_that_cannot_be_supervised_is_stopped_and_its_key_removed`
  (fails before: live process and key left). Denial:
  `test_a_failed_launch_never_removes_a_configured_key_file` — the same failure
  with `router_api_key_file` set must not unlink the user's file.
- `router.py:117-121,161-174` — `_inspect` returns `None` when `/props` fails and
  `load` caches only a real answer. Before, a single transient `/props` failure
  cached `{}`, which `local_levels`/`local_fields` read as "no thinking support"
  for the rest of the session (`effort.py:16-19,29-38`), silently dropping
  reasoning effort. Regression:
  `test_a_failed_property_read_is_not_cached_as_missing_support` (fails before:
  cache contains `{}` and `/props` is never retried).

**No change proposed** for hazard 1 (a start-time guard needs a deterministic
`/proc` seam to test), hazard 2 (option-form parsing plus new warning tests),
hazard 3 (a lifetime endpoint ownership/lending protocol; `ADR 0001` scopes the
desktop lock to one database), hazard 4 (retryable stop obligation), hazard 5 (a
startup sweep), hazard 6 (no reader of the exit status), hazard 7 (a router
generation counter). Follow-ups worth beads: hold router ownership for the
service lifetime or refuse a second adopter; accept `--port=`/`--host=`; guard
adoption with `/proc/<pid>/stat` start time; sweep abandoned key temporaries.

## Verification

```
.venv/bin/python scripts/check_unit_tests.py -p test_sm_16_router_supervision.py -v
    Ran 16 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_router_service.py
    Ran 12 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_router_management_contract.py
    Ran 14 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_router_child_auth_probe.py
    Ran 5 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_router_deployed_probe.py
    Ran 6 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_instance.py
    Ran 3 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_effort.py
    Ran 14 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_local_images.py
    Ran 43 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_image_recovery.py
    Ran 7 tests … OK
.venv/bin/python scripts/check_unit_tests.py -p test_gui_images.py
    Ran 9 tests … OK
.venv/bin/python -m ruff check src/blackrose/backend/service.py src/blackrose/backend/router.py tests/test_sm_16_router_supervision.py
    All checks passed!
.venv/bin/python -m mypy
    src/blackrose/diagram_repairs.py:36-37: 2 errors (another bead's in-flight file;
    instance.py is unchanged by this work)
git diff --check
    (clean)
```

Both regression tests were confirmed to fail against the `HEAD` versions of the
two modules (`Ran 16 tests … FAILED (failures=2)`) and pass with the change. There
is no `tests/test_service.py`; the router service is covered by
`test_router_service.py` plus the probes above. The full offline gate
(`scripts/check_health.py`) was not run because other beads' uncommitted changes
are present in this working tree (their files, not this bead's); the focused set
above is green.
