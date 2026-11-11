"""Where attachment bytes live.

Attachments go to files under the data directory and their metadata goes to
SQLite. That split follows the shape the database already has -- it stores
what happened, not what was transferred -- and keeps a chat with a dozen
screenshots in it from turning every ``SELECT * FROM messages`` into a
multi-megabyte read.

Names are content addresses, never the user's filename. A file called
``../../.ssh/id_rsa.png`` is a perfectly ordinary thing for a user to have,
and the original name survives as a label in the database where it cannot be
mistaken for a path.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """A file the store owns."""

    # Relative to the store root, and the only form written to the database:
    # an absolute path would break the moment the data directory moved.
    relative_path: str
    digest: str
    size: int


class AttachmentStore:
    """Content-addressed files under one directory.

    Identical images share a file, which makes re-pasting the same screenshot
    free and makes writes idempotent. Deletion is therefore reference-aware
    and belongs to the controller's artifact lifecycle. The repository records
    cleanup obligations and knows which originals or extracted text still own it.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, relative_path: str) -> Path:
        """Resolve a stored path, refusing anything that escapes the root.

        The database is trusted, but a corrupted or hand-edited row must not
        be able to point the reader at an arbitrary file.
        """
        root = self._root.resolve()
        candidate = (root / relative_path).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"attachment path escapes the store: {relative_path!r}")
        return candidate

    def write(self, data: bytes, suffix: str) -> StoredBlob:
        """Store bytes under their digest. Writing the same file twice is free.

        The suffix is passed in rather than derived, because the store holds
        images, documents and the text extracted from them, and knowing how
        to name each of those is not the store's job.
        """
        digest = hashlib.sha256(data).hexdigest()
        # Two levels of fan-out: a flat directory of thousands of files is
        # slow to list on some filesystems and unpleasant to look at on all.
        relative = f"{digest[:2]}/{digest[2:4]}/{digest}{suffix}"
        target = self.path_for(relative)
        if target.exists():
            return StoredBlob(relative, digest, len(data))
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write must not leave a truncated file
        # at a name the database already points to.
        temporary = target.with_name(f".{target.name}.partial")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredBlob(relative, digest, len(data))

    def read(self, relative_path: str) -> bytes | None:
        """Read stored bytes back, or ``None`` if the file is gone."""
        try:
            return self.path_for(relative_path).read_bytes()
        except (OSError, ValueError) as exc:
            log.warning("could not read attachment %s: %s", relative_path, exc)
            return None

    def exists(self, relative_path: str) -> bool:
        try:
            return self.path_for(relative_path).is_file()
        except ValueError:
            return False

    def delete(self, relative_path: str) -> bool:
        """Remove a stored file. Already gone counts as success."""
        try:
            self.path_for(relative_path).unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            log.warning("could not delete attachment %s: %s", relative_path, exc)
            return False
        return True
