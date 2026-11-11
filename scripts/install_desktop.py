"""Install this checkout in the current user's application launcher."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess


def install(data_home: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    applications = data_home / 'applications'
    icons = data_home / 'icons'
    applications.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)
    icon = icons / 'blackrose-bloom.png'
    shutil.copyfile(root / 'src/blackrose/gui/static/blackrose-bloom.png', icon)
    icon.chmod(0o644)
    # Desktop Exec quoting has its own escaping rules; no shell is invoked.
    escapes = {char: '\\' * 2 + char for char in ('"', '`', '$')}
    escapes.update({'\\': '\\' * 4, '%': '%%'})
    executable = ''.join(escapes.get(char, char) for char in str(root / 'run'))
    entry = applications / 'blackrose-bloom.desktop'
    entry.write_text(
        '[Desktop Entry]\n'
        'Type=Application\n'
        'Name=Blackrose Bloom\n'
        'Comment=Local AI assistant with knowledge, images, and animations\n'
        f'Exec="{executable}"\n'
        f'Icon={icon}\n'
        'Terminal=false\n'
        'Categories=Utility;\n'
        'Keywords=AI;Chat;Assistant;Blackrose;\n'
        'StartupNotify=true\n'
        'StartupWMClass=blackrose-bloom\n', encoding='utf-8')
    entry.chmod(0o644)
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(applications)], check=True)
    return entry


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-home', type=Path,
                        default=Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share'),
                        help='Application data directory (defaults to XDG_DATA_HOME)')
    args = parser.parse_args()
    print(f'Installed launcher: {install(args.data_home.expanduser().resolve())}')
