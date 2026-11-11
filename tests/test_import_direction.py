"""Guards the import-direction rule in engineering standards §2.3 (bra-lae)."""
import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src/blackrose'


def module_name(path):
    parts = list(path.relative_to(SOURCE).with_suffix('').parts)
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


def all_imports():
    """{module: modules it imports from the package}, at any nesting level."""
    found = {}
    for path in SOURCE.rglob('*.py'):
        name = module_name(path)
        package = name.split('.') if path.name == '__init__.py' else name.split('.')[:-1]
        targets = set()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = package[:len(package) - (node.level - 1)]
                target = '.'.join(base + ([node.module] if node.module else []))
                targets.update([target] if node.module else [f'{target}.{a.name}' for a in node.names])
        found[name] = targets
    return found


def importers(of, imports):
    return sorted(m for m, targets in imports.items()
                  if m != of and any(t == of or t.startswith(of + '.') for t in targets))


def imported_modules(name):
    """Every module the file imports, at any nesting level."""
    tree = ast.parse((SOURCE / f'{name}.py').read_text(encoding='utf-8'))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            found.add(node.module)
    return found


class ImportDirectionTests(unittest.TestCase):
    def test_animations_does_not_import_tools(self):
        # Previously a function-local import hid the tools <-> animations cycle.
        self.assertNotIn('tools', imported_modules('animations'))

    def test_citations_does_not_import_tool_execution(self):
        # The result-size budget lives in tools.py, so citations no longer reaches up.
        self.assertNotIn('tool_execution', imported_modules('citations'))


class LayerTests(unittest.TestCase):
    """§2.3: lower layers never reach the controller, session or page bridge for policy."""

    @classmethod
    def setUpClass(cls):
        cls.imports = all_imports()

    def test_only_the_shell_and_runtime_import_the_controller(self):
        allowed = {'runtime', 'gui.bridge', 'gui.session'}
        self.assertLessEqual(set(importers('chat', self.imports)), allowed)

    def test_the_desktop_shell_is_imported_only_from_gui_and_main(self):
        # gui/model_names.py is a pure display-name formatter with no window state.
        outside = [m for m in importers('gui', self.imports)
                   if not m.startswith('gui') and m != 'main'
                   and {t for t in self.imports[m] if t.startswith('gui')} != {'gui.model_names'}]
        self.assertEqual(outside, [])

    def test_storage_reaches_outside_itself_only_for_pure_helpers(self):
        # Answer digests and diagram token edits run inside storage transactions.
        allowed = {'citations', 'diagrams'}
        reached = {t.split('.')[0] for m, targets in self.imports.items() if m.startswith('storage')
                   for t in targets if not t.startswith('storage')}
        self.assertLessEqual(reached, allowed)

    def test_adapters_never_import_turn_or_desktop_policy(self):
        policy = ('chat', 'turns', 'gui', 'tool_execution', 'tool_host')
        for layer in ('backend', 'web', 'attachments', 'storage'):
            with self.subTest(layer=layer):
                reached = [f'{m} -> {t}' for m, targets in self.imports.items() if m.startswith(layer)
                           for t in targets if t.split('.')[0] in policy]
                self.assertEqual(reached, [])


if __name__ == '__main__':
    unittest.main()
