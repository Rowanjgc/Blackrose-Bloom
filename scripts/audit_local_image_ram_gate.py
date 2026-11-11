"""Opt-in, bounded live measurement of the local image sharing RAM gate (bra-01x.5).

The sharing path requires ComfyUI ``ram_free`` >= ``SHARING_RAM`` (18 GiB). This
harness bypasses only that term, inside the harness: the VRAM headroom check, the
sharing model list and every other rule stay production code. It runs production
``LocalImages`` against a private ComfyUI child on loopback port 8190 (started
with production comfyui.service's memory flags) and a private llama-router with
a scratch Gemma preset. It never controls comfyui.service, the production
llama-router, or any other client's model.

Cases, in order, on one ComfyUI process:

- ``cold_shared``: ComfyUI has never loaded the image model. One reply asks for
  three images (batched, sharing, keep-warm on), a second reply 20 s later asks
  for one more, then the 120 s window runs out.
- ``loaded_shared``: the same, after ComfyUI has kept the weights in its RAM.
- ``loaded_exclusive``: one batched three-image reply on the exclusive path, for
  comparison with whether that path needs a RAM floor of its own.

A watchdog reads /proc/meminfo every 0.25 s. If MemAvailable falls below 3 GiB
it cancels the run, which takes the production Stop path (release the image
model, restore Gemma), and then stops both scratch processes.

Run only inside an authorized GPU window:

    .venv/bin/python scripts/audit_local_image_ram_gate.py --run --output DIR
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
from blackrose.config import load_config  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from blackrose.backend.router import RouterClient
from blackrose.local_images import GIB, available_vram
from audit_local_image_gpu import FixtureImages, ScratchService, snapshot
from audit_local_image_handoff import GEMMA_ID, ROUTER_TIMEOUT, RouterBackend, vram_used, write_preset
from check_router_deployed_contract import ScratchRouterService, _free_loopback_port
from prepare_local_image_gpu_probe import comfy_software_identity, workflow_inventory

ABORT_BELOW = 3 * GIB
IDLE_WINDOW = 120
FOLLOW_UP_AFTER = 20
# name, keep_chat_loaded, idle_release seconds, follow-up reply
CASES = [
    ('cold_shared', True, IDLE_WINDOW, True),
    ('loaded_shared', True, IDLE_WINDOW, True),
    ('loaded_exclusive', False, 0, False),
]
PROMPTS = ['A red cube on a plain white background', 'A blue sphere on a plain white background',
           'A green cone on a plain white background']
FOLLOW_UP = 'A yellow torus on a plain white background'


def meminfo() -> dict:
    values = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, _, rest = line.partition(':')
        if key in ('MemAvailable', 'SwapFree', 'SwapTotal'):
            values[key] = int(rest.split()[0]) * 1024
    return values


def vmstat() -> dict:
    values = {}
    for line in Path('/proc/vmstat').read_text().splitlines():
        key, _, value = line.partition(' ')
        if key in ('pswpin', 'pswpout'):
            values[key] = int(value)
    return values


class RamGateBypassImages(FixtureImages):
    """Production sharing rules with the RAM check always passing; records each check's inputs.

    Production calls ``_ram_headroom`` for a fresh share and before reusing a warm
    shared model, so overriding it bypasses exactly the RAM term in both places.
    """

    decisions: list

    async def _ram_headroom(self, stats=None):
        if stats is None:
            stats = await self._json('GET', '/system_stats')
        try:
            free = available_vram(stats['devices'][0]['vram_total'])
            ram_free = stats['system']['ram_free']
        except (KeyError, IndexError, TypeError):
            free = ram_free = None
        self.decisions.append({'time': time.time(), 'free_vram': free, 'ram_free': ram_free,
                               'mem': meminfo(),
                               'production_gate_passes': await super()._ram_headroom(stats)})
        return True


def summarize(samples, before):
    used = [u for u in (vram_used(s) for s in samples) if u is not None]
    rss = [s['rss_bytes'] for s in samples if s.get('rss_bytes') is not None]
    available = [s['mem']['MemAvailable'] for s in samples]
    swap = [s['mem']['SwapFree'] for s in samples]
    first, last = (samples[0]['vmstat'], samples[-1]['vmstat']) if samples else ({}, {})
    return {'vram_peak_used': max(used, default=None),
            'comfy_rss_peak': max(rss, default=None), 'comfy_rss_min': min(rss, default=None),
            'mem_available_before': before['MemAvailable'],
            'mem_available_min': min(available, default=None),
            'mem_available_drop': before['MemAvailable'] - min(available) if available else None,
            'swap_free_before': before['SwapFree'], 'swap_free_min': min(swap, default=None),
            'pages_swapped_in': last.get('pswpin', 0) - first.get('pswpin', 0),
            'pages_swapped_out': last.get('pswpout', 0) - first.get('pswpout', 0)}


async def audit(root: Path, comfy: Path, workflows: Path, report: dict) -> dict:
    report.update({'scratch_root': str(root), 'software': comfy_software_identity(comfy),
                   'workflows': workflow_inventory(workflows, comfy / 'models'),
                   'router_handoff': 'private scratch router only; no production router or comfyui.service',
                   'bypassed': 'LocalImages._ram_headroom (ComfyUI ram_free >= SHARING_RAM) only',
                   'abort_below': ABORT_BELOW, 'idle_window': IDLE_WINDOW,
                   'baseline': {'mem': meminfo(), 'vmstat': vmstat(), 'snapshot': snapshot()},
                   'cases': [], 'samples': []})
    service = image = router = router_service = sampler = None

    def save():
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    async def sample():
        while True:
            report['samples'].append(snapshot(service.process if service else None)
                                     | {'mem': meminfo(), 'vmstat': vmstat()})
            await asyncio.sleep(.5)

    def last_vram():
        return vram_used(report['samples'][-1]) if report['samples'] else None

    async def reply(entry, prompts, batched, progress):
        started = time.monotonic()
        jobs = []
        hold = image.batch() if batched else contextlib.nullcontext()
        async with hold:
            for prompt in prompts:
                job_started = time.monotonic()
                result = await image.generate(prompt, '1:1', on_progress=progress.append, chat_model=GEMMA_ID)
                jobs.append({'seconds': time.monotonic() - job_started, 'image_bytes': len(result.data),
                             'vram_after_job': last_vram(), 'mem_after_job': meminfo()})
        entry['replies'].append({'images': len(prompts), 'batched': batched, 'jobs': jobs,
                                 'seconds': time.monotonic() - started,
                                 'status_after': image.status(), 'vram_after': last_vram()})

    try:
        for folder in ('input', 'output', 'temp', 'user', 'cache', 'state'):
            (root / folder).mkdir()
        (root / 'models.yaml').write_text(json.dumps({'installed': {
            'base_path': str(comfy / 'models'), 'diffusion_models': 'diffusion_models',
            'text_encoders': 'text_encoders', 'vae': 'vae'}}))
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
        report['scratch_router'] = {'endpoint': endpoint, 'chat_model': GEMMA_ID}
        backend = RouterBackend(router)
        image = RamGateBypassImages('http://127.0.0.1:8190', workflows, backend,
                                    lock_path=root / 'audit.lock', timeout=900, poll_interval=.25)
        image.decisions = []
        service = ScratchService(image._client, root, comfy, owned=False)
        image.service = service
        sampler = asyncio.create_task(sample())
        report['startup_stats'] = await service.start()
        report['comfy_pid'] = service.process.pid
        save()

        for name, keep, idle, follow_up in CASES:
            await router.load(GEMMA_ID)
            await service.free()  # Every case starts with no image model in VRAM.
            await asyncio.sleep(3)
            image.keep_chat_loaded = keep
            image.idle_release = idle
            image.decisions = []
            backend.events.clear()
            window_start = len(report['samples'])
            progress: list[str] = []
            entry = {'name': name, 'keep_chat_loaded': keep, 'idle_release': idle,
                     'vram_before': last_vram(), 'replies': [], 'progress': progress,
                     'decisions': image.decisions}
            report['cases'].append(entry)  # Filled in place, so an abort keeps what was measured.
            before = meminfo()
            entry['mem_available_before'] = before['MemAvailable']
            await reply(entry, PROMPTS, True, progress)
            if follow_up:
                await asyncio.sleep(FOLLOW_UP_AFTER)
                await reply(entry, [FOLLOW_UP], False, progress)
            if image.status()['state'] == 'loaded':
                # Let the idle window run out and the owner's timer release the model.
                warm_started = time.monotonic()
                async with asyncio.timeout(IDLE_WINDOW + 120):
                    while image.status()['state'] != 'not_loaded':  # noqa: ASYNC110 - polls another process's file
                        await asyncio.sleep(.5)
                entry['release_after_last_reply_seconds'] = time.monotonic() - warm_started
                await asyncio.sleep(3)
                entry['vram_after_release'] = last_vram()
            entry['router_events'] = [{'action': e['action'], 'time': e['time']} for e in backend.events]
            entry.update(summarize(report['samples'][window_start:], before))
            entry['recovery_error'] = image.recovery_error
            save()
            if image.recovery_error:
                raise RuntimeError(f'{name} left recovery incomplete: {image.recovery_error}')
        report['completed'] = True
        return report
    except BaseException as error:
        report['audit_error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
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


async def main(root: Path) -> dict:
    report: dict = {}
    task = asyncio.create_task(audit(root, Path(os.environ.get('COMFYUI_ROOT', Path.home() / 'ComfyUI')),
                                     Path(os.path.expanduser(load_config().local_image_workflows)), report))

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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True,
                        help='Confirm an authorized dedicated GPU window')
    parser.add_argument('--output', type=Path, required=True, help='Empty directory for the scratch root and report')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise SystemExit('--output must be empty')
    print(f'Audit artifacts: {args.output}', flush=True)
    result = asyncio.run(main(args.output))
    gib = GIB
    print(json.dumps({'completed': result.get('completed', False), 'aborted': result.get('aborted'),
                      'cases': [{
                          'name': c['name'],
                          'replies': [[round(j['seconds'], 1) for j in r['jobs']] for r in c['replies']],
                          'production_gate': [d['production_gate_passes'] for d in c['decisions']],
                          'ram_free_gib': [round(d['ram_free'] / gib, 2) for d in c['decisions'] if d['ram_free']],
                          'events': [e['action'] for e in c.get('router_events', [])],
                          'release_s': c.get('release_after_last_reply_seconds'),
                          'mem_available_min_gib': round((c.get('mem_available_min') or 0) / gib, 2),
                          'mem_available_drop_gib': round((c.get('mem_available_drop') or 0) / gib, 2),
                          'swap_free_min_gib': round((c.get('swap_free_min') or 0) / gib, 2),
                          'swapped_in_out': [c.get('pages_swapped_in'), c.get('pages_swapped_out')],
                          'comfy_rss_peak_gib': round((c.get('comfy_rss_peak') or 0) / gib, 2),
                          'vram': [round((v or 0) / gib, 2) for v in (c['vram_before'], c.get('vram_peak_used'),
                                                                       c.get('vram_after_release'))]}
                          for c in result.get('cases', [])]}, indent=2), flush=True)
