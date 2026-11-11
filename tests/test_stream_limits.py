"""Stream guards bound wire buffering, output growth, and total generation time."""
import asyncio
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

import httpx

from blackrose.backend.stream_limits import (
    StreamBounds, StreamBudget, StreamLimitExceeded, sse_lines,
)
from blackrose.backend.router import RouterClient
from blackrose.config import Config
from blackrose.turns import TurnRunner
import test_gui_images as fixtures


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        pass


class OpenStream(httpx.AsyncByteStream):
    def __init__(self):
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield b'data: first\r'
        await self.release.wait()
        yield b'\n\ndata: second\n\n'

    async def aclose(self):
        pass


class RouterErrorBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_endless_http_error_keeps_a_bounded_prefix_and_closes(self):
        class ErrorStream(httpx.AsyncByteStream):
            reads = 0
            closed = False

            async def __aiter__(self):
                while True:
                    self.reads += 1
                    await asyncio.sleep(0.001)
                    yield b'x' * 1024

            async def aclose(self):
                self.closed = True

        source = ErrorStream()
        transport = httpx.MockTransport(lambda _: httpx.Response(500, stream=source))
        router = RouterClient('http://router.test', client=httpx.AsyncClient(
            base_url='http://router.test', transport=transport))
        self.addAsyncCleanup(router.aclose)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 500') as caught:
            await asyncio.wait_for(anext(router.stream('fixture', [])), 1)
        self.assertEqual(source.reads, 8)
        self.assertTrue(source.closed)
        self.assertLess(len(str(caught.exception)), 1100)

    async def test_short_json_http_error_preserves_its_diagnostic(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(
            400, json={'error': {'message': 'Prompt exceeds the context limit'}}))
        router = RouterClient('http://router.test', client=httpx.AsyncClient(
            base_url='http://router.test', transport=transport))
        self.addAsyncCleanup(router.aclose)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 400.*Prompt exceeds the context limit'):
            await anext(router.stream('fixture', []))


class SseLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_stream_bounds_are_configurable_and_validated(self):
        config = Config(stream_reply_bytes=2 * 1024 * 1024,
                        stream_reasoning_bytes=3 * 1024 * 1024,
                        stream_response_bytes=8 * 1024 * 1024,
                        stream_sse_line_bytes=256 * 1024,
                        stream_duration_seconds=3600)
        self.assertEqual(config.stream_duration_seconds, 3600)
        with self.assertRaisesRegex(ValueError, 'cannot exceed'):
            Config(stream_response_bytes=1024, stream_sse_line_bytes=2048)
        with self.assertRaisesRegex(ValueError, 'stream_duration_seconds'):
            Config(stream_duration_seconds=0)

    async def test_first_short_event_is_delivered_while_upstream_remains_open(self):
        stream = OpenStream()
        response = httpx.Response(200, stream=stream)
        lines = sse_lines(response, StreamBudget())
        self.assertEqual(await asyncio.wait_for(anext(lines), 0.2), 'data: first')
        blocked = asyncio.create_task(anext(lines))
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())
        stream.release.set()
        self.assertEqual(await blocked, '')
        self.assertEqual(await anext(lines), 'data: second')
        self.assertEqual(await anext(lines), '')

    async def test_oversized_sse_line_is_rejected_before_line_buffer_growth(self):
        response = httpx.Response(200, stream=BytesStream([b'data: ' + b'x' * 4096]))
        budget = StreamBudget(StreamBounds(sse_line_bytes=128, response_bytes=8192))
        with self.assertRaisesRegex(StreamLimitExceeded, 'SSE line limit'):
            await anext(sse_lines(response, budget))

    async def test_large_fast_chunk_yields_cooperatively_and_parses_crlf_and_cr(self):
        response = httpx.Response(200, stream=BytesStream([b':ping\r\n' * 4096 + b':last\r']))
        lines = sse_lines(response, StreamBudget(StreamBounds(response_bytes=65536)))
        running = True
        ticks = 0
        max_delay = 0.0
        loop = asyncio.get_running_loop()
        previous = loop.time()

        async def heartbeat():
            nonlocal ticks, max_delay, previous
            while running:
                current = loop.time()
                max_delay = max(max_delay, current - previous)
                previous = current
                ticks += 1
                await asyncio.sleep(0)

        pulse = asyncio.create_task(heartbeat())
        parsed = [line async for line in lines]
        running = False
        await pulse
        self.assertEqual(len(parsed), 4097)
        self.assertGreater(ticks, 1)
        self.assertLess(max_delay, 0.2)

    async def test_ui_repair_substream_uses_the_same_turn_byte_budget(self):
        async def repair(model, messages, tools):
            yield {'delta': {'content': 'repair output'}}

        chat = SimpleNamespace(
            _repo=None,
            config=SimpleNamespace(interactive_ui_repairs=1),
            backend=SimpleNamespace(stream=repair),
            model_name='model',
        )
        budget = StreamBudget(StreamBounds(reply_bytes=1024))
        budget.add_output('reply', 'x' * 1018)
        ui_stream = SimpleNamespace(blocks=[{
            'kind': 'interactive_ui', 'status': 'invalid', 'source': '{"type":"comparison"}',
            'error': 'invalid document',
        }])

        class Builder:
            def admit(self, *_args):
                return None

        with self.assertRaisesRegex(StreamLimitExceeded, 'reply limit'):
            await TurnRunner(chat)._repair_ui(
                ui_stream, SimpleNamespace(id=1), 'comparison', {}, Builder(), budget)

    async def test_ui_repair_failure_logs_only_operation_and_class(self):
        async def failing_repair(model, messages, tools):
            raise RuntimeError('provider secret prompt text')
            yield  # pragma: no cover

        chat = SimpleNamespace(
            _repo=None,
            config=SimpleNamespace(interactive_ui_repairs=1),
            backend=SimpleNamespace(stream=failing_repair),
            model_name='model',
        )
        ui_stream = SimpleNamespace(blocks=[{
            'kind': 'interactive_ui', 'status': 'invalid', 'source': '{"type":"comparison"}',
            'error': 'invalid document',
        }])

        class Builder:
            def admit(self, *_args):
                return None

        with self.assertLogs('blackrose.turns', level='DEBUG') as captured:
            await TurnRunner(chat)._repair_ui(
                ui_stream, SimpleNamespace(id=1), 'comparison', {}, Builder(), StreamBudget())
        messages = '\n'.join(captured.output)
        self.assertIn('UI repair request failed (RuntimeError)', messages)
        self.assertNotIn('provider secret prompt text', messages)


class StreamTurnTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    def install(self, generator):
        controller = self.runtime.controller
        backend = fixtures.Backend()
        backend.load = AsyncMock()
        backend.stream = generator
        controller.backend = backend
        return controller

    async def test_fast_endless_output_stops_at_limit_and_persists_partial_text(self):
        started = asyncio.Event()

        async def endless(model, messages, tools):
            started.set()
            while True:
                yield {'delta': {'content': 'x'}}

        controller = self.install(endless)
        controller.config.stream_reply_bytes = 4096
        task = asyncio.create_task(controller.send('Keep going'))
        await started.wait()
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0)
        with self.assertRaisesRegex(StreamLimitExceeded, 'reply limit'):
            await task
        reply = (await controller.current_messages())[-1]
        self.assertEqual(reply.content, 'x' * 4096)
        self.assertEqual(reply.state.value, 'error')
        self.assertGreater(ticks, 1)

    async def test_reasoning_only_stream_has_its_own_byte_limit(self):
        async def reasoning(model, messages, tools):
            yield {'delta': {'reasoning_content': 'r' * 1024}}
            yield {'delta': {'reasoning_content': 's'}}

        controller = self.install(reasoning)
        controller.config.stream_reasoning_bytes = 1024
        with self.assertRaisesRegex(StreamLimitExceeded, 'reasoning limit'):
            await controller.send('Think silently')
        reply = (await controller.current_messages())[-1]
        self.assertEqual(reply.content, '')
        self.assertEqual(reply.state.value, 'error')

    async def test_slow_trickle_hits_total_deadline_despite_activity(self):
        async def trickle(model, messages, tools):
            while True:
                yield {'delta': {'content': 'a'}}
                await asyncio.sleep(0.2)

        controller = self.install(trickle)
        controller.config.stream_duration_seconds = 1
        with self.assertRaisesRegex(StreamLimitExceeded, 'stream deadline'):
            await controller.send('Trickle forever')
        reply = (await controller.current_messages())[-1]
        self.assertGreater(len(reply.content), 0)
        self.assertEqual(reply.state.value, 'error')

    async def test_stop_closes_stream_and_persists_accepted_prefix(self):
        started, closed, accepted = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def waiting(model, messages, tools):
            try:
                started.set()
                yield {'delta': {'content': 'prefix'}}
                await asyncio.Event().wait()
            finally:
                closed.set()

        controller = self.install(waiting)
        task = asyncio.create_task(controller.send('Stop this', on_delta=lambda _: accepted.set()))
        await started.wait()
        await accepted.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        reply = (await controller.current_messages())[-1]
        self.assertEqual(reply.content, 'prefix')
        self.assertEqual(reply.state.value, 'cancelled')

    async def test_tool_argument_budget_still_applies_inside_global_reply_limit(self):
        async def large_arguments(model, messages, tools):
            yield {'delta': {'content': 'partial'}}
            yield {'delta': {'tool_calls': [{'index': 0, 'id': 'call-1', 'function': {
                'name': 'fetch_content', 'arguments': 'x' * 256001}}]},
                'finish_reason': 'tool_calls'}

        controller = self.install(large_arguments)
        with self.assertRaisesRegex(RuntimeError, 'tool argument budget'):
            await controller.send('Call a tool')
        reply = (await controller.current_messages())[-1]
        self.assertEqual(reply.content, 'partial')
        self.assertEqual(reply.state.value, 'error')


