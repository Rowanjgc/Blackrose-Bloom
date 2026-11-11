"""Opt-in, bounded live GPU audit using only a private ComfyUI child.

Run after arranging a GPU window. Never controls installed systemd services or
the production chat router. Artifacts are retained, including crash files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from blackrose.config import load_config  # noqa: E402
from blackrose.local_images import ComfyService, GIB, LocalImages
from blackrose.backend.router import RouterClient
from check_router_deployed_contract import (
    MODEL_ID, ScratchRouterService, _free_loopback_port, _source_profile, _write_cpu_preset,
)
from prepare_local_image_gpu_probe import (
    comfy_software_identity, drm_snapshot, memory_snapshot, workflow_inventory,
)


class AuditRouter:
    def __init__(self, client):
        self.client = client
        self.events = []

    async def unload(self, model):
        await self.client.unload(model)
        self.events.append({'action': 'unload', 'time': time.time(),
                            'snapshot': snapshot()})

    async def load(self, model):
        await self.client.load(model)
        self.events.append({'action': 'load', 'time': time.time(),
                            'snapshot': snapshot()})


class FixtureImages(LocalImages):
    invalid = False
    fixture_seed = 123456

    async def _json(self, method, path, **kwargs):
        if method == 'POST' and path == '/prompt':
            self.audit_prompt_id = kwargs['json']['prompt_id']
        return await super()._json(method, path, **kwargs)

    def _workflow(self, *args):
        graph = super()._workflow(*args)
        graph['5']['inputs'].update(width=512, height=512, batch_size=1)
        self.fixture_seed += 1
        graph['6']['inputs'].update(steps=4, seed=self.fixture_seed)
        graph['8']['inputs']['filename_prefix'] = 'audit/fixture'
        if self.invalid:
            graph['6']['class_type'] = 'AuditDeliberatelyMissingNode'
        return graph


class ScratchService(ComfyService):
    """Own only the Popen handle created here; never invoke systemctl."""

    def __init__(self, client, root, comfy, *, owned):
        super().__init__(client)
        self.root, self.comfy, self.owned = root, comfy, owned
        self.process = None
        self.log = None

    async def start(self):
        env = dict(os.environ)
        env.update(HIP_VISIBLE_DEVICES='0', PYTHONDONTWRITEBYTECODE='1',
                   XDG_CACHE_HOME=str(self.root / 'cache'),
                   XDG_STATE_HOME=str(self.root / 'state'),
                   TMPDIR=str(self.root / 'temp'))
        args = [str(self.comfy / '.venv-rocm71/bin/python'), '-B',
                str(self.comfy / 'main.py'), '--listen', '127.0.0.1', '--port', '8190',
                '--base-directory', str(self.root),
                '--input-directory', str(self.root / 'input'),
                '--output-directory', str(self.root / 'output'),
                '--temp-directory', str(self.root / 'temp'),
                '--user-directory', str(self.root / 'user'),
                '--extra-model-paths-config', str(self.root / 'models.yaml'),
                '--disable-auto-launch', '--disable-all-custom-nodes',
                '--disable-smart-memory', '--disable-pinned-memory',
                '--database-url', 'sqlite:///:memory:']
        self.log = (self.root / 'comfy.log').open('ab')
        self.process = subprocess.Popen(args, cwd=self.root, env=env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        async with asyncio.timeout(120):
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError(f'Scratch Comfy exited: {self.process.returncode}')
                try:
                    response = await self.client.get('/system_stats')
                    response.raise_for_status()
                    stats = response.json()
                    assert abs(stats['devices'][0]['vram_total'] - 16 * GIB) < GIB
                    return stats
                except httpx.ConnectError:
                    await asyncio.sleep(.5)

    async def ensure_running(self):
        if self.process is None or self.process.poll() is not None:
            await self.start()
        else:
            response = await self.client.get('/system_stats')
            response.raise_for_status()

    async def stop(self, *, crash=False):
        if self.process and self.process.poll() is None:
            if crash:
                self.process.kill()
            else:
                self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), 30)
            except TimeoutError:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        if self.log:
            self.log.close()
            self.log = None

    async def release(self):
        if self.owned:
            await self.stop()
        else:
            await super().release()


def snapshot(process=None):
    rss = None
    if process and process.poll() is None:
        try:
            for line in Path(f'/proc/{process.pid}/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1]) * 1024
        except OSError:
            pass
    return {'time': time.time(), 'drm': drm_snapshot(), 'memory': memory_snapshot(),
            'rss_bytes': rss}


async def audit(root, comfy, workflows):
    report = {'scratch_root': str(root), 'software': comfy_software_identity(comfy),
              'workflows': workflow_inventory(workflows, comfy / 'models'),
              'router_handoff': 'private GPU scratch router; no production router accessed',
              'baseline': snapshot(), 'scenarios': [], 'samples': []}
    service = None
    image = None
    sampler = None
    router = None
    router_service = None

    def save():
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    async def sample():
        while True:
            report['samples'].append(snapshot(service.process if service else None))
            save()
            await asyncio.sleep(.5)

    async def running_task(task):
        async with asyncio.timeout(120):
            while not task.done():
                try:
                    response = await image._client.get('/queue')
                except httpx.ConnectError:
                    await asyncio.sleep(.25)
                    continue
                response.raise_for_status()
                if response.json()['queue_running']:
                    # This is our private child, and it accepts only our UUIDs.
                    await asyncio.sleep(2)
                    if not task.done():
                        return
                await asyncio.sleep(.1)
        raise RuntimeError('Fixture ended before active-job interruption')

    async def scenario(name, action='success'):
        entry = {'name': name, 'before': snapshot(service.process)}
        report['scenarios'].append(entry)
        save()
        started = time.monotonic()
        image.invalid = action == 'invalid'
        image.backend.events.clear()
        progress = []
        task = asyncio.create_task(image.generate('A red cube on a plain white background',
                                                 '1:1', on_progress=progress.append,
                                                 chat_model=MODEL_ID))
        try:
            if action in ('cancel', 'crash'):
                await running_task(task)
                entry['interrupt_before'] = snapshot(service.process)
                entry['interrupt_at'] = time.time()
                stopped = time.monotonic()
                if action == 'cancel':
                    task.cancel()
                else:
                    await service.stop(crash=True)
                try:
                    await task
                finally:
                    entry['stop_to_settle_seconds'] = time.monotonic() - stopped
            else:
                result = await task
                entry['image_bytes'] = len(result.data)
                entry['cleanup_warning'] = result.cleanup_warning
                entry['dimensions'] = [result.info.width, result.info.height]
        except asyncio.CancelledError:
            entry['outcome'] = 'cancelled'
        except Exception as error:
            entry['outcome'] = type(error).__name__
            entry['error'] = str(error)
        else:
            entry['outcome'] = 'success'
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            entry.update(seconds=time.monotonic() - started, progress=progress,
                         prompt_id=getattr(image, 'audit_prompt_id', None),
                         router_events=list(image.backend.events),
                         recovery_error=image.recovery_error,
                         after=snapshot(service.process),
                         files=[str(p.relative_to(root)) for folder in ('input', 'output', 'temp')
                                for p in (root / folder).rglob('*') if p.is_file()],
                         process_alive=service.process.poll() is None)
            if service.process.poll() is None:
                entry['queue_after'] = (await image._client.get('/queue')).json()
                entry['stats_after'] = (await image._client.get('/system_stats')).json()
                if entry['prompt_id']:
                    entry['history_after'] = (await image._client.get(
                        f"/history/{entry['prompt_id']}")).json()
            save()
        return entry

    try:
        for folder in ('input', 'output', 'temp', 'user', 'cache', 'state'):
            (root / folder).mkdir()
        (root / 'models.yaml').write_text(json.dumps({'installed': {
            'base_path': str(comfy / 'models'), 'diffusion_models': 'diffusion_models',
            'text_encoders': 'text_encoders', 'vae': 'vae'}}))
        gpu = [d for d in drm_snapshot() if d['total_bytes'] >= 4 * GIB]
        assert len(gpu) == 1 and gpu[0]['free_bytes'] >= 9 * GIB
        assert memory_snapshot()['available_bytes'] >= 18 * GIB
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 8190))
        source = _source_profile(Path(os.path.expanduser(load_config().router_preset)))
        preset = root / 'scratch-router.ini'
        _write_cpu_preset(preset, source)
        preset.write_text(preset.read_text().replace('n-gpu-layers = 0', 'n-gpu-layers = 999')
                          .replace('ctx-size = 16384', 'ctx-size = 2048'))
        for key in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
            os.environ[key] = str(root / key.lower())
        endpoint = f'http://127.0.0.1:{_free_loopback_port()}'
        router_service = ScratchRouterService(SimpleNamespace(
            endpoint=endpoint, llama_server_path=str(Path.home() / '.local/bin/llama-server'),
            router_preset=str(preset), router_start_timeout=120,
            router_stop_timeout=20, router_api_key_file=''))
        router = RouterClient(endpoint, timeout=120, service=router_service)
        await router.load(MODEL_ID)
        report['scratch_router'] = {'endpoint': endpoint, 'model': MODEL_ID,
                                    'source_model': source['model'],
                                    'pid': router_service._pid, 'gpu_layers': 999,
                                    'context': 2048, 'initial_catalog': await router.models()}

        async def chat_probe():
            response = await router.client.post('/v1/chat/completions', json={
                'model': MODEL_ID, 'messages': [{'role': 'user', 'content': 'Say hello.'}],
                'max_tokens': 32, 'temperature': 0, 'stream': False})
            response.raise_for_status()
            result = response.json()
            return {'status': response.status_code, 'usage': result.get('usage'),
                    'has_message': bool(result.get('choices', [{}])[0].get('message'))}

        report['chat_before'] = await chat_probe()
        image = FixtureImages('http://127.0.0.1:8190', workflows, AuditRouter(router),
                              lock_path=root / 'audit.lock', timeout=600,
                              poll_interval=.25)
        service = ScratchService(image._client, root, comfy, owned=False)
        image.service = service
        sampler = asyncio.create_task(sample())
        report['startup_stats'] = await service.start()
        report['pid'] = service.process.pid
        save()
        for name, action in [('external_success', 'success'), ('external_second_request', 'success'),
                             ('external_cancel', 'cancel'), ('invalid_node', 'invalid')]:
            result = await scenario(name, action)
            if action == 'success' and result['outcome'] != 'success':
                raise RuntimeError(f'{name} did not complete; see scenario evidence')
            if image.recovery_error:
                raise RuntimeError(f'{name} recovery failed; stopping audit')
            assert result['process_alive']
            assert not result['queue_after']['queue_running']
            assert not result['queue_after']['queue_pending']
            assert action != 'cancel' or result['outcome'] == 'cancelled'
            assert action != 'invalid' or result['outcome'] == 'RuntimeError'
        result = await scenario('external_crash', 'crash')
        assert result['outcome'] == 'RuntimeError' and image.recovery_error
        try:
            await image.generate('Must refuse while recovery is incomplete', chat_model=MODEL_ID)
        except RuntimeError as error:
            report['external_crash_recovery_guard'] = str(error) == image.recovery_error
        else:
            raise RuntimeError('Missing recovery guard after external service crash')
        await service.stop()
        await image.close()
        # A fresh app/session instance is required after the recovery guard trips.
        backend = image.backend
        await backend.load(MODEL_ID)
        image = FixtureImages('http://127.0.0.1:8190', workflows, backend,
                              lock_path=root / 'audit.lock', timeout=600,
                              poll_interval=.25, keep_chat_loaded=False)
        service.client = image._client
        image.service = service
        service.owned = True
        for name, action in [('owned_cancel', 'cancel'), ('owned_crash', 'crash'),
                             ('owned_request_after_crash', 'success')]:
            result = await scenario(name, action)
            if image.recovery_error:
                raise RuntimeError(f'{name} recovery failed; stopping audit')
            if action == 'success' and result['outcome'] != 'success':
                raise RuntimeError(f'{name} failed')
            assert not result['process_alive']
            assert action != 'cancel' or result['outcome'] == 'cancelled'
            assert action != 'crash' or result['outcome'] == 'RuntimeError'
            assert [e['action'] for e in result['router_events']] == ['unload', 'load']
        report['chat_after'] = await chat_probe()
        report['router_final_catalog'] = await router.models()
        report['completed'] = True
        save()
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
        print(json.dumps({'report': str(root / 'report.json'),
                          'completed': report.get('completed', False),
                          'error': report.get('audit_error')}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True,
                        help='Confirm an authorized dedicated GPU window')
    args = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix='bra-pzz-gpu-', dir='/tmp'))
    print(f'Audit artifacts: {scratch}', flush=True)
    asyncio.run(asyncio.wait_for(audit(scratch, Path(os.environ.get('COMFYUI_ROOT', Path.home() / 'ComfyUI')),
                                     Path(load_config().local_image_workflows).expanduser()), 1800))
