/* Blackrose — the window's behaviour.
 *
 * Python owns every decision; this file draws what it is told and forwards
 * what the user does. Events arrive in batches through bloom.receive(); calls
 * go out through window.pywebview.api.
 *
 * Motion follows the apple-design notes: feedback on press, critically damped
 * springs (damping 1, response 0.35 s) that always start from the value on
 * screen, so anything can be reversed mid-flight; no overshoot, because
 * nothing here is thrown. Reduced motion jumps straight to the end state.
 *
 * Text from the user or a model is only ever set with textContent. The one
 * exception is answer HTML, which Python renders from Markdown with raw HTML
 * disabled and links limited to http(s) and mailto.
 */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const call = (name, ...args) => {
    const api = window.pywebview && window.pywebview.api;
    if (!api || typeof api[name] !== "function") return Promise.resolve(null);
    const flush = ['open_conversation', 'new_conversation', 'submit', 'retry', 'run_command', 'review_edit'].includes(name) ? window.BlackroseUI?.flushAll() : null;
    return Promise.resolve(flush).catch((err) => {
      flash('Some form edits could not be saved: ' + String(err.message || err));
    }).then(() => api[name](...args)).catch((err) => { console.error(name, err); flash(String(err.message || err)); return null; });
  };

  const el = {
    app: $("app"), sidebar: $("sidebar"), chatList: $("chat-list"), newChat: $("new-chat"),
    modelsButton: $("models-button"), modelValue: $("model-value"),
    libraryButton: $("library-button"), skillsButton: $("skills-button"), knowledgeToggle: $("knowledge-toggle"),
    webToggle: $("web-toggle"), memoryToggle: $("memory-toggle"), trustedButton: $("trusted-button"), helpButton: $("help-button"), menuToggle: $("menu-toggle"),
    citationRigor: $('citation-rigor'), rigorDetail: $('rigor-detail'),
    sources: $('sources'), sourcesValue: $('sources-value'), sourcesPanel: $('sources-panel'), effort: $('effort'), effortValue: $('effort-value'),
    title: $("title"), activeModel: $("active-model"), status: $("status"), statusText: $("status-text"),
    transcript: $("transcript"), column: $("column"),
    composer: $("composer"), input: $("input"), attachments: $("attachments"),
    attach: $("attach"), retry: $("retry"), send: $("send"), commands: $("commands"), scrim: $("scrim"),
    dialog: $("dialog"), dialogTitle: $("dialog-title"), dialogDetail: $("dialog-detail"),
    dialogActions: $("dialog-actions"), toast: $("toast"), resizer: $("resizer"),
    announcement: $("reply-announcement"),
    dock: document.querySelector(".dock"),
  };

  const state = {
    keymap: new Map(),
    keymapBindings: [],
    commands: [],
    busy: false,
    rigorPending: false,
    conversations: [],
    current: null,
    replies: new Map(),
    recentOutcome: null,
    status: {},
    dialog: null,
  };

  // --- springs ------------------------------------------------------------------

  /** A critically damped spring on one number. Retargeting keeps the current
   *  value and velocity, which is what makes every transition interruptible. */
  class Spring {
    constructor(value, { response = 0.35, damping = 1 } = {}) {
      const omega = (2 * Math.PI) / response;
      this.k = omega * omega;
      this.c = 2 * damping * omega;
      this.value = value;
      this.velocity = 0;
      this.target = value;
      this.frame = 0;
      this.tick = this.tick.bind(this);
    }
    /** Stop where it is, or jump to a value, with no motion. */
    jump(value = this.value) {
      cancelAnimationFrame(this.frame);
      this.frame = 0;
      this.value = value;
      this.target = value;
      this.velocity = 0;
    }
    to(target, onUpdate, onRest) {
      this.target = target;
      this.onUpdate = onUpdate;
      this.onRest = onRest;
      if (reduceMotion.matches) {
        cancelAnimationFrame(this.frame);
        this.frame = 0;
        this.value = target;
        this.velocity = 0;
        onUpdate(target);
        if (onRest) onRest();
        return;
      }
      if (!this.frame) {
        this.last = performance.now();
        this.frame = requestAnimationFrame(this.tick);
      }
    }
    tick(now) {
      const dt = Math.min((now - this.last) / 1000, 1 / 30);
      this.last = now;
      const steps = Math.max(1, Math.ceil(dt * 240));
      const h = dt / steps;
      for (let i = 0; i < steps; i += 1) {
        const accel = -this.k * (this.value - this.target) - this.c * this.velocity;
        this.velocity += accel * h;
        this.value += this.velocity * h;
      }
      if (Math.abs(this.velocity) < 0.001 && Math.abs(this.value - this.target) < 0.001) {
        this.value = this.target;
        this.velocity = 0;
        this.frame = 0;
        this.onUpdate(this.value);
        const done = this.onRest;
        this.onRest = null;
        if (done) done();
        return;
      }
      this.onUpdate(this.value);
      this.frame = requestAnimationFrame(this.tick);
    }
  }

  /** New content settles into place: a short rise and a fade. */
  function appear(node) {
    const spring = new Spring(0);
    node.style.opacity = "0";
    spring.to(1, (p) => {
      node.style.opacity = String(Math.min(1, Math.max(0, p)));
      node.style.transform = reduceMotion.matches ? "" : `translateY(${(1 - p) * 8}px)`;
    }, () => { node.style.transform = ""; node.style.opacity = ""; });
  }

  /** An overlay that grows from the control that opened it, and returns there. */
  class Presence {
    constructor(node, { from = 0.96 } = {}) {
      this.node = node;
      this.from = from;
      this.spring = new Spring(0);
    }
    anchor(trigger) {
      if (!trigger || !trigger.getBoundingClientRect) return;
      const t = trigger.getBoundingClientRect();
      const n = this.node.getBoundingClientRect();
      const x = t.left + t.width / 2 - n.left;
      const y = t.top + t.height / 2 - n.top;
      this.node.style.transformOrigin = `${x}px ${y}px`;
    }
    draw(p) {
      const s = this.from + (1 - this.from) * p;
      this.node.style.opacity = String(Math.min(1, Math.max(0, p)));
      this.node.style.transform = reduceMotion.matches ? "" : `scale(${s})`;
    }
    // The node toggles its own hidden as well as its container's: WebKitGTK
    // leaves a subtree un-hidden by an ancestor attached to a dead accessible,
    // so a screen reader never follows focus into it (bra-max).
    show(container, trigger) {
      container.hidden = false;
      this.node.hidden = false;
      this.anchor(trigger);
      this.spring.to(1, (p) => this.draw(p));
    }
    hide(container, done) {
      this.spring.to(0, (p) => this.draw(p), () => {
        container.hidden = true;
        this.node.hidden = true;
        if (done) done();
      });
    }
  }

  // --- small helpers ---------------------------------------------------------------

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function nearBottom() {
    const t = el.transcript;
    return t.scrollHeight - t.scrollTop - t.clientHeight < 64;
  }

  let stickPending = false;
  /** Follow new text only while the reader is already at the bottom. */
  function follow(wasAtBottom) {
    if (!wasAtBottom || stickPending) return;
    stickPending = true;
    requestAnimationFrame(() => {
      stickPending = false;
      el.transcript.scrollTop = el.transcript.scrollHeight;
    });
  }

  function append(child) {
    const atBottom = nearBottom();
    const welcome = el.column.querySelector(".welcome");
    if (welcome) welcome.remove();
    el.column.appendChild(child);
    appear(child);
    follow(atBottom);
    return child;
  }

  let toastTimer = 0;
  const toastSpring = new Spring(0);
  function flash(text) {
    el.toast.textContent = text;
    toastSpring.to(1, (p) => { el.toast.style.opacity = String(p); });
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastSpring.to(0, (p) => { el.toast.style.opacity = String(p); }), 1400);
  }

  function bell() {
    el.composer.classList.add("is-refused");
    setTimeout(() => el.composer.classList.remove("is-refused"), 320);
  }

  function hasSelection() {
    const sel = window.getSelection();
    if (sel && String(sel).length > 0) return true;
    const input = el.input;
    return document.activeElement === input && input.selectionStart !== input.selectionEnd;
  }

  // --- the welcome: a quiet reading room ------------------------------------------------

  function welcome() {
    const wrap = node("div", "welcome");
    const arch = node("div", "arch");
    arch.innerHTML =
      '<img class="brand-logo" src="blackrose-bloom-logo.png" alt="" width="112" height="112">';
    arch.appendChild(node("h2", null, "Blackrose"));
    arch.appendChild(node("p", null,
      "Your local assistant, with a library of knowledge and room for your skills."));
    wrap.appendChild(arch);
    const hints = node("div", "hints");
    [["Ctrl Enter", "send"], ["Ctrl N", "new chat"], ["/", "commands"]].forEach(([k, what]) => {
      const span = node("span");
      span.appendChild(node("kbd", null, k));
      span.appendChild(document.createTextNode(` ${what}`));
      hints.appendChild(span);
    });
    wrap.appendChild(hints);
    return wrap;
  }

  // --- messages --------------------------------------------------------------------------

  // Keep loaded image elements alive when streaming Markdown is redrawn.
  function renderHTML(root, html) {
    const previous = new Map();
    const diagrams = new Map();
    root.querySelectorAll('figure[data-mermaid]').forEach(figure => {
      const source = figure.querySelector('pre code').textContent;
      if (!diagrams.has(source)) diagrams.set(source, []);
      diagrams.get(source).push(figure);
    });
    root.querySelectorAll(".image-frame[data-image-src]").forEach((frame) => {
      const src = frame.dataset.imageSrc;
      if (!previous.has(src)) previous.set(src, []);
      previous.get(src).push(frame);
    });
    root.innerHTML = html;
    root.querySelectorAll(".image-frame[data-image-src]").forEach((frame) => {
      const saved = previous.get(frame.dataset.imageSrc)?.shift();
      if (saved && saved.dataset.imageAlt === frame.dataset.imageAlt) frame.replaceWith(saved);
    });
    root.querySelectorAll('figure[data-mermaid]').forEach(figure => {
      const saved = diagrams.get(figure.querySelector('pre code').textContent)?.shift();
      if (saved) {
        saved.dataset.diagramBlock = figure.dataset.diagramBlock;
        saved.dataset.diagramIndex = figure.dataset.diagramIndex;
        figure.replaceWith(saved);
      }
    });
    window.BlackroseDiagrams?.mount(root, {
      expand: (image, trigger) => {
        openDialog({title: 'Diagram', image, wide: true, trigger,
          actions: [{label: 'Close', value: false}]});
        const viewer = el.dialog.querySelector('.image-viewer');
        viewer.classList.add('diagram-viewer');
        viewer.tabIndex = 0;
        viewer.setAttribute('role', 'region');
        viewer.setAttribute('aria-label', 'Expanded diagram; scroll to see more');
      },
      changed: update => { const bottom = nearBottom(); update(); follow(bottom); },
      failed: (figure, source, error) => {
        const message = figure.closest('.msg');
        return call('diagram_failed', Number(message.dataset.messageId),
          figure.dataset.diagramBlock || figure.closest('[data-block-id]')?.dataset.blockId || '',
          Number(figure.dataset.diagramIndex), source, error);
      },
    });
  }

  function renderBlocks(reply, blocks) {
    if (!blocks?.length) return;
    if (!reply.blocks) { reply.body.replaceChildren(); reply.blocks = new Map(); }
    const wanted = new Set(blocks.map(b => b.block_id));
    for (const [id, container] of reply.blocks) {
      if (!wanted.has(id)) { window.BlackroseUI?.unmount(id); container.remove(); reply.blocks.delete(id); }
    }
    for (const block of blocks) {
      let container = reply.blocks.get(block.block_id);
      if (!container) {
        container = node('div', block.kind === 'markdown' ? 'markdown-block' : 'interactive-block');
        container.dataset.blockId = block.block_id;
        reply.body.appendChild(container); reply.blocks.set(block.block_id, container);
        if (block.kind === 'interactive_ui') {
          if (window.BlackroseUI) window.BlackroseUI.mount(container, block, {
            state: payload => call('ui_state', payload), action: payload => call('ui_action', payload),
            export: payload => call('export_ui', payload),
          });
          else container.textContent = block.fallback_text || 'Interactive runtime unavailable.';
        }
      }
      if (block.kind === 'markdown') renderHTML(container, block.html || '');
      else window.BlackroseUI?.update(block.block_id, block);
    }
  }

  // Opening the file manager is confirmed natively, with the folder and app named.
  function revealButton(attachmentId) {
    const reveal = node('button', 'btn quiet', 'Show folder…');
    reveal.type = 'button';
    reveal.title = 'Asks first, showing the folder and the app that will open it';
    reveal.onclick = e => { e.stopPropagation(); call('open_artifact', attachmentId, true); };
    return reveal;
  }

  function imageFrame(attachment, thumbnail = false) {
    const frame = node("span", `image-frame${thumbnail ? " thumbnail" : ""}`);
    frame.dataset.imageAlt = attachment.filename || attachment.label || "Attached image";
    const caption = node("span", "image-caption");
    caption.appendChild(node("span", null, frame.dataset.imageAlt));
    if (attachment.width && attachment.height) caption.appendChild(node("span", "image-meta", `${attachment.width} × ${attachment.height}`));
    if (attachment.attachment_id != null) caption.appendChild(revealButton(attachment.attachment_id));
    frame.appendChild(caption);
    if (attachment.src) {
      frame.dataset.imageSrc = attachment.src;
      loadImage(frame, attachment.width, attachment.height);
    } else if (attachment.preview_id != null) {
      frame.dataset.previewId = attachment.preview_id;
      const button = node("button", "image-load");
      button.type = "button";
      button.appendChild(node("span", "image-placeholder", "Load image"));
      frame.prepend(button);
      if (previewObserver) previewObserver.observe(frame);
      else requestStoredImage(frame);
    } else {
      frame.dataset.state = "error";
      frame.insertBefore(node("span", "image-placeholder", "Image unavailable"), caption);
    }
    return frame;
  }

  function loadImage(frame, width, height) {
    const src = frame.dataset.imageSrc || "";
    if (!/^(https?:\/\/|data:image\/(png|jpeg|webp);base64,)/i.test(src)) return;
    if (frame.dataset.state === "loading") return;
    const atBottom = nearBottom();
    let button = frame.querySelector("button");
    if (!button) {
      button = node("button");
      button.type = "button";
      frame.prepend(button);
    }
    button.className = "image-open";
    if (width > 0 && height > 0 && !frame.classList.contains("thumbnail")) button.style.aspectRatio = `${width} / ${height}`;
    button.replaceChildren();
    const placeholder = node("span", "image-placeholder", "Loading image…");
    button.appendChild(placeholder);
    const image = node("img");
    image.alt = frame.dataset.imageAlt || "Image";
    image.referrerPolicy = "no-referrer";
    image.decoding = "async";
    image.hidden = true;
    if (width > 0 && height > 0) { image.width = width; image.height = height; }
    frame.dataset.state = "loading";
    button.setAttribute("aria-label", `Loading ${image.alt}`);
    button.setAttribute("aria-busy", "true");
    image.addEventListener("load", () => {
      frame.dataset.state = "ready";
      image.hidden = false;
      placeholder.remove();
      button.setAttribute("aria-label", `Enlarge ${image.alt}`);
      button.removeAttribute("aria-busy");
      button.title = "Click to enlarge";
      if (frame.isConnected) follow(pinned);
    }, { once: true });
    image.addEventListener("error", () => {
      frame.dataset.state = "error";
      image.remove();
      button.className = "image-load";
      placeholder.textContent = "Image unavailable · Retry";
      button.setAttribute("aria-label", `Retry loading ${image.alt}`);
      button.removeAttribute("aria-busy");
      if (frame.isConnected) follow(pinned);
    }, { once: true });
    button.appendChild(image);
    image.src = src;
    follow(atBottom);
  }

  async function requestStoredImage(frame) {
    if (frame.dataset.state === "loading" || frame.dataset.imageSrc) return;
    frame.dataset.state = "loading";
    const button = frame.querySelector("button");
    button.setAttribute("aria-busy", "true");
    button.querySelector(".image-placeholder").textContent = "Loading image…";
    const preview = await call("image_preview", Number(frame.dataset.previewId));
    if (!frame.isConnected) return;
    button.removeAttribute("aria-busy");
    frame.dataset.state = "waiting";
    if (preview?.src) {
      frame.dataset.imageSrc = preview.src;
      loadImage(frame, preview.width, preview.height);
    } else {
      frame.dataset.state = "error";
      button.querySelector(".image-placeholder").textContent = "Image unavailable · Retry";
      button.setAttribute("aria-label", `Retry loading ${frame.dataset.imageAlt}`);
    }
  }

  const previewObserver = typeof IntersectionObserver === "function" ? new IntersectionObserver((entries) => {
    entries.forEach(({ target, isIntersecting }) => {
      if (!isIntersecting) return;
      previewObserver.unobserve(target);
      requestStoredImage(target);
    });
  }, { root: el.transcript, rootMargin: "256px" }) : null;

  function imageClick(e) {
    const button = e.target.closest?.(".image-open, .image-load");
    if (!button) return;
    e.preventDefault();
    const frame = button.closest(".image-frame");
    if (button.classList.contains("image-load")) {
      if (frame.dataset.previewId && !frame.dataset.imageSrc) requestStoredImage(frame);
      else loadImage(frame);
      return;
    }
    if (frame.dataset.state !== "ready") return;
    const image = frame.querySelector("img");
    openDialog({
      title: image.alt || "Image", detail: `${image.naturalWidth} × ${image.naturalHeight}`,
      image, actions: [{ label: "Close", value: false, focus: true }], trigger: button,
    });
  }

  el.transcript.addEventListener("click", imageClick);
  el.attachments.addEventListener("click", imageClick);

  function attachmentChips(items, container) {
    if (!items || !items.length) return;
    const images = items.filter((a) => a.kind === "image");
    if (images.length) {
      const gallery = node("div", "image-gallery");
      images.forEach((a) => gallery.appendChild(imageFrame(a)));
      container.appendChild(gallery);
    }
    const list = node("div", "attach-list");
    items.filter((a) => a.kind === "document").forEach((a) => list.appendChild(node("span", "chip", `Document · ${a.label}`)));
    if (list.childElementCount) container.appendChild(list);
    items.filter(a => a.kind === 'artifact').forEach(a => {
      const card = node('div', 'artifact-card');
      card.appendChild(node('strong', '', a.filename));
      card.appendChild(node('small', '', `${a.mime_type} · ${a.size} bytes\n${a.path}`));
      // Leaving Blackrose goes through a native confirmation that names the path
      // and application; HTML is marked because only the preview contains it.
      if (/\.html?$/i.test(a.filename)) card.appendChild(node('p', 'note artifact-trust',
        'Active web page. Preview blocks its scripts and network access; opened outside Blackrose, it can use them.'));
      const preview = node('button', 'btn quiet', 'Preview');
      preview.onclick = () => previewArtifact(a);
      card.appendChild(preview);
      if (/\.(pdf|md|txt|html?|json)$/i.test(a.filename)) {
        const open = node('button', 'btn quiet', 'Open externally…');
        open.title = 'Asks first, showing the full path and the app that will open it';
        open.onclick = () => call('open_artifact', a.attachment_id); card.appendChild(open);
      }
      card.appendChild(revealButton(a.attachment_id));
      container.appendChild(card);
    });
    items.filter((a) => a.kind === "animation").forEach((a) => {
      const host = node("div");
      container.appendChild(host);
      if (a.error || !a.scene) {
        host.appendChild(node("p", "note error", a.error || "Animation unavailable"));
        return;
      }
      try {
        BlackroseAnimations.mount(host, a.scene, async () => {
          const result = await call("export_animation", a.attachment_id);
          if (result?.error) flash(result.error);
          else if (result?.ok) flash("Animation exported");
        }, a.player_document, {attachmentId: a.attachment_id, recovery: a.recovery,
          onFailure: error => call('animation_failed', a.attachment_id, error)});
        host.appendChild(revealButton(a.attachment_id));
      } catch (error) {
        host.replaceChildren(node("p", "note error", `Animation unavailable: ${error.message}`));
      }
    });
  }

  function userMessage(text, attachments) {
    const msg = node("article", "msg user");
    msg.setAttribute("aria-label", "You");
    attachmentChips(attachments, msg);
    if (text) msg.appendChild(node("div", "bubble", text));
    return msg;
  }

  function assistantMessage({ label, model, html, streaming }) {
    const msg = node("article", "msg assistant");
    const head = node("div", "msg-head");
    const labelNode = node("span", "label", label || "Assistant");
    head.appendChild(labelNode);
    const chip = node("span", "chip", model || "");
    chip.hidden = !model;
    head.appendChild(chip);
    msg.appendChild(head);
    const thinking = node("details", "thinking");
    thinking.hidden = true;
    thinking.appendChild(node("summary", null, "Reasoning"));
    const thought = node("pre");
    thinking.appendChild(thought);
    msg.appendChild(thinking);
    const body = node("div", "body prose");
    if (html) renderHTML(body, html);
    msg.appendChild(body);
    const foot = node("div", "foot");
    const images = node("div", "reply-images");
    msg.appendChild(images);
    msg.appendChild(foot);
    if (streaming) msg.classList.add("is-streaming");
    msg.setAttribute("aria-label", label || "Assistant");
    const reply = { msg, labelNode, chip, thinking, thought, body, foot, images };
    msg._reply = reply;
    return reply;
  }

  function retireFeedback() {
    state.recentOutcome = null;
    el.column.querySelectorAll('details.reply-note, details.notice[data-tone="error"]').forEach(detail => {
      detail.open = false;
    });
  }

  function diagnosticsButton(text) {
    const button = node('button', 'btn quiet copy-diagnostics', 'Copy diagnostics');
    button.type = 'button';
    button.title = 'Copy app version, reply state and local IDs. Message text and error details are omitted.';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const copied = await call('copy', text);
        flash(copied ? 'Diagnostics copied' : 'Could not copy diagnostics');
      } catch {
        flash('Could not copy diagnostics');
      } finally { button.disabled = false; }
    });
    return button;
  }

  function finishReply(reply, p, {historical = false} = {}) {
    reply.msg.classList.remove('is-streaming');
    reply.msg.setAttribute('aria-busy', 'false');
    if (p.id != null) reply.msg.dataset.messageId = p.id;
    reply.msg.dataset.messageState = p.state || '';
    window.BlackroseDiagrams?.settled(reply.body);
    // The latest tool message describes past activity once the request ends.
    // Keep its diagnostics in the disclosure, never as an active progress line.
    reply.msg.querySelector('.tool-activity')?.remove();
    const label = p.state === 'cancelled' ? 'Reply stopped' : p.state === 'error' ? 'Reply failed' :
      p.state === 'pending' ? 'Reply interrupted' : 'Reply complete';
    const hasOutput = reply.body.textContent.trim() || reply.images.childElementCount;
    const detail = p.error || (p.state === 'error' ? (hasOutput ?
      'The request did not finish. You can keep using the output above, or retry.' :
      'No answer was saved. Retry the request or choose another model.') :
      p.state === 'pending' ? 'This reply did not finish. Retry the request to continue.' : '');
    reply.msg.querySelector('.reply-note')?.remove();
    const incomplete = p.state === 'error' || p.state === 'pending' || p.state === 'cancelled' || p.error;
    if (incomplete || !historical) {
      const note = node(incomplete ? 'details' : 'div', 'note reply-note');
      if (incomplete) {
        note.open = !historical;
        note.dataset.tone = p.state === 'error' || p.error ? 'error' : 'info';
        note.appendChild(node('summary', '', 'Reply details'));
        note.appendChild(node('div', 'reply-diagnostic', `${label}${detail ? '. ' + detail : ''}`));
        if (p.diagnostics) note.appendChild(diagnosticsButton(p.diagnostics));
      } else note.textContent = label;
      reply.msg.insertBefore(note, reply.msg.querySelector('.tool-log') || reply.foot);
    }
    return `${label}.`;
  }

  function noticeCard(html, tone, historical = false, diagnostics = null) {
    const card = node(tone === 'error' ? 'details' : 'div', 'notice');
    card.dataset.tone = tone || "info";
    const label = { info: "Note", warning: "Heads up", error: "Problem", success: "Done" }[tone] || "Note";
    if (tone === 'error') {
      card.open = !historical;
      card.appendChild(node('summary', '', 'Request details'));
    } else card.appendChild(node("span", "tone-label", label));
    const body = node("div", "prose");
    renderHTML(body, html);
    card.appendChild(body);
    if (diagnostics) card.appendChild(diagnosticsButton(diagnostics));
    return card;
  }

  // --- saved history ------------------------------------------------------------------------------

  // The transcript starts at the latest page; older pages prepend on request.
  // A replaced transcript retires any page still loading for the previous one.
  let historyEpoch = 0;

  function historyMessage(m) {
    if (m.role === "user") return userMessage(m.text, m.attachments);
    const reply = assistantMessage({ label: m.label, model: m.model, html: m.html });
    renderBlocks(reply, m.blocks);
    attachmentChips(m.attachments, reply.images);
    markWrap(reply.body);
    finishReply(reply, m, {historical: true});
    renderAudit(reply, m.audit);
    renderSources(reply, m.sources || []);
    return reply.msg;
  }

  function earlierButton(before) {
    const button = node('button', 'btn quiet earlier-history', 'Show earlier messages');
    button.type = 'button';
    button.addEventListener('click', () => loadEarlier(button, before));
    return button;
  }

  async function loadEarlier(button, before) {
    // aria-busy rather than disabled, so a keyboard user's focus stays here.
    if (button.getAttribute('aria-busy') === 'true') return;
    button.setAttribute('aria-busy', 'true');
    button.textContent = 'Loading earlier messages…';
    const epoch = historyEpoch;
    const page = await call('earlier_messages', state.current, before);
    if (epoch !== historyEpoch || !button.isConnected) return;
    if (!page) {
      button.setAttribute('aria-busy', 'false');
      button.textContent = 'Show earlier messages';
      flash('Could not load earlier messages');
      return;
    }
    const messages = page.messages || [];
    const hadFocus = document.activeElement === button;
    const more = page.earlier && messages.length ? earlierButton(messages[0].id) : null;
    const fragment = document.createDocumentFragment();
    if (more) fragment.appendChild(more);
    messages.forEach((m) => fragment.appendChild(historyMessage(m)));
    const first = fragment.querySelector('.msg');
    // Keep the message the reader was looking at where it is on screen.
    const fromBottom = el.transcript.scrollHeight - el.transcript.scrollTop;
    button.replaceWith(fragment);
    el.transcript.scrollTop = el.transcript.scrollHeight - fromBottom;
    el.announcement.textContent = `Showing ${messages.length} earlier messages.`;
    if (hadFocus && more) more.focus({preventScroll: true});
    else if (hadFocus && first) { first.tabIndex = -1; first.focus({preventScroll: true}); }
  }

  // --- rendering from events ------------------------------------------------------------------

  const on = {
    status(p) {
      state.status = { ...state.status, ...p };
      if (p.state !== undefined) {
        const outcomes = {complete: 'Reply complete', error: 'Reply failed', cancelled: 'Reply stopped', pending: 'Reply interrupted'};
        const status = p.state === 'Ready' && !p.generating && state.recentOutcome === p.request_state ?
          outcomes[p.request_state] || p.state : p.state;
        el.statusText.textContent = p.detail ? `${status} — ${p.detail}` : status;
        el.statusText.title = el.statusText.textContent;
        const tone = /error|failed|unavailable/i.test(status) ? "error"
          : /…$/.test(status) ? "busy" : "idle";
        el.status.dataset.tone = tone;
      }
      if (p.availability !== undefined) $('model-availability').textContent = p.availability;
      if (p.metrics) on.metrics(p.metrics);
      if (typeof p.generating === 'boolean') setBusy(p.generating);
      renderControls();
    },

    metrics(p) {
      const used = Number.isFinite(p.context_used) ? p.context_used : null;
      const limit = Number.isFinite(p.context_window) && p.context_window > 0 ? p.context_window : null;
      const format = n => n === null ? '—' : n.toLocaleString();
      const bound = Number.isFinite(p.admission_input_bound) ? p.admission_input_bound : null;
      const reserve = Number.isFinite(p.output_reserve) ? p.output_reserve : null;
      const budget = bound !== null && reserve !== null ? bound + reserve : null;
      const ratio = used !== null && limit ? used / limit : null;
      $('context-value').textContent = `Context ${p.context_estimated && used !== null ? '≈' : ''}${format(used)} / ${format(limit)}`;
      $('context-fill').style.transform = `scaleX(${Math.min(1, Math.max(0, ratio || 0))})`;
      $('context-metric').dataset.level = ratio >= 0.9 ? 'high' : ratio >= 0.75 ? 'warm' : 'normal';
      $('context-metric').title = (used === null ? 'Context usage appears when a request starts.' :
        (p.context_estimated ? 'Approximate tokens, including instructions, history, tools, images and this response.' :
          'Provider-reported input and output tokens for the latest model request.')) +
        (ratio !== null ? ` ${(ratio * 100).toFixed(1)}% of context window used.` : ' Context window unavailable.') +
        (budget === null ? '' :
          ` Admission budget: conservative input bound ${format(bound)} + output reserve ${format(reserve)} = ${format(budget)} tokens; requests exceeding it are refused.`);
      const speed = Number.isFinite(p.tokens_per_second) ? p.tokens_per_second : null;
      $('generation-speed').textContent = `${speed === null ? '—' : (p.speed_estimated ? '≈' : '') + speed.toFixed(1)} tokens/s`;
      $('generation-speed').title = speed === null ? 'Generation speed appears as tokens arrive.' :
        (p.speed_estimated ? 'Approximate decode speed since the first output; excludes prompt processing and tool execution.' :
          'Decode speed reported by the model server.');
    },

    conversations(p) {
      state.conversations = p.items || [];
      olderConversations = { more: Boolean(p.more), total: p.total ?? state.conversations.length };
      if (state.current !== p.current || JSON.stringify(workspace) !== JSON.stringify(p.workspace)) {
        ++suggestionEpoch; clearTimeout(suggestionTimer); closeCommands();
      }
      state.current = p.current;
      workspace = p.workspace || null;
      $('workspace-value').textContent = workspace ? workspace.path.split('/').pop() : 'Downloads outputs';
      $('workspace-button').title = workspace?.path || 'Choose a working directory';
      renderConversations();
    },

    transcript(p) {
      // A new view treats outcomes as history. An admission failure can redraw
      // the transcript while its request is still unwinding; keep that fresh.
      if (!state.status.generating) retireFeedback();
      ++historyEpoch;
      BlackroseAnimations.pauseAll();
      window.BlackroseUI?.unmountAll();
      previewObserver?.disconnect();
      state.replies.clear();
      el.column.replaceChildren();
      const messages = p.messages || [];
      if (!messages.length) {
        el.column.appendChild(welcome());
        return;
      }
      if (p.earlier) el.column.appendChild(earlierButton(messages[0].id));
      messages.forEach((m) => el.column.appendChild(historyMessage(m)));
      el.transcript.scrollTop = el.transcript.scrollHeight;
    },

    snapshot(p) {
      state.recentOutcome = null;
      on.transcript({messages: p.messages, earlier: p.earlier});
      if (p.request_notice) on.notice({html: p.request_notice, tone: 'error', historical: true,
        diagnostics: p.request_diagnostics});
      if (p.active) {
        const active = p.active;
        if (active.user) on.user_message(active.user);
        on.reply_start(active);
        on.reply_update(active);
        if (active.reasoning) on.reasoning({key: active.key, text: active.reasoning});
        if (active.attachments?.length) on.reply_images({key: active.key, attachments: active.attachments});
        (active.tool_history || []).forEach(on.tool_activity);
        if (active.tool) on.tool_activity(active.tool);
      }
      on.attachments(p.attachments || {items: []});
      el.input.value = p.draft || '';
      selectedRefs = p.refs || [];
      (p.edits || []).forEach(showEditApproval);
      (p.library_proposals || []).forEach(showLibraryApproval);
      autosize();
      on.status(p.status || {});
    },

    user_message(p) { retireFeedback(); append(userMessage(p.text, p.attachments)); },

    animation_recovery(p) { BlackroseMotionPlayer.recovery(p.attachment_id, p); },

    reply_start(p) {
      retireFeedback();
      setBusy(true);
      const reply = assistantMessage({ label: p.label, model: p.model, streaming: true });
      reply.msg.setAttribute('aria-busy', 'true');
      el.announcement.textContent = 'Assistant reply started.';
      state.replies.set(p.key, reply);
      renderAudit(reply, p.audit);
      append(reply.msg);
    },

    reply_update(p) {
      const reply = state.replies.get(p.key);
      if (!reply) return;
      const atBottom = nearBottom();
      if (p.blocks?.length) renderBlocks(reply, p.blocks);
      else if (!reply.blocks) renderHTML(reply.body, p.html);
      markWrap(reply.body);
      follow(atBottom);
    },

    diagram_update(p) {
      const message = el.column.querySelector(`.msg[data-message-id="${p.id}"]`);
      const reply = message?._reply;
      if (!reply) return;
      const bottom = nearBottom();
      if (p.blocks?.length) renderBlocks(reply, p.blocks);
      else if (p.html !== undefined) renderHTML(reply.body, p.html);
      window.BlackroseDiagrams?.repairResult(reply.body, p.repair);
      follow(bottom);
    },

    audit_update(p) {
      const reply = state.replies.get(p.key);
      if (reply) renderAudit(reply, p.audit);
    },

    ui_update(p) {
      const reply = state.replies.get(p.key);
      if (!reply) return;
      const atBottom = nearBottom();
      renderBlocks(reply, p.blocks);
      follow(atBottom);
    },

    reply_images(p) {
      const reply = state.replies.get(p.key);
      if (!reply) return;
      const atBottom = nearBottom();
      BlackroseAnimations.pauseAll();
      reply.images.replaceChildren();
      attachmentChips(p.attachments, reply.images);
      follow(atBottom);
    },

    reasoning(p) {
      const reply = state.replies.get(p.key);
      if (!reply) return;
      const atBottom = nearBottom();
      reply.thinking.hidden = false;
      reply.thought.textContent += p.text;
      if (reply.thinking.open) reply.thought.scrollTop = reply.thought.scrollHeight;
      follow(atBottom);
    },

    reply_end(p) {
      const reply = state.replies.get(p.key);
      setBusy(false);
      if (!reply) return;
      reply.msg.classList.remove("is-streaming");
      if (p.state === "discard") { reply.msg.remove(); state.replies.delete(p.key); return; }
      state.recentOutcome = p.state;
      if (p.blocks?.length) renderBlocks(reply, p.blocks);
      else if (p.html !== undefined && !reply.blocks) { renderHTML(reply.body, p.html); markWrap(reply.body); }
      if (p.model) { reply.chip.textContent = p.model; reply.chip.hidden = false; }
      const outcome = finishReply(reply, p);
      renderAudit(reply, p.audit);
      renderSources(reply, p.sources || []);
      el.announcement.textContent = outcome;
      state.replies.delete(p.key);
    },

    tool_activity(p) {
      const reply = state.replies.get(p.key);
      if (!reply) return;
      let activity = reply.msg.querySelector('.tool-activity');
      if (!activity) { activity = node('div', 'tool-activity'); reply.msg.insertBefore(activity, reply.foot); }
      const labels = {file_list: 'Browsing files', file_search: 'Finding files', file_info: 'Inspecting file', file_read: 'Reading file', directory_create: 'Creating folder', document_create: 'Saving document', file_edit: 'Preparing edit for approval', animation_create: 'Preparing animation', animation_read: 'Reading animation source',
        knowledge_search: 'Searching local knowledge', knowledge_open: 'Reading local sources',
        knowledge_list: 'Browsing the library', knowledge_add: 'Preparing library proposal', read_skill: 'Reading skill instructions',
        run_skill_script: 'Running approved skill script',
        web_search: 'Searching the web', fetch_content: 'Reading a web page', activate_tools: 'Loading capability tools',
        image_gen: 'Generating image with OpenRouter'};
      const phases = {arguments: 'Preparing tool arguments…', rendering: 'Rendering animation preview…', reviewing: 'Reviewing animation…', repairing: 'Repairing animation…', saving: 'Saving…'};
      activity.textContent = phases[p.phase] || (p.name === 'animation_create' && p.phase === 'start' ? 'Validating animation…' : null) || (p.phase === 'progress' ? p.message :
        (p.label || labels[p.name] || p.name) + (p.phase === 'start' ? '…' : p.phase === 'error' ? ' · adjustment needed' : ' · done'));
      activity.classList.toggle('error', p.phase === 'error');
      if ((p.phase === 'complete' || p.phase === 'error') && p.sequence != null) {
        let log = reply.msg.querySelector('.tool-log');
        if (!log) { log = node('details', 'tool-log'); log.appendChild(node('summary', '', 'Tool activity')); reply.msg.insertBefore(log, reply.foot); }
        if (!log.querySelector(`[data-tool-sequence="${p.sequence}"]`) && log.childElementCount <= 48) {
          // Web rows show the exact destination and how it was authorized, or why it was not sent.
          const detail = p.target ? [p.authorization || (p.phase === 'error' ? 'failed' : null), p.target].filter(Boolean).join(' · ')
            : (p.path || p.error || 'done');
          const row = node('div', 'note', `${p.label || labels[p.name] || p.name} · ${detail}`);
          row.dataset.toolSequence = p.sequence; log.appendChild(row);
        }
      }
    },
    open_models() { openModels(); },
    open_library() { openLibrary(); },
    open_skills() { openSkills(); },
    open_memory() { openMemory(); },
    edit_approval(p) { showEditApproval(p); },
    library_approval(p) { showLibraryApproval(p); },

    notice(p) {
      const historical = p.historical && !state.recentOutcome;
      const card = noticeCard(p.html, p.tone, historical, p.diagnostics);
      markWrap(card);
      append(card);
    },

    attachments(p) {
      el.attachments.replaceChildren();
      (p.items || []).forEach((a, i) => {
        const chip = node("span", "attachment");
        if (a.kind === "image" && a.src) {
          chip.classList.add("with-image");
          chip.appendChild(imageFrame(a, true));
        }
        chip.appendChild(node("span", "name", a.label));
        const remove = node("button", null, "×");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove ${a.filename}`);
        remove.addEventListener("click", () => call("remove_attachment", i));
        chip.appendChild(remove);
        el.attachments.appendChild(chip);
      });
    },

    confirm_delete(p) {
      openDialog({
        title: "Delete this chat?",
        detail: `${p.title}\nThis cannot be undone.`,
        actions: [
          { label: "No", value: false, focus: true },
          { label: "Yes", value: true, destructive: true },
        ],
        trigger: state.deleteTrigger,
        onClose: (yes) => {
          if (yes) call("delete_conversation", p.id).then(() => el.input.focus());
        },
      });
    },

    help() {
      openDialog({
        title: "Keys and commands",
        body: helpSheet(),
        variant: "help-dialog",
        actions: [{ label: "Close", value: false, focus: true }],
        trigger: document.activeElement,
      });
    },

    bell() { bell(); },
    flash(p) { flash(p.text); },
    restore_draft(p) {
      if (p.replace || !el.input.value.trim()) { el.input.value = p.text; selectedRefs = p.refs || []; autosize(); }
    },
    quit() { call("quit"); },
  };

  function receive(events) {
    for (const [kind, payload] of events) {
      // A queued event can arrive after navigation replaced the live view.
      if (payload.conversation_id != null && payload.conversation_id !== state.current) continue;
      const handler = on[kind];
      if (handler) {
        try { handler(payload); } catch (err) { console.error(kind, err); }
      }
    }
  }

  // --- chrome --------------------------------------------------------------------------------

  function setBusy(busy) {
    window.BlackroseUI?.setBusy(busy);
    state.busy = busy;
    el.column.querySelectorAll('.library-review, .library-reject').forEach(button => { button.disabled = busy; });
    el.send.textContent = busy ? "Stop" : "Send";
    el.send.classList.toggle("primary", true);
    el.send.setAttribute("aria-label", busy ? "Stop generating (Ctrl C)" : "Send (Ctrl Enter)");
    el.attach.disabled = busy;
    el.retry.disabled = busy || !state.status.can_retry;
    renderRigor();
    el.send.disabled = state.rigorPending;
    if (!busy) {
      $('generation-speed').textContent = '— tokens/s';
      $('generation-speed').title = 'Generation speed appears while a reply is running.';
    }
  }

  function renderControls() {
    el.retry.disabled = state.busy || state.rigorPending || !state.status.can_retry;
    el.modelValue.textContent = state.status.model_display_name || state.status.model || 'Choose';
    el.modelValue.title = state.status.model || '';
    el.activeModel.textContent = state.status.response_model || '';
    el.webToggle.setAttribute('aria-checked', state.status.web ? 'true' : 'false');
    el.knowledgeToggle.setAttribute('aria-checked', state.status.knowledge ? 'true' : 'false');
    el.memoryToggle.setAttribute('aria-checked', state.status.memory ? 'true' : 'false');
    [el.modelsButton, el.webToggle, el.knowledgeToggle, el.memoryToggle].forEach(b => b.disabled = Boolean(state.status.generating));
    renderRigor();
    renderSourcesSummary();
    renderEffort();
  }

  // --- sources: what a reply may draw on, and how hard its claims are checked -------------------
  // One composer control opens a small panel above itself. Toggles apply as they flip and
  // leave the panel open; Escape, a click elsewhere or tabbing out puts it away.

  const RIGOR_MODES = {
    off: ['Off', 'No additional claim assessment. Ordinary source links remain available.'],
    advisory: ['Advisory', 'Assesses central factual claims. Keeps the draft; adds no research or rewrite.'],
    balanced: ['Balanced', 'Assesses material factual claims, with up to two evidence calls and one revision.'],
    strict: ['Strict', 'Assesses every checkable claim, source quality and counterevidence, with up to six evidence calls and one revision.'],
  };
  const RIGOR_COST = 'Uses the selected model and adds time and requests, which may cost money.';
  const rigorRadios = () => [...el.citationRigor.querySelectorAll('[role="radio"]')];
  let rigorTarget = null;  // optimistic mode while a save is in flight

  function rigorMode() { return rigorTarget ?? state.status.citation_rigor ?? 'off'; }

  function renderRigor() {
    const mode = rigorMode(), locked = Boolean(state.busy || state.rigorPending);
    el.citationRigor.dataset.saved = state.status.citation_rigor || 'off';
    el.citationRigor.setAttribute('aria-busy', state.rigorPending ? 'true' : 'false');
    rigorRadios().forEach((radio) => {
      const checked = radio.dataset.mode === mode;
      radio.setAttribute('aria-checked', checked ? 'true' : 'false');
      radio.tabIndex = checked ? 0 : -1;
      // aria-disabled, not disabled: focus stays on the choice while it saves.
      radio.setAttribute('aria-disabled', locked ? 'true' : 'false');
    });
    const [, text] = RIGOR_MODES[mode] || RIGOR_MODES.off;
    el.rigorDetail.textContent = mode === 'off' ? text : `${text} ${RIGOR_COST}`;
  }

  function renderSourcesSummary() {
    const on = [state.status.knowledge && 'Local', state.status.web && 'Web', state.status.memory && 'Memory'].filter(Boolean);
    const sources = on.length === 3 ? 'All' : on.length ? on.join(' · ') : 'None';
    const mode = rigorMode();
    const rigor = mode === 'off' ? '' : ` · ${RIGOR_MODES[mode]?.[0] || mode}`;
    el.sourcesValue.textContent = sources + rigor;
    el.sources.setAttribute('aria-label', `Sources: ${sources}. Citation rigor: ${RIGOR_MODES[mode]?.[0] || mode}`);
  }

  async function setRigor(mode) {
    if (state.busy || state.rigorPending || mode === rigorMode()) return;
    rigorTarget = mode;
    state.rigorPending = true;
    setBusy(state.busy);
    renderControls();
    try {
      const result = await call('set_citation_rigor', mode);
      if (result?.error) flash(result.error);
      if (result?.ok) state.status.citation_rigor = result.citation_rigor;
    } finally {
      rigorTarget = null;
      state.rigorPending = false;
      setBusy(state.busy);
      renderControls();
    }
  }

  const sourcesPresence = new Presence(el.sourcesPanel, { from: 0.96 });
  let sourcesOpen = false;

  function placeSources() {
    const a = el.sources.getBoundingClientRect(), panel = el.sourcesPanel;
    const room = window.innerWidth - panel.offsetWidth - 16;
    panel.style.left = `${Math.max(16, Math.min(a.left, room))}px`;
    panel.style.bottom = `${window.innerHeight - a.top + 8}px`;
    // Never taller than the room between the toolbar and the trigger; it scrolls instead.
    const ceiling = $('app-toolbar')?.getBoundingClientRect().bottom || 0;
    panel.style.maxHeight = `${Math.max(160, a.top - 8 - ceiling - 8)}px`;
  }

  function openSources() {
    if (sourcesOpen) return;
    closeCommands();
    sourcesOpen = true;
    el.sources.setAttribute('aria-expanded', 'true');
    el.sourcesPanel.hidden = false;
    placeSources();
    sourcesPresence.show(el.sourcesPanel, el.sources);
    const first = el.sourcesPanel.querySelector('button:not([disabled])');
    if (first) first.focus();
  }

  function closeSources({ restoreFocus = true } = {}) {
    if (!sourcesOpen) return;
    sourcesOpen = false;
    el.sources.setAttribute('aria-expanded', 'false');
    const hadFocus = el.sourcesPanel.contains(document.activeElement);
    sourcesPresence.hide(el.sourcesPanel);
    if (restoreFocus && hadFocus) el.sources.focus();
  }

  function toggleSources() { if (sourcesOpen) closeSources(); else openSources(); }

  // --- reasoning effort ---------------------------------------------------------------------
  // Default sends nothing, so the model's preset decides. A choice applies from the next prompt.

  const EFFORT_LABELS = { default: 'Default', off: 'Off', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'XHigh' };
  let effortTarget = null;  // optimistic level while a save is in flight

  function effortLevel() { return effortTarget ?? state.status.reasoning_effort ?? 'default'; }
  function effortLevels() { return state.status.reasoning_effort_levels || ['default']; }

  function renderEffort() {
    const level = effortLevel(), label = EFFORT_LABELS[level] || level;
    const adjustable = effortLevels().length > 1;
    el.effort.dataset.level = level;
    el.effort.disabled = !adjustable;
    el.effortValue.textContent = label;
    el.effort.setAttribute('aria-label', `Reasoning effort: ${label}`);
    el.effort.title = adjustable
      ? `Reasoning effort: ${label}. Shift+Tab in the message box cycles it; applies to the next prompt.`
      : `${state.status.model_display_name || 'This model'} doesn't expose reasoning effort; its preset decides.`;
  }

  function cycleEffort() {
    const levels = effortLevels();
    if (levels.length < 2) {
      bell();
      flash(`Reasoning effort isn't adjustable for ${state.status.model_display_name || 'this model'}`);
      return;
    }
    const next = levels[(levels.indexOf(effortLevel()) + 1) % levels.length];
    effortTarget = next;
    renderEffort();
    flash(`Reasoning: ${EFFORT_LABELS[next]}`);
    call('set_reasoning_effort', next).then((result) => {
      if (effortTarget !== next) return;  // a later press supersedes this save
      effortTarget = null;
      if (result?.error) flash(result.error);
      renderEffort();
    });
  }

  function renderConversations() {
    el.chatList.replaceChildren();
    state.conversations.forEach((c) => {
      const li = node("li");
      li.setAttribute("role", "presentation");
      const item = node("button", "chat-item");
      item.appendChild(node("span", "t", c.title));
      item.title = c.title;
      item.type = "button";
      item.setAttribute("role", "option");
      item.dataset.id = c.id;
      const current = c.id === state.current;
      item.setAttribute("aria-current", current ? "true" : "false");
      item.setAttribute("aria-selected", current ? "true" : "false");
      item.tabIndex = current ? 0 : -1;
      item.addEventListener("click", () => openConversation(c.id));
      li.appendChild(item);
      el.chatList.appendChild(li);
    });
    const current = state.conversations.find((c) => c.id === state.current);
    el.title.textContent = current ? current.title : "Blackrose";
    el.title.title = current ? current.title : "";
    document.title = current ? `${current.title} — Blackrose` : "Blackrose";
    renderOlderConversations();
  }

  // The sidebar loads history a page at a time; older chats stay one press away.
  let olderConversations = { more: false, total: 0 };
  let olderLoading = false;
  let focusLoadedConversation = null;

  function renderOlderConversations() {
    const shown = state.conversations.length;
    $("chat-more").hidden = !olderConversations.more;
    $("chat-count").textContent = olderConversations.more ?
      `Showing ${shown.toLocaleString()} of ${olderConversations.total.toLocaleString()} conversations` : "";
    $("chat-older").setAttribute("aria-disabled", olderLoading ? "true" : "false");
    // Move focus to the first newly loaded chat once it is rendered.
    const loaded = focusLoadedConversation !== null &&
      el.chatList.querySelector(`.chat-item[data-id="${focusLoadedConversation}"]`);
    if (loaded) {
      focusLoadedConversation = null;
      el.chatList.querySelectorAll(".chat-item").forEach((item) => { item.tabIndex = item === loaded ? 0 : -1; });
      loaded.focus();
    }
  }

  function loadOlderConversations() {
    if (olderLoading) return;
    olderLoading = true;
    renderOlderConversations();
    call("load_older_conversations").then((result) => {
      olderLoading = false;
      focusLoadedConversation = result?.first ?? null;
      renderOlderConversations();
    });
  }

  function openConversation(id) {
    call("open_conversation", id).then((ok) => {
      if (ok) { closeSidebarIfOverlay(); el.input.focus(); }
    });
  }

  // --- dialogs ---------------------------------------------------------------------------------

  const dialogPresence = new Presence(el.dialog);

  // --- keys and commands ---------------------------------------------------------------------

  // "ctrl+\\" -> "Ctrl \\", matching the toolbar's "Ctrl N".
  function keyLabel(combo) {
    return combo.split(/\+(?=.)/).map(part => part.length === 1 ? part.toUpperCase()
      : part[0].toUpperCase() + part.slice(1)).join(" ");
  }

  function helpSection(heading, rows, listClass) {
    const section = node("section", "help-section");
    const id = `help-${listClass}`;
    const label = node("h3", "section-label", heading);
    label.id = id;
    const list = node("dl", `help-list ${listClass}`);
    list.setAttribute("aria-labelledby", id);
    rows.forEach(([term, detail]) => {
      const row = node("div", "help-row");
      row.append(term, detail);
      list.appendChild(row);
    });
    section.append(label, list);
    return section;
  }

  function helpSheet() {
    const keyRow = (label, combo) => {
      const dd = node("dd");
      dd.appendChild(node("kbd", null, keyLabel(combo)));
      return [node("dt", null, label), dd];
    };
    const keys = [];
    state.keymapBindings.forEach((binding) => {
      keys.push(keyRow(binding.label, binding.keys[0]));
      if (binding.action === "send") keys.push(keyRow("New line", "enter"));
    });
    const commands = state.commands.map(({ usage, summary }) => {
      const space = usage.indexOf(" ");
      const name = space < 0 ? usage : usage.slice(0, space);
      const args = space < 0 ? "" : usage.slice(space + 1);
      const dd = node("dd");
      dd.appendChild(node("span", "help-summary", summary));
      if (args) {
        const hint = node("span", "help-args");
        if (/^<.+>$/.test(args)) hint.appendChild(node("em", null, args.slice(1, -1)));
        else hint.textContent = args.split("|").join(" · ");
        dd.appendChild(hint);
      }
      return [node("dt", "help-command", name), dd];
    });
    const sheet = document.createDocumentFragment();
    sheet.append(helpSection("Keyboard", keys, "help-keys"), helpSection("Commands", commands, "help-commands"));
    return sheet;
  }

  function openDialog({ title, detail, html, body, variant, actions, trigger, onClose, wide, image }) {
    if (state.dialog) closeDialog(false, true);
    // A fresh element for every dialog: WebKitGTK and Orca keep the accessible
    // name a reused dialog first had, so a later dialog was announced with an
    // earlier title (bra-max). Its parts move across, so references stay valid.
    const fresh = el.dialog.cloneNode(false);
    fresh.append(...el.dialog.childNodes);
    el.dialog.replaceWith(fresh);
    el.dialog = fresh;
    dialogPresence.node = fresh;
    el.dialogDetail.style.whiteSpace = '';
    el.dialogTitle.textContent = title;
    if (state.dialogVariant) el.dialog.classList.remove(state.dialogVariant);
    state.dialogVariant = variant || null;
    if (variant) el.dialog.classList.add(variant);
    if (body) { el.dialogDetail.replaceChildren(body); el.dialogDetail.classList.remove("prose"); }
    else if (html) { el.dialogDetail.innerHTML = html; el.dialogDetail.classList.add("prose"); }
    else { el.dialogDetail.textContent = detail || ""; el.dialogDetail.classList.remove("prose"); }
    el.dialog.classList.toggle("wide", Boolean(wide));
    el.dialog.classList.remove('evidence-dialog');
    el.dialog.classList.toggle("image-dialog", Boolean(image));
    el.dialog.querySelector(".image-viewer")?.remove();
    if (image) {
      const viewer = node("div", "image-viewer");
      viewer.appendChild(image.cloneNode());
      el.dialog.insertBefore(viewer, el.dialogActions);
    }
    el.dialogActions.replaceChildren();
    const buttons = actions.map((a) => {
      const b = node("button", `btn ${a.destructive ? "destructive" : ""}`, a.label);
      b.type = "button";
      b.addEventListener("click", () => closeDialog(a.value));
      el.dialogActions.appendChild(b);
      return { button: b, ...a };
    });
    state.dialog = { buttons, onClose, trigger: trigger || document.activeElement };
    el.scrim.classList.remove('is-closing');
    dialogPresence.show(el.scrim, state.dialog.trigger);
    el.app.inert = true;
    closeCommands();
    const focus = buttons.find((b) => b.focus) || buttons[0];
    focus.button.focus();
  }

  function closeDialog(value, immediate) {
    const dialog = state.dialog;
    if (!dialog) return;
    state.dialog = null;
    el.app.inert = false;
    const finish = () => {
      const trigger = dialog.trigger;
      if (trigger && trigger.isConnected && trigger.focus) trigger.focus();
      else el.input.focus();
      if (dialog.onClose) dialog.onClose(value);
    };
    if (immediate) { el.scrim.hidden = true; el.dialog.hidden = true; finish(); return; }
    // A closing scrim still covers the toolbar while it fades. Let clicks through
    // so a quick follow-up click opens the next dialog instead of being lost.
    el.scrim.classList.add('is-closing');
    dialogPresence.hide(el.scrim, () => el.scrim.classList.remove('is-closing'));
    finish();
  }

  function dialogKey(e) {
    const dialog = state.dialog;
    const controls = [...el.dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex="0"]')]
      .filter(control => control.getClientRects().length);
    const index = controls.indexOf(document.activeElement);
    // WebKitGTK can report shifted Tab as Unidentified (ISO_Left_Tab).
    // Its physical code still identifies Tab, so preserve the modal trap.
    const key = (e.code === 'Tab' ? 'Tab' : e.key).toLowerCase();
    if (key === 'escape') { e.preventDefault(); closeDialog(false); return; }
    if (key === 'tab' && controls.length) {
      e.preventDefault();
      const step = e.shiftKey ? -1 : 1;
      controls[(index + step + controls.length) % controls.length].focus();
    }
    if (e.target.closest('input, textarea, select')) return;
    if (key === 'y' && dialog.buttons.some(b => b.value === true)) { e.preventDefault(); closeDialog(true); }
    if (key === 'n' && dialog.buttons.some(b => b.value === true)) { e.preventDefault(); closeDialog(false); }
  }

  el.scrim.addEventListener("pointerdown", (e) => {
    if (e.target === el.scrim && state.dialog) closeDialog(false);
  });

  async function previewArtifact(artifact) {
    const result = await call('artifact_preview', artifact.attachment_id);
    if (!result) return;
    openDialog({title: artifact.filename, html: result.html, detail: result.pdf ? 'PDF text preview' : '',
      wide: true, actions: [{label: 'Close', value: false}]});
    if (result.document !== undefined) {
      const frame = node('iframe', 'artifact-html');
      frame.setAttribute('sandbox', '');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.title = artifact.filename;
      frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'">` + result.document;
      el.dialogDetail.replaceChildren(node('p', 'artifact-sandbox-note',
        'Sandboxed preview: scripts, forms, links and remote content are blocked.'), frame);
    }
  }

  // The comparison is computed and bounded in Python. Every change is marked by
  // a visible sign and a spoken label as well as color; file text is only ever
  // assigned through textContent.
  function editComparison(review) {
    const view = node('div', 'edit-review');
    const c = review.comparison || {summary: 'No comparison is available; review the full texts below.', notes: [], hunks: []};
    const facts = node('dl', 'source-facts');
    const create = review.operation === 'create';
    (create ? [['New file', review.path],
      ['Nothing replaced', 'Approval is refused if a file appears at this path first. Missing folders on the path are created.'],
      ['Content', review.path.toLowerCase().endsWith('.pdf') ? 'The PDF is rendered from the text shown here.' : 'Exactly the text shown here.']] :
     [['File', review.path],
      ['Backup', 'The current file moves to Blackrose\'s private backup store, outside this folder. Restore it from Workspace → Edit backups.'],
      ['If it changes', 'Approval is refused if the file changes after this review; request a new edit.']])
      .forEach(([term, value]) => { facts.appendChild(node('dt', '', term)); facts.appendChild(node('dd', '', value)); });
    view.appendChild(facts);
    const summary = node('p', 'edit-review-summary', c.summary); summary.id = 'edit-review-summary';
    view.appendChild(summary);
    if (c.notes.length) {
      const notes = node('ul', 'edit-review-notes');
      c.notes.forEach(text => notes.appendChild(node('li', '', text)));
      view.appendChild(notes);
    }
    if (c.hunks.length) {
      const diff = node('div', 'diff');
      diff.setAttribute('role', 'region'); diff.setAttribute('aria-label', 'Changes'); diff.tabIndex = 0;
      const range = (start, count) => count ? (count > 1 ? `${start}–${start + count - 1}` : `${start}`) : 'none';
      const labels = {removed: ['−', 'del', 'Removed'], added: ['+', 'ins', 'Added'], context: [' ', 'span', 'Unchanged']};
      c.hunks.forEach((hunk, index) => {
        diff.appendChild(node('h3', 'diff-hunk', `Change ${index + 1} of ${c.hunks.length + c.omitted.hunks} · ` +
          `current lines ${range(hunk.before_start, hunk.before_count)}, proposed lines ${range(hunk.after_start, hunk.after_count)}`));
        hunk.rows.forEach(row => {
          const [sign, tag, label] = labels[row.kind];
          const line = node('div', `diff-row ${row.kind}`);
          [row.before, row.after, sign].forEach(value => {
            const gutter = line.appendChild(node('span', 'diff-gutter', value ?? ''));
            gutter.setAttribute('aria-hidden', 'true');
          });
          const text = node(tag, 'diff-text');
          text.appendChild(node('span', 'visually-hidden', `${label}, line ${row.after ?? row.before}: `));
          if (row.skipped_before) text.appendChild(node('span', 'diff-skip', `[${row.skipped_before} characters before] `));
          text.appendChild(document.createTextNode(row.text));
          if (row.skipped_after) text.appendChild(node('span', 'diff-skip', ` [${row.skipped_after} more characters]`));
          if (row.mark) text.appendChild(node('span', 'diff-mark', ` [${row.mark}]`));
          line.appendChild(text);
          diff.appendChild(line);
        });
        if (hunk.truncated) diff.appendChild(node('p', 'diff-omitted',
          `Comparison display limit reached: ${c.omitted.removed} more removed and ${c.omitted.added} more added lines are not shown.`));
      });
      view.appendChild(diff);
    }
    (create ? [['Proposed file: full text', review.after]] :
     [['Current file: full text', review.before], ['Proposed replacement: full text', review.after]]).forEach(([title, text]) => {
      const full = node('details', 'edit-review-full');
      full.appendChild(node('summary', '', title));
      full.appendChild(node('pre', 'source-metadata', text));
      view.appendChild(full);
    });
    return view;
  }

  function showEditApproval(p) {
    if (el.column.querySelector(`[data-approval="${p.approval_id}"]`)) return;
    const card = node('div', 'artifact-card'); card.dataset.approval = p.approval_id;
    const create = p.operation === 'create';
    card.appendChild(node('span', '', `Approval needed · ${create ? 'new file · ' : ''}${p.path}`));
    const button = node('button', 'btn', create ? 'Review new file' : 'Review edit');
    button.onclick = async () => {
      const review = await call('review_edit', p.approval_id);
      if (!review) return;
      const creating = review.operation === 'create';
      openDialog({title: `${creating ? 'Create' : 'Replace'} ${review.path}?`, wide: true,
        actions: [{label: 'Cancel', value: false},
          {label: creating ? 'Approve new file' : 'Approve replacement', value: true, destructive: true}],
        onClose: async approve => {
          el.dialog.classList.remove('edit-review-dialog'); el.dialog.removeAttribute('aria-describedby');
          if (approve) {
            const result = await call('review_edit', p.approval_id, true);
            if (result) card.remove();
            if (result?.backup_stored) flash('Saved. The previous version is in Workspace → Edit backups.');
          }
        }});
      el.dialog.classList.add('edit-review-dialog');
      el.dialogDetail.replaceChildren(editComparison(review));
      el.dialog.setAttribute('aria-describedby', 'edit-review-summary');
    };
    card.appendChild(button);
    const reject = node('button', 'btn quiet', 'Reject');
    reject.onclick = async () => { if (await call('review_edit', p.approval_id, false, true)) card.remove(); };
    card.appendChild(reject); el.column.appendChild(card);
  }

  function libraryOrigin(source) {
    const metadata = source.source_metadata || {};
    const label = metadata.document_type === 'web_capture' ? 'Web capture' :
      metadata.document_type === 'generated_artifact' ? 'Model-generated artifact' : '';
    return label + (label && metadata.capture_completeness === 'partial' ? ' · Partial capture' : '');
  }

  function showLibraryApproval(p) {
    if (el.column.querySelector(`[data-library-approval="${p.approval_id}"]`)) return;
    const card = node('section', 'artifact-card library-proposal');
    card.dataset.libraryApproval = p.approval_id;
    card.setAttribute('aria-label', 'Library save proposal');
    const heading = node('strong', '', p.title);
    const description = node('span', 'muted', `${p.origin} · Awaiting review${p.truncated ? ' · Partial capture' : ''}`);
    const controls = node('div', 'management-actions');
    const reviewButton = node('button', 'btn library-review', 'Review');
    const rejectButton = node('button', 'btn quiet library-reject', 'Reject');
    reviewButton.disabled = rejectButton.disabled = state.busy;
    reviewButton.onclick = async () => {
      const review = await call('review_library_add', p.approval_id);
      if (!review) return;
      const metadata = review.source_metadata || {};
      const details = [review.origin, review.source_url || metadata.original_filename,
        metadata.generator_model && `Generated by ${metadata.generator_model}`,
        metadata.retrieved_at && `Captured ${metadata.retrieved_at}`,
        review.tags.length && `Tags: ${review.tags.join(', ')}`,
        review.truncated && 'Partial capture: this is only the fetched excerpt.',
        'Only the text below will be indexed. Approval does not verify its facts or instructions.',
        review.text].filter(Boolean).join('\n\n');
      openDialog({title: `Add ${review.title} to Library?`, wide: true, detail: details,
        actions: [{label: 'Cancel', value: false, focus: true}, {label: 'Add to Library', value: true}],
        onClose: async approve => {
          if (!approve) return;
          setBusy(true);
          description.textContent = 'Indexing…';
          try {
            const result = await call('review_library_add', p.approval_id, true);
            if (!result) {
              description.textContent = 'Indexing failed. Review and try again.';
              return;
            }
            description.textContent = result.status === 'unchanged' ? 'Already in Library' : 'Saved to Library';
            controls.replaceChildren(action('Open in Library', () => openSource(result.document_id)));
            card.removeAttribute('data-library-approval');
          } finally { setBusy(false); }
        }});
      el.dialogDetail.style.whiteSpace = 'pre-wrap';
      el.dialogDetail.style.overflowWrap = 'anywhere';
    };
    rejectButton.onclick = async () => {
      if (await call('review_library_add', p.approval_id, false, true)) card.remove();
    };
    controls.append(reviewButton, rejectButton);
    card.append(heading, description, controls);
    el.column.appendChild(card);
  }

  // Originals displaced by approved edits live in the app's private store, outside
  // project folders. Restore and removal are explicit; age-based cleanup never
  // removes content that changed while an edit was being approved.
  async function openEditBackups() {
    const result = await call('edit_backups', 'list');
    if (!result) return;
    const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
    const detail = `Previous versions of files you approved edits for. Stored privately in ${result.store}, outside your project folders.`;
    openDialog({title: 'Edit backups', detail, wide: true,
      actions: [{label: `Remove backups older than ${result.retention_days} days`, value: 'prune'}, {label: 'Close', value: false, focus: true}],
      onClose: async action => {
        if (action !== 'prune') return;
        const pruned = await call('edit_backups', 'prune');
        if (!pruned) return;
        flash(`Removed ${plural(pruned.removed, 'old backup')}` + (pruned.kept ? `; kept ${pruned.kept} with conflicts or in project folders.` : '.'));
        openEditBackups();
      }});
    const list = node('div', 'workspace-files');
    if (!result.items.length) list.appendChild(node('p', 'note', 'No edit backups in this conversation.'));
    // Backups from deleted chats can be previewed and removed, but not restored.
    const orphans = result.deleted_chats || [];
    for (const item of [...result.items, ...orphans]) {
      const restorable = !orphans.includes(item);
      if (item === orphans[0]) list.appendChild(node('p', 'note', `From deleted chats (${orphans.length})`));
      const when = new Date(item.created_at).toLocaleString();
      const row = node('div', 'workspace-file');
      row.appendChild(node('span', '', [item.filename, when, item.conflict && 'changed during approval',
        item.restored_at && 'restored', !item.stored && `kept at ${item.location}`].filter(Boolean).join(' · ')));
      row.title = item.path;
      const confirm = (title, text, label, run) => openDialog({title, detail: text,
        actions: [{label: 'Cancel', value: false, focus: true}, {label, value: true, destructive: true}],
        onClose: async ok => { if (ok) await run(); openEditBackups(); }});
      const preview = node('button', 'btn quiet', 'Preview');
      preview.onclick = async () => {
        const data = await call('edit_backups', 'preview', item.id);
        if (!data) return;
        openDialog({title: `Backup of ${item.filename}`, wide: true,
          detail: `${item.path} · ${when}\n\n${data.text}${data.truncated ? '\n\n[Preview truncated]' : ''}`,
          actions: [{label: 'Back', value: true}, {label: 'Close', value: false}],
          onClose: back => { if (back) openEditBackups(); }});
        el.dialogDetail.style.whiteSpace = 'pre-wrap';
      };
      const restore = node('button', 'btn quiet', 'Restore');
      restore.onclick = () => confirm(`Restore ${item.filename}?`,
        `${item.path} returns to its version from ${when}. The current version becomes a backup. ` +
        'Restore is refused if the file changed after this edit.', 'Restore', async () => {
          if (await call('edit_backups', 'restore', item.id)) flash(`Restored ${item.filename}.`);
        });
      const remove = node('button', 'btn quiet', 'Remove');
      remove.onclick = () => confirm(`Remove this backup of ${item.filename}?`, item.conflict ?
        'This copy holds changes made while the edit was being approved. They may exist nowhere else.' :
        (item.stored ? 'The backup is deleted from the backup store.' : `Blackrose stops listing it; the file stays at ${item.location}.`),
        'Remove', async () => { await call('edit_backups', 'remove', item.id); });
      row.append(...(restorable ? [preview, restore, remove] : [preview, remove]));
      list.appendChild(row);
    }
    el.dialogDetail.appendChild(list);
  }

  async function openWorkspace(path = '.', offset = 0) {
    const result = await call('workspace_browse', path, offset);
    if (!result) return;
    openDialog({title: 'Workspace', detail: workspace?.path || 'No workspace · outputs save to Downloads/Blackrose', wide: true,
      actions: [{label: 'Choose directory', value: 'choose'}, {label: 'Clear workspace', value: 'clear'},
        {label: 'Edit backups', value: 'backups'}, {label: 'Close', value: false}],
      onClose: async action => {
        if (action === 'choose') await call('pick_workspace');
        if (action === 'clear') await call('clear_workspace');
        if (action === 'backups') await openEditBackups();
      }});
    const list = node('div', 'workspace-files');
    if (workspace && path !== '.') {
      const up = node('button', 'btn quiet', 'Workspace root'); up.onclick = () => openWorkspace(); list.appendChild(up);
    }
    for (const file of result.entries || []) {
      const row = node('div', 'workspace-file');
      row.appendChild(node('span', '', `${file.kind === 'directory' ? '▸ ' : ''}${file.filename}`));
      row.title = file.path;
      if (file.kind === 'directory') {
        const browse = node('button', 'btn quiet', 'Browse'); browse.onclick = () => openWorkspace(file.path); row.appendChild(browse);
      } else {
        const preview = node('button', 'btn quiet', 'Preview');
        preview.onclick = async () => {
          const data = await call('workspace_preview', file.path);
          if (data) openDialog({title: file.filename, html: data.html, wide: true, actions: [{label: 'Close', value: false}]});
        }; row.appendChild(preview);
      }
      const mention = node('button', 'btn quiet', 'Reference');
      mention.onclick = () => { closeDialog(false); insertReference(file); }; row.appendChild(mention);
      list.appendChild(row);
    }
    if (result.next_offset != null) {
      const next = node('button', 'btn quiet', 'More files'); next.onclick = () => openWorkspace(path, result.next_offset); list.appendChild(next);
    }
    if (result.scan_truncated) list.appendChild(node('p', 'note', 'Listing limit reached. Browse a subdirectory or use @ search.'));
    el.dialogDetail.appendChild(list);
  }

  // --- popovers: commands ----------------------------------------------------------------

  const commandPresence = new Presence(el.commands, { from: 0.98 });
  const popover = { open: false, items: [], index: 0, reference: null, referenceDetail: null };
  let selectedRefs = [];
  let suggestionTimer = null;
  let suggestionEpoch = 0;
  let workspace = null;

  function retainedRefs() { return selectedRefs.filter(r => el.input.value.includes(r.token)); }

  function insertReference(ref, start = el.input.selectionStart, end = el.input.selectionEnd) {
    const token = ref.kind === 'skill' ? `$${ref.id}` : `@{${ref.path}}`;
    const item = {...ref, token};
    selectedRefs = [...retainedRefs().filter(r => r.token !== token), item];
    el.input.focus();
    el.input.setRangeText(token + ' ', start, end, 'end');
    closeCommands();
    autosize();
    call('update_draft', el.input.value, retainedRefs());
  }

  function placeAbove(menu, anchor) {
    const a = anchor.getBoundingClientRect();
    menu.style.left = `${Math.max(16, a.left)}px`;
    menu.style.bottom = `${window.innerHeight - a.top + 8}px`;
    menu.style.top = "auto";
  }

  function drawSuggestions() {
    const items = popover.items;
    const detail = popover.referenceDetail;
    if (!items.length && !detail?.message) { closeCommands(); return; }
    if (items.length) popover.index = Math.min(popover.index, items.length - 1);
    el.commands.replaceChildren();
    items.forEach((c, i) => {
      const li = node('li');
      li.setAttribute('role', 'option'); li.id = `cmd-${i}`;
      li.setAttribute('aria-selected', i === popover.index ? 'true' : 'false');
      li.appendChild(node('span', 'usage', c.usage));
      li.appendChild(node('span', 'summary', c.summary));
      li.addEventListener('pointerdown', e => { e.preventDefault(); popover.index = i; completeCommand(); });
      el.commands.appendChild(li);
    });
    if (detail?.message) {
      const li = node('li', 'command-status');
      li.setAttribute('role', 'presentation');
      li.appendChild(node('span', 'summary', detail.message));
      if (detail.scan_truncated && workspace) {
        const browse = node('button', 'btn quiet', 'Browse workspace');
        browse.type = 'button';
        browse.addEventListener('click', () => {
          ++suggestionEpoch;
          if (popover.reference) el.input.setSelectionRange(popover.reference.start, popover.reference.end);
          closeCommands();
          openWorkspace();
        });
        li.appendChild(browse);
      }
      el.commands.appendChild(li);
    }
    if (items.length) el.input.setAttribute('aria-activedescendant', `cmd-${popover.index}`);
    else el.input.removeAttribute('aria-activedescendant');
    el.input.setAttribute('aria-expanded', 'true');
    if (!popover.open) {
      popover.open = true; placeAbove(el.commands, el.composer);
      commandPresence.show(el.commands, el.input);
    }
  }

  function updateCommandPopover() {
    clearTimeout(suggestionTimer);
    const epoch = ++suggestionEpoch;
    const value = el.input.value;
    const match = /^\/(\S*)$/.exec(value);
    if (match) {
      popover.reference = null;
      popover.referenceDetail = null;
      popover.items = state.commands.filter(c => c.usage.toLowerCase().startsWith(`/${match[1].toLowerCase()}`));
      drawSuggestions(); return;
    }
    const cursor = el.input.selectionStart;
    const before = value.slice(0, cursor);
    const bracedMention = /(?:^|\s)@\{([^}\n]*)$/.exec(before);
    const mention = bracedMention ? null : /(?:^|\s)([@$])([^\s@${}]*)$/.exec(before);
    if (!mention && !bracedMention) { closeCommands(); return; }
    const trigger = bracedMention ? '@' : mention[1];
    const query = bracedMention ? bracedMention[1] : mention[2];
    const mentionStart = cursor - query.length - (bracedMention ? 2 : 1);
    // Close old results while the debounced request is in flight, preventing
    // selection of a stale path/query after further typing or switching chats.
    closeCommands();
    suggestionTimer = setTimeout(async () => {
      const detail = await call('reference_suggestions', trigger, query, true);
      if (epoch !== suggestionEpoch || value !== el.input.value || cursor !== el.input.selectionStart) return;
      popover.reference = {start: mentionStart, end: cursor};
      const response = detail && !Array.isArray(detail) ? detail : {items: detail || []};
      popover.referenceDetail = response;
      popover.items = (response.items || []).map(r => ({ref: r,
        usage: r.kind === 'skill' ? `$${r.id}` : `${r.kind === 'directory' ? '▸ ' : ''}${r.filename}`,
        summary: r.description || `${r.extension || r.kind} · ${r.path}`}));
      drawSuggestions();
    }, 180);
  }

  function closeCommands() {
    if (!popover.open) return;
    popover.open = false;
    popover.index = 0;
    el.input.setAttribute('aria-expanded', 'false');
    el.input.removeAttribute("aria-activedescendant");
    commandPresence.hide(el.commands);
  }

  function completeCommand() {
    const chosen = popover.items[popover.index];
    if (!chosen) return;
    if (chosen.ref && popover.reference) {
      const {start, end} = popover.reference; insertReference(chosen.ref, start, end); return;
    }
    const head = chosen.usage.split(" ")[0];
    const takesArgument = chosen.usage.includes("<");
    el.input.value = takesArgument ? `${head} ` : head;
    closeCommands();
    autosize();
    el.input.focus();
  }

  function commandKey(e) {
    const key = e.key;
    if (key === "ArrowDown" || key === "ArrowUp") {
      if (!popover.items.length) return true;
      e.preventDefault();
      const step = key === "ArrowDown" ? 1 : -1;
      popover.index = (popover.index + step + popover.items.length) % popover.items.length;
      drawSuggestions();
      return true;
    }
    if (key === "Tab" || (key === "Enter" && !e.ctrlKey && !e.altKey)) {
      if (!popover.items.length) return false;
      e.preventDefault();
      completeCommand();
      return true;
    }
    if (key === "Escape") { e.preventDefault(); ++suggestionEpoch; clearTimeout(suggestionTimer); closeCommands(); return true; }
    return false;
  }

  document.addEventListener('pointerdown', (e) => {
    if (popover.open && !el.commands.contains(e.target) && e.target !== el.input) closeCommands();
  });

  // --- panes: the sidebar's width and presence ------------------------------------------------------
  //
  // One spring, --gb-sidebar-p, says how present the sidebar is. On a wide
  // window it closes the sidebar's grid column, handing the room to the
  // conversation; on a narrow one the sidebar floats over a dimmed page.
  // Its width is dragged by the edge handle: tracked 1:1 from where it was
  // grabbed, rubber-banded past its limits, released into a spring carrying
  // the pointer's velocity. A flick inward past the minimum hides it.

  const layout = { width: null, collapsed: false, wrap: false };
  const paneSpring = new Spring(1);
  const widthSpring = new Spring(0);
  let overlayOpen = false;

  const rem = () => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  // Reserve a 32rem conversation pane plus the sidebar's 12.5rem minimum
  // and 1rem margin. Root text scaling must affect this breakpoint too.
  const needsOverlay = () => window.innerWidth <= Math.max(760, 45.5 * rem());
  let overlayMode = needsOverlay();
  /** The sidebar may take what the conversation can spare, keeping it readable. */
  function sidebarBounds() {
    const lo = 12.5 * rem();
    const hi = Math.max(lo, Math.min(28 * rem(), window.innerWidth - 33 * rem()));
    return [lo, hi];
  }
  /** The same starting width the stylesheet gives: clamp(15rem, 21vw, 19rem). */
  function defaultWidth() {
    return Math.min(19 * rem(), Math.max(15 * rem(), window.innerWidth * 0.21));
  }
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  function drawWidth(px) { el.app.style.setProperty("--gb-sidebar-width", `${px.toFixed(1)}px`); }
  function sidebarWidth() { return el.sidebar.getBoundingClientRect().width; }

  function sidebarShown() { return overlayMode ? overlayOpen : !layout.collapsed; }

  function drawPane(p) { el.app.style.setProperty("--gb-sidebar-p", p.toFixed(4)); }

  function applyPane({ animate = true, onRest } = {}) {
    const open = sidebarShown();
    const overlay = overlayMode;
    el.app.classList.toggle("is-overlay", overlay);
    el.app.classList.toggle("is-collapsed", !overlay && layout.collapsed);
    el.app.classList.toggle("is-overlay-open", overlay && overlayOpen);
    el.app.classList.remove("is-settled");
    el.sidebar.inert = !open;
    el.menuToggle.setAttribute("aria-expanded", open ? "true" : "false");
    const label = open ? "Hide conversations" : "Show conversations";
    el.menuToggle.setAttribute("aria-label", label);
    el.menuToggle.title = `${label} (Ctrl \\)`;
    const rest = () => { el.app.classList.add("is-settled"); if (onRest) onRest(); };
    if (!animate) { paneSpring.jump(open ? 1 : 0); drawPane(paneSpring.value); rest(); return; }
    paneSpring.to(open ? 1 : 0, drawPane, rest);
  }

  function saveLayout() {
    call("save_layout", { sidebar: layout.width, collapsed: layout.collapsed, wrap: layout.wrap });
  }

  function setSidebar(open) {
    if (overlayMode) {
      overlayOpen = open;
    } else {
      layout.collapsed = !open;
      saveLayout();
    }
    if (!open && el.sidebar.contains(document.activeElement)) el.input.focus();
    applyPane();
  }
  function toggleSidebar() { setSidebar(!sidebarShown()); }
  function closeSidebarIfOverlay() { if (overlayMode && overlayOpen) setSidebar(false); }

  el.menuToggle.addEventListener("click", () => toggleSidebar());
  // A tap on the dimmed page puts the floating list away.
  document.addEventListener("pointerdown", (e) => {
    if (overlayMode && overlayOpen && !el.sidebar.contains(e.target)
        && !el.menuToggle.contains(e.target)) setSidebar(false);
  });

  function describeResizer() {
    const [lo, hi] = sidebarBounds();
    el.resizer.setAttribute("aria-valuemin", String(Math.round(lo)));
    el.resizer.setAttribute("aria-valuemax", String(Math.round(hi)));
    el.resizer.setAttribute("aria-valuenow", String(Math.round(sidebarWidth())));
  }

  /** Width to rest at: the user's, kept inside what this window allows. */
  function settleWidth(velocity = 0) {
    const [lo, hi] = sidebarBounds();
    const from = sidebarWidth();
    const target = clamp(layout.width === null ? defaultWidth() : layout.width, lo, hi);
    widthSpring.jump(from);
    widthSpring.velocity = velocity;
    widthSpring.to(target, drawWidth, () => {
      describeResizer();
    });
  }

  // Past the limit, the edge follows less the further it is pulled.
  function rubberband(overshoot, dimension, constant = 0.55) {
    return (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));
  }
  // Where a flick would come to rest (Apple's deceleration projection).
  function project(velocity, rate = 0.998) { return ((velocity / 1000) * rate) / (1 - rate); }

  let drag = null;
  el.resizer.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || overlayMode) return;
    e.preventDefault();
    try { el.resizer.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
    widthSpring.jump();
    const start = sidebarWidth();
    drag = { id: e.pointerId, start, grab: e.clientX - start, samples: [{ x: e.clientX, t: e.timeStamp }] };
    el.app.classList.add("is-resizing");
  });
  el.resizer.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const [lo, hi] = sidebarBounds();
    const raw = e.clientX - drag.grab;
    let width = raw;
    if (raw < lo) width = lo - rubberband(lo - raw, lo);
    else if (raw > hi) width = hi + rubberband(raw - hi, hi);
    drawWidth(width);
    drag.samples.push({ x: e.clientX, t: e.timeStamp });
    while (drag.samples.length > 2 && e.timeStamp - drag.samples[0].t > 100) drag.samples.shift();
  });
  function endDrag(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const { samples, grab, start } = drag;
    drag = null;
    el.app.classList.remove("is-resizing");
    const first = samples[0];
    const last = { x: e.clientX, t: e.timeStamp };
    const dt = (last.t - first.t) / 1000;
    const velocity = dt > 0.008 ? (last.x - first.x) / dt : 0;
    const [lo] = sidebarBounds();
    const projected = last.x - grab + project(velocity);
    if (velocity < 0 && projected < lo - 6 * rem()) {
      // Thrown shut: hide it, and keep the width it had for next time.
      layout.width = start;
      layout.collapsed = true;
      saveLayout();
      if (el.sidebar.contains(document.activeElement)) el.input.focus();
      applyPane({ onRest: () => { widthSpring.jump(start); drawWidth(start); describeResizer(); } });
      return;
    }
    const [, hi] = sidebarBounds();
    layout.width = clamp(sidebarWidth(), lo, hi);
    saveLayout();
    settleWidth(velocity);
  }
  el.resizer.addEventListener("pointerup", endDrag);
  el.resizer.addEventListener("pointercancel", endDrag);
  el.resizer.addEventListener("dblclick", () => { layout.width = null; saveLayout(); settleWidth(); });
  el.resizer.addEventListener("keydown", (e) => {
    const [lo, hi] = sidebarBounds();
    const step = e.shiftKey ? 3 * rem() : rem();
    const now = layout.width === null ? sidebarWidth() : layout.width;
    let next = null;
    if (e.key === "ArrowLeft") next = now - step;
    else if (e.key === "ArrowRight") next = now + step;
    else if (e.key === "Home") next = lo;
    else if (e.key === "End") next = hi;
    else if (e.key === "Enter") { e.preventDefault(); toggleSidebar(); return; }
    if (next === null) return;
    e.preventDefault();
    layout.width = clamp(next, lo, hi);
    saveLayout();
    settleWidth();
  });

  function fitPanes() {
    const overlay = needsOverlay();
    if (overlay !== overlayMode) {
      overlayMode = overlay;
      overlayOpen = false;
      if (!sidebarShown() && (el.sidebar.contains(document.activeElement) || document.activeElement === el.resizer)) el.menuToggle.focus();
      applyPane({ animate: false });
    }
    if (drag) return;
    const [lo, hi] = sidebarBounds();
    widthSpring.jump();
    drawWidth(clamp(layout.width === null ? defaultWidth() : layout.width, lo, hi));
    describeResizer();
  }
  let resizeFrame = 0;
  function schedulePaneFit() {
    if (resizeFrame) return;
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      fitPanes();
    });
  }
  window.addEventListener("resize", schedulePaneFit);
  // A 1rem probe also catches text-size changes without a window resize.
  const paneScale = document.createElement("span");
  paneScale.setAttribute("aria-hidden", "true");
  paneScale.style.cssText = "position:fixed;width:1rem;height:1rem;visibility:hidden;pointer-events:none";
  el.app.appendChild(paneScale);
  new ResizeObserver(schedulePaneFit).observe(paneScale);

  // The narrow-screen conversation drawer starts beneath the wrapping toolbar.
  new ResizeObserver(([entry]) => {
    el.app.style.setProperty("--gb-toolbar-h", `${entry.target.offsetHeight}px`);
  }).observe($("app-toolbar"));

  // A tall composer needs an in-flow row in short/large-text panes. Observe
  // actual content sizes so this also responds to attachments and autosizing.
  const mainPane = $("main");
  const paneHeader = mainPane.querySelector(".header");
  function revealPaneFocus() {
    const target = document.activeElement;
    if (!mainPane.classList.contains("is-height-constrained") ||
        target === mainPane || !mainPane.contains(target)) return;
    const pane = mainPane.getBoundingClientRect();
    const control = target.getBoundingClientRect();
    // WebKit can leave a focused textarea partially below a scrolling grid.
    // Scroll only this pane, preserving the toolbar and transcript position.
    if (control.bottom > pane.bottom - 4) mainPane.scrollTop += Math.ceil(control.bottom - pane.bottom + 4);
    else if (control.top < pane.top + 4) mainPane.scrollTop -= Math.ceil(pane.top + 4 - control.top);
  }
  mainPane.addEventListener("focusin", () => requestAnimationFrame(revealPaneFocus));
  const fitComposer = new ResizeObserver(() => {
    const readingRoom = 8 * parseFloat(getComputedStyle(document.documentElement).fontSize);
    mainPane.classList.toggle("is-height-constrained",
      paneHeader.offsetHeight + el.dock.offsetHeight + readingRoom > mainPane.clientHeight);
    requestAnimationFrame(revealPaneFocus);
  });
  [mainPane, paneHeader, el.dock].forEach(element => fitComposer.observe(element));

  // The dock floats over the transcript; the transcript leaves room for it.
  new ResizeObserver(() => {
    const atBottom = nearBottom();
    document.documentElement.style.setProperty("--gb-dock-h", `${el.dock.offsetHeight}px`);
    follow(atBottom);
  }).observe(el.dock);

  // Reflow the input when its available width changes, including placeholder
  // wrapping. Ignore height changes from autosize itself.
  let composerWidth = 0;
  new ResizeObserver(([entry]) => {
    if (entry.contentRect.width === composerWidth) return;
    composerWidth = entry.contentRect.width;
    autosize();
  }).observe(el.composer);

  // When the column reflows (a pane resized, the window resized), a reader
  // who was at the end stays at the end.
  let pinned = true;
  el.transcript.addEventListener("scroll", () => { pinned = nearBottom(); }, { passive: true });
  new ResizeObserver(() => {
    if (pinned) el.transcript.scrollTop = el.transcript.scrollHeight;
  }).observe(el.transcript);

  // --- code: wrap long lines, for every block at once ---------------------------------------------

  function markWrap(root) {
    root.querySelectorAll(".code-wrap").forEach((b) => b.setAttribute("aria-pressed", layout.wrap ? "true" : "false"));
  }
  function setWrap(wrap, anchor) {
    // Keep the block that was clicked where it is while every block reflows.
    const before = anchor ? anchor.getBoundingClientRect().top : 0;
    layout.wrap = wrap;
    el.app.classList.toggle("wraps-code", wrap);
    markWrap(document);
    if (anchor) el.transcript.scrollTop += anchor.getBoundingClientRect().top - before;
  }

  // --- composer ------------------------------------------------------------------------------------

  function autosize() {
    el.input.style.height = "auto";
    el.input.style.height = `${el.input.scrollHeight}px`;
  }

  function insertText(text) {
    const input = el.input;
    input.focus();
    input.setRangeText(text, input.selectionStart, input.selectionEnd, "end");
    autosize();
    updateCommandPopover();
    call('update_draft', el.input.value, retainedRefs());
  }

  function send() {
    if (state.rigorPending) return;
    const text = el.input.value;
    const refs = retainedRefs();
    selectedRefs = [];
    ++suggestionEpoch; clearTimeout(suggestionTimer);
    closeCommands();
    // Clear before dispatch so an immediate cancellation can restore the
    // draft without a later acknowledgement erasing that restoration.
    el.input.value = '';
    autosize();
    call("submit", text, refs).then((result) => {
      if ((!result || !result.clear) && !el.input.value) {
        el.input.value = text; selectedRefs = refs; autosize();
      }
    });
  }

  el.composer.addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.rigorPending) return;
    if (state.busy) call("stop");
    else send();
  });
  el.input.addEventListener("input", () => {
    autosize(); updateCommandPopover(); call('update_draft', el.input.value, retainedRefs());
  });
  let retryPending = false;
  async function retryPrompt() {
    if (retryPending || state.busy || state.rigorPending || !state.status.can_retry) return;
    retryPending = true;
    el.retry.disabled = true;
    try { await call('retry'); }
    finally { retryPending = false; renderControls(); }
  }
  el.retry.addEventListener('click', retryPrompt);
  el.attach.addEventListener("click", () => call("pick_files").then(() => el.input.focus()));
  el.newChat.addEventListener("click", () => newConversation());
  $("chat-older").addEventListener("click", () => loadOlderConversations());
  el.webToggle.addEventListener("click", () => call("set_web", !state.status.web));
  el.modelsButton.addEventListener('click', openModels);
  el.libraryButton.addEventListener('click', () => openLibrary());
  $('workspace-button').addEventListener('click', () => openWorkspace());
  el.input.setAttribute('aria-controls', 'commands');
  el.input.setAttribute('aria-autocomplete', 'list');
  el.input.addEventListener('click', updateCommandPopover);
  el.skillsButton.addEventListener('click', openSkills);
  el.knowledgeToggle.addEventListener('click', () => call('set_knowledge', !state.status.knowledge));
  el.memoryToggle.addEventListener('click', () => call('set_memory', !state.status.memory));
  el.trustedButton.addEventListener('click', () => { closeSources({ restoreFocus: false }); openTrustedSites(); });
  el.sources.addEventListener('click', toggleSources);
  el.citationRigor.addEventListener('click', (e) => {
    const radio = e.target.closest('[role="radio"]');
    if (radio) setRigor(radio.dataset.mode);
  });
  el.citationRigor.addEventListener('keydown', (e) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const radios = rigorRadios();
    const at = Math.max(0, radios.indexOf(document.activeElement));
    const next = radios[(at + step + radios.length) % radios.length];
    next.focus();
    setRigor(next.dataset.mode);
  });
  // The panel sits after the page in the DOM, so Tab is carried across it by hand:
  // forward out of it continues after the trigger, backward returns to the trigger.
  el.sourcesPanel.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const stops = [...el.sourcesPanel.querySelectorAll('button:not([disabled])')].filter(b => b.tabIndex >= 0);
    const at = stops.indexOf(document.activeElement);
    if (e.shiftKey ? at > 0 : at < stops.length - 1) return;
    e.preventDefault();
    closeSources({ restoreFocus: false });
    if (e.shiftKey) { el.sources.focus(); return; }
    const order = [...el.composer.querySelectorAll('button:not([disabled]), textarea')];
    (order[order.indexOf(el.sources) + 1] || el.input).focus();
  });
  // Clicking away puts the panel away without stealing focus back.
  el.sourcesPanel.addEventListener('focusout', (e) => {
    const to = e.relatedTarget;
    if (sourcesOpen && to && !el.sourcesPanel.contains(to) && to !== el.sources) closeSources({ restoreFocus: false });
  });
  document.addEventListener('pointerdown', (e) => {
    if (sourcesOpen && !el.sourcesPanel.contains(e.target) && !el.sources.contains(e.target)) closeSources({ restoreFocus: false });
  });
  window.addEventListener('resize', () => { if (sourcesOpen) placeSources(); });
  el.effort.addEventListener('click', cycleEffort);
  el.helpButton.addEventListener("click", () => on.help());

  function newConversation() {
    call("new_conversation").then((ok) => { if (ok) { closeSidebarIfOverlay(); el.input.focus(); } });
  }

  // Press feedback lands on pointer-down, before the click commits.
  document.addEventListener("pointerdown", (e) => {
    const button = e.target.closest && e.target.closest(".btn");
    if (button && !button.disabled) button.classList.add("is-pressed");
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach((type) => {
    document.addEventListener(type, () => {
      document.querySelectorAll(".btn.is-pressed").forEach((b) => b.classList.remove("is-pressed"));
    }, true);
  });

  // --- paste and drop -------------------------------------------------------------------------------

  const PATHLIKE = /^(~|\/|\.{1,2}\/|[A-Za-z]:\\)[^\n]*\.(png|jpe?g|webp|pdf|docx|txt|md|markdown|csv|json|ya?ml|toml|py|js|ts|rs|go|c|h|cpp|java|rb|sh|html|css|sql)$/i;

  function readAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  async function attachFile(file, name) {
    const data = await readAsBase64(file);
    return call("attach_encoded", name || file.name || "attachment", data);
  }

  document.addEventListener("paste", (e) => {
    if (state.dialog) return;
    const data = e.clipboardData;
    const items = data ? [...data.items] : [];
    const image = items.find((item) => item.kind === "file" && item.type.startsWith("image/"));
    if (image) {
      e.preventDefault();
      const ext = image.type.split("/")[1] === "jpeg" ? "jpg" : image.type.split("/")[1];
      attachFile(image.getAsFile(), `clipboard-image.${ext}`);
      return;
    }
    const text = data ? data.getData("text/plain") : "";
    const empty = !el.input.value.trim();
    if (!text) {
      // The page could not see the clipboard; ask the desktop's own.
      e.preventDefault();
      call("paste", empty).then((r) => { if (r && r.text) insertText(r.text); });
      return;
    }
    const candidate = text.trim().replace(/^['"]|['"]$/g, "");
    if (empty && !candidate.includes("\n") && PATHLIKE.test(candidate)) {
      e.preventDefault();
      call("attach_path", candidate, true).then((ok) => { if (!ok) insertText(text); });
      return;
    }
    if (document.activeElement !== el.input) {
      e.preventDefault();
      insertText(text);
    }
  }, true);

  ["dragover", "drop"].forEach((type) => {
    document.addEventListener(type, (e) => {
      if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault();
      if (type === "drop") [...e.dataTransfer.files].forEach((file) => attachFile(file));
    });
  });

  // --- links and selection -----------------------------------------------------------------------------

  el.transcript.addEventListener("click", (e) => {
    if (e.defaultPrevented || !e.target.closest) return;
    const citation = e.target.closest('.claim-citation');
    if (citation) {
      const report = citation.closest('.msg')?._audit;
      const claim = report?.claims?.find(c => c.id === citation.dataset.claimId);
      if (claim) openClaim(report, claim, citation);
      return;
    }
    const copy = e.target.closest(".code-copy");
    if (copy) return copyCode(copy);
    const wrap = e.target.closest(".code-wrap");
    if (wrap) { setWrap(!layout.wrap, wrap.closest("figure.code")); saveLayout(); return; }
    const link = e.target.closest("a[href]");
    if (!link) return;
    e.preventDefault();
    const href = link.getAttribute("href");
    if (href.startsWith("#")) jumpWithin(link, href.slice(1));
    else call("open_link", href);
  });

  function copyCode(button) {
    const code = button.closest("figure.code").querySelector("pre code");
    call("copy", code.textContent.replace(/\n$/, "")).then((ok) => {
      if (!ok) return;
      button.textContent = "Copied";
      button.classList.add("done");
      clearTimeout(button._reset);
      button._reset = setTimeout(() => { button.textContent = "Copy"; button.classList.remove("done"); }, 1600);
    });
  }

  // Footnotes link by id, and every answer numbers its own from 1, so a jump
  // stays inside the answer it started in.
  function jumpWithin(link, id) {
    const scope = link.closest(".prose");
    const target = scope && [...scope.querySelectorAll("[id]")].find((n) => n.id === id);
    if (!target) return;
    target.scrollIntoView({ block: "center", behavior: reduceMotion.matches ? "auto" : "smooth" });
    target.classList.remove("lit-fade");
    target.classList.add("lit");
    clearTimeout(target._fade);
    target._fade = setTimeout(() => target.classList.add("lit-fade"), 700);
  }

  // As in the terminal: letting go of a selection copies it.
  el.transcript.addEventListener("pointerup", () => {
    const selection = window.getSelection();
    let text = String(selection || '');
    const anchor = selection?.anchorNode?.parentElement?.closest('.msg');
    const focus = selection?.focusNode?.parentElement?.closest('.msg');
    if (text.trim() && anchor && anchor === focus && anchor._audit?.copy_annotations) {
      text += '\n\n--- Source assessments for this answer ---\n' + anchor._audit.copy_annotations;
    }
    if (text.trim()) call("copy", text).then((ok) => { if (ok) flash("Copied"); });
  });

  el.transcript.addEventListener('copy', event => {
    const selection = window.getSelection();
    const anchor = selection?.anchorNode?.parentElement?.closest('.msg');
    const focus = selection?.focusNode?.parentElement?.closest('.msg');
    if (String(selection).trim() && anchor === focus && anchor?._audit?.copy_annotations && event.clipboardData) {
      event.clipboardData.setData('text/plain', String(selection) + '\n\n--- Source assessments for this answer ---\n' + anchor._audit.copy_annotations);
      event.preventDefault();
    }
  });

  // --- keys ------------------------------------------------------------------------------------------------

  function comboOf(e) {
    const parts = [];
    if (e.ctrlKey || e.metaKey) parts.push("ctrl");
    if (e.altKey) parts.push("alt");
    if (e.shiftKey) parts.push("shift");
    // WebKitGTK can report shifted Tab as Unidentified (ISO_Left_Tab).
    let key = (e.code === "Tab" ? "Tab" : e.key).toLowerCase();
    if (key === " ") key = "space";
    parts.push(key);
    return parts.join("+");
  }

  function focusList() {
    if (!sidebarShown()) setSidebar(true);
    const current = el.chatList.querySelector('[aria-current="true"]') || el.chatList.querySelector(".chat-item");
    if (current) current.focus();
  }

  const actions = {
    send() { if (document.activeElement === el.input) send(); else el.input.focus(); },
    retry() { retryPrompt(); },
    stop() {
      if (hasSelection()) return false;           // let the copy happen
      if (state.busy) call("stop"); else bell();
      return true;
    },
    quit() { call("quit"); },
    new_conversation() { newConversation(); },
    focus_list() { focusList(); },
    focus_composer() { closeSidebarIfOverlay(); el.input.focus(); },
    delete_conversation() { state.deleteTrigger = document.activeElement; call("request_delete"); },
    paste_image() {
      // The paste event does the work; it only has to land in the composer.
      if (document.activeElement !== el.input) el.input.focus();
      return false;
    },
    toggle_sidebar() { toggleSidebar(); },
    open_models() { if (state.busy) bell(); else openModels(); },
    open_sources() { toggleSources(); },
    cycle_effort() {
      // Elsewhere Shift+Tab keeps moving focus backwards.
      if (document.activeElement !== el.input) return false;
      cycleEffort();
    },
  };

  document.addEventListener("keydown", (e) => {
    if (state.dialog) { dialogKey(e); return; }
    if (popover.open && document.activeElement === el.input && commandKey(e)) return;
    if (sourcesOpen && e.key === "Escape") { e.preventDefault(); closeSources(); return; }
    const action = state.keymap.get(comboOf(e));
    if (action && actions[action]) {
      const handled = actions[action]();
      if (handled !== false) e.preventDefault();
      return;
    }
    if (e.key === "Escape" && overlayMode && overlayOpen) setSidebar(false);
  }, true);

  // The conversation list moves with the arrow keys and opens with Enter.
  el.chatList.addEventListener("keydown", (e) => {
    const items = [...el.chatList.querySelectorAll(".chat-item")];
    const index = items.indexOf(document.activeElement);
    if (index < 0) return;
    let next = null;
    if (e.key === "ArrowDown") next = items[Math.min(index + 1, items.length - 1)];
    else if (e.key === "ArrowUp") next = items[Math.max(index - 1, 0)];
    else if (e.key === "Home") next = items[0];
    else if (e.key === "End") next = items[items.length - 1];
    else if (e.key === "Escape") { e.preventDefault(); el.input.focus(); return; }
    if (next) {
      e.preventDefault();
      items.forEach((item) => { item.tabIndex = item === next ? 0 : -1; });
      next.focus();
    }
  });

  // --- start ---------------------------------------------------------------------------------------------------

  // --- models, library and skills --------------------------------------------------------------

  function action(label, fn, style = 'quiet') {
    const button = node('button', `btn ${style}`, label);
    button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await fn(button); }
      catch (error) { flash(String(error.message || error)); }
      finally { button.disabled = false; }
    });
    return button;
  }

  function managementDialog(title, trigger) {
    openDialog({title, detail: '', wide: true, trigger,
      actions: [{label: 'Close', value: false, focus: true}]});
    const content = node('div', 'management');
    el.dialogDetail.replaceChildren(content);
    return content;
  }

  function explanation(parent, text) { parent.appendChild(node('p', 'muted', text)); }

  function panelError(parent, message) {
    const error = node('p', 'panel-error', message);
    error.setAttribute('role', 'alert');
    parent.prepend(error);
  }

  // The image provider is chosen here, independently of the chat model. Python decides
  // availability and reasons; this renders them and asks Python to change the choice.
  function imageProviderPicker(root, isOpen, onLabel = () => {}) {
    const section = node('section', 'image-provider');
    // The tab already says Images; the heading stays for assistive tech.
    const heading = node('h3', 'image-provider-heading visually-hidden', 'Image generation');
    heading.id = 'image-provider-heading';
    section.appendChild(heading);
    const group = node('div', 'image-provider-options');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-labelledby', heading.id);
    section.appendChild(group);
    const message = node('p', 'muted image-provider-message');
    message.setAttribute('role', 'status');
    section.appendChild(message);
    root.appendChild(section);
    let choosing = false;
    let countdown = null;
    const memoryNames = {not_loaded: 'Not loaded', loading: 'Loading', in_use: 'In use', releasing: 'Releasing'};

    // Python owns the keep-warm policy; this shows its state and counts down locally.
    function memorySlot(memory, wrap) {
      const line = node('p', 'muted image-provider-detail image-memory-state');
      line.id = 'image-memory-state';
      const deadline = Date.now() + (memory.seconds_left || 0) * 1000;
      const label = () => {
        if (memory.state !== 'loaded') return `Image model: ${memoryNames[memory.state] || 'Unknown'}`;
        const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
        return `Image model: Loaded · releases in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
      };
      line.textContent = label();
      wrap.appendChild(line);
      if (memory.state !== 'loaded') return;
      const release = node('button', 'btn quiet image-memory-release', 'Release now');
      release.type = 'button';
      release.setAttribute('aria-describedby', line.id);
      release.addEventListener('click', () => releaseMemory());
      wrap.appendChild(release);
      countdown = setInterval(() => {
        if (!isOpen()) { clearInterval(countdown); return; }
        line.textContent = label();
        if (Date.now() >= deadline) { clearInterval(countdown); refresh(); }
      }, 1000);
    }

    async function refresh() {
      const latest = await call('image_providers');
      if (isOpen() && latest && !latest.error && !choosing) draw(latest, undefined);
    }

    async function releaseMemory() {
      if (choosing) return;
      choosing = true;
      group.querySelectorAll('button').forEach(item => item.setAttribute('aria-disabled', 'true'));
      const result = await call('release_image_memory');
      choosing = false;
      if (!isOpen()) return;
      if (!result || result.error) {
        await refresh();
        message.textContent = result?.error || 'Could not release the image model. Try again.';
        return;
      }
      draw(result, 'local');
      message.textContent = 'Image model released.';
    }

    function draw(result, focusId) {
      clearInterval(countdown);
      group.replaceChildren();
      message.textContent = '';
      for (const option of result.options) {
        const pressed = (result.choice || '') === option.id;
        const blocked = result.busy || choosing || !option.available;
        // Unavailable options stay in the Tab order with aria-disabled, so keyboard
        // users can reach them and hear why they cannot be chosen.
        const unavailable = !option.available
          ? option.reason || `${option.name} is not available.`
          : 'Wait for the current reply or image to finish before changing image generation.';
        const wrap = node('div', 'image-provider-option-wrap');
        const button = node('button', 'btn quiet image-provider-option', option.name);
        button.type = 'button';
        button.dataset.optionId = option.id;
        button.setAttribute('aria-pressed', String(pressed));
        if (blocked && !pressed) button.setAttribute('aria-disabled', 'true');
        const described = node('p', 'muted image-provider-detail');
        described.id = `image-provider-detail-${option.id || 'automatic'}`;
        button.setAttribute('aria-describedby', described.id);
        // The cost always shows; the longer detail only for the choice in force,
        // or when it explains why an option can't be chosen.
        const more = [option.detail];
        if (!option.available && option.reason) more.push(option.reason);
        const rest = more.filter(Boolean).join(' · ');
        described.appendChild(node('span', 'image-provider-cost', option.cost || ''));
        if (rest) {
          const tail = node('span', 'image-provider-more', `${option.cost ? ' · ' : ''}${rest}`);
          described.appendChild(tail);
          described.classList.toggle('expanded', pressed || !option.available);
        }
        button.addEventListener('click', () => {
          if (choosing) return;
          if (button.getAttribute('aria-disabled') === 'true') {
            message.textContent = unavailable;
            return;
          }
          choose(option.id, result, button);
        });
        wrap.append(button, described);
        if (option.id === 'local' && result.local_memory) memorySlot(result.local_memory, wrap);
        if (option.id === 'openrouter' && result.models.length > 1) {
          const label = node('label', 'model-field', 'OpenRouter image model');
          const select = node('select', 'model-select');
          for (const id of result.models) {
            const item = node('option', null, id);
            item.value = id;
            item.selected = id === result.model;
            select.appendChild(item);
          }
          select.disabled = blocked;
          select.addEventListener('change', () => choose('openrouter', result, select, select.value));
          label.appendChild(select);
          wrap.appendChild(label);
        }
        group.appendChild(wrap);
      }
      onLabel(result.options.find(option => option.id === (result.choice || ''))?.name || '');
      const target = group.querySelector(`[data-option-id="${focusId ?? ''}"]:not([aria-disabled="true"])`)
        || group.querySelector('button:not([aria-disabled="true"])');
      if (focusId !== undefined && target) target.focus();
    }

    async function choose(id, result, control, model = null) {
      if (choosing) return;
      choosing = true;
      // aria-disabled rather than disabled keeps focus on the activated button.
      group.querySelectorAll('button').forEach(item => item.setAttribute('aria-disabled', 'true'));
      group.querySelectorAll('select').forEach(item => item.disabled = true);
      const saved = await call('select_image_provider', id, model);
      choosing = false;
      if (!isOpen()) return;
      if (!saved || saved.error) {
        const latest = await call('image_providers');
        if (!isOpen()) return;
        if (latest && !latest.error) draw(latest, undefined);
        message.textContent = saved?.error || 'Could not change the image provider. Try again.';
        return;
      }
      draw(saved, id);
      message.textContent = `Image generation: ${saved.label}.`;
    }

    call('image_providers').then(result => {
      if (!isOpen()) return;
      if (!result || result.error) {
        message.textContent = result?.error || 'Could not read image generation options.';
        return;
      }
      draw(result, undefined);
    });
  }

  async function openModels(provider = null, view = {}) {
    // Event handlers can pass a MouseEvent; only a provider ID is meaningful.
    if (typeof provider !== 'string') provider = null;
    if (!view || typeof view !== 'object' || view instanceof Event) view = {};
    provider = provider || (state.status.model?.startsWith('opencode-go/') ? 'opencode-go' : 'local');
    view.tab = view.tab === 'images' ? 'images' : 'chat';
    const root = managementDialog('Models', el.modelsButton);
    root.classList.add('models');
    const isOpen = () => root.isConnected && state.dialog && !el.scrim.hidden;

    // Chat and Images are separate choices; each tab shows only its own.
    const tablist = node('div', 'model-tabs');
    tablist.setAttribute('role', 'tablist');
    tablist.setAttribute('aria-label', 'Model type');
    root.appendChild(tablist);
    const tabs = {}, panels = {}, values = {};
    for (const [key, label] of [['chat', 'Chat'], ['images', 'Images']]) {
      const tab = node('button', 'model-tab');
      tab.type = 'button';
      tab.id = `model-tab-${key}`;
      tab.dataset.tab = key;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', `model-panel-${key}`);
      values[key] = node('span', 'model-tab-value');
      tab.append(node('span', 'model-tab-label', label), values[key]);
      tab.addEventListener('click', () => showTab(key, true));
      tablist.appendChild(tab);
      const panel = node('div', 'model-panel');
      panel.id = `model-panel-${key}`;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', tab.id);
      root.appendChild(panel);
      tabs[key] = tab;
      panels[key] = panel;
    }
    function showTab(key, focus = false) {
      view.tab = key;
      for (const [name, tab] of Object.entries(tabs)) {
        const selected = name === key;
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
        panels[name].hidden = !selected;
      }
      if (focus) tabs[key].focus();
    }
    tablist.addEventListener('keydown', (e) => {
      const order = ['chat', 'images'];
      const at = order.indexOf(view.tab);
      const next = {ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: order.length - 1}[e.key];
      if (next === undefined) return;
      e.preventDefault();
      showTab(order[(next + order.length) % order.length], true);
    });
    values.chat.textContent = state.status.model_display_name || '';
    showTab(view.tab);
    imageProviderPicker(panels.images, isOpen, label => { values.images.textContent = label; });

    const content = node('div', 'management model-picker');
    panels.chat.appendChild(content);
    const controls = node('div', 'model-controls');
    const providers = node('select', 'model-select model-provider');
    providers.setAttribute('aria-label', 'Model provider');
    [['local', 'Local · llama-router'], ['opencode-go', 'OpenCode Go']].forEach(([value, name]) => {
      const option = node('option', null, name);
      option.value = value;
      providers.appendChild(option);
    });
    providers.value = provider;
    providers.addEventListener('change', () => openModels(providers.value, {tab: 'chat'}));
    const searchLabel = node('label', 'model-field model-search-field');
    searchLabel.appendChild(node('span', 'visually-hidden', 'Search all models'));
    const search = node('input', 'model-search');
    search.type = 'search';
    search.placeholder = 'Name or model ID';
    search.value = view.query || '';
    search.disabled = true;
    searchLabel.appendChild(search);
    controls.append(providers, searchLabel);
    content.appendChild(controls);
    const listing = node('div', 'model-listing');
    content.appendChild(listing);
    explanation(listing, 'Loading models…');
    if (view.tab === 'images') tabs.images.focus(); else if (!view.restore) providers.focus();
    const result = await call('models', provider);
    if (!isOpen()) return;
    listing.replaceChildren();
    const refresh = () => openModels(provider, view);
    if (!result || result.error) {
      panelError(content, result?.error || 'Could not reach the model provider. Try again.');
      content.appendChild(action('Retry', refresh));
      return;
    }
    search.disabled = false;
    let favorites = new Set(result.favorites || []);
    let savingFavorite = false;
    let choosing = null;  // the model ID being loaded
    const announcement = node('span', 'visually-hidden');
    announcement.setAttribute('role', 'status');
    content.appendChild(announcement);
    const footer = node('div', 'model-footer');
    if (provider === 'opencode-go') explanation(footer, 'Uses your OpenCode Go subscription.');
    footer.appendChild(action('Refresh models', refresh));
    content.appendChild(footer);

    const catalog = new Map(result.items.map(model => [model.id, model]));
    const belongs = id => id && (id.startsWith('opencode-go/') ? 'opencode-go' : 'local') === provider;
    // Keep saved/current choices understandable even if a preset disappears.
    for (const id of [result.default, result.selected, ...favorites]) {
      if (belongs(id) && !catalog.has(id)) catalog.set(id, {id, name: id, unavailable: true});
    }
    const name = model => model.name || model.id;
    const sorted = [...catalog.values()].sort((a, b) => name(a).localeCompare(name(b)));

    function restoreFocus(id, control) {
      const row = [...listing.querySelectorAll('.model-row')].find(row => row.dataset.modelId === id);
      const target = row?.querySelector({favorite: '.model-favorite', more: '.model-more'}[control] || '.model-choice');
      if (target?.getClientRects().length) target.focus();
      else (listing.querySelector('.model-browse') || search).focus();
    }

    function showError(message) {
      content.querySelector('.panel-error')?.remove();
      panelError(content, message);
    }

    // One press switches models; Make default and the details sit one level deeper.
    async function choose(model, makeDefault) {
      if (choosing) return;
      choosing = model.id;
      render();
      restoreFocus(model.id, makeDefault ? 'more' : 'choice');
      const loaded = await call('select_model', model.id, makeDefault);
      choosing = null;
      if (!isOpen()) return;
      if (!loaded || loaded.error) {
        showError(loaded?.error || 'Could not select the model. Try again.');
        render();
        restoreFocus(model.id, makeDefault ? 'more' : 'choice');
        return;
      }
      if (loaded.ok && state.dialog && !el.scrim.hidden) {
        flash(makeDefault ? `${name(model)} is the startup default` : `Using ${name(model)}`);
        // The refreshed list returns focus to this row, not to search: a screen
        // reader otherwise keeps the search field as its place after the dialog
        // closes, and pulls focus back into the hidden dialog (bra-max).
        view.restore = {id: model.id, control: makeDefault ? 'more' : 'choice'};
        refresh();
      }
    }

    function modelRow(model) {
      const row = node('section', 'model-row');
      row.dataset.modelId = model.id;
      const current = model.id === result.selected;
      if (current) row.classList.add('current');
      const header = node('div', 'model-row-header');
      const choice = node('button', 'model-choice');
      choice.type = 'button';
      if (current) choice.setAttribute('aria-current', 'true');
      const generating = Boolean(state.status.generating);
      const blocked = model.unavailable || current || Boolean(choosing) || generating;
      if (blocked) choice.setAttribute('aria-disabled', 'true');
      const mark = node('span', 'model-check', current ? '✓' : '');
      mark.setAttribute('aria-hidden', 'true');
      choice.appendChild(mark);
      choice.appendChild(node('span', 'model-name', name(model)));
      const labels = [];
      if (model.id === result.default) labels.push('Default');
      if (current) labels.push('Current');
      const availability = {loaded: 'Loaded', loading: 'Loading', unloaded: 'Not loaded'}[model.status?.value];
      labels.push(model.unavailable ? 'Unavailable' : availability || 'Available');
      const summary = choosing === model.id ? 'Loading…' : labels.join(' · ');
      choice.appendChild(node('span', 'model-summary', summary));
      choice.title = current ? 'Current model'
        : model.unavailable ? 'This model is no longer offered by the provider'
        : generating ? 'Wait for the current reply to finish before switching models'
        : `Use ${name(model)}`;
      choice.addEventListener('click', () => {
        if (choice.getAttribute('aria-disabled') === 'true') {
          if (!current && !choosing) announcement.textContent = choice.title;
          return;
        }
        choose(model, false);
      });
      header.appendChild(choice);
      const favorite = node('button', 'btn quiet icon-btn model-favorite');
      favorite.type = 'button';
      favorite.disabled = savingFavorite;
      const isFavorite = favorites.has(model.id);
      favorite.setAttribute('aria-pressed', String(isFavorite));
      const favoriteLabel = `${isFavorite ? 'Remove' : 'Add'} ${name(model)} ${isFavorite ? 'from' : 'to'} Favorites`;
      favorite.setAttribute('aria-label', favoriteLabel);
      favorite.title = favoriteLabel;
      const star = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      star.setAttribute('viewBox', '0 0 24 24');
      star.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'm12 3 2.78 5.63L21 9.54l-4.5 4.39 1.06 6.2L12 17.2l-5.56 2.93 1.06-6.2L3 9.54l6.22-.91Z');
      star.appendChild(path);
      favorite.appendChild(star);
      favorite.addEventListener('click', async () => {
        if (savingFavorite) return;
        savingFavorite = true;
        listing.querySelectorAll('.model-favorite').forEach(button => button.disabled = true);
        const saved = await call('set_model_favorite', model.id, !isFavorite);
        savingFavorite = false;
        if (!isOpen()) return;
        if (!saved || saved.error) {
          listing.querySelectorAll('.model-favorite').forEach(button => button.disabled = false);
          showError(saved?.error || 'Could not save Favorites. Try again.');
          return;
        }
        content.querySelector('.panel-error')?.remove();
        favorites = new Set(saved.favorites);
        render();
        restoreFocus(model.id, 'favorite');
        announcement.textContent = `${name(model)} ${isFavorite ? 'removed from' : 'added to'} Favorites.`;
      });
      header.appendChild(favorite);
      const expanded = view.detail === model.id;
      const detailId = `model-detail-${[...catalog.keys()].indexOf(model.id)}`;
      const more = node('button', 'btn quiet icon-btn model-more', '⋯');
      more.type = 'button';
      more.setAttribute('aria-expanded', String(expanded));
      more.setAttribute('aria-controls', detailId);
      more.setAttribute('aria-label', `Details for ${name(model)}`);
      more.title = 'Details and startup default';
      more.addEventListener('click', () => {
        view.detail = expanded ? null : model.id;
        render();
        restoreFocus(model.id, 'more');
      });
      header.appendChild(more);
      row.appendChild(header);
      const detail = node('div', 'model-details');
      detail.id = detailId;
      detail.hidden = !expanded;
      explanation(detail, model.id);
      if (model.meta?.n_ctx || model.context_length) {
        explanation(detail, `${(model.meta?.n_ctx || model.context_length).toLocaleString()} context`);
      }
      const buttons = node('div', 'management-actions');
      const isDefault = model.id === result.default;
      const save = node('button', 'btn quiet model-default', isDefault ? 'Startup default' : 'Make default');
      save.type = 'button';
      save.disabled = model.unavailable || isDefault || Boolean(choosing) || generating;
      save.addEventListener('click', () => choose(model, true));
      buttons.appendChild(save);
      detail.appendChild(buttons);
      row.appendChild(detail);
      return row;
    }

    function render() {
      listing.replaceChildren();
      const query = search.value.trim().toLocaleLowerCase();
      view.query = search.value;
      const visible = sorted.filter(model => !model.unavailable || favorites.has(model.id)
        || model.id === result.default || model.id === result.selected);
      const list = (models, className = '') => {
        const group = node('div', `model-list ${className}`);
        models.forEach(model => group.appendChild(modelRow(model)));
        listing.appendChild(group);
        return group;
      };
      if (query) {
        const matches = visible.filter(model => `${name(model)} ${model.id}`.toLocaleLowerCase().includes(query));
        const label = `${matches.length} ${matches.length === 1 ? 'model' : 'models'} found`;
        explanation(listing, matches.length ? label : 'No models found. Try another name or model ID.');
        announcement.textContent = label;
        list(matches, 'model-results');
        return;
      }
      const ids = [...new Set([result.default, ...favorites, result.selected].filter(belongs))];
      const pinned = ids.map(id => catalog.get(id));
      if (pinned.length) list(pinned, 'model-pinned');
      const remaining = visible.filter(model => !ids.includes(model.id));
      if (remaining.length && ![...favorites].some(belongs)) {
        explanation(listing, 'Star models in Browse all models to keep them here.');
      }
      if (!result.items.length) explanation(listing, 'No answering models are available from this provider.');
      if (!remaining.length) return;
      const browse = node('button', 'btn quiet model-browse');
      browse.type = 'button';
      browse.setAttribute('aria-expanded', String(Boolean(view.expanded)));
      browse.setAttribute('aria-controls', 'model-catalog');
      const arrow = node('span', 'model-chevron', '›');
      arrow.setAttribute('aria-hidden', 'true');
      browse.append(arrow, node('span', null, 'Browse all models'), node('span', 'model-count', String(remaining.length)));
      listing.appendChild(browse);
      const others = list(remaining, 'model-catalog');
      others.id = 'model-catalog';
      others.hidden = !view.expanded;
      browse.addEventListener('click', () => {
        view.expanded = !view.expanded;
        browse.setAttribute('aria-expanded', String(view.expanded));
        others.hidden = !view.expanded;
      });
    }
    search.addEventListener('input', render);
    render();
    const restore = view.restore;
    view.restore = null;
    if (restore && view.tab === 'chat') restoreFocus(restore.id, restore.control);
    else if (view.tab === 'chat' && (document.activeElement === providers || !root.contains(document.activeElement))) search.focus();
  }

  // A skill is third-party text that can steer every conversation, so the user
  // sees exactly what is approved. Package text is only assigned through textContent.
  function skillReview(review) {
    const view = node('div', 'edit-review');
    const size = bytes => bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
    const count = `${review.resources.length}${review.resources_truncated ? '+' : ''}`;
    const summary = node('p', 'edit-review-summary', 'Check this text before the assistant can use it. Approval applies to exactly this SKILL.md.');
    summary.id = 'skill-review-summary';
    view.appendChild(summary);
    const facts = node('dl', 'source-facts');
    [['Skill ID', review.id], ['Name', review.name], ['Description', review.description],
     ['Imported from', review.source || 'Not recorded (installed before skill reviews)'],
     ['Installed at', review.location],
     ['Resources', `${count} ${review.resources.length === 1 ? 'file' : 'files'} · ${size(review.resource_bytes)}`],
     ['Approval allows', 'Its name and description are offered to the assistant in every conversation as untrusted metadata. Its instructions load when you select it with $ or the assistant reads it. Skills cannot add tools, grant file or web access, or approve edits.'],
     ['If it changes', 'Any later change to SKILL.md makes it unavailable until you review it again.']]
      .forEach(([term, value]) => { facts.appendChild(node('dt', '', term)); facts.appendChild(node('dd', '', value)); });
    view.appendChild(facts);
    const text = node('details', 'edit-review-full');
    text.open = true;
    text.appendChild(node('summary', '', 'SKILL.md: full text'));
    text.appendChild(node('pre', 'source-metadata', review.text));
    view.appendChild(text);
    const files = node('details', 'edit-review-full');
    files.open = true;
    files.appendChild(node('summary', '', `Other files in the package (${count})`));
    const list = node('ul', 'edit-review-notes');
    review.resources.forEach(file => list.appendChild(node('li', '',
      `${file.path} · ${file.kind === 'symlink' ? 'link (never read)' : size(file.bytes)}`)));
    if (!review.resources.length) list.appendChild(node('li', '', 'None.'));
    if (review.resources_truncated) list.appendChild(node('li', '', 'More files exist than this list shows.'));
    files.appendChild(list);
    view.appendChild(files);
    return view;
  }

  function openSkillReview(review, importing, onClose) {
    openDialog({title: `${importing ? 'Install' : 'Approve'} ${review.name}?`, wide: true, trigger: el.skillsButton,
      actions: [{label: 'Cancel', value: false, focus: true},
        {label: importing ? 'Approve and install' : 'Approve and enable', value: true}],
      onClose: async approve => {
        el.dialog.classList.remove('edit-review-dialog'); el.dialog.removeAttribute('aria-describedby');
        await onClose(approve === true);
        // Another dialog may have replaced this review; leave it in front.
        if (!state.dialog) openSkills();
      }});
    el.dialog.classList.add('edit-review-dialog');
    el.dialogDetail.replaceChildren(skillReview(review));
    el.dialog.setAttribute('aria-describedby', 'skill-review-summary');
  }

  async function openSkills() {
    const root = managementDialog('Skills', el.skillsButton);
    explanation(root, 'Skills are third-party instructions. Import a folder containing SKILL.md and any resources; you review its text before the assistant can use it.');
    const toolbar = node('div', 'management-actions');
    const importButton = action('Import skill…', async () => {
      const review = await call('import_skill');
      if (!review || review.cancelled) return;
      // Cancelling discards the staged copy; nothing is installed or enabled.
      openSkillReview(review, true, async approve => {
        const result = await call('finish_skill_import', review.token, approve);
        if (result && !result.cancelled) flash('Skill reviewed, installed and enabled');
      });
    });
    importButton.disabled = Boolean(state.status.generating);
    toolbar.appendChild(importButton);
    root.appendChild(toolbar);
    const skills = await call('skills');
    if (!root.isConnected) return;
    if (!skills) { panelError(root, 'Could not read installed skills.'); return; }
    if (!skills.length) explanation(root, 'No skills installed yet. Imported packages are copied into this app’s own skill library.');
    skills.forEach(skill => {
      const row = node('section', 'management-card');
      row.appendChild(node('h3', null, skill.name));
      explanation(row, skill.description);
      const buttons = node('div', 'management-actions');
      if (skill.review === 'approved') {
        const toggle = action(skill.enabled ? 'Enabled' : 'Disabled', async () => {
          if (await call('set_skill', skill.id, !skill.enabled)) openSkills();
        });
        toggle.setAttribute('role', 'switch');
        toggle.setAttribute('aria-checked', String(skill.enabled));
        toggle.setAttribute('aria-label', `Enable ${skill.name}`);
        toggle.disabled = Boolean(state.status.generating);
        buttons.appendChild(toggle);
      } else {
        explanation(row, skill.review === 'changed' ? 'SKILL.md changed after you approved it. Unavailable until reviewed again.'
          : 'Not reviewed yet. Unavailable to the assistant until you approve it.');
        const review = action('Review…', async () => {
          const current = await call('review_skill', skill.id);
          if (!current) return;
          openSkillReview(current, false, async approve => {
            if (approve && await call('approve_skill', skill.id, current.digest)) flash('Skill approved and enabled');
          });
        });
        review.setAttribute('aria-label', `Review ${skill.name}`);
        review.disabled = Boolean(state.status.generating);
        buttons.appendChild(review);
      }
      if (skill.has_scripts && skill.enabled) {
        explanation(row, skill.execution_granted
          ? 'Script execution permission stored. Every package file is checked before each run.'
          : 'Scripts require separate review and permission. Reading this skill runs nothing.');
        const scripts = action('Review scripts…', async () => {
          const review = await call('review_skill_execution', skill.id);
          if (!review) return;
          openDialog({title: 'Review skill scripts', wide: true, trigger: scripts,
            actions: [{label: 'Cancel', value: false, focus: true},
              {label: 'Continue to permission', value: true}],
            onClose: async yes => {
              if (yes && await call('approve_skill_execution', skill.id, review.digest)) flash('Script execution allowed');
              if (!state.dialog) openSkills();
            }});
          const content = node('div', 'edit-review');
          content.appendChild(node('p', '', review.permissions));
          Object.entries(review.sources).forEach(([path, body]) => {
            const section = node('details', 'edit-review-full');
            section.open = true;
            section.append(node('summary', '', path), node('pre', 'source-metadata', body));
            content.appendChild(section);
          });
          review.resources.forEach(r => content.appendChild(node('p', 'source-metadata',
            `${r.path} · ${r.bytes} bytes · SHA-256 ${r.sha256}`)));
          el.dialogDetail.replaceChildren(content);
        });
        scripts.disabled = Boolean(state.status.generating);
        buttons.appendChild(scripts);
        if (skill.execution_granted) {
          const revoke = action('Revoke execution', async () => {
            if (await call('revoke_skill_execution', skill.id)) openSkills();
          });
          revoke.disabled = Boolean(state.status.generating);
          buttons.appendChild(revoke);
        }
      }
      const remove = action('Remove', () => {
        openDialog({title: `Remove ${skill.name}?`, detail: 'This removes the app’s copy. Your original skill folder stays unchanged.',
          actions: [{label: 'Cancel', value: false, focus: true}, {label: 'Remove', value: true, destructive: true}],
          onClose: async yes => { if (yes) await call('remove_skill', skill.id); openSkills(); }});
      });
      remove.disabled = Boolean(state.status.generating);
      buttons.appendChild(remove);
      row.appendChild(buttons);
      root.appendChild(row);
    });
  }

  const memoryLevels = {
    explicit: 'From a conversation', deductive: 'Deduced from other notes',
    inductive: 'Pattern across conversations', contradiction: 'Conflicting notes',
  };

  async function openMemory(search = '', page = 1, notice = '') {
    const root = managementDialog('Memory', el.input);
    const loading = node('p', 'muted', 'Reading what is remembered…');
    root.appendChild(loading);
    const review = await call('memory_review', search, page);
    if (!root.isConnected) return;
    loading.remove();
    if (!review || review.error) {
      explanation(root, 'Blackrose keeps its memory in the local Honcho service.');
      panelError(root, review?.error || 'Could not read memory.');
      root.appendChild(action('Try again', () => openMemory(search, page)));
      return;
    }
    if (notice) panelError(root, notice);
    explanation(root, (review.enabled ? 'Memory is on.' : 'Memory is off: nothing new is remembered.')
      + ' These notes were derived from your conversations. Replies treat them as hints that may be stale, never as instructions.');

    const profile = node('section', 'management-card memory-profile');
    profile.appendChild(node('h3', null, 'Profile'));
    explanation(profile, 'One statement per line. Blackrose keeps lines you add as notes written by you, so they stay when the profile is rebuilt.');
    if (review.card_stale) explanation(profile, 'Waiting to be rebuilt from the remaining notes, because something was forgotten. Replies leave it out until the next memory maintenance run rebuilds it; the notes below still apply.');
    const card = node('textarea');
    card.rows = Math.min(10, Math.max(3, review.card.length + 1));
    card.value = review.card.join('\n');
    card.spellcheck = false;
    card.setAttribute('aria-label', 'Profile statements, one per line');
    profile.appendChild(card);
    const save = action('Save profile', async () => {
      const result = await call('set_memory_card', card.value);
      if (!root.isConnected) return;
      if (result?.ok) { flash('Profile saved'); openMemory(search, page); }
      else panelError(root, result?.error || 'Could not save the profile.');
    });
    const profileActions = node('div', 'management-actions');
    profileActions.appendChild(save);
    if (!review.card_stale) {
      const rebuild = action('Rebuild profile', async () => {
        const result = await call('rebuild_memory_profile');
        if (!root.isConnected) return;
        if (result?.ok) { flash('Profile will be rebuilt'); openMemory(search, page); }
        else panelError(root, result?.error || 'Could not ask for a rebuild.');
      });
      rebuild.title = 'Rebuild it from the notes at the next memory maintenance run; lines you wrote are kept';
      profileActions.appendChild(rebuild);
    }
    profile.appendChild(profileActions);
    root.appendChild(profile);

    const toolbar = node('div', 'library-search');
    const input = node('input');
    input.type = 'search';
    input.placeholder = 'Search remembered notes';
    input.setAttribute('aria-label', 'Search remembered notes');
    input.value = search;
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); openMemory(input.value.trim()); } });
    toolbar.appendChild(input);
    toolbar.appendChild(action('Search', () => openMemory(input.value.trim())));
    root.appendChild(toolbar);

    const notes = review.conclusions;
    if (!notes.items.length) explanation(root, search ? 'No remembered notes match.' : 'Nothing is remembered yet.');
    else explanation(root, `${notes.total} ${notes.total === 1 ? 'note' : 'notes'}${search ? ' match' : ''}, newest first.`);
    notes.items.forEach(item => {
      const row = node('section', 'management-card');
      row.appendChild(node('p', 'memory-note', item.content));
      const level = item.written_by_you ? 'Written by you' : memoryLevels[item.level] || item.level;
      explanation(row, [level, evidenceDate(item.date)].filter(Boolean).join(' · '));
      if (item.withheld) explanation(row, `Not used in replies: it ${item.withheld}. Forget it unless you wrote it.`);
      const buttons = node('div', 'management-actions');
      const forget = action('Forget', async () => {
        const result = await call('delete_memory_note', item.id);
        if (!root.isConnected) return;
        if (result?.ok) { flash('Note forgotten'); openMemory(search, notes.items.length === 1 && page > 1 ? page - 1 : page); }
        else panelError(root, result?.error || 'Could not forget this note.');
      });
      forget.setAttribute('aria-label', `Forget: ${item.content.slice(0, 80)}`);
      buttons.appendChild(forget);
      row.appendChild(buttons);
      root.appendChild(row);
    });
    const pages = node('div', 'management-actions');
    if (page > 1) pages.appendChild(action('Newer notes', () => openMemory(search, page - 1)));
    if (page < notes.pages) pages.appendChild(action('Older notes', () => openMemory(search, page + 1)));
    if (search) pages.appendChild(action('Show all notes', () => openMemory()));
    if (pages.childElementCount) root.appendChild(pages);

    const danger = node('section', 'management-card');
    danger.appendChild(node('h3', null, 'Forget everything'));
    explanation(danger, 'Deletes every note, the profile and what each conversation contributed. Your conversations stay in Blackrose. Blackrose asks you to confirm.');
    danger.appendChild(action('Forget everything…', async () => {
      const result = await call('forget_memory');
      if (!root.isConnected || !result || result.cancelled) return;
      if (result.ok) { flash('Memory forgotten'); openMemory(); }
      else openMemory('', 1, result.error);
    }, 'destructive'));
    root.appendChild(danger);
  }

  async function openTrustedSites(error = '') {
    const root = managementDialog('Trusted sites', el.sources);
    explanation(root, 'The assistant reads pages on these sites over HTTPS without asking, including addresses it writes. Once a chat has used your local data, only addresses without a query. Blackrose asks you to confirm each site you add.');
    if (error) panelError(root, error);
    const busy = Boolean(state.status.generating);
    const trust = async entry => {
      const result = await call('add_trusted_site', entry);
      if (!root.isConnected || result?.cancelled) return;
      openTrustedSites(result?.error || '');
      if (result?.ok) flash(`Trusted ${entry.trim()}`);
    };
    const toolbar = node('div', 'library-search');
    const input = node('input');
    input.placeholder = 'docs.example.org or *.example.org';
    input.setAttribute('aria-label', 'Site to trust');
    input.spellcheck = false;
    input.disabled = busy;
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && input.value.trim()) { e.preventDefault(); trust(input.value); } });
    toolbar.appendChild(input);
    const add = action('Trust…', () => input.value.trim() && trust(input.value));
    add.disabled = busy;
    toolbar.appendChild(add);
    root.appendChild(toolbar);
    const current = await call('trusted_sites');
    if (!root.isConnected) return;
    if (!current) { panelError(root, 'Could not read trusted sites.'); return; }
    if (!current.sites.length) explanation(root, 'No trusted sites. Every address the assistant writes asks first.');
    current.sites.forEach(site => {
      const row = node('section', 'management-card');
      row.appendChild(node('h3', null, site));
      const buttons = node('div', 'management-actions');
      const remove = action('Remove', async () => {
        const result = await call('remove_trusted_site', site);
        if (root.isConnected) openTrustedSites(result?.error || '');
      });
      remove.setAttribute('aria-label', `Stop trusting ${site}`);
      remove.disabled = busy;
      buttons.appendChild(remove);
      row.appendChild(buttons);
      root.appendChild(row);
    });
    const suggested = current.defaults.filter(site => !current.sites.includes(site));
    if (suggested.length) {
      const row = node('section', 'management-card');
      row.appendChild(node('h3', null, 'Suggested'));
      const buttons = node('div', 'management-actions');
      suggested.forEach(site => {
        const button = action(site, () => trust(site));
        button.setAttribute('aria-label', `Trust ${site}`);
        button.disabled = busy;
        buttons.appendChild(button);
      });
      row.appendChild(buttons);
      root.appendChild(row);
    }
  }

  async function openLibrary(query = '', offset = 0) {
    const root = managementDialog('Library', el.libraryButton);
    explanation(root, 'Your existing Retriever library. Search combines keyword and semantic evidence. Imported files join the shared local index.');
    const toolbar = node('div', 'library-search');
    const input = node('input');
    input.type = 'search';
    input.placeholder = 'Search local sources';
    input.setAttribute('aria-label', 'Search local sources');
    input.value = query;
    toolbar.appendChild(input);
    const search = () => openLibrary(input.value.trim());
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); search(); } });
    toolbar.appendChild(action('Search', search));
    toolbar.appendChild(action('Import files…', async button => {
      button.textContent = 'Indexing…';
      const result = await call('knowledge_import');
      if (!root.isConnected) return;
      if (result?.imports.length) {
        const results = result.imports.flatMap(r => r.results || []);
        const failures = results.filter(r => r.status === 'failed');
        if (failures.length) {
          panelError(root, failures.map(r => `${r.path}: ${r.error}`).join('\n'));
          button.textContent = 'Import files…';
        } else { flash(`${results.length} source(s) indexed`); openLibrary(); }
      } else button.textContent = 'Import files…';
    }));
    root.appendChild(toolbar);
    const loading = node('p', 'muted', query ? 'Searching local knowledge…' : 'Reading the library…');
    root.appendChild(loading);
    const response = query ? await call('knowledge_search', query) : await call('knowledge_list', offset);
    if (!root.isConnected) return;
    loading.remove();
    if (!response) { panelError(root, 'Library unavailable. Check the Retriever config, index and cached embedding model.'); return; }
    const rows = query ? response.results || [] : response.documents || [];
    if (!rows.length) explanation(root, query ? 'No candidate passages found.' : 'Your library is empty. Import documents to begin.');
    if (query && rows.length) explanation(root, 'Ranked passages are candidate evidence. Check source project and version before relying on them.');
    rows.forEach(source => {
      const row = node('section', 'management-card');
      row.appendChild(node('h3', null, source.title || source.path || source.document_id || source.id));
      const metadata = source.source_metadata || {};
      explanation(row, [libraryOrigin(source), source.file_type, source.heading, metadata.project, metadata.version,
        (source.tags || []).join(', ')].filter(Boolean).join(' · '));
      if (source.text) row.appendChild(node('p', 'excerpt', source.text));
      sourceDetails(row, source);
      row.appendChild(action('Open source', () => openSource(source.document_id || source.id, source.chunk_id)));
      root.appendChild(row);
    });
    if (!query) {
      const pages = node('div', 'management-actions');
      if (offset > 0) pages.appendChild(action('Previous sources', () => openLibrary('', Math.max(0, offset - 50))));
      if (response.next_offset != null) pages.appendChild(action('More sources', () => openLibrary('', response.next_offset)));
      root.appendChild(pages);
    } else root.appendChild(action('Browse all sources', () => openLibrary()));
  }

  async function openSource(documentId, chunkId = null, offset = 0, returnTo = null, trigger = document.activeElement) {
    const root = managementDialog('Local source', trigger);
    root.classList.add('evidence-view');
    el.dialog.classList.add('evidence-dialog');
    const addBack = () => { if (returnTo) root.prepend(action('Back to claim', returnTo)); };
    addBack();
    explanation(root, 'Reading the indexed passage…');
    const response = await call('knowledge_open', documentId, chunkId, offset);
    if (!root.isConnected) return;
    root.replaceChildren();
    if (!response) { addBack(); panelError(root, 'This citation could not be resolved. The document may have changed or been removed.'); return; }
    addBack();
    (response.results || []).forEach(source => {
      const section = node('section', 'source-section');
      section.appendChild(node('h3', null, source.title || 'Untitled source'));
      explanation(section, [libraryOrigin(source), source.heading, source.source_metadata?.project, source.source_metadata?.version].filter(Boolean).join(' · '));
      if (source.source_url) section.appendChild(action('Open original URL', () => call('open_link', source.source_url)));
      section.appendChild(node('pre', 'source-text', source.text));
      sourceDetails(section, source);
      root.appendChild(section);
    });
    const buttons = node('div', 'management-actions');
    if (response.next_offset != null) buttons.appendChild(action('More passages', () => openSource(documentId, null, response.next_offset, returnTo, trigger)));
    buttons.appendChild(action('Back to library', () => openLibrary()));
    buttons.appendChild(action('Remove from library', () => {
      openDialog({title: 'Remove this indexed source?', detail: 'Its indexed passages will be removed. The original file stays unchanged.',
        actions: [{label: 'Cancel', value: false, focus: true}, {label: 'Remove', value: true, destructive: true}],
        onClose: async yes => {
          if (yes) { const removed = await call('knowledge_remove', documentId); if (removed) openLibrary(); }
          else openSource(documentId, chunkId, offset, returnTo, trigger);
        }});
    }));
    root.appendChild(buttons);
  }

  // Evidence views share a small vocabulary; raw provenance is always available one level down.
  const verdicts = {
    supported: ['Supported', 'The saved evidence supports this claim.'],
    partially_supported: ['Partially supported', 'The saved evidence supports only part of this claim.'],
    unsupported: ['Unsupported', 'The saved evidence does not establish this claim. Missing evidence is not disproof.'],
    contradicted: ['Contradicted', 'The assessment found evidence that conflicts with this claim.'],
    inference: ['Inference', 'This is a conclusion drawn from evidence, rather than a fact stated by the source.'],
    unchecked: ['Unchecked', 'Source support has not been established for this claim.'],
  };

  function disclosure(parent, label, className = '') {
    const details = node('details', className);
    details.appendChild(node('summary', '', label));
    parent.appendChild(details);
    return details;
  }

  function evidenceDate(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined,
      {year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC'});
  }

  function sourceDetails(parent, source, snapshot = null) {
    const details = disclosure(parent, 'Source details', 'captured-metadata');
    const provenance = source.source_metadata || {};
    const facts = node('dl', 'source-facts');
    [['Location', source.heading || source.path || source.source], ['Page', source.page],
      ['Lines', source.line_start == null ? null : `${source.line_start}–${source.line_end ?? source.line_start}`],
      ['Project', provenance.project], ['Version', provenance.version], ['Published', source.publication_date],
      ['Captured', source.captured_at || source.retrieved_at], ['Revision', source.revision],
      ['URL', source.source_url || source.url]].forEach(([label, value]) => {
      if (value == null || value === '') return;
      facts.append(node('dt', '', label), node('dd', '', String(value)));
    });
    details.appendChild(facts);
    if (source.citation) details.appendChild(node('code', 'source-citation', source.citation));
    const technical = disclosure(details, 'Technical record', 'source-technical');
    const {text, ...record} = source;
    technical.appendChild(node('pre', 'source-metadata', JSON.stringify(record, null, 2)));
    if (snapshot) explanation(technical, `Snapshot SHA-256: ${snapshot.sha256}`);
    return details;
  }

  function claimList(parent, audit, claims, trigger, label = 'Claim') {
    claims.forEach(claim => {
      const button = action(`${verdicts[claim.status]?.[0] || claim.status} — ${claim.text || ''}`,
        button => openClaim(audit, claim, trigger || button, label));
      button.classList.add('claim-row');
      const verdict = node('span', 'claim-verdict', verdicts[claim.status]?.[0] || claim.status);
      verdict.dataset.status = claim.status;
      button.replaceChildren(verdict, document.createTextNode(` — ${claim.text || ''}`));
      parent.appendChild(button);
    });
  }

  function openEvidence(audit, trigger) {
    const root = managementDialog('Answer evidence', trigger);
    root.classList.add('evidence-view');
    el.dialog.classList.add('evidence-dialog');
    explanation(root, 'Model-assessed support from captured sources; not a certification of truth.');
    const claims = audit.claims || [];
    const toolbar = node('div', 'evidence-toolbar');
    const filter = node('select');
    filter.setAttribute('aria-label', 'Filter claims');
    [['all', 'All claims'], ['attention', 'Needs attention'], ...Object.entries(verdicts).map(([key, value]) => [key, value[0]])]
      .forEach(([value, title]) => { const option = node('option', '', title); option.value = value; filter.appendChild(option); });
    const search = node('input');
    search.type = 'search';
    search.placeholder = 'Find a claim';
    search.setAttribute('aria-label', 'Find a claim');
    toolbar.append(filter, search);
    root.appendChild(toolbar);
    const list = node('div', 'evidence-claims');
    root.appendChild(list);
    const draw = () => {
      list.replaceChildren();
      const matches = claims.filter(c => (filter.value === 'all' ||
        (filter.value === 'attention' ? c.status !== 'supported' || c.quality_issues?.length : c.status === filter.value)) &&
        (c.text || '').toLocaleLowerCase().includes(search.value.toLocaleLowerCase()));
      explanation(list, `${matches.length} of ${claims.length} claims`);
      claimList(list, audit, matches, trigger);
      if (!matches.length) explanation(list, 'No claims match. Try another filter or search.');
    };
    filter.addEventListener('change', draw);
    search.addEventListener('input', draw);
    draw();
    assessmentColorKey(root);
  }

  function assessmentColorKey(parent) {
    const key = disclosure(parent, 'Assessment color key', 'assessment-key');
    Object.entries(verdicts).forEach(([status, [label, description]]) => {
      const item = node('p');
      const name = node('strong', 'claim-verdict', label);
      name.dataset.status = status;
      item.append(name, document.createTextNode(` · ${description}`));
      key.appendChild(item);
    });
  }

  function renderAudit(reply, audit) {
    reply.msg.querySelector('.answer-audit')?.remove();
    reply.msg._audit = audit || null;
    if (!audit) return;
    const root = node('div', 'answer-audit');
    root.dataset.state = audit.status;
    const pending = ['pending', 'checking', 'researching', 'revising'].includes(audit.status);
    const title = pending ? 'Provisional · claims not yet checked' :
      audit.status === 'not_applicable' ? 'Citation rigor · no source-based claim audit' :
      audit.status === 'incomplete' ? 'Audit incomplete' : 'Source support assessed';
    const label = node('div', 'audit-label', `${title} · ${audit.mode}`);
    label.setAttribute('role', 'status');
    root.appendChild(label);
    if (pending) explanation(root, {pending: 'The answer will be checked after drafting.', checking: 'Checking claims against saved evidence…', researching: 'Reading additional evidence…', revising: 'Revising the answer and checking it again…'}[audit.status]);
    if (!pending && audit.status !== 'not_applicable') {
      const counts = node('p', 'evidence-counts');
      Object.entries(audit.coverage || {}).filter(([, count]) => count > 0).forEach(([key, count], index) => {
        if (index) counts.appendChild(document.createTextNode(' · '));
        const item = node('span', 'claim-verdict', `${count} ${key.replaceAll('_', ' ')}`);
        item.dataset.status = key;
        counts.appendChild(item);
      });
      if (counts.childNodes.length) root.appendChild(counts);
      const issues = (audit.claims || []).filter(claim => claim.status !== 'supported' || claim.quality_issues?.length);
      const actions = node('div', 'evidence-actions');
      actions.appendChild(action('Review evidence', button => openEvidence(audit, button)));
      if (audit.copy_text) actions.appendChild(action('Copy answer with assessments', () => call('copy', audit.copy_text)));
      root.appendChild(actions);
      (audit.limitations || []).forEach(text => { const p = node('p', 'audit-limitation', text); root.appendChild(p); });
      if (issues.length) {
        const details = disclosure(root, `${issues.length} claim${issues.length === 1 ? '' : 's'} need attention`, 'audit-issues');
        claimList(details, audit, issues);
      }
      const methodology = disclosure(root, 'About this assessment', 'audit-method');
      explanation(methodology, 'Model-assessed support from captured sources; not a certification of truth. Numbered markers open the claim and its saved evidence. Sources consulted are listed separately and may not support a claim.');
      assessmentColorKey(methodology);
      if (audit.exclusions?.length) {
        const exclusions = disclosure(methodology, 'Excluded from factual assessment', 'audit-exclusions');
        audit.exclusions.forEach(item => {
          exclusions.appendChild(node('blockquote', 'source-text', item.text));
          explanation(exclusions, `${item.kind.replaceAll('_', ' ')} · ${item.reason}`);
        });
      }
      if (audit.revision?.claims?.length) {
        const revisions = disclosure(methodology, 'Provisional claim assessments before revision', 'audit-revisions');
        audit.revision.claims.forEach(claim => {
          const button = action(`${claim.status.replaceAll('_', ' ')} — ${claim.text}`,
            button => openClaim(audit, claim, button, 'Provisional claim'));
          const verdict = node('span', 'claim-verdict', claim.status.replaceAll('_', ' '));
          verdict.dataset.status = claim.status;
          button.replaceChildren(verdict, document.createTextNode(` — ${claim.text}`));
          revisions.appendChild(button);
        });
      }
    }
    if (pending || audit.status === 'incomplete') reply.msg.insertBefore(root, reply.body);
    else reply.body.after(root);
  }

  function openClaim(audit, claim, trigger, label = 'Claim') {
    const root = managementDialog(`${label} ${claim.id.slice(1)} · ${claim.status.replaceAll('_', ' ')}`, trigger);
    root.classList.add('evidence-view');
    el.dialog.classList.add('evidence-dialog');
    const toolbar = node('div', 'evidence-actions');
    toolbar.appendChild(action('All answer evidence', () => openEvidence(audit, trigger)));
    const claims = label === 'Claim' ? audit.claims || [] : audit.revision?.claims || [];
    const index = claims.findIndex(item => item.id === claim.id);
    if (index > 0) toolbar.appendChild(action('Previous claim', () => openClaim(audit, claims[index - 1], trigger, label)));
    if (index >= 0 && index < claims.length - 1) toolbar.appendChild(action('Next claim', () => openClaim(audit, claims[index + 1], trigger, label)));
    root.appendChild(toolbar);
    root.appendChild(node('h3', 'evidence-heading', 'Claim in the answer'));
    root.appendChild(node('blockquote', 'source-text', claim.text || ''));
    const verdict = node('h3', 'evidence-heading claim-verdict', verdicts[claim.status]?.[0] || claim.status);
    verdict.dataset.status = claim.status;
    root.appendChild(verdict);
    explanation(root, verdicts[claim.status]?.[1] || 'Source support has not been established.');
    explanation(root, claim.reason || 'No explanation supplied.');
    if (claim.quality_issues?.length) {
      const caveats = node('section', 'evidence-caveats');
      caveats.appendChild(node('h3', 'evidence-heading', 'Source caveats'));
      claim.quality_issues.forEach(text => explanation(caveats, text));
      root.appendChild(caveats);
    }
    root.appendChild(node('h3', 'evidence-heading saved-evidence-heading', 'Saved evidence'));
    if (!claim.passages?.length) explanation(root, 'No captured supporting passage is available for this claim.');
    (claim.passages || []).forEach(passage => {
      const section = node('section', 'evidence-passage');
      root.appendChild(section);
      const source = (audit.sources || []).find(s => s.id === passage.source_id);
      const meta = passage.metadata || {};
      section.appendChild(node('h4', '', meta.title || meta.url || meta.document_id || 'Captured source'));
      const provenance = meta.source_metadata || {};
      explanation(section, [provenance.project, provenance.version,
        meta.publication_date ? `Published ${evidenceDate(meta.publication_date)}` : '',
        meta.retrieved_at || meta.captured_at ? `Captured ${evidenceDate(meta.retrieved_at || meta.captured_at)}` : ''].filter(Boolean).join(' · '));
      explanation(section, 'Exact saved excerpts · gaps between excerpts indicate omitted text.');
      (passage.excerpts || []).forEach(text => section.appendChild(node('blockquote', 'source-text supporting-excerpt', text)));
      if (source) {
        const full = node('details', 'captured-passage');
        full.appendChild(node('summary', '', 'Read full captured passage'));
        const pre = node('pre', 'source-text');
        let position = 0;
        (source.spans || []).filter(span => passage.span_ids?.includes(span.id)).forEach(span => {
          // Backend positions count Unicode code points, rather than JS UTF-16 units.
          const chars = Array.from(source.text);
          pre.appendChild(document.createTextNode(chars.slice(position, span.start).join('')));
          pre.appendChild(node('mark', '', chars.slice(span.start, span.end).join('')));
          position = span.end;
        });
        pre.appendChild(document.createTextNode(Array.from(source.text).slice(position).join('')));
        full.appendChild(pre);
        section.appendChild(full);
      }
      sourceDetails(section, meta, source);
      const url = meta.url || meta.source_url;
      if (url) section.appendChild(action('Open current original', () => call('open_link', url)));
      else if (meta.document_id) section.appendChild(action('Open current indexed passage', () => openSource(meta.document_id, meta.chunk_id, 0,
        () => openClaim(audit, claim, trigger, label), trigger)));
    });
  }

  function renderSources(reply, sources) {
    if (!sources.length) return;
    const unique = [...new Map(sources.map(s => [s.chunk_id || s.url || s.path || s.origin, s])).values()];
    // Only compact exact citations present in this answer's retrieved source list.
    // Leave code samples, links and audited claim controls untouched.
    const local = unique.filter(s => s.citation && s.document_id);
    if (local.length) {
      const walker = document.createTreeWalker(reply.body, NodeFilter.SHOW_TEXT);
      const texts = [];
      while (walker.nextNode()) {
        if (!walker.currentNode.parentElement.closest('pre, a, button')) texts.push(walker.currentNode);
      }
      texts.forEach(text => {
        let remaining = text.textContent;
        const fragment = document.createDocumentFragment();
        let changed = false;
        while (remaining) {
          const matches = local.map(s => ({source: s, at: remaining.indexOf(s.citation)})).filter(m => m.at >= 0).sort((a, b) => a.at - b.at);
          if (!matches.length) { fragment.appendChild(document.createTextNode(remaining)); break; }
          const {source, at} = matches[0];
          fragment.appendChild(document.createTextNode(remaining.slice(0, at)));
          const button = action(`Source ${unique.indexOf(source) + 1}`, () => openSource(source.document_id, source.chunk_id));
          button.className = 'source-reference';
          button.title = source.citation;
          button.setAttribute('aria-label', `Read source: ${source.title || source.document_id}`);
          fragment.appendChild(button);
          remaining = remaining.slice(at + source.citation.length);
          changed = true;
        }
        if (changed) text.replaceWith(fragment);
      });
    }
    const details = node('details', 'answer-sources');
    details.appendChild(node('summary', null, `Sources consulted · ${unique.length}`));
    explanation(details, 'Material retrieved for this answer. A consulted source does not by itself establish support for a claim.');
    unique.forEach((source, index) => {
      const row = node('div', 'source-row');
      row.appendChild(node('span', 'source-title', `${index + 1}. ${source.title || source.url || 'Local source'}`));
      explanation(row, [libraryOrigin(source) || source.origin || (source.url ? 'Web source' : 'Local source'), source.heading, source.source_metadata?.project, source.source_metadata?.version].filter(Boolean).join(' · '));
      if (source.document_id) row.appendChild(action('Read passage', () => openSource(source.document_id, source.chunk_id)));
      else if (source.url) {
        row.appendChild(action('Open URL', () => call('open_link', source.url)));
      }
      sourceDetails(row, source);
      details.appendChild(row);
    });
    reply.msg.appendChild(details);
  }

  function start() {
    forwardLiveRegions();
    call("ready").then((initial) => {
      if (!initial) return;
      state.commands = initial.commands || [];
      state.keymapBindings = initial.keymap || [];
      state.keymapBindings.forEach((binding) => {
        binding.keys.forEach((key) => state.keymap.set(key, binding.action));
      });
      restoreLayout(initial.layout || {});
      if (initial.engine !== 'webkitgtk') addPopupHints();
    });
    el.column.appendChild(welcome());
    autosize();
    el.input.focus();
  }

  /** The panes as they were last left, without animating into them. */
  function restoreLayout(saved) {
    if (typeof saved.sidebar === "number") {
      layout.width = saved.sidebar;
      const [lo, hi] = sidebarBounds();
      drawWidth(clamp(layout.width, lo, hi));
    }
    layout.collapsed = saved.collapsed === true;
    setWrap(saved.wrap === true);
    fitPanes();
    applyPane({ animate: false });
    describeResizer();
  }

  // Before the first paint: a narrow window starts with the list put away.
  fitPanes();
  applyPane({ animate: false });

  /* WebKitGTK exposes live regions but raises no accessibility event when one
   * changes, so a screen reader on the native window never hears them. Where
   * the bridge offers announce(), forward each live region's settled text to
   * it; Python raises the native notification the screen reader speaks. Every
   * live region in the page is covered, including the players' and diagrams'.
   * The whole region is spoken (as if aria-atomic), and a region repeating the
   * text it last spoke stays quiet, since status redraws rewrite unchanged
   * text. Browsers keep their own live-region handling; this only runs when
   * the bridge has announce(). */
  function forwardLiveRegions() {
    const api = window.pywebview && window.pywebview.api;
    if (!api || typeof api.announce !== "function") return;
    const live = '[aria-live]:not([aria-live="off"]), [role="status"], [role="alert"], [role="log"]';
    const pending = new Map();
    const spoken = new WeakMap();
    const speak = (region) => {
      pending.delete(region);
      if (!region.isConnected || region.closest('[hidden], [aria-hidden="true"]') ||
          !region.getClientRects().length) return;
      const text = region.textContent.replace(/\s+/g, " ").trim();
      if (!text || spoken.get(region) === text) return;
      spoken.set(region, text);
      const assertive = region.getAttribute("aria-live") === "assertive" || region.getAttribute("role") === "alert";
      Promise.resolve(api.announce(text, assertive)).catch((err) => console.error("announce", err));
    };
    new MutationObserver((records) => {
      for (const record of records) {
        const target = record.target.nodeType === Node.ELEMENT_NODE ? record.target : record.target.parentElement;
        // The outermost live region owns the update, as a screen reader would read it.
        let region = target && target.closest(live);
        for (let outer = region && region.parentElement?.closest(live); outer; outer = outer.parentElement?.closest(live)) {
          region = outer;
        }
        if (!region || pending.has(region)) continue;
        pending.set(region, setTimeout(() => speak(region), 150));
      }
    }).observe(document.body, { childList: true, characterData: true, subtree: true });
  }

  /* WebKitGTK exposes any button with aria-haspopup as a combo box, so a screen
   * reader announced "Models combo box" and invited arrow keys (bra-yp9). It
   * also fixes the role when the button is first exposed, so the hint cannot be
   * removed later: the markup carries data-popup and only other engines, which
   * announce the hint correctly, get aria-haspopup. */
  function addPopupHints() {
    document.querySelectorAll('button[data-popup]').forEach((button) => button.setAttribute('aria-haspopup', button.dataset.popup));
  }

  window.bloom = { receive };
  if (window.pywebview && window.pywebview.api) start();
  else window.addEventListener("pywebviewready", start, { once: true });
})();
