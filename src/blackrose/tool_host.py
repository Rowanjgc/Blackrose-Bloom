"""The narrow controller surface that tool handlers may use.

ToolExecutor holds a ToolHost, never the ChatController, so a handler reaches only
the capabilities named here. Each member delegates at call time, so the controller
stays the single owner of effects, persistence and configuration. Model-proposed
egress still runs through the controller's outbound policy: handlers can ask for a
web tool or preauthorize a round, but never hold the policy itself.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


class ToolHost:
    __slots__ = ('_controller',)

    def __init__(self, controller: Any):
        self._controller = controller

    # Turn configuration -------------------------------------------------

    def tool_definitions(self, web: bool | None, **flags: Any) -> list[dict]:
        return self._controller.tool_definitions(web, **flags)

    @property
    def automatic_animation_repair(self) -> bool:
        return self._controller.automatic_animation_repair

    @property
    def animations_manual_only(self) -> bool:
        return self._controller.animations_manual_only

    @property
    def knowledge_enabled(self) -> bool:
        return self._controller.config.knowledge_enabled

    @property
    def illustration_drafts_root(self) -> Path:
        return self._controller._store.root / 'illustration-drafts'

    def visual_review_available(self) -> bool:
        return bool(self._controller.config.motion_visual_review
                    and self._controller._supports_vision())

    # Outbound policy ----------------------------------------------------

    @property
    def outbound_active(self) -> bool:
        return getattr(self._controller, 'outbound', None) is not None

    async def preauthorize(self, requests: list[tuple[str, Any]]) -> None:
        await self._controller.outbound.preauthorize(requests)

    # Effects ------------------------------------------------------------

    async def call_tool(self, name: str, arguments: Any) -> dict:
        """Read-only and web tools; web requests are authorized by the outbound policy."""
        return await self._controller._call_tool(name, arguments)

    async def file_tool(self, name: str, arguments: Any, assistant: Any) -> dict:
        return await self._controller.file_tool(name, arguments, assistant)

    async def run_skill_script(self, assistant: Any, arguments: dict) -> dict:
        return await self._controller.run_skill_script(assistant, **arguments)

    async def create_animation(self, assistant: Any, arguments: Any, **kwargs: Any) -> tuple[dict, list]:
        return await self._controller._create_animation(assistant, arguments, **kwargs)

    async def finish_animation_repair(self, conversation_id: int, attachment_id: int,
                                      status: str, replacement_id: int | None = None) -> None:
        await self._controller.animation_recovery.finish(conversation_id, attachment_id,
                                                         status, replacement_id)

    async def review_animation(self, scene: Any, request: Any, plan: Any, builder: Any,
                               options: dict, budget: Any, progress: Callable) -> tuple[Any, dict]:
        from .animation_visual_review import inspect_and_repair  # noqa: PLC0415 - Pillow loads only for animation visual review
        return await inspect_and_repair(self._controller, scene, request, plan, builder,
                                        options, budget, progress)

    async def generate_image(self, assistant: Any, arguments: Any, on_progress: Callable,
                             *, on_start: Callable) -> tuple[dict, list, Any]:
        return await self._controller._generate_image(assistant, arguments, on_progress,
                                                      on_start=on_start)

    def image_recovery_error(self) -> str | None:
        """The local image generator's recovery requirement, if it set one."""
        recovery = getattr(self._controller.local_image_generator, 'recovery_error', None)
        if self._controller.uses_local_images and isinstance(recovery, str) and recovery:
            return recovery
        return None

    async def propose_library_add(self, **kwargs: Any) -> dict:
        return await self._controller.propose_library_add(**kwargs)

    def web_capture(self, result: dict, assistant: Any) -> Any:
        return self._controller.library_proposals.web_capture(result, assistant)

    async def attachments_for(self, message_id: int) -> list:
        return await self._controller._repo.attachments_for(message_id)

    def sources(self, name: str, result: dict) -> list[dict]:
        return self._controller._sources(name, result)
