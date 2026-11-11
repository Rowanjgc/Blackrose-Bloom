"""Durable animation recovery contracts need neither WebKit nor playback."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from blackrose.animation_recovery import AnimationRecovery, MAX_REPAIRS


class RecoveryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'animation-recovery.sqlite3'
        self.recovery = AnimationRecovery(self.path)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_claim_finish_and_chain_limit_survive_reopen(self):
        for attempt in range(1, MAX_REPAIRS + 1):
            state = await self.recovery.claim(1, attempt, 'renderer failed')
            self.assertTrue(state['claimed'])
            self.assertEqual((state['root_id'], state['attempt']), (1, attempt))
            await self.recovery.finish(1, attempt, 'replaced', attempt + 1)
            self.recovery = AnimationRecovery(self.path)
            duplicate = await self.recovery.claim(1, attempt, 'duplicate')
            self.assertFalse(duplicate['claimed'])
            self.assertEqual(duplicate['status'], 'replaced')
        exhausted = await self.recovery.claim(1, MAX_REPAIRS + 1, 'still failing')
        self.assertEqual(exhausted['status'], 'exhausted')
        self.assertFalse(exhausted['claimed'])
        self.assertEqual(exhausted['root_id'], 1)

    async def test_concurrent_claims_spend_one_attempt_and_finish_is_terminal(self):
        claims = await asyncio.gather(*(self.recovery.claim(1, 10, 'bad') for _ in range(4)))
        self.assertEqual(sum(bool(c['claimed']) for c in claims), 1)
        await self.recovery.finish(1, 10, 'stopped')
        await self.recovery.finish(1, 10, 'replaced', 20)
        state = (await AnimationRecovery(self.path).states(1))[10]
        self.assertEqual(state['attempt'], 1)
        self.assertEqual(state['status'], 'stopped')
        self.assertIsNone(state['replacement_attachment_id'])

    async def test_delete_is_scoped_and_idempotent_including_absent_database(self):
        await self.recovery.delete(1)
        self.assertFalse(self.path.exists())
        await self.recovery.claim(1, 10, 'bad')
        await self.recovery.claim(2, 20, 'other chat')
        for _ in range(2):
            await self.recovery.delete(1)
            self.recovery = AnimationRecovery(self.path)
            self.assertFalse(await self.recovery.states(1))
            self.assertEqual(list(await self.recovery.states(2)), [20])
