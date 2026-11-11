/* Trusted chat controls. All generated code stays in sandbox=allow-scripts. */
(() => {
  'use strict';
  const players = new Set();
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function pauseAll() { for (const player of players) player.pause(); }
  function mount(host, scene, onExport, playerDocument, standalone = false, options = {}) {
    if (!playerDocument) throw new Error('Motion Canvas player document is missing');
    const figure = el('figure', 'animation-player motion-canvas-player');
    figure.append(el('figcaption', 'animation-title', scene.title));
    const frame = el('iframe', 'animation-canvas motion-canvas-frame');
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.title = scene.description;
    const cue = el('p', 'animation-cue', 'Preparing animation…');
    cue.setAttribute('aria-live', 'polite');
    const controls = el('div', 'animation-controls');
    const play = el('button', 'animation-play', 'Play');
    const replay = el('button', '', 'Replay');
    const retry = el('button', 'animation-retry', 'Try playback again');
    retry.hidden = true;
    const seek = el('input'); seek.type = 'range'; seek.min = '0'; seek.max = String(scene.duration);
    seek.step = '0.033'; seek.value = '0'; seek.setAttribute('aria-label', 'Animation position in seconds');
    const preference = matchMedia('(prefers-reduced-motion: reduce)');
    const loopLabel = el('label', 'animation-loop', 'Loop');
    const loop = el('input'); loop.type = 'checkbox';
    loop.checked = scene.loop === true && !preference.matches;
    loop.disabled = preference.matches; loopLabel.prepend(loop);
    const clock = el('output', 'animation-time', `0.0 / ${scene.duration.toFixed(1)}s`);
    controls.append(play, replay, seek, clock, loopLabel, retry);
    if (onExport) {
      const exportButton = el('button', '', 'Export HTML');
      exportButton.addEventListener('click', async () => {
        exportButton.disabled = true;
        try { await onExport(); } catch (e) { cue.textContent = `Export failed: ${e.message}`; }
        finally { exportButton.disabled = false; }
      });
      controls.append(exportButton);
    }
    for (const button of controls.querySelectorAll('button')) button.type = 'button';
    play.disabled = replay.disabled = seek.disabled = true;
    const description = el('p', 'animation-description', scene.description);
    const recoveryNote = el('p', 'animation-description animation-recovery');
    recoveryNote.setAttribute('role', 'status'); recoveryNote.hidden = true;
    figure.append(frame, cue, controls, recoveryNote, description);
    if (scene.cues?.length) {
      const details = el('details', 'animation-summary');
      details.append(el('summary', '', 'Animation steps'));
      const steps = el('ol');
      for (const c of scene.cues) steps.append(el('li', '', `${c.time}s · ${c.text}`));
      details.append(steps); figure.append(details);
    }
    let playing = false, ready = false, failed = false, time = 0, duration = scene.duration;
    const send = (command, value) => frame.contentWindow?.postMessage({channel: 'blackrose-motion-control', command, value}, '*');
    const setRecovery = result => {
      if (result?.status === 'queued' && figure.dataset.recovery && figure.dataset.recovery !== 'queued') return;
      options.recovery = result;
      const messages = {
        queued: 'Animation repair queued…',
        repairing: `Repairing animation (${result?.attempt || 1}/2)…`,
        replaced: 'A repaired version is saved below.',
        failed: 'Automatic repair did not finish. You can ask for another revision.',
        exhausted: 'Automatic repair stopped after two attempts. You can ask for another revision.',
        interrupted: 'Animation repair was interrupted. You can ask for another revision.',
        stopped: 'Animation repair stopped.',
        unavailable: 'Automatic repair is unavailable. Try playback again or ask for a revision.',
      };
      recoveryNote.textContent = messages[result?.status] || '';
      recoveryNote.hidden = !recoveryNote.textContent;
      figure.dataset.recovery = result?.status || '';
    };
    const player = {pause() { send('pause'); playing = false; update(); },
      seek(value) { send('seek', value); },
      attachmentId: options.attachmentId, recovery: setRecovery,
      get playing() { return playing; }, get time() { return time; }};
    const update = () => {
      play.textContent = playing ? 'Pause' : time >= duration - 0.04 ? 'Play again' : 'Play';
      play.setAttribute('aria-pressed', String(playing));
      seek.max = String(duration); seek.value = String(time);
      clock.textContent = `${time.toFixed(1)} / ${duration.toFixed(1)}s`;
      if (ready && !failed) cue.textContent = (scene.cues || []).filter(c => c.time <= time).at(-1)?.text || 'Press Play to begin';
    };
    let timeout;
    // WebKit suspends RAF in offscreen frames. Only count initialization time
    // while visible, including when revisiting a long conversation.
    const visibility = new IntersectionObserver(entries => {
      clearTimeout(timeout);
      if (entries[0].isIntersecting && !ready && !failed) timeout = setTimeout(() => {
        showError('Animation initialization timed out. Ask for a revision.');
      }, 20000);
      if (!entries[0].isIntersecting) player.pause();
    });
    const showError = message => {
      if (failed) return;
      failed = true; player.pause();
      cue.textContent = `Animation unavailable: ${message}`;
      play.disabled = replay.disabled = seek.disabled = true;
      figure.dataset.error = message;
      delete figure.dataset.ready;
      retry.hidden = false;
      // Unload a failed scene rather than leaving it running in the background.
      frame.srcdoc = '';
      if (options.recovery) setRecovery(options.recovery);
      else if (options.onFailure) {
        setRecovery({status: 'queued'});
        Promise.resolve().then(() => options.onFailure(message.slice(0, 1000)))
          .then(result => { if (figure.isConnected) setRecovery(result); })
          .catch(() => { if (figure.isConnected) setRecovery({status: 'unavailable'}); });
      }
    };
    const receive = event => {
      if (event.source !== frame.contentWindow || event.data?.channel !== 'blackrose-motion') return;
      const data = event.data;
      if (failed) return;
      if (data.type === 'host-ready') { send('initialize', playerDocument); return; }
      if (data.type === 'raster') {
        for (const key of ['width', 'height', 'displayWidth', 'pixelRatio']) {
          if (Number.isFinite(data[key]) && data[key] > 0 && data[key] < 10000) figure.dataset[key] = String(data[key]);
        }
        return;
      }
      if (data.type === 'error' && typeof data.message === 'string') {
        clearTimeout(timeout); showError(data.message.slice(0, 1000)); return;
      }
      if (data.type === 'ready') {
        ready = true; clearTimeout(timeout); figure.dataset.ready = 'true';
        play.disabled = replay.disabled = seek.disabled = false;
        send('loop', loop.checked);
      }
      if (data.type === 'ready' || data.type === 'state') {
        if (Number.isFinite(data.duration) && data.duration > 0 && data.duration <= 120) duration = data.duration;
        if (Number.isFinite(data.time)) time = Math.max(0, Math.min(duration, data.time));
        if (typeof data.playing === 'boolean') playing = data.playing;
        update();
      }
    };
    window.addEventListener('message', receive);
    const start = () => {
      window.BlackroseAnimations?.pauseAll(); pauseAll();
      send('play'); playing = true; update();
    };
    play.onclick = () => playing ? player.pause() : start();
    replay.onclick = () => { send('seek', 0); if (!preference.matches) start(); };
    retry.onclick = () => {
      ready = failed = playing = false; time = 0;
      delete figure.dataset.error; delete figure.dataset.ready;
      retry.hidden = true; play.disabled = replay.disabled = seek.disabled = true;
      cue.textContent = 'Preparing animation…';
      frame.removeAttribute('srcdoc');
      if (standalone) frame.srcdoc = playerDocument;
      else frame.src = 'motion-canvas-host.html';
      clearTimeout(timeout);
      timeout = setTimeout(() => { if (!ready) showError('Animation initialization timed out.'); }, 20000);
      update();
    };
    seek.oninput = () => { const value = Number(seek.value); player.pause(); send('seek', value); };
    loop.onchange = () => send('loop', loop.checked);
    const reduce = () => {
      loop.disabled = preference.matches;
      if (preference.matches) { loop.checked = false; send('loop', false); player.pause(); }
    };
    preference.addEventListener('change', reduce);
    // Tear down message listeners and frame execution when chat DOM is replaced.
    const observer = new MutationObserver(() => {
      if (figure.isConnected) return;
      player.pause(); clearTimeout(timeout); frame.srcdoc = '';
      players.delete(player); window.removeEventListener('message', receive);
      preference.removeEventListener('change', reduce); observer.disconnect();
      visibility.disconnect();
    });
    host.append(figure); players.add(player);
    observer.observe(document.body, {childList: true, subtree: true});
    visibility.observe(frame);
    if (standalone) frame.srcdoc = playerDocument;
    else frame.src = 'motion-canvas-host.html';
    update();
    return player;
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) pauseAll(); });
  window.BlackroseMotionPlayer = {mount, pauseAll, recovery(attachmentId, result) {
    for (const player of players) if (player.attachmentId === attachmentId) player.recovery(result);
  }};
})();
