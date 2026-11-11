"""Image API contract, secret handling, and durable chat/UI integration."""
from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

import httpx

from blackrose.credentials import CredentialError, OpenRouterKeychain
from blackrose.config import Config
from blackrose.image_generation import OpenRouterImages
import test_gui_images as gui_images

png = gui_images.png


class Keychain:
    def get(self):
        return 'test-secret-do-not-log'


class CredentialTests(unittest.TestCase):
    @patch('blackrose.credentials.shutil.which', return_value='/usr/bin/secret-tool')
    @patch('blackrose.credentials.subprocess.run')
    def test_key_passed_only_over_stdin(self, run, _which):
        run.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
        OpenRouterKeychain().set('test-secret-do-not-log')
        args, kwargs = run.call_args
        self.assertNotIn('test-secret-do-not-log', repr(args))
        self.assertEqual(kwargs['input'], 'test-secret-do-not-log')
        self.assertNotIn('shell', kwargs)
        self.assertIn('blackrose-bloom', args[0])

    @patch('blackrose.credentials.shutil.which', return_value=None)
    def test_missing_keychain_has_no_fallback(self, _which):
        with self.assertRaisesRegex(CredentialError, 'secret-tool'):
            OpenRouterKeychain().get()

    @patch('blackrose.credentials.shutil.which', return_value='/usr/bin/secret-tool')
    @patch('blackrose.credentials.subprocess.run')
    def test_backend_diagnostics_never_reveal_secrets(self, run, _which):
        run.return_value = SimpleNamespace(returncode=1, stdout='secret', stderr='secret')
        with self.assertRaises(CredentialError) as caught:
            OpenRouterKeychain().get()
        self.assertNotIn('secret', str(caught.exception))
        run.side_effect = subprocess.TimeoutExpired('secret-tool', 60, output='secret')
        with self.assertRaises(CredentialError) as caught:
            OpenRouterKeychain().get()
        self.assertNotIn('secret', str(caught.exception))


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def call_with(self, handler, **kwargs):
        service = OpenRouterImages('black-forest-labs/flux.2-klein-4b',
                                  keychain=Keychain(), transport=httpx.MockTransport(handler))
        try:
            return await service.generate('A hand-drawn creature on white.', **kwargs)
        finally:
            await service.close()

    async def test_image_api_request_and_response(self):
        def handler(request):
            self.assertEqual(str(request.url), 'https://openrouter.ai/api/v1/images')
            self.assertEqual(request.headers['Authorization'], 'Bearer test-secret-do-not-log')
            payload = json.loads(request.content)
            self.assertEqual(payload, {'model': 'black-forest-labs/flux.2-klein-4b',
                'prompt': 'A hand-drawn creature on white.', 'n': 1, 'aspect_ratio': '16:9',
                'output_format': 'png', 'input_references': [
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,reference'}}]})
            return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}],
                                             'usage': {'cost': 0.014}})
        result = await self.call_with(handler, references=['data:image/png;base64,reference'])
        self.assertEqual(result.data, png())
        self.assertEqual((result.info.width, result.info.height, result.cost), (320, 200, 0.014))

    async def test_seedream_default_uses_base_tier_and_preserves_exact_label_prompt(self):
        prompt = 'Xiaohei sketches a circle. Exact labels: "Draft", "Done". No other text.'
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}],
                                             'usage': {'cost': 0.045}})
        service = OpenRouterImages(Config().image_model, keychain=Keychain(),
                                  transport=httpx.MockTransport(handler))
        try:
            generated = await service.generate(prompt)
        finally:
            await service.close()
        self.assertEqual(requests, [{'model': 'bytedance-seed/seedream-5-0-pro',
            'prompt': prompt, 'aspect_ratio': '16:9', 'n': 1, 'resolution': '1K'}])
        self.assertEqual(generated.model, 'bytedance-seed/seedream-5-0-pro')
        self.assertEqual(generated.cost, 0.045)

    async def test_failed_paid_requests_are_not_retried_and_do_not_echo_provider_body(self):
        for status in (302, 401, 402, 429, 500):
            calls = []
            def handler(request, *, calls=calls, status=status):
                calls.append(request)
                return httpx.Response(status, headers={'Location': 'https://elsewhere.example'},
                                      text='test-secret-do-not-log')
            with self.assertRaises(RuntimeError) as caught:
                await self.call_with(handler)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('test-secret-do-not-log', str(caught.exception))

    async def test_network_errors_are_sanitized(self):
        def handler(request):
            raise httpx.ConnectError('test-secret-do-not-log', request=request)
        with self.assertRaises(RuntimeError) as caught:
            await self.call_with(handler)
        self.assertNotIn('test-secret-do-not-log', str(caught.exception))

    async def test_malformed_image_responses_are_rejected(self):
        for body in ({'data': []}, {'data': [{'b64_json': 'bad base64'}]},
                     {'data': [{'b64_json': base64.b64encode(b'not an image').decode()}]},
                     {'error': {'message': 'test-secret-do-not-log'}}, []):
            with self.assertRaisesRegex(RuntimeError, 'no valid supported image'):
                await self.call_with(lambda request, body=body: httpx.Response(200, json=body))

    async def test_missing_key_does_not_send_request(self):
        service = OpenRouterImages('test', transport=httpx.MockTransport(
            lambda request: self.fail('No request should be sent')))
        try:
            with patch.object(service.keychain, 'get', side_effect=CredentialError('missing key')):
                with self.assertRaisesRegex(CredentialError, 'missing key'):
                    await service.generate('test')
        finally:
            await service.close()


class GeneratingBackend(gui_images.Backend):
    def __init__(self, outcome='complete'):
        self.requests = []
        self.outcome = outcome

    async def stream(self, model, messages, tools):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            yield {'delta': {'tool_calls': [{'index': 0, 'id': 'image-call', 'function': {
                'name': 'image_gen', 'arguments': json.dumps({'prompt': 'Xiaohei unfolds a thought.'})}}]},
                'finish_reason': 'tool_calls'}
        elif self.outcome == 'error':
            raise RuntimeError('Continuation failed')
        elif self.outcome == 'cancel':
            raise asyncio.CancelledError
        else:
            yield {'delta': {'content': 'The illustration is ready.'}, 'finish_reason': 'stop'}


class ChatGenerationTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the real SQLite/store/UI fixture and its preview regressions.
    asyncSetUp = gui_images.ImageTests.asyncSetUp
    asyncTearDown = gui_images.ImageTests.asyncTearDown
    latest = gui_images.ImageTests.latest

    async def configure_generator(self, outcome='complete'):
        c = self.runtime.controller
        c.backend = GeneratingBackend(outcome)
        c.config.image_provider = 'openrouter'
        self.requests = []
        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})
        c.image_generator = OpenRouterImages(c.config.image_model, keychain=Keychain(),
                                            transport=httpx.MockTransport(handler))
        return c

    async def test_generated_image_displays_immediately_and_after_reopen_with_visual_context(self):
        c = await self.configure_generator()
        await self.session.submit('Illustrate this idea using Xiaohei')
        await self.session._generation
        item = self.latest('reply_images')['attachments'][0]
        self.assertTrue((await self.session.image_preview(item['preview_id']))['src'].startswith('data:image/png;'))
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.attachments[0].id, item['preview_id'])
        self.assertEqual(saved.state.value, 'complete')
        await self.session.render_transcript()
        self.assertEqual(self.latest('transcript')['messages'][-1]['attachments'][0], item)
        self.assertEqual(c.backend.requests[-1][-1]['content'][1]['type'], 'image_url')
        tool_message = next(m for m in c.backend.requests[-1] if m['role'] == 'tool')
        result = json.loads(tool_message['content'])
        self.assertTrue(result['saved_path'].startswith(str(self.root / 'attachments')))
        self.assertNotIn('b64_json', result)
        self.assertNotIn('test-secret-do-not-log', tool_message['content'])

    async def test_image_survives_continuation_failure_or_cancellation(self):
        for outcome in ('error', 'cancel'):
            c = await self.configure_generator(outcome)
            await c.new_conversation()
            await self.session.submit('Generate one image')
            try:
                await self.session._generation
            except asyncio.CancelledError:
                pass
            saved = (await c.current_messages())[-1]
            self.assertEqual(saved.state.value, 'error' if outcome == 'error' else 'cancelled')
            self.assertEqual(len(saved.attachments), 1)
            self.assertTrue(c.attachment_path(saved.attachments[0]).is_file())
            await c.image_generator.close()

    async def test_reference_images_are_scoped_to_current_conversation(self):
        c = await self.configure_generator()
        await self.session.attach_bytes(png(), 'reference.png')
        await self.session.submit('Draw this')
        await self.session._generation
        messages = await c.current_messages()
        reference_id = messages[0].attachments[0].id
        # Direct calls stand in for a reply whose user approves this exact upload.
        from blackrose.outbound import OutboundPolicy
        c.outbound = OutboundPolicy(approve=AsyncMock(return_value=True), private=True)
        result, _, _ = await c._generate_image(messages[-1], {
            'prompt': 'Simplify the reference', 'reference_attachment_ids': [reference_id]})
        self.assertIn('input_references', self.requests[-1])
        self.assertEqual(result['width'], 320)
        await c.new_conversation()
        with self.assertRaisesRegex(ValueError, 'this conversation'):
            await c._generate_image(messages[-1], {
                'prompt': 'test', 'reference_attachment_ids': [reference_id]})

    async def test_tool_can_be_disabled(self):
        c = await self.configure_generator()
        self.assertIn('image_gen', [t['function']['name'] for t in c.tool_definitions()])
        c.config.image_generation_enabled = False
        self.assertNotIn('image_gen', [t['function']['name'] for t in c.tool_definitions()])

    async def test_text_only_chat_can_generate_and_continue_without_vision_payloads(self):
        c = await self.configure_generator()
        c.backend.models = AsyncMock(return_value=[{'id': c.model_name,
            'architecture': {'input_modalities': ['text']}}])
        await self.session.submit('Draw an illustration')
        await self.session._generation
        saved = (await c.current_messages())[-1]
        self.assertEqual(len(saved.attachments), 1)
        self.assertEqual(c.backend.requests[-1][-1]['role'], 'tool')
        self.assertIsInstance(c._wire_message(saved)['content'], str)
        await self.session.submit('Thanks')
        await self.session._generation
        self.assertEqual(self.latest('reply_end')['state'], 'complete')


if __name__ == '__main__':
    unittest.main()
