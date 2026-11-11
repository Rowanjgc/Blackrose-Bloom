/* Exercise the file-replacement review: marked changes, spoken labels, keyboard reach, full texts and approval. */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
// The page renders the comparison the host computes, so build it with the real module.
const python = process.env.PYTHON || [path.join(root, '.venv/bin/python'), 'python3'].find(p => p === 'python3' || fs.existsSync(p));
const before = Array.from({length: 1000}, (_, i) => `Line ${i + 1}: <b>ordinary</b> text\r\n`).join('');
const after = before.replace('Line 640: <b>ordinary</b> text\r\n', 'Line 640: <img src=x onerror="window.injected=1"> changed\r\n')
  .replace('Line 900: <b>ordinary</b> text\r\n', 'Line 900: <b>ordinary</b> text\n');
const comparison = JSON.parse(execFileSync(python, ['-c',
  'import json, sys; from blackrose.edit_review import compare; d = json.load(sys.stdin); print(json.dumps(compare(d[0], d[1])))'],
  {input: JSON.stringify([before, after]), env: {...process.env, PYTHONPATH: path.join(root, 'src')}}).toString());
const review = {path: 'notes/report.md', before, after, approval_id: 'a1', comparison};
const created = '# New notes\n<script>window.injected=2</script>\n';
const creation = {path: '/work/notes/new.md', before: '', after: created, approval_id: 'c1', operation: 'create',
  comparison: JSON.parse(execFileSync(python, ['-c',
    'import json, sys; from blackrose.edit_review import compare; print(json.dumps(compare("", sys.stdin.read())))'],
    {input: created, env: {...process.env, PYTHONPATH: path.join(root, 'src')}}).toString())};

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1180, height: 900}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(([review, creation]) => {
      const emit = (kind, data) => window.bloom.receive([[kind, data]]);
      window.reviewTest = {emit, calls: []};
      window.pywebview = {api: {
        ready: async () => {
          setTimeout(() => emit('status', {state: 'Ready', generating: false}), 0);
          return {commands: [], keymap: []};
        },
        review_edit: async (...args) => {
          window.reviewTest.calls.push(args);
          const target = args[0] === 'c1' ? creation : review;
          return args.length > 1 ? {path: target.path} : target;
        },
        render_transcript: async () => true,
        save_layout: async () => true,
        update_draft: async () => true,
        reference_suggestions: async () => [],
        workspace_browse: async () => ({entries: []}),
        edit_backups: async (...args) => {
          window.reviewTest.calls.push(['edit_backups', ...args]);
          if (args[0] === 'restore') return {path: '/work/notes.md', backup: '/data/edit-backups/1/b-notes.md'};
          if (args[0] === 'preview') return {text: '<img src=x onerror="window.injected=3">before', truncated: false};
          const item = (id, conflict) => ({id, path: '/work/notes.md', filename: 'notes.md', location: `/data/edit-backups/1/${id}-notes.md`,
            stored: true, size: 6, conflict, created_at: '2026-10-30T12:00:00+00:00', restored_at: null});
          return {store: '/data/edit-backups', retention_days: 30, items: [item('b1', true)], deleted_chats: [item('b2', false)]};
        },
      }};
    }, [review, creation]);
    await page.goto(pathToFileURL(path.join(root, 'src/blackrose/gui/static/index.html')).href);
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === 'Ready');
    await page.evaluate(() => window.reviewTest.emit('edit_approval', {approval_id: 'a1', path: 'notes/report.md'}));
    await page.getByRole('button', {name: 'Review edit'}).click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    assert.equal(await page.locator('#dialog-title').textContent(), 'Replace notes/report.md?');

    // The summary, endings and both changes are reachable without reading the full texts.
    assert.equal(await dialog.getAttribute('aria-describedby'), 'edit-review-summary');
    assert.equal(await page.locator('#edit-review-summary').textContent(), '2 lines removed, 2 lines added in 2 places.');
    assert.match(await page.locator('.edit-review-notes').textContent(), /1 line changes only its line ending/);
    assert.match(await page.locator('.source-facts').textContent(),
      /notes\/report\.md.*private backup store, outside this folder.*Workspace → Edit backups.*changes after this review/);
    const changes = page.getByRole('region', {name: 'Changes'});
    assert.equal(await changes.locator('h3').count(), 2);
    assert.match(await changes.locator('h3').first().textContent(), /Change 1 of 2 · current lines 637–643, proposed lines 637–643/);
    assert.equal(await changes.locator('del').count(), 2);
    assert.equal(await changes.locator('ins').count(), 2);
    const added = changes.locator('ins').first();
    assert.equal(await added.textContent(), 'Added, line 640: Line 640: <img src=x onerror="window.injected=1"> changed');
    assert.equal(await added.locator('img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.equal(await changes.locator('del').nth(1).textContent(), 'Removed, line 900: Line 900: <b>ordinary</b> text [CRLF line ending]');
    assert.equal(await changes.locator('ins').nth(1).textContent(), 'Added, line 900: Line 900: <b>ordinary</b> text [LF line ending]');
    // Signs are visible text, not color alone.
    assert.deepEqual(await changes.locator('.diff-row.removed .diff-gutter:nth-child(3)').allTextContents(), ['−', '−']);
    assert.deepEqual(await changes.locator('.diff-row.added .diff-gutter:nth-child(3)').allTextContents(), ['+', '+']);

    // Cancel starts focused; Tab reaches the comparison, the full texts and the decision.
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Cancel');
    const order = [];
    for (let i = 0; i < 4; i++) {
      await page.keyboard.press('Tab');
      order.push(await page.evaluate(() => document.activeElement.getAttribute('aria-label') || document.activeElement.textContent));
    }
    assert.deepEqual(order, ['Approve replacement', 'Changes', 'Current file: full text', 'Proposed replacement: full text']);
    const box = await dialog.boundingBox();
    const actions = await page.locator('#dialog-actions').boundingBox();
    assert(actions.y + actions.height <= box.y + box.height + 1, 'decision stays visible while the comparison scrolls');
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('.edit-review-full').nth(1).locator('pre').textContent(), after);
    if (process.env.EDIT_REVIEW_SCREENSHOTS) {
      fs.mkdirSync(process.env.EDIT_REVIEW_SCREENSHOTS, {recursive: true});
      await dialog.screenshot({path: path.join(process.env.EDIT_REVIEW_SCREENSHOTS, 'edit-review.png')});
    }

    // Narrow layouts keep the dialog on-screen; approval clears the review state.
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.getByRole('button', {name: 'Approve replacement'}).click();
    await page.waitForFunction(() => window.reviewTest.calls.some(c => c[1] === true));
    await page.waitForFunction(() => !document.querySelector('[data-approval="a1"]'));
    assert.equal(await page.evaluate(() => document.querySelector('#dialog').classList.contains('edit-review-dialog')), false);
    assert.equal(await page.evaluate(() => document.querySelector('#dialog').hasAttribute('aria-describedby')), false);

    // A proposed new file names its destination, replaces nothing and shows only its own text.
    await page.evaluate(() => window.reviewTest.emit('edit_approval', {approval_id: 'c1', path: '/work/notes/new.md', operation: 'create'}));
    assert.match(await page.locator('[data-approval="c1"]').textContent(), /new file · \/work\/notes\/new\.md/);
    await page.getByRole('button', {name: 'Review new file'}).click();
    await dialog.waitFor();
    assert.equal(await page.locator('#dialog-title').textContent(), 'Create /work/notes/new.md?');
    const facts = await page.locator('.source-facts').textContent();
    assert.match(facts, /New file.*\/work\/notes\/new\.md.*refused if a file appears/);
    assert.doesNotMatch(facts, /backup/);
    assert.deepEqual(await page.locator('.edit-review-full summary').allTextContents(), ['Proposed file: full text']);
    assert.equal(await page.locator('.edit-review-full pre').textContent(), created);
    assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.getByRole('button', {name: 'Approve new file'}).click();
    await page.waitForFunction(() => window.reviewTest.calls.some(c => c[0] === 'c1' && c[1] === true));
    await page.waitForFunction(() => !document.querySelector('[data-approval="c1"]'));

    // Backups are listed from Workspace; preview text is inert and restore needs a confirmation.
    await page.locator('#workspace-button').click();
    await page.getByRole('button', {name: 'Edit backups'}).click();
    await page.waitForFunction(() => document.querySelector('#dialog-title').textContent === 'Edit backups');
    assert.match(await page.locator('#dialog-detail').textContent(), /\/data\/edit-backups, outside your project folders.*From deleted chats \(1\)/);
    const rows = page.locator('.workspace-file');
    assert.match(await rows.first().textContent(), /notes\.md · .* · changed during approval/);
    // A backup from a deleted chat can be previewed or removed, never restored into another chat.
    assert.deepEqual(await rows.nth(1).locator('button').allTextContents(), ['Preview', 'Remove']);
    await rows.first().getByRole('button', {name: 'Preview'}).click();
    await page.waitForFunction(() => document.querySelector('#dialog-title').textContent === 'Backup of notes.md');
    assert.match(await page.locator('#dialog-detail').textContent(), /<img src=x onerror="window\.injected=3">before/);
    assert.equal(await page.locator('#dialog-detail img').count(), 0);
    await page.getByRole('button', {name: 'Back'}).click();
    await page.locator('.workspace-file').first().getByRole('button', {name: 'Restore'}).click();
    await page.waitForFunction(() => document.querySelector('#dialog-title').textContent === 'Restore notes.md?');
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Cancel');
    await page.locator('#dialog-actions').getByRole('button', {name: 'Restore'}).click();
    await page.waitForFunction(() => window.reviewTest.calls.some(c => c[0] === 'edit_backups' && c[1] === 'restore' && c[2] === 'b1'));
    await page.waitForFunction(() => document.querySelector('#dialog-title').textContent === 'Edit backups');
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.deepEqual(errors, []);
    console.log('Edit review browser passed: marked changes in a 1000-line CRLF file, ending-only change, spoken labels, inert file text, keyboard order, pinned decision, full texts, narrow width, approval cleanup, new-file proposal, edit backup listing, inert preview and confirmed restore.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
