/* One lazy sandbox renderer; chat contains only escaped source and passive images. */
(() => {
  'use strict';
  const channel = 'blackrose-mermaid';
  const cache = new Map(), pending = new Map();
  const observations = new Map();
  let cacheBytes = 0, sequence = 0, frame, ready, broken;
  let queue = Promise.resolve();
  new MutationObserver(records => {
    if (!records.some(record => record.removedNodes.length)) return;
    for (const [figure, observer] of observations) {
      if (!figure.isConnected) { observer.disconnect(); observations.delete(figure); }
    }
  }).observe(document.getElementById('column'), {childList: true, subtree: true});
  const element = (tag, cls, text) => {
    const result = document.createElement(tag);
    if (cls) result.className = cls;
    if (text !== undefined) result.textContent = text;
    return result;
  };
  const button = text => {
    const result = element('button', 'diagram-control', text);
    result.type = 'button';
    return result;
  };
  function renderer() {
    if (ready) return ready;
    ready = new Promise((resolve, reject) => {
      frame = element('iframe', 'diagram-renderer');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('aria-hidden', 'true');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.tabIndex = -1;
      frame.title = 'Local diagram renderer';
      const timeout = setTimeout(() => fail(new Error('Diagram renderer unavailable.')), 20000);
      function fail(error) {
        broken = error;
        clearTimeout(timeout);
        frame.remove();
        window.removeEventListener('message', receive);
        reject(error);
        for (const job of pending.values()) job.reject(error);
        pending.clear();
      }
      function receive(event) {
        const data = event.data;
        if (event.source !== frame.contentWindow || data?.channel !== channel) return;
        if (data.type === 'ready') { clearTimeout(timeout); resolve(); return; }
        if (data.type !== 'result' || !Number.isSafeInteger(data.id)) return;
        const job = pending.get(data.id);
        if (!job) return;
        pending.delete(data.id);
        if (typeof data.error === 'string') {
          const error = new Error(data.error.slice(0, 500));
          error.repairable = data.repairable === true;
          job.reject(error);
        }
        else if (typeof data.svg === 'string' && data.svg.length <= 2 * 1024 * 1024
            && new TextEncoder().encode(data.svg).byteLength <= 2 * 1024 * 1024
            && typeof data.label === 'string') job.resolve({svg: data.svg, label: data.label.slice(0, 2000)});
        else job.reject(new Error('Invalid diagram result.'));
      }
      window.addEventListener('message', receive);
      frame.src = 'mermaid-host.html';
      document.body.appendChild(frame);
      frame.addEventListener('error', () => fail(new Error('Diagram renderer unavailable.')), {once: true});
      // A render timeout latches the failed renderer rather than repeatedly
      // reloading a pathological diagram or retaining an active parser.
      frame._fail = fail;
    });
    return ready;
  }
  function remember(source, result) {
    const bytes = (source.length + result.svg.length + result.label.length) * 2;
    if (bytes > 8 * 1024 * 1024) return;
    cache.set(source, {...result, bytes});
    cacheBytes += bytes;
    while (cache.size > 32 || cacheBytes > 8 * 1024 * 1024) {
      const key = cache.keys().next().value;
      cacheBytes -= cache.get(key).bytes;
      cache.delete(key);
    }
  }
  function render(source, current) {
    // The cache belongs to this page's single version and fixed theme.
    if (cache.has(source)) {
      const result = cache.get(source);
      cache.delete(source); cache.set(source, result);
      return Promise.resolve(result);
    }
    const task = queue.then(async () => {
      if (!current()) return null;
      if (source.length > 20000) throw new Error('Diagram exceeds 20,000 characters.');
      if (broken) throw broken;
      // A preceding job may have populated the same source while we waited.
      if (cache.has(source)) return cache.get(source);
      await renderer();
      if (!current()) return null;
      const id = ++sequence;
      const result = await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => frame._fail(new Error('Diagram rendering timed out.')), 20000);
        pending.set(id, {
          resolve(value) { clearTimeout(timeout); resolve(value); },
          reject(error) { clearTimeout(timeout); reject(error); },
        });
        frame.contentWindow.postMessage({channel, type: 'render', id, source}, '*');
      });
      remember(source, result);
      return result;
    });
    queue = task.catch(() => {});
    return task;
  }
  function mount(root, {expand, changed, failed}) {
    root.querySelectorAll('figure[data-mermaid]').forEach(figure => {
      if (figure._diagramMounted) return;
      figure._diagramMounted = true;
      const source = figure.querySelector('pre code').textContent;
      const pre = figure.querySelector('pre');
      const tools = figure.querySelector('.code-tools');
      const toggle = button('Source'), larger = button('Expand');
      toggle.disabled = larger.disabled = true;
      toggle.setAttribute('aria-expanded', 'true');
      pre.id = 'diagram-source-' + (++sequence);
      toggle.setAttribute('aria-controls', pre.id);
      tools.prepend(larger, toggle);
      const status = element('p', 'diagram-status', 'Preparing diagram…');
      status.setAttribute('role', 'status');
      figure.appendChild(status);
      let failure, reported = false;
      figure._notifyDiagramFailure = () => {
        const message = figure.closest('.msg');
        if (!failure?.repairable || reported || !message?.dataset.messageId
            || message.dataset.messageState !== 'complete') return;
        reported = true;
        Promise.resolve(failed(figure, source, failure.message)).then(result => {
          if (!figure.isConnected || !result) return;
          if (result.status === 'queued' && !figure._diagramRepairSettled) status.textContent = 'Repairing diagram…';
          else repairResult(figure.parentElement, result);
        });
      };
      const viewport = element('div', 'diagram-viewport');
      viewport.hidden = true;
      viewport.tabIndex = 0;
      viewport.setAttribute('role', 'region');
      viewport.setAttribute('aria-label', 'Diagram; scroll to see more');
      figure.insertBefore(viewport, pre);
      let image;
      toggle.addEventListener('click', () => {
        pre.hidden = !pre.hidden;
        toggle.setAttribute('aria-expanded', String(!pre.hidden));
      });
      larger.addEventListener('click', () => expand(image, larger));
      const observer = new IntersectionObserver(entries => {
        if (!entries.some(entry => entry.isIntersecting)) return;
        observer.disconnect();
        observations.delete(figure);
        render(source, () => figure.isConnected).then(result => {
          if (!result || !figure.isConnected) return;
          image = element('img', 'diagram-image');
          image.alt = result.label;
          image.addEventListener('load', () => {
            if (!figure.isConnected) return;
            changed(() => {
              viewport.hidden = false; pre.hidden = true; status.hidden = true;
              toggle.disabled = larger.disabled = false;
              toggle.setAttribute('aria-expanded', 'false');
            });
          }, {once: true});
          image.addEventListener('error', () => {
            status.textContent = 'Diagram image unavailable. Source is shown below.';
            pre.hidden = false;
          }, {once: true});
          image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(result.svg);
          viewport.appendChild(image);
        }).catch(error => {
          if (figure.isConnected) {
            status.textContent = 'Diagram unavailable: ' + error.message;
            failure = error;
            figure._notifyDiagramFailure();
          }
        });
      });
      observations.set(figure, observer);
      observer.observe(figure);
    });
  }
  function settled(root) {
    root.querySelectorAll('figure[data-mermaid]').forEach(figure => figure._notifyDiagramFailure?.());
  }
  function repairResult(root, result) {
    const messages = {
      failed: 'Automatic repair did not fix this diagram. Source is shown above.',
      exhausted: 'Automatic diagram repair limit reached. Source is shown above.',
      stopped: 'Diagram repair stopped. Source is shown above.',
      interrupted: 'Diagram repair was interrupted. Source is shown above.',
      unavailable: 'Automatic diagram repair is unavailable. Source is shown above.',
    };
    if (messages[result?.status]) root.querySelectorAll('figure[data-mermaid]').forEach(figure => {
      const status = figure.querySelector('.diagram-status');
      if (!status.hidden) {
        figure._diagramRepairSettled = true;
        status.textContent = messages[result.status];
      }
    });
  }
  window.BlackroseDiagrams = {mount, settled, repairResult};
})();
