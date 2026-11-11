"""Exercise shared-router sleep/auth/contention using an isolated CPU router.

Never connects to port 18080 or edits host configuration. Uses the installed
binary and a supplied small GGUF twice under distinct aliases, with no GPU
offload. Raw router output and a compact JSON report go to --output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time

import httpx

from blackrose.backend.router import RouterClient
from blackrose.backend.service import RouterKey


async def check(args):
    args.output.mkdir(parents=True, exist_ok=True)
    report = {}
    with tempfile.TemporaryDirectory(prefix='blackrose-shared-router-') as directory:
        root = Path(directory)
        key = root / 'router.key'
        key.write_text(secrets.token_urlsafe(32) + '\n')
        key.chmod(0o600)
        auth = RouterKey(key, owned=False)
        preset = root / 'models.ini'
        preset.write_text('version = 1\n\n' + '\n'.join(
            f'[{name}]\nmodel = {args.model}\nctx-size = 2048\nn-gpu-layers = 0\n'
            'threads = 4\nthreads-batch = 4\nparallel = 1\nn-predict = 32\nsleep-idle-seconds = 2\n'
            'load-on-startup = false\nstop-timeout = 5\n'
            for name in ('probe-a', 'probe-b')))
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        endpoint = f'http://127.0.0.1:{port}'
        command = [str(args.binary), '--models-preset', str(preset), '--models-max', '1',
                   '--models-autoload', '--host', '127.0.0.1', '--port', str(port),
                   '--api-key-file', str(key), '--no-webui']
        with (args.output / 'router.log').open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                async with httpx.AsyncClient(base_url=endpoint, auth=auth,
                                             trust_env=False, timeout=90) as client:
                    async def catalog():
                        response = await client.get('/v1/models')
                        response.raise_for_status()
                        return {r['id']: r['status']['value'] for r in response.json()['data']}

                    async def wait_status(model, state):
                        async with asyncio.timeout(45):
                            while (await catalog()).get(model) != state:
                                await asyncio.sleep(0.2)

                    async with asyncio.timeout(30):
                        while True:
                            if process.poll() is not None:
                                raise RuntimeError('Scratch router exited; see router.log')
                            try:
                                report['initial'] = await catalog()
                                break
                            except httpx.ConnectError:
                                await asyncio.sleep(0.1)
                    async with httpx.AsyncClient(base_url=endpoint, trust_env=False) as anonymous:
                        report['anonymous_status'] = (await anonymous.get('/v1/models')).status_code
                    assert report['anonymous_status'] == 401

                    def body(model, tokens=8):
                        return {'model': model, 'messages': [{'role': 'user', 'content':
                                'List numbers from one to a thousand, one per line.'}],
                                'max_tokens': tokens, 'temperature': 0, 'ignore_eos': True,
                                'chat_template_kwargs': {'enable_thinking': False}}

                    response = await client.post('/v1/chat/completions', json=body('probe-a'))
                    response.raise_for_status()
                    report['cold_autoload'] = response.status_code
                    await wait_status('probe-a', 'sleeping')
                    report['idle_sleep'] = await catalog()
                    router = RouterClient(endpoint, client=httpx.AsyncClient(
                        base_url=endpoint, auth=auth, trust_env=False, timeout=90))
                    await router.load('probe-a')
                    assert (await catalog())['probe-a'] == 'sleeping'
                    report['blackrose_load_preserves_sleep'] = True
                    chunks = [chunk async for chunk in router.stream('probe-a', [
                        {'role': 'user', 'content': 'Say OK.'}])]
                    assert any(chunk.get('finish_reason') for chunk in chunks)
                    await router.aclose()
                    assert process.poll() is None
                    report['blackrose_disconnect_preserves_router'] = True
                    await wait_status('probe-a', 'sleeping')

                    async def contention(mode='inference'):
                        response = await client.post('/v1/chat/completions', json=body('probe-a'))
                        response.raise_for_status()
                        started = asyncio.Event()
                        result = {}

                        async def first():
                            request = dict(body('probe-a', 192), stream=True)
                            async with client.stream('POST', '/v1/chat/completions', json=request) as stream:
                                result['first_http'] = stream.status_code
                                async for line in stream.aiter_lines():
                                    if not line.startswith('data: ') or line == 'data: [DONE]':
                                        continue
                                    packet = json.loads(line[6:])
                                    for choice in packet.get('choices', []):
                                        if choice.get('delta', {}).get('content'):
                                            started.set()
                                        if choice.get('finish_reason'):
                                            result['first_finish'] = choice['finish_reason']
                                result['first_ended'] = time.monotonic()

                        task = asyncio.create_task(first())
                        try:
                            async with asyncio.timeout(30):
                                await started.wait()
                            before = time.monotonic()
                            if mode == 'blackrose':
                                waiter = RouterClient(endpoint, client=httpx.AsyncClient(
                                    base_url=endpoint, auth=auth, trust_env=False, timeout=90))
                                try:
                                    await waiter.load('probe-b')
                                finally:
                                    await waiter.aclose()
                                result['blackrose_load_succeeded'] = True
                            else:
                                path = '/models/load' if mode == 'management' else '/v1/chat/completions'
                                payload = {'model': 'probe-b'} if mode == 'management' else body('probe-b')
                                response = await client.post(path, json=payload)
                                result['second_http'] = response.status_code
                                if response.is_error:
                                    result['second_error'] = response.text[:300]
                            result['second_seconds'] = round(time.monotonic() - before, 3)
                            result['first_done_before_second_returned'] = task.done()
                            await task
                            result.pop('first_ended', None)
                            result['catalog'] = await catalog()
                            return result
                        finally:
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

                    report['inference_contention'] = await contention()
                    report['management_contention'] = await contention(mode='management')
                    report['blackrose_contention'] = await contention(mode='blackrose')
                    for name in ('inference_contention', 'blackrose_contention'):
                        result = report[name]
                        assert result.get('first_finish') == 'length', result
                        assert result['first_done_before_second_returned'], result
                    assert report['inference_contention']['second_http'] == 200
                    assert report['management_contention']['second_http'] == 500
                    assert report['management_contention'].get('first_finish') == 'length'
                    assert report['blackrose_contention']['blackrose_load_succeeded']
                    version = subprocess.run([str(args.binary), '--version'],
                        capture_output=True, text=True, timeout=10)
                    report['binary_version'] = (version.stdout + version.stderr).strip()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path.home() / '.local/bin/llama-server')
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(check(parser.parse_args()))


if __name__ == '__main__':
    main()
