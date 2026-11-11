"""Keeping the local image model warm, batching a round's jobs, and releasing on demand."""
import asyncio
import json
import os
import time
import unittest

import httpx

import test_local_images as fixtures
from blackrose.local_images import GIB, LocalImages


class WarmService:
    """Records whether a release stops a service Blackrose started or only frees its models."""

    def __init__(self, events, owned=False):
        self.events = events
        self.owned = owned

    async def ensure_running(self):
        self.events.append('start')

    async def free(self):
        self.events.append('free')

    async def release(self):
        if self.owned:
            self.events.append('stop')
            self.owned = False
        else:
            await self.free()


class KeepWarmTests(unittest.IsolatedAsyncioTestCase):
    handle = fixtures.LocalProviderTests.handle
    _set_status = fixtures.LocalProviderTests._set_status
    _unload = fixtures.LocalProviderTests._unload
    _load = fixtures.LocalProviderTests._load

    async def asyncSetUp(self):
        await fixtures.LocalProviderTests.asyncSetUp(self)
        await self.provider.close()
        # Gemma alone is loaded with room for the image model beside it: the sharing path.
        self.free_vram = 16 * GIB
        self.service = WarmService(self.events)
        self.provider = self.make_provider(self.service)

    async def asyncTearDown(self):
        await self.provider.close()
        self.temp.cleanup()

    def make_provider(self, service, idle_release=120):
        return LocalImages('http://127.0.0.1:8189', self.root, self.backend,
                           transport=httpx.MockTransport(self.handle), service=service,
                           lock_path=self.root / 'lock', poll_interval=0.001,
                           idle_release=idle_release)

    async def generate(self, prompt='test', provider=None, **kwargs):
        kwargs.setdefault('chat_model', self.chat)
        return await (provider or self.provider).generate(prompt, **kwargs)

    def record(self):
        path = self.provider.warm_path
        return json.loads(path.read_text()) if path.exists() else None

    async def wait_for(self, predicate, limit=2.0):
        deadline = time.monotonic() + limit
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('condition not reached; events=' + repr(self.events))
            await asyncio.sleep(0.005)

    async def test_two_jobs_within_the_window_load_the_image_model_once(self):
        await self.generate()
        self.assertEqual(self.events, ['start'])
        self.assertEqual(self.record()['chat_model'], self.chat)
        status = self.provider.status()
        self.assertEqual(status['state'], 'loaded')
        self.assertGreater(status['seconds_left'], 110)
        # The resident image model now uses the memory a fresh check would want free.
        self.free_vram = 2 * GIB
        first = self.record()['token']
        await self.generate()
        self.assertEqual(self.events, ['start', 'start'])
        self.backend.unload.assert_not_awaited()
        self.backend.load.assert_not_awaited()
        self.assertNotEqual(self.record()['token'], first, 'Each job restarts the window')

    async def test_window_expiry_releases_and_a_later_job_loads_again(self):
        await self.provider.close()
        self.provider = self.make_provider(self.service, idle_release=0.02)
        await self.generate()
        await self.wait_for(lambda: self.record() is None)
        self.assertEqual(self.events, ['start', 'free'])
        self.assertEqual(self.provider.status(), {'state': 'not_loaded'})
        await self.generate()
        self.assertEqual(self.events, ['start', 'free', 'start'])

    async def test_stop_during_a_warm_job_releases_and_leaves_no_timer(self):
        await self.generate()
        self.block = True
        self.history_ready.clear()
        task = asyncio.create_task(self.generate())
        await self.history_ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events, ['start', 'start', 'free'])
        self.assertIsNone(self.record())
        self.assertTrue(self.provider._timer is None or self.provider._timer.done())

    async def test_failed_shared_job_is_not_kept_warm(self):
        self.error = True
        with self.assertRaisesRegex(RuntimeError, 'generation failed'):
            await self.generate()
        self.assertEqual(self.events, ['start', 'free'])
        self.assertIsNone(self.record())

    async def test_chat_load_preempts_the_warm_model_unless_it_shares(self):
        await self.generate()
        await self.provider.make_room_for_chat(self.chat)
        await self.provider.make_room_for_chat('opencode-go/kimi-k2')
        self.assertEqual(self.events, ['start'], 'Gemma loaded and a cloud model both leave it warm')
        self._set_status(self.chat, 'sleeping')
        await self.provider.make_room_for_chat(self.chat)
        self.assertEqual(self.events, ['start', 'free'], 'Waking Gemma needs the memory back')
        self.assertIsNone(self.record())

    async def test_switching_to_another_local_model_preempts(self):
        await self.generate()
        self.rows.append({'id': 'qwen-coder', 'status': {'value': 'unloaded'}})
        await self.provider.make_room_for_chat('qwen-coder')
        self.assertEqual(self.events, ['start', 'free'])

    async def test_non_sharing_job_frees_the_warm_model_before_exclusive_generation(self):
        await self.generate()
        self.free_vram = 2 * GIB
        self.free_after_release = 16 * GIB
        reference = 'data:image/png;base64,' + fixtures.base64.b64encode(fixtures.png()).decode()
        await self.generate(references=[reference, reference])
        self.assertEqual(self.events, ['start', 'start', 'free', 'unload:gemma-4-e4b-it',
                                       'free', 'load:gemma-4-e4b-it'])
        self.assertIsNone(self.record(), 'An exclusive job releases rather than keeping warm')

    async def test_warm_model_is_not_reused_once_system_memory_is_short(self):
        await self.generate()
        # ComfyUI kept the weights in RAM; a second shared job would stage them again.
        self.ram_free = 12 * GIB
        await self.generate()
        self.assertEqual(self.events, ['start', 'start', 'free', 'unload:gemma-4-e4b-it',
                                       'free', 'load:gemma-4-e4b-it'])
        self.assertIsNone(self.record())

    async def test_shared_job_that_leaves_memory_short_is_not_kept_warm(self):
        decide = self.provider._prepare_memory

        async def decide_then_hold_ram(*args):
            shared = await decide(*args)
            self.ram_free = 12 * GIB  # ComfyUI keeps the staged weights after the decision.
            return shared

        self.provider._prepare_memory = decide_then_hold_ram
        await self.generate()
        self.assertEqual(self.events, ['start', 'free'])
        self.assertIsNone(self.record())
        self.backend.unload.assert_not_awaited()

    async def test_batch_runs_exclusive_jobs_under_one_handoff(self):
        self.provider.keep_chat_loaded = False
        async with self.provider.batch():
            for _ in range(3):
                await self.generate()
                # A later job reuses the loaded image model despite the lower reading.
                self.free_vram = 2 * GIB
        self.assertEqual(self.events, ['start', 'unload:gemma-4-e4b-it', 'start', 'start',
                                       'free', 'load:gemma-4-e4b-it'])

    async def test_stop_inside_a_batch_still_restores_once(self):
        self.provider.keep_chat_loaded = False

        async def round_of_images():
            async with self.provider.batch():
                await self.generate()
                self.block = True
                self.history_ready.clear()
                await self.generate()

        task = asyncio.create_task(round_of_images())
        await self.wait_for(lambda: self.block)
        await self.history_ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events, ['start', 'unload:gemma-4-e4b-it', 'start',
                                       'free', 'load:gemma-4-e4b-it'])
        self.assertIsNone(self.record())

    async def test_shared_batch_stays_warm_afterwards(self):
        async with self.provider.batch():
            await self.generate()
            self.free_vram = 2 * GIB
            await self.generate()
        self.assertEqual(self.events, ['start', 'start'])
        self.assertIsNotNone(self.record())

    async def test_batch_without_a_job_keeps_the_adopted_model_warm(self):
        self.service.owned = True
        await self.generate()
        until = self.record()['until']
        async with self.provider.batch():
            pass  # Every image call in the round failed validation before reaching the service.
        self.assertEqual(self.events, ['start'])
        record = self.record()
        self.assertEqual(record['until'], until, 'The original deadline is kept')
        self.assertTrue(record['started_service'])
        self.assertIsNotNone(self.provider._timer)

    async def test_close_releases_this_windows_warm_model(self):
        await self.generate()
        await self.provider.close()
        self.assertEqual(self.events, ['start', 'free'])
        self.assertIsNone(self.record())

    async def test_owned_service_is_stopped_and_an_external_one_freed(self):
        self.service.owned = True
        await self.generate()
        self.assertTrue(self.record()['started_service'])
        self.assertFalse(self.service.owned, 'The stop duty travels with the record')
        await self.provider.release_now()
        self.assertEqual(self.events, ['start', 'stop'])
        self.service.owned = False
        await self.generate()
        self.assertFalse(self.record()['started_service'])
        await self.provider.release_now()
        self.assertEqual(self.events, ['start', 'stop', 'start', 'free'])

    async def test_another_window_reuses_the_warm_model_and_takes_over_its_timer(self):
        other_events = []
        other_service = WarmService(other_events)
        await self.provider.close()
        self.provider = self.make_provider(self.service, idle_release=0.05)
        other = self.make_provider(other_service, idle_release=0.3)
        self.addAsyncCleanup(other.close)
        self.service.owned = True
        await self.generate()
        self.free_vram = 2 * GIB
        await self.generate(provider=other)
        self.assertEqual(other_events, ['start'], 'The second window reused the warm model')
        await asyncio.sleep(0.1)
        self.assertEqual(self.events, ['start'], 'The first window\'s timer no longer owns it')
        self.assertIsNotNone(self.record())
        await self.wait_for(lambda: self.record() is None)
        self.assertEqual(other_events, ['start', 'stop'],
                         'The service Blackrose started is stopped by whoever releases it')

    async def test_close_leaves_another_windows_warm_model(self):
        await self.generate()
        record = self.record() | {'pid': os.getpid() + 100000}
        self.provider.warm_path.write_text(json.dumps(record))
        await self.provider.close()
        self.assertEqual(self.events, ['start'])
        self.assertIsNotNone(self.record())

    async def test_orphaned_expired_record_is_reaped(self):
        await self.generate()
        record = self.record() | {'pid': 2 ** 22 + 12345, 'until': time.time() - 1}
        self.provider.warm_path.write_text(json.dumps(record))
        await self.provider.reap_orphan()
        self.assertEqual(self.events, ['start', 'free'])
        self.assertIsNone(self.record())

    async def test_release_now_leaves_a_running_job_alone(self):
        await self.generate()
        self.block = True
        self.history_ready.clear()
        other = self.make_provider(WarmService([]))
        self.addAsyncCleanup(other.close)
        task = asyncio.create_task(self.generate(provider=other))
        await self.history_ready.wait()
        # The running job adopted the record, so there is nothing idle to release.
        self.assertEqual(self.provider.status(), {'state': 'in_use'})
        self.assertEqual(await self.provider.release_now(), {'state': 'in_use'})
        self.assertEqual(self.events, ['start'])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_status_reports_the_job_phase(self):
        self.block = True
        task = asyncio.create_task(self.generate())
        await self.history_ready.wait()
        self.assertEqual(self.provider.status(), {'state': 'in_use'})
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.provider.status(), {'state': 'not_loaded'})


if __name__ == '__main__':
    unittest.main()
