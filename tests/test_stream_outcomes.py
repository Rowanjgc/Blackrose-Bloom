"""Offline protocol matrix for truthful reply completion and tool execution."""
import json
import unittest
from unittest.mock import Mock, patch

import httpx

from blackrose.backend.opencode import OpenCodeGoClient, PREFIX
from blackrose.backend.providers import ChatProviders
from blackrose.backend.router import RouterClient
import test_gui_images as fixtures


MODELS = {'local': 'local-model', 'chat': 'glm-5.3', 'messages': 'minimax-m3',
          'responses': 'gpt-6-luna', 'google': 'gemini-4-argon-contributor'}
TEXT = 'Accepted partial answer.'
THOUGHT = 'Considering the request.'
ARGS = {'path': 'output.txt', 'content': 'Completed output'}


def packets(protocol, outcome, *, tools=False, text=True):
    rows = []
    if protocol in ('local', 'chat'):
        rows.append({'choices': [{'delta': {'reasoning_content': THOUGHT}}]})
        if text:
            rows.append({'choices': [{'delta': {'content': TEXT}}]})
        if tools:
            rows.append({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call-a',
                'function': {'name': 'document_create', 'arguments': json.dumps(ARGS)}}]}}]})
        if outcome not in ('missing', 'truncated', 'provider_error'):
            reason = {'normal': 'tool_calls' if tools else 'stop', 'length': 'length',
                      'filter': 'content_filter', 'unknown': 'future_reason',
                      'end_turn': 'end_turn', 'stop_tools': 'stop',
                      'empty_tools': 'tool_calls'}.get(outcome, outcome)
            rows.append({'choices': [{'delta': {}, 'finish_reason': reason}]})
    elif protocol == 'messages':
        rows += [{'type': 'content_block_start', 'index': 0, 'content_block':
                  {'type': 'thinking', 'thinking': ''}},
                 {'type': 'content_block_delta', 'index': 0,
                  'delta': {'type': 'thinking_delta', 'thinking': THOUGHT}},
                 {'type': 'content_block_stop', 'index': 0}]
        if text:
            rows += [{'type': 'content_block_start', 'index': 1,
                      'content_block': {'type': 'text', 'text': ''}},
                     {'type': 'content_block_delta', 'index': 1,
                      'delta': {'type': 'text_delta', 'text': TEXT}},
                     {'type': 'content_block_stop', 'index': 1}]
        if tools:
            rows += [{'type': 'content_block_start', 'index': 2, 'content_block':
                      {'type': 'tool_use', 'id': 'call-a', 'name': 'document_create', 'input': {}}},
                     {'type': 'content_block_delta', 'index': 2,
                      'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(ARGS)}},
                     {'type': 'content_block_stop', 'index': 2}]
        if outcome not in ('missing', 'truncated', 'provider_error'):
            reason = {'normal': 'tool_use' if tools else 'end_turn', 'length': 'max_tokens',
                      'filter': 'refusal', 'unknown': 'future_reason'}.get(outcome, outcome)
            rows.append({'type': 'message_delta', 'delta': {'stop_reason': reason}})
        if outcome not in ('truncated', 'provider_error'):
            rows.append({'type': 'message_stop'})
    elif protocol == 'responses':
        rows.append({'type': 'response.reasoning_summary_text.delta', 'delta': THOUGHT})
        if text:
            rows.append({'type': 'response.output_text.delta', 'delta': TEXT})
        if tools:
            call = {'type': 'function_call', 'id': 'item-a', 'call_id': 'call-a',
                    'name': 'document_create', 'arguments': json.dumps(ARGS)}
            rows += [{'type': 'response.output_item.added', 'output_index': 0,
                      'item': {**call, 'arguments': ''}},
                     {'type': 'response.function_call_arguments.delta', 'output_index': 0,
                      'delta': call['arguments']},
                     {'type': 'response.output_item.done', 'item': call}]
        if outcome not in ('missing', 'truncated', 'provider_error'):
            reason = {'length': 'max_output_tokens', 'filter': 'content_filter'}.get(outcome, outcome)
            response = {'status': 'completed'} if outcome == 'normal' else {
                'status': 'incomplete', 'incomplete_details': {'reason': reason}}
            rows.append({'type': 'response.completed' if outcome == 'normal' else 'response.incomplete',
                         'response': response})
    else:
        parts = [{'text': THOUGHT, 'thought': True}]
        if text:
            parts.append({'text': TEXT})
        if tools:
            parts.append({'functionCall': {'id': 'call-a', 'name': 'document_create', 'args': ARGS}})
        candidate = {'content': {'parts': parts}}
        if outcome not in ('missing', 'truncated', 'provider_error'):
            candidate['finishReason'] = {'normal': 'STOP', 'length': 'MAX_TOKENS',
                                        'filter': 'SAFETY', 'unknown': 'FUTURE_REASON'}.get(outcome, outcome)
        rows.append({'candidates': [candidate]})
    if outcome == 'provider_error':
        rows.append({'error': {'message': 'Injected provider failure'}, 'type': 'error'})
    if outcome != 'truncated':
        rows.append('[DONE]')
    return rows


def events(rows):
    return ''.join('data: ' + (row if isinstance(row, str) else json.dumps(row)) + '\n\n'
                   for row in rows)


class StreamOutcomeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def configure(self, protocol, rows):
        requests = []

        def handler(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'data': [{'id': model, 'status': {'value': 'loaded'}}
                                                        for model in MODELS.values()]})
            if request.url.path == '/props':
                return httpx.Response(200, json={})
            requests.append(json.loads(request.content))
            return httpx.Response(200, text=events(rows if len(requests) == 1 else
                                                   packets(protocol, 'normal')))

        transport = httpx.MockTransport(handler)
        local = RouterClient('https://router.test', client=httpx.AsyncClient(
            base_url='https://router.test', transport=transport))
        go = OpenCodeGoClient(keychain=Mock(get=Mock(return_value='fixture-key')), transport=transport)
        backend = ChatProviders(local, go)
        self.addAsyncCleanup(backend.aclose)
        c = self.runtime.controller
        c.backend = backend
        c.model_name = MODELS[protocol] if protocol == 'local' else PREFIX + MODELS[protocol]
        c.schedule_title = Mock()
        await c.new_conversation()
        # Outputs save without review only in the app-owned output folder.
        work = self.root / str(c.conversation.id) / 'Blackrose'
        work.mkdir(parents=True)
        self.enterContext(patch('blackrose.workspace.downloads_directory', return_value=work.parent))
        await c.set_workspace(None)
        return c, work, requests

    async def test_normal_text_and_tool_rounds_complete_across_protocols(self):
        for protocol in MODELS:
            for tools in (False, True):
                with self.subTest(protocol=protocol, tools=tools):
                    c, work, requests = await self.configure(protocol, packets(protocol, 'normal', tools=tools))
                    reasoning = []
                    turn = await c.send('Create an output', on_reasoning=reasoning.append)
                    self.assertEqual(turn.assistant_message.state.value, 'complete')
                    self.assertIn(TEXT, turn.assistant_message.content)
                    self.assertIn(THOUGHT, ''.join(reasoning))
                    self.assertEqual(len(requests), 2 if tools else 1)
                    self.assertEqual(len(list(work.iterdir())), int(tools))
                    if tools:
                        self.assertEqual((work / 'output.txt').read_text(), ARGS['content'])

    async def test_abnormal_outcomes_preserve_text_and_execute_no_tools(self):
        for protocol in MODELS:
            for outcome in ('length', 'filter', 'unknown', 'missing', 'truncated', 'provider_error'):
                for tools in (False, True):
                    with self.subTest(protocol=protocol, outcome=outcome, tools=tools):
                        c, work, requests = await self.configure(protocol, packets(protocol, outcome, tools=tools))
                        with self.assertRaises(RuntimeError):
                            await c.send('Create an output')
                        self.assertEqual(list(work.iterdir()), [])
                        self.assertEqual(len(requests), 1)
                        await self.runtime.database.close()
                        await self.runtime.database.connect()
                        saved = (await c.current_messages())[-1]
                        self.assertEqual(saved.state.value, 'error')
                        self.assertEqual(saved.content, TEXT)
                        self.assertTrue(saved.error)
                        self.assertFalse(c.is_generating)

    async def test_reasoning_only_never_becomes_a_visible_success(self):
        for protocol in MODELS:
            for outcome in ('normal', 'length', 'missing'):
                with self.subTest(protocol=protocol, outcome=outcome):
                    c, _, _ = await self.configure(protocol, packets(protocol, outcome, text=False))
                    reasoning = []
                    with self.assertRaises(RuntimeError):
                        await c.send('Answer', on_reasoning=reasoning.append)
                    saved = (await c.current_messages())[-1]
                    self.assertEqual(saved.state.value, 'error')
                    self.assertEqual(saved.content, '')
                    self.assertEqual(''.join(reasoning), THOUGHT)

    async def test_local_stop_with_tools_remains_supported(self):
        c, work, _ = await self.configure('local', packets('local', 'stop_tools', tools=True))
        turn = await c.send('Create output')
        self.assertEqual(turn.assistant_message.state.value, 'complete')
        self.assertEqual((work / 'output.txt').read_text(), ARGS['content'])

    async def test_explicit_normal_finishes_do_not_require_done_marker(self):
        for protocol in MODELS:
            with self.subTest(protocol=protocol):
                c, _, _ = await self.configure(protocol, packets(protocol, 'normal')[:-1])
                self.assertEqual((await c.send('Answer')).assistant_message.state.value, 'complete')

    async def test_end_turn_and_messages_stop_sequence_are_normal_text_finishes(self):
        for protocol, reason in (('local', 'end_turn'), ('chat', 'end_turn'),
                                 ('messages', 'stop_sequence')):
            with self.subTest(protocol=protocol, reason=reason):
                c, _, _ = await self.configure(protocol, packets(protocol, reason))
                self.assertEqual((await c.send('Answer')).assistant_message.state.value, 'complete')

    async def test_responses_completed_event_cannot_hide_an_incomplete_body(self):
        for response in ({'status': 'incomplete'}, {'status': 'failed'},
                         {'status': 'completed', 'incomplete_details': {'reason': 'content_filter'}},
                         {'status': 'completed', 'error': {'message': 'failure'}}):
            with self.subTest(response=response):
                rows = packets('responses', 'normal', tools=True)
                rows[-2]['response'] = response
                c, work, _ = await self.configure('responses', rows)
                with self.assertRaises(RuntimeError):
                    await c.send('Create output')
                self.assertEqual(list(work.iterdir()), [])
                self.assertEqual((await c.current_messages())[-1].content, TEXT)

    async def test_cloud_stop_with_tools_and_tool_finish_without_calls_are_rejected(self):
        for protocol, outcome, tools in (('chat', 'stop_tools', True), ('chat', 'empty_tools', False),
                                         ('messages', 'end_turn', True)):
            with self.subTest(protocol=protocol, outcome=outcome):
                c, work, _ = await self.configure(protocol, packets(protocol, outcome, tools=tools))
                with self.assertRaisesRegex(RuntimeError, 'inconsistent with its finish reason'):
                    await c.send('Create output')
                self.assertEqual(list(work.iterdir()), [])

    async def test_messages_pause_context_limit_and_unknown_reasons_never_execute(self):
        for reason in ('pause_turn', 'model_context_window_exceeded', 'refusal', 'future_reason'):
            with self.subTest(reason=reason):
                c, work, _ = await self.configure('messages', packets('messages', reason, tools=True))
                with self.assertRaises(RuntimeError):
                    await c.send('Create output')
                self.assertEqual(list(work.iterdir()), [])
                self.assertEqual((await c.current_messages())[-1].content, TEXT)

    async def test_bare_done_cannot_complete_any_protocol(self):
        for protocol in MODELS:
            with self.subTest(protocol=protocol):
                c, _, _ = await self.configure(protocol, ['[DONE]'])
                with self.assertRaises(RuntimeError):
                    await c.send('Answer')
                self.assertEqual((await c.current_messages())[-1].state.value, 'error')

    async def test_output_after_finish_and_conflicting_reasons_are_rejected(self):
        for terminal in ({'delta': {'content': 'Late output'}},
                         {'delta': {}, 'finish_reason': 'stop'}):
            with self.subTest(terminal=terminal):
                rows = packets('chat', 'filter', tools=True)
                rows.insert(-1, {'choices': [terminal]})
                c, work, _ = await self.configure('chat', rows)
                with self.assertRaises(RuntimeError):
                    await c.send('Create output')
                self.assertEqual(list(work.iterdir()), [])
                self.assertEqual((await c.current_messages())[-1].content, TEXT)

    async def test_messages_cannot_override_an_abnormal_stop_reason(self):
        rows = packets('messages', 'filter', tools=True)
        rows.insert(-2, {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}})
        c, work, _ = await self.configure('messages', rows)
        with self.assertRaisesRegex(RuntimeError, 'conflicting stop reasons'):
            await c.send('Create output')
        self.assertEqual(list(work.iterdir()), [])
        self.assertEqual((await c.current_messages())[-1].content, TEXT)
