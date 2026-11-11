"""Opt-in, bounded live handoff audit for the local image GPU handoff (bra-01x.3).

Runs the production ``LocalImages`` handoff against a private ComfyUI child on
loopback port 8190 and a private llama-router with its own scratch preset. It
never controls ``comfyui.service``, the production llama-router, or any other
client's model. Each case records VRAM before, at peak and after, the router's
events, the restored state, and whether the job shared memory or released it.

Run only inside an authorized GPU window:

    .venv/bin/python scripts/audit_local_image_handoff.py --run --output DIR
"""
from __future__ import annotations

import argparse
import asyncio
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
from blackrose.local_images import GIB
from audit_local_image_gpu import AuditRouter, FixtureImages, ScratchService, snapshot
from check_router_deployed_contract import ScratchRouterService, _free_loopback_port, _source_profile
from prepare_local_image_gpu_probe import comfy_software_identity, workflow_inventory

# The sharing whitelist in local_images.SHARING_MODELS is keyed by this id.
GEMMA_ID = 'gemma-4-e4b-it'
# The Gemma 4 E4B GGUF to load; set GEMMA_GGUF to its path.
GEMMA_GGUF = Path(os.environ.get('GEMMA_GGUF', '~/models/gemma-4-E4B-it-Q4_K_M.gguf')).expanduser()
SHARP_ID = 'sharp-minicpm5-2b'
SHARP_SOURCE = Path(load_config().router_preset).expanduser()
CLOUD_ID = 'opencode-go/gemma-4-e4b-it'
ROUTER_TIMEOUT = 180
# Each case: name, chat model, keep_chat_loaded, initial router status, action.
CASES = [
    ('gemma_resident_keep_flag', GEMMA_ID, True, 'loaded', 'success'),
    ('gemma_resident_exclusive', GEMMA_ID, False, 'loaded', 'success'),
    ('sharp_resident_keep_flag', SHARP_ID, True, 'loaded', 'success'),
    ('gemma_resident_cancel', GEMMA_ID, False, 'loaded', 'cancel'),
    ('cloud_chat_selected', CLOUD_ID, True, None, 'success'),
]


def write_preset(path: Path) -> None:
    """Scratch presets only: Gemma at the measured 64k context, Sharp at the bra-pzz 2k context."""
    sharp = _source_profile(SHARP_SOURCE, SHARP_ID)
    overrides = {'ctx-size': '2048', 'n-gpu-layers': '999', 'parallel': '1',
                 'load-on-startup': 'false', 'stop-timeout': '20'}
    lines = ['version = 1', '', f'[{GEMMA_ID}]', f'model = {GEMMA_GGUF}', 'ctx-size = 64000',
             'n-gpu-layers = 999', 'parallel = 1', 'jinja = true', 'load-on-startup = false',
             'stop-timeout = 20', '', f'[{SHARP_ID}]']
    lines += [f'{key} = {value}' for key, value in sharp.items() if key not in overrides]
    lines += [f'{key} = {value}' for key, value in overrides.items()]
    path.write_text('\n'.join(lines) + '\n')


def vram_used(sample) -> int | None:
    """Used bytes on the discrete card (>= 4 GiB), or None when the sample has no such card."""
    cards = [d['used_bytes'] for d in sample['drm'] if d['total_bytes'] >= 4 * GIB]
    return cards[0] if len(cards) == 1 else None


class RouterBackend(AuditRouter):
    """Records load/unload events and exposes the catalog that LocalImages reads."""

    async def models(self):
        return await self.client.models()


async def audit(root: Path, comfy: Path, workflows: Path) -> dict:
    report = {'scratch_root': str(root), 'software': comfy_software_identity(comfy),
              'workflows': workflow_inventory(workflows, comfy / 'models'),
              'router_handoff': 'private scratch router only; no production router or comfyui.service',
              'cases': [], 'samples': []}
    service = image = router = router_service = sampler = None

    def save():
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    async def sample():
        while True:
            report['samples'].append(snapshot(service.process if service else None))
            await asyncio.sleep(.5)

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
        report['scratch_router'] = {'endpoint': endpoint, 'chat_models': [GEMMA_ID, SHARP_ID]}

        async def statuses():
            return {row['id']: (row.get('status') or {}).get('value') for row in await router.models()
                    if row['id'] in (GEMMA_ID, SHARP_ID)}

        backend = RouterBackend(router)
        image = FixtureImages('http://127.0.0.1:8190', workflows, backend,
                              lock_path=root / 'audit.lock', timeout=900, poll_interval=.25)
        service = ScratchService(image._client, root, comfy, owned=False)
        image.service = service
        report['router_pid'] = router_service._pid
        sampler = asyncio.create_task(sample())
        report['startup_stats'] = await service.start()
        report['comfy_pid'] = service.process.pid
        save()

        for name, chat, keep, initial, action in CASES:
            if initial is not None:
                # Already-loaded models from earlier cases stay loaded; each case restores its own.
                await router.load(chat)
            before = await statuses()
            image.keep_chat_loaded = keep
            backend.events.clear()
            window_start = len(report['samples'])
            progress: list[str] = []
            entry = {'name': name, 'chat_model': chat, 'keep_chat_loaded': keep, 'action': action,
                     'status_before': before,
                     'vram_before': vram_used(report['samples'][-1]) if report['samples'] else None}
            started = time.monotonic()
            task = asyncio.create_task(image.generate('A red cube on a plain white background', '1:1',
                                                     on_progress=progress.append, chat_model=chat))
            try:
                if action == 'cancel':
                    async with asyncio.timeout(300):
                        while True:
                            queue = (await image._client.get('/queue')).json()
                            if queue['queue_running']:
                                break
                            await asyncio.sleep(.25)
                    await asyncio.sleep(2)
                    entry['interrupt_at'] = time.time()
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        entry['outcome'] = 'cancelled'
                else:
                    result = await task
                    entry['outcome'] = 'success'
                    entry['image_bytes'] = len(result.data)
            except Exception as error:  # Recorded as evidence, then the audit continues only if recovered.
                entry['outcome'] = type(error).__name__
                entry['error'] = str(error)
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass
            entry['seconds'] = time.monotonic() - started
            entry['progress'] = progress
            entry['share_decision'] = ('shared' if any('stays loaded' in p for p in progress)
                                       else 'released' if any('Preparing memory' in p for p in progress)
                                       else 'none')
            entry['router_events'] = [{'action': e['action'], 'time': e['time']} for e in backend.events]
            entry['status_after'] = await statuses()
            entry['restored_as_before'] = entry['status_after'] == before
            window = report['samples'][window_start:]
            used = [u for u in (vram_used(s) for s in window) if u is not None]
            entry['vram_peak_used'] = max(used) if used else None
            entry['vram_after'] = vram_used(report['samples'][-1]) if report['samples'] else None
            entry['recovery_error'] = image.recovery_error
            report['cases'].append(entry)
            save()
            if image.recovery_error:
                raise RuntimeError(f'{name} left recovery incomplete: {image.recovery_error}')
            if action == 'success' and entry['outcome'] != 'success':
                raise RuntimeError(f'{name} did not complete: {entry.get("error")}')
        report['completed'] = True
        return report
    except BaseException as error:
        report['audit_error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        if service:
            await service.stop()
        if sampler:
            sampler.cancel()
            try:
                await sampler
            except asyncio.CancelledError:
                pass
        if image:
            await image.close()
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
    print(json.dumps({'completed': result.get('completed', False),
                      'cases': [{k: c.get(k) for k in ('name', 'outcome', 'share_decision',
                                                       'restored_as_before', 'vram_peak_used')}
                                for c in result['cases']]}, indent=2), flush=True)
