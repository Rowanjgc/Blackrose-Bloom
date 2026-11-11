"""Prepare or explicitly run a bounded OpenCode Go protocol acceptance matrix.

Default mode is a local plan only: no network access and no keychain access.
Live mode sends synthetic prompts only and is gated by --confirm-live. See
the OpenCode Go section of docs/user-guide.md before using it.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
from pathlib import Path
import struct
import zlib
from contextlib import nullcontext
from unittest.mock import patch

import httpx

from blackrose.backend.opencode import ENDPOINT, PREFIX, OpenCodeGoClient
from blackrose.backend.opencode_protocols import StreamDecoder, prepare, protocol_for
from blackrose.backend.stream_limits import StreamBounds, StreamBudget
from blackrose.credentials import CredentialError, OpenCodeGoKeychain


MAX_POSTS = 5
MAX_TOTAL_HTTP = 6  # one public model catalog GET plus at most five inference POSTs
MAX_BODY_BYTES = 16 * 1024
MAX_OUTPUT_TOKENS = 256
MAX_TOTAL_OUTPUT_TOKENS = MAX_POSTS * MAX_OUTPUT_TOKENS
MAX_REQUEST_TIMEOUT = 60
MAX_RUN_SECONDS = 240
MAX_USAGE_ESTIMATE_USD = 0.75

# IDs and protocol names are fixed to the catalog documented on 2026-10-30.
# A live run also checks these IDs against the public catalog before reading a key.
CASES = (
    {'name': 'chat_tool_round', 'model': PREFIX + 'glm-5.3-flash', 'protocol': 'chat',
     'max_posts': 2, 'purpose': 'one synthetic function call and one tool-result continuation'},
    {'name': 'responses_reasoning_continuation', 'model': PREFIX + 'gpt-6-luna', 'protocol': 'responses',
     'max_posts': 2, 'purpose': 'native reasoning/tool-call state survives one continuation'},
    {'name': 'google_tiny_vision', 'model': PREFIX + 'gemini-4-argon', 'protocol': 'google',
     'max_posts': 1, 'purpose': 'interpret one generated 1x1 red PNG'},
)

REMAINING_CASES = (
    {'name': 'responses_state_continuation', 'model': PREFIX + 'gpt-6-luna', 'protocol': 'responses',
     'max_posts': 2, 'purpose': 'wire diagnostics and replay of all returned state, including optional reasoning'},
    {'name': 'messages_tool_round', 'model': PREFIX + 'minimax-m3', 'protocol': 'messages',
     'max_posts': 2, 'purpose': 'native Messages tool-result pairing and state replay'},
    CASES[2],
)

PRICING_ASSUMPTIONS = {
    'input_usd_per_million': 4.00,
    'output_usd_per_million': 20.00,
    'basis': 'undiscounted Gemini 4 Argon prices in current official Go docs; applies worst rate to every model',
    'buffer_multiplier': 2,
}


def estimate_worst_case_usd() -> float:
    # UTF-8 bytes are charged as if each were a token: a deliberately loose
    # bound for these ASCII prompts and one tiny base64 image.
    input_tokens = MAX_POSTS * MAX_BODY_BYTES
    output_tokens = MAX_TOTAL_OUTPUT_TOKENS
    raw = (input_tokens * PRICING_ASSUMPTIONS['input_usd_per_million']
           + output_tokens * PRICING_ASSUMPTIONS['output_usd_per_million']) / 1_000_000
    return raw * PRICING_ASSUMPTIONS['buffer_multiplier']


def plan(*, remaining=False) -> dict:
    return {
        'provider': 'OpenCode Go',
        'cases': list(REMAINING_CASES if remaining else CASES),
        'bounds': {'max_http_requests': MAX_TOTAL_HTTP, 'max_inference_posts': MAX_POSTS,
                   'max_body_bytes_each': MAX_BODY_BYTES,
                   'max_output_tokens_each': MAX_OUTPUT_TOKENS,
                   'max_output_tokens_total': MAX_TOTAL_OUTPUT_TOKENS,
                   'idle_timeout_seconds': MAX_REQUEST_TIMEOUT,
                   'wall_timeout_seconds': MAX_RUN_SECONDS,
                   'usage_equivalent_estimate_limit_usd': MAX_USAGE_ESTIMATE_USD,
                   'estimated_worst_case_usd': round(estimate_worst_case_usd(), 4),
                   'pricing_assumptions': PRICING_ASSUMPTIONS},
        'credential_failure': 'offline synthetic missing/locked and revoked-key HTTP 401 checks; no real key needed',
        'live_gate': 'live execution additionally requires --live --confirm-live and explicit external authorization',
        'endpoint_review': {
            'adapter': ENDPOINT,
            'current_official_base': 'https://opencode.ai/inference/go',
            'note': 'Official documented route paths differ from current adapter paths; see docs before live use.',
        },
    }


class RequestCap:
    """HTTP client wrapper that bounds and records only sanitized request metadata."""

    def __init__(self, client, *, post_limit=MAX_POSTS, http_limit=MAX_TOTAL_HTTP, cases=CASES):
        self._client = client
        self.post_limit = post_limit
        self.http_limit = http_limit
        self.http_count = 0
        self.catalog_count = 0
        self.post_count = 0
        self.expected = []
        for case in cases:
            model = case['model'][len(PREFIX):]
            path, _body = prepare(case['protocol'], model, [], [])
            expected = (model, case['protocol'], ENDPOINT + path)
            self.expected.extend([expected] * case['max_posts'])
        self.records: list[dict] = []

    async def get(self, url, **kwargs):
        self.http_count += 1
        self.catalog_count += 1
        if (self.http_count > self.http_limit or self.catalog_count > 1
                or str(url) != ENDPOINT + '/models'):
            raise RuntimeError('Acceptance runner blocked an unexpected HTTP request')
        self.records.append({'method': 'GET', 'url': str(url), 'purpose': 'public_catalog'})
        return await self._client.get(url, **kwargs)

    def stream(self, method, url, **kwargs):
        self.http_count += 1
        self.post_count += 1
        if self.http_count > self.http_limit or self.post_count > self.post_limit:
            raise RuntimeError('Acceptance runner request ceiling reached')
        if method != 'POST' or self.post_count > len(self.expected):
            raise RuntimeError('Acceptance runner blocked an unexpected HTTP request')
        body = kwargs.get('json')
        if not isinstance(body, dict):
            raise RuntimeError('Acceptance runner requires a JSON provider request')
        body = copy.deepcopy(body)
        expected_model, protocol, expected_url = self.expected[self.post_count - 1]
        if str(url) != expected_url:
            raise RuntimeError('Acceptance runner blocked an unexpected model/route combination')
        model = body.get('model')
        if protocol == 'google':
            if model is not None or protocol_for(expected_model) != protocol:
                raise RuntimeError('Acceptance runner blocked an unexpected Google request body')
            model = expected_model
        elif model != expected_model or protocol_for(model) != protocol:
            raise RuntimeError('Acceptance runner blocked an unexpected model/route combination')
        if protocol == 'chat':
            body['max_tokens'] = MAX_OUTPUT_TOKENS
        elif protocol == 'messages':
            body['max_tokens'] = MAX_OUTPUT_TOKENS
        elif protocol == 'responses':
            body['max_output_tokens'] = MAX_OUTPUT_TOKENS
        elif protocol == 'google':
            generation = dict(body.get('generationConfig') or {})
            generation['maxOutputTokens'] = MAX_OUTPUT_TOKENS
            body['generationConfig'] = generation
        serialized = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(serialized) > MAX_BODY_BYTES:
            raise RuntimeError('Acceptance request body exceeds the 16 KiB safety cap')
        kwargs['json'] = body
        self.records.append({'method': 'POST', 'url': str(url), 'model': model,
                             'protocol': protocol, 'body_bytes': len(serialized),
                             'max_output_tokens': MAX_OUTPUT_TOKENS})
        return self._client.stream(method, url, **kwargs)

    async def aclose(self):
        await self._client.aclose()


def _tool_fragments(choices):
    calls: dict[int, dict] = {}
    reasoning = []
    content = []
    finish = None
    provider_content = None
    usage = []
    for choice in choices:
        delta = choice.get('delta') or {}
        if delta.get('content'):
            content.append(delta['content'])
        if delta.get('reasoning_content'):
            reasoning.append(delta['reasoning_content'])
        for part in delta.get('tool_calls') or []:
            row = calls.setdefault(part.get('index', 0), {'id': '', 'name': '', 'arguments': ''})
            row['id'] += part.get('id') or ''
            function = part.get('function') or {}
            row['name'] += function.get('name') or ''
            row['arguments'] += function.get('arguments') or ''
        if choice.get('finish_reason'):
            finish = choice['finish_reason']
        if choice.get('_provider_content'):
            provider_content = choice['_provider_content']
        if choice.get('_usage'):
            usage.append(choice['_usage'])
    return {'calls': list(calls.values()), 'content': ''.join(content),
            'reasoning': ''.join(reasoning), 'finish': finish,
            'provider_content': provider_content, 'usage': usage}


def redact(value, secret):
    """Remove the provider credential recursively from every report string."""
    if isinstance(value, str):
        return value.replace(secret, '[REDACTED]') if secret else value
    if isinstance(value, dict):
        return {redact(key, secret) if isinstance(key, str) else key: redact(item, secret)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, secret) for item in value)
    return value


def safe_preview(value, secret, limit=240):
    # Redact before truncating: slicing a credential first can leave a prefix
    # that a later full-key replacement cannot recognize.
    return redact(value, secret)[:limit]


async def _stream(client, model, messages, tools, session_id, effort='default', *,
                  partial_streams=None, secret=''):
    budget = StreamBudget(StreamBounds(reply_bytes=128 * 1024, reasoning_bytes=128 * 1024,
        response_bytes=256 * 1024, sse_line_bytes=64 * 1024,
        duration_seconds=MAX_REQUEST_TIMEOUT))
    choices = []
    completed = False
    try:
        async for choice in client.stream(model, messages, tools, session_id=session_id,
                                          effort=effort, stream_budget=budget):
            # Accumulate incrementally: the finally block reports partial
            # evidence if a later protocol assertion fails mid-stream.
            choices.append(choice)  # noqa: PERF401
        completed = True
    finally:
        # Keep bounded evidence for streams that finish at the transport layer
        # but fail a later protocol assertion, and for streams that error after
        # yielding partial output. Never retain full model text or tool args.
        if partial_streams is not None:
            summary = _tool_fragments(choices)
            partial_streams.append(redact({
                'model': model,
                'session_id': session_id,
                'stream_complete': completed,
                'finish_reason': summary['finish'],
                'effort': effort,
                'native_item_types': [safe_preview(item.get('type', ''), secret, 80)
                                      for item in (summary['provider_content'] or {}).get('content', [])[:16]],
                'content_preview': safe_preview(summary['content'], secret),
                'reasoning_preview': safe_preview(summary['reasoning'], secret),
                'tool_calls': [{'id': safe_preview(call['id'], secret, 160),
                                'name': safe_preview(call['name'], secret, 120)}
                               for call in summary['calls'][:8]],
                'usage': summary['usage'][:10],
            }, secret))
    return _tool_fragments(choices)


TOOL = [{'type': 'function', 'function': {
    'name': 'read_fixture', 'description': 'Read the supplied synthetic fixture value.',
    'parameters': {'type': 'object', 'properties': {'key': {'type': 'string'}},
                   'required': ['key'], 'additionalProperties': False}}}]


async def run_tool_round(client, model, session_id, *, partial_streams=None, secret=''):
    messages = [{'role': 'system', 'content': 'Use the supplied local tool exactly once. No outside research.'},
                {'role': 'user', 'content': 'Call read_fixture with key "sample", then report its value.'}]
    first = await _stream(client, model, messages, TOOL, session_id,
                          partial_streams=partial_streams, secret=secret)
    if first['finish'] != 'tool_calls' or len(first['calls']) != 1:
        raise AssertionError('Tool-round model did not produce exactly one function call')
    call = first['calls'][0]
    if call['name'] != 'read_fixture' or not call['id']:
        raise AssertionError('Tool-round function name or call ID was malformed')
    arguments = json.loads(call['arguments'])
    if arguments != {'key': 'sample'}:
        raise AssertionError('Tool-round function arguments did not match the synthetic fixture')
    assistant = {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call['id'], 'type': 'function', 'function': {'name': call['name'], 'arguments': call['arguments']}}]}
    if first['provider_content']:
        assistant['_provider_content'] = first['provider_content']
    if first['reasoning']:
        assistant['reasoning_content'] = first['reasoning']
    messages.extend([assistant, {'role': 'tool', 'tool_call_id': call['id'],
                                'content': 'SYNTHETIC_FIXTURE_VALUE=42'}])
    second = await _stream(client, model, messages, (), session_id,
                           partial_streams=partial_streams, secret=secret)
    if second['finish'] != 'stop' or second['calls'] or '42' not in second['content']:
        raise AssertionError('Tool continuation did not return the synthetic fixture value')
    return {'status': 'passed', 'call_id': call['id'], 'tool_name': call['name'],
            'continuation': safe_preview(second['content'], secret)}


async def run_reasoning_continuation(client, model, session_id, captured_bodies, *,
                                     partial_streams=None, secret='', require_reasoning=True):
    messages = [{'role': 'system', 'content': 'Use the supplied local tool exactly once; then answer concisely.'},
                {'role': 'user', 'content': 'Call read_fixture for key "reasoning" and report the returned number.'}]
    first = await _stream(client, model, messages, TOOL, session_id, effort='low',
                          partial_streams=partial_streams, secret=secret)
    if first['finish'] != 'tool_calls' or len(first['calls']) != 1:
        raise AssertionError('Responses model did not produce exactly one function call')
    call = first['calls'][0]
    if not call['id'] or call['name'] != 'read_fixture':
        raise AssertionError('Responses tool call identity was malformed')
    if json.loads(call['arguments']) != {'key': 'reasoning'}:
        raise AssertionError('Responses tool arguments did not match the synthetic fixture')
    native = first['provider_content']
    if not native or native.get('protocol') != 'responses':
        raise AssertionError('Responses protocol continuation metadata was not retained')
    items = native.get('content') or []
    if require_reasoning and not any(item.get('type') == 'reasoning' for item in items):
        raise AssertionError('Responses stream did not include a native reasoning item')
    assistant = {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call['id'], 'type': 'function', 'function': {'name': call['name'], 'arguments': call['arguments']}}],
        '_provider_content': native}
    messages.extend([assistant, {'role': 'tool', 'tool_call_id': call['id'],
                                'content': 'SYNTHETIC_FIXTURE_VALUE=31'}])
    second = await _stream(client, model, messages, (), session_id, effort='low',
                           partial_streams=partial_streams, secret=secret)
    if second['finish'] != 'stop' or second['calls'] or '31' not in second['content']:
        raise AssertionError('Responses continuation did not return the synthetic value')
    continuation = captured_bodies[-1]
    inputs = continuation.get('input') or []
    if not any(row.get('type') == 'function_call_output' and row.get('call_id') == call['id']
               for row in inputs):
        raise AssertionError('Responses continuation omitted the matching tool-result call ID')
    # Every returned item must be replayed, even when reasoning was not emitted.
    if inputs[-len(items) - 1:-1] != items or not items:
        raise AssertionError('Responses continuation did not preserve all native state')
    return {'status': 'passed', 'call_id': call['id'],
            'reasoning_items': sum(item.get('type') == 'reasoning' for item in items),
            'reasoning_validation': ('replayed' if any(item.get('type') == 'reasoning' for item in items)
                                     else 'not emitted; preservation covered offline'),
            'continuation': safe_preview(second['content'], secret)}


def _png_red_1x1() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))


async def run_vision(client, model, session_id, *, partial_streams=None, secret=''):
    uri = 'data:image/png;base64,' + base64.b64encode(_png_red_1x1()).decode('ascii')
    result = await _stream(client, model, [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'What is the dominant color of this one-pixel image? Answer one color.'},
        {'type': 'image_url', 'image_url': {'url': uri}}]}], (), session_id,
        partial_streams=partial_streams, secret=secret)
    if result['finish'] != 'stop' or result['calls'] or 'red' not in result['content'].lower():
        raise AssertionError('Vision model did not identify the generated red pixel')
    return {'status': 'passed', 'answer': safe_preview(result['content'], secret),
            'image_bytes': len(_png_red_1x1())}


async def credential_failure_checks() -> dict:
    """Exercise missing/locked and revoked-key behavior with synthetic fixtures only."""
    sentinel = 'synthetic-revoked-key-never-real'

    class UnavailableKey:
        def __init__(self, message):
            self._message = message

        def get(self):
            raise CredentialError(self._message)

    for label, message in (('missing', 'OpenCode Go API key is missing.'),
                           ('locked', 'Cannot access the desktop keychain.')):
        calls = []
        client = OpenCodeGoClient(keychain=UnavailableKey(message), transport=httpx.MockTransport(
            lambda request, calls=calls: calls.append(request) or httpx.Response(200)))
        try:
            try:
                _ = [choice async for choice in client.stream(PREFIX + 'glm-5.3-flash', [], session_id='synthetic')]
            except CredentialError:
                pass
            else:
                raise AssertionError(f'{label} key did not fail closed')
            if calls:
                raise AssertionError(f'{label} key sent an HTTP request')
        finally:
            await client.aclose()

    seen = []
    class RevokedKey:
        def get(self):
            return sentinel

    def unauthorized(request):
        seen.append(request)
        return httpx.Response(401, text=sentinel, headers={'x-provider-debug': sentinel})

    client = OpenCodeGoClient(keychain=RevokedKey(), transport=httpx.MockTransport(unauthorized))
    try:
        try:
            _ = [choice async for choice in client.stream(PREFIX + 'glm-5.3-flash', [], session_id='synthetic')]
        except RuntimeError as error:
            if sentinel in str(error):
                raise AssertionError('Unauthorized provider response echoed a credential') from None
        else:
            raise AssertionError('Revoked key did not fail')
        if len(seen) != 1:
            raise AssertionError(f'Revoked key caused {len(seen)} requests; expected no retry')
    finally:
        await client.aclose()
    error_calls = []
    def stream_error(request):
        error_calls.append(request)
        return httpx.Response(200, text='data: ' + json.dumps({'error': {'message': sentinel}}) + '\n\n')
    client = OpenCodeGoClient(keychain=RevokedKey(), transport=httpx.MockTransport(stream_error))
    try:
        try:
            _ = [choice async for choice in client.stream(PREFIX + 'glm-5.3-flash', [], session_id='synthetic')]
        except RuntimeError as error:
            if sentinel in str(error):
                raise AssertionError('SSE provider error echoed a credential') from None
        else:
            raise AssertionError('SSE provider error did not fail')
        if len(error_calls) != 1:
            raise AssertionError('SSE provider error was automatically retried')
    finally:
        await client.aclose()

    content_calls = []
    def content_sentinel(request):
        content_calls.append(request)
        return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': {'content': sentinel},
            'finish_reason': 'stop'}]}) + '\n\n' + 'data: [DONE]\n\n')
    client = OpenCodeGoClient(keychain=RevokedKey(), transport=httpx.MockTransport(content_sentinel))
    try:
        choices = [choice async for choice in client.stream(PREFIX + 'glm-5.3-flash', [], session_id='synthetic')]
        report = redact({'answer': ''.join((choice.get('delta') or {}).get('content', '') for choice in choices)},
                        sentinel)
        if sentinel in json.dumps(report):
            raise AssertionError('Response content sentinel was retained in the report')
        if len(content_calls) != 1:
            raise AssertionError('Content sentinel control made an unexpected request count')
    finally:
        await client.aclose()
    return {'status': 'passed', 'missing': 'zero requests', 'locked': 'zero requests',
            'revoked': 'one 401, no retry, no secret echo',
            'sse_error': 'one mock request, no retry, no secret echo',
            'content_redaction': 'synthetic response string removed before reporting'}


def observed_decoder(records):
    """Observe wire structure before decoding; retain no text, IDs or opaque state."""
    known_types = {'reasoning', 'function_call', 'message', 'text', 'thinking',
                   'tool_use', 'redacted_thinking'}
    known_events = {'response.created', 'response.in_progress', 'response.output_item.added',
                    'response.output_item.done', 'response.function_call_arguments.delta',
                    'response.function_call_arguments.done', 'response.output_text.delta',
                    'response.output_text.done', 'response.content_part.added',
                    'response.content_part.done', 'response.completed', 'response.incomplete',
                    'response.failed', 'response.reasoning_summary_text.delta',
                    'response.reasoning_summary_text.done', 'response.reasoning_summary_part.added',
                    'response.reasoning_summary_part.done', 'message_start', 'content_block_start',
                    'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop', 'ping'}

    class ObservedDecoder(StreamDecoder):
        def __init__(self, protocol):
            super().__init__(protocol)
            self.trace = {'protocol': protocol, 'events': [], 'reasoning_seen_on_wire': False}
            records.append(self.trace)

        def feed(self, data):
            if data != '[DONE]':
                packet = json.loads(data)
                kind = packet.get('type')
                item = packet.get('item') or packet.get('content_block') or {}
                outputs = (packet.get('response') or {}).get('output') or []
                types = [item.get('type')] if item else []
                types.extend(row.get('type') for row in outputs if isinstance(row, dict))
                if 'reasoning' in types:
                    self.trace['reasoning_seen_on_wire'] = True
                if len(self.trace['events']) < 256:
                    self.trace['events'].append({
                        'event': kind if kind in known_events else 'other',
                        'item_types': [value if value in known_types else 'other' for value in types[:16]],
                    })
            result = super().feed(data)
            if self.protocol == 'responses' and self.completed:
                if self.trace['reasoning_seen_on_wire'] and not any(
                        item.get('type') == 'reasoning' for item in self.native):
                    raise AssertionError('Responses reasoning was on the wire but lost by the decoder')
            return result

    return ObservedDecoder


async def live(args) -> dict:
    if not args.confirm_live:
        raise ValueError('Live mode requires --confirm-live after separate cost and credential authorization.')
    if args.max_estimate_usd != MAX_USAGE_ESTIMATE_USD:
        raise ValueError(f'Live use is fixed at the documented ${MAX_USAGE_ESTIMATE_USD:.2f} estimate limit')
    if estimate_worst_case_usd() > MAX_USAGE_ESTIMATE_USD:
        raise RuntimeError('The calculated usage envelope exceeds its configured ceiling')

    client = None
    capped = None
    key = ''
    active_case = None
    remaining = getattr(args, 'remaining', False)
    cases = REMAINING_CASES if remaining else CASES
    results = {'plan': plan(remaining=remaining), 'cases': {
        case['name']: {'status': 'skipped', 'reason': 'Run stopped before this case'} for case in cases
    }, 'partial_streams': []}
    if remaining:
        results['wire_structure'] = []
    try:
        async with asyncio.timeout(MAX_RUN_SECONDS):
            client = OpenCodeGoClient(timeout=MAX_REQUEST_TIMEOUT)
            capped = RequestCap(client.client, cases=cases)
            client.client = capped
            results['credential_failure'] = await credential_failure_checks()
            catalog = await client.models()  # public, unauthenticated discovery; one GET
            by_id = {row['id']: row for row in catalog}
            for case in cases:
                row = by_id.get(case['model'])
                if not row or row.get('protocol') != case['protocol']:
                    raise RuntimeError(f"Catalog no longer advertises {case['model']} as {case['protocol']}")
                if case['name'] == 'google_tiny_vision' and 'image' not in row.get('architecture', {}).get('input_modalities', []):
                    raise RuntimeError(f"Catalog no longer advertises image input for {case['model']}")
            # Credential lookup is deliberately deferred until the user passed
            # the live gate and the public catalog checks succeeded.
            key = await asyncio.to_thread(OpenCodeGoKeychain().get)
            class FixedKey:
                def get(self):
                    return key
            client.keychain = FixedKey()
            client.client = capped
            observer = (patch('blackrose.backend.opencode.StreamDecoder',
                              observed_decoder(results['wire_structure'])) if remaining else nullcontext())
            with observer:
                for case in cases:
                    active_case = case['name']
                    options = {'partial_streams': results['partial_streams'], 'secret': key}
                    session = 'acceptance-' + case['name']
                    if case['protocol'] == 'responses':
                        captured = []
                        original_stream = capped.stream
                        def capture_stream(method, url, *, captured=captured,
                                           original_stream=original_stream, **kwargs):
                            if isinstance(kwargs.get('json'), dict):
                                captured.append(copy.deepcopy(kwargs['json']))
                            return original_stream(method, url, **kwargs)
                        capped.stream = capture_stream
                        try:
                            outcome = await run_reasoning_continuation(
                                client, case['model'], session, captured,
                                require_reasoning=not remaining, **options)
                        finally:
                            capped.stream = original_stream
                    elif case['protocol'] == 'google':
                        outcome = await run_vision(client, case['model'], session, **options)
                    else:
                        outcome = await run_tool_round(client, case['model'], session, **options)
                    results['cases'][case['name']] = outcome
            results['status'] = 'passed'
            active_case = None
    except Exception as error:
        results.update({'status': 'failed', 'error_type': type(error).__name__,
                        'error': safe_preview(str(error), key, 300)})
        if active_case is not None:
            results['cases'][active_case] = {'status': 'failed', 'error_type': type(error).__name__,
                                            'error': safe_preview(str(error), key, 300)}
    finally:
        if capped is not None:
            results['sanitized_requests'] = capped.records
        if client is not None:
            try:
                await client.aclose()
            except Exception as error:
                results.update({'status': 'failed', 'cleanup_error_type': type(error).__name__,
                                'cleanup_error': safe_preview(str(error), key, 200)})
    if capped is not None and (capped.post_count > MAX_POSTS or capped.http_count > MAX_TOTAL_HTTP):
        results.update({'status': 'failed', 'error': 'Acceptance runner exceeded its HTTP request budget'})
    return redact(results, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Send only the bounded synthetic acceptance matrix')
    parser.add_argument('--confirm-live', action='store_true', help='Confirm separate cost and credential authorization')
    parser.add_argument('--remaining', action='store_true',
                        help='Remaining Responses/Messages/vision matrix with structural wire diagnostics')
    parser.add_argument('--max-estimate-usd', type=float, default=MAX_USAGE_ESTIMATE_USD)
    parser.add_argument('--output', type=Path, default=Path('/tmp/opencode-go-acceptance.json'))
    args = parser.parse_args()
    if not args.live:
        print(json.dumps(plan(remaining=args.remaining), indent=2))
        return
    try:
        result = asyncio.run(live(args))
    except Exception as error:
        # Provider errors and keychain diagnostics are normalized by the client;
        # retain only the type and short safe message for partial-run evidence.
        result = {'status': 'failed', 'error_type': type(error).__name__, 'error': str(error)[:300]}
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'complete', 'output': str(args.output),
                      'run_status': result.get('status'),
                      'requests': result.get('sanitized_requests', [])}, indent=2))
    if result.get('status') != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
