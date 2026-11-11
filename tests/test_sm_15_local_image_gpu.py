"""SM-15: GPU handoff ownership, residency and recovery states of the local image model.

Every test here is offline: the ComfyUI transport is a ``MockTransport`` and the
service is a double that records ownership effects instead of calling systemd. The
handoff lock and warm record live under the test's own temporary root.
"""
import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch

import httpx

import test_local_images as fixtures
from blackrose.local_images import GIB, LocalImages


class OwnershipService:
    """A ``ComfyService`` double that records which duty each release discharged.

    ``owned`` mirrors the production flag: true means this process owes a stop.
    ``watch`` runs while a release is in flight so a test can read the phase.
    """

    def __init__(self, events, owned=False):
        self.events = events
        self.owned = owned
        self.fail_start = False
        self.fail_release = False
        self.watch = None

    async def ensure_running(self):
        if self.fail_start:
            raise RuntimeError('start failed')
        self.events.append('start')

    async def free(self):
        self.events.append('free')

    async def release(self):
        if self.watch is not None:
            self.watch()
        if self.fail_release:
            raise RuntimeError('stop failed')
        if self.owned:
            self.events.append('stop')
            self.owned = False
        else:
            await self.free()


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    """The pair (WarmRecord.started_service, ComfyService.owned) across every handoff."""

    _set_status = fixtures.LocalProviderTests._set_status
    _unload = fixtures.LocalProviderTests._unload
    _load = fixtures.LocalProviderTests._load

    async def asyncSetUp(self):
        await fixtures.LocalProviderTests.asyncSetUp(self)
        await self.provider.close()
        self.down = False
        self.free_vram = 16 * GIB
        self.service = OwnershipService(self.events)
        self.provider = self.make_provider(self.service)

    async def asyncTearDown(self):
        await self.provider.close()
        self.temp.cleanup()

    def make_provider(self, service, idle_release=120):
        return LocalImages('http://127.0.0.1:8189', self.root, self.backend,
                           transport=httpx.MockTransport(self.handle), service=service,
                           lock_path=self.root / 'lock', poll_interval=0.001,
                           idle_release=idle_release)

    async def handle(self, request):
        if self.down and request.url.path == '/queue':
            raise httpx.ConnectError('service is down', request=request)
        return await fixtures.LocalProviderTests.handle(self, request)

    async def generate(self, prompt='test', provider=None, **kwargs):
        kwargs.setdefault('chat_model', self.chat)
        return await (provider or self.provider).generate(prompt, **kwargs)

    def record(self):
        path = self.provider.warm_path
        return json.loads(path.read_text()) if path.exists() else None

    def write_record(self, **changes):
        record = {'token': 'warm-token', 'pid': os.getpid(), 'until': time.time() + 60,
                  'headroom': 10 * GIB, 'chat_model': self.chat, 'started_service': False}
        record.update(changes)
        self.provider.warm_path.write_text(json.dumps(record))
        return record

    async def wait_for(self, predicate, limit=2.0):
        deadline = time.monotonic() + limit
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('condition not reached; events=' + repr(self.events))
            await asyncio.sleep(0.005)

    # Handoff: process -> record

    async def test_a_warm_handoff_moves_the_stop_duty_to_the_record(self):
        self.service.owned = True
        await self.generate()
        record = self.record()
        self.assertEqual(self.events, ['start'])
        self.assertTrue(record['started_service'])
        self.assertFalse(self.service.owned)
        self.assertEqual(record['pid'], os.getpid())
        self.assertNotEqual(bool(record['started_service']), self.service.owned,
                            'Exactly one account claims the running service')

    async def test_the_stop_duty_is_cleared_only_after_the_record_is_durable(self):
        self.service.owned = True
        with patch.object(self.provider, '_write_record', side_effect=OSError('disk full')):
            image = await self.generate()
        self.assertIn('disk full', image.cleanup_warning)
        self.assertIn('disk full', self.provider.recovery_error)
        self.assertIsNone(self.record())
        self.assertTrue(self.service.owned,
                        'A record that never reached disk must not consume the in-memory duty')

    # Handoff: record -> process

    async def test_adoption_removes_the_record_before_this_process_owes_the_stop(self):
        self.write_record(started_service=True, pid=os.getpid() + 100000)
        async with self.provider.batch():
            self.assertIsNone(self.record(), 'The adopting session takes the record away')
            self.assertTrue(self.service.owned, 'The duty is in this process for the session')
        kept = self.record()
        self.assertEqual(self.events, [], 'No job ran, so no memory was touched')
        self.assertEqual(kept['pid'], os.getpid())
        self.assertTrue(kept['started_service'])
        self.assertFalse(self.service.owned)

    async def test_an_adopted_record_that_can_be_described_is_put_back_unchanged(self):
        until = time.time() + 60
        self.write_record(started_service=True, until=until)
        async with self.provider.batch():
            pass
        self.assertEqual(self.events, [])
        kept = self.record()
        self.assertEqual(kept['until'], until, 'The original deadline is kept')
        self.assertTrue(kept['started_service'])

    async def test_a_record_nobody_can_describe_is_released_instead_of_stranded(self):
        # An unreadable record is adopted as 'no known chat model, free at first use'.
        # A session that never reaches the service must still release it, otherwise the
        # image model stays loaded with no record, no timer and no owner.
        self.provider.warm_path.write_text('{not json')
        async with self.provider.batch():
            pass
        self.assertEqual(self.events, ['free'])
        self.assertIsNone(self.record())

    # Preemption and reaping

    async def test_preempting_an_owned_warm_model_leaves_no_owner_at_all(self):
        self.service.owned = True
        await self.generate()
        self.rows.append({'id': 'qwen-coder', 'status': {'value': 'unloaded'}})
        await self.provider.make_room_for_chat('qwen-coder')
        self.assertEqual(self.events, ['start', 'stop'])
        self.assertIsNone(self.record())
        self.assertFalse(self.service.owned)

    async def test_preempting_an_external_service_only_frees_its_models(self):
        await self.generate()
        self.assertFalse(self.record()['started_service'])
        self._set_status(self.chat, 'sleeping')
        await self.provider.make_room_for_chat(self.chat)
        self.assertEqual(self.events, ['start', 'free'],
                         'A service Blackrose did not start is never stopped')
        self.assertIsNone(self.record())

    async def test_an_orphan_reap_stops_the_service_the_record_owes(self):
        self.write_record(pid=2 ** 22 + 12345, until=time.time() - 1, started_service=True)
        await self.provider.reap_orphan()
        self.assertEqual(self.events, ['stop'])
        self.assertIsNone(self.record())
        self.assertFalse(self.service.owned)

    async def test_a_dead_owner_keeps_its_warm_model_until_the_deadline(self):
        self.write_record(pid=2 ** 22 + 12345, until=time.time() + 60, started_service=True)
        await self.provider.reap_orphan()
        self.assertEqual(self.events, [], 'The model is still warm and still adoptable')
        self.assertIsNotNone(self.record())
        self.assertFalse(self.service.owned, 'Reading a record never takes its duty')

    async def test_the_idle_timer_stops_the_service_after_the_window(self):
        self.service.owned = True
        await self.provider.close()
        self.provider = self.make_provider(self.service, idle_release=0.02)
        await self.generate()
        await self.wait_for(lambda: self.record() is None)
        self.assertEqual(self.events, ['start', 'stop'])
        self.assertFalse(self.service.owned)

    # Release guards

    async def test_a_release_for_another_token_is_refused(self):
        self.write_record(started_service=True)
        self.assertFalse(await self.provider._release_record(token='someone-else'))
        self.assertEqual(self.events, [])
        self.assertIsNotNone(self.record())
        self.assertFalse(self.service.owned)

    async def test_an_unexpired_record_is_rearmed_instead_of_released(self):
        self.write_record(started_service=True)
        self.assertFalse(await self.provider._release_record(expired_only=True))
        self.assertEqual(self.events, [])
        self.assertIsNotNone(self.record())
        self.assertIsNotNone(self.provider._timer)
        self.assertFalse(self.provider._timer.done())

    async def test_closing_a_window_never_releases_another_windows_model(self):
        self.write_record(started_service=True, pid=os.getpid() + 100000)
        self.assertFalse(await self.provider._release_record(owner_only=True, wait=True))
        self.assertEqual(self.events, [])
        self.assertIsNotNone(self.record())
        self.assertFalse(self.service.owned)

    # Phase reporting

    async def test_the_releasing_phase_is_reported_while_the_release_is_in_flight(self):
        self.write_record(started_service=True)
        seen = []
        self.service.watch = lambda: seen.append(self.provider.status())
        self.assertEqual(await self.provider.release_now(), {'state': 'not_loaded'})
        self.assertEqual(seen, [{'state': 'releasing'}])
        self.assertEqual(self.events, ['stop'])
        self.assertIsNone(self.provider._phase)

    async def test_the_phase_follows_the_job_from_loading_to_generating(self):
        phases = {}

        def progress(message):
            phases[message] = self.provider.status()

        await self.generate(on_progress=progress)
        self.assertEqual(phases['Starting local image generation…'], {'state': 'loading'})
        # 'generating' is displayed as in_use, the same word an adopted job gets.
        self.assertEqual(phases['Generating image locally · Qwen Image 2.1…'], {'state': 'in_use'})
        self.assertIsNone(self.provider._phase)
        status = self.provider.status()
        self.assertEqual(status['state'], 'loaded')
        self.assertTrue(status['this_window'])
        self.assertGreater(status['seconds_left'], 110)

    # Chat-model restoration

    async def test_cancellation_restores_the_chat_model_once(self):
        self.provider.keep_chat_loaded = False
        self.block = True
        task = asyncio.create_task(self.generate())
        await self.history_ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.backend.load.await_count, 1)
        self.assertEqual(self.events, ['start', 'unload:gemma-4-e4b-it', 'free',
                                       'load:gemma-4-e4b-it'])
        self.assertIsNone(self.provider._phase)
        self.assertFalse(self.provider._lock_held_elsewhere())

    async def test_a_batch_restores_the_chat_model_once_after_its_last_job(self):
        self.provider.keep_chat_loaded = False
        async with self.provider.batch():
            for _ in range(3):
                await self.generate()
        self.assertEqual(self.backend.load.await_count, 1)
        self.assertEqual(self.events, ['start', 'unload:gemma-4-e4b-it', 'start', 'start',
                                       'free', 'load:gemma-4-e4b-it'])

    async def test_a_failed_job_inside_the_batch_still_restores_the_chat_model(self):
        self.provider.keep_chat_loaded = False
        with self.assertRaisesRegex(RuntimeError, 'No paid request was sent'):
            async with self.provider.batch():
                await self.generate()
                self.fail_submit = True
                await self.generate()
        self.assertEqual(self.backend.load.await_count, 1)
        self.assertEqual(self.events[-2:], ['free', 'load:gemma-4-e4b-it'])

    # Recovery latch and refusal

    async def test_a_failed_release_latches_recovery_and_refuses_later_work(self):
        self.provider.keep_chat_loaded = False
        self.service.fail_release = True
        image = await self.generate()
        error = self.provider.recovery_error
        self.assertIn('stop failed', image.cleanup_warning)
        self.assertEqual(error, image.cleanup_warning)
        count = len(self.requests)
        with self.assertRaisesRegex(RuntimeError, 'stop failed'):
            await self.generate()
        with self.assertRaisesRegex(RuntimeError, 'stop failed'):
            async with self.provider.batch():
                pass
        self.assertEqual(len(self.requests), count, 'A latched error refuses before any request')
        self.assertEqual(await self.provider.release_now(), {'state': 'not_loaded'})
        self.assertEqual(self.provider.recovery_error, error,
                         'recovery_error is never cleared inside a process')
        self.assertIsNone(self.provider._phase)

    async def test_a_failed_start_does_not_latch_recovery(self):
        # Nothing was submitted and the service was never reached, so there is no
        # memory to recover; the start failure must not poison later jobs.
        self.service.owned = True
        self.service.fail_start = True
        self.down = True
        with self.assertRaisesRegex(RuntimeError, 'start failed'):
            await self.generate()
        self.assertIsNone(self.provider.recovery_error)
        self.assertEqual(self.events, ['stop'], 'The service this process started is stopped')
        self.assertFalse(self.service.owned)
        self.down = False
        self.service.fail_start = False
        self.assertIsNotNone(await self.generate())

    async def test_the_handoff_lock_is_free_after_a_failed_submission(self):
        self.fail_submit = True
        with self.assertRaisesRegex(RuntimeError, 'No paid request was sent'):
            await self.generate()
        self.assertFalse(self.provider._lock_held_elsewhere())


if __name__ == '__main__':
    unittest.main()
