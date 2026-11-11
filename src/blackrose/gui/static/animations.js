/* Trusted timeline interpreter. Scene strings are text, never code, markup or URLs. */
(() => {
  "use strict";
  // Canonical Gothic Bloom tokens, also used by standalone exports. In chat
  // readTheme resolves the app's CSS variables so this follows its design system.
  const defaults = {canvas: "#0d0d0d", surface: "#1a1a1a", text: "#e0e0e0", edge: "#6e6e6e",
    action: "#ef6461", font: "system-ui, sans-serif",
    palette: {neutral: "#b3b3b3", muted: "#b3b3b3", cherry: "#b8506b", rose: "#ef6461",
      blue: "#9bbcf2", gold: "#d4af37", green: "#8cc792"}};
  let active = null;

  function readTheme(figure) {
    const style = getComputedStyle(figure);
    const color = (key, fallback) => style.getPropertyValue(`--animation-${key}`).trim() || fallback;
    const theme = Object.fromEntries(["canvas", "surface", "text", "edge", "action"].map(key => [key, color(key, defaults[key])]));
    theme.font = style.fontFamily || defaults.font;
    theme.palette = Object.fromEntries(Object.entries(defaults.palette).map(([key, fallback]) => [key, color(key, fallback)]));
    return theme;
  }

  function blend(from, to, progress) {
    const rgb = hex => hex.match(/[a-f0-9]{2}/gi).map(pair => parseInt(pair, 16));
    const a = rgb(from), b = rgb(to);
    return `rgb(${a.map((n, i) => Math.round(n + (b[i] - n) * progress)).join(",")})`;
  }

  function valueAt(track, time) {
    const frames = track.keyframes;
    if (time <= frames[0].time) return frames[0].value;
    for (let i = 1; i < frames.length; i++) {
      const a = frames[i - 1], b = frames[i];
      if (time >= b.time) continue;
      let p = (time - a.time) / (b.time - a.time);
      if (track.easing === "step") p = 0;
      else if (track.easing !== "linear") p = p * p * (3 - 2 * p);
      return a.value + (b.value - a.value) * p;
    }
    return frames[frames.length - 1].value;
  }

  function frameAt(scene, time, reducedMotion = false) {
    const nodes = new Map(scene.nodes.map(n => [n.id, {opacity: 1, emphasis: 0, ...n}]));
    scene.tracks.forEach(track => {
      const sampled = reducedMotion && ["x", "y"].includes(track.property) ? {...track, easing: "step"} : track;
      nodes.get(track.node)[track.property] = valueAt(sampled, time);
    });
    return [...nodes.values()];
  }

  function draw(canvas, scene, time, theme, reducedMotion) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Canvas playback is unavailable");
    // Rasterize at the displayed physical size, retaining scene coordinates.
    // Cap allocation for large windows / extreme zoom (about 19 MB per player).
    const width = canvas.getBoundingClientRect().width;
    const scale = Math.min(3, Math.max(1 / 960, (width || 960) * (window.devicePixelRatio || 1) / 960));
    const pixels = Math.round(960 * scale);
    if (canvas.width !== pixels || canvas.height !== Math.round(pixels * 540 / 960)) {
      canvas.width = pixels;
      canvas.height = Math.round(pixels * 540 / 960);
    }
    ctx.setTransform(canvas.width / 960, 0, 0, canvas.height / 540, 0, 0);
    ctx.clearRect(0, 0, 960, 540);
    ctx.fillStyle = theme.canvas;
    ctx.fillRect(0, 0, 960, 540);
    frameAt(scene, time, reducedMotion).forEach(n => {
      ctx.save();
      ctx.globalAlpha = n.opacity;
      const accent = theme.palette[n.color || (n.kind === "dot" ? "rose" : "neutral")];
      ctx.strokeStyle = accent;
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = 2 + n.emphasis;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      let tx = n.x, ty = n.y, textWidth = n.kind === "text" ? 900 : 200, textHeight = 108;
      if (n.kind === "box") {
        const w = n.width || 160, h = n.height || 64;
        ctx.fillStyle = theme.surface;
        ctx.strokeStyle = blend(theme.edge, theme.action, n.emphasis);
        ctx.beginPath();
        // arcTo works on older WebKit too.
        const r = Math.min(16, w / 2, h / 2);
        ctx.moveTo(n.x + r, n.y);
        ctx.arcTo(n.x + w, n.y, n.x + w, n.y + h, r);
        ctx.arcTo(n.x + w, n.y + h, n.x, n.y + h, r);
        ctx.arcTo(n.x, n.y + h, n.x, n.y, r);
        ctx.arcTo(n.x, n.y, n.x + w, n.y, r);
        ctx.closePath(); ctx.fill(); ctx.stroke();
        if (n.emphasis > 0) {
          ctx.save(); ctx.globalAlpha *= n.emphasis * 0.06;
          ctx.fillStyle = theme.action; ctx.fill(); ctx.restore();
        }
        if (n.color && !["neutral", "muted"].includes(n.color)) {
          // Small, labeled data marker; most of the box stays neutral ink.
          const half = Math.min(8, h / 4);
          ctx.beginPath(); ctx.moveTo(n.x + 1, n.y + h / 2 - half);
          ctx.lineTo(n.x + 1, n.y + h / 2 + half);
          ctx.strokeStyle = accent; ctx.lineWidth = 3; ctx.stroke();
        }
        tx += w / 2; ty += h / 2; textWidth = w - 24; textHeight = h - 16;
      } else if (n.kind === "dot") {
        ctx.beginPath(); ctx.arc(n.x, n.y, n.radius || 9, 0, Math.PI * 2); ctx.fill();
        ty -= (n.radius || 9) + 20;
      } else if (n.kind === "arrow") {
        const angle = Math.atan2(n.y2 - n.y, n.x2 - n.x);
        const head = Math.min(12, Math.hypot(n.x2 - n.x, n.y2 - n.y) / 2);
        // End the rounded shaft inside the head so it cannot protrude at the tip.
        ctx.beginPath(); ctx.moveTo(n.x, n.y);
        ctx.lineTo(n.x2 - head * 0.7 * Math.cos(angle), n.y2 - head * 0.7 * Math.sin(angle)); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(n.x2, n.y2);
        ctx.lineTo(n.x2 - head * Math.cos(angle - 0.45), n.y2 - head * Math.sin(angle - 0.45));
        ctx.lineTo(n.x2 - head * Math.cos(angle + 0.45), n.y2 - head * Math.sin(angle + 0.45));
        ctx.closePath(); ctx.fill();
        tx = (n.x + n.x2) / 2; ty = (n.y + n.y2) / 2 - 20;
      }
      ctx.fillStyle = theme.text;
      ctx.font = `22px ${theme.font}`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      textWidth = Math.max(1, Math.min(textWidth, 2 * Math.min(tx, 960 - tx) - 8));
      const lines = (n.label || "").split("\n").slice(0, 4);
      const measured = Math.max(1, ...lines.map(line => ctx.measureText(line).width));
      const fontSize = Math.min(22, 22 * textWidth / measured, textHeight / (lines.length * 1.25));
      ctx.font = `${fontSize}px ${theme.font}`;
      const leading = fontSize * 1.25;
      lines.forEach((line, i) => ctx.fillText(line, tx, ty + (i - (lines.length - 1) / 2) * leading));
      ctx.restore();
    });
  }

  function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function pauseAll() { if (active) active.pause(); globalThis.BlackroseMotionPlayer?.pauseAll(); }

  function mount(host, scene, onExport, playerDocument, options) {
    if (scene.engine === 'motion-canvas') return BlackroseMotionPlayer.mount(host, scene, onExport, playerDocument, false, options);
    const figure = element("figure", "animation-player");
    const title = element("figcaption", "animation-title", scene.title);
    const description = element("p", "animation-description", scene.description);
    const canvas = element("canvas", "animation-canvas");
    canvas.width = 960; canvas.height = 540;
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", scene.description);
    const cue = element("p", "animation-cue");
    cue.setAttribute("aria-live", "polite");
    const controls = element("div", "animation-controls");
    const play = element("button", null, "Play"); play.type = "button";
    play.className = "animation-play";
    const replay = element("button", null, "Replay"); replay.type = "button";
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const loopLabel = element("label", "animation-loop", "Loop");
    const loop = element("input"); loop.type = "checkbox";
    loop.checked = scene.loop === true && !preference.matches;
    loop.disabled = preference.matches;
    loopLabel.prepend(loop);
    loopLabel.title = preference.matches ? "Looping is off for reduced motion" : "Repeat this animation";
    const seek = element("input"); seek.type = "range";
    seek.min = "0"; seek.max = String(scene.duration); seek.step = "0.01"; seek.value = "0";
    seek.setAttribute("aria-label", "Animation position in seconds");
    const clock = element("output", "animation-time");
    controls.append(play, replay, seek, clock, loopLabel);
    if (onExport) {
      const save = element("button", null, "Export HTML"); save.type = "button";
      save.addEventListener("click", async () => {
        save.disabled = true;
        try { await onExport(); }
        catch (_) { cue.textContent = "Export failed. Try again."; }
        finally { save.disabled = false; }
      });
      controls.appendChild(save);
    }
    figure.append(title, canvas, cue, controls, description);
    const summary = element("details", "animation-summary");
    summary.appendChild(element("summary", null, "Animation steps"));
    const steps = element("ol");
    (scene.cues || []).forEach(c => steps.appendChild(element("li", null, `${c.time}s · ${c.text}`)));
    summary.appendChild(steps); figure.appendChild(summary);
    host.appendChild(figure);
    const theme = readTheme(figure);
    let time = 0, playing = false, frame = 0, previous = null;
    const player = {pause, seek: setTime, get time() { return time; }, get playing() { return playing; }};

    function render() {
      if (preference.matches) {
        loop.checked = false; loop.disabled = true;
        loopLabel.title = "Looping is off for reduced motion";
      } else {
        loop.disabled = false; loopLabel.title = "Repeat this animation";
      }
      draw(canvas, scene, time, theme, preference.matches);
      seek.value = String(time);
      clock.textContent = `${time.toFixed(1)} / ${scene.duration.toFixed(1)}s`;
      const current = (scene.cues || []).filter(c => c.time <= time).at(-1);
      const caption = current ? current.text : "Press Play to begin";
      if (cue.textContent !== caption) cue.textContent = caption;
    }
    function pause() {
      playing = false; previous = null;
      cancelAnimationFrame(frame);
      play.textContent = time >= scene.duration ? "Play again" : "Play";
      play.setAttribute("aria-pressed", "false");
      if (active === player) active = null;
    }
    function start() {
      pauseAll();
      if (time >= scene.duration) time = 0;
      playing = true; active = player; previous = null;
      play.textContent = "Pause"; play.setAttribute("aria-pressed", "true");
      frame = requestAnimationFrame(tick);
    }
    function setTime(value) {
      time = Math.min(scene.duration, Math.max(0, Number(value) || 0));
      previous = null;
      if (time >= scene.duration) pause();
      render();
    }
    function tick(now) {
      if (!playing) return;
      if (!figure.isConnected || document.hidden) { pause(); return; }
      if (previous !== null) {
        const elapsed = time + (now - previous) / 1000;
        time = loop.checked && !preference.matches ? elapsed % scene.duration : Math.min(scene.duration, elapsed);
      }
      previous = now;
      try { render(); }
      catch (_) { pause(); cue.textContent = "Animation playback failed. Ask for a revised animation."; return; }
      if (time >= scene.duration) pause();
      else frame = requestAnimationFrame(tick);
    }
    play.addEventListener("click", () => playing ? pause() : start());
    replay.addEventListener("click", () => { setTime(0); start(); });
    seek.addEventListener("input", () => setTime(seek.value));
    // Also redraw paused scenes when the chat column changes size.
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(() => {
        if (!figure.isConnected) { observer.disconnect(); return; }
        render();
      });
      observer.observe(canvas);
    }
    // Always starts paused, including when reduced motion is requested.
    render(); play.setAttribute("aria-pressed", "false");
    return player;
  }
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => { if (document.hidden) pauseAll(); });
  }
  globalThis.BlackroseAnimations = {mount, pauseAll, frameAt, valueAt};
})();
