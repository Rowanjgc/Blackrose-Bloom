"""Read one PDF from stdin under CPU and memory limits; print its text as JSON.

Run by ``documents._extract_pdf`` in a separate interpreter, so a PDF that
makes pypdf spin or allocate without end costs this process, not the app.
The limits are set before pypdf is imported or the PDF is read. Only a short,
controlled message leaves on failure: never a traceback or document text.
"""
from __future__ import annotations

import json
import resource
import sys

MEMORY_LIMIT = 768 * 1024 ** 2  # address space, as for knowledge ingestion
CPU_SECONDS = 20


def main() -> int:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    from .documents import MAX_DOCUMENT_BYTES, InvalidDocument, parse_pdf  # noqa: PLC0415 - set CPU and memory limits before the parser loads
    try:
        text, pages = parse_pdf(sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1))
    except InvalidDocument as exc:
        print(json.dumps({'error': str(exc)[:300]}))
        return 1
    except MemoryError:
        print(json.dumps({'error': 'the PDF needs more memory to read than is allowed'}))
        return 1
    print(json.dumps({'text': text, 'pages': pages}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
