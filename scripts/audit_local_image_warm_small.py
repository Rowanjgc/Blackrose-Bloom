"""Opt-in, bounded live audit of warm shared image-model reuse with a substituted small model (bra-8kb).

The production sharing path keeps a ComfyUI image model warm beside Gemma 4 E4B
at 64k for ``IDLE_RELEASE_SECONDS``. With Qwen Image on a 30.5 GiB machine the
RAM gate (ComfyUI ``ram_free`` >= ``SHARING_RAM``) refuses every shared job after
ComfyUI's first one, so the warm path has never run live. The gate's input is
ComfyUI's own reading, so a smaller image model lets the **unmodified** gate pass.

Only the image workflow is substituted, inside this harness: a stock ComfyUI
checkpoint graph (SDXL by default, already on disk) replaces the Qwen Image
graph. ``SHARING_RAM``, both VRAM headroom checks, ``SHARING_MODELS`` and every
other rule are production code. Decisions are recorded, never overridden.

Cases, in order, on one private ComfyUI child (loopback port 8190, production
comfyui.service's memory flags) and a private llama-router with Gemma at 64k:

- ``warm_reuse``: a sharing reply that stays warm, a follow-up inside the window
  that reuses it without a chat-model reload, then the window expiring.
- ``refusal_recheck``: a sharing reply that stays warm, then a ballast process
  holds enough anonymous memory to put ComfyUI's ``ram_free`` under the gate;
  the follow-up's reuse recheck fails and production releases the warm model at
  once, then decides afresh.

A watchdog cancels the run, through the production Stop path, if MemAvailable
falls below 3 GiB. It never controls comfyui.service, the production
llama-router, or any other client's model.

Run only inside an authorized GPU window:

    .venv/bin/python scripts/audit_local_image_warm_small.py --run --output DIR
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from blackrose.backend.router import RouterClient
from blackrose.local_images import GIB, SHARING_RAM, LocalImages, available_vram
from audit_local_image_gpu import ScratchService, snapshot
from audit_local_image_handoff import GEMMA_ID, ROUTER_TIMEOUT, RouterBackend, vram_used, write_preset
from audit_local_image_ram_gate import ABORT_BELOW, meminfo, vmstat
from check_router_deployed_contract import ScratchRouterService, _free_loopback_port
from prepare_local_image_gpu_probe import comfy_software_identity

DEFAULT_CHECKPOINT = Path.home() / 'Dev/GeneralAssetPipeline/models/image-generation/sd_xl_base_1.0.safetensors'
IDLE_WINDOW = 120
FOLLOW_UP_AFTER = 20
# How far under SHARING_RAM the ballast pushes ComfyUI's ram_free for the refusal case.
BALLAST_MARGIN = 2 * GIB
# Card VRAM within this of the Gemma-resident baseline counts as released.
BASELINE_TOLERANCE = GIB // 2
CASES = ['warm_reuse', 'refusal_recheck']
PROMPTS = ['A red cube on a plain white background', 'A blue sphere on a plain white background']
BALLAST = """
import sys, time
size = int(sys.argv[1])
block = bytearray(b'\\x01') * size  # Written, so every page is resident and anonymous.
print('ready', flush=True)
while True:
    time.sleep(60)
"""


class SmallModelImages(LocalImages):
    """Production ``LocalImages`` with only the image graph substituted; records each decision.

    The graph keeps the node ids production reads: ``6`` is the sampler and ``8``
    the SaveImage whose output ``_wait`` collects.
    """

    checkpoint: str
    decisions: list
    prompt_ids: list
    seed = 8_000_000

    def _workflow(self, prompt, aspect_ratio, editing):
        if editing:
            raise RuntimeError('This audit substitutes text to image only')
        self.seed += 1
        return {
            '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': self.checkpoint}},
            '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': prompt, 'clip': ['1', 1]}},
            '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': '', 'clip': ['1', 1]}},
            '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 512, 'height': 512, 'batch_size': 1}},
            '6': {'class_type': 'KSampler', 'inputs': {
                'model': ['1', 0], 'positive': ['2', 0], 'negative': ['3', 0], 'latent_image': ['5', 0],
                'seed': self.seed, 'steps': 6, 'cfg': 5.0, 'sampler_name': 'euler',
                'scheduler': 'normal', 'denoise': 1.0}},
            '7': {'class_type': 'VAEDecode', 'inputs': {'samples': ['6', 0], 'vae': ['1', 2]}},
            '8': {'class_type': 'SaveImage', 'inputs': {'images': ['7', 0], 'filename_prefix': 'audit/small'}},
        }

    _last_stats = None

    async def _json(self, method, path, **kwargs):
        if method == 'POST' and path == '/prompt':
            self.prompt_ids.append(kwargs['json']['prompt_id'])
        result = await super()._json(method, path, **kwargs)
        if method == 'GET' and path == '/system_stats':
            self._last_stats = result  # The exact reading the production check compares.
        return result

    def _record(self, site, stats, result):
        try:
            free = available_vram(stats['devices'][0]['vram_total'])
            ram_free = stats['system']['ram_free']
        except (KeyError, IndexError, TypeError):
            free = ram_free = None
        self.decisions.append({'time': time.time(), 'site': site, 'free_vram': free,
                               'ram_free': ram_free, 'mem': meminfo(), 'shared': result})

    async def _can_share(self, references, chat_model):
        self._last_stats = None
        result = await super()._can_share(references, chat_model)
        self._record('share', self._last_stats, result)
        return result

    async def _ram_headroom(self, stats=None):
        if stats is None:
            self._last_stats = None
        result = await super()._ram_headroom(stats)
        if stats is None:  # With stats it is the RAM term of _can_share, recorded there.
            self._record(self._site, self._last_stats, result)
        return result

    _site = 'keep'

    async def _prepare_memory(self, *args):
        self._site = 'reuse'  # The only _ram_headroom call without stats here is the reuse recheck.
        try:
            return await super()._prepare_memory(*args)
        finally:
            self._site = 'keep'  # _finish's keep-warm check.


class RecordingService(ScratchService):
    """Records every ``/free`` of the image models with a snapshot."""

    events: list

    async def free(self):
        self.events.append({'action': 'free', 'time': time.time()})
        await super().free()


def summarize(samples, before):
    used = [u for u in (vram_used(s) for s in samples) if u is not None]
    rss = [s['rss_bytes'] for s in samples if s.get('rss_bytes') is not None]
    available = [s['mem']['MemAvailable'] for s in samples]
    swap = [s['mem']['SwapFree'] for s in samples]
    first, last = (samples[0]['vmstat'], samples[-1]['vmstat']) if samples else ({}, {})
    return {'vram_peak_used': max(used, default=None), 'vram_min_used': min(used, default=None),
            'comfy_rss_peak': max(rss, default=None), 'comfy_rss_min': min(rss, default=None),
            'mem_available_before': before['MemAvailable'],
            'mem_available_min': min(available, default=None),
            'swap_free_before': before['SwapFree'], 'swap_free_min': min(swap, default=None),
            'pages_swapped_in': last.get('pswpin', 0) - first.get('pswpin', 0),
            'pages_swapped_out': last.get('pswpout', 0) - first.get('pswpout', 0)}


def cached_nodes(history: dict, prompt_id: str) -> list[str] | None:
    """Node ids ComfyUI served from its cache for one prompt, from its history messages."""
    item = history.get(prompt_id) or {}
    for kind, data in item.get('status', {}).get('messages', []):
        if kind == 'execution_cached':
            return sorted(data.get('nodes', []))
    return None


def checks(entry: dict) -> dict:
    """The bead's acceptance, evaluated from what the case recorded."""
    sites = [(d['site'], d['shared']) for d in entry['decisions']]
    events = [e['action'] for e in entry['router_events']]
    first, second = entry['replies'][0], entry['replies'][1]
    result = {'first_reply_shared_naturally': ('share', True) in first['decisions'],
              'first_reply_kept_warm': ('keep', True) in first['decisions']
                                       and first['status_after']['state'] == 'loaded',
              'every_decision_has_inputs': all(d['ram_free'] is not None and d['free_vram'] is not None
                                               for d in entry['decisions']) and bool(sites)}
    if entry['name'] == 'warm_reuse':
        result.update(
            follow_up_reused=('reuse', True) in second['decisions'] and second['status_after']['state'] == 'loaded',
            no_chat_reload=events == [],
            no_image_free_before_expiry=all(e['time'] >= entry['expiry_started'] for e in entry['free_events']),
            checkpoint_cached_on_reuse='1' in (second.get('cached_nodes') or []),
            expiry_released=entry.get('status_after_expiry') == 'not_loaded',
            vram_back_at_baseline=(entry.get('vram_after_release') is not None and entry['vram_before'] is not None
                                   and abs(entry['vram_after_release'] - entry['vram_before']) <= BASELINE_TOLERANCE))
    else:
        result.update(
            ballast_put_gate_under=entry.get('ballast', {}).get('ram_free_after', SHARING_RAM) < SHARING_RAM,
            reuse_refused=('reuse', False) in second['decisions'],
            warm_model_freed_before_new_decision=bool(entry['free_events'])
                and entry['free_events'][0]['time'] <= min((d['time'] for d in entry['decisions']
                                                            if d['site'] == 'share' and d['time'] >= second['started_at']),
                                                           default=0),
            released_after_follow_up=second['status_after']['state'] == 'not_loaded',
            chat_unloaded_and_restored_once=events == ['unload', 'load'],
            vram_back_at_baseline=(entry.get('vram_after_release') is not None and entry['vram_before'] is not None
                                   and abs(entry['vram_after_release'] - entry['vram_before']) <= BASELINE_TOLERANCE))
    return result


async def hold_ballast(size: int) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', BALLAST, str(size),
                                                   stdout=asyncio.subprocess.PIPE)
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 120)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if line.strip() != b'ready':
        process.kill()
        await process.wait()
        raise RuntimeError('Ballast process did not allocate')
    return process


async def audit(root: Path, comfy: Path, checkpoint: Path, checkpoint_bytes: int, cases: list[str],
                report: dict) -> dict:
    report.update({'scratch_root': str(root), 'software': comfy_software_identity(comfy),
                   'image_model': {'substituted': True, 'checkpoint': str(checkpoint),
                                   'bytes': checkpoint_bytes,
                                   'why': 'Qwen Image weights keep ComfyUI ram_free under SHARING_RAM on '
                                          'this machine; production workflow layout replaced in the harness only'},
                   'router_handoff': 'private scratch router only; no production router or comfyui.service',
                   'overridden': 'LocalImages._workflow only; every sharing check is production code',
                   'sharing_ram': SHARING_RAM, 'abort_below': ABORT_BELOW, 'idle_window': IDLE_WINDOW,
                   'baseline': {'mem': meminfo(), 'vmstat': vmstat(), 'snapshot': snapshot()},
                   'cases': [], 'samples': []})
    service = image = router = router_service = sampler = ballast = None

    def save():
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    async def sample():
        while True:
            report['samples'].append(snapshot(service.process if service else None)
                                     | {'mem': meminfo(), 'vmstat': vmstat()})
            await asyncio.sleep(.5)

    def last_vram():
        return vram_used(report['samples'][-1]) if report['samples'] else None

    async def ram_free():
        return (await image._json('GET', '/system_stats'))['system']['ram_free']

    async def reply(entry, prompt):
        progress: list[str] = []
        decisions_before, prompts_before = len(image.decisions), len(image.prompt_ids)
        started, started_at = time.monotonic(), time.time()
        result = await image.generate(prompt, '1:1', on_progress=progress.append, chat_model=GEMMA_ID)
        done = {'started_at': started_at, 'seconds': time.monotonic() - started, 'image_bytes': len(result.data),
                'cleanup_warning': result.cleanup_warning, 'progress': progress,
                'decisions': [(d['site'], d['shared']) for d in image.decisions[decisions_before:]],
                'status_after': image.status(), 'vram_after': last_vram(), 'mem_after': meminfo()}
        if len(image.prompt_ids) > prompts_before:
            prompt_id = image.prompt_ids[-1]
            done['prompt_id'] = prompt_id
            history = await image._json('GET', f'/history/{prompt_id}')
            done['cached_nodes'] = cached_nodes(history, prompt_id)
        entry['replies'].append(done)
        save()

    async def wait_released(entry):
        entry['expiry_started'] = time.time()
        started = time.monotonic()
        async with asyncio.timeout(IDLE_WINDOW + 120):
            while image.status()['state'] != 'not_loaded':  # noqa: ASYNC110 - polls another process's file
                await asyncio.sleep(.5)
        entry['release_after_last_reply_seconds'] = time.monotonic() - started
        entry['status_after_expiry'] = image.status()['state']

    try:
        for folder in ('input', 'output', 'temp', 'user', 'cache', 'state'):
            (root / folder).mkdir()
        (root / 'models.yaml').write_text(json.dumps({'small': {
            'base_path': str(checkpoint.parent.parent), 'checkpoints': checkpoint.parent.name}}))
        for key in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
            os.environ[key] = str(root / key.lower())
        preset = root / 'scratch-router.ini'
        write_preset(preset)
        endpoint = f'http://127.0.0.1:{_free_loopback_port()}'
        router_service = ScratchRouterService(SimpleNamespace(
            endpoint=endpoint, llama_server_path=str(Path.home() / '.local/bin/llama-server'),
            router_preset=str(preset), router_start_timeout=ROUTER_TIMEOUT,
            router_stop_timeout=30, router_api_key_file=''))
        router = RouterClient(endpoint, timeout=ROUTER_TIMEOUT, service=router_service)
        report['scratch_router'] = {'endpoint': endpoint, 'chat_model': GEMMA_ID, 'context': 64000}
        backend = RouterBackend(router)
        image = SmallModelImages('http://127.0.0.1:8190', root, backend, lock_path=root / 'audit.lock',
                                 timeout=900, poll_interval=.25, idle_release=IDLE_WINDOW)
        image.checkpoint, image.decisions, image.prompt_ids = checkpoint.name, [], []
        service = RecordingService(image._client, root, comfy, owned=False)
        service.events = []
        image.service = service
        sampler = asyncio.create_task(sample())
        report['startup_stats'] = await service.start()
        report['comfy_pid'] = service.process.pid
        save()

        for name in cases:
            await router.load(GEMMA_ID)
            await service.free()  # Every case starts with no image model loaded.
            await asyncio.sleep(3)
            image.decisions = []
            service.events = []
            backend.events.clear()
            window_start = len(report['samples'])
            before = meminfo()
            entry = {'name': name, 'vram_before': last_vram(), 'ram_free_before': await ram_free(),
                     'replies': [], 'decisions': image.decisions, 'free_events': service.events}
            report['cases'].append(entry)  # Filled in place, so an abort keeps what was measured.
            started = time.monotonic()
            await reply(entry, PROMPTS[0])
            if entry['replies'][0]['status_after']['state'] != 'loaded':
                entry['stopped'] = 'first reply was not kept warm; the production gate refused naturally'
            else:
                await asyncio.sleep(FOLLOW_UP_AFTER)
                if name == 'refusal_recheck':
                    current = await ram_free()
                    size = max(0, current - SHARING_RAM + BALLAST_MARGIN)
                    if meminfo()['MemAvailable'] - size < ABORT_BELOW + 2 * GIB:
                        raise RuntimeError('Ballast would leave too little memory; refusal case not run')
                    ballast = await hold_ballast(size)
                    await asyncio.sleep(2)
                    entry['ballast'] = {'bytes': size, 'ram_free_before': current,
                                        'ram_free_after': await ram_free(), 'mem_after': meminfo()}
                await reply(entry, PROMPTS[1])
                if ballast:
                    ballast.kill()
                    await ballast.wait()
                    ballast = None
                if image.status()['state'] == 'loaded':
                    await wait_released(entry)
                else:
                    entry['expiry_started'] = time.time()
            await asyncio.sleep(3)
            entry['vram_after_release'] = last_vram()
            entry['seconds'] = time.monotonic() - started
            entry['router_events'] = [{'action': e['action'], 'time': e['time']} for e in backend.events]
            entry.update(summarize(report['samples'][window_start:], before))
            entry['recovery_error'] = image.recovery_error
            if 'stopped' not in entry:
                entry['checks'] = checks(entry)
            save()
            if image.recovery_error:
                raise RuntimeError(f'{name} left recovery incomplete: {image.recovery_error}')
            if 'stopped' in entry:
                break
        report['completed'] = all('checks' in c for c in report['cases']) and len(report['cases']) == len(cases)
        return report
    except BaseException as error:
        report['audit_error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        if ballast:
            ballast.kill()
            await ballast.wait()
        if image:
            await image.close()
        if service:
            await service.stop()
        if sampler:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
        if router:
            await router.aclose()
        elif router_service:
            await router_service.close()
        await asyncio.sleep(2)
        report['final'] = {'mem': meminfo(), 'vmstat': vmstat(), 'snapshot': snapshot()}
        save()


async def main(root: Path, checkpoint: Path, checkpoint_bytes: int, cases: list[str]) -> dict:
    report: dict = {}
    task = asyncio.create_task(audit(root, Path(os.environ.get('COMFYUI_ROOT', Path.home() / 'ComfyUI')), checkpoint, checkpoint_bytes, cases,
                                     report))

    async def watchdog():
        while not task.done():
            available = meminfo()['MemAvailable']
            if available < ABORT_BELOW:
                report['aborted'] = {'time': time.time(), 'mem_available': available}
                task.cancel()
                return
            await asyncio.sleep(.25)

    guard = asyncio.create_task(watchdog())
    try:
        async with asyncio.timeout(3600):
            return await task
    except asyncio.CancelledError:
        if 'aborted' in report:
            return report
        raise
    finally:
        guard.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await guard


def preflight(checkpoint: Path) -> str | None:
    """Why the unmodified gate cannot be expected to pass now, or None.

    ComfyUI runs with --disable-smart-memory, so after every prompt it offloads the
    weights to system RAM: the keep-warm check sees ram_free minus roughly the
    checkpoint. Starting below that only measures a refusal.
    """
    need = SHARING_RAM + checkpoint.stat().st_size + GIB
    available = meminfo()['MemAvailable']
    if available < need:
        return (f'MemAvailable is {available / GIB:.1f} GiB; keeping {checkpoint.name} warm under the '
                f'{SHARING_RAM / GIB:.0f} GiB gate needs about {need / GIB:.1f} GiB. Free memory or pass --force.')
    return None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--run', action='store_true', required=True,
                        help='Confirm an authorized dedicated GPU window')
    parser.add_argument('--output', type=Path, required=True, help='Empty directory for the scratch root and report')
    parser.add_argument('--checkpoint', type=Path, default=DEFAULT_CHECKPOINT,
                        help='A ComfyUI CheckpointLoaderSimple checkpoint (SD1.5/SDXL class)')
    parser.add_argument('--cases', nargs='+', choices=CASES, default=CASES,
                        help='Cases to run, in order, each on a fresh image model (default: all)')
    parser.add_argument('--force', action='store_true', help='Run even when the memory preflight predicts a refusal')
    args = parser.parse_args()
    problem = preflight(args.checkpoint)
    if problem and not args.force:
        raise SystemExit(problem)
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise SystemExit('--output must be empty')
    print(f'Audit artifacts: {args.output}', flush=True)
    result = asyncio.run(main(args.output, args.checkpoint, args.checkpoint.stat().st_size, args.cases))
    print(json.dumps({'completed': result.get('completed', False), 'aborted': result.get('aborted'),
                      'error': result.get('audit_error'), 'cases': [{
                          'name': c['name'], 'stopped': c.get('stopped'),
                          'replies_s': [round(r['seconds'], 1) for r in c['replies']],
                          'decisions': [r['decisions'] for r in c['replies']],
                          'ram_free_gib': [round(d['ram_free'] / GIB, 2) for d in c['decisions'] if d['ram_free']],
                          'events': [e['action'] for e in c.get('router_events', [])],
                          'release_s': c.get('release_after_last_reply_seconds'),
                          'mem_available_min_gib': round((c.get('mem_available_min') or 0) / GIB, 2),
                          'vram_gib': [round((v or 0) / GIB, 2) for v in (c['vram_before'], c.get('vram_peak_used'),
                                                                           c.get('vram_after_release'))],
                          'checks': c.get('checks')} for c in result.get('cases', [])]}, indent=2), flush=True)
