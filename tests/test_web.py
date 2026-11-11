"""Public destination isolation exercised through the actual HTTP transport."""
import asyncio
import os
import socket
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpcore
import httpx

from blackrose.web.extractor import ContentExtractor
from blackrose.web.fetcher import PageFetcher, PageFetchError
from blackrose.web.models import FetchedPage
from blackrose.web.transport import PublicNetworkBackend, PublicTransport
from blackrose.web.urls import validate_fetch_url


def addresses(*values):
    return [(socket.AF_INET6 if ':' in value else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, '', (value, 443)) for value in values]


class RecordingNetwork(httpcore.AsyncMockBackend):
    def __init__(self, responses):
        super().__init__([])
        self.responses = list(responses)
        self.connections = []
        self.writes = []
        self.sni = []

    async def connect_tcp(self, host, port, **kwargs):
        self.connections.append((host, port))
        self._buffer = [self.responses.pop(0)]
        stream = await super().connect_tcp(host, port, **kwargs)
        original_write, original_tls = stream.write, stream.start_tls
        async def write(data, timeout=None):
            self.writes.append(data)
            await original_write(data, timeout)
        async def tls(ssl_context, server_hostname=None, timeout=None):
            self.sni.append((server_hostname, ssl_context.check_hostname))
            return await original_tls(ssl_context, server_hostname, timeout)
        stream.write, stream.start_tls = write, tls
        return stream


def response(status='200 OK', body=b'Public text', extra=b''):
    return (b'HTTP/1.1 ' + status.encode() + b'\r\nContent-Type: text/plain\r\n'
            b'Connection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n'
            + extra + b'\r\n' + body)


class PublicFetchTests(unittest.IsolatedAsyncioTestCase):
    async def fetch_with(self, url, network, resolver):
        transport = PublicTransport(network_backend=PublicNetworkBackend(network, resolver))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            return await PageFetcher(client=client).fetch(url)

    async def test_dns_public_validation_dials_numeric_ip_and_preserves_https_origin(self):
        network = RecordingNetwork([response()])
        resolver = AsyncMock(side_effect=[addresses('93.184.216.34'), addresses('127.0.0.1')])
        page = await self.fetch_with('https://public.example/article', network, resolver)
        self.assertEqual(page.url, 'https://public.example/article')
        self.assertEqual(network.connections, [('93.184.216.34', 443)])
        self.assertEqual(resolver.await_count, 1)
        self.assertEqual(network.sni, [('public.example', True)])
        self.assertIn(b'Host: public.example\r\n', b''.join(network.writes))

    async def test_private_dns_and_mixed_answers_never_open_a_connection(self):
        for values in [('127.0.0.1',), ('10.0.0.1',), ('169.254.169.254',),
                       ('::1',), ('fd00::1',), ('::ffff:127.0.0.1',),
                       ('224.0.0.1',), ('ff02::1',), ('64:ff9b::7f00:1',),
                       ('93.184.216.34', '192.168.1.2')]:
            with self.subTest(values=values):
                network = RecordingNetwork([])
                with self.assertRaisesRegex(PageFetchError, 'private or local'):
                    await self.fetch_with('https://private.example/', network,
                                          AsyncMock(return_value=addresses(*values)))
                self.assertEqual(network.connections, [])

    async def test_global_ipv6_address_is_dialed_without_changing_hostname(self):
        network = RecordingNetwork([response()])
        await self.fetch_with('https://public.example/', network,
            AsyncMock(return_value=addresses('2606:4700:4700::1111')))
        self.assertEqual(network.connections, [('2606:4700:4700::1111', 443)])
        self.assertEqual(network.sni, [('public.example', True)])

    async def test_redirect_to_private_dns_is_rejected_before_second_connection(self):
        network = RecordingNetwork([response('302 Found', b'', b'Location: https://private.example/\r\n')])
        resolver = AsyncMock(side_effect=[addresses('93.184.216.34'), addresses('10.0.0.1')])
        with self.assertRaisesRegex(PageFetchError, 'private or local'):
            await self.fetch_with('https://public.example/', network, resolver)
        self.assertEqual(network.connections, [('93.184.216.34', 443)])

    async def test_redirect_rebinding_same_hostname_is_validated_again(self):
        network = RecordingNetwork([response('302 Found', b'', b'Location: /next\r\n')])
        resolver = AsyncMock(side_effect=[addresses('93.184.216.34'), addresses('127.0.0.1')])
        with self.assertRaises(PageFetchError):
            await self.fetch_with('https://public.example/', network, resolver)
        self.assertEqual(network.connections, [('93.184.216.34', 443)])

    async def test_default_fetcher_ignores_proxy_environment(self):
        network = RecordingNetwork([response()])
        public = PublicNetworkBackend(network, AsyncMock(return_value=addresses('93.184.216.34')))
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:8888',
                                    'ALL_PROXY': 'http://127.0.0.1:8889'}), \
             patch('blackrose.web.transport.PublicNetworkBackend', return_value=public):
            fetcher = PageFetcher()
            try:
                self.assertEqual((await fetcher.fetch('https://public.example/')).body, 'Public text')
            finally:
                await fetcher.aclose()
        self.assertEqual(network.connections, [('93.184.216.34', 443)])

    async def test_numeric_obfuscated_loopback_dns_is_rejected(self):
        network = RecordingNetwork([])
        with self.assertRaises(PageFetchError):
            await self.fetch_with('http://2130706433/', network,
                                  AsyncMock(return_value=addresses('127.0.0.1')))
        self.assertEqual(network.connections, [])

    async def test_numeric_host_spellings_are_refused_before_connecting(self):
        # The platform's numeric parser expands these spellings locally; no DNS query is made.
        # Spellings httpx cannot parse are refused as invalid URLs, also before connecting.
        async def numeric(host, port, **kwargs):
            return socket.getaddrinfo(host, port, flags=socket.AI_NUMERICHOST, **kwargs)
        for host in ('127.1', '0x7f000001', '0177.0.0.1', '2130706433', '[::1]',
                     '[::ffff:7f00:1]', '0.0.0.0', '10.1'):
            with self.subTest(host=host):
                network = RecordingNetwork([])
                with self.assertRaises((PageFetchError, httpx.InvalidURL)):
                    await self.fetch_with(f'http://{host}/', network, numeric)
                self.assertEqual(network.connections, [])

    async def test_resolution_timeout_and_cancellation_do_not_connect(self):
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        network = RecordingNetwork([])
        backend = PublicNetworkBackend(network, blocked)
        with self.assertRaises(httpcore.ConnectTimeout):
            await backend.connect_tcp('public.example', 443, timeout=0.001)
        task = asyncio.create_task(backend.connect_tcp('public.example', 443))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(network.connections, [])

    async def test_alternate_public_address_is_used_after_connect_failure(self):
        network = AsyncMock(spec=httpcore.AsyncNetworkBackend)
        stream = object()
        network.connect_tcp.side_effect = [httpcore.ConnectError('unavailable'), stream]
        backend = PublicNetworkBackend(network,
            AsyncMock(return_value=addresses('93.184.216.34', '1.1.1.1')))
        self.assertIs(await backend.connect_tcp('public.example', 443), stream)
        self.assertEqual([call.args[0] for call in network.connect_tcp.await_args_list],
                         ['93.184.216.34', '1.1.1.1'])


class PageLinkTests(unittest.TestCase):
    def test_links_are_absolute_http_main_content_first_and_bounded(self):
        body = (
            '<html><head><base href="https://docs.example/v2/"></head><body>'
            '<nav><a href="intro">Intro</a><a href="https://docs.example/v2/api">API (nav)</a></nav>'
            '<main><p>Read the <a href="api#top">API   reference</a> and <a href="#here">base</a>.'
            '<a href="https://docs.example/v2/guide#s">this page</a></p>'
            '<p>' + 'Words. ' * 40 + '<a href="mailto:a@b.example">mail</a>'
            '<a href="javascript:alert(1)">js</a><a href="https://u:p@evil.example/">creds</a>'
            '<a href="ftp://files.example/">ftp</a></p></main>'
            '<footer><a href="//cdn.example/x.js">CDN</a></footer></body></html>')
        page = FetchedPage('https://docs.example/v2/guide', 'text/html', body, datetime.now(UTC))
        links = ContentExtractor().extract(page).links
        self.assertEqual(links, (('https://docs.example/v2/api', 'API reference'),
                                 ('https://docs.example/v2/', 'base'),
                                 ('https://docs.example/v2/intro', 'Intro'),
                                 ('https://cdn.example/x.js', 'CDN')))
        many = '<main><p>' + 'Text. ' * 40 + ''.join(
            f'<a href="/p{i}">{"x" * 200}</a>' for i in range(200)) + '</p></main>'
        links = ContentExtractor().extract(FetchedPage('https://docs.example/', 'text/html', many,
                                                       datetime.now(UTC))).links
        self.assertEqual(len(links), 60)
        self.assertEqual(max(len(text) for _, text in links), 80)

    def test_plain_text_pages_declare_no_links(self):
        page = FetchedPage('https://raw.example/a.txt', 'text/plain', 'See https://other.example/',
                           datetime.now(UTC))
        self.assertEqual(ContentExtractor().extract(page).links, ())


class FetchAddressRefusalTests(unittest.IsolatedAsyncioTestCase):
    """Addresses a model writes are refused before any request leaves, not only by the DNS-bound transport."""

    class RecordingClient:
        def __init__(self):
            self.requests = []

        def stream(self, method, url, **kwargs):
            self.requests.append(url)
            raise AssertionError('a refused address reached the HTTP client')

    async def test_credentials_and_local_names_are_refused_before_any_request(self):
        for url in ('https://user:not-a-secret@example.com/', 'http://localhost:8080/', 'https://intranet.localhost/'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_fetch_url(url)
                client = self.RecordingClient()
                with self.assertRaises(PageFetchError):
                    await PageFetcher(client=client).fetch(url)
                self.assertEqual(client.requests, [])
