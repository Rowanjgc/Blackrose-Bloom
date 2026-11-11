"""Measure synthetic checkpoint traffic and contention on a scratch chat database.

Linux /proc counters include SQLite main-database and WAL writes together; they
are process counters, not device-level measurements. Run on the target filesystem.
"""
import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from blackrose.drafts import ComposerDrafts
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository


def io_counters():
    return {key: int(value) for key, value in
            (line.split(':', 1) for line in Path('/proc/self/io').read_text().splitlines())}


def summary(values):
    ordered = sorted(values)
    return {'median_ms': round(statistics.median(ordered) * 1000, 3),
            'p95_ms': round(ordered[int((len(ordered) - 1) * .95)] * 1000, 3),
            'max_ms': round(max(ordered) * 1000, 3)}


async def measure(root, checkpoints):
    db = Database(root / 'chats.db')
    await db.connect()
    repo = Repository(db)
    conv = await repo.create_conversation()
    other = await repo.create_conversation()
    _, reply = await repo.begin_turn(conv.id, 'Question', 'synthetic')
    drafts = ComposerDrafts(repo)
    elapsed = {'checkpoint': [], 'draft': [], 'turn': [], 'queue': []}
    submitted = 0
    before = io_counters()

    async def timed(kind, operation):
        start = time.perf_counter()
        await operation
        elapsed[kind].append(time.perf_counter() - start)

    # Deliberately queue the checkpoint first, followed by competing work.
    # The compressed schedule is a stress test of the same 600 snapshots that
    # a 100 KB reply emits over ten minutes, not a ten-minute latency trace.
    for tick in range(1, 601):
        pending = []
        if checkpoints:
            content = 'x' * (100_000 * tick // 600)
            submitted += len(content.encode())
            pending.append(asyncio.create_task(timed('checkpoint', repo.checkpoint_content(reply.id, content))))
        if tick % 10 == 0:
            pending.append(asyncio.create_task(timed('draft', drafts.save(other.id, f'draft {tick}', [], []))))
            async def queue_probe():
                start = time.perf_counter()
                async with db._lock:
                    elapsed['queue'].append(time.perf_counter() - start)
            pending.append(asyncio.create_task(queue_probe()))
        if tick % 100 == 0:
            pending.append(asyncio.create_task(timed('turn', repo.begin_turn(other.id, f'question {tick}', 'synthetic'))))
        if pending:
            await asyncio.gather(*pending)
    after = io_counters()
    result = {'submitted_content_bytes': submitted,
              'process_write_syscall_bytes': after['wchar'] - before['wchar'],
              'process_storage_write_bytes': after['write_bytes'] - before['write_bytes'],
              'latency': {kind: summary(values) for kind, values in elapsed.items() if values}}
    await db.close()
    return result


async def main(directory):
    with tempfile.TemporaryDirectory(dir=directory, prefix='checkpoint-measure-') as temp:
        root = Path(temp)
        control, treatment = root / 'control', root / 'checkpoints'
        control.mkdir()
        treatment.mkdir()
        print(json.dumps({'control': await measure(control, False),
                          'checkpoints': await measure(treatment, True)}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='.', help='scratch database parent filesystem')
    asyncio.run(main(parser.parse_args().directory))
