"""The page's live-region forwarding reaches the native window bounded and validated (bra-max)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from blackrose.gui import announcements
from blackrose.gui.bridge import Api


def bare_api(window=None) -> Api:
    api = Api.__new__(Api)  # announce() needs only the attached window
    api._window = window
    return api


class CleanTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(announcements.clean('  Reply\n complete\t '), 'Reply complete')

    def test_bounds_length(self):
        self.assertEqual(len(announcements.clean('x' * 5000)), announcements.MAX_ANNOUNCEMENT)

    def test_refuses_empty_and_non_text(self):
        for value in ('', '  \n', None, 42, ['Reply complete'], {'text': 'x'}):
            with self.subTest(value=value):
                self.assertIsNone(announcements.clean(value))


class AnnounceTests(unittest.TestCase):
    def test_queues_cleaned_text_with_politeness(self):
        window = object()
        api = bare_api(window)
        with patch.object(announcements, 'announce', return_value=True) as announce:
            self.assertTrue(api.announce(' Reply  complete ', False))
            self.assertTrue(api.announce('Reply failed', True))
        self.assertEqual(announce.call_args_list[0].args, (window, 'Reply complete', False))
        self.assertEqual(announce.call_args_list[1].args, (window, 'Reply failed', True))

    def test_refuses_bad_values_without_queueing(self):
        api = bare_api(object())
        with patch.object(announcements, 'announce') as announce:
            self.assertFalse(api.announce('', False))
            self.assertFalse(api.announce(7, False))
            self.assertFalse(api.announce('text', 'yes'))
            self.assertFalse(api.announce('text', 1))
        announce.assert_not_called()

    def test_refuses_before_a_window_is_attached(self):
        with patch.object(announcements, 'announce') as announce:
            self.assertFalse(bare_api().announce('Reply complete'))
        announce.assert_not_called()


if __name__ == '__main__':
    unittest.main()
