"""Local ComfyUI image workflows, sharing GPU memory with the selected chat model when possible.

The configured workflows and service are never modified. In-memory workflow copies
bypass an LM Studio unload node if present: this app owns the llama-router handoff instead.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import fcntl
import json
import logging
import math
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import httpx

from .attachments.images import MAX_IMAGE_BYTES, inspect_image
from .backend.providers import model_provider
from .config import state_dir
from .gui.model_names import model_display_name
from .image_generation import GeneratedImage

MODEL = 'qwen-image-2.1-local'
GIB = 1024 ** 3
# Free VRAM an image job needs once nothing else of ours is resident. Measured on
# this machine with Qwen Image at ~1 MP; edits carry their reference images too.
GENERATION_HEADROOM = 9 * GIB
EDIT_HEADROOM = 10.5 * GIB
# Only the model these headroom numbers were measured with (Gemma 4 E4B at its 64k
# preset) may stay resident beside an image job. Other models' KV growth is unmeasured,
# so their memory is always released before generation.
SHARING_MODELS = frozenset({'gemma-4-e4b-it'})
# Sharing also needs this much system RAM free, as ComfyUI reports it, before every
# shared job, including one that reuses a warm image model. With the chat model
# resident, ComfyUI stages the text encoder and diffusion weights through RAM; in
# the 2026-11-07 measurement (bra-01x.5) a second shared job drove MemAvailable
# from 10 GiB to 2.1 GiB on a 30.5 GiB machine. The value is the measured first-job
# draw of the one supported model set (Qwen Image, 12.9 GiB) plus a desktop reserve,
# so another image model needs its own figure (measured 2026-11-10).
SHARING_RAM = 18 * GIB
# After a job on the sharing path the image model stays loaded this long, so a
# following image reuses it. Each job restarts the window (bra-01x, decision 3).
IDLE_RELEASE_SECONDS = 120
# How long a busy lock postpones an expiring release, and how long close() may wait.
RELEASE_RETRY_SECONDS = 5
CLOSE_TIMEOUT_SECONDS = 30

log = logging.getLogger(__name__)


def available_vram(total):
    """Read the kernel's allocation count, including Vulkan as well as ROCm.

    ROCm's free-memory estimate can miss another runtime's allocations. Match
    the ComfyUI device by VRAM capacity; an ambiguous match means no sharing.
    """
    candidates = {}
    for card in Path('/sys/class/drm').glob('card[0-9]*'):
        device = card / 'device'
        try:
            capacity = int((device / 'mem_info_vram_total').read_text())
            used = int((device / 'mem_info_vram_used').read_text())
            if abs(capacity - total) < 256 * 1024 * 1024:
                candidates[device.resolve()] = capacity - used
        except (OSError, ValueError):
            # A DRM entry vanished or lacks readable memory counters.
            continue
    return next(iter(candidates.values())) if len(candidates) == 1 else None


class ComfyService:
    """Start the user's measured ROCm service on demand; stop only if we started it."""

    def __init__(self, client):
        self.client = client
        self.owned = False

    async def _systemctl(self, action):
        process = await asyncio.create_subprocess_exec(
            'systemctl', '--user', action, 'comfyui.service',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(60):
                code = await process.wait()
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if code:
            raise RuntimeError(f'Could not {action} the local image service (comfyui.service).')

    async def ensure_running(self):
        try:
            response = await self.client.get('/system_stats')
            response.raise_for_status()
            return
        except httpx.ConnectError:
            # Nothing is listening yet; a starting service keeps its original owner.
            pass
        # Keep a service that is already starting owned by its original caller.
        process = await asyncio.create_subprocess_exec(
            'systemctl', '--user', 'is-active', '--quiet', 'comfyui.service')
        active = await process.wait() == 0
        if not active:
            self.owned = True
            await self._systemctl('start')
        async with asyncio.timeout(120):
            while True:
                try:
                    response = await self.client.get('/system_stats')
                    if response.status_code == 200:
                        return
                except httpx.ConnectError:
                    # Still starting; poll again below.
                    pass
                await asyncio.sleep(0.5)

    async def release(self):
        """Release all image memory: stop a service Blackrose started, otherwise unload its models."""
        if self.owned:
            await self._systemctl('stop')
            self.owned = False
            return
        await self.free()

    async def free(self):
        """Unload the image models but keep the service running, whoever started it."""
        # /free acknowledges queued work, not completion. Wait until the worker
        # actually releases its tensors before restoring the chat model.
        response = await self.client.post('/free', json={'unload_models': True, 'free_memory': True})
        response.raise_for_status()
        async with asyncio.timeout(60):
            while True:
                response = await self.client.get('/system_stats')
                response.raise_for_status()
                devices = response.json()['devices']
                if devices and all(d['torch_vram_total'] < 512 * 1024 * 1024 for d in devices):
                    return
                await asyncio.sleep(0.5)


def _workflow_name(editing: bool) -> str:
    return 'qwen21-edit.api.json' if editing else 'qwen21-t2i.api.json'


def _assert_workflow(graph) -> None:
    """Refuse a workflow whose node layout this integration does not understand."""
    assert graph['4']['class_type'] == 'TextEncodeQwenImage21'
    assert graph['6']['class_type'] == 'KSampler'
    assert graph['8']['class_type'] == 'SaveImage'
    assert graph['10']['class_type'] == 'LMStudioUnloadPassthrough'
    assert graph['4']['inputs']['prompt'] == ['10', 0]


@dataclass
class WarmRecord:
    """An image model left loaded after a sharing job, shared by every window through a file.

    Written and removed only while holding the handoff lock. ``started_service``
    carries the duty to stop comfyui.service with the record, whichever window
    releases it.
    """
    token: str
    pid: int
    until: float
    headroom: int
    chat_model: str
    started_service: bool


@dataclass
class _Resident:
    """The image model one handoff session has loaded, and what it was checked against."""
    headroom: int
    chat_model: str | None
    shared: bool
    until: float | None = None  # Set when adopted from a warm record.


@dataclass
class _Session:
    """One hold of the handoff lock: a single job, or a batch of one round's jobs."""
    progress: object
    ready: bool = False
    jobs: int = 0
    resident: _Resident | None = None
    restore: str | None = None
    failed: bool = False
    cancelled: bool = False
    owner_pid: int = field(default_factory=os.getpid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LocalImages:
    def __init__(self, endpoint, workflows, backend, timeout=600, *,
                 transport=None, service=None, lock_path=None, poll_interval=0.5,
                 keep_chat_loaded=True, idle_release=IDLE_RELEASE_SECONDS):
        self.workflows = Path(workflows).expanduser()
        self.backend = backend
        self.timeout = timeout
        # ComfyUI is loopback-only (config.loopback_url), so a proxy could only be a detour.
        self._client = httpx.AsyncClient(base_url=endpoint, timeout=30, follow_redirects=False,
                                         transport=transport, trust_env=False)
        self.service = service or ComfyService(self._client)
        self.lock_path = lock_path or state_dir() / 'local-images.lock'
        self.poll_interval = poll_interval
        self.keep_chat_loaded = keep_chat_loaded
        self.idle_release = idle_release
        self.warm_path = self.lock_path.with_name(self.lock_path.stem + '.warm.json')
        self.recovery_error = None
        self._batch: contextvars.ContextVar[_Session | None] = contextvars.ContextVar(
            f'local_images_batch_{id(self)}', default=None)
        self._timer: asyncio.Task | None = None
        self._phase: str | None = None  # 'loading', 'generating' or 'releasing' in this process.

    async def close(self):
        """Release this window's warm image model, with a bounded wait, then close the client."""
        try:
            await self._cancel_timer()
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await self._release_record(owner_only=True, wait=True)
            except TimeoutError:
                log.warning('Timed out releasing the warm local image model at close')
            except Exception:  # noqa: BLE001 - Closing must finish; the next job or Release now recovers.
                log.exception('Could not release the warm local image model at close')
        finally:
            await self._client.aclose()

    @asynccontextmanager
    async def _lock(self, patience=None):
        # Serializes our image jobs across desktop windows without blocking the
        # event loop. The lock is held through cleanup and chat model restoration.
        # patience=0 tries once and raises TimeoutError when another holder has it.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        wait = self.timeout if patience is None else patience
        with self.lock_path.open('a') as lock:
            async with asyncio.timeout(wait if wait > 0 else None):
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if wait <= 0:
                            raise TimeoutError('The local image handoff lock is held') from None
                        await asyncio.sleep(self.poll_interval)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # Warm record: read without the lock for status, written only under it.

    def _read_record(self) -> WarmRecord | None:
        try:
            data = json.loads(self.warm_path.read_text())
            return WarmRecord(**{key: data[key] for key in WarmRecord.__dataclass_fields__})
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            # Unreadable state cannot say who started the service; the model is freed instead.
            return WarmRecord('invalid', 0, 0.0, 0, '', False)

    def _write_record(self, record: WarmRecord) -> None:
        self.warm_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.warm_path.with_name(self.warm_path.name + f'.{os.getpid()}.tmp')
        temporary.write_text(json.dumps(asdict(record)))
        os.replace(temporary, self.warm_path)

    def _remove_record(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.warm_path.unlink()

    async def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer

    def _arm_timer(self, token: str, delay: float) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.create_task(self._expire(token, delay))

    async def _expire(self, token: str, delay: float) -> None:
        await asyncio.sleep(max(0.0, delay))
        while True:
            try:
                await self._release_record(token=token, expired_only=True)
                return
            except TimeoutError:
                # A status read or another release holds the lock briefly; a job
                # instead adopts the record, after which the token no longer matches.
                await asyncio.sleep(RELEASE_RETRY_SECONDS)
            except Exception:  # noqa: BLE001 - A timer has no caller; recovery_error reports it.
                log.exception('Could not release the idle local image model')
                return

    async def _release_record(self, *, token=None, expired_only=False, owner_only=False,
                              wait=False) -> bool:
        """Release a warm image model under the lock. Returns whether one was released.

        Without ``wait`` this raises TimeoutError instead of waiting for a running job,
        which adopts the record itself.
        """
        if self._read_record() is None:
            return False
        async with self._lock(patience=None if wait else 0):
            record = self._read_record()
            if record is None or (token is not None and record.token != token):
                return False
            if owner_only and record.pid != os.getpid():
                return False
            if expired_only and record.until > time.time():
                self._arm_timer(record.token, record.until - time.time())
                return False
            self._phase = 'releasing'
            try:
                if record.started_service:
                    self.service.owned = True
                try:
                    await self.service.release()
                except Exception as error:
                    self.recovery_error = 'Local image memory recovery failed: ' + str(error)
                    raise
                finally:
                    self._remove_record()
            finally:
                self._phase = None
            if self._timer is not None and self._timer is not asyncio.current_task():
                self._timer.cancel()
            self._timer = None
            return True

    def _adopt(self, session: _Session) -> None:
        """Take over a warm record under the lock; this session now decides its fate."""
        record = self._read_record()
        if record is None:
            return
        self._remove_record()
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if record.started_service:
            self.service.owned = True
        if record.token != 'invalid':
            session.resident = _Resident(record.headroom, record.chat_model, True, record.until)
        else:
            session.resident = _Resident(0, None, False)  # Never reused; freed at first use.

    # Public warm-state controls used by the controller and the desktop bridge.

    async def make_room_for_chat(self, chat_model) -> None:
        """Release a warm image model before ``chat_model`` needs local memory it may not share."""
        record = self._read_record()
        if record is None:
            return
        state, _catalog = await self._residency(chat_model)
        if state == 'cloud' or (state == 'loaded' and chat_model == record.chat_model):
            return
        with contextlib.suppress(TimeoutError):
            await self._release_record()

    async def release_now(self) -> dict:
        """Release the idle image model, whichever window left it loaded."""
        with contextlib.suppress(TimeoutError):
            # A job holding the lock has adopted the record and decides its own release.
            await self._release_record()
        return self.status()

    def status(self) -> dict:
        """The image model's memory state for display. Python owns the policy."""
        phase = {'loading': 'loading', 'generating': 'in_use', 'releasing': 'releasing'}.get(self._phase)
        if phase is not None:
            return {'state': phase}
        record = self._read_record()
        if record is not None:
            return {'state': 'loaded', 'releases_at': record.until,
                    'seconds_left': max(0, round(record.until - time.time())),
                    'this_window': record.pid == os.getpid()}
        return {'state': 'in_use' if self._lock_held_elsewhere() else 'not_loaded'}

    def _lock_held_elsewhere(self) -> bool:
        """Whether another window's image job holds the handoff lock right now."""
        try:
            with self.lock_path.open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            return False
        return False

    async def reap_orphan(self) -> None:
        """Release an expired warm record whose owning process has exited."""
        record = self._read_record()
        if record is None or record.until > time.time() or _alive(record.pid):
            return
        with contextlib.suppress(TimeoutError):
            await self._release_record(token=record.token)

    def workflow_problem(self) -> str | None:
        """Why the bundled workflows cannot run, or None when both validate."""
        for editing in (False, True):
            try:
                _assert_workflow(json.loads((self.workflows / _workflow_name(editing)).read_text()))
            except (OSError, ValueError, KeyError, TypeError, AssertionError):
                return f'Local image workflows are missing or incompatible in {self.workflows}.'
        return None

    def _workflow(self, prompt, aspect_ratio, editing):
        name = _workflow_name(editing)
        try:
            graph = json.loads((self.workflows / name).read_text())
            _assert_workflow(graph)
            # Deliberately remove the LM Studio side effect in our private copy.
            del graph['10']
            graph['4']['inputs']['prompt'] = prompt
            graph['6']['inputs']['seed'] = secrets.randbits(63)
            graph['8']['inputs']['filename_prefix'] = 'blackrose/qwen21'
            if not editing:
                width, height = map(int, aspect_ratio.split(':'))
                ratio = width / height
                graph['5']['inputs'].update(
                    width=round(1024 * math.sqrt(ratio) / 32) * 32,
                    height=round(1024 / math.sqrt(ratio) / 32) * 32)
            return graph
        except (OSError, ValueError, KeyError, TypeError, AssertionError):
            raise RuntimeError('Local image workflows are missing or incompatible. '
                               'Check local_image_workflows in the app configuration.') from None

    async def _json(self, method, path, **kwargs):
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _queue(self):
        queue = await self._json('GET', '/queue')
        return queue['queue_running'] + queue['queue_pending']

    async def _residency(self, chat_model):
        """(state, catalog) for the chat model on the shared router, read at job start.

        'cloud' and 'absent' involve no local memory. Otherwise the state is the
        router's status; 'loading' is waited out so a displacement decision is never
        made against a half-loaded model.
        """
        if not chat_model:
            return 'absent', []
        if model_provider(chat_model) == 'opencode-go':
            return 'cloud', []
        rows = await self.backend.models()
        while True:
            row = next((r for r in rows if r['id'] == chat_model), None)
            if row is None:
                return 'absent', rows
            status = row.get('status')
            value = status.get('value') if isinstance(status, dict) else None
            if value != 'loading':
                return value or 'unknown', rows
            await asyncio.sleep(self.poll_interval)
            rows = await self.backend.models()

    async def _free_vram(self):
        stats = await self._json('GET', '/system_stats')
        try:
            return available_vram(stats['devices'][0]['vram_total'])
        except (KeyError, IndexError, TypeError):
            return None

    async def _has_free_vram(self, references):
        """Exclusive generation needs its headroom free. An unreadable VRAM counter is not refused."""
        free = await self._free_vram()
        return free is None or free >= (EDIT_HEADROOM if references else GENERATION_HEADROOM)

    async def _can_share(self, references, chat_model):
        if not self.keep_chat_loaded or len(references) > 1 or chat_model not in SHARING_MODELS:
            return False
        stats = await self._json('GET', '/system_stats')
        try:
            free = available_vram(stats['devices'][0]['vram_total'])
            required = EDIT_HEADROOM if references else GENERATION_HEADROOM
        except (KeyError, IndexError, TypeError):
            return False
        return free is not None and free >= required and await self._ram_headroom(stats)

    async def _ram_headroom(self, stats=None):
        """Keep RAM for the desktop as well as VRAM for activations; unreadable means no."""
        if stats is None:
            stats = await self._json('GET', '/system_stats')
        try:
            return stats['system']['ram_free'] >= SHARING_RAM
        except (KeyError, TypeError):
            return False

    async def _upload(self, reference):
        header, encoded = reference.split(',', 1)
        if not header.startswith('data:image/') or not header.endswith(';base64'):
            raise ValueError('Local image references must be stored image attachments.')
        data = base64.b64decode(encoded, validate=True)
        info = inspect_image(data)
        result = await self._json('POST', '/upload/image',
            files={'image': (f'blackrose-{uuid.uuid4().hex}{info.suffix}', data, info.mime_type)},
            data={'type': 'temp', 'overwrite': 'false'})
        subfolder = result.get('subfolder', '')
        return (subfolder + '/' if subfolder else '') + result['name'] + ' [temp]'

    async def _wait(self, prompt_id):
        while True:
            history = await self._json('GET', f'/history/{prompt_id}')
            if prompt_id in history:
                item = history[prompt_id]
                if item.get('status', {}).get('status_str') == 'error':
                    raise RuntimeError('Local image generation failed. See the ComfyUI service log for details.')
                images = item.get('outputs', {}).get('8', {}).get('images', [])
                if not images:
                    raise RuntimeError('Local image generation returned no image.')
                return images[0]
            await asyncio.sleep(self.poll_interval)

    async def _download(self, image):
        async with self._client.stream('GET', '/view', params={
                'filename': image['filename'], 'subfolder': image.get('subfolder', ''),
                'type': image.get('type', 'output')}) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    raise RuntimeError('Local image exceeds the attachment size limit.')
        data = bytes(data)
        info = inspect_image(data)
        if info.width is None or info.height is None:
            raise RuntimeError('Local image service returned an invalid image.')
        return GeneratedImage(data, info, MODEL, 0.0)

    async def _settle(self, prompt_id, *, completed):
        """Stop our unfinished prompt and wait until ComfyUI's queue is empty."""
        if prompt_id and not completed:
            # Includes a submission whose response was lost. Never interrupt
            # another application's image job.
            try:
                response = await self._client.post('/queue', json={'delete': [prompt_id]})
                response.raise_for_status()
                response = await self._client.post('/interrupt', json={'prompt_id': prompt_id})
                response.raise_for_status()
            except httpx.HTTPError:
                if not self.service.owned:
                    raise
                return  # Stopping the owned service at session end still releases the GPU.
            if self.service.owned:
                return  # The cancelled session stops the service it started.
        async with asyncio.timeout(60):
            while await self._queue():  # noqa: ASYNC110 - remote queue has no local event
                await asyncio.sleep(self.poll_interval)

    async def _finish(self, session: _Session):
        """Keep a shared image model warm, or release it; then restore a displaced chat model once."""
        progress = session.progress
        resident = session.resident
        if not session.ready:
            # No job reached the service. Put an adopted record back unchanged, so
            # the warm model it describes keeps its owner and its original deadline.
            if resident is not None and resident.until is not None and resident.chat_model:
                self._keep_warm(resident, resident.until)
            elif resident is not None or self.service.owned:
                # An adopted record nobody can describe again (the unreadable sentinel,
                # or a record without a chat model) cannot be put back faithfully. Its
                # owner is gone and the model must not stay loaded without one.
                await self._release_service()
            return
        keep = (self.idle_release > 0 and resident is not None and resident.shared
                and resident.chat_model is not None and not session.failed and not session.cancelled)
        if keep:
            # A warm model is only worth holding if the next shared job could reuse it.
            try:
                keep = await self._ram_headroom()
            except Exception:  # noqa: BLE001 - An unreadable reading releases, as refusal does.
                keep = False
        if keep:
            self._keep_warm(resident, time.time() + self.idle_release)
        else:
            await self._release_service()
        if session.restore:
            progress(f'Restoring {model_display_name(session.restore)}…')
            await self.backend.load(session.restore)

    async def _release_service(self) -> None:
        """Release the image memory, reporting the releasing phase for the whole call."""
        self._phase = 'releasing'
        try:
            await self.service.release()
        finally:
            self._phase = None

    def _keep_warm(self, resident: _Resident, until: float) -> None:
        record = WarmRecord(uuid.uuid4().hex, os.getpid(), until, resident.headroom,
                            resident.chat_model or '', self.service.owned)
        # The duty to stop a service Blackrose started now travels with the record.
        self._write_record(record)
        self.service.owned = False
        self._arm_timer(record.token, until - time.time())

    async def _run_shielded(self, coroutine):
        """Run GPU recovery to completion even when Stop arrives; return that cancellation, if any."""
        task = asyncio.create_task(coroutine)
        cancelled = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                if task.cancelled():
                    self.recovery_error = 'Local image memory recovery was interrupted.'
                    break
                # Repeated Stop requests still cannot abandon the recovery
                # worker or release the GPU handoff lock.
            except Exception as error:  # noqa: BLE001 - Preserve downloaded output on any recovery failure.
                self.recovery_error = 'Local image memory recovery failed: ' + str(error)
                break
        return cancelled

    async def _prepare_memory(self, session: _Session, references, chat_model, required, progress):
        """Decide whether the chat model shares the GPU; returns True when it stays loaded."""
        state, catalog = await self._residency(chat_model)
        holds_memory = state in ('loaded', 'unknown')
        resident = session.resident
        if resident is not None:
            if resident.shared:
                reusable = (self.keep_chat_loaded and state == 'loaded' and chat_model == resident.chat_model
                            and chat_model in SHARING_MODELS and len(references) <= 1
                            and required <= resident.headroom and await self._ram_headroom())
            else:
                reusable = not holds_memory and required <= resident.headroom
            if reusable:
                if resident.shared:
                    progress(f'{model_display_name(chat_model, catalog)} and the image model stay loaded…')
                return resident.shared
            # The loaded image model was checked for other conditions. Free it and
            # decide again, so the VRAM readings below describe only other users.
            progress('Releasing the loaded image model…')
            await self.service.free()
            session.resident = None
        if holds_memory and await self._can_share(references, chat_model):
            progress(f'{model_display_name(chat_model, catalog)} stays loaded during local image generation…')
            return True
        if holds_memory:
            progress('Preparing memory for local image generation…')
            session.restore = chat_model  # Also restore if cancellation lands during unload.
            await self.backend.unload(chat_model)
        if not await self._has_free_vram(references):
            # Another client's model is never evicted to make room (bra-24j).
            raise RuntimeError('Another application is using GPU memory, so local image '
                               'generation did not start. No paid request was sent.')
        return False

    async def _job(self, session: _Session, graph, references, progress, chat_model):
        """Run one image job inside a held session; returns (image or None, failure or None)."""
        session.progress = progress
        session.jobs += 1
        prompt_id = None
        generated = None
        failure = None
        shared = False
        required = EDIT_HEADROOM if references else GENERATION_HEADROOM
        self._phase = 'loading'
        reached = False
        try:
            async with asyncio.timeout(self.timeout):
                progress('Starting local image generation…')
                await self.service.ensure_running()
                reached = True
                if await self._queue():
                    raise RuntimeError('The local image service is busy. Wait for its current job and try again.')
                session.ready = True
                for index, reference in enumerate(references, 1):
                    node = '9' if index == 1 else f'blackrose_ref_{index}'
                    graph[node] = {'class_type': 'LoadImage',
                                   'inputs': {'image': await self._upload(reference)}}
                    graph['4']['inputs'][f'images.image_{index}'] = [node, 0]
                shared = await self._prepare_memory(session, references, chat_model, required, progress)
                prompt_id = str(uuid.uuid4())
                response = await self._json('POST', '/prompt', json={
                    'prompt': graph, 'prompt_id': prompt_id, 'client_id': 'blackrose-bloom'})
                if response.get('prompt_id') != prompt_id or response.get('node_errors'):
                    raise RuntimeError('The local image service rejected the image workflow.')
                self._phase = 'generating'
                progress('Generating image locally · Qwen Image 2.1…')
                image = await self._wait(prompt_id)
                generated = await self._download(image)
        except TimeoutError as error:
            failure = RuntimeError('Local image generation timed out. No paid request was sent.')
            failure.__cause__ = error
        except httpx.HTTPError as error:
            failure = RuntimeError('The local image service request failed. No paid request was sent.')
            failure.__cause__ = error
        except BaseException as error:  # noqa: BLE001 - Re-raised after GPU recovery settles.
            failure = error
        finally:
            self._phase = None
        if session.ready or self.service.owned:
            # Stop waits for this, so closing the window or changing models
            # cannot race an abandoned diffusion job on the GPU.
            session.ready = True
            cancelled = None
            if prompt_id is not None or reached:
                # Nothing of ours can be queued unless a prompt was submitted, and
                # a service that was never reached has no queue to wait for. Polling
                # it here would latch recovery_error for a plain start failure.
                cancelled = await self._run_shielded(
                    self._settle(prompt_id, completed=generated is not None))
            failure = failure or cancelled
        if isinstance(failure, asyncio.CancelledError):
            session.cancelled = True
        if generated is not None:
            previous = session.resident.headroom if session.resident else 0
            session.resident = _Resident(max(required, previous), chat_model, shared)
        elif session.ready:
            session.failed = True
        return generated, failure

    def _outcome(self, generated, failure):
        """Attach recovery state to the job's result, then return it or raise its failure."""
        if self.recovery_error:
            if generated is not None:
                generated = replace(generated, cleanup_warning=self.recovery_error)
            if failure is not None:
                failure.add_note(self.recovery_error)
        if failure is not None:
            if generated is not None and isinstance(failure, asyncio.CancelledError):
                # The caller persists the completed artifact before honoring
                # cancellation. It must not continue inference from this result.
                return replace(generated, cancelled=True)
            raise failure
        return generated

    @asynccontextmanager
    async def batch(self):
        """Hold one handoff across one round's image calls; release and restore once at the end."""
        if self._batch.get() is not None:
            yield
            return
        if self.recovery_error:
            raise RuntimeError(self.recovery_error)
        async with self._lock():
            session = _Session(lambda message: None)
            self._adopt(session)
            token = self._batch.set(session)
            failure = None
            try:
                yield
            except BaseException as error:
                failure = error
                if isinstance(error, asyncio.CancelledError):
                    session.cancelled = True
                raise
            finally:
                self._batch.reset(token)
                cancelled = await self._run_shielded(self._finish(session))
                if self.recovery_error and failure is not None:
                    failure.add_note(self.recovery_error)
                if failure is None:
                    if cancelled is not None:
                        raise cancelled
                    if self.recovery_error:
                        raise RuntimeError(self.recovery_error)

    async def generate(self, prompt, aspect_ratio='16:9', references=(), *, on_progress=None,
                       chat_model=None):
        """Generate one image. ``chat_model`` is the window's selected chat model at job start."""
        if self.recovery_error:
            raise RuntimeError(self.recovery_error)
        progress = on_progress or (lambda message: None)
        graph = self._workflow(prompt, aspect_ratio, bool(references))
        if len(references) > 4:
            raise ValueError('At most four image references are supported.')
        session = self._batch.get()
        if session is not None:
            generated, failure = await self._job(session, graph, references, progress, chat_model)
            return self._outcome(generated, failure)
        async with self._lock():
            if self.recovery_error:
                raise RuntimeError(self.recovery_error)
            session = _Session(progress)
            self._adopt(session)
            generated, failure = await self._job(session, graph, references, progress, chat_model)
            cancelled = await self._run_shielded(self._finish(session))
            failure = failure or cancelled
        return self._outcome(generated, failure)
