"""Opt-in, bounded live audit of keep-warm and round batching for local images (bra-01x.4).

Runs production ``LocalImages`` against a private ComfyUI child on loopback port
8190 (started with production comfyui.service's memory flags) and a private
llama-router with a scratch Gemma preset. It never controls comfyui.service, the
production llama-router, or any other client's model.

Each case is one reply asking for three images, compared with the per-job
baseline (keep-warm off, no batching) on the same path:

- exclusive path: three separate handoffs against one batched handoff;
- sharing path: three freed jobs against one batch that stays warm, followed by
  the idle window and its release.

The sampler records card VRAM, system memory and the ComfyUI RSS every 0.5 s,
and every sharing decision records its inputs.

Run only inside an authorized GPU window:

    .venv/bin/python scripts/audit_local_image_keep_warm.py --run --output DIR
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
from blackrose.local_images import available_vram
from audit_local_image_gpu import FixtureImages, ScratchService, snapshot
from audit_local_image_handoff import GEMMA_ID, ROUTER_TIMEOUT, RouterBackend, vram_used, write_preset
from check_router_deployed_contract import ScratchRouterService, _free_loopback_port
from prepare_local_image_gpu_probe import comfy_software_identity, workflow_inventory

IMAGES_PER_REPLY = 3
IDLE_WINDOW = 120
# name, keep_chat_loaded, idle_release seconds, batched
CASES = [
    ('exclusive_per_job_baseline', False, 0, False),
    ('exclusive_batched', False, 0, True),
    ('shared_per_job_baseline', True, 0, False),
    ('shared_warm_batched', True, IDLE_WINDOW, True),
]
PROMPTS = ['A red cube on a plain white background', 'A blue sphere on a plain white background',
           'A green cone on a plain white background']


class GateRecordingImages(FixtureImages):
    """Records the inputs and outcome of every sharing decision."""

    decisions: list

    async def _can_share(self, references, chat_model):
        stats = await self._json('GET', '/system_stats')
        try:
            free = available_vram(stats['devices'][0]['vram_total'])
            ram_free = stats['system']['ram_free']
        except (KeyError, IndexError, TypeError):
            free = ram_free = None
        result = await super()._can_share(references, chat_model)
        self.decisions.append({'time': time.time(), 'chat_model': chat_model,
                               'keep_chat_loaded': self.keep_chat_loaded, 'free_vram': free,
                               'ram_free': ram_free, 'shared': result})
        return result


def summarize(samples):
    used = [u for u in (vram_used(s) for s in samples) if u is not None]
    rss = [s['rss_bytes'] for s in samples if s.get('rss_bytes') is not None]
    available = [s['memory'].get('MemAvailable') for s in samples if s.get('memory')]
    available = [a for a in available if a is not None]
    return {'vram_peak_used': max(used) if used else None,
            'vram_min_used': min(used) if used else None,
            'comfy_rss_peak': max(rss) if rss else None,
            'mem_available_min': min(available) if available else None}


async def audit(root: Path, comfy: Path, workflows: Path) -> dict:
    report = {'scratch_root': str(root), 'software': comfy_software_identity(comfy),
              'workflows': workflow_inventory(workflows, comfy / 'models'),
              'router_handoff': 'private scratch router only; no production router or comfyui.service',
              'images_per_reply': IMAGES_PER_REPLY, 'idle_window': IDLE_WINDOW,
              'cases': [], 'samples': []}
    service = image = router = router_service = sampler = None

    def save():
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    async def sample():
        while True:
            report['samples'].append(snapshot(service.process if service else None))
            await asyncio.sleep(.5)

    def last_vram():
        return vram_used(report['samples'][-1]) if report['samples'] else None

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
        image = GateRecordingImages('http://127.0.0.1:8190', workflows, backend,
                                    lock_path=root / 'audit.lock', timeout=900, poll_interval=.25)
        image.decisions = []
        service = ScratchService(image._client, root, comfy, owned=False)
        image.service = service
        sampler = asyncio.create_task(sample())
        report['startup_stats'] = await service.start()
        report['comfy_pid'] = service.process.pid
        save()

        for name, keep, idle, batched in CASES:
            await router.load(GEMMA_ID)
            await service.free()  # Every case starts with no image model loaded.
            await asyncio.sleep(3)
            image.keep_chat_loaded = keep
            image.idle_release = idle
            image.decisions = []
            backend.events.clear()
            window_start = len(report['samples'])
            progress: list[str] = []
            entry = {'name': name, 'keep_chat_loaded': keep, 'idle_release': idle,
                     'batched': batched, 'vram_before': last_vram(), 'jobs': []}
            started = time.monotonic()
            hold = image.batch() if batched else contextlib.nullcontext()
            async with hold:
                for prompt in PROMPTS[:IMAGES_PER_REPLY]:
                    job_started = time.monotonic()
                    result = await image.generate(prompt, '1:1', on_progress=progress.append,
                                                  chat_model=GEMMA_ID)
                    entry['jobs'].append({'seconds': time.monotonic() - job_started,
                                          'image_bytes': len(result.data),
                                          'vram_after_job': last_vram()})
            entry['reply_seconds'] = time.monotonic() - started
            entry['status_after_reply'] = image.status()
            entry['vram_after_reply'] = last_vram()
            if entry['status_after_reply']['state'] == 'loaded':
                # Let the idle window run out and the owner's timer release the model.
                warm_started = time.monotonic()
                async with asyncio.timeout(IDLE_WINDOW + 120):
                    while image.status()['state'] != 'not_loaded':  # noqa: ASYNC110 - polls another process's file
                        await asyncio.sleep(.5)
                entry['release_after_reply_seconds'] = time.monotonic() - warm_started
                await asyncio.sleep(3)
                entry['vram_after_release'] = last_vram()
            entry['progress'] = progress
            entry['decisions'] = image.decisions
            entry['router_events'] = [{'action': e['action'], 'time': e['time']} for e in backend.events]
            entry.update(summarize(report['samples'][window_start:]))
            entry['recovery_error'] = image.recovery_error
            report['cases'].append(entry)
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
        report['final'] = snapshot()
        save()


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
    result = asyncio.run(asyncio.wait_for(audit(args.output, Path(os.environ.get('COMFYUI_ROOT', Path.home() / 'ComfyUI')),
                                                Path(load_config().local_image_workflows).expanduser()), 3600))
    print(json.dumps({'completed': result.get('completed', False), 'cases': [
        {k: c.get(k) for k in ('name', 'reply_seconds', 'release_after_reply_seconds',
                               'vram_before', 'vram_peak_used', 'vram_after_reply',
                               'vram_after_release', 'comfy_rss_peak')}
        | {'shared': [d['shared'] for d in c['decisions']],
           'events': [e['action'] for e in c['router_events']]}
        for c in result['cases']]}, indent=2), flush=True)
