/* SM-22: drive the real animation players, the Motion Canvas control bridge and
 * the Mermaid renderer adapters as state machines.
 *
 * Run: node tests/sm22_players.cjs
 *
 * The harness supplies the browser surfaces the shipped scripts use (DOM nodes,
 * IntersectionObserver, MutationObserver, matchMedia, timers) and speaks the
 * postMessage protocols itself, so every transition is driven without a browser.
 */
'use strict';

const assert = require('node:assert/strict');
const {Element, document, clock, timers, advance, settle} = require('./sm22_dom.cjs');

// --- browser surface ---------------------------------------------------------------
const windowListeners = Object.create(null);
globalThis.window = globalThis;
globalThis.document = document;
globalThis.addEventListener = (type, handler) => { (windowListeners[type] ||= []).push(handler); };
globalThis.removeEventListener = (type, handler) => {
  const list = windowListeners[type] || [];
  windowListeners[type] = list.filter((candidate) => candidate !== handler);
};
function fireWindow(type, event) {
  for (const handler of [...(windowListeners[type] || [])]) handler(event);
}
const media = [];
globalThis.matchMedia = (query) => {
  const entry = {
    query, matches: false, listeners: [],
    addEventListener(type, handler) { this.listeners.push({type, handler}); },
    removeEventListener(type, handler) { this.listeners = this.listeners.filter((entry) => entry.handler !== handler); },
  };
  media.push(entry);
  return entry;
};
globalThis.getComputedStyle = () => ({getPropertyValue: () => '', fontFamily: 'system-ui'});
globalThis.devicePixelRatio = 1;
globalThis.innerWidth = 960;
globalThis.innerHeight = 800;
globalThis.performance = {now: () => clock.now};
globalThis.setTimeout = timers.setTimeout;
globalThis.clearTimeout = timers.clearTimeout;
globalThis.setInterval = timers.setInterval;
globalThis.clearInterval = timers.clearInterval;
globalThis.requestAnimationFrame = timers.requestAnimationFrame;
globalThis.cancelAnimationFrame = timers.cancelAnimationFrame;

const intersectionObservers = [];
class IntersectionObserver {
  constructor(callback) { this.callback = callback; this.targets = []; this.disconnected = false; intersectionObservers.push(this); }
  observe(target) { this.targets.push(target); }
  unobserve(target) { this.targets = this.targets.filter((candidate) => candidate !== target); }
  disconnect() { this.disconnected = true; this.targets = []; }
}
globalThis.IntersectionObserver = IntersectionObserver;
function intersect(target, isIntersecting) {
  for (const observer of intersectionObservers) {
    if (!observer.disconnected && observer.targets.includes(target)) {
      observer.callback([{target, isIntersecting}], observer);
    }
  }
}

const mutationObservers = [];
class MutationObserver {
  constructor(callback) { this.callback = callback; this.observing = false; mutationObservers.push(this); }
  observe() { this.observing = true; }
  disconnect() { this.observing = false; }
}
globalThis.MutationObserver = MutationObserver;
function mutateDom() {
  for (const observer of mutationObservers) if (observer.observing) observer.callback([{removedNodes: [{}]}], observer);
}

// --- load the shipped scripts ------------------------------------------------------
require('../src/blackrose/gui/static/animations.js');
require('../src/blackrose/gui/static/motion-canvas-player.js');
const diagramsPath = require.resolve('../src/blackrose/gui/static/diagrams.js');
require(diagramsPath);
const Animations = globalThis.BlackroseAnimations;
const Motion = globalThis.BlackroseMotionPlayer;

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) { const found = find(child, predicate); if (found) return found; }
  return null;
}
const byClass = (root, name) => find(root, (node) => node._classes && node._classes.has(name));
const frameIn = (host) => find(host, (node) => node.tagName === 'IFRAME');
function attach(host) { const column = document.getElementById('column'); column.appendChild(host); return host; }

const builtinScene = (overrides = {}) => ({
  version: 1, title: 'Built-in', description: 'A dot moves.', duration: 2, loop: false,
  nodes: [{id: 'n', kind: 'dot', x: 10, y: 10}],
  tracks: [{node: 'n', property: 'x', keyframes: [{time: 0, value: 10}, {time: 2, value: 900}]}],
  ...overrides,
});
const motionScene = (overrides = {}) => ({...builtinScene({title: 'Motion', engine: 'motion-canvas'}), ...overrides});

const commands = (frame) => frame.posts.map((post) => post.command).filter(Boolean);

(async () => {
// ===================================================================================
// A. Players: one PLAYING across both engines
// ===================================================================================
{
  const builtinHost = attach(new Element('div'));
  const motionHost = attach(new Element('div'));
  const builtin = Animations.mount(builtinHost, builtinScene());
  builtin.playButton = byClass(builtinHost, 'animation-play');
  const failures = [];
  const motion = Animations.mount(motionHost, motionScene(), null, 'DOC', {
    attachmentId: 11, recovery: null, onFailure: (message) => { failures.push(message); },
  });
  const frame = frameIn(motionHost);
  assert.ok(frame, 'the motion engine mounts an iframe');
  assert.equal(frame.getAttribute('sandbox'), 'allow-scripts', 'generated code stays sandboxed');
  assert.equal(frame.srcdoc, undefined, 'the shared host page is loaded by src');
  assert.equal(frame.src, 'motion-canvas-host.html');

  // The frame handshake: host-ready -> initialize(document), ready -> controls live.
  fireWindow('message', {source: frame.contentWindow, data: {channel: 'blackrose-motion', type: 'host-ready'}});
  assert.deepEqual(frame.posts.at(-1), {channel: 'blackrose-motion-control', command: 'initialize', value: 'DOC'});
  assert.equal(motion.playing, false);
  fireWindow('message', {source: frame.contentWindow, data: {channel: 'blackrose-motion', type: 'ready', duration: 4}});
  const figure = find(motionHost, (node) => node._classes?.has('animation-player'));
  assert.equal(figure.dataset.ready, 'true');
  assert.equal(byClass(motionHost, 'animation-play').disabled, false, 'controls enable on ready');

  // Built-in plays, then the motion player starts: exactly one remains PLAYING.
  builtin.playButton.dispatch('click');
  assert.equal(builtin.playing, true);
  byClass(motionHost, 'animation-play').dispatch('click');
  assert.equal(motion.playing, true);
  assert.equal(builtin.playing, false, 'starting the other engine pauses the first');
  assert.equal(commands(frame).includes('play'), true);
  assert.equal([builtin.playing, motion.playing].filter(Boolean).length, 1);

  // ...and the other direction.
  builtin.playButton.dispatch('click');
  assert.equal(builtin.playing, true);
  assert.equal(motion.playing, false);
  assert.equal(commands(frame).at(-1), 'pause', 'the motion frame is told to pause');
  assert.equal([builtin.playing, motion.playing].filter(Boolean).length, 1);

  // The frame reports playback state; the control follows it, not just its own hope.
  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-motion', type: 'state', time: 1.5, playing: true, duration: 4}});
  assert.equal(motion.playing, true, 'a state message is authoritative');
  assert.equal(builtin.playing, true, 'nothing pauses the built-in player on a frame report');
  // This is the documented limit of the invariant: it is enforced at start entry
  // points, not by reconciling an unsolicited frame report.
  assert.equal([builtin.playing, motion.playing].filter(Boolean).length, 2);

  // An error from the frame is terminal for that mount and reports once.
  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-motion', type: 'error', message: 'scene exploded'}});
  assert.equal(figure.dataset.error, 'scene exploded');
  assert.equal(figure.dataset.ready, undefined);
  assert.equal(frame.srcdoc, '', 'a failed scene is unloaded');
  assert.equal(motion.playing, false);
  assert.equal(figure.dataset.recovery, 'queued');
  await settle();
  assert.deepEqual(failures, ['scene exploded'], 'one failure report per mount');

  // The fail latch: later frame messages and duplicate errors change nothing.
  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-motion', type: 'error', message: 'second'}});
  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-motion', type: 'ready', duration: 2}});
  await settle();
  assert.deepEqual(failures, ['scene exploded']);
  assert.equal(figure.dataset.error, 'scene exploded');

  // The recovery label never downgrades a settled status to queued.
  Motion.recovery(11, {status: 'replaced'});
  assert.equal(figure.dataset.recovery, 'replaced');
  Motion.recovery(11, {status: 'queued'});
  assert.equal(figure.dataset.recovery, 'replaced', 'a late queued event cannot overwrite a later one');
  Motion.recovery(99, {status: 'stopped'});
  assert.equal(figure.dataset.recovery, 'replaced', 'recovery is routed by attachment id');
  assert.equal(failures.length, 1);
}

// ===================================================================================
// A2. Boot timeout is visibility-gated; retry is not
// ===================================================================================
{
  const host = attach(new Element('div'));
  const failures = [];
  const options = {attachmentId: 21, recovery: null, onFailure: async (message) => {
    failures.push(message);
    return {status: 'repairing', attempt: 1};
  }};
  const player = Motion.mount(host, motionScene(), null, 'DOC', true, options);
  const figure = find(host, (node) => node._classes?.has('animation-player'));
  const frame = frameIn(host);
  assert.equal(frame.srcdoc, 'DOC', 'a standalone player embeds its own document');

  // Offscreen frames never count initialization time (WebKit suspends RAF there).
  advance(30000);
  assert.equal(figure.dataset.error, undefined, 'no timeout while offscreen');
  intersect(frame, true);
  advance(19999);
  assert.equal(figure.dataset.error, undefined);
  intersect(frame, false);
  advance(30000);
  assert.equal(figure.dataset.error, undefined, 'leaving the viewport clears the timer');
  intersect(frame, true);
  advance(20001);
  assert.equal(figure.dataset.error, 'Animation initialization timed out. Ask for a revision.');
  assert.equal(player.playing, false);
  assert.equal(figure.dataset.recovery, 'queued', 'the queued label is shown while the call runs');
  await settle();
  assert.equal(failures.length, 1);
  assert.equal(options.recovery.status, 'repairing', 'the bridge result replaces the optimistic label');
  assert.match(byClass(host, 'animation-recovery').textContent, /Repairing animation \(1\/2\)/);

  // Retry resets the mount and arms a fresh 20 s timer...
  const retry = byClass(host, 'animation-retry');
  assert.equal(retry.hidden, false, 'the retry control is revealed on failure');
  retry.dispatch('click');
  assert.equal(figure.dataset.error, undefined);
  assert.equal(frame.srcdoc, 'DOC', 'retry reloads the document');
  advance(19999);
  assert.equal(figure.dataset.error, undefined);
  // Leaving the viewport clears the retry timer as well; returning re-arms it.
  intersect(frame, false);
  advance(2);
  assert.equal(figure.dataset.error, undefined, 'scrolling away cancels the retry timer');
  intersect(frame, true);
  advance(19999);
  assert.equal(figure.dataset.error, undefined);

  // The gate is intersection, not page visibility: a hidden document stops RAF
  // but keeps the clock running, so booting can "time out" without a chance to render.
  document.hidden = true;
  document.dispatch('visibilitychange');
  advance(2);
  assert.equal(figure.dataset.error, 'Animation initialization timed out. Ask for a revision.',
    'hazard: the boot timer runs while the page cannot render');
  document.hidden = false;
  document.dispatch('visibilitychange');

  // A bridge call that resolves with null (call() swallows errors) clears the
  // label instead of showing the unavailable message.
  const quiet = attach(new Element('div'));
  const quietOptions = {attachmentId: 22, recovery: null, onFailure: async () => null};
  Motion.mount(quiet, motionScene(), null, 'DOC', true, quietOptions);
  const quietFrame = frameIn(quiet);
  const quietFigure = find(quiet, (node) => node._classes?.has('animation-player'));
  fireWindow('message', {source: quietFrame.contentWindow, data: {
    channel: 'blackrose-motion', type: 'error', message: 'nope'}});
  await settle();
  await settle();
  assert.equal(quietFigure.dataset.recovery, '', 'a null result erases the recovery label (hazard)');
  assert.equal(byClass(quiet, 'animation-recovery').hidden, true);
}

// ===================================================================================
// A3. Dispose removes the player, its listener and its frame execution
// ===================================================================================
{
  const host = attach(new Element('div'));
  const player = Motion.mount(host, motionScene(), null, 'DOC', true, {attachmentId: 31});
  const figure = find(host, (node) => node._classes?.has('animation-player'));
  const frame = frameIn(host);
  const before = frame.posts.length;
  figure.remove();
  mutateDom();
  assert.equal(frame.posts.length, before + 1, 'disposal pauses the frame once');
  const disposed = frame.posts.length;
  Motion.pauseAll();
  assert.equal(frame.posts.length, disposed, 'a disposed player is no longer in pauseAll');
  assert.equal(frame.srcdoc, '', 'disposal unloads the frame');
  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-motion', type: 'ready', duration: 2}});
  assert.equal(figure.dataset.ready, undefined, 'the message listener is gone');
  assert.equal(player.playing, false);
}

// ===================================================================================
// B. Mermaid: one renderer per page, serialised jobs, latched failure
// ===================================================================================
function mermaidFigure(source, {messageId = 1, messageState = 'complete'} = {}) {
  const column = document.getElementById('column');
  const message = new Element('article');
  message.className = 'msg';
  message.dataset.messageId = messageId;
  if (messageState !== null) message.dataset.messageState = messageState;
  const figure = new Element('figure');
  figure.dataset.mermaid = '';
  const pre = new Element('pre');
  const code = new Element('code');
  code.textContent = source;
  pre.appendChild(code);
  const tools = new Element('div');
  tools.className = 'code-tools';
  figure.appendChild(pre);
  figure.appendChild(tools);
  message.appendChild(figure);
  column.appendChild(message);
  return figure;
}
function mermaidRoot() { return document.getElementById('column'); }
function reloadDiagrams() {
  delete require.cache[diagramsPath];
  require(diagramsPath);
  return globalThis.BlackroseDiagrams;
}

{
  const column = mermaidRoot();
  column.replaceChildren();
  const reports = [];
  const callbacks = {
    expand() {},
    changed(update) { update(); },
    failed(figure, source, error) { reports.push({source, error}); return Promise.resolve({status: 'queued'}); },
  };
  const first = mermaidFigure('flowchart LR\n A-->B');
  const second = mermaidFigure('flowchart LR\n B-->C');
  globalThis.BlackroseDiagrams.mount(column, callbacks);
  assert.equal(first._diagramMounted, true);
  const status = byClass(first, 'diagram-status');
  assert.equal(status.hidden, false);
  assert.equal(byClass(first, 'diagram-control').disabled, true, 'controls stay off until the image renders');

  intersect(first, true);
  intersect(second, true);
  await settle();
  // One singleton iframe, created before any job can be posted.
  const iframes = () => document.body.children.filter((node) => node.tagName === 'IFRAME');
  assert.equal(iframes().length, 1);
  const frame = iframes()[0];
  assert.equal(frame.src, 'mermaid-host.html');
  assert.equal(frame.posts.length, 0, 'no render is posted before the renderer says ready');

  fireWindow('message', {source: frame.contentWindow, data: {channel: 'blackrose-mermaid', type: 'ready'}});
  await settle();
  assert.equal(frame.posts.length, 1, 'jobs are serialised: one render in flight');
  const firstJob = frame.posts[0];
  assert.equal(firstJob.type, 'render');
  assert.equal(firstJob.source, 'flowchart LR\n A-->B');
  assert.equal(Number.isSafeInteger(firstJob.id), true);

  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-mermaid', type: 'result', id: firstJob.id,
    svg: '<svg/>', label: 'A to B'}});
  await settle();
  assert.equal(frame.posts.length, 2, 'the next queued job starts after the first result');
  assert.equal(frame.posts[1].source, 'flowchart LR\n B-->C');
  assert.equal(iframes()[0], frame, 'still exactly one renderer');

  let image = find(first, (node) => node.tagName === 'IMG');
  assert.ok(image, 'the resolved figure gets an image');
  assert.equal(image.alt, 'A to B');
  assert.match(image.src, /^data:image\/svg\+xml;charset=utf-8,/);
  assert.equal(byClass(first, 'diagram-control').disabled, true, 'controls wait for the image load');
  image.dispatch('load');
  assert.equal(byClass(first, 'diagram-control').disabled, false);
  assert.equal(status.hidden, true);
  assert.equal(preHidden(first), true);

  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-mermaid', type: 'result', id: frame.posts[1].id,
    svg: '<svg/>', label: 'B to C'}});
  await settle();
  find(second, (node) => node.tagName === 'IMG').dispatch('load');
  assert.equal(byClass(second, 'diagram-control').disabled, false);

  // Cache: the same source mounts again and renders without touching the frame.
  const third = mermaidFigure('flowchart LR\n A-->B');
  globalThis.BlackroseDiagrams.mount(column, callbacks);
  intersect(third, true);
  await settle();
  assert.equal(frame.posts.length, 2, 'a cached source posts no render');
  find(third, (node) => node.tagName === 'IMG').dispatch('load');
  assert.equal(byClass(third, 'diagram-control').disabled, false);
  assert.deepEqual(reports, []);

  // A rejected result carrying repairable=true is reported exactly once.
  const fourth = mermaidFigure('flowchart LR\n C-->D', {messageId: 7});
  globalThis.BlackroseDiagrams.mount(column, callbacks);
  intersect(fourth, true);
  await settle();
  assert.equal(frame.posts.length, 3);
  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-mermaid', type: 'result', id: frame.posts[2].id,
    error: 'Parse error', repairable: true}});
  await settle();
  assert.equal(reports.length, 1);
  assert.deepEqual(reports[0], {source: 'flowchart LR\n C-->D', error: 'Parse error'});
  assert.match(byClass(fourth, 'diagram-status').textContent, /Repairing diagram/,
    'a queued repair replaces the transient error text');
  globalThis.BlackroseDiagrams.settled(fourth.parentElement);
  await settle();
  assert.equal(reports.length, 1, 'the failure is reported once per mount');

  // A non-repairable error, and a message that is still streaming, never report.
  const fifth = mermaidFigure('flowchart LR\n D-->E', {messageId: 8, messageState: 'streaming'});
  globalThis.BlackroseDiagrams.mount(column, callbacks);
  intersect(fifth, true);
  await settle();
  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-mermaid', type: 'result', id: frame.posts.at(-1).id,
    error: 'Too many edges', repairable: false}});
  await settle();
  globalThis.BlackroseDiagrams.settled(fifth.parentElement);
  await settle();
  assert.equal(reports.length, 1, 'a non-repairable failure is not reported');

  // repairResult only rewrites a status that is still visible, for every figure
  // in the root it is given (the same message).
  const sixth = mermaidFigure('flowchart LR\n E-->F', {messageId: 8, messageState: 'streaming'});
  globalThis.BlackroseDiagrams.mount(column, callbacks);
  intersect(sixth, true);
  await settle();
  fireWindow('message', {source: frame.contentWindow, data: {
    channel: 'blackrose-mermaid', type: 'result', id: frame.posts.at(-1).id,
    error: 'Parse error again', repairable: true}});
  await settle();
  globalThis.BlackroseDiagrams.repairResult(sixth.parentElement, {status: 'failed'});
  assert.match(byClass(sixth, 'diagram-status').textContent, /Automatic repair did not fix this diagram/);
  assert.equal(sixth._diagramRepairSettled, true);
  globalThis.BlackroseDiagrams.repairResult(sixth.parentElement, {status: 'queued'});
  assert.match(byClass(sixth, 'diagram-status').textContent, /Automatic repair did not fix this diagram/,
    'a queued status never replaces a terminal message');
  globalThis.BlackroseDiagrams.repairResult(sixth.parentElement, {status: 'stopped'});
  assert.match(byClass(sixth, 'diagram-status').textContent, /Diagram repair stopped/,
    'terminal messages are last-writer-wins inside one root');
  column.replaceChildren();
  mutateDom();
}

function preHidden(figure) {
  const pre = find(figure, (node) => node.tagName === 'PRE' && node.parentNode === figure);
  return pre.hidden;
}

// ===================================================================================
// B2. Boot timeout latches the singleton; a second figure gets the same error
// ===================================================================================
{
  const column = mermaidRoot();
  column.replaceChildren();
  const diagram = reloadDiagrams();
  const iframesBefore = document.body.children.filter((node) => node.tagName === 'IFRAME').length;
  const first = mermaidFigure('graph TD\n A-->B');
  diagram.mount(column, {expand() {}, changed(update) { update(); }, failed: async () => ({status: 'queued'})});
  intersect(first, true);
  await settle();
  const frames = document.body.children.filter((node) => node.tagName === 'IFRAME');
  assert.equal(frames.length, iframesBefore + 1);
  advance(20001);
  await settle();
  assert.match(byClass(first, 'diagram-status').textContent, /Diagram renderer unavailable/);
  assert.equal(document.body.children.includes(frames.at(-1)), false, 'a failed renderer is removed');

  const second = mermaidFigure('graph TD\n B-->C');
  diagram.mount(column, {expand() {}, changed(update) { update(); }, failed: async () => ({status: 'queued'})});
  intersect(second, true);
  await settle();
  assert.match(byClass(second, 'diagram-status').textContent, /Diagram renderer unavailable/,
    'the failure is latched for the page, not retried per figure');
  assert.equal(document.body.children.filter((node) => node.tagName === 'IFRAME').length, iframesBefore,
    'no replacement renderer is created');
}

// ===================================================================================
// B3. A render timeout, an oversized source and a disconnected figure
// ===================================================================================
{
  const column = mermaidRoot();
  column.replaceChildren();
  const diagram = reloadDiagrams();
  const iframesBefore = document.body.children.filter((node) => node.tagName === 'IFRAME').length;
  const tooLong = mermaidFigure('x'.repeat(20001));
  const small = mermaidFigure('graph LR\n A-->B');
  diagram.mount(column, {expand() {}, changed(update) { update(); }, failed: async () => ({status: 'queued'})});
  intersect(tooLong, true);
  await settle();
  assert.equal(document.body.children.filter((node) => node.tagName === 'IFRAME').length, iframesBefore,
    'an oversized source is refused before the renderer boots');
  assert.match(byClass(tooLong, 'diagram-status').textContent, /Diagram exceeds 20,000 characters/);

  intersect(small, true);
  await settle();
  const frame = document.body.children.filter((node) => node.tagName === 'IFRAME').at(-1);
  fireWindow('message', {source: frame.contentWindow, data: {channel: 'blackrose-mermaid', type: 'ready'}});
  await settle();
  assert.equal(frame.posts.length, 1);
  advance(20001);
  await settle();
  assert.match(byClass(small, 'diagram-status').textContent, /Diagram rendering timed out/);
  assert.equal(document.body.children.includes(frame), false, 'a timed-out renderer is removed');

  // A figure removed while its job waits is never sent to the renderer.
  column.replaceChildren();
  const fresh = reloadDiagrams();
  const before = document.body.children.filter((node) => node.tagName === 'IFRAME').length;
  const kept = mermaidFigure('graph LR\n C-->D');
  const gone = mermaidFigure('graph LR\n D-->E');
  fresh.mount(column, {expand() {}, changed(update) { update(); }, failed: async () => ({status: 'queued'})});
  intersect(kept, true);
  intersect(gone, true);
  await settle();
  assert.equal(document.body.children.filter((node) => node.tagName === 'IFRAME').length, before + 1,
    'the renderer boots once for the queue');
  const late = document.body.children.filter((node) => node.tagName === 'IFRAME').at(-1);
  gone.remove();
  fireWindow('message', {source: late.contentWindow, data: {channel: 'blackrose-mermaid', type: 'ready'}});
  await settle();
  assert.equal(late.posts.length, 1, 'only the connected figure reaches the renderer');
  assert.equal(late.posts[0].source, 'graph LR\n C-->D');
  fireWindow('message', {source: late.contentWindow, data: {
    channel: 'blackrose-mermaid', type: 'result', id: late.posts[0].id, svg: '<svg/>', label: 'C'}});
  await settle();
  assert.equal(late.posts.length, 1, 'the disconnected figure is skipped when its turn comes');
  assert.equal(find(gone, (node) => node.tagName === 'IMG'), null);

  // A figure removed before its turn never boots the renderer at all.
  column.replaceChildren();
  const empty = reloadDiagrams();
  const beforeEmpty = document.body.children.filter((node) => node.tagName === 'IFRAME').length;
  const orphan = mermaidFigure('graph LR\n E-->F');
  empty.mount(column, {expand() {}, changed(update) { update(); }, failed: async () => ({status: 'queued'})});
  intersect(orphan, true);
  orphan.remove();
  await settle();
  assert.equal(document.body.children.filter((node) => node.tagName === 'IFRAME').length, beforeEmpty,
    'a queued job whose figure is gone does not boot the renderer');
}

console.log('Animation player and Mermaid state machines passed.');
})().catch((error) => { console.error(error); process.exit(1); });
