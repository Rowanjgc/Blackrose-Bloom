"""llama-router client. Server presets own all inference tuning."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from ..clock_context import request_messages
from .effort import local_fields, local_levels
from .service import key_rejected, local_client
from .stream_limits import StreamBounds, StreamBudget, sse_lines


class RouterClient:
    def __init__(self, endpoint: str, timeout: float = 600, client=None, service=None, budgets=None,
                 stream_bounds: StreamBounds | None = None, auth: httpx.Auth | None = None):
        self.endpoint = endpoint.rstrip('/')
        self.timeout = timeout
        self.service = service
        self.budgets = budgets
        self.stream_bounds = stream_bounds or StreamBounds()
        self.capabilities: dict[str, dict] = {}
        # The managing service's key follows each launch (see service.router_key).
        if auth is None and service is not None:
            auth = service.key
        self.client = client or local_client(self.endpoint, base_url=self.endpoint, timeout=timeout)
        if auth is not None:
            self.client.auth = auth

    def take_warnings(self) -> list[str]:
        return self.service.take_warnings() if self.service is not None else []

    async def models(self) -> list[dict]:
        if self.service is not None:
            await self.service.ensure_running()
        response = await self.client.get('/v1/models')
        if response.status_code == 401:
            raise key_rejected(self.endpoint)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(f'{self.endpoint}/v1/models returned an invalid model catalog; '
                               'expected a JSON object with a data list') from error
        rows = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict)
                                              or not isinstance(row.get('id'), str)
                                              or not row['id'] for row in rows):
            raise RuntimeError(f'{self.endpoint}/v1/models returned an invalid model catalog; '
                               'expected a data list containing model objects with string ids')
        for row in rows:
            architecture = row.get('architecture', {})
            if architecture is not None and not isinstance(architecture, dict):
                raise RuntimeError(f'{self.endpoint}/v1/models returned an invalid model catalog; '
                                   'model architecture metadata must be an object')
        return [r for r in rows if 'text' in (r.get('architecture') or {}).get('output_modalities', ['text'])]

    def _management_required(self) -> RuntimeError:
        return RuntimeError(
            'The endpoint is reachable and lists models, but it does not expose the llama-router '
            'model management contract. Blackrose requires llama-router status metadata and '
            '`/models/load` (and `/models/unload`) to manage local models. Configure a '
            'llama-router endpoint with a model preset; a plain OpenAI-compatible server is not '
            'supported for local model loading.'
        )

    async def load(self, model: str) -> list[dict]:
        """Return the final fresh catalog used to establish readiness."""
        rows = await self.models()
        row = next((r for r in rows if r['id'] == model), None)
        if row is None:
            raise ValueError(f'Model {model!r} is not available at {self.endpoint}')
        status = row.get('status')
        status_value = status.get('value') if isinstance(status, dict) else None
        if status_value is None:
            raise self._management_required()
        deadline = asyncio.get_running_loop().time() + self.timeout
        # Sleeping children are ready to accept inference: only an actual task
        # wakes them. A management load is redundant (and can be rejected).
        if status_value not in ('loaded', 'sleeping', 'loading'):
            self.capabilities.pop(model, None)
            # Crucially, no sampling, context, template or GPU overrides.
            while True:
                response = await self.client.post('/models/load', json={'model': model})
                # The router protects another client's active generation. Its
                # inference route queues, but explicit management loads return
                # this specific transient error. Wait without unloading anyone.
                try:
                    detail = response.json()
                except ValueError:
                    detail = {}
                error = detail.get('error') if isinstance(detail, dict) else None
                message = error.get('message') if isinstance(error, dict) else None
                busy = response.status_code == 500 and message == 'model limit reached, try again later'
                raced = response.status_code == 400 and message == 'model is already running'
                if not busy and not raced:
                    if response.status_code == 404:
                        raise self._management_required()
                    response.raise_for_status()
                    if detail.get('success') is False:
                        raise RuntimeError(f'llama-router could not load {model}')
                    rows = await self.models()
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f'Timed out waiting for llama-router to load {model}')
                await asyncio.sleep(min(0.25, remaining))
                rows = await self.models()
                row = next((r for r in rows if r['id'] == model), None)
                if row and row.get('status', {}).get('value') in ('loaded', 'sleeping', 'loading'):
                    break
        while True:
            row = next((r for r in rows if r['id'] == model), None)
            status = row.get('status', {}).get('value') if row else None
            if status in ('loaded', 'sleeping'):
                if model not in self.capabilities:
                    inspected = await self._inspect(model)
                    if inspected is not None:
                        self.capabilities[model] = inspected
                return rows
            if status not in ('loading', 'unloaded'):
                raise RuntimeError(f'llama-router could not load {model} (status: {status})')
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f'Timed out waiting for llama-router to load {model}')
            await asyncio.sleep(min(0.25, remaining))
            rows = await self.models()

    async def unload(self, model: str) -> None:
        """Wait for GPU memory to be released before a local image job."""
        rows = await self.models()
        row = next((r for r in rows if r['id'] == model), None)
        if row is None:
            raise ValueError(f'Model {model!r} is not available at {self.endpoint}')
        status = row.get('status')
        status_value = status.get('value') if isinstance(status, dict) else None
        if status_value is None:
            raise self._management_required()
        if status_value == 'unloaded':
            return
        try:
            response = await self.client.post('/models/unload', json={'model': model})
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise self._management_required() from error
            raise
        if response.json().get('success') is False:
            raise RuntimeError(f'llama-router could not unload {model}')
        async with asyncio.timeout(self.timeout):
            while True:
                row = next((r for r in await self.models() if r['id'] == model), None)
                if row and row.get('status', {}).get('value') == 'unloaded':
                    return
                if row is None or row.get('status', {}).get('value') == 'failed':
                    raise RuntimeError(f'llama-router could not unload {model}')
                await asyncio.sleep(0.25)

    async def _inspect(self, model: str) -> dict | None:
        """Read template reasoning support; /props answers only for a loaded model.

        ``None`` means the read failed: caching that as "no support" would keep
        reasoning effort switched off for the rest of the session.
        """
        try:
            response = await self.client.get('/props', params={'model': model})
            response.raise_for_status()
            props = response.json()
            return {'thinking': 'enable_thinking' in (props.get('chat_template') or ''),
                    'reasoning_effort': bool((props.get('chat_template_caps') or {}).get('supports_reasoning_effort'))}
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    def reasoning_levels(self, model: str) -> tuple:
        return local_levels(self.capabilities.get(model))

    async def stream(self, model: str, messages: list[dict], tools=(), *, effort: str = 'default',
                     stream_budget: StreamBudget | None = None) -> AsyncIterator[dict]:
        budget = stream_budget or StreamBudget(self.stream_bounds)
        body = {'model': model, 'messages': request_messages(messages), 'stream': True, 'stream_options': {'include_usage': True},
                'timings_per_token': True}
        if tools:
            body['tools'] = list(tools)
            body['tool_choice'] = 'auto'
        # Omitted generation parameters inherit the exact live server preset;
        # only a reasoning effort the user explicitly chose is sent.
        body.update(local_fields(effort, self.capabilities.get(model), self.budgets))
        completed = False
        saw_calls = False
        async with self.client.stream('POST', '/v1/chat/completions', json=body) as response:
            if response.is_error:
                # Error responses may stream indefinitely too. Retain only a
                # small diagnostic prefix and close the response afterwards.
                error_body = bytearray()
                async with asyncio.timeout_at(budget.deadline):
                    async for chunk in response.aiter_bytes():
                        budget.add_response_bytes(len(chunk))
                        error_body.extend(chunk[:8192 - len(error_body)])
                        if len(error_body) >= 8192:
                            break
                error_text = error_body.decode(response.encoding or 'utf-8', errors='replace')
                try:
                    detail = json.loads(error_body).get('error', error_text)
                    detail = detail.get('message', str(detail)) if isinstance(detail, dict) else str(detail)
                except (ValueError, AttributeError):
                    detail = error_text
                raise RuntimeError(f'Model request failed (HTTP {response.status_code}): {detail[:1000]}')
            async for line in sse_lines(response, budget):
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                if not data:
                    continue
                packet = json.loads(data)
                if packet.get('error'):
                    raise RuntimeError(str(packet['error']))
                if packet.get('usage') or packet.get('timings'):
                    yield {'delta': {}, '_usage': packet.get('usage', {}),
                           '_timings': packet.get('timings', {})}
                for choice in packet.get('choices', []):
                    saw_calls = saw_calls or bool((choice.get('delta') or {}).get('tool_calls'))
                    # Some local server templates finish tool rounds with stop.
                    # Require explicit finish metadata, but retain that variant.
                    if saw_calls and choice.get('finish_reason') == 'stop':
                        choice = dict(choice, finish_reason='tool_calls')
                    yield choice
                    if choice.get('finish_reason'):
                        completed = True
        if not completed:
            raise RuntimeError('The model stream ended before completion')

    async def suggest_title(self, model, messages):
        from ..titles import generate_title
        return await generate_title(self, model, messages)

    async def aclose(self):
        try:
            if self.service is not None:
                await self.service.close()
        finally:
            await self.client.aclose()
