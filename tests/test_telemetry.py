"""Usage-only packets, live decode timing and session ownership."""
import json
import unittest

import httpx

from blackrose.backend.opencode_protocols import StreamDecoder
from blackrose.backend.router import RouterClient
from blackrose.telemetry import GenerationMetrics
from blackrose.errors import InputRejected
from blackrose.prompts import Prompt, PromptBuilder, estimated_tokens


class MetricsTests(unittest.TestCase):
    def test_estimates_become_exact_and_cached_prompt_counts_toward_context(self):
        events = []
        now = [0.0]
        metrics = GenerationMetrics({'meta': {'n_ctx': 8192}},
            [{'role': 'user', 'content': 'hello'}], [], events.append, clock=lambda: now[0])
        self.assertTrue(events[-1]['context_estimated'])
        budget = events[-1]['admission_input_bound'] + events[-1]['output_reserve']
        metrics.update({'delta': {'content': None, 'reasoning_content': 'thinking'}})
        now[0] = 1
        metrics.update({'delta': {'content': 'answer'}, '_timings': {
            'prompt_n': 100, 'cache_n': 200, 'predicted_n': 20, 'predicted_per_second': 19.5}})
        self.assertEqual(events[-1]['context_used'], 320)
        self.assertEqual(events[-1]['tokens_per_second'], 19.5)
        self.assertFalse(events[-1]['context_estimated'])
        self.assertFalse(events[-1]['speed_estimated'])
        self.assertEqual(events[-1]['admission_input_bound'] + events[-1]['output_reserve'], budget)
        metrics.update({'delta': {}, '_usage': {'prompt_tokens': 301, 'completion_tokens': 22}})
        metrics.publish(force=True)
        self.assertEqual(events[-1]['context_used'], 323)
        now[0] = 2
        metrics.update({'delta': {'content': ' final'}})
        self.assertEqual(events[-1]['tokens_per_second'], 19.5)

    def test_decode_speed_excludes_late_usage_packet_and_round_resets(self):
        events, now = [], [100.0]
        metrics = GenerationMetrics({}, [], [], events.append, clock=lambda: now[0])
        metrics.update({'delta': {'content': 'a' * 4}})
        now[0] = 101
        metrics.update({'delta': {'tool_calls': [{'function': {'arguments': 'b' * 36}}]}})
        self.assertEqual(events[-1]['tokens_per_second'], 10)
        now[0] = 110
        metrics.update({'_usage': {'prompt_tokens': 20, 'completion_tokens': 10}})
        self.assertEqual(events[-1]['tokens_per_second'], 10)
        next_round = GenerationMetrics({}, [], [], events.append, clock=lambda: now[0])
        self.assertIsNone(events[-1]['tokens_per_second'])
        self.assertIsNone(next_round.first_output)
        self.assertEqual(events[-1]['context_window'], 128000)

    def test_budget_meter_reaches_limit_at_admission_boundary(self):
        for limit in (32768, 128000):
            with self.subTest(limit=limit):
                model = {'meta': {'n_ctx': limit}}
                builder = PromptBuilder(None, None, None, model)
                messages = [{'role': 'user', 'content': ''}]
                events = []
                metrics = GenerationMetrics(model, messages, [], events.append)
                capacity = limit - metrics.output_reserve - estimated_tokens(messages, [])
                messages[0]['content'] = 'x' * capacity
                builder.admit(Prompt(messages=messages, instructions=""), [])
                GenerationMetrics(model, messages, [], events.append)
                self.assertEqual(events[-1]['admission_input_bound'] + events[-1]['output_reserve'], limit)
                # Estimated occupancy remains distinct from the safety bound.
                self.assertLess(events[-1]['context_used'], limit / 3)
                messages[0]['content'] += 'x'
                GenerationMetrics(model, messages, [], events.append)
                with self.assertRaisesRegex(InputRejected, 'context budget shown by the meter'):
                    builder.admit(Prompt(messages=messages, instructions=""), [])
                self.assertEqual(events[-1]['admission_input_bound'] + events[-1]['output_reserve'], limit + 1)

    def test_provider_usage_normalization(self):
        cases = [
            ('chat', [{'choices': [], 'usage': {'prompt_tokens': 42, 'completion_tokens': 8}}]),
            ('messages', [{'type': 'message_start', 'message': {'usage': {
                'input_tokens': 12, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 10}}},
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
                 'usage': {'output_tokens': 8}}, {'type': 'message_stop'}]),
            ('responses', [{'type': 'response.completed', 'response': {'usage': {
                'input_tokens': 42, 'output_tokens': 8}}}]),
            ('google', [{'usageMetadata': {'promptTokenCount': 42, 'candidatesTokenCount': 5,
                'thoughtsTokenCount': 3}, 'candidates': [{'finishReason': 'STOP'}]}]),
        ]
        for protocol, packets in cases:
            with self.subTest(protocol=protocol):
                decoder = StreamDecoder(protocol)
                choices = [choice for packet in packets for choice in decoder.feed(json.dumps(packet))]
                self.assertEqual(choices[-1]['_usage'], {'prompt_tokens': 42, 'completion_tokens': 8})


class RouterMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_only_packet_and_per_token_timings_survive_adapter(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertTrue(body['stream_options']['include_usage'])
            self.assertTrue(body['timings_per_token'])
            self.assertNotIn('temperature', body)
            packets = [
                {'choices': [{'delta': {'content': 'Answer'}, 'finish_reason': None}],
                 'timings': {'predicted_n': 1, 'predicted_per_second': 20}},
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 1}},
            ]
            return httpx.Response(200, text=''.join('data: ' + json.dumps(p) + '\n\n' for p in packets)
                                  + 'data: [DONE]\n\n')
        client = httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler))
        router = RouterClient('http://test', client=client)
        try:
            choices = [c async for c in router.stream('test', [])]
            self.assertEqual(choices[0]['_timings']['predicted_n'], 1)
            self.assertEqual(choices[-1]['_usage']['prompt_tokens'], 100)
            self.assertEqual(''.join((c.get('delta') or {}).get('content', '') for c in choices), 'Answer')
        finally:
            await router.aclose()
