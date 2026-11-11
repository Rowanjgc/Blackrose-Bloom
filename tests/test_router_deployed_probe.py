"""Offline safety checks for the optional installed-router integration probe."""
from pathlib import Path
import sys
import tempfile
import unittest
import asyncio
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_router_deployed_contract as probe


class RouterDeployedProbeTests(unittest.TestCase):
    def test_scratch_profile_forces_cpu_and_bounded_context(self):
        source = {
            'model': '/cached/model.gguf',
            'chat-template-file': '/cached/template.jinja',
            'device': 'Vulkan0',
            'n-gpu-layers': 'all',
            'ctx-size': '131072',
            'spec-type': 'draft-mtp',
            'spec-draft-model': '/cached/draft.gguf',
            'temp': '0.6',
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scratch.ini'
            probe._write_cpu_preset(path, source)
            text = path.read_text()
        self.assertIn('[bra-bik-sharp-minicpm5-2b-cpu]', text)
        self.assertIn('n-gpu-layers = 0', text)
        self.assertIn('ctx-size = 16384', text)
        self.assertIn('reasoning-budget = 256', text)
        self.assertIn('n-predict = 1024', text)
        self.assertIn('model = /cached/model.gguf', text)
        self.assertIn('chat-template-file = /cached/template.jinja', text)
        self.assertNotIn('device =', text)
        self.assertNotIn('spec-', text)
        self.assertNotIn('load-on-startup = true', text)

    def test_source_profile_rejects_missing_files_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.ini'
            path.write_text('[sharp-minicpm5-2b]\nmodel = /missing/model.gguf\n'
                            'chat-template-file = /missing/template.jinja\n')
            with self.assertRaises(FileNotFoundError):
                probe._source_profile(path)
            self.assertEqual(path.read_text().count('[sharp-minicpm5-2b]'), 1)

    def test_scratch_router_keeps_its_key_but_restores_slot_telemetry(self):
        from blackrose.backend.service import RouterKey
        service = probe.ScratchRouterService.__new__(probe.ScratchRouterService)
        service.binary, service.preset = Path('/scratch/llama-server'), Path('/scratch/preset.ini')
        service.host, service.port = '127.0.0.1', 18999
        service.key = RouterKey(Path('/scratch/router.key'), owned=True)
        command = service._command()
        self.assertIn('--slots', command)
        self.assertNotIn('--no-slots', command)
        self.assertEqual(command[command.index('--api-key-file') + 1], '/scratch/router.key')

    def test_scratch_service_refuses_to_adopt_any_unowned_endpoint_process(self):
        service = probe.ScratchRouterService.__new__(probe.ScratchRouterService)
        service._process = None
        service._pid = None
        service._handle = None
        with self.assertRaisesRegex(RuntimeError, 'Refusing to adopt'):
            service._adopt()

    def test_existing_ready_endpoint_is_rejected_without_adoption_or_signal(self):
        class Http:
            aclose = AsyncMock()

        async def verify():
            service = probe.ScratchRouterService.__new__(probe.ScratchRouterService)
            service.endpoint = 'http://127.0.0.1:18080'
            service.host = '127.0.0.1'
            service.port = 18080
            service.local = True
            service._operation = asyncio.Lock()
            http = Http()
            service._http = http
            service._process = None
            service._pid = None
            service._handle = None
            service._output = None
            service._ready = AsyncMock(return_value=True)
            service._launch = unittest.mock.Mock(side_effect=AssertionError('must not launch'))
            with tempfile.TemporaryDirectory() as directory:
                service.lock_path = Path(directory) / 'router.lock'
                with self.assertRaisesRegex(RuntimeError, 'Refusing to adopt'):
                    await service.ensure_running()
                await service.close()
            self.assertIsNone(service._handle)
            service._launch.assert_not_called()
            http.aclose.assert_awaited_once()

        asyncio.run(verify())

    def test_session_probe_uses_a_short_isolated_prompt_not_the_product_prompt(self):
        class Client:
            pass

        with tempfile.TemporaryDirectory() as directory:
            runtime, session, _events = probe._session_runtime(Path(directory), Client())
            self.assertEqual(runtime.controller.model_name, probe.MODEL_ID)
            self.assertIn('scratch probe model', runtime.controller.config.system_prompt)
            self.assertLess(len(runtime.controller.config.system_prompt), 160)
            self.assertFalse(runtime.controller.config.knowledge_enabled)
            self.assertFalse(runtime.controller.config.web_enabled)


if __name__ == '__main__':
    unittest.main()
