"""Direct selected-model chat, native evidence tools, and durable history."""
from __future__ import annotations

import asyncio
import contextlib
import base64
import hashlib
import json
import mimetypes
import logging
import re
import secrets
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from .attachments import (
    ImageInfo, DocumentInfo, inspect_image, inspect_document,
    check_budget, check_document_budget, sniff_type, read_image_file, read_document_file,
    MAX_TOTAL_DOCUMENT_CHARS, TooManyDocuments,
)
from .backend.effort import DEFAULT_ONLY, clamp
from .config import IMAGE_PROVIDERS, save_preferences, save_citation_rigor, save_trusted_hosts
from .storage.records import StoredAttachment
from .storage.repository import NewAttachment
from .tools import KNOWLEDGE_TOOLS, WEB_TOOLS, SKILL_TOOL, activation_tool, LIBRARY_ADD_TOOL, FILE_TOOLS, animation_tools, image_tool
from .animations import MIME_TYPE, encode_scene, load_scene
from .errors import ChatBusy, InputRejected as InputRejected, VisionUnsupported as VisionUnsupported, RecoveryRequired
from .prompts import wire_message
from .turns import Turn as Turn, TurnRunner
from .storage.records import TitleSource
from .storage.repository import DEFAULT_TITLE, derive_title
from .storage.db import _worker
from .artifacts import ArtifactLifecycle, settle
from .titles import TITLE_TIMEOUT, title_messages

log = logging.getLogger(__name__)

# A typed skill token, e.g. $explanatory-animations. Only enabled skill IDs
# are promoted, so shell variables and ordinary dollar amounts stay inert.
# An ID may contain '.', '_' and '-' but must end in a letter or digit, so
# sentence punctuation after a typed token ('Use $thinking-partner.') is not part of it.
SKILL_TOKEN = re.compile(r'(?<![\w$])\$([A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?)')


class PreferenceSaveError(OSError):
    """A requested persistent preference could not be written."""


@dataclass(frozen=True)
class ImageSelection:
    """The user's image provider choice, resolved to something a job can use."""
    kind: str                 # 'local', 'openrouter' or 'off'
    generator: object | None
    model: str | None
    label: str
    paid: bool


@dataclass(frozen=True, slots=True)
class PendingAttachment:
    filename: str
    data: bytes
    kind: str = 'image'
    image: ImageInfo | None = None
    document: DocumentInfo | None = None
    text: str | None = None

    @property
    def is_image(self):
        return self.kind == 'image'

    @property
    def info(self):
        return self.image if self.image is not None else self.document

    @property
    def size(self):
        return self.info.size

    @property
    def label(self):
        return f'{self.filename} {self.image.dimensions if self.image else self.document.summary}'


class ChatController:
    def __init__(self, backend, repository, config, knowledge, skills, web, attachment_store,
                 image_generator=None, local_image_generator=None, edit_backups=None, memory=None):
        self.backend = backend
        self._repo = repository
        self.config = config
        from .edit_backups import EditBackups
        # Beside the attachment store by default: data_dir()/edit-backups.
        self.edit_backups = EditBackups(repository, edit_backups or attachment_store.root.parent / 'edit-backups')
        from .interactive_answers import InteractiveAnswers
        self.interactive_answers = InteractiveAnswers(repository, config)
        from .drafts import ComposerDrafts
        self.drafts = ComposerDrafts(repository)
        self.knowledge = knowledge
        self.skills = skills
        from .skill_execution import ScriptRunner
        self.skill_scripts = ScriptRunner()
        self.web = web
        self._store = attachment_store
        from .animation_recovery import AnimationRecovery
        self.animation_recovery = AnimationRecovery(attachment_store.root / 'animation-recovery.sqlite3')
        from .diagram_repairs import DiagramRepairs
        self.diagram_repairs = DiagramRepairs(self)
        self.artifacts = ArtifactLifecycle(repository, attachment_store, self.animation_recovery)
        self.cleanup_result = {'completed': [], 'preserved': [], 'pending': []}
        self.image_generator = image_generator
        self.local_image_generator = local_image_generator
        # Optional HonchoMemory; used only while config.memory_enabled is on.
        self.memory = memory
        from .memory import MemoryOutbox
        self._memory_outbox = MemoryOutbox(repository, memory, config) if memory is not None else None
        self._memory_task = None
        self._memory_pending = False
        self.model_name = config.default_model
        self.selected_conversation = None
        # Tool calls and child tasks inherit the originating turn's owner.
        # Navigation changes only the selection outside that task context.
        self._turn_conversation = ContextVar('turn_conversation', default=None)
        # Bumped whenever the selection moves to another chat; a metadata refresh
        # of the same chat does not count as navigation for a pending picker.
        self._navigation_epoch = 0
        self._workspace_selection = None
        self._workspace_lock = asyncio.Lock()
        self.is_generating = False
        self.attachments_enabled = True
        self.catalog = []
        self.last_request_stored = False
        # The last send's committed complete turn, kept when Stop lands during its final save.
        self.last_completed_turn = None
        self.pending_edits = {}
        from .library import LibraryProposals
        self.library_proposals = LibraryProposals()
        self._reference_index = None
        self.launch_workspace = None
        self.directory_grants = {}
        # Sites the user allowed for each conversation, in memory only: host -> granted while private.
        self.site_grants = {}
        # The active reply's outbound request policy; web tools are refused without one.
        self.outbound = None
        self._title_task = None

    @property
    def conversation(self):
        return self._turn_conversation.get() or self.selected_conversation

    @conversation.setter
    def conversation(self, value):
        if self._turn_conversation.get() is not None:
            self._turn_conversation.set(value)
            return
        current = self.selected_conversation
        if (current is None) != (value is None) or (current is not None and current.id != value.id):
            # Selecting another chat expires a picker opened beforehand, even if
            # navigation later returns to the chat that picker captured.
            self._navigation_epoch += 1
        self.selected_conversation = value

    def attachment_path(self, attachment: StoredAttachment) -> Path | None:
        """A stored attachment's local path, confined to the attachment store."""
        if self._store is None:
            return None
        try:
            path = self._store.path_for(attachment.storage_path)
            return path if path.is_file() else None
        except (OSError, ValueError):
            return None

    @property
    def web_enabled(self):
        return self.config.web_enabled

    @property
    def outputs_folder(self):
        """The only folder where model output may create files without the user's review."""
        from .workspace import WorkspaceFiles
        return WorkspaceFiles().outputs

    def _set_toggle(self, key, enabled):
        if self.is_generating:
            raise ChatBusy('Wait for the reply to finish')
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        previous = getattr(self.config, key)
        setattr(self.config, key, enabled)
        try:
            save_preferences(self.config)
        except OSError:
            setattr(self.config, key, previous)
            raise

    def set_web_enabled(self, enabled):
        self._set_toggle('web_enabled', enabled)

    def set_citation_rigor(self, mode):
        if self.is_generating:
            raise ChatBusy('Wait for the reply to finish')
        save_citation_rigor(self.config, mode)

    def set_trusted_hosts(self, entries):
        """A reply keeps the trusted sites it started with, so change them between replies."""
        if self.is_generating:
            raise ChatBusy('Wait for the reply to finish')
        save_trusted_hosts(self.config, entries)

    def effort_levels(self, model=None):
        levels = getattr(self.backend, 'reasoning_levels', None)
        return levels(model or self.model_name) if levels else DEFAULT_ONLY

    def effort_for(self, model=None):
        model = model or self.model_name
        return clamp(self.config.reasoning_effort.get(model, 'default'), self.effort_levels(model))

    def set_reasoning_effort(self, level):
        """Allowed mid-reply: a running turn has already taken its snapshot."""
        if level not in self.effort_levels():
            raise ValueError(f'{self.model_name} does not support reasoning effort {level!r}')
        previous = self.config.reasoning_effort
        updated = {k: v for k, v in previous.items() if k != self.model_name}
        if level != 'default':
            updated[self.model_name] = level
        self.config.reasoning_effort = updated
        try:
            save_preferences(self.config)
        except OSError:
            self.config.reasoning_effort = previous
            raise

    def set_knowledge_enabled(self, enabled):
        self._set_toggle('knowledge_enabled', enabled)

    def set_memory_enabled(self, enabled):
        self._set_toggle('memory_enabled', enabled)

    async def recover_interrupted(self):
        """Settle crash leftovers under desktop ownership before publishing history."""
        await self._repo.reap_pending()
        await self.edit_backups.recover()

    async def start(self):
        """Load the selected provider after local startup recovery and replay."""
        self.require_recovered_memory()
        await self.load_model(self.model_name)

    async def load_model(self, model):
        """Reuse load's readiness observation; legacy backends may return None."""
        from .backend.providers import model_provider
        await self.make_room_for_chat(model)
        rows = await self.backend.load(model)
        self.catalog = rows if isinstance(rows, list) else await self.backend.models(model_provider(model))

    async def shutdown(self):
        await self.skill_scripts.close()
        await self.cancel_title()
        if self._memory_task is not None:
            # Unsent rows stay queued for the next start.
            self._memory_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._memory_task
        self.library_proposals.pending.clear()
        try:
            if self.local_image_generator is not None:
                await self.local_image_generator.close()
        finally:
            try:
                await self.backend.aclose()
            finally:
                try:
                    await self.knowledge.close()
                finally:
                    try:
                        await self.web.close()
                    finally:
                        try:
                            if self.image_generator is not None:
                                await self.image_generator.close()
                        finally:
                            if self.memory is not None:
                                await self.memory.aclose()

    async def refresh_models(self, provider=None):
        from .backend.providers import model_provider
        selected_provider = provider or model_provider(self.model_name)
        rows = await self.backend.models(provider=selected_provider)
        self.catalog = [m for m in self.catalog if model_provider(m['id']) != selected_provider] + rows
        return rows

    def set_model_favorite(self, model: str, favorite: bool):
        if not isinstance(model, str) or not model.strip():
            raise ValueError('model must be a nonempty identifier')
        if type(favorite) is not bool:
            raise ValueError('favorite must be a boolean')
        previous = self.config.favorite_models
        models = list(previous) if favorite else [item for item in previous if item != model]
        if favorite and model not in models:
            models.append(model)
        self.config.favorite_models = models
        try:
            save_preferences(self.config)
        except OSError:
            self.config.favorite_models = previous
            raise
        return list(models)

    async def select_model(self, model: str, make_default: bool = False):
        if self.is_generating:
            raise ChatBusy('Wait for the reply to finish')
        self.require_recovered_memory()
        self.is_generating = True
        try:
            await self.cancel_title()
            await self.make_room_for_chat(model)
            await self.backend.load(model)
            self.model_name = model
            if make_default:
                previous_default = self.config.default_model
                self.config.default_model = model
                try:
                    save_preferences(self.config)
                except OSError as error:
                    # Loading a model changes this session immediately. Keep that
                    # selection while restoring the startup default that failed
                    # to persist.
                    self.config.default_model = previous_default
                    raise PreferenceSaveError(str(error)) from error
            await self.refresh_models()
        finally:
            self.is_generating = False

    async def make_room_for_chat(self, model=None):
        """Release an idle warm image model before a chat model may need its memory.

        Runs whatever image provider is selected: a warm model outlives a switch to Off.
        """
        local = self.local_image_generator
        if local is not None and hasattr(local, 'make_room_for_chat'):
            await local.make_room_for_chat(model or self.model_name)
            self.require_recovered_memory()

    def image_round(self, calls):
        """One GPU handoff for a round's several local image calls; a single call needs none."""
        local = self.local_image_generator
        count = sum(call['function']['name'] == 'image_gen' for call in calls)
        if count < 2 or not self.uses_local_images or not hasattr(local, 'batch'):
            return contextlib.nullcontext()
        return local.batch()

    def require_recovered_memory(self):
        recovery = getattr(self.local_image_generator, 'recovery_error', None)
        if isinstance(recovery, str) and recovery:
            raise RecoveryRequired(recovery)

    async def ensure_conversation(self):
        if self.conversation is None:
            items = await self.list_conversations()
            self.conversation = items[0] if items else await self._repo.create_conversation()
            if not items and self.launch_workspace:
                await self.set_workspace(self.launch_workspace['path'])
        return self.conversation

    async def list_conversations(self):
        return await self._repo.list_conversations()

    async def conversation_page(self, **window):
        return await self._repo.conversation_page(**window)

    async def message_evidence(self, message_id):
        return await self._repo.evidence_for(message_id)

    async def messages_evidence(self, message_ids):
        return await self._repo.evidence_for_messages(message_ids)

    async def message_audit(self, message_id):
        return await self._repo.audit_for(message_id)

    async def current_messages(self):
        return await self.conversation_messages(self.conversation.id) if self.conversation else []

    async def conversation_messages(self, conversation_id):
        return await self._repo.messages(conversation_id)

    async def conversation_message_page(self, conversation_id, **window):
        return await self._repo.message_page(conversation_id, **window)

    async def conversation_message_summary(self, conversation_id):
        return await self._repo.message_summary(conversation_id)

    async def conversation_message(self, conversation_id, message_id):
        return await self._repo.message(conversation_id, message_id)

    async def latest_request(self, conversation_id, *, exclude_prefix):
        return await self._repo.latest_request(conversation_id, exclude_prefix=exclude_prefix)

    async def request_was_stored(self, conversation_id, baseline, text, max_chars):
        return await self._repo.request_was_stored(conversation_id, baseline, text, max_chars)

    async def new_conversation(self):
        if self.is_generating or self.artifacts.busy:
            raise ChatBusy('Wait for the reply to finish')
        self.conversation = await self._repo.create_conversation()
        if self.launch_workspace:
            await self.set_workspace(self.launch_workspace['path'])
        return self.conversation

    async def open_conversation(self, conversation_id):
        if self.artifacts.busy and not self.is_generating:
            raise ChatBusy('Wait for the reply to finish')
        conversation = await self._repo.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError('Conversation not found')
        self.conversation = conversation

    async def rename_conversation(self, conversation_id, title):
        title = title.strip()[:120]
        if not title:
            raise ValueError('Title cannot be empty')
        await self._repo.rename_conversation(conversation_id, title)
        if self.conversation and self.conversation.id == conversation_id:
            self.conversation = await self._repo.get_conversation(conversation_id)
        return title

    async def cancel_title(self):
        task = self._title_task
        self._title_task = None
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise

    def schedule_title(self, conversation, request, answer, on_title=None):
        if conversation.is_unnamed:
            self._title_task = asyncio.create_task(
                self._name_conversation(conversation, request, answer, self.model_name, on_title))

    def schedule_memory_sync(self):
        """Send queued memory work in the background; never part of a reply."""
        if self._memory_outbox is None:
            return
        self._memory_pending = True
        if self._memory_task is None or self._memory_task.done():
            self._memory_task = asyncio.create_task(self._sync_memory())

    async def _sync_memory(self):
        from .memory import MemoryUnavailable
        while self._memory_pending:
            self._memory_pending = False
            try:
                await self._memory_outbox.drain()
            except MemoryUnavailable:
                return  # Queued rows wait for the next turn or start.
            except Exception:
                # Never log the exception text: it may quote conversation content.
                log.debug('Memory sync failed; queued turns are kept')
                return

    async def set_memory_excluded(self, excluded):
        """Keep this chat out of memory, forgetting what it already contributed."""
        if type(excluded) is not bool:
            raise ValueError('excluded must be a boolean')
        await self.ensure_conversation()
        await self._repo.set_memory_excluded(self.conversation.id, excluded)
        if excluded:
            self.schedule_memory_sync()

    async def memory_excluded(self):
        return bool(self.conversation) and await self._repo.memory_excluded(self.conversation.id)

    async def forget_memory(self):
        """Forget everything remembered about the user, whether memory is on or off."""
        from .memory import MemoryUnavailable
        if self._memory_outbox is None:
            raise MemoryUnavailable('Memory is not available in this session')
        await self._memory_outbox.forget_everything()

    async def _name_conversation(self, conversation, request, answer, model, on_title):
        try:
            current = await self._repo.get_conversation(conversation.id)
            if current is None or not current.is_unnamed:
                return
            fallback = derive_title(request.content)
            if fallback == DEFAULT_TITLE:
                # Nothing in this exchange can name the chat. Stay a placeholder so a
                # later turn can name it, and do not spend a provider request.
                return
            title = None
            suggest = getattr(self.backend, 'suggest_title', None)
            if suggest is not None:
                options = self.backend.request_options(conversation, model) if hasattr(self.backend, 'request_options') else {}
                try:
                    async with asyncio.timeout(TITLE_TIMEOUT):
                        title = await suggest(model, title_messages(request, answer), **options)
                except Exception:
                    # This optional request must never change a reply's outcome.
                    # Do not log provider diagnostics that may echo chat content.
                    log.debug('Chat naming unavailable; using a local fallback')
            title = title or fallback
            changed = await self._repo.rename_conversation(
                conversation.id, title, source=TitleSource.AUTO, only_if_unnamed=True)
            if changed:
                refreshed = await self._repo.get_conversation(conversation.id)
                if self.selected_conversation and self.selected_conversation.id == conversation.id and refreshed:
                    self.selected_conversation = refreshed
                if on_title:
                    await on_title()
        except Exception:
            log.debug('Could not save the conversation title')

    async def delete_current_conversation(self):
        async with self.artifacts.exclusive():
            return await settle(self._delete_current_conversation())

    async def artifact_report(self):
        async with self.artifacts.exclusive():
            return await self.artifacts.report()

    async def retry_artifact_cleanup(self):
        async with self.artifacts.exclusive():
            self.cleanup_result = await self.artifacts.retry()
            if self.cleanup_result['pending']:
                log.warning('Artifact cleanup has %d pending obligations', len(self.cleanup_result['pending']))
            return self.cleanup_result

    async def repair_artifacts(self, ownership):
        async with self.artifacts.exclusive():
            result = await self.artifacts.repair(ownership)
            self.cleanup_result = result['cleanup']
            return result

    async def _delete_current_conversation(self):
        if self.is_generating:
            raise ChatBusy('Wait for the reply to finish')
        if self.conversation:
            # Select the neighbour in sidebar order, which may lie beyond its first page.
            neighbour = await self._repo.adjacent_conversation(self.conversation.id)
            deleted_id = self.conversation.id
            await self._repo.delete_conversation(deleted_id)
            self.directory_grants.pop(deleted_id, None)
            self.site_grants.pop(deleted_id, None)
            self.pending_edits = {k: v for k, v in self.pending_edits.items() if v['conversation'] != deleted_id}
            self.library_proposals.discard_conversation(deleted_id)
            self.schedule_memory_sync()
            self.cleanup_result = await self.artifacts.retry()
            if self.cleanup_result['pending']:
                log.warning('Conversation deleted with %d pending cleanup obligations',
                            len(self.cleanup_result['pending']))
            self.conversation = neighbour
        return await self.ensure_conversation()

    async def refusing_vision_model(self):
        row = next((m for m in self.catalog if m['id'] == self.model_name), {})
        modalities = row.get('architecture', {}).get('input_modalities')
        if modalities is not None and 'image' not in modalities:
            return self.model_name
        return None

    @staticmethod
    def _attachment_name(filename):
        filename = Path(filename.replace('\\', '/')).name
        return ''.join(c for c in filename if c.isprintable()).strip().strip('.')[:180] or 'attachment'

    def prepare_attachment(self, data: bytes, filename: str):
        filename = self._attachment_name(filename)
        if sniff_type(data) is not None:
            return PendingAttachment(filename, data, image=inspect_image(data, claimed_name=filename))
        text, info = inspect_document(data, claimed_name=filename)
        return PendingAttachment(filename, data, kind='document', document=info, text=text)

    def prepare_attachment_path(self, path):
        path = Path(path).expanduser()
        from .attachments import SUPPORTED_IMAGE_TYPES
        if path.suffix.lower() in {s for suffixes in SUPPORTED_IMAGE_TYPES.values() for s in suffixes}:
            data, info = read_image_file(path)
            return PendingAttachment(self._attachment_name(path.name), data, image=info)
        data, text, info = read_document_file(path)
        return PendingAttachment(self._attachment_name(path.name), data,
                                 kind='document', document=info, text=text)

    def validate_attachment_budget(self, pending, existing):
        if pending.is_image:
            check_budget([a.size for a in existing if a.is_image], pending.size)
        else:
            check_document_budget([a.size for a in existing if not a.is_image], pending.size)
            if sum(len(a.text or '') for a in [*existing, pending] if not a.is_image) > MAX_TOTAL_DOCUMENT_CHARS:
                raise TooManyDocuments(f'Extracted documents may total {MAX_TOTAL_DOCUMENT_CHARS:,} characters')

    def attach(self, data: bytes, filename: str, existing: Sequence[PendingAttachment] = ()):
        pending = self.prepare_attachment(data, filename)
        self.validate_attachment_budget(pending, existing)
        return pending

    def _persist_attachments(self, attachments):
        rows = []
        for pending in attachments:
            info = pending.info
            blob = self._store.write(pending.data, info.suffix)
            text_blob = self._store.write(pending.text.encode(), '.txt') if pending.text is not None else None
            rows.append(NewAttachment(kind=pending.kind, mime_type=info.mime_type,
                        filename=pending.filename, storage_path=blob.relative_path,
                        digest=blob.digest, size=blob.size,
                        width=info.width if pending.is_image else None,
                        height=info.height if pending.is_image else None,
                        text_path=text_blob.relative_path if text_blob else None,
                        pages=info.pages if not pending.is_image else None,
                        characters=info.characters if not pending.is_image else None))
        return rows

    def _wire_message(self, stored):
        return wire_message(stored, self._store, self._supports_vision())

    def _supports_vision(self):
        row = next((m for m in self.catalog if m['id'] == self.model_name), {})
        return 'image' in row.get('architecture', {}).get('input_modalities', [])

    def image_selection(self) -> ImageSelection:
        """The image provider from the saved choice, independent of the chat model.

        Unset means Local when it is available, otherwise Off. An unavailable choice
        resolves to Off; it never falls back to the paid provider.
        """
        config = self.config
        if not config.image_generation_enabled:
            return ImageSelection('off', None, None, 'Image generation disabled', False)
        local = self.local_image_generator
        remote = self.image_generator
        choice = config.image_provider or ('local' if local is not None else 'off')
        if choice == 'local' and local is not None:
            return ImageSelection('local', local, None, 'Local images · Qwen Image 2.1 · no API charge', False)
        if choice == 'openrouter' and remote is not None:
            return ImageSelection('openrouter', remote, config.image_model,
                                  'OpenRouter images · paid', True)
        if choice == 'local':
            return ImageSelection('off', None, None, 'Local images unavailable', False)
        if choice == 'openrouter':
            return ImageSelection('off', None, None, 'OpenRouter images unavailable', False)
        return ImageSelection('off', None, None, 'Image generation off', False)

    @property
    def uses_local_images(self):
        return self.image_selection().kind == 'local'

    @property
    def image_provider_label(self):
        return self.image_selection().label

    def _image_provider(self):
        return self.image_selection().generator

    def set_image_provider(self, provider: str, model: str | None = None):
        """Persist the image provider (and paid model) choice; it applies from the next turn."""
        if not isinstance(provider, str) or provider not in ('', *IMAGE_PROVIDERS):
            raise ValueError('image provider must be empty (automatic), local, openrouter, or off')
        if model is not None and model not in self.config.openrouter_image_models:
            raise ValueError('image model must be one of the configured OpenRouter image models')
        if self.is_generating:
            raise ChatBusy('Wait for the reply to finish')
        previous = (self.config.image_provider, self.config.image_model)
        self.config.image_provider = provider
        if model is not None:
            self.config.image_model = model
        try:
            save_preferences(self.config)
        except (OSError, ValueError) as error:
            self.config.image_provider, self.config.image_model = previous
            raise PreferenceSaveError(str(error)) from error
        return self.image_selection()

    @property
    def animations_available(self):
        """Every provider can author when the user asks; see animations_manual_only."""
        return True

    @property
    def animations_manual_only(self):
        """Local models skip standing animation tools and activation overhead."""
        from .backend.providers import model_provider
        return model_provider(self.model_name) == 'local'

    @property
    def automatic_animation_repair(self):
        """Playback-failure repair is model-initiated, so it stays cloud-only."""
        return not self.animations_manual_only

    def tool_definitions(self, web=None, *, xiaohei=False, animation_ids=None,
                         animations_enabled=False, skills_available=None, library_write_enabled=False,
                         ui_used=False):
        if skills_available is None:
            skills_available = any(s['enabled'] for s in self.skills.list())
        definitions = [SKILL_TOOL] if skills_available else []
        if skills_available and self.skills.has_execution_approval():
            from .tools import SKILL_SCRIPT_TOOL
            definitions.append(SKILL_SCRIPT_TOOL)
        capabilities = []
        if self.config.interactive_ui_enabled and self.config.citation_rigor == 'off' and not ui_used:
            from .ui_documents import available
            if available():
                capabilities.append('interactive_ui')
        if (self.animations_available and not self.animations_manual_only
                and not animations_enabled and not animation_ids):
            capabilities.append('animations')
        if self.config.knowledge_enabled and not library_write_enabled:
            capabilities.append('library_write')
        if capabilities:
            definitions.insert(0, activation_tool(capabilities))
        selection = self.image_selection()
        if selection.generator is not None:
            definitions += [image_tool(selection.kind == 'local', xiaohei, model=selection.model)]
            if xiaohei:
                from .tools import illustration_draft_tool
                definitions.append(illustration_draft_tool())
        if self.config.knowledge_enabled:
            definitions += KNOWLEDGE_TOOLS
            if library_write_enabled:
                definitions.append(LIBRARY_ADD_TOOL)
        if self.web_enabled if web is None else web:
            definitions += WEB_TOOLS
        animations = (animation_tools(animation_ids, plan_first=self.config.motion_plan_first)
                      if self.animations_available and (animations_enabled
                          or (animation_ids and not self.animations_manual_only)) else [])
        return [*definitions, *animations, *FILE_TOOLS]

    async def _call_tool(self, name, arguments, origin='assistant'):
        if name == 'animation_read':
            attachment, scene = await self.read_animation(arguments['attachment_id'])
            return {'attachment_id': attachment.id,
                    'scene': {k: v for k, v in scene.items() if k != 'compiled'}}
        if name in ('knowledge_search', 'knowledge_open', 'knowledge_list'):
            await self.expose_local_data()
            return await self.knowledge.call(name.removeprefix('knowledge_'), **arguments)
        if name == 'read_skill':
            return await asyncio.to_thread(self.skills.read, **arguments)
        if name in ('web_search', 'fetch_content'):
            return await self._outbound(name, arguments, origin)
        raise ValueError('Unknown tool')

    async def run_skill_script(self, assistant, skill_id, entry_point, input):
        from .skill_execution import validate_artifact
        if self.conversation is None or self.conversation.id != assistant.conversation_id:
            raise ValueError('Skill execution requires its originating conversation')
        files, entry = await asyncio.to_thread(self.skills.prepare_execution, skill_id, entry_point)
        # A script may disclose private packaged resources in its output. Preserve
        # the same durable egress guard used for other local-data tools.
        await self.expose_local_data()
        outcome = await self.skill_scripts.run(files, entry, input)
        artifact = validate_artifact(outcome['result'])
        if artifact is not None:
            workspace = await self.workspace_files()
            # Only the host-selected output folder accepts generated text. The
            # script has no destination/overwrite capability or filesystem mount.
            outcome['artifact'] = await self._save_output(
                workspace, assistant, str(workspace.outputs / artifact['filename']),
                artifact['content'].encode('utf-8'))
        return {'skill_id': skill_id, 'entry_point': entry_point, **outcome}

    async def expose_local_data(self):
        """Guard outbound requests before local data reaches the model, in this reply and later ones.

        The durable mark is written first, so a reply stopped, failed or pruned
        from context afterwards cannot return the conversation to automatic searches.
        """
        if self.outbound is not None and self.outbound.private:
            return  # The reply began private, which is already recorded.
        if self.conversation is not None:
            await self._repo.mark_local_data_exposed(self.conversation.id)
        if self.outbound is not None:
            self.outbound.mark_private()

    async def _outbound(self, name, arguments, origin):
        """The only path to WebAccess: authorize the exact request before sending it."""
        from .outbound import SITE_GRANT, TRUSTED_SITE, OutboundDenied, OutboundRequest
        policy = self.outbound
        if policy is None:
            raise OutboundDenied('no_active_reply', 'Web requests run only during a reply. Nothing was sent.',
                OutboundRequest('search' if name == 'web_search' else 'fetch',
                                str(arguments.get('query') or arguments.get('url')), origin, ''))
        basis = await policy.authorize(name, arguments, origin)
        if name == 'web_search':
            result = await self.web.search(**arguments)
            policy.note_results(result)
        elif basis in (SITE_GRANT, TRUSTED_SITE):
            # The model chose this address on the strength of the site alone, so an open
            # redirect there must not carry it elsewhere.
            result = await self.web.fetch(**arguments, redirect_allowed=policy.stays_allowed)
            policy.note_links(arguments['url'], result)
        else:
            result = await self.web.fetch(**arguments)
            policy.note_links(arguments['url'], result)
        return {**result, 'authorization': basis}

    async def begin_workspace_selection(self):
        if self.is_generating:
            raise ChatBusy('Wait for the reply to finish')
        conversation = await self.ensure_conversation()
        self._workspace_selection = (object(), conversation, self._navigation_epoch)
        return self._workspace_selection

    def _check_workspace_selection(self, selection):
        # The conversation is compared by id, not by object: an automatic title
        # rename refreshes the selected object for the same chat, which must not
        # expire a picker the user already opened. The epoch still expires a chat
        # that was left, even one navigated back to.
        current = self.conversation
        if (selection is not self._workspace_selection
                or selection[2] != self._navigation_epoch
                or current is None or current.id != selection[1].id
                or self.is_generating):
            raise ChatBusy('Workspace selection expired; choose the directory again')

    async def set_workspace(self, path, *, selection=None):
        from .workspace import authorize_directory
        if selection is None:
            selection = await self.begin_workspace_selection()
        async with self._workspace_lock:
            self._check_workspace_selection(selection)
            conversation_id = selection[1].id
            grant = await asyncio.to_thread(authorize_directory, path) if path else None
            self._check_workspace_selection(selection)
            # Always persist to the captured owner, even if navigation occurs
            # while the database worker is running.
            await self._repo.set_workspace(conversation_id, grant)
            conversation = await self._repo.get_conversation(conversation_id)
            self.pending_edits = {k: v for k, v in self.pending_edits.items()
                                  if v['conversation'] != conversation_id}
            self.directory_grants.pop(conversation_id, None)
            self._check_workspace_selection(selection)
            if conversation is None:
                raise ChatBusy('Workspace selection expired; choose the directory again')
            self.conversation = conversation
            self._reference_index = None
            return grant

    async def configure_launch_workspace(self, path):
        """A CLI directory is explicit authorization, also used for new chats."""
        await self.set_workspace(path)
        self.launch_workspace = self.conversation.workspace

    async def directory_access_requests(self, text):
        from .workspace import mentioned_directories
        await self.ensure_conversation()
        files = await self.workspace_files()
        requests = []
        for grant in await asyncio.to_thread(mentioned_directories, text):
            try:
                await asyncio.to_thread(files.metadata, grant['path'])
            except (OSError, ValueError):
                requests.append(grant)
        return requests

    async def approve_directories(self, conversation_id, grants):
        from .workspace import authorize_directory
        if self.is_generating or not self.conversation or self.conversation.id != conversation_id:
            raise ChatBusy('Conversation changed while directory access was requested; send again')
        workspace = self.conversation.workspace
        for grant in grants:
            current = await asyncio.to_thread(authorize_directory, grant['path'])
            if current != grant:
                raise ValueError('Directory changed while access was requested; send again')
        if (self.is_generating or not self.conversation or self.conversation.id != conversation_id
                or self.conversation.workspace != workspace):
            raise ChatBusy('Conversation changed while directory access was requested; send again')
        approved = self.directory_grants.setdefault(conversation_id, {})
        approved.update((g['path'], g) for g in grants)

    async def workspace_files(self):
        from .workspace import WorkspaceFiles
        grants = self.directory_grants.get(self.conversation.id, {}) if self.conversation else {}
        files = WorkspaceFiles(self.conversation.workspace if self.conversation else None,
                               additional=grants.values(), backups=self.edit_backups.root,
                               backup_group=self.conversation.id if self.conversation else 'unassigned')
        files.readable = {a.output_path for m in await self.current_messages()
                          for a in m.attachments if a.output_path}
        return files

    async def resolve_references(self, refs):
        if not isinstance(refs, (list, tuple)) or len(refs) > 20:
            raise ValueError('At most 20 references per message')
        files = await self.workspace_files()
        result = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError('Invalid reference')
            if ref.get('kind') == 'skill':
                key = ref.get('id')
                # Resolve enabled package and instructions before accepting the turn.
                await asyncio.to_thread(self.skills.read, key)
                result.append({'kind': 'skill', 'id': key})
            elif ref.get('kind') in ('file', 'directory'):
                meta = await asyncio.to_thread(files.metadata, ref.get('path'))
                if meta['identity'] != ref.get('identity') or meta['kind'] != ref['kind']:
                    raise ValueError('Referenced file changed; select it again')
                result.append({k: meta[k] for k in ('path', 'kind', 'identity', 'filename')})
            else:
                raise ValueError('Unknown reference kind')
        return result

    async def resolve_skill_tokens(self, text, refs):
        """Promote literal ``$skill-id`` text to the reference a chip would add.

        The composer only stores a ref when a skill is picked from the ``$``
        menu. A typed token looked identical in the message but left ``refs``
        empty, which withheld host contracts such as local animation tools.
        """
        enabled = {s['id'] for s in await asyncio.to_thread(self.skills.list) if s['enabled']}
        selected = {(ref.get('kind'), ref.get('id')) for ref in refs}
        found = []
        for match in SKILL_TOKEN.finditer(text):
            skill_id = match.group(1)
            if skill_id in enabled and ('skill', skill_id) not in selected:
                selected.add(('skill', skill_id))
                found.append({'kind': 'skill', 'id': skill_id})
        if not found:
            return list(refs)
        return [*refs, *(await self.resolve_references(found))]

    async def reference_suggestions(self, trigger, query):
        return (await self.reference_suggestion_details(trigger, query))['items']

    async def reference_suggestion_details(self, trigger, query):
        from .workspace import fuzzy_score
        if trigger == '$':
            items = await asyncio.to_thread(self.skills.list)
            items = [dict(s, kind='skill') for s in items if s['enabled']]
            matches = sorted([s for s in items if fuzzy_score(query, s['id'] + ' ' + s['name'])],
                             key=lambda s: -fuzzy_score(query, s['id'] + ' ' + s['name']))[:30]
            return {'items': matches, 'status': 'ready', 'scan_truncated': False, 'message': ''}

        def current_context_key():
            conversation = self.conversation
            if conversation is None:
                return None
            grants = self.directory_grants.get(conversation.id, {})
            return (conversation.id, json.dumps(conversation.workspace, sort_keys=True),
                    tuple(sorted(json.dumps(grant, sort_keys=True) for grant in grants.values())))

        files = await self.workspace_files()
        conversation_id = self.conversation.id if self.conversation else None
        context_key = current_context_key()
        files_key = (conversation_id, json.dumps(files.workspace, sort_keys=True),
                     tuple(sorted(json.dumps(grant, sort_keys=True) for grant in files.additional)))
        if context_key is None or context_key != files_key:
            return {'items': [], 'status': 'unavailable', 'scan_truncated': False,
                    'message': 'Workspace access changed. Try the reference again.'}
        if not files.workspace:
            items = []
            for path in files.readable:
                try:
                    items.append(await asyncio.to_thread(files.metadata, path))
                except (OSError, ValueError) as error:
                    # Bounded discovery skips unreadable entries; keep the class only.
                    log.debug('Skipped unreadable file candidate (%s)', type(error).__name__)
            matches = [m for m in items if fuzzy_score(query, m['path'])][:30]
            if current_context_key() != context_key:
                return {'items': [], 'status': 'unavailable', 'scan_truncated': False,
                        'message': 'Workspace access changed. Try the reference again.'}
            return {'items': matches, 'status': 'no_workspace' if not matches else 'ready',
                    'scan_truncated': False,
                    'message': 'No workspace selected. Only saved outputs are available.' if not matches else ''}

        key = context_key
        cached = self._reference_index
        try:
            # Cached metadata remains subject to the live directory identity check.
            await asyncio.to_thread(files.metadata, '.')
            if cached is None or cached[0] != key or time.monotonic() - cached[1] > 5:
                result = await asyncio.to_thread(files.browse, '.', recursive=True, limit=2000)
                cached = (key, time.monotonic(), result['entries'], result['scan_truncated'])
        except (OSError, ValueError):
            return {'items': [], 'status': 'unavailable', 'scan_truncated': False,
                    'message': 'Workspace is unavailable. Choose it again to restore suggestions.'}
        if current_context_key() != context_key:
            return {'items': [], 'status': 'unavailable', 'scan_truncated': False,
                    'message': 'Workspace access changed. Try the reference again.'}
        self._reference_index = cached
        items = {m['path']: m for m in cached[2]}
        recent_paths = list(files.readable)
        for message in (await self.current_messages())[-40:]:
            recent_paths.extend(r['path'] for r in message.refs if r['kind'] in ('file', 'directory'))
        for path in dict.fromkeys(recent_paths):
            if path not in items:
                try:
                    items[path] = await asyncio.to_thread(files.metadata, path)
                except (OSError, ValueError) as error:
                    log.debug('Skipped stale recent file (%s)', type(error).__name__)
        # If bounded discovery stopped before the requested entry, resolve one
        # exact workspace-relative path directly. This is O(path depth), stays
        # under the current grant, and does not expand the recursive scan.
        if cached[3] and query:
            try:
                direct = await asyncio.to_thread(files.direct_reference, query)
                items[direct['path']] = direct
            except (OSError, ValueError, KeyError, TypeError) as error:
                log.debug('Could not resolve typed file reference (%s)', type(error).__name__)
        if current_context_key() != context_key:
            return {'items': [], 'status': 'unavailable', 'scan_truncated': False,
                    'message': 'Workspace access changed. Try the reference again.'}
        try:
            # Revalidate after any background scan and message lookup, so a
            # workspace replacement during this request cannot return cached
            # paths under the old directory identity.
            await asyncio.to_thread(files.metadata, '.')
        except (OSError, ValueError):
            return {'items': [], 'status': 'unavailable', 'scan_truncated': False,
                    'message': 'Workspace is unavailable. Choose it again to restore suggestions.'}
        if current_context_key() != context_key:
            return {'items': [], 'status': 'unavailable', 'scan_truncated': False,
                    'message': 'Workspace access changed. Try the reference again.'}
        matches = sorted([m for m in items.values() if fuzzy_score(query, m['path'])],
                         key=lambda m: (not Path(m['path']).is_relative_to(files.workspace['path']),
                                        -fuzzy_score(query, m['path']), m['path'] not in recent_paths))[:30]
        truncated = bool(cached[3])
        if truncated:
            message = 'Showing a bounded set of workspace files. Type the full relative path as @{folder/file name} to find an omitted file.'
            status = 'incomplete'
        else:
            message = '' if matches else 'No matching files.'
            status = 'ready' if matches else 'no_matches'
        return {'items': matches, 'status': status, 'scan_truncated': truncated, 'message': message}

    async def file_tool(self, name, arguments, assistant):
        from .documents import render_pdf
        if name not in ('directory_create', 'document_create'):
            # Local file names and contents are private; later model-written searches need approval.
            await self.expose_local_data()
        files = await self.workspace_files()
        args = dict(arguments)
        if name == 'file_list':
            return await asyncio.to_thread(files.browse, **args)
        if name == 'file_search':
            return await asyncio.to_thread(files.browse, recursive=True, **args)
        if name == 'file_info':
            return await asyncio.to_thread(files.metadata, **args)
        if name == 'file_read':
            return await asyncio.to_thread(files.read, **args)
        if name == 'directory_create':
            from .workspace import FileAccessError
            try:
                return await asyncio.to_thread(files.mkdir, **args)
            except FileAccessError as error:
                if error.code != 'review_required':
                    raise
                raise FileAccessError('review_required', f'Folders outside {files.outputs} are created only '
                                      'by an approved new file; propose the file with document_create') from None
        path = args['path']
        content = args['content']
        if len(content.encode('utf-8')) > 256000:
            raise ValueError('Content exceeds 256 KB')
        if name == 'file_edit':
            if len(self.pending_edits) >= 8:
                raise ValueError('Review pending edits before requesting more')
            old = await asyncio.to_thread(files.read_bytes, path, 256000)
            # Only UTF-8 text replacement; binary/PDF revisions create a new artifact.
            old_text = old.decode('utf-8')
            if b'\x00' in old or Path(path).suffix.lower() in ('.pdf', '.docx'):
                raise ValueError('Binary documents require a new output file')
            token = secrets.token_hex(16)
            self.pending_edits[token] = {'conversation': self.conversation.id,
                'workspace': self.conversation.workspace, 'assistant': assistant,
                'path': path, 'data': content.encode(), 'old': old_text,
                'digest': hashlib.sha256(old).hexdigest()}
            return {'approval_required': True, 'approval_id': token, 'path': path, 'operation': 'edit',
                    'notice': 'No file was changed. User must review and approve this edit.'}
        suffix = Path(path).suffix.lower()
        if suffix == '.pdf':
            data = await asyncio.to_thread(render_pdf, content)
        else:
            if suffix not in {'.md', '.txt', '.html', '.htm', '.json', '.yaml', '.yml', '.csv',
                              '.py', '.sh', '.js', '.ts', '.css', '.xml', '.toml', '.sql', '.rst'}:
                raise ValueError('Choose a supported document or script extension')
            if suffix == '.json':
                json.loads(content)
            data = content.encode('utf-8')
        from .workspace import FileAccessError
        try:
            return await self._save_output(files, assistant, path, data)
        except FileAccessError as error:
            if error.code != 'review_required':
                raise
        # Outside the app-owned output folder, a new file is a proposal bound to this
        # conversation, workspace, destination and content. Nothing is written yet.
        if len(self.pending_edits) >= 8:
            raise ValueError('Review pending edits before requesting more')
        destination = await asyncio.to_thread(files.destination, path)
        token = secrets.token_hex(16)
        self.pending_edits[token] = {'operation': 'create', 'conversation': self.conversation.id,
            'workspace': self.conversation.workspace, 'assistant': assistant,
            'path': destination, 'data': data, 'text': content, 'old': '', 'digest': None}
        return {'approval_required': True, 'approval_id': token, 'path': destination, 'operation': 'create',
                'notice': f'No file was created. The user must review and approve this new file; '
                          f'only {files.outputs} accepts new files without review.'}

    async def _save_output(self, files, assistant, path, data, **options):
        # Once writing starts, settle both disk and attachment persistence before
        # propagating cancellation, preserving completed outputs in interrupted replies.
        async def save():
            from .config import write_json
            # A pending record survives controller/process restart. Only metadata is
            # retried: never replay the external write or replace an edited output.
            identity = json.dumps([assistant.conversation_id, path,
                hashlib.sha256(data).hexdigest(), options], sort_keys=True)
            key = hashlib.sha256(identity.encode()).hexdigest()
            journal = self._store.root / 'output-recovery' / f'{key}.json'
            if journal.exists():
                record = await asyncio.to_thread(lambda: json.loads(journal.read_text()))
                result = record['result']
                if record['workspace'] != files.workspace:
                    raise ValueError('Output recovery requires its original workspace')
                files.readable.add(result['path'])
                current = await asyncio.to_thread(files.read_bytes, result['path'])
                if current != data:
                    raise ValueError(f"Saved output changed; metadata recovery stopped: {result['path']}")
                owner = record['assistant_id']
            else:
                result = await asyncio.to_thread(files.write, path, data, **options)
                owner = assistant.id
            self._reference_index = None
            output = result['path']
            try:
                await asyncio.to_thread(write_json, journal, {
                    'result': result, 'assistant_id': owner, 'workspace': files.workspace,
                    'conversation_id': assistant.conversation_id, 'requested_path': path,
                    'options': options, 'digest': hashlib.sha256(data).hexdigest()})
                existing = await self._repo.attachments_for(owner)
                attachment = next((a for a in existing if a.output_path == output
                    and a.digest == hashlib.sha256(data).hexdigest()), None)
                if attachment is None:
                    blob = await _worker(lambda: self._store.write(data, Path(output).suffix))
                    new = NewAttachment(kind='artifact',
                        mime_type=mimetypes.guess_type(output)[0] or 'text/plain',
                        filename=Path(output).name, storage_path=blob.relative_path,
                        digest=blob.digest, size=blob.size, output_path=output)
                    saved = await self._repo.append_attachments(owner, [new])
                    attachment = saved[-1]
                await asyncio.to_thread(journal.unlink, missing_ok=True)
            except Exception as error:
                return {**result, 'filename': Path(output).name,
                    'artifact_pending': True, 'recovery_id': key if journal.exists() else None,
                    'warning': str(error),
                    'notice': 'Saved on disk at ' + output +
                        '; conversation artifact could not be saved. Do not create another copy. ' +
                        ('Retry the same output request to recover metadata without rewriting the file.'
                         if journal.exists() else
                         'Recovery record unavailable; locate the saved file directly before retrying.')}
            return {**result, 'attachment_id': attachment.id, 'filename': Path(output).name,
                    'notice': 'Saved on disk and displayed as a conversation artifact.'}
        saving = asyncio.create_task(save())
        cancelled = False
        while not saving.done():
            try:
                await asyncio.shield(saving)
            except asyncio.CancelledError:
                cancelled = True
        result = saving.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def recover_output(self, recovery_id):
        """Retry one saved output's metadata after restart, without a disk write."""
        if (not isinstance(recovery_id, str) or len(recovery_id) != 64
                or any(c not in '0123456789abcdef' for c in recovery_id)):
            raise ValueError('Invalid output recovery ID')
        async with self.artifacts.exclusive():
            journal = self._store.root / 'output-recovery' / f'{recovery_id}.json'
            record = json.loads(await asyncio.to_thread(journal.read_text))
            if not self.conversation or record['conversation_id'] != self.conversation.id:
                raise ValueError('Open the original conversation to recover its output')
            files = await self.workspace_files()
            if files.workspace != record['workspace']:
                raise ValueError('Output recovery requires its original workspace')
            files.readable.add(record['result']['path'])
            data = await asyncio.to_thread(files.read_bytes, record['result']['path'])
            if hashlib.sha256(data).hexdigest() != record['digest']:
                raise ValueError('Saved output changed; metadata recovery stopped')
            assistant = next(m for m in await self.current_messages()
                             if m.id == record['assistant_id'])
            return await self._save_output(files, assistant, record['requested_path'],
                                           data, **record['options'])

    async def review_edit(self, token, approve=False, reject=False):
        if approve and not reject:
            async with self.artifacts.exclusive():
                return await self._review_edit(token, approve, reject)
        return await self._review_edit(token, approve, reject)

    async def _review_edit(self, token, approve=False, reject=False):
        pending = self.pending_edits.get(token)
        if not pending or not self.conversation or pending['conversation'] != self.conversation.id:
            raise ValueError('Edit approval expired')
        if self.conversation.workspace != pending['workspace']:
            raise ValueError('Workspace changed; request a new edit')
        if reject:
            self.pending_edits.pop(token)
            return {'rejected': True}
        create = pending.get('operation') == 'create'
        if not approve:
            from .edit_review import compare
            # A proposed PDF is reviewed as the Markdown it was rendered from.
            after = pending['text'] if create else pending['data'].decode()
            return {'path': pending['path'], 'before': pending['old'], 'after': after, 'approval_id': token,
                    'operation': 'create' if create else 'edit',
                    'comparison': await asyncio.to_thread(compare, pending['old'], after)}
        if self.is_generating:
            raise ChatBusy('Wait for the reply to finish before approving')
        self.is_generating = True
        try:
            from .workspace import FileAccessError
            files = await self.workspace_files()
            try:
                if create:
                    # A new file is refused, not renamed or replaced, if one appeared since review.
                    result = await self._save_output(files, pending['assistant'], pending['path'], pending['data'],
                                                    reviewed=True, collision='error')
                else:
                    # The displaced original moves to the private backup store under a durable record.
                    original = (await asyncio.to_thread(files.metadata, pending['path']))['path']
                    result = await self.edit_backups.replace(
                        self.conversation.id, original, token, pending['digest'],
                        hashlib.sha256(pending['data']).hexdigest(),
                        lambda backup_id: self._save_output(
                            files, pending['assistant'], pending['path'], pending['data'], reviewed=True,
                            collision='replace', expected_digest=pending['digest'], backup_id=backup_id))
            except FileAccessError as error:
                if error.code == 'file_changed_published':
                    self.pending_edits.pop(token, None)
                raise
            self.pending_edits.pop(token, None)
            return result
        finally:
            self.is_generating = False

    async def edit_backup_action(self, action, backup_id=None):
        """List, preview, restore or remove this conversation's edit backups, or apply retention."""
        if action == 'list':
            return await self.edit_backups.listing(self.conversation)
        if action == 'preview':
            return await self.edit_backups.preview(self.conversation, backup_id, await self.workspace_files())
        if action not in ('restore', 'remove', 'prune'):
            raise ValueError('Unknown backup action')
        async with self.artifacts.exclusive():
            if self.is_generating:
                raise ChatBusy('Wait for the reply to finish')
            if action == 'remove':
                return await self.edit_backups.remove(self.conversation, backup_id)
            if action == 'prune':
                return await self.edit_backups.prune()
            return await self._restore_edit_backup(backup_id)

    async def _restore_edit_backup(self, backup_id):
        """Bring back an edit's exact original under the edit's own guards.

        The backup must belong to this conversation and still match its record, the
        current grants must authorize the file, and the file must still hold the
        approved replacement. The version it replaces becomes a backup in turn.
        """
        from .workspace import FileAccessError
        record = await self.edit_backups.owned(self.conversation, backup_id)
        files = await self.workspace_files()
        data = await self.edit_backups.read(record, files)
        path = record['original_path']
        self.is_generating = True
        try:
            result = await self.edit_backups.replace(
                self.conversation.id, path, 'restore:' + backup_id, record['published_digest'], record['digest'],
                lambda new_id: _worker(lambda: files.write(path, data, collision='replace', reviewed=True,
                    expected_digest=record['published_digest'], backup_id=new_id)))
        except FileAccessError as error:
            if error.code == 'file_changed':
                raise FileAccessError('file_changed', f'{path} changed after this edit; restoring would '
                                      'discard the newer content. The backup remains available.') from None
            if error.code == 'file_changed_published':
                await self._repo.mark_edit_backup_restored(backup_id)
            raise
        finally:
            self.is_generating = False
            self._reference_index = None
        await self._repo.mark_edit_backup_restored(backup_id)
        return result

    async def propose_library_add(self, source_ref, assistant, web_captures, title=None, tags=()):
        if not self.config.knowledge_enabled:
            raise ValueError('Local knowledge is disabled')
        if not self.conversation or self.conversation.id != assistant.conversation_id:
            raise ValueError('Conversation changed')
        capture = web_captures.get(source_ref)
        if capture is None and source_ref.startswith('artifact:'):
            try:
                attachment_id = int(source_ref.removeprefix('artifact:'))
            except ValueError:
                raise ValueError('Invalid artifact reference') from None
            matches = [(m, a) for m in await self.current_messages() if m.role == 'assistant'
                       for a in m.attachments if a.id == attachment_id and a.kind == 'artifact']
            if not matches:
                raise ValueError('Saved assistant artifact not found in this conversation')
            message, attachment = matches[0]
            def prepare():
                data = self._store.read(attachment.storage_path)
                if data is None:
                    raise ValueError('Saved artifact is missing')
                return self.library_proposals.artifact_capture(attachment, data, message)
            capture = await asyncio.to_thread(prepare)
        if capture is None:
            raise ValueError('Unknown source reference; fetch a page or create an artifact first')
        return self.library_proposals.propose(assistant.conversation_id, capture, title, tags)

    async def review_library_add(self, token, approve=False, reject=False):
        if not self.conversation:
            raise ValueError('Library approval expired')
        proposal = self.library_proposals.get(token, self.conversation.id)
        if approve or reject:
            if self.is_generating:
                raise ChatBusy('Wait for the reply or library indexing to finish')
        if reject:
            del self.library_proposals.pending[token]
            return {'status': 'rejected'}
        if not approve:
            return proposal.review(token, include_text=True)
        if not self.config.knowledge_enabled:
            raise ValueError('Local knowledge is disabled')
        self.is_generating = True
        try:
            # Settle both the writer and proposal consumption even on cancellation.
            async def commit():
                result = await self.knowledge.call('capture', proposal=proposal)
                self.library_proposals.pending.pop(token, None)
                return {**result, 'title': proposal.title}
            task = asyncio.create_task(commit())
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            result = task.result()
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            self.is_generating = False

    async def read_animation(self, attachment_id):
        attachment = next((a for m in await self.current_messages() for a in m.attachments
                           if a.id == attachment_id and a.kind == 'animation'), None)
        if attachment is None:
            raise ValueError('Animation must be an attachment in this conversation')
        path = self.attachment_path(attachment)
        if path is None:
            raise ValueError('Animation is missing from storage')
        return attachment, await asyncio.to_thread(load_scene, path)

    async def _create_animation(self, assistant, arguments, on_progress=None, *, expected_duration=None,
                                visual_review=None, plan=None):
        started = time.monotonic()
        motion = {}
        data = await asyncio.to_thread(encode_scene, arguments['scene'], motion=motion, plan=plan)
        scene = json.loads(data)
        if expected_duration is not None and abs(scene['duration'] - expected_duration) > 0.1:
            raise ValueError('Rendered duration differs from the planned timeline; use direct generation')
        if 'revises_attachment_id' in arguments:
            try:
                await self.read_animation(arguments['revises_attachment_id'])
            except ValueError as error:
                raise ValueError(f'Invalid revises_attachment_id: {error}. '
                    'For a NEW animation omit revises_attachment_id entirely. '
                    'For a revision read an existing conversation animation ID. '
                    'A skill example is a template, not a saved attachment.') from error
        # Bound per-turn artifact work independently of the scene's complexity.
        existing = await self._repo.attachments_for(assistant.id)
        if sum(a.kind == 'animation' for a in existing) >= 4:
            raise ValueError('At most four animations per reply; revise in the next message')
        review = None
        if visual_review is not None and scene.get('engine') == 'motion-canvas':
            scene, review = await visual_review(scene)
            data = json.dumps(scene, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            if review.get('repaired'):
                # The element inventory came from the pre-repair source.
                motion['elements'] = {'assessment': 'unknown', 'issues': [], 'checked': 0,
                                      'stale': True}
            if review.get('assessment') == 'pass':
                # A passing review supersedes the coarse pre-review motion hint.
                motion['reviewed'] = True
                motion['notice'] = None
        if on_progress:
            on_progress('saving')
        blob = await _worker(lambda: self._store.write(data, '.animation.json'))
        attachment = NewAttachment(kind='animation', mime_type=MIME_TYPE,
            filename=arguments['scene']['title'] + '.animation.json',
            storage_path=blob.relative_path, digest=blob.digest, size=blob.size,
            width=960, height=540)
        saved = await self._repo.append_attachments(assistant.id, [attachment])
        validation = {'playback': 'passed' if scene.get('engine') == 'motion-canvas' else 'legacy',
                      'visual_quality': 'unreviewed'}
        if scene.get('engine') == 'motion-canvas':
            validation['motion'] = motion.get('assessment', 'unknown')
        result = {'attachment_id': saved[-1].id, 'title': arguments['scene']['title'],
                  'duration': scene['duration'],
                  'loop': scene.get('loop', False),
                  'validation': validation,
                  'notice': 'Finished: animation saved and displayed with playback controls. '
                            'Return a brief final answer now. The user starts playback; the player '
                            'handles looping. Do not call animation_create again to play or repeat it.'}
        if scene.get('engine') == 'motion-canvas':
            result['motion'] = motion
            if motion.get('notice'):
                result['notice'] += ' ' + motion['notice'] + ' Tell the user.'
        if 'revises_attachment_id' in arguments:
            result['revises_attachment_id'] = arguments['revises_attachment_id']
        if review is not None:
            review['timings']['creation_seconds'] = time.monotonic() - started
            result['visual_review'] = review
            result['validation']['visual_quality'] = review['assessment']
            if review['assessment'] != 'pass':
                result['notice'] += ' Visual review did not establish a pass; tell the user and report its findings.'
        return result, saved

    async def _generate_image(self, assistant, arguments, on_progress=None, *, on_start=None):
        references = []
        names = {}
        available = {a.id: a for m in await self.current_messages() for a in m.attachments
                     if a.kind == 'image'}
        for attachment_id in arguments.get('reference_attachment_ids', []):
            attachment = available.get(attachment_id)
            if attachment is None:
                raise ValueError('Reference image must be an attachment in this conversation')
            data = await asyncio.to_thread(self._store.read, attachment.storage_path)
            if data is None:
                raise ValueError('Reference image is missing from storage')
            references.append(f'data:{attachment.mime_type};base64,' + base64.b64encode(data).decode())
            names[attachment.id] = attachment.filename
        selection = self.image_selection()
        provider = selection.generator
        if provider is None:
            raise RuntimeError('The selected image provider is unavailable.')
        basis = None
        if selection.paid:
            # The prompt and references leave this computer; authorize their exact contents first.
            basis = await self._authorize_remote_image(arguments, names, selection)
        # The window's chat model is read here, at job start, so the handoff follows what is resident.
        kwargs = ({'on_progress': on_progress, 'chat_model': self.model_name} if selection.kind == 'local'
                  else {'model': selection.model})
        if on_start:
            on_start()
        generated = await provider.generate(arguments['prompt'],
            arguments.get('aspect_ratio', '16:9'), references, **kwargs)

        async def persist_completed_image():
            count = len(await self._repo.attachments_for(assistant.id)) + 1
            pending = PendingAttachment(f'illustration-{count:02d}{generated.info.suffix}',
                                        generated.data, image=generated.info)
            persisted = await _worker(lambda: self._persist_attachments([pending]))
            return await self._repo.append_attachments(assistant.id, persisted)

        # The effect has completed. Stop may end the reply, but it must receive
        # the saved artifact before cancellation can propagate to the executor.
        saving = asyncio.create_task(persist_completed_image())
        cancelled = False
        while not saving.done():
            try:
                await asyncio.shield(saving)
            except asyncio.CancelledError:
                cancelled = True
        saved = saving.result()
        if cancelled:
            generated = replace(generated, cancelled=True)
        attachment = saved[-1]
        result = {'attachment_id': attachment.id, 'filename': attachment.filename,
                  'saved_path': str(self._store.path_for(attachment.storage_path)),
                  'width': attachment.width, 'height': attachment.height,
                  'model': generated.model, 'cost_usd': generated.cost,
                  'notice': 'Image saved and displayed. Do not add a Markdown image link. '
                            'Inspect the supplied image before reporting visual QA.'}
        if basis is not None:
            result['authorization'] = basis
        return result, saved, generated

    async def _authorize_remote_image(self, arguments, references, selection):
        from .outbound import OutboundDenied, OutboundRequest
        prompt, aspect_ratio = arguments['prompt'], arguments.get('aspect_ratio', '16:9')
        service = f'OpenRouter image generation (paid) · {selection.model}'
        if self.outbound is None:
            raise OutboundDenied('no_active_reply', 'Remote image requests run only during a reply. '
                                 'Nothing was sent.', OutboundRequest('image', prompt, 'assistant', '', service))
        return await self.outbound.authorize_image(prompt, aspect_ratio, references, service=service)

    @staticmethod
    def _sources(name, result):
        if name == 'file_read' and 'text' in result:
            return [{'title': Path(result['path']).name, 'path': result['path'], 'origin': 'Local file read'}]
        if name in ('knowledge_search', 'knowledge_open'):
            return [{k: row.get(k) for k in ('document_id', 'chunk_id', 'title', 'citation',
                    'source_url', 'heading', 'page', 'line_start', 'line_end', 'source_metadata')}
                    for row in result.get('results', [])]
        if name == 'web_search':
            return [{'url': row['url'], 'title': row['title'], 'origin': 'Search snippet'}
                    for row in result.get('results', [])]
        if name == 'fetch_content' and 'text' in result:
            return [{'url': result['url'], 'title': result['url'], 'origin': 'Page read'}]
        return []

    async def send(self, text, *, attachments=(), refs=(), web=None, on_delta=None,
                   on_reasoning=None, on_tool=None, on_images=None, on_animations=None, on_metrics=None,
                   animation_repair_id=None, on_title=None, on_ui=None, ui_event_id=None, on_audit=None,
                   approve_outbound=None):
        return await TurnRunner(self).run(
            text, attachments=attachments, refs=refs, web=web, on_delta=on_delta,
            on_reasoning=on_reasoning, on_tool=on_tool, on_images=on_images,
            on_animations=on_animations, on_metrics=on_metrics, animation_repair_id=animation_repair_id,
            on_title=on_title, on_ui=on_ui, ui_event_id=ui_event_id, on_audit=on_audit,
            approve_outbound=approve_outbound)
