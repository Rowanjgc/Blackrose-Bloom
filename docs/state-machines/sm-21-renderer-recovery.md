# SM-21: WebKit renderer recovery, the native consent sheet, external hand-off and the bridge emitter

## Scope and ownership

| Area | Files and symbols | Owner |
| --- | --- | --- |
| Renderer crash recovery | `src/blackrose/gui/renderer_recovery.py::RendererRecovery`: its class constants (`src/blackrose/gui/renderer_recovery.py::RendererRecovery.SETTLE_TIMEOUT_SECONDS`, `READY_TIMEOUT_SECONDS`, `PERMISSION_POLL_MILLISECONDS`, `MAX_RELOADS`, `RELOAD`, `STOP`, `CLOSE`), fields (`src/blackrose/gui/renderer_recovery.py::RendererRecovery.__init__`), `attach`, `_web_process_terminated`, `_handle_termination`, `_begin_settlement`, `_wait_for_settlement`, `_settlement_finished`, `_resume_after_permission`, `_issue_reload`, `page_ready`, `window_closed`, `_cleanup_closed_window`, `_page_ready`, `_ready_timeout`, `_show_fallback`, `_dialog_response`, `_close_window` | `RendererRecovery` object on the GTK/main thread. The pywebview window owns it (`window._blackrose_renderer_recovery` in `src/blackrose/gui/app.py::_run_gui`); it keeps weak references to the window and `Api` and only queues GTK work. `Session` still owns cancellation and durable turn settlement (`src/blackrose/gui/session.py::Session.settle_after_renderer_termination`), per the [architecture ownership table](../architecture.md#modules-and-invariants). |
| Native consent sheet | `src/blackrose/gui/consent.py`: `src/blackrose/gui/consent.py::ARM_DELAY_MS`, `src/blackrose/gui/consent.py::_CLOSE_POLL_SECONDS`, `src/blackrose/gui/consent.py::ConsentSheet`, its `_turn`/`_current`/`_closed` fields (`src/blackrose/gui/consent.py::ConsentSheet.__init__`), `ask`, `_show`, `_arm`, `_refocus`, `_key`, `_answer`, `_settled`, `_destroyed` | A GTK widget inside the window's `Gtk.Overlay` (built in `src/blackrose/gui/consent.py::ConsentSheet.__init__`), installed by `src/blackrose/gui/consent.py::install` on `before_show`. It is the only native approval surface; callers on other threads wait for the GTK thread to show and answer it. |
| External hand-off | `src/blackrose/gui/external.py`: `src/blackrose/gui/external.py::_asking`, `src/blackrose/gui/external.py::Handoff`/`src/blackrose/gui/external.py::Handler`/`src/blackrose/gui/external.py::Confirmation`, `src/blackrose/gui/external.py::link`/`src/blackrose/gui/external.py::saved_file`/`src/blackrose/gui/external.py::folder`, `src/blackrose/gui/external.py::handler_for`, `src/blackrose/gui/external.py::confirmation`, `src/blackrose/gui/external.py::ask`, `src/blackrose/gui/external.py::launch`, `src/blackrose/gui/external.py::hand_off` | The desktop process, called from pywebview worker threads (`src/blackrose/gui/bridge.py::Api.open_link`, `src/blackrose/gui/bridge.py::Api.open_artifact`). It is the only path from the window to another application (`architecture.md`). |
| Bridge emitter and handshake | `src/blackrose/gui/bridge.py`: `src/blackrose/gui/bridge.py::_BATCH_SECONDS`, `src/blackrose/gui/bridge.py::Emitter`, `src/blackrose/gui/bridge.py::coalesce`, `src/blackrose/gui/bridge.py::Api.ready`, `src/blackrose/gui/bridge.py::Api._redraw`, `src/blackrose/gui/bridge.py::Api._shutdown` | The emitter thread owns delivery; the loop thread owns the session; the main thread owns `evaluate_js`. `Api.ready` is called by pywebview on a worker thread. |
| Window life cycle | `src/blackrose/gui/app.py`: `src/blackrose/gui/app.py::_SHUTDOWN_SECONDS`, `src/blackrose/gui/app.py::gui_problem`, `src/blackrose/gui/app.py::run_gui`, `src/blackrose/gui/app.py::_run_gui`, `src/blackrose/gui/app.py::_exit_past_stuck_workers`, `src/blackrose/gui/app.py::_route_exit_signals` | The process/main thread; `desktop_instance` holds the one-owner lock (`src/blackrose/instance.py::desktop_instance`). |

The surrounding contract is that the page renders state and never owns turn or
tool policy, the renderer never replays a request, approvals stay outside page
script, and every hand-off is host-described.

## State variables

`RendererRecovery` fields are initialised in
`src/blackrose/gui/renderer_recovery.py::RendererRecovery.__init__`, `ConsentSheet`
fields in `src/blackrose/gui/consent.py::ConsentSheet.__init__`, `Emitter` fields in
`src/blackrose/gui/bridge.py::Emitter.__init__` and `Api` fields in
`src/blackrose/gui/bridge.py::Api.__init__`. Writers are methods of the same class
unless a path is given.

| Variable | Owner | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `_termination_count` | `RendererRecovery` | `int` | Diagnostic count of renderer-termination signals | `_handle_termination` |
| `_reload_count` | `RendererRecovery` | `int 0..MAX_RELOADS` | Host loads issued for this window | `_issue_reload` |
| `_automatic_used` | `RendererRecovery` | `bool` | The one automatic reload has been spent | `_handle_termination` (first-crash branch) |
| `_settling` | `RendererRecovery` | `bool` | A settlement worker is outstanding | `_begin_settlement` (set; reset on both failure paths), `_settlement_finished` |
| `_waiting_permission` | `RendererRecovery` | `bool` | A native decision is outstanding; a poll is armed | `_settlement_finished`, `_resume_after_permission` |
| `_failure_during_settlement` | `RendererRecovery` | `bool` | The renderer died again before settlement completed | `_handle_termination` (settling and waiting-permission branches); reset by `_begin_settlement` unless `preserve_failure` |
| `_pending_reload` | `RendererRecovery` | `bool` | Whether the current settlement should end in a reload | `_begin_settlement` |
| `_reloading` | `RendererRecovery` | `bool` | A host load was issued and its handshake is outstanding | `_issue_reload`, `_page_ready`, `_ready_timeout`, `_show_fallback` |
| `_ready_timeout_id` | `RendererRecovery` | GLib source id or `None` | The 15 s handshake deadline | `_issue_reload`, `_clear_ready_timeout` |
| `_permission_timer_id` | `RendererRecovery` | GLib source id or `None` | The 500 ms permission poll | `_settlement_finished`, `_resume_after_permission`, `_cleanup_closed_window` |
| `_dialog` | `RendererRecovery` | `Gtk.MessageDialog` or `None` | The one native fallback dialog | `_show_fallback`, `_dismiss_dialog`, `_dialog_response` |
| `_closed` | `RendererRecovery` | `bool` | The window/recovery is terminal | `window_closed`, `_native_window_destroyed`, `_cleanup_closed_window` (also reached from `_close_window`) |
| `_cleanup_done` | `RendererRecovery` | `bool` | One-shot cleanup latch | `_cleanup_closed_window` |
| `_turn` | `ConsentSheet` | `threading.Lock` | Serializes `ask` callers; one question at a time | `ask` (`with self._turn`) |
| `_current` | `ConsentSheet` | `dict \| None` | The live question: `answer`, `done`, `armed`, `timer`, `checks`, `wider` | `_show` (sets `_current`, `checks`, `wider`), `_answer`, `_destroyed` |
| `_closed` | `ConsentSheet` | `bool` | The sheet's toplevel was destroyed | `_destroyed` |
| webview sensitivity | `ConsentSheet` | GTK output latch | Page input is blocked exactly while a question is shown | `_show` (`set_sensitive(False)`), `_answer` (`set_sensitive(True)`) |
| `_asking` | `src/blackrose/gui/external.py::_asking` | module-level `threading.Lock` | One external-hand-off question at a time | `src/blackrose/gui/external.py::hand_off` (acquire, and release in its `finally`) |
| `_ready` | `Emitter` | `threading.Event` | The first `Api.ready`; gates delivery | `page_ready` |
| `_closed` | `Emitter` | `threading.Event` | No more events may be queued/delivered | `close`, `_run` (on the `None` sentinel) |
| `_queue` | `Emitter` | `queue.Queue` | Ordered `(kind, payload)` events plus the `None` sentinel | `__call__`, `close` |
| `_window` | `Emitter` | window or `None` | Delivery target | `attach` |
| `latest_update`, `reasoning_at` | `src/blackrose/gui/bridge.py::coalesce` | local dicts | Coalescing indices inside one batch | `coalesce` only |
| `Api._booted` | `Api` | `bool` | First `ready` boots, later ones redraw | `src/blackrose/gui/bridge.py::Api.ready` |
| `Api._renderer_recovery` | `Api` | weakref or `None` | The recovery object to signal from `ready` | `src/blackrose/gui/bridge.py::Api._set_renderer_recovery`, read in `src/blackrose/gui/bridge.py::Api.ready` |
| desktop lock | `src/blackrose/gui/app.py::run_gui` | `DesktopOwnership` | One desktop owns the database and router | `src/blackrose/instance.py::desktop_instance` |
| signal dispositions | `src/blackrose/gui/app.py::_route_exit_signals` | process state per signal | First signal closes, then `SIG_DFL` so a second kills | `_route_exit_signals` |
| `_SHUTDOWN_SECONDS` | `src/blackrose/gui/app.py::_SHUTDOWN_SECONDS` | `float` | Bound on session/runtime close | module constant |

`RendererRecovery` has no persisted state. The only durable state touched by this
machine is `Session`'s reply/draft state, which it settles but never rewrites.

## States

### RendererRecovery

The six names are the transition phase, chosen in this priority order so they are
mutually exclusive. `_dialog is not None` is an orthogonal output latch: a dialog
can coexist with `SETTLING` (Stop was pressed) and with `WAITING_PERMISSION`
(Stop was pressed, then settlement reported a permission).

| State | Predicate |
| --- | --- |
| `CLOSED` | `_closed` |
| `SETTLING` | `not _closed and _settling` |
| `WAITING_PERMISSION` | `not _closed and not _settling and _waiting_permission` |
| `RELOADING` | `not _closed and not _settling and not _waiting_permission and _reloading` |
| `FALLBACK` | `not _closed and not _settling and not _waiting_permission and not _reloading and _dialog is not None` |
| `NORMAL` | none of the above |

Initial state is `NORMAL` (the field defaults in
`src/blackrose/gui/renderer_recovery.py::RendererRecovery.__init__`).

### ConsentSheet

| State | Predicate |
| --- | --- |
| `CLOSED` | `_closed` |
| `HIDDEN` | `not _closed and _current is None` |
| `DISARMED` | `_current is not None and not _current['armed']` |
| `ARMED` | `_current is not None and _current['armed']` |

`CLOSING` is a projection, not a state: `_current is None` while the revealer
animates out; `_settled` hides the overlays only when the child is no longer
revealed (`src/blackrose/gui/consent.py::ConsentSheet._settled`).

### External hand-off

All line-level behaviour is in `src/blackrose/gui/external.py::hand_off`.

| State | Predicate |
| --- | --- |
| `IDLE` | `not _asking.locked()` |
| `ASKING` | `_asking` held; the sheet/dialog has been invoked (the non-blocking acquire, then `confirmation` and `ask` inside the `try`) |
| `REFUSED` | a second request while `ASKING`; it raises before any question (the failed non-blocking acquire) |
| `LAUNCHING` | accept returned exactly `True`, mutex released, `launch` running (after the `finally`) |
| `DONE`/`FAILED` | `launch` returned true / raised `ValueError` (`src/blackrose/gui/external.py::launch`) or the user declined (the `not accepted` branch) |

The sheet's own `HIDDEN/DISARMED/ARMED` are a nested machine reachable from
`ASKING`.

### Bridge

The emitter states follow `src/blackrose/gui/bridge.py::Emitter._run`.

| State | Predicate |
| --- | --- |
| `PRE_READY` | thread started, `_ready` unset; `_run` blocks at `_ready.wait()` |
| `ACTIVE` | `_ready` set, `_closed` unset, waiting on `_queue.get()` |
| `BATCHING` | one item taken, 1/60 s window open (the `deadline.wait` and `get_nowait` drain) |
| `DELIVERING` | `coalesce` then one `evaluate_js` call (`src/blackrose/gui/bridge.py::Emitter._deliver`) |
| `CLOSED` | `_closed` set; `__call__` drops (`src/blackrose/gui/bridge.py::Emitter.__call__`); `None` ends `_run` (the sentinel checks in `_run`) |
| `UNBOOTED`/`BOOTED` | `Api._booted` false/true (`src/blackrose/gui/bridge.py::Api.__init__`, `src/blackrose/gui/bridge.py::Api.ready`) |

### Window process (`app.py`)

`LOCKED → STARTING → RUNNING → CLOSING → EXITED`; `_run_gui` returns normally or
`_exit_past_stuck_workers` calls `os._exit` (`src/blackrose/gui/app.py::_exit_past_stuck_workers`).
Each of `SIGTERM`, `SIGHUP`, `SIGINT` is `ARMED` until its first delivery, then the
handler sets `SIG_DFL` and destroys the window
(`src/blackrose/gui/app.py::_route_exit_signals`); a second delivery is fatal.

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Renderer process terminated | WebKit `web-process-terminated` (connected in `src/blackrose/gui/renderer_recovery.py::RendererRecovery.attach`) → `src/blackrose/gui/renderer_recovery.py::RendererRecovery._web_process_terminated` |
| E2 | Settlement finished | `src/blackrose/gui/renderer_recovery.py::RendererRecovery._wait_for_settlement` worker → idle `_settlement_finished` |
| E3 | Settlement worker could not start | `Thread.start` raises (the second `try` in `src/blackrose/gui/renderer_recovery.py::RendererRecovery._begin_settlement`) |
| E4 | Settlement future could not be created | `api._renderer_termination_future` raises (the first `try` in `src/blackrose/gui/renderer_recovery.py::RendererRecovery._begin_settlement`) |
| E5 | Window closed/destroyed | pywebview `closed` → `src/blackrose/gui/renderer_recovery.py::RendererRecovery.window_closed`; GTK `destroy` → `src/blackrose/gui/renderer_recovery.py::RendererRecovery._native_window_destroyed` |
| E6 | Permission poll fired | GLib 500 ms timer (armed in `_settlement_finished`) → `src/blackrose/gui/renderer_recovery.py::RendererRecovery._resume_after_permission` |
| E7 | Page handshake | `Api.ready` → `recovery.page_ready` (`src/blackrose/gui/bridge.py::Api.ready`) → `src/blackrose/gui/renderer_recovery.py::RendererRecovery.page_ready` → `_page_ready` |
| E8 | Handshake deadline | GLib 15 s timer (armed in `_issue_reload`) → `src/blackrose/gui/renderer_recovery.py::RendererRecovery._ready_timeout` |
| E9 | Reload chosen | dialog response `RELOAD` (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._dialog_response`) |
| E10 | Stop chosen | dialog response `STOP` (the first branch of `_dialog_response`) |
| E11 | Close chosen | any other dialog response (the final `else` of `_dialog_response`) |
| E12 | Another fallback detail | any `_show_fallback` while a dialog exists (its `self._dialog is not None` branch) |
| C1 | `ask` called | `src/blackrose/gui/external.py::ask` (its sheet branch) or any host caller; waits on `_turn` (`src/blackrose/gui/consent.py::ConsentSheet.ask`) |
| C2 | `show` ran / failed | idle callback or direct main-thread call (the nested `show` in `src/blackrose/gui/consent.py::ConsentSheet.ask` and its scheduling branches) |
| C3 | 400 ms arm timer fired | GLib timer → `ready` (nested in `src/blackrose/gui/consent.py::ConsentSheet._arm`) |
| C4 | Toplevel regained focus | `focus-in-event` → `src/blackrose/gui/consent.py::ConsentSheet._refocus` |
| C5 | Key pressed | `key-press-event` → `src/blackrose/gui/consent.py::ConsentSheet._key` |
| C6 | Button clicked | `_decline`/`_accept`/`_wider` `clicked` handlers (connected in `src/blackrose/gui/consent.py::ConsentSheet.__init__`) |
| C7 | Closing animation settled | `notify::child-revealed` → `src/blackrose/gui/consent.py::ConsentSheet._settled` |
| C8 | Toplevel destroyed | `destroy` → `src/blackrose/gui/consent.py::ConsentSheet._destroyed` |
| O1 | `hand_off` requested | `Api.open_link`/`open_artifact` → `src/blackrose/gui/external.py::hand_off` |
| O2 | Native answer | sheet or fallback dialog (the `ask` call in `src/blackrose/gui/external.py::hand_off`) |
| B1 | `Emitter.__call__` | `src/blackrose/gui/session.py::Session.emit` |
| B2 | `Api.ready` | page handshake (`src/blackrose/gui/bridge.py::Api.ready`) |
| B3 | Emitter `close` | `src/blackrose/gui/bridge.py::Api._shutdown` |
| A1 | Termination signal | `GLib.unix_signal_add` (`src/blackrose/gui/app.py::_route_exit_signals`) |
| A2 | `webview.start` returned | window closed (`src/blackrose/gui/app.py::_run_gui`) |

## Transition table

### RendererRecovery (`renderer_recovery.py`)

Owned by `src/blackrose/gui/renderer_recovery.py::RendererRecovery`; guards are named
by the method and branch that implement them.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `NORMAL` | E1 | `not _automatic_used` (`_handle_termination`'s first-crash branch) | `_automatic_used=True`; begin settlement `reload=True` (`_begin_settlement`) | `SETTLING` (`pending_reload=True`) |
| `NORMAL` | E1 | `_automatic_used` (`_handle_termination`'s `else`) | begin settlement `reload=False` | `SETTLING` (`pending_reload=False`) |
| `SETTLING` | E1 | always (`_handle_termination`'s `_settling` branch) | `_failure_during_settlement=True`; no new worker | `SETTLING` |
| `SETTLING` | E2 | `result == "permission_pending"` (`_settlement_finished`'s first branch) | `_settling=False`; `_waiting_permission=True`; 500 ms poll if none | `WAITING_PERMISSION` |
| `SETTLING` | E2 | `settled`/`idle`, no failure, `_pending_reload`, `_reload_count < 3`, window alive (`_settlement_finished` → `_issue_reload`) | dismiss dialog; count++; `_reloading=True`; `load_url(real_url)`; 15 s timer | `RELOADING` |
| `SETTLING` | E2 | `settled`/`idle`, `_pending_reload`, `_reload_count >= 3` (`_issue_reload`'s budget check) | fallback "could not be restored" | `FALLBACK` |
| `SETTLING` | E2 | `settled`/`idle`, no `_pending_reload` (`_settlement_finished`'s `else`) | fallback "reply stopped/ none active, page unavailable" | `FALLBACK` |
| `SETTLING` | E2 | `busy`, other result, or `_failure_during_settlement` (`_settlement_finished`'s failure branch) | fallback "still settling" / "stopped repeatedly" | `FALLBACK` |
| `SETTLING` | E3 | worker construction/`start` raises (`_begin_settlement`'s second `try`) | `_settling=False`; fallback "could not be settled safely" | `FALLBACK` |
| `SETTLING` | E4 | future creation raises (`_begin_settlement`'s first `try`) | `_settling=False`; fallback "could not be settled safely" | `FALLBACK` |
| `NORMAL`/`FALLBACK` | E9/E10/E1 | `api()` is `None` (`_begin_settlement`'s session check) | fallback "session has closed"; `_settling` is never set | `FALLBACK` |
| `WAITING_PERMISSION` | E1 | always (`_handle_termination`'s `_waiting_permission` branch) | `_failure_during_settlement=True`; the existing decision stays the only one | `WAITING_PERMISSION` |
| `WAITING_PERMISSION` | E6 | not closed (`_resume_after_permission`'s `_closed` check) | clear timer id/flag; begin settlement with `_pending_reload`, preserving the failure flag (`preserve_failure=True`) | `SETTLING` |
| `RELOADING` | E7 | `_reloading` (`_page_ready`) | `_reloading=False`; remove ready timer; log handshake | `NORMAL` |
| `RELOADING` | E8 | `_reloading` and not closed (`_ready_timeout`) | `_reloading=False`; fallback "did not become ready" | `FALLBACK` |
| `RELOADING` | E1 | always (`_handle_termination`'s `_awaiting_ready()` branch) | clear ready timer; fallback "stopped during recovery"; fix also clears `_reloading` (`_show_fallback`) | `FALLBACK` |
| `FALLBACK` | E9 | Reload button exists only when `_reload_count < 3` at creation (`_show_fallback`'s button choice) | destroy dialog; begin settlement `reload=True` (`_dialog_response`) | `SETTLING` |
| `FALLBACK` | E10 | always (`_dialog_response`'s `STOP` branch) | keep dialog; begin settlement `reload=False` | `SETTLING` |
| `FALLBACK` | E11 | always (`_dialog_response`'s final `else`) | cleanup; `window.destroy()` | `CLOSED` |
| `FALLBACK` | E1 | not settling/waiting/reloading (`_handle_termination`'s automatic branches) | begin settlement `reload=False` (automatic already used) | `SETTLING`, dialog retained |
| `FALLBACK` | E12 | dialog exists (`_show_fallback`'s reuse branch) | replace secondary text; no new dialog/buttons | `FALLBACK` |
| any | E5 | — | `_closed=True`; remove timers; dismiss dialog; one-shot latch (`_cleanup_closed_window`) | `CLOSED` |
| `CLOSED` | any | `_closed` (checked in `_handle_termination`, `_begin_settlement`, `_settlement_finished`, `_resume_after_permission`, `_issue_reload`, `_ready_timeout`, `_show_fallback`, `_close_window`) | ignored | `CLOSED` |

`_issue_reload` also has a self-transition to `FALLBACK` if `window.load_url`
raises (its `except BaseException`), and `page_ready` is harmless in every
non-`RELOADING` state because `_page_ready` checks `_reloading`.

### ConsentSheet (`consent.py`)

Owned by `src/blackrose/gui/consent.py::ConsentSheet`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `HIDDEN` | C1 | not closed; `_turn` acquired (`ask`) | create state; schedule/run `show`; webview insensitive; revealers shown; decline focused; `_arm` (`_show`) | `DISARMED` |
| `HIDDEN` | C1 | closed (`ask`'s `_closed` check) | return `False` | `CLOSED` |
| `DISARMED` | C3 | `_current is state` (the nested `ready` in `_arm`) | `armed=True`; accepts sensitive; timer id cleared | `ARMED` |
| `ARMED`/`DISARMED` | C4 | `_current is not None` (`_refocus`) | cancel any pending timer; disarm; restart 400 ms (`_arm`) | `DISARMED` |
| `ARMED` | C5/C6 accept | `armed`; `A`/`Y`/click (`_key`'s `_ACCEPT_KEYS` branch, the accept button); `S` only with `wider` (`_key`'s `_WIDER_KEYS` branch) | clear current/timer; answer `True`/`WIDER`/(choice tuple); reveal out; webview sensitive; `done` (`_answer`) | `HIDDEN`/`CLOSING` |
| `ARMED`/`DISARMED` | C5/C6 decline | `Escape`/`D`/`N`, a focused decline via `Return`/`KP_Enter`/`space`, or the decline button (`_key`'s `_DECLINE_KEYS` and Return branches; the decline `clicked` handler) | answer `False`; same restore as accept (`_answer`) | `HIDDEN`/`CLOSING` |
| `DISARMED` | C5/C6 accept | not `armed` (`_answer`'s armed check) | ignored; keys swallowed | `DISARMED` |
| `HIDDEN` | C7 | child no longer revealed and `_current is None` (`_settled`) | hide revealer and scrim | `HIDDEN` |
| any | C8 | — | `_closed=True`; current state's `done` set (answer stays `False`) (`_destroyed`) | `CLOSED` |
| `CLOSED` | C1 | closed (`ask`'s `_closed` check) | return `False`; only `done` is required | `CLOSED` |

### External hand-off (`external.py`)

Owned by `src/blackrose/gui/external.py::hand_off`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | O1 | window is not `None` (`hand_off`'s first check) | resolve `handler_for`; validate has a handler | `IDLE` |
| `IDLE` | O1 | `_asking.acquire(blocking=False)` succeeds | build `confirmation`; call `ask` | `ASKING` |
| `ASKING` | O1 | second caller (the failed acquire) | raise `ValueError`; never blocks or queues | `REFUSED` |
| `ASKING` | O2 | `ask` returned exactly `True` (`is True`) | release lock in `finally`; `launch` | `LAUNCHING` |
| `ASKING` | O2 | anything else (decline, `WIDER`, exception) | release lock; log decline or propagate | `DONE` |
| `LAUNCHING` | launch result | `launch_uris` false/raises (`src/blackrose/gui/external.py::launch`) | raise `ValueError` | `FAILED` |
| `LAUNCHING` | launch result | true | log kind/site/app only | `DONE` |

### Emitter and `Api.ready` (`bridge.py`)

Owned by `src/blackrose/gui/bridge.py::Emitter` and
`src/blackrose/gui/bridge.py::Api.ready`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `PRE_READY` | B1 | not closed (`Emitter.__call__`) | enqueue `(kind, payload)` | `PRE_READY` |
| `PRE_READY` | B2 | first ready (`Api.ready` calls `page_ready`) | `_ready.set()` | `ACTIVE` |
| `ACTIVE` | B1 | not closed | enqueue | `ACTIVE` |
| `ACTIVE` | dequeue | not closed (`_run`'s loop condition) | take first item; open 1/60 s window | `BATCHING` |
| `BATCHING` | queue drain | every non-`None` item (`_run`'s `get_nowait` loop) | append | `BATCHING` |
| `BATCHING` | `None` sentinel | — | `_closed.set()`; stop draining | `DELIVERING` |
| `BATCHING` | window elapsed | queue empty (`queue.Empty`) | — | `DELIVERING` |
| `DELIVERING` | — | window attached (`Emitter._deliver`) | `coalesce`, one `evaluate_js`; exceptions logged and the batch dropped | `ACTIVE`/`CLOSED` |
| `CLOSED` | B1 | closed (`Emitter.__call__`) | dropped | `CLOSED` |
| `CLOSED` | B3 again | — | another `None`; `_run` already returned (`Emitter.close`, `_run`) | `CLOSED` |
| `UNBOOTED` | B2 | `not _booted` (`Api.ready`) | `_booted=True`; submit `Session.boot()` | `BOOTED` |
| `BOOTED` | B2 | `_booted` (`Api.ready`'s `else`) | submit `redraw()` | `BOOTED` |
| any | B2 | recovery weakref resolves (`Api.ready`) | `recovery.page_ready()` | unchanged |

### Window process (`app.py`)

Owned by `src/blackrose/gui/app.py::run_gui`, `src/blackrose/gui/app.py::_run_gui`,
`src/blackrose/gui/app.py::_route_exit_signals` and
`src/blackrose/gui/app.py::_exit_past_stuck_workers`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `LOCKED` | `run_gui` | `desktop_instance(lock)` acquired (`run_gui`) | enter `_run_gui` | `STARTING` |
| `STARTING` | window created | `webview.create_window` returned non-`None` (`_run_gui`) | attach API/recovery/consent/emitter; route signals | `RUNNING` |
| `RUNNING` | A1 | first signal for that number (the nested `close` in `_route_exit_signals`) | log; set `SIG_DFL`; `window.destroy()` | `CLOSING` |
| `RUNNING` | A1 | second signal | process default action (handler no longer installed) | `EXITED` |
| `RUNNING` | A2 | `webview.start` returned | enter `finally` | `CLOSING` |
| `CLOSING` | — | — | `api._shutdown(180 s)`; `runtime.close(180 s)`; `loop.stop()` (`_run_gui`'s `finally`) | `EXITED` |
| `EXITED` | stuck workers | any non-daemon live thread (`_exit_past_stuck_workers`) | flush logs/streams; `os._exit(0)` | process gone |

## Invariants

### RendererRecovery

1. **A reload happens only after a clean settlement.** `_issue_reload` is reached
   from `_settlement_finished` only when `result in ('settled','idle')` and
   `_failure_during_settlement` is false; the permission branch returns first
   (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._settlement_finished`).
   Pinned by
   `test_the_first_termination_settles_then_reloads_once_and_a_handshake_ends_it`
   and `test_a_crash_while_waiting_for_permission_ends_in_the_fallback`.
2. **At most one automatic reload, and at most `MAX_RELOADS = 3` host loads.**
   `_automatic_used` is set only in the first-crash branch
   (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._handle_termination`); the
   count increases only in `_issue_reload` under its `MAX_RELOADS` guard
   (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._issue_reload`). Pinned by
   `test_the_reload_budget_is_three_loads_and_the_bounded_fallback_offers_no_reload`.
3. **The bounded fallback cannot offer a fourth reload.** Buttons are chosen at
   creation (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._show_fallback`),
   and every issued reload dismisses the dialog before incrementing
   (`_dismiss_dialog` precedes `_reload_count += 1` in `_issue_reload`), so a
   surviving dialog was created at a count below 3 and that count cannot change until
   the dialog is destroyed. Pinned by the same test.
4. **There is exactly one fallback dialog.** `_show_fallback` reuses
   `self._dialog` and only assigns a new dialog when it is `None`
   (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._show_fallback`). Pinned
   by `test_stop_settles_without_reloading_and_keeps_the_fallback`
   (dialog identity) and `test_a_crash_during_reload_ends_the_handshake_epoch`.
5. **A crash during settlement or permission is not silently retried.** It sets
   `_failure_during_settlement` (`_handle_termination`'s settling and
   waiting-permission branches), and the eventual completion falls back instead of
   reloading (`_settlement_finished`'s failure branch), including after the permission
   poll (`preserve_failure=True` in
   `src/blackrose/gui/renderer_recovery.py::RendererRecovery._resume_after_permission`).
   Pinned by
   `test_a_termination_while_settling_ends_in_the_fallback_without_a_reload` and
   `test_a_crash_while_waiting_for_permission_ends_in_the_fallback`.
6. **A settlement result is applied at most once, on the GTK thread.**
   `_wait_for_settlement` queues exactly one callback
   (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._wait_for_settlement`), and
   every state write after it is guarded by `_closed`
   (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._settlement_finished`).
   Pinned by `test_closing_during_settlement_discards_the_result_and_later_signals`.
7. **`_settling` always has a completion path.** The future/worker creation is
   now inside the fallback guard (both `try` blocks of
   `src/blackrose/gui/renderer_recovery.py::RendererRecovery._begin_settlement`).
   Pinned by
   `test_a_failed_settlement_worker_shows_the_fallback_instead_of_stranding_recovery`.
8. **A fallback means no reload handshake is outstanding.** `_show_fallback`
   clears `_reloading` and the 15 s timer (its first statements after the `_closed`
   check), so a later termination is a new event rather than a stale `RELOADING`
   self-transition. Pinned by `test_a_crash_during_reload_ends_the_handshake_epoch`.
9. **The ready timer is removed on handshake, timeout, fallback and close.**
   `_clear_ready_timeout` in `_page_ready`, the id reset in `_ready_timeout`,
   `_clear_ready_timeout` in `_show_fallback` and in `_cleanup_closed_window` (all on
   `src/blackrose/gui/renderer_recovery.py::RendererRecovery`). Pinned by the first
   termination test (timer id in `removed`) and the crash-during-reload test.
10. **`CLOSED` is absorbing and cleanup is idempotent.** Every transition checks
    `_closed`; `_cleanup_done` prevents a second timer/dialog pass
    (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._cleanup_closed_window`).
    Pinned by
    `test_closing_during_settlement_discards_the_result_and_later_signals`.
11. **The reload count is spent even when the load fails.** `_reload_count += 1`
    precedes `load_url`; an exception falls back
    (`src/blackrose/gui/renderer_recovery.py::RendererRecovery._issue_reload`). This is
    deliberate: a failed host load must not become an unbounded retry loop. (Reasoned
    from the code; no test because the fake window's `load_url` never fails.)

### ConsentSheet

12. **Acceptance requires an armed question.** Every accept path (the accept and
    wider keys in `src/blackrose/gui/consent.py::ConsentSheet._key`, the two buttons'
    `clicked` handlers in `src/blackrose/gui/consent.py::ConsentSheet.__init__`)
    funnels through `_answer`, which rejects truthy answers while `armed` is false
    (`src/blackrose/gui/consent.py::ConsentSheet._answer`). Pinned by
    `test_no_accept_path_works_before_the_arm_timer_fires`.
13. **Arming is per-show and per-focus.** `_arm` disarms, disables both accepts,
    cancels the previous timer and schedules one new timer whose closure checks
    the state identity (`src/blackrose/gui/consent.py::ConsentSheet._arm`). Pinned by
    `test_refocus_restarts_exactly_one_arm_delay_and_disarms_again`.
14. **Enter and Space never accept.** They decline only when the decline has
    focus; otherwise they are swallowed (the `Return`/`KP_Enter`/`space` branch of
    `src/blackrose/gui/consent.py::ConsentSheet._key`). Pinned by
    `test_enter_and_space_never_accept`.
15. **Declining is always available and never strands the page.** A decline is
    accepted even while disarmed, and every answered path re-enables the webview
    (`src/blackrose/gui/consent.py::ConsentSheet._answer`). Pinned by
    `test_declining_before_arming_always_restores_the_page` and
    `test_every_answer_restores_webview_sensitivity`.
16. **One question at a time.** `_turn` is held for the whole call
    (`with self._turn` in `src/blackrose/gui/consent.py::ConsentSheet.ask`), and
    `_current` is a single slot; a second caller cannot show until the first
    answer releases the lock. (Structural; demonstrated indirectly by the
    blocking behaviour of `ask`.)
17. **Window destruction releases every waiting caller.** `_destroyed` completes
    the *current* state (`src/blackrose/gui/consent.py::ConsentSheet._destroyed`) and a
    worker waiting for a not-yet-current `show` now notices `_closed` within
    `_CLOSE_POLL_SECONDS` (the `done.wait(_CLOSE_POLL_SECONDS)` loop in
    `src/blackrose/gui/consent.py::ConsentSheet.ask`). Pinned
    by `test_a_window_closed_before_the_queued_sheet_runs_releases_the_caller`.
18. **A late closing animation cannot hide a newer question.** `_settled`
    requires `_current is None` before hiding
    (`src/blackrose/gui/consent.py::ConsentSheet._settled`).
19. **The webview is insensitive only while a question is shown.** It is disabled
    in `_show` (`set_sensitive(False)` in
    `src/blackrose/gui/consent.py::ConsentSheet._show`) and re-enabled on every answer
    (`set_sensitive(True)` in `src/blackrose/gui/consent.py::ConsentSheet._answer`);
    there is no timeout in this machine, so "timed out" can only mean a caller whose
    `show` never ran, which invariant 17 covers.

### External hand-off

20. **One hand-off at a time, refused rather than queued.** The acquire is
    non-blocking and a second request raises
    (`src/blackrose/gui/external.py::hand_off`). The pre-existing test
    `tests/test_external_actions.py::test_a_second_request_is_refused_while_one_waits_for_an_answer`
    pins this.
21. **The mutex is released on every path.** The `ask`/`confirmation` block is
    wrapped in `try/finally` (`src/blackrose/gui/external.py::hand_off`), including a
    raising question. Pinned by
    `test_a_raising_question_does_not_leave_the_handoff_mutex_held`.
22. **Launching cannot extend the lock.** `launch` runs after the `finally`
    (`src/blackrose/gui/external.py::hand_off`). Pinned by
    `test_launch_happens_only_after_the_mutex_is_released`.
23. **Only an explicit `True` launches.** `WIDER`, `None`, `1` and strings are
    declines at this layer (the `is True` comparison and the `not accepted` branch in
    `src/blackrose/gui/external.py::hand_off`); the pre-existing link tests pin the
    exact-address and named-handler behaviour.

### Emitter and `Api.ready`

24. **Delivery starts only after the page handshake.** `_run` blocks on
    `_ready.wait()` (`src/blackrose/gui/bridge.py::Emitter._run`); `Api.ready` sets it
    before boot/redraw (its first statement, `self._emitter.page_ready()`, in
    `src/blackrose/gui/bridge.py::Api.ready`). Pinned indirectly by `EmitterTests`
    (delivery needs `page_ready`).
25. **One batch per 1/60 s window, in queue order.**
    (`src/blackrose/gui/bridge.py::Emitter._run`;
    `src/blackrose/gui/bridge.py::_BATCH_SECONDS`). The batch order is the emission
    order from the loop thread.
26. **Within a batch, `reply_end` is never dropped or crossed.** `coalesce` has
    no terminal branch; it appends and clears that key's coalescing entries (the
    `reply_end` branch of `src/blackrose/gui/bridge.py::coalesce`), so a later update
    is never merged into a pre-terminal slot. Pinned by
    `test_a_terminal_is_never_dropped_and_later_updates_do_not_cross_it`
    and `test_a_terminal_queued_before_close_reaches_the_page_exactly_once`.
27. **Snapshot is a coalescing boundary.** Both index maps are cleared before the
    snapshot is appended (the `snapshot` branch of
    `src/blackrose/gui/bridge.py::coalesce`). Pinned by
    `test_a_snapshot_separates_coalescing_epochs`.
28. **Reasoning joins in arrival order within an epoch.** Same-key deltas append
    text to the first occurrence (the `reasoning` branch of
    `src/blackrose/gui/bridge.py::coalesce`). Pinned by
    `test_reasoning_deltas_join_in_order_within_one_epoch`.
29. **`Api.ready` boots once and redraws afterwards.** `_booted` is the latch
    (`src/blackrose/gui/bridge.py::Api.__init__`, `src/blackrose/gui/bridge.py::Api.ready`).
    Pinned by `test_ready_boots_once_then_redraws_and_signals_both_recovery_hooks`.
30. **Every ready signals the emitter and the recovery object.** The
    `page_ready()` call and the recovery-weakref branch of
    `src/blackrose/gui/bridge.py::Api.ready`. Pinned by the same test.
31. **After close, new events are dropped.** `__call__` checks `_closed`
    (`src/blackrose/gui/bridge.py::Emitter.__call__`); the `None` sentinel ends `_run`
    after the current batch (`src/blackrose/gui/bridge.py::Emitter._run`).
    A call racing `close` between the check and the put can enqueue one event
    that is never delivered; that is acceptable because shutdown is discarding
    the page, and no durable state depends on it.

### Window process

32. **One desktop per database, before storage and the router are adopted.**
    `desktop_instance` acquires `flock` before `_run_gui`
    (`src/blackrose/gui/app.py::run_gui`); the lock is held through shutdown and
    released by the context manager finally
    (`src/blackrose/instance.py::desktop_instance`). Pinned by `test_instance.py`.
33. **Shutdown is attempted after every exit path.** `_run_gui`'s `finally`
    closes the session and runtime, then stops the loop, even when a step raised
    (`src/blackrose/gui/app.py::_run_gui`).
34. **First signal closes, second kills.** `close` installs `SIG_DFL` before
    `window.destroy()` (`src/blackrose/gui/app.py::_route_exit_signals`). Pinned by
    `test_the_first_signal_restores_the_default_before_destroying_the_window`.
35. **`os._exit` is a last resort after shutdown.** It only runs after the
    `finally` block and only when non-daemon workers are still alive
    (`src/blackrose/gui/app.py::_exit_past_stuck_workers`).

## Hazards and open questions

- **H1 — `defect` (fixed) — `src/blackrose/gui/renderer_recovery.py::RendererRecovery._begin_settlement`.**
  The settlement worker was created and started outside the `try` that guards future
  creation. If `Thread.start` raised (an exhausted thread pool, `RuntimeError: can't
  start new thread`), the exception escaped the GLib idle callback, `_settling` stayed
  `True`, no worker existed to call `_settlement_finished`, and no dialog was
  shown. Every later termination then hit `if self._settling` (in
  `src/blackrose/gui/renderer_recovery.py::RendererRecovery._handle_termination`) and
  only set the failure flag, so recovery was permanently wedged with a dead page and
  no native control. Demonstrated before the fix by
  `test_a_failed_settlement_worker_shows_the_fallback_instead_of_stranding_recovery`
  (the exception propagated; `_settling` was `True`). Fix: guard construction and
  `start`, reset `_settling`, log and show the fallback (the second `try` in
  `_begin_settlement`).
- **H2 — `fragile` (fixed) — the `_awaiting_ready()` branch of
  `src/blackrose/gui/renderer_recovery.py::RendererRecovery._handle_termination`,
  `src/blackrose/gui/renderer_recovery.py::RendererRecovery._show_fallback`.** A
  termination while `_reloading` called `_clear_ready_timeout` and showed the
  fallback but left `_reloading=True`. The derived state was therefore `RELOADING`
  forever even though the host had given up; `_awaiting_ready()` stayed true, so a
  later termination was folded into "stopped during recovery" instead of starting
  a new settlement, and a late handshake only cleared a flag nothing was waiting
  on. Demonstrated before the fix by
  `test_a_crash_during_reload_ends_the_handshake_epoch` (`_awaiting_ready()`
  stayed `True`, the ready timer id was not removed by the fallback, and the next
  settlement future was never consumed). Fix: `_show_fallback` clears
  `_reloading` and the ready timer.
- **H3 — `fragile` (fixed) — `src/blackrose/gui/consent.py::ConsentSheet.ask`.** A
  non-main-thread `ask` checked `_closed` while holding `_turn`, then queued `show`
  and waited on `done` with no timeout. If the toplevel was destroyed in that gap,
  `_destroyed` (`src/blackrose/gui/consent.py::ConsentSheet._destroyed`) only completed
  `_current`, which `_show` had not set yet, and `show` never ran because the GTK loop
  was gone. The caller blocked forever, kept `_turn` held (all later approvals blocked
  too), and, for a hand-off, kept `external._asking` held for the life of the process.
  Demonstrated before the fix by
  `test_a_window_closed_before_the_queued_sheet_runs_releases_the_caller`
  (`caller.is_alive()` was true). Fix: bound the wait and re-check `_closed` (the
  off-main-thread branch of `ask`). Severity is `fragile` rather than `defect`
  because destroying the main window normally ends the process, and
  `app._exit_past_stuck_workers` (`src/blackrose/gui/app.py::_exit_past_stuck_workers`)
  exists for exactly this class of blocked pywebview worker.
- **H4 — `fragile` — the permission branch of
  `src/blackrose/gui/renderer_recovery.py::RendererRecovery._settlement_finished`,
  `src/blackrose/gui/renderer_recovery.py::RendererRecovery._resume_after_permission`,
  the `while self._permission_pending` loop of
  `src/blackrose/gui/session.py::Session.settle_after_renderer_termination`.** A pending
  permission keeps the page dead and re-arms the 500 ms poll forever. Each poll calls
  `_begin_settlement`, which waits up to the 8 s settlement deadline before returning
  `permission_pending` again, so an unanswered sheet costs one daemon worker and one
  8 s loop sleep per cycle and no fallback is offered (by design: the existing native
  decision is the only one). If `Session._permission_pending` were ever left true
  after its decision surface disappears, recovery would poll indefinitely. `Session`
  clears it in the `finally` of the directory-approval branch of
  `src/blackrose/gui/session.py::Session.submit` and the outbound done-callback
  (`closed` in `src/blackrose/gui/session.py::Session._authorize_outbound`); no product
  path was found that leaves it set. Not demonstrated. Confirmation would need an
  injected session that reports `permission_pending` after the sheet is gone.
- **H5 — `documentation-gap` — `src/blackrose/gui/bridge.py::coalesce`.** The
  docstring says "keeping order", but a coalesced `reply_update`/`ui_update` replaces
  the payload at the *first* occurrence's index (the `latest_update` branch), so the
  surviving payload moves earlier past interleaved events of other keys. Example
  pinned by `test_cross_key_coalescing_moves_the_surviving_payload_earlier`:
  `[update k1 old, audit k1, update k1 new]` becomes `[update k1 new, audit k1]`.
  This is presentation-only today: the page handles each event by `p.key`, the
  update payload is a full render, and the audit handler does not depend on the
  update's position (`src/blackrose/gui/static/bloom.js::on.reply_update`,
  `src/blackrose/gui/static/bloom.js::on.audit_update`,
  `src/blackrose/gui/static/bloom.js::on.ui_update`). It cannot move a
  terminal: `reply_end` clears that key's coalescing entries and is appended
  (the `reply_end` branch of `coalesce`). No change proposed: preserving the last
  position instead of the first would change the batching contract and contradicts
  the existing pinned expectation in
  `tests/test_ui_documents.py::ValidationTests.test_cancel_invalid_and_coalescing_keep_canonical_snapshots`
  (its final `coalesce` assertions).
- **H6 — `none-found` — consent arming.** Every accept route is guarded by
  `armed` (`src/blackrose/gui/consent.py::ConsentSheet._answer`); Enter/Space accept
  nothing (`src/blackrose/gui/consent.py::ConsentSheet._key`); refocus only makes the
  delay stricter and cancels the old timer
  (`src/blackrose/gui/consent.py::ConsentSheet._arm`,
  `src/blackrose/gui/consent.py::ConsentSheet._refocus`); the armed callback checks
  state identity (the nested `ready` in `_arm`); and every answer restores the webview
  (`_answer`). Pinned by the consent tests above. The one non-arming path is
  programmatic: a caller could call `_answer(True)` directly after `armed` was set by
  the timer, which is the intended accept path, not a bypass.
- **H7 — `fragile` — `src/blackrose/gui/consent.py::ConsentSheet._show`.** If an
  exception occurs after `_webview.set_sensitive(False)` and before `_answer` restores
  it (`set_sensitive(True)` in `src/blackrose/gui/consent.py::ConsentSheet._answer`),
  `ask`'s handler (the nested `show` in
  `src/blackrose/gui/consent.py::ConsentSheet.ask`) returns `False` but leaves the
  webview insensitive and `_current` set. No plausible product trigger was found:
  every call between `set_sensitive(False)` and the final `_arm()` in `_show` is a
  plain GTK setter on widgets already realized in `__init__`, and `_fit_well` (which
  can raise on a Pango problem) runs before `set_sensitive(False)`. Not demonstrated;
  confirmation means injecting a raising `_arm` in `ConsentSheet._show` and observing
  the webview. Not fixed because the fix would be defensive only, and the smallest
  change should follow a real trigger.
- **H8 — `none-found` — external mutex.** `_asking.acquire(blocking=False)`
  cannot block; the `finally` releases it on success, decline, `WIDER` and a raising
  question; the launcher runs after release (all in
  `src/blackrose/gui/external.py::hand_off`). The only unbounded wait is the user's
  answer inside `ask`/`dialog.run`, which is the intended gate; H3 is the one way that
  wait could outlive the window. Pinned by the two hand-off tests above plus the
  pre-existing `test_a_second_request_is_refused_while_one_waits_for_an_answer`.
- **H9 — `none-found` — emitter terminal and close.** `reply_end` has no
  coalescing branch and is appended in order; same-key state is cleared at the
  terminal (the `reply_end` branch of `src/blackrose/gui/bridge.py::coalesce`). A
  delivery exception drops the batch (`src/blackrose/gui/bridge.py::Emitter._deliver`),
  but the next `Api.ready` redraws the full session state (its `else` branch in
  `src/blackrose/gui/bridge.py::Api.ready`), which is the recovery path. The
  pre-existing lifecycle and UI suites pin the snapshot and coalescing contracts
  (`tests/test_session_lifecycle.py::SnapshotBatchTests.test_reasoning_and_updates_never_coalesce_across_snapshot`,
  `tests/test_ui_documents.py::ValidationTests.test_cancel_invalid_and_coalescing_keep_canonical_snapshots`).
- **H10 — `fragile` (accepted) — `src/blackrose/gui/renderer_recovery.py::RendererRecovery._page_ready`,
  `src/blackrose/gui/renderer_recovery.py::RendererRecovery._ready_timeout`.** A
  handshake that arrives just after the 15 s deadline leaves the fallback up although
  the page is usable; the user must choose Reload (a second load) or Close. No request
  is replayed and no data is lost, so this is the intended deadline semantics
  rather than a defect. Not demonstrated (it needs a 15 s native probe).
- **H11 — `none-found` — signals and shutdown.** The first signal for each
  number is disarmed before destroy, so a wedged unload can be escaped with a
  second signal (`src/blackrose/gui/app.py::_route_exit_signals`); shutdown runs
  after every exit path (`src/blackrose/gui/app.py::_run_gui`); the lock is released
  by the context manager or the OS (`src/blackrose/instance.py::desktop_instance`).

## Formalization decisions

Three small source changes, each with a regression test that fails on the previous
code. No state-enum refactor was made: the flags carry both the phase and parallel
outputs (dialog visible, reload budget, automatic spent), and replacing them would
be a larger change than the demonstrated defects justify. The specification above
is the formalization.

1. **`RendererRecovery._begin_settlement`** now guards worker creation
   and `start` (its second `try`). The hazard (H1) was a permanently wedged recovery
   with no UI. The change only adds the failure path; the success path is the same
   call.
2. **`RendererRecovery._show_fallback`** now ends the reload epoch by
   clearing `_reloading` and the ready timer. This is what makes `FALLBACK`
   mutually exclusive with `RELOADING` and lets a later termination start a new
   settlement (H2). It changes no success-path behaviour: every other caller of
   `_show_fallback` already had `_reloading` false.
3. **`ConsentSheet.ask`** now treats a destroyed window as an answer
   while a worker waits for a queued `show` (its off-main-thread branch). The
   alternative (registering every pending state for `_destroyed` to complete) would
   add a second lock and a new shared collection; a bounded re-check of the single
   `_closed` flag is smaller and has no ordering constraints (H3). The timer constant
   is `_CLOSE_POLL_SECONDS = 0.1` (`src/blackrose/gui/consent.py::_CLOSE_POLL_SECONDS`).

No change to `gui/session.py`, `turns.py`, `chat.py`, `repository.py`,
`bridge.py`, `external.py` or `app.py`. The candidate changes there are proposals
only:

- `bridge.coalesce` reordering (H5) is presentation-only and would collide with
  the existing pinned batching contract; leave it and document it.
- The permission poll (H4) would need a session-level answer surface or a bounded
  poll in `RendererRecovery`; no product path was found, so it is a follow-up for
  the `session.py` owner if ever observed.
- The post-disable exception window in `ConsentSheet._show` (H7) is defensive
  only; fix it only after a real trigger is found.

## Verification

From the repository root, offline, after
[checkout setup](../../CONTRIBUTING.md#set-up-a-checkout);
`scripts/check_unit_tests.py` points `XDG_*` at scratch directories. The suite for
this machine, and a repeated run to check for flakes:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_21_renderer_recovery.py -v
for i in 1 2 3; do .venv/bin/python scripts/check_unit_tests.py -p test_sm_21_renderer_recovery.py; done
```

Pre-existing focused suites for the modules (`test_session_lifecycle.py` includes the
coalesce/snapshot contract and `test_ui_documents.py` the coalesce ordering contract):

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_gui_renderer_recovery.py
.venv/bin/python scripts/check_unit_tests.py -p test_consent_sheet.py
.venv/bin/python scripts/check_unit_tests.py -p test_external_actions.py
.venv/bin/python scripts/check_unit_tests.py -p test_session_lifecycle.py
.venv/bin/python scripts/check_unit_tests.py -p test_ui_documents.py
```

Lint and whitespace:

```sh
.venv/bin/python -m ruff check src/blackrose/gui/renderer_recovery.py \
    src/blackrose/gui/consent.py tests/test_sm_21_renderer_recovery.py
git diff --check
```

The full offline gate is `.venv/bin/python scripts/check_health.py`.

Regression demonstration: revert the three source hunks listed under
[Formalization decisions](#formalization-decisions) and rerun the suite.
`test_a_window_closed_before_the_queued_sheet_runs_releases_the_caller` and
`test_a_crash_during_reload_ends_the_handshake_epoch` then fail, and
`test_a_failed_settlement_worker_shows_the_fallback_instead_of_stranding_recovery`
errors with `RuntimeError: can't start new thread`; with the changes restored the
suite passes.

The native `--gui` probes and a real WebKit termination
(`BLACKROSE_TEST_WEBKIT=1`) need a display, and some of them send real model
requests.
