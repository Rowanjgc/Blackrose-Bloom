"""Recognising an image without decoding one.

Blackrose needs three facts about an attachment: what it is, how big it is,
and whether the backend can be handed it. All three come out of the file's
header, so nothing here decodes pixel data.

That is a deliberate security decision as much as a dependency one. Image
decoders are among the most attacked pieces of code in a desktop stack, and
Blackrose has no reason to run one: the model does the looking. Parsing a
fixed number of bytes of header structure has none of that exposure, and it
means an attachment can be rejected as malformed before any of it is passed on.

The cost is that Blackrose cannot resize or re-encode. That is why the limits
below are enforced rather than worked around, and why an oversized image is
refused with an explanation instead of being silently downscaled.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

# What the vision path accepts. Anything else is refused by name rather than
# passed to a backend to fail on.
SUPPORTED_IMAGE_TYPES: dict[str, tuple[str, ...]] = {
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/webp": (".webp",),
}

# One message's worth of vision context. More than a handful of images stops
# being a question about a screenshot and starts being an album.
MAX_IMAGE_ATTACHMENTS_PER_MESSAGE = 4
# 12 MB is comfortably above any screenshot and below anything that would
# make a base64 payload a problem for the runtime.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 24 * 1024 * 1024
# Guards the pathological case a byte limit misses: a highly compressible
# image whose decoded size would dwarf its file size.
MAX_IMAGE_PIXELS = 64_000_000


class InvalidImage(ValueError):
    """The bytes offered are not a usable image of a supported type."""


class TooManyAttachments(ValueError):
    """The message is already carrying as much as it may."""


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """What the header says. Dimensions are optional: some are unreadable."""

    mime_type: str
    size: int
    width: int | None = None
    height: int | None = None

    @property
    def suffix(self) -> str:
        return SUPPORTED_IMAGE_TYPES[self.mime_type][0]

    @property
    def dimensions(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "unknown size"


def suffix_for(mime_type: str) -> str:
    """The canonical file extension for a supported type."""
    try:
        return SUPPORTED_IMAGE_TYPES[mime_type][0]
    except KeyError:
        raise InvalidImage(f"unsupported image type {mime_type!r}") from None


def sniff_type(data: bytes) -> str | None:
    """The MIME type the bytes actually are, ignoring any claimed name."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    # IHDR is required to be the first chunk, at a fixed offset.
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk the marker segments to the first frame header."""
    index = 2
    end = len(data)
    while index + 9 < end:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # Padding and standalone markers carry no length field.
        if marker in (0xFF, 0x01) or 0xD0 <= marker <= 0xD9:
            index += 2
            continue
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        if length < 2:
            return None
        # SOF0..SOF15, excluding the two that are not frame headers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """WebP has three container flavours and each stores size differently."""
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


_DIMENSION_READERS = {
    "image/png": _png_dimensions,
    "image/jpeg": _jpeg_dimensions,
    "image/webp": _webp_dimensions,
}


def inspect_image(data: bytes, *, claimed_name: str | None = None) -> ImageInfo:
    """Validate bytes as a supported image and report what they are.

    The file's own header decides the type; a name is only ever a hint, so a
    ``.png`` full of something else is refused rather than believed.
    """
    if not data:
        return _reject("the file is empty", claimed_name)
    mime = sniff_type(data)
    if mime is None:
        return _reject(
            "this is not a PNG, JPEG or WebP image "
            "(the file's own header decides, not its name)",
            claimed_name,
        )
    if len(data) > MAX_IMAGE_BYTES:
        return _reject(
            f"it is {_megabytes(len(data))}, over the "
            f"{_megabytes(MAX_IMAGE_BYTES)} limit for one image",
            claimed_name,
        )

    width = height = None
    dimensions = _DIMENSION_READERS[mime](data)
    if dimensions is not None:
        width, height = dimensions
        if width <= 0 or height <= 0:
            return _reject("its header reports an impossible size", claimed_name)
        if width * height > MAX_IMAGE_PIXELS:
            return _reject(
                f"{width}×{height} is more than {MAX_IMAGE_PIXELS // 1_000_000} "
                "megapixels",
                claimed_name,
            )
    return ImageInfo(mime_type=mime, size=len(data), width=width, height=height)


def _reject(reason: str, claimed_name: str | None) -> ImageInfo:
    name = f"{claimed_name}: " if claimed_name else ""
    raise InvalidImage(f"{name}{reason}")


def _megabytes(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MB"


def read_image_file(path: Path) -> tuple[bytes, ImageInfo]:
    """Read and validate an image from disk.

    Reads at most one byte past the limit, so pointing Blackrose at a very
    large file costs a bounded read rather than the whole file.
    """
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise InvalidImage(f"{path}: {exc}") from exc
    if not resolved.is_file():
        raise InvalidImage(f"{path}: no such file")
    try:
        with resolved.open("rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError as exc:
        raise InvalidImage(f"{path}: {exc}") from exc
    return data, inspect_image(data, claimed_name=resolved.name)


def check_budget(existing: list[int], incoming: int) -> None:
    """Whether one more attachment of ``incoming`` bytes may join ``existing``."""
    if len(existing) >= MAX_IMAGE_ATTACHMENTS_PER_MESSAGE:
        raise TooManyAttachments(
            f"a message may carry {MAX_IMAGE_ATTACHMENTS_PER_MESSAGE} images"
        )
    total = sum(existing) + incoming
    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        raise TooManyAttachments(
            f"that would put {_megabytes(total)} on one message, over the "
            f"{_megabytes(MAX_TOTAL_ATTACHMENT_BYTES)} limit"
        )
