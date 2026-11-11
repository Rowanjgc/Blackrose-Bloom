/* Exercise the composer's Sources panel: source toggles, trusted sites, citation rigor, dismissal and narrow widths. */
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
      const emit = (kind, data) => window.bloom.receive([[kind, data]]);
      const status = {state: 'Ready', generating: false, model: 'qwen', model_display_name: 'Qwen',
        web: true, knowledge: true, memory: false, citation_rigor: 'off'};
      const toggle = (key, call) => async value => {
        window.sourcesTest.calls.push([call, value]);
        status[key] = value;
        emit('status', {[key]: value});
        return true;
      };
      window.sourcesTest = {emit, calls: []};
      window.pywebview = {api: {
        ready: async () => {
          setTimeout(() => emit('status', {...status}), 0);
          return {commands: [{usage: '/new', summary: 'New conversation'}], keymap};
        },
        set_web: toggle('web', 'set_web'),
        set_knowledge: toggle('knowledge', 'set_knowledge'),
        set_memory: toggle('memory', 'set_memory'),
        set_citation_rigor: async mode => {
          window.sourcesTest.calls.push(['set_citation_rigor', mode]);
          return {ok: true, citation_rigor: mode};
        },
        trusted_sites: async () => ({sites: [], defaults: []}),
        save_layout: async () => true,
        update_draft: async () => true,
        reference_suggestions: async () => [],
      }};
    }, {keymap});
    await page.goto(pathToFileURL(path.resolve(__dirname, '../src/blackrose/gui/static/index.html')).href);
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === 'Ready');
    const trigger = page.locator('#sources');
    const panel = page.locator('#sources-panel');
    const value = page.locator('#sources-value');
    const calls = () => page.evaluate(() => window.sourcesTest.calls);
    const isOpen = async () => (await trigger.getAttribute('aria-expanded')) === 'true';
    const closed = () => page.waitForFunction(() => document.querySelector('#sources-panel').hidden);

    // The sidebar no longer holds the toggles; only Keys and commands remains under the list.
    assert.equal(await page.locator('.sidebar-foot button').count(), 1);
    assert.equal(await page.locator('#sidebar #web-toggle').count(), 0);
    assert.equal(await value.textContent(), 'Local · Web');
    assert.match(await trigger.getAttribute('aria-label'), /^Sources: Local · Web\. Citation rigor: Off$/);
    assert.equal(await panel.isHidden(), true);
    // Browsers announce the popup hint correctly, so they keep it; only the
    // desktop window (WebKitGTK) leaves it off (bra-yp9).
    assert.deepEqual(await page.$$eval('button[data-popup]', (buttons) => buttons
      .filter((button) => button.getAttribute('aria-haspopup') !== 'dialog').map((button) => button.id)), []);
    assert.equal(await page.locator('button[data-popup]').count(), 6);

    // Opening moves focus into the panel; toggles apply at once and leave it open.
    await trigger.click();
    assert.equal(await isOpen(), true);
    await page.getByRole('dialog', {name: 'Chat settings'}).waitFor();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'knowledge-toggle');
    await page.getByRole('switch', {name: 'Memory'}).click();
    await page.waitForFunction(() => document.querySelector('#memory-toggle').getAttribute('aria-checked') === 'true');
    assert.equal(await isOpen(), true);
    assert.equal(await value.textContent(), 'All');
    await page.getByRole('switch', {name: 'Web search'}).click();
    await page.getByRole('switch', {name: 'Local knowledge'}).click();
    await page.waitForFunction(() => document.querySelector('#sources-value').textContent === 'Memory');
    assert.deepEqual(await calls(), [['set_memory', true], ['set_web', false], ['set_knowledge', false]]);

    // Citation rigor: a radio group whose detail line explains the choice; the summary shows non-Off modes.
    await page.getByRole('radio', {name: 'Balanced', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#citation-rigor [data-mode="balanced"]').getAttribute('aria-checked') === 'true');
    assert.equal(await value.textContent(), 'Memory · Balanced');
    assert.match(await page.locator('#rigor-detail').textContent(), /up to two evidence calls/);
    await page.keyboard.press('ArrowLeft');
    await page.waitForFunction(() => document.querySelector('#citation-rigor [data-mode="advisory"]').getAttribute('aria-checked') === 'true');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.mode), 'advisory');
    assert.equal(await page.locator('#citation-rigor [tabindex="0"]').count(), 1);

    // Escape closes and hands focus back to the trigger.
    await page.keyboard.press('Escape');
    await closed();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'sources');

    // The keyboard shortcut toggles it; a click elsewhere puts it away without stealing focus.
    await page.locator('#input').focus();
    await page.keyboard.press('Control+,');
    assert.equal(await isOpen(), true);
    await page.locator('#transcript').click({position: {x: 12, y: 12}});
    await closed();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'transcript');

    // Tabbing out of the panel closes it too.
    await trigger.click();
    await page.locator('#citation-rigor [tabindex="0"]').focus();
    await page.keyboard.press('Tab');
    await closed();

    // Trusted sites opens its dialog from here and returns focus to the trigger.
    await trigger.click();
    await page.getByRole('button', {name: /Trusted sites/}).click();
    await page.waitForFunction(() => document.querySelector('#dialog-title').textContent === 'Trusted sites');
    assert.equal(await isOpen(), false);
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.activeElement?.id === 'sources');

    // While a reply runs, the panel still shows the settings but nothing in it can change.
    await page.evaluate(() => window.sourcesTest.emit('status', {generating: true}));
    await trigger.click();
    assert(await page.locator('#web-toggle').isDisabled());
    assert.equal(await page.locator('#citation-rigor [data-mode="strict"]').getAttribute('aria-disabled'), 'true');
    const before = (await calls()).length;
    await page.locator('#citation-rigor [data-mode="strict"]').click({force: true});
    assert.equal((await calls()).length, before);
    await page.keyboard.press('Escape');
    await page.evaluate(() => window.sourcesTest.emit('status', {generating: false}));

    // Narrow windows: the panel stays on screen and the trigger keeps a name.
    for (const width of [640, 390, 320]) {
      await page.setViewportSize({width, height: 760});
      await page.emulateMedia({reducedMotion: 'reduce'});
      await trigger.click();
      assert(await panel.evaluate(el => {
        const box = el.getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight;
      }), `panel off screen at ${width}px`);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert(await page.locator('#citation-rigor [data-mode="advisory"]').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      if (process.env.SOURCES_SCREENSHOTS) {
        fs.mkdirSync(process.env.SOURCES_SCREENSHOTS, {recursive: true});
        await page.screenshot({path: path.join(process.env.SOURCES_SCREENSHOTS, `sources-${width}.png`)});
      }
      await page.keyboard.press('Escape');
      await closed();
    }
    assert.deepEqual(errors, []);
    console.log('Sources panel passed: toggles off the sidebar, live summary, toggles keep it open, rigor radio group with arrows and detail, Escape/outside/Tab dismissal with focus return, Ctrl+, shortcut, Trusted sites hand-off, locked while generating, narrow widths and reduced motion.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
