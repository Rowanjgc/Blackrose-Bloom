"""Model text to safe HTML, and stored messages to what the window draws.

Markdown is rendered here, in Python, with raw HTML disabled: whatever a
model writes reaches the page as escaped text or as elements this module
produces, never as markup of its own. Links are restricted to http(s) and
mailto, and the page opens them in the system browser rather than
navigating the window.

On top of CommonMark the renderer adds what models write without being
asked: GitHub tables, task lists, alerts and footnotes; TeX maths, as
MathML; highlighted code blocks; and a short allowlist of attribute-free
HTML tags (``restore_tags``).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import html as htmllib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urlsplit

from latex2mathml.converter import convert as latex_to_mathml
from lxml import etree
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from ..storage.records import MessageState, StoredMessage
from ..diagnostics import reply_diagnostics
from ..diagrams import closed_fence, FENCE_OPTIONS, diagram_tokens
from ..outbound import ascii_host, visible

_SAFE_SCHEMES = ("http://", "https://", "mailto:")


class StoredHTMLCache:
    """Retain only rendered strings and fixed-size digests, never session payloads.

    Both entry count and retained bytes are bounded; a single large answer is
    rendered normally but does not displace the entire cache.
    """

    def __init__(self, max_entries=512, max_bytes=8 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.entries: OrderedDict[bytes, str] = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    @staticmethod
    def _size(key, value):
        return sys.getsizeof(key) + sys.getsizeof(value) + 128

    def render(self, identity, text, report=None, *, diagrams=None, diagram_block='', state=None,
               promoted_source=None):
        # Hash actual content, including mutable reports/blocks. Repository ids
        # and source_hash alone cannot detect in-place edits or another database.
        key = hashlib.sha256(json.dumps(
            [identity, text, report, diagrams, diagram_block, state, promoted_source],
            ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).digest()
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
                return self.entries[key]
        if promoted_source is not None:
            diagrams = {i: t.content for i, t in enumerate(diagram_tokens(promoted_source))}
        value = audited_html(text, report, diagrams=diagrams, diagram_block=diagram_block)
        size = self._size(key, value)
        with self.lock:
            if key in self.entries:
                return self.entries[key]
            if size <= self.max_bytes and self.max_entries > 0:
                while self.entries and (len(self.entries) >= self.max_entries
                                        or self.bytes + size > self.max_bytes):
                    old_key, old_value = self.entries.popitem(last=False)
                    self.bytes -= self._size(old_key, old_value)
                self.entries[key] = value
                self.bytes += size
        return value


_STORED_HTML = StoredHTMLCache()


def stored_block_html(identity, block) -> str:
    return _STORED_HTML.render(identity, block['source'],
        diagram_block=block['block_id'], state=block)


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.options["tasklists"] = True  # read-only checkboxes
    md.options["alerts"] = True     # > [!NOTE], [!TIP], [!IMPORTANT], [!WARNING], [!CAUTION]
    md.use(footnote_plugin, inline=False)
    # Currency must survive: "$5 and $10" is not maths, so a digit may not
    # touch a dollar and neither may a space. Labels would put model text in
    # an id attribute, so there are none.
    # $$...$$ is also read inside a paragraph, since models rarely leave the
    # blank line before it that a block needs.
    md.use(dollarmath_plugin, allow_labels=False, allow_space=False, allow_digits=False,
           allow_blank_lines=False, double_inline=True)
    md.inline.ruler.before("escape", "math_bracketed", _bracketed_math)
    md.add_render_rule("math_inline", _math_rule(display=False))
    md.add_render_rule("math_inline_double", _math_rule(display=True))
    md.add_render_rule("math_block", _math_rule(display=True, block=True))
    md.add_render_rule("fence", _render_fence)
    md.block.ruler.at('fence', closed_fence, FENCE_OPTIONS)
    md.add_render_rule("image", _render_image)
    md.add_render_rule("link_open", _render_link_open)
    default_validate = md.validateLink

    def validate(url: str) -> bool:
        return default_validate(url) and url.strip().lower().startswith(_SAFE_SCHEMES)

    md.validateLink = validate
    return md


def _render_link_open(renderer, tokens, idx, options, env) -> str:
    """Hovering a link shows where it leads; the model's own title is not used."""
    token = tokens[idx]
    token.attrSet("title", visible(token.attrGet("href") or ""))
    return renderer.renderToken(tokens, idx, options, env)


def _render_image(renderer, tokens, idx, options, env) -> str:
    """Remote images are deliberate loads, never requests made by model text."""
    token = tokens[idx]
    src = token.attrGet("src") or ""
    alt = renderer.renderInlineAsText(token.children or [], options, env).strip()
    if not src.lower().startswith(("https://", "http://")):
        return escapeHtml(alt or "Image")
    try:
        host = urlsplit(src).hostname
    except ValueError:
        host = None
    if not host:
        return escapeHtml(alt or "Image")
    caption = token.attrGet("title") or alt or "Image"
    host = ascii_host(host)
    # The whole address is sent to that host, so it can be read before loading.
    return (
        f'<span class="image-frame" data-image-src="{escapeHtml(src)}" '
        f'data-image-alt="{escapeHtml(alt or caption)}">'
        f'<button class="image-load" type="button" aria-label="Load image from {escapeHtml(host)}" '
        f'title="Loads {escapeHtml(visible(src))}">'
        '<span class="image-placeholder">Load image</span></button>'
        f'<span class="image-caption"><span>{escapeHtml(caption)}</span>'
        f'<span class="image-meta">{escapeHtml(host)}</span></span></span>'
    )


def to_html(text: str, *, diagrams=None, diagram_block='') -> str:
    return restore_tags(_MD.render(tidy_alerts(text),
        {'diagrams': diagrams or {}, 'diagram_block': diagram_block}))


def audited_html(text: str, report: dict | None, *, diagrams=None, diagram_block='') -> str:
    """Insert trusted citation controls in text nodes, never model HTML/attributes."""
    if not report or report['status'] == 'not_applicable':
        return to_html(text, diagrams=diagrams, diagram_block=diagram_block)
    from html.parser import HTMLParser
    import secrets
    from ..citations import digest, display_report
    if report['body_hash'] != digest(text):
        return to_html(text, diagrams=diagrams, diagram_block=diagram_block)
    report = display_report(text, report)
    markers: dict[str, str] = {}
    insertions: dict[int, list[str]] = {}
    nonce = secrets.token_hex(12)
    for number, claim in enumerate(report['claims'], 1):
        if not 0 <= claim['start'] < claim['end'] <= len(text):
            continue
        # Put controls at line/cell ends rather than inside Markdown syntax.
        end = text.find('\n', claim['end'])
        if end < 0:
            end = len(text)
        if text[:end].rstrip().endswith('|'):
            end = text[:end].rfind('|')
        marker = f'CITATION{nonce}X{number}END'
        status = claim['status'].replace('_', ' ')
        suffix = ' · source caveat' if claim['quality_issues'] else ''
        # Keep qualifications visible without repeating long verdicts throughout prose.
        label = f'{number}' if claim['status'] == 'supported' and not suffix else f'{number} · {status}{suffix}'
        markers[marker] = (f'<button type="button" class="claim-citation" '
                           f'data-claim-id="{escapeHtml(claim["id"])}" '
                           f'data-status="{escapeHtml(claim["status"])}" '
                           f'data-caveat="{str(bool(claim["quality_issues"])).lower()}" '
                           f'title="Claim {number}: {escapeHtml(status + suffix)}" '
                           f'aria-label="Claim {number}: {escapeHtml(status + suffix)}. Read captured evidence">'
                           f'{escapeHtml(label)}</button>')
        insertions.setdefault(end, []).append(marker)
    for position in sorted(insertions, reverse=True):
        text = text[:position] + ' ' + ' '.join(insertions[position]) + text[position:]

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs):
            self.parts.append(self.get_starttag_text())

        def handle_startendtag(self, tag, attrs):
            self.parts.append(self.get_starttag_text())

        def handle_endtag(self, tag):
            self.parts.append(f'</{tag}>')

        def handle_data(self, data):
            for marker, control in markers.items():
                data = data.replace(marker, control)
            self.parts.append(data)

        def handle_entityref(self, name):
            self.parts.append(f'&{name};')

        def handle_charref(self, name):
            self.parts.append(f'&#{name};')

    parser = Controls()
    parser.feed(to_html(text, diagrams=diagrams, diagram_block=diagram_block))
    parser.close()
    return ''.join(parser.parts)


# GitHub wants an alert's marker alone on the first line of its quote.
# Models write "> [!TIP] Use filtered water." and stack a second marker in
# the same quote; both are mended here, outside fenced code.
_ALERT_LINE = re.compile(
    r"^( {0,3}>[ \t]?)\[!(note|tip|important|warning|caution)\][ \t]*(.*)$", re.IGNORECASE)
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def tidy_alerts(text: str) -> str:
    if "[!" not in text:
        return text
    out: list[str] = []
    fence: str | None = None
    quoted = False
    for line in text.split("\n"):
        opener = _FENCE_LINE.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = None
        elif opener:
            fence = opener.group(1)
        elif match := _ALERT_LINE.match(line):
            prefix, kind, rest = match.groups()
            if quoted:
                out.append("")  # a second marker starts a callout of its own
            out.append(f"{prefix}[!{kind.upper()}]")
            if rest.strip():
                out.append(prefix + rest)
            quoted = True
            continue
        quoted = fence is None and line.lstrip().startswith(">")
        out.append(line)
    return "\n".join(out)


# --- maths ------------------------------------------------------------------

# \( inline \) and \[ display \], which most models prefer to dollars. Without
# this rule CommonMark reads the backslashes as escapes and eats them.
_BRACKETED = re.compile(r"\\\((.+?)\\\)|\\\[(.+?)\\\]", re.DOTALL)


def _bracketed_math(state, silent: bool) -> bool:
    if not state.src.startswith("\\", state.pos):
        return False
    match = _BRACKETED.match(state.src, state.pos)
    if not match:
        return False
    if not silent:
        inline = match.group(1) is not None
        token = state.push("math_inline" if inline else "math_inline_double", "math", 0)
        token.content = match.group(1) if inline else match.group(2)
        token.markup = match.group(0)[:2]
    state.pos = match.end()
    return True


def _math_rule(*, display: bool, block: bool = False):
    def render(self, tokens, idx, options, env) -> str:
        tex = tokens[idx].content.strip()
        mathml = to_mathml(tex, display)
        if mathml is None:
            body = f'<code class="math-source">{escapeHtml(tex)}</code>'
        else:
            body = mathml
        kind = "display" if display else "inline"
        if block:
            return f'<div class="math display">{body}</div>\n'
        return f'<span class="math {kind}">{body}</span>'
    return render


# latex2mathml copies \text{...} through verbatim, markup and all, so its
# output is parsed and rebuilt from an allowlist: MathML elements and
# presentation attributes only, every text node escaped. Anything that will
# not parse, or that the converter rejects, is shown as its TeX source.
_MATHML_TAGS = frozenset((
    "math", "mrow", "mi", "mn", "mo", "ms", "mtext", "mspace", "msub", "msup", "msubsup",
    "munder", "mover", "munderover", "mfrac", "msqrt", "mroot", "mstyle", "merror",
    "mpadded", "mphantom", "menclose", "mtable", "mtr", "mtd", "mmultiscripts",
    "mprescripts", "none",
))
_MATHML_ATTRS = frozenset((
    "display", "mathvariant", "stretchy", "fence", "separator", "accent", "accentunder",
    "columnalign", "columnlines", "columnspacing", "rowalign", "rowlines", "rowspacing",
    "frame", "lspace", "rspace", "movablelimits", "largeop", "symmetric", "minsize",
    "maxsize", "width", "height", "depth", "linethickness", "displaystyle", "scriptlevel",
    "form", "notation",
))
_XML = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)


# 92^\circ C is how TeX spells a degree, but set as an operator it is spaced
# like one ("92 ∘ C"); the degree sign itself sits tight.
_DEGREE = re.compile(r"\^\s*(?:\{\s*\\circ\s*\}|\\circ\b)")


# Models carry <del> and <ins> into maths as \del{...} and \ins{...}, which
# TeX lacks; they become a strike-through and an underline, as in prose.
_EDITS = re.compile(r"\\(del|ins)(?=\s*\{)")


def _edit(match: re.Match) -> str:
    return r"\sout" if match.group(1) == "del" else r"\underline"


@lru_cache(maxsize=512)
def to_mathml(tex: str, display: bool) -> str | None:
    if not tex:
        return None
    try:
        markup = latex_to_mathml(_DEGREE.sub(r"\\degree ", _EDITS.sub(_edit, tex)),
                                 display="block" if display else "inline")
    except Exception:  # noqa: BLE001 - malformed TeX of any kind falls back to source
        return None
    # A command the converter does not know is copied through as text
    # ("\del"); the source reads better than half-typeset maths.
    if ">\\" in markup:
        return None
    try:
        root = etree.fromstring(markup.encode(), _XML)
    except etree.XMLSyntaxError:
        return None
    if etree.QName(root).localname != "math":
        return None
    return _clean_mathml(root, tail=False)


def _clean_mathml(node, *, tail: bool = True) -> str:
    after = htmllib.escape(node.tail or "") if tail else ""
    if not isinstance(node.tag, str):  # comments and processing instructions
        return after
    name = etree.QName(node).localname
    if name not in _MATHML_TAGS:
        return after
    if _is_underline(node):
        # \underline arrives as an accent bar that WebKit will not stretch;
        # a bottom-edged enclosure spans the whole base instead.
        return f'<menclose notation="bottom">{_clean_mathml(node[0], tail=False)}</menclose>{after}'
    attrs = "".join(f' {key}="{htmllib.escape(value)}"'
                    for key, value in sorted(node.attrib.items()) if key in _MATHML_ATTRS)
    inner = htmllib.escape(node.text or "") + "".join(_clean_mathml(child) for child in node)
    return f"<{name}{attrs}>{inner}</{name}>{after}"


def _is_underline(node) -> bool:
    if etree.QName(node).localname != "munder" or len(node) != 2:
        return False
    bar = node[1]
    return (isinstance(bar.tag, str) and etree.QName(bar).localname == "mo"
            and (bar.text or "").strip() in {"―", "_", "¯", "̲"})


# --- code -------------------------------------------------------------------

_FORMATTER = HtmlFormatter(nowrap=True, classprefix="tok-")
_LANGUAGE = re.compile(r"[^\w+#.-]")


# Some lexers (SQL, MIME, HTTP, Modula-2, ...) keep state on the instance while
# lexing, so a cached lexer is never shared between threads.
@lru_cache(maxsize=64)
def _lexer(language: str, thread: int):
    try:
        return get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def _render_fence(self, tokens, idx, options, env) -> str:
    """A code block with its language named, a wrap toggle and a copy button."""
    token = tokens[idx]
    language = _LANGUAGE.sub("", token.info.strip().split(maxsplit=1)[0] if token.info.strip()
                             else "")[:24]
    if (token.info.strip().lower().split(maxsplit=1)[:1] == ['mermaid']
            and token.meta.get('closed')):
        index = env.get('diagram_index', 0)
        env['diagram_index'] = index + 1
        source = env.get('diagrams', {}).get(index, token.content)
        block = escapeHtml(env.get('diagram_block', ''))
        return (f'<figure class="code mermaid-diagram" data-mermaid="" '
                f'data-diagram-index="{index}" data-diagram-block="{block}">'
                '<figcaption><span class="code-lang">Mermaid</span><span class="code-tools">'
                '<button class="code-copy" type="button">Copy source</button></span></figcaption>'
                f'<pre><code class="language-mermaid">{escapeHtml(source)}</code></pre>'
                '</figure>\n')
    lexer = _lexer(language.lower(), threading.get_ident()) if language else None
    body = highlight(token.content, lexer, _FORMATTER) if lexer else escapeHtml(token.content)
    attribute = f' class="language-{language}"' if language else ""
    return (f'<figure class="code"><figcaption><span class="code-lang">{language or "text"}'
            f'</span><span class="code-tools"><button class="code-wrap" type="button" '
            f'aria-pressed="false">Wrap</button>'
            f'<button class="code-copy" type="button">Copy</button></span></figcaption>'
            f"<pre><code{attribute}>{body}</code></pre></figure>\n")


# --- model-written tags -----------------------------------------------------

# Models reach for a few HTML elements whatever they are told: a disclosure
# to hide an answer, <kbd> for keys, <sub>/<sup> for formulae, <br> in a
# table cell. Exactly these are allowed back, without attributes (a bare
# `open` on <details> aside), and only as properly nested pairs outside code
# and maths. An unclosed or stray tag stays visible as text, which is also
# what a half-streamed one looks like until its closing tag arrives.
_BLOCK_TAGS = ("details", "summary")
_INLINE_TAGS = ("kbd", "mark", "sub", "sup", "ins", "del")
_TAG = re.compile(
    r"&lt;(?:(/?)(" + "|".join(_BLOCK_TAGS + _INLINE_TAGS) + r")(\s+open)?\s*|(br)\s*/?\s*)&gt;",
    re.IGNORECASE)
_PROTECTED = re.compile(r"<(pre|code|math)\b.*?</\1>", re.DOTALL)
# An inline pair may not span blocks: "<mark>" in one paragraph closed in the
# next would be torn apart by the browser.
_BLOCK_BOUNDARY = re.compile(
    r"</?(?:p|li|ul|ol|h\d|blockquote|pre|table|tr|td|th|div|section|figure|hr)\b"
    r"|&lt;/?(?:details|summary)\b", re.IGNORECASE)
# Markdown wraps a tag on a line of its own in a paragraph; the browser would
# then split it into empty paragraphs around the disclosure.
_UNWRAP = (
    (re.compile(r"<p>\s*(<details(?: open)?>)"), r"\1"),
    (re.compile(r"(</summary>)\s*</p>"), r"\1"),
    (re.compile(r"<p>\s*(</details>)\s*</p>"), r"\1"),
)


def restore_tags(html: str) -> str:
    protected = [(m.start(), m.end()) for m in _PROTECTED.finditer(html)]
    tags = [m for m in _TAG.finditer(html)
            if not any(start <= m.start() < end for start, end in protected)]
    keep: set[int] = set()
    stack: list[tuple[str, int]] = []
    for index, match in enumerate(tags):
        if match.group(4):
            keep.add(index)
            continue
        closing, name, opened = match.group(1) == "/", match.group(2).lower(), match.group(3)
        if opened and (closing or name != "details"):
            continue
        if not closing:
            stack.append((name, index))
        elif stack and stack[-1][0] == name:
            start = stack.pop()[1]
            if name in _INLINE_TAGS and _BLOCK_BOUNDARY.search(
                    html, tags[start].end(), match.start()):
                continue
            keep.update((index, start))
    if not keep:
        return html
    parts: list[str] = []
    last = 0
    for index, match in enumerate(tags):
        if index not in keep:
            continue
        if match.group(4):
            tag = "<br>"
        else:
            closing, name = match.group(1) == "/", match.group(2).lower()
            tag = f"</{name}>" if closing else ("<details open>" if match.group(3) else f"<{name}>")
        parts += [html[last:match.start()], tag]
        last = match.end()
    parts.append(html[last:])
    restored = "".join(parts)
    for pattern, replacement in _UNWRAP:
        restored = pattern.sub(replacement, restored)
    return restored


_MD = _markdown()


_WORKER: ThreadPoolExecutor | None = None


def _render_worker() -> ThreadPoolExecutor:
    """One thread for streaming repaints, so they queue behind each other."""
    global _WORKER
    if _WORKER is None:
        _WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bloom-render")
    return _WORKER


def _timed_html(text: str) -> tuple[str, float]:
    started = time.monotonic()
    return to_html(text), time.monotonic() - started


class StreamRenderer:
    """Re-renders a streaming answer no more often than it can afford.

    Rendering is a full Markdown parse of everything so far, so its cost grows
    with the answer. The interval adapts to keep it under about a sixth of
    the time -- the same budget the terminal's adaptive repaint uses -- and
    never drops below one frame at 30 Hz.

    On an event loop the parse runs on the render worker, leaving the loop
    free for Stop and the bridge. At most one render is in flight; deltas
    that arrive meanwhile are rendered together once it returns, and a
    result is published only if nothing newer has been shown. ``publish``
    is called on the loop.
    """

    MIN_INTERVAL = 1 / 30
    MAX_INTERVAL = 1.0

    def __init__(self, publish=None) -> None:
        self.text = ""
        self.publish = publish
        self._last = 0.0
        self._cost = 0.0
        self._revision = 0  # of self.text
        self._shown = -1    # revision of the HTML last handed out
        self._busy = False
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False

    def append(self, delta: str) -> None:
        """Add a delta; publish fresh HTML once a repaint is due."""
        self.text += delta
        self._revision += 1
        self._schedule()

    def render(self, now: float | None = None) -> str:
        """The current text's HTML, now; any older render in flight is dropped."""
        started = time.monotonic()
        html = to_html(self.text)
        self._cost = time.monotonic() - started
        self._last = now if now is not None else started
        self._shown = self._revision
        return html

    def close(self) -> None:
        """Publish nothing more: the reply's final HTML replaces this one."""
        self._closed = True
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _schedule(self) -> None:
        if self._closed or self._busy or self._timer or self._shown >= self._revision:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop to keep free
            loop = None
        wait = (min(max(self._cost * 6, self.MIN_INTERVAL), self.MAX_INTERVAL)
                - (time.monotonic() - self._last))
        if loop is None:
            if wait <= 0 and self.publish:
                self.publish(self.render())
            return
        if wait > 0:
            self._timer = loop.call_later(wait, self._start)
            return
        self._start()

    def _start(self) -> None:
        self._timer = None
        if self._closed or self._busy:
            return
        revision = self._revision
        self._busy = True
        self._last = time.monotonic()
        future = asyncio.get_running_loop().run_in_executor(
            _render_worker(), _timed_html, self.text)
        future.add_done_callback(lambda done: self._finish(done, revision))

    def _finish(self, future: asyncio.Future, revision: int) -> None:
        self._busy = False
        if self._closed or future.cancelled():
            return
        html, self._cost = future.result()
        if revision > self._shown:
            self._shown = revision
            if self.publish:
                self.publish(html)
        self._schedule()


def message_payload(
    stored: StoredMessage,
    *,
    label: str | None = None,
    model: str | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """One stored message as the window's transcript needs it."""
    payload: dict = {
        "id": stored.id,
        "role": stored.role,
        "state": stored.state.value,
        "attachments": attachments if attachments is not None else [
            {"kind": a.kind, "label": a.label, "filename": a.filename}
            for a in stored.attachments],
    }
    if stored.role == "user":
        payload["text"] = stored.content
        return payload
    payload.update(
        label=label or "Assistant",
        model=model,
        html='' if stored.blocks else _STORED_HTML.render(
            (stored.conversation_id, stored.id), stored.content, stored.audit),
        error=stored.error,
        stopped=stored.state is MessageState.CANCELLED,
        diagnostics=reply_diagnostics(stored.state.value,
            conversation_id=stored.conversation_id, message_id=stored.id),
    )
    if stored.blocks:
        if len(stored.blocks) == 1 and stored.blocks[0].get('diagram_promoted'):
            block = stored.blocks[0]
            payload['html'] = _STORED_HTML.render(
                (stored.conversation_id, stored.id), stored.content, stored.audit,
                diagram_block=block['block_id'], state=stored.blocks,
                promoted_source=block['source'])
        else:
            payload['blocks'] = [dict(b, html=stored_block_html(
                (stored.conversation_id, stored.id), b)) if b['kind'] == 'markdown' else dict(b)
                                 for b in stored.blocks]
            if stored.audit:
                payload['html'] = _STORED_HTML.render(
                    (stored.conversation_id, stored.id), stored.content, stored.audit)
    return payload
