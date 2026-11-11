"""One image per OpenRouter request, with credentials resolved at use time."""
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass

import httpx

from .attachments.images import MAX_IMAGE_BYTES, ImageInfo, inspect_image
from .credentials import OpenRouterKeychain

IMAGE_ENDPOINT = 'https://openrouter.ai/api/v1/images'
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
# Request options that differ by model. Unlisted models use the default output format.
MODEL_OPTIONS = {'bytedance-seed/seedream-5-0-pro': {'resolution': '1K'}}
DEFAULT_OPTIONS = {'output_format': 'png'}


@dataclass(frozen=True)
class GeneratedImage:
    """Downloaded output, retained even when later cleanup fails or is stopped.

    Callers persist the image before reporting cleanup_warning or honoring
    cancelled; either flag prevents normal model continuation.
    """
    data: bytes
    info: ImageInfo
    model: str
    cost: float | None
    cleanup_warning: str | None = None
    cancelled: bool = False


class OpenRouterImages:
    def __init__(self, model: str, timeout: float = 600, *, keychain=None, transport=None):
        self.model = model
        self.keychain = keychain or OpenRouterKeychain()
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport)

    async def close(self):
        await self._client.aclose()

    async def generate(self, prompt: str, aspect_ratio: str = '16:9', references=(), *,
                       model: str | None = None) -> GeneratedImage:
        model = model or self.model
        key = await asyncio.to_thread(self.keychain.get)
        # Seedream: pin the $0.045 base tier; 2K uses the $0.09 high-resolution tier.
        # That endpoint does not advertise an output_format parameter.
        payload = {'model': model, 'prompt': prompt, 'aspect_ratio': aspect_ratio,
                   'n': 1, **MODEL_OPTIONS.get(model, DEFAULT_OPTIONS)}
        if references:
            payload['input_references'] = [
                {'type': 'image_url', 'image_url': {'url': reference}} for reference in references]
        try:
            # Do not retry a paid request automatically or forward authorization on redirects.
            async with self._client.stream('POST', IMAGE_ENDPOINT, json=payload,
                    headers={'Authorization': f'Bearer {key}', 'X-Title': 'Blackrose Bloom'}) as response:
                if response.status_code != 200:
                    hints = {401: 'Check the API key.', 402: 'Check your OpenRouter credit.',
                             429: 'Rate limited; try again later.'}
                    raise RuntimeError(f'OpenRouter image generation failed (HTTP {response.status_code}). '
                                       + hints.get(response.status_code, 'Try again or check the model configuration.'))
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise RuntimeError('OpenRouter image response exceeds the size limit.')
        except httpx.HTTPError:
            raise RuntimeError('OpenRouter image request failed. Check your connection; the request was not retried.') from None
        try:
            import json
            body = json.loads(data)
            encoded = body['data'][0]['b64_json']
            if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                raise ValueError
            image = base64.b64decode(encoded, validate=True)
            info = inspect_image(image)
            if info.width is None or info.height is None:
                raise ValueError
            cost = body.get('usage', {}).get('cost')
            if not isinstance(cost, (float, int)) or isinstance(cost, bool):
                cost = None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, binascii.Error):
            raise RuntimeError('OpenRouter returned no valid supported image.') from None
        return GeneratedImage(image, info, model, cost)
