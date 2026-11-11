"""Safety checks for the native WebKit child-process probe."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_gui_web_process_recovery as probe


class WebProcessProbeSafetyTests(unittest.TestCase):
    def test_renderer_selection_requires_descendancy_from_scratch_probe(self):
        rows = {
            100: (1, 'python scratch probe'),
            101: (100, 'WebKitWebProcess /usr/libexec/WebKitWebProcess --web process'),
            102: (1, 'WebKitWebProcess /usr/libexec/WebKitWebProcess --other desktop'),
            103: (102, 'WebKitWebProcess /usr/libexec/WebKitWebProcess --shared sibling'),
            104: (101, 'WebKitNetworkProcess /usr/libexec/WebKitNetworkProcess --network'),
        }
        self.assertEqual(probe._renderer_descendants(rows, 100), [
            (101, 'WebKitWebProcess /usr/libexec/WebKitWebProcess --web process')])

    def test_renderer_selection_requires_exactly_the_expected_process_kind(self):
        rows = {100: (1, 'python scratch probe'),
                101: (100, 'WebKitNetworkProcess /usr/libexec/WebKitNetworkProcess')}
        self.assertEqual(probe._renderer_descendants(rows, 100), [])

    def test_renderer_selection_ignores_command_line_spoofing(self):
        rows = {100: (1, 'python scratch probe'),
                101: (100, 'python3 python3 scripts/probe.py WebKitWebProcess')}
        self.assertEqual(probe._renderer_descendants(rows, 100), [])


if __name__ == '__main__':
    unittest.main()
