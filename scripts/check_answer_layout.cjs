/* Guard readable answer content, including clipping hidden by page overflow. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require('playwright');

(async () => {
  const staticRoot = path.resolve(__dirname, '../src/blackrose/gui/static');
  const artifact = JSON.parse(fs.readFileSync(path.join(staticRoot, 'ui-contract.json')));
  const css = fs.readFileSync(path.join(staticRoot, 'bloom.css'), 'utf8');
  const output = path.resolve(__dirname, '../artifacts/browser/answer-layout');
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true});
  const errors = [], records = [];
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 1000}, locale: 'en-US'});
    await page.emulateMedia({reducedMotion: 'reduce'});
    page.on('pageerror', error => errors.push(error.message));
    let initialFont = 16;
    await page.route('**/bloom.css', route => route.fulfill({contentType: 'text/css',
      body: css + `\n:root { font-size: ${initialFont}px; }`}));
    // The saved layout stands in for Python's preference file, so it lives in Node
    // and survives reloads. Page storage did not reliably survive page.reload() on
    // CI (bra-q59).
    let savedLayout = {};
    await page.exposeFunction('testLayout', () => savedLayout);
    await page.addInitScript(() => {
      window.layoutTest = {saved: []};
      window.pywebview = {api: {
        ready: async () => ({commands: [], keymap: [], layout: await window.testLayout()}),
        save_layout: async layout => {layoutTest.saved.push(layout); return true;},
        ui_state: async p => ({ok: true, state_sequence: p.state_sequence}),
      }};
    });
    const url = pathToFileURL(path.join(staticRoot, 'index.html')).href;
    await page.goto(url);
    const documents = [artifact.examples.metrics, artifact.examples.calculation,
      {type: 'metrics', title: 'Long dataset labels', unit: 'milliseconds_per_processed_request',
        aggregate: 'sum', data: [{label: 'service_' + 'x'.repeat(150), value: 123456789}, {label: 'reference', value: 12}]},
      {type: 'calculation', title: 'Long calculation units', unit: 'milliseconds_per_processed_request',
        scale_label: 'Quantity multiplier', items: [{label: 'Supplied item', quantity: 1, unit_price: 123456789}]}];
    async function show(document) {
      const block = {block_id: 'layout', message_id: 2, conversation_id: 1, position: 0,
        kind: 'interactive_ui', protocol_version: 1, schema_hash: artifact.schema_hash,
        library_version: artifact.library_version, source_revision: 1, source_sequence: 1,
        source: JSON.stringify(document), document, status: 'ready', fallback_text: document.title,
        view_id: 'layout-view', actions_enabled: true};
      await page.evaluate(block => {
        BlackroseUI.unmountAll();
        bloom.receive([['transcript', {messages: [
          {role: 'assistant', html: '<p>Ordinary Markdown remains readable beside interactive answers.</p>'},
          {role: 'assistant', blocks: [block]}]}],
        ['status', {state: 'Ready', generating: false, model: 'fixture', can_retry: false}]]);
      }, block);
      await page.locator('.blackrose-ui h3').waitFor();
    }
    async function settle() {
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)))));
      await page.waitForFunction(() => document.querySelector('#app').classList.contains('is-settled'));
    }
    async function capture(name) {
      // Expand only for the image so long answers can be inspected in full.
      // Geometry assertions still exercise the ordinary 1000px viewport.
      const viewport = page.viewportSize();
      await page.setViewportSize({...viewport, height: 6000});
      await settle();
      await page.locator('.blackrose-ui').screenshot({path: path.join(output, name)});
      await page.setViewportSize(viewport);
      await settle();
    }
    async function geometry(label) {
      const result = await page.evaluate(() => {
        const host = document.querySelector('.blackrose-ui'), box = host.getBoundingClientRect();
        // Check each text fragment against both its cell and the answer. A
        // clipped descendant can pass document.scrollWidth <= innerWidth.
        const clipped = [];
        for (const el of host.querySelectorAll('.ui-bars li > span:not(.ui-bar-track), .ui-total > *, h3, h4, label, .ui-hint')) {
          const cell = el.getBoundingClientRect(), range = document.createRange();
          range.selectNodeContents(el);
          for (const text of range.getClientRects()) {
            if (text.left < Math.max(box.left, cell.left) - 1 ||
                text.right > Math.min(box.right, cell.right) + 1) {
              clipped.push({text: el.textContent, left: text.left, right: text.right,
                cellLeft: cell.left, cellRight: cell.right});
            }
          }
        }
        const ordinary = document.querySelector('.msg.assistant .prose p').getBoundingClientRect();
        return {viewport: innerWidth, pageWidth: document.documentElement.scrollWidth,
          fontSize: parseFloat(getComputedStyle(document.documentElement).fontSize),
          mainWidth: document.querySelector('#main').clientWidth, answerWidth: box.width,
          ordinaryWidth: ordinary.width, clipped, overlay: document.querySelector('#app').classList.contains('is-overlay'),
          sidebarInert: document.querySelector('#sidebar').inert};
      });
      records.push({label, ...result});
      assert(result.pageWidth <= result.viewport + 1, `${label}: page overflow`);
      assert.deepEqual(result.clipped, [], `${label}: clipped answer content`);
      assert(result.answerWidth >= Math.min(result.viewport / 2 - 4, 300), `${label}: unreadable answer column ${result.answerWidth}`);
      assert(result.ordinaryWidth >= Math.min(result.viewport / 2 - 4, 300), `${label}: unreadable Markdown column`);
      return result;
    }
    // All requested metric widths and text scales, with both ordinary and long
    // labels/units. Tables may scroll; totals and labels must remain readable.
    for (const width of [320, 390, 520, 768, 1280]) for (const font of [16, 32]) {
      await page.setViewportSize({width, height: 1000});
      await page.evaluate(font => {document.documentElement.style.fontSize = `${font}px`;}, font);
      await settle();
      for (const document of documents) {
        await show(document);
        await geometry(`${document.title}-${width}-${font}`);
        if (document.title === 'Long dataset labels' && width === 390) {
          await capture(`metrics-${width}-${font}.png`);
        }
      }
    }
    // First load and resizing with expanded/collapsed desktop preferences and
    // a saved width larger than the available space. Drawer state is transient.
    for (const width of [760, 768, 800, 1024]) for (const font of [16, 24, 32]) for (const collapsed of [false, true]) {
      initialFont = font;
      await page.setViewportSize({width, height: 1000});
      savedLayout = {sidebar: 640, collapsed};
      await page.reload();
      await settle();
      await show(artifact.examples.form);
      const label = `sidebar-${width}-${font}-${collapsed}`;
      const before = await geometry(label);
      assert.equal(before.fontSize, font, `${label}: fresh load text size`);
      if (font > 16 || width === 760) {
        assert(before.overlay, `${label}: needs a drawer`);
        assert(before.sidebarInert, `${label}: drawer should start closed`);
        await page.locator('#menu-toggle').click();
        await settle();
        assert.equal(await page.locator('#menu-toggle').getAttribute('aria-expanded'), 'true');
        assert.equal(await page.locator('#sidebar').evaluate(el => el.inert), false);
        assert.equal((await geometry(`${label}-open`)).mainWidth, before.mainWidth, 'Drawer must not squeeze the answer');
        await page.locator('#help-button').focus();
        await page.keyboard.press('Escape');
        await settle();
        assert.equal(await page.locator('#sidebar').evaluate(el => el.inert), true);
        assert.equal(await page.evaluate(() => document.activeElement.id), 'input');
      }
      await page.setViewportSize({width: 1920, height: 1000});
      await settle();
      assert.equal(await page.locator('#app').evaluate(el => el.classList.contains('is-overlay')), false);
      assert.equal(await page.locator('#sidebar').evaluate(el => el.inert), collapsed, 'Desktop preference must survive drawer transitions');
      await geometry(`${label}-wide`);
      await page.setViewportSize({width, height: 1000});
      await settle();
      await geometry(`${label}-resize`);
      assert.deepEqual(await page.evaluate(() => layoutTest.saved), [], 'Responsive layout must not overwrite saved preferences');
      if (width === 768 && font === 32 && !collapsed) {
        await capture('form-768-32.png');
      }
    }
    // Font changes alone must trigger reflow, including keyboard focus when a
    // visible desktop sidebar becomes an initially closed drawer.
    await page.setViewportSize({width: 1024, height: 1000});
    await page.evaluate(() => {document.documentElement.style.fontSize = '16px';});
    savedLayout = {collapsed: false};
    initialFont = 16;
    await page.reload();
    await settle();
    await page.locator('#help-button').focus();
    await page.evaluate(() => {document.documentElement.style.fontSize = '32px';});
    await settle();
    assert.equal(await page.locator('#sidebar').evaluate(el => el.inert), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'menu-toggle', 'Focus must leave the hidden sidebar');
    await page.evaluate(() => {document.documentElement.style.fontSize = '16px';});
    await settle();
    assert.equal(await page.locator('#sidebar').evaluate(el => el.inert), false);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'geometry.json'), JSON.stringify(records, null, 2));
    console.log(`Answer layout passed: ${records.length} geometry checks, fresh load/resize, text-only scaling, drawer controls, focus and saved preferences.`);
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
