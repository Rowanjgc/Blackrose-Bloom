"""Every advertised tool has exactly one explicit handler, and unlisted tools never reach one."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from blackrose import tool_execution, tools
from blackrose.tool_execution import CONTROLLER_TOOLS, FAILURE_HOOKS, FILE_TOOL_NAMES, HANDLERS, ToolExecutor
from blackrose.tool_host import ToolHost


def advertised_names() -> set[str]:
    definitions = [tools.SKILL_TOOL, tools.SKILL_SCRIPT_TOOL, tools.LIBRARY_ADD_TOOL, tools.IMAGE_TOOL,
                   tools.illustration_draft_tool(), tools.activation_tool(
                       ['animations', 'interactive_ui', 'library_write']),
                   tools.image_tool(local=True, xiaohei=True),
                   *tools.KNOWLEDGE_TOOLS, *tools.WEB_TOOLS, *tools.FILE_TOOLS,
                   *tools.animation_tools(['attachment'], plan_first=True)]
    return {definition['function']['name'] for definition in definitions}


class ToolHandlerContractTests(unittest.TestCase):
    def test_handler_sets_are_disjoint(self):
        groups = [set(HANDLERS), set(FILE_TOOL_NAMES), set(CONTROLLER_TOOLS)]
        for index, group in enumerate(groups):
            for other in groups[index + 1:]:
                self.assertEqual(group & other, set())

    def test_advertised_tools_match_handlers_exactly(self):
        handled = set(HANDLERS) | set(FILE_TOOL_NAMES) | set(CONTROLLER_TOOLS)
        advertised = advertised_names()
        self.assertEqual(advertised - handled, set(), 'advertised tools without a handler')
        self.assertEqual(handled - advertised, set(), 'handlers for tools never advertised')

    def test_every_handler_method_exists(self):
        for name, method in HANDLERS.items():
            self.assertTrue(callable(getattr(ToolExecutor, method, None)), f'{name} -> {method}')

    def test_failure_hooks_name_advertised_tools_and_existing_methods(self):
        advertised = advertised_names()
        for name, method in FAILURE_HOOKS.items():
            self.assertIn(name, advertised, f'failure hook for unadvertised tool {name}')
            self.assertTrue(callable(getattr(ToolExecutor, method, None)), f'{name} -> {method}')


class UnadvertisedToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_unadvertised_tool_is_rejected_before_any_handler_runs(self):
        chat = SimpleNamespace(
            _store=SimpleNamespace(root=Path('/tmp')), animations_available=False,
            animations_manual_only=False, automatic_animation_repair=False,
            tool_definitions=lambda *args, **kwargs: [],
            _sources=lambda name, result: [],
            _call_tool=mock.AsyncMock(side_effect=AssertionError('effect reached')),
        )
        prompt = SimpleNamespace(
            animation_ids=set(), xiaohei=False, plan_only=False, animations_enabled=False,
            skills_available=False, library_write_enabled=False, ui_used=False, messages=[],
        )
        executor = ToolExecutor(chat, SimpleNamespace(id=1, conversation_id=2), prompt, False,
                                on_delta=lambda *args: None)
        call = {'id': 'call-1', 'function': {'name': 'shell_run', 'arguments': json.dumps({'cmd': 'x'})}}
        with mock.patch.object(executor, '_run', side_effect=AssertionError('handler reached')) as run:
            await executor.execute(call, {})
        run.assert_not_called()
        chat._call_tool.assert_not_called()
        result = json.loads(prompt.messages[0]['content'])
        self.assertIn('error', result)
        self.assertEqual(prompt.messages[0]['tool_call_id'], 'call-1')



# Widening this surface is a deliberate boundary change: update the allowlist with it.
HOST_SURFACE = {
    'tool_definitions', 'automatic_animation_repair', 'animations_manual_only', 'knowledge_enabled',
    'illustration_drafts_root', 'visual_review_available', 'outbound_active', 'preauthorize',
    'call_tool', 'file_tool', 'create_animation', 'finish_animation_repair', 'review_animation',
    'generate_image', 'image_recovery_error', 'propose_library_add', 'web_capture',
    'attachments_for', 'sources',
    'run_skill_script',
}


class ToolHostBoundaryTests(unittest.TestCase):
    """Handlers reach the controller only through ToolHost's named capabilities."""

    def executor_tree(self) -> ast.Module:
        return ast.parse(Path(tool_execution.__file__).read_text(encoding='utf-8'))

    def test_host_surface_is_the_allowlist(self):
        public = {name for name in vars(ToolHost) if not name.startswith('_')}
        self.assertEqual(public, HOST_SURFACE)
        self.assertEqual(ToolHost.__slots__, ('_controller',))

    def test_executor_uses_only_named_host_capabilities(self):
        used = {node.attr for node in ast.walk(self.executor_tree())
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                and node.value.attr == 'host' and isinstance(node.value.value, ast.Name)
                and node.value.value.id == 'self'}
        self.assertTrue(used)
        self.assertEqual(used - HOST_SURFACE, set())

    def test_executor_reads_no_foreign_private_attributes(self):
        # self._x is the executor's own state; any other obj._x would bypass the host.
        foreign = sorted(f'{ast.unparse(node.value)}.{node.attr}' for node in ast.walk(self.executor_tree())
                         if isinstance(node, ast.Attribute) and node.attr.startswith('_')
                         and not node.attr.startswith('__')
                         and not (isinstance(node.value, ast.Name) and node.value.id == 'self'))
        self.assertEqual(foreign, [])

    def test_executor_holds_no_controller_reference(self):
        controller = SimpleNamespace(_store=SimpleNamespace(root=Path('/tmp')))
        executor = ToolExecutor(controller, SimpleNamespace(id=1, conversation_id=2),
                                SimpleNamespace(), False, on_delta=lambda *args: None)
        self.assertFalse(any(value is controller for value in vars(executor).values()))
        self.assertIsInstance(executor.host, ToolHost)

    def test_host_preauthorizes_without_exposing_the_policy(self):
        policy = SimpleNamespace(preauthorize=mock.AsyncMock())
        host = ToolHost(SimpleNamespace(outbound=policy))
        self.assertTrue(host.outbound_active)
        asyncio.run(host.preauthorize([('web_search', {'query': 'q'})]))
        policy.preauthorize.assert_awaited_once_with([('web_search', {'query': 'q'})])
        self.assertFalse(ToolHost(SimpleNamespace(outbound=None)).outbound_active)


if __name__ == '__main__':
    unittest.main()
