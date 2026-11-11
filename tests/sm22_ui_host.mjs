/* SM-22: drive the real Interactive UI host (ui-runtime/host.jsx) as a state machine.
 *
 * React itself is never committed: tests/sm22_stubs replaces react-dom/client
 * with a root that records the element tree, and the host objects created by
 * host.jsx render() are read out of that tree. That exercises the shipped
 * change/flush/submit/unmount logic without a DOM.
 *
 * Run: node tests/sm22_ui_host.mjs
 */
import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {tmpdir} from 'node:os';
import {dirname, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const runtime = resolve(root, '../ui-runtime');
// esbuild lives in ui-runtime/node_modules; the repository root has no install.
const {build} = createRequire(resolve(runtime, 'package.json'))('esbuild');
const contract = JSON.parse(await readFile(
  resolve(root, '../src/blackrose/gui/static/ui-contract.json'), 'utf8'));

// --- deterministic timers ---------------------------------------------------------
const timers = new Map();
let sequence = 0;
let now = 0;
globalThis.setTimeout = (callback, ms = 0) => {
  const id = ++sequence;
  timers.set(id, {at: now + Math.max(0, ms), callback});
  return id;
};
globalThis.clearTimeout = (id) => { timers.delete(id); };
globalThis.setInterval = (callback) => { const id = ++sequence; timers.set(id, {at: Infinity, callback}); return id; };
globalThis.clearInterval = (id) => { timers.delete(id); };
function tick(ms) {
  now += ms;
  for (;;) {
    const due = [...timers].filter(([, task]) => task.at <= now)
      .sort((a, b) => a[1].at - b[1].at || a[0] - b[0]);
    if (!due.length) return;
    const [id, task] = due[0];
    timers.delete(id);
    task.callback();
  }
}
const drain = async () => { for (let i = 0; i < 24; i += 1) await Promise.resolve(); };

// --- load the module under test with a recording root ------------------------------
globalThis.__sm22UiRoots = [];
const scratch = await mkdtemp(resolve(tmpdir(), 'blackrose-sm22-ui-'));
let hostModule;
try {
  await build({
    absWorkingDir: runtime,
    entryPoints: ['host.jsx'],
    bundle: true,
    platform: 'node',
    format: 'esm',
    outfile: resolve(scratch, 'host.mjs'),
    alias: {
      'react-dom': resolve(root, 'sm22_stubs/react-dom.cjs'),
      'react-dom/client': resolve(root, 'sm22_stubs/react-dom-client.cjs'),
    },
    define: {
      'process.env.NODE_ENV': '"production"',
      __UI_SCHEMA_HASH__: JSON.stringify(contract.schema_hash),
    },
  });
  hostModule = await import(pathToFileURL(resolve(scratch, 'host.mjs')));
} finally {
  await rm(scratch, {recursive: true, force: true});
}
const {mount, update, unmount, flushAll, unmountAll, setBusy, inspect} = hostModule;

// --- fixtures ---------------------------------------------------------------------
const FORM = {
  type: 'form', title: 'Weekly plan',
  fields: [{id: 'days', kind: 'number', default: 5}, {id: 'note', kind: 'text'}],
  submit_label: 'Send',
};

function block(overrides = {}) {
  return {
    block_id: 'block-1', message_id: 2, conversation_id: 1,
    protocol_version: 1, library_version: 'blackrose-semantic/1', schema_hash: contract.schema_hash,
    source_sequence: 1, source_revision: 1, view_id: 'view-1', position: 0,
    kind: 'interactive_ui', status: 'ready', document: FORM,
    fallback_text: 'fallback', source: '{"type":"form"}', actions_enabled: true,
    state_sequence: 0, saved_state: {},
    ...overrides,
  };
}

function container() { return {addEventListener() {}}; }

function treeText(node) {
  if (node === null || node === undefined || node === false) return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(treeText).join('');
  return treeText(node.props?.children);
}

function findHost(node) {
  if (!node || typeof node !== 'object') return null;
  if (Array.isArray(node)) {
    for (const child of node) { const found = findHost(child); if (found) return found; }
    return null;
  }
  const value = node.props?.value;
  if (value && typeof value.submit === 'function' && typeof value.change === 'function') return value;
  return findHost(node.props?.children);
}

function recordFor(c) {
  const roots = globalThis.__sm22UiRoots.filter((candidate) => candidate.container === c);
  const latest = roots.at(-1);
  return latest && {root: latest, host: findHost(latest.tree), text: treeText(latest.tree)};
}

function recorder() {
  const calls = {states: [], actions: [], exports: []};
  const callbacks = {
    state: async (payload) => { calls.states.push(payload); return {ok: true, state_sequence: payload.state_sequence}; },
    action: async (payload) => { calls.actions.push(payload); return {ok: true}; },
    export: async (payload) => { calls.exports.push(payload); return {ok: true}; },
  };
  return {calls, callbacks};
}

// --- 1. mount initialises state and sequence --------------------------------------
{
  const c = container();
  mount(c, block({state_sequence: 3, saved_state: {note: 'kept'}}), recorder().callbacks);
  assert.deepEqual(inspect(), [{id: 'block-1', state: {note: 'kept'}, sequence: 3, status: 'ready'}]);
  unmountAll();
  assert.deepEqual(inspect(), [], 'unmountAll clears every mount');

  const fresh = container();
  mount(fresh, block(), recorder().callbacks);
  assert.deepEqual(inspect()[0].state, {days: 5}, 'document defaults seed a new widget');
  unmountAll();
}

// --- 2. render modes --------------------------------------------------------------
{
  const unsupported = container();
  mount(unsupported, block({protocol_version: 2}), recorder().callbacks);
  let view = recordFor(unsupported);
  assert.equal(view.root.tree.props['data-ui-status'], 'unsupported_version');
  assert.match(view.text, /This answer needs a newer UI runtime/);
  assert.equal(view.host, null, 'an unsupported block renders no host');
  unmountAll();

  const preparing = container();
  mount(preparing, block({status: 'streaming', document: null}), recorder().callbacks);
  view = recordFor(preparing);
  assert.equal(view.root.tree.props['data-ui-status'], 'streaming');
  assert.match(view.text, /Preparing interactive answer/);
  unmountAll();

  const invalid = container();
  mount(invalid, block({status: 'invalid', document: null}), recorder().callbacks);
  view = recordFor(invalid);
  assert.match(view.text, /Interactive answer invalid/);
  unmountAll();

  const ready = container();
  mount(ready, block(), recorder().callbacks);
  view = recordFor(ready);
  assert.equal(view.root.tree.props['data-ui-status'], 'ready');
  assert.ok(view.host, 'a usable block exposes the host');
  // `disabled` is `false || ... || record.submitting`: undefined until a submit ran.
  assert.ok(!view.host.effectsDisabled);
  assert.equal(view.host.disabledReason, '');
  unmountAll();
}

// --- 3. the disabled matrix -------------------------------------------------------
{
  const c = container();
  mount(c, block({status: 'streaming'}), recorder().callbacks);
  let view = recordFor(c);
  assert.equal(view.host.effectsDisabled, true);
  assert.equal(view.host.disabledReason, 'Available when this answer finishes.');
  unmountAll();

  const busy = container();
  mount(busy, block(), recorder().callbacks);
  setBusy(true);
  view = recordFor(busy);
  assert.equal(view.host.effectsDisabled, true);
  assert.equal(view.host.disabledReason, 'Wait for the current reply to finish.');
  setBusy(false);
  view = recordFor(busy);
  assert.ok(!view.host.effectsDisabled);
  unmountAll();

  const gated = container();
  mount(gated, block({actions_enabled: false}), recorder().callbacks);
  view = recordFor(gated);
  assert.equal(view.host.effectsDisabled, true);
  assert.equal(view.host.disabledReason, 'Follow-up actions are disabled in settings.');
  unmountAll();
}

// --- 4. 250 ms debounce, one save, latest values -----------------------------------
{
  const c = container();
  const {calls, callbacks} = recorder();
  mount(c, block(), callbacks);
  const host = recordFor(c).host;

  host.change('note', 'a');
  tick(249);
  await drain();
  assert.equal(calls.states.length, 0, 'nothing is saved before the debounce elapses');
  host.change('note', 'ab');
  tick(249);
  await drain();
  assert.equal(calls.states.length, 0, 'a second change restarts the 250 ms window');
  tick(1);
  await drain();
  assert.equal(calls.states.length, 1, 'one save for a coalesced burst');
  assert.deepEqual(calls.states[0].values, {days: 5, note: 'ab'});
  assert.equal(calls.states[0].state_sequence, 1, 'the first save posts sequence 1');
  assert.equal(inspect()[0].sequence, 1);
  unmountAll();
}

// --- 5. saves are serialised and the sequence advances once per save --------------
{
  const c = container();
  const {callbacks} = recorder();
  const pending = [];
  let outstanding = 0;
  let maxOutstanding = 0;
  callbacks.state = (payload) => {
    outstanding += 1;
    maxOutstanding = Math.max(maxOutstanding, outstanding);
    return new Promise((resolvePromise) => {
      pending.push({payload, resolve: (value) => { outstanding -= 1; resolvePromise(value); }});
    });
  };
  mount(c, block(), callbacks);
  recordFor(c).host.change('note', 'one');
  tick(250);
  await drain();
  assert.equal(pending.length, 1);

  recordFor(c).host.change('note', 'two');
  tick(250);
  await drain();
  assert.equal(pending.length, 1, 'a second save waits for the in-flight one');
  assert.equal(maxOutstanding, 1);

  pending[0].resolve({ok: true, state_sequence: 1});
  await drain();
  assert.equal(pending.length, 2, 'the queued change saves after the first settles');
  assert.equal(pending[1].payload.state_sequence, 2);
  assert.deepEqual(pending[1].payload.values, {days: 5, note: 'two'});
  pending[1].resolve({ok: true, state_sequence: 2});
  await drain();
  assert.equal(inspect()[0].sequence, 2);
  unmountAll();
}

// --- 6. a refused save keeps the record dirty and retries the same sequence -------
{
  const c = container();
  const {calls, callbacks} = recorder();
  callbacks.state = async (payload) => {
    calls.states.push(payload);
    return {ok: false, error: 'state rejected'};
  };
  mount(c, block(), callbacks);
  recordFor(c).host.change('note', 'x');
  tick(250);
  await drain();
  assert.equal(calls.states.length, 1);
  assert.equal(inspect()[0].sequence, 0, 'a refused save does not advance the sequence');
  assert.match(recordFor(c).text, /state rejected/);

  callbacks.state = async (payload) => {
    calls.states.push(payload);
    return {ok: true, state_sequence: payload.state_sequence};
  };
  recordFor(c).host.change('note', 'xy');
  tick(250);
  await drain();
  assert.equal(calls.states.at(-1).state_sequence, 1, 'the retry reuses sequence 1');
  assert.equal(inspect()[0].sequence, 1);
  unmountAll();
}

// --- 7. H5 (cross-ref SM-05): a committed-but-unacknowledged save wedges retries --
{
  const store = {sequence: 0, values: {}};
  let loseNextReply = true;
  const callbacks = {
    state: async (payload) => {
      if (payload.state_sequence !== store.sequence + 1) return {ok: false, error: 'Stale UI state sequence'};
      store.sequence = payload.state_sequence;
      store.values = payload.values;
      if (loseNextReply) { loseNextReply = false; return {ok: false, error: 'reply lost'}; }
      return {ok: true, state_sequence: payload.state_sequence};
    },
    action: async () => ({ok: true}),
  };
  const c = container();
  mount(c, block(), callbacks);
  recordFor(c).host.change('note', 'committed');
  tick(250);
  await drain();
  assert.equal(store.sequence, 1, 'the fake backend committed sequence 1');

  recordFor(c).host.change('note', 'retry');
  tick(250);
  await drain();
  assert.equal(inspect()[0].sequence, 0, 'the page still believes sequence 0');
  assert.match(recordFor(c).text, /Stale UI state sequence/);

  update('block-1', block({state_sequence: 1, saved_state: {note: 'committed'}, source_sequence: 2}));
  await drain();
  assert.equal(inspect()[0].sequence, 0, 'update() does not adopt the backend sequence (documented)');

  recordFor(c).host.change('note', 'again');
  tick(250);
  await drain();
  assert.match(recordFor(c).text, /Stale UI state sequence/, 'every retry is refused until remount');

  unmount('block-1');
  mount(c, block({state_sequence: 1, saved_state: {note: 'committed'}}), callbacks);
  recordFor(c).host.change('note', 'after remount');
  tick(250);
  await drain();
  assert.equal(store.sequence, 2, 'a remount recovers from the persisted sequence');
  unmountAll();
}

// --- 8. submit: lock, identity, ordering -----------------------------------------
{
  const c = container();
  const {calls, callbacks} = recorder();
  mount(c, block(), callbacks);
  let host = recordFor(c).host;
  host.change('note', 'chosen');
  await host.submit();
  assert.equal(calls.states.length, 1, 'submit flushes dirty state first');
  assert.equal(calls.actions.length, 1);
  const action = calls.actions[0];
  assert.deepEqual({...action, event_id: undefined}, {
    conversation_id: 1, assistant_message_id: 2, block_id: 'block-1',
    source_revision: 1, view_id: 'view-1', event_id: undefined,
    action_id: 'submit', action_type: 'continue_conversation',
    values: {days: 5, note: 'chosen'},
  });
  assert.match(action.event_id, /^[0-9a-f-]{36}$/);

  // The submit lock: a second submit from the same render is refused while the
  // first action is in flight, even though React has not committed disabled=true.
  let release;
  callbacks.action = (payload) => { calls.actions.push(payload); return new Promise((r) => { release = r; }); };
  host = recordFor(c).host;
  const first = host.submit();
  const second = host.submit();
  await drain();
  assert.equal(calls.actions.length, 2, 'one action for two rapid submits');
  release({ok: true});
  await first;
  await second;
  assert.ok(!recordFor(c).host.effectsDisabled);

  // A refused action reports the error and releases the lock.
  callbacks.action = async (payload) => { calls.actions.push(payload); return {ok: false, error: 'Wait for the current reply to finish'}; };
  await recordFor(c).host.submit();
  assert.match(recordFor(c).text, /Wait for the current reply to finish/);
  callbacks.action = async (payload) => { calls.actions.push(payload); return {ok: true}; };
  await recordFor(c).host.submit();
  assert.equal(calls.actions.length, 4, 'a later submit is accepted');

  // Busy and streaming refuse the transition without calling the bridge.
  setBusy(true);
  const before = calls.actions.length;
  await recordFor(c).host.submit();
  assert.equal(calls.actions.length, before, 'setBusy blocks submit');
  setBusy(false);
  unmountAll();
}

// --- 9. flushAll settles every mount and reports the first failure ----------------
{
  const first = container();
  const second = container();
  const seen = [];
  const callbacks = (fail) => ({
    state: async (payload) => { seen.push(payload.block_id); return fail ? {ok: false, error: 'no'} : {ok: true, state_sequence: payload.state_sequence}; },
    action: async () => ({ok: true}),
  });
  mount(first, block({block_id: 'a'}), callbacks(true));
  mount(second, block({block_id: 'b'}), callbacks(false));
  recordFor(first).host.change('note', 'x');
  recordFor(second).host.change('note', 'y');
  await assert.rejects(flushAll(), /no/);
  assert.deepEqual(seen.sort(), ['a', 'b'], 'every record is attempted');
  unmountAll();
}

// --- 10. teardown drops a debounced save (hazard, documented) ---------------------
{
  const c = container();
  const {calls, callbacks} = recorder();
  mount(c, block(), callbacks);
  recordFor(c).host.change('note', 'lost');
  unmount('block-1');
  tick(500);
  await drain();
  assert.equal(calls.states.length, 0, 'unmount clears the pending debounce without flushing');
  assert.deepEqual(inspect(), []);
}

// --- 11. update ordering, status-driven flush, unmount idempotence ----------------
{
  const c = container();
  const {calls, callbacks} = recorder();
  mount(c, block(), callbacks);
  update('block-1', block({source_sequence: 0, document: {type: 'text', title: 'old', text: 'old'}}));
  assert.equal(recordFor(c).root.tree.props['data-ui-status'], 'ready');
  assert.match(recordFor(c).text, /No complete block was received|fallback/);

  update('missing', block());
  assert.equal(inspect().length, 1);

  const streaming = container();
  const second = recorder();
  mount(streaming, block({block_id: 'streaming', status: 'streaming'}), second.callbacks);
  recordFor(streaming).host.change('note', 'early');
  tick(250);
  await drain();
  assert.equal(second.calls.states.length, 1, 'streaming widget state is saved');
  update('streaming', block({block_id: 'streaming', status: 'ready', source_sequence: 2}));

  unmount('block-1');
  unmount('block-1');
  assert.deepEqual(inspect().map((entry) => entry.id), ['streaming']);
  unmountAll();
}

console.log('Interactive UI host state machine passed.');
