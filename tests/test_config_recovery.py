"""Damaged optional preferences must not prevent safe application startup."""
import os
import logging
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blackrose.config import Config, ConfigLoadError, load_config, save_preferences


class ConfigRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_home = self.root / 'xdg-config'
        self.preferences = self.config_home / 'blackrose-bloom' / 'preferences.json'
        self.environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config_home)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_malformed_nonmapping_and_invalid_types_are_ignored_and_preserved(self):
        cases = ['{', '[]', '{"web_enabled": "false"}', '{"favorite_models": [null]}',
                 '{"reasoning_effort": {"model": []}}']
        for contents in cases:
            with self.subTest(contents=contents):
                self.preferences.parent.mkdir(parents=True, exist_ok=True)
                self.preferences.write_text(contents)
                with self.assertLogs('blackrose.config', level='WARNING') as captured:
                    config = load_config(self.root / 'missing.toml')
                self.assertEqual(config.favorite_models, [])
                self.assertFalse(config.web_enabled)
                self.assertFalse(config.knowledge_enabled)
                self.assertEqual(self.preferences.read_text(), contents)
                self.assertIn(str(self.preferences), captured.output[0])
                self.assertNotIn(contents, captured.output[0])

    def test_invalid_preferences_leave_authored_settings_in_effect(self):
        startup = self.root / 'settings.toml'
        startup.write_text('default_model = "opencode-go/authored-model"\nweb_enabled = false\n')
        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        original = '{"default_model": 7, "favorite_models": ["authored-favorite"]}'
        self.preferences.write_text(original)

        with self.assertLogs('blackrose.config', level='WARNING'):
            config = load_config(startup)

        self.assertEqual(config.default_model, 'opencode-go/authored-model')
        self.assertFalse(config.web_enabled)
        self.assertEqual(config.favorite_models, ['authored-favorite'])
        self.assertEqual(self.preferences.read_text(), original)

    def test_valid_saved_provider_is_kept_when_a_sibling_preference_is_invalid(self):
        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        self.preferences.write_text(
            '{"default_model": "opencode-go/saved-model", "favorite_models": null}')

        with self.assertLogs('blackrose.config', level='WARNING'):
            config = load_config(self.root / 'missing.toml')

        self.assertEqual(config.default_model, 'opencode-go/saved-model')
        self.assertEqual(config.favorite_models, [])
        self.assertFalse(config.web_enabled)

    def test_cli_doctor_and_desktop_launch_reach_their_startup_handlers(self):
        from blackrose import main

        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        self.preferences.write_text('{')
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'xdg-state')}):
            with (patch('blackrose.diagnostics.BoundedLogHandler', return_value=logging.NullHandler()),
                  patch('blackrose.main.doctor', return_value=None) as doctor):
                # main runs the async handler; use a real coroutine mock.
                from unittest.mock import AsyncMock
                doctor.side_effect = AsyncMock(return_value=0)
                with self.assertLogs('blackrose.config', level='WARNING'):
                    result = main.main(['doctor'])
                self.assertEqual(result, 0)
                self.assertFalse(doctor.call_args.args[0].web_enabled)
                self.assertFalse(doctor.call_args.args[0].knowledge_enabled)

            with (patch('blackrose.diagnostics.BoundedLogHandler', return_value=logging.NullHandler()),
                  patch('blackrose.workspace.downloads_directory', return_value=self.root),
                  patch('blackrose.workspace.authorize_directory', return_value={'path': self.root}),
                  patch('blackrose.gui.app.gui_problem', return_value=None),
                  patch('blackrose.gui.app.run_gui', return_value=0) as run_gui):
                with self.assertLogs('blackrose.config', level='WARNING'):
                    result = main.main([])
                self.assertEqual(result, 0)
                self.assertFalse(run_gui.call_args.args[0].web_enabled)
                self.assertFalse(run_gui.call_args.args[0].knowledge_enabled)

    def test_corrupt_preferences_are_not_overwritten_by_a_later_save(self):
        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        original = '{'
        self.preferences.write_text(original)

        with self.assertRaisesRegex(ValueError, 'Refusing to replace invalid saved preferences'):
            save_preferences(Config())

        self.assertEqual(self.preferences.read_text(), original)

    def test_explicit_toml_network_choices_survive_corrupt_preferences(self):
        startup = self.root / 'settings.toml'
        startup.write_text('web_enabled = true\nknowledge_enabled = false\n')
        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        self.preferences.write_text('{')

        with self.assertLogs('blackrose.config', level='WARNING'):
            config = load_config(startup)

        self.assertTrue(config.web_enabled)
        self.assertFalse(config.knowledge_enabled)

    def test_preferences_permission_error_does_not_block_startup(self):
        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == self.preferences:
                raise PermissionError('private diagnostic text')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', read_text):
            with self.assertLogs('blackrose.config', level='WARNING') as captured:
                config = load_config(self.root / 'missing.toml')

        self.assertEqual(config.favorite_models, [])
        self.assertIn('PermissionError', captured.output[0])
        self.assertNotIn('private diagnostic text', captured.output[0])

    def test_preferences_decode_error_does_not_block_startup(self):
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == self.preferences:
                raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', read_text):
            with self.assertLogs('blackrose.config', level='WARNING'):
                config = load_config(self.root / 'missing.toml')

        self.assertFalse(config.web_enabled)
        self.assertFalse(config.knowledge_enabled)

    def test_invalid_authored_toml_fails_clearly_and_is_preserved(self):
        startup = self.root / 'settings.toml'
        original = 'default_model = "private-value"\nweb_enabled = [\n'
        startup.write_text(original)

        with self.assertRaises(ConfigLoadError) as raised:
            load_config(startup)

        self.assertIn(str(startup), str(raised.exception))
        self.assertIn('Invalid TOML', str(raised.exception))
        self.assertNotIn('private-value', str(raised.exception))
        self.assertEqual(startup.read_text(), original)

    def test_invalid_authored_settings_name_key_and_expected_values(self):
        cases = [
            ('web_enabeld = false', 'web_enabeld', 'accepted keys'),
            ('endpoint = 42', 'endpoint', 'a string'),
            ('default_model = false', 'default_model', 'a string'),
            ('system_prompt = []', 'system_prompt', 'a string'),
            ('max_tool_rounds = "eight"', 'max_tool_rounds', 'an integer'),
            ('max_tool_rounds = true', 'max_tool_rounds', 'an integer'),
            ('max_tool_rounds = 17', 'max_tool_rounds', '1..16'),
            ('request_timeout = "slow"', 'request_timeout', 'a number'),
            ('request_timeout = true', 'request_timeout', 'a number'),
            ('request_timeout = 0', 'request_timeout', '1..3600'),
            ('router_start_timeout = 121', 'router_start_timeout', 'at most 120'),
            ('web_enabled = "false"', 'web_enabled', 'a boolean'),
            ('citation_rigor = "careful"', 'citation_rigor', 'off, advisory, balanced, or strict'),
            ('favorite_models = [42]', 'favorite_models', 'an array of strings'),
            ('favorite_models = [""]', 'favorite_models', 'nonempty model identifiers'),
            ('trusted_hosts = "example.com"', 'trusted_hosts', 'an array of strings'),
            ('reasoning_effort = {model = []}', 'reasoning_effort', 'a table of string values'),
            ('reasoning_effort = {model = "fast"}', 'reasoning_effort', 'default, off, low'),
            ('reasoning_budgets = {low = true}', 'reasoning_budgets', 'a table of integer values'),
            ('reasoning_budgets = {low = 0}', 'reasoning_budgets', 'positive token counts'),
            ('memory_endpoint = "http://["', 'memory_endpoint', 'loopback HTTP URL'),
            ('memory_peer = "a b"', 'memory_peer', 'letters, digits'),
            ('memory_peer = "blackrose"', 'memory_peer', "not 'blackrose'"),
        ]
        startup = self.root / 'settings.toml'
        for text, key, expected in cases:
            with self.subTest(key=key, text=text):
                original = text + '\n'
                startup.write_text(original)
                with self.assertRaises(ConfigLoadError) as raised:
                    load_config(startup)
                message = str(raised.exception)
                self.assertIn(str(startup), message)
                self.assertIn(key, message)
                self.assertIn(expected, message)
                self.assertIn('correct the file before restarting', message)
                self.assertEqual(startup.read_text(), original)

    def test_saved_preferences_do_not_hide_invalid_authored_setting(self):
        startup = self.root / 'settings.toml'
        startup.write_text('web_enabled = "false"\n')
        self.preferences.parent.mkdir(parents=True, exist_ok=True)
        self.preferences.write_text('{"web_enabled": true}')
        with self.assertRaisesRegex(ConfigLoadError, 'web_enabled must be a boolean'):
            load_config(startup)
        self.assertEqual(startup.read_text(), 'web_enabled = "false"\n')

    def test_unrelated_constructor_errors_are_not_hidden(self):
        for error in (ValueError('programming error'), TypeError('programming error'),
                      AttributeError('programming error'), OSError('I/O failure')):
            with self.subTest(error=type(error).__name__):
                with patch.object(Config, '__post_init__', side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        load_config(self.root / 'missing.toml')
                self.assertIs(raised.exception, error)

    def test_cli_invalid_settings_exit_two_without_traceback(self):
        from blackrose import main

        startup = self.root / 'settings.toml'
        for original in ('web_enabeld = false\n', 'max_tool_rounds = "eight"\n',
                         'max_tool_rounds = 17\n', 'citation_rigor = "careful"\n'):
            with self.subTest(original=original):
                startup.write_text(original)
                stderr = StringIO()
                with (patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state')}),
                      patch('blackrose.diagnostics.BoundedLogHandler', return_value=logging.NullHandler()),
                      redirect_stderr(stderr)):
                    result = main.main(['--config', str(startup), 'doctor'])
                self.assertEqual(result, 2)
                self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                self.assertIn('correct the file before restarting', stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())
                self.assertEqual(startup.read_text(), original)


if __name__ == '__main__':
    unittest.main()
