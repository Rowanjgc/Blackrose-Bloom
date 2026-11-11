"""Refuse incomplete release archives; editable installs need no Node build."""
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == 'editable':
            return
        root = Path(self.root)
        for relative in ('animation-runtime/exports.json',
                         'src/blackrose/gui/static/motion-canvas-NOTICES.txt',
                         'src/blackrose/gui/static/motion-canvas-runtime.js'):
            if not (root / relative).is_file():
                raise RuntimeError('Build Motion Canvas before packaging: '
                                   'npm ci --prefix animation-runtime && '
                                   'npm run build --prefix animation-runtime')
        if self.target_name == 'wheel':
            build_data['force_include'][str(root / 'skills')] = 'blackrose/_bundled/skills'
            for filename in ('package.json', 'package-lock.json', 'compile.mjs',
                             'diagnostics.mjs', 'exports.json'):
                build_data['force_include'][str(root / 'animation-runtime' / filename)] = (
                    f'blackrose/_bundled/animation-runtime/{filename}')
        import hashlib
        import json
        static = root / 'src/blackrose/gui/static'
        required = ('ui-runtime.js', 'ui-contract.json', 'ui-NOTICES.txt')
        if any(not (static / name).is_file() for name in required):
            raise RuntimeError('Build interactive UI before packaging: '
                               'npm ci --prefix ui-runtime && npm run build --prefix ui-runtime')
        contract = json.loads((static / 'ui-contract.json').read_text())
        if hashlib.sha256((static / 'ui-runtime.js').read_bytes()).hexdigest() != contract['bundle_hash']:
            raise RuntimeError('Interactive UI bundle and schema disagree; rebuild ui-runtime')
        sources = ('contract.mjs', 'library.jsx', 'host.jsx', 'build.mjs', 'package.json', 'package-lock.json')
        digest = hashlib.sha256(b''.join((root / 'ui-runtime' / name).read_bytes() for name in sources)).hexdigest()
        if digest != contract['source_hash']:
            raise RuntimeError('Interactive UI sources changed; rebuild ui-runtime before packaging')
        expected = contract.pop('artifact_hash')
        packed = json.dumps(contract, ensure_ascii=False, separators=(',', ':')).encode()
        if hashlib.sha256(packed).hexdigest() != expected:
            raise RuntimeError('Interactive UI prompt/schema metadata changed; rebuild ui-runtime')
        required = ('mermaid-host.html', 'mermaid-manifest.json', 'mermaid-NOTICES.txt')
        if any(not (static / name).is_file() for name in required):
            raise RuntimeError('Build Mermaid before packaging: '
                               'npm ci --prefix diagram-runtime && npm run build --prefix diagram-runtime')
        manifest = json.loads((static / 'mermaid-manifest.json').read_text())
        sources = ('host.js', 'build.mjs', 'package.json', 'package-lock.json')
        digest = hashlib.sha256(b''.join((root / 'diagram-runtime' / name).read_bytes()
                                       for name in sources)).hexdigest()
        if digest != manifest['source_hash']:
            raise RuntimeError('Mermaid sources changed; rebuild diagram-runtime before packaging')
        for filename, key in (('mermaid-host.html', 'host_hash'), ('mermaid-NOTICES.txt', 'notices_hash')):
            if hashlib.sha256((static / filename).read_bytes()).hexdigest() != manifest[key]:
                raise RuntimeError('Mermaid assets disagree; rebuild diagram-runtime before packaging')
