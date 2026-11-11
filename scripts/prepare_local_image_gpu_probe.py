"""Read-only preflight for a separately authorized local image GPU probe.

This command never starts/stops a service, submits/cancels a Comfy job, loads a
model, queries the router, or reads a router credential. It inventories the
configured workflow/model files, current host counters, and loopback GET-only
Comfy endpoints so an operator can decide whether a dedicated window is safe.
"""
from __future__ import annotations

import argparse
import os
import ast
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

GIB = 1024 ** 3
EXPECTED_WORKFLOWS = ('qwen21-t2i.api.json', 'qwen21-edit.api.json')
MODEL_KEYS = {
    'UNETLoader': ('unet_name', 'diffusion_models'),
    'CLIPLoader': ('clip_name', 'text_encoders'),
    'VAELoader': ('vae_name', 'vae'),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def workflow_inventory(workflows: Path, models: Path) -> dict:
    result = {}
    for name in EXPECTED_WORKFLOWS:
        path = workflows / name
        entry = {'path': str(path), 'exists': path.is_file()}
        if path.is_file():
            raw = path.read_bytes()
            entry['sha256'] = hashlib.sha256(raw).hexdigest()
            try:
                graph = json.loads(raw)
                refs = {(node.get('class_type'), node.get('inputs', {}).get(key))
                        for node in graph.values() if isinstance(node, dict)
                        for node_type, (key, _) in MODEL_KEYS.items()
                        if node.get('class_type') == node_type
                        and isinstance(node.get('inputs', {}).get(key), str)}
                entry['model_references'] = []
                for node_type, filename in sorted(refs):
                    _, subdirectory = MODEL_KEYS[node_type]
                    model_path = models / subdirectory / filename
                    item = {'loader': node_type, 'name': filename,
                            'path': str(model_path), 'exists': model_path.is_file()}
                    if item['exists']:
                        item['bytes'] = model_path.stat().st_size
                    entry['model_references'].append(item)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                entry['parse_error'] = 'invalid workflow JSON'
        result[name] = entry
    return result


def memory_snapshot(meminfo: Path = Path('/proc/meminfo')) -> dict:
    values = {}
    try:
        for line in meminfo.read_text().splitlines():
            key, _, rest = line.partition(':')
            if key in ('MemTotal', 'MemAvailable'):
                values[key] = int(rest.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {'available': False}
    return {'available': 'MemAvailable' in values,
            'total_bytes': values.get('MemTotal'),
            'available_bytes': values.get('MemAvailable')}


def drm_snapshot(root: Path = Path('/sys/class/drm')) -> list[dict]:
    devices = []
    try:
        cards = sorted(root.glob('card[0-9]*'))
    except OSError:
        return devices
    for card in cards:
        device = card / 'device'
        try:
            total = int((device / 'mem_info_vram_total').read_text())
            used = int((device / 'mem_info_vram_used').read_text())
            product = (device / 'device').read_text().strip()
            devices.append({'card': card.name, 'device_id': product,
                            'total_bytes': total, 'used_bytes': used,
                            'free_bytes': max(0, total - used)})
        except (OSError, ValueError):
            continue
    return devices


def loopback_get_json(endpoint: str, path: str, timeout: float = 2.0) -> dict:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != 'http' or not parsed.hostname:
        return {'available': False, 'reason': 'endpoint must be plain HTTP loopback'}
    try:
        if not ipaddress.ip_address(socket.gethostbyname(parsed.hostname)).is_loopback:
            return {'available': False, 'reason': 'endpoint is not loopback'}
    except (OSError, ValueError):
        return {'available': False, 'reason': 'endpoint host did not resolve to loopback'}

    # Explicitly issue only GET. Reject redirects so a local endpoint cannot
    # turn this diagnostic into a request to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    url = endpoint.rstrip('/') + path
    request = urllib.request.Request(url, method='GET', headers={'Accept': 'application/json'})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.loads(response.read(2 * 1024 * 1024))
        if not isinstance(value, dict):
            return {'available': False, 'reason': 'response was not a JSON object'}
        return {'available': True, 'data': value}
    except (OSError, urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, UnicodeDecodeError) as error:
        return {'available': False, 'reason': type(error).__name__}


def systemd_readonly_status(timeout: float = 3.0) -> dict:
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'show', 'comfyui.service',
             '--property=LoadState,ActiveState,MainPID,FragmentPath'],
            check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'available': False, 'reason': type(error).__name__}
    if result.returncode:
        return {'available': False, 'reason': 'systemctl query unavailable'}
    return {'available': True, 'properties': dict(
        line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)}


def parse_service_unit(path: Path) -> dict:
    """Return only whitelisted operational fields; never echo arbitrary env."""
    try:
        lines = path.read_text().splitlines()
    except OSError as error:
        return {'available': False, 'reason': type(error).__name__}
    fields = {'WorkingDirectory': None, 'ExecStart': None, 'HIP_VISIBLE_DEVICES': None}
    for line in lines:
        text = line.strip()
        for key in ('WorkingDirectory', 'ExecStart'):
            prefix = key + '='
            if text.startswith(prefix):
                fields[key] = text[len(prefix):]
        if text.startswith('Environment=') and 'HIP_VISIBLE_DEVICES=' in text:
            for token in text[len('Environment='):].split():
                if token.startswith('HIP_VISIBLE_DEVICES='):
                    fields['HIP_VISIBLE_DEVICES'] = token.split('=', 1)[1].strip('"')
    fields['available'] = True
    return fields


def comfy_software_identity(root: Path) -> dict:
    """Read local source/package version metadata without importing Comfy or torch."""
    identity = {'root': str(root)}
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                check=False, capture_output=True, text=True, timeout=2)
        identity['git_commit'] = result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        identity['git_commit'] = None
    version_file = root / 'comfyui_version.py'
    try:
        tree = ast.parse(version_file.read_text())
        version = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == '__version__'
                               for target in node.targets))
    except (OSError, SyntaxError, StopIteration, ValueError):
        version = None
    identity['source_version'] = version
    package_dir = next(iter(sorted((root / '.venv-rocm71/lib').glob(
        'python*/site-packages'))), None)
    identity['packages'] = {}
    for distribution in ('torch', 'comfyui-frontend-package',
                         'comfyui-workflow-templates', 'comfyui-embedded-docs'):
        installed = None
        if package_dir:
            directory_prefix = distribution.replace('-', '_')
            for metadata in package_dir.glob(f'{directory_prefix}-*.dist-info/METADATA'):
                for line in metadata.read_text(errors='replace').splitlines():
                    if line.startswith('Version: '):
                        installed = line.partition(': ')[2]
                        break
                if installed:
                    break
        identity['packages'][distribution] = installed
    return identity


def build_report(config, *, comfy_root: Path,
                 unit_path: Path | None = None) -> dict:
    if unit_path is None:
        unit_path = Path.home() / '.config/systemd/user/comfyui.service'
    workflows = Path(config.local_image_workflows).expanduser()
    models = comfy_root / 'models'
    memory = memory_snapshot()
    gpu = drm_snapshot()
    discrete = [device for device in gpu if device['total_bytes'] >= 4 * GIB]
    matched_gpu = discrete[0] if len(discrete) == 1 else None
    free_gpu = matched_gpu['free_bytes'] if matched_gpu else None
    configured_host = urllib.parse.urlsplit(config.local_image_endpoint).hostname
    local_endpoints = {}
    if configured_host and configured_host in ('127.0.0.1', 'localhost', '::1'):
        local_endpoints = {
            'system_stats': loopback_get_json(config.local_image_endpoint, '/system_stats'),
            'queue': loopback_get_json(config.local_image_endpoint, '/queue')}
    ram_free = memory.get('available_bytes')
    sharing_ok = (free_gpu is not None and free_gpu >= 9 * GIB
                  and ram_free is not None and ram_free >= 18 * GIB)
    workflows_found = workflow_inventory(workflows, models)
    workflow_files_ok = all(
        entry.get('exists') and not entry.get('parse_error')
        and all(reference['exists'] for reference in entry.get('model_references', []))
        for entry in workflows_found.values())
    systemd_status = systemd_readonly_status()
    queue_response = local_endpoints.get('queue', {})
    queue_data = queue_response.get('data', {})
    queue_clear = (queue_response.get('available') is True
                   and not queue_data.get('queue_running')
                   and not queue_data.get('queue_pending'))
    checks = {'sharing_thresholds': sharing_ok,
              'workflow_models_present': workflow_files_ok,
              'service_status_readable': systemd_status.get('available') is True,
              'existing_comfy_queue_observed_empty': queue_clear}
    ready = all(checks.values())
    return {
        'mode': 'read_only_preflight',
        'safety': {'gpu_model_load': False, 'service_start_stop': False,
                   'comfy_job_submit_cancel': False, 'router_query_or_key_read': False,
                   'existing_job_interruption': False},
        'configuration': {
            'local_image_enabled': bool(config.local_image_enabled),
            'keep_chat_loaded': bool(config.local_image_keep_chat_loaded),
            'endpoint': config.local_image_endpoint,
            'workflow_directory': str(workflows),
            'comfy_root': str(comfy_root),
        },
        'workflows': workflows_found,
        'host': {'memory': memory, 'drm_devices': gpu,
                 'matched_discrete_device': matched_gpu,
                 'sharing_thresholds': {'free_vram_bytes': 9 * GIB,
                                        'available_ram_bytes': 18 * GIB},
                 'meets_sharing_thresholds': sharing_ok},
        'service': {'systemd_unit': parse_service_unit(unit_path),
                    'systemd_user_status': systemd_status,
                    'loopback_get': local_endpoints},
        'software': comfy_software_identity(comfy_root),
        'readiness_checks': checks,
        'readiness': ('eligible_for_parent_window_review' if ready
                      else 'blocked_below_shared_model_thresholds_or_missing_measurement'),
        'isolated_probe_manifest': {
            'service_process': 'new direct ComfyUI child, tracked by exact PID/process handle',
            'port': 8190,
            'scratch_root': '<new private temporary root; output/input/temp/user/cache/state all below it>',
            'model_access': 'existing model directories mounted/read-only via a private extra_model_paths.yaml',
            'device': 'HIP_VISIBLE_DEVICES=0 only after host GPU identity is verified',
            'fixture': 'single 512x512 text-to-image, batch 1, 4 steps; no user image input',
            'scenarios': ['baseline and peak VRAM/RSS sampling',
                          'success then second request',
                          'cancel only own prompt UUID and verify queue/output cleanup',
                          'controlled invalid-node failure and cleanup',
                          'terminate only the tracked scratch child during a job; verify process death cleanup',
                          'restore chat model only after owned service exit and verify second chat/image request'],
            'must_not_touch': ['systemd comfyui.service', 'unrecognized ComfyUI processes/jobs',
                               'router models without explicit ownership window', 'production key before approval'],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='startup config file; preferences still follow normal XDG lookup')
    parser.add_argument('--comfy-root', type=Path, default=Path(os.environ.get('COMFYUI_ROOT', Path.home() / 'ComfyUI')))
    parser.add_argument('--systemd-unit', type=Path,
                        default=Path.home() / '.config/systemd/user/comfyui.service')
    args = parser.parse_args(argv)
    # Import at runtime so the helper functions remain independently testable.
    from blackrose.config import load_config
    report = build_report(load_config(args.config), comfy_root=args.comfy_root,
                          unit_path=args.systemd_unit)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report['readiness'] == 'eligible_for_parent_window_review' else 2


if __name__ == '__main__':
    sys.exit(main())
