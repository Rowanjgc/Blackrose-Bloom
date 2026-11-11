/* Live-chat replay, background terminal events, and reading another chat. */
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
      const emit = (kind, payload) => window.bloom.receive([[kind, payload]]);
      const t = window.navigationTest = {emit, current: 1, generating: true, state: 'pending',
        text: 'Partial A', calls: []};
      const user = {id: 1, role: 'user', text: 'Question A', attachments: []};
      const answer = (id, html, state = 'complete') => ({id, role: 'assistant', label: 'Assistant',
        state, html, attachments: [], ...(state === 'error' ? {error: 'A provider failed'} : {})});
      t.status = () => ({state: t.generating ? 'Generating…' : 'Ready', generating: t.generating,
        generation_conversation: t.generating ? 1 : null, model: 'test', can_retry: !t.generating});
      t.sidebar = () => emit('conversations', {items: [{id: 1, title: 'Chat A'}, {id: 2, title: 'Chat B'}],
        current: t.current});
      t.snapshot = () => {
        t.sidebar();
        emit('snapshot', {
          messages: t.current === 2 ? [answer(20, '<p>Saved B</p>' + '<p>B reading history.</p>'.repeat(50))] :
            [user, ...(!t.generating ? [answer(2, `<p>${t.text}</p>`, t.state)] : [])],
          active: t.current === 1 && t.generating ? {key: 1, conversation_id: 1, label: 'Assistant',
            html: `<p>${t.text}</p>`, reasoning: 'A reasoning', user: null} : null,
          draft: t.current === 1 ? 'A draft' : 'B draft', status: t.status(),
        });
      };
      window.pywebview = {api: {
        ready: async () => {setTimeout(t.snapshot, 0); return {commands: [], keymap: []};},
        open_conversation: async id => {t.calls.push(id); t.current = id; t.snapshot(); return true;},
        save_layout: async () => true,
        update_draft: async () => true,
        reference_suggestions: async () => [],
      }};
    });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../src/blackrose/gui/static/index.html')).href);
    await page.waitForFunction(() => document.querySelector('.is-streaming'));
    const chat = id => page.locator(`.chat-item[data-id="${id}"]`);
    const body = () => page.locator('#column').textContent();
    assert.equal(await page.locator('.msg.assistant').count(), 1);
    await chat(2).click();
    assert.match(await body(), /Saved B/);
    assert.doesNotMatch(await body(), /Partial A|Question A|A reasoning/);
    assert.equal(await page.locator('#input').inputValue(), 'B draft');
    assert.equal(await page.locator('#send').textContent(), 'Stop');
    assert(await page.locator('#retry').isDisabled());
    // Reading stays scrollable while A streams; stale queued events cannot
    // insert a second reply, append reasoning, replace history or end Stop.
    await page.evaluate(() => {document.querySelector('#transcript').scrollTop = 150;});
    const scroll = await page.locator('#transcript').evaluate(e => e.scrollTop);
    assert(scroll > 0);
    await page.evaluate(() => {
      const t = window.navigationTest;
      t.text += ' continued';
      t.emit('reply_start', {conversation_id: 1, key: 1, label: 'Assistant'});
      t.emit('reply_update', {conversation_id: 1, key: 1, html: '<p>Should stay in A</p>'});
      t.emit('reasoning', {conversation_id: 1, key: 1, text: 'Hidden reasoning'});
      t.emit('reply_end', {conversation_id: 1, key: 1, state: 'error', error: 'Hidden error'});
    });
    assert.equal(await page.locator('#transcript').evaluate(e => e.scrollTop), scroll);
    assert.doesNotMatch(await body(), /Should stay|Hidden/);
    assert.equal(await page.locator('#send').textContent(), 'Stop');
    await chat(1).click();
    assert.match(await body(), /Partial A continued/);
    assert.equal(await page.locator('.msg.assistant').count(), 1);
    assert.equal(await page.locator('.msg.user').count(), 1);
    assert.equal(await page.locator('.thinking pre').textContent(), 'A reasoning');
    await chat(2).click();
    await page.evaluate(() => {
      const t = window.navigationTest;
      t.generating = false; t.state = 'complete'; t.text += ' finished';
      t.emit('status', t.status()); t.sidebar();
    });
    assert.match(await page.locator('#title').textContent(), /Chat B/);
    assert.match(await body(), /Saved B/);
    assert.equal(await page.locator('#send').textContent(), 'Send');
    await chat(1).click();
    assert.match(await body(), /Partial A continued finished/);
    assert.equal(await page.locator('.msg.assistant').count(), 1);
    assert.equal(await page.locator('.is-streaming').count(), 0);
    await chat(2).click();
    await page.evaluate(() => {const t = window.navigationTest; t.state = 'error'; t.emit('status', t.status());});
    assert.doesNotMatch(await body(), /A provider failed/);
    await chat(1).click();
    assert.match(await body(), /A provider failed/);
    assert.deepEqual(errors, []);
    console.log('chat navigation: live replay, background completion/error, scoped events and scrolling passed');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
