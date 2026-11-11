"""H10 synthetic growth baselines; no inference, network or live application data.

Run with --output artifacts/h10-growth.json. Timings are observations, not gates.
The native companion imports the same persisted chat fixture.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_gui_images import make_runtime, png
from test_retriever import FakeProvider

from blackrose.gui.session import Session
from blackrose.storage.repository import NewAttachment
from local_knowledge.config import Config
from local_knowledge.database import connect, doctor
from local_knowledge.ingestion import import_file
from local_knowledge.retrieval import list_documents, search


def identity():
    root = Path(__file__).resolve().parents[1]
    return {'recorded_at': datetime.now(UTC).isoformat(), 'python': platform.python_version(),
            'platform': platform.platform(), 'cpu': platform.processor(),
            'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
            'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'method': 'five repeated wall-clock samples; first access reported separately; OS caches not flushed',
            'limitations': ['synthetic data and deterministic 128-dimensional embeddings',
                            'no real provider/model initialization, GPU work or production data',
                            'host/session rendering excludes native bridge, layout and paint; see native report',
                            'observations only; no performance threshold asserted']}


def summary(samples):
    return {'first_ms': round(samples[0], 3), 'median_ms': round(statistics.median(samples), 3),
            'max_ms': round(max(samples), 3), 'samples_ms': [round(v, 3) for v in samples]}


async def timed_async(operation, count=5):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        await operation()
        samples.append((time.perf_counter() - start) * 1000)
    return summary(samples)


def timed(operation, count=5):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - start) * 1000)
    return summary(samples)


async def seed_chat(root, conversations, messages, artifacts):
    config, runtime = make_runtime(root)
    await runtime.open()
    repo = runtime.controller._repo
    for i in range(conversations - 1):
        await repo.create_conversation(f'Synthetic history {i:04d}')
    current = await repo.create_conversation('Synthetic growth transcript')
    runtime.controller.conversation = current
    for i in range(messages):
        attachments = []
        if i < artifacts:
            data = png(width=320 + i, height=200)
            blob = runtime.controller._store.write(data, '.png')
            attachments = [NewAttachment('image/png', f'fixture-{i}.png', blob.relative_path,
                                         blob.digest, blob.size, 320 + i, 200)]
        await repo.add_message(current.id, 'assistant' if i % 2 else 'user',
                               f'## Synthetic turn {i}\n\n' +
                               ('Database records preserve evidence. **Review** the saved result.\n\n' * 8),
                               attachments=attachments)
    return config, runtime


async def measure_chat(root, conversations, messages, artifacts):
    _, runtime = await seed_chat(root, conversations, messages, artifacts)
    repo = runtime.controller._repo
    result = {'dataset': {'conversations': conversations, 'messages': messages,
                          'image_artifacts': artifacts, 'paragraphs_per_message': 8},
              'conversation_listing': await timed_async(repo.list_conversations)}
    result['listed_conversations'] = len(await repo.list_conversations())
    result['stored_message_reload'] = await timed_async(runtime.controller.current_messages)
    events = []
    session = Session(runtime.controller, lambda kind, payload: events.append((kind, payload)))

    async def render():
        events.clear()
        await session.render_transcript()

    result['session_transcript_payload'] = await timed_async(render)
    payload = next(value for kind, value in events if kind == 'transcript')
    result['payload_bytes'] = len(json.dumps(payload).encode())
    result['rendered_messages'] = len(payload['messages'])
    # The page starts at the latest window; every older page stays reachable.
    history, earlier, page_samples = list(payload['messages']), payload['earlier'], []
    while earlier:
        start = time.perf_counter()
        page = await session.earlier_messages(runtime.controller.conversation.id, history[0]['id'])
        page_samples.append((time.perf_counter() - start) * 1000)
        history[:0], earlier = page['messages'], page['earlier']
    assert len(history) == messages
    result['earlier_pages'] = len(page_samples)
    if page_samples:
        result['earlier_page'] = summary(page_samples)
    result['artifacts_loaded'] = sum(len(m['attachments']) for m in history)
    assert result['artifacts_loaded'] == artifacts
    await session.close()
    await runtime.close()
    result['runtime_reopen'] = await timed_async(runtime.open, count=1)
    # Each subsequent sample must actually close and reopen storage.
    await runtime.close()
    samples = result['runtime_reopen']['samples_ms']
    for _ in range(4):
        start = time.perf_counter()
        await runtime.open()
        samples.append((time.perf_counter() - start) * 1000)
        await runtime.close()
    result['runtime_reopen'] = summary(samples)
    result['chat_db_bytes'] = Path(runtime.database.path).stat().st_size
    result['blob_bytes'] = sum(p.stat().st_size for p in (root / 'attachments').rglob('*') if p.is_file())
    return result


def measure_library(root, documents):
    root.mkdir(parents=True)
    config = Config(database_path=str(root / 'knowledge.db'), dimensions=128,
                    chunk_tokens=32, chunk_overlap=4)
    provider = FakeProvider()
    for i in range(documents):
        path = root / f'reference-{i:04d}.md'
        path.write_text(f'# Database {i}\n\nDatabase transactions preserve records for fixture {i}.\n')
        import_file(config, provider, path, tags=['synthetic'])
    result = {'documents': documents, 'list_first_page': timed(lambda: list_documents(config)),
              'list_last_page': timed(lambda: list_documents(config, offset=max(0, documents - 50))),
              'keyword_search': timed(lambda: search(config, provider, 'database', mode='keyword')),
              'hybrid_search_fake_embedder': timed(lambda: search(config, provider, 'database'))}
    assert search(config, provider, 'database')['results']
    db = connect(config, readonly=True)
    try:
        assert doctor(db, 128)['ok']
        result['chunks'] = db.execute('SELECT count(*) FROM chunks').fetchone()[0]
    finally:
        db.close()
    result['db_bytes'] = Path(config.database_path).stat().st_size
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = identity()
    with tempfile.TemporaryDirectory(prefix='blackrose-h10-growth-') as directory:
        root = Path(directory)
        for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
            os.environ[name] = str(root / name)
        report['chat'] = []
        for count, messages, artifacts in [(20, 20, 4), (200, 200, 40), (1000, 1000, 100)]:
            report['chat'].append(await measure_chat(root / f'chat-{count}', count, messages, artifacts))
        report['library'] = [measure_library(root / f'library-{size}', size) for size in (20, 200, 1000)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Growth measurements recorded: {args.output}')


if __name__ == '__main__':
    asyncio.run(main())
