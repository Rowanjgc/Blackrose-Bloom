"""GUI skill imports stage filesystem work away from the asyncio loop and wait for review."""
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from blackrose.gui.bridge import Api
from blackrose.skills import SkillManager


def staging_left(manager):
    return any(p.name.startswith('.import-') for p in manager.root.iterdir())


class SkillImportBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_staging_does_not_block_loop_and_publishes_only_after_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'SKILL.md').write_text('---\nname: imported\n---\nInstructions')
            manager = SkillManager(root / 'managed')
            controller = SimpleNamespace(is_generating=False, skills=manager)
            api = Api.__new__(Api)
            api._runtime = SimpleNamespace(controller=controller)
            entered, release = threading.Event(), threading.Event()
            stage = manager.stage_install

            def slow_stage(path):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError('test staging gate timed out')
                return stage(path)

            with patch.object(manager, 'stage_install', side_effect=slow_stage):
                importing = asyncio.create_task(api._stage_skill_import(str(source)))
                await asyncio.to_thread(entered.wait, 1)
                # This runs while worker-side staging is deliberately paused.
                heartbeat = asyncio.Event()
                asyncio.get_running_loop().call_soon(heartbeat.set)
                await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
                self.assertFalse((manager.root / 'imported').exists())
                release.set()
                review = await importing

            self.assertEqual(review['id'], 'imported')
            self.assertIn('Instructions', review['text'])
            self.assertEqual(review['source'], str(source))
            # Reviewing installs nothing; only approval publishes, enabled.
            self.assertFalse((manager.root / 'imported').exists())
            self.assertEqual(manager.list(), [])
            result = api._finish_skill_import(review['token'], True)
            self.assertEqual((result['id'], result['enabled']), ('imported', True))
            self.assertTrue((manager.root / 'imported/SKILL.md').is_file())
            self.assertIn('imported', manager.catalog())
            self.assertFalse(staging_left(manager))

    async def test_cancelled_import_cleans_worker_staging_without_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'SKILL.md').write_text('---\nname: imported\n---\nInstructions')
            manager = SkillManager(root / 'managed')
            controller = SimpleNamespace(is_generating=False, skills=manager)
            api = Api.__new__(Api)
            api._runtime = SimpleNamespace(controller=controller)
            entered, release = threading.Event(), threading.Event()
            stage = manager.stage_install

            def slow_stage(path):
                entered.set()
                release.wait(2)
                return stage(path)

            with patch.object(manager, 'stage_install', side_effect=slow_stage):
                importing = asyncio.create_task(api._stage_skill_import(str(source)))
                await asyncio.to_thread(entered.wait, 1)
                importing.cancel()
                await asyncio.sleep(0)
                importing.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await importing

            self.assertFalse((manager.root / 'imported').exists())
            self.assertFalse(staging_left(manager))
            self.assertIsNone(getattr(api, '_skill_import', None))


class SkillImportEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / 'source'
        self.source.mkdir()
        (self.source / 'SKILL.md').write_text('---\nname: imported\ndescription: Notes.\n---\nInstructions')
        self.manager = SkillManager(root / 'managed')

        class Loop:
            def run(self, coroutine):
                return asyncio.run(coroutine)

            def call(self, fn, *args):
                return fn(*args)

        self.api = Api.__new__(Api)
        self.api._loop = Loop()
        self.api._window = SimpleNamespace(create_file_dialog=lambda *_: [str(self.source)])
        self.controller = SimpleNamespace(is_generating=False, skills=self.manager)
        self.api._runtime = SimpleNamespace(controller=self.controller)

    def import_review(self):
        webview = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER='folder'))
        with patch.dict(sys.modules, {'webview': webview}):
            return self.api.import_skill()

    def test_imports_without_a_window_do_not_open_dialogs_or_start_work(self):
        self.api._window = None
        self.assertEqual(self.api.import_skill(), {'cancelled': True})
        self.assertEqual(self.api.knowledge_import(), {'imports': []})
        self.assertEqual(self.manager.list(), [])

    def test_file_dialog_entry_returns_a_review_and_cancel_leaves_nothing(self):
        review = self.import_review()
        self.assertEqual((review['id'], review['description']), ('imported', 'Notes.'))
        self.assertEqual(self.api.finish_skill_import(review['token'], False), {'cancelled': True})
        self.assertEqual(self.manager.list(), [])
        self.assertEqual(self.manager.catalog(), '')
        self.assertFalse(staging_left(self.manager))
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.api.finish_skill_import(review['token'], True)
        self.assertEqual(self.manager.list(), [])

    def test_only_the_latest_review_token_can_publish(self):
        first = self.import_review()
        second = self.import_review()
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.api.finish_skill_import(first['token'], True)
        # A refused token never consumes the current review.
        self.assertTrue(self.api.finish_skill_import(second['token'], True)['enabled'])
        self.assertFalse(staging_left(self.manager))

    def test_approval_during_a_reply_installs_nothing(self):
        review = self.import_review()
        self.controller.is_generating = True
        with self.assertRaisesRegex(ValueError, 'Wait for the reply'):
            self.api.finish_skill_import(review['token'], True)
        self.assertEqual(self.manager.list(), [])
        self.assertFalse(staging_left(self.manager))

    def test_installed_skill_review_and_approval_go_through_the_bridge(self):
        self.manager.install(str(self.source))
        review = self.api.review_skill('imported')
        self.assertEqual(review['review'], 'required')
        self.assertTrue(self.api.approve_skill('imported', review['digest']))
        self.assertTrue(self.manager.list()[0]['enabled'])
