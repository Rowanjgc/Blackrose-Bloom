"""Per-reply authorization for web and remote image requests that model output proposes.

The model chooses tool arguments, so a URL or search query it writes can carry
private data to a destination it also chose. SSRF checks decide where a request
may connect; this policy decides whether the user authorized sending it at all.

A page read runs without asking only when the user typed that exact URL in this
conversation or a search in this reply returned it. While the conversation has
not used local data, a link on a page read in this reply also counts, up to
MAX_LINK_HOPS links away from such an address: the page's author wrote it
before seeing anything private, as for search results. Form submissions and other
app-composed requests are not typed text; the caller leaves them out. A search runs without asking
until the conversation has used local files (including names and listings),
the library, references or attachments; that use is recorded on the
conversation and is never cleared, so it survives later replies, stops,
context pruning and restarts. Anything else needs the user's approval of that exact request, and
is refused when no approver is available. Prompt text never grants authority.

When asking about a page read, the user may instead allow its whole site for
this conversation (``SITE``); later reads on that host, at its default port,
then run without asking until the app closes. The grant is held in memory on
the conversation, not the reply, and is never offered for hosts where anyone
can publish or read the requests (``web.sites``). A grant given before the
conversation used local data stops applying once it does, since the user
allowed the site when nothing private could reach it. A read that a site grant
alone authorized must stay on allowed sites through every redirect, so an open
redirect on the site cannot pass the address on.

The user's trusted sites (``trusted_hosts`` in the configuration, edited in
Trusted sites) act as a site grant in every conversation, over HTTPS only, and
are held to the same hosts (``web.sites.trusted_entry``) and the same redirect
rule. Once the conversation has used local data they cover only addresses
without a query, since a trusted site's own search or analytics may still
record what a query carries.

When one model step proposes several web requests that need approval, the
user is asked once (``preauthorize``): the sheet lists every exact request
with a checkbox (up to MAX_BATCH), the answer counts once toward MAX_PROMPTS, and each request
is then approved or declined as if asked alone. A request left unchecked is a
decline, so the rest of the reply asks no further. A batch offers no site
grant; asked alone, a read still can.

Remote image generation sends a model-written prompt and stored reference
images to a separate service, so it follows the search rule: automatic until
the conversation has used local data or a request was declined in this reply,
then only the exact approved prompt, aspect ratio and references are sent.
Choosing a chat model or enabling images is not consent to disclose context.
Local image generation sends nothing and is never asked about.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import NoReturn
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from .web.sites import site_key, trusted_by
from .web.urls import validate_fetch_url

log = logging.getLogger(__name__)

MAX_PROMPTS = 8
MAX_LINK_HOPS = 2
MAX_BATCH = 8  # requests in one batched question; later ones are asked alone
PAGE_LINK = 'link on a page read in this reply'
SITE_GRANT = 'site you allowed for this chat'
TRUSTED_SITE = 'trusted site'
SITE = 'site'  # an approver's answer allowing the request's whole site for this conversation
_URL = re.compile(r'https?://[^\s<>"\'`]+', re.IGNORECASE)
_DEFAULT_PORTS = {'http': 80, 'https': 443}


@dataclass(frozen=True)
class OutboundBatch:
    """Several requests from one model step, asked about together; the answer is one bool per request."""
    requests: tuple[OutboundRequest, ...]


class OutboundDenied(PermissionError):
    """An outbound request was not authorized; nothing was sent."""

    def __init__(self, code: str, message: str, request: OutboundRequest):
        super().__init__(message)
        self.code = code
        self.request = request


@dataclass(frozen=True)
class OutboundRequest:
    kind: str    # 'fetch', 'search' or 'image'
    target: str  # the exact URL, query or image prompt that would be sent
    origin: str  # 'assistant' or 'citation check'
    reason: str
    service: str = ''               # where an image request goes
    references: tuple[str, ...] = ()  # stored images an image request uploads
    site: str = ''                  # the host the user may allow for this chat; empty if not offered

    @property
    def host(self) -> str | None:
        if self.kind != 'fetch':
            return None
        try:
            host = urlsplit(self.target).hostname
        except ValueError:
            return None
        return ascii_host(host) if host else None


# Asked about one request: True for it alone, or ``SITE``. Asked about a batch: one bool per request.
Approver = Callable[[OutboundRequest | OutboundBatch], Awaitable[bool | str | tuple[bool, ...]]]


def ascii_host(host: str) -> str:
    """The ASCII form of a hostname, which exposes look-alike Unicode names."""
    try:
        return host.encode('idna').decode('ascii')
    except UnicodeError:
        return visible(host)


def site_of(url: str) -> str | None:
    """The site grant that would cover ``url``: its host, when the URL uses the scheme's default port."""
    key = request_key(url)
    if key is None or key[2] != _DEFAULT_PORTS[key[0]]:
        return None
    return site_key(key[1])


def request_key(url: str) -> tuple | None:
    """What a GET transmits to its server: everything in the URL but the fragment."""
    try:
        parts = urlsplit(url.strip())
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
    except (AttributeError, ValueError):
        return None
    if scheme not in _DEFAULT_PORTS or not host or parts.username or parts.password:
        return None
    return scheme, host, port or _DEFAULT_PORTS[scheme], parts.path or '/', parts.query


def refusal_key(kind: str, target: str) -> tuple[str, object]:
    """What declining ``target`` covers: the same GET as ``request_key``, or the exact search query.

    A batch refusal must not lapse because the model spells the same request differently
    (a fragment, a host's case, the scheme's default port), so both sides use one identity.
    """
    key = request_key(target) if kind == 'fetch' else None
    return kind, key if key is not None else target


def user_urls(text: str) -> set[tuple]:
    """Exact request keys for URLs written in user text, with or without trailing punctuation."""
    keys = set()
    for match in _URL.finditer(text or ''):
        raw = match.group(0)
        for candidate in (raw, raw.rstrip('.,;:!?\'")]}>')):
            if (key := request_key(candidate)) is not None:
                keys.add(key)
    return keys


def visible(text: str) -> str:
    """Show control, formatting, bidirectional and non-ASCII space characters instead of rendering them.

    A run of Unicode spaces renders as blank, so an address carrying one would read as
    the address without it; the ASCII space stays readable in prose.
    """
    return ''.join(f'\\u{ord(c):04X}' if unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp')
                   or (c != ' ' and unicodedata.category(c) == 'Zs') else c
                   for c in text)


@dataclass(frozen=True)
class Approval:
    """What the approval sheet shows, most important first; every field is plain, visible text."""
    title: str    # names the destination
    summary: str  # who asks and why it needs approval
    label: str    # what the payload is and who receives it
    payload: str  # exactly what is sent, hidden characters shown
    note: str     # when to allow it, and what each answer covers
    accept: str   # label of the accept button


def approval(request: OutboundRequest) -> Approval:
    """The approval question for one request, split for display."""
    who = 'the citation check' if request.origin == 'citation check' else 'the assistant'
    payload = visible(request.target) or '(empty)'
    note = 'Allow it only if you expected it and it contains nothing private.'
    if request.kind == 'fetch':
        host = request.host or 'an unknown site'
        title = f'Read a page on {host}?'
        label = f'Full address, all of it sent to {host}'
    elif request.kind == 'image':
        service = visible(request.service) or 'an unknown service'
        title = 'Generate an image remotely?'
        label = f'Prompt sent to {service}'
        uploads = '\n'.join(visible(r) for r in request.references) or 'none'
        payload += f'\n\nReference images uploaded with it:\n{uploads}'
    else:
        title = 'Search the web?'
        label = 'Query sent to your search service and the engines it uses'
    if request.site:
        note += (f' Allowing the site lets {who} read any address on {request.site} in this chat '
                 'without asking, including addresses it writes, until Blackrose closes.')
    accept = 'Allow once' if request.site else 'Allow'
    summary = request.reason if request.origin != 'citation check' else f'Asked by {who}. {request.reason}'
    return Approval(title, summary, label, payload, note, accept)


def batch_approval(batch: OutboundBatch) -> tuple[Approval, list[str]]:
    """The question for a batch, and one exact, visible payload per request in order."""
    count = len(batch.requests)
    kinds = {r.kind for r in batch.requests}
    title = (f'Read {count} pages?' if kinds == {'fetch'} else f'Run {count} searches?' if kinds == {'search'}
             else f'Send {count} web requests?')
    summary = ('The assistant asked for these together. Each needs approval: it wrote the address itself, '
               'or this chat has used your local files, library, references or attachments.')
    items = [f'{"Search" if r.kind == "search" else r.host or "unknown site"}\n{visible(r.target) or "(empty)"}'
             for r in batch.requests]
    note = ('Uncheck anything you did not expect or that contains something private. Allowing sends only '
            'the checked requests; unchecking one also stops further requests that need approval in this reply.')
    return Approval(title, summary, 'Each request, all of it sent to the named site or your search service',
                    '', note, 'Allow checked'), items


def approval_text(request: OutboundRequest) -> str:
    """The whole question as plain text, for a dialog that shows one message."""
    shown = approval(request)
    return '\n\n'.join((shown.title, shown.summary, f'{shown.label}:\n{shown.payload}', shown.note))


def _noun(request: OutboundRequest) -> str:
    return 'image request' if request.kind == 'image' else 'web request'


class OutboundPolicy:
    def __init__(self, user_texts=(), *, approve: Approver | None = None,
                 private: bool = False, sites: dict[str, bool] | None = None, trusted=()):
        self._allowed = {key: 'your message' for text in user_texts for key in user_urls(text)}
        self._hops: dict[tuple, int] = {}  # links followed from a non-link grant; absent means 0
        self._queries: set[str] = set()
        self._images: set[tuple] = set()
        self._approve = approve
        self._prompts = 0
        self._declined = False
        self._refused: set[tuple[str, object]] = set()  # (kind, identity) unchecked in a batch
        self.private = private
        # The conversation's site grants: host -> whether it was granted after local data was in use.
        self._sites = {} if sites is None else sites
        self._trusted = tuple(trusted)  # normalized trusted_hosts entries

    def mark_private(self) -> None:
        self.private = True

    def site_allows(self, url: str) -> bool:
        """Whether a site grant covers reading ``url`` now; redirects of granted reads are held to this."""
        site = site_of(url)
        return site is not None and site in self._sites and (self._sites[site] or not self.private)

    def trusted_allows(self, url: str) -> bool:
        """Whether the user's trusted sites cover reading ``url`` now."""
        key = request_key(url)
        return (key is not None and key[0] == 'https' and key[2] == 443 and not (self.private and key[4])
                and trusted_by(key[1], self._trusted) is not None)

    def stays_allowed(self, url: str) -> bool:
        """The redirect check for reads that a site grant or trusted site alone authorized."""
        return self.site_allows(url) or self.trusted_allows(url)

    def note_results(self, result: dict) -> None:
        """Search results are host-validated addresses the model did not write."""
        for row in result.get('results', []):
            url = row.get('url') if isinstance(row, dict) else None
            if isinstance(url, str) and (key := request_key(url)) is not None:
                self._allowed.setdefault(key, 'search result')

    def note_links(self, url: str, result: dict) -> None:
        """Links on a page that ``url`` fetched; they grant reads only while the chat is not private."""
        page = request_key(url)
        hops = (self._hops.get(page, 0) if page is not None else 0) + 1
        if hops > MAX_LINK_HOPS:
            return
        for row in result.get('links', []):
            link = row.get('url') if isinstance(row, dict) else None
            if isinstance(link, str) and (key := request_key(link)) is not None and key not in self._allowed:
                self._allowed[key] = PAGE_LINK
                self._hops[key] = hops

    async def authorize(self, name: str, arguments: dict, origin: str = 'assistant') -> str:
        """Return the authorization basis, or raise OutboundDenied before anything is sent."""
        basis, request, key = self._check(name, arguments, origin)
        if request is None:
            kind, target = ('search', arguments['query']) if name == 'web_search' else ('fetch', arguments['url'])
            if refusal_key(kind, target) not in self._refused:
                assert basis is not None  # _check returns a basis whenever it returns no request
                return basis
            # Unchecked in a batch, it stays declined even if this round has since granted it.
            self._refuse(OutboundRequest(kind, target, origin, ''))
        answer = await self._ask(request)
        self._record(request, key)
        if answer == SITE:
            # Granted now, it covers this conversation whether or not local data is in use.
            # The ``or`` only matters if a True grant is asked over again, which cannot happen: a
            # True entry always satisfies site_allows, so authorize never asks about its host.
            # Dropping the ``or`` is therefore an equivalent mutant (see SiteGrantTests).
            self._sites[request.site] = self.private or self._sites.get(request.site, False)
        return 'approved by you'

    async def preauthorize(self, calls, origin: str = 'assistant') -> None:
        """Ask once about every request in ``calls`` (name, arguments) that would need approval.

        Answers are recorded for ``authorize``, which each call still passes
        through; with fewer than two such requests nothing is asked here.
        """
        pending: dict[tuple, tuple[OutboundRequest, tuple | None]] = {}
        for name, arguments in calls:
            try:
                _, request, key = self._check(name, arguments, origin)
            except (KeyError, TypeError, ValueError):
                continue  # The call fails on its own when it runs.
            if request is not None and len(pending) < MAX_BATCH:
                pending.setdefault((request.kind, request.target), (request, key))
        if (len(pending) < 2 or self._approve is None or self._declined or self._prompts >= MAX_PROMPTS):
            return  # Asked, or refused, one at a time as each call runs.
        requests = tuple(replace(request, site='') for request, _ in pending.values())
        self._prompts += 1
        answer = await self._approve(OutboundBatch(requests))
        answers = ((True,) * len(requests) if answer is True else tuple(answer)
                   if isinstance(answer, (tuple, list)) and len(answer) == len(requests) else (False,) * len(requests))
        for (request, key), approved in zip(pending.values(), answers, strict=True):
            if approved is True:
                self._record(request, key)
            else:
                self._refused.add(refusal_key(request.kind, request.target))
                self._declined = True
        log.info('Outbound batch of %d: %d approved by the user', len(requests), sum(a is True for a in answers))

    def _record(self, request: OutboundRequest, key: tuple | None) -> None:
        if request.kind == 'fetch' and key is not None:
            self._allowed[key] = 'approved by you'
            self._hops.pop(key, None)
        elif request.kind == 'search':
            self._queries.add(request.target)

    def _check(self, name: str, arguments: dict, origin: str) -> tuple[str | None, OutboundRequest | None, tuple | None]:
        """The basis a request runs on, or the request to ask about; raises if it can never be sent."""
        key = None
        if name == 'fetch_content':
            # Never ask the user to approve a request that SSRF validation would refuse, and
            # key, show and send one address: the fetcher transmits this normalized form.
            url = validate_fetch_url(arguments['url'])
            key = request_key(url)
            basis = self._allowed.get(key) if key is not None else None
            # Once local data is in use, a hostile page's many links would widen the
            # model's covert choice among them, so links on pages ask again.
            if basis is not None and not (basis == PAGE_LINK and self.private):
                return basis, None, key
            if self.site_allows(url):
                return SITE_GRANT, None, key
            if self.trusted_allows(url):
                return TRUSTED_SITE, None, key
            request = OutboundRequest('fetch', url, origin, 'The assistant wrote this address; it is not '
                                      'in your messages or in search results from this reply.'
                                      if basis is None else 'It is a link on a page read in this reply, and '
                                      'this chat has used your local files, library, references or attachments.', site=site_of(url) or '')
        elif name == 'web_search':
            query = arguments['query']
            if not self.private:
                return 'automatic', None, None
            if query in self._queries:
                return 'approved by you', None, None
            request = OutboundRequest('search', query, origin, 'This chat has used your local files, library, '
                                      'references or attachments, so new searches need approval.')
        else:
            raise ValueError(f'{name} is not an outbound request')
        return None, request, key

    async def authorize_image(self, prompt: str, aspect_ratio: str, references: dict[int, str], *,
                              service: str, origin: str = 'assistant') -> str:
        """Authorize one remote image job; ``references`` maps attachment IDs to display names."""
        identity = (prompt, aspect_ratio, tuple(sorted(references)))
        if not (self.private or self._declined):
            return 'automatic'
        if identity in self._images:
            return 'approved by you'
        # A decline earlier in this reply refuses this before anyone is asked.
        reason = ('This chat has used your local files, library, references or attachments, '
                  'so remote image requests need approval.')
        await self._ask(OutboundRequest('image', prompt, origin, reason, service,
                                        tuple(f'{references[i]} (attachment {i})' for i in sorted(references))))
        self._images.add(identity)
        return 'approved by you'

    async def _ask(self, request: OutboundRequest) -> bool | str:
        """Return True for this request alone or ``SITE`` for its site, or raise OutboundDenied."""
        if self._approve is None:
            self._deny('approval_unavailable', f'This {_noun(request)} needs your approval, which is '
                       'unavailable here. Nothing was sent.', request)
        if refusal_key(request.kind, request.target) in self._refused:
            self._refuse(request)
        if self._declined:
            self._deny('approval_declined', 'A request was declined earlier in this reply; further '
                       'unapproved requests are blocked until the next message. Nothing was sent.', request)
        if self._prompts >= MAX_PROMPTS:
            self._deny('approval_limit', 'This reply reached its limit of approval requests. '
                       'Nothing was sent. Pages from search results and links on pages already read '
                       'need no approval; use those instead of composing addresses.', request)
        self._prompts += 1
        answer = await self._approve(request)
        if not (answer is True or (answer == SITE and request.site)):
            self._declined = True
            self._deny('approval_declined', f'The user declined this {_noun(request)}. Nothing was sent; '
                       + ('do not retry it with a different prompt or references.' if request.kind == 'image'
                          else 'do not retry it with a different address or query.'), request)
        log.info('Outbound %s approved by the user%s%s', request.kind,
                 f' ({request.host})' if request.host else '', ', with its site for this chat' if answer == SITE else '')
        return answer

    def _refuse(self, request: OutboundRequest) -> NoReturn:
        self._deny('approval_declined', f'The user declined this {_noun(request)}. Nothing was sent; '
                   'do not retry it with a different address or query.', request)

    @staticmethod
    def _deny(code: str, message: str, request: OutboundRequest) -> NoReturn:
        # Log the decision and destination host only; URLs and queries may contain private data.
        log.info('Outbound %s refused: %s%s', request.kind, code, f' ({request.host})' if request.host else '')
        raise OutboundDenied(code, message, request)
