"""Durable interactive-answer policy, owned by the conversation controller.

Session owns ephemeral view tokens and scheduling. This collaborator owns the
persisted identity/runtime gate and validated state/event acceptance, including
settlement if cancellation races an event commit. The repository owns atomicity.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from . import ui_documents as ui
from .artifacts import settle
from .config import Config
from .storage.records import UIBlock
from .ui_documents import UIEnvelope, LIBRARY, continuation, event_id, validate_state


class AnswerRepository(Protocol):
    async def ui_block(self, conversation_id: int, message_id: int, block_id: str) -> UIBlock: ...
    async def save_ui_state(self, block_id: str, sequence: int, state: dict[str, Any]) -> int: ...
    async def accept_ui_event(self, block_id: str, action_id: str, event_id: str,
                              payload: dict[str, Any]) -> None: ...
    async def finish_ui_event(self, event_id: str, status: str, user_message_id: int | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class SavedUIState:
    sequence: int
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AcceptedUIAction:
    event_id: str
    text: str


class InteractiveAnswers:
    def __init__(self, repository: AnswerRepository, config: Config) -> None:
        self._repo = repository
        self._config = config

    async def owned_block(self, envelope: UIEnvelope, conversation_id: int, *,
                          for_action: bool = False, source_only: bool = False) -> UIBlock:
        if envelope.conversation_id != conversation_id:
            raise ValueError('UI event belongs to another conversation')
        block = await self._repo.ui_block(conversation_id, envelope.assistant_message_id, envelope.block_id)
        if block['kind'] != 'interactive_ui' or block['source_revision'] != envelope.source_revision:
            raise ValueError('UI block identity is stale')
        if not source_only:
            if not ui.available():
                raise ValueError('Interactive UI runtime unavailable; rebuild ui-runtime')
            statuses = ('ready',) if for_action else ('ready', 'streaming')
            if (block['status'] not in statuses or block['protocol_version'] != 1
                    or block['library_version'] != LIBRARY or block['schema_hash'] != ui.contract()['schema_hash']):
                raise ValueError('UI document is incomplete, stale or unsupported')
        return block

    async def save_state(self, block: UIBlock, payload: dict[str, Any]) -> SavedUIState:
        sequence = payload.get('state_sequence')
        if type(sequence) is not int or sequence < 1:
            raise ValueError('Invalid state sequence')
        document = block['document']
        if document is None:
            raise ValueError('UI document is incomplete')
        values = validate_state(document, payload.get('values'))
        await self._repo.save_ui_state(block['block_id'], sequence, values)
        return SavedUIState(sequence, values)

    async def accept_action(self, block: UIBlock, payload: dict[str, Any]) -> AcceptedUIAction:
        if not self._config.interactive_ui_actions_enabled:
            raise ValueError('Interactive follow-up actions are disabled')
        if payload.get('action_type') != 'continue_conversation':
            raise ValueError('Unsupported UI action type')
        event = event_id(payload.get('event_id'))
        text = continuation(block, payload)
        accepted = False

        async def persist() -> None:
            nonlocal accepted
            await self._repo.accept_ui_event(block['block_id'], 'submit', event, payload)
            accepted = True

        try:
            await settle(persist())
        except asyncio.CancelledError:
            if accepted:
                await settle(self.finish(event, 'interrupted'))
            raise
        return AcceptedUIAction(event, text)

    async def finish(self, event: str, status: str) -> None:
        if status not in ('complete', 'cancelled', 'error', 'interrupted'):
            raise ValueError('Invalid UI event outcome')
        await self._repo.finish_ui_event(event, status)
