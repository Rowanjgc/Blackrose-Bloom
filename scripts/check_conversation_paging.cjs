/* Exercise sidebar paging: disclosure, keyboard loading, focus and selection across reorders. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const keymap = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../src/blackrose/gui/static/keymap.json'), 'utf8'));

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1180, height: 900}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(({keymap}) => {
      // 450 saved chats, newest first; the session sends 200 at a time.
      const all = Array.from({length: 450}, (_, i) => ({id: 450 - i, title: `Chat ${450 - i}`}));
      const test = {all, shown: 200, current: 450, calls: [], emitFirst: true, hold: null};
      const emit = (kind, data) => window.bloom.receive([[kind, data]]);
      test.emit = emit;
      test.sidebar = () => emit('conversations', {items: test.all.slice(0, test.shown), current: test.current,
        total: test.all.length, more: test.shown < test.all.length});
      window.pagingTest = test;
      window.pywebview = {api: {
        ready: async () => {
          setTimeout(() => {
            test.sidebar();
            emit('transcript', {messages: []});
            emit('status', {state: 'Ready', generating: false, model: 'qwen', model_display_name: 'Qwen'});
          }, 0);
          return {commands: [], keymap};
        },
        load_older_conversations: async () => {
          test.calls.push('load_older_conversations');
          if (test.hold) await test.hold;
          const first = test.all[test.shown]?.id ?? null;
          test.shown = Math.min(test.all.length, test.shown + 200);
          // Page events and the call result can arrive in either order.
          if (test.emitFirst) test.sidebar(); else setTimeout(test.sidebar, 50);
          await new Promise(resolve => setTimeout(resolve, 20));
          return {first, more: test.shown < test.all.length};
        },
        open_conversation: async id => { test.calls.push(['open_conversation', id]); test.current = id; test.sidebar(); return true; },
        save_layout: async () => true,
        update_draft: async () => true,
        reference_suggestions: async () => [],
      }};
    }, {keymap});
    await page.goto(pathToFileURL(path.resolve(__dirname, '../src/blackrose/gui/static/index.html')).href);
    await page.waitForFunction(() => document.querySelectorAll('.chat-item').length === 200);

    const older = page.locator('#chat-older');
    assert(await older.isVisible());
    assert.equal(await page.locator('#chat-count').textContent(), 'Showing 200 of 450 conversations');
    assert.equal(await older.getAttribute('aria-describedby'), 'chat-count');

    // Keyboard: End in the list, Tab to the control, Enter loads the next page
    // and focus lands on its first chat.
    await page.locator('.chat-item[aria-current="true"]').focus();
    await page.keyboard.press('End');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.id), '251');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'chat-older');
    // Hold the page open so the repeat reliably arrives while it is loading;
    // otherwise a slow runner can deliver it after focus moved to a chat.
    await page.evaluate(() => { window.pagingTest.hold = new Promise(resolve => { window.pagingTest.release = resolve; }); });
    await page.keyboard.press('Enter');
    await page.keyboard.press('Enter');   // a repeat while loading is ignored
    assert.equal(await page.evaluate(() => document.activeElement.id), 'chat-older');
    assert.equal(await older.getAttribute('aria-disabled'), 'true');
    await page.evaluate(() => { window.pagingTest.hold = null; window.pagingTest.release(); });
    await page.waitForFunction(() => document.activeElement?.dataset?.id === '250');
    assert.equal(await page.locator('.chat-item').count(), 400);
    assert.equal(await page.locator('#chat-count').textContent(), 'Showing 400 of 450 conversations');
    assert.deepEqual(await page.evaluate(() => window.pagingTest.calls), ['load_older_conversations']);
    assert.equal(await page.locator('.chat-item[data-id="250"]').getAttribute('tabindex'), '0');

    // The last page arrives after the call returns; the control then disappears.
    await page.evaluate(() => { window.pagingTest.emitFirst = false; });
    await older.focus();
    await page.keyboard.press('Space');
    await page.waitForFunction(() => document.activeElement?.dataset?.id === '50');
    assert.equal(await page.locator('.chat-item').count(), 450);
    assert(!(await older.isVisible()));

    // An old chat opens, and stays selected when activity reorders the list.
    await page.locator('.chat-item[data-id="3"]').click();
    await page.waitForFunction(() => document.querySelector('.chat-item[aria-current="true"]')?.dataset.id === '3');
    await page.evaluate(() => {
      const t = window.pagingTest;
      const moved = t.all.splice(t.all.findIndex(c => c.id === 120), 1)[0];
      t.all.unshift(moved);
      t.sidebar();
    });
    assert.equal(await page.locator('.chat-item').first().getAttribute('data-id'), '120');
    assert.equal(await page.locator('.chat-item[aria-current="true"]').getAttribute('data-id'), '3');
    assert.equal(await page.locator('#title').textContent(), 'Chat 3');
    assert.deepEqual(errors, []);
    console.log('conversation paging: ok');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
