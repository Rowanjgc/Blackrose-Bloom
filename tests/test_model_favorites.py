"""Favorites persist independently of provider discovery and model loading."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from blackrose.backend.providers import ChatProviders
from blackrose.config import Config, load_config, save_preferences
from blackrose.gui.session import Session
from test_gui_images import make_runtime


class ModelFavoritesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')})
        self.environment.start()
        self.config, self.runtime = make_runtime(self.root)
        self.local = self.runtime.controller.backend
        self.local.load = AsyncMock()
        self.go = SimpleNamespace(models=AsyncMock(return_value=[{'id': 'opencode-go/test-vision'}]),
                                  load=AsyncMock(), aclose=AsyncMock())
        self.runtime.controller.backend = ChatProviders(self.local, self.go)
        await self.runtime.open()
        self.session = Session(self.runtime.controller, lambda *args: None)
        await self.session.boot()
        self.local.load.reset_mock()

    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.environment.stop()
        self.temp.cleanup()

    async def test_namespaced_favorites_survive_restart_and_default_changes_without_loading(self):
        for model in ['test-vision', 'opencode-go/test-vision', 'removed-preset', 'test-vision']:
            self.assertNotIn('error', self.session.set_model_favorite(model, True))
        expected = ['test-vision', 'opencode-go/test-vision', 'removed-preset']
        self.assertEqual(load_config(self.root / 'missing.toml').favorite_models, expected)
        self.local.load.assert_not_awaited()
        self.go.load.assert_not_awaited()
        self.assertEqual(self.runtime.controller.model_name, 'test-vision')
        self.assertEqual(self.config.default_model, 'test-vision')
        self.assertEqual(await self.session.select_model('opencode-go/test-vision', True), {'ok': True})
        saved = load_config(self.root / 'missing.toml')
        self.assertEqual(saved.favorite_models, expected)
        self.assertEqual(saved.default_model, 'opencode-go/test-vision')
        self.assertFalse(saved.web_enabled)
        self.assertFalse(saved.knowledge_enabled)

    async def test_browsing_keeps_full_catalog_and_unavailable_favorites(self):
        self.session.set_model_favorite('removed-preset', True)
        for provider, model in [('local', 'test-vision'), ('opencode-go', 'opencode-go/test-vision')]:
            result = await self.session.models(provider)
            self.assertEqual([row['id'] for row in result['items']], [model])
            self.assertEqual(result['favorites'], ['removed-preset'])
        self.assertEqual(self.session.set_model_favorite('removed-preset', False), {'favorites': []})
        self.assertEqual(load_config(self.root / 'missing.toml').favorite_models, [])
        self.local.load.assert_not_awaited()
        self.go.load.assert_not_awaited()

    async def test_save_failure_retains_previous_favorites_in_memory_and_on_disk(self):
        self.session.set_model_favorite('test-vision', True)
        with patch('blackrose.chat.save_preferences', side_effect=PermissionError('Cannot save preferences')):
            result = self.session.set_model_favorite('another-model', True)
        self.assertIn('Cannot save preferences', result['error'])
        self.assertEqual(self.config.favorite_models, ['test-vision'])
        self.assertEqual(load_config(self.root / 'missing.toml').favorite_models, ['test-vision'])

    async def test_old_preferences_and_invalid_inputs(self):
        save_preferences(self.config)
        path = self.root / 'config/blackrose-bloom/preferences.json'
        values = json.loads(path.read_text())
        values.pop('favorite_models')
        path.write_text(json.dumps(values))
        self.assertEqual(load_config(self.root / 'missing.toml').favorite_models, [])
        for model, favorite in [('', True), ('  ', True), (None, True), ('test-vision', 'false')]:
            self.assertIn('error', self.session.set_model_favorite(model, favorite))
        for value in [None, 'model', [None], [' '], [False]]:
            with self.assertRaises(ValueError):
                Config(favorite_models=value)
        self.assertEqual(Config(favorite_models=['model', 'model']).favorite_models, ['model'])
