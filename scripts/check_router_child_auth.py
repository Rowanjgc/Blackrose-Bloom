"""Measure what a llama-router model child exposes on its loopback port (bra-wmw).

Opt-in integration check with the installed router build. Like
check_router_deployed_contract.py, it reads (but never edits) the configured
preset, loads a cached model into a temporary CPU-only preset, and uses a fresh
loopback port plus scratch XDG state. The router keeps the production launch
options, slots disabled included. The key is read in memory and never printed.

Following llama.cpp, children get no key and the router alone authenticates.
The probe requires that design (no key reaches a child, the router's own stream
calls reach it, replies and Stop work) and records the keyless child exposure
that the project accepts (see docs/architecture.md). Re-run it after llama.cpp upgrades.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from blackrose.config import load_config  # noqa: E402
import check_router_deployed_contract as contract
from blackrose.backend.router import RouterClient
from blackrose.backend.service import RouterService

MODEL_ID = contract.MODEL_ID
FOREIGN_ORIGIN = 'https://rebinding.example'
CHAT = {'model': MODEL_ID, 'messages': [{'role': 'user', 'content': 'Say hi.'}], 'max_tokens': 4}
# Routes a local process or rebinding page could try on a child port without the key.
ANONYMOUS_ROUTES = (
    ('GET', '/v1/models', None),
    ('GET', '/props', None),
    ('GET', '/slots', None),
    ('GET', '/metrics', None),
    ('POST', '/tokenize', {'content': 'hi'}),
    ('POST', '/completion', {'prompt': 'hi', 'n_predict': 4}),
    ('POST', '/v1/chat/completions', CHAT),
)
LONG_PROMPT = [{'role': 'user', 'content': 'Count from 1 to 400, one number per line.'}]


KEY_VARIABLES = (b'LLAMA_API_KEY', b'LLAMA_ARG_API_KEY_FILE')


class ChildAuthService(contract.ScratchRouterService):
    """Production launch options; adoption limited to the process this probe started."""

    def _command(self):
        return RouterService._command(self)


def _children(pid: int) -> list[int]:
    """Direct children, wherever the router's threads forked them."""
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            status = (entry / 'status').read_text()
            parent = next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:'))
            if parent == str(pid):
                found.append(int(entry.name))
    return found


def _child_port(arguments: list[str]) -> int | None:
    try:
        return int(arguments[arguments.index('--port') + 1])
    except (ValueError, IndexError):
        return None


def _cpu_seconds(pid: int) -> float:
    # utime and stime are fields 14 and 15; the command name may contain spaces.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')


async def _cpu_rate(pid: int, seconds: float = 3.0) -> float:
    """CPU seconds per second; a generating 2-thread child runs near 2."""
    start = _cpu_seconds(pid)
    await asyncio.sleep(seconds)
    return round((_cpu_seconds(pid) - start) / seconds, 2)


async def _gone(pid: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                return True
        except OSError:
            return True
        await asyncio.sleep(0.2)
    return False


def _entries(response: httpx.Response) -> int | None:
    with contextlib.suppress(ValueError):
        rows = response.json()
        return len(rows) if isinstance(rows, list) else None
    return None


async def _model_child(service: RouterService, timeout: float = 30) -> tuple[int, list[str]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pid in _children(service._pid):
            with contextlib.suppress(OSError, UnicodeError):
                arguments = Path(f'/proc/{pid}/cmdline').read_bytes().decode().strip('\0').split('\0')
                if _child_port(arguments) is not None:
                    return pid, arguments
        await asyncio.sleep(0.2)
    raise RuntimeError('The loaded model child process was not found')


async def _status(http: httpx.AsyncClient, method: str, path: str, body=None, **options) -> int | str:
    try:
        response = await http.request(method, path, json=body, **options)
        return response.status_code
    except httpx.HTTPError as error:
        return type(error).__name__


async def _child_checks(port: int, key: str) -> dict:
    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=60) as http:
        anonymous = {f'{method} {path}': await _status(http, method, path, body)
                     for method, path, body in ANONYMOUS_ROUTES}
        response = await http.get('/v1/models', headers={'Origin': FOREIGN_ORIGIN,
                                                         'Host': 'rebinding.example'})
        rebinding = {'status': response.status_code,
                     'allow_origin': response.headers.get('access-control-allow-origin'),
                     'allow_credentials': response.headers.get('access-control-allow-credentials')}
        wrong = await _status(http, 'GET', '/v1/models', headers={'Authorization': 'Bearer wrong-key'})
        bearer = {'Authorization': f'Bearer {key}'}
        keyed = {'GET /v1/models': await _status(http, 'GET', '/v1/models', headers=bearer),
                 'POST /tokenize': await _status(http, 'POST', '/tokenize', {'content': 'hi'},
                                                 headers=bearer)}
        health = await _status(http, 'GET', '/health')
    return {'anonymous': anonymous, 'foreign_origin_and_host': rebinding,
            'wrong_key': wrong, 'with_key': keyed, 'health_without_key': health}


async def _cancellation(client: RouterClient, child: int, timeout: float) -> dict:
    """Blackrose stops a reply by closing its stream; the child must go idle."""
    first = asyncio.Event()
    task = asyncio.create_task(contract._drain(client, LONG_PROMPT, first_event=first))
    try:
        async with asyncio.timeout(timeout):
            await first.wait()
        generating = await _cpu_rate(child, 2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await asyncio.sleep(2)
    after = await _cpu_rate(child)
    async with asyncio.timeout(timeout):
        events = await contract._drain(client, [{'role': 'user',
                                                 'content': 'Reply with exactly CLEAN.'}])
    return {'cpu_rate_while_generating': generating, 'cpu_rate_after_close': after,
            'next_reply_finish_reasons': contract._finish_reasons(events),
            'next_reply_contains_clean': 'CLEAN' in contract._answer_text(events)}


async def _stream_session(client: RouterClient, child: int, port: int, key: str,
                          timeout: float) -> dict:
    """The router's own header-less calls into a child (resumable sessions)."""
    conversation = 'bra-wmw-probe'
    body = {'model': MODEL_ID, 'messages': LONG_PROMPT, 'stream': True, 'max_tokens': 192}
    report = {}
    async with asyncio.timeout(timeout):
        async with client.client.stream('POST', '/v1/chat/completions', json=body,
                                        headers={'X-Conversation-Id': conversation}) as response:
            report['request_status'] = response.status_code
            async for line in response.aiter_lines():
                if line.startswith('data:'):
                    break
            lookup = await client.client.post('/v1/streams/lookup',
                                              json={'conversation_ids': [conversation]})
            report['router_lookup'] = {'status': lookup.status_code, 'entries': _entries(lookup)}
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=10) as http:
                direct = await http.post('/v1/streams/lookup',
                                         json={'conversation_ids': [conversation]},
                                         headers={'Authorization': f'Bearer {key}'})
                report['child_lookup_with_key'] = {'status': direct.status_code,
                                                   'entries': _entries(direct)}
                stop = await client.client.delete('/v1/stream', params={'conv_id': conversation})
                report['router_stop_status'] = stop.status_code
                report['cpu_rate_after_router_stop'] = await _cpu_rate(child, 2)
                stop = await http.delete('/v1/stream', params={'conv_id': conversation},
                                         headers={'Authorization': f'Bearer {key}'})
                report['child_stop_with_key_status'] = stop.status_code
    await asyncio.sleep(2)
    report['cpu_rate_after_child_stop'] = await _cpu_rate(child)
    # These calls carry no key, so they reach only a keyless child.
    report['router_calls_reach_child'] = (report['router_lookup']['entries'] == 1
                                          and report['cpu_rate_after_router_stop'] < 0.2)
    return report


def _exposure(child: dict) -> dict:
    """What the accepted keyless design leaves open on the child port."""
    rebinding = child['foreign_origin_and_host']
    return {'answers_without_key': sorted(route for route, status in child['anonymous'].items()
                                          if status == 200),
            'answers_foreign_origin_and_host': rebinding['status'] == 200,
            'browser_can_read_cross_origin': rebinding['allow_origin'] == FOREIGN_ORIGIN}


def _expected(report: dict) -> dict:
    argv = report['checks']['child_argv']
    return {
        'no_key_reaches_children': not argv['key_options'] and not argv['key_variables'],
        'key_absent_from_child_argv_environ_and_log': not any(
            report['checks']['secrecy'][name] for name in
            ('key_in_child_argv', 'key_in_child_environ', 'key_in_router_log')),
        'props_proxied_with_key': bool(report['checks']['load']['capabilities']),
        'proxied_reply_completes': report['checks']['proxied_reply']['ok'],
        'closing_the_stream_idles_the_child': (
            report['checks']['cancellation']['cpu_rate_after_close'] < 0.2
            and report['checks']['cancellation']['next_reply_contains_clean']),
        'router_stream_calls_reach_child': report['checks']['stream_session']['router_calls_reach_child'],
    }


async def _run(args) -> dict:
    source = contract._source_profile(args.preset.expanduser())
    report = {'build': args.version, 'source_profile': contract.SOURCE_SECTION, 'model_id': MODEL_ID, 'checks': {}}
    with tempfile.TemporaryDirectory(prefix='blackrose-wmw-router-') as directory:
        root = Path(directory)
        for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
            os.environ[name] = str(root / name.lower())
        preset = root / 'scratch.ini'
        contract._write_cpu_preset(preset, source)
        port = contract._free_loopback_port()
        endpoint = f'http://127.0.0.1:{port}'
        config = SimpleNamespace(endpoint=endpoint, llama_server_path=str(args.binary.expanduser()),
                                 router_preset=str(preset), router_start_timeout=args.start_timeout,
                                 router_stop_timeout=20, router_api_key_file='')
        service = ChildAuthService(config)
        client = RouterClient(endpoint, timeout=args.request_timeout, service=service)
        process = None
        try:
            await client.models()
            process = service._process
            started = time.monotonic()
            await client.load(MODEL_ID)
            child, arguments = await _model_child(service)
            child_port = _child_port(arguments)
            key = service.key.read()
            report['process'] = {'router_pid': service._pid, 'router_port': port,
                                 'child_pid': child, 'child_port': child_port,
                                 'scratch_xdg_only': True}
            environment = Path(f'/proc/{child}/environ').read_bytes().split(b'\0')
            report['checks']['load'] = {'seconds': round(time.monotonic() - started, 2),
                                        'capabilities': client.capabilities.get(MODEL_ID, {})}
            report['checks']['child_argv'] = {
                'key_options': [option for option in arguments if option.startswith('--api-key')],
                'hardening': [option for option in arguments
                              if option in ('--no-slots', '--no-webui', '--no-cors-credentials')],
                'cors_origins': arguments[arguments.index('--cors-origins') + 1]
                                if '--cors-origins' in arguments else None,
                'key_variables': sorted(name.decode() for name in KEY_VARIABLES
                                        if any(entry.startswith(name + b'=') for entry in environment))}
            report['checks']['child_port'] = await _child_checks(child_port, key)
            report['exposure'] = _exposure(report['checks']['child_port'])
            async with asyncio.timeout(args.request_timeout):
                events = await contract._drain(client, [{'role': 'user',
                                                         'content': 'Reply with exactly READY.'}])
            report['checks']['proxied_reply'] = {
                'ok': 'stop' in contract._finish_reasons(events)
                      and 'READY' in contract._answer_text(events),
                'finish_reasons': contract._finish_reasons(events)}
            report['checks']['cancellation'] = await _cancellation(client, child,
                                                                   args.request_timeout)
            report['checks']['stream_session'] = await _stream_session(
                client, child, child_port, key, args.request_timeout)
            log_text = service.log_path.read_bytes()
            report['checks']['secrecy'] = {
                'key_in_child_argv': key in '\0'.join(arguments),
                'key_in_child_environ': key.encode() in b'\0'.join(environment),
                'key_in_router_log': key.encode() in log_text}
            await client.unload(MODEL_ID)
            report['checks']['child_exited_after_unload'] = await _gone(child)
            report['expectations'] = _expected(report)
        finally:
            key_path = service.key.path
            await client.aclose()
            if process is not None:
                exit_code = process.poll()
                report.setdefault('process', {})['router_exit_code'] = exit_code
                report['process']['router_exit_signal'] = (
                    signal.Signals(-exit_code).name
                    if isinstance(exit_code, int) and exit_code < 0 else None)
            report['checks']['key_file_removed_on_close'] = not key_path.exists()
    return report


async def _async_main(args) -> int:
    report = {'probe': 'bra-wmw-router-child-auth', 'outcome': 'started'}
    try:
        report.update(await _run(args))
        report['outcome'] = ('passed' if all(report['expectations'].values()) else 'failed')
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report['outcome'] == 'passed' else 1
    except BaseException as error:
        report['outcome'] = 'error'
        report['error_type'] = type(error).__name__
        report['error'] = str(error)[:500]
        print(json.dumps(report, indent=2, sort_keys=True))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--binary', type=Path, default=Path('~/.local/bin/llama-server'))
    parser.add_argument('--preset', type=Path, default=Path(load_config().router_preset))
    parser.add_argument('--version', default='unknown')
    parser.add_argument('--start-timeout', type=float, default=120)
    parser.add_argument('--request-timeout', type=float, default=180)
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == '__main__':
    raise SystemExit(main())
