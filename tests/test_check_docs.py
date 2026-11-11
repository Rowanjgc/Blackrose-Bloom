"""The documentation check reports each kind of stale reference."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'check_docs', Path(__file__).resolve().parents[1] / 'scripts/check_docs.py')
check_docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_docs)


def never_ignored(_root, _relative):
    return False


class DocumentationCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'docs').mkdir()
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_thing.py').write_text(
            'class ThingTests:\n    def test_survives_restart(self):\n        pass\n')
        (self.root / 'docs/guide.md').write_text('# Guide\n\n## Set up\n\n## Set up\n')

    def tearDown(self):
        self.directory.cleanup()

    def problems(self, text):
        (self.root / 'docs/page.md').write_text(text)
        return check_docs.check(self.root, ['docs/page.md'], ignored=never_ignored)

    def test_valid_links_paths_and_test_references_pass(self):
        self.assertEqual(self.problems(
            '[guide](guide.md#set-up) and [again](guide.md#set-up-1) and [web](https://x.invalid/a.py:3)\n'
            '`tests/test_thing.py` `test_thing.py::ThingTests::test_survives_restart`\n'
            '```sh\n[broken](nowhere.md) `tests/missing.py` main.py:12\n```\n'), [])

    def test_missing_link_target_and_anchor_are_reported(self):
        problems = self.problems('[a](nowhere.md) [b](guide.md#tear-down)\n')
        self.assertEqual(len(problems), 2)
        self.assertIn('missing file nowhere.md', problems[0])
        self.assertIn('missing anchor guide.md#tear-down', problems[1])

    def test_missing_backticked_path_is_reported_unless_git_ignores_it(self):
        self.assertIn('missing path `tests/gone.py`', self.problems('`tests/gone.py`\n')[0])
        (self.root / 'docs/page.md').write_text('`src/generated.js`\n')
        self.assertEqual(check_docs.check(self.root, ['docs/page.md'],
                                          ignored=lambda _root, _path: True), [])

    def test_every_dotted_part_of_a_reference_must_be_defined(self):
        problems = self.problems('`test_thing.py::RenamedTests.test_survives_restart`\n')
        self.assertEqual(len(problems), 1)
        self.assertIn('names nothing defined', problems[0])

    def test_reference_to_an_undefined_test_is_reported(self):
        problems = self.problems('`tests/test_thing.py::test_was_renamed`\n')
        self.assertEqual(len(problems), 1)
        self.assertIn('names nothing defined', problems[0])

    def test_every_line_number_reference_is_reported_with_its_location(self):
        problems = self.problems('See `tests/test_thing.py:2`.\n\nAnd a.py:1 or b.js:20-24.\n')
        self.assertEqual(len(problems), 3)
        self.assertTrue(problems[0].startswith('docs/page.md:1: line-number reference tests/test_thing.py:2'))
        self.assertTrue(problems[2].startswith('docs/page.md:3: line-number reference b.js:20'))

    def test_host_and_port_are_not_line_references(self):
        self.assertEqual(self.problems('Serve on localhost:8080 or 127.0.0.1:9000.\n'), [])


class PythonCommentCheckTests(unittest.TestCase):
    def problems(self, source):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'module.py').write_text(source)
            return check_docs.check_python_file(root, 'module.py')

    def test_line_references_in_comments_and_docstrings_are_reported(self):
        problems = self.problems(
            '"""Module, see turns.py:358."""\n'
            'class Thing:\n'
            '    """Mirrors\n'
            '    repository.py:791-794."""\n'
            '    def run(self):\n'
            '        return 1  # chat.py:231 handles this\n')
        self.assertEqual([p.split(';')[0] for p in problems], [
            'module.py:1: line-number reference turns.py:358',
            'module.py:4: line-number reference repository.py:791',
            'module.py:6: line-number reference chat.py:231'])

    def test_code_strings_symbols_and_urls_are_not_reported(self):
        self.assertEqual(self.problems(
            '"""Cite ``Repository.finish_message`` or https://x.invalid/a.py:3."""\n'
            'LOCATION = "main.py:12"\n'
            'def f():\n'
            '    return "b.js:20"  # see storage.snapshot.readonly_snapshot\n'), [])


if __name__ == '__main__':
    unittest.main()
