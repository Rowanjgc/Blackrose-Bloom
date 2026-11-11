"""Native Retriever integration, with one lazy CPU embedder per app session."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from local_knowledge.config import Config
from local_knowledge.embeddings import GemmaProvider
from .library import import_capture


class KnowledgeService:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path).expanduser()
        self._config = None
        self._provider = None
        self._worker_lock = threading.Lock()
        self._tasks = set()

    def _run(self, operation: str, arguments: dict):
        # All native work is serialized, including model initialization and writers.
        with self._worker_lock:
            from local_knowledge.retrieval import list_documents, open_document, search  # noqa: PLC0415 - Retriever store loads sqlite-vec and numpy on first use
            if self._config is None:
                self._config = Config.load(self.config_path)
            config = self._config
            if operation in ('search', 'add', 'capture') and self._provider is None:
                self._provider = GemmaProvider(config)
            if operation == 'search':
                return search(config, self._provider, **arguments)
            if operation == 'open':
                return open_document(config, **arguments)
            if operation == 'list':
                return list_documents(config, **arguments)
            if operation == 'capture':
                return import_capture(config, self._provider, arguments['proposal'])
            if operation == 'add':
                from local_knowledge.ingestion import import_file, selected_files  # noqa: PLC0415 - Retriever store loads sqlite-vec and numpy on first use
                result = []
                for path in selected_files(Path(arguments['path']).expanduser()):
                    try:
                        result.append({'path': str(path), **import_file(config, self._provider, path)})
                    except Exception as error:
                        result.append({'path': str(path), 'status': 'failed', 'error': str(error)})
                return {'results': result}
            if operation == 'remove':
                from local_knowledge.ingestion import remove  # noqa: PLC0415 - Retriever store loads sqlite-vec and numpy on first use
                remove(config, arguments['document_id'])
                return {'status': 'removed'}
            raise ValueError('Unknown knowledge operation')

    async def call(self, operation: str, **arguments):
        # Cancellation of a chat must not abandon a writer or destroy a live embedder.
        task = asyncio.create_task(asyncio.to_thread(self._run, operation, arguments))
        self._tasks.add(task)
        task.add_done_callback(self._settle)
        return await asyncio.shield(task)

    def _settle(self, task):
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieve failures even if the awaiting chat was cancelled

    async def close(self):
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
