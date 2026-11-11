/* Exercise citation controls, captured evidence, copy and reload at real sizes. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
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
      window.citationTest = {emit, calls: []};
      window.pywebview = {api: {
        ready: async () => {
          setTimeout(() => emit('status', {state: 'Ready', generating: false, citation_rigor: 'off'}), 0);
          return {commands: [], keymap: []};
        },
        set_citation_rigor: async mode => {
          window.citationTest.calls.push(['mode', mode]);
          if (window.citationTest.delayRigor) await new Promise(resolve => {window.citationTest.finishRigor = resolve;});
          emit('status', {citation_rigor: mode});
          return {ok: true, citation_rigor: mode};
        },
        copy: async text => {window.citationTest.calls.push(['copy', text]); return true;},
        knowledge_open: async (documentId, chunkId) => {
          window.citationTest.calls.push(['open', documentId, chunkId]);
          return {results: [{title: 'Illustrative local report', text: 'Full indexed passage for the synthetic source.',
            citation: '[doc:fixture, chunk:passage, lines:1-4]', source_metadata: {project: 'Fixture project', version: 'v2'}}]};
        },
        save_layout: async () => true,
        update_draft: async () => true,
      }};
    });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../src/blackrose/gui/static/index.html')).href);
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === 'Ready');
    // Citation rigor lives in the composer's Sources panel as a segmented radio group.
    const sources = page.locator('#sources');
    const group = page.getByRole('radiogroup', {name: 'Citation rigor'});
    const radio = name => group.getByRole('radio', {name, exact: true});
    const openSources = async () => {
      if (await sources.getAttribute('aria-expanded') !== 'true') await sources.click();
      await group.waitFor();
    };
    const settled = () => page.waitForFunction(() => document.querySelector('#citation-rigor').getAttribute('aria-busy') === 'false');
    const locked = () => page.locator('#citation-rigor [data-mode="off"]').getAttribute('aria-disabled');
    await openSources();
    assert.equal(await radio('Off').getAttribute('aria-checked'), 'true');
    assert.match(await page.locator('#rigor-detail').textContent(), /No additional claim assessment/);
    await radio('Off').focus();
    await page.keyboard.press('ArrowRight');
    await page.waitForFunction(() => window.citationTest.calls.some(c => c[0] === 'mode' && c[1] === 'advisory'));
    await settled();
    assert.equal(await radio('Advisory').evaluate(e => e === document.activeElement && e.getAttribute('aria-checked') === 'true'), true);
    await radio('Strict').click();
    await page.waitForFunction(() => window.citationTest.calls.some(c => c[0] === 'mode' && c[1] === 'strict'));
    await settled();
    assert.match(await page.locator('#sources-value').textContent(), /Strict$/);
    assert.match(await page.locator('#rigor-detail').textContent(), /up to six evidence calls.*may cost money/);
    await page.evaluate(() => {window.citationTest.delayRigor = true;});
    await radio('Balanced').click();
    assert.equal(await locked(), 'true');
    assert(await page.locator('#send').isDisabled());
    await page.evaluate(() => {
      document.querySelector('#input').value = 'Preserved draft';
      document.querySelector('#composer').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
    });
    assert.equal(await page.locator('#input').inputValue(), 'Preserved draft');
    await page.evaluate(() => {window.citationTest.delayRigor = false; window.citationTest.finishRigor();});
    await settled();
    await openSources();
    await radio('Strict').click();
    await settled();
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.querySelector('#sources-panel').hidden);
    await page.evaluate(() => {
      window.citationTest.emit('reply_start', {key: 1, audit: {mode: 'strict', status: 'pending'}});
      window.citationTest.emit('status', {generating: true});
      window.citationTest.emit('reply_update', {key: 1, html: '<p>Provisional factual claim.</p>'});
    });
    assert.equal(await locked(), 'true');
    assert.match(await page.locator('.audit-label').textContent(), /Provisional/);
    const report = {mode: 'strict', status: 'complete', coverage: {unsupported: 1}, limitations: [],
      copy_text: 'Claim [1: unsupported]\nSource: exact captured evidence.',
      exclusions: [{text: 'Hello!', kind: 'nonfactual', reason: 'A greeting.'}],
      revision: {text: 'Provisional claim.', claims: [{id: 'c1', text: 'Provisional claim.', status: 'unsupported',
        quality_issues: [], reason: 'No original evidence supported it.', passages: []}]},
      claims: [{id: 'c1', text: 'A factual claim.', status: 'unsupported', reason: 'The passage does not support this assertion.',
        quality_issues: ['Synthetic fixture, not a publication.'], references: [],
        passages: [{source_id: 'm2s1', metadata: {title: 'Synthetic source', revision: 'r1', document_id: 'fixture', chunk_id: 'passage'},
          excerpts: ['Exact <script>window.citationAttack=true</script> captured evidence.']}]}],
      sources: [{id: 'm2s1', sha256: 'fixture', text: 'Exact <script>window.citationAttack=true</script> captured evidence. Surrounding context.'}]};
    const message = {role: 'assistant', label: 'Assistant', html: '<p>A factual claim. <button class="claim-citation" data-claim-id="c1">[1: unsupported]</button></p>', audit: report};
    await page.evaluate(({message}) => window.citationTest.emit('reply_end', {...message, key: 1, state: 'complete'}), {message});
    assert.equal(await locked(), 'false');
    assert.match(await page.locator('.audit-label').textContent(), /Source support assessed/);
    const citation = page.locator('.claim-citation');
    await citation.focus();
    await page.keyboard.press('Enter');
    await page.getByRole('dialog').waitFor();
    assert.match(await page.locator('#dialog-detail').textContent(), /Exact <script>/);
    assert.equal(await page.locator('#dialog-detail script').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.citationAttack)), false);
    await page.getByRole('button', {name: 'Open current indexed passage', exact: true}).click();
    assert.match(await page.locator('#dialog-detail').textContent(), /Full indexed passage/);
    await page.getByRole('button', {name: 'Back to claim', exact: true}).click();
    assert.match(await page.locator('.supporting-excerpt').textContent(), /Exact <script>/);
    await page.getByText('Read full captured passage', {exact: true}).click();
    assert.match(await page.locator('.captured-passage .source-text').textContent(), /Surrounding context/);
    if (process.env.CITATION_SCREENSHOTS) {
      fs.mkdirSync(process.env.CITATION_SCREENSHOTS, {recursive: true});
      await page.waitForFunction(() => getComputedStyle(document.querySelector('#dialog')).opacity > .99);
      await page.screenshot({path: path.join(process.env.CITATION_SCREENSHOTS, 'desktop.png')});
    }
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.activeElement?.classList.contains('claim-citation'));
    await page.getByRole('button', {name: 'Copy answer with assessments'}).click();
    assert(await page.evaluate(() => window.citationTest.calls.some(c => c[0] === 'copy' && c[1].includes('exact captured evidence'))));
    await page.getByRole('button', {name: 'Review evidence', exact: true}).click();
    await page.getByRole('combobox', {name: 'Filter claims'}).selectOption('supported');
    assert.match(await page.locator('.evidence-claims').textContent(), /No claims match/);
    await page.getByRole('combobox', {name: 'Filter claims'}).selectOption('attention');
    await page.getByRole('searchbox', {name: 'Find a claim'}).fill('factual');
    await page.getByRole('button', {name: 'Unsupported — A factual claim.'}).click();
    assert.match(await page.locator('#dialog-detail').textContent(), /Missing evidence is not disproof/);
    await page.keyboard.press('Escape');
    await page.getByText('About this assessment', {exact: true}).click();
    await page.getByText('Excluded from factual assessment', {exact: true}).click();
    assert.match(await page.locator('.audit-exclusions').textContent(), /Hello!/);
    await page.getByText('Provisional claim assessments before revision', {exact: true}).click();
    await page.getByRole('button', {name: 'unsupported — Provisional claim.', exact: true}).click();
    assert.match(await page.locator('#dialog-title').textContent(), /Provisional claim/);
    await page.keyboard.press('Escape');
    await page.evaluate(({message}) => window.citationTest.emit('transcript', {messages: [message]}), {message});
    assert.equal(await page.locator('.claim-citation').count(), 1);
    await page.setViewportSize({width: 390, height: 844});
    assert(await sources.isVisible());
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await openSources();
    assert(await page.locator('#sources-panel').evaluate(el => {
      const box = el.getBoundingClientRect();
      return box.left >= 0 && box.right <= innerWidth && box.top >= 0;
    }));
    await page.keyboard.press('Escape');
    await page.locator('.claim-citation').click();
    if (process.env.CITATION_SCREENSHOTS) {
      await page.waitForFunction(() => getComputedStyle(document.querySelector('#dialog')).opacity > .99);
      await page.screenshot({path: path.join(process.env.CITATION_SCREENSHOTS, 'mobile.png')});
    }
    assert(await page.locator('#dialog-actions').evaluate(el => el.getBoundingClientRect().bottom <= innerHeight));
    await page.keyboard.press('Escape');
    await openSources();
    assert.match(await page.locator('#rigor-detail').textContent(), /up to six evidence calls/);
    await page.keyboard.press('Escape');
    // Ordinary local references compact only exact retrieved citations, including inline code.
    const sourceMessage = {role: 'assistant', label: 'Assistant',
      html: '<p>A source-backed answer. <code>[doc:fixture, chunk:passage, lines:1-4]</code></p><pre><code>[doc:fixture, chunk:passage, lines:1-4]</code></pre>',
      sources: [{title: 'Illustrative local report', document_id: 'fixture', chunk_id: 'passage',
        citation: '[doc:fixture, chunk:passage, lines:1-4]', source_metadata: {project: 'Fixture project', version: 'v2'}}]};
    await page.evaluate(message => window.citationTest.emit('transcript', {messages: [message]}), sourceMessage);
    assert.equal(await page.locator('.source-reference').count(), 1);
    assert.match(await page.locator('.prose pre').textContent(), /doc:fixture/);
    await page.locator('.source-reference').click();
    assert.match(await page.locator('#dialog-detail').textContent(), /Full indexed passage/);
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.activeElement?.classList.contains('source-reference'));
    await page.getByText('Sources consulted · 1', {exact: true}).click();
    assert.match(await page.locator('.answer-sources').textContent(), /Fixture project · v2/);
    await page.locator('.answer-sources').getByText('Source details', {exact: true}).click();
    assert.match(await page.locator('.answer-sources .source-citation').textContent(), /doc:fixture/);
    if (process.env.CITATION_SCREENSHOTS) await page.screenshot({path: path.join(process.env.CITATION_SCREENSHOTS, 'sources-mobile.png')});
    const multi = {...message, audit: {...report, claims: [...report.claims,
      {id: 'c2', text: 'A second supported claim.', status: 'supported', reason: 'Established in the saved passage.', quality_issues: [], passages: []}]}};
    await page.evaluate(message => window.citationTest.emit('transcript', {messages: [message]}), multi);
    await page.locator('.claim-citation').click();
    await page.getByRole('button', {name: 'Next claim', exact: true}).click();
    assert.match(await page.locator('#dialog-detail').textContent(), /A second supported claim/);
    await page.getByRole('button', {name: 'Previous claim', exact: true}).click();
    assert.match(await page.locator('#dialog-title').textContent(), /unsupported/);
    await page.keyboard.press('Escape');
    await page.getByRole('button', {name: 'Review evidence', exact: true}).click();
    await page.getByRole('combobox', {name: 'Filter claims'}).selectOption('supported');
    assert.equal(await page.locator('#dialog-detail .claim-row').count(), 1);
    await page.setViewportSize({width: 1180, height: 900});
    if (process.env.CITATION_SCREENSHOTS) {
      await page.waitForFunction(() => getComputedStyle(document.querySelector('#dialog')).opacity > .99);
      await page.screenshot({path: path.join(process.env.CITATION_SCREENSHOTS, 'review-desktop.png')});
      await page.locator('#dialog-detail .assessment-key summary').click();
      await page.screenshot({path: path.join(process.env.CITATION_SCREENSHOTS, 'colors-desktop.png')});
    }
    await page.keyboard.press('Escape');
    await page.evaluate(() => window.citationTest.emit('snapshot', {messages: [], active: {
      key: 2, html: '<p>Partial answer.</p>', audit: {mode: 'balanced', status: 'checking'}}, status: {generating: true}}));
    assert.match(await page.locator('.audit-label').textContent(), /Provisional/);
    await page.evaluate(() => window.citationTest.emit('reply_end', {key: 2, state: 'cancelled', html: '<p>Partial answer.</p>',
      audit: {mode: 'balanced', status: 'incomplete', claims: [], limitations: ['Interrupted.']}}));
    assert.match(await page.locator('.audit-label').textContent(), /Audit incomplete/);
    assert.deepEqual(errors, []);
    console.log('Citation browser passed: keyboard setting, provisional/final/incomplete states, safe exact excerpts, copy, focus restoration, saved and active reload, desktop/mobile.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
