"""Go credential isolation, provider selection, and native streaming tool loops."""
import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from blackrose.backend.opencode import ENDPOINT, PREFIX, OpenCodeGoClient
from blackrose.backend.opencode_protocols import prepare
from blackrose.backend.providers import ChatProviders
from blackrose.credentials import CredentialError, OpenCodeGoKeychain, OpenRouterKeychain
from blackrose.gui.session import Session
from blackrose.main import main
from test_gui_images import make_runtime

SECRET = 'test-go-secret-never-log'


def events(*packets):
    return ''.join('data: ' + (p if isinstance(p, str) else json.dumps(p)) + '\n\n' for p in packets)


class CredentialTests(unittest.TestCase):
    @patch('blackrose.credentials.shutil.which', return_value='/usr/bin/secret-tool')
    @patch('blackrose.credentials.subprocess.run')
    def test_go_and_openrouter_have_distinct_accounts_and_no_secret_in_arguments(self, run, which):
        run.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
        for keychain, account in [(OpenCodeGoKeychain(), 'opencode-go-api-key'),
                                  (OpenRouterKeychain(), 'openrouter-api-key')]:
            keychain.set(SECRET)
            args, kwargs = run.call_args
            self.assertIn(account, args[0])
            self.assertIn('blackrose-bloom', args[0])
            self.assertNotIn(SECRET, repr(args))
            self.assertEqual(kwargs['input'], SECRET)

    @patch('blackrose.credentials.shutil.which', return_value='/usr/bin/secret-tool')
    @patch('blackrose.credentials.subprocess.run')
    def test_go_missing_or_locked_keyring_never_exposes_diagnostics(self, run, which):
        run.return_value = SimpleNamespace(returncode=1, stdout='', stderr='')
        with self.assertRaisesRegex(CredentialError, 'opencode-go-key set'):
            OpenCodeGoKeychain().get()
        for keychain in (OpenCodeGoKeychain(), OpenRouterKeychain()):
            with self.assertRaisesRegex(CredentialError, 'keychain is locked.*Unlock it and retry'):
                keychain.get()
        run.return_value = SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET)
        with self.assertRaises(CredentialError) as caught:
            OpenCodeGoKeychain().get()
        self.assertNotIn(SECRET, str(caught.exception))

    @patch('blackrose.credentials.OpenCodeGoKeychain.get', return_value=SECRET)
    def test_cli_status_does_not_print_secret(self, get):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['opencode-go-key', 'status']), 0)
        self.assertIn('OpenCode Go', output.getvalue())
        self.assertNotIn(SECRET, output.getvalue())

    @patch('blackrose.credentials.OpenCodeGoKeychain.set')
    @patch('getpass.getpass', return_value=SECRET)
    @patch('sys.stdin.isatty', return_value=True)
    def test_cli_hidden_entry(self, isatty, prompt, store):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['opencode-go-key', 'set']), 0)
        store.assert_called_once_with(SECRET)
        self.assertNotIn(SECRET, output.getvalue())

    @patch('sys.stdin.isatty', return_value=False)
    def test_cli_refuses_unhidden_input(self, isatty):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(['opencode-go-key', 'set']), 1)
        self.assertIn('hidden input', error.getvalue())


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.keychain = Mock(get=Mock(return_value=SECRET))
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()

    def client(self, handler):
        client = OpenCodeGoClient(keychain=self.keychain, transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return client

    async def test_load_reuses_catalog_until_explicit_refresh(self):
        requests = []

        def handler(request):
            requests.append(request.url.path)
            return httpx.Response(200, json={'data': [{'id': 'glm-5.3'}]})

        client = self.client(handler)
        backend = ChatProviders(SimpleNamespace(timeout=10), client)
        first = await backend.load(PREFIX + 'glm-5.3')
        second = await backend.load(PREFIX + 'glm-5.3')
        self.assertEqual(first, second)
        self.assertEqual(first[0]['id'], PREFIX + 'glm-5.3')
        self.assertEqual(requests, [httpx.URL(ENDPOINT).path + '/models'])
        await backend.models('opencode-go')
        self.assertEqual(requests, [httpx.URL(ENDPOINT).path + '/models'] * 2)

    async def test_public_catalog_does_not_unlock_keyring_and_namespaces_models(self):
        def handler(request):
            self.assertEqual(str(request.url), ENDPOINT + '/models')
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(200, json={'data': [{'id': 'glm-5.3'}, {'id': 'minimax-m3'}]})
        client = self.client(handler)
        rows = await client.models()
        self.keychain.get.assert_not_called()
        self.assertEqual([r['id'] for r in rows], [PREFIX + 'glm-5.3', PREFIX + 'minimax-m3'])
        self.assertEqual(rows[1]['protocol'], 'messages')
        self.assertEqual(rows[0]['architecture']['input_modalities'], ['text'])
        await client.load(PREFIX + 'glm-5.3')
        self.keychain.get.assert_called_once()

    async def test_catalog_exposes_provider_context_to_admission_and_meter(self):
        client = self.client(lambda request: httpx.Response(200, json={'data': [
            {'id': 'glm-5.3', 'limit': {'context': 1000000, 'input': 900000, 'output': 131072}},
            {'id': 'glm-unknown'},
        ]}))
        rows = await client.models()
        from blackrose.prompts import context_budget
        from blackrose.telemetry import GenerationMetrics
        self.assertEqual(rows[0]['context_length'], 1000000)
        self.assertEqual(context_budget(rows[0])[0], 1000000)
        self.assertEqual(GenerationMetrics(rows[0], [], [], None).limit, 1000000)
        self.assertEqual(context_budget(rows[1])[0], 128000)

    def counting_client(self, handler, keychain):
        client = OpenCodeGoClient(keychain=keychain, transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return client

    async def test_turn_reuses_one_lookup_for_load_validation_and_streams(self):
        class CountingKeychain:
            def __init__(self):
                self.calls = 0
                self.key = SECRET
                self.error = None

            def get(self):
                self.calls += 1
                if self.error is not None:
                    raise self.error
                return self.key

        authorizations = []
        def handler(request):
            if request.method == 'GET':
                return httpx.Response(200, json={'data': [{'id': 'glm-5.3'}]})
            authorizations.append(request.headers['authorization'])
            return httpx.Response(200, text=events({'choices': [{'delta': {'content': 'Done'},
                                                                 'finish_reason': 'stop'}]}))
        keychain = CountingKeychain()
        client = self.counting_client(handler, keychain)
        async with client.turn_credentials():
            await client.load(PREFIX + 'glm-5.3')
            for _ in range(2):
                _ = [c async for c in client.stream(PREFIX + 'glm-5.3', [],
                                                    session_id='a')]
        self.assertEqual(keychain.calls, 1)
        self.assertEqual(authorizations, ['Bearer ' + SECRET] * 2)
        # A later turn looks the key up again and sees an out-of-process change.
        keychain.key = 'rotated-secret'
        async with client.turn_credentials():
            await client.load(PREFIX + 'glm-5.3')
            _ = [c async for c in client.stream(PREFIX + 'glm-5.3', [], session_id='a')]
        self.assertEqual(keychain.calls, 2)
        self.assertEqual(authorizations[-1], 'Bearer rotated-secret')

    async def test_failed_turn_lookup_is_not_cached_and_is_retried(self):
        class FailingKeychain:
            def __init__(self):
                self.calls = 0
                self.error = CredentialError('OpenCode Go key is missing')
                self.key = SECRET

            def get(self):
                self.calls += 1
                if self.error is not None:
                    raise self.error
                return self.key

        keychain = FailingKeychain()
        client = self.counting_client(lambda request: httpx.Response(
            200, json={'data': [{'id': 'glm-5.3'}]}), keychain)
        for expected in (1, 2):
            with self.assertRaisesRegex(CredentialError, 'missing'):
                async with client.turn_credentials():
                    await client.load(PREFIX + 'glm-5.3')
            self.assertEqual(keychain.calls, expected)
        keychain.error = None
        async with client.turn_credentials():
            await client.load(PREFIX + 'glm-5.3')
        self.assertEqual(keychain.calls, 3)

    async def test_cancelled_turn_releases_the_credential_snapshot(self):
        class CountingKeychain:
            def __init__(self):
                self.calls = 0

            def get(self):
                self.calls += 1
                return SECRET

        keychain = CountingKeychain()
        client = self.counting_client(lambda request: httpx.Response(
            200, text=events({'choices': [{'delta': {'content': 'Done'},
                                          'finish_reason': 'stop'}]})), keychain)
        with self.assertRaises(asyncio.CancelledError):
            async with client.turn_credentials():
                raise asyncio.CancelledError
        _ = [c async for c in client.stream(PREFIX + 'glm-5.3', [], session_id='a')]
        self.assertEqual(keychain.calls, 2)

    async def test_local_turns_do_not_resolve_the_go_credential(self):
        keychain = Mock(get=Mock(return_value=SECRET))
        go = OpenCodeGoClient(keychain=keychain)
        self.clients.append(go)
        backend = ChatProviders(SimpleNamespace(timeout=10), go)
        async with backend.turn_credentials('local-model'):
            pass
        keychain.get.assert_not_called()

    async def test_chat_requests_use_key_session_agent_and_replay_reasoning(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers['authorization'], 'Bearer ' + SECRET)
            self.assertEqual(request.headers['x-opencode-session'], 'conversation-a')
            self.assertEqual(request.headers['user-agent'], 'blackrose-bloom/0.2.0')
            self.assertEqual(str(request.url), ENDPOINT + '/chat/completions')
            body = json.loads(request.content)
            self.assertEqual(body['model'], 'glm-5.3')
            self.assertIn('Current request time', body['messages'][0]['content'])
            self.assertEqual(body['messages'][2]['reasoning_content'], 'Retained reasoning')
            self.assertNotIn('_provider_content', body['messages'][2])
            return httpx.Response(200, text=events({'choices': [{'delta': {'content': 'Done'},
                                                                 'finish_reason': 'stop'}]}, '[DONE]'))
        client = self.client(handler)
        choices = [c async for c in client.stream(PREFIX + 'glm-5.3', [
            {'role': 'user', 'content': 'Question'},
            {'role': 'assistant', 'content': None, 'reasoning_content': 'Retained reasoning',
             '_provider_content': {'protocol': 'chat'}}], session_id='conversation-a')]
        self.assertEqual(choices[0]['delta']['content'], 'Done')
        self.assertEqual(len(requests), 1)

    async def test_http_errors_and_redirects_do_not_echo_secrets_or_retry(self):
        for status in (302, 401, 403, 429, 500):
            with self.subTest(status=status):
                handler = Mock(return_value=httpx.Response(status, text=SECRET,
                               headers={'location': 'https://elsewhere.invalid/' + SECRET}))
                client = self.client(handler)
                with self.assertRaises(RuntimeError) as caught:
                    _ = [c async for c in client.stream(PREFIX + 'glm-5.3', [], session_id='a')]
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertIn(str(status), str(caught.exception))
                self.assertEqual(handler.call_count, 1)

    async def test_native_request_paths_auth_tools_and_completion(self):
        tool = {'type': 'function', 'function': {'name': 'search', 'description': 'Find evidence',
                'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}},
                               'additionalProperties': False}}}
        cases = [
            ('minimax-m3', '/messages', [
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}},
                {'type': 'message_stop'}]),
            ('gpt-6-luna', '/responses', [{'type': 'response.completed', 'response': {}}]),
            ('gemini-4-argon-contributor', '/models/gemini-4-argon-contributor:streamGenerateContent?alt=sse',
             [{'candidates': [{'content': {'parts': [{'text': 'Done'}]}, 'finishReason': 'STOP'}]}])]
        for model, path, packets in cases:
            with self.subTest(model=model):
                def handler(request, *, path=path, packets=packets):
                    self.assertEqual(str(request.url), ENDPOINT + path)
                    self.assertEqual(request.headers['authorization'], 'Bearer ' + SECRET)
                    self.assertEqual(request.headers['x-opencode-session'], 'stable')
                    body = json.loads(request.content)
                    if path == '/messages':
                        self.assertEqual(request.headers['anthropic-version'], '2023-06-01')
                        self.assertEqual(body['tools'][0]['input_schema'], tool['function']['parameters'])
                    elif path == '/responses':
                        self.assertEqual(body['tools'][0]['name'], 'search')
                        self.assertFalse(body['tools'][0]['strict'])
                    else:
                        self.assertEqual(body['tools'][0]['functionDeclarations'][0]['parametersJsonSchema'],
                                         tool['function']['parameters'])
                    return httpx.Response(200, text=events(*packets))
                client = self.client(handler)
                choices = [c async for c in client.stream(PREFIX + model,
                    [{'role': 'user', 'content': 'Question'}], [tool], session_id='stable')]
                self.assertEqual(choices[-1]['finish_reason'], 'stop')

    async def test_truncated_and_error_streams_fail_safely(self):
        for stream in [events({'choices': [{'delta': {'content': 'Partial'}}]}),
                       events({'error': {'message': SECRET}}), 'data: broken-' + SECRET + '\n\n']:
            client = self.client(lambda request, stream=stream: httpx.Response(200, text=stream))
            with self.assertRaises(RuntimeError) as caught:
                _ = [c async for c in client.stream(PREFIX + 'glm-5.3', [], session_id='a')]
            self.assertNotIn(SECRET, str(caught.exception))

    async def test_anthropic_tool_stream_preserves_thinking_signature_and_parallel_results(self):
        packets = [
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'Think'}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'signed'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'tool-a', 'name': 'search', 'input': {}}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"q":'}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '"test"}'}},
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}}, {'type': 'message_stop'}]
        client = self.client(lambda request: httpx.Response(200, text=events(*packets)))
        choices = [c async for c in client.stream(PREFIX + 'minimax-m3', [], session_id='a')]
        self.assertEqual(choices[0]['delta']['reasoning_content'], 'Think')
        native = choices[-1]['_provider_content']
        self.assertEqual(native['content'][0]['signature'], 'signed')
        self.assertEqual(native['content'][1]['input'], {'q': 'test'})
        path, body = prepare('messages', 'minimax-m3', [
            {'role': 'user', 'content': 'Question'},
            {'role': 'assistant', '_provider_content': native},
            {'role': 'tool', 'tool_call_id': 'tool-a', 'content': 'Evidence'},
            {'role': 'tool', 'tool_call_id': 'tool-b', 'content': 'More evidence'}], [])
        self.assertEqual(path, '/messages')
        self.assertEqual(body['messages'][1]['content'][0]['signature'], 'signed')
        self.assertEqual(len(body['messages'][2]['content']), 2)
        self.assertGreater(body['max_tokens'], 0)

    async def test_responses_stream_preserves_encrypted_reasoning_and_call_ids(self):
        reasoning = {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'opaque', 'summary': []}
        call = {'type': 'function_call', 'id': 'f1', 'call_id': 'call-a', 'name': 'search', 'arguments': '{"q":"test"}'}
        packets = [{'type': 'response.output_item.done', 'item': reasoning},
                   {'type': 'response.output_item.added', 'output_index': 1, 'item': {**call, 'arguments': ''}},
                   {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': call['arguments']},
                   {'type': 'response.output_item.done', 'item': call},
                   {'type': 'response.completed', 'response': {'incomplete_details': None}}]
        client = self.client(lambda request: httpx.Response(200, text=events(*packets)))
        choices = [c async for c in client.stream(PREFIX + 'gpt-6-luna', [], session_id='a')]
        self.assertEqual(choices[0]['delta']['tool_calls'][0]['id'], 'call-a')
        self.assertEqual(choices[-1]['finish_reason'], 'tool_calls')
        path, body = prepare('responses', 'gpt-6-luna', [
            {'role': 'assistant', '_provider_content': choices[-1]['_provider_content']},
            {'role': 'tool', 'tool_call_id': 'call-a', 'content': 'Evidence'}], [])
        self.assertEqual(path, '/responses')
        self.assertFalse(body['store'])
        self.assertEqual(body['input'][0], reasoning)
        self.assertEqual(body['input'][-1]['call_id'], 'call-a')

    async def test_google_stream_preserves_thought_signature(self):
        part = {'functionCall': {'name': 'search', 'args': {'q': 'test'}, 'id': 'native-a'}, 'thoughtSignature': 'signed'}
        client = self.client(lambda request: httpx.Response(200, text=events(
            {'candidates': [{'content': {'parts': [part]}, 'finishReason': 'STOP'}]})))
        choices = [c async for c in client.stream(PREFIX + 'gemini-4-argon-contributor', [], session_id='a')]
        call = choices[0]['delta']['tool_calls'][0]
        path, body = prepare('google', 'gemini-4-argon-contributor', [
            {'role': 'assistant', '_provider_content': choices[-1]['_provider_content'],
             'tool_calls': [{'id': call['id'], 'function': call['function']}]},
            {'role': 'tool', 'tool_call_id': call['id'], 'content': 'Evidence'}], [])
        self.assertTrue(path.endswith(':streamGenerateContent?alt=sse'))
        self.assertEqual(body['contents'][0]['parts'][0], part)
        self.assertEqual(body['contents'][1]['parts'][0]['functionResponse']['name'], 'search')
        self.assertEqual(body['contents'][1]['parts'][0]['functionResponse']['id'], 'native-a')

    async def test_provider_isolation_and_failed_selection_keeps_active_provider(self):
        local = SimpleNamespace(timeout=10, models=AsyncMock(return_value=[{'id': 'local-model'}]),
            load=AsyncMock(), stream=Mock(), aclose=AsyncMock())
        go = SimpleNamespace(models=AsyncMock(return_value=[{'id': PREFIX + 'glm-5.3'}]),
            load=AsyncMock(), aclose=AsyncMock())
        backend = ChatProviders(local, go)
        await backend.models()
        go.models.assert_not_awaited()
        await backend.models('opencode-go')
        await backend.load(PREFIX + 'glm-5.3')
        local.load.assert_not_awaited()
        local.load.side_effect = RuntimeError('Local router offline')
        with self.assertRaises(RuntimeError):
            await backend.load('local-model')
        # A failed load changes nothing: each call still derives the provider from its model.
        conversation = SimpleNamespace(id='chat-1', created_at=datetime(2026, 11, 5, tzinfo=UTC))
        self.assertEqual(backend.request_options(conversation, 'local-model'), {})
        self.assertIn('session_id', backend.request_options(conversation, PREFIX + 'glm-5.3'))
        await backend.aclose()
        go.aclose.assert_awaited_once()

    def test_local_and_cloud_turns_derive_continuation_from_their_own_model(self):
        local = SimpleNamespace(timeout=10, aclose=AsyncMock())
        go = SimpleNamespace(aclose=AsyncMock())
        backend = ChatProviders(local, go)
        conversation = SimpleNamespace(id='chat-2', created_at=datetime(2026, 11, 5, tzinfo=UTC))
        cloud_model = PREFIX + 'glm-5.3'
        # Interleave: a cloud reply's wire fields must not leak into a local turn, and vice versa.
        cloud_options = backend.request_options(conversation, cloud_model)
        self.assertTrue(cloud_options['session_id'])
        self.assertEqual(backend.request_options(conversation, 'local-model'), {})
        local_wire = backend.continuation({'role': 'assistant'}, 'thinking', {'p': 1}, 'local-model')
        self.assertEqual(local_wire, {'role': 'assistant'})
        cloud_wire = backend.continuation({'role': 'assistant'}, 'thinking', {'p': 1}, cloud_model)
        self.assertEqual(cloud_wire, {'role': 'assistant', 'reasoning_content': 'thinking',
                                      '_provider_content': {'p': 1}})


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config, self.runtime = make_runtime(self.root)
        self.local = self.runtime.controller.backend
        self.go = SimpleNamespace(models=AsyncMock(return_value=[{'id': PREFIX + 'glm-5.3'}]),
                                  load=AsyncMock(), aclose=AsyncMock())
        self.runtime.controller.backend = ChatProviders(self.local, self.go)
        await self.runtime.open()
        self.session = Session(self.runtime.controller, lambda *args: None)
        await self.session.boot()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.temp.cleanup()

    async def test_browsing_go_does_not_select_it_or_drop_local_capabilities(self):
        models = await self.session.models('opencode-go')
        self.assertEqual(models['items'][0]['id'], PREFIX + 'glm-5.3')
        self.go.load.assert_not_awaited()
        self.assertEqual(self.runtime.controller.model_name, 'test-vision')
        self.assertTrue(self.runtime.controller._supports_vision())
        local = await self.session.models('local')
        self.assertEqual([m['id'] for m in local['items']], ['test-vision'])

    async def test_go_unavailable_does_not_block_local_picker(self):
        self.go.models.side_effect = RuntimeError('Go is offline')
        self.assertIn('error', await self.session.models('opencode-go'))
        self.assertEqual(len((await self.session.models('local'))['items']), 1)

    async def test_go_startup_does_not_touch_local_router(self):
        c = self.runtime.controller
        c.model_name = PREFIX + 'glm-5.3'
        with patch.object(self.local, 'load', new_callable=AsyncMock) as load, \
             patch.object(self.local, 'models', new_callable=AsyncMock) as models:
            await c.start()
        load.assert_not_awaited()
        models.assert_not_awaited()
        self.go.load.assert_awaited_once_with(PREFIX + 'glm-5.3')

    async def test_go_default_persists_and_controller_replays_tool_reasoning_and_session(self):
        requests = []
        naming_requests = []
        catalog_requests = []
        def handler(request):
            if request.method == 'GET':
                catalog_requests.append(request.url.path)
                return httpx.Response(200, json={'data': [{'id': 'glm-5.3'}]})
            body = json.loads(request.content)
            from blackrose.titles import TITLE_INSTRUCTIONS
            if body['messages'][0]['content'].startswith(TITLE_INSTRUCTIONS):
                naming_requests.append((request.headers['x-opencode-session'], body))
                return httpx.Response(200, text=events({'choices': [{'delta': {
                    'content': 'Checking the current time'}, 'finish_reason': 'stop'}]}))
            requests.append((request.headers['x-opencode-session'], body))
            if len(requests) == 1:
                return httpx.Response(200, text=events({'choices': [{'delta': {
                    'reasoning_content': 'Need evidence', 'tool_calls': [{'index': 0, 'id': 'a',
                        'function': {'name': 'knowledge_list', 'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}]}))
            return httpx.Response(200, text=events({'choices': [{'delta': {'content': 'Done'}, 'finish_reason': 'stop'}]}))
        go = OpenCodeGoClient(keychain=Mock(get=Mock(return_value=SECRET)), transport=httpx.MockTransport(handler))
        self.runtime.controller.backend.go = go
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')}):
            self.assertEqual(await self.session.select_model(PREFIX + 'glm-5.3', True), {'ok': True})
            from blackrose.config import load_config
            self.assertEqual(load_config(self.root / 'missing.toml').default_model, PREFIX + 'glm-5.3')
        # Discovery and the explicit selection refresh are retained.
        self.assertEqual(len(catalog_requests), 2)
        c = self.runtime.controller
        c.tool_definitions = Mock(return_value=[{'type': 'function', 'function': {
            'name': 'knowledge_list', 'parameters': {'type': 'object', 'properties': {}}}}])
        c._call_tool = AsyncMock(return_value={'time': 'now'})
        await self.session.submit('Question')
        await self.session._generation
        await c._title_task
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(naming_requests), 1)
        self.assertEqual(naming_requests[0][0], requests[0][0])
        self.assertNotIn('tools', naming_requests[0][1])
        self.assertEqual(c.conversation.title, 'Checking the current time')
        c._call_tool.assert_awaited_once_with('knowledge_list', {})
        self.assertEqual(requests[0][0], requests[1][0])
        self.assertEqual(requests[1][1]['messages'][-2]['reasoning_content'], 'Need evidence')
        self.assertEqual(json.loads(requests[1][1]['messages'][-1]['content']), {'time': 'now'})
        await self.session.submit('Follow up')
        await self.session._generation
        self.assertEqual(requests[-1][0], requests[0][0])
        await c.new_conversation()
        await self.session.submit('New question')
        await self.session._generation
        self.assertNotEqual(requests[-1][0], requests[0][0])
        # Three turns, including a tool round, reuse the selected Go catalog.
        self.assertEqual(len(catalog_requests), 2)
        # Selection validates once; each turn then resolves one key for its own
        # load validation, tool rounds and title request.
        self.assertEqual(go.keychain.get.call_count, 4)
        self.assertNotIn(SECRET, (self.root / 'config/blackrose-bloom/preferences.json').read_text())


if __name__ == '__main__':
    unittest.main()
