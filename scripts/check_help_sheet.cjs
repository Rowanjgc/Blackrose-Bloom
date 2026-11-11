/* Exercise the Keys and commands sheet with the real keymap and command list.
 * PLAYWRIGHT_MODULE and CHROMIUM_PATH can point at existing installations.
 * HELP_SHEET_SCREENSHOTS optionally captures the checked layouts into that directory.
 */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const keymap = JSON.parse(fs.readFileSync(path.join(root, 'src/blackrose/gui/static/keymap.json'), 'utf8'));
const python = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
const commands = JSON.parse(execFileSync(python, ['-c',
  'import json; from blackrose.gui.session import COMMANDS; print(json.dumps([{"usage": u, "summary": s} for u, s in COMMANDS]))'],
  {cwd: root, env: {...process.env, PYTHONPATH: path.join(root, 'src')}}).toString());

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1180, height: 900}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(({keymap, commands}) => {
      const emit = (kind, data) => window.bloom.receive([[kind, data]]);
      window.pywebview = {api: {
        ready: async () => {
          setTimeout(() => emit('status', {state: 'Ready', generating: false, model: 'qwen', model_display_name: 'Qwen'}), 0);
          return {commands, keymap};
        },
        save_layout: async () => true,
        update_draft: async () => true,
        reference_suggestions: async () => [],
      }};
    }, {keymap, commands});
    await page.goto(pathToFileURL(path.join(root, 'src/blackrose/gui/static/index.html')).href);
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === 'Ready');
    const shots = process.env.HELP_SHEET_SCREENSHOTS;

    for (const [width, height] of [[1180, 900], [1180, 520], [380, 720]]) {
      await page.setViewportSize({width, height});
      await page.evaluate(() => window.bloom.receive([['help', {}]]));
      await page.locator('#dialog-title').waitFor();
      assert.equal(await page.locator('#dialog-title').textContent(), 'Keys and commands');
      assert(await page.locator('#dialog').evaluate(d => d.classList.contains('help-dialog')));
      const detail = page.locator('#dialog-detail');
      assert.equal(await detail.locator('.help-list').count(), 2);
      assert.equal(await detail.locator('ul').count(), 0);
      assert.equal(await detail.locator('.help-keys .help-row').count(), keymap.length + 1);
      assert.equal(await detail.locator('.help-commands .help-row').count(), commands.length);
      assert.deepEqual(await detail.locator('.help-keys .help-row').first().locator('dt, kbd').allTextContents(),
        ['Send', 'Ctrl Enter']);
      const rigor = detail.locator('.help-commands .help-row', {has: page.locator('dt', {hasText: /^\/rigor$/})});
      assert.equal(await rigor.locator('.help-args').textContent(), 'off · advisory · balanced · strict');
      const attach = detail.locator('.help-commands .help-row', {has: page.locator('dt', {hasText: /^\/attach$/})});
      assert.equal(await attach.locator('.help-args em').textContent(), 'path');
      // Close stays in view and focused; only the list scrolls.
      const close = page.getByRole('button', {name: 'Close', exact: true});
      assert(await close.evaluate(b => b === document.activeElement));
      const box = await close.boundingBox();
      assert(box && box.y + box.height <= height, `Close is off screen at ${width}x${height}`);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.equal(overflow, 0, `horizontal overflow at ${width}px`);
      if (height < 600) assert(await detail.evaluate(d => d.scrollHeight > d.clientHeight));
      if (shots) await page.waitForTimeout(500);
      if (shots) await page.screenshot({path: path.join(shots, `help-${width}x${height}.png`)});
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.querySelector('#scrim').hidden);
    }

    // Another dialog afterwards does not inherit the sheet's layout.
    await page.setViewportSize({width: 1180, height: 900});
    await page.locator('#help-button').click();
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.querySelector('#scrim').hidden);
    await page.evaluate(() => window.bloom.receive([['confirm_delete', {id: 1, title: 'A chat'}]]));
    assert(!(await page.locator('#dialog').evaluate(d => d.classList.contains('help-dialog'))));
    await page.keyboard.press('Escape');

    assert.deepEqual(errors, []);
    console.log('help sheet checks passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
