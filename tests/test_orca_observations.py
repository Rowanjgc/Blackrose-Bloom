"""Distinguish Orca's speech dispatch from output-only debug statements."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_gui_orca import (braille_observations, journey_observations, protocol_observations,
                            speech_observations, timed_dispatches)


class OrcaObservationTests(unittest.TestCase):
    def test_output_without_a_speech_backend_is_not_dispatch_evidence(self):
        result = speech_observations("SPEECH OUTPUT: 'Reply complete' None\n")
        self.assertEqual(result['dispatch_count'], 0)
        self.assertFalse(result['reply_complete_dispatched'])

    def test_dispatch_records_preserve_multiline_text_and_bound_the_report(self):
        log = "SPEECH DISPATCHER: Speaking 'Help\nReply complete' as string\n"
        log += "SPEECH DISPATCHER: Speaking 'x' as char\n" * 260
        result = speech_observations(log)
        self.assertEqual(result['dispatch_count'], 261)
        self.assertEqual(len(result['dispatched_utterances']), 250)
        self.assertTrue(result['utterances_truncated'])
        self.assertTrue(result['reply_complete_dispatched'])
        self.assertEqual(result['combo_box_dispatched'], [])

    def test_combo_box_speech_is_reported(self):
        result = speech_observations("SPEECH DISPATCHER: Speaking 'Models combo box Models.' as string\n")
        self.assertEqual(result['combo_box_dispatched'], ['Models combo box Models.'])

    def test_ssml_debug_details_cannot_absorb_a_later_dispatch_record(self):
        log = (
            "08:00:01.123456 - SPEECH DISPATCHER: Speaking '<speak>Unrelated</speak>' \n"
            "    ORCA rate 50\n"
            "08:00:01.234567 - SPEECH DISPATCHER: Speaking 'Screen reader on.' as string\n"
            "08:00:01.345678 - Another event containing Help\n")
        result = speech_observations(log)
        self.assertEqual(result['dispatched_utterances'], ['Screen reader on.'])
        self.assertFalse(result['help_dispatched'])

    def test_ssip_markup_is_decoded_and_output_debug_is_not_protocol_evidence(self):
        log = (
            'timestamp speechd: 27:DATA:|<speak><mark name="0"/>Reply complete.</speak>\n| (68)\n'
            '225 OK MESSAGE QUEUED\n702 END\n'
            "SPEECH OUTPUT: 'Visible status control marker.' None\n")
        result = protocol_observations(log)
        self.assertEqual(result['ssip_single_line_utterances'], ['Reply complete.'])
        self.assertTrue(result['reply_complete_received_by_speechd'])
        self.assertFalse(result['visible_live_control_received_by_speechd'])
        self.assertEqual(result['ssip_end_notifications'], 1)

    def test_virtual_display_lines_collapse_repeats_and_ignore_dot_frames(self):
        lines = [
            'Visual "Message  $l @ files                     "',
            'Braille "1234|15|  "',
            'Visual "Message  $l @ files                     "',
            'Visual "Reply complete.                         "',
            'Visual "Send (Ctrl Enter) button                "',
            'Braille "234|  "',
        ]
        result = braille_observations(lines)
        self.assertEqual(result['brailled_lines'],
                         ['Message  $l @ files', 'Reply complete.', 'Send (Ctrl Enter) button'])
        self.assertEqual(result['braille_dot_frames'], 2)
        self.assertTrue(result['message_entry_brailled'])
        self.assertTrue(result['reply_complete_brailled'])
        self.assertTrue(result['send_button_brailled'])

    def test_braille_without_display_lines_is_not_evidence(self):
        result = braille_observations(['Braille "1|2"'])
        self.assertEqual(result['brailled_lines'], [])
        self.assertFalse(result['reply_complete_brailled'])

    def test_dispatch_times_come_from_orca_stamps_and_skip_ssml_records(self):
        log = (
            "10:00:01.500000 - SPEECH DISPATCHER: Speaking 'Models dialog' as string\n"
            "10:00:01.500100 - SPEECH DISPATCHER: Speaking '<speak>Models dialog</speak>' \n"
            "    ORCA rate 50\n"
            "SPEECH DISPATCHER: Speaking 'unstamped' as string\n")
        self.assertEqual(timed_dispatches(log), [(36001.5, 'Models dialog'), (None, 'unstamped')])

    def test_speech_and_braille_are_attributed_to_the_journey_that_produced_them(self):
        journeys = [{'name': 'send_stop', 'start': 10.0, 'end': 20.0},
                    {'name': 'keys_sheet', 'start': 20.0}]
        spoken = [(9.0, 'before'), (12.0, 'Reply stopped.'), (21.0, 'Keys and commands dialog'), (None, 'x')]
        brailled = [(12.5, 'Reply stopped.'), (12.6, 'Reply stopped.'), (25.0, 'Close button')]
        stop, sheet = journey_observations(journeys, spoken, brailled)
        self.assertEqual(stop['spoken'], ['Reply stopped.'])
        self.assertEqual(stop['brailled'], ['Reply stopped.'])
        self.assertEqual(stop['expected_spoken'], {'Reply stopped': True})
        self.assertEqual(sheet['spoken'], ['Keys and commands dialog'])
        self.assertEqual(sheet['expected_brailled'], {'Keys and commands': False})


if __name__ == '__main__':
    unittest.main()
