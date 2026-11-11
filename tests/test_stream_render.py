"""Streaming repaints run off the application loop without publishing stale HTML."""
from __future__ import annotations

import asyncio
import contextlib
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from blackrose.gui import render
from blackrose.gui.render import StreamRenderer, audited_html, to_html
from blackrose.gui.session import Session
from test_gui_images import make_runtime

CODE_LINE = 'value = call(123, name="example")\n'
SAMPLES = {
    'code': 'Intro\n\n```python\n' + CODE_LINE * 40 + '```\n\nAfter.',
    'math': 'Inline $x^2 + y_1$ and \\(\\frac{a}{b}\\), display \\[\\sum_{i=1}^n i\\]\n\n$$E = mc^2$$',
    'citations': ('A claim.[^1] See [the source](https://example.com/a) and <sup>2</sup>.\n\n'
                  '[^1]: Footnote with `code`.\n'),
    'partial_fence': 'Before\n\n```js\nconst x = 1;\nfunction f() {\n  return x',
    'mixed': ('> [!NOTE] Watch out.\n\n| a | b |\n|---|---|\n| 1 | $2$ |\n\n'
              '<details><summary>More</summary>\n\n```http\nGET / HTTP/1.1\nHost: a\n```\n\n</details>'),
}


async def settle(renderer: StreamRenderer):
    """Wait until no render is in flight or waiting for its interval."""
    for _ in range(2000):
        if not renderer._busy and not renderer._timer:
            return
        await asyncio.sleep(0.005)
    raise AssertionError('renderer did not settle')


class Gate:
    """A worker render that holds until released, counting concurrency."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls: list[str] = []
        self.active = self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, text):
        with self._lock:
            self.calls.append(text)
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.entered.set()
        self.release.wait(5)
        with self._lock:
            self.active -= 1
        return f'html:{text}', 0.0


class StreamRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_published_html_equals_synchronous_render(self):
        for name, text in SAMPLES.items():
            with self.subTest(name):
                published = []
                renderer = StreamRenderer(published.append)
                for start in range(0, len(text), 7):
                    renderer.append(text[start:start + 7])
                    await asyncio.sleep(0)
                await settle(renderer)
                self.assertEqual(published[-1], to_html(text))
                self.assertEqual(renderer.render(), to_html(text))

    async def test_worker_and_loop_threads_render_identically_at_once(self):
        # Stateful lexers (psql, http) and the shared parsers in concurrent use.
        texts = list(SAMPLES.values()) + [
            '```psql\nSELECT 1;\n\\d users\n```', '```http\nPOST / HTTP/1.1\nContent-Type: application/json\n\n{"a": 1}\n```']
        expected = [to_html(t) for t in texts]
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(to_html, texts * 25))
        self.assertEqual(results, expected * 25)

    async def test_renders_coalesce_to_one_in_flight_and_the_latest_text(self):
        gate, published = Gate(), []
        renderer = StreamRenderer(published.append)
        with patch.object(render, '_timed_html', gate):
            renderer.append('a')
            await asyncio.to_thread(gate.entered.wait, 5)
            for char in 'bcdefgh':
                renderer.append(char)
                await asyncio.sleep(0)
            self.assertEqual(published, [])  # the loop was not waiting on the worker
            gate.release.set()
            await settle(renderer)
        self.assertEqual(gate.calls, ['a', 'abcdefgh'])
        self.assertEqual(gate.peak, 1)
        self.assertEqual(published, ['html:a', 'html:abcdefgh'])

    async def test_trailing_text_is_rendered_without_another_delta(self):
        published = []
        renderer = StreamRenderer(published.append)
        renderer.append('first')
        renderer.append(' and **last**')  # within the interval of the first
        await settle(renderer)
        self.assertEqual(published[-1], to_html('first and **last**'))

    async def test_superseded_render_never_replaces_a_newer_revision(self):
        gate, published = Gate(), []
        renderer = StreamRenderer(published.append)
        with patch.object(render, '_timed_html', gate):
            renderer.append('old')
            await asyncio.to_thread(gate.entered.wait, 5)
            renderer.append(' new')
            snapshot = renderer.render()  # a redraw shows the newer revision first
            gate.release.set()
            await settle(renderer)
            self.assertEqual(snapshot, to_html('old new'))
            self.assertEqual(published, [])  # 'html:old' arrived late and was dropped
            self.assertEqual(gate.calls, ['old'])
            renderer.append('er')
            await settle(renderer)
        self.assertEqual(published, ['html:old newer'])

    async def test_closed_renderer_publishes_nothing_more(self):
        gate, published = Gate(), []
        renderer = StreamRenderer(published.append)
        with patch.object(render, '_timed_html', gate):
            renderer.append('partial')
            await asyncio.to_thread(gate.entered.wait, 5)
            renderer.append(' more')
            renderer.close()
            gate.release.set()
            await asyncio.to_thread(render._render_worker().submit(lambda: None).result)
            await asyncio.sleep(0.05)
        self.assertEqual(published, [])
        self.assertFalse(renderer._busy)
        self.assertIsNone(renderer._timer)

    async def test_loop_heartbeat_stays_short_while_streaming_large_code(self):
        text = '```python\n' + CODE_LINE * 5000
        started = time.monotonic()
        to_html(text)
        synchronous = time.monotonic() - started
        published = []
        renderer = StreamRenderer(published.append)
        renderer.text = text
        loop, delays, running = asyncio.get_running_loop(), [], True
        # Debug mode's per-callback stack capture stats files, and each of those
        # GIL releases waits out a switch interval behind the worker.
        loop.set_debug(False)
        self.addCleanup(loop.set_debug, True)

        async def heartbeat():
            while running:
                expected = loop.time() + 0.005
                await asyncio.sleep(0.005)
                delays.append(loop.time() - expected)

        beat = asyncio.create_task(heartbeat())
        for _ in range(120):
            renderer.append(CODE_LINE)
            await asyncio.sleep(0.005)
        await settle(renderer)
        running = False
        await beat
        self.assertTrue(published)
        self.assertEqual(published[-1], to_html(renderer.text))
        # Inline, every repaint stalled the loop for a whole parse.
        self.assertLess(max(delays), synchronous / 2)


class StreamingBackend:
    def __init__(self, parts, *, hold=False):
        self.parts, self.hold = parts, hold
        self.streamed = asyncio.Event()

    async def load(self, model):
        pass

    async def models(self, provider=None):
        return [{'id': 'test-vision', 'architecture': {'input_modalities': ['text', 'image']}}]

    async def stream(self, model, messages, tools):
        for part in self.parts:
            yield {'delta': {'content': part}}
            await asyncio.sleep(0)
        self.streamed.set()
        if self.hold:
            await asyncio.Event().wait()
        yield {'delta': {}, 'finish_reason': 'stop'}

    async def aclose(self):
        pass


class SessionStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        _, self.runtime = make_runtime(Path(self.temp.name))
        await self.runtime.open()
        self.events = []
        self.session = Session(self.runtime.controller, lambda kind, payload: self.events.append((kind, payload)))
        await self.session.boot()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.temp.cleanup()

    def kinds(self):
        return [kind for kind, _ in self.events]

    async def test_final_html_is_complete_and_no_update_follows_reply_end(self):
        text = '```python\n' + CODE_LINE * 300 + '```\n\nDone with $x^2$.'
        parts = [text[i:i + 40] for i in range(0, len(text), 40)]
        self.runtime.controller.backend = StreamingBackend(parts)
        await self.session.submit('Write code')
        await self.session._generation
        await asyncio.sleep(0.1)  # any late worker result has landed by now
        kinds = self.kinds()
        end = kinds.index('reply_end')
        self.assertNotIn('reply_update', kinds[end:])
        self.assertEqual(self.events[end][1]['html'], audited_html(text, None))

    async def test_stop_is_prompt_while_a_render_is_in_flight(self):
        text = '```python\n' + CODE_LINE * 50
        backend = StreamingBackend([text[i:i + 20] for i in range(0, len(text), 20)], hold=True)
        self.runtime.controller.backend = backend
        gate = Gate()
        with patch.object(render, '_timed_html', gate):
            await self.session.submit('Write code')
            await backend.streamed.wait()
            await asyncio.to_thread(gate.entered.wait, 5)
            task = self.session._generation
            self.assertTrue(self.session.stop())
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)  # the worker is still held
            gate.release.set()
            await asyncio.to_thread(render._render_worker().submit(lambda: None).result)
            await asyncio.sleep(0.05)
        kinds = self.kinds()
        end = kinds.index('reply_end')
        self.assertEqual(self.events[end][1]['state'], 'cancelled')
        self.assertNotIn('reply_update', kinds[end:])
        self.assertEqual(self.events[end][1]['html'], to_html(text))


if __name__ == '__main__':
    unittest.main()
