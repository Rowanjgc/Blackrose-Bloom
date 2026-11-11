"""Motion Canvas authoring and compilation; generated code only executes in a frame.

The Node worker parses TSX with esbuild. Its resolver allows only installed Motion
Canvas exports; it never imports/evaluates a generated module or build config.
"""
from __future__ import annotations

from functools import lru_cache
import html
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import signal
import sys
import time

import jsonschema

from .resources import animation_runtime_dir

STATIC = Path(__file__).parent / 'gui' / 'static'
RUNTIME = animation_runtime_dir()
MAX_SOURCE_BYTES = 64_000
MAX_SAVED_BYTES = 512_000
PLAYBACK_TIMEOUT = 25
MOTION_GRID_COLUMNS = 24
MOTION_GRID_ROWS = 12
MOTION_CAPTION_ROWS = 2
MOTION_EARLY_FRACTION = 0.55

INPUT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'version': {'type': 'integer', 'const': 2},
        'engine': {'type': 'string', 'const': 'motion-canvas'},
        'title': {'type': 'string', 'minLength': 1, 'maxLength': 120},
        'description': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'duration': {'type': 'number', 'minimum': 0.5, 'maximum': 120,
                     'description': 'Expected seconds; saving replaces this with the measured source duration.'},
        'source': {'type': 'string', 'minLength': 1, 'maxLength': MAX_SOURCE_BYTES,
                   'description': 'Complete TSX module exporting default makeScene2D(function* (view) {...}).'},
        'loop': {'type': 'boolean'},
        'cues': {'type': 'array', 'maxItems': 32, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'time': {'type': 'number', 'minimum': 0, 'maximum': 120},
                           'text': {'type': 'string', 'minLength': 1, 'maxLength': 240}},
            'required': ['time', 'text']}},
    },
    'required': ['title', 'description', 'duration', 'source'],
}

MODEL_INSTRUCTIONS = '''Use animation_create for playable animations requested by the user.
Read the relevant enabled animation skill before authoring; load examples only as needed.
Author standard Motion Canvas TSX,
exporting default makeScene2D(function* (view) { ... }). Imports are available from
@motion-canvas/2d and @motion-canvas/core only; JSX works. Use Txt for text, Node
for groups, Line with points={[[x,y],...]}, and Path with data="SVG path string".
Use fill={null} for unfilled shapes. The canvas is 960×540 with origin
at its CENTER (x ±480, y ±270). Use paths, curves, groups, rotation,
scale, shapes and signal-driven motion to show the actual process. Choose geometry
that resembles the subject. Build on an ink background (#0d0d0d), readable light
labels, and restrained rose/gold accents. Keep important content within margins.
Use finite generator timelines with yield* and explicit durations; all/chain/waitFor,
createRef, createSignal and tween are available. Every tween and all/waitFor call
must be yielded; a bare generator call is rejected before playback. spawn is the
auto-starting exception. Assets and network are unavailable.
Add nodes before dereferencing their refs. For linked motion use reactive callbacks,
e.g. x={() => progress() * 200}, rather than capturing progress() once. Use shared
timing constants for motion and captions. No editor-managed time events or slides
are configured. Top-level loop repeats a finite timeline; do not use infinite loops.
Create nodes with JSX props or new Circle({...}), then add them to the view.
Animate signals with node.x(value, seconds); do not assign node.x = value.
Do not spread a constructed node into JSX props; add the node itself or its children.
Example source:
import {makeScene2D, Node, Circle, Txt} from '@motion-canvas/2d';
import {createRef, all, waitFor} from '@motion-canvas/core';
export default makeScene2D(function* (view) {
  const ring = createRef<Circle>();
  view.add(<Node>
    <Txt text="A moving ring" y={-160} fontSize={32} fill="#e0e0e0" />
    <Circle ref={ring} size={120} stroke="#ef6461" lineWidth={5} />
  </Node>);
  yield* all(ring().scale(1.8, 1), ring().rotation(90, 1));
  yield* waitFor(1);
});
Supply scene {title,description,duration,source,cues?} and top-level loop boolean.
Emit each JSON field once: one scene.source, one title/description/duration, and
one top-level loop. Do not append a second scene or repeat source inside the object.
The app checks types and sample rendering in supervised WebKit, measures the source
duration, and rejects captions beyond it. Metadata alone cannot extend a timeline.
These checks do not judge visual or conceptual correctness. One successful call creates the whole
animation, including loops. After success give a brief final answer. If compilation
fails, fix the reported source error and retry. For revisions read animation_read,
then submit complete new source with revises_attachment_id; omit that ID for new
animations. Older scenes use nodes/tracks: recreate their subject in Motion Canvas
when revising. No mandatory written plan or verification checklist is needed.'''


def is_motion_scene(scene):
    return isinstance(scene, dict) and (scene.get('version') == 2
                                       or scene.get('engine') == 'motion-canvas'
                                       or 'source' in scene)


def validate_scene(scene):
    schema = {**INPUT_SCHEMA, 'properties': {**INPUT_SCHEMA['properties'],
        'compiled': {'type': 'string', 'minLength': 1, 'maxLength': 400_000}},
        'required': [*INPUT_SCHEMA['required'], 'version', 'engine']}
    try:
        raw = json.dumps(scene, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ValueError('Animation must be finite UTF-8 JSON') from error
    if len(raw) > MAX_SAVED_BYTES:
        raise ValueError('Motion Canvas scene exceeds 512000 bytes')
    error = next(jsonschema.Draft202012Validator(schema).iter_errors(scene), None)
    if error:
        path = '.'.join(map(str, error.absolute_path)) or 'scene'
        raise ValueError(f'Invalid Motion Canvas {path}: {error.message[:400]}')
    if len(scene['source'].encode('utf-8')) > MAX_SOURCE_BYTES:
        raise ValueError('Motion Canvas source exceeds 64000 bytes')
    previous = -1
    for cue in scene.get('cues', []):
        if not previous < cue['time'] <= scene['duration']:
            raise ValueError('Caption times must increase and fit within duration')
        previous = cue['time']
    return dict(scene)


def compile_source(source: str) -> str:
    node = shutil.which('node')
    if (not node or not (RUNTIME / 'node_modules/esbuild').is_dir()
            or not (RUNTIME / 'node_modules/typescript').is_dir()
            or not (RUNTIME / 'exports.json').is_file()
            or not (STATIC / 'motion-canvas-runtime.js').is_file()):
        raise ValueError('Motion Canvas needs setup: run blackrose setup-animations '
                         '(requires Node.js 22+ and npm).')
    signature = tuple((path.stat().st_mtime_ns, path.stat().st_size) for path in (
        RUNTIME / 'compile.mjs', RUNTIME / 'diagnostics.mjs', RUNTIME / 'exports.json',
        RUNTIME / 'package-lock.json', STATIC / 'motion-canvas-runtime.js'))
    return _compile_source(source, node, signature)


@lru_cache(maxsize=16)
def _compile_source(source, node, signature):
    try:
        result = subprocess.run([node, '--max-old-space-size=256', str(RUNTIME / 'compile.mjs')],
            input=source, capture_output=True, text=True, timeout=20, cwd=RUNTIME)
    except subprocess.TimeoutExpired as error:
        raise ValueError('Motion Canvas compilation exceeded 20 seconds; simplify the source') from error
    if result.returncode:
        raise ValueError('Motion Canvas compilation failed:\n' + result.stderr[:3000])
    if not result.stdout or len(result.stdout.encode('utf-8')) > 400_000:
        raise ValueError('Compiled scene exceeds 400000 bytes; simplify the source')
    return result.stdout


def probe_document(document: str, times: tuple = (), capture: bool = False) -> dict:
    # PNGs are large and are used only once for review. Never retain them in
    # the playback-validation cache.
    if capture:
        return _run_probe(document, times, True)
    return _cached_probe(document, times)


@lru_cache(maxsize=16)
def _cached_probe(document: str, times: tuple) -> dict:
    return _run_probe(document, times, False)


def _run_probe(document: str, times: tuple, capture: bool) -> dict:
    """Supervise execution outside the chat web process; cache by full runtime/source.

    Killing the process group also cleans up the disposable WebKit subprocesses.
    A failed probe is not cached, allowing retry after fixing environment issues.
    """
    payload = json.dumps({'document': document, 'times': times, 'capture': capture})
    process = subprocess.Popen([sys.executable, '-m', 'blackrose.motion_canvas_probe'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)
    try:
        try:
            output, error = process.communicate(payload, timeout=PLAYBACK_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f'Motion Canvas playback validation exceeded {PLAYBACK_TIMEOUT} seconds; '
                             'use a finite timeline and simplify the source') from error
        if process.returncode:
            raise ValueError('Motion Canvas playback validation unavailable: ' + error[-1500:])
        try:
            result = json.loads(output)
        except (ValueError, TypeError) as error:
            raise ValueError('Motion Canvas playback validation returned no valid result') from error
        if result.get('error'):
            raise ValueError('Motion Canvas playback validation failed: ' + str(result['error'])[:1500])
        if not isinstance(result.get('duration'), (float, int)) or not 0.5 <= result['duration'] <= 120:
            raise ValueError('Motion Canvas playback validation returned an invalid duration')
        if not result.get('samples'):
            raise ValueError('Motion Canvas playback validation rendered no samples')
        return result
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The process group already exited.
            pass
        process.communicate()


def motion_report(result, duration=None):
    """Report whether rendered frames change outside the bottom text band.

    This is a coarse deterministic check, not a visual-quality verdict. It uses
    the occupancy grid emitted with every probe sample, so it costs no extra
    render and never retains frame images.
    """
    samples = result.get('samples') if isinstance(result, dict) else None
    if not isinstance(samples, list) or len(samples) < 3:
        return {'assessment': 'unknown', 'notice': None}
    duration = float(duration if duration is not None else result.get('duration') or 0)
    cells = MOTION_GRID_COLUMNS * MOTION_GRID_ROWS
    grids = {}
    foregrounds = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        grid = sample.get('grid')
        try:
            at = round(float(sample['time']), 3)
        except (KeyError, TypeError, ValueError):
            continue
        if (isinstance(grid, list) and len(grid) == cells
                and all(isinstance(value, int) and not isinstance(value, bool)
                        for value in grid)):
            # Repeated seek targets are expected; keep the first render of a time.
            grids.setdefault(at, grid)
            foreground = sample.get('foreground')
            if isinstance(foreground, int) and not isinstance(foreground, bool):
                foregrounds.append(foreground)
    ordered = sorted(grids.items())
    if len(ordered) < 3:
        return {'assessment': 'unknown', 'notice': None}
    if foregrounds and not any(foregrounds):
        return {'assessment': 'blank', 'last_motion': None,
                'duration': round(duration, 2),
                'notice': 'Visual motion check: rendered frames are empty.'}
    # Ignore the bottom caption band so text changes cannot stand in for motion.
    visible = cells - MOTION_CAPTION_ROWS * MOTION_GRID_COLUMNS
    last_motion = None
    for (_, before), (at, after) in zip(ordered, ordered[1:], strict=False):
        if any(before[index] != after[index] for index in range(visible)):
            last_motion = at
    if last_motion is None:
        return {'assessment': 'static', 'last_motion': None,
                'duration': round(duration, 2),
                'notice': 'Visual motion check: no visual change outside the bottom '
                          'text band; the scene appears static.'}
    report = {'assessment': 'moving', 'last_motion': round(last_motion, 2),
              'duration': round(duration, 2), 'notice': None}
    if last_motion < duration * MOTION_EARLY_FRACTION:
        report['assessment'] = 'limited'
        report['notice'] = (f'Visual motion check: after {last_motion:.2f}s of {duration:.2f}s, '
                            'only the bottom text band changed. Check that the mechanism itself moves.')
    return report


def prepare_scene(scene, *, metrics=None, motion=None, plan=None):
    # The model never supplies compiled output. Recompile even if a caller passed
    # a saved scene so stale source/compiled pairs cannot survive a revision.
    normalized = validate_scene(scene)
    started = time.perf_counter()
    normalized['compiled'] = compile_source(normalized['source'])
    if metrics is not None:
        metrics['compile_seconds'] = time.perf_counter() - started
    times = [cue['time'] for cue in normalized.get('cues', [])]
    started = time.perf_counter()
    result = probe_document(player_document(normalized),
                            tuple(sorted(set(times))))
    if metrics is not None:
        metrics['probe_seconds'] = time.perf_counter() - started
    normalized['duration'] = result['duration']
    if motion is not None:
        motion.update(motion_report(result, normalized['duration']))
        if plan is not None:
            from .motion_plan import element_report
            elements = element_report(result, plan)
            motion['elements'] = elements
            if elements.get('issues'):
                detail = '; '.join(elements['issues'][:3])
                notice = f'Element check: {detail}.'
                motion['notice'] = (f"{motion['notice']} {notice}"
                                    if motion.get('notice') else notice)
    # Motion Canvas quantizes to frames; a nominal endpoint may be one frame
    # beyond the measured last frame. Only that rounding tolerance is accepted.
    # Two nominal cue times inside that tolerance clamp onto the same last frame;
    # keep the last, which is the cue the player displays at that time.
    if 'cues' in normalized:
        clamped = {}
        for cue in normalized['cues']:
            clamped[min(cue['time'], normalized['duration'])] = cue
        normalized['cues'] = [cue | {'time': at} for at, cue in clamped.items()]
    validate_scene(normalized)
    return normalized


def script_text(value):
    # Prevent source strings/comments from terminating an inline script element.
    return re.sub(r'</script', r'<\\/script', value, flags=re.IGNORECASE)


def player_document(scene):
    """Used ONLY inside sandbox=allow-scripts frames, including offline export."""
    scene = validate_scene(scene)
    compiled = scene.get('compiled') or compile_source(scene['source'])
    runtime = (STATIC / 'motion-canvas-runtime.js').read_text()
    player = (STATIC / 'motion-canvas-frame.js').read_text()
    metadata = json.dumps({k: v for k, v in scene.items() if k not in ('source', 'compiled')},
                          ensure_ascii=True).replace('<', '\\u003c')
    return ('<!doctype html><html><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data: blob:; '
        'font-src data:; connect-src \'none\'; base-uri \'none\'; form-action \'none\'">'
        '<style>html,body{margin:0;background:#0d0d0d;overflow:hidden;color:#e0e0e0;'
        'font:16px system-ui}canvas{display:block;width:100%;height:auto}#error{padding:24px}</style>'
        f'<title>{html.escape(scene["title"])}</title></head><body>'
        '<script>window.addEventListener("error",e=>parent.postMessage('
        '{channel:"blackrose-motion",type:"error",message:String(e.message)},"*"));</script>'
        f'<script>{script_text(runtime)}</script><script>{script_text(compiled)}</script>'
        f'<script>const sceneMetadata={metadata};{script_text(player)}</script></body></html>')
