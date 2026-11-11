"""Bounded public page fetch and extraction values."""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    media_type: str
    body: str
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    url: str
    content: str
    retrieved_at: datetime
    truncated: bool = False
    links: tuple[tuple[str, str], ...] = ()  # (absolute URL, link text), in page order
