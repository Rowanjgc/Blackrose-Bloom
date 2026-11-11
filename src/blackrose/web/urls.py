"""Conservative URL handling for result deduplication and page fetching."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit


def is_public_address(value: str) -> bool:
    """Accept routable public unicast addresses, without local scope IDs."""
    if '%' in value:
        return False
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast and not address.is_reserved


def validate_fetch_url(url: str) -> str:
    """The URL to request, or ValueError for unsupported and obviously local targets.

    The result is the address as ``urlsplit`` reads it (outer whitespace removed, tabs
    and newlines dropped), and callers must send that rather than ``url``: the outbound
    grant key is computed from the same parse, so the authorized request and the
    transmitted one cannot differ by characters the parse ignores.
    """
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("only absolute HTTP(S) URLs can be fetched")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not fetched")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("local URLs are not fetched")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not is_public_address(hostname):
        raise ValueError("private or local IP addresses are not fetched")
    return urlunsplit(parsed)
