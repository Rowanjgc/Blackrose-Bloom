"""Which sites a user may allow as a whole, rather than one exact address at a time.

Allowing a site lets the model write any address on it, so the model can put
data in the path or query and whoever reads that site's request logs sees it.
That is acceptable only where one operator runs the whole host. It is not on
hosts where anyone can publish, collect requests or see per-path statistics:
names under a Public Suffix List private-section entry (github.io, pages.dev
and similar hand out subdomains), package CDNs, pastebins, URL shorteners and
request-capture services. IP literals are refused too: nothing names who runs
them. Such hosts stay on exact-address approval.

The PSL comes from the system copy, which the distribution keeps current; the
built-in suffixes below are always refused as well, so a missing system list
still covers the common publishing hosts.

The user's trusted sites (``trusted_hosts``) are site grants that last for
every conversation, so their entries are held to the same rule.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_LABEL = re.compile(r'(?!-)[a-z0-9-]{1,63}(?<!-)')

PSL_PATHS = (Path('/usr/share/publicsuffix/public_suffix_list.dat'),
             Path('/usr/share/publicsuffix/effective_tld_names.dat'))

# Shared hosting that hands out subdomains; a subset of the PSL private section.
USER_SUBDOMAIN_SUFFIXES = frozenset({
    'github.io', 'githubusercontent.com', 'gitlab.io', 'codeberg.page', 'pages.dev', 'workers.dev',
    'vercel.app', 'netlify.app', 'herokuapp.com', 'fly.dev', 'onrender.com', 'glitch.me', 'repl.co',
    'replit.dev', 'surge.sh', 'neocities.org', 'blogspot.com', 'wordpress.com', 'web.app',
    'firebaseapp.com', 'appspot.com', 'azurewebsites.net', 'cloudfront.net', 's3.amazonaws.com',
    'ngrok.io', 'ngrok-free.app', 'trycloudflare.com', 'deno.dev', 'val.run',
})
# Hosts where anyone can publish at a path, shorten a link or read the requests sent.
PUBLISH_ANYWHERE = frozenset({
    'gist.github.com', 'unpkg.com', 'jsdelivr.net', 'jsdelivr.com', 'cdnjs.cloudflare.com', 'esm.sh',
    'skypack.dev', 'pastebin.com', 'paste.ee', 'hastebin.com', 'dpaste.org', 'rentry.co', 'ghostbin.co',
    'bit.ly', 'tinyurl.com', 't.co', 'is.gd', 'goo.gl', 'ow.ly', 'buff.ly', 'cutt.ly', 'rebrand.ly',
    'webhook.site', 'requestbin.com', 'pipedream.net', 'beeceptor.com', 'hookbin.com',
    'canarytokens.com', 'burpcollaborator.net', 'oastify.com', 'interact.sh',
})


def _ascii(name: str) -> str | None:
    name = name.strip().rstrip('.').lower()
    try:
        return name.encode('idna').decode('ascii') if name else None
    except UnicodeError:
        return None


@lru_cache(maxsize=1)
def _suffixes() -> tuple[frozenset[str], frozenset[str]] | None:
    """PSL (ICANN, private) rules; wildcard and exception rules count as their base name."""
    for path in PSL_PATHS:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError:
            # Optional PSL location; try the next candidate.
            continue
        icann, private, section = set(), set(), None
        for line in lines:
            line = line.strip()
            if line.startswith('// ===BEGIN ICANN DOMAINS==='):
                section = icann
            elif line.startswith('// ===BEGIN PRIVATE DOMAINS==='):
                section = private
            elif line.startswith(('// ===END ICANN DOMAINS===', '// ===END PRIVATE DOMAINS===')):
                section = None
            elif section is not None and line and not line.startswith('//'):
                if (rule := _ascii(line.lstrip('!').removeprefix('*.'))) is not None:
                    section.add(rule)
        if private:
            return frozenset(icann), frozenset(private)
    log.warning('No system Public Suffix List; refusing only the built-in publishing hosts for site grants')
    return None


def _private_suffixes() -> frozenset[str]:
    return suffixes[1] if (suffixes := _suffixes()) else frozenset()


def _within(host: str, names) -> bool:
    labels = host.split('.')
    return any('.'.join(labels[i:]) in names for i in range(len(labels)))


def site_key(host: str | None) -> str | None:
    """The ASCII host a site grant is recorded under, or None if it cannot be allowed as a whole."""
    host = _ascii(host or '')
    if host is None or '.' not in host or not all(map(_LABEL.fullmatch, host.split('.'))):
        return None
    try:
        ipaddress.ip_address(host.strip('[]'))
        return None
    except ValueError:
        # Not an IP literal; continue with hostname checks.
        pass
    if _within(host, PUBLISH_ANYWHERE) or _within(host, USER_SUBDOMAIN_SUFFIXES | _private_suffixes()):
        return None
    return host


def trusted_entry(entry: str) -> str | None:
    """The normalized form of a trusted-sites entry, or None if it cannot be trusted.

    An entry is one host, or ``*.`` and a domain to cover the domain and all
    its subdomains. Every name an entry covers must be one a site grant could
    cover, and a wildcard must not span a public suffix (``*.co.uk``), which
    needs the system list to tell.
    """
    if not isinstance(entry, str):
        return None
    entry = entry.strip()
    if '://' in entry:
        # A pasted address stands for its site, when it names one at the default port.
        try:
            parts = urlsplit(entry)
            if (parts.scheme.lower() not in ('http', 'https') or parts.port is not None
                    or parts.username is not None or parts.password is not None):
                return None
        except ValueError:
            return None
        entry = parts.hostname or ''
    wildcard = entry.startswith('*.')
    host = site_key(entry.removeprefix('*.'))
    if host is None or not wildcard:
        return host
    suffixes = _suffixes()
    if suffixes is None or host in suffixes[0] or any(name.endswith('.' + host) for name in
                                                      (*PUBLISH_ANYWHERE, *USER_SUBDOMAIN_SUFFIXES, *suffixes[1])):
        return None
    return '*.' + host


def trusted_by(host: str | None, entries) -> str | None:
    """The entry in ``entries`` (normalized) that covers ``host``, if any."""
    host = site_key(host)
    if host is None:
        return None
    for entry in entries:
        if host == entry or (entry.startswith('*.') and (host == entry[2:] or host.endswith(entry[1:]))):
            return entry
    return None
