"""Unit checks for native contrast math and isolated setting/compositing guards."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_native_contrast_motion.py'
spec = importlib.util.spec_from_file_location('check_native_contrast_motion', SCRIPT)
probe = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(probe)


class NativeContrastProbeTests(unittest.TestCase):
    def test_parses_webkit_rgb_and_rgba_colors(self):
        self.assertEqual(probe.rgb('rgb(224, 224, 224)'), (224, 224, 224))
        self.assertEqual(probe.rgb('rgba(13, 13, 13, 1)'), (13, 13, 13))
        self.assertEqual(probe.rgb('#ffffff'), (255, 255, 255))
        with self.assertRaisesRegex(ValueError, 'translucent'):
            probe.rgb('rgba(13, 13, 13, 0.78)')

    def test_translucent_foreground_is_composited_over_the_sampled_background(self):
        self.assertEqual(probe.composite('rgba(255, 255, 255, 0.5)', (0, 0, 0)), (128, 128, 128))
        self.assertEqual(probe.composite('rgb(10, 20, 30)', (200, 200, 200)), (10, 20, 30))
        self.assertEqual(probe.composite('#ffffff', (0, 0, 0)), (255, 255, 255))

    def test_contrast_math_matches_black_white_reference(self):
        self.assertEqual(probe.contrast_ratio((255, 255, 255), (0, 0, 0)), 21)
        self.assertEqual(round(probe.contrast_ratio((224, 224, 224), (13, 13, 13)), 2), 14.72)

    def test_private_desktop_uses_memory_backend_without_dconf_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'GSETTINGS_BACKEND': 'dconf', 'DCONF_PROFILE': '/should/not/be/used',
                'XDG_CONFIG_HOME': '/previous/config', 'XDG_DATA_HOME': '/previous/data',
                'XDG_STATE_HOME': '/previous/state', 'XDG_CACHE_HOME': '/previous/cache',
                'XDG_RUNTIME_DIR': '/previous/runtime'}):
            result = probe.setup_private_desktop(Path(directory))
            self.assertEqual(result, {
                'gsettings_backend': 'memory',
                'persistent_dconf_mutation': False,
                'gtk_setting_scope': 'current process Gtk.Settings object only'})
            self.assertNotIn('DCONF_PROFILE', os.environ)
            self.assertEqual(os.environ['GSETTINGS_BACKEND'], 'memory')
            for key in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
                        'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR'):
                self.assertTrue(os.environ[key].startswith(directory))

    def test_disabled_or_composited_text_has_no_reported_ratio(self):
        self.assertIsNone(probe.comparison_block_reason({'enabled': True, 'visual_effects': []}))
        self.assertIn('disabled', probe.comparison_block_reason({'enabled': False}))
        self.assertIn('opacity', probe.comparison_block_reason({
            'enabled': True, 'visual_effects': [{'selector': '.btn', 'opacity': 0.55}]}))
        # The page itself reports the camel-case key.
        self.assertIn('opacity', probe.comparison_block_reason({
            'enabled': True, 'visualEffects': [{'selector': '.btn', 'opacity': 0.55}]}))


if __name__ == '__main__':
    unittest.main()
