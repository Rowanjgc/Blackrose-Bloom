"""Reject ambiguous streamed identities before a round can write any files."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import httpx

from blackrose.backend.opencode import OpenCodeGoClient, PREFIX
from blackrose.backend.providers import ChatProviders
from blackrose.backend.router import RouterClient
import test_gui_images as fixtures


def fragment(index, identity, arguments=None, name='document_create'):
    return {'index': index, 'id': identity, 'function': {
        'name': name, 'arguments': arguments if arguments is not None else
        json.dumps({'path': 'output.txt', 'content': 'Saved output'})}}


class ToolIdentityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def configure(self, provider, fragments):
        requests = []

        def handler(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'data': [
                    {'id': 'glm-5.3', 'status': {'value': 'loaded'}}]})
            if request.url.path == '/props':
                return httpx.Response(200, json={})
            body = json.loads(request.content)
            requests.append(deepcopy(body['messages']))
            if len(requests) == 1:
                choices = [{'delta': {'content': 'Preparing outputs.'}}]
                choices += [{'delta': {'tool_calls': [f]}} for f in fragments]
                choices.append({'delta': {}, 'finish_reason': 'tool_calls'})
            else:
                choices = [{'delta': {'content': 'Done.'}, 'finish_reason': 'stop'}]
            wire = ''.join('data: ' + json.dumps({'choices': [choice]}) + '\n\n'
                           for choice in choices)
            return httpx.Response(200, text=wire + 'data: [DONE]\n\n')

        transport = httpx.MockTransport(handler)
        local = RouterClient('https://router.test', client=httpx.AsyncClient(
            base_url='https://router.test', transport=transport))
        go = OpenCodeGoClient(keychain=Mock(get=Mock(return_value='fixture-key')),
                              transport=transport)
        backend = ChatProviders(local, go)
        self.addAsyncCleanup(backend.aclose)
        c = self.runtime.controller
        c.backend = backend
        c.model_name = PREFIX + 'glm-5.3' if provider == 'cloud' else 'glm-5.3'
        c.schedule_title = Mock()
        await c.new_conversation()
        # Outputs save without review only in the app-owned output folder.
        work = self.root / provider / 'Blackrose'
        work.mkdir(parents=True, exist_ok=True)
        self.enterContext(patch('blackrose.workspace.downloads_directory', return_value=work.parent))
        await c.set_workspace(None)
        return c, work, requests

    async def test_duplicate_ids_reject_entire_round_before_any_side_effect(self):
        for provider in ('local', 'cloud'):
            with self.subTest(provider=provider):
                # Even the unambiguous first call must not execute before the
                # host discovers that the remaining identities are ambiguous.
                c, work, requests = await self.configure(provider, [
                    fragment(0, 'unique'), fragment(1, 'duplicate'),
                    fragment(2, 'duplicate')])
                with self.assertRaisesRegex(RuntimeError, 'duplicate tool-call IDs'):
                    await c.send('Create outputs')
                self.assertEqual(list(work.iterdir()), [])
                self.assertEqual(len(requests), 1)
                saved = (await c.current_messages())[-1]
                self.assertEqual(saved.state.value, 'error')
                self.assertEqual(saved.content, 'Preparing outputs.')
                self.assertIn('no tools from this round were executed', saved.error)
                self.assertEqual(saved.attachments, [])
                self.assertFalse(c.is_generating)

    def assert_continuation(self, requests, expected_ids):
        self.assertEqual(len(requests), 2)
        continuation = requests[1]
        calls = next(m['tool_calls'] for m in reversed(continuation) if m.get('tool_calls'))
        self.assertEqual([call['id'] for call in calls], expected_ids)
        results = [m for m in continuation if m['role'] == 'tool']
        self.assertEqual([m['tool_call_id'] for m in results], expected_ids)
        self.assertTrue(all('error' not in json.loads(m['content']) for m in results))
        return calls

    async def test_identical_actions_with_distinct_ids_both_execute(self):
        for provider in ('local', 'cloud'):
            with self.subTest(provider=provider):
                c, work, requests = await self.configure(provider, [
                    fragment(0, 'first'), fragment(1, 'second')])
                turn = await c.send('Create two identical outputs')
                self.assertEqual(sorted(p.name for p in work.iterdir()),
                                 ['output (1).txt', 'output.txt'])
                self.assertTrue(all(p.read_text() == 'Saved output' for p in work.iterdir()))
                self.assertEqual(turn.assistant_message.state.value, 'complete')
                self.assert_continuation(requests, ['first', 'second'])

    async def test_missing_ids_are_unique_and_avoid_later_supplied_ids(self):
        for provider in ('local', 'cloud'):
            with self.subTest(provider=provider):
                c, work, requests = await self.configure(provider, [
                    fragment(0, None), fragment(1, ''),
                    fragment(2, 'call_0_0'), fragment(3, 'call_0_0_')])
                await c.send('Create four outputs')
                self.assertEqual(len(list(work.iterdir())), 4)
                self.assert_continuation(requests,
                                         ['call_0_0__', 'call_0_1', 'call_0_0', 'call_0_0_'])

    async def test_interleaved_fragments_keep_stable_ids_and_argument_associations(self):
        for provider in ('local', 'cloud'):
            with self.subTest(provider=provider):
                c, work, requests = await self.configure(provider, [
                    fragment(7, 'first', '{"path":"first.txt",'),
                    fragment(2, None, '{"path":"second.txt",'),
                    fragment(2, 'second', '"content":"Second"}', name=''),
                    fragment(7, 'first', '"content":"First"}', name='')])
                await c.send('Create two outputs')
                self.assertEqual((work / 'first.txt').read_text(), 'First')
                self.assertEqual((work / 'second.txt').read_text(), 'Second')
                calls = self.assert_continuation(requests, ['first', 'second'])
                self.assertEqual([json.loads(call['function']['arguments'])['path'] for call in calls],
                                 ['first.txt', 'second.txt'])

    async def test_changed_identity_cannot_hide_duplicate_fragments(self):
        for provider in ('local', 'cloud'):
            with self.subTest(provider=provider):
                c, work, requests = await self.configure(provider, [
                    fragment(0, 'same'), fragment(1, 'same'),
                    fragment(1, 'replacement', '', name='')])
                with self.assertRaisesRegex(RuntimeError, 'changed a tool-call ID'):
                    await c.send('Create outputs')
                self.assertEqual(list(work.iterdir()), [])
                self.assertEqual(len(requests), 1)

    async def test_nonstring_ids_fail_before_side_effects(self):
        for provider in ('local', 'cloud'):
            for identity in (42, ['invalid'], {'invalid': 'id'}):
                with self.subTest(provider=provider, identity=identity):
                    c, work, requests = await self.configure(provider, [fragment(0, identity)])
                    with self.assertRaisesRegex(RuntimeError, 'invalid tool-call ID'):
                        await c.send('Create output')
                    self.assertEqual(list(work.iterdir()), [])
                    self.assertEqual(len(requests), 1)
