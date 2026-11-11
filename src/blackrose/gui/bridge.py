"""The seam between the page and the session.

Three threads, each with one job:

- the **main thread** runs GTK and pywebview, which must own it;
- the **loop thread** runs the asyncio event loop the controller, the
  database and every model request live on -- one loop for the whole
  session, opened and closed on the same loop;
- the **emitter thread** carries events to the page. ``evaluate_js`` blocks
  until the page has run the script, so it must never be called from the
  loop thread, where it would stall a streaming reply.

pywebview calls the ``Api`` methods from worker threads of its own. Each one
schedules a coroutine on the loop and waits for its result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import stat
import threading
import weakref
from collections.abc import Coroutine
from concurrent.futures import Future
from pathlib import Path
from typing import Any
import secrets
import html

from ..runtime import Runtime
from ..skills import StagedSkill
from . import announcements, external
from .session import Session
from ..outbound import SITE, OutboundBatch, approval, batch_approval
from .consent import WIDER
from ..animations import export_html
from ..config import DEFAULT_TRUSTED_HOSTS, state_dir
from ..web.sites import trusted_entry
from ..clipboard import copy_text
from ..chat import ChatBusy
from .render import to_html
from ..workspace import FileAccessError
from ..attachments.documents import inspect_document

log = logging.getLogger(__name__)

# One frame at 60 Hz. Events that arrive within it reach the page together.
_BATCH_SECONDS = 1 / 60


class LoopThread:
    """An asyncio loop on a thread of its own, for the life of the window."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="blackrose-loop", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def submit(self, coro: Coroutine) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro: Coroutine, timeout: float | None = None) -> Any:
        return self.submit(coro).result(timeout)

    def call(self, fn, *args) -> Any:
        """Run a plain function on the loop thread and wait for it."""
        async def wrapped():
            return fn(*args)
        return self.run(wrapped())

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


class Emitter:
    """Batches events for the page and delivers them off the loop thread.

    Streaming updates for one reply are coalesced: only the newest HTML of a
    batch is sent, and reasoning deltas are joined, so a fast model never
    floods the page with work it would throw away.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._window = None
        self._thread = threading.Thread(target=self._run, name="blackrose-emit", daemon=True)

    def attach(self, window) -> None:
        self._window = window

    def start(self) -> None:
        self._thread.start()

    def page_ready(self) -> None:
        self._ready.set()

    def close(self) -> None:
        self._closed.set()
        self._queue.put(None)

    def __call__(self, kind: str, payload: dict) -> None:
        if not self._closed.is_set():
            self._queue.put((kind, payload))

    def _run(self) -> None:
        self._ready.wait()
        while not self._closed.is_set():
            item = self._queue.get()
            if item is None:
                return
            batch = [item]
            deadline = threading.Event()
            deadline.wait(_BATCH_SECONDS)
            while True:
                try:
                    more = self._queue.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    self._closed.set()
                    break
                batch.append(more)
            self._deliver(coalesce(batch))

    def _deliver(self, events: list[tuple[str, dict]]) -> None:
        window = self._window
        if window is None or not events:
            return
        script = f"window.bloom && window.bloom.receive({json.dumps(events)})"
        try:
            window.evaluate_js(script)
        except Exception:  # noqa: BLE001 - the window may be closing
            log.debug("could not deliver %d event(s)", len(events), exc_info=True)


def coalesce(batch: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Drop superseded streaming updates and join reasoning, keeping order."""
    out: list[tuple[str, dict]] = []
    latest_update: dict[tuple, int] = {}
    reasoning_at: dict[Any, int] = {}
    for kind, payload in batch:
        if kind == 'snapshot':
            # Snapshot fields already include all earlier deltas. Never
            # coalesce later reasoning across that replacement boundary.
            latest_update.clear()
            reasoning_at.clear()
        key = payload.get("key")
        update_key = (kind, key)
        if kind in ('reply_update', 'ui_update') and update_key in latest_update:
            out[latest_update[update_key]] = (kind, payload)
            continue
        if kind == "reasoning" and key in reasoning_at:
            index = reasoning_at[key]
            joined = dict(out[index][1])
            joined["text"] += payload["text"]
            out[index] = (kind, joined)
            continue
        if kind == "reply_end":
            # Anything after the end of a reply belongs to a later one.
            latest_update.pop(('reply_update', key), None)
            latest_update.pop(('ui_update', key), None)
            reasoning_at.pop(key, None)
        out.append((kind, payload))
        if kind in ('reply_update', 'ui_update'):
            latest_update[update_key] = len(out) - 1
        elif kind == "reasoning":
            reasoning_at[key] = len(out) - 1
    return out


def save_export(chosen, text: str) -> None:
    """Write a user export so its name only ever holds the complete file.

    The text goes to a private temporary file beside the target and is flushed
    to disk, then one rename publishes it. Nothing ever sees an empty or partial
    export, and a failed save leaves an existing file untouched. An existing
    file keeps its permissions, and a symlinked target is resolved first, so
    the save updates the file the link points to, as an in-place write did.
    """
    target = Path(chosen if isinstance(chosen, str) else chosen[0]).resolve()
    temporary = target.with_name(f'.{target.name}.{secrets.token_hex(8)}.partial')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(temporary, flags, 0o666)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            if target.exists():
                os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


class Api:
    """What the page may call, as ``window.pywebview.api.<name>(...)``.

    Every public method here is reachable from page script, so each takes
    plain values, validates them, and does nothing the user could not do by
    typing a command. Underscored attributes are never exposed.
    """

    def __init__(self, runtime: Runtime, loop: LoopThread, emitter: Emitter) -> None:
        self._runtime = runtime
        self._loop = loop
        self._emitter = emitter
        self._session = Session(runtime.controller, emitter, approve_directories=self._approve_directories,
                                approve_outbound=self._approve_outbound)
        self._window: Any | None = None
        self._renderer_recovery: weakref.ReferenceType[Any] | None = None
        self._booted = False
        self._layout_path: Path | None = None
        self._skill_import: tuple[str, StagedSkill] | None = None

    def _attach(self, window) -> None:
        self._window = window

    def _set_renderer_recovery(self, recovery) -> None:
        self._renderer_recovery = weakref.ref(recovery)

    def _renderer_termination_future(self, timeout: float):
        """Return a future that settles work before a host-owned page reload."""
        return self._loop.submit(self._s.settle_after_renderer_termination(timeout))

    @property
    def _s(self) -> Session:
        return self._session

    # --- startup ------------------------------------------------------------

    def ready(self) -> dict:
        """The page has loaded. Start the session; return the static state."""
        self._emitter.page_ready()
        if not self._booted:
            self._booted = True
            self._loop.submit(self._s.boot())
        else:
            # A reload: redraw everything from the session's current state.
            self._loop.submit(self._redraw())
        if self._renderer_recovery is not None:
            recovery = self._renderer_recovery()
            if recovery is not None:
                recovery.page_ready()
        # Only the desktop window calls this; the page adapts to its engine.
        return {**self._s.initial_state(), "layout": self._read_layout(), "engine": "webkitgtk"}

    async def _redraw(self) -> None:
        await self._s.redraw()

    def announce(self, text: str, assertive: bool = False) -> bool:
        """Have the screen reader speak a live-region update the page made.

        WebKitGTK raises no accessibility event when a live region changes, so
        the page forwards the region's settled text here (see announcements.py).
        Returns whether the text was queued for speech.
        """
        cleaned = announcements.clean(text)
        if cleaned is None or not isinstance(assertive, bool) or self._window is None:
            return False
        return announcements.announce(self._window, cleaned, assertive)

    # --- conversations ------------------------------------------------------

    def open_conversation(self, conversation_id: int) -> bool:
        return self._loop.run(self._s.open_conversation(int(conversation_id)))

    def earlier_messages(self, conversation_id: int, before_id: int) -> dict | None:
        return self._loop.run(self._s.earlier_messages(int(conversation_id), int(before_id)))

    def new_conversation(self) -> bool:
        return self._loop.run(self._s.new_conversation())

    def load_older_conversations(self) -> dict:
        return self._loop.run(self._s.load_older_conversations())

    def request_delete(self) -> None:
        self._loop.call(self._s.request_delete)

    def delete_conversation(self, conversation_id: int) -> bool:
        return self._loop.run(self._s.delete_conversation(int(conversation_id)))

    # --- composing ----------------------------------------------------------

    def submit(self, text: str, refs=None) -> dict:
        return self._loop.run(self._s.submit(str(text), refs))

    def retry(self) -> bool:
        return self._loop.run(self._s.retry())

    def animation_failed(self, attachment_id: int, error: str) -> dict:
        return self._loop.run(self._s.animation_failed(attachment_id, error))

    def diagram_failed(self, message_id: int, block_id: str, index: int, source: str, error: str) -> dict:
        return self._loop.run(self._s.diagram_failed(message_id, block_id, index, source, error))

    def ui_action(self, payload) -> dict:
        return self._loop.run(self._s.ui_action(payload))

    def ui_state(self, payload) -> dict:
        return self._loop.run(self._s.ui_state(payload))

    def export_ui(self, payload) -> dict:
        import webview  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI
        try:
            source = self._loop.run(self._s.ui_source(payload))
            if self._window is None:
                return {'error': 'Window unavailable'}
            chosen = self._window.create_file_dialog(webview.FileDialog.SAVE,
                save_filename='interactive-answer.json', file_types=('JSON (*.json)',))
            if not chosen:
                return {'cancelled': True}
            save_export(chosen, source)
            return {'ok': True}
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            return {'error': str(error)[:300]}

    def _approve_directories(self, grants):
        if self._window is None:
            return False
        paths = '\n'.join(g['path'] for g in grants)
        return external.ask(self._window, 'Allow directory access?',
            'Blackrose would read files in these directories for this conversation, until the '
            'app closes or its workspace changes.', 'Allow', detail=paths,
            detail_label='Directories', note='New files and replacements still need your review.') is True

    def _approve_outbound(self, request):
        """Native, per-request consent; page script cannot answer this dialog."""
        if self._window is None:
            return False
        if isinstance(request, OutboundBatch):
            shown, items = batch_approval(request)
            answer = external.ask(self._window, shown.title, shown.summary, shown.accept,
                                  detail_label=shown.label, note=shown.note, choices=items)
            return (tuple(a is True for a in answer)
                    if isinstance(answer, tuple) and len(answer) == len(items) else False)
        shown = approval(request)
        answer = external.ask(self._window, shown.title, shown.summary, shown.accept, detail=shown.payload,
                              detail_label=shown.label, note=shown.note,
                              wider='Allow site for this chat' if request.site else None)
        if answer == WIDER and request.site:
            return SITE
        return answer is True

    def update_draft(self, text: str, refs=None) -> None:
        self._loop.call(self._s.update_draft, str(text), refs)

    def stop(self) -> bool:
        return self._loop.call(self._s.stop)

    def paste(self, composer_empty: bool) -> dict:
        return self._loop.run(self._s.paste(bool(composer_empty)))

    def attach_encoded(self, filename: str, data: str) -> bool:
        return self._loop.run(self._s.attach_encoded(str(filename), str(data)))

    def attach_path(self, path: str, gesture: bool = True) -> bool:
        return self._loop.run(self._s.attach_path(str(path), gesture=bool(gesture)))

    def pick_files(self) -> int:
        """The attach button: the system file dialog, then each file in turn."""
        import webview  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI

        if self._window is None:
            return 0
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True,
            file_types=("Images and documents (*.png;*.jpg;*.jpeg;*.webp;*.pdf;*.docx;"
                        "*.txt;*.md;*.csv;*.json;*.yaml;*.yml;*.toml;*.py)", "All files (*.*)"))
        attached = 0
        for path in paths or ():
            if self._loop.run(self._s.attach_path(str(path))):
                attached += 1
        return attached

    def remove_attachment(self, index: int) -> bool:
        return self._loop.call(self._s.remove_attachment, int(index))

    def image_preview(self, attachment_id: int) -> dict | None:
        return self._loop.run(self._s.image_preview(int(attachment_id)))

    def pick_workspace(self):
        import webview  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI
        if self._window is None:
            return None
        selection = self._loop.run(self._runtime.controller.begin_workspace_selection())
        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not paths:
            return None
        async def select():
            try:
                grant = await self._runtime.controller.set_workspace(str(paths[0]), selection=selection)
            except ChatBusy as error:
                self._s.notice(str(error), 'error')
                return {'error': str(error)}
            await self._s.refresh_conversations()
            return grant
        return self._loop.run(select())

    def clear_workspace(self):
        async def clear():
            await self._runtime.controller.set_workspace(None)
            await self._s.refresh_conversations()
        self._loop.run(clear())

    def reference_suggestions(self, trigger, query='', details=False):
        if trigger not in ('@', '$') or not isinstance(query, str) or len(query) > 200:
            return []
        if details is True:
            return self._loop.run(self._runtime.controller.reference_suggestion_details(trigger, query))
        return self._loop.run(self._runtime.controller.reference_suggestions(trigger, query))

    def workspace_preview(self, path):
        async def preview():
            files = await self._runtime.controller.workspace_files()
            result = await asyncio.to_thread(files.read, str(path))
            text = result['text']
            suffix = Path(path).suffix.lower()
            if suffix != '.md':
                text = '```' + suffix.lstrip('.') + '\n' + text.replace('```', '` ` `') + '\n```'
            notice = '\n\nPreview is limited; use file_read pagination for more text.' if result['next_offset'] is not None or result['extraction_truncated'] else ''
            return {'html': to_html(text + notice), 'next_offset': result['next_offset']}
        return self._loop.run(preview())

    def workspace_browse(self, path='.', offset=0):
        async def browse():
            files = await self._runtime.controller.workspace_files()
            if not files.workspace:
                return {'entries': await self._runtime.controller.reference_suggestions('@', '')}
            return await asyncio.to_thread(files.browse, str(path), offset=max(0, int(offset)))
        return self._loop.run(browse())

    def review_edit(self, token, approve=False, reject=False):
        async def review():
            try:
                result = await self._runtime.controller.review_edit(str(token), approve is True, reject is True)
            except FileAccessError as error:
                # Keep both locations visible after the toast fades.
                if error.code == 'file_changed_published':
                    self._s.notice(str(error), 'error')
                raise
            if approve or reject:
                await self._s.render_transcript()
            if isinstance(result, dict) and result.get('backup_warning'):
                self._s.notice(result['backup_warning'], 'warning')
            return result
        return self._loop.run(review())

    def edit_backups(self, action='list', backup_id=None):
        """List, preview, restore or remove this conversation's edit backups, or apply retention."""
        async def run():
            try:
                return await self._runtime.controller.edit_backup_action(
                    str(action), None if backup_id is None else str(backup_id))
            except FileAccessError as error:
                if error.code == 'file_changed_published':
                    self._s.notice(str(error), 'error')
                raise
        return self._loop.run(run())

    def review_library_add(self, token, approve=False, reject=False):
        return self._loop.run(self._runtime.controller.review_library_add(
            str(token), approve is True, reject is True))

    def artifact_preview(self, attachment_id):
        async def preview():
            c = self._runtime.controller
            attachment = next((a for m in await c.current_messages() for a in m.attachments
                               if a.id == int(attachment_id) and a.kind == 'artifact'), None)
            if attachment is None:
                raise ValueError('Artifact is not in this conversation')
            data = await asyncio.to_thread(c._store.read, attachment.storage_path)
            if data is None:
                raise ValueError('Artifact snapshot is missing')
            suffix = Path(attachment.filename).suffix.lower()
            if suffix == '.pdf':
                text, _ = await asyncio.to_thread(inspect_document, data, claimed_name=attachment.filename)
                return {'html': '<p>PDF text preview. Use Open externally for the full page layout.</p><pre>' + html.escape(text) + '</pre>', 'pdf': True}
            text = data.decode('utf-8')
            if suffix in ('.html', '.htm'):
                return {'document': text}
            return {'html': to_html(text) if suffix == '.md' else to_html(
                '```' + suffix.lstrip('.') + '\n' + text.replace('```', '` ` `') + '\n```')}
        return self._loop.run(preview())

    def open_artifact(self, attachment_id, reveal=False):
        async def location():
            c = self._runtime.controller
            attachment = next((a for m in await c.current_messages() for a in m.attachments
                               if a.id == int(attachment_id)), None)
            if attachment is None:
                raise ValueError('Artifact is not in this conversation')
            # Images/animations already have safe previews; expose their containing
            # store folder through the same explicit reveal gesture.
            if attachment.kind == 'artifact':
                files = await c.workspace_files()
                await asyncio.to_thread(files.metadata, attachment.output_path)
                return attachment.output_path
            path = c.attachment_path(attachment)
            if path is None:
                raise ValueError('File is missing')
            return str(path)
        path = Path(self._loop.run(location()))
        # Leaving the sandboxed preview needs the user's answer in a native
        # dialog that names the path and the application first.
        handoff = external.folder(path.parent) if reveal else external.saved_file(path)
        return external.hand_off(self._window, handoff)

    def export_animation(self, attachment_id: int) -> dict:
        """User-selected animation to a user-chosen standalone HTML file."""
        import webview  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI

        async def document():
            _, scene = await self._runtime.controller.read_animation(int(attachment_id))
            return export_html(scene)

        try:
            content = self._loop.run(document())
            if self._window is None:
                return {'error': 'Window unavailable'}
            chosen = self._window.create_file_dialog(webview.FileDialog.SAVE,
                save_filename='animation.html', file_types=('HTML (*.html)',))
            if not chosen:
                return {'cancelled': True}
            save_export(chosen, content)
            return {'ok': True}
        except (OSError, ValueError, RecursionError) as error:
            return {'error': str(error)[:300]}

    def run_command(self, command: str) -> None:
        self._loop.run(self._s.run_command(str(command)))

    def set_web(self, enabled: bool) -> None:
        self._loop.run(self._s.set_web(bool(enabled)))

    def set_knowledge(self, enabled: bool) -> None:
        self._loop.run(self._s.set_knowledge(bool(enabled)))

    def set_memory(self, enabled: bool) -> None:
        self._loop.run(self._s.set_memory(bool(enabled)))

    def memory_review(self, search: str = '', page: int = 1) -> dict:
        return self._loop.run(self._s.memory_review(str(search), int(page)))

    def set_memory_card(self, text: str) -> dict:
        return self._loop.run(self._s.set_memory_card(str(text)))

    def delete_memory_note(self, conclusion_id: str) -> dict:
        return self._loop.run(self._s.delete_memory_note(str(conclusion_id)))

    def rebuild_memory_profile(self) -> dict:
        return self._loop.run(self._s.rebuild_memory_profile())

    def forget_memory(self) -> dict:
        """Irreversible, so only the native sheet, outside page script's reach, can confirm it."""
        if self._window is None or external.ask(
                self._window, 'Forget everything Blackrose remembers?',
                'Every remembered note, the profile and what each conversation contributed will be '
                'deleted from memory. Your conversations stay in Blackrose.', 'Forget everything',
                warning=True, note='This cannot be undone.') is not True:
            return {'cancelled': True}
        return self._loop.run(self._s.forget_memory())

    def set_citation_rigor(self, mode: str) -> dict:
        return self._loop.run(self._s.set_citation_rigor(mode))

    def trusted_sites(self) -> dict:
        return {'sites': list(self._runtime.controller.config.trusted_hosts),
                'defaults': list(DEFAULT_TRUSTED_HOSTS)}

    def add_trusted_site(self, entry: str) -> dict:
        """Trusting a site widens what model-written requests may reach, so only the native sheet adds one."""
        trusted = trusted_entry(entry)
        if trusted is None:
            return {'error': 'That cannot be a trusted site: use one host name, or *. and a domain. IP '
                    'addresses, public suffixes and hosts where anyone can publish or read the requests '
                    'sent stay on approval.'}
        entry = trusted
        sites = list(self._runtime.controller.config.trusted_hosts)
        if entry in sites:
            return {'ok': True, 'sites': sites}
        covers = f'any page on {entry[2:]} or its subdomains' if entry.startswith('*.') else f'any page on {entry}'
        if self._window is None or external.ask(
                self._window, f'Trust {entry}?',
                f'The assistant could then read {covers} in every chat without asking, including addresses it '
                'writes. Once a chat has used your local data, only addresses without a query.', 'Trust',
                detail=entry, detail_label='Site', note='Trust a site only if one operator runs all of it '
                'and you would not mind it seeing what the assistant asks for.') is not True:
            return {'cancelled': True}
        return self._loop.run(self._s.set_trusted_sites([*sites, entry]))

    def remove_trusted_site(self, entry: str) -> dict:
        sites = self._runtime.controller.config.trusted_hosts
        return self._loop.run(self._s.set_trusted_sites([s for s in sites if s != entry]))

    def set_reasoning_effort(self, level: str) -> dict:
        return self._loop.run(self._s.set_reasoning_effort(level))

    def models(self, provider: str | None = None) -> dict:
        return self._loop.run(self._s.models(provider))

    def select_model(self, model: str, make_default: bool = False) -> dict:
        return self._loop.run(self._s.select_model(str(model), bool(make_default)))

    def set_model_favorite(self, model: str, favorite: bool) -> dict:
        return self._loop.call(self._s.set_model_favorite, model, favorite)

    def image_providers(self) -> dict:
        return self._loop.run(self._s.image_providers())

    def select_image_provider(self, provider: str, model: str | None = None) -> dict:
        return self._loop.run(self._s.select_image_provider(str(provider), None if model is None else str(model)))

    def release_image_memory(self) -> dict:
        return self._loop.run(self._s.release_image_memory())

    def skills(self) -> list:
        return self._loop.call(self._runtime.controller.skills.list)

    def set_skill(self, key: str, enabled: bool) -> bool:
        return self._loop.call(self._change_skill, 'set_enabled', str(key), bool(enabled))

    def remove_skill(self, key: str) -> bool:
        return self._loop.call(self._change_skill, 'remove', str(key))

    def _change_skill(self, action, *args):
        if self._runtime.controller.is_generating:
            raise ValueError('Wait for the reply to finish')
        getattr(self._runtime.controller.skills, action)(*args)
        return True

    def review_skill(self, key: str) -> dict:
        """Current text and contents of an installed skill, for approval."""
        skills = self._runtime.controller.skills
        return self._loop.run(asyncio.to_thread(skills.review, str(key)))

    def approve_skill(self, key: str, digest: str) -> bool:
        # The digest binds approval to the exact SKILL.md text the user was shown.
        return self._loop.call(self._change_skill, 'approve', str(key), str(digest))

    def review_skill_execution(self, key: str) -> dict:
        skills = self._runtime.controller.skills
        return self._loop.run(asyncio.to_thread(skills.execution_review, str(key)))

    def approve_skill_execution(self, key: str, digest: str) -> bool:
        """Page requests a review; only the native sheet can authorize execution."""
        if self._window is None:
            return False
        review = self.review_skill_execution(str(key))
        if review['digest'] != digest:
            raise ValueError('Execution package changed during review; review it again')
        # Content is bounded and comes from the host snapshot, never page text.
        detail = '\n\n'.join(f'{name}\n{body}' for name, body in review['sources'].items())
        detail += '\n\nResource hashes:\n' + '\n'.join(
            f"{r['path']} ({r['bytes']} bytes): {r['sha256']}" for r in review['resources'])
        if external.ask(self._window, 'Allow skill script execution?', review['permissions'],
                        'Allow execution', detail=detail, detail_label='Reviewed package',
                        note='Approval covers this exact package. Importing or reading it runs nothing.') is not True:
            return False
        return self._loop.run(self._approve_skill_execution(str(key), str(digest)))

    async def _approve_skill_execution(self, key, digest):
        skills = self._runtime.controller.skills
        current = await asyncio.to_thread(skills.execution_review, key)
        if current['digest'] != digest:
            raise ValueError('Execution package changed during review; review it again')
        if self._runtime.controller.is_generating:
            raise ValueError('Wait for the reply to finish')
        skills._record_execution_approval(key, digest)
        return True

    def revoke_skill_execution(self, key: str) -> bool:
        return self._loop.call(self._change_skill, 'revoke_execution', str(key))

    def import_skill(self) -> dict:
        """Stage a chosen folder and return its review; nothing is installed yet."""
        if self._window is None:
            return {'cancelled': True}
        import webview  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI
        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not paths:
            return {'cancelled': True}
        return self._loop.run(self._stage_skill_import(str(paths[0])))

    def finish_skill_import(self, token: str, approve: bool = False) -> dict:
        """Publish the reviewed package enabled, or discard it so nothing is installed."""
        return self._loop.call(self._finish_skill_import, str(token), approve is True)

    def _discard_skill_import(self) -> None:
        pending = getattr(self, '_skill_import', None)
        self._skill_import = None
        if pending is not None:
            pending[1].cleanup()

    def _finish_skill_import(self, token, approve):
        pending = getattr(self, '_skill_import', None)
        if pending is None or not secrets.compare_digest(pending[0], token):
            raise ValueError('This skill review has expired. Import the folder again.')
        self._skill_import = None
        staged = pending[1]
        if not approve or self._runtime.controller.is_generating:
            staged.cleanup()
            if approve:
                raise ValueError('Wait for the reply to finish, then import the skill again')
            return {'cancelled': True}
        return self._runtime.controller.skills.publish_install(staged, approved=True)

    async def _stage_skill_import(self, path):
        if self._runtime.controller.is_generating:
            raise ValueError('Wait for the reply to finish')
        skills = self._runtime.controller.skills

        def stage():
            staged = skills.stage_install(path)
            try:
                return staged, staged.review()
            except BaseException:
                staged.cleanup()
                raise

        staging = asyncio.create_task(asyncio.to_thread(stage))
        cancelled = False
        while not staging.done():
            try:
                await asyncio.shield(staging)
            except asyncio.CancelledError:
                # to_thread cannot stop an in-flight filesystem copy. Keep
                # ownership until it returns even if cancellation repeats.
                cancelled = True
        try:
            staged, review = staging.result()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        if cancelled:
            staged.cleanup()
            raise asyncio.CancelledError
        # One review at a time: a newer import replaces any abandoned one.
        self._discard_skill_import()
        token = secrets.token_hex(16)
        self._skill_import = (token, staged)
        return {'token': token, **review}

    def knowledge_list(self, offset: int = 0) -> dict:
        return self._loop.run(self._runtime.controller.knowledge.call('list', offset=int(offset)))

    def knowledge_search(self, query: str) -> dict:
        return self._loop.run(self._runtime.controller.knowledge.call('search', query=str(query)))

    def knowledge_open(self, document_id: str, chunk_id: str | None = None, offset: int = 0) -> dict:
        return self._loop.run(self._runtime.controller.knowledge.call(
            'open', document_id=str(document_id), chunk_id=chunk_id, offset=int(offset)))

    def knowledge_remove(self, document_id: str) -> dict:
        return self._loop.run(self._runtime.controller.knowledge.call('remove', document_id=str(document_id)))

    def knowledge_import(self) -> dict:
        if self._window is None:
            return {'imports': []}
        import webview  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                    file_types=('Local documents (*.md;*.txt;*.pdf;*.html;*.htm)',))
        return {'imports': [self._loop.run(self._runtime.controller.knowledge.call('add', path=str(path)))
                            for path in paths or ()]}

    # --- the window ---------------------------------------------------------

    def open_link(self, url: str) -> bool:
        """Links open in the default browser or email app once the user confirms
        the full address natively; the window never navigates."""
        return external.hand_off(self._window, external.link(str(url)))

    def save_layout(self, layout: dict) -> bool:
        """Remember the sidebar's width, whether it is hidden, and code wrapping.

        The window runs in private mode, so the page's own storage does not
        outlive it; this small file does.
        """
        if not isinstance(layout, dict):
            return False
        clean: dict[str, Any] = {}
        width = layout.get("sidebar")
        if isinstance(width, (int, float)) and 160 <= width <= 640:
            clean["sidebar"] = round(float(width), 1)
        for flag in ("collapsed", "wrap"):
            if isinstance(layout.get(flag), bool):
                clean[flag] = layout[flag]
        path = self._layout_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(clean))
        except OSError:
            log.debug("could not save the window layout", exc_info=True)
            return False
        return True

    def _layout_file(self):
        if self._layout_path is None:

            self._layout_path = state_dir() / "window.json"
        return self._layout_path

    def _read_layout(self) -> dict:
        try:
            data = json.loads(self._layout_file().read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def copy(self, text: str) -> bool:
        return bool(self._loop.run(copy_text(str(text))))

    def quit(self) -> None:
        """Close the window -- after this call has returned to the page.

        pywebview delivers a call's return value with ``evaluate_js``.
        Destroying the window inside the call leaves that delivery waiting on
        a main loop that no longer exists, in a thread the interpreter then
        waits for at exit.
        """
        window = self._window
        if window is not None:
            timer = threading.Timer(0.1, window.destroy)
            timer.daemon = True
            timer.start()

    # --- teardown (not reachable from the page: called by the launcher) -----

    def _shutdown(self, timeout: float) -> None:
        self._emitter.close()
        self._loop.run(self._s.close(), timeout)
