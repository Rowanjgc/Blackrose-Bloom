"""Guards the import-direction rule in engineering standards §2.3 (bra-lae)."""
import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src/blackrose'


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


if __name__ == '__main__':
    unittest.main()
