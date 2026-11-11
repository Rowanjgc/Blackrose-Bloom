/* SM-22: drive the real main page controller (src/blackrose/gui/static/bloom.js)
 * as the set of state machines the bead names.
 *
 * Run: node tests/sm22_bloom.cjs
 *
 * bloom.js is one closure with no exports beyond bloom.receive(), so the harness
 * boots it against a minimal DOM (tests/sm22_dom.cjs), a fake pywebview bridge
 * and a deterministic clock, then drives the documented inputs (bridge events,
 * pointer/keyboard/input events, timers) and observes what the module exposes:
 * bridge calls, element state, focus and scroll position.
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
  windowListeners[type] = (windowListeners[type] || []).filter((candidate) => candidate !== handler);
};
globalThis.getComputedStyle = () => ({getPropertyValue: () => '', fontFamily: 'system-ui', fontSize: '16px'});
globalThis.devicePixelRatio = 1;
globalThis.innerWidth = 1200;
globalThis.innerHeight = 900;
globalThis.performance = {now: () => clock.now};
globalThis.getSelection = () => ({toString: () => '', anchorNode: null, focusNode: null});
globalThis.setTimeout = timers.setTimeout;
globalThis.clearTimeout = timers.clearTimeout;
globalThis.setInterval = timers.setInterval;
globalThis.clearInterval = timers.clearInterval;
globalThis.requestAnimationFrame = timers.requestAnimationFrame;
globalThis.cancelAnimationFrame = timers.cancelAnimationFrame;

const reducedMotion = {matches: true, listeners: []};
reducedMotion.addEventListener = (type, handler) => reducedMotion.listeners.push(handler);
reducedMotion.removeEventListener = (type, handler) => {
  reducedMotion.listeners = reducedMotion.listeners.filter((entry) => entry !== handler);
};
globalThis.matchMedia = () => reducedMotion;

const resizeObservers = [];
class ResizeObserver {
  constructor(callback) { this.callback = callback; this.targets = []; resizeObservers.push(this); }
  observe(target) { if (target) this.targets.push(target); }
  unobserve(target) { this.targets = this.targets.filter((candidate) => candidate !== target); }
  disconnect() { this.targets = []; }
}
globalThis.ResizeObserver = ResizeObserver;
function resize(target) {
  for (const observer of resizeObservers) {
    if (observer.targets.includes(target)) {
      observer.callback([{target, contentRect: {width: target._rect.width, height: target._rect.height}}], observer);
    }
  }
}

const intersectionObservers = [];
class IntersectionObserver {
  constructor(callback, options = {}) {
    this.callback = callback; this.options = options; this.targets = []; intersectionObservers.push(this);
  }
  observe(target) { this.targets.push(target); }
  unobserve(target) { this.targets = this.targets.filter((candidate) => candidate !== target); }
  disconnect() { this.targets = []; }
}
globalThis.IntersectionObserver = IntersectionObserver;
function intersect(target, isIntersecting) {
  for (const observer of intersectionObservers) {
    if (observer.targets.includes(target)) observer.callback([{target, isIntersecting}], observer);
  }
}
class MutationObserver {
  constructor(callback) { this.callback = callback; }
  observe() {}
  disconnect() {}
}
globalThis.MutationObserver = MutationObserver;

// --- the fake native bridge ---------------------------------------------------------
const calls = [];
const responses = Object.create(null);
const bridge = new Proxy({}, {
  get(_target, name) {
    if (name === 'announce') return undefined;   // keeps forwardLiveRegions inert
    if (typeof name !== 'string') return undefined;
    return (...args) => {
      calls.push({name, args});
      const handler = responses[name];
      const result = Promise.resolve(typeof handler === 'function' ? handler(...args) : handler ?? null);
      
      return result;
    };
  },
});
const callsTo = (name) => calls.filter((call) => call.name === name);
const lastCall = (name) => callsTo(name).at(-1);
const resetCalls = () => { calls.length = 0; };
globalThis.pywebview = {api: bridge};

// --- the page's elements ------------------------------------------------------------
const el = (id) => document.getElementById(id);
const input = el('input');
input.selectionStart = 0;
input.selectionEnd = 0;
input.setRangeText = function setRangeText(text, start, end, mode) {
  const from = start ?? this.selectionStart;
  const to = end ?? this.selectionEnd;
  this.value = this.value.slice(0, from) + text + this.value.slice(to);
  this.selectionStart = this.selectionEnd = from + text.length;
  void mode;
};
const send = el('send');
const retry = el('retry');
const attach = el('attach');
const composer = el('composer');
const app = el('app');
const sidebar = el('sidebar');
const transcript = el('transcript');
const column = el('column');
const chatList = el('chat-list');
const menuToggle = el('menu-toggle');
const resizer = el('resizer');
const scrim = el('scrim');
const toast = el('toast');
const announcement = el('reply-announcement');
const commands = el('commands');
const citationRigor = el('citation-rigor');
const effort = el('effort');
// The markup classes the module queries and clones.
el('dialog').className = 'dialog';
scrim.className = 'scrim';
transcript.className = 'transcript';
column.className = 'column';
composer.className = 'composer';
const dock = new Element('div');
dock.className = 'dock';
document.body.appendChild(dock);
el('main').appendChild(new Element('div'));

const radios = ['off', 'advisory', 'balanced', 'strict'].map((mode) => {
  const radio = new Element('button');
  radio.setAttribute('role', 'radio');
  radio.dataset.mode = mode;
  citationRigor.appendChild(radio);
  return radio;
});
const radio = (mode) => radios.find((candidate) => candidate.dataset.mode === mode);

// The players and the interactive runtime are separate modules; the page only
// calls into them, so records of those calls are the observable bridge.
let pauseAllCalls = 0;
const mounts = [];
const recoveries = [];
globalThis.BlackroseAnimations = {
  pauseAll() { pauseAllCalls += 1; },
  mount(host, scene, onExport, documentSource, options) {
    mounts.push({host, scene, onExport, document: documentSource, options});
    return {};
  },
};
globalThis.BlackroseMotionPlayer = {
  pauseAll() {},
  recovery(attachmentId, result) { recoveries.push({attachmentId, result}); },
};
globalThis.BlackroseDiagrams = {mount() {}, settled() {}, repairResult() {}};
globalThis.BlackroseUI = {mount() {}, update() {}, unmount() {}, unmountAll() {}, setBusy() {}, flushAll: async () => {}};

// --- boot ---------------------------------------------------------------------------
responses.ready = {
  commands: [{usage: '/help', summary: 'Show the keys'}, {usage: '/new', summary: 'New chat'}],
  keymap: [{keys: ['ctrl+enter'], action: 'send', label: 'Send'},
    {keys: ['ctrl+c'], action: 'stop', label: 'Stop'}],
  layout: {},
  engine: 'webkitgtk',
};
require('../src/blackrose/gui/static/bloom.js');

const receive = (events) => globalThis.bloom.receive(events);
const status = (payload) => receive([['status', {state: 'Ready', ...payload}]]);
const conversation = (overrides = {}) => receive([['conversations', {
  items: [], current: 1, workspace: null, ...overrides,
}]]);
const defer = () => {
  const entry = {};
  entry.promise = new Promise((resolve) => { entry.resolve = resolve; });
  return entry;
};
const resizeWindow = () => {
  for (const handler of [...(windowListeners.resize || [])]) handler();
  advance(16);
};

(async () => {
  await settle();
  assert.equal(typeof globalThis.bloom?.receive, 'function');
  assert.equal(callsTo('ready').length, 1, 'boot asks the bridge for commands, keymap and layout');
  assert.ok(column.querySelector('.welcome'), 'an empty transcript starts on the welcome');
  assert.equal(document.activeElement, input, 'the composer takes focus at boot');
  conversation();

  // =================================================================================
  // 1. Turn lock: generating -> Stop, submit -> stop, and the rigour interlock
  // =================================================================================
  status({generating: true});
  assert.equal(send.textContent, 'Stop');
  assert.equal(send.getAttribute('aria-label'), 'Stop generating (Ctrl C)');
  assert.equal(attach.disabled, true);
  resetCalls();
  composer.dispatch('submit');
  await settle();
  assert.equal(callsTo('stop').length, 1, 'the composer submit stops a running reply');
  assert.equal(callsTo('submit').length, 0);

  status({generating: false});
  assert.equal(send.textContent, 'Send');
  assert.equal(attach.disabled, false);

  // The keyboard action goes straight to send(); Python refuses while busy and
  // the draft is restored. Documents that the busy->stop routing is
  // composer-only.
  input.value = 'draft while busy';
  status({generating: true});
  resetCalls();
  responses.submit = {clear: false};
  document.dispatch('keydown', {key: 'Enter', code: 'Enter', ctrlKey: true, shiftKey: false, altKey: false});
  await settle();
  assert.equal(callsTo('submit').length, 1, 'the keymap sends instead of stopping');
  assert.equal(callsTo('stop').length, 0);
  assert.equal(input.value, 'draft while busy', 'a refused send restores the draft');
  responses.submit = null;
  status({generating: false});
  input.value = '';

  // reply_start/reply_end own the same lock.
  receive([['reply_start', {conversation_id: 1, key: 'k1', label: 'Assistant'}]]);
  assert.equal(send.textContent, 'Stop');
  receive([['reply_end', {conversation_id: 1, key: 'k1', state: 'complete'}]]);
  assert.equal(send.textContent, 'Send');

  // A citation-rigour save blocks send, retry and a second rigour change.
  const rigor = defer();
  responses.set_citation_rigor = () => rigor.promise;
  status({citation_rigor: 'advisory'});
  resetCalls();
  citationRigor.dispatch('click', {target: radio('strict')});
  await settle();
  assert.equal(callsTo('set_citation_rigor').length, 1);
  assert.equal(send.disabled, true, 'send is locked while rigour saves');
  assert.equal(retry.disabled, true, 'retry is locked while rigour saves');
  citationRigor.dispatch('click', {target: radio('off')});
  await settle();
  assert.equal(callsTo('set_citation_rigor').length, 1, 'one rigour save at a time');
  composer.dispatch('submit');
  await settle();
  assert.equal(callsTo('submit').length, 0, 'a locked composer sends nothing');
  assert.equal(citationRigor.getAttribute('aria-busy'), 'true');

  // =================================================================================
  // 2. Citation rigour saver: optimistic mode, adopt or revert on the result
  // =================================================================================
  assert.equal(radio('strict').getAttribute('aria-checked'), 'true', 'the target shows while saving');
  assert.equal(radio('off').getAttribute('aria-disabled'), 'true', 'the group is locked while saving');
  rigor.resolve({ok: true, citation_rigor: 'strict'});
  await settle();
  assert.equal(citationRigor.getAttribute('aria-busy'), 'false');
  assert.equal(citationRigor.dataset.saved, 'strict');
  assert.equal(send.disabled, false);
  assert.equal(radio('strict').getAttribute('aria-checked'), 'true');
  assert.equal(radio('off').getAttribute('aria-disabled'), 'false');

  responses.set_citation_rigor = {error: 'Rigour is unavailable'};
  resetCalls();
  citationRigor.dispatch('click', {target: radio('off')});
  await settle();
  assert.equal(toast.textContent, 'Rigour is unavailable');
  assert.equal(radio('strict').getAttribute('aria-checked'), 'true', 'a refused save reverts the shown choice');
  assert.equal(citationRigor.dataset.saved, 'strict');
  assert.equal(send.disabled, false, 'the lock always releases');
  resetCalls();
  citationRigor.dispatch('click', {target: radio('strict')});
  await settle();
  assert.equal(callsTo('set_citation_rigor').length, 0, 'choosing the saved mode is a no-op');

  // =================================================================================
  // 3. Reasoning effort saver: a later press supersedes an earlier save
  // =================================================================================
  const pendingEffort = [];
  responses.set_reasoning_effort = () => {
    const entry = defer();
    pendingEffort.push(entry);
    return entry.promise;
  };
  status({reasoning_effort: 'low', reasoning_effort_levels: ['default', 'low', 'high']});
  assert.equal(effort.dataset.level, 'low');
  resetCalls();
  effort.dispatch('click');
  await settle();
  assert.equal(lastCall('set_reasoning_effort').args[0], 'high');
  assert.equal(effort.dataset.level, 'high', 'the optimistic level shows immediately');
  effort.dispatch('click');
  await settle();
  assert.equal(lastCall('set_reasoning_effort').args[0], 'default', 'the second press starts from the target');
  assert.equal(effort.dataset.level, 'default');

  pendingEffort[0].resolve({ok: true, reasoning_effort: 'high'});
  await settle();
  assert.equal(effort.dataset.level, 'default', 'a superseded save leaves the newer target alone');
  pendingEffort[1].resolve({ok: true, reasoning_effort: 'default'});
  await settle();
  // The saver drops its optimistic target and re-reads the last pushed status;
  // it ignores the reasoning_effort the bridge returns, so the label can show
  // the old level until Python pushes a new status event.
  assert.equal(effort.dataset.level, 'low', 'the label falls back to the last pushed status');
  status({reasoning_effort: 'default'});
  assert.equal(effort.dataset.level, 'default', 'the pushed status settles the label');

  // A superseded save that reports an error is not surfaced.
  const stale = defer();
  responses.set_reasoning_effort = () => stale.promise;
  effort.dispatch('click');    // low
  await settle();
  const replacement = defer();
  responses.set_reasoning_effort = () => replacement.promise;
  effort.dispatch('click');    // high
  await settle();
  const before = toast.textContent;
  stale.resolve({error: 'stale effort error'});
  await settle();
  assert.equal(toast.textContent, before, 'a superseded error is ignored');
  replacement.resolve({ok: true, reasoning_effort: 'high'});
  await settle();
  status({reasoning_effort: 'high'});
  assert.equal(effort.dataset.level, 'high');

  // =================================================================================
  // 4. Suggestion popover: the 180 ms debounce and its epoch
  // =================================================================================
  const suggestions = [];
  responses.reference_suggestions = (_trigger, query) => {
    const entry = defer();
    suggestions.push({query, resolve: entry.resolve});
    return entry.promise;
  };
  const type = (value, cursor) => {
    input.value = value;
    input.selectionStart = input.selectionEnd = cursor;
    input.dispatch('input');
  };
  type('@ro', 3);
  advance(180);
  await settle();
  assert.equal(suggestions.length, 1);
  assert.equal(suggestions[0].query, 'ro');
  type('@rou', 4);
  advance(180);
  await settle();
  assert.equal(suggestions.length, 2);
  assert.equal(suggestions[1].query, 'rou');

  suggestions[1].resolve({items: [{kind: 'file', filename: 'route.py', path: '/p/route.py', extension: 'py'}]});
  await settle();
  assert.equal(commands.hidden, false);
  assert.equal(input.getAttribute('aria-expanded'), 'true');
  assert.equal(commands.children.length, 1);
  assert.match(commands.textContent, /route\.py/);

  suggestions[0].resolve({items: [{kind: 'file', filename: 'old.py', path: '/p/old.py'}]});
  await settle();
  assert.equal(commands.children.length, 1);
  assert.doesNotMatch(commands.textContent, /old\.py/);

  // A result for text that moved on is dropped even without a newer request.
  type('@a', 2);
  advance(180);
  await settle();
  const late = suggestions.at(-1);
  input.value = '@ab';
  input.selectionStart = input.selectionEnd = 3;
  late.resolve({items: [{kind: 'file', filename: 'a.py', path: '/p/a.py'}]});
  await settle();
  assert.equal(commands.hidden, true, 'a result for older text is ignored');

  // Escape closes an open popover and invalidates the older in-flight request.
  type('@a', 2);
  advance(180);
  await settle();
  const escaped = suggestions.at(-1);
  type('@ab', 3);
  advance(180);
  await settle();
  suggestions.at(-1).resolve({items: [{kind: 'file', filename: 'kept.py', path: '/p/k.py'}]});
  await settle();
  assert.equal(commands.hidden, false);
  document.dispatch('keydown', {key: 'Escape', code: 'Escape'});
  assert.equal(commands.hidden, true, 'Escape closes the popover');
  escaped.resolve({items: [{kind: 'file', filename: 'escaped.py', path: '/p/e.py'}]});
  await settle();
  assert.equal(commands.hidden, true, 'Escape invalidates an in-flight suggestion request');
  type('', 0);

  // =================================================================================
  // 5. Modal dialog: one open, inert page, focus trap and restore
  // =================================================================================
  assert.equal(app.inert, undefined);
  input.focus();
  receive([['help', {}]]);
  assert.equal(app.inert, true, 'the page is inert while the dialog is open');
  assert.equal(scrim.hidden, false);
  assert.equal(scrim.classList.contains('is-closing'), false);
  assert.equal(el('dialog-title').textContent, 'Keys and commands');
  let open = document.querySelector('.dialog');
  assert.ok(open, 'the dialog element is present');
  assert.equal(open.hidden, false);
  assert.equal(document.activeElement.tagName, 'BUTTON', 'focus moves to the first action');

  // Opening another dialog replaces it without stacking.
  receive([['confirm_delete', {id: 3, title: 'Doomed chat'}]]);
  let dialogs = [...document.body.children].filter((node) => node._classes?.has('dialog'));
  assert.equal(dialogs.length, 1, 'at most one dialog node exists');
  open = dialogs[0];
  assert.equal(el('dialog-title').textContent, 'Delete this chat?');
  assert.deepEqual(callsTo('delete_conversation'), []);

  document.dispatch('keydown', {key: 'n', code: 'KeyN'});
  await settle();
  assert.equal(app.inert, false);
  assert.equal(scrim.hidden, true, 'closing hides the scrim');
  assert.equal(scrim.classList.contains('is-closing'), false);
  assert.equal(document.activeElement, input, 'focus returns to the trigger');
  assert.deepEqual(callsTo('delete_conversation'), []);

  receive([['confirm_delete', {id: 4, title: 'Doomed chat'}]]);
  document.dispatch('keydown', {key: 'y', code: 'KeyY'});
  await settle();
  assert.deepEqual(callsTo('delete_conversation').map((call) => call.args[0]), [4]);

  receive([['help', {}]]);
  scrim.dispatch('pointerdown', {target: scrim});
  assert.equal(scrim.hidden, true, 'a backdrop press closes the dialog');
  receive([['help', {}]]);
  document.dispatch('keydown', {key: 'Escape', code: 'Escape'});
  assert.equal(app.inert, false);
  assert.equal(scrim.hidden, true);

  // =================================================================================
  // 6. Sidebar: collapse, drag/flick, keyboard resize, overlay mode
  // =================================================================================
  assert.equal(app.classList.contains('is-overlay'), false);
  assert.equal(menuToggle.getAttribute('aria-expanded'), 'true');
  resetCalls();
  menuToggle.dispatch('click');
  await settle();
  assert.deepEqual(lastCall('save_layout').args[0], {sidebar: null, collapsed: true, wrap: false});
  assert.equal(app.classList.contains('is-collapsed'), true);
  assert.equal(sidebar.inert, true);
  assert.equal(menuToggle.getAttribute('aria-expanded'), 'false');
  menuToggle.dispatch('click');
  assert.equal(app.classList.contains('is-collapsed'), false);
  assert.equal(sidebar.inert, false);

  sidebar._rect.width = 350;
  resetCalls();
  resizer.dispatch('pointerdown', {button: 0, pointerId: 1, clientX: 350, timeStamp: 0});
  assert.equal(app.classList.contains('is-resizing'), true);
  resizer.dispatch('pointermove', {pointerId: 1, clientX: 300, timeStamp: 10});
  resizer.dispatch('pointerup', {pointerId: 1, clientX: 300, timeStamp: 20});
  await settle();
  assert.equal(app.classList.contains('is-resizing'), false);
  assert.deepEqual(lastCall('save_layout').args[0], {sidebar: 350, collapsed: true, wrap: false},
    'a flick shuts the sidebar and keeps its width');
  assert.equal(app.classList.contains('is-collapsed'), true);
  assert.equal(sidebar.inert, true);

  menuToggle.dispatch('click');
  sidebar._rect.width = 350;
  resetCalls();
  resizer.dispatch('pointerdown', {button: 0, pointerId: 2, clientX: 350, timeStamp: 100});
  resizer.dispatch('pointermove', {pointerId: 2, clientX: 400, timeStamp: 200});
  sidebar._rect.width = 400;
  resizer.dispatch('pointerup', {pointerId: 2, clientX: 400, timeStamp: 200});
  await settle();
  assert.deepEqual(lastCall('save_layout').args[0], {sidebar: 400, collapsed: false, wrap: false},
    'a slow drag settles at the released width');

  resetCalls();
  resizer.dispatch('keydown', {key: 'ArrowLeft', shiftKey: false});
  await settle();
  assert.deepEqual(lastCall('save_layout').args[0], {sidebar: 384, collapsed: false, wrap: false});
  resizer.dispatch('keydown', {key: 'Home'});
  await settle();
  assert.equal(lastCall('save_layout').args[0].sidebar, 200);
  resizer.dispatch('keydown', {key: 'End'});
  await settle();
  assert.equal(lastCall('save_layout').args[0].sidebar, 448);
  resizer.dispatch('keydown', {key: 'Enter'});
  await settle();
  assert.equal(app.classList.contains('is-collapsed'), true, 'Enter on the handle toggles the sidebar');

  // Crossing the breakpoint switches to the floating drawer without saving.
  window.innerWidth = 600;
  resizeWindow();
  assert.equal(app.classList.contains('is-overlay'), true);
  assert.equal(menuToggle.getAttribute('aria-expanded'), 'false', 'the drawer starts hidden');
  assert.equal(sidebar.inert, true);

  resetCalls();
  resizer.dispatch('pointerdown', {button: 0, pointerId: 3, clientX: 300, timeStamp: 0});
  resizer.dispatch('pointermove', {pointerId: 3, clientX: 200, timeStamp: 10});
  resizer.dispatch('pointerup', {pointerId: 3, clientX: 200, timeStamp: 20});
  await settle();
  assert.equal(callsTo('save_layout').length, 0, 'the resize handle is inert in overlay mode');

  menuToggle.dispatch('click');
  assert.equal(app.classList.contains('is-overlay-open'), true);
  assert.equal(menuToggle.getAttribute('aria-expanded'), 'true');
  assert.equal(callsTo('save_layout').length, 0, 'opening the drawer does not persist a layout');
  document.dispatch('keydown', {key: 'Escape', code: 'Escape'});
  assert.equal(app.classList.contains('is-overlay-open'), false, 'Escape closes the drawer');

  menuToggle.dispatch('click');
  conversation({items: [{id: 5, title: 'Other'}], current: 1});
  responses.open_conversation = true;
  chatList.querySelector('.chat-item').dispatch('click');
  await settle();
  assert.equal(app.classList.contains('is-overlay-open'), false, 'opening a chat closes the drawer');
  assert.equal(document.activeElement, input);
  window.innerWidth = 1200;
  resizeWindow();
  assert.equal(app.classList.contains('is-overlay'), false);
  conversation();

  // =================================================================================
  // 7. Transcript: pagination epochs and follow-the-reader scrolling
  // =================================================================================
  const pages = [];
  responses.earlier_messages = () => {
    const entry = defer();
    pages.push(entry.resolve);
    return entry.promise;
  };
  receive([['transcript', {conversation_id: 1, messages: [{role: 'user', id: 10, text: 'hello'}], earlier: true}]]);
  let earlier = column.querySelector('.earlier-history');
  assert.ok(earlier, 'an earlier-page button is rendered');
  earlier.dispatch('click');
  await settle();
  assert.equal(earlier.getAttribute('aria-busy'), 'true');
  assert.equal(earlier.textContent, 'Loading earlier messages…');

  // A redraw retires the page request; its resolution must not touch the new view.
  announcement.textContent = '';
  receive([['transcript', {conversation_id: 1, messages: [], earlier: false}]]);
  pages[0]({messages: [], earlier: false});
  await settle();
  assert.equal(announcement.textContent, '', 'a stale page resolution is dropped');

  receive([['transcript', {conversation_id: 1, messages: [{role: 'user', id: 11, text: 'again'}], earlier: true}]]);
  earlier = column.querySelector('.earlier-history');
  earlier.dispatch('click');
  await settle();
  pages[1](null);
  await settle();
  assert.equal(earlier.getAttribute('aria-busy'), 'false');
  assert.equal(earlier.textContent, 'Show earlier messages');
  assert.equal(toast.textContent, 'Could not load earlier messages');

  earlier.dispatch('click');
  await settle();
  pages[2]({messages: [], earlier: false});
  await settle();
  assert.equal(announcement.textContent, 'Showing 0 earlier messages.');
  assert.equal(column.querySelector('.earlier-history'), null, 'the last page replaces the button');

  // Auto-scroll follows only a reader who is at the bottom.
  transcript.scrollHeight = 1000;
  transcript.clientHeight = 400;
  transcript.scrollTop = 600;
  transcript.dispatch('scroll');
  receive([['reply_start', {conversation_id: 1, key: 'scroll', label: 'A'}]]);
  receive([['reply_update', {conversation_id: 1, key: 'scroll', html: '<p>one</p>'}]]);
  advance(16);
  assert.equal(transcript.scrollTop, 1000, 'new text follows the bottom');

  transcript.scrollTop = 100;
  transcript.dispatch('scroll');
  receive([['reply_update', {conversation_id: 1, key: 'scroll', html: '<p>two</p>'}]]);
  advance(16);
  assert.equal(transcript.scrollTop, 100, 'a reader who scrolled up is not dragged down');

  transcript.scrollTop = 600;
  transcript.dispatch('scroll');
  transcript.scrollHeight = 1400;
  resize(transcript);
  assert.equal(transcript.scrollTop, 1400, 'a pinned reader stays pinned across a reflow');
  receive([['reply_end', {conversation_id: 1, key: 'scroll', state: 'complete'}]]);

  // =================================================================================
  // 8. Stored/inline image loader: frame.dataset.state transitions
  // =================================================================================
  receive([['reply_start', {conversation_id: 1, key: 'images', label: 'A'}]]);
  receive([['reply_images', {conversation_id: 1, key: 'images', attachments: [
    {kind: 'image', filename: 'inline.png', src: 'data:image/png;base64,AAAA', width: 10, height: 5},
  ]}]]);
  let frame = column.querySelector('.image-frame');
  assert.ok(frame, 'an inline image frame is rendered');
  assert.equal(frame.dataset.state, 'loading');
  let button = frame.querySelector('button');
  assert.equal(button.getAttribute('aria-busy'), 'true');
  let image = frame.querySelector('img');
  image.dispatch('load');
  assert.equal(frame.dataset.state, 'ready');
  assert.equal(image.hidden, false);
  assert.equal(button.getAttribute('aria-label'), 'Enlarge inline.png');
  assert.equal(button.hasAttribute('aria-busy'), false);

  receive([['reply_images', {conversation_id: 1, key: 'images', attachments: [
    {kind: 'image', filename: 'broken.png', src: 'https://example.invalid/x.png'},
  ]}]]);
  frame = column.querySelector('.image-frame');
  image = frame.querySelector('img');
  image.dispatch('error');
  assert.equal(frame.dataset.state, 'error');
  assert.equal(frame.querySelector('img'), null);
  button = frame.querySelector('button');
  assert.equal(button.className, 'image-load');
  assert.match(button.textContent, /Image unavailable · Retry/);
  transcript.dispatch('click', {target: button});
  assert.equal(frame.dataset.state, 'loading', 'a retry re-enters loading');
  assert.ok(frame.querySelector('img'));

  // A stored preview is fetched on intersection, then loaded as an image.
  const previews = [];
  responses.image_preview = (id) => {
    const entry = defer();
    previews.push({id, resolve: entry.resolve});
    return entry.promise;
  };
  receive([['reply_images', {conversation_id: 1, key: 'images', attachments: [
    {kind: 'image', filename: 'stored.png', preview_id: 9},
  ]}]]);
  frame = column.querySelector('.image-frame');
  assert.equal(frame.dataset.previewId, '9');
  assert.match(frame.textContent, /Load image/);
  intersect(frame, true);
  await settle();
  assert.equal(frame.dataset.state, 'loading');
  assert.equal(previews.length, 1);
  assert.equal(previews[0].id, 9);
  previews[0].resolve({src: 'data:image/png;base64,BBBB', width: 2, height: 2});
  await settle();
  assert.equal(frame.dataset.imageSrc, 'data:image/png;base64,BBBB');
  assert.equal(frame.dataset.state, 'loading');
  frame.querySelector('img').dispatch('load');
  assert.equal(frame.dataset.state, 'ready');

  // A stored preview the bridge cannot produce is an error, not a spinner.
  const missing = [];
  responses.image_preview = (id) => {
    const entry = defer();
    missing.push({id, resolve: entry.resolve});
    return entry.promise;
  };
  receive([['reply_images', {conversation_id: 1, key: 'images', attachments: [
    {kind: 'image', filename: 'gone.png', preview_id: 12},
  ]}]]);
  frame = column.querySelector('.image-frame');
  intersect(frame, true);
  await settle();
  missing[0].resolve(null);
  await settle();
  assert.equal(frame.dataset.state, 'error');
  assert.match(frame.querySelector('button').textContent, /Image unavailable · Retry/);

  // =================================================================================
  // 9. Animation attachments: the SM-09 bridge wiring and recovery routing
  // =================================================================================
  mounts.length = 0;
  receive([['reply_images', {conversation_id: 1, key: 'images', attachments: [
    {kind: 'animation', attachment_id: 5, scene: {title: 'A'}, player_document: 'DOC', recovery: {status: 'queued'}},
  ]}]]);
  assert.equal(mounts.length, 1);
  assert.equal(mounts[0].document, 'DOC');
  assert.equal(mounts[0].options.attachmentId, 5);
  assert.deepEqual(mounts[0].options.recovery, {status: 'queued'});
  resetCalls();
  responses.animation_failed = {status: 'queued'};
  mounts[0].options.onFailure('boom');
  await settle();
  assert.deepEqual(lastCall('animation_failed').args, [5, 'boom']);
  receive([['animation_recovery', {conversation_id: 1, attachment_id: 5, status: 'repaired'}]]);
  assert.deepEqual(recoveries, [{attachmentId: 5,
    result: {conversation_id: 1, attachment_id: 5, status: 'repaired'}}]);
  receive([['animation_recovery', {conversation_id: 2, attachment_id: 5, status: 'stopped'}]]);
  assert.equal(recoveries.length, 1, 'an event for another conversation is ignored');

  // =================================================================================
  // 10. Tool activity: the live line is demoted into the log and never duplicated
  // =================================================================================
  receive([['reply_start', {conversation_id: 1, key: 'tools', label: 'A'}]]);
  receive([['tool_activity', {conversation_id: 1, key: 'tools', name: 'file_read', phase: 'start'}]]);
  const activity = column.querySelector('.tool-activity');
  assert.ok(activity, 'a live tool line appears');
  assert.equal(activity.textContent, 'Reading file…');
  receive([['tool_activity', {conversation_id: 1, key: 'tools', name: 'file_read', phase: 'complete',
    sequence: 1, path: 'a.txt'}]]);
  const log = column.querySelector('.tool-log');
  assert.ok(log, 'completion opens the activity log');
  assert.equal(log.children.length, 2, 'summary plus one row');
  receive([['tool_activity', {conversation_id: 1, key: 'tools', name: 'file_read', phase: 'complete',
    sequence: 1, path: 'a.txt'}]]);
  assert.equal(log.children.length, 2, 'a replayed sequence does not duplicate a row');
  receive([['reply_end', {conversation_id: 1, key: 'tools', state: 'complete'}]]);
  assert.equal(column.querySelector('.tool-activity'), null, 'the live line is removed when the reply ends');
  assert.ok(column.querySelector('.tool-log'), 'the log survives');

  // A transcript redraw pauses every animation (both engines).
  const pauses = pauseAllCalls;
  receive([['transcript', {conversation_id: 1, messages: [], earlier: false}]]);
  assert.ok(pauseAllCalls > pauses, 'a transcript redraw pauses playback');

  console.log('Main page controller state machines passed.');
})().catch((error) => { console.error(error); process.exit(1); });
