"""Pure, bounded checks for the read-only GPU probe preflight."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prepare_local_image_gpu_probe as probe  # noqa: E402


class LocalImagePreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_workflow_inventory_fingerprints_and_resolves_loader_models(self):
        workflow_dir = self.tmp_path / 'workflows'
        models = self.tmp_path / 'models'
        workflow_dir.mkdir()
        model = models / 'diffusion_models' / 'demo.safetensors'
        model.parent.mkdir(parents=True)
        model.write_bytes(b'fixture')
        graph = {'1': {'class_type': 'UNETLoader',
                       'inputs': {'unet_name': 'demo.safetensors'}}}
        (workflow_dir / 'qwen21-t2i.api.json').write_text(json.dumps(graph))

        result = probe.workflow_inventory(workflow_dir, models)

        item = result['qwen21-t2i.api.json']
        self.assertIs(item['exists'], True)
        self.assertEqual(len(item['sha256']), 64)
        self.assertEqual(item['model_references'], [{
            'loader': 'UNETLoader', 'name': 'demo.safetensors',
            'path': str(model), 'exists': True, 'bytes': 7}])
        self.assertIs(result['qwen21-edit.api.json']['exists'], False)

    def test_memory_snapshot_parses_kib_values(self):
        meminfo = self.tmp_path / 'meminfo'
        meminfo.write_text('MemTotal:       1024 kB\nMemAvailable:    512 kB\n')

        self.assertEqual(probe.memory_snapshot(meminfo), {
            'available': True, 'total_bytes': 1024 * 1024,
            'available_bytes': 512 * 1024})

    def test_system_unit_parser_only_returns_whitelisted_fields(self):
        unit = self.tmp_path / 'service'
        unit.write_text('WorkingDirectory=/opt/Comfy\n'
                        'Environment=TOKEN=private HIP_VISIBLE_DEVICES=0\n'
                        'ExecStart=/opt/Comfy/python main.py --port 8189\n')

        result = probe.parse_service_unit(unit)

        self.assertEqual(result, {'available': True,
                                  'WorkingDirectory': '/opt/Comfy',
                                  'ExecStart': '/opt/Comfy/python main.py --port 8189',
                                  'HIP_VISIBLE_DEVICES': '0'})
        self.assertNotIn('TOKEN', json.dumps(result))

    def test_loopback_probe_uses_get_only_and_rejects_redirect(self):
        captured = {}

        class FakeOpener:
            def open(self, request, timeout):
                captured['method'] = request.method
                captured['url'] = request.full_url
                captured['timeout'] = timeout
                raise HTTPError(request.full_url, 302, 'redirect', {}, None)

        with (patch.object(probe.urllib.request, 'build_opener',
                           return_value=FakeOpener()),
              patch.object(probe.socket, 'gethostbyname', return_value='127.0.0.1')):
            result = probe.loopback_get_json('http://localhost:8189', '/queue')

        self.assertEqual(captured, {'method': 'GET', 'url': 'http://localhost:8189/queue',
                                    'timeout': 2.0})
        self.assertEqual(result, {'available': False, 'reason': 'HTTPError'})

    def test_loopback_probe_refuses_non_loopback_hosts(self):
        with patch.object(probe.socket, 'gethostbyname', return_value='192.0.2.10'):
            result = probe.loopback_get_json('http://example.invalid:8189', '/queue')

        self.assertEqual(result, {
            'available': False, 'reason': 'endpoint is not loopback'})
