"""One desktop owns the shared runtime and local router lifecycle at a time."""
import fcntl
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import IO

from .config import data_dir


class AlreadyRunning(RuntimeError):
    pass


@dataclass
class DesktopOwnership:
    path: Path
    handle: IO[str]
    active: bool = True

    def require(self, database_path):
        if (not self.active or self.handle.closed or database_path == ':memory:'
                or self.path.resolve() != (Path(database_path).parent / 'desktop.lock').resolve()):
            raise RuntimeError('Artifact repair requires ownership of this chat database.')


@contextmanager
def desktop_instance(path: Path | None = None, *, create: bool = True):
    path = path if path is not None else data_dir() / 'desktop.lock'
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    # Do not unlink this file: another process may already hold its inode open.
    with path.open('a' if create else 'r') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning('Blackrose Bloom is already running. Close its other window first.') from None
        ownership = DesktopOwnership(path, handle)
        try:
            yield ownership
        finally:
            ownership.active = False
            fcntl.flock(handle, fcntl.LOCK_UN)
