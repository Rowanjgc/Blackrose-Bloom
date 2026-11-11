"""The minimal-install CLI path imports no undeclared third-party packages.

scripts/check_distribution.py installs the wheel with only pyyaml and jsonschema,
then runs setup-animations, install-skill and an import smoke test. That check
needs a package build and network access, so it runs only in CI. This test
reproduces its import surface offline: a module-level import of any other
third-party package on these paths fails here instead of after a push.
"""
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

SRC = Path(__file__).resolve().parents[1] / 'src'
# The modules check_distribution.py loads through the CLI and its smoke script.
MODULES = ('blackrose.main', 'blackrose.resources', 'blackrose.config', 'blackrose.skills',
           'blackrose.motion_canvas', 'blackrose.skill_execution', 'blackrose.ui_documents',
           'blackrose.diagrams')
# pyyaml and jsonschema, plus jsonschema's own dependencies.
ALLOWED = {'blackrose', 'yaml', '_yaml', 'jsonschema', 'jsonschema_specifications',
           'referencing', 'rpds', 'attr', 'attrs', 'typing_extensions'}

PROBE = textwrap.dedent('''
    import importlib, importlib.abc, sys
    allowed = set(sys.argv[2].split(',')) | set(sys.stdlib_module_names)
    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split('.')[0] not in allowed:
                raise ModuleNotFoundError(f'not installed by check_distribution.py: {name}', name=name)
    sys.meta_path.insert(0, Block())
    sys.path.insert(0, sys.argv[1])
    for module in sys.argv[3].split(','):
        importlib.import_module(module)
''')


class MinimalInstallTests(unittest.TestCase):
    def test_cli_path_imports_only_the_minimal_dependencies(self):
        result = subprocess.run(
            [sys.executable, '-I', '-c', PROBE, str(SRC), ','.join(sorted(ALLOWED)), ','.join(MODULES)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
