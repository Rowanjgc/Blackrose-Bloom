"""Shared resource limits for model response streams."""
from __future__ import annotations

import asyncio
import re
from contextlib import aclosing
from dataclasses import dataclass


_LINE_END = re.compile(b'[\r\n]')


class StreamLimitExceeded(RuntimeError):
    """A model stream exceeded a configured host resource bound."""


@dataclass(frozen=True)
class StreamBounds:
    reply_bytes: int = 16 * 1024 * 1024
    reasoning_bytes: int = 8 * 1024 * 1024
    response_bytes: int = 64 * 1024 * 1024
    sse_line_bytes: int = 1024 * 1024
    duration_seconds: float = 1800.0


class StreamBudget:
    """One budget can cover multiple provider requests in a single reply."""

    def __init__(self, bounds: StreamBounds | None = None):
        self.bounds = bounds or StreamBounds()
        self.deadline = asyncio.get_running_loop().time() + self.bounds.duration_seconds
        self.response_bytes = 0
        self.reply_bytes = 0
        self.reasoning_bytes = 0

    def check_time(self):
        if asyncio.get_running_loop().time() >= self.deadline:
            raise StreamLimitExceeded(
                f'The reply exceeded the configured {self.bounds.duration_seconds:g}-second '
                'stream deadline; the partial answer is saved.')

    def add_response_bytes(self, size: int):
        self.check_time()
        self.response_bytes += size
        if self.response_bytes > self.bounds.response_bytes:
            raise StreamLimitExceeded(
                f'The model stream exceeded the configured {self.bounds.response_bytes}-byte '
                'response limit; the partial answer is saved.')

    def add_output(self, kind: str, text: str):
        size = len(text.encode('utf-8'))
        if kind == 'reasoning':
            self.reasoning_bytes += size
            used, limit, label = self.reasoning_bytes, self.bounds.reasoning_bytes, 'reasoning'
        else:
            self.reply_bytes += size
            used, limit, label = self.reply_bytes, self.bounds.reply_bytes, 'reply'
        if used > limit:
            raise StreamLimitExceeded(
                f'The model exceeded the configured {limit}-byte {label} limit; '
                'the partial answer is saved.')


async def bounded_choices(stream, budget: StreamBudget):
    """Enforce the total deadline and yield cooperatively for fast iterators."""
    iterator = stream.__aiter__()
    count = 0
    async with aclosing(stream):
        while True:
            budget.check_time()
            try:
                async with asyncio.timeout_at(budget.deadline):
                    choice = await iterator.__anext__()
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise StreamLimitExceeded(
                    f'The reply exceeded the configured {budget.bounds.duration_seconds:g}-second '
                    'stream deadline; the partial answer is saved.') from None
            yield choice
            count += 1
            if count % 32 == 0:
                await asyncio.sleep(0)


async def sse_lines(response, budget: StreamBudget):
    """Read SSE lines with bounded raw bytes and line buffering."""
    iterator = response.aiter_bytes().__aiter__()
    pending = bytearray()
    first_line = True
    skip_lf = False
    line_count = 0
    while True:
        budget.check_time()
        try:
            async with asyncio.timeout_at(budget.deadline):
                chunk = await iterator.__anext__()
        except StopAsyncIteration:
            break
        except TimeoutError:
            raise StreamLimitExceeded(
                f'The reply exceeded the configured {budget.bounds.duration_seconds:g}-second '
                'stream deadline; the partial answer is saved.') from None
        budget.add_response_bytes(len(chunk))
        start = 0
        if skip_lf and chunk:
            if chunk.startswith(b'\n'):
                start = 1
            skip_lf = False
        while start < len(chunk):
            match = _LINE_END.search(chunk, start)
            newline = match.start() if match else -1
            end = len(chunk) if newline < 0 else newline
            segment_size = end - start
            if len(pending) + segment_size > budget.bounds.sse_line_bytes:
                raise StreamLimitExceeded(
                    f'The model stream exceeded the configured {budget.bounds.sse_line_bytes}-byte '
                    'SSE line limit; the partial answer is saved.')
            pending.extend(chunk[start:end])
            if newline < 0:
                break
            line = bytes(pending)
            pending.clear()
            if line.endswith(b'\r'):
                line = line[:-1]
            decoded = line.decode('utf-8', errors='replace')
            if first_line:
                decoded = decoded.removeprefix('\ufeff')
                first_line = False
            yield decoded
            line_count += 1
            if line_count % 32 == 0:
                await asyncio.sleep(0)
            is_cr = chunk[newline] == 13
            start = newline + 1
            if is_cr:
                if start < len(chunk) and chunk[start] == 10:
                    start += 1
                elif start == len(chunk):
                    skip_lf = True
    if len(pending) > budget.bounds.sse_line_bytes:
        raise StreamLimitExceeded(
            f'The model stream exceeded the configured {budget.bounds.sse_line_bytes}-byte '
            'SSE line limit; the partial answer is saved.')
    if pending:
        decoded = bytes(pending).decode('utf-8', errors='replace')
        if first_line:
            decoded = decoded.removeprefix('\ufeff')
        yield decoded
