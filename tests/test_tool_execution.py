"""Tool execution keeps malformed animation calls recoverable and effect free."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from blackrose.tool_execution import ToolExecutor
from blackrose.tools import animation_tools


class ToolExecutorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.animation_calls = []
        self.tool_events = []

        async def create_animation(*args, **kwargs):
            self.animation_calls.append((args, kwargs))
            raise AssertionError('invalid animation reached the effect boundary')

        self.chat = SimpleNamespace(
            _store=SimpleNamespace(root=Path('/tmp')),
            animations_available=True,
            animations_manual_only=False,
            automatic_animation_repair=True,
            tool_definitions=lambda *args, **kwargs: animation_tools([]),
            _create_animation=create_animation,
            _sources=lambda name, result: [],
            _call_tool=lambda *args: self.fail('unsupported tool reached dispatch'),
        )
        self.prompt = SimpleNamespace(
            animation_ids=set(), xiaohei=False, plan_only=False,
            animations_enabled=True, skills_available=False,
            library_write_enabled=False, ui_used=False,
            messages=[],
        )
        self.assistant = SimpleNamespace(id=1, conversation_id=2)
        self.executor = ToolExecutor(
            self.chat, self.assistant, self.prompt, False,
            on_delta=lambda *args: None,
            on_tool=lambda *args: self.tool_events.append(args),
        )

    async def execute(self, name, arguments, call_id='call-malformed'):
        return await self.execute_raw(name, arguments, call_id)

    async def execute_raw(self, name, arguments, call_id='call-malformed'):
        call = {'id': call_id, 'function': {
            'name': name, 'arguments': json.dumps(arguments),
        }}
        await self.executor.execute(call, {})
        self.assertEqual(len(self.prompt.messages), 1)
        message = self.prompt.messages[0]
        self.assertEqual(message['tool_call_id'], call_id)
        result = json.loads(message['content'])
        json.dumps(result, allow_nan=False)
        return result

    async def test_arbitrary_scene_shapes_and_malformed_tracks_return_tool_errors(self):
        valid_scene = {
            'title': 'Example', 'description': 'A moving dot.', 'duration': 1,
            'nodes': [{'id': 'dot', 'kind': 'dot', 'x': 10, 'y': 10}],
            'tracks': [{'node': 'dot', 'property': 'x',
                        'keyframes': [{'time': 0, 'value': 0}, {'time': 1, 'value': 5}]}],
        }
        malformed = [
            None, 7, 'scene', [], ['scene'],
            {**valid_scene, 'tracks': None},
            {**valid_scene, 'tracks': {}},
            {**valid_scene, 'tracks': [None]},
            {**valid_scene, 'tracks': ['track']},
            {**valid_scene, 'tracks': [{'node': 'dot', 'property': 'x', 'keyframes': None}]},
            {**valid_scene, 'tracks': [{'node': 'dot', 'property': 'x', 'keyframes': [None]}]},
        ]
        malformed_arguments = [None, [], ['animation_create']]
        for index, arguments in enumerate(malformed_arguments, start=len(malformed)):
            with self.subTest(arguments=arguments):
                self.prompt.messages.clear()
                result = await self.execute_raw('animation_create', arguments, f'call-{index}')
                self.assertIn('error', result)
                self.assertTrue(result.get('validation_errors'))
        self.assertEqual(self.animation_calls, [])

        for index, scene in enumerate(malformed):
            with self.subTest(scene=scene):
                self.prompt.messages.clear()
                result = await self.execute('animation_create', {'scene': scene, 'loop': False},
                                            f'call-{index}')
                self.assertIn('error', result)
                self.assertTrue(result.get('validation_errors'))
                self.assertTrue(any(event[0:2] == ('animation_create', 'error')
                                    for event in self.tool_events))
        self.assertEqual(self.animation_calls, [])

    async def test_unsupported_tool_returns_associated_error_and_terminal_failure(self):
        result = await self.execute('file_read', {'path': '/tmp/never-read'})
        self.assertIn('unavailable', result['error'])
        self.assertEqual(len(self.prompt.messages), 1)

        # Rejected animation creation remains a failed artifact at the turn boundary.
        self.prompt.messages.clear()
        result = await self.execute('animation_create', {'scene': None, 'loop': False})
        self.assertIn('Invalid animation_create', result['original_error'])
        self.assertTrue(result['validation_errors'])
        with self.assertRaisesRegex(RuntimeError, 'Animation creation failed'):
            self.executor.require_successful_artifacts()
        self.assertEqual(self.animation_calls, [])

    async def test_corrected_call_after_rejection_succeeds_on_the_same_executor(self):
        await self.execute('animation_create', {'scene': None, 'loop': False})
        self.prompt.messages.clear()  # the following call represents the next model round

        async def save_animation(*args, **kwargs):
            self.animation_calls.append((args, kwargs))
            return {'attachment_id': 77}, object()

        self.chat._create_animation = save_animation
        scene = {
            'title': 'Example', 'description': 'A moving dot.', 'duration': 1,
            'nodes': [{'id': 'dot', 'kind': 'dot', 'x': 10, 'y': 10}],
            'tracks': [{'node': 'dot', 'property': 'x',
                        'keyframes': [{'time': 0, 'value': 0}, {'time': 1, 'value': 5}]}],
        }
        result = await self.execute('animation_create', {'scene': scene, 'loop': False},
                                    'call-corrected')
        self.assertEqual(result['attachment_id'], 77)
        self.assertTrue(self.executor.animation_created)
        self.executor.require_successful_artifacts()
        self.assertEqual(self.prompt.animation_ids, {77})
        self.assertEqual(len(self.animation_calls), 1)


if __name__ == '__main__':
    unittest.main()
