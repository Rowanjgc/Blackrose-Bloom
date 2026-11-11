"""Size-bounded local diagnostics, including a pipe for router output."""
from __future__ import annotations

import codecs
import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO

from . import __version__


def reply_diagnostics(state: str | None, *, conversation_id: int | None = None,
                      message_id: int | None = None) -> str | None:
    """Shareable host metadata only; never accept free-form error/provider text."""
    if state not in ('error', 'cancelled', 'pending'):
        return None
    import sys

    return '\n'.join([
        'Blackrose Bloom reply diagnostics',
        f'App version: {__version__}',
        f'Platform: {sys.platform}',
        f'Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
        f'Reply state: {state}',
        f'Conversation ID: {conversation_id if type(conversation_id) is int else "unavailable"}',
        f'Assistant message ID: {message_id if type(message_id) is int else "unavailable"}',
        'Privacy: message text, error details, paths, model/endpoint settings and logs omitted.',
    ])

LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3
RECORD_BYTES = 32 * 1024
# Private LogRecord attribute shared by shouldRollover and format for one emit.
FORMAT_CACHE = '_blackrose_bounded_format'


class BoundedLogHandler(RotatingFileHandler):
    """Bound records as well as files, and normalize oversized legacy logs."""

    def __init__(self, path: Path, *, max_bytes: int = LOG_BYTES, backups: int = LOG_BACKUPS):
        if max_bytes < 128 or backups < 1:
            raise ValueError('Diagnostic logs require at least 128 bytes and one backup')
        path.parent.mkdir(parents=True, exist_ok=True)
        # Run before opening the writer. Keep recent errors from old unbounded
        # logs, including backups, instead of moving a huge file into retention.
        for item in (path, *(Path(f'{path}.{index}') for index in range(1, backups + 1))):
            try:
                with item.open('r+b') as stream:
                    if stream.seek(0, os.SEEK_END) > max_bytes:
                        stream.seek(-max_bytes, os.SEEK_END)
                        # A byte-boundary tail can start inside a UTF-8 codepoint.
                        recent = stream.read(max_bytes).decode('utf-8', errors='ignore').encode('utf-8')
                        stream.seek(0)
                        stream.write(recent)
                        stream.truncate()
                item.chmod(0o600)
            except FileNotFoundError:
                # No legacy log at this path to normalize.
                pass
        super().__init__(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
        path.chmod(0o600)

    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors,
                    opener=lambda path, flags: os.open(path, flags, 0o600))

    def format(self, record: logging.LogRecord) -> str:
        cached = record.__dict__.get(FORMAT_CACHE)
        if cached is not None:
            return cached
        encoded = super().format(record).encode('utf-8', errors='replace')
        budget = min(RECORD_BYTES, self.maxBytes - 1)
        if len(encoded) > budget:
            marker = b'[earlier log text omitted] '
            encoded = marker + encoded[-(budget - len(marker)):]
        return encoded.decode('utf-8', errors='ignore')

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, os.SEEK_END)
        # StreamHandler.emit formats the same record again after this check.
        # Reuse this result instead of encoding and truncating the record twice.
        formatted = self.format(record)
        record.__dict__[FORMAT_CACHE] = formatted
        size = len((formatted + self.terminator).encode('utf-8'))
        return self.stream.tell() + size > self.maxBytes

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        finally:
            record.__dict__.pop(FORMAT_CACHE, None)


class RouterOutput:
    """Drain a child pipe in fixed chunks; rotation never leaves an old fd open."""

    def __init__(self, handler: BoundedLogHandler):
        self.handler = handler
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self._thread: threading.Thread | None = None

    def start(self, stream: BinaryIO) -> None:
        self._thread = threading.Thread(target=self._drain, args=(stream,), name='router-log', daemon=True)
        self._thread.start()

    def _drain(self, stream: BinaryIO) -> None:
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            with stream:
                while data := os.read(stream.fileno(), 8192):
                    text = decoder.decode(data)
                    if text:
                        self.handler.handle(logging.makeLogRecord({'msg': text, 'levelno': logging.INFO}))
                text = decoder.decode(b'', final=True)
                if text:
                    self.handler.handle(logging.makeLogRecord({'msg': text, 'levelno': logging.INFO}))
        except OSError:
            logging.getLogger(__name__).exception('Cannot read llama-router output')
        finally:
            self.handler.close()

    def join(self, timeout: float = 2) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                logging.getLogger(__name__).warning('Router output pipe is still open after shutdown')
                return False
        return True
