"""Offline safety checks for the optional router child-exposure probe."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_router_child_auth as probe
from blackrose.backend.service import RouterKey


def scratch():
    service = probe.ChildAuthService.__new__(probe.ChildAuthService)
    service.binary, service.preset = Path('/scratch/llama-server'), Path('/scratch/preset.ini')
    service.host, service.port = '127.0.0.1', 18999
    service.key = RouterKey(Path('/scratch/router.key'), owned=True)
    service._process = service._pid = service._handle = None
    return service


def report(**changes):
    checks = {
        'child_argv': {'key_options': [], 'key_variables': []},
        'child_port': {'anonymous': {'GET /v1/models': 200, 'GET /slots': 501},
                       'foreign_origin_and_host': {'status': 200,
                                                   'allow_origin': 'https://blackrose.invalid'},
                       'wrong_key': 200, 'with_key': {'GET /v1/models': 200}},
        'secrecy': {'key_in_child_argv': False, 'key_in_child_environ': False,
                    'key_in_router_log': False},
        'load': {'capabilities': {'thinking': True}},
        'proxied_reply': {'ok': True},
        'cancellation': {'cpu_rate_after_close': 0.0, 'next_reply_contains_clean': True},
        'stream_session': {'router_calls_reach_child': True}}
    checks.update(changes)
    return {'checks': checks}


class RouterChildAuthProbeTests(unittest.TestCase):
    def test_probe_keeps_production_launch_options(self):
        command = scratch()._command()
        self.assertIn('--no-slots', command)
        self.assertNotIn('--slots', command)
        self.assertEqual(command[command.index('--api-key-file') + 1], '/scratch/router.key')

    def test_probe_refuses_to_adopt_an_unowned_router(self):
        with self.assertRaisesRegex(RuntimeError, 'Refusing to adopt'):
            scratch()._adopt()

    def test_keyless_children_pass_and_their_exposure_is_recorded(self):
        result = report()
        self.assertTrue(all(probe._expected(result).values()))
        exposure = probe._exposure(result['checks']['child_port'])
        self.assertEqual(exposure['answers_without_key'], ['GET /v1/models'])
        self.assertTrue(exposure['answers_foreign_origin_and_host'])
        self.assertFalse(exposure['browser_can_read_cross_origin'])

    def test_a_key_reaching_children_fails(self):
        keyed = report(child_argv={'key_options': [], 'key_variables': ['LLAMA_API_KEY']},
                       stream_session={'router_calls_reach_child': False})
        expected = probe._expected(keyed)
        self.assertFalse(expected['no_key_reaches_children'])
        self.assertFalse(expected['router_stream_calls_reach_child'])

    def test_child_port_comes_from_rendered_arguments(self):
        self.assertEqual(probe._child_port(['llama-server', '--host', '127.0.0.1',
                                            '--port', '41234', '--alias', 'm']), 41234)
        self.assertIsNone(probe._child_port(['llama-server', '--port']))
        self.assertIsNone(probe._child_port(['llama-server', '--models-preset', 'x']))


if __name__ == '__main__':
    unittest.main()
