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
| Motion Canvas player | `src/blackrose/gui/static/motion-canvas-player.js` | the page's main frame | the page (`players` Set, one closure per mount) |
| Motion Canvas frame adapter | `src/blackrose/gui/static/motion-canvas-frame.js` | opaque `sandbox="allow-scripts"` iframe, no bridge | the frame (one closure) |
| Built-in canvas player | `src/blackrose/gui/static/animations.js` | the page's main frame | the page (`active`, one closure per mount) |
| Mermaid adapter | `src/blackrose/gui/static/diagrams.js` | the page's main frame | the page (module singleton + one closure per figure) |
| Mermaid renderer | `diagram-runtime/host.js` → `mermaid-host.html` (`diagram-runtime/build.mjs`) | opaque `sandbox="allow-scripts"` iframe, `default-src 'none'` CSP | the frame (`busy`) |
| Interactive UI host | `ui-runtime/host.jsx`, `ui-runtime/library.jsx` → `ui-runtime.js` (`ui-runtime/build.mjs`) | the page's main frame | the page (`mounts` Map, module `busy`) |
| Main page controller | `src/blackrose/gui/static/bloom.js` | the page's main frame | the page (one closure) |

`index.html` loads `animations.js`, `motion-canvas-player.js`, `ui-runtime.js`,
`diagrams.js`, `bloom.js` in that order (the `<script>` tags at the end of
`src/blackrose/gui/static/index.html`); `diagrams.js` resolves `#column` while it
loads (the module-level `MutationObserver` in `src/blackrose/gui/static/diagrams.js`)
and `bloom.js` resolves `.dock` (the `dock` entry of
`src/blackrose/gui/static/bloom.js::el`), so the order is a load-time dependency, not
a preference. The four generated artifacts (`ui-runtime.js`, `ui-contract.json`,
`mermaid-host.html`, `motion-canvas-runtime.js`), with `mermaid-manifest.json` and
the `*-NOTICES.txt` files beside them, are gitignored build outputs
(the generated `src/blackrose/gui/static/` entries in `.gitignore`) produced from the
sources above; a checkout must run `npm run build --prefix ui-runtime` and
`--prefix diagram-runtime` and `--prefix animation-runtime` before the window can
render.

Ownership follows [architecture](../architecture.md#modules-and-invariants):

- **Python owns every decision.** `bloom.receive` is the only inbound path
  (`src/blackrose/gui/static/bloom.js::receive`, exposed as `window.bloom` at the end
  of the file) and it drops any event whose `conversation_id` is not the current
  conversation. Outbound calls go through `window.pywebview.api`
  (`src/blackrose/gui/static/bloom.js::call`; `gui/bridge.py`).
- **The trusted page is the boundary.** Model output only reaches the page as
  escaped Markdown HTML (the header comment of `src/blackrose/gui/static/bloom.js`),
  validated UI documents (`ui_documents.py`, SM-05) or scene JSON; the sandboxed
  frames receive postMessage strings and hold no bridge (the iframe set-up in
  `src/blackrose/gui/static/motion-canvas-player.js::mount` and
  `src/blackrose/gui/static/diagrams.js::renderer`, `diagram-runtime/build.mjs` CSP).
- **Both sandboxed frames run with an opaque origin** and answer an explicit
  message allow-list; the `blackrose-motion-control` `message` listener in
  `src/blackrose/gui/static/motion-canvas-frame.js` and the `message` listener in
  `diagram-runtime/host.js` check `event.source`/channel before acting.
- **The page never writes durable state.** It reports and requests: animation
  failure (`animation_failed`), diagram failure (`diagram_failed`), UI state
  (`ui_state`), UI action (`ui_action`), layout (`save_layout`).
- The async bridge means every page transition that ends in a bridge call is at
  least one microtask long (`call()` chains through `Promise.resolve`,
  `src/blackrose/gui/static/bloom.js::call`); tests that observe calls must flush
  promise jobs.

## State variables

### A. Animation players

Player variables are closure state of
`src/blackrose/gui/static/motion-canvas-player.js::mount` (Motion Canvas) and
`src/blackrose/gui/static/animations.js::mount` (built-in); the frame's are top-level
in `src/blackrose/gui/static/motion-canvas-frame.js`, and the renderer's in
`diagram-runtime/host.js`.

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `players` | `src/blackrose/gui/static/motion-canvas-player.js::players` | `Set<player>` | every live Motion Canvas mount in the page | `mount`, the dispose observer (`mount.observer`) |
| `active` | `src/blackrose/gui/static/animations.js::active` | `player \| null` | the one built-in player that is playing | `start`, `pause` (`animations.js` `mount`) |
| `playing` | both `mount` closures | bool | per-player playback intent | `start`/`pause`, frame `state` reports (`mount.receive`) |
| `ready`, `failed` | Motion Canvas `mount` | bool | boot completed / mount is terminal | `mount.receive` (`ready` branch), `mount.showError`, the retry button's `onclick` |
| `time`, `duration` | Motion Canvas `mount` | number | playback position (s) and the frame-reported duration | `mount.receive` (`ready`/`state` branch) and `seek` |
| `timeout` | Motion Canvas `mount` | timer id | the 20 s boot/retry clock | `mount.visibility`, `mount.receive` error branch, the retry button's `onclick` |
| `options.recovery`, `figure.dataset.recovery` | `mount.setRecovery` | object / string | the label and the dedup marker for the repair chain | `setRecovery` only |
| `figure.dataset.ready`, `.error`, `.recovery` | Motion Canvas `mount` | string | DOM-visible lifecycle | `receive`, `showError`, retry |
| `ready`, `failed`, `inspection` | `src/blackrose/gui/static/motion-canvas-frame.js::inspection` (one `let`) | bool / object | frame-side lifecycle and the one pending review seek | the frame |
| `busy` (renderer) | `diagram-runtime/host.js::busy` | bool | one Mermaid render at a time inside the frame | the `message` listener (set on entry, reset in its `finally`) |

### B. Mermaid

All variables are module state of `src/blackrose/gui/static/diagrams.js` unless noted.

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `ready` | `src/blackrose/gui/static/diagrams.js::ready` | `Promise` | the one renderer boot; cached for the page's life | `renderer` only |
| `frame`, `broken` | `src/blackrose/gui/static/diagrams.js::broken` (same `let`) | element / Error | the singleton iframe and the latched boot/render failure | `renderer`/`renderer.fail` |
| `queue` | `src/blackrose/gui/static/diagrams.js::queue` | `Promise` | serialises render jobs | `render` |
| `pending` | `src/blackrose/gui/static/diagrams.js::pending` | `Map<id, job>` | render jobs awaiting a frame result | `render`, `renderer.receive`, `renderer.fail` |
| `sequence` | `src/blackrose/gui/static/diagrams.js::sequence` | int | job ids **and** source-control element ids | `render`, `mount` |
| `cache`, `cacheBytes` | `src/blackrose/gui/static/diagrams.js::cache` | `Map<source, result>` / int | LRU of rendered SVGs (32 entries / 8 MiB) | `remember` |
| `observations` | `src/blackrose/gui/static/diagrams.js::observations` | `Map<figure, IntersectionObserver>` | figures whose render is still pending | `mount`, the column MutationObserver, the per-figure intersect callback |
| `figure._diagramMounted` | `mount` | bool | mount-once marker | `mount` |
| `failure`, `reported` | `mount` (per figure) | Error / bool | the last failure and the report-once latch | `mount._notifyDiagramFailure`, the render `catch` |
| `figure._diagramRepairSettled`, `status.hidden` | `mount`, `repairResult` | bool | whether an optimistic label may overwrite the status | `repairResult` |

### C. Interactive UI host

All variables live in `ui-runtime/host.jsx`; a record is built by
`ui-runtime/host.jsx::mount`.

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `mounts` | `ui-runtime/host.jsx::mounts` | `Map<block_id, record>` | one record per rendered block | `mount`/`unmount` |
| `busy` | `ui-runtime/host.jsx::busy` | bool | the reply lock mirrored from `bloom.setBusy` | `setBusy` |
| `record.block` | `mount`, `update` | dict | last accepted block payload | `mount`, `update` |
| `record.state`, `stateInitialized` | `mount` | dict / bool | widget values and whether defaults were merged | `mount`, `host.change`, `update` |
| `record.sequence` | `mount` | int | last acknowledged `ui_state.sequence` | `mount`, `flush` |
| `record.dirty` | `mount` | bool | a change not yet acknowledged | `host.change`, `flush` |
| `record.timer` | `host.change` | timer id | the 250 ms debounce | `change`, `flush`, `unmount` |
| `record.saving` | `flush` | `Promise \| null` | the one in-flight save | `flush` |
| `record.submitting` | `host.submit` | bool | the submit lock | `submit` |
| `record.error` | `mount`, `host.submit` | string | the surface error | `submit`, `flush` callers, `Boundary` |

### D. Main page controller

All variables are closure state of `src/blackrose/gui/static/bloom.js`; the "Location"
column names the declaration.

| Variable | Location | Type | Meaning | Who writes |
| --- | --- | --- | --- | --- |
| `state.busy`, `state.status`, `state.replies`, `state.recentOutcome` | `src/blackrose/gui/static/bloom.js::state` | — | reply lock, last status payload, live replies by key, last outcome | `setBusy`, `on.status`, `on.reply_*` |
| `state.rigorPending`, `rigorTarget` | `state`; `src/blackrose/gui/static/bloom.js::rigorTarget` | bool / string | the one in-flight rigour save and its optimistic mode | `setRigor` |
| `effortTarget` | `src/blackrose/gui/static/bloom.js::effortTarget` | string \| null | the optimistic effort level | `cycleEffort` |
| `historyEpoch` | `src/blackrose/gui/static/bloom.js::historyEpoch` | int | invalidates an in-flight earlier-page request | `on.transcript` |
| `suggestionEpoch`, `suggestionTimer`, `popover` | `src/blackrose/gui/static/bloom.js::popover` and the `let`s after it | int / timer / `{open,items,index,reference,referenceDetail}` | the `/` `@` `$` popover and its debounce | `updateCommandPopover`, `closeCommands`, `send`, `on.conversations` |
| `state.dialog`, `state.dialogVariant`, `el.app.inert`, `.scrim.is-closing` | `state`; `openDialog`, `closeDialog` | — | the modal machine | `openDialog`/`closeDialog` |
| `layout` | `src/blackrose/gui/static/bloom.js::layout` | `{width, collapsed, wrap}` | persisted pane layout | `setSidebar`, `endDrag`, `setWrap`, `restoreLayout` |
| `overlayMode`, `overlayOpen`, `drag` | `src/blackrose/gui/static/bloom.js::overlayMode`, `src/blackrose/gui/static/bloom.js::overlayOpen`, `src/blackrose/gui/static/bloom.js::drag` | bool / object | responsive drawer mode, drawer visibility, live drag | `fitPanes`, `setSidebar`, the `el.resizer` pointer handlers |
| `paneSpring`, `widthSpring`, `stickPending`, `pinned` | `src/blackrose/gui/static/bloom.js::paneSpring`, `src/blackrose/gui/static/bloom.js::widthSpring`, `src/blackrose/gui/static/bloom.js::stickPending`, `src/blackrose/gui/static/bloom.js::pinned` | Spring / bool | pane presence, sidebar width, follow coalescing, reader pin | `applyPane`, `settleWidth`, `follow`, the transcript `scroll` listener |
| `frame.dataset.state`, `.imageSrc`, `.previewId`, `.imageAlt` | `src/blackrose/gui/static/bloom.js::imageFrame`, `src/blackrose/gui/static/bloom.js::loadImage` | string | per-image-frame loader state | `imageFrame`, `loadImage`, `requestStoredImage` |

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

`INSPECTING` is not a page state: it is a frame-only state (the `inspect` command
branch of the control `message` listener and the inspection block of the
`player.onRender` subscriber in `src/blackrose/gui/static/motion-canvas-frame.js`) that
the page never observes except through the resulting `inspected` message (consumed by
the Motion Canvas probe harness, not by `bloom.js`).

### A2. One built-in canvas player (`animations.js`)

`IDLE` (`!playing`), `PLAYING` (`playing`, `active === player`), `ENDED`
(`!playing`, `time >= scene.duration`), `DEFUNCT` (`!figure.isConnected` — the
observers and animation frames are released lazily by `tick`, see
`src/blackrose/gui/static/animations.js::mount.tick`). The first render at mount is
`IDLE` regardless of `prefers-reduced-motion` (the final `render()` call of
`src/blackrose/gui/static/animations.js::mount`).

### B. Mermaid singleton and figures

| State | Predicate |
| --- | --- |
| `UNINITIALIZED` | `ready` undefined |
| `BOOTING` | `ready` pending, no failure yet |
| `READY_IDLE` | `ready` resolved, `pending` empty |
| `RENDERING_JOB` | a job is registered in `pending` (at most one, by the queue) |
| `BROKEN` | `broken` set; latched for the page (`src/blackrose/gui/static/diagrams.js::renderer.fail` and the `broken` check in `src/blackrose/gui/static/diagrams.js::render`) |

Per figure: `OBSERVING` (mounted, `_diagramMounted`, observer armed) →
`QUEUED_AND_RENDERING` (intersected; inside the queue) → `IMAGE_LOADING` (img
created, `load` not yet fired) → `RENDERED_DIAGRAM` (load fired; viewport shown,
source hidden) or `FAILED_AWAITING_SETTLE` (`failure` set, not yet reported) →
`REPAIR_REPORTED` (`reported` true). A failure before `messageState ===
'complete'` stays in `FAILED_AWAITING_SETTLE` until `settled()`
(`src/blackrose/gui/static/diagrams.js::settled`) or a later `mount` (via the
`_notifyDiagramFailure` hook) reports it.

### C. Interactive UI host

Render mode (per record, recomputed every `render`):

| State | Predicate | Shown |
| --- | --- | --- |
| `UNSUPPORTED_VERSION` | `!supported(b)` (`ui-runtime/host.jsx::supported`) | `data-ui-status="unsupported_version"`, "needs a newer UI runtime" |
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
`record.error`, because `submit` catches its own failure
(`ui-runtime/host.jsx::render.host.submit`).

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
  `bell`, `flash`, `restore_draft`, `quit` (the handlers of
  `src/blackrose/gui/static/bloom.js::on`).
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
  (`src/blackrose/gui/static/bloom.js::forwardLiveRegions`); 100 ms Motion Canvas
  resize debounce.
- **Observers**: IntersectionObserver (player visibility, image previews,
  diagram lazy render), MutationObserver (player dispose, diagram figure
  collection, live regions), ResizeObserver (panes, composer, transcript,
  dock, settings text scale).
- **Startup**: `window.pywebview` present → `start()` (the last statements of
  `src/blackrose/gui/static/bloom.js`); else wait for `pywebviewready`. `start` calls
  `ready` once and appends the welcome (`src/blackrose/gui/static/bloom.js::start`).
  `restoreLayout` applies the saved layout without animating
  (`src/blackrose/gui/static/bloom.js::restoreLayout`).

## Transition table

### A. Animation players

Owned by `src/blackrose/gui/static/motion-canvas-player.js::mount` and its closures
(`mount.visibility`, `mount.receive`, `mount.showError`, `mount.start`,
`mount.observer`, the button handlers), with the module-level
`src/blackrose/gui/static/motion-canvas-player.js::pauseAll`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `—` | `mount` | `playerDocument` present (`mount`'s first check) | build DOM, add to `players`, observe body + frame, load frame (the end of `mount`) | `BOOTING_*` |
| `BOOTING_OFFSCREEN` | intersect true | `!ready && !failed` (`mount.visibility`) | arm 20 s timer (`showError`) | `BOOTING_VISIBLE` |
| `BOOTING_VISIBLE` | intersect false | — | `clearTimeout`; `player.pause()` (`mount.visibility`) | `BOOTING_OFFSCREEN` |
| `BOOTING_VISIBLE` | timer | `!ready` at fire (the retry timer's check) | `showError('…timed out…')` (the `mount.visibility` and retry timers) | `FAILED` |
| `BOOTING_*` | message `host-ready` | same frame, not failed (`mount.receive`'s source and `failed` checks) | `postMessage initialize(document)` | `BOOTING_*` |
| `BOOTING_*` | message `ready` | not failed | `ready=true`; clear timer; `dataset.ready`; enable controls; send `loop` (`mount.receive`'s `ready` branch) | `READY_PAUSED` |
| `READY_PAUSED` | Play press | `playing` false (the play button's `onclick`) | `animations.pauseAll`; `motion.pauseAll`; send `play`; `playing=true` (`mount.start`) | `PLAYING` |
| `PLAYING` | Play press | `playing` (the play button's `onclick`) | `send('pause')`; `playing=false` (`player.pause`) | `READY_PAUSED` |
| `PLAYING` | other engine starts | — | `pauseAll()` posts `pause` to every player (`src/blackrose/gui/static/animations.js::pauseAll`, `src/blackrose/gui/static/motion-canvas-player.js::pauseAll`) | `READY_PAUSED` |
| any live | `document.hidden` | — | `pauseAll()` (the `visibilitychange` listeners of both modules) | `READY_PAUSED` |
| any live | message `state` | not failed | adopt `time`/`duration`/`playing` (`mount.receive`'s `ready`/`state` branch) | `PLAYING`/`READY_PAUSED` |
| any live | message `error` | not failed (`mount.receive`) | `showError` (`mount.receive`'s `error` branch) | `FAILED` |
| any live | `showError` | not already failed (`mount.showError`'s first check) | latch; pause; disable; `dataset.error`; **`srcdoc=''`**; report once (the `options.onFailure` chain) | `FAILED` |
| `FAILED` | retry press | — | reset flags; clear dataset; re-arm 20 s; reload frame (the retry button's `onclick`) | `BOOTING_VISIBLE` |
| any live | figure removed | `!figure.isConnected` (`mount.observer`) | pause; clear timer; `srcdoc=''`; leave `players`; remove listeners | `DISPOSED` |
| `PLAYING` | seek/replay/loop | — | `send('seek' \| 'play' \| 'loop')`; pause first (the replay, seek and loop handlers) | `READY_PAUSED`/`PLAYING` |

Built-in player (`src/blackrose/gui/static/animations.js::mount`): `start` pauses
every other engine, resets an ended scene, sets `active`, and schedules `tick`
(`mount.start`); `pause` cancels the frame, restores the button, clears `active`
(`mount.pause`); `tick` pauses on `!isConnected || document.hidden`, advances by wall
time with optional modulo looping, renders, and pauses at the end or on a render throw
(`mount.tick`).

### B. Mermaid

Owned by `src/blackrose/gui/static/diagrams.js::renderer`,
`src/blackrose/gui/static/diagrams.js::render`,
`src/blackrose/gui/static/diagrams.js::mount`,
`src/blackrose/gui/static/diagrams.js::settled` and
`src/blackrose/gui/static/diagrams.js::repairResult`.

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `UNINITIALIZED` | first `render` | cache miss, `current()` (`render`) | create `frame`, arm 20 s boot timer, listen, load `mermaid-host.html`, append (`renderer`) | `BOOTING` |
| `BOOTING` | message `ready` | same frame (`renderer.receive`'s source check) | clear timer; resolve `ready` | `READY_IDLE` |
| `BOOTING` | boot timer / frame error | — (the boot `setTimeout` and the frame `error` listener) | `fail`: latch `broken`, remove frame, reject `ready` and every job (`renderer.fail`) | `BROKEN` |
| `READY_IDLE` | queued job | `current()`, size ≤ 20 000, `!broken`, still cache miss (the checks at the top of the queued task in `render`) | `id = ++sequence`; arm 20 s job timer; post `render` | `RENDERING_JOB` |
| `RENDERING_JOB` | message `result` | known id (`renderer.receive`) | resolve with `{svg,label}` or reject with `repairable`; `remember` | `READY_IDLE` |
| `RENDERING_JOB` | job timer | — (the per-job `setTimeout` in `render`) | `frame._fail('Diagram rendering timed out.')` | `BROKEN` |
| any | `result` with a bad shape | id known (`renderer.receive`'s final `else`) | reject `'Invalid diagram result.'` | unchanged |
| `—` | `current()` false after an await | the two `current()` checks in `render` and the figure check in `mount`'s render callback | return `null`; nothing posted | unchanged |
| figure `OBSERVING` | intersect | first intersection (`mount`'s `IntersectionObserver` callback) | disconnect, forget, `render` | `QUEUED_AND_RENDERING` |
| `QUEUED_AND_RENDERING` | render resolved | figure connected | create img, attach `load`/`error`, set data URL | `IMAGE_LOADING` |
| `IMAGE_LOADING` | img `load` | connected (the `load` listener) | `changed(...)`: show viewport, hide source, enable controls | `RENDERED_DIAGRAM` |
| `IMAGE_LOADING` | img `error` | — (the `error` listener) | status text; show source | `FAILED` (no repair) |
| `QUEUED_AND_RENDERING` | render rejected | connected (the render `catch`) | status text; store `failure`; `_notifyDiagramFailure` | `FAILED_AWAITING_SETTLE` |
| `FAILED_AWAITING_SETTLE` | `settled(root)` | repairable, `!reported`, `.msg[data-message-id]`, `messageState==='complete'` (`mount._notifyDiagramFailure`'s guard) | `reported=true`; `failed(...)` | `REPAIR_REPORTED` |
| `REPAIR_REPORTED` | result `queued` | `!_diagramRepairSettled` (`mount._notifyDiagramFailure`) | status "Repairing diagram…" | `REPAIR_REPORTED` |
| `REPAIR_REPORTED` | result terminal | `repairResult` has a message | rewrite every visible status in that root (`repairResult`) | `REPAIR_REPORTED` |
| `RENDERED_DIAGRAM` | `repairResult` | `status.hidden` (`repairResult`) | no-op | `RENDERED_DIAGRAM` |

### C. Interactive UI host

Owned by `ui-runtime/host.jsx::mount`, `ui-runtime/host.jsx::update`,
`ui-runtime/host.jsx::unmount`, `ui-runtime/host.jsx::flush` and the `host` object
built in `ui-runtime/host.jsx::render` (`render.host.change`, `render.host.submit`).

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `—` | `mount` | id not mounted (`mount`'s `mounts.has` check) | build record; sequence from `state_sequence`; defaults or saved snapshot; `focusout` listener; render | `RENDERED`/mode |
| `—` | `mount` | id already mounted | delegate to `update` | unchanged |
| `DIRTY*` | `change(key,value)` | — | set/delete key; `dirty=true`; re-arm 250 ms; render (`render.host.change`) | `DIRTY_DEBOUNCING` |
| `DIRTY_DEBOUNCING` | timer | — | `flush().catch(show error)` (the debounce `setTimeout` in `change`) | `SAVING`/`CLEAN` |
| `CLEAN`/`DIRTY*` | `focusout` | — | `flush().catch(show error)` (the `focusout` listener added in `mount`) | `SAVING` |
| `SAVING` | another `flush` | `saving` set (`flush`'s first `await`) | await the in-flight save first | `SAVING` |
| `SAVING` | save ok | `result.ok`, status ready/streaming (`flush`) | `sequence = result.state_sequence` | `CLEAN` |
| `SAVING` | save refused/throws | — (`flush`'s `catch`) | `dirty=true`; rethrow (`record.error`) | `SAVE_FAILED` |
| `SAVE_FAILED` | `update(status='ready')` | `dirty` (`update`'s last statement) | `flush` again | `SAVING` |
| `—` | `update(id, block)` | record exists; `source_sequence` not lower (`update`'s first checks) | merge block; seed defaults once; render | unchanged |
| `IDLE` | `submit()` | not disabled, `!submitting` (`render`'s `disabled`, `submit`'s guard) | `submitting=true`; flush; `ui_action` with a fresh `event_id` | `SUBMITTING` |
| `SUBMITTING` | action ok | `result.ok` | — | `IDLE` |
| `SUBMITTING` | action refused/throws | — (`submit`'s `catch`) | `record.error` | `IDLE` |
| `IDLE` | `submit()` | `disabled` or `submitting` (`submit`'s guard) | return without calling the bridge | `IDLE` |
| any | `unmount` | — (`unmount`) | clear debounce, unmount root, forget | `—` |

`disabled` is computed at render time (the `disabled` constant in
`ui-runtime/host.jsx::render`): status not `ready`, module `busy`,
`!actions_enabled`, or `submitting`. Because `render` is not `flushSync`
(`flushSync` is only used by the probe-only `ui-runtime/host.jsx::sync`), the prop
lags the state; the transition guard in `submit` is what actually serialises submits.

### D. Main page controller

All functions named in these tables are in `src/blackrose/gui/static/bloom.js`;
anonymous listeners are named by the element they are attached to.

Turn/composer lock (`src/blackrose/gui/static/bloom.js::setBusy`,
`src/blackrose/gui/static/bloom.js::setRigor`,
`src/blackrose/gui/static/bloom.js::send`,
`src/blackrose/gui/static/bloom.js::retryPrompt` and the `el.composer` `submit`
listener):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `IDLE` | `status.generating=true` | — (`on.status`) | `setBusy(true)`: Stop label, attach/retry disabled, UI `setBusy` (`setBusy`) | `GENERATING` |
| `IDLE`/`GENERATING` | `reply_start` | — (`on.reply_start`) | `setBusy(true)` | `GENERATING` |
| any | `reply_end` | — (`on.reply_end`) | `setBusy(false)` even if the reply is unknown | `IDLE` |
| `GENERATING` | composer submit | `!rigorPending` (the `el.composer` `submit` listener) | `call('stop')` | `GENERATING` until events settle |
| `IDLE` | composer submit | `!rigorPending` | `send()` | `GENERATING` after `reply_start` |
| any | Ctrl/Cmd+Enter | keymap `send` (`actions.send`) | `send()` regardless of `busy` (`send`) | unchanged (Python refuses) |
| `IDLE`/`GENERATING` | rigour save starts | `!busy && !rigorPending && mode differs` (`setRigor`'s first check) | `rigorPending=true`; `rigorTarget=mode`; send/retry disabled; `aria-busy` | `LOCKED_BY_RIGOR` |
| `LOCKED_BY_RIGOR` | save settles | — (`setRigor`'s `finally`) | clear target/pending; adopt `result.citation_rigor` if ok; re-render | back |
| any | retry press | `!retryPending && !busy && !rigorPending && can_retry` (`retryPrompt`) | `retryPending`; `call('retry')` | unchanged |

Suggestion popover (`src/blackrose/gui/static/bloom.js::updateCommandPopover`,
`src/blackrose/gui/static/bloom.js::closeCommands`,
`src/blackrose/gui/static/bloom.js::commandKey`,
`src/blackrose/gui/static/bloom.js::completeCommand`):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `CLOSED`/`OPEN` | input | `/` prefix (`updateCommandPopover`'s command branch) | items = matching commands; draw immediately | `OPEN` |
| any | input | `@`/`$`/`@{` before the caret (`updateCommandPopover`'s reference branch) | `closeCommands()`; `epoch=++suggestionEpoch`; 180 ms timer | `DEBOUNCING` |
| `DEBOUNCING` | timer fires | — | `await reference_suggestions(trigger, query, true)` | `DEBOUNCING` |
| `DEBOUNCING` | response | `epoch === suggestionEpoch && value === input.value && cursor === selectionStart` (the response check in `updateCommandPopover`) | map items; `drawSuggestions` | `OPEN` |
| `DEBOUNCING` | response | any guard false | drop | `CLOSED` |
| `OPEN` | Escape | popover open and input focused (the document `keydown` listener → `commandKey`) | `++epoch`; clear timer; `closeCommands` | `CLOSED` |
| `OPEN` | pointerdown outside | not in the list and not the input (the document `pointerdown` listener after `commandKey`) | close | `CLOSED` |
| `OPEN` | Tab/Enter on an item | items non-empty (`commandKey`) | `completeCommand`: insert reference or command head | `CLOSED` |
| `DEBOUNCING` | send / conversations change / browse | `send`, `on.conversations`, `drawSuggestions` | `++epoch`; clear timer; close | `CLOSED` |

Modal (`src/blackrose/gui/static/bloom.js::openDialog`,
`src/blackrose/gui/static/bloom.js::closeDialog`,
`src/blackrose/gui/static/bloom.js::dialogKey`, and the `el.scrim` `pointerdown`
listener):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `CLOSED` | `openDialog` | — | clone the dialog node (fresh accessible name); populate; `show(scrim, trigger)`; `app.inert=true`; focus first action (`openDialog`) | `OPEN` |
| `OPEN` | `openDialog` | `state.dialog` (`openDialog`'s first check) | `closeDialog(false, true)` then open | `OPEN` (new) |
| `OPEN` | action button | — (the action button's click handler in `openDialog`) | `closeDialog(value)` | `CLOSING` |
| `OPEN` | `y`/`n` | a `true`-valued button exists (`dialogKey`) | `closeDialog(true/false)` | `CLOSING` |
| `OPEN` | Escape | — (`dialogKey`) | `closeDialog(false)` | `CLOSING` |
| `OPEN` | Tab | controls non-empty (`dialogKey`) | wrap focus inside the dialog | `OPEN` |
| `OPEN` | scrim pointerdown | `e.target === scrim` (the `el.scrim` listener) | `closeDialog(false)` | `CLOSING` |
| `CLOSING` | spring rest | reduced motion or ~0.35 s | remove `is-closing`; hide container (`closeDialog`'s `dialogPresence.hide` callback, `Presence.hide`) | `CLOSED` |
| `CLOSING` | `openDialog` | — (`openDialog` removes `is-closing`) | `is-closing` removed; spring retargeted to 1 | `OPEN` |
| any | `closeDialog` | `immediate` (`closeDialog`'s immediate branch) | hide at once, restore focus, run `onClose` | `CLOSED` |

Focus is restored to the trigger (or the composer) and `onClose(value)` runs in
both the immediate and animated paths (`closeDialog.finish`); `app.inert` drops
immediately at close, before the fade finishes.

Sidebar (`src/blackrose/gui/static/bloom.js::setSidebar`,
`src/blackrose/gui/static/bloom.js::endDrag`,
`src/blackrose/gui/static/bloom.js::fitPanes`, and the `el.resizer` listeners
declared beside `src/blackrose/gui/static/bloom.js::drag`):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `WIDE_*` | menu toggle | not overlay (`setSidebar`'s `else`) | `layout.collapsed = !open`; `save_layout`; `applyPane` | toggled |
| `WIDE_OPEN` | pointerdown on handle | `button===0 && !overlayMode` (the `el.resizer` `pointerdown` listener) | capture; `drag={id,start,grab,samples}`; `is-resizing` | `DRAGGING` |
| `DRAGGING` | pointermove | same pointer id (the `pointermove` listener) | rubber-band past the bounds; track samples | `DRAGGING` |
| `DRAGGING` | pointerup/cancel | same pointer id (`endDrag`) | velocity projection; a fast inward flick collapses and keeps the width; otherwise clamp, persist, spring | `WIDE_*` |
| `WIDE_*` | handle keys | (the `el.resizer` `keydown` listener) | arrow ±1 rem/3 rem; Home/End bounds; Enter toggles | toggled |
| `WIDE_*` | dblclick | — (the `el.resizer` `dblclick` listener) | `layout.width=null`; persist; settle to default | `WIDE_*` |
| any | breakpoint crossed | `drag` null (`fitPanes`) | flip `overlayMode`; `overlayOpen=false`; `applyPane({animate:false})` | `OVERLAY_HIDDEN` |
| `OVERLAY_*` | menu toggle | overlay (`setSidebar`'s overlay branch) | `overlayOpen=open`; `applyPane` (no persist) | toggled |
| `OVERLAY_SHOWN` | outside pointerdown | not sidebar/menu toggle (the document `pointerdown` listener after `closeSidebarIfOverlay`) | `setSidebar(false)` | `OVERLAY_HIDDEN` |
| `OVERLAY_SHOWN` | Escape | overlay open (the last branch of the document `keydown` listener) | `setSidebar(false)` | `OVERLAY_HIDDEN` |
| `OVERLAY_SHOWN` | conversation opened | (`openConversation`) | `closeSidebarIfOverlay` | `OVERLAY_HIDDEN` |

Transcript (`src/blackrose/gui/static/bloom.js::on.transcript`,
`src/blackrose/gui/static/bloom.js::loadEarlier`,
`src/blackrose/gui/static/bloom.js::follow`):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| any | `transcript` | — (`on.transcript`) | `++historyEpoch`; pause players; `unmountAll`; clear replies; replace column; welcome or messages; scroll to end | `PINNED` |
| `IDLE` | earlier button | `aria-busy !== 'true'` (`loadEarlier`) | `aria-busy`; text; `epoch=historyEpoch`; `await earlier_messages` | `LOADING` |
| `LOADING` | page resolves | `epoch === historyEpoch && button.isConnected` (`loadEarlier`) | splice older messages; preserve scroll from the bottom; focus; announce | `IDLE` |
| `LOADING` | page is null | same guard | reset button; toast | `IDLE` |
| `LOADING` | page resolves late | guard false | drop | `IDLE` |
| `PINNED`/`READING` | scroll | — (the `el.transcript` `scroll` listener) | `pinned = nearBottom()` | recomputed |
| `PINNED` | append/update/dock reflow | `wasAtBottom` or `pinned` (`follow`, the `el.dock` `ResizeObserver`) | one rAF scroll to `scrollHeight` (`stickPending` coalesces) | `PINNED` |
| `READING` | append/update | `wasAtBottom` false | no scroll | `READING` |

Images (`src/blackrose/gui/static/bloom.js::imageFrame`,
`src/blackrose/gui/static/bloom.js::loadImage`,
`src/blackrose/gui/static/bloom.js::requestStoredImage`,
`src/blackrose/gui/static/bloom.js::previewObserver`,
`src/blackrose/gui/static/bloom.js::imageClick`,
`src/blackrose/gui/static/bloom.js::renderHTML`):

| From | Event | Guard | Effect | To |
| --- | --- | --- | --- | --- |
| `ABSENT` | inline `src` | URL is http(s) or a png/jpeg/webp data URL (`loadImage`'s URL check) | build button/img/placeholder; `state=loading`; async decode (`loadImage`) | `LOADING` |
| `ABSENT` | `preview_id` | — (`imageFrame`) | observe for intersection (or fetch now) | `ABSENT` |
| `ABSENT` | intersect | observer not yet unobserved (`previewObserver`) | `requestStoredImage` | `LOADING` |
| `LOADING` (preview) | bridge returns a src | frame still connected (`requestStoredImage`'s `isConnected` check) | store `imageSrc`; `state=waiting`; `loadImage` | `WAITING` → `LOADING` |
| `LOADING` (preview) | bridge returns null | connected | `state=error`; retry label (`requestStoredImage`) | `ERROR` |
| `LOADING` | img `load` | — (`loadImage`'s `load` listener) | `state=ready`; reveal; aria labels; `follow(pinned)` | `READY` |
| `LOADING` | img `error` | — (`loadImage`'s `error` listener) | `state=error`; drop img; retry label | `ERROR` |
| `ERROR` | retry click | `.image-load` (`imageClick`) | `loadImage` again (or re-fetch the preview) | `LOADING` |
| `READY` | click | `state==='ready'` (`imageClick`) | open the image dialog | `READY` |
| any | streaming redraw | same `imageSrc` **and** `imageAlt` (the frame-reuse loop in `renderHTML`) | adopt the old node object (state and listeners preserved) | unchanged |

## Invariants

1. **At most one player is PLAYING across both engines at any instant**, for
   players the two modules created. Every start entry point pauses the other
   engine first: `src/blackrose/gui/static/animations.js::pauseAll` (plus
   `BlackroseMotionPlayer.pauseAll`), `src/blackrose/gui/static/animations.js::mount.start`,
   `src/blackrose/gui/static/motion-canvas-player.js::pauseAll` and
   `src/blackrose/gui/static/motion-canvas-player.js::mount.start`, and
   `document.hidden` pauses both (the `visibilitychange` listeners of both modules).
   An unsolicited frame `state` report is adopted without pausing the other engine
   (the `ready`/`state` branch of
   `src/blackrose/gui/static/motion-canvas-player.js::mount.receive`) — see H2. Pinned
   by `sm22_players.cjs` ("one PLAYING across both engines").
2. **A failed mount is terminal and reported once.** `showError` is idempotent
   (its `failed` check), unloads the frame (`frame.srcdoc = ''`), and reports exactly
   one failure per mount (the `options.onFailure` chain in
   `src/blackrose/gui/static/motion-canvas-player.js::mount.showError`); the recovery
   label is monotone: a late `queued` cannot overwrite a later status (the first check
   in `src/blackrose/gui/static/motion-canvas-player.js::mount.setRecovery`). Pinned by
   the fail-latch and recovery-routing assertions.
3. **A failure report never outlives its figure.** The `onFailure` result is
   applied only while `figure.isConnected` (`mount.showError`); `recovery()` routes by
   attachment id (`recovery` on `window.BlackroseMotionPlayer` in
   `src/blackrose/gui/static/motion-canvas-player.js`). Pinned.
4. **Disposal releases every listener and the frame's execution.** The observer
   removes the player from `players`, removes the `message` and
   `prefers-reduced-motion` listeners, disconnects both observers, pauses and
   clears `srcdoc` (`src/blackrose/gui/static/motion-canvas-player.js::mount.observer`).
   Pinned (`sm22_players.cjs` A3).
5. **Boot time is only counted while the frame is intersecting.**
   `IntersectionObserver` arms the 20 s clock on entry and clears it on exit
   (`src/blackrose/gui/static/motion-canvas-player.js::mount.visibility`). It is *not*
   gated on page visibility (H1).
6. **One Mermaid renderer per page, created at most once.** `renderer()` returns
   the cached promise (its first statement); `fail` removes the iframe, rejects
   `ready` and every pending job, and latches `broken` so no replacement is created
   (`src/blackrose/gui/static/diagrams.js::renderer.fail` and the `broken` check in
   `src/blackrose/gui/static/diagrams.js::render`). Pinned by `sm22_players.cjs` B2.
7. **Mermaid jobs are serialised and one at a time in the frame.** `queue`
   chains every render (`src/blackrose/gui/static/diagrams.js::render`) and `pending`
   holds at most one id; the frame itself drops a second `render` while busy (the
   `busy` check in the `message` listener of `diagram-runtime/host.js`). Pinned by B1
   ("jobs are serialised").
8. **A queued job never touches a disconnected figure.** `current()` is checked
   before the queue work, again after boot, and before the resolved image is
   used (the two `current()` checks in `src/blackrose/gui/static/diagrams.js::render`
   and the `isConnected` check in the render callback of
   `src/blackrose/gui/static/diagrams.js::mount`); a removed figure never boots the
   renderer. Pinned by B3.
9. **A diagram source over 20 000 characters or a non-cache miss is refused
   before any frame message.** (The size check in
   `src/blackrose/gui/static/diagrams.js::render`; the `maxTextSize` check in
   `diagram-runtime/host.js`.) Pinned.
10. **A diagram failure is reported once per mount, and only for a repairable
    error on a complete message.** `reported` and
    `message.dataset.messageState === 'complete'` gate the call
    (`src/blackrose/gui/static/diagrams.js::mount._notifyDiagramFailure`); an
    unrepairable error never reports. Pinned by B1's repair assertions.
11. **The Mermaid cache is keyed by source only, bounded by count and bytes, and
    never stores a result over 8 MiB** (`src/blackrose/gui/static/diagrams.js::remember`).
    The page's theme and Mermaid version are fixed per page, so the key is sufficient
    (the comment at the top of `src/blackrose/gui/static/diagrams.js::render`).
12. **A UI state save advances `record.sequence` by exactly one per acknowledged
    save and never overlaps another save** (`ui-runtime/host.jsx::flush`). The backend
    enforces `sequence == current + 1`
    (`src/blackrose/storage/repository.py::Repository.save_ui_state`), so the two
    must agree. Pinned by `sm22_ui_host.mjs` 5 and 6.
13. **A refused save keeps the record dirty and retries the same `sequence + 1`**
    (`flush`'s `catch`); a refused or non-ready flush leaves `dirty` untouched
    (`flush`'s early return in `ui-runtime/host.jsx::flush`). Pinned.
14. **A submit flushes first and sends one `ui_action` with a fresh `event_id`,
    one identity, `action_id='submit'`, `action_type='continue_conversation'`**
    (`ui-runtime/host.jsx::render.host.submit`). Pinned.
15. **The submit lock is the record, not the DOM.** `submit` refuses when
    `disabled` (captured at render time) *or* `record.submitting` (its first check).
    Pinned by the double-submit regression, which failed before this guard was added.
16. **`unmount` clears the debounce and the root and forgets the block; it does
    not flush** (`ui-runtime/host.jsx::unmount`). Consequence in H3/H4; pinned.
17. **A model-authored UI document only reaches React as a JSON literal**
    (`ui-runtime/library.jsx::compile`), never as language or code; the schema hash
    must equal the artifact's (`ui-runtime/host.jsx::supported`). Pinned by
    `ui-runtime/runtime.test.mjs`.
18. **The page drops an event for another conversation**
    (`src/blackrose/gui/static/bloom.js::receive`). Pinned by the `animation_recovery`
    assertion in `sm22_bloom.cjs`.
19. **The turn lock is exactly `state.busy`, derived only from bridge status and
    reply events** (`src/blackrose/gui/static/bloom.js::setBusy`); a composer submit
    while locked sends `stop`, never `submit` (the `el.composer` `submit` listener).
    Pinned.
20. **A rigour save is single-flight and optimistic**: `state.rigorPending` blocks
    send, retry, and a second save (`setRigor`'s first check, `setBusy`,
    `renderControls`, `send` and the composer `submit` listener in
    `src/blackrose/gui/static/bloom.js`); the shown mode is
    `rigorTarget ?? state.status.citation_rigor`
    (`src/blackrose/gui/static/bloom.js::rigorMode`) and a refused save reverts it
    (`setRigor`'s `finally`). Pinned.
21. **An effort save is supersedable**: a later press replaces `effortTarget`, and
    an earlier save cannot clear or report an error over it (the `effortTarget !==
    next` check in `src/blackrose/gui/static/bloom.js::cycleEffort`). Pinned.
22. **A suggestion result is applied only if its epoch, the input value and the
    caret position all still match** (the response check in
    `src/blackrose/gui/static/bloom.js::updateCommandPopover`); every input, Escape,
    send, conversation change and browse invalidates the epoch
    (`updateCommandPopover`, `commandKey`, `send`, `on.conversations`,
    `drawSuggestions`). Pinned.
23. **At most one dialog is open, the page is inert while it is, and focus
    returns to the trigger** (`src/blackrose/gui/static/bloom.js::openDialog`,
    `src/blackrose/gui/static/bloom.js::closeDialog`). Pinned.
24. **The sidebar's persisted layout is written only from a user action and only
    in wide mode** (`src/blackrose/gui/static/bloom.js::setSidebar`,
    `src/blackrose/gui/static/bloom.js::endDrag`, the `el.resizer` `dblclick` and
    `keydown` listeners); overlay toggles never persist. Pinned.
25. **A drag flicks shut only for an inward release whose projection lands more
    than 6 rem past the minimum** (`src/blackrose/gui/static/bloom.js::endDrag`);
    otherwise the released width is clamped to `sidebarBounds` and persisted. Pinned.
26. **An earlier-page response is applied only to the transcript it was requested
    for** (the epoch check in `src/blackrose/gui/static/bloom.js::loadEarlier`).
    Pinned.
27. **Auto-scroll follows only a reader who was at the bottom** (64 px
    threshold, `src/blackrose/gui/static/bloom.js::nearBottom`); `follow` measures
    before the mutation and coalesces to one rAF
    (`src/blackrose/gui/static/bloom.js::follow`); a pinned reader is re-pinned across
    reflows (the `el.transcript` `scroll` listener and `ResizeObserver` beside
    `src/blackrose/gui/static/bloom.js::pinned`). Pinned.
28. **An image frame's loader state is `dataset.state`**, one of absent /
    `loading` / `waiting` / `ready` / `error`, and a streaming redraw preserves
    a frame only when both `imageSrc` and `imageAlt` match (the frame-reuse loops in
    `src/blackrose/gui/static/bloom.js::renderHTML`), so a load in flight survives the
    redraw. Pinned.
29. **Generated code stays in an opaque-origin frame.** Both iframes are
    `sandbox="allow-scripts"` with `referrerpolicy="no-referrer"`
    (`src/blackrose/gui/static/motion-canvas-player.js::mount`,
    `src/blackrose/gui/static/diagrams.js::renderer`), neither frame has the
    desktop bridge, and both validate `event.source` and a channel before
    acting. Pinned by `test_sm_22_frontend_runtimes.LoadOrderTests`.
30. **The page's script order is a load-time contract**: runtimes before
    `bloom.js`, `#column` before `diagrams.js`. Pinned.

## Hazards and open questions

- **H1 — `fragile` — `src/blackrose/gui/static/motion-canvas-player.js::mount.visibility`
  with the retry button's `onclick` in
  `src/blackrose/gui/static/motion-canvas-player.js::mount`.**
  The init clock is gated on *intersection*, not page visibility. A minimized
  window or a hidden document stops WebKit's RAF (the comment above
  `mount.visibility` says so) while the document is still "intersecting", so the 20 s
  timer fires and the player reports a timeout for a frame that never had a chance to
  render. Demonstrated by `sm22_players.cjs` ("hazard: the boot timer runs while the
  page cannot render"). The retry timer (armed in the retry `onclick`) is armed
  without consulting intersection at all; the observer clears it on the next
  entry/exit event (`clearTimeout` at the top of `mount.visibility`).
  Confirming in the product needs a GTK window minimize/hide during a boot.
  Fix shape: arm the timer only while `isIntersecting && !document.hidden`, and
  re-arm on `visibilitychange`.
- **H2 — `none-found` (limit) — the `ready`/`state` branch of
  `src/blackrose/gui/static/motion-canvas-player.js::mount.receive`.** The
  single-`PLAYING` invariant is enforced cooperatively at start entry points,
  not centrally. A frame that reports `state.playing === true` is believed and
  the page does not pause the other engine, so two players can report playing if
  a frame starts itself (e.g. a looping timeline resumed by the frame). Nothing
  in the frame can start playback without a command (the control `message` listener
  in `src/blackrose/gui/static/motion-canvas-frame.js`), so this is a structural
  limit, not a reachable defect. Pinned as such.
- **H3 — `fragile` — the `options.onFailure` chain in
  `src/blackrose/gui/static/motion-canvas-player.js::mount.showError` with
  `src/blackrose/gui/static/bloom.js::call`.**
  `bloom.call` swallows a bridge rejection and resolves `null`, so
  `options.onFailure` resolves `null`; `setRecovery(null)` clears
  `figure.dataset.recovery` and hides the note instead of showing
  "Automatic repair is unavailable" (the chain's `.catch` only covers a genuine
  rejection inside the page). The user sees the failure text with no repair
  status. Demonstrated by `sm22_players.cjs` ("a null result erases the recovery
  label"). Fix shape: treat a null result as `{status:'unavailable'}` in the
  player, or stop swallowing in `bloom.call` for this endpoint.
- **H4 — `documentation-gap` — the `inspect` command branch and the
  `player.onRender` subscriber in `src/blackrose/gui/static/motion-canvas-frame.js`.**
  `inspection` is only cleared when the rendered frame equals the requested
  frame. If `playback.frame` never reaches it (seek clamped to the 120 s range
  while the measured duration is shorter), `inspection` stays set and the
  `player.deactivate()` at the end of the `onRender` subscriber is skipped for the
  life of the frame, leaving the RAF loop alive in an opaque frame. Reasoned-only: the
  request is clamped to the same 120 s range the player is configured with (the
  `range: [0, 120]` passed to `new Player`), so the frame should always render.
  Confirming needs a scene whose measured duration is under the requested time by more
  than a frame.
- **H5 — `fragile` — `src/blackrose/gui/static/diagrams.js::renderer.fail` and the
  per-job timeout in `src/blackrose/gui/static/diagrams.js::render`.** One slow
  render (20 s) latches `broken` for the whole page, so every later diagram in every
  message fails until the window reloads. Deliberate (the comment above
  `frame._fail = fail` in `renderer` explains the latency/parser argument), but it
  couples one pathological diagram to the rest of the conversation. Demonstrated by
  B2/B3. A narrower fix (replace the frame and retry once) would need a policy
  decision, so none is proposed.
- **H6 — `fragile` — `src/blackrose/gui/static/diagrams.js::mount._notifyDiagramFailure`,
  `src/blackrose/gui/static/diagrams.js::repairResult`.** A terminal repair result
  is applied to **every** `figure[data-mermaid]` in the root with a visible
  status, not only the failed figure, and terminal messages are last-writer-wins
  (`_diagramRepairSettled` only suppresses the optimistic label). Two failed
  diagrams in one message would both receive the same repair text. Pinned as
  "terminal messages are last-writer-wins inside one root"; confirming user
  impact needs two failures in one reply (Python reports one repair per reply —
  see SM-08).
- **H7 — `fragile` — `src/blackrose/gui/static/diagrams.js::mount._notifyDiagramFailure`.**
  `reported` latches *before* `failed(...)` resolves. If the bridge returns `null` (a
  swallowed call), the figure never re-requests repair: `settled()` sees `reported` and
  stops. Reasoned-only; confirmation needs a bridge failure on `diagram_failed`.
- **H8 — `defect` (fixed) — `ui-runtime/host.jsx::render.host.submit`.** The submit
  lock was only the React `disabled` prop, and `render` is not synchronous
  (`flushSync` is used only by the probe `ui-runtime/host.jsx::sync`). Two submits
  inside one commit both read the captured `disabled === false` and each called
  `ui_action`; Python refuses the second (the entry guard of
  `src/blackrose/gui/session.py::Session.ui_action` returns "Wait for the current
  reply to finish", or the DB refuses it with "Duplicate or in-flight UI submission",
  SM-06 H1) and the page shows a spurious error. Demonstrated: the regression
  assertion failed before the guard was added and passes after
  (`sm22_ui_host.mjs` 8).
- **H9 — `fragile` — `ui-runtime/host.jsx::flush`, `ui-runtime/host.jsx::update`.**
  Cross-references SM-05 H5. A save that the backend committed but whose reply was
  lost leaves `record.sequence` one behind; every retry posts the same stale number and
  is refused (the sequence check in
  `src/blackrose/storage/repository.py::Repository.save_ui_state`), and `update()`
  never adopts `block.state_sequence`. Only a remount recovers. Demonstrated end to end
  against a sequence-checking fake backend (`sm22_ui_host.mjs` 7). The repair
  belongs with SM-05's proposal (adopt a larger incoming `state_sequence` in
  `update`), so no source change is proposed here.
- **H10 — `fragile` — `ui-runtime/host.jsx::unmount`, `ui-runtime/host.jsx::unmountAll`
  with `src/blackrose/gui/static/bloom.js::on.transcript`.**
  `unmount`/`unmountAll` clear the debounce without flushing, and
  `bloom.on.transcript` calls `unmountAll()` on every redraw. A dirty widget
  torn down by a redraw loses the pending edit. Demonstrated ("unmount clears
  the pending debounce without flushing"). A fire-and-forget flush on unmount
  would *cause* H9 (a save landing after the redraw's snapshot), so the fix must
  come with H9's sequence adoption: keep the record across a redraw keyed by
  `block_id`, or make the redraw payload authoritative for `state_sequence`.
- **H11 — `documentation-gap` — the `disabled` constant in `ui-runtime/host.jsx::render`.**
  `effectsDisabled` is `false || … || record.submitting`, so it is `undefined` until
  the first submit and React treats it as "not disabled". Any consumer that compares
  it to `false` or serialises it is wrong. Pinned by `assert.ok(!host.effectsDisabled)`.
- **H12 — `documentation-gap` — the defaults seeding in `ui-runtime/host.jsx::update`.**
  `stateInitialized` becomes true as soon as a document exists, so defaults added by a
  later (final) document are never merged for a widget that streamed with a document.
  Reasoned-only; SM-05 H4 covers the status half of the same window.
- **H13 — `fragile` — `src/blackrose/gui/static/bloom.js::send`, the `el.composer`
  `submit` listener and `src/blackrose/gui/static/bloom.js::actions.send` with
  `keymap.json` (`ctrl+enter`, `alt+enter`, `ctrl+s` → `send`).** Those bindings map
  to `send()` directly while the composer's submit event routes `busy` to `stop()`.
  The keyboard path therefore calls `submit` during generation; Python refuses with a
  bell and `{clear:false}` (the busy guard in
  `src/blackrose/gui/session.py::Session.submit`) and the page restores the draft (the
  refusal branch of `send`), so nothing is lost, but the two entry points disagree.
  Demonstrated.
- **H14 — `fragile` — `src/blackrose/gui/static/bloom.js::cycleEffort`.** After a
  successful effort save the page clears `effortTarget` and re-reads
  `state.status.reasoning_effort` instead of the `reasoning_effort` the bridge returns
  (`src/blackrose/gui/session.py::Session.set_reasoning_effort`). The label shows the
  previous level until the pushed `status` event arrives — a one-event flicker.
  Demonstrated ("the label falls back to the last pushed status").
- **H15 — `fragile` — the debounce callback in
  `src/blackrose/gui/static/bloom.js::updateCommandPopover`.** The debounce result is
  dropped if the caret moved for an unrelated reason (focus change, programmatic edit,
  a click that repositions the caret) even though the value still matches; nothing
  retries until the next keystroke. Reasoned-only.
- **H16 — `fragile` — `src/blackrose/gui/static/bloom.js::on.reply_start`.**
  `reply_start` for a key already in `state.replies` overwrites the mapping without
  removing the earlier article, leaving an `is-streaming` bubble that no later
  `reply_update`/`reply_end` reaches. No emitter does this today (every `reply_end`
  deletes the key and `transcript`/`snapshot` clear the map first). Reasoned-only.
- **H17 — `fragile` — `src/blackrose/gui/static/bloom.js::endDrag` with
  `src/blackrose/gui/static/bloom.js::fitPanes`.** `fitPanes` returns early while
  `drag` is set *after* it may have flipped `overlayMode` and cleared `overlayOpen`.
  An `endDrag` that follows a window resize across the breakpoint then persists
  `layout.collapsed` and a width clamped with overlay bounds. Reasoned-only;
  confirming needs a resize during a drag.
- **H18 — `fragile` — `src/blackrose/gui/static/bloom.js::openEditBackups`,
  `src/blackrose/gui/static/bloom.js::openWorkspace`.** Nested-dialog
  continuations call `openDialog` (via `openEditBackups`) after an `await`; a
  dialog the user opened during that wait is silently replaced by the
  continuation, because `openDialog` closes the current dialog with value
  `false` (its `state.dialog` check) and its `onClose` no-ops. Reasoned-only.
- **H19 — `fragile` — `src/blackrose/gui/static/bloom.js::loadImage`,
  `src/blackrose/gui/static/bloom.js::requestStoredImage`.** The image loader has no
  timeout: a frame whose `img` never fires `load`/`error` (or whose preview
  bridge never resolves) stays `loading` with `aria-busy="true"` for the life of
  the frame, and `loadImage` refuses to retry while `dataset.state ===
  'loading'` (its first check). A disconnected frame returns early (the `isConnected`
  check in `requestStoredImage`) and leaves the same flags on a discarded node.
  Reasoned-only; needs a stalled network/bridge.
- **H20 — `none-found` — the rest of the audited machines.** The dialog
  presence machine (`openDialog`/`closeDialog`/`Presence`), the popover epoch,
  transcript paging and follow, and the sidebar springs are internally
  consistent: closing during the fade is handled by clearing `is-closing` on open
  (`src/blackrose/gui/static/bloom.js::openDialog`), `follow` measures before
  mutating and coalesces (`src/blackrose/gui/static/bloom.js::follow`), and the
  drag release is idempotent per pointer id
  (`src/blackrose/gui/static/bloom.js::endDrag`). Pinned by the corresponding
  `sm22_bloom.cjs` sections.

## Formalization decisions

One source change:

- **`ui-runtime/host.jsx::render.host.submit` — the submit lock now checks
  `record.submitting` as well as the captured `disabled`.** This is the smallest
  change that makes the `IDLE -> SUBMITTING` transition's guard real instead of
  an asynchronously-committed DOM attribute (H8). It changes no bridge contract:
  the payload, identity and `event_id` shape are untouched, and a refused second
  submit now produces no call instead of a spurious error. The regression is
  `sm22_ui_host.mjs` section 8 (two synchronous submits → one action), which
  fails without the guard. The generated bundle
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

Run from the repository root, after
[checkout setup](../../CONTRIBUTING.md#set-up-a-checkout). The suite for this machine
runs the three node harnesses and the load-order checks:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_sm_22_frontend_runtimes.py -v
```

The three harnesses on their own (`node`, offline, no display):

```sh
node tests/sm22_players.cjs
node tests/sm22_ui_host.mjs
node tests/sm22_bloom.cjs
```

Pre-existing focused tests for the same modules:

```sh
.venv/bin/python scripts/check_unit_tests.py -p test_animations.py
.venv/bin/python scripts/check_unit_tests.py -p test_motion_canvas.py
.venv/bin/python scripts/check_unit_tests.py -p test_diagrams.py
.venv/bin/python scripts/check_unit_tests.py -p test_diagram_repairs.py
.venv/bin/python scripts/check_unit_tests.py -p test_interactive_ui_evaluation.py
.venv/bin/python scripts/check_unit_tests.py -p test_interactive_ui_fidelity.py
.venv/bin/python scripts/check_unit_tests.py -p test_conversation_paging.py
.venv/bin/python scripts/check_unit_tests.py -p test_chat_navigation.py
.venv/bin/python scripts/check_unit_tests.py -p test_effort.py
.venv/bin/python scripts/check_unit_tests.py -p test_composer_drafts.py
```

Static and bundle checks:

```sh
node --check src/blackrose/gui/static/bloom.js
node --check src/blackrose/gui/static/diagrams.js
node --check src/blackrose/gui/static/animations.js
node --check src/blackrose/gui/static/motion-canvas-player.js
node --check src/blackrose/gui/static/motion-canvas-frame.js
npm test --prefix ui-runtime
.venv/bin/python -m ruff check tests/test_sm_22_frontend_runtimes.py
```

The full offline gate is `.venv/bin/python scripts/check_health.py`.

To reproduce the H8 regression, revert the `record.submitting` guard in
`ui-runtime/host.jsx::render.host.submit` and rerun `node tests/sm22_ui_host.mjs`;
the "one action for two rapid submits" assertion fails.

The native and browser probes need a display: `scripts/check_interactive_ui.cjs`,
`scripts/check_diagrams.cjs`, `scripts/check_effort.cjs`,
`scripts/check_conversation_paging.cjs`, `scripts/check_history_paging.cjs`,
`scripts/check_chat_navigation.cjs`, `scripts/check_reading_room.cjs`, and the
`scripts/check_gui_*.py --gui` probes, which need `xvfb-run`/GTK/WebKit. The three
node harnesses are the offline replacement for those probes' state-machine
assertions, not for their rendering/visual assertions.
