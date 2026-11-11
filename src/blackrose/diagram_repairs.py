"""Failure-driven diagram source revisions; inference uses the shared repair path."""
import asyncio
import logging

from .artifacts import settle
from .backend.stream_limits import StreamBounds, StreamBudget
from .diagrams import repair_messages, repaired_source
from .errors import ChatBusy
from .prompts import PromptBuilder
from .repairs import repair_text

log = logging.getLogger(__name__)


class DiagramRepairs:
    def __init__(self, controller):
        self.chat = controller

    async def repair(self, conversation_id, message_id, block_id, index, source, error):
        c = self.chat
        if c.is_generating or c.artifacts.busy:
            raise ChatBusy('Wait for the reply to finish')
        current = c.selected_conversation
        if current is None or current.id != conversation_id:
            return {'status': 'stale'}
        c.is_generating = True
        claim = None
        try:
            async with c.artifacts.exclusive():
                await c.cancel_title()
                if not block_id:
                    # A failure can be queued before a plain reply is promoted to
                    # a diagram block; resolve the block that promotion created.
                    message = await c.conversation_message(conversation_id, message_id)
                    blocks = list(message.blocks) if message is not None else []
                    if len(blocks) == 1 and blocks[0].get('diagram_promoted'):
                        block_id = blocks[0]['block_id']
                claim = await c._repo.claim_diagram_repair(conversation_id, message_id, block_id,
                    index, source, c.config.diagram_repairs)
                if claim['status'] != 'repairing':
                    return claim
                model: dict = next((m for m in c.catalog if m['id'] == c.model_name), {})
                builder = PromptBuilder(c.config, c.skills, c._store, model)
                options = c.backend.request_options(current, c.model_name) if hasattr(c.backend, 'request_options') else {}
                effort = c.effort_for(c.model_name)
                if effort != 'default':
                    options = dict(options, effort=effort)
                bounds = StreamBounds(reply_bytes=min(80000, c.config.stream_reply_bytes),
                    reasoning_bytes=min(128000, c.config.stream_reasoning_bytes),
                    duration_seconds=min(60, c.config.stream_duration_seconds))
                text = await repair_text(c, repair_messages(source, error), options, builder, StreamBudget(bounds))
                replacement = repaired_source(text)
                if replacement.strip() == source.strip():
                    await settle(c._repo.finish_diagram_repair(claim, 'failed'))
                    return {'status': 'failed'}
                await settle(c._repo.finish_diagram_repair(claim, 'updated', replacement))
                return {'status': 'updated'}
        except asyncio.CancelledError:
            if claim and claim['status'] == 'repairing':
                await settle(c._repo.finish_diagram_repair(claim, 'stopped'))
            raise
        except Exception:
            log.warning('Diagram repair did not finish', exc_info=True)
            if claim and claim['status'] == 'repairing':
                await settle(c._repo.finish_diagram_repair(claim, 'failed'))
            return {'status': 'failed'}
        finally:
            c.is_generating = False
