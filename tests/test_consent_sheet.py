"""The in-window approval sheet accepts only a deliberate, armed accept."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from blackrose.gui import external
from blackrose.gui.consent import WIDER, ConsentSheet


class FakeGLib:
    SOURCE_REMOVE = False

    def __init__(self):
        self.timers = {}

    def timeout_add(self, _ms, callback):
        handle = len(self.timers) + 1
        self.timers[handle] = callback
        return handle

    def source_remove(self, handle):
        self.timers.pop(handle, None)

    def fire(self):
        for handle in list(self.timers):
            self.timers.pop(handle)()


def sheet(wider=False):
    """A sheet with its widgets stubbed; only the decision logic runs."""
    s = ConsentSheet.__new__(ConsentSheet)
    s._glib, s._gdk = FakeGLib(), SimpleNamespace(keyval_name=lambda value: value)
    s._accept, s._decline, s._wider = Mock(), Mock(), Mock()
    s._decline.has_focus.return_value = True
    s._revealer, s._scrim, s._webview = Mock(), Mock(), Mock()
    s._closed = False
    s._current = {'answer': False, 'done': threading.Event(), 'wider': wider}
    s._arm()
    return s


def press(s, key):
    return s._key(None, SimpleNamespace(keyval=key))


class ConsentSheetTests(unittest.TestCase):
    def test_accept_is_ignored_until_armed_then_answers_once(self):
        s = sheet()
        state = s._current
        self.assertTrue(press(s, 'a'))
        self.assertIs(s._current, state)
        s._accept.set_sensitive.assert_called_with(False)
        s._glib.fire()
        s._accept.set_sensitive.assert_called_with(True)
        press(s, 'a')
        self.assertTrue(state['done'].is_set())
        self.assertIs(state['answer'], True)
        s._webview.set_sensitive.assert_called_with(True)
        self.assertFalse(press(s, 'a'))  # Closed: keys go back to the page.

    def test_enter_and_space_never_accept(self):
        for focused_decline in (True, False):
            s = sheet()
            s._glib.fire()
            s._decline.has_focus.return_value = focused_decline
            state = s._current
            for key in ('Return', 'KP_Enter', 'space'):
                press(s, key)
            self.assertIs(state['answer'], False)
            self.assertEqual(state['done'].is_set(), focused_decline)

    def test_decline_keys_answer_even_before_arming(self):
        for key in ('Escape', 'd', 'N'):
            s = sheet()
            state = s._current
            press(s, key)
            self.assertTrue(state['done'].is_set())
            self.assertIs(state['answer'], False)

    def test_refocusing_the_window_disarms_again(self):
        s = sheet()
        s._glib.fire()
        s._refocus()
        state = s._current
        press(s, 'y')
        self.assertIs(s._current, state)
        s._glib.fire()
        press(s, 'y')
        self.assertIs(state['answer'], True)

    def test_wider_accept_needs_an_offer_and_arming(self):
        s = sheet()
        s._glib.fire()
        state = s._current
        press(s, 's')
        self.assertIs(s._current, state)  # Not offered: S does nothing.
        s = sheet(wider=True)
        state = s._current
        press(s, 'S')
        self.assertIs(s._current, state)  # Offered but not yet armed.
        s._wider.set_sensitive.assert_called_with(False)
        s._glib.fire()
        s._wider.set_sensitive.assert_called_with(True)
        press(s, 's')
        self.assertEqual(state['answer'], WIDER)
        self.assertTrue(state['done'].is_set())

    def test_other_keys_are_swallowed_but_focus_keys_pass(self):
        s = sheet()
        self.assertTrue(press(s, 'x'))
        self.assertTrue(press(s, 'Delete'))
        self.assertFalse(press(s, 'Tab'))
        self.assertIsNotNone(s._current)

    def test_closing_the_window_declines_and_refuses_later_questions(self):
        s = sheet()
        state = s._current
        s._turn = threading.Lock()
        s._destroyed()
        self.assertTrue(state['done'].is_set())
        self.assertIs(state['answer'], False)
        self.assertFalse(s.ask('Title', 'Message', 'Allow'))

    def test_choices_toggle_by_number_and_accept_returns_each(self):
        s = sheet()
        checks = [Mock(), Mock(), Mock()]
        for check in checks:
            check.get_active.return_value = True
            check.has_focus.return_value = False
            check.set_active.side_effect = lambda value, c=check: setattr(c.get_active, 'return_value', value)
        state = s._current
        state['checks'] = checks
        self.assertTrue(press(s, '2'))
        self.assertTrue(press(s, '9'))  # No ninth choice: swallowed, nothing toggles.
        checks[0].has_focus.return_value = True
        s._decline.has_focus.return_value = False
        self.assertFalse(press(s, 'space'))  # The focused checkbox toggles itself.
        press(s, 'Return')
        self.assertFalse(state['done'].is_set())
        press(s, 'a')  # Not armed yet.
        self.assertFalse(state['done'].is_set())
        s._glib.fire()
        press(s, 'a')
        self.assertEqual(state['answer'], (True, False, True))

    def test_external_ask_without_the_sheet_accepts_all_choices_or_none(self):
        window = SimpleNamespace()
        for answer, expected in ((True, (True, True)), (False, False)):
            with self.subTest(answer=answer), unittest.mock.patch.object(
                    external, 'ask', wraps=external.ask) as wrapped:
                def inner(*args, answer=answer, **kwargs):
                    if kwargs.get('choices'):
                        return wrapped._mock_wraps(*args, **kwargs)
                    return answer
                wrapped.side_effect = inner
                self.assertEqual(external.ask(window, 'T', 'M', 'Allow checked', choices=['a', 'b'],
                                              gtk=object(), glib=object()), expected)
                self.assertEqual(wrapped.call_args.args[3], 'Allow all')
                self.assertEqual(wrapped.call_args.kwargs['detail'], 'a\n\nb')

    def test_external_ask_prefers_the_sheet(self):
        window = SimpleNamespace(_blackrose_consent=Mock(ask=Mock(return_value=True)))
        self.assertTrue(external.ask(window, 'Title', 'Message', 'Open', warning=True))
        window._blackrose_consent.ask.assert_called_once_with('Title', 'Message', 'Open', warning=True, wider=None,
                                                              detail='', detail_label='', note='', choices=None)


if __name__ == '__main__':
    unittest.main()
