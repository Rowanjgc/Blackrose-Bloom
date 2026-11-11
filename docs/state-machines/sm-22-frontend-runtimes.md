# SM-22: Browser and sandbox-iframe frontend state machines

The presentation layer in the desktop window: the two animation players, the
Mermaid renderer adapter, the Interactive UI host and the main page controller.
None of these decide anything durable; they translate bridge events and UI input
into DOM state and bridge calls. The Python-side machines they talk to are
[SM-05](sm-05-interactive-ui-stream.md), [SM-06](sm-06-ui-action-events.md),
[SM-08](sm-08-diagram-repair.md) and [SM-09](sm-09-animation-recovery.md).

## Scope and ownership

| Runtime | Source | Executes in | Owner of its state |
| --- | --- | --- | --- |
| Motion Canvas player | `src/blackrose/gui/static/motion-canvas-player.js` (1–188) | the page's main frame | the page (`players` Set, one closure per mount) |
| Motion Canvas frame adapter | `src/blackrose/gui/static/motion-canvas-frame.js` (1–166) | opaque `sandbox="allow-scripts"` iframe, no bridge | the frame (one closure) |
| Built-in canvas player | `src/blackrose/gui/static/animations.js` (1–253) | the page's main frame | the page (`active`, one closure per mount) |
| Mermaid adapter | `src/blackrose/gui/static/diagrams.js` (1–208) | the page's main frame | the page (module singleton + one closure per figure) |
| Mermaid renderer | `diagram-runtime/host.js` (1–74) → `mermaid-host.html` (`diagram-runtime/build.mjs`) | opaque `sandbox="allow-scripts"` iframe, `default-src 'none'` CSP | the frame (`busy`) |
| Interactive UI host | `ui-runtime/host.jsx` (1–122), `ui-runtime/library.jsx` (1–109) → `ui-runtime.js` (`ui-runtime/build.mjs`) | the page's main frame | the page (`mounts` Map, module `busy`) |
| Main page controller | `src/blackrose/gui/static/bloom.js` (1–3565) | the page's main frame | the page (one closure) |

`index.html` loads `animations.js`, `motion-canvas-player.js`, `ui-runtime.js`,
`diagrams.js`, `bloom.js` in that order (165–169); `diagrams.js` resolves
`#column` while it loads (`diagrams.js:14`) and `bloom.js` resolves `.dock`
(`bloom.js:44`), so the order is a load-time dependency, not a preference. The
three generated artifacts (`ui-runtime.js`, `ui-contract.json`,
`mermaid-host.html`, `motion-canvas-runtime.js`) are gitignored build outputs
(`.gitignore:31–36`) produced from the sources above; a checkout must run
`npm run build --prefix ui-runtime` and `--prefix diagram-runtime` and
`--prefix animation-runtime` before the window can render.

Ownership follows [architecture](../architecture.md#modules-and-invariants):

- **Python owns every decision.** `bloom.receive` is the only inbound path
  (`bloom.js:952–961`, exposed at 3562) and it drops any event whose
  `conversation_id` is not the current conversation. Outbound calls go through
  `window.pywebview.api` (`bloom.js:21–28`; `gui/bridge.py`).
- **The trusted page is the boundary.** Model output only reaches the page as
  escaped Markdown HTML (`bloom.js:12–14`), validated UI documents
  (`ui_documents.py`, SM-05) or scene JSON; the sandboxed frames receive
  postMessage strings and hold no bridge (`motion-canvas-player.js:17`,
  `diagrams.js:30`, `diagram-runtime/build.mjs` CSP).
- **Both sandboxed frames run with an opaque origin** and answer an explicit
  message allow-list; `motion-canvas-frame.js:143` and `diagram-runtime/host.js:21`
  check `event.source`/channel before acting.
- **The page never writes durable state.** It reports and requests: animation
  failure (`animation_failed`), diagram failure (`diagram_failed`), UI state
  (`ui_state`), UI action (`ui_action`), layout (`save_layout`).
- The async bridge means every page transition that ends in a bridge call is at
  least one microtask long (`call()` chains through `Promise.resolve`,
  `bloom.js:24–27`); tests that observe calls must flush promise jobs.

## State variables

### A. Animation players

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `players` | `motion-canvas-player.js:4` | `Set<player>` | every live Motion Canvas mount in the page | `mount` (176), the dispose observer (172) |
| `active` | `animations.js:10` | `player \| null` | the one built-in player that is playing | `start` (211), `pause` (206) |
| `playing` | `motion-canvas-player.js:58`, `animations.js:184` | bool | per-player playback intent | `start`/`pause`, frame `state` reports (138) |
| `ready`, `failed` | `motion-canvas-player.js:58` | bool | boot completed / mount is terminal | `receive` (119, 131), `showError` (99), `retry` (150) |
| `time`, `duration` | `:58` | number | playback position (s) and the frame-reported duration | `receive` (136–137) and `seek` |
| `timeout` | `:88` | timer id | the 20 s boot/retry clock | observer (93–95), `receive` error branch (128), `retry` (157–158) |
| `options.recovery`, `figure.dataset.recovery` | `:62, 75` | object / string | the label and the dedup marker for the repair chain | `setRecovery` only |
| `figure.dataset.ready`, `.error`, `.recovery` | `:75, 103–104, 131` | string | DOM-visible lifecycle | `receive`, `showError`, `retry` |
| `ready`, `failed`, `inspection` | `motion-canvas-frame.js:4` | bool / object | frame-side lifecycle and the one pending review seek | the frame |
| `busy` (renderer) | `diagram-runtime/host.js:18` | bool | one Mermaid render at a time inside the frame | the message handler (23, 72) |

### B. Mermaid

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `ready` | `diagrams.js:7` | `Promise` | the one renderer boot; cached for the page's life | `renderer` (28) only |
| `frame`, `broken` | `:7` | element / Error | the singleton iframe and the latched boot/render failure | `renderer`/`fail` (36–44) |
| `queue` | `:8` | `Promise` | serialises render jobs | `render` (91, 111) |
| `pending` | `:5` | `Map<id, job>` | render jobs awaiting a frame result | `render` (102), `receive` (52), `fail` (42) |
| `sequence` | `:7` | int | job ids **and** source-control element ids | `render` (99), `mount` (124) |
| `cache`, `cacheBytes` | `:5–6` | `Map<source, result>` / int | LRU of rendered SVGs (32 entries / 8 MiB) | `remember` (73–83) |
| `observations` | `:6` | `Map<figure, IntersectionObserver>` | figures whose render is still pending | `mount` (184), the column MutationObserver (12), intersect (157) |
| `figure._diagramMounted` | `:116` | bool | mount-once marker | `mount` |
| `failure`, `reported` | `:130` | Error / bool | the last failure and the report-once latch | `mount` (131–141), catch (179) |
| `figure._diagramRepairSettled`, `status.hidden` | `:138, 201–204` | bool | whether an optimistic label may overwrite the status | `repairResult` |

### C. Interactive UI host

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `mounts` | `host.jsx:7` | `Map<block_id, record>` | one record per rendered block | `mount`/`unmount` |
| `busy` | `:8` | bool | the reply lock mirrored from `bloom.setBusy` | `setBusy` (119) |
| `record.block` | `:86, 97` | dict | last accepted block payload | `mount`, `update` |
| `record.state`, `stateInitialized` | `:86–89` | dict / bool | widget values and whether defaults were merged | `mount`, `change` (29), `update` (99) |
| `record.sequence` | `:90` | int | last acknowledged `ui_state.sequence` | `mount` (90), `flush` (80) |
| `record.dirty` | `:90` | bool | a change not yet acknowledged | `change` (30), `flush` (74, 81) |
| `record.timer` | `:32` | timer id | the 250 ms debounce | `change`, `flush`, `unmount` |
| `record.saving` | `:76` | `Promise \| null` | the one in-flight save | `flush` |
| `record.submitting` | `:40` | bool | the submit lock | `submit` |
| `record.error` | `:90, 46` | string | the surface error | `submit`, `flush` callers, `Boundary` |

### D. Main page controller

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `state.busy`, `state.status`, `state.replies`, `state.recentOutcome` | `bloom.js:47–59` | — | reply lock, last status payload, live replies by key, last outcome | `setBusy` (965), `on.status` (676), `on.reply_*` (779–859) |
| `state.rigorPending`, `rigorTarget` | `:52, 1008` | bool / string | the one in-flight rigour save and its optimistic mode | `setRigor` (1036–1052) |
| `effortTarget` | `:1094` | string \| null | the optimistic effort level | `cycleEffort` (1119–1127) |
| `historyEpoch` | `:622` | int | invalidates an in-flight earlier-page request | `on.transcript` (735) |
| `suggestionEpoch`, `suggestionTimer`, `popover` | `:1604–1608` | int / timer / `{open,items,index,reference,referenceDetail}` | the `/` `@` `$` popover and its debounce | `updateCommandPopover` (1672–1705), `closeCommands` (1707), `send` (2042), `on.conversations` (722) |
| `state.dialog`, `state.dialogVariant`, `el.app.inert`, `.scrim.is-closing` | `:58, 1264–1265, 1290, 1310` | — | the modal machine | `openDialog`/`closeDialog` |
| `layout` | `:1763` | `{width, collapsed, wrap}` | persisted pane layout | `setSidebar`, `endDrag`, `setWrap`, `restoreLayout` |
| `overlayMode`, `overlayOpen`, `drag` | `:1766, 1772, 1858` | bool / object | responsive drawer mode, drawer visibility, live drag | `fitPanes` (1924), `setSidebar` (1812), pointer handlers (1859–1905) |
| `paneSpring`, `widthSpring`, `stickPending`, `pinned` | `:1764–1765, 189, 2001` | Spring / bool | pane presence, sidebar width, follow coalescing, reader pin | `applyPane`, `settleWidth`, `follow` (191), scroll listener (2002) |
| `frame.dataset.state`, `.imageSrc`, `.previewId`, `.imageAlt` | `:357, 346, 349, 339` | string | per-image-frame loader state | `imageFrame`, `loadImage`, `requestStoredImage` |

## States

### A1. One Motion Canvas player (`motion-canvas-player.js`)

| State | Predicate |
| --- | --- |
| `UNMOUNTED` | not in `players` |
| `BOOTING_OFFSCREEN` | mounted, `!ready && !failed`, last intersection false (no timer armed) |
| `BOOTING_VISIBLE` | mounted, `!ready && !failed`, intersecting, timer armed |
| `READY_PAUSED` | `ready && !playing && !failed` |
| `PLAYING` | `ready && playing && !failed` |
| `FAILED` | `failed`; `dataset.error` set, controls disabled, `srcdoc` cleared |
| `DISPOSED` | observer saw `!figure.isConnected`; player removed, listeners removed |

`INSPECTING` is not a page state: it is a frame-only state
(`motion-canvas-frame.js:152–157, 62–133`) that the page never observes except
through the resulting `inspected` message (consumed by the Motion Canvas probe
harness, not by `bloom.js`).

### A2. One built-in canvas player (`animations.js`)

`IDLE` (`!playing`), `PLAYING` (`playing`, `active === player`), `ENDED`
(`!playing`, `time >= scene.duration`), `DEFUNCT` (`!figure.isConnected` — the
observers and animation frames are released lazily by `tick`, `:223`). The first
render at mount is `IDLE` regardless of `prefers-reduced-motion` (`:245–246`).

### B. Mermaid singleton and figures

| State | Predicate |
| --- | --- |
| `UNINITIALIZED` | `ready` undefined |
| `BOOTING` | `ready` pending, no failure yet |
| `READY_IDLE` | `ready` resolved, `pending` empty |
| `RENDERING_JOB` | a job is registered in `pending` (at most one, by the queue) |
| `BROKEN` | `broken` set; latched for the page (`:36–44, 94`) |

Per figure: `OBSERVING` (mounted, `_diagramMounted`, observer armed) →
`QUEUED_AND_RENDERING` (intersected; inside the queue) → `IMAGE_LOADING` (img
created, `load` not yet fired) → `RENDERED_DIAGRAM` (load fired; viewport shown,
source hidden) or `FAILED_AWAITING_SETTLE` (`failure` set, not yet reported) →
`REPAIR_REPORTED` (`reported` true). A failure before `messageState ===
'complete'` stays in `FAILED_AWAITING_SETTLE` until `settled()` (`:188–190`)
or a later `mount` (via the `_notifyDiagramFailure` hook) reports it.

### C. Interactive UI host

Render mode (per record, recomputed every `render`):

| State | Predicate | Shown |
| --- | --- | --- |
| `UNSUPPORTED_VERSION` | `!supported(b)` (`host.jsx:18–20`) | `data-ui-status="unsupported_version"`, "needs a newer UI runtime" |
| `PREPARING` | compatible, no `document`, `status==='streaming'` | "Preparing interactive answer…" |
| `UNUSABLE_STATUS` | compatible, `document`, status not `ready`/`streaming` | "Interactive answer {status}." |
| `RENDERED` | `usable` | `Renderer` inside `Host.Provider` |
| `BOUNDARY_ERROR` | `Boundary` caught | "Interactive view unavailable. Use the text below." + `record.error` |

Save state (per record): `CLEAN` (`!dirty`, no timer, no `saving`) →
`DIRTY_DEBOUNCING` (`dirty`, `timer` armed, 250 ms) → `SAVING` (`saving` non-null)
→ `CLEAN` on acknowledgement, or `SAVE_FAILED` (`dirty` restored, `error` set).
`DIRTY_DEBOUNCING`/`SAVE_FAILED` are indistinguishable from `CLEAN` in
`inspect()` (it exposes only `state`, `sequence`, `status`).

Submit: `IDLE` → `SUBMITTING` (`submitting`) → `IDLE`; `REFUSED` is `IDLE` plus a
`record.error`, because `submit` catches its own failure (`:41–47`).

### D. Main page controller

| Machine | States |
| --- | --- |
| Turn/composer lock | `IDLE` (`!busy`), `GENERATING` (`busy`), `LOCKED_BY_RIGOR` (`rigorPending`, orthogonal to `busy`) |
| Citation rigour | `SAVED(mode)`, `SAVING(target)`, `SAVE_FAILED(mode)` (reverted, toast) |
| Reasoning effort | `SAVED(level)`, `SAVING(target)` (superseding), `SAVE_FAILED(level)` |
| Suggestion popover | `CLOSED`, `DEBOUNCING(epoch, query)`, `OPEN(items,index)` |
| Modal | `CLOSED`, `OPEN(dialog={buttons,onClose,trigger})`, `CLOSING(is-closing)`, `IMMEDIATE_CLOSE` |
| Sidebar | `WIDE_OPEN`, `WIDE_COLLAPSED` (`layout.collapsed`), `DRAGGING`, `OVERLAY_HIDDEN`, `OVERLAY_SHOWN` (`overlayOpen`) |
| Transcript follow | `PINNED` (`pinned`, `nearBottom()`), `READING` (not at bottom) |
| Earlier pages | `IDLE`, `LOADING(epoch, button)` |
| Stored image | `ABSENT` (no `dataset.state`), `LOADING` (inline src), `WAITING` (preview requested), `READY`, `ERROR` |

## Events

- **Bridge events** (`bloom.receive`): `status`, `metrics`, `conversations`,
  `transcript`, `snapshot`, `user_message`, `reply_start`, `reply_update`,
  `reply_end`, `reasoning`, `reply_images`, `tool_activity`, `audit_update`,
  `ui_update`, `diagram_update`, `notice`, `animation_recovery`, `attachments`,
  `confirm_delete`, `help`, `open_*`, `edit_approval`, `library_approval`,
  `bell`, `flash`, `restore_draft`, `quit` (`bloom.js:675–950`).
- **Frame messages**: `blackrose-motion` `host-ready` / `ready` / `state` /
  `raster` / `error` / `inspected` (frame → page) and
  `blackrose-motion-control` `initialize` / `play` / `pause` / `seek` / `loop` /
  `inspect` (page → frame); `blackrose-mermaid` `ready` / `result` (frame →
  page) and `render` (page → frame).
- **User input**: pointerdown/move/up/cancel on the resize handle, `dblclick`,
  its arrow/Home/End/Enter keys; menu-toggle and scrim presses; document
  keydown (dialog trap, keymap, Escape); input events and the 180 ms debounce;
  `focusout` on a UI block; image `load`/`error` and clicks.
- **Timers**: 20 s boot/render timeouts; 250 ms UI-state debounce; 180 ms
  suggestion debounce; 1400 ms toast; 150 ms live-region coalescing
  (`bloom.js:3548`); 100 ms Motion Canvas resize debounce.
- **Observers**: IntersectionObserver (player visibility, image previews,
  diagram lazy render), MutationObserver (player dispose, diagram figure
  collection, live regions), ResizeObserver (panes, composer, transcript,
  dock, settings text scale).
- **Startup**: `window.pywebview` present → `start()` (`bloom.js:3563–3564`);
  else wait for `pywebviewready`. `start` calls `ready` once and appends the
  welcome (`3479–3494`). `restoreLayout` applies the saved layout without
  animating (`3496–3508`).

## Transition table

### A. Animation players

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `—` | `mount` | `playerDocument` present (13) | build DOM, add to `players` (176), observe body + frame (177–178), load frame (179–180) | `BOOTING_*` |
| `BOOTING_OFFSCREEN` | intersect true | `!ready && !failed` (93) | arm 20 s timer (`showError`) (93–95) | `BOOTING_VISIBLE` |
| `BOOTING_VISIBLE` | intersect false | — | `clearTimeout`; `player.pause()` (92, 96) | `BOOTING_OFFSCREEN` |
| `BOOTING_VISIBLE` | timer | `!ready` at fire (158) | `showError('…timed out…')` (94, 158) | `FAILED` |
| `BOOTING_*` | message `host-ready` | same frame, not failed (117, 119) | `postMessage initialize(document)` (120) | `BOOTING_*` |
| `BOOTING_*` | message `ready` | not failed | `ready=true`; clear timer; `dataset.ready`; enable controls; send `loop` (130–134) | `READY_PAUSED` |
| `READY_PAUSED` | Play press | `playing` false (147) | `animations.pauseAll`; `motion.pauseAll`; send `play`; `playing=true` (143–146) | `PLAYING` |
| `PLAYING` | Play press | `playing` (147) | `send('pause')`; `playing=false` (77) | `READY_PAUSED` |
| `PLAYING` | other engine starts | — | `pauseAll()` posts `pause` to every player (`animations.js:137`, `motion-canvas-player.js:11`) | `READY_PAUSED` |
| any live | `document.hidden` | — | `pauseAll()` (184, `animations.js:250`) | `READY_PAUSED` |
| any live | message `state` | not failed | adopt `time`/`duration`/`playing` (135–140) | `PLAYING`/`READY_PAUSED` |
| any live | message `error` | not failed (119) | `showError` (127–128) | `FAILED` |
| any live | `showError` | not already failed (99) | latch; pause; disable; `dataset.error`; **`srcdoc=''`** (107); report once (108–114) | `FAILED` |
| `FAILED` | retry press | — | reset flags; clear dataset; re-arm 20 s; reload frame (149–160) | `BOOTING_VISIBLE` |
| any live | figure removed | `!figure.isConnected` (170) | pause; clear timer; `srcdoc=''`; leave `players`; remove listeners (171–174) | `DISPOSED` |
| `PLAYING` | seek/replay/loop | — | `send('seek' \| 'play' \| 'loop')`; pause first (148, 161–162) | `READY_PAUSED`/`PLAYING` |

Built-in player (`animations.js`): `start` pauses every other engine, resets an
ended scene, sets `active`, and schedules `tick` (208–214); `pause` cancels the
frame, restores the button, clears `active` (201–207); `tick` pauses on
`!isConnected || document.hidden`, advances by wall time with optional modulo
looping, renders, and pauses at the end or on a render throw (221–233).

### B. Mermaid

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNINITIALIZED` | first `render` | cache miss, `current()` (86, 92) | create `frame` (29–34), arm 20 s boot timer (35), listen (63), load `mermaid-host.html` (64), append (65) | `BOOTING` |
| `BOOTING` | message `ready` | same frame (47) | clear timer; resolve `ready` (48) | `READY_IDLE` |
| `BOOTING` | boot timer / frame error | — (35, 66) | `fail`: latch `broken`, remove frame, reject `ready` and every job (36–44) | `BROKEN` |
| `READY_IDLE` | queued job | `current()`, size ≤ 20 000, `!broken`, still cache miss (92–96) | `id = ++sequence`; arm 20 s job timer; post `render` (99–106) | `RENDERING_JOB` |
| `RENDERING_JOB` | message `result` | known id (50) | resolve with `{svg,label}` or reject with `repairable` (53–61); `remember` (108) | `READY_IDLE` |
| `RENDERING_JOB` | job timer | — (101) | `frame._fail('Diagram rendering timed out.')` | `BROKEN` |
| any | `result` with a bad shape | id known (58–61) | reject `'Invalid diagram result.'` | unchanged |
| `—` | `current()` false after an await | 92, 98, 158 | return `null`; nothing posted | unchanged |
| figure `OBSERVING` | intersect | first intersection (155) | disconnect, forget, `render` (156–158) | `QUEUED_AND_RENDERING` |
| `QUEUED_AND_RENDERING` | render resolved | figure connected (159) | create img, attach `load`/`error`, set data URL (160–175) | `IMAGE_LOADING` |
| `IMAGE_LOADING` | img `load` | connected (163) | `changed(...)`: show viewport, hide source, enable controls (164–168) | `RENDERED_DIAGRAM` |
| `IMAGE_LOADING` | img `error` | — (170) | status text; show source (171–172) | `FAILED` (no repair) |
| `QUEUED_AND_RENDERING` | render rejected | connected (177) | status text; store `failure`; `_notifyDiagramFailure` (178–180) | `FAILED_AWAITING_SETTLE` |
| `FAILED_AWAITING_SETTLE` | `settled(root)` | repairable, `!reported`, `.msg[data-message-id]`, `messageState==='complete'` (133–135) | `reported=true`; `failed(...)` (136) | `REPAIR_REPORTED` |
| `REPAIR_REPORTED` | result `queued` | `!_diagramRepairSettled` (138) | status "Repairing diagram…" | `REPAIR_REPORTED` |
| `REPAIR_REPORTED` | result terminal | `repairResult` has a message (198) | rewrite every visible status in that root (199–205) | `REPAIR_REPORTED` |
| `RENDERED_DIAGRAM` | `repairResult` | `status.hidden` (201) | no-op | `RENDERED_DIAGRAM` |

### C. Interactive UI host

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `—` | `mount` | id not mounted (85) | build record; sequence from `state_sequence`; defaults or saved snapshot; `focusout` listener; render (86–92) | `RENDERED`/mode |
| `—` | `mount` | id already mounted (85) | delegate to `update` | unchanged |
| `DIRTY*` | `change(key,value)` | — | set/delete key; `dirty=true`; re-arm 250 ms; render (29–33) | `DIRTY_DEBOUNCING` |
| `DIRTY_DEBOUNCING` | timer | — | `flush().catch(show error)` (32) | `SAVING`/`CLEAN` |
| `CLEAN`/`DIRTY*` | `focusout` | — | `flush().catch(show error)` (91) | `SAVING` |
| `SAVING` | another `flush` | `saving` set (72) | await the in-flight save first | `SAVING` |
| `SAVING` | save ok | `result.ok`, status ready/streaming (73, 79) | `sequence = result.state_sequence` (80) | `CLEAN` |
| `SAVING` | save refused/throws | — (79, 81) | `dirty=true`; rethrow (`record.error`) | `SAVE_FAILED` |
| `SAVE_FAILED` | `update(status='ready')` | `dirty` (103) | `flush` again | `SAVING` |
| `—` | `update(id, block)` | record exists; `source_sequence` not lower (95–96) | merge block; seed defaults once (98–101); render | unchanged |
| `IDLE` | `submit()` | not disabled, `!submitting` (25, 39) | `submitting=true`; flush; `ui_action` with a fresh `event_id` (40–45) | `SUBMITTING` |
| `SUBMITTING` | action ok | `result.ok` (45) | — | `IDLE` |
| `SUBMITTING` | action refused/throws | — (45–46) | `record.error` | `IDLE` |
| `IDLE` | `submit()` | `disabled` or `submitting` (39) | return without calling the bridge | `IDLE` |
| any | `unmount` | — (107) | clear debounce, unmount root, forget | `—` |

`disabled` is computed at render time (25): status not `ready`, module `busy`,
`!actions_enabled`, or `submitting`. Because `render` is not `flushSync`
(`flushSync` is only used by the probe-only `sync`, 122), the prop lags the
state; the transition guard at 39 is what actually serialises submits.

### D. Main page controller

Turn/composer lock:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | `status.generating=true` | — (690) | `setBusy(true)`: Stop label, attach/retry disabled, UI `setBusy` (965–980) | `GENERATING` |
| `IDLE`/`GENERATING` | `reply_start` | — (781) | `setBusy(true)` | `GENERATING` |
| any | `reply_end` | — (846) | `setBusy(false)` even if the reply is unknown | `IDLE` |
| `GENERATING` | composer submit | `!rigorPending` (2057) | `call('stop')` (2058) | `GENERATING` until events settle |
| `IDLE` | composer submit | `!rigorPending` | `send()` (2059) | `GENERATING` after `reply_start` |
| any | Ctrl/Cmd+Enter | keymap `send` (2289) | `send()` regardless of `busy` (2037) | unchanged (Python refuses) |
| `IDLE`/`GENERATING` | rigour save starts | `!busy && !rigorPending && mode differs` (1037) | `rigorPending=true`; `rigorTarget=mode`; send/retry disabled; `aria-busy` (1038–1041) | `LOCKED_BY_RIGOR` |
| `LOCKED_BY_RIGOR` | save settles | — (1046–1051) | clear target/pending; adopt `result.citation_rigor` if ok; re-render | back |
| any | retry press | `!retryPending && !busy && !rigorPending && can_retry` (2066) | `retryPending`; `call('retry')` | unchanged |

Suggestion popover:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `CLOSED`/`OPEN` | input | `/` prefix (1676–1682) | items = matching commands; draw immediately | `OPEN` |
| any | input | `@`/`$`/`@{` before the caret (1685–1690) | `closeCommands()`; `epoch=++suggestionEpoch`; 180 ms timer (1673–1694, 1704) | `DEBOUNCING` |
| `DEBOUNCING` | timer fires | — (1694) | `await reference_suggestions(trigger, query, true)` | `DEBOUNCING` |
| `DEBOUNCING` | response | `epoch === suggestionEpoch && value === input.value && cursor === selectionStart` (1696) | map items; `drawSuggestions` (1700–1703) | `OPEN` |
| `DEBOUNCING` | response | any guard false | drop | `CLOSED` |
| `OPEN` | Escape | popover open and input focused (2318, 1746) | `++epoch`; clear timer; `closeCommands` | `CLOSED` |
| `OPEN` | pointerdown outside | not in the list and not the input (1750–1752) | close | `CLOSED` |
| `OPEN` | Tab/Enter on an item | items non-empty (1740–1744) | `completeCommand`: insert reference or command head (1716–1728) | `CLOSED` |
| `DEBOUNCING` | send / conversations change / browse | 2042, 722, 1654 | `++epoch`; clear timer; close | `CLOSED` |

Modal:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `CLOSED` | `openDialog` | — (1252) | clone the dialog node (fresh accessible name, 1254–1261); populate; `show(scrim, trigger)`; `app.inert=true`; focus first action (1262–1293) | `OPEN` |
| `OPEN` | `openDialog` | `state.dialog` (1253) | `closeDialog(false, true)` then open | `OPEN` (new) |
| `OPEN` | action button | — (1283) | `closeDialog(value)` | `CLOSING` |
| `OPEN` | `y`/`n` | a `true`-valued button exists (1330–1331) | `closeDialog(true/false)` | `CLOSING` |
| `OPEN` | Escape | — (1323) | `closeDialog(false)` | `CLOSING` |
| `OPEN` | Tab | controls non-empty (1324–1328) | wrap focus inside the dialog | `OPEN` |
| `OPEN` | scrim pointerdown | `e.target === scrim` (1335) | `closeDialog(false)` | `CLOSING` |
| `CLOSING` | spring rest | reduced motion or ~0.35 s | remove `is-closing`; hide container (1310–1311, `Presence.hide`) | `CLOSED` |
| `CLOSING` | `openDialog` | — (1288) | `is-closing` removed; spring retargeted to 1 | `OPEN` |
| any | `closeDialog` | `immediate` (1307) | hide at once, restore focus, run `onClose` | `CLOSED` |

Focus is restored to the trigger (or the composer) and `onClose(value)` runs in
both the immediate and animated paths (1301–1312); `app.inert` drops
immediately at close, before the fade finishes.

Sidebar:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `WIDE_*` | menu toggle | not overlay (1813–1817) | `layout.collapsed = !open`; `save_layout`; `applyPane` | toggled |
| `WIDE_OPEN` | pointerdown on handle | `button===0 && !overlayMode` (1860) | capture; `drag={id,start,grab,samples}`; `is-resizing` (1863–1866) | `DRAGGING` |
| `DRAGGING` | pointermove | same pointer id (1869) | rubber-band past the bounds; track samples (1870–1877) | `DRAGGING` |
| `DRAGGING` | pointerup/cancel | same pointer id (1880) | velocity projection; a fast inward flick collapses and keeps the width (1890–1898); otherwise clamp, persist, spring (1899–1902) | `WIDE_*` |
| `WIDE_*` | handle keys | (1907–1922) | arrow ±1 rem/3 rem; Home/End bounds; Enter toggles | toggled |
| `WIDE_*` | dblclick | — (1906) | `layout.width=null`; persist; settle to default | `WIDE_*` |
| any | breakpoint crossed | `drag` null (1932) | flip `overlayMode`; `overlayOpen=false`; `applyPane({animate:false})` | `OVERLAY_HIDDEN` |
| `OVERLAY_*` | menu toggle | overlay (1813–1814) | `overlayOpen=open`; `applyPane` (no persist) | toggled |
| `OVERLAY_SHOWN` | outside pointerdown | not sidebar/menu toggle (1827–1830) | `setSidebar(false)` | `OVERLAY_HIDDEN` |
| `OVERLAY_SHOWN` | Escape | overlay open (2326) | `setSidebar(false)` | `OVERLAY_HIDDEN` |
| `OVERLAY_SHOWN` | conversation opened | (1190) | `closeSidebarIfOverlay` | `OVERLAY_HIDDEN` |

Transcript:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | `transcript` | — (731–749) | `++historyEpoch`; pause players; `unmountAll`; clear replies; replace column; welcome or messages; scroll to end | `PINNED` |
| `IDLE` | earlier button | `aria-busy !== 'true'` (645) | `aria-busy`; text; `epoch=historyEpoch`; `await earlier_messages` | `LOADING` |
| `LOADING` | page resolves | `epoch === historyEpoch && button.isConnected` (650) | splice older messages; preserve scroll from the bottom; focus; announce (657–670) | `IDLE` |
| `LOADING` | page is null | same guard | reset button; toast | `IDLE` |
| `LOADING` | page resolves late | guard false | drop | `IDLE` |
| `PINNED`/`READING` | scroll | — (2002) | `pinned = nearBottom()` | recomputed |
| `PINNED` | append/update/dock reflow | `wasAtBottom` or `pinned` (191–198, 1985–1988) | one rAF scroll to `scrollHeight` (`stickPending` coalesces) | `PINNED` |
| `READING` | append/update | `wasAtBottom` false | no scroll | `READING` |

Images:

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | inline `src` | URL is http(s) or a png/jpeg/webp data URL (365) | build button/img/placeholder; `state=loading`; async decode (367–408) | `LOADING` |
| `ABSENT` | `preview_id` | — (348–355) | observe for intersection (or fetch now) | `ABSENT` |
| `ABSENT` | intersect | observer not yet unobserved (432–436) | `requestStoredImage` | `LOADING` |
| `LOADING` (preview) | bridge returns a src | frame still connected (418) | store `imageSrc`; `state=waiting`; `loadImage` | `WAITING` → `LOADING` |
| `LOADING` (preview) | bridge returns null | connected | `state=error`; retry label (425–427) | `ERROR` |
| `LOADING` | img `load` | — (388–396) | `state=ready`; reveal; aria labels; `follow(pinned)` | `READY` |
| `LOADING` | img `error` | — (397–405) | `state=error`; drop img; retry label | `ERROR` |
| `ERROR` | retry click | `.image-load` (444–447) | `loadImage` again (or re-fetch the preview) | `LOADING` |
| `READY` | click | `state==='ready'` (449) | open the image dialog | `READY` |
| any | streaming redraw | same `imageSrc` **and** `imageAlt` (270–273) | adopt the old node object (state and listeners preserved) | unchanged |

## Invariants

1. **At most one player is PLAYING across both engines at any instant**, for
   players the two modules created. Every start entry point pauses the other
   engine first: `animations.js:137` (plus `BlackroseMotionPlayer.pauseAll`),
   `animations.js:209`, `motion-canvas-player.js:11, 144`, and
   `document.hidden` pauses both (184, `animations.js:250`). An unsolicited
   frame `state` report is adopted without pausing the other engine
   (`motion-canvas-player.js:135–140`) — see H2. Pinned by
   `sm22_players.cjs` ("one PLAYING across both engines").
2. **A failed mount is terminal and reported once.** `showError` is idempotent
   (99), unloads the frame (107), and reports exactly one failure per mount
   (110–113); the recovery label is monotone: a late `queued` cannot overwrite a
   later status (61). Pinned by the fail-latch and recovery-routing assertions.
3. **A failure report never outlives its figure.** The `onFailure` result is
   applied only while `figure.isConnected` (112–113); `recovery()` routes by
   attachment id (185–187). Pinned.
4. **Disposal releases every listener and the frame's execution.** The observer
   removes the player from `players`, removes the `message` and
   `prefers-reduced-motion` listeners, disconnects both observers, pauses and
   clears `srcdoc` (169–175). Pinned (`sm22_players.cjs` A3).
5. **Boot time is only counted while the frame is intersecting.**
   `IntersectionObserver` arms the 20 s clock on entry and clears it on exit
   (91–97). It is *not* gated on page visibility (H1).
6. **One Mermaid renderer per page, created at most once.** `renderer()` returns
   the cached promise (27); `fail` removes the iframe, rejects `ready` and every
   pending job, and latches `broken` so no replacement is created (36–44, 94).
   Pinned by `sm22_players.cjs` B2.
7. **Mermaid jobs are serialised and one at a time in the frame.** `queue`
   chains every render (91, 111) and `pending` holds at most one id; the frame
   itself drops a second `render` while busy (`diagram-runtime/host.js:22–23`).
   Pinned by B1 ("jobs are serialised").
8. **A queued job never touches a disconnected figure.** `current()` is checked
   before the queue work, again after boot, and before the resolved image is
   used (92, 98, 158–159); a removed figure never boots the renderer. Pinned by
   B3.
9. **A diagram source over 20 000 characters or a non-cache miss is refused
   before any frame message.** (93, `diagram-runtime/host.js:26`.) Pinned.
10. **A diagram failure is reported once per mount, and only for a repairable
    error on a complete message.** `reported` (135) and
    `message.dataset.messageState === 'complete'` (134) gate the call; an
    unrepairable error never reports. Pinned by B1's repair assertions.
11. **The Mermaid cache is keyed by source only, bounded by count and bytes, and
    never stores a result over 8 MiB** (`diagrams.js:73–83`). The page's theme
    and Mermaid version are fixed per page, so the key is sufficient (84–86).
12. **A UI state save advances `record.sequence` by exactly one per acknowledged
    save and never overlaps another save** (`host.jsx:72, 75, 80`). The backend
    enforces `sequence == current + 1` (`repository.py:1060–1063`), so the two
    must agree. Pinned by `sm22_ui_host.mjs` 5 and 6.
13. **A refused save keeps the record dirty and retries the same `sequence + 1`**
    (`host.jsx:81`); a refused or non-ready flush leaves `dirty` untouched
    (73). Pinned.
14. **A submit flushes first and sends one `ui_action` with a fresh `event_id`,
    one identity, `action_id='submit'`, `action_type='continue_conversation'`**
    (`host.jsx:41–45`). Pinned.
15. **The submit lock is the record, not the DOM.** `submit` refuses when
    `disabled` (captured at render time) *or* `record.submitting` (39). Pinned by
    the double-submit regression, which failed before this guard was added.
16. **`unmount` clears the debounce and the root and forgets the block; it does
    not flush** (`host.jsx:107`). Consequence in H3/H4; pinned.
17. **A model-authored UI document only reaches React as a JSON literal**
    (`library.jsx:105–108`), never as language or code; the schema hash must
    equal the artifact's (`host.jsx:19`). Pinned by
    `ui-runtime/runtime.test.mjs`.
18. **The page drops an event for another conversation** (`bloom.js:952–961`).
    Pinned by the `animation_recovery` assertion in `sm22_bloom.cjs`.
19. **The turn lock is exactly `state.busy`, derived only from bridge status and
    reply events** (`bloom.js:965–980`); a composer submit while locked sends
    `stop`, never `submit` (2055–2060). Pinned.
20. **A rigour save is single-flight and optimistic**: `state.rigorPending` blocks
    send, retry, and a second save (1037, 975, 983, 2038, 2057); the shown mode
    is `rigorTarget ?? state.status.citation_rigor` (1010) and a refused save
    reverts it (1046–1051). Pinned.
21. **An effort save is supersedable**: a later press replaces `effortTarget`, and
    an earlier save cannot clear or report an error over it (`bloom.js:1122–1127`).
    Pinned.
22. **A suggestion result is applied only if its epoch, the input value and the
    caret position all still match** (`bloom.js:1696`); every input, Escape,
    send, conversation change and browse invalidates the epoch (1674, 1746,
    2042, 722, 1654). Pinned.
23. **At most one dialog is open, the page is inert while it is, and focus
    returns to the trigger** (`bloom.js:1253, 1290, 1301–1312`). Pinned.
24. **The sidebar's persisted layout is written only from a user action and only
    in wide mode** (`bloom.js:1812–1817, 1890–1901, 1906–1921`); overlay toggles
    never persist. Pinned.
25. **A drag flicks shut only for an inward release whose projection lands more
    than 6 rem past the minimum** (`bloom.js:1886–1898`); otherwise the released
    width is clamped to `sidebarBounds` and persisted. Pinned.
26. **An earlier-page response is applied only to the transcript it was requested
    for** (`bloom.js:648–650`). Pinned.
27. **Auto-scroll follows only a reader who was at the bottom** (64 px
    threshold, 184–187); `follow` measures before the mutation and coalesces to
    one rAF (189–198); a pinned reader is re-pinned across reflows (2001–2005).
    Pinned.
28. **An image frame's loader state is `dataset.state`**, one of absent /
    `loading` / `waiting` / `ready` / `error`, and a streaming redraw preserves
    a frame only when both `imageSrc` and `imageAlt` match (`bloom.js:264–273`),
    so a load in flight survives the redraw. Pinned.
29. **Generated code stays in an opaque-origin frame.** Both iframes are
    `sandbox="allow-scripts"` with `referrerpolicy="no-referrer"`
    (`motion-canvas-player.js:17–18`, `diagrams.js:30–32`), neither frame has the
    desktop bridge, and both validate `event.source` and a channel before
    acting. Pinned by `test_sm_22_frontend_runtimes.LoadOrderTests`.
30. **The page's script order is a load-time contract**: runtimes before
    `bloom.js`, `#column` before `diagrams.js`. Pinned.

## Hazards and open questions

- **H1 — `fragile` — `motion-canvas-player.js:91–97` with `147–160`.**
  The init clock is gated on *intersection*, not page visibility. A minimized
  window or a hidden document stops WebKit's RAF (the comment at 89–90 says so)
  while the document is still "intersecting", so the 20 s timer fires and the
  player reports a timeout for a frame that never had a chance to render.
  Demonstrated by `sm22_players.cjs` ("hazard: the boot timer runs while the page
  cannot render"). The retry timer (157–158) is armed without consulting
  intersection at all; the observer clears it on the next entry/exit event (92).
  Confirming in the product needs a GTK window minimize/hide during a boot.
  Fix shape: arm the timer only while `isIntersecting && !document.hidden`, and
  re-arm on `visibilitychange`.
- **H2 — `none-found` (limit) — `motion-canvas-player.js:135–140`.** The
  single-`PLAYING` invariant is enforced cooperatively at start entry points,
  not centrally. A frame that reports `state.playing === true` is believed and
  the page does not pause the other engine, so two players can report playing if
  a frame starts itself (e.g. a looping timeline resumed by the frame). Nothing
  in the frame can start playback without a command (`motion-canvas-frame.js:142–163`),
  so this is a structural limit, not a reachable defect. Pinned as such.
- **H3 — `fragile` — `motion-canvas-player.js:109–113` with `bloom.js:27`.**
  `bloom.call` swallows a bridge rejection and resolves `null`, so
  `options.onFailure` resolves `null`; `setRecovery(null)` clears
  `figure.dataset.recovery` and hides the note instead of showing
  "Automatic repair is unavailable" (the `.catch` at 113 only covers a genuine
  rejection inside the page). The user sees the failure text with no repair
  status. Demonstrated by `sm22_players.cjs` ("a null result erases the recovery
  label"). Fix shape: treat a null result as `{status:'unavailable'}` in the
  player, or stop swallowing in `bloom.call` for this endpoint.
- **H4 — `documentation-gap` — `motion-canvas-frame.js:152–157, 62–136`.**
  `inspection` is only cleared when the rendered frame equals the requested
  frame. If `playback.frame` never reaches it (seek clamped to the 120 s range
  while the measured duration is shorter), `inspection` stays set and the
  `player.deactivate()` at 136 is skipped for the life of the frame, leaving the
  RAF loop alive in an opaque frame. Reasoned-only: the request is clamped to the
  same 120 s range the player is configured with (33), so the frame should
  always render. Confirming needs a scene whose measured duration is under the
  requested time by more than a frame.
- **H5 — `fragile` — `diagrams.js:35–44, 101`.** One slow render (20 s) latches
  `broken` for the whole page, so every later diagram in every message fails
  until the window reloads. Deliberate (the comment at 67–69 explains the
  latency/parser argument), but it couples one pathological diagram to the rest
  of the conversation. Demonstrated by B2/B3. A narrower fix (replace the frame
  and retry once) would need a policy decision, so none is proposed.
- **H6 — `fragile` — `diagrams.js:136–140, 191–206`.** A terminal repair result
  is applied to **every** `figure[data-mermaid]` in the root with a visible
  status, not only the failed figure, and terminal messages are last-writer-wins
  (`_diagramRepairSettled` only suppresses the optimistic label). Two failed
  diagrams in one message would both receive the same repair text. Pinned as
  "terminal messages are last-writer-wins inside one root"; confirming user
  impact needs two failures in one reply (Python reports one repair per reply —
  see SM-08).
- **H7 — `fragile` — `diagrams.js:131–141`.** `reported` latches *before*
  `failed(...)` resolves. If the bridge returns `null` (a swallowed call), the
  figure never re-requests repair: `settled()` sees `reported` and stops.
  Reasoned-only; confirmation needs a bridge failure on `diagram_failed`.
- **H8 — `defect` (fixed) — `ui-runtime/host.jsx:35–39`.** The submit lock was
  only the React `disabled` prop, and `render` is not synchronous
  (`flushSync` is used only by the probe `sync`, 122). Two submits inside one
  commit both read the captured `disabled === false` and each called
  `ui_action`; Python refuses the second (`session.py:578–580` returns "Wait for
  the current reply to finish", or the DB refuses it with "Duplicate or
  in-flight UI submission", SM-06 H1) and the page shows a spurious error.
  Demonstrated: the regression assertion reported `3 !== 2` before the guard was
  added and passes after (`sm22_ui_host.mjs` 8).
- **H9 — `fragile` — `ui-runtime/host.jsx:70–83, 96`.** Cross-references
  SM-05 H5. A save that the backend committed but whose reply was lost leaves
  `record.sequence` one behind; every retry posts the same stale number and is
  refused (`repository.py:1062–1063`), and `update()` never adopts
  `block.state_sequence`. Only a remount recovers. Demonstrated end to end
  against a sequence-checking fake backend (`sm22_ui_host.mjs` 7). The repair
  belongs with SM-05's proposal (adopt a larger incoming `state_sequence` in
  `update`), so no source change is proposed here.
- **H10 — `fragile` — `ui-runtime/host.jsx:105–108` with `bloom.js:731–749`.**
  `unmount`/`unmountAll` clear the debounce without flushing, and
  `bloom.on.transcript` calls `unmountAll()` on every redraw. A dirty widget
  torn down by a redraw loses the pending edit. Demonstrated ("unmount clears
  the pending debounce without flushing"). A fire-and-forget flush on unmount
  would *cause* H9 (a save landing after the redraw's snapshot), so the fix must
  come with H9's sequence adoption: keep the record across a redraw keyed by
  `block_id`, or make the redraw payload authoritative for `state_sequence`.
- **H11 — `documentation-gap` — `ui-runtime/host.jsx:25`.** `effectsDisabled` is
  `false || … || record.submitting`, so it is `undefined` until the first submit
  and React treats it as "not disabled". Any consumer that compares it to
  `false` or serialises it is wrong. Pinned by `assert.ok(!host.effectsDisabled)`.
- **H12 — `documentation-gap` — `ui-runtime/host.jsx:98–101`.** `stateInitialized`
  becomes true as soon as a document exists, so defaults added by a later
  (final) document are never merged for a widget that streamed with a document.
  Reasoned-only; SM-05 H4 covers the status half of the same window.
- **H13 — `fragile` — `bloom.js:2037–2060, 2289` with `keymap.json`
  (`ctrl+enter`, `alt+enter`, `ctrl+s` → `send`).** Those bindings map to
  `send()` directly while the composer's submit event routes `busy` to `stop()`
  (2058). The keyboard path therefore calls `submit` during generation; Python
  refuses with a bell and `{clear:false}` (`session.py:935–937`) and the page
  restores the draft (`bloom.js:2048–2052`), so nothing is lost, but the two
  entry points disagree. Demonstrated.
- **H14 — `fragile` — `bloom.js:1111–1128`.** After a successful effort save the
  page clears `effortTarget` and re-reads `state.status.reasoning_effort`
  instead of the `reasoning_effort` the bridge returns
  (`session.py:1712–1716`). The label shows the previous level until the pushed
  `status` event arrives — a one-event flicker. Demonstrated ("the label falls
  back to the last pushed status").
- **H15 — `fragile` — `bloom.js:1694–1696`.** The debounce result is dropped if
  the caret moved for an unrelated reason (focus change, programmatic edit, a
  click that repositions the caret) even though the value still matches; nothing
  retries until the next keystroke. Reasoned-only.
- **H16 — `fragile` — `bloom.js:779–788`.** `reply_start` for a key already in
  `state.replies` overwrites the mapping without removing the earlier article,
  leaving an `is-streaming` bubble that no later `reply_update`/`reply_end`
  reaches. No emitter does this today (every `reply_end` deletes the key and
  `transcript`/`snapshot` clear the map first). Reasoned-only.
- **H17 — `fragile` — `bloom.js:1879–1902` with `1924–1937`.** `fitPanes`
  returns early while `drag` is set (`1932`) *after* it may have flipped
  `overlayMode` and cleared `overlayOpen` (1926–1931). An `endDrag` that follows
  a window resize across the breakpoint then persists `layout.collapsed` and a
  width clamped with overlay bounds. Reasoned-only; confirming needs a resize
  during a drag.
- **H18 — `fragile` — `bloom.js:1533–1535, 1569–1573`.** Nested-dialog
  continuations call `openDialog` (via `openEditBackups`) after an `await`; a
  dialog the user opened during that wait is silently replaced by the
  continuation, because `openDialog` closes the current dialog with value
  `false` (`1253`) and its `onClose` no-ops. Reasoned-only.
- **H19 — `fragile` — `bloom.js:363–367, 411–429`.** The image loader has no
  timeout: a frame whose `img` never fires `load`/`error` (or whose preview
  bridge never resolves) stays `loading` with `aria-busy="true"` for the life of
  the frame, and `loadImage` refuses to retry while `dataset.state ===
  'loading'` (366). A disconnected frame returns early at 418 and leaves the
  same flags on a discarded node. Reasoned-only; needs a stalled network/bridge.
- **H20 — `none-found` — the rest of the audited machines.** The dialog
  presence machine (`openDialog`/`closeDialog`/`Presence`), the popover epoch,
  transcript paging and follow, and the sidebar springs are internally
  consistent: closing during the fade is handled by clearing `is-closing` on open
  (1288), `follow` measures before mutating and coalesces (189–198), and the
  drag release is idempotent per pointer id (1880). Pinned by the corresponding
  `sm22_bloom.cjs` sections.

## Formalization decisions

One source change:

- **`ui-runtime/host.jsx:35–39` — the submit lock now checks
  `record.submitting` as well as the captured `disabled`.** This is the smallest
  change that makes the `IDLE -> SUBMITTING` transition's guard real instead of
  an asynchronously-committed DOM attribute (H8). It changes no bridge contract:
  the payload, identity and `event_id` shape are untouched, and a refused second
  submit now produces no call instead of a spurious error. The regression is
  `sm22_ui_host.mjs` section 8 (two synchronous submits → one action), which
  fails with `3 !== 2` without the guard. The generated bundle
  (`src/blackrose/gui/static/ui-runtime.js`, `ui-contract.json`) is gitignored;
  the harness bundles `host.jsx` itself, and a prepared app must be rebuilt with
  `npm run build --prefix ui-runtime` to pick the fix up.

No other source change is proposed:

- **H9/H10 (sequence repair and teardown flush)** are one design decision about
  who owns `state_sequence` after a lost acknowledgement, and SM-05 already
  carries the proposal on the Python side. `update()` adopting a larger
  `block.state_sequence` (and the redraw payload including it) would fix both;
  doing it here alone would risk a half-fix that trades a wedge for a skipped
  sequence.
- **H5 (latched Mermaid renderer)** needs a policy choice between "reload once"
  and "one bad diagram disables the rest"; the current latch is deliberate.
- **H6/H7 (repair scoping and report latch)** would change how Python groups
  diagram repairs (SM-08); the page cannot tell which figure a reply-level
  repair belongs to.
- **H1 (visibility-aware boot clock)** is a behaviour change in the player's
  timing contract; the failure mode needs a native minimize to confirm.
- **H3, H13–H19** are display/UX fragilities where the proposed change would be
  speculative without a reproduction.

## Verification

Run from the repository root.

```
$ .venv/bin/python scripts/check_unit_tests.py -p test_sm_22_frontend_runtimes.py -v
test_render_modes_flush_and_submit_locks ... ok
test_generated_code_stays_in_opaque_origin_frames ... ok
test_runtimes_load_before_the_page_controller ... ok
test_locks_savers_popover_dialog_sidebar_and_paging ... ok
test_animation_players_and_mermaid_lifecycles ... ok
----------------------------------------------------------------------
Ran 5 tests in 0.310s
OK
```

(Timing varies per machine; the five `ok` lines and `OK` are the result.)

The three harnesses on their own (`node`, offline, no display):

```
$ node tests/sm22_players.cjs
Animation player and Mermaid state machines passed.
$ node tests/sm22_ui_host.mjs
Interactive UI host state machine passed.
$ node tests/sm22_bloom.cjs
Main page controller state machines passed.
```

Pre-existing focused tests for the same modules:

```
$ .venv/bin/python scripts/check_unit_tests.py -p test_animations.py        # Ran 35 tests ... OK
$ .venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas.py     # Ran 11 tests ... OK (skipped=3)
$ .venv/bin/python scripts/check_unit_tests.py -p test_diagrams.py          # Ran 6 tests ... OK
$ .venv/bin/python scripts/check_unit_tests.py -p test_diagram_repairs.py   # Ran 11 tests ... OK
$ .venv/bin/python scripts/check_unit_tests.py -p test_interactive_ui_evaluation.py  # Ran 5 ... OK
$ .venv/bin/python scripts/check_unit_tests.py -p test_interactive_ui_fidelity.py    # Ran 8 ... OK
$ .venv/bin/python scripts/check_unit_tests.py -p test_conversation_paging.py        # Ran 11 ... OK
$ .venv/bin/python scripts/check_unit_tests.py -p test_chat_navigation.py            # Ran 7 ... OK
$ .venv/bin/python scripts/check_unit_tests.py -p test_effort.py                     # Ran 14 ... OK
$ .venv/bin/python scripts/check_unit_tests.py -p test_composer_drafts.py            # Ran 11 ... OK
```

Static and bundle checks:

```
$ node --check src/blackrose/gui/static/bloom.js            # ok
$ node --check src/blackrose/gui/static/diagrams.js         # ok
$ node --check src/blackrose/gui/static/animations.js       # ok
$ node --check src/blackrose/gui/static/motion-canvas-player.js  # ok
$ node --check src/blackrose/gui/static/motion-canvas-frame.js   # ok
$ npm test --prefix ui-runtime                              # tests 1, pass 1, fail 0
$ .venv/bin/python -m ruff check tests/test_sm_22_frontend_runtimes.py  # All checks passed
```

The regression proof for H8 was taken by reverting the guard locally and
re-running `node tests/sm22_ui_host.mjs`:

```
AssertionError [ERR_ASSERTION]: one action for two rapid submits
3 !== 2
```

**Not run here.** The native and browser probes need a display and packages this
machine does not have (`scripts/check_interactive_ui.cjs`,
`check_diagrams.cjs`, `check_effort.cjs`, `check_conversation_paging.cjs`,
`check_history_paging.cjs`, `check_chat_navigation.cjs`,
`check_reading_room.cjs`, and `scripts/check_gui_*.py --gui`, which need
`xvfb-run`/GTK/WebKit). `scripts/check_health.py` was not run as a whole (it
includes the full unit suite and mypy); the parts that cover this change were
run individually as listed. The three node harnesses are the offline replacement
for those probes' state-machine assertions, not for their rendering/visual
assertions.
