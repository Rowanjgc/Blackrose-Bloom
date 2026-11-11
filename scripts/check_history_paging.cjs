/* Exercise earlier-history pages: order, scroll anchoring, keyboard focus and stale pages. */
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1180, height: 900}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(() => {
      const emit = (kind, data) => window.bloom.receive([[kind, data]]);
      // Ten saved turns; ids 1..20, alternating request and reply.
      const turn = id => id % 2 ? {id, role: 'user', text: `Question ${id}`, attachments: []} :
        {id, role: 'assistant', label: 'Assistant', state: 'complete', attachments: [],
         html: `<p>Answer ${id}</p>` + '<p>Saved paragraph.</p>'.repeat(6),
         sources: id === 4 ? [{title: 'Earlier source', url: 'https://example.com/earlier'}] : []};
      const range = (from, to) => Array.from({length: to - from + 1}, (_, i) => turn(from + i));
      window.historyTest = {emit, range, calls: [], hold: null};
      window.pywebview = {api: {
        ready: async () => {
          setTimeout(() => {
            emit('conversations', {items: [{id: 7, title: 'Long history'}], current: 7, workspace: null});
            emit('transcript', {messages: range(13, 20), earlier: true});
            emit('status', {state: 'Ready', generating: false});
          }, 0);
          return {commands: [], keymap: []};
        },
        earlier_messages: async (conversation, before) => {
          window.historyTest.calls.push([conversation, before]);
          if (window.historyTest.hold) await window.historyTest.hold;
          if (window.historyTest.fail) return null;
          return {messages: range(Math.max(1, before - 6), before - 1), earlier: before - 6 > 1};
        },
        save_layout: async () => true,
        update_draft: async () => true,
      }};
    });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../src/blackrose/gui/static/index.html')).href);
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === 'Ready');
    const earlier = page.getByRole('button', {name: 'Show earlier messages', exact: true});
    const texts = () => page.$$eval('#column .msg', nodes => nodes.map(n =>
      n.querySelector('.bubble, .body p')?.textContent));
    assert.equal(await earlier.count(), 1);
    assert.equal(await page.evaluate(() => document.querySelector('#column').firstElementChild.className),
      'btn quiet earlier-history');
    assert.deepEqual((await texts()).slice(0, 2), ['Question 13', 'Answer 14']);
    // The page opens at the latest message.
    assert(await page.evaluate(() => {
      const t = document.querySelector('#transcript');
      return t.scrollHeight - t.scrollTop - t.clientHeight < 2;
    }));

    // Keyboard activation keeps the reader's message in place and focus on the next control.
    await page.evaluate(() => { document.querySelector('#transcript').scrollTop = 0; });
    const anchorTop = () => page.evaluate(() => [...document.querySelectorAll('#column .msg .bubble')]
      .find(n => n.textContent === 'Question 13').getBoundingClientRect().top);
    const before = await anchorTop();
    await earlier.focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('#column .msg').length === 14);
    assert.deepEqual(await page.evaluate(() => window.historyTest.calls), [[7, 13]]);
    // bra-q59: this has failed intermittently on CI only; report the geometry it saw.
    const after = await anchorTop();
    const scroll = await page.evaluate(() => {
      const t = document.querySelector('#transcript');
      return {scrollTop: t.scrollTop, scrollHeight: t.scrollHeight, clientHeight: t.clientHeight,
        fonts: [...document.fonts].map(f => `${f.style} ${f.weight} ${f.status}`)};
    });
    assert(Math.abs(after - before) <= 1,
      `earlier page moved the visible message: before=${before} after=${after} ${JSON.stringify(scroll)}`);
    assert.deepEqual((await texts()).slice(0, 3), ['Question 7', 'Answer 8', 'Question 9']);
    assert(await page.evaluate(() => document.activeElement.classList.contains('earlier-history')));
    assert.match(await page.locator('#reply-announcement').textContent(), /6 earlier messages/);

    // A failed page leaves the control usable.
    await page.evaluate(() => { window.historyTest.fail = true; });
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('.earlier-history')?.getAttribute('aria-busy') === 'false');
    assert.equal(await earlier.textContent(), 'Show earlier messages');
    await page.evaluate(() => { window.historyTest.fail = false; });

    // Activating the final page removes the control and focuses the oldest message.
    await earlier.click();
    await page.waitForFunction(() => document.querySelectorAll('#column .msg').length === 20);
    assert.equal(await earlier.count(), 0);
    assert.deepEqual((await texts()).slice(0, 2), ['Question 1', 'Answer 2']);
    assert.equal(await page.locator('.answer-sources').count(), 1);
    assert.equal(await page.evaluate(() => document.querySelectorAll('#column .msg').length), 20);

    // A page still loading for a replaced transcript is discarded.
    await page.evaluate(() => {
      window.historyTest.emit('transcript', {messages: window.historyTest.range(13, 20), earlier: true});
      window.historyTest.hold = new Promise(resolve => { window.historyTest.release = resolve; });
    });
    await earlier.click();
    await page.waitForFunction(() => window.historyTest.calls.length === 4);
    await page.evaluate(() => {
      window.historyTest.emit('transcript', {messages: window.historyTest.range(17, 20), earlier: true});
      window.historyTest.release();
    });
    await page.waitForTimeout(50);
    assert.deepEqual(await texts(), ['Question 17', 'Answer 18', 'Question 19', 'Answer 20']);
    assert.equal(await earlier.textContent(), 'Show earlier messages');
    assert.deepEqual(errors, []);
    console.log('History paging browser passed: latest window, ordered earlier pages, scroll anchoring, keyboard focus, failure retry and stale-page discard.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
