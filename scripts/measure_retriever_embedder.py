"""Measure the pinned Retriever embedder offline on temporary indexes only.

The model snapshot is required to exist before this command runs. All model
loads use local_files_only; both Retriever databases and all XDG state are
created below a temporary directory and removed at exit.
"""
from __future__ import annotations

import argparse
import asyncio
import faulthandler
from dataclasses import replace
from datetime import UTC, datetime
import gc
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def read_memory_kib() -> int | None:
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def read_available_memory_kib() -> int | None:
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def resources() -> dict:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        'rss_kib': read_memory_kib(),
        'system_mem_available_kib': read_available_memory_kib(),
        'peak_rss_kib': usage.ru_maxrss,
        'threads': len(list(Path('/proc/self/task').iterdir())) if Path('/proc/self/task').exists() else None,
        'cpu_seconds': round(usage.ru_utime + usage.ru_stime, 3),
    }


def measured(operation):
    start = time.perf_counter()
    result = operation()
    return result, (time.perf_counter() - start) * 1000


def summary(samples):
    return {
        'first_ms': round(samples[0], 3),
        'median_ms': round(statistics.median(samples), 3),
        'max_ms': round(max(samples), 3),
        'samples_ms': [round(value, 3) for value in samples],
    }


def check_snapshot(config) -> dict:
    model_cache = Path(config.cache_dir)
    model_key = config.model_id.replace('/', '--')
    snapshot = model_cache / f'models--{model_key}' / 'snapshots' / config.model_revision
    required = ('config.json', 'model.safetensors', 'modules.json', 'tokenizer.json',
                'tokenizer_config.json')
    missing = [name for name in required if not (snapshot / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f'Pinned local model snapshot is incomplete at {snapshot}: {", ".join(missing)}. '
            'No download was attempted.')
    weights = snapshot / 'model.safetensors'
    return {'cache_dir': str(model_cache), 'snapshot': str(snapshot),
            'required_files': list(required), 'weights_bytes': weights.stat().st_size,
            'tokenizer_bytes': (snapshot / 'tokenizer.json').stat().st_size}


class DeterministicControl:
    """Small deterministic 768D control; it does not load any model."""
    def __init__(self, dimensions):
        self.dimensions = dimensions

    def identity(self):
        return {'measurement_control': 'deterministic-v1', 'dimensions': self.dimensions}

    def count_tokens(self, text):
        return len(text.split())

    def vector(self, text):
        values = [1 + text.lower().count(term) for term in ('database', 'forest', 'music')]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] + [0.0] * (self.dimensions - len(values))

    def embed_documents(self, items):
        return [self.vector(title + ' ' + text) for title, text in items]

    def embed_query(self, query):
        return self.vector(query)


def make_sources(root: Path, count: int):
    sources = []
    for index in range(count):
        path = root / f'reference-{index:02d}.md'
        sources.append(path)
    return sources


def write_source(path: Path, index: int, cycle: int):
    path.write_text(
        f'# Database reference {index}\n\n'
        f'Database transactions preserve records for forest music sample {index}, cycle {cycle}.\n'
        'The source supports repeatable hybrid retrieval and index integrity measurements.\n',
        encoding='utf-8')


def measure_index(root: Path, config, provider, cycles=20, document_count=4):
    from local_knowledge.database import connect, doctor
    from local_knowledge.ingestion import import_file
    from local_knowledge.retrieval import search

    root.mkdir(parents=True, exist_ok=True)
    sources_root = root / 'sources'
    sources_root.mkdir(parents=True, exist_ok=True)
    sources = make_sources(sources_root, document_count)
    first_import_ms = None
    for index, path in enumerate(sources):
        write_source(path, index, 0)
        result, elapsed = measured(lambda path=path: import_file(config, provider, path))
        if result['status'] != 'imported':
            raise RuntimeError(f'Expected a scratch import, got {result!r}')
        if first_import_ms is None:
            first_import_ms = elapsed

    query = 'database records forest'
    first_search, first_search_ms = measured(lambda: search(config, provider, query))
    if not first_search['results']:
        raise RuntimeError('The real/synthetic scratch index returned no hybrid-search results')
    warm_search = [measured(lambda: search(config, provider, query))[1] for _ in range(5)]
    cycles_report = []
    cycle_resources = []
    for cycle in range(1, cycles + 1):
        start = time.perf_counter()
        for index, path in enumerate(sources):
            write_source(path, index, cycle)
            result = import_file(config, provider, path)
            if result['status'] != 'updated':
                raise RuntimeError(f'Expected a scratch revision update, got {result!r}')
        import_ms = (time.perf_counter() - start) * 1000
        _, search_ms = measured(lambda: search(config, provider, query))
        cycles_report.append({'cycle': cycle, 'reimport_ms': round(import_ms, 3),
                              'hybrid_search_ms': round(search_ms, 3)})
        if cycle % 5 == 0 or cycle == cycles:
            cycle_resources.append({'cycle': cycle, **resources()})
            print(f'  {config.database_path}: completed revision cycle {cycle}/{cycles}', flush=True)
    db = connect(config, readonly=True)
    try:
        integrity = doctor(db, config.dimensions)
        counts = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                  for name in ('documents', 'chunks', 'embeddings', 'vectors')}
        if not integrity['ok'] or len({counts[name] for name in ('chunks', 'embeddings', 'vectors')}) != 1:
            raise RuntimeError(f'Scratch index integrity failed: {integrity}; counts={counts}')
    finally:
        db.close()
    return {
        'identity': provider.identity(),
        'documents': document_count,
        'first_import_ms': round(first_import_ms or 0, 3),
        'first_hybrid_search_ms': round(first_search_ms, 3),
        'warm_hybrid_search': summary(warm_search),
        'revision_cycles': cycles_report,
        'resource_checkpoints': cycle_resources,
        'counts': counts,
        'doctor': integrity,
        'database_bytes': Path(config.database_path).stat().st_size,
    }


async def measure_serialized_operations(root: Path, config, provider) -> dict:
    """Measure actual model operations queued through KnowledgeService."""
    from blackrose.knowledge import KnowledgeService

    config_path = root / 'knowledge.toml'
    config.save(config_path)
    service = KnowledgeService(str(config_path))
    service._config = config
    service._provider = provider
    original_run = service._run

    def traced_run(operation, arguments):
        print(f'  real worker start {threading.get_ident()}', flush=True)
        result = original_run(operation, arguments)
        print(f'  real worker finish {threading.get_ident()}', flush=True)
        return result

    service._run = traced_run
    query = 'database records forest'
    starts = []

    async def timed_search():
        start = time.perf_counter()
        print('  real KnowledgeService.search submitted', flush=True)
        result = await service.call('search', query=query)
        starts.append((time.perf_counter() - start) * 1000)
        print('  real KnowledgeService.search completed', flush=True)
        return result

    batch_start = time.perf_counter()
    faulthandler.dump_traceback_later(20, repeat=True)
    results = await asyncio.gather(*(timed_search() for _ in range(4)))
    faulthandler.cancel_dump_traceback_later()
    batch_ms = (time.perf_counter() - batch_start) * 1000
    if any(not result['results'] for result in results):
        raise RuntimeError('Serialized control search returned no results')

    await service.close()
    print('  real KnowledgeService searches settled', flush=True)
    return {
        'provider': provider.identity(),
        'parallel_search_calls': 4,
        'batch_ms': round(batch_ms, 3),
        'request_latency_ms': [round(value, 3) for value in starts],
        'serialized_queue_overhead_ms': round(max(starts) - min(starts), 3),
        'resources_after_queue': resources(),
    }


async def measure_shutdown_settlement(root: Path, config, provider) -> dict:
    """Verify cancellation leaves serialized worker settlement to close()."""
    from blackrose.knowledge import KnowledgeService

    config_path = root / 'knowledge.toml'
    config.save(config_path)
    service = KnowledgeService(str(config_path))
    service._config = config
    service._provider = provider
    query = 'database records forest'

    began = threading.Event()
    release = threading.Event()
    original_run = service._run

    def delayed_run(operation, arguments):
        began.set()
        if not release.wait(10):
            raise TimeoutError('measurement release gate timed out')
        return original_run(operation, arguments)

    service._run = delayed_run
    cancelled = asyncio.create_task(service.call('search', query=query))
    if not await asyncio.to_thread(began.wait, 10):
        release.set()
        raise TimeoutError('serialized cancellation operation did not start')
    cancelled.cancel()
    try:
        await cancelled
    except asyncio.CancelledError:
        pass
    close_task = asyncio.create_task(service.close())
    await asyncio.sleep(0.05)
    waited_for_worker = not close_task.done()
    close_start = time.perf_counter()
    release.set()
    await close_task
    print('  cancelled KnowledgeService worker settled', flush=True)
    close_ms = (time.perf_counter() - close_start) * 1000
    service._run = original_run
    return {
        'provider': provider.identity(),
        'cancelled_worker_settled_before_close_returned': waited_for_worker,
        'close_after_release_ms': round(close_ms, 3),
        'controlled_release_gate_ms': 50,
    }


def versions():
    cpu_model = platform.processor()
    if not cpu_model:
        try:
            cpu_model = next((line.split(':', 1)[1].strip()
                              for line in Path('/proc/cpuinfo').read_text().splitlines()
                              if line.startswith('model name')), '')
        except OSError:
            cpu_model = ''
    result = {'python': platform.python_version(), 'platform': platform.platform(),
              'cpu_model': cpu_model, 'logical_cpus': os.cpu_count()}
    for package in ('torch', 'transformers', 'sentence-transformers', 'huggingface-hub', 'sqlite-vec'):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--knowledge-config', type=Path)
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--threads', type=int)
    parser.add_argument('--documents', type=int, default=4)
    parser.add_argument('--cycles', type=int, default=20)
    args = parser.parse_args()

    from local_knowledge.config import Config
    from local_knowledge.embeddings import EmbeddingInitializationError, GemmaProvider

    base = Config.load(args.knowledge_config)
    cache_dir = (args.cache_dir or Path(base.cache_dir)).expanduser().resolve()
    configured = replace(base, cache_dir=str(cache_dir), threads=args.threads or base.threads)
    snapshot_info = check_snapshot(configured)
    root_context = tempfile.TemporaryDirectory(prefix='blackrose-retriever-measure-')
    try:
        root = Path(root_context.name)
        for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
            os.environ[name] = str(root / name)
        # The config path points to an absent cache for one explicit recovery
        # probe. The real configured cache is restored only after offline failure.
        scratch_config = replace(configured, data_dir=str(root / 'real-data'),
                                 database_path=str(root / 'real-data/knowledge.db'))
        recovery_config = replace(scratch_config, cache_dir=str(root / 'empty-model-cache'))
        provider = GemmaProvider(recovery_config)
        baseline = resources()
        failed_at = time.perf_counter()
        try:
            provider.initialize()
        except EmbeddingInitializationError as error:
            offline_failure = {'type': type(error).__name__, 'message': str(error),
                               'elapsed_ms': round((time.perf_counter() - failed_at) * 1000, 3)}
        else:
            raise RuntimeError('Missing-cache fixture unexpectedly initialized a model')
        if provider._model is not None or provider._tokenizer is not None:
            raise RuntimeError('Failed initialization left model/tokenizer state cached')
        provider_state_empty_after_failure = provider._model is None and provider._tokenizer is None
        provider.config.cache_dir = str(cache_dir)
        print('Pinned model cache verified; measuring cached initialization.', flush=True)
        _, recovered_init_ms = measured(provider.initialize)
        identity = provider.identity()
        report = {
            'recorded_at': datetime.now(UTC).isoformat(),
            'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'method': 'one process; local_files_only model access; temporary config/state/data/index paths; no production index or network access',
            'versions': versions(),
            'model_cache': snapshot_info,
            'embedder_identity': identity,
            'configuration_id': provider.get_configuration_id(),
            'configured_threads': configured.threads,
            'missing_cache_recovery': {
                'failure': offline_failure,
                'provider_state_empty_after_failure': provider_state_empty_after_failure,
                'cached_retry_initialization_ms': round(recovered_init_ms, 3),
            },
            'resources': {'before_model_init': baseline, 'after_model_init': resources()},
        }
        print('Measuring real scratch import/search cycles.', flush=True)
        report['real_embedder_index'] = measure_index(
            root / 'real-index', scratch_config, provider, args.cycles, args.documents)
        gc.collect()
        report['resources']['after_real_cycles_gc'] = resources()

        fake_config = replace(configured, data_dir=str(root / 'fake-data'),
                              database_path=str(root / 'fake-data/knowledge.db'))
        fake_provider = DeterministicControl(fake_config.dimensions)
        print('Measuring fake-provider scratch control.', flush=True)
        report['fake_provider_control'] = measure_index(root / 'fake-index', fake_config,
                                                        fake_provider, args.cycles, args.documents)
        print('Measuring real-model serialized service operations.', flush=True)
        report['real_serialized_operations'] = asyncio.run(
            measure_serialized_operations(root / 'real-service', scratch_config, provider))
        print('Measuring fake-provider shutdown settlement.', flush=True)
        report['shutdown_settlement'] = asyncio.run(
            measure_shutdown_settlement(root / 'settlement-service', fake_config, fake_provider))
        report['resources']['after_fake_and_settlement'] = resources()
        report['limitations'] = [
            'The cache was already warm on disk; no OS cache flush was attempted.',
            'RSS values are process-wide and peak RSS is a high-water mark, not an incremental model-only allocation.',
            f'{args.documents} scratch documents and {args.cycles} update cycles were used; results are observations, not performance gates.',
            'The cancelled fake-provider worker uses a deterministic release gate to verify close settlement.',
        ]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(f'Retriever embedder measurements recorded: {args.output}')
    finally:
        root_context.cleanup()


if __name__ == '__main__':
    main()
