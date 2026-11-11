"""Installed resources must not depend on, or write into, a source checkout."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from blackrose import resources
from blackrose.main import main
from blackrose.skills import SkillManager


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.bundle = self.root / '_bundled'
        self.template = self.bundle / 'animation-runtime'
        self.template.mkdir(parents=True)
        (self.template / 'package-lock.json').write_text('{"version": 1}')

    def test_wheel_cache_tracks_bundled_content_and_respects_xdg(self):
        with patch.object(resources, 'resource_root', return_value=self.bundle), \
                patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache')}):
            first = resources.animation_runtime_dir()
            self.assertTrue(first.is_relative_to(self.root / 'cache'))
            self.assertFalse(first.exists())
            self.assertEqual(first, resources.animation_runtime_dir())
            (self.template / 'package-lock.json').write_text('{"version": 2}')
            self.assertNotEqual(first, resources.animation_runtime_dir())

    def test_failed_install_does_not_publish_partial_cache(self):
        with patch.object(resources, 'resource_root', return_value=self.bundle), \
                patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache')}), \
                patch.object(resources.shutil, 'which', return_value='/usr/bin/npm'), \
                patch.object(resources.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'npm')):
            destination = resources.animation_runtime_dir()
            with self.assertRaises(subprocess.CalledProcessError):
                resources.setup_animation_runtime()
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.iterdir()), [])
            self.assertEqual(list(self.template.iterdir()), [self.template / 'package-lock.json'])

    def test_completed_install_is_published_and_reused_without_reinstalling(self):
        def install(command, *, cwd, **kwargs):
            for dependency in ('esbuild', 'typescript'):
                (cwd / 'node_modules' / dependency).mkdir(parents=True)

        with patch.object(resources, 'resource_root', return_value=self.bundle), \
                patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache')}), \
                patch.object(resources.shutil, 'which', return_value='/usr/bin/npm'), \
                patch.object(resources.subprocess, 'run', side_effect=install) as run:
            installed = resources.setup_animation_runtime()
            self.assertTrue((installed / 'node_modules/esbuild').is_dir())
            self.assertEqual(resources.setup_animation_runtime(), installed)
            self.assertEqual(run.call_count, 1)
            self.assertFalse((self.template / 'node_modules').exists())

    def test_incomplete_existing_cache_is_reported_without_installing_over_it(self):
        with patch.object(resources, 'resource_root', return_value=self.bundle), \
                patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache')}), \
                patch.object(resources.shutil, 'which', return_value='/usr/bin/npm'), \
                patch.object(resources.subprocess, 'run') as run:
            destination = resources.animation_runtime_dir()
            destination.mkdir(parents=True)
            (destination / 'keep.txt').write_text('Keep this')
            with self.assertRaisesRegex(ValueError, 'Incomplete animation cache'):
                resources.setup_animation_runtime()
            run.assert_not_called()
            self.assertEqual((destination / 'keep.txt').read_text(), 'Keep this')

    def test_explicit_skill_install_copies_resources_and_preserves_existing_skill(self):
        skills = self.bundle / 'skills'
        skill = skills / 'sample'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: sample\ndescription: Fixture\n---\nInstructions')
        (skill / 'reference.txt').write_text('Reference')
        with patch.object(resources, 'bundled_skills_dir', return_value=skills), \
                patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'data')}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['install-skill', 'sample']), 0)
            installed = self.root / 'data/blackrose-bloom/skills/sample/reference.txt'
            self.assertEqual(installed.read_text(), 'Reference')
            # Bundled skills ship reviewed with the application source.
            managed = SkillManager(self.root / 'data/blackrose-bloom/skills')
            self.assertEqual([(s['id'], s['enabled'], s['review']) for s in managed.list()],
                             [('sample', True, 'approved')])
            self.assertEqual(json.loads(managed.reviews_path.read_text())['sample']['origin'], 'bundled')
            installed.write_text('Local revision')
            self.assertEqual(main(['install-skill', 'sample']), 1)
            self.assertEqual(installed.read_text(), 'Local revision')
            with self.assertRaises(SystemExit):
                main(['install-skill', '../sample'])
