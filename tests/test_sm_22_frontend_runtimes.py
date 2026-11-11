"""SM-22 invariants: the browser and sandbox-iframe frontend state machines.

See ``docs/state-machines/sm-22-frontend-runtimes.md``. The four sub-machines
live in JavaScript, so the assertions run in node against the shipped files and a
minimal DOM (``tests/sm22_dom.cjs``): no browser, display, network or GTK is
needed, and each harness speaks the postMessage protocol itself.

Harnesses (run directly with ``node`` as well):

- ``tests/sm22_players.cjs``  animation players + Motion Canvas control bridge,
  Mermaid singleton and per-figure lifecycle.
- ``tests/sm22_ui_host.mjs``  Interactive UI host mount/flush/submit, bundled
  with esbuild and a recording react-dom root.
- ``tests/sm22_bloom.cjs``    the main page controller's locks, savers, popover
  epoch, dialog, sidebar, transcript paging/auto-scroll and image loader.

Offline: node is local, esbuild comes from ``ui-runtime/node_modules``, and the
UI artifact is read from the repository (built by ``npm run build --prefix
ui-runtime``; it is gitignored and present in a prepared checkout).
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'src/blackrose/gui/static'


def run_harness(name: str) -> str:
    result = subprocess.run(['node', str(ROOT / 'tests' / name)],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f'{name} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}')
    return result.stdout


@unittest.skipUnless(shutil.which('node'), 'the frontend runtimes are JavaScript and need node')
class PlayerStateMachineTests(unittest.TestCase):
    def test_animation_players_and_mermaid_lifecycles(self):
        """One PLAYING across both engines, boot/fail/retry/dispose, one renderer."""
        output = run_harness('sm22_players.cjs')
        self.assertIn('passed', output)


@unittest.skipUnless(shutil.which('node'), 'the frontend runtimes are JavaScript and need node')
class InteractiveUiHostTests(unittest.TestCase):
    def test_render_modes_flush_and_submit_locks(self):
        """250 ms debounce, serialised saves, sequence +1, and the submit lock."""
        output = run_harness('sm22_ui_host.mjs')
        self.assertIn('passed', output)


@unittest.skipUnless(shutil.which('node'), 'the frontend runtimes are JavaScript and need node')
class MainPageControllerTests(unittest.TestCase):
    def test_locks_savers_popover_dialog_sidebar_and_paging(self):
        """bloom.js turn/composer lock, savers, epochs, modal, drag, paging, images."""
        output = run_harness('sm22_bloom.cjs')
        self.assertIn('passed', output)


class LoadOrderTests(unittest.TestCase):
    """The page depends on a load order the modules never state themselves.

    blooms.js calls globalThis.BlackroseAnimations/Diagrams/UI/MotionPlayer at
    call time, diagrams.js resolves #column while it loads, and the UI runtime
    publishes the artifact the block's schema_hash is checked against.
    """

    def test_runtimes_load_before_the_page_controller(self):
        html = (STATIC / 'index.html').read_text()
        order = [html.index(f'src="{name}"') for name in
                 ('animations.js', 'motion-canvas-player.js', 'ui-runtime.js', 'diagrams.js', 'bloom.js')]
        self.assertEqual(order, sorted(order), 'runtimes must load before bloom.js')
        self.assertIn('id="column"', html)
        # bloom.js resolves .dock at load; diagrams.js resolves #column at load.
        self.assertLess(html.index('id="column"'), html.index('src="diagrams.js"'))

    def test_generated_code_stays_in_opaque_origin_frames(self):
        player = (STATIC / 'motion-canvas-player.js').read_text()
        diagrams = (STATIC / 'diagrams.js').read_text()
        self.assertIn("setAttribute('sandbox', 'allow-scripts')", player)
        self.assertIn("setAttribute('sandbox', 'allow-scripts')", diagrams)
        # Neither sandboxed frame may receive a bridge: the control channel is a
        # postMessage allow-list, not window.pywebview.
        self.assertNotIn('pywebview', player)
        self.assertNotIn('pywebview', diagrams)
        self.assertNotIn('pywebview', (STATIC / 'motion-canvas-frame.js').read_text())


if __name__ == '__main__':
    unittest.main()
