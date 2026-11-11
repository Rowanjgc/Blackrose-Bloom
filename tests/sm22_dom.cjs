/* Minimal DOM for the SM-22 frontend state-machine harnesses.
 *
 * This is a test double, deliberately small: it implements the element protocol
 * the shipped browser scripts actually use (attributes, camelCase dataset,
 * children, a subset of CSS selectors, focus) and a deterministic clock. It
 * never parses HTML: `innerHTML = ...` only records the string and clears
 * children, which is enough because the harness constructs the nodes it needs.
 *
 * Selector support: comma groups, descendant combinators and compounds made of
 * a tag, `.class`, `#id`, `[attr]` and `[attr="value"]`. Pseudo-classes
 * (`:not(...)`) are stripped, so a `:not` filter matches nothing extra.
 */
'use strict';

const camel = (name) => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
const kebab = (name) => 'data-' + name.replace(/[A-Z]/g, (letter) => '-' + letter.toLowerCase());

class Element {
  constructor(tag) {
    this.tagName = tag === '#fragment' ? '#FRAGMENT' : String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this._data = Object.create(null);
    this._classes = new Set();
    this.listeners = Object.create(null);
    this.style = {setProperty(name, value) { this[name] = value; }, removeProperty(name) { delete this[name]; }};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.textContentValue = '';
    this.innerHTMLValue = '';
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.clientHeight = 0;
    this.offsetHeight = 0;
    this.offsetWidth = 0;
    this.tabIndex = 0;
    this._rect = {left: 0, top: 0, width: 0, height: 0};
    this._connected = true;
    this.focusCalls = 0;
    this.posts = [];
    this.dataset = new Proxy(this._data, {
      get: (target, key) => target[key],
      set: (target, key, value) => { target[key] = String(value); this.attributes[kebab(key)] = String(value); return true; },
      deleteProperty: (target, key) => { delete target[key]; delete this.attributes[kebab(key)]; return true; },
      has: (target, key) => key in target,
    });
  }

  get className() { return [...this._classes].join(' '); }
  set className(value) {
    this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
    if (this._classes.size) this.attributes.class = this.className; else delete this.attributes.class;
  }

  get classList() {
    const set = this._classes;
    return {
      add: (...names) => { names.forEach((name) => set.add(name)); this.attributes.class = this.className; },
      remove: (...names) => { names.forEach((name) => set.delete(name)); this.attributes.class = this.className; },
      toggle: (name, force) => {
        const on = force === undefined ? !set.has(name) : Boolean(force);
        if (on) set.add(name); else set.delete(name);
        this.attributes.class = this.className;
        return on;
      },
      contains: (name) => set.has(name),
    };
  }

  get id() { return this.attributes.id || ''; }
  set id(value) { this.attributes.id = String(value); }

  get textContent() {
    return this.textContentValue + this.children.map((child) => child.textContent || '').join('');
  }
  set textContent(value) {
    this.textContentValue = value === null || value === undefined ? '' : String(value);
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
  }

  get innerHTML() { return this.innerHTMLValue; }
  set innerHTML(value) {
    this.innerHTMLValue = String(value);
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.textContentValue = '';
  }

  get parentElement() { return this.parentNode; }
  get childNodes() { return this.children; }
  get childElementCount() { return this.children.length; }
  get firstChild() { return this.children[0] || null; }

  get isConnected() {
    if (!this._connected) return false;
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === document.body || node === document.documentElement;
  }

  get contentWindow() {
    if (!this._window) this._window = {postMessage: (data) => { this.posts.push(data); }};
    return this._window;
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name === 'class') this.className = text;
    else if (name === 'id') this.attributes.id = text;
    else if (name === 'hidden') this.hidden = true;
    else if (name === 'checked') this.checked = true;
    else if (name === 'disabled') this.disabled = true;
    else if (name === 'value') this.value = text;
    else if (name.startsWith('data-')) this._data[camel(name.slice(5))] = text;
  }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  hasAttribute(name) { return name in this.attributes; }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === 'hidden') this.hidden = false;
    else if (name === 'checked') this.checked = false;
    else if (name === 'disabled') this.disabled = false;
    else if (name.startsWith('data-')) delete this._data[camel(name.slice(5))];
    else if (name === 'srcdoc' || name === 'src') delete this[name];
  }

  append(...nodes) { nodes.forEach((child) => this.appendChild(child)); }
  appendChild(child) {
    if (child.tagName === '#FRAGMENT') { child.children.forEach((grandchild) => this.appendChild(grandchild)); return child; }
    child.parentNode = this;
    child._connected = true;
    this.children.push(child);
    return child;
  }
  prepend(...nodes) {
    nodes.reverse().forEach((child) => { child.parentNode = this; child._connected = true; this.children.unshift(child); });
  }
  insertBefore(child, reference) {
    if (!reference) return this.appendChild(child);
    const at = this.children.indexOf(reference);
    child.parentNode = this;
    child._connected = true;
    if (at < 0) this.children.push(child); else this.children.splice(at, 0, child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.textContentValue = '';
    nodes.forEach((child) => this.appendChild(child));
  }
  replaceWith(replacement) {
    const parent = this.parentNode;
    if (!parent) return;
    const at = parent.children.indexOf(this);
    const replace = replacement.tagName === '#FRAGMENT' ? [...replacement.children] : [replacement];
    replace.forEach((child) => { child.parentNode = parent; child._connected = true; });
    parent.children.splice(at, at < 0 ? replace.length : 1, ...replace);
    this.parentNode = null;
  }
  remove() {
    const parent = this.parentNode;
    if (parent) {
      const at = parent.children.indexOf(this);
      if (at >= 0) parent.children.splice(at, 1);
    }
    this.parentNode = null;
    this._connected = false;
  }
  cloneNode() {
    const clone = new Element(this.tagName.toLowerCase());
    clone.className = this.className;
    clone.attributes = {...this.attributes};
    clone._data = {...this._data};
    clone._classes = new Set(this._classes);
    clone.hidden = this.hidden;
    clone.disabled = this.disabled;
    clone.tabIndex = this.tabIndex;
    return clone;
  }

  focus() { this.focusCalls += 1; document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = null; }
  scrollIntoView() {}
  getBoundingClientRect() { return {...this._rect}; }
  getClientRects() { return this.isConnected ? [this._rect] : []; }
  getContext() {
    const state = {};
    return new Proxy(state, {
      get: (target, key) => {
        if (key in target) return target[key];
        if (key === 'measureText') return (text) => ({width: String(text).length * 12});
        return () => {};
      },
      set: (target, key, value) => { target[key] = value; return true; },
    });
  }
  setPointerCapture() {}
  releasePointerCapture() {}

  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }
  matches(selector) { return matchesSelector(this, selector); }
  closest(selector) {
    for (let current = this; current; current = current.parentNode) if (matchesSelector(current, selector)) return current;
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (matchesSelector(child, selector)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  removeEventListener(type, handler) {
    const list = this.listeners[type];
    if (list) this.listeners[type] = list.filter((candidate) => candidate !== handler);
  }
  /** Harness helper: invoke every listener registered for one event type. */
  dispatch(type, event = {}) {
    const payload = {type, target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...event};
    for (const handler of [...(this.listeners[type] || [])]) handler(payload);
    const property = this['on' + type];
    if (typeof property === 'function') property(payload);
    return payload;
  }
  listenerCount(type) { return (this.listeners[type] || []).length; }
}

// --- selectors ---------------------------------------------------------------------
function parseCompound(text) {
  const clean = text.replace(/::?[a-zA-Z-]+(\([^)]*\))?/g, '');
  const match = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+|\[[^\]]*\])*)$/.exec(clean);
  if (!match) return null;
  const compound = {tag: match[1] ? match[1].toUpperCase() : null, classes: [], id: null, attrs: []};
  const rest = match[2] || '';
  for (const part of rest.match(/[.#][\w-]+|\[[^\]]*\]/g) || []) {
    if (part[0] === '.') compound.classes.push(part.slice(1));
    else if (part[0] === '#') compound.id = part.slice(1);
    else {
      const body = part.slice(1, -1);
      const equals = body.indexOf('=');
      if (equals < 0) compound.attrs.push({name: body.trim(), value: null});
      else compound.attrs.push({
        name: body.slice(0, equals).trim(),
        value: body.slice(equals + 1).trim().replace(/^["']|["']$/g, ''),
      });
    }
  }
  return compound;
}

function matchesCompound(element, compound) {
  if (!element || element.tagName === '#FRAGMENT') return false;
  if (compound.tag && element.tagName !== compound.tag) return false;
  if (compound.id && element.id !== compound.id) return false;
  for (const name of compound.classes) if (!element._classes.has(name)) return false;
  for (const {name, value} of compound.attrs) {
    const actual = element.attributes[name];
    if (actual === undefined) return false;
    if (value !== null && actual !== value) return false;
  }
  return true;
}

function matchesSelector(element, selector) {
  for (const group of String(selector).split(',')) {
    const parts = group.trim().split(/\s+/).filter(Boolean).map(parseCompound);
    if (parts.some((part) => !part)) continue;
    let node = element;
    if (!matchesCompound(node, parts[parts.length - 1])) continue;
    let index = parts.length - 2;
    let ancestor = node.parentNode;
    while (index >= 0) {
      while (ancestor && !matchesCompound(ancestor, parts[index])) ancestor = ancestor.parentNode;
      if (!ancestor) break;
      index -= 1;
      ancestor = ancestor.parentNode;
    }
    if (index < 0) return true;
  }
  return false;
}

// --- document ----------------------------------------------------------------------
const documentElement = new Element('html');
const body = new Element('body');
documentElement.appendChild(body);
const byId = new Map();
body.id = 'body';

const document = {
  documentElement,
  body,
  activeElement: null,
  hidden: false,
  listeners: Object.create(null),
  createElement: (tag) => new Element(tag),
  createElementNS: (_namespace, tag) => new Element(tag),
  createDocumentFragment: () => new Element('#fragment'),
  createTextNode: (text) => {
    const node = new Element('#text');
    node.textContentValue = String(text);
    return node;
  },
  getElementById(id) {
    if (byId.has(id)) return byId.get(id);
    const element = new Element(id === 'transcript' || id === 'column' || id === 'dialog' || id === 'scrim' ? 'div' : 'div');
    element.id = id;
    if (['input', 'send', 'retry', 'attach', 'new-chat', 'menu-toggle', 'chat-older', 'chat-more'].includes(id)) {
      element.tagName = 'BUTTON';
    }
    body.appendChild(element);
    byId.set(id, element);
    return element;
  },
  querySelectorAll(selector) { return body.querySelectorAll(selector); },
  querySelector(selector) { return body.querySelector(selector); },
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
  removeEventListener(type, handler) {
    const list = this.listeners[type];
    if (list) this.listeners[type] = list.filter((candidate) => candidate !== handler);
  },
  dispatch(type, event = {}) {
    const payload = {type, target: event.target || body, preventDefault() {}, stopPropagation() {}, ...event};
    for (const handler of [...(this.listeners[type] || [])]) handler(payload);
    return payload;
  },
};
// --- deterministic clock -----------------------------------------------------------
const clock = {
  now: 0,
  sequence: 0,
  tasks: new Map(),
  frameSequence: 1000000,
  frames: new Map(),
};
function schedule(map, callback, delay) {
  const id = ++clock.sequence;
  map.set(id, {at: clock.now + Math.max(0, delay), callback});
  return id;
}
const timers = {
  setTimeout: (callback, delay = 0) => schedule(clock.tasks, callback, delay),
  clearTimeout: (id) => { clock.tasks.delete(id); },
  setInterval: (callback, delay = 0) => schedule(clock.tasks, callback, delay),
  clearInterval: (id) => { clock.tasks.delete(id); },
  requestAnimationFrame: (callback) => {
    const id = ++clock.frameSequence;
    clock.frames.set(id, {at: clock.now + 16, callback});
    return id;
  },
  cancelAnimationFrame: (id) => { clock.frames.delete(id); },
};
/** Run every timer and animation frame due within `ms`, in time order. */
function advance(ms = 0) {
  const target = clock.now + ms;
  for (;;) {
    let nextId = 0;
    let next = null;
    for (const map of [clock.tasks, clock.frames]) {
      for (const [id, task] of map) {
        if (task.at > target) continue;
        if (!next || task.at < next.at || (task.at === next.at && id < nextId)) { next = task; nextId = id; }
      }
    }
    if (!next) break;
    clock.tasks.delete(nextId);
    clock.frames.delete(nextId);
    clock.now = Math.max(clock.now, next.at);
    next.callback(clock.now);
  }
  clock.now = Math.max(clock.now, target);
}
/** Drain pending microtasks after a timer-driven transition. */
async function settle() { for (let i = 0; i < 24; i += 1) await Promise.resolve(); }

module.exports = {
  Element, document, clock, timers, advance, settle, matchesSelector,
};
