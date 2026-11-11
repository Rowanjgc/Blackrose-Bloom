// Exercise the same interpreter that WebKit and exported documents load.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
require('../src/blackrose/gui/static/animations.js');
const {valueAt, frameAt} = globalThis.BlackroseAnimations;
const scene = JSON.parse(fs.readFileSync(path.join(__dirname, '../skills/explanatory-animations/references/rag.animation.json')));
const track = {easing: 'linear', keyframes: [{time: 1, value: 10}, {time: 3, value: 30}]};
assert.equal(valueAt(track, 0), 10);
assert.equal(valueAt(track, 2), 20);
assert.equal(valueAt(track, 3), 30);
assert.equal(valueAt(track, 10), 30);
assert.equal(valueAt({...track, easing: 'step'}, 2.9), 10);
assert.equal(valueAt({...track, easing: 'step'}, 3), 30);
assert.equal(valueAt({...track, easing: 'smooth'}, 1.5), 13.125);
const start = JSON.stringify(scene);
assert.equal(frameAt(scene, 0).find(n => n.id === 'doc1').opacity, 0.35);
assert.equal(frameAt(scene, 6).find(n => n.id === 'doc1').opacity, 1);
assert.equal(frameAt(scene, 6).find(n => n.id === 'doc2').opacity, 0.1);
assert.equal(frameAt(scene, 12).find(n => n.id === 'answer').emphasis, 1);
assert.equal(JSON.stringify(scene), start);
assert.equal(frameAt(scene, 1.5, true).find(n => n.id === 'packet').x, 230);

// Drive the actual player clock and controls without wall-clock sleeps.
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.isConnected = true; this.textContent = ''; }
  setAttribute() {}
  append(...els) { this.children.push(...els); }
  appendChild(el) { this.children.push(el); }
  prepend(el) { this.children.unshift(el); }
  addEventListener(event, handler) { this.events[event] = handler; }
  getBoundingClientRect() { return {width: this.displayWidth || 960}; }
  getContext() {
    return new Proxy({}, {get: (_, key) => key === 'measureText' ? text => ({width: text.length * 12}) : () => {}});
  }
}
globalThis.document = {createElement: tag => new Element(tag), hidden: false};
const preference = {matches: false};
globalThis.window = {matchMedia: () => preference};
globalThis.getComputedStyle = () => ({getPropertyValue: () => '', fontFamily: 'system-ui'});
let id = 0;
const frames = new Map();
globalThis.requestAnimationFrame = callback => { frames.set(++id, callback); return id; };
globalThis.cancelAnimationFrame = id => frames.delete(id);
function advance(now) {
  const pending = [...frames.values()]; frames.clear();
  pending.forEach(callback => callback(now));
}
function find(el, predicate) { return predicate(el) ? el : el.children.map(child => find(child, predicate)).find(Boolean); }
function player(looping) {
  const host = new Element('div');
  const scene = {version:1, title:'Loop', description:'A packet repeats.', duration:1, loop:looping,
    nodes:[{id:'packet', kind:'dot', x:100, y:100}],
    tracks:[{node:'packet',property:'x',keyframes:[{time:0,value:100},{time:1,value:800}]}]};
  const control = BlackroseAnimations.mount(host, scene);
  return {control, play:find(host, e => e.className === 'animation-play'),
    canvas:find(host, e => e.tag === 'canvas'),
    loop:find(host, e => e.tag === 'input' && e.type === 'checkbox')};
}
const first = player(true);
assert.equal(first.canvas.width, 960);
window.devicePixelRatio = 2;
first.canvas.displayWidth = 720;
first.control.seek(0);
assert.equal(first.canvas.width, 1440);
assert.equal(first.canvas.height, 810);
first.canvas.displayWidth = 2000;
first.control.seek(0);
assert.equal(first.canvas.width, 2880); // bound backing-store memory
window.devicePixelRatio = 1;
first.play.events.click(); advance(0); advance(1500);
assert.equal(first.control.playing, true);
assert.equal(first.control.time, 0.5);
first.play.events.click();
assert.equal(frames.size, 0);
assert.equal(first.control.playing, false);
const second = player(false);
first.play.events.click(); second.play.events.click();
assert.equal(first.control.playing, false); // one active player
advance(0); advance(1500);
assert.equal(second.control.time, 1);
assert.equal(second.control.playing, false);
preference.matches = true;
const reduced = player(true);
assert.equal(reduced.loop.checked, false);
assert.equal(reduced.loop.disabled, true);
reduced.play.events.click(); advance(0); advance(1500);
assert.equal(reduced.control.playing, false);
console.log('Animation interpolation, looping, pause, and reduced motion passed.');
