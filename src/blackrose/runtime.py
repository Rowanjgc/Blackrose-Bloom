"""The desktop's runtime, with native Retriever and no model-selection policy."""
from dataclasses import dataclass

from .chat import ChatController
from .config import Config, data_dir, load_config
from .storage.db import Database


@dataclass
class Runtime:
    controller: ChatController
    database: Database

    async def open(self):
        await self.database.connect()
        await self.controller.retry_artifact_cleanup()
        self.controller.schedule_memory_sync()

    async def close(self):
        try:
            await self.controller.shutdown()
        finally:
            await self.database.close()


def build_runtime(config: Config | None = None):
    from .attachments import AttachmentStore
    from .backend.router import RouterClient
    from .backend.providers import ChatProviders
    from .backend.service import RouterService, router_key
    from .backend.opencode import OpenCodeGoClient
    from .backend.stream_limits import StreamBounds
    from .knowledge import KnowledgeService
    from .image_generation import OpenRouterImages
    from .local_images import LocalImages
    from .memory import HonchoMemory
    from .skills import SkillManager
    from .storage.repository import Repository
    from .web.access import WebAccess
    config = config or load_config()
    database = Database(data_dir() / 'chats.db')
    stream_bounds = StreamBounds(
        reply_bytes=config.stream_reply_bytes,
        reasoning_bytes=config.stream_reasoning_bytes,
        response_bytes=config.stream_response_bytes,
        sse_line_bytes=config.stream_sse_line_bytes,
        duration_seconds=config.stream_duration_seconds,
    )
    service = RouterService(config) if config.router_autostart else None
    backend = RouterClient(config.endpoint, config.request_timeout, service=service,
                           auth=service.key if service else router_key(config),
                           budgets=config.reasoning_budgets, stream_bounds=stream_bounds)
    return Runtime(ChatController(
        backend=ChatProviders(backend, OpenCodeGoClient(timeout=config.request_timeout,
                                                        stream_bounds=stream_bounds)),
        repository=Repository(database), config=config,
        knowledge=KnowledgeService(config.knowledge_config),
        skills=SkillManager(data_dir() / 'skills'),
        web=WebAccess(config.searxng_url),
        attachment_store=AttachmentStore(data_dir() / 'attachments'),
        edit_backups=data_dir() / 'edit-backups',
        image_generator=OpenRouterImages(config.image_model, config.request_timeout)
            if config.image_generation_enabled else None,
        local_image_generator=LocalImages(config.local_image_endpoint,
            config.local_image_workflows, backend,
            config.request_timeout, keep_chat_loaded=config.local_image_keep_chat_loaded)
            if config.image_generation_enabled and config.local_image_enabled else None,
        memory=HonchoMemory(config.memory_endpoint, peer=config.memory_peer)), database)
