/* Exercise real Markdown, full Mermaid, passive images, and chat lifecycle offline. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {pathToFileURL} = require('node:url');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const output = path.join(root, 'artifacts/browser/diagrams');
const samples = {
  flow: 'flowchart LR\naccTitle: Read and understand\naccDescr: Reading leads to understanding.\nA[Read] --> B[Understand]',
  sequence: 'sequenceDiagram\nUser->>Assistant: Ask\nAssistant-->>User: Explain',
  state: 'stateDiagram-v2\n[*] --> Draft\nDraft --> Ready\nReady --> [*]',
  class: 'classDiagram\nclass Reader\nclass Book\nReader --> Book : reads',
  er: 'erDiagram\nREADER ||--o{ BOOK : reads',
  mindmap: 'mindmap\n  root((Learning))\n    Read\n    Practice',
  architecture: 'architecture-beta\n group api(cloud)[API]\n service db(database)[Database] in api',
};
function html(source, closed = true) {
  return execFileSync(path.join(root, '.venv/bin/python'), ['-c',
    'import sys; from blackrose.gui.render import to_html; print(to_html(sys.stdin.read()),end="")'],
  {input: '```mermaid\n' + source + (closed ? '\n```' : ''), encoding: 'utf8'});
}
(async () => {
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1180, height: 1000}});
  const errors = [], outbound = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {if (/^https?:/.test(request.url())) outbound.push(request.url());});
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.addInitScript(() => {
    window.diagramCopies = [];
    window.repairReports = [];
    window.diagramResults = [];
    addEventListener('message', event => {
      if (event.data?.channel === 'blackrose-mermaid' && event.data.type === 'result') diagramResults.push(event.data.id);
    });
    window.pywebview = {api: {
      ready: async () => ({commands: [], keymap: [], layout: {}}),
      copy: async text => {diagramCopies.push(text); return true;},
      diagram_failed: async (...args) => {repairReports.push(args); return {status: 'queued'};},
    }};
  });
  const url = pathToFileURL(path.join(root, 'src/blackrose/gui/static/index.html')).href;
  const show = (markup, id = null) => page.evaluate(({markup, id}) => bloom.receive([['transcript', {messages: [
    {role: 'assistant', id, html: markup, state: 'complete'}]}]]), {markup, id});
  const rendered = () => page.waitForFunction(() => {
    const image = document.querySelector('.diagram-image');
    const status = document.querySelector('.diagram-status');
    if (status?.textContent.startsWith('Diagram unavailable:')) throw new Error(status.textContent);
    return image?.naturalWidth > 0 && !document.querySelector('.diagram-viewport').hidden;
  });
  try {
    await page.goto(url);
    await show('<p>Ordinary chat.</p>');
    assert.equal(await page.locator('.diagram-renderer').count(), 0);
    await show(html(samples.flow, false));
    assert.equal(await page.locator('.diagram-renderer').count(), 0);
    await page.evaluate(() => bloom.receive([['reply_start', {key: 'stream', label: 'Assistant'}]]));
    const markup = html(samples.flow);
    await page.evaluate(markup => bloom.receive([['reply_update', {key: 'stream', html: markup}]]), markup);
    await rendered();
    assert.equal(await page.locator('.diagram-renderer').count(), 1);
    assert.equal(await page.locator('.diagram-image').getAttribute('alt'),
      'Read and understand. Reading leads to understanding.');
    await page.evaluate(() => {window.savedDiagram = document.querySelector('figure[data-mermaid]');});
    await page.evaluate(markup => bloom.receive([['reply_update', {key: 'stream', html: markup + '<p>More prose.</p>'}]]), markup);
    assert(await page.evaluate(() => savedDiagram === document.querySelector('figure[data-mermaid]')));
    await page.locator('.diagram-control', {hasText: 'Source'}).click();
    assert(await page.locator('figure[data-mermaid] pre').isVisible());
    await page.locator('figure[data-mermaid] .code-copy').click();
    await page.waitForFunction(source => diagramCopies[0] === source, samples.flow);
    await page.locator('.diagram-control', {hasText: 'Source'}).click();
    const inlineWidth = await page.locator('#column .diagram-image').evaluate(image => image.getBoundingClientRect().width);
    await page.locator('.diagram-control', {hasText: 'Expand'}).click();
    assert(await page.locator('.image-viewer img').isVisible());
    assert(await page.locator('.image-viewer img').evaluate(image => image.getBoundingClientRect().width) > inlineWidth);
    await page.locator('#dialog').screenshot({path: path.join(output, 'expanded.png')});
    await page.keyboard.press('Escape');
    assert(await page.locator('.diagram-control', {hasText: 'Expand'}).evaluate(node => node === document.activeElement));
    await page.evaluate(markup => bloom.receive([['reply_end', {key: 'stream', state: 'cancelled', html: markup}]]), markup);
    assert.equal(await page.locator('#column .diagram-image').count(), 1);
    for (const [name, source] of Object.entries(samples)) {
      await show(html(source));
      await rendered();
      await page.locator('figure[data-mermaid]').screenshot({path: path.join(output, name + '.png')});
      assert.equal(await page.locator('.msg .body svg').count(), 0);
    }
    // Repeated source uses the existing renderer and cached image, including duplicates.
    const resultCount = await page.evaluate(() => diagramResults.length);
    await show(markup + markup);
    await page.waitForFunction(() => [...document.querySelectorAll('#column .diagram-image')].length === 2
      && [...document.querySelectorAll('#column .diagram-image')].every(image => image.naturalWidth > 0));
    assert.equal(await page.locator('.diagram-renderer').count(), 1);
    assert.equal(await page.evaluate(() => diagramResults.length), resultCount);
    assert(await page.locator('.diagram-renderer').evaluate(frame => frame.contentDocument === null));
    for (const source of ['this is invalid', 'flowchart LR\n' + 'A'.repeat(20001),
      'flowchart LR\n' + Array.from({length: 201}, (_, i) => `A${i}-->A${i+1}`).join('\n'),
      '%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B',
      '---\nconfig:\n  securityLevel: loose\n---\nflowchart LR\nA-->B']) {
      await show(html(source));
      await page.waitForFunction(() => document.querySelector('.diagram-status')?.textContent.startsWith('Diagram unavailable:'));
      assert(await page.locator('figure[data-mermaid] pre').isVisible());
    }
    await show(html('flowchart LR\nA["<script>parent.bad=true</script>"] --> B[Safe]\nclick B "https://example.invalid/escape"'));
    await rendered();
    assert.equal(await page.evaluate(() => window.bad), undefined);
    await show(html(samples.flow));
    await rendered();
    await page.setViewportSize({width: 640, height: 800});
    await page.locator('figure[data-mermaid]').screenshot({path: path.join(output, 'narrow.png')});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    // Reload, saved history, mixed blocks, and obsolete queued work.
    await page.reload();
    await show('<p>Reloaded prose.</p>');
    assert.equal(await page.locator('.diagram-renderer').count(), 0);
    await page.evaluate(markup => bloom.receive([['transcript', {messages: [{role: 'assistant', state: 'complete',
      blocks: [{kind: 'markdown', block_id: 'saved', html: markup}]}]}]]), markup);
    await rendered();
    await show(html(samples.sequence));
    await show('<p>Another conversation.</p>');
    assert.equal(await page.locator('#column .diagram-image').count(), 0);
    assert.deepEqual(outbound, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(await page.evaluate(() => repairReports), []);
    const bad = 'flowchart LR\nA[Start]-->B[Verify]\\nB-->C[Close]';
    await show('<p>Before.</p>' + html(bad) + '<p>After.</p>', 777);
    await page.waitForFunction(() => repairReports.length === 1);
    assert.deepEqual((await page.evaluate(() => repairReports[0])).slice(0, 4), [777, '', 0, bad + '\n']);
    await page.waitForFunction(() => document.querySelector('.diagram-status').textContent === 'Repairing diagram…');
    await page.evaluate(() => bloom.receive([['diagram_update', {id: 777, repair: {status: 'stopped'}}]]));
    assert.match(await page.locator('.diagram-status').textContent(), /repair stopped/);
    assert.match(await page.locator('#column .body').textContent(), /Before\.[\s\S]*After\./);
    await page.evaluate(markup => bloom.receive([['diagram_update', {id: 777,
      html: '<p>Before.</p>' + markup + '<p>After.</p>', repair: {status: 'updated'}}]]), html(bad.replace('\\n', '\n')));
    await rendered();
    assert.equal(await page.locator('#column .msg.assistant').count(), 1);
    assert.match(await page.locator('#column .body').textContent(), /Before\.[\s\S]*After\./);
    assert.equal(await page.evaluate(() => repairReports.length), 1);
    // Resource/configuration/runtime failures never trigger model repair.
    for (const source of ['flowchart LR\n' + 'A'.repeat(20001),
      '%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B']) {
      await show(html(source), 778);
      await page.waitForFunction(() => document.querySelector('.diagram-status')?.textContent.startsWith('Diagram unavailable:'));
      assert.equal(await page.evaluate(() => repairReports.length), 1);
    }
    // A missing editable-install bundle keeps source usable.
    await page.reload();
    await page.route('**/mermaid-host.html', route => route.abort());
    await page.clock.install();
    await show(markup);
    await page.waitForFunction(() => document.querySelector('.diagram-renderer'));
    await page.clock.runFor(21000);
    assert.match(await page.locator('.diagram-status').textContent(), /unavailable/);
    assert(await page.locator('figure[data-mermaid] pre').isVisible());
    console.log('Mermaid browser checks passed: full types, streaming, source/copy/expand, isolation, failures, reload, paging/block path, narrow layout, no outbound requests.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
