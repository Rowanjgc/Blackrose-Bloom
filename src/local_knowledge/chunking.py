"""Deterministic, tokenizer-measured splitting with original character ranges."""
from dataclasses import dataclass
import hashlib
import re
from .embeddings import document_input


@dataclass
class Chunk:
    text: str
    heading: str
    page: int | None
    line_start: int | None
    line_end: int | None
    token_count: int
    content_hash: str
    order: int


def chunk_sections(sections, title, provider, config):
    result = []
    headings = []
    for section in sections:
        text = section.text
        initial_heading = " / ".join(headings)
        # Group fenced code as one unit, prefer blank-line and heading boundaries.
        boundaries = [0]
        positions = []
        fence = None  # (fence character, opening run length)
        offset = 0
        for line in text.splitlines(keepends=True):
            marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
            if marker:
                symbol, rest = marker[1][0], marker[2]
                if fence is None:
                    fence = (symbol, len(marker[1]))
                elif symbol == fence[0] and len(marker[1]) >= fence[1] and not rest.strip():
                    # A closing fence keeps the opening character, is at least as
                    # long, and carries no info string.
                    fence = None
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line) if fence is None else None
            if heading:
                depth = len(heading[1])
                headings = headings[:depth - 1] + [heading[2]]
                positions.append((offset, " / ".join(headings)))
                boundaries.append(offset)
            offset += len(line)
            if not line.strip() and fence is None:
                boundaries.append(offset)
        boundaries.append(len(text))
        boundaries = sorted(set(boundaries))
        start = 0
        previous_end = 0
        while start < len(text):
            if not text[start:].strip():
                break
            def count(end, *, start=start, text=text):
                return provider.count_tokens(document_input(title, text[start:end]))
            fitting = []
            for boundary in boundaries:
                if boundary <= max(start, previous_end):
                    continue
                if boundary - start > config.chunk_tokens * 32 or count(boundary) > config.chunk_tokens:
                    break
                fitting.append(boundary)
            if fitting:
                end = max(fitting)
            else:
                # Very large paragraphs/code blocks: split by original character
                # offsets, measuring the real tokenizer, never tokenizer.decode().
                low, high = start, min(len(text), start + config.chunk_tokens * 32)
                while low < high:
                    mid = (low + high + 1) // 2
                    if count(mid) <= config.chunk_tokens:
                        low = mid
                    else:
                        high = mid - 1
                end = low
                while end > start and count(end) > config.chunk_tokens:
                    end -= 1
                if end == start:
                    raise ValueError("Title/prefix exceeds chunk token budget")
            if end <= previous_end:
                # A large title can consume more budget than the overlap leaves.
                # Drop overlap for this chunk instead of emitting repeated text.
                start = previous_end
                continue
            previous_end = end
            value = text[start:end]
            active = [h for pos, h in positions if pos <= start]
            heading = active[-1] if active else initial_heading
            result.append(Chunk(value, heading, section.page,
                                text.count("\n", 0, start) + 1 if section.reliable_lines else None,
                                text.count("\n", 0, end - 1) + 1 if section.reliable_lines else None,
                                count(end), hashlib.sha256(value.encode()).hexdigest(), len(result)))
            if end == len(text):
                break
            # Prefer whole previous paragraphs for overlap; fall back to a bounded
            # suffix when a paragraph alone is too large. Always make progress.
            candidates = [p for p in boundaries if start < p < end and provider.count_tokens(text[p:end]) <= config.chunk_overlap]
            next_start = min(candidates) if candidates else end
            if next_start == end and config.chunk_overlap:
                low, high = start + 1, end
                while low < high:
                    mid = (low + high) // 2
                    if provider.count_tokens(text[mid:end]) <= config.chunk_overlap:
                        high = mid
                    else:
                        low = mid + 1
                next_start = low
            start = max(start + 1, next_start)
    return result
