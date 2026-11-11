"""Bounded HTTP page retrieval, independent of search and extraction."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx

from .constants import (
    FETCH_RETRIES,
    FETCH_TIMEOUT_SECONDS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BODY_BYTES,
    RETRY_BACKOFF_SECONDS,
    WEB_USER_AGENT,
)
from .models import FetchedPage
from .transport import PublicTransport
from .urls import validate_fetch_url


class PageFetchError(RuntimeError):
    """One page was unavailable or unsafe to consume."""


class PageFetcher:
    """Fetch selected pages with strict time, redirect, type and size bounds."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
            headers={"User-Agent": WEB_USER_AGENT},
            transport=PublicTransport(), trust_env=False,
        )

    async def fetch(self, url: str, *, redirect_allowed: Callable[[str], bool] | None = None) -> FetchedPage:
        """``redirect_allowed`` vets each redirect target before it is requested."""
        last_error: Exception | None = None
        for attempt in range(FETCH_RETRIES + 1):
            try:
                return await self._fetch_once(url, redirect_allowed)
            except (httpx.HTTPError, PageFetchError, ValueError) as exc:
                last_error = exc
                retryable = isinstance(exc, httpx.HTTPError) or (
                    isinstance(exc, PageFetchError)
                    and exc.args
                    and str(exc).startswith("transient HTTP")
                )
                if not retryable or attempt >= FETCH_RETRIES:
                    break
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        if isinstance(last_error, PageFetchError):
            raise last_error
        raise PageFetchError(str(last_error or "page fetch failed")) from last_error

    async def _fetch_once(self, url: str, redirect_allowed: Callable[[str], bool] | None = None) -> FetchedPage:
        current = validate_fetch_url(url)
        for redirect in range(MAX_REDIRECTS + 1):
            async with self._client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=FETCH_TIMEOUT_SECONDS,
                headers={"Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise PageFetchError("redirect had no Location header")
                    if redirect >= MAX_REDIRECTS:
                        raise PageFetchError("too many redirects")
                    current = validate_fetch_url(urljoin(str(response.url), location))
                    if redirect_allowed is not None and not redirect_allowed(current):
                        raise PageFetchError("redirect left the sites allowed for this request")
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    raise PageFetchError(f"transient HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise PageFetchError(f"HTTP {response.status_code}")

                media_type = response.headers.get("content-type", "").split(";", 1)[0]
                media_type = media_type.strip().lower()
                if media_type not in ("text/html", "application/xhtml+xml", "text/plain"):
                    raise PageFetchError(f"unsupported content type {media_type or 'unknown'}")
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > MAX_RESPONSE_BODY_BYTES:
                            raise PageFetchError("response body exceeds size limit")
                    except ValueError:
                        # Unparsable Content-Length is treated as unknown, not zero.
                        pass

                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BODY_BYTES:
                        raise PageFetchError("response body exceeds size limit")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                body = b"".join(chunks).decode(encoding, errors="replace")
                return FetchedPage(
                    url=str(response.url),
                    media_type=media_type,
                    body=body,
                    retrieved_at=datetime.now(UTC),
                )
        raise PageFetchError("too many redirects")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
