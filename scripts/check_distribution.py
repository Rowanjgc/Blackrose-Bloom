"""Verify release archives and exercise a wheel outside the source checkout.

Requires uv, Node/npm, and registry access for the YAML/JSON validation
dependencies and locked Node packages; uv reuses its cache where it can. Installs
only the wheel and those dependencies in a fresh venv; GTK, model weights, and
live providers are unused.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import venv
import zipfile


SMOKE = '''
from pathlib import Path
import blackrose
from blackrose.resources import animation_runtime_dir, bundled_skills_dir
from blackrose.motion_canvas import compile_source, player_document

package = Path(blackrose.__file__).parent
assert package.is_relative_to(Path(__import__('sys').prefix)), package
skills = bundled_skills_dir()
assert skills.is_relative_to(package), skills
from blackrose.skill_execution import package_snapshot
files, digest, manifest = package_snapshot(skills / 'numeric-summary')
assert manifest['entry_points']['summarize'] == 'summarize.py'
assert (package / 'skill_script_launcher.py').is_file()
assert len(digest) == 64
assert not (package / '_bundled/animation-runtime/node_modules').exists()
source = (skills / 'explanatory-animations/references/gears.tsx').read_text()
compiled = compile_source(source)
assert 'BlackroseScene' in compiled
document = player_document({'version': 2, 'engine': 'motion-canvas',
    'title': 'Installed gears', 'description': 'Wheel smoke test', 'duration': 8,
    'source': source, 'compiled': compiled, 'loop': False})
assert 'BlackroseMotion' in document
assert 'allow-same-origin' not in document
assert animation_runtime_dir().is_dir()
from blackrose.ui_documents import available, contract, validate_document
from blackrose.diagrams import available as diagrams_available
import json
assert available()
assert diagrams_available()
validate_document(json.dumps(contract()['examples']['form']))
print('Installed wheel: bundled skill, real TSX compilation, and standalone player passed.')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='Directory containing uv build output')
    args = parser.parse_args()
    wheels = sorted(args.directory.glob('*.whl'))
    sources = sorted(args.directory.glob('*.tar.gz'))
    if len(wheels) != 1 or len(sources) != 1:
        parser.error('Expected exactly one wheel and one source archive')
    wheel, source = wheels[0].resolve(), sources[0].resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for suffix in ('/licenses/LICENSE', '/licenses/THIRD_PARTY_NOTICES.md'):
            if not any(name.endswith(suffix) for name in names):
                raise RuntimeError(f'Wheel is missing its project notice: {suffix}')
        for required in ('blackrose/_bundled/animation-runtime/compile.mjs',
                         'blackrose/skill_script_launcher.py',
                         'blackrose/_bundled/skills/numeric-summary/SKILL.md',
                         'blackrose/_bundled/skills/numeric-summary/execution.json',
                         'blackrose/_bundled/skills/numeric-summary/summarize.py',
                         'blackrose/_bundled/animation-runtime/diagnostics.mjs',
                         'blackrose/_bundled/animation-runtime/package-lock.json',
                         'blackrose/_bundled/animation-runtime/exports.json',
                         'blackrose/_bundled/skills/explanatory-animations/SKILL.md',
                         'blackrose/_bundled/skills/ian-xiaohei-illustrations/LICENSE',
                         'blackrose/gui/static/motion-canvas-runtime.js',
                         'blackrose/gui/static/motion-canvas-NOTICES.txt',
                         'blackrose/gui/static/ui-runtime.js',
                         'blackrose/gui/static/ui-contract.json',
                         'blackrose/gui/static/ui-NOTICES.txt',
                         'blackrose/gui/static/ui.css',
                         'blackrose/gui/static/diagrams.js',
                         'blackrose/gui/static/mermaid-host.html',
                         'blackrose/gui/static/mermaid-manifest.json',
                         'blackrose/gui/static/mermaid-NOTICES.txt',
                         'blackrose/gui/static/fonts/libron/LICENSE'):
            if required not in names:
                raise RuntimeError(f'Wheel is missing {required}')
        # Every bundled skill file ships, so `install-skill` copies complete packages.
        skills = Path(__file__).resolve().parents[1] / 'skills'
        for path in sorted(skills.rglob('*')):
            required = 'blackrose/_bundled/skills/' + path.relative_to(skills).as_posix()
            if path.is_file() and required not in names:
                raise RuntimeError(f'Wheel is missing {required}')
    with tarfile.open(source) as archive:
        source_names = archive.getnames()
        for suffix in ('/LICENSE', '/THIRD_PARTY_NOTICES.md', '/animation-runtime/exports.json',
                       '/src/blackrose/gui/static/motion-canvas-runtime.js',
                       '/src/blackrose/gui/static/motion-canvas-NOTICES.txt',
                       '/src/blackrose/gui/static/ui-runtime.js',
                       '/src/blackrose/gui/static/ui-contract.json',
                       '/src/blackrose/gui/static/ui-NOTICES.txt',
                       '/src/blackrose/gui/static/mermaid-host.html',
                       '/src/blackrose/gui/static/mermaid-manifest.json',
                       '/src/blackrose/gui/static/mermaid-NOTICES.txt',
                       '/diagram-runtime/package-lock.json'):
            if not any(name.endswith(suffix) for name in source_names):
                raise RuntimeError(f'Source archive is missing {suffix}')
        names += source_names
    for name in names:
        if set(Path(name).parts) & {'node_modules', '__pycache__', '.venv', 'artifacts'}:
            raise RuntimeError(f'Unexpected generated content in release: {name}')
    with tempfile.TemporaryDirectory(prefix='bloom-wheel-check-') as folder:
        root = Path(folder)
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        environment.pop('PYTHONHOME', None)
        environment.update({key: str(root / key.lower()) for key in (
            'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME')})
        venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(root / 'venv')
        python = root / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

        def run(command):
            subprocess.run([str(part) for part in command], cwd=root,
                           env=environment, check=True, timeout=360)

        # Keep uv's populated package cache even though application XDG paths
        # are isolated; this is a small Python install, with no model stack.
        cache = subprocess.check_output(['uv', 'cache', 'dir'], text=True).strip()
        environment['UV_CACHE_DIR'] = cache
        environment['UV_LINK_MODE'] = 'copy'
        run(['uv', 'pip', 'install', '--offline', '--python', python, '--no-deps', wheel])
        # Not --offline: `uv sync --locked` caches wheels without the index pages
        # that resolving these names needs, so a fresh CI cache cannot satisfy it.
        run(['uv', 'pip', 'install', '--python', python, 'pyyaml', 'jsonschema'])
        run([python, '-I', '-m', 'blackrose.main', 'setup-animations'])
        run([python, '-I', '-m', 'blackrose.main', 'install-skill', 'explanatory-animations'])
        run([python, '-I', '-m', 'blackrose.main', 'install-skill', 'research-synthesis'])
        run([python, '-I', '-c', SMOKE])
        # A fresh editable checkout must install before any Node build exists.
        checkout = root / 'checkout'
        with tarfile.open(source) as archive:
            archive.extractall(checkout, filter='data')
        project = next(checkout.iterdir())
        for relative in ('animation-runtime/exports.json',
                         'src/blackrose/gui/static/motion-canvas-runtime.js',
                         'src/blackrose/gui/static/motion-canvas-NOTICES.txt',
                         'src/blackrose/gui/static/ui-runtime.js',
                         'src/blackrose/gui/static/ui-contract.json',
                         'src/blackrose/gui/static/ui-NOTICES.txt',
                         'src/blackrose/gui/static/mermaid-host.html',
                         'src/blackrose/gui/static/mermaid-manifest.json',
                         'src/blackrose/gui/static/mermaid-NOTICES.txt'):
            (project / relative).unlink()
        run(['uv', 'pip', 'install', '--offline', '--python', python, '--no-deps', '--editable', project])
        run([python, '-I', '-c',
             'from blackrose.resources import resource_root; '
             'from blackrose.diagrams import available; assert not available(); '
             f'assert str(resource_root()) == {str(project)!r}; '
             'print("Fresh editable checkout installs without Node build output.")'])
    print('Distribution checks passed.', flush=True)


if __name__ == '__main__':
    main()
