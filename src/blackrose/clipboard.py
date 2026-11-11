"""Read and write the host clipboard through installed desktop helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Anything slower than this is a hung helper, not a slow clipboard.
_TIMEOUT = 2.0

# The formats worth asking a clipboard for, best first. PNG is what every
# screenshot tool on every one of these desktops puts on the clipboard.
IMAGE_TARGETS: tuple[tuple[str, str], ...] = (
    ("image/png", ".png"),
    ("image/jpeg", ".jpg"),
    ("image/webp", ".webp"),
)


@dataclass(frozen=True, slots=True)
class ClipboardImage:
    """Image bytes taken off the clipboard, with the type it claimed to be."""

    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ClipboardTool:
    """One desktop's clipboard helper, described rather than hardcoded."""

    name: str
    copy: tuple[str, ...]
    paste_text: tuple[str, ...]
    # ``{target}`` is substituted with a MIME type; ``None`` means this
    # helper cannot be asked for a specific type and so cannot fetch images.
    paste_typed: tuple[str, ...] | None
    list_targets: tuple[str, ...] | None


# Order matters: the session's own compositor first, then the X11 helpers that
# also work under XWayland, then macOS.
TOOLS: tuple[ClipboardTool, ...] = (
    ClipboardTool(
        name="wl-clipboard",
        copy=("wl-copy",),
        paste_text=("wl-paste", "--no-newline", "--type", "text/plain"),
        paste_typed=("wl-paste", "--no-newline", "--type", "{target}"),
        list_targets=("wl-paste", "--list-types"),
    ),
    ClipboardTool(
        name="xclip",
        copy=("xclip", "-selection", "clipboard"),
        paste_text=("xclip", "-selection", "clipboard", "-o"),
        paste_typed=("xclip", "-selection", "clipboard", "-t", "{target}", "-o"),
        list_targets=("xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"),
    ),
    ClipboardTool(
        name="xsel",
        copy=("xsel", "--clipboard", "--input"),
        paste_text=("xsel", "--clipboard", "--output"),
        paste_typed=None,
        list_targets=None,
    ),
    ClipboardTool(
        name="pbcopy",
        copy=("pbcopy",),
        paste_text=("pbpaste",),
        paste_typed=None,
        list_targets=None,
    ),
)


def available_tool() -> ClipboardTool | None:
    """The first clipboard helper this machine actually has."""
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    ordered = sorted(TOOLS, key=lambda t: not (wayland and t.name == "wl-clipboard"))
    for tool in ordered:
        if shutil.which(tool.copy[0]) and shutil.which(tool.paste_text[0]):
            return tool
    return None


async def _run(
    argv: tuple[str, ...], *, stdin: bytes | None = None
) -> tuple[int, bytes]:
    """Run a helper. Returns ``(-1, b"")`` for anything that goes wrong."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        log.debug("clipboard helper %s could not start: %s", argv[0], exc)
        return -1, b""
    try:
        out, _ = await asyncio.wait_for(process.communicate(stdin), timeout=_TIMEOUT)
    except TimeoutError:
        log.debug("clipboard helper %s timed out", argv[0])
        with_kill = getattr(process, "kill", None)
        if with_kill is not None:
            try:
                process.kill()
            except ProcessLookupError:
                # The process already exited.
                pass
        return -1, b""
    except (OSError, ValueError) as exc:
        log.debug("clipboard helper %s failed: %s", argv[0], exc)
        return -1, b""
    return process.returncode or 0, out or b""


async def copy_text(text: str) -> bool:
    """Put text on the host clipboard. Returns whether a helper accepted it."""
    tool = available_tool()
    if tool is None:
        return False
    code, _ = await _run(tool.copy, stdin=text.encode("utf-8"))
    return code == 0


async def paste_text() -> str | None:
    """Read text from the host clipboard, or ``None`` if there is no text.

    Asking a clipboard holding a PNG for ``text/plain`` does not fail on every
    helper -- some hand back the image bytes. Decoding those leniently would
    put binary straight into the composer, so this decodes strictly and treats
    anything that is not text as "no text".
    """
    tool = available_tool()
    if tool is None:
        return None
    code, out = await _run(tool.paste_text)
    if code != 0 or not out:
        return None
    return decode_text(out)


def decode_text(raw: bytes) -> str | None:
    """Decode clipboard bytes as text, or ``None`` if they are not text."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Binary that happens to decode is still not text. Tab, newline and
    # carriage return are the only control characters a paste should carry.
    if any(ch < " " and ch not in "\t\n\r" for ch in text):
        return None
    return text


async def clipboard_targets() -> tuple[str, ...]:
    """What the clipboard says it can offer. Empty when unknown."""
    tool = available_tool()
    if tool is None or tool.list_targets is None:
        return ()
    code, out = await _run(tool.list_targets)
    if code != 0:
        return ()
    return tuple(
        line.strip()
        for line in out.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    )


async def paste_image() -> ClipboardImage | None:
    """Read an image off the clipboard, if there is one.

    Asks only for types the vision path can actually accept, and only when the
    clipboard advertises one -- probing blind makes some helpers block.
    """
    tool = available_tool()
    if tool is None or tool.paste_typed is None:
        return None
    advertised = await clipboard_targets()
    for mime, _suffix in IMAGE_TARGETS:
        if advertised and mime not in advertised:
            continue
        argv = tuple(part.replace("{target}", mime) for part in tool.paste_typed)
        code, out = await _run(argv)
        if code == 0 and out:
            return ClipboardImage(mime_type=mime, data=out)
    return None


async def clipboard_has_image() -> bool:
    """Whether the clipboard is advertising an image type we can use."""
    advertised = await clipboard_targets()
    return any(mime in advertised for mime, _ in IMAGE_TARGETS)
