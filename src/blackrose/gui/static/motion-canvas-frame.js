/* Trusted adapter inside the opaque-origin frame. No desktop bridge is present. */
(() => {
  const send = (type, data = {}) => parent.postMessage({channel: 'blackrose-motion', type, ...data}, '*');
  let player, stage, ready = false, failed = false, inspection = null;
  const fail = error => {
    if (failed) return;
    failed = true;
    player?.deactivate();
    send('error', {message: String(error?.message || error).slice(0, 1000)});
    document.body.replaceChildren(Object.assign(document.createElement('p'), {
      id: 'error', textContent: 'Animation could not play. Ask for a revision.'}));
  };
  window.addEventListener('error', event => fail(event.error || event.message));
  window.addEventListener('unhandledrejection', event => fail(event.reason));
  try {
    const {Player, Stage, Vector2, Logger} = BlackroseMotion.core;
    const description = BlackroseScene.default;
    if (!description?.klass || typeof description.config !== 'function') {
      throw new Error('Export default makeScene2D(function* (view) {...}) from the source module.');
    }
    description.name = 'animation';
    const logger = new Logger();
    logger.onLogged.subscribe(entry => { if (entry.level === 'error') fail(entry.message); });
    const size = new Vector2(960, 540);
    // Supersample at twice the physical display density for clean curved edges
    // and small type, capped at a 2880×1620 bitmap.
    const pixelScale = () => Math.min(3, Math.max(1, innerWidth * (devicePixelRatio || 1) * 2 / 960));
    stage = new Stage();
    stage.configure({size, resolutionScale: pixelScale(), background: '#0d0d0d'});
    document.body.append(stage.finalBuffer);
    player = new Player({name: sceneMetadata.title, scenes: [description], logger,
      experimentalFeatures: false, variables: {}, plugins: []},
      {size, fps: 30, range: [0, 120], resolutionScale: pixelScale()}, {paused: true, loop: false});
    let resizeTimer;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(async () => {
        if (failed) return;
        const resolutionScale = pixelScale();
        stage.configure({size, resolutionScale});
        await player.configure({size, fps: 30, range: [0, 120], resolutionScale, audioOffset: 0});
        player.activate();
        player.requestRender();
      }, 100);
    });
    const status = () => send('state', {
      time: player.onFrameChanged.current / 30,
      playing: !player.onStateChanged.current.paused,
      duration: player.onDurationChanged.current / 30,
    });
    player.onRender.subscribe(async () => {
      await stage.render(player.playback.currentScene, player.playback.previousScene);
      send('raster', {width: stage.finalBuffer.width, height: stage.finalBuffer.height,
        displayWidth: innerWidth, pixelRatio: devicePixelRatio || 1});
      if (!ready && !failed) {
        ready = true;
        send('ready', {duration: player.onDurationChanged.current / 30});
        status();
      }
      // Player publishes onFrameChanged after onRender; playback.frame identifies
      // the image that Stage just rendered, including a paused seek.
      if (inspection && player.playback.frame === Math.round(inspection.time * 30)) {
        const request = inspection;
        inspection = null;
        const canvas = stage.finalBuffer;
        // Sample the actual raster. These are review hints, not a visual-quality verdict.
        const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        const width = canvas.width, height = canvas.height;
        const columns = 24, rows = 12;
        const grid = new Array(columns * rows).fill(2166136261);
        let hash = 2166136261, foreground = 0;
        for (let i = 0; i < pixels.length; i += 16) {
          hash = Math.imul(hash ^ pixels[i], 16777619);
          hash = Math.imul(hash ^ pixels[i + 1], 16777619);
          hash = Math.imul(hash ^ pixels[i + 2], 16777619);
          if (Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) > 24) {
            foreground++;
            // A coarse per-cell fingerprint lets the host tell mechanism motion
            // from caption-only changes without retaining a PNG for every sample.
            const pixel = i / 4;
            const x = pixel % width;
            const y = (pixel - x) / width;
            const column = Math.min(columns - 1, Math.floor(x * columns / width));
            const row = Math.min(rows - 1, Math.floor(y * rows / height));
            const cell = row * columns + column;
            grid[cell] = Math.imul(grid[cell] ^ pixels[i], 16777619);
            grid[cell] = Math.imul(grid[cell] ^ pixels[i + 1], 16777619);
            grid[cell] = Math.imul(grid[cell] ^ pixels[i + 2], 16777619);
          }
        }
        let png;
        if (request.capture) {
          const thumbnail = document.createElement('canvas');
          thumbnail.width = 960; thumbnail.height = 540;
          thumbnail.getContext('2d').drawImage(canvas, 0, 0, 960, 540);
          png = thumbnail.toDataURL('image/png');
        }
        // Plan-first scenes publish their element registry. Report rendered
        // opacity and transform values so the host can catch invisible or
        // unmoving planned elements without retaining another image.
        const elements = {};
        const registry = globalThis.__blackrosePlanElements;
        if (registry && typeof registry === 'object') {
          for (const [id, node] of Object.entries(registry)) {
            try {
              const opacity = typeof node?.absoluteOpacity === 'function'
                ? node.absoluteOpacity() : node?.opacity?.();
              const position = typeof node?.position === 'function' ? node.position() : null;
              const absolute = typeof node?.absolutePosition === 'function'
                ? node.absolutePosition() : null;
              const size = typeof node?.size === 'function' ? node.size() : null;
              const rotation = typeof node?.rotation === 'function' ? node.rotation() : null;
              const scale = typeof node?.scale === 'function' ? node.scale() : null;
              const values = {opacity: Number(opacity)};
              if (position) { values.x = Number(position.x); values.y = Number(position.y); }
              if (absolute) {
                const ratio = pixelScale();
                values.absoluteX = Number(absolute.x) / ratio - 480;
                values.absoluteY = Number(absolute.y) / ratio - 270;
              }
              if (size) { values.width = Number(size.x); values.height = Number(size.y); }
              if (rotation !== null) values.rotation = Number(rotation);
              if (scale) values.scale = Number(scale.x);
              if (Object.values(values).every(Number.isFinite)) elements[id] = values;
            } catch { /* A broken node must not stop the rest of the probe. */ }
          }
        }
        send('inspected', {id: request.id, time: player.playback.frame / 30,
          hash: (hash >>> 0).toString(16), foreground,
          grid: grid.map(value => value >>> 0),
          ...(Object.keys(elements).length ? {elements} : {}),
          ...(request.capture ? {png} : {})});
      }
      // Pausing alone leaves Motion Canvas's RAF loop alive. Stop after the
      // requested frame has actually rendered, including paused seeks/resizes.
      if (player.onStateChanged.current.paused && !inspection) player.deactivate();
    });
    player.onFrameChanged.subscribe(status);
    player.onStateChanged.subscribe(state => {
      status();
    });
    window.addEventListener('message', event => {
      if (event.source !== parent || event.data?.channel !== 'blackrose-motion-control' || failed) return;
      const {command, value} = event.data;
      if (command === 'pause') {
        player.togglePlayback(false);
        player.deactivate();
      } else if (command === 'play') {
        player.activate();
        if (player.onFrameChanged.current >= player.onDurationChanged.current - 1) player.requestSeek(0);
        player.togglePlayback(true);
      } else if (command === 'inspect' && Number.isFinite(value?.time) && Number.isInteger(value?.id)) {
        inspection = value;
        player.togglePlayback(false);
        player.activate();
        player.requestSeek(Math.round(Math.max(0, Math.min(120, value.time)) * 30));
        player.requestRender();
      } else if (command === 'seek' && Number.isFinite(value)) {
        player.activate();
        player.requestSeek(Math.round(Math.max(0, Math.min(120, value)) * 30));
        player.requestRender();
      } else if (command === 'loop') player.toggleLoop(value === true);
    });
    window.addEventListener('pagehide', () => player.deactivate());
  } catch (error) { fail(error); }
})();
