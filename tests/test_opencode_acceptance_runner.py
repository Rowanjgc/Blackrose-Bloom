import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from blackrose.backend.opencode import ENDPOINT, OpenCodeGoClient
from blackrose.backend.opencode_protocols import prepare
from scripts.accept_opencode_go import (
    CASES, MAX_BODY_BYTES, MAX_OUTPUT_TOKENS, MAX_POSTS, RequestCap,
    credential_failure_checks, estimate_worst_case_usd, plan, redact,
    _stream, live, observed_decoder, REMAINING_CASES, run_reasoning_continuation, safe_preview,
)


class FakeHTTP:
    def __init__(self):
        self.requests = []

    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return object()

    async def get(self, url, **kwargs):
        self.requests.append(('GET', url, kwargs))
        return object()

    async def aclose(self):
        pass


class AcceptancePlanTests(unittest.TestCase):
    def test_plan_has_fixed_models_and_conservative_finite_budget(self):
        value = plan()
        self.assertEqual([case['model'] for case in value['cases']], [
            'opencode-go/glm-5.3-flash', 'opencode-go/gpt-6-luna', 'opencode-go/gemini-4-argon'])
        self.assertEqual(value['bounds']['max_inference_posts'], 5)
        self.assertEqual(value['bounds']['max_output_tokens_each'], 256)
        self.assertLessEqual(estimate_worst_case_usd(), 0.75)
        self.assertEqual(value['bounds']['usage_equivalent_estimate_limit_usd'], 0.75)
        self.assertIn('differ', value['endpoint_review']['note'])

    def test_request_cap_uses_actual_prepare_routes_and_payloads(self):
        fake = FakeHTTP()
        bounded = RequestCap(fake)
        for case in CASES:
            model = case['model'].removeprefix('opencode-go/')
            content = 'synthetic input'
            if case['protocol'] == 'google':
                content = [{'type': 'text', 'text': 'synthetic image question'},
                           {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]
            path, body = prepare(case['protocol'], model,
                [{'role': 'user', 'content': content}], [], effort='default')
            url = ENDPOINT + path
            for _ in range(case['max_posts']):
                original_body = json.loads(json.dumps(body))
                bounded.stream('POST', url, json=body)
                sent = fake.requests[-1][2]['json']
                self.assertEqual(fake.requests[-1][1], url)
                if case['protocol'] == 'google':
                    self.assertNotIn('model', original_body)
                    self.assertNotIn('model', sent)
                    self.assertEqual(sent['generationConfig']['maxOutputTokens'], MAX_OUTPUT_TOKENS)
                    self.assertIn('inlineData', sent['contents'][0]['parts'][1])
                else:
                    self.assertEqual(original_body['model'], model)
                    field = 'max_output_tokens' if case['protocol'] == 'responses' else 'max_tokens'
                    self.assertEqual(sent[field], MAX_OUTPUT_TOKENS)
                self.assertLessEqual(len(json.dumps(sent).encode()), MAX_BODY_BYTES)
        self.assertEqual(bounded.post_count, sum(case['max_posts'] for case in CASES))

    def test_request_cap_rejects_model_route_mismatch(self):
        fake = FakeHTTP()
        bounded = RequestCap(fake)
        path, body = prepare('chat', 'gpt-6-luna', [{'role': 'user', 'content': 'x'}], [])
        with self.assertRaisesRegex(RuntimeError, 'model/route'):
            bounded.stream('POST', ENDPOINT + path, json=body)
        self.assertFalse(fake.requests)

    def test_request_cap_allows_only_one_public_catalog_get(self):
        import asyncio
        fake = FakeHTTP()
        bounded = RequestCap(fake)
        asyncio.run(bounded.get(ENDPOINT + '/models'))
        with self.assertRaisesRegex(RuntimeError, 'unexpected HTTP'):
            asyncio.run(bounded.get(ENDPOINT + '/models'))
        self.assertEqual(len(fake.requests), 1)

    def test_request_cap_rejects_unexpected_url_oversize_and_sixth_post(self):
        fake = FakeHTTP()
        bounded = RequestCap(fake)
        with self.assertRaisesRegex(RuntimeError, 'model/route'):
            bounded.stream('POST', 'https://elsewhere.invalid/', json={'model': 'glm-5.3-flash'})
        fake = FakeHTTP()
        bounded = RequestCap(fake)
        path, _ = prepare('chat', 'glm-5.3-flash', [], [])
        with self.assertRaisesRegex(RuntimeError, '16 KiB'):
            bounded.stream('POST', ENDPOINT + path,
                           json={'model': 'glm-5.3-flash', 'messages': ['x' * MAX_BODY_BYTES]})
        fake = FakeHTTP()
        bounded = RequestCap(fake)
        for case in CASES:
            path, body = prepare(case['protocol'], case['model'].removeprefix('opencode-go/'), [], [])
            for _ in range(case['max_posts']):
                bounded.stream('POST', ENDPOINT + path, json=body)
        with self.assertRaisesRegex(RuntimeError, 'ceiling'):
            bounded.stream('POST', ENDPOINT + path, json=body)
        self.assertEqual(len(fake.requests), MAX_POSTS)


class CredentialControlTests(unittest.IsolatedAsyncioTestCase):
    def test_redacts_before_truncation_at_each_report_boundary(self):
        sentinel = 'synthetic-secret-which-must-not-be-partially-retained'
        for limit in (80, 120, 160, 200, 240, 300):
            with self.subTest(limit=limit):
                value = 'x' * (limit - 20) + sentinel
                preview = safe_preview(value, sentinel, limit)
                self.assertNotIn(sentinel[:20], preview)
                self.assertIn('[REDACTED]', preview)
                self.assertLessEqual(len(preview), limit)

    async def test_missing_locked_and_revoked_credentials_are_mock_only_and_bounded(self):
        result = await credential_failure_checks()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['missing'], 'zero requests')
        self.assertEqual(result['locked'], 'zero requests')
        self.assertIn('no retry', result['revoked'])
        self.assertIn('no secret echo', result['sse_error'])
        self.assertIn('removed before reporting', result['content_redaction'])

    async def test_recursive_redaction_removes_sentinel_from_nested_content_and_errors(self):
        sentinel = 'synthetic-key-marker'
        value = {'error': f'provider error included {sentinel}',
                 'case': {'answer': [f'model said {sentinel}', 2]}, sentinel: 'sensitive key name'}
        cleaned = redact(value, sentinel)
        self.assertNotIn(sentinel, json.dumps(cleaned))

    async def test_protocol_assertion_keeps_bounded_sanitized_partial_stream_and_usage(self):
        sentinel = 'synthetic-provider-key-sentinel'

        class FakeResponsesClient:
            async def stream(self, *args, **kwargs):
                yield {
                    'delta': {'content': f'partial {sentinel}', 'tool_calls': [{
                        'index': 0, 'id': 'call-1', 'function': {
                            'name': 'read_fixture', 'arguments': '{"key":"reasoning"}'}}]},
                    '_usage': {'input_tokens': 17, 'output_tokens': 4},
                    '_provider_content': {'protocol': 'responses', 'content': []},
                }
                yield {'finish_reason': 'tool_calls'}

        partial = []
        with self.assertRaisesRegex(AssertionError, 'native reasoning item'):
            await run_reasoning_continuation(
                FakeResponsesClient(), 'opencode-go/gpt-6-luna', 'synthetic-session', [],
                partial_streams=partial, secret=sentinel)

        self.assertEqual(len(partial), 1)
        record = partial[0]
        self.assertTrue(record['stream_complete'])
        self.assertEqual(record['finish_reason'], 'tool_calls')
        self.assertEqual(record['tool_calls'], [{'id': 'call-1', 'name': 'read_fixture'}])
        self.assertEqual(record['usage'], [{'input_tokens': 17, 'output_tokens': 4}])
        self.assertIn('[REDACTED]', record['content_preview'])
        self.assertNotIn(sentinel, json.dumps(record))

    async def test_interrupted_stream_redacts_secrets_crossing_preview_boundaries(self):
        sentinel = 'synthetic-boundary-credential-must-be-removed'

        class InterruptedClient:
            async def stream(self, *args, **kwargs):
                yield {'delta': {
                    'content': 'x' * 220 + sentinel,
                    'reasoning_content': 'x' * 220 + sentinel,
                    'tool_calls': [{'index': 0, 'id': 'x' * 140 + sentinel,
                                    'function': {'name': 'x' * 100 + sentinel}}]},
                    '_usage': {'input_tokens': 17}}
                raise RuntimeError('connection interrupted')

        partial = []
        with self.assertRaisesRegex(RuntimeError, 'connection interrupted'):
            await _stream(InterruptedClient(), CASES[0]['model'], [], (), 'synthetic',
                          partial_streams=partial, secret=sentinel)
        self.assertFalse(partial[0]['stream_complete'])
        self.assertIsNone(partial[0]['finish_reason'])
        self.assertEqual(partial[0]['usage'], [{'input_tokens': 17}])
        self.assertNotIn(sentinel[:20], json.dumps(partial))
        self.assertEqual(json.dumps(partial).count('[REDACTED]'), 4)


class MatrixReplayTests(unittest.IsolatedAsyncioTestCase):
    """Synthetic SSE contracts, not captured provider responses or live evidence."""

    async def replay(self, *, reasoning=True, arguments='{"key":"reasoning"}', status=200):
        seen = []
        sentinel = 'synthetic-matrix-credential'
        native = {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'opaque', 'summary': []}

        def events(*packets):
            return ''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets)

        def handler(request):
            if request.method == 'GET':
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(200, json={'data': [
                    {'id': case['model'].removeprefix('opencode-go/')} for case in CASES]})
            body = json.loads(request.content)
            seen.append(body)
            self.assertEqual(request.headers['authorization'], 'Bearer ' + sentinel)
            if len(seen) == 1:
                return httpx.Response(200, text=events({'choices': [{'delta': {'tool_calls': [{
                    'index': 0, 'id': 'chat-1', 'function': {
                        'name': 'read_fixture', 'arguments': '{"key":"sample"}'}}]},
                    'finish_reason': 'tool_calls'}]}))
            if len(seen) == 2:
                self.assertEqual(body['messages'][-1]['tool_call_id'], 'chat-1')
                return httpx.Response(200, text=events({'choices': [{
                    'delta': {'content': '42'}, 'finish_reason': 'stop'}]}))
            if len(seen) == 3:
                if status != 200:
                    return httpx.Response(status, text=sentinel, headers={'Retry-After': '0'})
                call = {'type': 'function_call', 'id': 'f1', 'call_id': 'responses-1',
                        'name': 'read_fixture', 'arguments': arguments}
                packets = ([{'type': 'response.output_item.done', 'item': native}] if reasoning else [])
                packets += [
                    {'type': 'response.output_item.added', 'output_index': 1, 'item': call},
                    {'type': 'response.function_call_arguments.delta', 'output_index': 1,
                     'delta': arguments},
                    {'type': 'response.output_item.done', 'item': call},
                    {'type': 'response.completed', 'response': {
                        'usage': {'input_tokens': 17, 'output_tokens': 4}}}]
                return httpx.Response(200, text=events(*packets))
            if len(seen) == 4:
                self.assertEqual(body['reasoning']['effort'], 'low')
                self.assertEqual(body['input'][-3], native)
                self.assertEqual(body['input'][-2]['call_id'], 'responses-1')
                self.assertEqual(body['input'][-1]['call_id'], 'responses-1')
                return httpx.Response(200, text=events(
                    {'type': 'response.output_text.delta', 'delta': '31'},
                    {'type': 'response.completed', 'response': {}}))
            self.assertEqual(len(seen), 5)
            self.assertIn('inlineData', body['contents'][0]['parts'][1])
            return httpx.Response(200, text=events({'candidates': [{
                'content': {'parts': [{'text': 'red'}]}, 'finishReason': 'STOP'}]}))

        client = OpenCodeGoClient(transport=httpx.MockTransport(handler))
        with patch('scripts.accept_opencode_go.OpenCodeGoClient', return_value=client), \
                patch('scripts.accept_opencode_go.OpenCodeGoKeychain',
                      return_value=SimpleNamespace(get=Mock(return_value=sentinel))), \
                patch('scripts.accept_opencode_go.credential_failure_checks',
                      new=AsyncMock(return_value={'status': 'passed'})):
            result = await live(SimpleNamespace(confirm_live=True, max_estimate_usd=0.75))
        self.assertNotIn(sentinel, json.dumps(result))
        return result, seen

    async def test_complete_synthetic_matrix_replays_reasoning_and_tool_pairing(self):
        result, seen = await self.replay()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(case['status'] == 'passed' for case in result['cases'].values()))
        self.assertEqual(result['partial_streams'][2]['native_item_types'], ['reasoning', 'function_call'])

    async def test_absent_reasoning_retains_usage_and_marks_vision_skipped(self):
        result, seen = await self.replay(reasoning=False)
        self.assertEqual(len(seen), 3)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['cases']['chat_tool_round']['status'], 'passed')
        self.assertEqual(result['cases']['responses_reasoning_continuation']['status'], 'failed')
        self.assertEqual(result['cases']['google_tiny_vision']['status'], 'skipped')
        self.assertIn('native reasoning item', result['error'])
        self.assertEqual(result['partial_streams'][-1]['native_item_types'], ['function_call'])
        self.assertEqual(result['partial_streams'][-1]['usage'], [
            {'prompt_tokens': 17, 'completion_tokens': 4}])

    async def test_wrong_tool_arguments_do_not_send_a_continuation(self):
        result, seen = await self.replay(arguments='{"key":"wrong"}')
        self.assertEqual(len(seen), 3)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('tool arguments', result['error'])

    async def test_auth_and_rate_limit_errors_stop_without_retry(self):
        for status in (401, 429):
            with self.subTest(status=status):
                result, seen = await self.replay(status=status)
                self.assertEqual(len(seen), 3)
                self.assertEqual(result['status'], 'failed')
                self.assertIn(str(status), result['error'])
                self.assertEqual(result['cases']['google_tiny_vision']['status'], 'skipped')


class RemainingMatrixTests(unittest.IsolatedAsyncioTestCase):
    def test_observed_wire_event_shapes_replay_without_inventing_reasoning(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' /
                              'opencode_go_responses_20261030.json').read_text())
        call = {'type': 'function_call', 'id': 'synthetic-item', 'call_id': 'synthetic-call',
                'name': 'read_fixture', 'arguments': '{"key":"reasoning"}'}
        message = {'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': '31'}]}
        for index, observed in enumerate(fixture['wire_structure']):
            traces = []
            decoder = observed_decoder(traces)('responses')
            choices = []
            argument_sent = False
            for event in observed['events']:
                kind = event['event']
                packet = {'type': kind, 'output_index': 0}
                if kind.startswith('response.output_item.'):
                    packet['item'] = (dict(call) if event['item_types'] == ['function_call']
                                      else message)
                    if kind.endswith('.added') and packet['item']['type'] == 'function_call':
                        packet['item']['arguments'] = ''
                if kind == 'response.function_call_arguments.delta':
                    packet['delta'] = '' if argument_sent else call['arguments']
                    argument_sent = True
                if kind == 'response.output_text.delta':
                    packet['delta'] = '31'
                if kind == 'response.completed':
                    usage = fixture['normalized_usage'][index]
                    packet['response'] = {'status': 'completed', 'output': [call if index == 0 else message],
                                          'usage': {'input_tokens': usage['prompt_tokens'],
                                                    'output_tokens': usage['completion_tokens']}}
                choices.extend(decoder.feed(json.dumps(packet)))
            self.assertEqual(traces[0], observed)
            self.assertEqual(choices[-1]['finish_reason'], 'tool_calls' if index == 0 else 'stop')
            self.assertEqual(choices[-1]['_usage'], fixture['normalized_usage'][index])
            self.assertEqual(choices[-1]['_provider_content']['content'], [call if index == 0 else message])

    async def replay(self, *, reasoning=False):
        seen = []
        opaque = 'synthetic-private-state-not-for-report'
        reason = {'type': 'reasoning', 'id': 'r1', 'encrypted_content': opaque, 'summary': []}
        call = {'type': 'function_call', 'id': 'f1', 'call_id': 'r-call',
                'name': 'read_fixture', 'arguments': '{"key":"reasoning"}'}
        thinking = {'type': 'thinking', 'thinking': opaque, 'signature': opaque}

        def events(*packets):
            return ''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets)

        def handler(request):
            if request.method == 'GET':
                return httpx.Response(200, json={'data': [
                    {'id': case['model'].removeprefix('opencode-go/')} for case in REMAINING_CASES]})
            body = json.loads(request.content)
            seen.append(body)
            if len(seen) == 1:
                packets = [{'type': 'response.output_item.done', 'item': reason}] if reasoning else []
                packets += [
                    {'type': 'response.output_item.added', 'output_index': 1, 'item': call},
                    {'type': 'response.function_call_arguments.delta', 'output_index': 1,
                     'delta': call['arguments']},
                    {'type': 'response.output_item.done', 'item': call},
                    {'type': 'response.completed', 'response': {}}]
                return httpx.Response(200, text=events(*packets))
            if len(seen) == 2:
                self.assertEqual(body['input'][-2], call)
                self.assertEqual(body['input'][-1]['call_id'], call['call_id'])
                self.assertEqual(body['reasoning']['effort'], 'low')
                if reasoning:
                    self.assertEqual(body['input'][-3], reason)
                return httpx.Response(200, text=events(
                    {'type': 'response.output_text.delta', 'delta': '31'},
                    {'type': 'response.completed', 'response': {}}))
            if len(seen) == 3:
                self.assertEqual(body['model'], 'minimax-m3')
                return httpx.Response(200, text=events(
                    {'type': 'content_block_start', 'index': 0, 'content_block': thinking},
                    {'type': 'content_block_stop', 'index': 0},
                    {'type': 'content_block_start', 'index': 1, 'content_block': {
                        'type': 'tool_use', 'id': 'm-call', 'name': 'read_fixture', 'input': {}}},
                    {'type': 'content_block_delta', 'index': 1, 'delta': {
                        'type': 'input_json_delta', 'partial_json': '{"key":"sample"}'}},
                    {'type': 'content_block_stop', 'index': 1},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}},
                    {'type': 'message_stop'}))
            if len(seen) == 4:
                self.assertEqual(body['messages'][-2]['content'][0], thinking)
                self.assertEqual(body['messages'][-1]['content'][0]['tool_use_id'], 'm-call')
                return httpx.Response(200, text=events(
                    {'type': 'content_block_start', 'index': 0,
                     'content_block': {'type': 'text', 'text': '42'}},
                    {'type': 'content_block_stop', 'index': 0},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}},
                    {'type': 'message_stop'}))
            self.assertEqual(len(seen), 5)
            self.assertIn('inlineData', body['contents'][0]['parts'][1])
            return httpx.Response(200, text=events({'candidates': [{
                'content': {'parts': [{'text': 'red'}]}, 'finishReason': 'STOP'}]}))

        client = OpenCodeGoClient(transport=httpx.MockTransport(handler))
        with patch('scripts.accept_opencode_go.OpenCodeGoClient', return_value=client), \
                patch('scripts.accept_opencode_go.OpenCodeGoKeychain',
                      return_value=SimpleNamespace(get=lambda: 'synthetic-only-key')), \
                patch('scripts.accept_opencode_go.credential_failure_checks',
                      new=AsyncMock(return_value={'status': 'passed'})):
            result = await live(SimpleNamespace(confirm_live=True, max_estimate_usd=0.75, remaining=True))
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(len(seen), 5)
        self.assertNotIn(opaque, json.dumps(result))
        self.assertEqual(result['wire_structure'][0]['reasoning_seen_on_wire'], reasoning)
        self.assertEqual(result['cases']['responses_state_continuation']['reasoning_items'], int(reasoning))

    async def test_remaining_matrix_replays_all_optional_reasoning_and_messages_signatures(self):
        await self.replay(reasoning=True)

    async def test_absent_wire_reasoning_still_validates_continuation_without_claiming_reasoning(self):
        await self.replay(reasoning=False)

    def test_diagnostics_detect_reasoning_lost_from_terminal_output_and_hide_arbitrary_fields(self):
        traces = []
        decoder = observed_decoder(traces)('responses')
        with self.assertRaisesRegex(AssertionError, 'lost by the decoder'):
            decoder.feed(json.dumps({'type': 'response.completed', 'response': {'output': [
                {'type': 'reasoning', 'encrypted_content': 'do-not-retain'}]}}))
        self.assertTrue(traces[0]['reasoning_seen_on_wire'])
        self.assertNotIn('do-not-retain', json.dumps(traces))

    def test_remaining_request_allowlist_and_budget(self):
        fake = FakeHTTP()
        cap = RequestCap(fake, cases=REMAINING_CASES)
        for case in REMAINING_CASES:
            path, body = prepare(case['protocol'], case['model'].removeprefix('opencode-go/'), [], [])
            for _ in range(case['max_posts']):
                cap.stream('POST', ENDPOINT + path, json=body)
        with self.assertRaisesRegex(RuntimeError, 'ceiling'):
            cap.stream('POST', ENDPOINT + path, json=body)
        self.assertEqual(len(fake.requests), 5)


if __name__ == '__main__':
    unittest.main()
