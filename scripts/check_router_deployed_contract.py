"""Exercise the installed llama-router build with an isolated CPU scratch model.

This is an opt-in integration check. It reads (but never edits) the configured
router preset, copies a selected cached model's relevant settings into a
temporary CPU-only preset, and uses a fresh loopback port plus scratch XDG state.
"""
from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import time
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from blackrose.config import load_config  # noqa: E402
from blackrose.backend.router import RouterClient
from blackrose.backend.service import RouterService


SOURCE_SECTION = 'sharp-minicpm5-2b'
SCRATCH_SECTION = 'bra-bik-sharp-minicpm5-2b-cpu'
MODEL_ID = SCRATCH_SECTION


class ScratchRouterService(RouterService):
    """Allow adoption only of the child this exact service instance launched."""

    def _command(self):
        # The isolated scratch router keeps its key but re-enables slot telemetry,
        # which provides the prompt/decode cancellation evidence below.
        return ['--slots' if argument == '--no-slots' else argument
                for argument in super()._command()]

    def _adopt(self):
        process = self._process
        if (process is None or process.poll() is not None or self._pid != process.pid
                or self._handle is None):
            raise RuntimeError('Refusing to adopt a router process not launched by this scratch probe')
        process_root = Path(f'/proc/{process.pid}')
        try:
            executable = (process_root / 'exe').resolve()
            arguments = (process_root / 'cmdline').read_bytes().decode().strip('\0').split('\0')
            preset_index = arguments.index('--models-preset') + 1
            port_index = arguments.index('--port') + 1
            host_index = arguments.index('--host') + 1
        except (OSError, UnicodeError, ValueError, IndexError) as error:
            raise RuntimeError('Cannot verify scratch router process ownership') from error
        if (executable != self.binary.resolve()
                or Path(arguments[preset_index]).resolve() != self.preset.resolve()
                or int(arguments[port_index]) != self.port
                or arguments[host_index] != self.host):
            raise RuntimeError('Scratch router process identity changed; refusing adoption')


def _source_profile(path: Path, section: str = SOURCE_SECTION) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    text = path.read_text(encoding='utf-8')
    # llama-router accepts a version marker before the INI sections; Python's
    # ConfigParser expects all non-comment values to belong to a section.
    text = '\n'.join(line for line in text.splitlines()
                     if not line.lstrip().startswith('version ='))
    if not text.strip():
        raise FileNotFoundError(f'Router preset is not readable: {path}')
    parser.read_string(text, source=str(path))
    if not parser.has_section(section):
        raise ValueError(f'Router preset has no [{section}] profile')
    profile = dict(parser[section])
    for key in ('model', 'chat-template-file'):
        if not Path(profile[key]).is_file():
            raise FileNotFoundError(f'Cached model profile is missing required {key}')
    return profile


def _write_cpu_preset(path: Path, source: dict[str, str]) -> None:
    """Keep the selected cached GGUF/template/sampler; force small CPU test limits."""
    profile = {
        'model': source['model'],
        'chat-template-file': source['chat-template-file'],
        'ctx-size': '16384',
        'n-gpu-layers': '0',
        'threads': '2',
        'parallel': '1',
        'batch-size': '256',
        'ubatch-size': '128',
        'load-mode': 'mmap',
        'jinja': 'true',
        'temp': source.get('temp', '0.6'),
        'top-p': source.get('top-p', '0.95'),
        'top-k': source.get('top-k', '20'),
        'min-p': source.get('min-p', '0.05'),
        'repeat-penalty': source.get('repeat-penalty', '1.0'),
        'reasoning': 'on',
        'reasoning-format': 'deepseek',
        'reasoning-budget': '256',
        'n-predict': '1024',
        'load-on-startup': 'false',
        'stop-timeout': '15',
    }
    with path.open('w', encoding='utf-8') as stream:
        stream.write('version = 1\n\n[' + SCRATCH_SECTION + ']\n')
        for key, value in profile.items():
            stream.write(f'{key} = {value}\n')


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def _content(event: dict) -> str:
    delta = event.get('delta') or {}
    return ''.join(value for value in (delta.get('content'), delta.get('reasoning_content'))
                   if isinstance(value, str))


def _answer_text(events: list[dict]) -> str:
    return ''.join(value for event in events
                   if isinstance((delta := event.get('delta')), dict)
                   if isinstance((value := delta.get('content')), str))


def _finish_reasons(events: list[dict]) -> list[str]:
    return [event['finish_reason'] for event in events
            if isinstance(event.get('finish_reason'), str)]


async def _drain(client: RouterClient, messages: list[dict], *, effort='default', tools=(),
                 first_event: asyncio.Event | None = None) -> list[dict]:
    events = []
    async for event in client.stream(MODEL_ID, messages, tools, effort=effort):
        events.append(event)
        if first_event is not None and _content(event):
            first_event.set()
    return events


def _tool_calls(events: list[dict]) -> list[dict]:
    calls: dict[int, dict] = {}
    for event in events:
        for item in (event.get('delta') or {}).get('tool_calls', []):
            index = item.get('index', 0)
            call = calls.setdefault(index, {'id': '', 'type': 'function',
                                             'function': {'name': '', 'arguments': ''}})
            call['id'] = item.get('id') or call['id']
            function = item.get('function') or {}
            call['function']['name'] += function.get('name') or ''
            call['function']['arguments'] += function.get('arguments') or ''
    return [calls[index] for index in sorted(calls)]


async def _slots(client: RouterClient) -> list[dict] | None:
    try:
        response = await client.client.get('/slots', params={'model': MODEL_ID})
        response.raise_for_status()
        rows = response.json()
        return rows if isinstance(rows, list) else None
    except Exception:  # noqa: BLE001 - endpoint support varies by build
        return None


async def _wait_idle(client: RouterClient, seconds: float = 15) -> bool | None:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        rows = await _slots(client)
        if rows is None:
            return None
        if not any(row.get('is_processing') for row in rows if isinstance(row, dict)):
            return True
        await asyncio.sleep(.2)
    return False


def _session_runtime(root: Path, client: RouterClient):
    """Build a real Session/SQLite stack while retaining the owned scratch client."""
    from blackrose.attachments import AttachmentStore
    from blackrose.chat import ChatController
    from blackrose.config import Config
    from blackrose.gui.session import Session
    from blackrose.runtime import Runtime
    from blackrose.skills import SkillManager
    from blackrose.storage.db import Database
    from blackrose.storage.repository import Repository

    class LocalService:
        async def close(self):
            return None

    config = Config(default_model=MODEL_ID, knowledge_enabled=False, web_enabled=False,
                    system_prompt=('You are a local scratch probe model. Follow the '
                                   'user instruction exactly and briefly.'))
    database = Database(root / 'session.db')
    controller = ChatController(client, Repository(database), config, LocalService(),
                                SkillManager(root / 'skills'), LocalService(),
                                AttachmentStore(root / 'attachments'))
    runtime = Runtime(controller, database)
    events = []
    return runtime, Session(controller, lambda kind, payload: events.append((kind, payload))), events


async def _session_reopen_workflow(root: Path, client: RouterClient,
                                   service: ScratchRouterService, owned_processes: list,
                                   timeout: float) -> dict:
    """Check Session-driven turns survive a fresh SQLite connection and resume once."""
    report = {}
    request_count = 0

    async def count_completion(request):
        nonlocal request_count
        if request.method == 'POST' and request.url.path == '/v1/chat/completions':
            request_count += 1

    client.client.event_hooks['request'].append(count_completion)
    runtime, session, events = _session_runtime(root, client)
    await runtime.open()
    await session.boot()
    conversation_id = session.controller.conversation.id
    report['session_boot'] = {
        'ready': session._ready,
        'model': session.controller.model_name,
        'catalog_contains_model': any(item.get('id') == MODEL_ID
                                      for item in session.controller.catalog),
        'status_events': [payload.get('state') for kind, payload in events
                          if kind == 'status'],
    }
    await session.controller.rename_conversation(conversation_id, 'scratch persistence probe')

    async def submit_and_settle(prompt):
        before = request_count
        accepted = await session.submit(prompt)
        task = session._generation
        def concise_errors():
            return [payload.get('error') or payload.get('html')
                    for kind, payload in events if kind == 'reply_end' and
                    (payload.get('error') or payload.get('state') == 'error')][-2:]
        if task is None:
            report.setdefault('submit_failures', []).append({
                'prompt': prompt[:80], 'accepted': accepted, 'ready': session._ready,
                'errors': concise_errors(),
            })
            return request_count - before
        try:
            await asyncio.wait_for(task, timeout)
        except asyncio.CancelledError:
            pass
        if request_count == before:
            report.setdefault('submit_failures', []).append({
                'prompt': prompt[:80], 'accepted': accepted, 'ready': session._ready,
                'errors': concise_errors(),
            })
        return request_count - before

    try:
        count = await submit_and_settle('Reply with exactly PERSIST-ONE.')
        messages = await session.controller.current_messages()
        report['initial_turn'] = {
            'completion_requests': count,
            'stored_complete_pair': len(messages) == 2 and messages[0].role == 'user'
                                    and messages[-1].role == 'assistant'
                                    and 'PERSIST-ONE' in messages[-1].content
                                    and messages[-1].state.value == 'complete',
            'conversation_id': conversation_id,
            'message_count': len(messages),
        }
        await session.close()
        await runtime.database.close()

        runtime, session, events = _session_runtime(root, client)
        await runtime.open()
        await session.boot()
        messages = await session.controller.current_messages()
        transcript_events = [payload for kind, payload in events if kind == 'transcript']
        transcript_has_turn = bool(transcript_events and any(
            'PERSIST-ONE' in str(item) for page in transcript_events for item in
            (page.get('messages') or [])))
        report['reopen'] = {
            'same_conversation': session.controller.conversation.id == conversation_id,
            'stored_turn_visible': len(messages) == 2 and 'PERSIST-ONE' in messages[-1].content,
            'transcript_rendered': transcript_has_turn,
            'message_count': len(messages),
        }

        count = await submit_and_settle('Reply with exactly PERSIST-TWO.')
        messages = await session.controller.current_messages()
        report['continued_turn'] = {
            'completion_requests': count,
            'exactly_one_new_request': count == 1,
            'one_new_pair': len(messages) == 4 and messages[-2].role == 'user'
                            and messages[-2].content.endswith('PERSIST-TWO.')
                            and messages[-1].role == 'assistant'
                            and 'PERSIST-TWO' in messages[-1].content
                            and messages[-1].state.value == 'complete',
            'message_count': len(messages),
        }

        stopped = {'after_stream_delta': False}
        original_emit = session._emit

        def stop_after_delta(kind, payload):
            if (kind == 'reply_update' and payload.get('html')
                    and not stopped['after_stream_delta']):
                stopped['after_stream_delta'] = True
                session.stop()
            original_emit(kind, payload)

        session._emit = stop_after_delta
        count = await submit_and_settle(
            'Write a long list of harmless synthetic inventory items.')
        messages = await session.controller.current_messages()
        report['stop_persistence'] = {
            'completion_requests': count,
            'stop_called_after_stream_delta': stopped['after_stream_delta'],
            'stored_partial_pair': len(messages) == 6 and messages[-2].role == 'user'
                                   and messages[-1].role == 'assistant'
                                   and bool(messages[-1].content.strip()),
            'assistant_state': messages[-1].state.value if messages else None,
            'message_count': len(messages),
        }
        await session.close()
        await runtime.database.close()

        runtime, session, events = _session_runtime(root, client)
        await runtime.open()
        await session.boot()
        messages = await session.controller.current_messages()
        report['stop_reopen'] = {
            'same_conversation': session.controller.conversation.id == conversation_id,
            'partial_turn_visible': len(messages) == 6 and bool(messages[-1].content.strip()),
            'transcript_rendered': any(kind == 'transcript' for kind, _ in events),
            'message_count': len(messages),
        }

        outage = {'after_stream_delta': asyncio.Event()}
        original_emit = session._emit

        def watch_outage_delta(kind, payload):
            if kind == 'reply_update' and payload.get('html'):
                outage['after_stream_delta'].set()
            original_emit(kind, payload)

        session._emit = watch_outage_delta
        before = request_count
        await session.submit('Write a long, harmless synthetic list of color names.')
        task = session._generation
        if task is None:
            raise RuntimeError('Session did not start the backend outage request')
        try:
            await asyncio.wait_for(outage['after_stream_delta'].wait(), min(timeout, 60))
        except TimeoutError:
            pass

        old_process = service._process
        if (outage['after_stream_delta'].is_set() and old_process is not None
                and old_process.poll() is None):
            service._adopt()
            process_group = os.getpgid(old_process.pid)
            if process_group != old_process.pid:
                raise RuntimeError('Scratch router process did not own its process group')
            os.killpg(process_group, signal.SIGKILL)
            try:
                await asyncio.wait_for(task, 30)
                generation_error = None
            except asyncio.CancelledError:
                generation_error = 'cancelled'
            except Exception as error:  # noqa: BLE001 - expected broken stream
                generation_error = f'{type(error).__name__}: {str(error)[:160]}'
            old_exit = old_process.wait(timeout=10)
            messages = await session.controller.current_messages()
            report['outage_persistence'] = {
                'completion_requests': request_count - before,
                'delta_observed': True,
                'old_owned_process_exit': old_exit,
                'generation_error': generation_error,
                'stored_interrupted_pair': len(messages) == 8 and messages[-2].role == 'user'
                                            and messages[-1].role == 'assistant',
                'assistant_state': messages[-1].state.value if messages else None,
                'partial_content_stored': bool(messages and messages[-1].content.strip()),
                'message_count': len(messages),
            }
            await session.close()
            await runtime.database.close()

            runtime, session, events = _session_runtime(root, client)
            await runtime.open()
            await session.boot()
            restarted = service._process
            if restarted is not None and all(restarted is not item for item in owned_processes):
                owned_processes.append(restarted)
            messages = await session.controller.current_messages()
            report['outage_reopen'] = {
                'same_conversation': session.controller.conversation.id == conversation_id,
                'interrupted_turn_visible': len(messages) == 8
                                           and bool(messages[-1].content.strip()),
                'backend_restarted': restarted is not None and restarted.pid != old_process.pid,
                'message_count': len(messages),
            }
            count = await submit_and_settle('Reply with exactly AFTER-OUTAGE.')
            messages = await session.controller.current_messages()
            report['after_outage'] = {
                'completion_requests': count,
                'exactly_one_new_request': count == 1,
                'one_new_pair': len(messages) == 10 and messages[-2].role == 'user'
                                and messages[-2].content.endswith('AFTER-OUTAGE.')
                                and messages[-1].role == 'assistant'
                                and 'AFTER-OUTAGE' in messages[-1].content
                                and messages[-1].state.value == 'complete',
                'message_count': len(messages),
            }
        else:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            report['outage_persistence'] = {
                'tested': False,
                'delta_observed': outage['after_stream_delta'].is_set(),
                'completion_requests': request_count - before,
                'reason': 'No streamed response delta was available for safe owned-process outage injection',
            }
    finally:
        await session.close()
        await runtime.database.close()

    report['completion_requests_total'] = request_count
    report['workflow_ok'] = all((
        report.get('initial_turn', {}).get('stored_complete_pair'),
        report.get('initial_turn', {}).get('completion_requests') == 1,
        report.get('reopen', {}).get('same_conversation'),
        report.get('reopen', {}).get('stored_turn_visible'),
        report.get('reopen', {}).get('transcript_rendered'),
        report.get('continued_turn', {}).get('exactly_one_new_request'),
        report.get('continued_turn', {}).get('one_new_pair'),
        report.get('stop_persistence', {}).get('completion_requests') == 1,
        report.get('stop_persistence', {}).get('stop_called_after_stream_delta'),
        report.get('stop_persistence', {}).get('stored_partial_pair'),
        report.get('stop_reopen', {}).get('partial_turn_visible'),
        report.get('outage_persistence', {}).get('completion_requests') == 1,
        report.get('outage_persistence', {}).get('stored_interrupted_pair'),
        report.get('outage_reopen', {}).get('interrupted_turn_visible'),
        report.get('outage_reopen', {}).get('backend_restarted'),
        report.get('after_outage', {}).get('exactly_one_new_request'),
        report.get('after_outage', {}).get('one_new_pair'),
        request_count == 5,
    ))
    return report


def _slot_summary(rows: list[dict] | None) -> list[dict] | None:
    if rows is None:
        return None
    summary = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        next_tokens = row.get('next_token') or []
        nested_decoded = (next_tokens[0].get('n_decoded')
                          if next_tokens and isinstance(next_tokens[0], dict) else None)
        summary.append({
            'id': row.get('id'),
            'is_processing': row.get('is_processing'),
            'n_prompt_tokens': row.get('n_prompt_tokens'),
            'n_prompt_tokens_processed': row.get('n_prompt_tokens_processed'),
            'n_decoded': row.get('n_decoded', row.get('n_decoded_tokens', nested_decoded)),
        })
    return summary


async def _run(args) -> dict:
    source = _source_profile(args.preset.expanduser())
    report = {
        'build': args.version,
        'source_profile': SOURCE_SECTION,
        'scratch_profile': SCRATCH_SECTION,
        'model_id': MODEL_ID,
        'model_bytes': Path(source['model']).stat().st_size,
        'scratch_deviations': ['ctx-size set to 16384 for persisted Session turns',
                               'n-gpu-layers=0',
                               'threads=2', 'batch sizes reduced', 'draft decoding omitted',
                               'scratch reasoning budget=256', 'scratch n-predict=1024'],
        'scratch_sampler': {key: source.get(key, value) for key, value in {
            'temp': '0.6', 'top-p': '0.95', 'top-k': '20', 'min-p': '0.05',
            'repeat-penalty': '1.0'}.items()},
        'sampler_values_absent_from_source': [key for key in
            ('temp', 'top-p', 'top-k', 'min-p', 'repeat-penalty') if key not in source],
        'checks': {},
    }
    with tempfile.TemporaryDirectory(prefix='blackrose-bik-router-') as directory:
        root = Path(directory)
        for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
            os.environ[name] = str(root / name.lower())
        preset = root / 'scratch.ini'
        _write_cpu_preset(preset, source)
        port = _free_loopback_port()
        endpoint = f'http://127.0.0.1:{port}'
        config = SimpleNamespace(endpoint=endpoint,
                                 llama_server_path=str(args.binary.expanduser()),
                                 router_preset=str(preset),
                                 router_start_timeout=args.start_timeout,
                                 router_stop_timeout=20, router_api_key_file='')
        service = ScratchRouterService(config)
        client = RouterClient(endpoint, timeout=args.request_timeout, service=service,
                              budgets={'low': 256, 'medium': 256, 'high': 256, 'xhigh': 256})
        process = None
        owned_processes = []
        try:
            rows = await client.models()
            process = service._process
            owned_processes.append(process)
            report['process'] = {'pid': service._pid, 'port': port,
                                 'scratch_xdg_only': True}
            report['checks']['catalog'] = {'ok': any(row['id'] == MODEL_ID for row in rows),
                                           'models': [row['id'] for row in rows]}
            try:
                await client.load('bra-bik-model-that-does-not-exist')
            except ValueError as error:
                report['checks']['missing_model'] = {'ok': True,
                                                      'result': str(error)[:180]}
            else:
                report['checks']['missing_model'] = {'ok': False,
                                                      'error': 'Unexpectedly accepted missing model'}

            if not any(row['id'] == MODEL_ID for row in rows):
                raise RuntimeError(f'Scratch preset model absent from catalog: {MODEL_ID}')
            load_started = time.monotonic()
            await client.load(MODEL_ID)
            initial_slots = await _slots(client)
            report['slot_telemetry_available'] = initial_slots is not None
            report['checks']['load'] = {'ok': True,
                                        'seconds': round(time.monotonic() - load_started, 2),
                                        'capabilities': client.capabilities.get(MODEL_ID, {}),
                                        'reasoning_levels': client.reasoning_levels(MODEL_ID)}

            async with asyncio.timeout(args.request_timeout):
                events = await _drain(client, [{'role': 'user',
                                               'content': 'Reply with exactly READY.'}])
            report['checks']['prompt'] = {'ok': ('stop' in _finish_reasons(events)
                                                and 'READY' in _answer_text(events)),
                                          'finish_reasons': _finish_reasons(events),
                                          'normal_stop': 'stop' in _finish_reasons(events),
                                          'contains_requested_answer': 'READY' in _answer_text(events),
                                          'text_chars': sum(len(_content(e)) for e in events)}

            caps = client.capabilities.get(MODEL_ID, {})
            effort = 'low' if 'low' in client.reasoning_levels(MODEL_ID) else 'default'
            async with asyncio.timeout(args.request_timeout):
                reasoning_events = await _drain(client, [{'role': 'user',
                    'content': 'What is 19 times 23? Give the integer answer.'}], effort=effort)
            report['checks']['reasoning_effort'] = {
                'requested': effort,
                'supported': bool(caps.get('thinking')),
                'finish_reasons': _finish_reasons(reasoning_events),
                'normal_stop': 'stop' in _finish_reasons(reasoning_events),
                'contains_expected_answer': '437' in _answer_text(reasoning_events),
                'reasoning_chars': sum(len((e.get('delta') or {}).get('reasoning_content') or '')
                                       for e in reasoning_events),
            }

            tools = [{'type': 'function', 'function': {
                'name': 'local_probe', 'description': 'Return the exact probe marker.',
                'parameters': {'type': 'object', 'properties': {'marker': {'type': 'string'}},
                               'required': ['marker'], 'additionalProperties': False}}}]
            tool_messages = [{'role': 'user', 'content':
                "You must call local_probe exactly once with marker 'probe-17'. "
                'Do not answer directly.'}]
            async with asyncio.timeout(args.request_timeout):
                tool_events = await _drain(client, tool_messages, tools=tools)
            calls = _tool_calls(tool_events)
            tool_arguments = None
            if len(calls) == 1:
                try:
                    tool_arguments = json.loads(calls[0]['function']['arguments'])
                except (TypeError, ValueError):
                    pass
            tool_call_valid = (len(calls) == 1
                               and calls[0]['function']['name'] == 'local_probe'
                               and tool_arguments == {'marker': 'probe-17'})
            tool_followup_reasons = []
            if tool_call_valid:
                tool_messages.append({'role': 'assistant', 'content': None, 'tool_calls': calls})
                tool_messages.extend({'role': 'tool', 'tool_call_id': call['id'],
                                      'content': 'probe-result-17'} for call in calls)
                async with asyncio.timeout(args.request_timeout):
                    followup = await _drain(client, tool_messages)
                tool_followup_reasons = _finish_reasons(followup)
            tool_round = (tool_call_valid and 'tool_calls' in _finish_reasons(tool_events)
                          and 'stop' in tool_followup_reasons)
            report['checks']['tool_round'] = {
                'requested': True, 'calls_observed': len(calls), 'followup_completed': tool_round,
                'call_arguments_valid': tool_call_valid,
                'finish_reasons': _finish_reasons(tool_events),
                'followup_finish_reasons': tool_followup_reasons,
            }

            # A large local synthetic prompt should be rejected by the server's
            # context validator. Capture the actual status/error without asserting
            # a specific wording across llama.cpp revisions.
            try:
                async with asyncio.timeout(args.request_timeout):
                    overflow_events = await _drain(client, [{'role': 'user',
                        'content': 'token ' * 18000}])
                overflow = {'accepted_or_completed': True,
                            'finish': any(e.get('finish_reason') for e in overflow_events)}
            except Exception as error:  # noqa: BLE001 - expected server rejection
                overflow = {'accepted_or_completed': False,
                            'error_type': type(error).__name__, 'error': str(error)[:240]}
                overflow['context_rejected'] = 'HTTP 400' in str(error)
            report['checks']['context_overflow'] = overflow

            # Prompt-stage Stop: prefer the exact running-slot counters. If the
            # endpoint is unavailable, preserve the weaker dispatch/no-delta evidence.
            prompt_first_delta = asyncio.Event()
            prompt_dispatched = asyncio.Event()

            async def mark_request(request):
                if request.method == 'POST' and request.url.path == '/v1/chat/completions':
                    prompt_dispatched.set()

            client.client.event_hooks['request'].append(mark_request)
            prompt_task = asyncio.create_task(_drain(client, [{'role': 'user',
                'content': 'item ' * 3500}],
                first_event=prompt_first_delta))
            prompt_phase_seen = False
            prompt_slot_observation = None
            deadline = asyncio.get_running_loop().time() + min(20, args.request_timeout)
            while asyncio.get_running_loop().time() < deadline:
                slots = await _slots(client)
                if slots is not None:
                    prompt_slot_observation = _slot_summary(slots)
                    for row in slots:
                        if isinstance(row, dict) and row.get('is_processing'):
                            decoded_tokens = row.get('n_decoded', row.get('n_decoded_tokens'))
                            if decoded_tokens is None:
                                summary = _slot_summary([row])
                                decoded_tokens = summary[0]['n_decoded'] if summary else None
                            if decoded_tokens == 0:
                                prompt_phase_seen = True
                                break
                elif prompt_dispatched.is_set() and not prompt_first_delta.is_set():
                    prompt_phase_seen = True
                if prompt_phase_seen or prompt_first_delta.is_set():
                    break
                if prompt_task.done():
                    break
                await asyncio.sleep(.1)
            prompt_cancel_sent = not prompt_task.done()
            if not prompt_task.done():
                prompt_task.cancel()
            prompt_error = None
            try:
                await asyncio.wait_for(prompt_task, 10)
            except (asyncio.CancelledError, TimeoutError):
                pass
            except Exception as error:  # noqa: BLE001 - report actual server outcome
                prompt_error = f'{type(error).__name__}: {str(error)[:180]}'
            prompt_idle = await _wait_idle(client)
            report['checks']['stop_prompt'] = {'requested': True,
                                                'prompt_phase_observed': prompt_phase_seen,
                                                'slot_counters': prompt_slot_observation,
                                                'request_dispatched': prompt_dispatched.is_set(),
                                                'task_cancel_sent': prompt_cancel_sent,
                                                'server_idle_after': prompt_idle,
                                                'request_error': prompt_error}

            # Decode-stage Stop: cancel only after at least one generated text or
            # reasoning delta has reached this scratch client.
            decoded = asyncio.Event()
            decode_task = asyncio.create_task(_drain(client, [{'role': 'user',
                'content': 'Write a long, numbered list of ordinary household objects.'}],
                first_event=decoded))
            decode_observed = False
            try:
                await asyncio.wait_for(decoded.wait(), min(60, args.request_timeout))
                decode_observed = True
            except TimeoutError:
                pass
            decode_cancel_sent = not decode_task.done()
            decode_task.cancel()
            try:
                await asyncio.wait_for(decode_task, 10)
            except (asyncio.CancelledError, TimeoutError):
                pass
            decode_idle = await _wait_idle(client)
            report['checks']['stop_decode'] = {'requested': True,
                                                'first_delta_observed': decode_observed,
                                                'task_cancel_sent': decode_cancel_sent,
                                                'server_idle_after': decode_idle}

            # Client disconnect: close a live stream iterator after first output,
            # exercising the response-context close path independently of task cancel.
            disconnect_stream = client.stream(MODEL_ID, [{'role': 'user',
                'content': 'Continue writing a long list of numbers.'}])
            disconnected = False
            try:
                async with asyncio.timeout(min(60, args.request_timeout)):
                    async for event in disconnect_stream:
                        if _content(event):
                            disconnected = True
                            break
            finally:
                await disconnect_stream.aclose()
            disconnect_idle = await _wait_idle(client)
            report['checks']['disconnect'] = {'after_first_delta': disconnected,
                                               'server_idle_after': disconnect_idle}

            async with asyncio.timeout(args.request_timeout):
                clean_events = await _drain(client, [{'role': 'user',
                    'content': 'Reply with exactly CLEAN.'}])
            report['checks']['subsequent_request'] = {
                'ok': ('stop' in _finish_reasons(clean_events)
                       and 'CLEAN' in _answer_text(clean_events)),
                'finish_reasons': _finish_reasons(clean_events),
                'contains_requested_answer': 'CLEAN' in _answer_text(clean_events),
            }

            # Simulate a backend outage by terminating only the exact service
            # process group launched in this TemporaryDirectory, then verify the
            # application service/client can start it again and complete work.
            outage_first_delta = asyncio.Event()
            outage_dispatched = asyncio.Event()
            async def note_outage_request(request):
                if request.method == 'POST' and request.url.path == '/v1/chat/completions':
                    outage_dispatched.set()
            client.client.event_hooks['request'].append(note_outage_request)
            outage_task = asyncio.create_task(_drain(client, [{'role': 'user', 'content':
                'Write a long list of harmless synthetic test words.'}],
                first_event=outage_first_delta))
            try:
                await asyncio.wait_for(outage_first_delta.wait(), min(30, args.request_timeout))
            except TimeoutError:
                pass
            old_process = service._process
            if (old_process is None or old_process is not service._process
                    or old_process.poll() is not None or not outage_dispatched.is_set()):
                outage_task.cancel()
                try:
                    await outage_task
                except asyncio.CancelledError:
                    pass
                report['checks']['backend_outage_recovery'] = {
                    'started': False, 'request_dispatched': outage_dispatched.is_set(),
                    'first_delta_observed': outage_first_delta.is_set(),
                    'reason': 'No live owned request was available for a safe process outage test',
                }
            else:
                service._adopt()  # verify exact executable, preset, host and port
                pgid = os.getpgid(old_process.pid)
                if pgid != old_process.pid:
                    raise RuntimeError('Scratch router process did not own its process group')
                os.killpg(pgid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(outage_task, 20)
                    outage_error = None
                except Exception as error:  # noqa: BLE001 - expected network disconnect
                    outage_error = {'type': type(error).__name__, 'message': str(error)[:180]}
                except asyncio.CancelledError:
                    outage_error = {'type': 'CancelledError', 'message': 'request task was canceled'}
                old_process_code = old_process.wait(timeout=10)
                recovered_rows = await client.models()
                new_process = service._process
                owned_processes.append(new_process)
                await client.load(MODEL_ID)
                async with asyncio.timeout(args.request_timeout):
                    recovery_events = await _drain(client, [{'role': 'user',
                        'content': 'Reply with exactly RECOVERED.'}])
                report['checks']['backend_outage_recovery'] = {
                    'started': True, 'request_dispatched': outage_dispatched.is_set(),
                    'first_delta_observed': outage_first_delta.is_set(),
                    'disconnected_request_error': outage_error,
                    'old_process_exit_code': old_process_code,
                    'restart_pid': service._pid,
                    'catalog_restored': any(row['id'] == MODEL_ID for row in recovered_rows),
                    'model_reloaded': True,
                    'recovery_reply_completed': ('stop' in _finish_reasons(recovery_events)
                                                 and 'RECOVERED' in _answer_text(recovery_events)),
                    'recovery_finish_reasons': _finish_reasons(recovery_events),
                }
            session_process = service._process
            if session_process is not None and all(
                    session_process is not owned for owned in owned_processes):
                owned_processes.append(session_process)
            report['checks']['session_database_reopen'] = await _session_reopen_workflow(
                root, client, service, owned_processes, args.request_timeout)
        finally:
            await client.aclose()
            for owned in owned_processes:
                if owned is None:
                    continue
                exit_code = owned.poll()
                report.setdefault('process', {}).setdefault('owned_processes', []).append({
                    'pid': owned.pid, 'exit_code_after_owned_shutdown': exit_code,
                    'exit_signal': (signal.Signals(-exit_code).name
                                    if isinstance(exit_code, int) and exit_code < 0 else None),
                })
    return report


async def _async_main(args) -> int:
    report = {'probe': 'bra-bik-scratch-router-contract', 'outcome': 'started'}
    try:
        result = await _run(args)
        report.update(result)
        report['outcome'] = 'completed'
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except BaseException as error:
        report['outcome'] = 'failed'
        report['error_type'] = type(error).__name__
        report['error'] = str(error)[:500]
        print(json.dumps(report, indent=2, sort_keys=True))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path('~/.local/bin/llama-server'))
    parser.add_argument('--preset', type=Path, default=Path(load_config().router_preset))
    parser.add_argument('--version', default='unknown')
    parser.add_argument('--start-timeout', type=float, default=120)
    parser.add_argument('--request-timeout', type=float, default=180)
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == '__main__':
    raise SystemExit(main())
