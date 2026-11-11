"""Offline artifact inspection/repair without constructing inference services."""
import json
import sqlite3
import sys

from .animation_recovery import AnimationRecovery
from .artifacts import ArtifactLifecycle, reconciliation
from .attachments import AttachmentStore
from .config import data_dir
from .instance import AlreadyRunning, desktop_instance
from .storage.db import Database, StorageError
from .storage.migrations import MIGRATIONS
from .storage.repository import Repository, read_ownership_inventory
from .storage.snapshot import readonly_snapshot


def read_inventory(path):
    with readonly_snapshot(path) as connection:
        version = connection.execute('SELECT MAX(version) FROM schema_metadata').fetchone()[0]
        if version is None or version > max(version for version, _ in MIGRATIONS):
            raise StorageError('database schema is newer than this application')
        return read_ownership_inventory(connection)


async def run(action=None):
    root = data_dir()
    path = root / 'chats.db'
    store = AttachmentStore(root / 'attachments')
    try:
        if action not in (None, 'repair'):
            raise ValueError('artifacts accepts only the optional repair action')
        if not path.is_file():
            raise ValueError(f'Chat database does not exist: {path}')
        if action is None:
            # Even the lock is opened read-only. Legacy/copied data without one
            # needs an empty desktop.lock created explicitly before inspection.
            with desktop_instance(root / 'desktop.lock', create=False):
                report = reconciliation(store, read_inventory(path))
            print(json.dumps(report, indent=2))
            return 0
        with desktop_instance(root / 'desktop.lock') as ownership:
            read_inventory(path)  # Refuse unknown schemas before allowing migration.
            database = Database(path)
            try:
                await database.connect()
                lifecycle = ArtifactLifecycle(Repository(database), store,
                    AnimationRecovery(store.root / 'animation-recovery.sqlite3'))
                async with lifecycle.exclusive():
                    result = await lifecycle.repair(ownership)
                print(json.dumps(result, indent=2))
                return 1 if result['cleanup']['pending'] or result['after']['recovery']['error'] else 0
            finally:
                await database.close()
    except (OSError, ValueError, sqlite3.Error, StorageError, AlreadyRunning) as error:
        print(str(error), file=sys.stderr)
        return 1
