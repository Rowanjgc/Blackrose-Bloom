/* Exercise the reasoning-effort control: Shift+Tab in the composer, focus elsewhere, unsupported models, narrow widths. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const keymap = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../src/blackrose/gui/static/keymap.json'), 'utf8'));
const LEVELS = ['default', 'off', 'low', 'medium', 'high', 'xhigh'];

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1180, height: 900}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(({keymap, LEVELS}) => {
      const emit = (kind, data) => window.bloom.receive([[kind, data]]);
      window.effortTest = {emit, calls: []};
      window.pywebview = {api: {
        ready: async () => {
          setTimeout(() => emit('status', {state: 'Ready', generating: false, model: 'qwen', model_display_name: 'Qwen',
            reasoning_effort: 'default', reasoning_effort_levels: LEVELS}), 0);
          return {commands: [{usage: '/new', summary: 'New conversation'}], keymap};
        },
        set_reasoning_effort: async level => {
          window.effortTest.calls.push(level);
          emit('status', {reasoning_effort: level});
          return {ok: true, reasoning_effort: level};
        },
        save_layout: async () => true,
        update_draft: async () => true,
        reference_suggestions: async () => [],
      }};
    }, {keymap, LEVELS});
    await page.goto(pathToFileURL(path.resolve(__dirname, '../src/blackrose/gui/static/index.html')).href);
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === 'Ready');
    const chip = page.locator('#effort');
    assert(await chip.isEnabled());
    assert.equal(await chip.getAttribute('aria-label'), 'Reasoning effort: Default');

    // Shift+Tab in the composer cycles and keeps focus there.
    await page.locator('#input').focus();
    await page.keyboard.press('Shift+Tab');
    await page.waitForFunction(() => window.effortTest.calls.length === 1);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'input');
    assert.equal(await chip.getAttribute('data-level'), 'off');
    assert.match(await page.locator('#toast').textContent(), /Reasoning: Off/);

    // WebKitGTK reports shifted Tab as Unidentified; the code still identifies it.
    await page.evaluate(() => document.querySelector('#input').dispatchEvent(new KeyboardEvent('keydown',
      {key: 'Unidentified', code: 'Tab', shiftKey: true, bubbles: true, cancelable: true})));
    await page.waitForFunction(() => window.effortTest.calls.length === 2);
    assert.equal(await chip.getAttribute('data-level'), 'low');

    // Rapid presses land on the last level, and the cycle wraps back to Default.
    for (let i = 0; i < 4; i++) await page.keyboard.press('Shift+Tab');
    await page.waitForFunction(() => window.effortTest.calls.length === 6);
    assert.deepEqual(await page.evaluate(() => window.effortTest.calls), ['off', 'low', 'medium', 'high', 'xhigh', 'default']);
    assert.equal(await page.locator('#effort-value').textContent(), 'Default');

    // Clicking the chip does the same.
    await chip.click();
    await page.waitForFunction(() => window.effortTest.calls.length === 7);
    assert.equal(await chip.getAttribute('data-level'), 'off');

    // Outside the composer, Shift+Tab still moves focus backwards.
    await page.locator('#send').focus();
    await page.keyboard.press('Shift+Tab');
    assert.notEqual(await page.evaluate(() => document.activeElement.id), 'send');
    assert.equal(await page.evaluate(() => window.effortTest.calls.length), 7);

    // The command popover keeps priority while it is open.
    await page.locator('#input').fill('/');
    await page.locator('#input').dispatchEvent('input');
    await page.waitForFunction(() => document.querySelector('#input').getAttribute('aria-expanded') === 'true');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => window.effortTest.calls.length), 7);
    await page.locator('#input').fill('');

    // A model without the knob: disabled chip, and Shift+Tab bells instead of cycling.
    await page.evaluate(() => window.effortTest.emit('status', {model: 'lfm', model_display_name: 'LFM',
      reasoning_effort: 'default', reasoning_effort_levels: ['default']}));
    assert(await chip.isDisabled());
    await page.locator('#input').focus();
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'input');
    assert.match(await page.locator('#toast').textContent(), /isn't adjustable for LFM/);
    assert.equal(await page.evaluate(() => window.effortTest.calls.length), 7);

    // Narrow layouts keep the chip on-screen without horizontal scrolling.
    await page.evaluate(() => window.effortTest.emit('status', {model: 'qwen', model_display_name: 'Qwen',
      reasoning_effort: 'high', reasoning_effort_levels: ['default', 'off', 'low', 'medium', 'high', 'xhigh']}));
    for (const width of [760, 390]) {
      await page.setViewportSize({width, height: 844});
      assert(await chip.isVisible());
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const box = await chip.boundingBox();
      assert(box.x >= 0 && box.x + box.width <= width, `chip fits at ${width}px`);
      if (process.env.EFFORT_SCREENSHOTS) {
        fs.mkdirSync(process.env.EFFORT_SCREENSHOTS, {recursive: true});
        await page.locator('.composer').screenshot({path: path.join(process.env.EFFORT_SCREENSHOTS, `composer-${width}.png`)});
      }
    }
    assert.deepEqual(errors, []);
    console.log('Effort browser passed: Shift+Tab in composer (incl. WebKitGTK Unidentified), rapid presses, chip click, focus elsewhere, popover priority, unsupported model, narrow widths.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
