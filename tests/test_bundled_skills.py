"""Shipped skill packages: well-formed, installable, selectable and progressively loaded."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blackrose.chat import ChatController
from blackrose.config import Config
from blackrose.main import main
from blackrose.prompts import SELECTED_SKILL_END, SELECTED_SKILL_START, DraftMessage, PromptBuilder
from blackrose.resources import bundled_skills_dir
from blackrose.skills import CATALOG_DESCRIPTION_CHARS, SkillManager

STARTER_SKILLS = ('creative-workshop', 'learning-partner', 'research-synthesis',
                  'thinking-partner', 'writing-revision')
LINK = re.compile(r'\]\(([^)#\s]+)\)')


def packages():
    return sorted(path for path in bundled_skills_dir().iterdir() if (path / 'SKILL.md').is_file())


class BundledPackageTests(unittest.TestCase):
    def test_packages_have_matching_identity_and_starter_descriptions_fit_the_catalog(self):
        names = [path.name for path in packages()]
        self.assertTrue(set(STARTER_SKILLS) <= set(names))
        for package in packages():
            with self.subTest(skill=package.name):
                meta = SkillManager.metadata(package / 'SKILL.md')
                self.assertEqual(meta['name'], package.name)
                self.assertTrue(meta['description'].strip())
                if package.name in STARTER_SKILLS:
                    # Discovery sees only the catalog description; truncation would hide triggers.
                    self.assertLessEqual(len(meta['description']), CATALOG_DESCRIPTION_CHARS)

    def test_resource_links_resolve_inside_the_package_and_starter_resources_are_indexed(self):
        for package in packages():
            text = (package / 'SKILL.md').read_text()
            linked = set()
            for target in LINK.findall(text):
                if '://' in target:
                    continue
                resolved = (package / target).resolve()
                with self.subTest(skill=package.name, link=target):
                    self.assertTrue(resolved.is_relative_to(package.resolve()))
                    self.assertTrue(resolved.is_file())
                linked.add(resolved)
            if package.name in STARTER_SKILLS:
                # Every resource is reachable from SKILL.md, so progressive reads can find it.
                resources = {p.resolve() for p in package.rglob('*') if p.is_file() and p.name != 'SKILL.md'}
                self.assertEqual(resources, linked, package.name)
                self.assertIn(package / 'references/examples.md', {p for p in package.rglob('*.md')})

    def test_starter_skills_point_to_real_neighbors_for_other_needs(self):
        ids = {path.name for path in packages()}
        for skill in STARTER_SKILLS:
            text = (bundled_skills_dir() / skill / 'SKILL.md').read_text()
            # Neighbors are named in parentheses so their triggers stay distinguishable.
            named = set(re.findall(r'\(([a-z]+(?:-[a-z]+)+)\)', text))
            with self.subTest(skill=skill):
                self.assertGreaterEqual(len(named & set(STARTER_SKILLS)), 2)
                self.assertNotIn(skill, named)
                self.assertLessEqual(named, ids)


class StarterSkillInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / 'data/blackrose-bloom/skills'
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'data')}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for skill in STARTER_SKILLS:
                self.assertEqual(main(['install-skill', skill]), 0)
        self.manager = SkillManager(self.data)

    def test_install_skill_publishes_reviewed_enabled_packages_with_full_catalog_entries(self):
        listed = {s['id']: s for s in self.manager.list()}
        self.assertEqual(set(listed), set(STARTER_SKILLS))
        for skill in STARTER_SKILLS:
            with self.subTest(skill=skill):
                self.assertEqual((listed[skill]['enabled'], listed[skill]['review']), (True, 'approved'))
                source = bundled_skills_dir() / skill
                for path in source.rglob('*'):
                    if path.is_file():
                        self.assertEqual((self.data / skill / path.relative_to(source)).read_bytes(),
                                         path.read_bytes())
        catalog = json.loads(self.manager.catalog().splitlines()[1])
        for entry in catalog:
            self.assertEqual(entry['description'], SkillManager.metadata(bundled_skills_dir() / entry['id'] / 'SKILL.md')['description'])

    def test_explicit_selection_loads_instructions_but_not_resource_bodies(self):
        for skill in STARTER_SKILLS:
            with self.subTest(skill=skill):
                prompt = PromptBuilder(Config(), self.manager, None, {}).build(
                    [], DraftMessage(f'Use ${skill} for this.', refs=({'kind': 'skill', 'id': skill},)))
                request = prompt.messages[-1]['content']
                start = request.index(SELECTED_SKILL_START) + len(SELECTED_SKILL_START) + 1
                loaded = json.loads(request[start:request.index('\n' + SELECTED_SKILL_END)])
                self.assertEqual(loaded['text'], (bundled_skills_dir() / skill / 'SKILL.md').read_text())
                self.assertEqual(loaded['resources'], [{'path': 'references', 'kind': 'directory'}])
                for resource in (bundled_skills_dir() / skill / 'references').iterdir():
                    # A distinctive line from each resource body stays out until read_skill asks.
                    self.assertNotIn(resource.read_text().splitlines()[2], request)
                self.assertNotIn(skill, prompt.messages[0]['content'])
                # Starter identities load no host contract or extra tools.
                self.assertFalse(prompt.xiaohei)
                self.assertFalse(prompt.animations_enabled)
                self.assertTrue(prompt.skills_available)

    def test_read_skill_pages_index_then_returns_only_the_requested_reference(self):
        for skill in STARTER_SKILLS:
            with self.subTest(skill=skill):
                index = self.manager.read(skill, 'references')
                self.assertNotIn('text', index)
                paths = [entry['path'] for entry in index['resources']]
                self.assertIn('references/examples.md', paths)
                for path in paths:
                    result = self.manager.read(skill, path)
                    self.assertEqual(result['text'], (bundled_skills_dir() / skill / path).read_text())

    async def resolve(self, text):
        stub = SimpleNamespace(skills=self.manager)

        async def resolve_references(refs):
            return list(refs)
        stub.resolve_references = resolve_references
        return await ChatController.resolve_skill_tokens(stub, text, [])

    def test_typed_tokens_select_each_starter_skill(self):
        import asyncio
        for skill in STARTER_SKILLS:
            with self.subTest(skill=skill):
                self.assertEqual(asyncio.run(self.resolve(f'Help with this using ${skill} please')),
                                 [{'kind': 'skill', 'id': skill}])
        self.manager.set_enabled('thinking-partner', False)
        self.assertEqual(asyncio.run(self.resolve('Use $thinking-partner')), [])


if __name__ == '__main__':
    unittest.main()
