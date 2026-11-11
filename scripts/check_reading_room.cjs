/* Exercise the actual Reading Room page with a scripted desktop bridge.
 * PLAYWRIGHT_MODULE and CHROMIUM_PATH can point at existing installations.
 * READING_ROOM_SCREENSHOTS optionally captures the checked layouts.
 */
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const items = [{id: 1, title: 'The shape of a good answer'},
        {id: 2, title: 'Notes on local knowledge'},
        {id: 3, title: 'A very long conversation title about revisiting sources, recovering drafts, and keeping a line of thought alive'}];
      const messages = [{role: 'user', text: 'What changes when we treat an AI conversation as a place to think?'},
        {role: 'assistant', label: 'Blackrose', model: 'Qwen 3.6', html: '<p>The center of gravity shifts from the answer to the inquiry. A useful conversation leaves room for a thought to develop: a question, a tentative explanation, a return to the evidence.</p><h2>Continuity before certainty</h2><p>A good thinking space makes it easy to return, reconsider, and follow a thread. The interface can support that with a steady reading column, clear attribution, and a quiet place for the next question.</p><blockquote>Let the typography carry the thought.<br>Let the interface leave room for it.</blockquote>'}];
      let current = 1;
      const emit = (kind, data) => window.bloom.receive([[kind, data]]);
      const calls = [];
      const load = () => {emit('conversations', {items, current}); emit('transcript', {messages});};
      window.roomTest = {calls, emit};
      window.pywebview = {api: {
        ready: async () => {
          setTimeout(() => {load(); emit('status', {state: 'Ready', availability: 'Gemma 4 E4B IT Ready', request_state: 'complete', generating: false, model: 'gemma-4-e4b-it', model_display_name: 'Gemma 4 E4B IT', response_model: 'Qwen 3.6', can_retry: true, knowledge: true, web: false});
            emit('metrics', {context_used: 1224, admission_input_bound: 4800, output_reserve: 16000, context_window: 128000, tokens_per_second: null});}, 0);
          return {commands: [], keymap: [{keys: ['ctrl+\\'], action: 'toggle_sidebar'}, {keys: ['ctrl+r'], action: 'retry'}, {keys: ['ctrl+m'], action: 'open_models'}]};
        },
        models: async () => {calls.push(['models']); return {selected: 'Qwen 3.6', default: 'Qwen 3.6',
          endpoint: 'Scripted test provider', items: [{id: 'Qwen 3.6'}, {id: 'Another model'}]};},
        select_model: async () => new Promise(resolve => {window.roomTest.finishLoading = () => resolve({ok: true});}),
        review_library_add: async (token, approve, reject) => {
          calls.push(['review_library_add', token, approve, reject]);
          if (approve) return {document_id: 'saved-document', status: 'imported'};
          if (reject) return {status: 'rejected'};
          return {...window.roomTest.proposal, text: '<script>window.libraryAttack=true</script>\n<img src="https://example.org/tracker">\nDatabase reference text'};
        },
        save_layout: async data => {calls.push(['save_layout', data]); return true;},
        open_conversation: async id => {calls.push(['open_conversation', id]); current = id; load(); return true;},
        update_draft: async () => true,
        copy: async text => {calls.push(['copy', text]); return !window.roomTest.copyFails;},
        retry: async () => {
          calls.push(['retry']); emit('status', {generating: true, can_retry: false});
          setTimeout(() => emit('status', {generating: false, can_retry: true}), 150);
          return true;
        },
      }};
    });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../src/blackrose/gui/static/index.html')).href);
    await page.locator('#status-text').getByText('Ready', {exact: true}).waitFor();
    assert((await page.locator('#composer-legend').textContent()).includes('Drop images or documents'));
    assert.equal(await page.locator('#model-availability').textContent(), 'Gemma 4 E4B IT Ready');
    assert.equal(await page.locator('#model-value').textContent(), 'Gemma 4 E4B IT');
    assert.equal(await page.locator('#active-model').textContent(), 'Qwen 3.6');
    await page.waitForFunction(() => document.querySelector('.brand-logo')?.naturalWidth > 0);
    assert.equal(await page.locator('.brand-logo').getAttribute('src'), 'blackrose-bloom-logo.png');
    await page.evaluate(() => document.fonts.ready);
    assert.deepEqual(await page.locator('.new-chat kbd').evaluate(el => {
      const css = getComputedStyle(el);
      return [css.paddingTop, css.paddingRight, css.paddingBottom, css.paddingLeft];
    }), ['2.4px', '4.8px', '2.4px', '4.8px'], 'New chat shortcut keeps its compact padding');
    const toggle = page.getByRole('button', {name: 'Hide conversations', exact: true});
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('.chat-item').count(), 3);
    const metrics = async () => {
      assert.equal(await page.locator('#context-value').textContent(), 'Context 1,224 / 128,000');
      assert.equal(await page.locator('#generation-speed').textContent(), '— tokens/s');
      assert(await page.locator('#context-value').isVisible());
      assert(await page.locator('#generation-speed').isVisible());
    };
    await metrics();
    await page.locator('#input').fill('Keep my next question');
    await page.locator('#retry').click();
    assert(await page.locator('#retry').isDisabled());
    await page.locator('#retry').waitFor({state: 'visible'});
    await page.waitForFunction(() => !document.querySelector('#retry').disabled);
    await page.keyboard.press('Control+r');
    await page.waitForFunction(() => window.roomTest.calls.filter(([name]) => name === 'retry').length === 2);
    assert.equal(await page.locator('#input').inputValue(), 'Keep my next question');
    await page.waitForFunction(() => !document.querySelector('#retry').disabled);
    await page.locator('#input').fill('');
    const capture = async name => {
      if (process.env.READING_ROOM_SCREENSHOTS) {
        fs.mkdirSync(process.env.READING_ROOM_SCREENSHOTS, {recursive: true});
        await page.screenshot({path: path.join(process.env.READING_ROOM_SCREENSHOTS, name + '.png')});
      }
    };
    const fits = async () => {
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.waitForFunction(() => {
        const input = document.querySelector('#input');
        return input.scrollHeight <= input.clientHeight;
      });
      assert(await page.evaluate(() => {
        const box = document.querySelector('.chat-metrics').getBoundingClientRect();
        return box.bottom <= innerHeight && box.left >= 0 && box.right <= innerWidth;
      }), 'Metrics must remain inside the viewport');
    };
    await fits(); await capture('desktop-conversations');
    await page.locator('#menu-toggle').click();
    await page.waitForFunction(() => document.querySelector('#sidebar').inert && document.querySelector('#app').classList.contains('is-settled'));
    assert.equal(await page.locator('#menu-toggle').getAttribute('aria-expanded'), 'false');
    await metrics(); await capture('desktop-reading');
    await page.keyboard.press('Control+\\');
    await page.waitForFunction(() => !document.querySelector('#sidebar').inert && document.querySelector('#app').classList.contains('is-settled'));
    await page.locator('.chat-item').nth(1).click();
    assert.equal(await page.locator('#title').textContent(), 'Notes on local knowledge');
    assert.equal(await page.locator('.chat-item').nth(1).getAttribute('aria-current'), 'true');
    const handle = page.locator('#resizer');
    await handle.focus();
    const before = await page.locator('#sidebar').evaluate(el => el.getBoundingClientRect().width);
    await page.keyboard.press('ArrowLeft');
    await page.waitForFunction(width => document.querySelector('#sidebar').getBoundingClientRect().width < width, before);
    await page.waitForFunction(() => document.querySelector('#app').classList.contains('is-settled'));
    assert(await page.evaluate(() => window.roomTest.calls.some(([name]) => name === 'save_layout')));
    await page.locator('#help-button').click();
    await page.locator('#dialog').waitFor({state: 'visible'});
    assert(await page.locator('#app').evaluate(el => el.inert));
    // Native WebKitGTK reports ISO_Left_Tab as an unidentified logical key.
    // Keep reverse traversal inside the modal using the physical Tab code.
    await page.evaluate(() => {
      const controls = [...document.querySelectorAll('#dialog button, #dialog a[href]')]
        .filter(control => control.getClientRects().length);
      controls[0].focus();
      const event = new KeyboardEvent('keydown', {key: 'Unidentified', code: 'Tab',
        shiftKey: true, bubbles: true, cancelable: true});
      controls[0].dispatchEvent(event);
      if (!event.defaultPrevented || document.activeElement !== controls.at(-1)) {
        throw new Error('WebKit reverse Tab escaped the modal focus loop');
      }
    });
    await page.keyboard.press('Escape');
    for (const width of [1024, 760, 390]) {
      await page.setViewportSize({width, height: 844});
      await fits(); await metrics();
      if (width <= 760) {
        await page.waitForFunction(() => document.querySelector('#menu-toggle').getAttribute('aria-expanded') === 'false');
        await page.locator('#menu-toggle').click();
        await page.waitForFunction(() => document.querySelector('#app').classList.contains('is-overlay-open') && document.querySelector('#app').classList.contains('is-settled'));
        const top = await page.locator('#sidebar').evaluate(el => el.getBoundingClientRect().top);
        const bottom = await page.locator('#app-toolbar').evaluate(el => el.getBoundingClientRect().bottom);
        assert(top >= bottom, 'Drawer must clear the wrapping toolbar');
        await capture(`drawer-${width}`);
        await page.locator('.chat-item').first().click();
        await page.waitForFunction(() => !document.querySelector('#app').classList.contains('is-overlay-open') && document.querySelector('#sidebar').inert && document.querySelector('#app').classList.contains('is-settled'));
        await fits();
      }
      await capture(`reading-${width}`);
    }
    await page.evaluate(() => {roomTest.emit('status', {generating: true}); roomTest.emit('metrics', {context_used: 28000, context_estimated: true, admission_input_bound: 112000, output_reserve: 16000, context_window: 128000, tokens_per_second: 45.2});});
    // The meter shows measured usage; a full admission budget appears only in the tooltip.
    assert.equal(await page.locator('#context-value').textContent(), 'Context ≈28,000 / 128,000');
    assert.equal(await page.locator('#context-metric').getAttribute('data-level'), 'normal');
    assert.match(await page.locator('#context-metric').getAttribute('title'), /^Approximate tokens, including instructions/);
    assert.match(await page.locator('#context-metric').getAttribute('title'), /Admission budget: conservative input bound 112,000 \+ output reserve 16,000 = 128,000 tokens; requests exceeding it are refused\./);
    // Exact provider usage changes the label and tooltip; the warning level follows usage.
    await page.evaluate(() => roomTest.emit('metrics', {context_used: 120000, context_estimated: false,
      admission_input_bound: 112000, output_reserve: 16000, context_window: 128000, tokens_per_second: 45.2}));
    assert.equal(await page.locator('#context-value').textContent(), 'Context 120,000 / 128,000');
    assert.equal(await page.locator('#context-metric').getAttribute('data-level'), 'high');
    assert.match(await page.locator('#context-metric').getAttribute('title'), /^Provider-reported input and output tokens/);
    assert.match(await page.locator('#context-metric').getAttribute('title'), /93\.8% of context window used\./);
    await capture('context-budget-limit');
    assert.equal(await page.locator('#generation-speed').textContent(), '45.2 tokens/s');
    await page.evaluate(() => roomTest.emit('status', {generating: false}));
    assert.equal(await page.locator('#generation-speed').textContent(), '— tokens/s');
    await page.evaluate(() => {roomTest.emit('transcript', {messages: []}); roomTest.emit('status', {can_retry: false});});
    assert(await page.locator('#retry').isDisabled());
    await page.locator('.welcome').waitFor(); await fits(); await capture('welcome-mobile');
    await page.emulateMedia({reducedMotion: 'reduce'});
    await page.locator('#menu-toggle').click();
    await page.waitForFunction(() => !document.querySelector('#sidebar').inert);
    // Review must remain safe and reachable at both reading widths, even with
    // hostile source labels and a filename without natural wrapping points.
    await page.keyboard.press('Escape');
    await page.evaluate(() => {
      roomTest.proposal = {approval_id: 'library-test', approval_kind: 'library',
        title: 'Database reference ' + 'x'.repeat(150), tags: ['reference'],
        origin: 'Web capture', truncated: true, source_url: 'https://example.org/database',
        source_metadata: {document_type: 'web_capture', capture_completeness: 'partial',
          retrieved_at: '2026-10-15T12:00:00+00:00'}};
      roomTest.emit('library_approval', roomTest.proposal);
      roomTest.emit('status', {generating: true});
    });
    assert(await page.locator('.library-review').isDisabled());
    await page.evaluate(() => roomTest.emit('status', {generating: false}));
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 900});
      await page.locator('.library-review').click();
      await page.getByRole('button', {name: 'Add to Library', exact: true}).waitFor();
      await page.waitForFunction(() => parseFloat(getComputedStyle(document.querySelector('#dialog')).opacity) > .99);
      assert.equal(await page.locator('#dialog-detail script, #dialog-detail img').count(), 0);
      assert.equal(await page.evaluate(() => !!window.libraryAttack), false);
      assert((await page.locator('#dialog-detail').textContent()).includes('<script>'));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert(await page.evaluate(() => {
        const box = document.querySelector('#dialog').getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth;
      }));
      await capture(`library-review-${width}`);
      await page.getByRole('button', {name: 'Cancel', exact: true}).click();
    }
    await page.locator('.library-review').click();
    await page.getByRole('button', {name: 'Add to Library', exact: true}).click();
    await page.getByRole('button', {name: 'Open in Library', exact: true}).waitFor();
    assert(await page.locator('.library-proposal').getByText('Saved to Library', {exact: true}).isVisible());
    assert.deepEqual(await page.evaluate(() => roomTest.calls.filter(c => c[0] === 'review_library_add' && c[2])),
      [['review_library_add', 'library-test', true, undefined]]);
    await page.keyboard.press('Escape');
    await page.setViewportSize({width: 1440, height: 1000});
    // Loading must respect dismissal, replacement, and an open picker.
    for (const destination of ['closed', 'help', 'models']) {
      await page.keyboard.press('Control+m');
      await page.getByRole('button', {name: 'Browse all models'}).click();
      await page.locator('.model-catalog .model-choice').first().click();
      if (destination !== 'models') await page.keyboard.press('Escape');
      if (destination === 'help') await page.locator('#help-button').click();
      const count = await page.evaluate(() => roomTest.calls.filter(c => c[0] === 'models').length);
      await page.evaluate(() => roomTest.finishLoading());
      if (destination === 'models') {
        await page.waitForFunction(n => roomTest.calls.filter(c => c[0] === 'models').length > n, count);
        assert.equal(await page.locator('#dialog-title').textContent(), 'Models');
        await page.keyboard.press('Escape');
      } else {
        await page.waitForTimeout(150);
        assert.equal(await page.evaluate(() => roomTest.calls.filter(c => c[0] === 'models').length), count);
        if (destination === 'help') {
          assert.equal(await page.locator('#dialog-title').textContent(), 'Keys and commands');
          await page.keyboard.press('Escape');
        } else assert(await page.locator('#scrim').evaluate(el => el.hidden));
      }
    }
    // Reasoning-only and tool-repair failures keep an obvious terminal notice.
    const contextError = 'Blackrose could not fit this request within its context safety budget. Estimated input: 34,000 tokens, plus 4,000 reserved for output; configured model context: 32,000 tokens. This is a conservative host estimate, not a measured model overflow. Tool results and any repair history are included. Start a new chat with the original request, or choose a model with a larger context.';
    for (const [state, error] of [['error', 'The model returned no visible answer'],
      ['error', 'Animation creation failed; no animation was saved.'], ['error', contextError],
      ['cancelled', null], ['pending', null], ['complete', null]]) {
      await page.evaluate(({state, error}) => {
        roomTest.emit('transcript', {messages: []});
        roomTest.emit('reply_start', {key: 99, model: 'Scripted test model'});
        roomTest.emit('status', {state: 'Generating…', generating: true});
        roomTest.emit('reasoning', {key: 99, text: 'Considering the request.'});
        roomTest.emit('tool_activity', {key: 99, name: 'animation_create', phase: 'error', error: 'Example validation diagnostic', sequence: 0});
        roomTest.emit('tool_activity', {key: 99, name: 'animation_create', phase: 'repairing'});
        roomTest.emit('metrics', {context_used: 1224, admission_input_bound: 4800, output_reserve: 16000, context_window: 128000, tokens_per_second: 31.5});
        roomTest.emit('reply_end', {key: 99, state, error, diagnostics: state === 'complete' ? null : `Host diagnostics: ${state}`,
          html: state === 'complete' ? '<p>Finished.</p>' : ''});
        roomTest.emit('status', {state: 'Ready', request_state: state, availability: 'Gemma 4 E4B IT Ready', generating: false});
      }, {state, error});
      assert.equal(await page.locator('.thinking summary').textContent(), 'Reasoning');
      assert.equal(await page.locator('.tool-activity').count(), 0);
      assert.equal(await page.locator('.is-streaming').count(), 0);
      assert.equal(await page.locator('#generation-speed').textContent(), '— tokens/s');
      const outcome = {error: 'Reply failed', cancelled: 'Reply stopped', pending: 'Reply interrupted', complete: 'Reply complete'}[state];
      assert.equal(await page.locator('#status-text').textContent(), outcome);
      assert.equal(await page.locator('#reply-announcement').textContent(), `${outcome}.`);
      if (state === 'complete') assert.equal(await page.locator('.copy-diagnostics').count(), 0);
      else {
        await page.getByRole('button', {name: 'Copy diagnostics', exact: true}).focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => document.querySelector('#toast').textContent === 'Diagnostics copied');
        assert.equal(await page.evaluate(() => roomTest.calls.filter(([name]) => name === 'copy').at(-1)[1]), `Host diagnostics: ${state}`);
        await page.evaluate(() => {roomTest.copyFails = true;});
        await page.getByRole('button', {name: 'Copy diagnostics', exact: true}).click();
        await page.waitForFunction(() => document.querySelector('#toast').textContent === 'Could not copy diagnostics');
        assert(await page.getByRole('button', {name: 'Copy diagnostics', exact: true}).isEnabled());
        await page.evaluate(() => {roomTest.copyFails = false;});
      }
      await page.locator('.tool-log summary').click();
      assert(await page.getByText('Preparing animation · Example validation diagnostic', {exact: true}).isVisible());
      for (const width of [1440, 390]) {
        await page.setViewportSize({width, height: 1000});
        await fits();
        if (error === contextError) await capture(`context-failure-${width}`);
      }
      // Reopening a conversation restores its useful output and keeps old
      // diagnostics available without reviving a failure in the status bar.
      await page.evaluate(({state, error}) => {
        roomTest.emit('transcript', {messages: [
          {role: 'assistant', state, error, diagnostics: state === 'complete' ? null : `Saved host diagnostics: ${state}`,
            html: '<p>A useful saved answer.</p>'}]});
        roomTest.emit('status', {state: 'Ready', request_state: state, generating: false});
      }, {state, error});
      assert.equal(await page.locator('#status-text').textContent(), 'Ready');
      assert.equal(await page.locator('#status').getAttribute('data-tone'), 'idle');
      assert(await page.getByText('A useful saved answer.', {exact: true}).isVisible());
      if (state === 'complete') assert.equal(await page.locator('.reply-note').count(), 0);
      else {
        assert.equal(await page.locator('.reply-note').getAttribute('open'), null);
        assert.equal(await page.locator('.reply-note summary').textContent(), 'Reply details');
        assert.equal(await page.locator('.reply-diagnostic').isVisible(), false);
        assert.equal(await page.locator('.copy-diagnostics').isVisible(), false);
        for (const width of [1440, 390]) {
          await page.setViewportSize({width, height: 1000});
          await fits();
          if (error === contextError) await capture(`historical-failure-${width}`);
        }
        await page.locator('.reply-note summary').focus();
        await page.keyboard.press('Enter');
        assert((await page.locator('.reply-diagnostic').textContent()).startsWith(outcome));
        assert(await page.locator('.reply-diagnostic').isVisible());
        if (error) assert((await page.locator('.reply-diagnostic').textContent()).includes(error));
        await page.keyboard.press('Tab');
        assert(await page.locator('.copy-diagnostics').evaluate(button => button === document.activeElement));
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => document.querySelector('#toast').textContent === 'Diagnostics copied');
        assert.equal(await page.evaluate(() => roomTest.calls.filter(([name]) => name === 'copy').at(-1)[1]), `Saved host diagnostics: ${state}`);
        await page.evaluate(() => roomTest.emit('user_message', {text: 'Continue from here'}));
        assert.equal(await page.locator('.reply-diagnostic').isVisible(), false);
      }
    }
    // A fresh failure can be folded immediately, and a follow-up folds it too.
    await page.evaluate(() => {
      roomTest.emit('reply_start', {key: 100});
      roomTest.emit('reply_end', {key: 100, state: 'error', html: '<p>Partial but useful.</p>'});
      roomTest.emit('status', {state: 'Ready', request_state: 'error', generating: false});
    });
    assert(await page.locator('.reply-diagnostic').isVisible());
    assert((await page.locator('.reply-diagnostic').textContent()).includes('keep using the output above'));
    await page.locator('.reply-note summary').click();
    assert.equal(await page.locator('.reply-diagnostic').isVisible(), false);
    await page.locator('.reply-note summary').click();
    await page.evaluate(() => roomTest.emit('reply_start', {key: 101}));
    assert.equal(await page.locator('.reply-diagnostic').isVisible(), false);
    // Admission failures have no saved reply: their restored notices follow
    // the same quiet history policy, with the diagnostic and draft retained.
    await page.evaluate(contextError => roomTest.emit('snapshot', {
      messages: [], request_notice: `<p>${contextError}</p>`, request_diagnostics: 'Unstored host diagnostics', draft: 'Recoverable request',
      status: {state: 'Ready', request_state: 'error', generating: false},
    }), contextError);
    assert.equal(await page.locator('#status-text').textContent(), 'Ready');
    assert.equal(await page.locator('#input').inputValue(), 'Recoverable request');
    assert.equal(await page.locator('.notice').getAttribute('open'), null);
    assert.equal(await page.locator('.notice .prose').isVisible(), false);
    assert.equal(await page.locator('.copy-diagnostics').isVisible(), false);
    await page.locator('.notice summary').click();
    assert((await page.locator('.notice .prose').textContent()).includes(contextError));
    assert(await page.locator('.notice .prose').isVisible());
    await page.getByRole('button', {name: 'Copy diagnostics', exact: true}).click();
    await page.waitForFunction(() => roomTest.calls.filter(([name]) => name === 'copy').at(-1)[1] === 'Unstored host diagnostics');
    await page.evaluate(() => roomTest.emit('user_message', {text: 'Try again'}));
    assert.equal(await page.locator('.notice .prose').isVisible(), false);
    // Multiple exchanges must read as distinct speakers and groups, including
    // a narrow pane caused by the sidebar rather than the viewport alone.
    await page.evaluate(() => roomTest.emit('transcript', {messages: [
      {role: 'user', text: 'How does local knowledge differ from memory?'},
      {role: 'assistant', label: 'Blackrose', model: 'Qwen 3.6', html:
        '<p><strong>Local knowledge is material you can return to. Memory is context carried between conversations.</strong></p><p>The library lets you inspect the original document. Memory helps recall earlier discussions; those notes can need correction.</p>',
        sources: [{title: 'Context notes', url: 'https://example.org/context'}]},
      {role: 'user', text: 'Can I use both in the same conversation?\n' + '/long-path'.repeat(35)},
      {role: 'assistant', label: 'Blackrose', model: 'model_' + 'x'.repeat(100), html:
        '<p>Yes. Use the library for source material and memory for continuity.</p><details><summary>More detail</summary><p>Return to the source when a detail matters.</p></details>'},
      {role: 'user', text: '', attachments: [{kind: 'document', filename: 'reference.txt', label: 'Reference document'}]},
      {role: 'assistant', html: '<p>The attached reference is available.</p><pre><code>example code</code></pre><table><tr><th>Source</th><th>Purpose</th></tr><tr><td>Library</td><td>Evidence</td></tr></table>'},
    ]}));
    for (const [width, font, wideSidebar] of [[1440, 16, false], [1024, 16, true],
      [390, 16, false], [768, 32, false]]) {
      await page.setViewportSize({width, height: 1000});
      if (wideSidebar && await page.locator('#menu-toggle').getAttribute('aria-expanded') === 'false') {
        await page.locator('#menu-toggle').click();
        await page.waitForFunction(() => document.querySelector('#app').classList.contains('is-settled'));
      }
      await page.evaluate(font => {
        document.documentElement.style.fontSize = `${font}px`;
      }, font);
      if (wideSidebar) {
        await page.locator('#resizer').press('End');
        await page.waitForFunction(() => document.querySelector('#main').clientWidth <= 600);
      }
      await page.waitForFunction(() => document.querySelector('#app').classList.contains('is-settled'));
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const layout = await page.evaluate(() => {
        const column = document.querySelector('#column'), c = column.getBoundingClientRect();
        const messages = [...column.querySelectorAll(':scope > .msg')];
        const boxes = messages.map(m => m.getBoundingClientRect());
        return {
          transparent: getComputedStyle(column).backgroundColor === 'rgba(0, 0, 0, 0)' &&
            getComputedStyle(column).backgroundImage === 'none',
          paper: messages.every(m => getComputedStyle(m).backgroundImage !== 'none'),
          speakers: messages.every((m, i) => m.classList.contains('user')
            ? Math.abs(boxes[i].right - c.right) <= 1 && boxes[i].width < c.width - 1
            : Math.abs(boxes[i].left - c.left) <= 1 && Math.abs(boxes[i].width - c.width) <= 1),
          gaps: [boxes[1].top - boxes[0].bottom, boxes[2].top - boxes[1].bottom],
          clipped: [...column.querySelectorAll('.msg, .bubble, .body, .msg-head, .attachment')]
            .filter(m => m.scrollWidth > m.clientWidth + 1).map(m => m.className),
          pane: document.querySelector('#main').clientWidth,
        };
      });
      assert(layout.transparent, 'Conversation canvas must not join the message surfaces');
      assert(layout.paper, 'Each message surface must retain the paper texture');
      assert(layout.speakers, `Prompt/answer alignment failed at ${width}px / ${font}px: ${JSON.stringify(layout)}`);
      assert(layout.gaps[1] > layout.gaps[0] * 1.5, 'A new exchange needs more separation than its prompt and answer');
      assert.deepEqual(layout.clipped, [], `Message content clipped at ${width}px / ${font}px`);
      if (wideSidebar) assert(layout.pane <= 600, `Fixture must exercise the narrow-pane styles on a desktop viewport: ${JSON.stringify(layout)}`);
      await fits();
      await page.locator('.answer-sources > summary').click();
      assert(await page.locator('.answer-sources .source-row').isVisible());
      await page.locator('.answer-sources > summary').click();
      await page.evaluate(() => {document.querySelector('#transcript').scrollTop = 0; document.querySelector('#input').blur();});
      await capture(`separate-surfaces-${width}-${font}`);
    }
    await page.evaluate(() => {
      document.documentElement.style.removeProperty('font-size');
    });
    await page.emulateMedia({contrast: 'more', reducedMotion: 'reduce'});
    assert(await page.locator('.msg').evaluateAll(messages => messages.every(message => {
      const css = getComputedStyle(message);
      return css.backgroundImage === 'none' && css.borderTopColor === 'rgb(110, 110, 110)';
    })), 'Increased contrast removes paper grain and strengthens both message borders');
    await page.emulateMedia({contrast: 'no-preference', reducedMotion: 'reduce'});
    assert.deepEqual(errors, []);
    await page.evaluate(() => roomTest.emit('transcript', {messages: []}));
    await page.waitForFunction(() => document.querySelector('.welcome .brand-logo')?.naturalWidth > 0);
    await capture('dragon-rose-welcome');
    console.log('Reading Room passed: Dragon Rose logos, responsive toolbar/composer, separate prompt/answer surfaces and exchange spacing, long/attachment-only prompts, narrow panes and enlarged text, paper/increased contrast, history/drawer/resize, metrics, modal, reduced motion, library review, Ctrl+M, model-loading dismissal/replacement races, terminal outcomes/diagnostics/announcements at desktop and mobile widths, no JS errors.');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
