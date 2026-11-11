"""scripts/ and experiments/ stay fully indexed, and the probe groups match their sources."""
import ast
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
_ENTRY = re.compile(r'^\|\s*\[`[^`]+`\]\(([^)\s]+)\)')

spec = importlib.util.spec_from_file_location('check_health', ROOT / 'scripts/check_health.py')
check_health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_health)


def indexed(readme):
    """{section heading: [linked paths]} for each table row in an index."""
    sections, current = {}, None
    for line in readme.read_text(encoding='utf-8').splitlines():
        if line.startswith('## '):
            current = line[3:].strip()
            sections[current] = []
        elif (match := _ENTRY.match(line)) and current:
            sections[current].append(match.group(1))
    return sections


def files_under(directory):
    return sorted(path.relative_to(directory).as_posix() for path in directory.rglob('*')
                  if path.is_file() and path.name != 'README.md'
                  and '__pycache__' not in path.parts and 'node_modules' not in path.parts)


class ScriptIndexTests(unittest.TestCase):
    def assert_complete(self, directory):
        listed = [path for paths in indexed(directory / 'README.md').values() for path in paths]
        self.assertEqual(len(listed), len(set(listed)), 'a file is listed twice')
        present = files_under(directory)
        self.assertEqual(sorted(set(present) - set(listed)), [], 'unlisted; add to README.md')
        self.assertEqual(sorted(set(listed) - set(present)), [], 'listed but missing')

    def test_every_script_is_indexed(self):
        self.assert_complete(ROOT / 'scripts')

    def test_every_experiment_is_indexed(self):
        self.assert_complete(ROOT / 'experiments')

    def test_every_python_script_states_its_purpose(self):
        for directory in (ROOT / 'scripts', ROOT / 'experiments'):
            for path in directory.rglob('*.py'):
                with self.subTest(path=path.relative_to(ROOT).as_posix()):
                    self.assertTrue(ast.get_docstring(ast.parse(path.read_text(encoding='utf-8'))))

    def test_native_probe_group_is_the_health_gate_list(self):
        listed = indexed(ROOT / 'scripts/README.md')['Native probes in the health gate']
        self.assertEqual(sorted(listed), sorted({name for name, *_options in check_health.GUI_PROBES}))

    def test_browser_probe_group_is_the_test_browser_script(self):
        command = json.loads((ROOT / 'package.json').read_text())['scripts']['test:browser']
        expected = re.findall(r'node scripts/(\S+)', command)
        self.assertEqual(sorted(indexed(ROOT / 'scripts/README.md')['Browser probes']), sorted(expected))


if __name__ == '__main__':
    unittest.main()
