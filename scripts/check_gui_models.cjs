/* Exercise the real model picker with a scripted bridge and illustrative catalog.
 * MODEL_PICKER_SCREENSHOTS captures the checked layouts; no models are loaded.
 */
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
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    // The fixture's saved preferences stand in for Python's preference file, so they
    // live in Node and survive reloads. Page localStorage did not reliably survive
    // page.reload() on CI (bra-q59).
    let savedFixture = null;
    await page.exposeFunction('modelFixtureStore', state => {
      if (state !== undefined) savedFixture = state;
      return savedFixture;
    });
    await page.addInitScript(() => {
      // Illustrative fixture names/IDs, including long and HTML-like names.
      const items = [
        {id: 'qwen36-35b-a3b', name: 'Qwen 3.6', status: {value: 'loaded'}},
        {id: 'gemma-4-e4b-it', name: 'Gemma 4 E4B IT'},
        {id: 'qwen-coder', name: 'Qwen Coder'},
        {id: 'long-model', name: 'A very long model name with a descriptive quantization preset and extended context configuration'},
        {id: 'literal-markup', name: '<img src=x onerror="window.modelAttack=true">'},
        ...Array.from({length: 36}, (_, i) => ({id: `fixture-${i}`, name: `Fixture model ${i}`})),
      ].map(m => ({meta: {n_ctx: 128000}, status: {value: 'unloaded'}, ...m}));
      const fixture = window.modelFixture = {items, selected: items[0].id,
        default: items[0].id, favorites: [], calls: [],
        offline: false, failSave: false, failLoad: false, delayLoad: false,
        imageChoice: '', imageLocalReason: 'Local image workflows are missing in this fixture.',
        imageMemory: {state: 'not_loaded'}};
      const imageState = () => ({
        local_memory: fixture.imageMemory,
        choice: fixture.imageChoice, effective: fixture.imageChoice || 'local', label: 'fixture', model: 'fixture/model',
        models: ['fixture/model'], busy: false,
        options: [
          {id: '', name: 'Automatic', cost: 'Local when available, otherwise off', detail: 'Fixture automatic.', available: true, reason: null},
          {id: 'local', name: 'Local · Qwen Image 2.1', cost: 'No API charge', detail: 'Fixture local.',
            available: !fixture.imageLocalReason, reason: fixture.imageLocalReason || null},
          {id: 'openrouter', name: 'OpenRouter', cost: 'Paid · OpenRouter', detail: 'Fixture paid.', available: true, reason: null},
          {id: 'off', name: 'Off', cost: 'No images', detail: 'Fixture off.', available: true, reason: null},
        ]});
      const loaded = window.modelFixtureStore().then(saved => {
        if (saved) Object.assign(fixture, {selected: saved.default, default: saved.default, favorites: [...saved.favorites]});
      });
      const persist = () => window.modelFixtureStore({default: fixture.default, favorites: [...fixture.favorites]});
      const emit = status => window.bloom.receive([['status', status]]);
      window.pywebview = {api: {
        ready: async () => {
          await loaded;
          setTimeout(() => emit({state: 'Ready', model: fixture.selected, generating: false}), 0);
          return {commands: [], keymap: [{keys: ['ctrl+m'], action: 'open_models'}]};
        },
        models: async provider => {
          fixture.calls.push(['models', provider]);
          if (fixture.delayModels) await new Promise(resolve => fixture.finishModels = resolve);
          if (fixture.offline) return {error: 'Provider offline. Try again.'};
          return {selected: fixture.selected, default: fixture.default, favorites: [...fixture.favorites],
            items: provider === 'opencode-go' ? [{id: 'opencode-go/gemma-4-e4b-it', name: 'Gemma Go fixture'}] : fixture.items};
        },
        set_model_favorite: async (model, favorite) => {
          fixture.calls.push(['favorite', model, favorite]);
          if (fixture.delayFavorite) await new Promise(resolve => fixture.finishFavorite = resolve);
          if (fixture.failSave) return {error: 'Could not write preferences'};
          fixture.favorites = fixture.favorites.filter(id => id !== model);
          if (favorite) fixture.favorites.push(model);
          await persist();
          return {favorites: [...fixture.favorites]};
        },
        select_model: async (model, makeDefault) => {
          fixture.calls.push(['select', model, makeDefault]);
          if (fixture.delayLoad) await new Promise(resolve => fixture.finishLoading = resolve);
          if (fixture.failLoad) return {error: 'Fixture could not load model'};
          fixture.selected = model;
          if (makeDefault) fixture.default = model;
          await persist();
          emit({state: 'Ready', model, generating: false});
          return {ok: true};
        },
        image_providers: async () => imageState(),
        release_image_memory: async () => {
          fixture.calls.push(['release-image-memory']);
          fixture.imageMemory = {state: 'not_loaded'};
          return imageState();
        },
        select_image_provider: async (provider, model) => {
          fixture.calls.push(['image', provider, model]);
          if (provider === 'local' && fixture.imageLocalReason) return {error: fixture.imageLocalReason};
          fixture.imageChoice = provider;
          return imageState();
        },
        save_layout: async () => true,
        update_draft: async () => true,
      }};
    });
    const open = async () => {
      await page.locator('#models-button').click();
      await page.getByLabel('Search all models').waitFor();
    };
    const close = async () => {
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('#app').inert);
    };
    const openImages = async () => {
      await open();
      await page.getByRole('tab', {name: /^Images/}).click();
      await page.getByRole('group', {name: 'Image generation'}).waitFor();
    };
    const row = id => page.locator(`.model-row[data-model-id="${id}"]`);
    const search = text => page.getByLabel('Search all models').fill(text);
    const capture = async name => {
      if (process.env.MODEL_PICKER_SCREENSHOTS) {
        await page.waitForFunction(() => getComputedStyle(document.querySelector('#dialog')).opacity === '1');
        await page.evaluate(() => document.fonts.ready);
        fs.mkdirSync(process.env.MODEL_PICKER_SCREENSHOTS, {recursive: true});
        await page.screenshot({path: path.join(process.env.MODEL_PICKER_SCREENSHOTS, `${name}.png`)});
      }
    };
    const fits = async () => {
      assert(await page.evaluate(() => {
        const box = document.querySelector('#dialog').getBoundingClientRect();
        return document.documentElement.scrollWidth <= innerWidth && box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight;
      }));
    };
    await page.goto(pathToFileURL(path.resolve(__dirname, '../src/blackrose/gui/static/index.html')).href);
    await page.getByText('Ready', {exact: true}).waitFor();
    await open();
    assert.equal(await page.locator('.model-row:visible').count(), 1);
    assert((await row('qwen36-35b-a3b').textContent()).includes('Default · Current'));
    assert.equal(await page.locator('.model-browse').getAttribute('aria-expanded'), 'false');
    // Chat and Images are tabs; only the chosen one is shown, and arrows move between them.
    assert.equal(await page.getByRole('tab', {name: /^Chat/}).getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#model-panel-images').isHidden(), true);
    assert.equal(await page.getByRole('tab', {name: /^Images/}).textContent(), 'ImagesAutomatic');
    await page.getByRole('tab', {name: /^Chat/}).focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.getByRole('tab', {name: /^Images/}).evaluate(e => e === document.activeElement && e.getAttribute('aria-selected') === 'true'), true);
    assert.equal(await page.locator('#model-panel-chat').isHidden(), true);
    await page.keyboard.press('ArrowLeft');
    await page.getByLabel('Search all models').waitFor();
    await fits(); await capture('default-desktop');
    // Collapsed content must be skipped by the dialog's custom Tab loop.
    for (let i = 0; i < 10; i++) {
      await page.keyboard.press('Tab');
      assert(await page.evaluate(() => document.activeElement.getClientRects().length > 0));
    }
    await page.getByRole('button', {name: 'Browse all models'}).click();
    assert.equal(await page.locator('.model-row:visible').count(), 41);
    await search('e4b');
    assert.equal(await page.locator('.model-row:visible').count(), 1);
    await row('gemma-4-e4b-it').locator('.model-favorite').click();
    await page.waitForFunction(() => document.querySelector('.model-favorite').getAttribute('aria-pressed') === 'true');
    assert.equal(await row('gemma-4-e4b-it').locator('.model-favorite').evaluate(e => e === document.activeElement), true);
    assert.equal(await page.evaluate(() => modelFixture.calls.filter(c => c[0] === 'select').length), 0);
    await search('');
    assert.equal(await page.locator('.model-pinned .model-row').count(), 2);
    // Fold and reopen: only explicitly saved models are in the everyday picker.
    await close(); await open();
    assert.equal(await page.locator('.model-row:visible').count(), 2);
    await capture('favorites-desktop');
    // One press on a row switches to that model.
    await search('qwen-coder');
    await row('qwen-coder').locator('.model-choice').click();
    await page.waitForFunction(() => modelFixture.selected === 'qwen-coder');
    await page.getByLabel('Search all models').waitFor();
    await search('');
    assert.equal(await page.locator('.model-row:visible').count(), 3);
    assert((await row('qwen-coder').textContent()).includes('Current'));
    assert.equal(await row('qwen-coder').locator('.model-favorite').getAttribute('aria-pressed'), 'false');
    assert.equal(await row('qwen-coder').locator('.model-choice').getAttribute('aria-current'), 'true');
    assert.equal(await row('qwen-coder').locator('.model-choice').getAttribute('aria-disabled'), 'true');
    // Make default sits behind the row's details button.
    assert.equal(await row('qwen-coder').getByRole('button', {name: 'Make default', exact: true}).isVisible(), false);
    assert.match(await row('qwen-coder').locator('.model-more').getAttribute('aria-label'), /^Details for /);
    await row('qwen-coder').locator('.model-more').click();
    assert.equal(await row('qwen-coder').locator('.model-more').getAttribute('aria-expanded'), 'true');
    assert.equal(await row('qwen-coder').locator('.model-more').evaluate(e => e === document.activeElement), true);
    await row('qwen-coder').getByRole('button', {name: 'Make default', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('.model-pinned .model-row')?.dataset.modelId === 'qwen-coder');
    assert.equal(await page.locator('.model-row:visible').count(), 2);
    // Selection failures keep search/expanded context and permit recovery.
    await search('qwen36');
    await page.evaluate(() => modelFixture.failLoad = true);
    await row('qwen36-35b-a3b').locator('.model-choice').click();
    await page.getByRole('alert').getByText('Fixture could not load model').waitFor();
    assert.equal(await page.getByLabel('Search all models').inputValue(), 'qwen36');
    assert.equal(await row('qwen36-35b-a3b').locator('.model-choice').getAttribute('aria-disabled'), null);
    assert.equal(await row('qwen36-35b-a3b').locator('.model-choice').evaluate(e => e === document.activeElement), true);
    await page.evaluate(() => modelFixture.failLoad = false);
    await close();
    // Reload tests persistence through a fresh page/bridge.
    await page.reload(); await page.getByText('Ready', {exact: true}).waitFor(); await open();
    // Report what was shown and saved if this fails again (bra-q59).
    assert.equal(await page.locator('.model-row:visible').count(), 2, JSON.stringify(await page.evaluate(async () => ({
      rows: [...document.querySelectorAll('.model-row')].map(row => [row.dataset.modelId, row.checkVisibility()]),
      fixture: {selected: modelFixture.selected, default: modelFixture.default, favorites: modelFixture.favorites},
      saved: await window.modelFixtureStore(), calls: modelFixture.calls}))));
    assert((await row('qwen-coder').textContent()).includes('Default · Current'));
    await page.evaluate(() => modelFixture.failSave = true);
    await row('gemma-4-e4b-it').locator('.model-favorite').click();
    await page.getByRole('alert').getByText('Could not write preferences').waitFor();
    assert.equal(await row('gemma-4-e4b-it').locator('.model-favorite').getAttribute('aria-pressed'), 'true');
    await page.evaluate(() => modelFixture.failSave = false);
    await row('gemma-4-e4b-it').locator('.model-favorite').click();
    await page.waitForFunction(() => document.querySelectorAll('.model-pinned .model-row').length === 1);
    assert.equal(await page.locator('.model-browse').evaluate(e => e === document.activeElement), true);
    await search('no-such-model');
    await page.getByText('No models found. Try another name or model ID.').waitFor();
    await search('literal-markup');
    assert.equal(await page.locator('.model-row img').count(), 0);
    assert.equal(await page.evaluate(() => !!window.modelAttack), false);
    await search('gemma');
    await row('gemma-4-e4b-it').locator('.model-favorite').click();
    await page.waitForFunction(() => document.querySelector('.model-favorite').getAttribute('aria-pressed') === 'true');
    await close();
    // Image generation: Python-decided reasons, focusable unavailable options, and a changed choice.
    await openImages();
    const imageGroup = page.getByRole('group', {name: 'Image generation'});
    await imageGroup.waitFor();
    assert.equal(await imageGroup.getByRole('button', {name: 'Automatic', exact: true}).getAttribute('aria-pressed'), 'true');
    const localImage = imageGroup.getByRole('button', {name: /Local · Qwen Image 2.1/});
    assert.equal(await localImage.getAttribute('aria-disabled'), 'true');
    assert.equal(await localImage.evaluate(button => button.disabled), false);
    assert((await imageGroup.locator('#image-provider-detail-local').textContent()).includes('Local image workflows are missing'));
    // An unavailable option is reachable by keyboard and announces why it cannot be chosen.
    const imageCalls = await page.evaluate(() => modelFixture.calls.filter(c => c[0] === 'image').length);
    await localImage.focus();
    await page.keyboard.press('Enter');
    await page.getByRole('status').filter({hasText: 'Local image workflows are missing'}).waitFor();
    assert.equal(await localImage.evaluate(button => button === document.activeElement), true);
    assert.equal(await localImage.getAttribute('aria-pressed'), 'false');
    assert.equal(await page.evaluate(() => modelFixture.calls.filter(c => c[0] === 'image').length), imageCalls);
    await imageGroup.getByRole('button', {name: 'Off', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('.image-provider-option[data-option-id="off"]').getAttribute('aria-pressed') === 'true');
    assert.equal(await page.evaluate(() => modelFixture.imageChoice), 'off');
    assert.equal(await page.getByRole('status').filter({hasText: 'Image generation:'}).count(), 1);
    assert.match(await page.getByRole('tab', {name: /^Images/}).textContent(), /^ImagesOff/);
    await fits(); await capture('images-desktop');
    assert.equal(await page.locator('#image-memory-state').textContent(), 'Image model: Not loaded');
    assert.equal(await imageGroup.getByRole('button', {name: 'Release now'}).count(), 0);
    await close();
    // A warm image model shows its release countdown and can be released now.
    await page.evaluate(() => modelFixture.imageMemory = {state: 'loaded', seconds_left: 95});
    await openImages();
    const memory = page.locator('#image-memory-state');
    await memory.waitFor();
    assert.match(await memory.textContent(), /^Image model: Loaded · releases in 1:3[45]$/);
    const before = await memory.textContent();
    await page.waitForFunction(text => document.querySelector('#image-memory-state').textContent !== text, before, {timeout: 3000});
    const releaseNow = imageGroup.getByRole('button', {name: 'Release now'});
    assert.equal(await releaseNow.getAttribute('aria-describedby'), 'image-memory-state');
    await releaseNow.focus();
    await page.keyboard.press('Enter');
    await page.getByRole('status').filter({hasText: 'Image model released.'}).waitFor();
    assert.equal(await memory.textContent(), 'Image model: Not loaded');
    assert.deepEqual(await page.evaluate(() => modelFixture.calls.filter(c => c[0] === 'release-image-memory').length), 1);
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('.image-provider-options')), true);
    await close();
    // When the window ends the page asks Python again rather than assuming the release.
    await page.evaluate(() => modelFixture.imageMemory = {state: 'loaded', seconds_left: 1});
    await openImages();
    await page.locator('#image-memory-state').waitFor();
    await page.evaluate(() => modelFixture.imageMemory = {state: 'not_loaded'});
    await page.waitForFunction(() => document.querySelector('#image-memory-state')?.textContent === 'Image model: Not loaded', null, {timeout: 4000});
    await close();
    // A missing saved preset remains removable, with no selectable actions.
    await page.evaluate(() => modelFixture.items = modelFixture.items.filter(m => m.id !== 'gemma-4-e4b-it'));
    await open();
    assert((await row('gemma-4-e4b-it').textContent()).includes('Unavailable'));
    const selectsBefore = await page.evaluate(() => modelFixture.calls.filter(c => c[0] === 'select').length);
    assert.equal(await row('gemma-4-e4b-it').locator('.model-choice').getAttribute('aria-disabled'), 'true');
    await row('gemma-4-e4b-it').locator('.model-choice').click({force: true});
    assert.equal(await page.evaluate(() => modelFixture.calls.filter(c => c[0] === 'select').length), selectsBefore);
    await row('gemma-4-e4b-it').locator('.model-more').click();
    assert(await row('gemma-4-e4b-it').locator('.model-default').isDisabled());
    await row('gemma-4-e4b-it').locator('.model-favorite').click();
    await page.waitForFunction(() => document.querySelectorAll('.model-pinned .model-row').length === 1);
    // Provider-specific favorites use the full ID and do not change selection.
    await page.getByLabel('Model provider').selectOption('opencode-go');
    await page.getByRole('button', {name: 'Browse all models'}).waitFor();
    await search('gemma');
    await row('opencode-go/gemma-4-e4b-it').locator('.model-favorite').click();
    await page.waitForFunction(() => document.querySelector('.model-favorite').getAttribute('aria-pressed') === 'true');
    await page.getByLabel('Model provider').selectOption('local');
    await page.getByLabel('Search all models').waitFor();
    assert.equal(await page.locator('.model-pinned .model-row').count(), 1);
    // Narrow layouts, long names, reduced motion, and keyboard use.
    for (const width of [640, 390]) {
      await page.setViewportSize({width, height: 760});
      await page.emulateMedia({reducedMotion: 'reduce'});
      await search('long-model');
      await row('long-model').locator('.model-more').click();
      await fits(); await capture(`long-name-${width}`);
      assert(await row('long-model').locator('.model-name').evaluate(e => e.scrollWidth <= e.clientWidth));
      await search('');
      await page.getByRole('button', {name: 'Browse all models'}).click();
      await fits(); await capture(`browse-${width}`);
      await page.getByRole('tab', {name: /^Images/}).click();
      await fits(); await capture(`images-${width}`);
      await page.getByRole('tab', {name: /^Chat/}).click();
      await close(); await page.keyboard.press('Control+m');
      await page.getByLabel('Search all models').waitFor();
    }
    await page.setViewportSize({width: 1180, height: 820});
    await page.getByLabel('Model provider').selectOption('opencode-go');
    await page.getByLabel('Search all models').waitFor();
    await close();
    await page.evaluate(() => modelFixture.offline = true);
    await page.locator('#models-button').click();
    await page.getByRole('alert').getByText('Provider offline. Try again.').waitFor();
    await page.evaluate(() => modelFixture.offline = false);
    await page.locator('#dialog').getByRole('button', {name: 'Retry', exact: true}).click();
    await page.getByLabel('Search all models').waitFor();
    // Late discovery/favorite responses must never steal focus after dismissal.
    await close();
    await page.evaluate(() => modelFixture.delayModels = true);
    await page.locator('#models-button').click();
    await page.waitForFunction(() => !!modelFixture.finishModels);
    await close();
    await page.evaluate(async () => {
      modelFixture.delayModels = false;
      modelFixture.finishModels();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    assert(await page.locator('#models-button').evaluate(e => e === document.activeElement));
    await open();
    await page.evaluate(() => modelFixture.delayFavorite = true);
    await row('qwen-coder').locator('.model-favorite').click();
    await page.waitForFunction(() => !!modelFixture.finishFavorite);
    await close();
    await page.evaluate(async () => {
      modelFixture.finishFavorite();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    assert(await page.locator('#models-button').evaluate(e => e === document.activeElement));
    await page.evaluate(() => {modelFixture.items = []; modelFixture.delayFavorite = false;});
    await open();
    await page.getByText('No answering models are available from this provider.').waitFor();
    assert.equal(await page.locator('.model-browse').count(), 0);
    assert((await row('qwen-coder').textContent()).includes('Unavailable'));
    assert.deepEqual(errors, []);
    console.log('Model picker passed: Chat/Images tabs, one-press switching with details behind ⋯, image generation availability and choice, image model countdown and Release now, folded catalog, search by name/ID, Favorites and default persistence, current visibility, provider isolation, failures/retry, missing presets, keyboard focus, literal text, desktop/narrow/mobile and reduced motion.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
