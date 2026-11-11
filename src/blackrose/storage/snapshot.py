"""Read stopped SQLite storage without changing even its sidecar files."""
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3
import tempfile


@contextmanager
def readonly_snapshot(path):
    """Caller excludes writers for the entire read using desktop ownership.

    A cleanly stopped database needs no copy. An interrupted WAL/journal is read
    from a private copy, where SQLite may recover it without touching user files.
    Opening the source with mode=ro alone can still create WAL/SHM files.
    """
    sidecars = [Path(str(path) + suffix) for suffix in ('-wal', '-journal')]
    if not any(p.exists() and p.stat().st_size for p in sidecars):
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
            yield conn
    else:
        with tempfile.TemporaryDirectory(prefix='bloom-artifact-report-') as temporary:
            copy = Path(temporary) / path.name
            shutil.copyfile(path, copy)
            for source in sidecars:
                if source.exists():
                    shutil.copyfile(source, Path(str(copy) + source.name[len(path.name):]))
            with closing(sqlite3.connect(copy)) as conn:
                yield conn
