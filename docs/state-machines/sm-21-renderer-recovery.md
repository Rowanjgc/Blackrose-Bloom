# SM-21: WebKit renderer recovery, the native consent sheet, external hand-off and the bridge emitter

## Scope and ownership

| Area | Files and symbols | Owner |
| --- | --- | --- |
| Renderer crash recovery | `src/blackrose/gui/renderer_recovery.py`: `RendererRecovery` (12), constants (20–27), fields (35–47), `attach` (49–66), `_web_process_terminated` (68–70), `_handle_termination` (72–96), `_begin_settlement` (101–133), `_wait_for_settlement` (135–146), `_settlement_finished` (148–172), `_resume_after_permission` (174–180), `_issue_reload` (182–200), `page_ready` (202–204), `window_closed` (206–209), `_cleanup_closed_window` (216–226), `_page_ready` (228–233), `_ready_timeout` (235–240), `_show_fallback` (247–285), `_dialog_response` (287–301), `_close_window` (308–314) | `RendererRecovery` object on the GTK/main thread. The pywebview window owns it (`app.py:93–96`); it keeps weak references to the window and `Api` and only queues GTK work. `Session` still owns cancellation and durable turn settlement (`session.py:1089–1124`), per the [architecture ownership table](../architecture.md#modules-and-invariants). |
| Native consent sheet | `src/blackrose/gui/consent.py`: `ARM_DELAY_MS`, `_CLOSE_POLL_SECONDS` (26–28), `ConsentSheet` (110), `_turn`/`_current`/`_closed` (116–118), `ask` (242–286), `_show` (289–340), `_arm` (367–383), `_refocus` (385–388), `_key` (390–414), `_answer` (416–432), `_settled` (434–437), `_destroyed` (439–443) | A GTK widget inside the window's `Gtk.Overlay` (124–149), installed by `install` (454–467) on `before_show`. It is the only native approval surface; callers on other threads wait for the GTK thread to show and answer it. |
| External hand-off | `src/blackrose/gui/external.py`: `_asking` (38), `Handoff`/`Handler`/`Confirmation` (41–58, 127–135), `link`/`saved_file`/`folder` (61–101), `handler_for` (104–124), `confirmation` (138–169), `ask` (172–237), `launch` (240–246), `hand_off` (249–271) | The desktop process, called from pywebview worker threads (`Api.open_link` 735–739, `open_artifact` 475–498). It is the only path from the window to another application (`architecture.md`). |
| Bridge emitter and handshake | `src/blackrose/gui/bridge.py`: `_BATCH_SECONDS` (38), `Emitter` (72–132), `coalesce` (135–167), `Api.ready` (206–220), `Api._redraw` (222–223), `Api._shutdown` (800–802) | The emitter thread owns delivery; the loop thread owns the session; the main thread owns `evaluate_js`. `Api.ready` is called by pywebview on a worker thread. |
| Window life cycle | `src/blackrose/gui/app.py`: `_SHUTDOWN_SECONDS` (21), `gui_problem` (24–40), `run_gui` (43–53), `_run_gui` (56–121), `_exit_past_stuck_workers` (124–140), `_route_exit_signals` (143–167) | The process/main thread; `desktop_instance` holds the one-owner lock (`instance.py:35–47`). |

The surrounding contract is that the page renders state and never owns turn or
tool policy, the renderer never replays a request, approvals stay outside page
script, and every hand-off is host-described.

## State variables

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `_termination_count` | `renderer_recovery.py:35` | `int` | Diagnostic count of renderer-termination signals | `_handle_termination` (75) |
| `_reload_count` | `:36` | `int 0..MAX_RELOADS` | Host loads issued for this window | `_issue_reload` (190) |
| `_automatic_used` | `:37` | `bool` | The one automatic reload has been spent | `_handle_termination` (90) |
| `_settling` | `:38` | `bool` | A settlement worker is outstanding | begin (108), finished (149), failures (115, 131) |
| `_waiting_permission` | `:39` | `bool` | A native decision is outstanding; a poll is armed | finished (153), resume (176) |
| `_failure_during_settlement` | `:40` | `bool` | The renderer died again before settlement completed | termination (83, 87); reset by begin (111) |
| `_pending_reload` | `:41` | `bool` | Whether the current settlement should end in a reload | begin (109) |
| `_reloading` | `:42` | `bool` | A host load was issued and its handshake is outstanding | issue (191), page_ready (230), timeout (238), fallback (253) |
| `_ready_timeout_id` | `:43` | GLib source id or `None` | The 15 s handshake deadline | issue (199), clear (243–245) |
| `_permission_timer_id` | `:44` | GLib source id or `None` | The 500 ms permission poll | finished (155), resume (175), cleanup (222–224) |
| `_dialog` | `:45` | `Gtk.MessageDialog` or `None` | The one native fallback dialog | show (284), dismiss (304), response (295) |
| `_closed` | `:46` | `bool` | The window/recovery is terminal | window closed (208), native destroyed (213), cleanup (217), close (311) |
| `_cleanup_done` | `:47` | `bool` | One-shot cleanup latch | cleanup (220) |
| `_turn` | `consent.py:116` | `threading.Lock` | Serializes `ask` callers; one question at a time | `ask` (253) |
| `_current` | `:117` | `dict \| None` | The live question: `answer`, `done`, `armed`, `timer`, `checks`, `wider` | `_show` (294, 304, 324), `_answer` (416–432), `_destroyed` (440) |
| `_closed` | `:118` | `bool` | The sheet's toplevel was destroyed | `_destroyed` (439) |
| webview sensitivity | `:333`, `:430` | GTK output latch | Page input is blocked exactly while a question is shown | `_show`, `_answer` |
| `_asking` | `external.py:38` | module-level `threading.Lock` | One external-hand-off question at a time | `hand_off` acquire (256), release (263) |
| `_ready` | `bridge.py:82` | `threading.Event` | The first `Api.ready`; gates delivery | `page_ready` (93–94) |
| `_closed` | `:83` | `threading.Event` | No more events may be queued/delivered | `close` (96–98), `_run` (119) |
| `_queue` | `:81` | `queue.Queue` | Ordered `(kind, payload)` events plus the `None` sentinel | `__call__` (100–102), `close` (98) |
| `_window` | `:84` | window or `None` | Delivery target | `attach` (87–88) |
| `latest_update`, `reasoning_at` | `:138–139` | local dicts | Coalescing indices inside one batch | `coalesce` only |
| `Api._booted` | `:186` | `bool` | First `ready` boots, later ones redraw | `Api.ready` (209–214) |
| `Api._renderer_recovery` | `:185` | weakref or `None` | The recovery object to signal from `ready` | `_set_renderer_recovery` (193–194), read at 215–218 |
| desktop lock | `app.py:50–52` | `DesktopOwnership` | One desktop owns the database and router | `desktop_instance` (`instance.py:35–47`) |
| signal dispositions | `app.py:166–167` | process state per signal | First signal closes, then `SIG_DFL` so a second kills | `_route_exit_signals` |
| `_SHUTDOWN_SECONDS` | `app.py:21` | `float` | Bound on session/runtime close | module constant |

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

Initial state is `NORMAL` (fields 35–47).

### ConsentSheet

| State | Predicate |
| --- | --- |
| `CLOSED` | `_closed` |
| `HIDDEN` | `not _closed and _current is None` |
| `DISARMED` | `_current is not None and not _current['armed']` |
| `ARMED` | `_current is not None and _current['armed']` |

`CLOSING` is a projection, not a state: `_current is None` while the revealer
animates out; `_settled` hides the overlays only when the child is no longer
revealed (434–437).

### External hand-off

| State | Predicate |
| --- | --- |
| `IDLE` | `not _asking.locked()` |
| `ASKING` | `_asking` held; the sheet/dialog has been invoked (256–261) |
| `REFUSED` | a second request while `ASKING`; it raises before any question (256–257) |
| `LAUNCHING` | accept returned exactly `True`, mutex released, `launch` running (263, 269) |
| `DONE`/`FAILED` | `launch` returned true / raised `ValueError` (240–246) or the user declined (266–268) |

The sheet's own `HIDDEN/DISARMED/ARMED` are a nested machine reachable from
`ASKING`.

### Bridge

| State | Predicate |
| --- | --- |
| `PRE_READY` | thread started, `_ready` unset; `_run` blocks at 105 |
| `ACTIVE` | `_ready` set, `_closed` unset, waiting on `_queue.get()` (106–107) |
| `BATCHING` | one item taken, 1/60 s window open (110–121) |
| `DELIVERING` | `coalesce` then one `evaluate_js` call (122, 124–132) |
| `CLOSED` | `_closed` set; `__call__` drops (100–102); `None` ends `_run` (108–109, 118–119) |
| `UNBOOTED`/`BOOTED` | `Api._booted` false/true (186, 209–214) |

### Window process (`app.py`)

`LOCKED → STARTING → RUNNING → CLOSING → EXITED`; `_run_gui` returns normally or
`_exit_past_stuck_workers` calls `os._exit` (124–140). Each of `SIGTERM`,
`SIGHUP`, `SIGINT` is `ARMED` until its first delivery, then the handler sets
`SIG_DFL` and destroys the window (157–167); a second delivery is fatal.

## Events

| Id | Event | Origin |
| --- | --- | --- |
| E1 | Renderer process terminated | WebKit `web-process-terminated` → `_web_process_terminated` (62, 68–70) |
| E2 | Settlement finished | `_wait_for_settlement` worker → idle `_settlement_finished` (135–146) |
| E3 | Settlement worker could not start | `Thread.start` raises (120–133) |
| E4 | Settlement future could not be created | `api._renderer_termination_future` raises (112–118) |
| E5 | Window closed/destroyed | pywebview `closed` → `window_closed` (206); GTK `destroy` → `_native_window_destroyed` (211) |
| E6 | Permission poll fired | GLib 500 ms timer → `_resume_after_permission` (155, 174) |
| E7 | Page handshake | `Api.ready` → `recovery.page_ready` (bridge.py:215–218) → `_page_ready` (202, 228) |
| E8 | Handshake deadline | GLib 15 s timer → `_ready_timeout` (199, 235) |
| E9 | Reload chosen | dialog response `RELOAD` (298–299) |
| E10 | Stop chosen | dialog response `STOP` (288–294) |
| E11 | Close chosen | any other dialog response (300–301) |
| E12 | Another fallback detail | any `_show_fallback` while a dialog exists (255–259) |
| C1 | `ask` called | `external.ask` (185–188) or any host caller; waits on `_turn` (253) |
| C2 | `show` ran / failed | idle callback or direct main-thread call (259–265, 267–273) |
| C3 | 400 ms arm timer fired | GLib timer → `ready` (376–383) |
| C4 | Toplevel regained focus | `focus-in-event` → `_refocus` (385–388) |
| C5 | Key pressed | `key-press-event` → `_key` (390–414) |
| C6 | Button clicked | `_decline`/`_accept`/`_wider` handlers (199–201) |
| C7 | Closing animation settled | `notify::child-revealed` → `_settled` (434) |
| C8 | Toplevel destroyed | `destroy` → `_destroyed` (439) |
| O1 | `hand_off` requested | `Api.open_link`/`open_artifact` (249–261) |
| O2 | Native answer | sheet or fallback dialog (259–261) |
| B1 | `Emitter.__call__` | `Session.emit` (session.py:138–139) |
| B2 | `Api.ready` | page handshake (bridge.py:206–220) |
| B3 | Emitter `close` | `Api._shutdown` (800–802) |
| A1 | Termination signal | `GLib.unix_signal_add` (166–167) |
| A2 | `webview.start` returned | window closed (app.py:105) |

## Transition table

### RendererRecovery (`renderer_recovery.py`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `NORMAL` | E1 | `not _automatic_used` (89–90) | `_automatic_used=True`; begin settlement `reload=True` (90–91, 101–133) | `SETTLING` (`pending_reload=True`) |
| `NORMAL` | E1 | `_automatic_used` (92–95) | begin settlement `reload=False` | `SETTLING` (`pending_reload=False`) |
| `SETTLING` | E1 | always (82–84) | `_failure_during_settlement=True`; no new worker | `SETTLING` |
| `SETTLING` | E2 | `result == "permission_pending"` (152–157) | `_settling=False`; `_waiting_permission=True`; 500 ms poll if none | `WAITING_PERMISSION` |
| `SETTLING` | E2 | `settled`/`idle`, no failure, `_pending_reload`, `_reload_count < 3`, window alive (159–166, 186–193) | dismiss dialog; count++; `_reloading=True`; `load_url(real_url)`; 15 s timer | `RELOADING` |
| `SETTLING` | E2 | `settled`/`idle`, `_pending_reload`, `_reload_count >= 3` (186–188) | fallback "could not be restored" | `FALLBACK` |
| `SETTLING` | E2 | `settled`/`idle`, no `_pending_reload` (167–171) | fallback "reply stopped/ none active, page unavailable" | `FALLBACK` |
| `SETTLING` | E2 | `busy`, other result, or `_failure_during_settlement` (159–163) | fallback "still settling" / "stopped repeatedly" | `FALLBACK` |
| `SETTLING` | E3 | worker construction/`start` raises (120–133) | `_settling=False`; fallback "could not be settled safely" | `FALLBACK` |
| `SETTLING` | E4 | future creation raises (112–118) | `_settling=False`; fallback "could not be settled safely" | `FALLBACK` |
| `NORMAL`/`FALLBACK` | E9/E10/E1 | `api()` is `None` (104–107) | fallback "session has closed"; `_settling` is never set | `FALLBACK` |
| `WAITING_PERMISSION` | E1 | always (85–88) | `_failure_during_settlement=True`; the existing decision stays the only one | `WAITING_PERMISSION` |
| `WAITING_PERMISSION` | E6 | not closed (177–178) | clear timer id/flag; begin settlement with `_pending_reload`, preserving the failure flag (179) | `SETTLING` |
| `RELOADING` | E7 | `_reloading` (229) | `_reloading=False`; remove ready timer; log handshake | `NORMAL` |
| `RELOADING` | E8 | `_reloading` and not closed (237) | `_reloading=False`; fallback "did not become ready" | `FALLBACK` |
| `RELOADING` | E1 | always (78–81) | clear ready timer; fallback "stopped during recovery"; fix also clears `_reloading` (253–254) | `FALLBACK` |
| `FALLBACK` | E9 | Reload button exists only when `_reload_count < 3` at creation (279–280) | destroy dialog; begin settlement `reload=True` (295–299) | `SETTLING` |
| `FALLBACK` | E10 | always (288–294) | keep dialog; begin settlement `reload=False` | `SETTLING` |
| `FALLBACK` | E11 | always (300–301) | cleanup; `window.destroy()` | `CLOSED` |
| `FALLBACK` | E1 | not settling/waiting/reloading (89–95) | begin settlement `reload=False` (automatic already used) | `SETTLING`, dialog retained |
| `FALLBACK` | E12 | dialog exists (255–259) | replace secondary text; no new dialog/buttons | `FALLBACK` |
| any | E5 | — | `_closed=True`; remove timers; dismiss dialog; one-shot latch (216–226) | `CLOSED` |
| `CLOSED` | any | `_closed` (73–74, 102, 150–151, 177–178, 184, 237, 248–249, 309–310) | ignored | `CLOSED` |

`_issue_reload` also has a self-transition to `FALLBACK` if `window.load_url`
raises (192–197), and `page_ready` is harmless in every non-`RELOADING` state
because `_page_ready` checks `_reloading` (229).

### ConsentSheet (`consent.py`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `HIDDEN` | C1 | not closed; `_turn` acquired (253–255) | create state; schedule/run `show`; webview insensitive; revealers shown; decline focused; `_arm` (289–340) | `DISARMED` |
| `HIDDEN` | C1 | closed (254–255) | return `False` | `CLOSED` |
| `DISARMED` | C3 | `_current is state` (377) | `armed=True`; accepts sensitive; timer id cleared (378–381) | `ARMED` |
| `ARMED`/`DISARMED` | C4 | `_current is not None` (386) | cancel any pending timer; disarm; restart 400 ms (372–383) | `DISARMED` |
| `ARMED` | C5/C6 accept | `armed`; `A`/`Y`/click (404–406, 419–421); `S` only with `wider` (407–409) | clear current/timer; answer `True`/`WIDER`/(choice tuple); reveal out; webview sensitive; `done` (416–432) | `HIDDEN`/`CLOSING` |
| `ARMED`/`DISARMED` | C5/C6 decline | `Escape`/`D`/`N`, a focused decline via `Return`/`KP_Enter`/`space`, or the decline button (393–403, 199–201) | answer `False`; same restore as accept (416–432) | `HIDDEN`/`CLOSING` |
| `DISARMED` | C5/C6 accept | not `armed` (419–420) | ignored; keys swallowed | `DISARMED` |
| `HIDDEN` | C7 | child no longer revealed and `_current is None` (434–437) | hide revealer and scrim | `HIDDEN` |
| any | C8 | — | `_closed=True`; current state's `done` set (answer stays `False`) (439–443) | `CLOSED` |
| `CLOSED` | C1 | closed (254–255) | return `False`; only `done` is required | `CLOSED` |

### External hand-off (`external.py`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | O1 | window is not `None` (251–252) | resolve `handler_for`; validate has a handler (253–255) | `IDLE` |
| `IDLE` | O1 | `_asking.acquire(blocking=False)` succeeds (256) | build `confirmation`; call `ask` (259–261) | `ASKING` |
| `ASKING` | O1 | second caller (256–257) | raise `ValueError`; never blocks or queues | `REFUSED` |
| `ASKING` | O2 | `ask` returned exactly `True` (261, 266–269) | release lock in `finally` (262–263); `launch` (269) | `LAUNCHING` |
| `ASKING` | O2 | anything else (decline, `WIDER`, exception) (261, 266–268) | release lock; log decline or propagate | `DONE` |
| `LAUNCHING` | launch result | `launch_uris` false/raises (242–246) | raise `ValueError` | `FAILED` |
| `LAUNCHING` | launch result | true | log kind/site/app only (270) | `DONE` |

### Emitter and `Api.ready` (`bridge.py`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `PRE_READY` | B1 | not closed (101) | enqueue `(kind, payload)` | `PRE_READY` |
| `PRE_READY` | B2 | first ready (208) | `_ready.set()` | `ACTIVE` |
| `ACTIVE` | B1 | not closed | enqueue | `ACTIVE` |
| `ACTIVE` | dequeue | not closed (106–107) | take first item; open 1/60 s window (110–112) | `BATCHING` |
| `BATCHING` | queue drain | every non-`None` item (113–121) | append | `BATCHING` |
| `BATCHING` | `None` sentinel | — | `_closed.set()`; stop draining (118–119) | `DELIVERING` |
| `BATCHING` | window elapsed | queue empty (115–117) | — | `DELIVERING` |
| `DELIVERING` | — | window attached (125–126) | `coalesce`, one `evaluate_js` (122, 128–130); exceptions logged and the batch dropped (131–132) | `ACTIVE`/`CLOSED` |
| `CLOSED` | B1 | closed (101) | dropped | `CLOSED` |
| `CLOSED` | B3 again | — | another `None`; `_run` already returned (96–98, 107–109) | `CLOSED` |
| `UNBOOTED` | B2 | `not _booted` (209–210) | `_booted=True`; submit `Session.boot()` | `BOOTED` |
| `BOOTED` | B2 | `_booted` (212–214) | submit `redraw()` | `BOOTED` |
| any | B2 | recovery weakref resolves (215–218) | `recovery.page_ready()` | unchanged |

### Window process (`app.py`)

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `LOCKED` | `run_gui` | `desktop_instance(lock)` acquired (50–52) | enter `_run_gui` | `STARTING` |
| `STARTING` | window created | `webview.create_window` returned non-`None` (78–89) | attach API/recovery/consent/emitter; route signals (90–104) | `RUNNING` |
| `RUNNING` | A1 | first signal for that number (157–159) | log; set `SIG_DFL`; `window.destroy()` | `CLOSING` |
| `RUNNING` | A1 | second signal | process default action (handler no longer installed) | `EXITED` |
| `RUNNING` | A2 | `webview.start` returned (105) | enter `finally` | `CLOSING` |
| `CLOSING` | — | — | `api._shutdown(180 s)`; `runtime.close(180 s)`; `loop.stop()` (107–119) | `EXITED` |
| `EXITED` | stuck workers | any non-daemon live thread (132–133) | flush logs/streams; `os._exit(0)` (136–140) | process gone |

## Invariants

### RendererRecovery

1. **A reload happens only after a clean settlement.** `_issue_reload` is reached
   from `_settlement_finished` only when `result in ('settled','idle')` and
   `_failure_during_settlement` is false (159–166); the permission branch returns
   first (152–157). Pinned by
   `test_the_first_termination_settles_then_reloads_once_and_a_handshake_ends_it`
   and `test_a_crash_while_waiting_for_permission_ends_in_the_fallback`.
2. **At most one automatic reload, and at most `MAX_RELOADS = 3` host loads.**
   `_automatic_used` is set only in the first-crash branch (89–91); the count
   increases only in `_issue_reload` (190) under the guard at 186. Pinned by
   `test_the_reload_budget_is_three_loads_and_the_bounded_fallback_offers_no_reload`.
3. **The bounded fallback cannot offer a fourth reload.** Buttons are chosen at
   creation (279–282), and every issued reload dismisses the dialog before
   incrementing (189–190), so a surviving dialog was created at a count below 3
   and that count cannot change until the dialog is destroyed. Pinned by the same
   test.
4. **There is exactly one fallback dialog.** `_show_fallback` reuses
   `self._dialog` (255–259) and only assigns a new dialog when it is `None`
   (284). Pinned by `test_stop_settles_without_reloading_and_keeps_the_fallback`
   (dialog identity) and `test_a_crash_during_reload_ends_the_handshake_epoch`.
5. **A crash during settlement or permission is not silently retried.** It sets
   `_failure_during_settlement` (83, 87), and the eventual completion falls back
   instead of reloading (159–163), including after the permission poll
   (`preserve_failure=True`, 179). Pinned by
   `test_a_termination_while_settling_ends_in_the_fallback_without_a_reload` and
   `test_a_crash_while_waiting_for_permission_ends_in_the_fallback`.
6. **A settlement result is applied at most once, on the GTK thread.**
   `_wait_for_settlement` queues exactly one callback (144–146), and every state
   write after it is guarded by `_closed` (150–151). Pinned by
   `test_closing_during_settlement_discards_the_result_and_later_signals`.
7. **`_settling` always has a completion path.** The future/worker creation is
   now inside the fallback guard (112–133). Pinned by
   `test_a_failed_settlement_worker_shows_the_fallback_instead_of_stranding_recovery`.
8. **A fallback means no reload handshake is outstanding.** `_show_fallback`
   clears `_reloading` and the 15 s timer (250–254), so a later termination is a
   new event rather than a stale `RELOADING` self-transition. Pinned by
   `test_a_crash_during_reload_ends_the_handshake_epoch`.
9. **The ready timer is removed on handshake, timeout, fallback and close.**
   231, 236–239, 253–254, 221. Pinned by the first termination test (timer id in
   `removed`) and the crash-during-reload test.
10. **`CLOSED` is absorbing and cleanup is idempotent.** Every transition checks
    `_closed`; `_cleanup_done` prevents a second timer/dialog pass (218–220).
    Pinned by
    `test_closing_during_settlement_discards_the_result_and_later_signals`.
11. **The reload count is spent even when the load fails.** `_reload_count += 1`
    precedes `load_url`; an exception falls back (190–197). This is deliberate:
    a failed host load must not become an unbounded retry loop. (Reasoned from
    the code; no test because the fake window's `load_url` never fails.)

### ConsentSheet

12. **Acceptance requires an armed question.** Every accept path (keys 404–409,
    the two buttons 199–201) funnels through `_answer`, which rejects truthy
    answers while `armed` is false (419–420). Pinned by
    `test_no_accept_path_works_before_the_arm_timer_fires`.
13. **Arming is per-show and per-focus.** `_arm` disarms, disables both accepts,
    cancels the previous timer and schedules one new timer whose closure checks
    the state identity (367–383). Pinned by
    `test_refocus_restarts_exactly_one_arm_delay_and_disarms_again`.
14. **Enter and Space never accept.** They decline only when the decline has
    focus; otherwise they are swallowed (397–403). Pinned by
    `test_enter_and_space_never_accept`.
15. **Declining is always available and never strands the page.** A decline is
    accepted even while disarmed, and every answered path re-enables the webview
    (416–432). Pinned by `test_declining_before_arming_always_restores_the_page`
    and `test_every_answer_restores_webview_sensitivity`.
16. **One question at a time.** `_turn` is held for the whole call (253), and
    `_current` is a single slot; a second caller cannot show until the first
    answer releases the lock. (Structural; demonstrated indirectly by the
    blocking behaviour of `ask`.)
17. **Window destruction releases every waiting caller.** `_destroyed` completes
    the *current* state (439–443) and a worker waiting for a not-yet-current
    `show` now notices `_closed` within `_CLOSE_POLL_SECONDS` (271–282). Pinned
    by `test_a_window_closed_before_the_queued_sheet_runs_releases_the_caller`.
18. **A late closing animation cannot hide a newer question.** `_settled`
    requires `_current is None` before hiding (434–437).
19. **The webview is insensitive only while a question is shown.** It is disabled
    in `_show` (333) and re-enabled on every answer (430); there is no timeout in
    this machine, so "timed out" can only mean a caller whose `show` never ran,
    which invariant 17 covers.

### External hand-off

20. **One hand-off at a time, refused rather than queued.** The acquire is
    non-blocking (256); a second request raises (257). The pre-existing test
    `test_external_actions.py:test_a_second_request_is_refused_while_one_waits_for_an_answer`
    pins this.
21. **The mutex is released on every path.** The `ask`/`confirmation` block is
    wrapped in `try/finally` (258–263), including a raising question. Pinned by
    `test_a_raising_question_does_not_leave_the_handoff_mutex_held`.
22. **Launching cannot extend the lock.** `launch` runs after the `finally`
    (263, 269). Pinned by
    `test_launch_happens_only_after_the_mutex_is_released`.
23. **Only an explicit `True` launches.** `WIDER`, `None`, `1` and strings are
    declines at this layer (261, 266–269); the pre-existing link tests pin the
    exact-address and named-handler behaviour.

### Emitter and `Api.ready`

24. **Delivery starts only after the page handshake.** `_run` blocks on
    `_ready.wait()` (105); `Api.ready` sets it before boot/redraw (208).
    Pinned indirectly by `EmitterTests` (delivery needs `page_ready`).
25. **One batch per 1/60 s window, in queue order.** (104–122; `_BATCH_SECONDS`
    38). The batch order is the emission order from the loop thread.
26. **Within a batch, `reply_end` is never dropped or crossed.** `coalesce` has
    no terminal branch; it appends (157–162) and clears that key's coalescing
    entries (159–161), so a later update is never merged into a pre-terminal slot.
    Pinned by `test_a_terminal_is_never_dropped_and_later_updates_do_not_cross_it`
    and `test_a_terminal_queued_before_close_reaches_the_page_exactly_once`.
27. **Snapshot is a coalescing boundary.** Both index maps are cleared before the
    snapshot is appended (141–145). Pinned by
    `test_a_snapshot_separates_coalescing_epochs`.
28. **Reasoning joins in arrival order within an epoch.** Same-key deltas append
    text to the first occurrence (151–156). Pinned by
    `test_reasoning_deltas_join_in_order_within_one_epoch`.
29. **`Api.ready` boots once and redraws afterwards.** `_booted` is the latch
    (186, 209–214). Pinned by
    `test_ready_boots_once_then_redraws_and_signals_both_recovery_hooks`.
30. **Every ready signals the emitter and the recovery object.** 208 and 215–218.
    Pinned by the same test.
31. **After close, new events are dropped.** `__call__` checks `_closed`
    (100–102); the `None` sentinel ends `_run` after the current batch (108–121).
    A call racing `close` between the check and the put can enqueue one event
    that is never delivered; that is acceptable because shutdown is discarding
    the page, and no durable state depends on it.

### Window process

32. **One desktop per database, before storage and the router are adopted.**
    `desktop_instance` acquires `flock` before `_run_gui` (43–53); the lock is
    held through shutdown and released by the context manager finally
    (`instance.py:35–47`). Pinned by `test_instance.py`.
33. **Shutdown is attempted after every exit path.** `_run_gui`'s `finally`
    closes the session and runtime, then stops the loop, even when a step raised
    (107–119).
34. **First signal closes, second kills.** `close` installs `SIG_DFL` before
    `window.destroy()` (157–167). Pinned by
    `test_the_first_signal_restores_the_default_before_destroying_the_window`.
35. **`os._exit` is a last resort after shutdown.** It only runs after the
    `finally` block and only when non-daemon workers are still alive (124–140).

## Hazards and open questions

- **H1 — `defect` (fixed) — `renderer_recovery.py:119–133`.** The settlement
  worker was created and started outside the `try` that guards future creation.
  If `Thread.start` raised (an exhausted thread pool, `RuntimeError: can't start
  new thread`), the exception escaped the GLib idle callback, `_settling` stayed
  `True`, no worker existed to call `_settlement_finished`, and no dialog was
  shown. Every later termination then hit `if self._settling` (82–84) and only
  set the failure flag, so recovery was permanently wedged with a dead page and
  no native control. Demonstrated before the fix by
  `test_a_failed_settlement_worker_shows_the_fallback_instead_of_stranding_recovery`
  (the exception propagated; `_settling` was `True`). Fix: guard construction and
  `start`, reset `_settling`, log and show the fallback (120–133).
- **H2 — `fragile` (fixed) — `renderer_recovery.py:78–81`, `247–259`.** A
  termination while `_reloading` called `_clear_ready_timeout` and showed the
  fallback but left `_reloading=True`. The derived state was therefore `RELOADING`
  forever even though the host had given up; `_awaiting_ready()` stayed true, so a
  later termination was folded into "stopped during recovery" instead of starting
  a new settlement, and a late handshake only cleared a flag nothing was waiting
  on. Demonstrated before the fix by
  `test_a_crash_during_reload_ends_the_handshake_epoch` (`_awaiting_ready()`
  stayed `True`, the ready timer id was not removed by the fallback, and the next
  settlement future was never consumed). Fix: `_show_fallback` clears
  `_reloading` and the ready timer (250–254).
- **H3 — `fragile` (fixed) — `consent.py:271–282`.** A non-main-thread `ask`
  checked `_closed` while holding `_turn`, then queued `show` and waited on
  `done` with no timeout. If the toplevel was destroyed in that gap, `_destroyed`
  (439–443) only completed `_current`, which `_show` had not set yet, and `show`
  never ran because the GTK loop was gone. The caller blocked forever, kept
  `_turn` held (all later approvals blocked too), and, for a hand-off, kept
  `external._asking` held for the life of the process. Demonstrated before the fix
  by `test_a_window_closed_before_the_queued_sheet_runs_releases_the_caller`
  (`caller.is_alive()` was true). Fix: bound the wait and re-check `_closed`
  (271–282). Severity is `fragile` rather than `defect` because destroying the
  main window normally ends the process, and `app._exit_past_stuck_workers`
  (124–140) exists for exactly this class of blocked pywebview worker.
- **H4 — `fragile` — `renderer_recovery.py:152–157`, `174–180`,
  `session.py:1096–1102`.** A pending permission keeps the page dead and re-arms
  the 500 ms poll forever. Each poll calls `_begin_settlement`, which waits up to
  the 8 s settlement deadline before returning `permission_pending` again, so an
  unanswered sheet costs one daemon worker and one 8 s loop sleep per cycle and
  no fallback is offered (by design: the existing native decision is the only
  one). If `Session._permission_pending` were ever left true after its decision
  surface disappears, recovery would poll indefinitely. `Session` clears it in
  `_send`'s `finally` (966) and the outbound done-callback (1044–1046); no product
  path was found that leaves it set. Not demonstrated. Confirmation would need an
  injected session that reports `permission_pending` after the sheet is gone.
- **H5 — `documentation-gap` — `bridge.py:135–167`.** The docstring says
  "keeping order", but a coalesced `reply_update`/`ui_update` replaces the payload
  at the *first* occurrence's index (148–150), so the surviving payload moves
  earlier past interleaved events of other keys. Example pinned by
  `test_cross_key_coalescing_moves_the_surviving_payload_earlier`:
  `[update k1 old, audit k1, update k1 new]` becomes `[update k1 new, audit k1]`.
  This is presentation-only today: the page handles each event by `p.key`, the
  update payload is a full render, and the audit handler does not depend on the
  update's position (`bloom.js:790–843`). It cannot move a
  terminal: `reply_end` clears that key's coalescing entries and is appended
  (157–162). No change proposed: preserving the last position instead of the
  first would change the batching contract and contradicts the existing pinned
  expectation in `test_ui_documents.py:196–198`
  (`test_cancel_invalid_and_coalescing_keep_canonical_snapshots`).
- **H6 — `none-found` — consent arming.** Every accept route is guarded by
  `armed` (419–420); Enter/Space accept nothing (397–403); refocus only makes the
  delay stricter and cancels the old timer (367–388); the armed callback checks
  state identity (377); and every answer restores the webview (416–432). Pinned
  by the consent tests above. The one non-arming path is programmatic: a caller
  could call `_answer(True)` directly after `armed` was set by the timer, which is
  the intended accept path, not a bypass.
- **H7 — `fragile` — `consent.py:333–340`.** If an exception occurs after
  `_webview.set_sensitive(False)` (333) and before `_answer` restores it (430),
  `ask`'s handler (259–265) returns `False` but leaves the webview insensitive and
  `_current` set. No plausible product trigger was found: every call between 333
  and 340 is a plain GTK setter on widgets already realized in `__init__`, and
  `_fit_well` (which can raise on a Pango problem) runs before 333. Not
  demonstrated; confirmation means injecting a raising `_arm` in
  `ConsentSheet._show` and observing the webview. Not fixed because the fix would
  be defensive only, and the smallest change should follow a real trigger.
- **H8 — `none-found` — external mutex.** `_asking.acquire(blocking=False)`
  cannot block (256); the `finally` releases it on success, decline, `WIDER` and
  a raising question (258–263); the launcher runs after release (269). The only
  unbounded wait is the user's answer inside `ask`/`dialog.run`, which is the
  intended gate; H3 is the one way that wait could outlive the window. Pinned by
  the two hand-off tests above plus the pre-existing
  `test_a_second_request_is_refused_while_one_waits_for_an_answer`.
- **H9 — `none-found` — emitter terminal and close.** `reply_end` has no
  coalescing branch and is appended in order (157–162); same-key state is cleared
  at the terminal (159–161). A delivery exception drops the batch (131–132), but
  the next `Api.ready` redraws the full session state (212–214), which is the
  recovery path. The pre-existing lifecycle and UI suites pin the snapshot and
  coalescing contracts (`test_session_lifecycle.py:589–598`,
  `test_ui_documents.py:186–198`).
- **H10 — `fragile` (accepted) — `renderer_recovery.py:228–240`.** A handshake
  that arrives just after the 15 s deadline leaves the fallback up although the
  page is usable; the user must choose Reload (a second load) or Close. No request
  is replayed and no data is lost, so this is the intended deadline semantics
  rather than a defect. Not demonstrated (it needs a 15 s native probe).
- **H11 — `none-found` — signals and shutdown.** The first signal for each
  number is disarmed before destroy, so a wedged unload can be escaped with a
  second signal (157–167); shutdown runs after every exit path (107–119); the
  lock is released by the context manager or the OS (`instance.py:35–47`).

## Formalization decisions

Three small source changes, each with a regression test that fails on the previous
code. No state-enum refactor was made: the flags carry both the phase and parallel
outputs (dialog visible, reload budget, automatic spent), and replacing them would
be a larger change than the demonstrated defects justify. The specification above
is the formalization.

1. **`RendererRecovery._begin_settlement` (119–133)** now guards worker creation
   and `start`. The hazard (H1) was a permanently wedged recovery with no UI.
   The change only adds the failure path; the success path is the same call.
2. **`RendererRecovery._show_fallback` (250–254)** now ends the reload epoch by
   clearing `_reloading` and the ready timer. This is what makes `FALLBACK`
   mutually exclusive with `RELOADING` and lets a later termination start a new
   settlement (H2). It changes no success-path behaviour: every other caller of
   `_show_fallback` already had `_reloading` false.
3. **`ConsentSheet.ask` (271–282)** now treats a destroyed window as an answer
   while a worker waits for a queued `show`. The alternative (registering every
   pending state for `_destroyed` to complete) would add a second lock and a new
   shared collection; a bounded re-check of the single `_closed` flag is smaller
   and has no ordering constraints (H3). The timer constant is
   `_CLOSE_POLL_SECONDS = 0.1` (28).

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

From the repository root, offline, with the runner's scratch XDG directories:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_21_renderer_recovery.py -v
# Ran 27 tests in 0.121s
# OK
```

Repeated three times with the same result (no flakes):

```sh
for i in 1 2 3; do .venv/bin/python scripts/check_unit_tests.py -p test_sm_21_renderer_recovery.py; done
# OK
# OK
# OK
```

Pre-existing focused suites for the modules:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_gui_renderer_recovery.py
# Ran 11 tests in 0.208s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_consent_sheet.py
# Ran 10 tests in 0.003s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_external_actions.py
# Ran 21 tests in 0.194s ... OK
.venv/bin/python scripts/check_unit_tests.py -p test_session_lifecycle.py
# Ran 26 tests in 0.595s ... OK   (includes the coalesce/snapshot contract)
.venv/bin/python scripts/check_unit_tests.py -p test_ui_documents.py
# Ran 37 tests in 0.657s ... OK  (includes the coalesce ordering contract)
```

Lint and whitespace:

```sh
.venv/bin/python -m ruff check src/blackrose/gui/renderer_recovery.py \
    src/blackrose/gui/consent.py tests/test_sm_21_renderer_recovery.py
# All checks passed!
git diff --check
# (no output)
```

Regression demonstration: with the three source hunks reverted, the new suite
reports:

```text
FAIL test_a_window_closed_before_the_queued_sheet_runs_releases_the_caller
    AssertionError: True is not false : a destroyed window left the approval caller blocked
FAIL test_a_crash_during_reload_ends_the_handshake_epoch
    AssertionError: True is not false          (recovery._awaiting_ready())
ERROR test_a_failed_settlement_worker_shows_the_fallback_instead_of_stranding_recovery
    RuntimeError: can't start new thread
Ran 27 tests in 2.022s
FAILED (failures=2, errors=1)
```

With the changes restored all 27 pass.

Not run: `scripts/check_health.py` (its full unit + mypy + node + npm + `uv lock`
gate). This worktree also contains other agents' in-flight changes
(`docs/README.md`, `ui-runtime/host.jsx`, untracked `sm-18`/`sm-20`/`sm-22` test
files), so a full-gate failure would not be attributable to this bead. The
module-level commands above are the relevant gate and all pass. The native
`--gui` probes and any real WebKit termination (`BLACKROSE_TEST_WEBKIT=1`) were
not run; they need a display and would send real model requests in some cases.
