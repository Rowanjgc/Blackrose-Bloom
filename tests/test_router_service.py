"""Router lifecycle tests use disposable processes, never the real router."""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from blackrose.backend.router import RouterClient
from blackrose.backend.providers import ChatProviders
from blackrose.backend.service import BROWSER_ORIGIN, RouterService, is_loopback
from blackrose.config import Config


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def argv_of(process) -> list[bytes]:
    """The router's argv once it has been installed.

    The shebang script's first exec still shows the parent's argv, so wait for the
    router's own option to appear rather than reading /proc immediately.
    """
    path = Path(f'/proc/{process.pid}/cmdline')
    deadline = time.monotonic() + 5
    while True:
        arguments = path.read_bytes().split(b'\0')
        if b'--api-key-file' in arguments or time.monotonic() >= deadline:
            return arguments
        time.sleep(0.01)


class Listener:
    """A loopback HTTP/1.1 listener that records each request line."""

    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        self.url = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def serve(self, reader, writer):
        head = (await reader.readuntil(b'\r\n\r\n')).decode('latin-1').split('\r\n')
        self.requests.append(head[0])
        body = b'{"data": []}'
        writer.write(b'HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: close\r\n'
                     b'content-length: %d\r\n\r\n%s' % (len(body), body))
        await writer.drain()
        writer.close()


class RouterProxyEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    """A loopback router is reached directly, so prompts and its key never pass an environment proxy."""

    async def test_loopback_router_clients_ignore_proxy_environment(self):
        with tempfile.TemporaryDirectory() as root:
            async with Listener() as router, Listener() as proxy:
                environment = {'HTTP_PROXY': proxy.url, 'http_proxy': proxy.url, 'ALL_PROXY': proxy.url,
                               'NO_PROXY': '', 'no_proxy': ''}
                with patch.dict(os.environ, environment), \
                        patch('blackrose.backend.service.state_dir', return_value=Path(root)):
                    service = RouterService(Config(endpoint=router.url))
                    client = RouterClient(router.url, timeout=5, auth=service.key)
                try:
                    self.assertTrue(await service._ready())
                    self.assertEqual(await client.models(), [])
                finally:
                    await client.client.aclose()
                    await service._http.aclose()
        self.assertEqual(proxy.requests, [])
        self.assertEqual(router.requests, ['GET /v1/models HTTP/1.1'] * 2)

    def test_a_remote_router_keeps_the_environment(self):
        for endpoint, loopback in (('http://127.0.0.1:18080', True), ('http://localhost:1', True),
                                   ('http://[::1]:1', True), ('http://127.8.0.1:1', True),
                                   ('https://router.example', False), ('http://10.0.0.5:8080', False)):
            with self.subTest(endpoint=endpoint):
                self.assertIs(is_loopback(endpoint), loopback)
                client = RouterClient(endpoint)
                self.assertIs(client.client.trust_env, not loopback)

    async def test_the_loopback_image_service_client_ignores_the_environment(self):
        from blackrose.local_images import LocalImages
        images = LocalImages('http://127.0.0.1:8189', '/nonexistent', backend=None)
        try:
            self.assertFalse(images._client.trust_env)
        finally:
            await images._client.aclose()


class RouterServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        binary = self.root / 'dummy-router'
        binary.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n')
        binary.chmod(0o700)
        preset = self.root / 'presets.ini'
        preset.write_text('[dummy]\n')
        self.service = self.make_service(llama_server_path=str(binary), router_preset=str(preset),
                                         router_stop_timeout=1)

    def make_service(self, **options):
        # A private port per service: /proc adoption matches on port, so a shared port lets
        # concurrent test runs adopt each other's routers.
        options.setdefault('endpoint', f'http://127.0.0.1:{free_port()}')
        with patch('blackrose.backend.service.state_dir', return_value=self.root):
            service = RouterService(Config(**options))
        # Never probe a real listener: the default endpoint may be the user's router.
        self.anonymous_status = 401
        self.requests = []

        def handler(request):
            self.requests.append(request)
            if 'Authorization' not in request.headers:
                return httpx.Response(self.anonymous_status, json={'data': []})
            return httpx.Response(200, json={'data': []})

        service._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return service

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def owned_key(self):
        return self.service.key.path.read_text().strip()

    async def test_launch_names_only_the_private_key_file_and_closes_browser_access(self):
        self.service._launch()
        process = self.service._process
        key = self.owned_key()
        self.assertEqual(self.service.key.path.stat().st_mode & 0o777, 0o600)
        self.assertGreaterEqual(len(key), 40)
        arguments = argv_of(process)
        environment = Path(f'/proc/{process.pid}/environ').read_bytes()
        self.assertNotIn(key.encode(), b'\0'.join(arguments))
        self.assertNotIn(key.encode(), environment)
        command = [argument.decode() for argument in arguments if argument]
        self.assertEqual(command[command.index('--api-key-file') + 1], str(self.service.key.path))
        self.assertEqual(command[command.index('--cors-origins') + 1], BROWSER_ORIGIN)
        for flag in ('--no-slots', '--no-cors-credentials', '--no-webui'):
            self.assertIn(flag, command)
        for flag in ('--api-key', '--slots', '--props', '--metrics', '--cors-credentials'):
            self.assertNotIn(flag, command)
        await self.service.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.service.key.path.exists())

    async def test_each_launch_rotates_the_key_that_requests_present(self):
        self.service._launch()
        first = self.owned_key()
        self.assertTrue(await self.service._ready())
        self.assertEqual(self.requests[-1].headers['Authorization'], f'Bearer {first}')
        self.service._process.kill()
        self.service._process.wait(timeout=3)
        self.service._launch()
        second = self.owned_key()
        self.assertNotEqual(first, second)
        seen = []

        def handler(request):
            seen.append(request.headers.get('Authorization'))
            return httpx.Response(200, json={'data': [{'id': 'local-model'}]})

        client = RouterClient(self.service.endpoint, service=self.service,
                              client=httpx.AsyncClient(base_url=self.service.endpoint,
                                                       transport=httpx.MockTransport(handler)))
        self.service.ensure_running = AsyncMock()
        await client.models()
        await client.client.aclose()
        self.assertEqual(seen, [f'Bearer {second}'])

    async def test_rejected_key_is_actionable_without_revealing_it(self):
        self.service.key.create()
        key = self.owned_key()
        self.service._http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={'error': {'message': 'Invalid API Key'}})))
        with self.assertRaisesRegex(RuntimeError, 'requires an API key.*router_api_key_file') as raised:
            await self.service._ready()
        self.assertNotIn(key, str(raised.exception))
        client = RouterClient('http://127.0.0.1:18080', auth=self.service.key,
                              client=httpx.AsyncClient(base_url='http://127.0.0.1:18080',
                                  transport=httpx.MockTransport(lambda request: httpx.Response(401))))
        with self.assertRaisesRegex(RuntimeError, 'requires an API key') as raised:
            await client.models()
        await client.client.aclose()
        self.assertNotIn(key, str(raised.exception))

    async def test_configured_key_file_must_be_private_and_is_never_removed(self):
        path = self.root / 'shared-router.key'
        path.write_text('# shared with another client\n\nconfigured-secret-key\nsecond-key\n')
        path.chmod(0o644)
        await self.service.close()
        self.service = self.make_service(llama_server_path=str(self.root / 'dummy-router'),
                                         router_preset=str(self.root / 'presets.ini'),
                                         router_stop_timeout=1, router_api_key_file=str(path))
        with self.assertRaisesRegex(RuntimeError, 'chmod 600') as raised:
            self.service._launch()
        self.assertNotIn('configured-secret-key', str(raised.exception))
        self.assertIsNone(self.service._process)
        path.chmod(0o600)
        self.assertEqual(self.service.key.read(), 'configured-secret-key')
        self.service._launch()
        process = self.service._process
        command = [argument.decode() for argument in argv_of(process)]
        self.assertEqual(command[command.index('--api-key-file') + 1], str(path))
        await self.service.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(path.exists())
        self.assertFalse(list(self.root.glob('llama-router-*.key')))
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'router_api_key_file does not exist'):
            self.service.key.read()
        with self.assertRaisesRegex(ValueError, 'router_api_key_file'):
            Config(router_api_key_file=1)

    def running_router(self, host):
        """A disposable process the /proc scan identifies as this endpoint's router."""
        # The scan compares /proc/<pid>/exe, which is the interpreter for a script.
        self.service.binary = Path(os.path.realpath(sys.executable))
        process = subprocess.Popen([str(self.service.binary), '-c', 'import time; time.sleep(30)',
                                    '--models-preset', str(self.service.preset),
                                    '--host', host, '--port', str(self.service.port)])

        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)

        self.addCleanup(stop)
        self.service._ready = AsyncMock(return_value=True)
        return process

    async def test_adopting_an_exposed_unauthenticated_router_warns_once(self):
        process = self.running_router('0.0.0.0')
        self.anonymous_status = 200
        with self.assertLogs('blackrose.backend.service', 'WARNING'):
            await self.service.ensure_running()
        self.assertEqual(self.service._pid, process.pid)
        self.assertNotIn('Authorization', self.requests[-1].headers)
        warnings = self.service.take_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn('accepts requests without an API key', warnings[0])
        self.assertIn('all network interfaces (`0.0.0.0`)', warnings[0])
        self.assertIn('router_autostart = false', warnings[0])
        self.assertIn('A key file alone does not change router ownership', warnings[0])
        await self.service.ensure_running()
        self.assertEqual(self.service.take_warnings(), [])
        await self.service.close()
        self.assertIsNotNone(process.wait(timeout=3))

    async def test_next_desktop_adopts_a_surviving_key_protected_router_silently(self):
        # A crashed desktop leaves its router and key; the next one adopts both.
        self.service.key.create()
        process = self.running_router('127.0.0.1')
        await self.service.ensure_running()
        self.assertEqual(self.service._pid, process.pid)
        self.assertEqual(self.service.take_warnings(), [])
        await self.service.close()
        self.assertIsNotNone(process.wait(timeout=3))
        self.assertFalse(self.service.key.path.exists())

    async def test_launch_waits_until_ready_and_close_reaps_owned_process(self):
        self.service._ready = AsyncMock(side_effect=[httpx.ConnectError('absent'),
                                                    httpx.ConnectError('absent'), True])
        await self.service.ensure_running()
        process = self.service._process
        self.assertIsNone(process.poll())
        await self.service.close()
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.service._handle)

    async def test_adopted_live_process_is_stopped_without_popen_ownership(self):
        self.service._launch()
        process = self.service._process
        self.service._process = None
        self.service._ready = AsyncMock(return_value=True)
        try:
            await self.service.ensure_running()
            await self.service.close()
            process.wait(timeout=3)
            self.assertIsNotNone(process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    async def test_startup_timeout_retains_handle_for_shutdown(self):
        self.service.start_timeout = 0.02
        self.service._ready = AsyncMock(side_effect=[httpx.ConnectError('absent'),
                                                    httpx.ConnectError('absent'), False])
        with self.assertRaisesRegex(TimeoutError, 'Timed out starting'):
            await self.service.ensure_running()
        process = self.service._process
        self.assertIsNotNone(process)
        await self.service.close()
        self.assertIsNotNone(process.poll())

    async def test_relaunch_drains_old_output_before_opening_the_next_writer(self):
        self.service.binary.write_text(
            '#!/usr/bin/env python3\nimport os\nos.write(2, b"router recent error\\n")\n')
        self.service._launch()
        self.service._process.wait(timeout=3)
        previous = self.service._output
        self.service._launch()
        self.assertFalse(previous._thread.is_alive())
        self.service._process.wait(timeout=3)
        await self.service.close()
        self.assertEqual(self.service.log_path.read_text().count('router recent error'), 2)


class RouterWarningNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_turn_switch_and_explicit_refresh_catalog_request_counts(self):
        from test_gui_images import make_runtime
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': model, 'status': {'value': 'loaded'},
                     'architecture': {'input_modalities': ['text']}}
                    for model in ('test-vision', 'other-model')]})
            if request.url.path == '/props':
                return httpx.Response(200, json={})
            if request.url.path == '/v1/chat/completions':
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Answer."},'
                    '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            self.fail(f'Unexpected request: {request.url}')

        with tempfile.TemporaryDirectory() as directory:
            _, runtime = make_runtime(Path(directory))
            router = RouterClient('http://router.test', client=httpx.AsyncClient(
                base_url='http://router.test', transport=httpx.MockTransport(handler)))
            runtime.controller.backend = ChatProviders(router)
            await runtime.open()
            try:
                controller = runtime.controller
                await controller.start()
                self.assertEqual(calls.count('/v1/models'), 1)
                for question in ('First question', 'Follow up'):
                    calls.clear()
                    await controller.send(question)
                    self.assertEqual(calls.count('/v1/models'), 1)
                    self.assertEqual(controller.catalog[0]['status']['value'], 'loaded')
                calls.clear()
                await controller.select_model('other-model')
                # Selection deliberately refreshes the picker after loading.
                self.assertEqual(calls.count('/v1/models'), 2)
                await controller.refresh_models('local')
                self.assertEqual(calls.count('/v1/models'), 3)
            finally:
                await runtime.close()

    async def test_adoption_warning_is_shown_once_as_a_session_notice(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_gui_images import make_runtime
        from blackrose.gui.session import Session

        with tempfile.TemporaryDirectory() as directory:
            _, runtime = make_runtime(Path(directory))
            await runtime.open()
            pending = ['The llama-router already running at http://127.0.0.1:18080 '
                       'accepts requests without an API key.']
            runtime.controller.backend.take_warnings = lambda: [pending.pop()] if pending else []
            events = []
            session = Session(runtime.controller, lambda kind, payload: events.append((kind, payload)))
            try:
                await session.boot()
                await session.models('local')
            finally:
                await session.close()
                await runtime.close()
        notices = [payload for kind, payload in events
                   if kind == 'notice' and payload['tone'] == 'warning']
        self.assertEqual(len(notices), 1)
        self.assertIn('without an API key', notices[0]['html'])
