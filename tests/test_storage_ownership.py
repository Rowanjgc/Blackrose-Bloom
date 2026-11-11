"""Only storage/ touches the chat database (engineering standards §2.1, bra-a2r8).

Serialized persistence is Established: all chat-database access goes through the
Repository and its one-connection worker. Python privacy is a convention, so these
tests parse the source and reject the shortcuts that broke it before: running SQL
through ``repository._db``, importing private storage names, and opening another
SQLite connection.
"""
import ast
from pathlib import Path
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / 'src/blackrose'
STORAGE = PACKAGE / 'storage'
DATABASE_HANDLES = {'_db', '_conn'}
# Separate databases with their own documented owner, not the chat database.
OTHER_DATABASES = {
    'animation_recovery.py': 'auxiliary recovery ledger under the attachment store (architecture.md)',
}


def modules_outside_storage():
    for path in sorted(PACKAGE.rglob('*.py')):
        if STORAGE not in path.parents:
            yield path.relative_to(PACKAGE).as_posix(), ast.parse(path.read_text(encoding='utf-8'))


class StorageOwnershipTests(unittest.TestCase):
    def test_no_module_reaches_the_database_handle(self):
        found = [f'{name}:{node.lineno} .{node.attr}' for name, tree in modules_outside_storage()
                 for node in ast.walk(tree)
                 if isinstance(node, ast.Attribute) and node.attr in DATABASE_HANDLES]
        self.assertEqual(found, [], 'add a Repository method instead')

    def test_no_module_imports_private_storage_names(self):
        found = []
        for name, tree in modules_outside_storage():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and 'storage' in (node.module or '').split('.'):
                    found += [f'{name}:{node.lineno} {alias.name}' for alias in node.names
                              if alias.name.startswith('_')]
        self.assertEqual(found, [], 'make the name public in storage/ or add a Repository method')

    def test_only_documented_databases_open_sqlite_connections(self):
        found = [f'{name}:{node.lineno}' for name, tree in modules_outside_storage()
                 if name not in OTHER_DATABASES for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == 'connect' and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == 'sqlite3']
        self.assertEqual(found, [], 'chat-database access goes through storage/')


if __name__ == '__main__':
    unittest.main()
