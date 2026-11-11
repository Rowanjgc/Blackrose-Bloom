"""Preference write failures keep runtime and persisted choices coherent."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from blackrose.config import load_config, save_preferences
from blackrose.gui.session import Session
from test_gui_images import make_runtime


class PreferenceSaveFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')})
        self.environment.start()
        self.config, self.runtime = make_runtime(self.root)
        await self.runtime.open()
        self.events = []
        self.session = Session(self.runtime.controller, lambda *event: self.events.append(event))
        await self.session.boot()
        save_preferences(self.config)

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.environment.stop()
        self.temp.cleanup()

    async def test_tool_toggle_save_failures_keep_config_and_visible_status_unchanged(self):
        for operation, key in ((self.session.set_web, 'web_enabled'),
                               (self.session.set_knowledge, 'knowledge_enabled')):
            self.events.clear()
            with patch('blackrose.chat.save_preferences', side_effect=PermissionError('read only')):
                await operation(True)
            self.assertFalse(getattr(self.config, key))
            self.assertFalse(getattr(load_config(self.root / 'missing.toml'), key))
            self.assertFalse(any(kind == 'status' for kind, _ in self.events))
            notices = [payload for kind, payload in self.events if kind == 'notice']
            self.assertTrue(notices)
            self.assertEqual(notices[-1]['tone'], 'warning')

    async def test_failed_default_save_keeps_session_model_but_restores_startup_default(self):
        controller = self.runtime.controller
        controller.backend.load = AsyncMock()
        result = await self.session.select_model('another-model', False)
        self.assertEqual(result, {'ok': True})
        with patch('blackrose.chat.save_preferences', side_effect=PermissionError('read only')):
            result = await self.session.select_model('third-model', True)
        self.assertIn('error', result)
        self.assertIn('loaded for this session', result['error'])
        self.assertEqual(controller.model_name, 'third-model')
        self.assertEqual(self.config.default_model, 'test-vision')
        self.assertEqual(load_config(self.root / 'missing.toml').default_model, 'test-vision')

