"""Observe native GTK/WebKit memory during repeated synthetic artifact navigation.

Run under a private display, for example:
  xvfb-run -a dbus-run-session -- .venv/bin/python scripts/measure_gui_memory.py \
      --output artifacts/gui-memory.json

This is an observation, not a pass/fail leak threshold. It never contacts a
provider or reads the user's application data.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import traceback

import webview

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_gui_images import make_runtime  # noqa: E402
from blackrose.animations import encode_scene  # noqa: E402
from blackrose.gui.app import run_gui  # noqa: E402
from blackrose.storage.repository import NewAttachment  # noqa: E402
from blackrose.ui_documents import UIStream, contract  # noqa: E402


class HeldBackend:
    async def load(self, _model):
        return None

    async def models(self):
        return [{'id': 'opencode-go/test-vision', 'architecture': {'input_modalities': ['text', 'image']}}]

    async def stream(self, _model, _messages, _tools):
        yield {'delta': {'content': 'Synthetic partial response; waiting for Stop.'}}
        await asyncio.Event().wait()

    async def aclose(self):
        return None


def landscape_png(width=1600, height=1000):
    """A deterministic, high-detail synthetic landscape (not a camera photo)."""
    import struct
    import zlib

    def chunk(kind, value):
        return (struct.pack('>I', len(value)) + kind + value +
                struct.pack('>I', zlib.crc32(kind + value) & 0xffffffff))

    rows = []
    for y in range(height):
        horizon = int(height * (.57 + .025 * __import__('math').sin(y / 83)))
        row = bytearray([0])
        for x in range(width):
            noise = ((x * 17 + y * 29 + (x * y) % 97) % 23) - 11
            if y < horizon:
                r, g, b = 86 + y // 30, 151 + y // 35, 205 + y // 50
            else:
                r, g, b = 50 + (x // 50) % 35, 93 + (x // 41) % 45, 48 + (x // 67) % 25
            row.extend((max(0, min(255, r + noise)), max(0, min(255, g + noise)),
                        max(0, min(255, b + noise))))
        rows.append(bytes(row))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))


def canvas_scene():
    return {'version': 2, 'engine': 'motion-canvas', 'title': 'Synthetic landscape field notes',
            'description': 'A compact scene included to exercise a sandboxed animation attachment.',
            'duration': 8, 'source': (ROOT / 'skills/explanatory-animations/references/gears.tsx').read_text(),
            'cues': [{'time': 0, 'text': 'Landscape field notes begin.'},
                     {'time': 4, 'text': 'The scene remains paused until opened.'}]}


async def seed(root):
    config, runtime = make_runtime(root)
    config.interactive_ui_enabled = True
    config.interactive_ui_actions_enabled = True
    await runtime.open()
    repo = runtime.controller._repo
    long_chat = await repo.create_conversation('Synthetic long transcript')
    plain_chat = await repo.create_conversation('Synthetic long transcript · plain control')
    spare_chat = await repo.create_conversation('No-navigation control')

    for index in range(122):
        body = (f'## Plain field note {index:03d}\n\n'
                'A synthetic transcript paragraph preserves a realistic mixture of words, punctuation, '
                'and repeated context for native layout. The observation is not a product workload.\n\n' * 3)
        await repo.add_message(plain_chat.id, 'assistant' if index % 2 else 'user', body)

    runtime.controller.conversation = plain_chat
    for index in range(120):
        body = (f'## Field note {index:03d}\n\n'
                'A synthetic transcript paragraph preserves a realistic mixture of words, punctuation, '
                'and repeated context for native layout. The observation is not a product workload.\n\n' * 3)
        message = await repo.add_message(long_chat.id, 'assistant' if index % 2 else 'user', body)
        if index == 80:
            raw = landscape_png()
            blob = runtime.controller._store.write(raw, '.png')
            await repo.append_attachments(message.id, [NewAttachment(
                'image/png', 'synthetic-landscape.png', blob.relative_path, blob.digest,
                blob.size, 1600, 1000)])
        if index == 90:
            message = await repo.add_message(long_chat.id, 'assistant', 'Editable synthetic field notes')
            stream = UIStream(message)
            stream.begin('form')
            stream.append(json.dumps(contract()['examples']['form']) + '\n')
            stream.end()
            await repo.save_blocks(message.id, stream.blocks)
        if index == 100:
            message = await repo.add_message(long_chat.id, 'assistant', 'Sandboxed synthetic animation preview')
            data = await asyncio.to_thread(encode_scene, canvas_scene())
            blob = runtime.controller._store.write(data, '.animation.json')
            await repo.append_attachments(message.id, [NewAttachment(
                'application/vnd.blackrose.animation+json', 'synthetic-field.animation.json',
                blob.relative_path, blob.digest, blob.size, 960, 540, kind='animation')])
    await repo.add_message(spare_chat.id, 'assistant', 'Control transcript stays unmodified while idle.')
    sizes = {'transcript_messages': len(await repo.messages(long_chat.id)),
             'plain_control_messages': len(await repo.messages(plain_chat.id)),
             'image_file_bytes': len(landscape_png()),
             'form_blocks': 1, 'animation_scene_bytes': len(encode_scene(canvas_scene()))}
    await runtime.database.close()
    return config, runtime, long_chat.id, plain_chat.id, spare_chat.id, sizes


def process_tree(pid):
    """Return best-effort RSS/peak RSS for the app and its current descendants."""
    pending, found = [pid], []
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.append(current)
        try:
            child_file = Path(f'/proc/{current}/task/{current}/children')
            pending.extend(int(value) for value in child_file.read_text().split())
        except (OSError, ValueError):
            pass
    rows = []
    for current in found:
        try:
            fields = {}
            for line in Path(f'/proc/{current}/status').read_text().splitlines():
                if line.startswith(('Name:', 'VmRSS:', 'VmHWM:')):
                    key, value = line.split(':', 1)
                    fields[key] = value.strip()
            rows.append({'pid': current, 'name': fields.get('Name'),
                         'rss': fields.get('VmRSS'), 'peak_rss': fields.get('VmHWM')})
        except OSError:
            continue
    return {'processes': rows, 'rss_kib': sum(int(r['rss'].split()[0]) for r in rows if r['rss']),
            'peak_rss_kib': sum(int(r['peak_rss'].split()[0]) for r in rows if r['peak_rss'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=60)
    args = parser.parse_args()
    if args.cycles < 50 or args.cycles > 120:
        parser.error('--cycles must be between 50 and 120')
    report = {'recorded_at': datetime.now(UTC).isoformat(), 'python': platform.python_version(),
              'platform': platform.platform(), 'commit': subprocess.check_output(
                  ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'method': 'synthetic isolated chats; plain 20-cycle transcript-only navigation control, then mixed artifact 60-cycle navigation; process-tree and DOM samples followed by 0/5/15/30-second idle series',
              'limitations': ['synthetic scenery approximates a photo preview but is not a camera image',
                             'RSS is allocator/high-water influenced; WebKit renderer process discovery is best-effort and children may reparent',
                             'performance.memory is Chromium-specific and may be unavailable in WebKit',
                             'one bounded run cannot establish a universal leak or performance threshold']}
    failures = []
    original_create = webview.create_window
    with tempfile.TemporaryDirectory(prefix='blackrose-zp9-native-') as temp:
        root = Path(temp)
        for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
            os.environ[name] = str(root / name)
        config, runtime, long_id, plain_id, spare_id, dataset = asyncio.run(seed(root))
        config.default_model = 'opencode-go/test-vision'
        runtime.controller.backend = HeldBackend()
        runtime.controller.model_name = 'opencode-go/test-vision'
        report['dataset'] = dataset
        begun = time.perf_counter()

        def create(*positional, **kwargs):
            kwargs['hidden'] = False
            window = original_create(*positional, **kwargs)

            def loaded():
                try:
                    def js(source):
                        return window.evaluate_js(source)

                    def wait(source, timeout=30):
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            if js(source):
                                return
                            time.sleep(.04)
                        raise TimeoutError('native probe timed out: ' + source)

                    wait("document.querySelector('#status-text').textContent==='Ready'")
                    report['selected_model'] = runtime.controller.model_name
                    wait("document.querySelector('.msg')")
                    report['startup_to_usable_ms'] = round((time.perf_counter() - begun) * 1000, 2)
                    from gi.repository import Gtk, WebKit2
                    report['native_versions'] = {
                        'gtk': '.'.join(str(f()) for f in (Gtk.get_major_version, Gtk.get_minor_version, Gtk.get_micro_version)),
                        'webkit': '.'.join(str(f()) for f in (WebKit2.get_major_version, WebKit2.get_minor_version, WebKit2.get_micro_version))}
                    web_sample = "({nodes:document.querySelectorAll('*').length, messages:document.querySelectorAll('.msg').length, images:document.querySelectorAll('.image-frame img').length, forms:document.querySelectorAll('.blackrose-ui').length, animations:document.querySelectorAll('.motion-canvas-player').length, heap:performance.memory?performance.memory.usedJSHeapSize:null})"
                    report['rendered_plain_fixture'] = js("({messages:document.querySelectorAll('.msg').length, images:document.querySelectorAll('.image-frame').length, forms:document.querySelectorAll('.blackrose-ui').length, animations:document.querySelectorAll('.motion-canvas-player').length, dom_nodes:document.querySelectorAll('*').length, heap:performance.memory?performance.memory.usedJSHeapSize:null})")
                    if report['rendered_plain_fixture']['messages'] < 100:
                        raise AssertionError('Plain control transcript did not render the latest 100 messages')
                    if any(report['rendered_plain_fixture'][key] for key in ('images', 'forms', 'animations')):
                        raise AssertionError('Plain transcript control unexpectedly contains artifacts')
                    report['attachment_sizes_bytes'] = dataset | {
                        'all_stored_attachment_bytes': sum(p.stat().st_size for p in (root / 'attachments').rglob('*') if p.is_file()) if (root / 'attachments').exists() else 0}
                    report['rendered_plain_fixture']['stored_messages'] = dataset['plain_control_messages']
                    report['rendered_plain_fixture']['paging_window'] = 'latest 100 rows; earlier transcript remains paged'
                    time.sleep(5)
                    report['no_navigation_control'] = {'idle_seconds': 5, 'host_tree': process_tree(os.getpid()), 'web': js(web_sample)}

                    plain_samples = []
                    for index in range(20):
                        js(f"document.querySelector('.chat-item[data-id=\"{spare_id}\"]')?.click()")
                        wait("document.querySelectorAll('.msg').length===1")
                        js(f"document.querySelector('.chat-item[data-id=\"{plain_id}\"]')?.click()")
                        wait("document.querySelectorAll('.msg').length>=100")
                        if (index + 1) % 10 == 0:
                            print(f'Plain transcript navigation cycles completed: {index + 1}/20', flush=True)
                            plain_samples.append({'completed_cycles': index + 1,
                                'host_tree': process_tree(os.getpid()), 'web': js(web_sample)})
                    time.sleep(5)
                    report['plain_navigation_control'] = {'cycles': 20, 'samples': plain_samples,
                        'post_idle_seconds': 5, 'post_idle': {'host_tree': process_tree(os.getpid()), 'web': js(web_sample)}}

                    # Mount and decode the artifact-bearing history after the
                    # plain transcript control, so its growth is separable.
                    js(f"document.querySelector('.chat-item[data-id=\"{spare_id}\"]')?.click()")
                    wait("document.querySelectorAll('.msg').length===1")
                    js(f"document.querySelector('.chat-item[data-id=\"{long_id}\"]')?.click()")
                    wait("document.querySelectorAll('.msg').length>=100")
                    wait("document.querySelector('.motion-canvas-player')?.dataset.ready==='true'", 60)
                    report['rendered_artifact_fixture'] = js("({messages:document.querySelectorAll('.msg').length, images:document.querySelectorAll('.image-frame').length, forms:document.querySelectorAll('.blackrose-ui').length, animations:document.querySelectorAll('.motion-canvas-player').length, dom_nodes:document.querySelectorAll('*').length, heap:performance.memory?performance.memory.usedJSHeapSize:null})")
                    report['rendered_artifact_fixture']['stored_messages'] = dataset['transcript_messages']
                    report['rendered_artifact_fixture']['paging_window'] = 'latest 100 rows; earlier transcript remains paged'
                    if not (report['rendered_artifact_fixture']['images'] and report['rendered_artifact_fixture']['forms'] and report['rendered_artifact_fixture']['animations']):
                        raise AssertionError('The native window did not render all three seeded attachment types: ' + repr(report['rendered_artifact_fixture']))
                    js("document.querySelector('.image-load')?.click()")
                    wait("document.querySelector('.image-frame img')?.naturalWidth===1600", 30)
                    js("document.querySelector('.image-viewer-close')?.click()")
                    report['artifact_warmup_sample'] = {'host_tree': process_tree(os.getpid()), 'web': js(web_sample)}
                    print('Plain control complete; starting repeated artifact navigation.', flush=True)
                    samples = []
                    durations = []
                    for index in range(args.cycles):
                        cycle_start = time.perf_counter()
                        js(f"document.querySelector('.chat-item[data-id=\"{spare_id}\"]')?.click()")
                        wait("document.querySelectorAll('.msg').length===1")
                        js(f"document.querySelector('.chat-item[data-id=\"{long_id}\"]')?.click()")
                        wait("document.querySelectorAll('.msg').length>=100")
                        if index % 3 == 0:
                            js("document.querySelector('.image-load')?.click()")
                            wait("document.querySelector('.image-frame img')?.naturalWidth===1600")
                            js("document.querySelector('.image-open')?.click()")
                            wait("document.querySelector('.image-viewer img')?.naturalWidth===1600")
                            js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
                        elif index % 3 == 1:
                            js("document.querySelector('.blackrose-ui')?.scrollIntoView({block:'center'})")
                            wait("document.querySelector('.blackrose-ui input[name=days]')")
                            js("(()=>{const input=document.querySelector('.blackrose-ui input[name=days]');input.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'6');input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));return input.value==='6'})()")
                            wait("document.querySelector('.blackrose-ui input[name=days]')?.value==='6'")
                        else:
                            js("document.querySelector('.motion-canvas-player')?.scrollIntoView({block:'center'})")
                            wait("document.querySelector('.motion-canvas-player')?.dataset.ready==='true'", 30)
                            js("document.querySelector('.animation-play')?.click()")
                            wait("parseFloat(document.querySelector('.animation-time')?.textContent||'0')>.1", 12)
                            js("document.querySelector('.animation-play')?.click()")
                        durations.append((time.perf_counter() - cycle_start) * 1000)
                        if (index + 1) % 10 == 0:
                            print(f'Navigation cycles completed: {index + 1}/{args.cycles}', flush=True)
                            samples.append({'completed_cycles': index + 1,
                                'host_tree': process_tree(os.getpid()),
                                'web': js("({nodes:document.querySelectorAll('*').length, messages:document.querySelectorAll('.msg').length, images:document.querySelectorAll('.image-frame img').length, forms:document.querySelectorAll('.blackrose-ui').length, animations:document.querySelectorAll('.motion-canvas-player').length, heap:performance.memory?performance.memory.usedJSHeapSize:null})")})
                    report['cycles'] = args.cycles
                    report['sample_series'] = samples
                    report['cycle_duration_ms'] = {'median': round(statistics.median(durations), 2),
                        'p95': round(sorted(durations)[int(.95 * (len(durations) - 1))], 2),
                        'max': round(max(durations), 2)}
                    # Sample promptly and over a longer idle interval. No
                    # universal plateau is assumed from these checkpoints.
                    post_idle = []
                    idle_started = time.monotonic()
                    for seconds in (0, 5, 15, 30):
                        remaining = seconds - (time.monotonic() - idle_started)
                        if remaining > 0:
                            time.sleep(remaining)
                        post_idle.append({'idle_seconds': seconds,
                            'host_tree': process_tree(os.getpid()), 'web': js(web_sample)})
                    report['post_idle_series'] = post_idle
                    report['post_idle_seconds'] = 30
                    report['post_idle'] = post_idle[-1]
                    # Stop responsiveness uses a local backend that yields once,
                    # then blocks until cancellation; no inference/provider work.
                    js("document.querySelector('#input').value='synthetic stop response';document.querySelector('#composer').requestSubmit()")
                    wait("Array.from(document.querySelectorAll('.msg.assistant .body')).some(node=>node.textContent.includes('Synthetic partial response'))")
                    stop_start = time.perf_counter()
                    js("window.__memoryStopResult=null; pywebview.api.stop().then(result=>window.__memoryStopResult=result); true")
                    wait("window.__memoryStopResult!==null")
                    report['stop_bridge_result'] = js('window.__memoryStopResult')
                    if report['stop_bridge_result'] is not True:
                        raise AssertionError('Native Stop did not find an active generation: ' + repr(js("({status:document.querySelector('#status-text').textContent,send:document.querySelector('#send').textContent})")))
                    wait("['Reply stopped','Ready'].includes(document.querySelector('#status-text').textContent)")
                    report['stop_responsiveness_ms'] = round((time.perf_counter() - stop_start) * 1000, 2)
                    report['passed'] = True
                except BaseException as error:
                    failures.append(error)
                    report['error'] = repr(error)
                    traceback.print_exc()
                finally:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(report, indent=2) + '\n')
                    window.destroy()

            window.events.loaded += loaded
            return window

        webview.create_window = create
        run_gui(config, runtime=runtime)
    if failures:
        raise SystemExit(1)
    print(f'Native memory observations recorded: {args.output}', flush=True)


if __name__ == '__main__':
    main()
