"""Persisted record types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, NotRequired, TypedDict


class UIBlock(TypedDict):
    """Immutable source identity plus replay state; JSON shape stays compatible."""

    block_id: str
    message_id: int
    conversation_id: int
    position: int
    kind: str
    protocol_version: int
    library_version: str
    schema_hash: str
    source_revision: int
    source_sequence: int
    source: str
    source_hash: str
    status: str
    fallback_text: str
    document: dict[str, Any] | None
    error: NotRequired[str | None]
    saved_state: NotRequired[dict[str, Any]]
    state_sequence: NotRequired[int]
    diagram_repairs: NotRequired[dict[str, Any]]
    diagram_promoted: NotRequired[bool]


class MessageState(StrEnum):
    """Lifecycle of a stored message.

    Assistant rows start ``PENDING`` and are resolved when generation ends, so
    a crash mid-stream is distinguishable from a completed answer on restart.
    """

    PENDING = "pending"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"

    @property
    def is_finished(self) -> bool:
        return self is not MessageState.PENDING


class TitleSource(StrEnum):
    """Who named a conversation.

    The distinction exists for one rule: automatic naming may replace a
    placeholder, and may never replace a title the user chose.
    """

    PLACEHOLDER = "placeholder"
    AUTO = "auto"
    MANUAL = "manual"


@dataclass(slots=True)
class Conversation:
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    title_source: TitleSource = TitleSource.AUTO
    workspace: dict | None = None

    @property
    def is_unnamed(self) -> bool:
        """Whether automatic naming is still allowed to name this chat."""
        return self.title_source is TitleSource.PLACEHOLDER


@dataclass(slots=True)
class StoredAttachment:
    """One image, document, or animation persisted alongside a message.

    ``storage_path`` is relative to the attachment store's root; ``filename``
    is the user's own name for the file and is a label only. Keeping them
    apart is what makes a hostile filename harmless.

    ``text_path`` points at a second blob holding a document's extracted
    text. It is ``NULL`` for images, which are replayed as bytes.
    """

    id: int
    message_id: int
    kind: str
    mime_type: str
    filename: str
    storage_path: str
    size: int
    digest: str
    created_at: datetime
    width: int | None = None
    height: int | None = None
    text_path: str | None = None
    pages: int | None = None
    characters: int | None = None
    output_path: str | None = None

    @property
    def is_document(self) -> bool:
        return self.kind == "document"

    @property
    def label(self) -> str:
        """What the composer and the transcript show."""
        if self.width and self.height:
            return f"{self.filename} {self.width}×{self.height}"
        if self.pages:
            return f"{self.filename} {self.pages} page{'s' if self.pages != 1 else ''}"
        return self.filename


@dataclass(slots=True)
class StoredMessage:
    id: int
    conversation_id: int
    role: str
    content: str
    created_at: datetime
    model_used: str | None = None
    state: MessageState = MessageState.COMPLETE
    error: str | None = None
    attachments: list[StoredAttachment] = field(default_factory=list)
    # The user request this answer belongs to.
    reply_to: int | None = None
    refs: list[dict] = field(default_factory=list)
    blocks: list[UIBlock] = field(default_factory=list)
    ui_context: dict | None = None
    audit: dict | None = None

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    @property
    def is_usable_history(self) -> bool:
        """Whether this row should be replayed to a model.

        Empty or failed assistant rows carry no information; a cancelled reply
        with partial text is real conversational context and is kept. A user
        message carrying an image is real context even with no text at all --
        "what is this?" is often the whole question, and sometimes not even
        that gets typed.
        """
        if self.role == "user":
            return bool(self.content.strip()) or self.has_attachments
        if self.state in (MessageState.PENDING, MessageState.ERROR):
            return False
        return bool(self.content.strip()) or self.has_attachments or bool(self.blocks)
