"""OpenCode Go discovery and streaming, with credentials resolved only at use time.

API and protocol assignments: https://opencode.ai/v2/docs/console/go/#endpoints
The public catalog provides model limits when available.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx

from ..clock_context import request_messages
from ..credentials import OpenCodeGoKeychain
from .effort import DEFAULT_ONLY, clamp, cloud_levels
from .opencode_protocols import protocol_for, prepare, StreamDecoder
from .stream_limits import StreamBounds, StreamBudget, sse_lines

PREFIX = 'opencode-go/'
ENDPOINT = 'https://opencode.ai/inference/go/v1'


class OpenCodeGoClient:
    def __init__(self, timeout=600, *, keychain=None, transport=None,
                 stream_bounds: StreamBounds | None = None):
        self.keychain = keychain or OpenCodeGoKeychain()
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport,
                                        headers={'User-Agent': 'blackrose-bloom/0.2.0'})
        self.catalog = None
        self.stream_bounds = stream_bounds or StreamBounds()
        # A turn snapshot is task-local so an unrelated turn or a credential
        # prompt cannot inherit a key resolved for another turn.
        self._turn_credential: ContextVar[str | None] = ContextVar(
            f'opencode-go-turn-credential-{id(self)}', default=None)

    @asynccontextmanager
    async def turn_credentials(self):
        """Resolve one key for a whole turn and release it when the turn ends.

        Load validation and every tool-round stream reuse this snapshot. A new
        turn looks the key up again, so CLI ``set``/``delete`` in another
        process takes effect there. Nothing is cached across turns because a
        running process cannot be invalidated by an out-of-process mutation.
        """
        key = await asyncio.to_thread(self.keychain.get)
        token = self._turn_credential.set(key)
        try:
            yield
        finally:
            self._turn_credential.reset(token)

    async def _credential(self) -> str:
        key = self._turn_credential.get()
        if key is not None:
            return key
        return await asyncio.to_thread(self.keychain.get)

    async def models(self):
        try:
            # Public discovery must not prompt to unlock the keyring.
            response = await self.client.get(ENDPOINT + '/models', timeout=30)
            self._check(response)
            rows = response.json()['data']
            self.catalog = []
            for row in rows:
                model = row['id']
                protocol = protocol_for(model)
                if protocol is None:
                    continue
                modalities = row.get('architecture', {}).get('input_modalities')
                if modalities is None:
                    # Be conservative when the provider supplies no capabilities.
                    modalities = ['text', 'image'] if model.startswith(
                        ('kimi-', 'gemini-', 'gpt-', 'claude-', 'grok-', 'muse-')) else ['text']
                limits = row.get('limit') or {}
                context = limits.get('context')
                self.catalog.append({**row, 'id': PREFIX + model, 'name': row.get('name', model),
                    **({'context_length': context} if context is not None else {}),
                    'provider': 'opencode-go', 'protocol': protocol,
                    'architecture': {'input_modalities': modalities, 'output_modalities': ['text']},
                    'status': {'value': 'available'}})
            return self.catalog
        except httpx.HTTPError:
            raise RuntimeError('Cannot reach OpenCode Go. Check your connection and try again.') from None
        except (ValueError, KeyError, TypeError):
            raise RuntimeError('OpenCode Go returned an invalid model catalog.') from None

    @staticmethod
    def _check(response):
        if response.status_code != 200:
            hints = {401: 'Check your API key with blackrose opencode-go-key set.',
                     403: 'Check your Go subscription and API access.',
                     402: 'Check your Go subscription and usage limits.',
                     429: 'Usage limit reached; try again later.'}
            raise RuntimeError(f'OpenCode Go request failed (HTTP {response.status_code}). '
                + hints.get(response.status_code, 'Try again or check the model availability.'))

    async def load(self, model):
        if self.catalog is None:
            await self.models()
        if not any(row['id'] == model for row in self.catalog):
            raise ValueError('This model is unavailable through OpenCode Go. Refresh the model list.')
        # Fail before selecting/saving a provider whose credential is unavailable.
        await self._credential()
        return self.catalog

    def reasoning_levels(self, model):
        return cloud_levels(model[len(PREFIX):]) if model.startswith(PREFIX) else DEFAULT_ONLY

    async def stream(self, model, messages, tools=(), *, session_id=None, effort='default',
                     stream_budget: StreamBudget | None = None):
        if not model.startswith(PREFIX) or protocol_for(model[len(PREFIX):]) is None:
            raise ValueError('Invalid OpenCode Go model')
        if not session_id:
            raise ValueError('OpenCode Go requires a stable conversation ID')
        key = await self._credential()
        raw_model = model[len(PREFIX):]
        protocol = protocol_for(raw_model)
        path, body = prepare(protocol, raw_model, request_messages(messages), tools,
                             clamp(effort, cloud_levels(raw_model)))
        headers = {'Authorization': f'Bearer {key}', 'x-opencode-session': str(session_id)}
        if protocol == 'messages':
            headers['anthropic-version'] = '2023-06-01'
        decoder = StreamDecoder(protocol)
        budget = stream_budget or StreamBudget(self.stream_bounds)
        try:
            # No automatic retries or redirect forwarding of credentials.
            async with self.client.stream('POST', ENDPOINT + path, json=body, headers=headers) as response:
                self._check(response)
                data = []
                async for line in sse_lines(response, budget):
                    if line.startswith('data:'):
                        data.append(line[5:].strip())
                    elif not line and data:
                        for choice in decoder.feed('\n'.join(data)):
                            yield choice
                        data.clear()
                if data:
                    for choice in decoder.feed('\n'.join(data)):
                        yield choice
            if not decoder.completed:
                raise RuntimeError('The OpenCode Go stream ended before completion.')
        except httpx.HTTPError:
            raise RuntimeError('OpenCode Go connection failed; the request was not retried.') from None
        except (ValueError, KeyError, TypeError):
            raise RuntimeError('OpenCode Go returned an invalid stream response.') from None

    async def aclose(self):
        await self.client.aclose()
