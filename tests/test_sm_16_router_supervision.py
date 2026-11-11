"""SM-16: local router supervision, model residency and desktop ownership.

Offline: disposable sleeping processes stand in for the router, an
``httpx.MockTransport`` stands in for its HTTP surface, and every state file
lives in a temporary directory.
"""
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from blackrose.backend.effort import DEFAULT_ONLY, LEVELS
from blackrose.backend.router import RouterClient
from blackrose.backend.service import RouterService
from blackrose.config import Config
from blackrose.instance import desktop_instance


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


# The /proc scan compares /proc/<pid>/exe, which is the interpreter for a script.
ROUTER_BINARY = Path(os.path.realpath(sys.executable))


class RouterServiceTests(unittest.IsolatedAsyncioTestCase):
    """The supervision state: unattached, adopted, owned, exited, stopping."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        binary = self.root / 'dummy-router'
        binary.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n')
        binary.chmod(0o700)
        preset = self.root / 'presets.ini'
        preset.write_text('[dummy]\n')
        self.router_binary = ROUTER_BINARY
        self.preset = preset
        self.service = self.make_service(llama_server_path=str(binary), router_preset=str(preset))

    def make_service(self, **options):
        # A private port per test: /proc adoption matches on port.
        options.setdefault('endpoint', f'http://127.0.0.1:{free_port()}')
        options.setdefault('llama_server_path', str(self.root / 'dummy-router'))
        options.setdefault('router_preset', str(self.preset))
        options.setdefault('router_stop_timeout', 1)
        with patch('blackrose.backend.service.state_dir', return_value=self.root):
            service = RouterService(Config(**options))
        # The adoption warning probe deliberately sends no key; answer it as a
        # key-protected loopback router so only the tests that want it warn.
        service._http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200 if 'Authorization' in request.headers else 401, json={'data': []})))
        return service

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def spawn(self, *arguments):
        """A disposable process the /proc scan can identify as this endpoint's router."""
        process = subprocess.Popen([str(self.router_binary), '-c', 'import time; time.sleep(30)',
                                    *arguments])

        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)

        self.addCleanup(stop)
        return process

    def running_router(self, host, port=None):
        self.service.binary = self.router_binary
        return self.spawn('--models-preset', str(self.preset), '--host', host,
                          '--port', str(self.service.port if port is None else port))

    async def test_a_spawn_that_cannot_be_supervised_is_stopped_and_its_key_removed(self):
        # os.pidfd_open failing after Popen (fd exhaustion) used to leave a
        # detached router holding the port and this launch's key, and close()
        # returned early because there was no handle.
        pids = []

        def refuse(pid, flags=0):
            pids.append(pid)
            raise OSError('too many open files')

        with patch('blackrose.backend.service.os.pidfd_open', refuse):
            with self.assertRaises(OSError):
                self.service._launch()
        self.assertEqual(len(pids), 1)
        self.assertIsNone(self.service._process)
        self.assertIsNone(self.service._pid)
        self.assertIsNone(self.service._handle)
        self.assertFalse(self.service.key.path.exists(), 'the launch key must not outlive the launch')
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)
        await self.service.close()
        self.assertFalse(self.service.key.path.exists())

    async def test_a_failed_launch_never_removes_a_configured_key_file(self):
        path = self.root / 'shared-router.key'
        path.write_text('configured-secret-key\n')
        path.chmod(0o600)
        await self.service.close()
        self.service = self.make_service(router_api_key_file=str(path))

        def refuse(pid, flags=0):
            raise OSError('too many open files')

        with patch('blackrose.backend.service.os.pidfd_open', refuse):
            with self.assertRaises(OSError):
                self.service._launch()
        self.assertEqual(path.read_text(), 'configured-secret-key\n')
        self.assertFalse(list(self.root.glob('llama-router-*.key')))

    async def test_close_reaps_the_owned_router_and_is_idempotent(self):
        self.service._launch()
        process = self.service._process
        self.assertTrue(self.service.key.path.exists())
        await self.service.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.service.key.path.exists())
        for name in ('_pid', '_handle', '_process', '_output'):
            self.assertIsNone(getattr(self.service, name), name)
        await self.service.close()
        self.assertIsNone(self.service._handle)
        self.assertFalse(self.service.key.path.exists())

    async def test_a_router_that_is_still_warming_up_is_waited_for_not_duplicated(self):
        # /v1/models answers 503 while models warm: _ready() is False, which must
        # not reach _launch().
        process = self.running_router('127.0.0.1')
        self.service._ready = AsyncMock(side_effect=[False, False, True])
        with patch.object(self.service, '_launch',
                          side_effect=AssertionError('started a second router')):
            await self.service.ensure_running()
        self.assertEqual(self.service._pid, process.pid)
        self.assertIsNone(self.service._process)

    async def test_adoption_refuses_a_process_serving_another_port(self):
        process = self.running_router('127.0.0.1', port=free_port())
        self.service._ready = AsyncMock(return_value=True)
        with self.assertRaisesRegex(RuntimeError, 'Cannot identify the local llama-router'):
            await self.service.ensure_running()
        self.assertIsNone(self.service._pid)
        self.assertIsNone(self.service._handle)
        self.assertIsNone(process.poll(), 'an unidentified process must never be signalled')

    async def test_adoption_requires_separate_port_and_host_arguments(self):
        process = self.spawn('--models-preset', str(self.preset), '--host', '127.0.0.1',
                             f'--port={self.service.port}')
        self.service.binary = self.router_binary
        self.service._ready = AsyncMock(return_value=True)
        with self.assertRaisesRegex(RuntimeError, 'Cannot identify the local llama-router'):
            await self.service.ensure_running()
        self.assertIsNone(self.service._pid)
        self.assertIsNone(process.poll())

    async def test_an_exited_router_is_relaunched_with_a_fresh_key(self):
        self.service._launch()
        first_pid = self.service._pid
        first_key = self.service.key.path.read_text()
        self.service._process.kill()
        self.service._process.wait(timeout=3)
        self.service._ready = AsyncMock(side_effect=[
            httpx.ConnectError('absent'), httpx.ConnectError('absent'), True])
        await self.service.ensure_running()
        self.assertNotEqual(self.service._pid, first_pid)
        self.assertIsNotNone(self.service._handle)
        with self.assertRaises(ProcessLookupError):
            os.kill(first_pid, 0)
        self.assertNotEqual(self.service.key.path.read_text(), first_key)

    async def test_the_endpoint_lock_is_not_a_lifetime_ownership_lock(self):
        # Characterises the documented hazard: only desktop.lock scopes router
        # ownership, so a second service on the same endpoint adopts the same
        # pid and closing either one stops the router the other still holds.
        process = self.running_router('127.0.0.1')
        self.service._ready = AsyncMock(return_value=True)
        await self.service.ensure_running()
        second = self.make_service(endpoint=self.service.endpoint)
        second.binary = self.router_binary
        self.addAsyncCleanup(second.close)
        second._ready = AsyncMock(return_value=True)
        await second.ensure_running()
        self.assertEqual(second._pid, process.pid)
        self.assertIsNone(second._process)
        await self.service.close()
        self.assertEqual(process.wait(timeout=3), -signal.SIGTERM)
        self.assertTrue(select.select([second._handle], [], [], 0)[0])
        await second.close()
        self.assertIsNone(second._handle)


class ModelResidencyTests(unittest.IsolatedAsyncioTestCase):
    """RouterClient.load: bounded polling and the per-model capability cache."""

    async def client_for(self, handler):
        client = httpx.AsyncClient(base_url='http://router.test',
                                   transport=httpx.MockTransport(handler))
        router = RouterClient('http://router.test', client=client)
        self.addAsyncCleanup(router.aclose)
        return router

    async def test_a_model_that_falls_back_to_unloaded_is_waited_for(self):
        states = iter(['unloaded', 'loading', 'unloaded', 'loaded'])
        calls = []

        def handler(request):
            if request.url.path == '/v1/models':
                calls.append((request.url.path, next(states)))
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': calls[-1][1]}}]})
            if request.url.path == '/models/load':
                calls.append((request.url.path, None))
                return httpx.Response(200, json={'success': True})
            self.assertEqual(request.url.path, '/props')
            return httpx.Response(200, json={'chat_template': '{{ enable_thinking }}'})

        router = await self.client_for(handler)
        rows = await router.load('router-model')
        self.assertEqual(rows[0]['status']['value'], 'loaded')
        self.assertEqual([status for _, status in calls],
                         ['unloaded', None, 'loading', 'unloaded', 'loaded'])
        self.assertTrue(router.capabilities['router-model']['thinking'])

    async def test_a_model_that_never_becomes_ready_is_bounded(self):
        def handler(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': 'unloaded'}}]})
            self.assertEqual(request.url.path, '/models/load')
            return httpx.Response(200, json={'success': True})

        router = await self.client_for(handler)
        router.timeout = 0.05
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'Timed out waiting'):
            await router.load('router-model')
        self.assertLess(time.monotonic() - started, 2)
        self.assertNotIn('router-model', router.capabilities)

    async def test_capabilities_are_dropped_and_reread_before_a_reload(self):
        state = 'loaded'
        inspected = []

        def handler(request):
            nonlocal state
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': state}}]})
            if request.url.path == '/props':
                inspected.append(state)
                return httpx.Response(200, json={'chat_template': '{{ enable_thinking }}'})
            if request.url.path == '/models/load':
                state = 'loaded'
                return httpx.Response(200, json={'success': True})
            self.assertEqual(request.url.path, '/models/unload')
            state = 'unloaded'
            return httpx.Response(200, json={'success': True})

        router = await self.client_for(handler)
        await router.load('router-model')
        self.assertTrue(router.capabilities['router-model']['thinking'])
        await router.unload('router-model')
        self.assertEqual(state, 'unloaded')
        await router.load('router-model')
        self.assertEqual(inspected, ['loaded', 'loaded'])
        self.assertTrue(router.capabilities['router-model']['thinking'])

    async def test_a_failed_property_read_is_not_cached_as_missing_support(self):
        reads = []

        def handler(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': 'loaded'}}]})
            self.assertEqual(request.url.path, '/props')
            reads.append(request.url.params['model'])
            if len(reads) == 1:
                return httpx.Response(500, json={'error': 'temporarily unavailable'})
            return httpx.Response(200, json={'chat_template': '{{ enable_thinking }}'})

        router = await self.client_for(handler)
        await router.load('router-model')
        self.assertNotIn('router-model', router.capabilities)
        await router.load('router-model')
        self.assertEqual(len(reads), 2)
        self.assertTrue(router.capabilities['router-model']['thinking'])
        self.assertEqual(router.reasoning_levels('router-model'), LEVELS)

    async def test_capabilities_are_per_model_across_a_switch(self):
        def handler(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': model, 'status': {'value': 'loaded'}}
                    for model in ('thinking-model', 'plain-model')]})
            self.assertEqual(request.url.path, '/props')
            template = ('{{ enable_thinking }}'
                        if request.url.params['model'] == 'thinking-model' else 'plain')
            return httpx.Response(200, json={'chat_template': template})

        router = await self.client_for(handler)
        await router.load('thinking-model')
        await router.load('plain-model')
        self.assertEqual(set(router.capabilities), {'thinking-model', 'plain-model'})
        self.assertEqual(router.reasoning_levels('thinking-model'), LEVELS)
        self.assertEqual(router.reasoning_levels('plain-model'), DEFAULT_ONLY)
        self.assertEqual(router.reasoning_levels('unseen-model'), DEFAULT_ONLY)


class DesktopOwnershipTests(unittest.TestCase):
    """desktop.lock: one owner per database directory, valid only in the context."""

    def test_ownership_is_valid_only_while_the_context_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'desktop.lock'
            with desktop_instance(path) as ownership:
                ownership.require(Path(root) / 'chats.db')
                self.assertTrue(ownership.active)
            self.assertFalse(ownership.active)
            self.assertTrue(ownership.handle.closed)
            with self.assertRaisesRegex(RuntimeError, 'ownership'):
                ownership.require(Path(root) / 'chats.db')

    def test_require_accepts_only_the_lock_beside_this_database(self):
        with tempfile.TemporaryDirectory() as root:
            with desktop_instance(Path(root) / 'desktop.lock') as ownership:
                ownership.require(Path(root) / 'chats.db')
                for database in (':memory:', str(Path(root) / 'other' / 'chats.db'),
                                 str(Path(root).parent / 'elsewhere' / 'chats.db')):
                    with self.assertRaisesRegex(RuntimeError, 'ownership'):
                        ownership.require(database)

    def test_locks_in_different_database_directories_are_independent(self):
        with tempfile.TemporaryDirectory() as root:
            with desktop_instance(Path(root) / 'a' / 'desktop.lock') as first, \
                    desktop_instance(Path(root) / 'b' / 'desktop.lock') as second:
                self.assertTrue(first.active)
                self.assertTrue(second.active)
                self.assertNotEqual(first.path, second.path)


if __name__ == '__main__':
    unittest.main()
