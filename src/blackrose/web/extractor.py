"""Readable text extraction from fetched pages."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from lxml import html
from readability import Document

from .constants import MAX_CHARS_PER_PAGE, MAX_LINK_TEXT, MAX_LINKS_PER_PAGE
from .models import ExtractedPage, FetchedPage

_SPACE = re.compile(r"[ \t\f\v]+")
_BLANKS = re.compile(r"\n{3,}")
_NOISE_XPATH = ".//script|.//style|.//noscript|.//nav|.//footer|.//form|.//svg"
_BLOCK_XPATH = ".//h1|.//h2|.//h3|.//h4|.//p|.//li|.//pre|.//blockquote"


class ContentExtractionError(RuntimeError):
    """A fetched response did not contain useful readable text."""


class ContentExtractor:
    """Use readability-lxml for main content, then retain useful block shape."""

    def extract(self, page: FetchedPage) -> ExtractedPage:
        links: tuple[tuple[str, str], ...] = ()
        if page.media_type == "text/plain":
            content = self._clean_plain(page.body)
        else:
            content = self._extract_html(page.body)
            links = self._links(page.body, page.url)
        truncated = len(content) > MAX_CHARS_PER_PAGE
        content = self._cap(content)
        if not content.strip():
            raise ContentExtractionError("page contained no readable text")
        return ExtractedPage(
            url=page.url,
            content=content,
            retrieved_at=page.retrieved_at,
            truncated=truncated,
            links=links,
        )

    def _extract_html(self, source: str) -> str:
        try:
            readable = Document(source).summary(html_partial=True)
            root = html.fromstring(readable)
            original = html.fromstring(source)
        except Exception as exc:
            raise ContentExtractionError(f"readability extraction failed: {exc}") from exc

        for tree in (root, original):
            for node in tree.xpath(_NOISE_XPATH):
                node.drop_tree()

        content = self._render_blocks(root)
        # Readability occasionally sanitizes short but useful lists from an
        # explicit semantic article/main. Prefer that bounded semantic element
        # when it preserves more structure; never fall back to the noisy body.
        semantic = original.xpath(".//article|.//main")
        if semantic:
            candidate = self._render_blocks(
                max(semantic, key=lambda node: len(node.text_content()))
            )
            if len(candidate) > len(content):
                content = candidate
        return content

    def _links(self, source: str, base: str) -> tuple[tuple[str, str], ...]:
        """Absolute HTTP(S) links the page itself declares, main content first.

        Navigation is kept here though it is dropped from the text: for
        documentation it is how a reader reaches the next page.
        """
        try:
            root = html.fromstring(source)
            root.make_links_absolute(base, resolve_base_href=True)
        except Exception:
            return ()
        page = urlsplit(base)._replace(fragment="")
        semantic = root.xpath(".//article|.//main")
        scopes = [max(semantic, key=lambda node: len(node.text_content()))] if semantic else []
        found: dict[str, str] = {}
        for scope in [*scopes, root]:
            for anchor in scope.xpath(".//a[@href]"):
                try:
                    parts = urlsplit(anchor.get("href", "").strip())
                except ValueError:
                    # Malformed href; skip this link.
                    continue
                if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
                        or parts.username or parts.password):
                    continue
                parts = parts._replace(fragment="")
                if parts == page:
                    continue
                url = urlunsplit(parts)
                if url not in found:
                    found[url] = _SPACE.sub(" ", anchor.text_content()).strip()[:MAX_LINK_TEXT]
                if len(found) == MAX_LINKS_PER_PAGE:
                    return tuple(found.items())
        return tuple(found.items())

    def _render_blocks(self, root) -> str:
        blocks: list[str] = []
        for node in root.xpath(_BLOCK_XPATH):
            tag = str(node.tag).lower()
            ancestors = {str(parent.tag).lower() for parent in node.iterancestors()}
            if tag in ("p", "li") and ancestors & {"li", "pre", "blockquote"}:
                continue
            if tag == "pre":
                text = node.text_content().strip("\n")
            else:
                text = _SPACE.sub(" ", node.text_content()).strip()
            if not text:
                continue
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                text = f"{'#' * min(int(tag[1]), 4)} {text}"
            elif tag == "li":
                text = f"- {text}"
            elif tag == "pre":
                text = f"```\n{text}\n```"
            elif tag == "blockquote":
                text = "\n".join(f"> {line}" for line in text.splitlines())
            blocks.append(text)

        if not blocks:
            fallback = _SPACE.sub(" ", root.text_content()).strip()
            if fallback:
                blocks.append(fallback)
        return _BLANKS.sub("\n\n", "\n\n".join(blocks)).strip()

    def _clean_plain(self, source: str) -> str:
        lines = [_SPACE.sub(" ", line).strip() for line in source.splitlines()]
        return _BLANKS.sub("\n\n", "\n".join(lines)).strip()

    def _cap(self, content: str) -> str:
        if len(content) <= MAX_CHARS_PER_PAGE:
            return content
        clipped = content[: MAX_CHARS_PER_PAGE - 1]
        boundary = clipped.rfind("\n")
        if boundary >= MAX_CHARS_PER_PAGE // 2:
            clipped = clipped[:boundary]
        return clipped.rstrip() + "…"
