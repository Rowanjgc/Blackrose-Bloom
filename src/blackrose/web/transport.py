"""Public-only TCP connections, with DNS validation bound to the dialed IP.

The HTTP origin is never rewritten: httpcore retains the requested hostname for
Host, TLS certificate validation and SNI, while this backend dials numeric IPs.
"""
from __future__ import annotations

import asyncio
import socket
import ssl

import httpcore
import httpx

from .urls import is_public_address


class UnsafeDestination(ValueError):
    """A fetch would connect to a non-public destination."""


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend=None, resolver=None):
        self._backend = backend or httpcore.AnyIOBackend()
        self._resolver = resolver

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore backend interface
        try:
            async with asyncio.timeout(timeout):
                resolver = self._resolver or asyncio.get_running_loop().getaddrinfo
                records = await resolver(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
                addresses = list(dict.fromkeys(record[4][0] for record in records))
                if not addresses:
                    raise httpcore.ConnectError('The public hostname did not resolve')
                # Refuse mixed public/private answers too. A retry must never
                # fall through to a private address from the same DNS response.
                if not all(is_public_address(address) for address in addresses):
                    raise UnsafeDestination('private or local destinations are not fetched')
                last_error = None
                for address in addresses:
                    try:
                        return await self._backend.connect_tcp(address, port, timeout=timeout,
                            local_address=local_address, socket_options=socket_options)
                    except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                        last_error = error
                raise last_error
        except TimeoutError:
            raise httpcore.ConnectTimeout('Public connection timed out') from None
        except OSError:
            raise httpcore.ConnectError('The public hostname could not be resolved') from None

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # noqa: ASYNC109 - httpcore backend interface
        raise UnsafeDestination('local sockets are not fetched')

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


class PublicTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *, network_backend=None):
        # HTTPX exposes transport replacement; httpcore exposes network backend
        # replacement. Keep their single pool integration point in this class.
        # No proxy pool or environment-derived TLS settings are installed.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=network_backend or PublicNetworkBackend(), retries=0)
