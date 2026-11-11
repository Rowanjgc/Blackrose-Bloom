"""Local endpoints need llama-router's model lifecycle management API."""
import tempfile
import json
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from blackrose.backend.router import RouterClient
from blackrose.config import Config
from blackrose.runtime import build_runtime


class RouterManagementContractTests(unittest.IsolatedAsyncioTestCase):
    async def client_for(self, handler):
        client = httpx.AsyncClient(base_url='http://router.test',
                                   transport=httpx.MockTransport(handler))
        router = RouterClient('http://router.test', client=client)
        self.addAsyncCleanup(router.aclose)
        return router

    async def test_openai_catalog_without_status_gets_actionable_router_diagnostic(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={'data': [{'id': 'plain-model'}]})

        router = await self.client_for(handler)
        with self.assertRaisesRegex(RuntimeError, 'endpoint is reachable.*llama-router') as raised:
            await router.load('plain-model')
        self.assertIn('/models/load', str(raised.exception))
        self.assertIn('plain OpenAI-compatible server is not supported', str(raised.exception))
        self.assertEqual(calls, ['/v1/models'])

    async def test_supported_loaded_catalog_still_loads_without_management_post(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': 'loaded'}}]})
            if request.url.path == '/props':
                return httpx.Response(200, json={'chat_template': 'template'})
            return httpx.Response(404)

        router = await self.client_for(handler)
        rows = await router.load('router-model')
        self.assertEqual(rows[0]['status']['value'], 'loaded')
        self.assertEqual(calls, ['/v1/models', '/props'])

    async def test_missing_management_route_is_reported_as_incompatible_endpoint(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': 'unloaded'}}]})
            return httpx.Response(404, json={'error': 'not found'})

        router = await self.client_for(handler)
        with self.assertRaisesRegex(RuntimeError, 'model management contract'):
            await router.load('router-model')
        self.assertEqual(calls, ['/v1/models', '/models/load'])

    async def test_sleeping_model_stays_asleep_until_inference_and_can_sleep_again(self):
        calls = []
        state = 'sleeping'

        def handler(request):
            nonlocal state
            calls.append((request.method, request.url.path))
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': state}}]})
            if request.url.path == '/props':
                return httpx.Response(200, json={'is_sleeping': state == 'sleeping',
                                                'chat_template': '{{ enable_thinking }}'})
            if request.url.path == '/v1/chat/completions':
                self.assertEqual(json.loads(request.content)['model'], 'router-model')
                state = 'loaded'
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"awake"},'
                                      '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            self.fail(f'Unexpected request: {request.method} {request.url}')

        router = await self.client_for(handler)
        await router.load('router-model')
        self.assertEqual(state, 'sleeping')
        self.assertTrue(router.capabilities['router-model']['thinking'])
        for _ in range(2):
            chunks = [chunk async for chunk in router.stream('router-model', [])]
            self.assertEqual(chunks[0]['delta']['content'], 'awake')
            self.assertEqual(state, 'loaded')
            state = 'sleeping'
            await router.load('router-model')
        await router.aclose()
        self.assertEqual(state, 'sleeping')
        self.assertNotIn(('POST', '/models/load'), calls)
        self.assertNotIn(('POST', '/models/unload'), calls)
        self.assertEqual(calls.count(('GET', '/v1/models')), 3)

    async def test_model_can_fall_asleep_between_catalog_reads(self):
        states = iter(['loaded', 'sleeping'])

        def handler(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': next(states)}}]})
            if request.url.path == '/props':
                return httpx.Response(200, json={'is_sleeping': True})
            self.fail(f'Unexpected request: {request.method} {request.url}')

        router = await self.client_for(handler)
        self.assertEqual((await router.load('router-model'))[0]['status']['value'], 'loaded')
        self.assertEqual((await router.load('router-model'))[0]['status']['value'], 'sleeping')

    async def test_model_loading_elsewhere_is_waited_for_without_duplicate_load(self):
        states = iter(['loading', 'loading', 'loaded'])
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': next(states)}}]})
            if request.url.path == '/props':
                return httpx.Response(200, json={})
            self.fail(f'Unexpected request: {request.method} {request.url}')

        router = await self.client_for(handler)
        rows = await router.load('router-model')
        self.assertEqual(rows[0]['status']['value'], 'loaded')
        self.assertEqual(calls, ['/v1/models'] * 3 + ['/props'])

    async def test_successful_load_polls_until_ready_and_returns_final_catalog(self):
        states = iter(['unloaded', 'loading', 'loaded'])
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': next(states)}}]})
            if request.url.path == '/models/load':
                return httpx.Response(200, json={'success': True})
            if request.url.path == '/props':
                return httpx.Response(200, json={})
            self.fail(f'Unexpected request: {request.url}')

        router = await self.client_for(handler)
        rows = await router.load('router-model')
        self.assertEqual(rows[0]['status']['value'], 'loaded')
        self.assertEqual(calls, ['/v1/models', '/models/load', '/v1/models', '/v1/models', '/props'])

    async def test_busy_shared_router_retries_load_without_unloading_other_model(self):
        posts = []
        calls = []
        ready = False

        def handler(request):
            nonlocal ready
            calls.append(request.url.path)
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': 'loaded' if ready else 'unloaded'}},
                    {'id': 'other-client-model', 'status': {'value': 'loaded'}}]})
            if request.url.path == '/models/load':
                posts.append(json.loads(request.content))
                if len(posts) == 1:
                    return httpx.Response(500, json={'error': {
                        'message': 'model limit reached, try again later'}})
                ready = True
                return httpx.Response(200, json={'success': True})
            if request.url.path == '/props':
                return httpx.Response(200, json={})
            self.fail(f'Unexpected request: {request.method} {request.url}')

        router = await self.client_for(handler)
        rows = await router.load('router-model')
        self.assertEqual(posts, [{'model': 'router-model'}] * 2)
        self.assertEqual(rows[0]['status']['value'], 'loaded')
        self.assertEqual(calls.count('/v1/models'), 3)

    async def test_busy_router_wait_is_bounded_and_cancellable(self):
        reached = asyncio.Event()

        def handler(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': 'unloaded'}}]})
            self.assertEqual(request.url.path, '/models/load')
            reached.set()
            return httpx.Response(500, json={'error': {
                'message': 'model limit reached, try again later'}})

        router = await self.client_for(handler)
        router.timeout = 0.01
        with self.assertRaisesRegex(TimeoutError, 'Timed out waiting'):
            await router.load('router-model')
        router.timeout = 600
        reached.clear()
        task = asyncio.create_task(router.load('router-model'))
        await reached.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_unrelated_router_error_is_not_retried(self):
        posts = []

        def handler(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': 'unloaded'}}]})
            posts.append(request.url.path)
            return httpx.Response(500, json={'error': {'message': 'model initialization failed'}})

        router = await self.client_for(handler)
        with self.assertRaises(httpx.HTTPStatusError):
            await router.load('router-model')
        self.assertEqual(posts, ['/models/load'])

    async def test_another_client_wins_the_load_race(self):
        ready = False
        posts = []
        calls = []

        def handler(request):
            nonlocal ready
            calls.append(request.url.path)
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [
                    {'id': 'router-model', 'status': {'value': 'sleeping' if ready else 'unloaded'}}]})
            if request.url.path == '/props':
                return httpx.Response(200, json={'is_sleeping': True})
            self.assertEqual(request.url.path, '/models/load')
            posts.append(request.url.path)
            ready = True
            return httpx.Response(400, json={'error': {'message': 'model is already running'}})

        router = await self.client_for(handler)
        rows = await router.load('router-model')
        self.assertEqual(posts, ['/models/load'])
        self.assertEqual(rows[0]['status']['value'], 'sleeping')
        self.assertEqual(calls.count('/v1/models'), 2)

    async def test_connection_refusal_remains_distinct_from_incompatible_endpoint(self):
        def handler(request):
            raise httpx.ConnectError('connection refused', request=request)

        router = await self.client_for(handler)
        with self.assertRaises(httpx.ConnectError):
            await router.models()

    async def test_malformed_catalog_has_a_specific_catalog_error(self):
        def handler(request):
            return httpx.Response(200, json={'data': [{'name': 'no-id'}]})

        router = await self.client_for(handler)
        with self.assertRaisesRegex(RuntimeError, 'invalid model catalog'):
            await router.models()

    async def test_autostart_disabled_does_not_start_a_service_for_plain_catalog(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={'data': [{'id': 'plain-model'}]})

        with tempfile.TemporaryDirectory() as temporary, \
                patch('blackrose.runtime.data_dir', return_value=Path(temporary)), \
                patch('blackrose.backend.service.RouterService') as service_type:
            runtime = build_runtime(Config(endpoint='http://router.test', router_autostart=False,
                                           image_generation_enabled=False))
            self.addAsyncCleanup(runtime.close)
            router = runtime.controller.backend.local
            router.client = httpx.AsyncClient(base_url='http://router.test',
                                              transport=httpx.MockTransport(handler))
        # Runtime passes service=None when router_autostart is disabled.
        self.assertIsNone(router.service)
        service_type.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'llama-router'):
            await router.load('plain-model')
        self.assertEqual(calls, ['/v1/models'])

