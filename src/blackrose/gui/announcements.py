"""Speak live-region updates through GTK, because WebKitGTK does not.

WebKitGTK 2.52 exposes ``aria-live``/``role=status`` attributes over AT-SPI but
emits no event when a live region's text changes, so Orca never speaks it
(bra-max). The page forwards each settled live-region update to the bridge, and
this module emits an ATK ``notification`` on the native window. The AT-SPI
bridge delivers that as ``object:announcement``, which Orca speaks.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Long enough for any status or reply outcome the page announces; a screen
# reader reading more than this unprompted would be a defect of its own.
MAX_ANNOUNCEMENT = 500


def clean(text) -> str | None:
    """Collapse whitespace and bound the length; None for anything not worth speaking."""
    if not isinstance(text, str):
        return None
    text = ' '.join(text.split())
    return text[:MAX_ANNOUNCEMENT] or None


def announce(window, text: str, assertive: bool = False) -> bool:
    """Queue ``text`` for the GTK thread; return whether it was queued."""
    from gi.repository import GLib

    def emit():
        from gi.repository import Atk
        try:
            browser = window.gui.BrowserView.instances[window.uid]
            accessible = browser.window.get_accessible()
            accessible.emit('notification', text, Atk.Live.ASSERTIVE if assertive else Atk.Live.POLITE)
        except Exception:  # noqa: BLE001 - an unspoken status must never break the page
            log.exception('Could not announce a live-region update')
        return False

    GLib.idle_add(emit)
    return True
