"""Turning a document into text, and refusing the ones that are not.

Images are validated without being decoded, for the reasons ``images.py``
sets out at length. Documents cannot work that way and this module does not
pretend otherwise: the question "is this a usable document?" and the question
"what does it say?" are the same parse, because a PDF whose text cannot be
extracted is of no use to a model that will only ever see the text.

So this is a decoder, running on bytes the user supplied. That is a real
change in posture and it is bounded deliberately rather than assumed safe:

- Nothing is parsed until the byte size is known and accepted, so a huge file
  is refused before a parser sees it.
- A PDF is parsed in a separate interpreter (``pdf_worker``) with CPU,
  address-space and wall-clock limits, so a PDF crafted to make the parser
  spin or allocate is refused without stalling or exhausting the app.
- A PDF's page count is checked before any page is read, so a document that
  is small on disk and enormous when walked is refused rather than explored.
- A DOCX is a zip, and a zip is a decompression bomb waiting to happen, so
  the *declared* uncompressed size of the one member that matters is checked
  before that member is read.
- Only ``word/document.xml`` is read out of a DOCX. Relationships, embedded
  objects and external references are never resolved -- a document is text
  here, not a container to be followed.
- Every parser is wrapped, and anything it raises becomes ``InvalidDocument``.
  A malformed file is a refusal with a reason, never a traceback out of a
  third-party library.

The other departure from ``images.py`` is that the file extension matters.
A ``.csv`` and a ``.md`` are both UTF-8 text and nothing in the bytes tells
them apart, so the name is what decides -- and, for the text family, whether
the file is offered at all. That is weaker than "the header decides", and it
is acceptable only because the cost of believing a wrong name here is a worse
label on a block of text, not a parser pointed at bytes it did not expect.
PDF and DOCX are still confirmed by their own magic before anything is read.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

PDF_TYPE = "application/pdf"
DOCX_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# What the document path accepts, and the name each type travels under.
#
# The text family is one MIME type per extension group rather than a single
# "text/plain", because the label is what the model is told the thing is, and
# "this is CSV" is a genuinely different hint from "this is Markdown".
SUPPORTED_DOCUMENT_TYPES: dict[str, tuple[str, ...]] = {
    PDF_TYPE: (".pdf",),
    DOCX_TYPE: (".docx",),
    "text/markdown": (".md", ".markdown"),
    "text/csv": (".csv", ".tsv"),
    "application/json": (".json",),
    "application/yaml": (".yaml", ".yml"),
    "application/toml": (".toml",),
    "text/x-python": (".py",),
    "text/x-script": (".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".rb", ".sh"),
    "text/x-source": (".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".swift", ".sql"),
    "text/html": (".html", ".htm", ".xml"),
    "text/plain": (".txt", ".text", ".log", ".rst", ".ini", ".cfg", ".conf", ".env"),
}

# Which extensions are read as plain text rather than handed to a parser.
_BINARY_TYPES = frozenset({PDF_TYPE, DOCX_TYPE})

# One message's worth of documents. The same reasoning as the image limit:
# past a handful this stops being a question about a file.
MAX_DOCUMENT_ATTACHMENTS_PER_MESSAGE = 4
# Comfortably larger than any report and small enough that parsing it is a
# bounded amount of work.
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_DOCUMENT_BYTES = 20 * 1024 * 1024
# Checked before a single page is read: a PDF can be tiny on disk and very
# long to walk.
MAX_DOCUMENT_PAGES = 200
# What one document may contribute to a prompt, and what all of them may
# contribute together. Characters rather than tokens because this is applied
# before any tokenizer sees the text.
MAX_EXTRACTED_CHARS = 40_000
MAX_TOTAL_DOCUMENT_CHARS = 80_000
# Wall-clock bound on one PDF parse, including starting the worker; the worker
# also stops itself after its own CPU allowance (``pdf_worker``).
PDF_TIMEOUT_SECONDS = 30
# -I: the worker's imports come from the installation, never the working directory.
_PDF_WORKER = ("-I", "-m", "blackrose.attachments.pdf_worker")
# A DOCX member that claims to decompress to more than this is a bomb, not a
# document, and is refused without being read.
MAX_DOCX_MEMBER_BYTES = 16 * 1024 * 1024
# Above this share of control characters, a "text" file is a binary someone
# renamed. Not zero: a legitimate text file may carry a form feed or an
# escape sequence, and refusing those would be wrong.
_MAX_CONTROL_RATIO = 0.02

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_MEMBER = "word/document.xml"
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Extension -> MIME, derived from the table above so the two cannot drift.
_TYPE_BY_SUFFIX: dict[str, str] = {
    suffix: mime
    for mime, suffixes in SUPPORTED_DOCUMENT_TYPES.items()
    for suffix in suffixes
}


class InvalidDocument(ValueError):
    """The bytes offered are not a usable document of a supported type."""


class TooManyDocuments(ValueError):
    """The message is already carrying as many documents as it may."""


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    """What the parse found. Pages are optional: only a PDF has them."""

    mime_type: str
    size: int
    characters: int
    pages: int | None = None
    truncated: bool = False

    @property
    def suffix(self) -> str:
        return SUPPORTED_DOCUMENT_TYPES[self.mime_type][0]

    @property
    def summary(self) -> str:
        """The short description shown beside the filename."""
        if self.pages is not None:
            pages = f"{self.pages} page{'s' if self.pages != 1 else ''}"
            return f"{pages}, truncated" if self.truncated else pages
        chars = f"{self.characters:,} chars"
        return f"{chars}, truncated" if self.truncated else chars


def suffix_for(mime_type: str) -> str:
    """The canonical file extension for a supported type."""
    try:
        return SUPPORTED_DOCUMENT_TYPES[mime_type][0]
    except KeyError:
        raise InvalidDocument(f"unsupported document type {mime_type!r}") from None


def is_document_suffix(suffix: str) -> bool:
    """Whether a file extension names a document Blackrose will consider."""
    return suffix.lower() in _TYPE_BY_SUFFIX


def sniff_document(data: bytes, *, claimed_name: str | None = None) -> str | None:
    """The MIME type of these bytes, or ``None`` if they are not a document.

    PDF and DOCX are decided by their own magic, so a ``.txt`` holding a PDF
    is read as one. Everything else is decided by the name, because nothing
    in the bytes of a CSV distinguishes it from the bytes of a Markdown file.
    """
    if data.startswith(_PDF_MAGIC):
        return PDF_TYPE
    if data.startswith(_ZIP_MAGIC) and _is_docx(data):
        return DOCX_TYPE
    if claimed_name is None:
        return None
    mime = _TYPE_BY_SUFFIX.get(Path(claimed_name).suffix.lower())
    # A name that claims a parsed format but whose bytes did not match above
    # is a mislabelled file, not a text one. Refuse rather than fall back to
    # reading a PDF as text.
    return None if mime is None or mime in _BINARY_TYPES else mime


def _is_docx(data: bytes) -> bool:
    """Whether a zip carries the one member that makes it a Word document."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _DOCX_MEMBER in archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


def inspect_document(
    data: bytes, *, claimed_name: str | None = None
) -> tuple[str, DocumentInfo]:
    """Validate bytes as a document and return its text alongside its facts.

    The text comes back with the info because extracting it *is* the
    validation. Truncation happens here, at the per-document limit, so
    nothing downstream has to carry an unbounded string around.
    """
    if not data:
        raise InvalidDocument("the file is empty")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise InvalidDocument(
            f"the file is {_megabytes(len(data))} and the limit is "
            f"{_megabytes(MAX_DOCUMENT_BYTES)}"
        )
    mime_type = sniff_document(data, claimed_name=claimed_name)
    if mime_type is None:
        raise InvalidDocument(
            "this is not a document Blackrose can read -- PDF, DOCX, and "
            "plain-text files such as .md, .csv, .json or source code"
        )

    pages: int | None = None
    if mime_type == PDF_TYPE:
        text, pages = _extract_pdf(data)
    elif mime_type == DOCX_TYPE:
        text = _extract_docx(data)
    else:
        text = _extract_text(data)

    text = text.strip()
    if not text:
        raise InvalidDocument("the document contains no readable text")

    full_length = len(text)
    truncated = full_length > MAX_EXTRACTED_CHARS
    if truncated:
        text = text[:MAX_EXTRACTED_CHARS].rstrip()
    return text, DocumentInfo(
        mime_type=mime_type,
        size=len(data),
        characters=full_length,
        pages=pages,
        truncated=truncated,
    )


def read_document_file(path: Path) -> tuple[bytes, str, DocumentInfo]:
    """Read and validate a document from disk.

    Reads one byte more than the limit so an oversized file is refused by
    ``inspect_document`` without the whole thing being pulled into memory.
    """
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError as exc:
        raise InvalidDocument(f"cannot read {path}: {exc}") from exc
    if not resolved.is_file():
        raise InvalidDocument(f"{path} is not a file")
    try:
        with resolved.open("rb") as handle:
            data = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise InvalidDocument(f"cannot read {path}: {exc}") from exc
    text, info = inspect_document(data, claimed_name=resolved.name)
    return data, text, info


def check_document_budget(existing: list[int], incoming: int) -> None:
    """Whether one more document fits in this message's allowance."""
    if len(existing) >= MAX_DOCUMENT_ATTACHMENTS_PER_MESSAGE:
        raise TooManyDocuments(
            f"a message may carry {MAX_DOCUMENT_ATTACHMENTS_PER_MESSAGE} documents"
        )
    if sum(existing) + incoming > MAX_TOTAL_DOCUMENT_BYTES:
        raise TooManyDocuments(
            f"the documents on one message may total "
            f"{_megabytes(MAX_TOTAL_DOCUMENT_BYTES)}"
        )


# --- extraction ------------------------------------------------------------


def _extract_pdf(data: bytes) -> tuple[str, int]:
    """Text and page count from a PDF, parsed in a bounded worker, or ``InvalidDocument``."""
    try:
        result = subprocess.run(
            [sys.executable, *_PDF_WORKER],
            input=data, capture_output=True, timeout=PDF_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise InvalidDocument("the PDF took too long to read") from None
    except OSError as exc:
        raise InvalidDocument(f"the PDF reader could not start: {exc}") from exc
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        # Killed by its CPU limit, or failed before it could say why.
        raise InvalidDocument("the PDF needs more time or memory to read than is allowed")
    if result.returncode or "error" in answer:
        raise InvalidDocument(str(answer.get("error") or "the PDF could not be read"))
    return answer["text"], answer["pages"]


def parse_pdf(data: bytes) -> tuple[str, int]:
    """Text and page count from a PDF, or ``InvalidDocument``; ``pdf_worker`` runs this.

    ``pypdf`` raises a wide and undocumented range of exceptions on malformed
    input, so everything it can throw is caught. An encrypted document is
    refused by name rather than attempted with an empty password: guessing at
    a password is not this function's business.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise InvalidDocument("the PDF is encrypted")
        total = len(reader.pages)
        if total > MAX_DOCUMENT_PAGES:
            raise InvalidDocument(
                f"the PDF has {total} pages and the limit is {MAX_DOCUMENT_PAGES}"
            )
        pages = [page.extract_text() or "" for page in reader.pages]
    except (InvalidDocument, MemoryError):
        raise
    except Exception as exc:
        raise InvalidDocument(f"the PDF could not be read: {exc}") from exc
    return "\n\n".join(part.strip() for part in pages if part.strip()), total


def _extract_docx(data: bytes) -> str:
    """Text from the body of a Word document.

    Only ``word/document.xml`` is opened. Headers, footnotes, comments and
    embedded objects are deliberately not followed: they are where a document
    hides content the user did not know they were sending.
    """
    from lxml import etree

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                member = archive.getinfo(_DOCX_MEMBER)
            except KeyError:
                raise InvalidDocument("the file is a zip but not a Word document") from None
            # The declared size is a cheap first refusal, but it comes out of
            # the zip's own central directory and a hostile file can simply
            # lie about it. The read below is what actually holds: it stops
            # after the limit no matter what the header claimed, so a small
            # archive that expands to gigabytes never gets to allocate them.
            if member.file_size > MAX_DOCX_MEMBER_BYTES:
                raise InvalidDocument("the document body is implausibly large")
            with archive.open(_DOCX_MEMBER) as handle:
                body = handle.read(MAX_DOCX_MEMBER_BYTES + 1)
            if len(body) > MAX_DOCX_MEMBER_BYTES:
                raise InvalidDocument("the document body is implausibly large")
    except InvalidDocument:
        raise
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise InvalidDocument(f"the DOCX could not be read: {exc}") from exc

    try:
        # No network, no entity expansion: a document must not be able to
        # make the parser fetch anything or expand its way out of memory.
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            huge_tree=False,
            load_dtd=False,
        )
        root = etree.fromstring(body, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise InvalidDocument(f"the DOCX body is malformed: {exc}") from exc

    namespace = {"w": _WORD_NAMESPACE}
    lines: list[str] = []
    for paragraph in root.iter(f"{{{_WORD_NAMESPACE}}}p"):
        runs = paragraph.findall(".//w:t", namespace)
        line = "".join(run.text or "" for run in runs)
        lines.append(line)
    return "\n".join(lines)


def _extract_text(data: bytes) -> str:
    """Decode a text file, refusing a binary that happens to decode."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = _decode_detected(data)
    control = sum(1 for ch in text if ch < " " and ch not in "\t\n\r")
    if text and control / len(text) > _MAX_CONTROL_RATIO:
        raise InvalidDocument("this looks like a binary file, not text")
    # Normalise line endings so a CRLF file does not spend its budget on
    # carriage returns the model has no use for.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_detected(data: bytes) -> str:
    """Second chance for a text file that is not UTF-8."""
    import chardet

    guess = chardet.detect(data)
    encoding = guess.get("encoding") if guess else None
    if not encoding:
        raise InvalidDocument("the file is not text in any encoding we could detect")
    try:
        return data.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError) as exc:
        raise InvalidDocument(f"the file could not be decoded as {encoding}") from exc


def _megabytes(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MB"
