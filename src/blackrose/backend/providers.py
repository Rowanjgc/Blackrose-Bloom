"""Explicit model-provider selection; local presets stay with llama-router."""
from contextlib import aclosing, nullcontext

from .effort import DEFAULT_ONLY
from .opencode import PREFIX, OpenCodeGoClient


def model_provider(model):
    return 'opencode-go' if model.startswith(PREFIX) else 'local'


class ChatProviders:
    accepts_stream_budget = True

    def __init__(self, local, go=None):
        self.local = local
        self.go = go or OpenCodeGoClient(timeout=local.timeout,
                                         stream_bounds=getattr(local, 'stream_bounds', None))

    def _client(self, provider):
        if provider == 'local':
            return self.local
        if provider == 'opencode-go':
            return self.go
        raise ValueError('Unknown model provider')

    async def models(self, provider='local'):
        """Catalog of one provider; callers name it, usually via model_provider(model)."""
        return await self._client(provider).models()

    def take_warnings(self):
        """Local router adoption warnings not yet shown to the user."""
        take = getattr(self.local, 'take_warnings', None)
        return take() if take else []

    async def load(self, model):
        return await self._client(model_provider(model)).load(model)

    async def stream(self, model, messages, tools=(), *, session_id=None, effort='default',
                     stream_budget=None):
        client = self._client(model_provider(model))
        kwargs = {'session_id': session_id} if model_provider(model) == 'opencode-go' else {}
        if effort != 'default':
            kwargs['effort'] = effort
        if stream_budget is not None:
            kwargs['stream_budget'] = stream_budget
        async with aclosing(client.stream(model, messages, tools, **kwargs)) as stream:
            async for choice in stream:
                yield choice

    def reasoning_levels(self, model):
        levels = getattr(self._client(model_provider(model)), 'reasoning_levels', None)
        return levels(model) if levels else DEFAULT_ONLY

    async def suggest_title(self, model, messages, **options):
        from ..titles import generate_title
        return await generate_title(self, model, messages, **options)

    def request_options(self, conversation, model):
        """Provider transport identity for one model; no hidden selection is consulted."""
        if model_provider(model) != 'opencode-go':
            return {}
        from uuid import NAMESPACE_URL, uuid5
        identity = f'blackrose-bloom:{conversation.id}:{conversation.created_at.isoformat()}'
        return {'session_id': str(uuid5(NAMESPACE_URL, identity))}

    def turn_credentials(self, model):
        """Scope one credential lookup to this turn when it uses a keyed provider."""
        if model_provider(model) != 'opencode-go':
            return nullcontext()
        scope = getattr(self.go, 'turn_credentials', None)
        return scope() if scope else nullcontext()

    def continuation(self, message, reasoning, provider_content, model):
        if model_provider(model) == 'opencode-go':
            if reasoning:
                message['reasoning_content'] = reasoning
            if provider_content:
                message['_provider_content'] = provider_content
        return message

    async def aclose(self):
        try:
            await self.go.aclose()
        finally:
            await self.local.aclose()
