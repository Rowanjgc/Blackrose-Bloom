"""Per-request reasoning effort: Default sends nothing; chosen levels map per provider."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from blackrose.backend.effort import LEVELS, cloud_fields, cloud_levels, local_fields, local_levels
from blackrose.backend.opencode import OpenCodeGoClient
from blackrose.backend.opencode_protocols import _prepare, prepare
from blackrose.backend.router import RouterClient
from blackrose.config import Config, load_config
from blackrose.gui.session import Session
from test_gui_images import make_runtime

THINKING = {'thinking': True, 'reasoning_effort': False}
PRESET_BODY = {'model', 'messages', 'stream', 'stream_options', 'timings_per_token'}


class MappingTests(unittest.TestCase):
    def test_default_and_unknown_templates_send_nothing(self):
        for caps in (None, {}, {'thinking': False}, THINKING):
            self.assertEqual(local_fields('default', caps), {})
        self.assertEqual(local_fields('high', {}), {})
        self.assertEqual(local_levels({}), ('default',))
        self.assertEqual(local_levels(THINKING), LEVELS)

    def test_off_uses_the_template_switch_without_a_zero_budget(self):
        # A zero budget alone leaked reasoning into content on the live Nemotron template.
        self.assertEqual(local_fields('off', THINKING), {'chat_template_kwargs': {'enable_thinking': False}})

    def test_levels_budget_and_only_pass_effort_to_templates_that_read_it(self):
        self.assertEqual(local_fields('low', THINKING), {'thinking_budget_tokens': 1024})
        self.assertEqual(local_fields('high', THINKING, {'low': 1, 'medium': 2, 'high': 3, 'xhigh': 4}),
                         {'thinking_budget_tokens': 3})
        self.assertEqual(local_fields('xhigh', {'thinking': True, 'reasoning_effort': True}),
                         {'thinking_budget_tokens': 32768, 'reasoning_effort': 'xhigh'})

    def test_cloud_families(self):
        self.assertEqual(cloud_levels('gpt-6-luna'), LEVELS)
        self.assertNotIn('off', cloud_levels('claude-opus-5-5'))
        for model in ('gemini-4-argon', 'minimax-m3', 'qwen4-max', 'kimi-k3'):
            self.assertEqual(cloud_levels(model), ('default',))
        self.assertEqual(cloud_fields('responses', 'off'), {'reasoning': {'effort': 'none'}})
        self.assertEqual(cloud_fields('messages', 'xhigh'), {'output_config': {'effort': 'xhigh'}})
        self.assertEqual(cloud_fields('messages', 'off'), {})
        self.assertEqual(cloud_fields('google', 'high'), {})

    def test_prepare_at_default_is_byte_identical_to_the_preset_request(self):
        messages = [{'role': 'system', 'content': 'Be brief.'}, {'role': 'user', 'content': 'Hi'}]
        for protocol in ('chat', 'messages', 'responses', 'google'):
            with self.subTest(protocol=protocol):
                self.assertEqual(json.dumps(prepare(protocol, 'm', messages, [])),
                                 json.dumps(_prepare(protocol, 'm', messages, [])))
        _, body = prepare('responses', 'gpt-6-luna', messages, [], 'high')
        self.assertEqual(body['reasoning'], {'effort': 'high'})
        self.assertEqual(body['include'], ['reasoning.encrypted_content'])

    def test_go_client_reports_levels_by_family(self):
        client = OpenCodeGoClient()
        self.assertEqual(client.reasoning_levels('opencode-go/gpt-6-luna'), LEVELS)
        self.assertEqual(client.reasoning_levels('opencode-go/minimax-m3'), ('default',))


class RouterEffortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bodies, self.props_calls = [], 0
        self.template = '{% if enable_thinking %}<think>{% endif %}'

        def handler(request):
            if request.url.path == '/v1/models':
                return httpx.Response(200, json={'data': [{'id': 'qwen', 'status': {'value': 'loaded'}}]})
            if request.url.path == '/props':
                self.props_calls += 1
                self.assertEqual(request.url.params['model'], 'qwen')
                if self.template is None:
                    return httpx.Response(400, json={'error': {'message': 'model is not loaded'}})
                return httpx.Response(200, json={'chat_template': self.template,
                                                 'chat_template_caps': {'supports_reasoning_effort': False}})
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, text='data: ' + json.dumps(
                {'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n')
        client = httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler))
        self.router = RouterClient('http://test', client=client, budgets={'low': 1, 'medium': 2, 'high': 3, 'xhigh': 4})

    async def asyncTearDown(self):
        await self.router.aclose()

    async def test_load_inspects_the_template_once(self):
        await self.router.load('qwen')
        await self.router.load('qwen')
        self.assertEqual(self.props_calls, 1)
        self.assertEqual(self.router.reasoning_levels('qwen'), LEVELS)
        self.assertEqual(self.router.reasoning_levels('never-loaded'), ('default',))

    async def test_default_body_keeps_the_preset_and_levels_add_fields(self):
        await self.router.load('qwen')
        for effort in ('default', 'off', 'high'):
            [_ async for _ in self.router.stream('qwen', [{'role': 'user', 'content': 'Hi'}], effort=effort)]
        default, off, high = self.bodies
        self.assertEqual(set(default), PRESET_BODY)
        self.assertEqual(off['chat_template_kwargs'], {'enable_thinking': False})
        self.assertNotIn('thinking_budget_tokens', off)
        self.assertEqual(high['thinking_budget_tokens'], 3)

    async def test_templates_without_thinking_and_failed_inspection_stay_default(self):
        self.template = '{{ messages }}'
        await self.router.load('qwen')
        self.assertEqual(self.router.reasoning_levels('qwen'), ('default',))
        [_ async for _ in self.router.stream('qwen', [], effort='high')]
        self.assertEqual(set(self.bodies[0]), PRESET_BODY)
        self.router.capabilities.clear()
        self.template = None
        await self.router.load('qwen')
        self.assertEqual(self.router.reasoning_levels('qwen'), ('default',))


class ConfigTests(unittest.TestCase):
    def test_validation_and_budget_overrides(self):
        with self.assertRaises(ValueError):
            Config(reasoning_effort={'qwen': 'turbo'})
        with self.assertRaises(ValueError):
            Config(reasoning_budgets={'enormous': 1})
        config = Config(reasoning_effort={'a': 'default', 'b': 'low'}, reasoning_budgets={'high': 9000})
        self.assertEqual(config.reasoning_effort, {'b': 'low'})
        self.assertEqual(config.reasoning_budgets, {'low': 1024, 'medium': 4096, 'high': 9000, 'xhigh': 32768})


class Backend:
    """A thinking-capable local model, a model without the knob, and recorded request levels."""
    def __init__(self):
        self.efforts = []
        self.release = asyncio.Event()
        self.release.set()

    def reasoning_levels(self, model):
        return LEVELS if model == 'test-vision' else ('default',)

    async def load(self, model):
        pass

    async def models(self, provider=None):
        return [{'id': 'test-vision', 'architecture': {'input_modalities': ['text', 'image']}},
                {'id': 'plain', 'architecture': {'input_modalities': ['text']}}]

    async def stream(self, model, messages, tools, effort='default'):
        self.efforts.append(effort)
        await self.release.wait()
        yield {'delta': {'content': 'Done.'}, 'finish_reason': 'stop'}

    async def aclose(self):
        pass


class SessionEffortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')})
        self.environment.start()
        self.config, self.runtime = make_runtime(self.root)
        self.backend = self.runtime.controller.backend = Backend()
        await self.runtime.open()
        self.events = []
        self.session = Session(self.runtime.controller, lambda kind, payload: self.events.append((kind, payload)))
        await self.session.boot()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.environment.stop()
        self.temp.cleanup()

    def status(self):
        return next(payload for kind, payload in reversed(self.events) if kind == 'status')

    async def test_level_is_saved_per_model_and_restored_on_switch(self):
        self.assertEqual(self.status()['reasoning_effort'], 'default')
        self.assertEqual(self.status()['reasoning_effort_levels'], list(LEVELS))
        self.assertEqual(await self.session.set_reasoning_effort('high'), {'ok': True, 'reasoning_effort': 'high'})
        self.assertEqual(self.status()['reasoning_effort'], 'high')
        self.assertEqual(load_config(self.root / 'missing.toml').reasoning_effort, {'test-vision': 'high'})

        await self.session.select_model('plain', False)
        self.assertEqual(self.status()['reasoning_effort'], 'default')
        self.assertEqual(self.status()['reasoning_effort_levels'], ['default'])
        self.assertIn('error', await self.session.set_reasoning_effort('low'))

        await self.session.select_model('test-vision', False)
        self.assertEqual(self.status()['reasoning_effort'], 'high')
        await self.session.set_reasoning_effort('default')
        self.assertEqual(load_config(self.root / 'missing.toml').reasoning_effort, {})

    async def test_effort_command_and_unknown_level(self):
        await self.session.run_command('/effort off')
        self.assertEqual(self.status()['reasoning_effort'], 'off')
        self.assertIn('error', await self.session.set_reasoning_effort('turbo'))

    async def test_turn_snapshots_the_level_and_a_mid_reply_change_applies_next(self):
        await self.session.set_reasoning_effort('high')
        self.backend.release.clear()
        self.assertEqual(await self.session.submit('First'), {'clear': True})
        while not self.backend.efforts:
            await asyncio.sleep(0)
        # Changing during a reply is allowed and does not touch the running request.
        self.assertEqual((await self.session.set_reasoning_effort('low'))['reasoning_effort'], 'low')
        self.backend.release.set()
        await self.session._generation
        self.assertEqual(await self.session.submit('Second'), {'clear': True})
        await self.session._generation
        self.assertEqual(self.backend.efforts, ['high', 'low'])

    async def test_default_never_passes_an_effort_argument(self):
        await self.session.submit('Hello')
        await self.session._generation
        self.assertEqual(self.backend.efforts, ['default'])


if __name__ == '__main__':
    unittest.main()
