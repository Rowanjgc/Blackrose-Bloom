"""Validate an animation scene and export a portable player without starting Blackrose."""
from __future__ import annotations

import argparse
from pathlib import Path

from blackrose.animations import export_html, load_scene


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scene', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        document = export_html(load_scene(args.scene))
        args.output.write_text(document, encoding='utf-8')
    except (OSError, ValueError, RecursionError) as error:
        parser.exit(1, f'Animation export failed: {error}\n')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
