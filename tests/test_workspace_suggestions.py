"""Bounded autocomplete recovery, cache and authorization contracts."""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from blackrose.gui.bridge import Api, Emitter, LoopThread
from blackrose.workspace import WorkspaceFiles
import test_gui_images as fixtures


class WorkspaceSuggestionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    async def workspace(self):
        root = self.root / 'workspace'
        root.mkdir(exist_ok=True)
        await self.runtime.controller.set_workspace(str(root))
        return root

    async def test_large_flat_and_nested_results_are_reachable_by_exact_braced_path(self):
        root = await self.workspace()
        flat_candidates = []
        for index in range(2100):
            candidate = root / f'candidate file with spaces {index:04}.txt'
            candidate.touch()
            flat_candidates.append(candidate)
        nested = root / 'nested'
        nested.mkdir()
        nested_target = nested / 'omitted-nested.txt'
        nested_target.write_text('nested')

        controller = self.runtime.controller
        initial = await controller.reference_suggestion_details('@', 'no-such-entry')
        self.assertTrue(initial['scan_truncated'])
        self.assertEqual(initial['status'], 'incomplete')
        indexed = {item['path'] for item in controller._reference_index[2]}
        flat = next(path for path in flat_candidates if str(path) not in indexed)
        self.assertNotIn(str(flat), {item['path'] for item in controller._reference_index[2]})

        found_flat = await controller.reference_suggestion_details('@', flat.name)
        self.assertIn(str(flat), {item['path'] for item in found_flat['items']})
        found_nested = await controller.reference_suggestion_details('@', 'nested/omitted-nested.txt')
        self.assertIn(str(nested_target), {item['path'] for item in found_nested['items']})
        # Existing non-detail callers keep receiving a list.
        self.assertIsInstance(await controller.reference_suggestions('@', flat.name), list)

    async def test_no_match_unavailable_and_no_workspace_states_are_distinct(self):
        root = await self.workspace()
        (root / 'known.txt').write_text('known')
        controller = self.runtime.controller
        no_match = await controller.reference_suggestion_details('@', 'nothing-matches')
        self.assertEqual(no_match['status'], 'no_matches')
        self.assertFalse(no_match['scan_truncated'])

        moved = root.with_name('moved-workspace')
        root.rename(moved)
        unavailable = await controller.reference_suggestion_details('@', 'known.txt')
        self.assertEqual(unavailable['status'], 'unavailable')
        self.assertEqual(unavailable['items'], [])

        await controller.set_workspace(None)
        no_workspace = await controller.reference_suggestion_details('@', 'known.txt')
        self.assertEqual(no_workspace['status'], 'no_workspace')

    async def test_cache_expiry_refreshes_and_workspace_switch_drops_inflight_results(self):
        root = await self.workspace()
        first = root / 'first.txt'
        first.write_text('first')
        controller = self.runtime.controller
        await controller.reference_suggestion_details('@', 'first.txt')
        second = root / 'second.txt'
        second.write_text('second')
        before_expiry = await controller.reference_suggestion_details('@', 'second.txt')
        self.assertNotIn(str(second), {item['path'] for item in before_expiry['items']})
        controller._reference_index = (*controller._reference_index[:1], 0, *controller._reference_index[2:])
        after_expiry = await controller.reference_suggestion_details('@', 'second.txt')
        self.assertIn(str(second), {item['path'] for item in after_expiry['items']})

        started = threading.Event()
        release = threading.Event()
        original_browse = WorkspaceFiles.browse

        def delayed_browse(files, *args, **kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError('test did not release browse')
            return original_browse(files, *args, **kwargs)

        controller._reference_index = None
        with patch.object(WorkspaceFiles, 'browse', delayed_browse):
            pending = asyncio.create_task(controller.reference_suggestion_details('@', 'first.txt'))
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            replacement = self.root / 'replacement-workspace'
            replacement.mkdir()
            (replacement / 'replacement.txt').write_text('replacement')
            await controller.set_workspace(str(replacement))
            release.set()
            stale = await pending
        self.assertEqual(stale['status'], 'unavailable')
        self.assertEqual(stale['items'], [])

    async def test_bridge_optional_details_mode_returns_data_and_preserves_list_default(self):
        controller = self.runtime.controller
        async def details(trigger, query):
            return {'items': [{'kind': 'file', 'path': 'detail'}], 'status': 'incomplete',
                    'scan_truncated': True, 'message': 'narrow'}

        async def legacy(trigger, query):
            return [{'kind': 'file', 'path': 'legacy'}]

        loop = LoopThread()
        loop.start()
        api = Api(self.runtime, loop, Emitter())
        try:
            with patch.object(controller, 'reference_suggestion_details', details), \
                    patch.object(controller, 'reference_suggestions', legacy):
                self.assertEqual(api.reference_suggestions('@', 'detail', True)['status'], 'incomplete')
                self.assertEqual(api.reference_suggestions('@', 'legacy'), [{'kind': 'file', 'path': 'legacy'}])
        finally:
            loop.stop()
            loop.loop.close()


if __name__ == '__main__':
    unittest.main()
