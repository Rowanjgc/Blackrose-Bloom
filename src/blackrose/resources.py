"""Locate shipped resources and explicitly prepare the optional Node compiler."""
from __future__ import annotations

import hashlib
import errno
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def resource_root() -> Path:
    packaged = Path(__file__).parent / '_bundled'
    return packaged if packaged.is_dir() else Path(__file__).resolve().parents[2]


def bundled_skills_dir() -> Path:
    return resource_root() / 'skills'


def animation_runtime_dir() -> Path:
    root = resource_root()
    if root.name != '_bundled':
        return root / 'animation-runtime'
    template = root / 'animation-runtime'
    digest = hashlib.sha256()
    for path in sorted(template.iterdir()):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return cache / 'blackrose-bloom' / 'animation-runtime' / digest.hexdigest()[:16]


def setup_animation_runtime() -> Path:
    """Install pinned Node packages only on an explicit user command.

    Wheel resources stay read-only. Stage a complete compiler in the user cache
    before making it visible, so interrupted installs cannot become active.
    """
    npm = shutil.which('npm')
    if not npm or not shutil.which('node'):
        raise ValueError('Install Node.js 22+ and npm, then run blackrose setup-animations.')
    root = resource_root()
    destination = animation_runtime_dir()
    if root.name != '_bundled':
        subprocess.run([npm, 'ci'], cwd=destination, check=True, timeout=300)
        subprocess.run([npm, 'run', 'build'], cwd=destination, check=True, timeout=120)
        return destination
    if destination.exists():
        if not (destination / 'node_modules/esbuild').is_dir() or not (destination / 'node_modules/typescript').is_dir():
            raise ValueError(f'Incomplete animation cache at {destination}; remove it and rerun setup-animations.')
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.setup-') as folder:
        staged = Path(folder) / 'runtime'
        shutil.copytree(root / 'animation-runtime', staged)
        subprocess.run([npm, 'ci'], cwd=staged, check=True, timeout=300)
        # Another explicit setup may have completed while npm was running.
        try:
            staged.rename(destination)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            if not (destination / 'node_modules/esbuild').is_dir() or not (destination / 'node_modules/typescript').is_dir():
                raise ValueError(f'Incomplete animation cache at {destination}.') from None
    return destination
