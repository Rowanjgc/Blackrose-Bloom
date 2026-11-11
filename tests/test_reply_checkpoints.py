"""Coalesced plain-reply durability, including abrupt process termination."""
import asyncio
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from blackrose.storage.db import Database
from blackrose.storage.records import MessageState
from blackrose.storage.repository import Repository
import test_gui_images as fixtures


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def test_long_reply_checkpoint_submitted_volume(self):
        repo = self.runtime.controller._repo
        conversation = self.runtime.controller.conversation
        _, reply = await repo.begin_turn(conversation.id, 'Long reply', 'test')
        submitted = 0
        for tick in range(1, 601):
            content = 'x' * (100_000 * tick // 600)
            submitted += len(content.encode('utf-8'))
            self.assertTrue(await repo.checkpoint_content(reply.id, content))
        # This documents the current full-prefix cost; it is not a bound on
        # SQLite/WAL traffic, which depends on page and checkpoint behavior.
        self.assertEqual(submitted, 30_049_800)
        self.assertEqual((await repo.messages(conversation.id))[-1].content, 'x' * 100_000)

    async def test_checkpoint_guards_terminal_user_missing_and_audited_rows(self):
        repo = self.runtime.controller._repo
        conversation = self.runtime.controller.conversation
        user, reply = await repo.begin_turn(conversation.id, 'Question', 'test')
        self.assertTrue(await repo.checkpoint_content(reply.id, 'Recent prefix'))
        messages = await repo.messages(conversation.id)
        self.assertEqual(messages[-1].content, 'Recent prefix')
        self.assertEqual(messages[-1].state, MessageState.PENDING)
        self.assertFalse(await repo.checkpoint_content(user.id, 'Wrong'))
        self.assertFalse(await repo.checkpoint_content(-1, 'Wrong'))
        await repo.finish_message(reply.id, 'Final answer', MessageState.COMPLETE)
        self.assertFalse(await repo.checkpoint_content(reply.id, 'Stale'))
        self.assertEqual((await repo.messages(conversation.id))[-1].content, 'Final answer')
        _, audited = await repo.begin_turn(conversation.id, 'Audited question', 'test')
        from blackrose.citations import EvidenceRegistry, new_report
        report = new_report('balanced', 'test', 'Audited prefix', EvidenceRegistry(audited.id))
        await repo.save_audit(audited.id, report, content='Audited prefix')
        self.assertFalse(await repo.checkpoint_content(audited.id, 'Wrong'))
        self.assertEqual((await repo.messages(conversation.id))[-1].content, 'Audited prefix')
        self.assertEqual(await repo.audit_for(audited.id), report)

    async def test_idle_tail_is_saved_once_and_stop_flushes_the_full_reply(self):
        c = self.runtime.controller
        checkpointed = asyncio.Event()
        resume = asyncio.Event()
        writes = []
        checkpoint = c._repo.checkpoint_content

        async def save(identity, content):
            writes.append(content)
            result = await checkpoint(identity, content)
            checkpointed.set()
            return result

        class PausedBackend(fixtures.Backend):
            async def stream(self, *args, **kwargs):
                for _ in range(100):
                    yield {'delta': {'content': 'a'}}
                await resume.wait()
                yield {'delta': {'content': ' unsaved tail'}}
                await asyncio.Event().wait()

        c.backend = PausedBackend()
        visible = []
        tail = asyncio.Event()

        def delta(part):
            visible.append(part)
            if part == ' unsaved tail':
                tail.set()

        with patch('blackrose.turns.CONTENT_CHECKPOINT_SECONDS', 0.02), \
                patch.object(c._repo, 'checkpoint_content', side_effect=save):
            task = asyncio.create_task(c.send('Question', on_delta=delta))
            try:
                await asyncio.wait_for(checkpointed.wait(), 5)
                await asyncio.sleep(0.08)
                self.assertEqual(writes, ['a' * 100])
                current = (await c.current_messages())[-1]
                self.assertEqual(current.content, ''.join(visible))
                self.assertEqual(current.state, MessageState.PENDING)
                resume.set()
                await asyncio.wait_for(tail.wait(), 5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            final = (await c.current_messages())[-1]
            self.assertEqual(final.content, 'a' * 100 + ' unsaved tail')
            self.assertEqual(final.state, MessageState.CANCELLED)
            count = len(writes)
            await asyncio.sleep(0.04)
            self.assertEqual(len(writes), count)

    async def test_cancellation_drains_inflight_checkpoint_before_terminal_write(self):
        c = self.runtime.controller
        started, release = asyncio.Event(), asyncio.Event()
        checkpoint = c._repo.checkpoint_content
        order = []

        async def save(identity, content):
            started.set()
            await release.wait()
            result = await checkpoint(identity, content)
            order.append('checkpoint')
            return result

        class PausedBackend(fixtures.Backend):
            async def stream(self, *args, **kwargs):
                yield {'delta': {'content': 'Accepted text'}}
                await asyncio.Event().wait()

        c.backend = PausedBackend()
        with patch('blackrose.turns.CONTENT_CHECKPOINT_SECONDS', 0.01), \
                patch.object(c._repo, 'checkpoint_content', side_effect=save):
            task = asyncio.create_task(c.send('Question'))
            try:
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual((await c.current_messages())[-1].state, MessageState.PENDING)
            finally:
                release.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(order, ['checkpoint'])
            final = (await c.current_messages())[-1]
            self.assertEqual(final.content, 'Accepted text')
            self.assertEqual(final.state, MessageState.CANCELLED)

    async def test_checkpoint_failure_retries_newest_prefix_and_normal_finish_flushes(self):
        c = self.runtime.controller
        retried = asyncio.Event()
        checkpoint = c._repo.checkpoint_content
        attempts = 0

        async def save(identity, content):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError('injected checkpoint failure')
            result = await checkpoint(identity, content)
            retried.set()
            return result

        class PausedBackend(fixtures.Backend):
            async def stream(self, *args, **kwargs):
                yield {'delta': {'content': 'Prefix'}}
                await retried.wait()
                yield {'delta': {'content': ' final tail'}, 'finish_reason': 'stop'}

        c.backend = PausedBackend()
        with patch('blackrose.turns.CONTENT_CHECKPOINT_SECONDS', 0.01), \
                patch.object(c._repo, 'checkpoint_content', side_effect=save), \
                self.assertLogs('blackrose.turns', level='WARNING'):
            await asyncio.wait_for(c.send('Question'), 5)
        self.assertEqual(attempts, 2)
        final = (await c.current_messages())[-1]
        self.assertEqual(final.content, 'Prefix final tail')
        self.assertEqual(final.state, MessageState.COMPLETE)

    async def test_killed_stream_recovers_checkpoint_without_duplicate_turns(self):
        scratch = self.root / 'killed-stream'
        scratch.mkdir()
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), '--checkpoint-child', str(scratch),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 15)
            if not line:
                self.fail((await process.stderr.read()).decode())
            evidence = json.loads(line)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        self.assertEqual(evidence['writes'], 2)
        self.assertGreaterEqual(evidence['checkpoint_gap_seconds'], 1.0)
        self.assertTrue(evidence['accepted'].startswith(evidence['saved']))
        self.assertTrue(evidence['accepted'].endswith(' unsaved tail'))
        self.assertNotEqual(evidence['accepted'], evidence['saved'])
        # Reopen twice to exercise WAL recovery and idempotent startup reaping.
        for attempt in range(2):
            db = Database(scratch / 'test.db')
            await db.connect()
            try:
                repo = Repository(db)
                self.assertEqual(await repo.reap_pending(), 1 if attempt == 0 else 0)
                messages = await repo.messages(evidence['conversation_id'])
                self.assertEqual(len(messages), 2)
                self.assertEqual(messages[-1].content, evidence['saved'])
                self.assertEqual(messages[-1].reply_to, messages[0].id)
                self.assertEqual(messages[-1].state, MessageState.ERROR)
                self.assertIn('interrupted', messages[-1].error)
            finally:
                await db.close()
        print('Reply checkpoint crash probe: ' + json.dumps({key: evidence[key] for key in (
            'writes', 'chunks', 'checkpoint_gap_seconds', 'max_delta_gap_seconds')}))


async def checkpoint_child(root):
    _, runtime = fixtures.make_runtime(root)
    await runtime.open()
    c = runtime.controller
    c.schedule_title = lambda *args: None
    checkpoint = c._repo.checkpoint_content
    times = []
    saved = ''
    ready = asyncio.Event()
    chunks = 0
    visible = ''
    delta_times = []

    async def save(identity, content):
        nonlocal saved
        result = await checkpoint(identity, content)
        saved = content
        times.append(time.monotonic())
        if len(times) == 2:
            ready.set()
        return result

    class StreamingBackend(fixtures.Backend):
        async def stream(self, *args, **kwargs):
            nonlocal chunks
            while not ready.is_set():
                yield {'delta': {'content': f'chunk{chunks} '}}
                chunks += 1
                await asyncio.sleep(0.01)
            yield {'delta': {'content': ' unsaved tail'}}
            await asyncio.Event().wait()

    def delta(part):
        nonlocal visible
        visible += part
        delta_times.append(time.monotonic())
        if part == ' unsaved tail':
            print(json.dumps({'saved': saved, 'accepted': visible, 'writes': len(times),
                              'chunks': chunks, 'conversation_id': c.conversation.id,
                              'checkpoint_gap_seconds': times[1] - times[0],
                              'max_delta_gap_seconds': max(b - a for a, b in
                                                          zip(delta_times, delta_times[1:], strict=False))}), flush=True)

    c.backend = StreamingBackend()
    c._repo.checkpoint_content = save
    await c.send('Synthetic crash recovery question', on_delta=delta)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--checkpoint-child':
        asyncio.run(checkpoint_child(Path(sys.argv[2])))
    else:
        unittest.main()
