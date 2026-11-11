"""SM-04: SSE line framing, the per-protocol stream decoders and the stream budget.

`docs/state-machines/sm-04-sse-stream-decoder.md` records the states, transitions
and invariants these tests pin. Everything here is offline: no network, no XDG
directories, no wall-clock sleeps.
"""
import asyncio
import json
import unittest

import httpx

from blackrose.backend.opencode_protocols import StreamDecoder
from blackrose.backend.router import RouterClient
from blackrose.backend.stream_limits import (
    StreamBudget, StreamBounds, StreamLimitExceeded, sse_lines,
)
from blackrose.turns import collect_tool_call


def decode(protocol, packets):
    """Feed JSON packets (or the literal '[DONE]') to a fresh decoder."""
    decoder = StreamDecoder(protocol)
    choices = []
    for packet in packets:
        choices.extend(decoder.feed(packet if isinstance(packet, str) else json.dumps(packet)))
    return decoder, choices


def block_start(index, block):
    return {'type': 'content_block_start', 'index': index, 'content_block': block}


def block_delta(index, **change):
    return {'type': 'content_block_delta', 'index': index, 'delta': change}


def block_stop(index):
    return {'type': 'content_block_stop', 'index': index}


def message_delta(reason=None, **usage):
    packet = {'type': 'message_delta', 'delta': {} if reason is None else {'stop_reason': reason}}
    if usage:
        packet['usage'] = usage
    return packet


MESSAGE_STOP = {'type': 'message_stop'}


class MessagesDecoderTests(unittest.TestCase):
    """The Anthropic branch is the only one that owns a per-block open/closed map."""

    def test_tool_use_arguments_accumulate_into_the_native_input(self):
        decoder, choices = decode('messages', [
            block_start(0, {'type': 'tool_use', 'id': 'call-a', 'name': 'search', 'input': {}}),
            block_delta(0, type='input_json_delta', partial_json='{"q":'),
            block_delta(0, type='input_json_delta', partial_json='"test"}'),
            block_stop(0),
            message_delta('tool_use'),
            MESSAGE_STOP,
        ])
        self.assertEqual(choices[0]['delta']['tool_calls'][0]['id'], 'call-a')
        self.assertEqual(''.join(fragment['function']['arguments'] for choice in choices
                                for fragment in (choice['delta'].get('tool_calls') or [])),
                         '{"q":"test"}')
        self.assertEqual(decoder.native, [
            {'type': 'tool_use', 'id': 'call-a', 'name': 'search', 'input': {'q': 'test'}}])
        self.assertEqual(choices[-1]['finish_reason'], 'tool_calls')
        self.assertEqual(decoder.blocks, {})
        self.assertEqual(decoder.arguments, {})

    def test_duplicate_block_start_at_an_open_index_is_rejected(self):
        # Before the guard, the retry reset arguments[0] and re-emitted the id/name
        # fragment, so the round silently called 'searchsearch' with concatenated
        # JSON arguments (see the SM-04 spec, hazard H1).
        decoder, _ = decode('messages', [
            block_start(0, {'type': 'tool_use', 'id': 'call-a', 'name': 'search', 'input': {}}),
            block_delta(0, type='input_json_delta', partial_json='{"q":'),
        ])
        with self.assertRaisesRegex(RuntimeError, 'started a content block twice'):
            decoder.feed(json.dumps(block_start(
                0, {'type': 'tool_use', 'id': 'call-a', 'name': 'search', 'input': {}})))
        self.assertEqual(decoder.arguments[0], '{"q":')
        self.assertEqual(decoder.native, [])

    def test_duplicate_block_stop_is_rejected(self):
        # Before the guard, the block was appended to native twice, so the next
        # request replayed a duplicate tool_use id (hazard H1).
        decoder, _ = decode('messages', [
            block_start(0, {'type': 'text', 'text': ''}),
            block_delta(0, type='text_delta', text='answer'),
            block_stop(0),
        ])
        with self.assertRaisesRegex(RuntimeError, 'stopped an unknown content block'):
            decoder.feed(json.dumps(block_stop(0)))
        self.assertEqual(decoder.native, [{'type': 'text', 'text': 'answer'}])

    def test_block_stop_for_an_unopened_index_is_rejected(self):
        decoder = StreamDecoder('messages')
        with self.assertRaisesRegex(RuntimeError, 'stopped an unknown content block'):
            decoder.feed(json.dumps(block_stop(7)))

    def test_reusing_a_stopped_index_starts_with_a_clean_argument_buffer(self):
        # The guard rejects reuse only while a block is open: a provider that
        # numbers blocks per message instead of per conversation still decodes,
        # and the retired block's arguments cannot leak into the new block.
        decoder, _ = decode('messages', [
            block_start(0, {'type': 'tool_use', 'id': 'call-a', 'name': 'search', 'input': {}}),
            block_delta(0, type='input_json_delta', partial_json='{"q":"a"}'),
            block_stop(0),
            block_start(0, {'type': 'text', 'text': 'second'}),
            block_stop(0),
        ])
        self.assertEqual([block['type'] for block in decoder.native], ['tool_use', 'text'])
        self.assertEqual(decoder.native[1], {'type': 'text', 'text': 'second'})

    def test_output_after_the_stop_reason_is_rejected(self):
        for late in (block_start(1, {'type': 'text', 'text': 'late'}),
                     block_delta(0, type='text_delta', text='late')):
            with self.subTest(late=late['type']):
                decoder, _ = decode('messages', [
                    block_start(0, {'type': 'text', 'text': ''}),
                    block_delta(0, type='text_delta', text='answer'),
                    block_stop(0),
                    message_delta('end_turn'),
                ])
                with self.assertRaisesRegex(RuntimeError, 'after its stop reason'):
                    decoder.feed(json.dumps(late))
                self.assertEqual(decoder.finish, 'stop')
                self.assertFalse(decoder.completed)

    def test_message_stop_without_a_stop_reason_is_rejected(self):
        decoder = StreamDecoder('messages')
        with self.assertRaisesRegex(RuntimeError, 'without a finish reason'):
            decoder.feed(json.dumps(MESSAGE_STOP))
        self.assertFalse(decoder.completed)

    def test_conflicting_stop_reasons_are_rejected(self):
        decoder = StreamDecoder('messages')
        decoder.feed(json.dumps(message_delta('end_turn')))
        with self.assertRaisesRegex(RuntimeError, 'conflicting stop reasons'):
            decoder.feed(json.dumps(message_delta('max_tokens')))
        self.assertEqual(decoder.finish, 'stop')

    def test_repeated_stop_reason_and_message_stop_keep_the_first_reason(self):
        decoder, choices = decode('messages', [
            message_delta('end_turn'), message_delta('end_turn'), MESSAGE_STOP, MESSAGE_STOP])
        self.assertEqual(decoder.finish, 'stop')
        self.assertTrue(decoder.completed)
        self.assertEqual([choice['finish_reason'] for choice in choices], ['stop', 'stop'])

    def test_delta_for_an_unopened_index_is_rejected(self):
        decoder = StreamDecoder('messages')
        with self.assertRaises(KeyError):
            decoder.feed(json.dumps(block_delta(3, type='text_delta', text='x')))
        self.assertFalse(decoder.completed)


class ResponsesDecoderTests(unittest.TestCase):
    def test_function_call_items_select_the_tool_calls_reason(self):
        call = {'type': 'function_call', 'id': 'item-a', 'call_id': 'call-a',
                'name': 'search', 'arguments': '{"q":"test"}'}
        for rows, expected in (
                ([{'type': 'response.output_text.delta', 'delta': 'answer'},
                  {'type': 'response.completed', 'response': {}}], 'stop'),
                ([{'type': 'response.output_item.added', 'output_index': 0,
                   'item': {**call, 'arguments': ''}},
                  {'type': 'response.function_call_arguments.delta', 'output_index': 0,
                   'delta': call['arguments']},
                  {'type': 'response.output_item.done', 'item': call},
                  {'type': 'response.completed', 'response': {}}], 'tool_calls')):
            with self.subTest(expected=expected):
                decoder, choices = decode('responses', rows)
                self.assertEqual(choices[-1]['finish_reason'], expected)
                self.assertTrue(decoder.completed)

    def test_incomplete_statuses_map_to_their_reason(self):
        cases = [
            ({'type': 'response.incomplete',
              'response': {'status': 'incomplete',
                           'incomplete_details': {'reason': 'max_output_tokens'}}}, 'length'),
            ({'type': 'response.completed',
              'response': {'status': 'completed',
                           'incomplete_details': {'reason': 'content_filter'}}}, 'content_filter'),
            ({'type': 'response.incomplete', 'response': {'status': 'incomplete'}}, 'incomplete'),
            ({'type': 'response.completed', 'response': {'status': 'failed'}}, 'incomplete'),
            ({'type': 'response.completed',
              'response': {'status': 'completed', 'error': {'message': 'x'}}}, 'incomplete'),
        ]
        for packet, expected in cases:
            with self.subTest(expected=expected):
                _, choices = decode('responses', [packet])
                self.assertEqual(choices[-1]['finish_reason'], expected)

    def test_response_failed_is_rejected(self):
        decoder = StreamDecoder('responses')
        with self.assertRaisesRegex(RuntimeError, 'could not complete'):
            decoder.feed(json.dumps({'type': 'response.failed', 'response': {}}))
        self.assertFalse(decoder.completed)

    def test_function_call_arguments_completed_only_in_the_done_item_are_not_recovered(self):
        """Pins the documented gap (hazard H2): the done item carries `arguments`
        but the decoder never emits an arguments fragment for it, so the round's
        call stays empty. Invert this test when the proposed fallback lands."""
        call = {'type': 'function_call', 'id': 'item-a', 'call_id': 'call-a',
                'name': 'search', 'arguments': '{"q":"test"}'}
        _, choices = decode('responses', [
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': {**call, 'arguments': ''}},
            {'type': 'response.output_item.done', 'item': call},
            {'type': 'response.completed', 'response': {}},
        ])
        arguments = [fragment['function']['arguments'] for choice in choices
                     for fragment in (choice['delta'].get('tool_calls') or [])]
        self.assertEqual(arguments, [''])
        self.assertEqual(choices[-1]['_provider_content']['content'], [call])


class ChatDecoderTests(unittest.TestCase):
    def test_done_before_a_finish_reason_is_rejected(self):
        decoder, _ = decode('chat', [{'choices': [{'delta': {'content': 'partial'}}]}])
        with self.assertRaisesRegex(RuntimeError, 'without a finish reason'):
            decoder.feed('[DONE]')
        self.assertFalse(decoder.completed)

    def test_done_after_a_finish_reason_is_accepted(self):
        decoder, choices = decode('chat', [{'choices': [{'delta': {}, 'finish_reason': 'stop'}]}])
        self.assertEqual(decoder.feed('[DONE]'), [])
        self.assertTrue(decoder.completed)
        self.assertEqual(choices[0]['finish_reason'], 'stop')

    def test_usage_packet_after_the_finish_reason_is_delivered_without_content(self):
        # OpenAI-compatible servers send the usage-only chunk after the finish
        # reason; the decoder must not drop it with the stream.
        decoder, choices = decode('chat', [
            {'choices': [{'delta': {'content': 'answer'}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 2}},
        ])
        self.assertTrue(decoder.completed)
        self.assertEqual(choices[-1]['_usage'], {'prompt_tokens': 7, 'completion_tokens': 2})
        self.assertEqual(choices[-1]['delta'], {})

    def test_decoder_does_not_normalise_stop_after_tool_deltas(self):
        # RouterClient.stream owns that normalisation for the local protocol; the
        # OpenCode Go chat branch passes the provider's reason through untouched.
        _, choices = decode('chat', [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call-a',
                                                    'function': {'name': 'search', 'arguments': '{}'}}]}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
        ])
        self.assertEqual(choices[-1]['finish_reason'], 'stop')

    def test_provider_error_packet_is_rejected(self):
        decoder = StreamDecoder('chat')
        with self.assertRaisesRegex(RuntimeError, 'stream error'):
            decoder.feed(json.dumps({'type': 'error', 'error': {'message': 'boom'}}))
        self.assertFalse(decoder.completed)


class GoogleDecoderTests(unittest.TestCase):
    def test_finish_reasons_map_and_count_function_calls(self):
        cases = [
            ({'candidates': [{'content': {'parts': [{'text': 'answer'}]},
                              'finishReason': 'STOP'}]}, 'stop'),
            ({'candidates': [{'content': {'parts': [
                {'functionCall': {'name': 'search', 'args': {'q': 'x'}}}]},
                'finishReason': 'STOP'}]}, 'tool_calls'),
            ({'candidates': [{'content': {'parts': [{'text': 'answer'}]},
                              'finishReason': 'MAX_TOKENS'}]}, 'length'),
            ({'candidates': [{'content': {'parts': [{'text': 'answer'}]},
                              'finishReason': 'SAFETY'}]}, 'content_filter'),
            ({'candidates': [{'content': {'parts': [{'text': 'answer'}]},
                              'finishReason': 'FUTURE_REASON'}]}, 'unsupported'),
        ]
        for packet, expected in cases:
            with self.subTest(expected=expected):
                decoder, choices = decode('google', [packet])
                self.assertEqual(choices[-1]['finish_reason'], expected)
                self.assertTrue(decoder.completed)

    def test_thought_parts_and_function_calls_are_indexed_in_arrival_order(self):
        signature = {'functionCall': {'name': 'search', 'args': {'q': 'x'}}, 'thoughtSignature': 'signed'}
        decoder, choices = decode('google', [
            {'candidates': [{'content': {'parts': [{'text': 'why', 'thought': True}]}}]},
            {'candidates': [{'content': {'parts': [{'functionCall': {'name': 'first', 'args': {}}}]}}]},
            {'candidates': [{'content': {'parts': [signature]}, 'finishReason': 'STOP'}]},
        ])
        reasoning = [choice['delta']['reasoning_content'] for choice in choices
                     if 'reasoning_content' in choice['delta']]
        self.assertEqual(reasoning, ['why'])
        indices = [fragment['index'] for choice in choices
                   for fragment in (choice['delta'].get('tool_calls') or [])]
        self.assertEqual(indices, [0, 1])
        self.assertEqual(decoder.calls, 2)
        self.assertEqual(decoder.native[-1], signature)
        self.assertEqual([fragment['id'] for choice in choices
                          for fragment in (choice['delta'].get('tool_calls') or [])],
                         ['go_call_0', 'go_call_1'])

    def test_blocked_prompt_is_rejected(self):
        decoder = StreamDecoder('google')
        with self.assertRaisesRegex(RuntimeError, 'could not process'):
            decoder.feed(json.dumps({'promptFeedback': {'blockReason': 'SAFETY'}}))
        self.assertFalse(decoder.completed)


class AbortTests(unittest.TestCase):
    def test_an_aborted_decode_never_reports_completed(self):
        decoder = StreamDecoder('messages')
        decoder.feed(json.dumps(block_start(0, {'type': 'text', 'text': ''})))
        with self.assertRaises(RuntimeError):
            decoder.feed(json.dumps({'type': 'error', 'error': {'message': 'boom'}}))
        self.assertFalse(decoder.completed)
        self.assertIsNone(decoder.finish)
        with self.assertRaisesRegex(RuntimeError, 'without a finish reason'):
            decoder.end()

    def test_end_uses_an_explicit_reason_over_the_recorded_one(self):
        decoder = StreamDecoder('messages')
        decoder.feed(json.dumps(message_delta('end_turn')))
        self.assertEqual(decoder.end('length')['finish_reason'], 'length')


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        pass


def response(*chunks):
    return httpx.Response(200, stream=BytesStream(list(chunks)))


async def framed(*chunks, bounds=None):
    return [line async for line in sse_lines(response(*chunks), StreamBudget(bounds))]


class SseFramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_crlf_split_across_chunks_yields_one_terminator(self):
        # The \r ends one chunk and the \n opens the next; the framer must not
        # treat the swallowed \n as a blank line (a spurious event dispatch).
        self.assertEqual(await framed(b'data: first\r', b'\ndata: second\n\n'),
                         ['data: first', 'data: second', ''])

    async def test_empty_chunk_between_cr_and_lf_preserves_the_frame(self):
        self.assertEqual(await framed(b'data: first\r', b'', b'\ndata: second\n'),
                         ['data: first', 'data: second'])

    async def test_lone_cr_terminates_lines(self):
        self.assertEqual(await framed(b'data: a\rdata: b\r'), ['data: a', 'data: b'])

    async def test_crlf_blank_line_split_across_chunks(self):
        self.assertEqual(await framed(b'data: a\r\n\r', b'\ndata: b\r\n'),
                         ['data: a', '', 'data: b'])

    async def test_bom_split_across_chunks_is_stripped_from_the_first_line_only(self):
        self.assertEqual(await framed(b'\xef', b'\xbb\xbfdata: a\n\ndata: \xef\xbb\xbfb\n'),
                         ['data: a', '', 'data: \ufeffb'])

    async def test_unterminated_final_line_is_delivered_at_eof(self):
        # The client feeds whatever the framer yields at EOF, so dropping this
        # line would lose the last event of a stream that ends without a newline.
        self.assertEqual(await framed(b'data: a\n\ndata: partial'), ['data: a', '', 'data: partial'])

    async def test_multibyte_character_split_across_chunks_decodes(self):
        self.assertEqual(await framed(b'data: \xc3', b'\xa9\n'), ['data: \u00e9'])

    async def test_line_at_the_limit_is_accepted_and_one_byte_more_is_rejected(self):
        bounds = StreamBounds(sse_line_bytes=16, response_bytes=4096)
        lines = sse_lines(response(b'data: ' + b'x' * 10 + b'\n', b'data: ' + b'y' * 11 + b'\n'),
                          StreamBudget(bounds))
        self.assertEqual(await anext(lines), 'data: ' + 'x' * 10)
        with self.assertRaisesRegex(StreamLimitExceeded, 'SSE line limit'):
            await anext(lines)


class StreamBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_tool_call_fragments_still_consume_the_reply_budget(self):
        budget = StreamBudget(StreamBounds(reply_bytes=10))
        calls = {}
        collect_tool_call(calls, {'index': 0, 'id': 'c1',
                                  'function': {'name': 'n', 'arguments': '{"a"'}}, budget)
        self.assertEqual(budget.reply_bytes, 7)  # 2 + 1 + 4 bytes, charged on arrival
        with self.assertRaisesRegex(StreamLimitExceeded, 'reply limit'):
            collect_tool_call(calls, {'index': 0, 'function': {'arguments': '":1}'}}, budget)
        # The rejected fragment is charged first and then refused, so the partial
        # call keeps exactly the arguments that were admitted while its bytes are
        # counted against the reply budget.
        self.assertEqual(calls[0]['function']['arguments'], '{"a"')
        self.assertEqual(budget.reply_bytes, 11)

    async def test_reasoning_and_reply_budgets_are_independent_and_count_utf8_bytes(self):
        budget = StreamBudget(StreamBounds(reply_bytes=4, reasoning_bytes=4))
        budget.add_output('reply', '\u00e9\u00e9')
        self.assertEqual(budget.reply_bytes, 4)
        budget.add_output('reasoning', '\u00e9\u00e9')
        self.assertEqual(budget.reasoning_bytes, 4)
        with self.assertRaisesRegex(StreamLimitExceeded, 'reply limit'):
            budget.add_output('reply', 'x')

    async def test_one_budget_accumulates_across_two_provider_requests(self):
        budget = StreamBudget(StreamBounds(reply_bytes=10))
        budget.add_output('reply', 'x' * 6)
        with self.assertRaisesRegex(StreamLimitExceeded, 'reply limit'):
            budget.add_output('reply', 'x' * 6)
        self.assertEqual(budget.reply_bytes, 12)

    async def test_response_budget_counts_framing_bytes_that_are_not_data_lines(self):
        budget = StreamBudget(StreamBounds(response_bytes=6))
        with self.assertRaisesRegex(StreamLimitExceeded, 'response limit'):
            await anext(sse_lines(response(b':ping\n\n'), budget))

    async def test_expired_deadline_is_reported_before_any_read(self):
        budget = StreamBudget(StreamBounds(duration_seconds=1))
        budget.deadline = asyncio.get_running_loop().time() - 1
        with self.assertRaisesRegex(StreamLimitExceeded, 'stream deadline'):
            budget.check_time()
        with self.assertRaisesRegex(StreamLimitExceeded, 'stream deadline'):
            await anext(sse_lines(response(b'data: a\n'), budget))
        self.assertEqual(budget.response_bytes, 0)


def sse(*packets):
    return ''.join('data: ' + (packet if isinstance(packet, str) else json.dumps(packet)) + '\n\n'
                   for packet in packets)


class RouterStreamTests(unittest.IsolatedAsyncioTestCase):
    def install(self, body):
        client = httpx.AsyncClient(base_url='http://router.test',
                                   transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
        router = RouterClient('http://router.test', client=client)
        self.addAsyncCleanup(router.aclose)
        return router

    async def test_done_without_a_finish_reason_is_rejected(self):
        router = self.install(sse({'choices': [{'delta': {'content': 'partial'}}]}, '[DONE]'))
        with self.assertRaisesRegex(RuntimeError, 'before completion'):
            _ = [choice async for choice in router.stream('fixture', [])]

    async def test_eof_without_done_or_finish_is_rejected(self):
        router = self.install(sse({'choices': [{'delta': {'content': 'partial'}}]}))
        with self.assertRaisesRegex(RuntimeError, 'before completion'):
            _ = [choice async for choice in router.stream('fixture', [])]

    async def test_stop_after_a_tool_delta_is_normalised_to_tool_calls(self):
        router = self.install(sse(
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call-a',
                                                    'function': {'name': 'search',
                                                                 'arguments': '{}'}}]}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 1}},
            '[DONE]'))
        choices = [choice async for choice in router.stream('fixture', [])]
        self.assertEqual(choices[1]['finish_reason'], 'tool_calls')
        self.assertEqual(choices[-1]['_usage'], {'prompt_tokens': 3, 'completion_tokens': 1})

    async def test_stop_without_a_tool_delta_stays_stop(self):
        router = self.install(sse(
            {'choices': [{'delta': {'content': 'answer'}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
            '[DONE]'))
        choices = [choice async for choice in router.stream('fixture', [])]
        self.assertEqual(choices[-1]['finish_reason'], 'stop')

    async def test_comment_and_event_lines_do_not_disturb_framing(self):
        body = ': keepalive\n\nevent: message\ndata: ' + json.dumps(
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n'
        router = self.install(body)
        choices = [choice async for choice in router.stream('fixture', [])]
        self.assertEqual([choice['finish_reason'] for choice in choices], ['stop'])

    async def test_error_packet_is_rejected(self):
        router = self.install(sse({'error': {'message': 'boom'}}))
        with self.assertRaisesRegex(RuntimeError, 'boom'):
            _ = [choice async for choice in router.stream('fixture', [])]


if __name__ == '__main__':
    unittest.main()
