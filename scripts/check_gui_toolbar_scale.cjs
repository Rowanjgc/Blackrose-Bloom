/* Check toolbar reachability with ordinary and 200% root text at target widths. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1180, height: 820}});
    await page.goto(pathToFileURL(path.resolve(__dirname,
      '../src/blackrose/gui/static/index.html')).href);
    await page.evaluate(() => document.fonts.ready);

    const controls = ['#menu-toggle', '#models-button', '#workspace-button',
      '#library-button', '#skills-button', '#new-chat'];
    const cases = [];
    for (const width of [1920, 1600, 1400, 1180, 760, 640, 320]) {
      for (const {fontSize, height} of [16, 32].flatMap(fontSize =>
        [820, 480].map(height => ({fontSize, height})))) {
        await page.setViewportSize({width, height});
        await page.evaluate(size => {
          document.documentElement.style.fontSize = `${size}px`;
        }, fontSize);
        await page.evaluate(() => new Promise(resolve =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))));

        const before = await page.evaluate(() => ({
          viewportWidth: innerWidth,
          documentWidth: document.documentElement.scrollWidth,
          toolbarWidth: document.querySelector('#app-toolbar').clientWidth,
          toolbarScrollWidth: document.querySelector('#app-toolbar').scrollWidth,
          navClientWidth: document.querySelector('.toolbar-nav').clientWidth,
          navScrollWidth: document.querySelector('.toolbar-nav').scrollWidth,
          children: Array.from(document.querySelector('#app-toolbar').children, e => {
            const r = e.getBoundingClientRect();
            return {selector: e.id || e.className, x: Math.round(r.x), y: Math.round(r.y),
              right: Math.round(r.right), bottom: Math.round(r.bottom),
              width: Math.round(r.width), height: Math.round(r.height),
              scrollWidth: e.scrollWidth, clientWidth: e.clientWidth};
          }),
        }));
        assert(before.documentWidth <= width,
          `document horizontal overflow at ${width}px / ${fontSize}px: ${JSON.stringify(before)}`);
        assert(before.toolbarScrollWidth <= before.toolbarWidth,
          `toolbar horizontal overflow at ${width}px / ${fontSize}px: ${JSON.stringify(before)}`);
        for (let i = 0; i < before.children.length; i++) {
          for (const other of before.children.slice(i + 1)) {
            const item = before.children[i];
            const overlaps = item.x < other.right && other.x < item.right &&
              item.y < other.bottom && other.y < item.bottom;
            assert(!overlaps,
              `toolbar items overlap at ${width}px / ${fontSize}px: ${JSON.stringify([item, other])}`);
          }
        }

        await page.locator(controls[0]).focus();
        for (const selector of controls.slice(1)) {
          await page.keyboard.press('Tab');
          assert.equal(await page.evaluate(() => `#${document.activeElement.id}`), selector,
            `${selector} is missing from sequential keyboard order at ${width}px / ${fontSize}px`);
        }
        for (const selector of controls) {
          const button = page.locator(selector);
          await button.focus();
          assert(await button.evaluate(e => e === document.activeElement),
            `${selector} cannot receive keyboard focus at ${width}px / ${fontSize}px`);
          const rect = await button.evaluate(e => {
            const r = e.getBoundingClientRect();
            return {left: r.left, right: r.right, width: r.width,
              visible: e.getClientRects().length > 0};
          });
          assert(rect.visible && rect.width > 0 && rect.left >= 0 && rect.right <= width,
            `${selector} is outside the viewport at ${width}px / ${fontSize}px: ${JSON.stringify(rect)}`);
        }
        // Focus must reveal the composer below the toolbar even when its
        // natural height exceeds the remaining pane. Test actual hit targets,
        // not just DOM focus or horizontal bounds.
        for (const selector of ['#input', '#attach', '#send']) {
          await page.locator(selector).focus();
          await page.evaluate(() => new Promise(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const target = await page.locator(selector).evaluate(e => {
            const r = e.getBoundingClientRect();
            const toolbar = document.querySelector('#app-toolbar').getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
              toolbarBottom: toolbar.bottom, height: innerHeight,
              reachable: hit === e || e.contains(hit)};
          });
          assert(target.reachable && target.top >= target.toolbarBottom &&
            target.bottom <= target.height && target.left >= 0 && target.right <= width,
          `${selector} is occluded at ${width}px / ${fontSize}px: ${JSON.stringify(target)}`);
        }
        if (width === 1180 && fontSize === 16 && height === 820) {
          assert.equal(await page.locator('#main').evaluate(e =>
            e.classList.contains('is-height-constrained')), false,
          'ordinary desktop layout should retain its floating composer');
        }
        if (process.env.TOOLBAR_SCALE_SCREENSHOTS && fontSize === 32) {
          fs.mkdirSync(process.env.TOOLBAR_SCALE_SCREENSHOTS, {recursive: true});
          await page.screenshot({path: path.join(process.env.TOOLBAR_SCALE_SCREENSHOTS,
            `toolbar-${width}-${height}-200.png`)});
        }
        cases.push({width, height, fontSize, ...before});
      }
    }
    console.log(`Toolbar scale matrix passed: ${JSON.stringify(cases.map(({children, ...entry}) => entry))}`);
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
