"""Completed images survive cleanup failures and cancellation during recovery."""
import asyncio
import unittest
from unittest.mock import AsyncMock

import test_local_images as fixtures
from test_gui_images import png


class ImageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.LocalProviderTests.asyncSetUp
    asyncTearDown = fixtures.LocalProviderTests.asyncTearDown
    handle = fixtures.LocalProviderTests.handle
    generate = fixtures.LocalProviderTests.generate
    _set_status = fixtures.LocalProviderTests._set_status
    _unload = fixtures.LocalProviderTests._unload
    _load = fixtures.LocalProviderTests._load

    async def test_completed_job_never_interrupts_and_returns_clean_image(self):
        image = await self.generate('a bird')
        self.assertEqual(image.data, png())
        self.assertIsNone(image.cleanup_warning)
        self.assertFalse(image.cancelled)
        self.assertFalse(any(r.url.path == '/interrupt' for r in self.requests))
        self.assertFalse(any(r.url.path == '/queue' and r.method == 'POST' for r in self.requests))
        self.backend.load.assert_awaited_once()

    async def test_download_survives_release_failure_and_blocks_new_jobs(self):
        self.service.release = AsyncMock(side_effect=RuntimeError('release failed'))
        image = await self.generate('a bird')
        self.assertEqual(image.data, png())
        self.assertIn('release failed', image.cleanup_warning)
        self.assertEqual(self.provider.recovery_error, image.cleanup_warning)
        self.backend.load.assert_not_awaited()
        count = len(self.requests)
        with self.assertRaisesRegex(RuntimeError, 'release failed'):
            await self.generate('another bird')
        self.assertEqual(len(self.requests), count)

    async def test_download_survives_chat_restore_failure(self):
        self.backend.load.side_effect = RuntimeError('reload failed')
        image = await self.generate('a bird')
        self.assertEqual(image.data, png())
        self.assertIn('reload failed', image.cleanup_warning)
        self.assertIn('release', self.events)

    async def test_generation_error_remains_primary_when_cleanup_also_fails(self):
        self.error = True
        self.service.release = AsyncMock(side_effect=RuntimeError('cleanup also failed'))
        with self.assertRaisesRegex(RuntimeError, 'Local image generation failed') as caught:
            await self.generate('a bird')
        self.assertIn('cleanup also failed', self.provider.recovery_error)
        self.assertIn('cleanup also failed', '\n'.join(caught.exception.__notes__))

    async def test_cancellation_before_download_remains_cancellation_on_cleanup_failure(self):
        self.block = True
        self.service.release = AsyncMock(side_effect=RuntimeError('cleanup failed'))
        task = asyncio.create_task(self.generate('a bird'))
        await self.history_ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertIn('cleanup failed', self.provider.recovery_error)
        self.assertIn('cleanup failed', '\n'.join(caught.exception.__notes__))

    async def test_cancellation_after_download_returns_artifact_after_recovery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def cleanup_release():
            entered.set()
            await release.wait()
        self.service.release = cleanup_release
        task = asyncio.create_task(self.generate('a bird'))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        image = await task
        self.assertEqual(image.data, png())
        self.assertTrue(image.cancelled)
        self.assertIsNone(image.cleanup_warning)
        self.backend.load.assert_awaited_once()

    async def test_post_download_cancel_and_cleanup_error_keep_both_outcomes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def cleanup_release():
            entered.set()
            await release.wait()
            raise RuntimeError('release failed')
        self.service.release = cleanup_release
        task = asyncio.create_task(self.generate('a bird'))
        await entered.wait()
        task.cancel()
        release.set()
        image = await task
        self.assertEqual(image.data, png())
        self.assertTrue(image.cancelled)
        self.assertIn('release failed', image.cleanup_warning)
