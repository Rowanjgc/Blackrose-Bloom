"""Bounded animation data: no generated source, expressions, URLs or markup execute."""
from __future__ import annotations

import html
import json
from pathlib import Path
from copy import deepcopy

import jsonschema

from . import motion_canvas

MAX_BYTES = 64_000
MIME_TYPE = 'application/vnd.blackrose.animation+json'


def obj(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required,
            'additionalProperties': False}


def number(low, high):
    return {'type': 'number', 'minimum': low, 'maximum': high}


LABEL = {'type': 'string', 'maxLength': 160}
IDENTIFIER = {'type': 'string', 'pattern': '^[a-zA-Z][a-zA-Z0-9_-]{0,39}$'}
TRACK_VALUE_LIMITS = {'x': 960, 'y': 540, 'opacity': 1, 'emphasis': 1}
NODE_SCHEMA = obj({
    'id': IDENTIFIER,
    'kind': {'type': 'string', 'enum': ['box', 'text', 'dot', 'arrow']},
    'x': number(0, 960), 'y': number(0, 540),
    'x2': number(0, 960), 'y2': number(0, 540),
    'width': number(20, 900), 'height': number(20, 480),
    'radius': number(3, 60), 'label': LABEL,
    'color': {'type': 'string', 'enum': ['neutral', 'cherry', 'rose', 'blue', 'gold', 'green', 'muted']},
    'opacity': number(0, 1), 'emphasis': number(0, 1),
}, ['id', 'kind', 'x', 'y'])
TRACK_SCHEMA = obj({
    'node': IDENTIFIER,
    'property': {'type': 'string', 'enum': list(TRACK_VALUE_LIMITS),
                 'description': 'x values: 0..960; y: 0..540; opacity and emphasis: '
                                'decimal fractions 0..1, never percentages or pixel coordinates.'},
    'easing': {'type': 'string', 'enum': ['linear', 'smooth', 'step']},
    'keyframes': {'type': 'array', 'minItems': 2, 'maxItems': 32,
                  'items': obj({'time': number(0, 120), 'value': number(0, 960)}, ['time', 'value'])},
}, ['node', 'property', 'keyframes'])
# llama.cpp compiles anyOf alternatives independently, ignoring sibling fields.
# Each branch must carry the complete track/keyframe shape or its grammar can
# forbid node/time even though JSON Schema validation requires those fields.
_track_alternatives = []
for _prop, _limit in TRACK_VALUE_LIMITS.items():
    _branch = deepcopy(TRACK_SCHEMA)
    _branch['properties']['property']['enum'] = [_prop]
    _branch['properties']['keyframes']['items']['properties']['value'] = number(0, _limit)
    _track_alternatives.append(_branch)
TRACK_SCHEMA['anyOf'] = _track_alternatives
SCENE_SCHEMA = obj({
    'version': {'type': 'integer', 'const': 1},
    'title': {'type': 'string', 'minLength': 1, 'maxLength': 120},
    'description': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
    'duration': number(0.5, 120),
    'loop': {'type': 'boolean', 'description': 'Repeat on playback when requested; starts paused.'},
    'nodes': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': NODE_SCHEMA},
    'tracks': {'type': 'array', 'minItems': 1, 'maxItems': 128, 'items': TRACK_SCHEMA},
    'cues': {'type': 'array', 'maxItems': 32, 'items': obj({
        'time': number(0, 120), 'text': {'type': 'string', 'minLength': 1, 'maxLength': 240},
    }, ['time', 'text'])},
}, ['version', 'title', 'description', 'duration', 'nodes', 'tracks'])
_VALIDATOR = jsonschema.Draft202012Validator(SCENE_SCHEMA)
# The app owns the current format version. Model calls may omit it, while saved
# scenes remain self-describing and strict when reopened or exported.
SCENE_INPUT_SCHEMA = deepcopy(SCENE_SCHEMA)
SCENE_INPUT_SCHEMA['required'].remove('version')
SCENE_INPUT_SCHEMA['properties']['version'].update(
    default=1, description='Format version. Omit to use 1; explicit values must be 1.')
# Parameters of the animation_create tool. Owned here so tools.py advertises them and
# repair_details validates against them without importing tools.py back.
ANIMATION_CREATE_PARAMETERS = {
    'type': 'object',
    'properties': {
        'scene': motion_canvas.INPUT_SCHEMA,
        'loop': {'type': 'boolean', 'description': 'Whether playback repeats.'},
        'revises_attachment_id': {'type': 'integer', 'minimum': 1},
    },
    'required': ['scene', 'loop'],
    'additionalProperties': False,
}

MODEL_INSTRUCTIONS = motion_canvas.MODEL_INSTRUCTIONS


class AnimationValidationError(ValueError):
    """A rejected scene carries its field path into model repair feedback."""

    def __init__(self, message: str, path: str = 'scene'):
        super().__init__(message)
        self.path = path


def validate_scene(scene: dict) -> dict:
    """Validate both structure and cross references, including non-finite JSON numbers."""
    if motion_canvas.is_motion_scene(scene):
        return motion_canvas.validate_scene(scene)
    try:
        raw = json.dumps(scene, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise AnimationValidationError('Animation must be finite UTF-8 JSON data') from error
    if len(raw) > MAX_BYTES:
        raise AnimationValidationError('Animation exceeds 64000 bytes; simplify the scene')
    error = next(_VALIDATOR.iter_errors(scene), None)
    if error:
        path = '.'.join(map(str, error.absolute_path)) or 'scene'
        raise AnimationValidationError(f'Invalid animation {path}: {error.message[:400]}',
                                       'scene.' + path if path != 'scene' else 'scene')
    nodes = {}
    for index, node in enumerate(scene['nodes']):
        if node['id'] in nodes:
            raise AnimationValidationError(f"Duplicate node ID: {node['id']}", f'scene.nodes.{index}.id')
        nodes[node['id']] = node
        if node['kind'] == 'arrow' and not {'x2', 'y2'} <= node.keys():
            raise AnimationValidationError(f"Arrow {node['id']} requires x2 and y2", f'scene.nodes.{index}')
        for axis in ('x', 'y'):
            _check_position(node, axis, node[axis], f'scene.nodes.{index}.{axis}')
    for track_index, track in enumerate(scene['tracks']):
        if track['node'] not in nodes:
            raise AnimationValidationError(f"Unknown track node: {track['node']}",
                                           f'scene.tracks.{track_index}.node')
        limit = TRACK_VALUE_LIMITS[track['property']]
        previous = -1
        for index, frame in enumerate(track['keyframes']):
            if not previous < frame['time'] <= scene['duration']:
                raise AnimationValidationError(
                    f"Track {track['node']}.{track['property']} keyframes.{index}.time "
                    f"must increase strictly and fit within duration {scene['duration']}",
                    f'scene.tracks.{track_index}.keyframes.{index}.time')
            if frame['value'] > limit:
                raise AnimationValidationError(f"Track {track['property']} values must be 0..{limit}",
                                               f'scene.tracks.{track_index}.keyframes.{index}.value')
            if track['property'] in ('x', 'y'):
                _check_position(nodes[track['node']], track['property'], frame['value'],
                                f'scene.tracks.{track_index}.keyframes.{index}.value')
            previous = frame['time']
    previous = -1
    for index, cue in enumerate(scene.get('cues', [])):
        if not previous < cue['time'] <= scene['duration']:
            raise AnimationValidationError(f'Cues.{index}.time must increase strictly and fit within duration',
                                           f'scene.cues.{index}.time')
        previous = cue['time']
    # Local models sometimes emit a track per phase. Canonicalize compatible
    # segments after validating every input frame; never guess conflicting intent.
    normalized = deepcopy(scene)
    try:
        normalized['tracks'] = _merge_tracks(scene['tracks'])
    except ValueError as error:
        raise AnimationValidationError(str(error), 'scene.tracks') from error
    return normalized


def _merge_tracks(tracks: list[dict]) -> list[dict]:
    groups = {}
    for track in tracks:
        groups.setdefault((track['node'], track['property']), []).append(track)
    merged = []
    for (node, prop), parts in groups.items():
        easings = {part.get('easing', 'smooth') for part in parts}
        if len(easings) > 1:
            raise ValueError(f'Conflicting easing for {node}.{prop}. Use one track with one easing '
                             'and all phases in its keyframes.')
        if len(parts) > 1 and prop in ('opacity', 'emphasis') and all(
                p['keyframes'][0]['value'] == p['keyframes'][-1]['value'] == 0 for p in parts):
            combined = deepcopy(parts[0])
            combined['keyframes'] = _merge_highlight_phases(node, prop, parts)
            merged.append(combined)
            continue
        combined = None
        for part in sorted(parts, key=lambda t: t['keyframes'][0]['time']):
            if combined is None:
                combined = deepcopy(part)
                continue
            existing = combined['keyframes']
            frames = part['keyframes']
            if frames == existing or any(existing[i:i + len(frames)] == frames
                                         for i in range(len(existing) - len(frames) + 1)):
                continue  # An exactly repeated segment adds no information.
            boundary, first = existing[-1], frames[0]
            if first['time'] < boundary['time'] or (
                    first['time'] == boundary['time'] and first['value'] != boundary['value']):
                raise ValueError(f'Conflicting tracks for {node}.{prop} at {first["time"]}s '
                                 f'(previous segment ends at {boundary["time"]}s). '
                                 'Use one track with strictly increasing times and one value per time. '
                                 'Put every highlight on/off phase in that track; use loop=true for repetition.')
            existing.extend(deepcopy(frames[1:] if first['time'] == boundary['time'] else frames))
            if len(existing) > 32:
                raise ValueError(f'Merged track {node}.{prop} exceeds 32 keyframes; simplify its phases')
        merged.append(combined)
    return merged


def _merge_highlight_phases(node, prop, parts):
    """Remove neutral padding around separate pulses without changing their curves.

    A model may surround every highlight with 0s spanning the whole scene.
    Idle padding can overlap; active intervals must be disjoint or identical.
    """
    phases = []
    for part in parts:
        frames = part['keyframes']
        phase = []
        for a, b in zip(frames, frames[1:], strict=False):
            if a['value'] or b['value']:
                if not phase:
                    phase.append(a)
                phase.append(b)
            elif phase:
                if phase not in phases:
                    phases.append(phase)
                phase = []
        if phase and phase not in phases:
            phases.append(phase)
    frames = [{'time': min(p['keyframes'][0]['time'] for p in parts), 'value': 0}]
    for phase in sorted(phases, key=lambda f: f[0]['time']):
        if phase[0]['time'] < frames[-1]['time']:
            raise ValueError(f'Conflicting tracks for {node}.{prop}: active highlight phases overlap. '
                             'Use one track containing every on/off phase with strictly increasing times.')
        frames.extend(deepcopy(phase[1:] if phase[0]['time'] == frames[-1]['time'] else phase))
    end = max(p['keyframes'][-1]['time'] for p in parts)
    if end > frames[-1]['time']:
        frames.append({'time': end, 'value': 0})
    if len(frames) > 32:
        raise ValueError(f'Merged track {node}.{prop} exceeds 32 keyframes; simplify its phases')
    return frames


def _check_position(node: dict, axis: str, value: float, path: str = 'scene') -> None:
    extent = 960 if axis == 'x' else 540
    if node['kind'] == 'box':
        size = node.get('width', 160) if axis == 'x' else node.get('height', 64)
        low, high = 0, extent - size
    elif node['kind'] == 'dot':
        radius = node.get('radius', 9)
        low, high = radius, extent - radius
    else:
        return
    if not low <= value <= high:
        raise AnimationValidationError(
            f"Node {node['id']} {axis} must be {low}..{high} to keep its "
            f"{node['kind']} inside the canvas (including tracked positions)", path)


def encode_scene(scene: dict, *, motion=None, plan=None) -> bytes:
    scene = (motion_canvas.prepare_scene(scene, motion=motion, plan=plan)
             if motion_canvas.is_motion_scene(scene) else validate_scene(scene))
    return json.dumps(scene, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def normalize_arguments(arguments):
    """Expose playback as an explicit tool flag; preserve older scene-only calls."""
    if not isinstance(arguments, dict) or not isinstance(arguments.get('scene'), dict):
        return arguments
    normalized = dict(arguments)
    # Some local models split scene fields across the envelope and scene.
    # Move only unambiguous misplaced fields; conflicts/unknowns still fail validation.
    scene = dict(arguments['scene'])
    for key in SCENE_SCHEMA['properties'].keys() - {'loop'}:
        if key in normalized and key not in scene:
            scene[key] = normalized.pop(key)
    if motion_canvas.is_motion_scene(scene):
        scene.setdefault('version', 2)
        scene.setdefault('engine', 'motion-canvas')
    else:
        scene.setdefault('version', 1)
    normalized['scene'] = scene
    normalized['loop'] = arguments.get('loop', arguments['scene'].get('loop', False))
    if type(normalized['loop']) is bool and ('loop' in arguments or 'loop' in arguments['scene']):
        normalized['scene']['loop'] = normalized['loop']
    return normalized


def repair_details(arguments, failure: Exception | None = None):
    """Report schema and semantic failures using the same scene constraints."""
    schema = ANIMATION_CREATE_PARAMETERS
    scene = arguments.get('scene') if isinstance(arguments, dict) else None
    if (motion_canvas.is_motion_scene(scene) or isinstance(scene, dict)
            and not any(key in scene for key in ('nodes', 'tracks'))):
        schema = {**schema, 'properties': {**schema['properties'], 'scene': motion_canvas.INPUT_SCHEMA}}
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(arguments))
        return {'validation_errors': [
            {'path': '.'.join(map(str, e.absolute_path)) or 'arguments', 'message': e.message[:400]}
            for e in errors[:8]] or [{'path': 'scene.source', 'message': str(failure)[:3000]}],
            'required_shape': {'loop': 'boolean', 'scene': {'title': 'string', 'description': 'string',
                               'duration': 'seconds', 'source': 'complete Motion Canvas TSX module'}}}
    schema = {**schema, 'properties': {**schema['properties'], 'scene': SCENE_INPUT_SCHEMA}}
    def track_at(index):
        scene = arguments.get('scene') if isinstance(arguments, dict) else None
        tracks = scene.get('tracks') if isinstance(scene, dict) else None
        if (isinstance(tracks, list) and type(index) is int
                and 0 <= index < len(tracks) and isinstance(tracks[index], dict)):
            return tracks[index]
        return None

    def field_errors():
        for error in jsonschema.Draft202012Validator(schema).iter_errors(arguments):
            path = list(error.absolute_path)
            if (error.validator == 'anyOf' and len(path) == 3
                    and path[:2] == ['scene', 'tracks']):
                track = track_at(path[2])
                prop = track.get('property') if track else None
                if isinstance(prop, str) and prop in TRACK_VALUE_LIMITS:
                    branch = list(TRACK_VALUE_LIMITS).index(prop)
                    relevant = [child for child in error.context if child.schema_path[0] == branch]
                    # Other branches describe different properties; their enum
                    # failures obscure the actual invalid keyframe value.
                    yield from relevant or [error]
                    continue
            yield error

    issues = []
    for error in field_errors():
        path = list(error.absolute_path)
        message = error.message[:400]
        if (len(path) == 6 and path[:2] == ['scene', 'tracks']
                and path[3] == 'keyframes' and path[5] == 'value'):
            track = track_at(path[2])
            prop = track.get('property') if track else None
            if isinstance(prop, str) and prop in TRACK_VALUE_LIMITS:
                message = (f"Track {prop} values must be 0..{TRACK_VALUE_LIMITS[prop]}; "
                           f"received {str(error.instance)[:80]}.")
                if prop in ('opacity', 'emphasis'):
                    message += ' Use decimal fractions such as 0, 0.5, 1; not percentages or pixel coordinates.'
        issue = {'path': '.'.join(map(str, path)) or 'arguments', 'message': message}
        if issue not in issues:
            issues.append(issue)
        if len(issues) == 8:
            break
    if not issues and isinstance(arguments, dict) and isinstance(arguments.get('scene'), dict):
        try:
            validate_scene(arguments['scene'])
        except ValueError as error:
            issues.append({'path': getattr(error, 'path', 'scene'), 'message': str(error)[:400]})
    if not issues and failure is not None:
        path = getattr(failure, 'path', 'arguments')
        if not isinstance(path, str):
            path = '.'.join(map(str, path)) or 'arguments'
        issues.append({'path': path, 'message': getattr(failure, 'message', str(failure))[:400]})
    return {
        'validation_errors': issues,
        'track_value_ranges': {prop: {'minimum': 0, 'maximum': limit}
                               for prop, limit in TRACK_VALUE_LIMITS.items()},
        'required_shape': {'loop': 'boolean', 'scene': {
            'version': 1, 'title': 'nonempty string', 'description': 'nonempty string',
            'duration': 'seconds, 0.5..120', 'nodes': 'array of nodes',
            'tracks': 'array of tracks', 'cues': 'optional array of captions'}},
    }


def load_scene(path: Path) -> dict:
    # Limit reads even when a stored file has been corrupted/replaced.
    with path.open('rb') as stream:
        data = stream.read(motion_canvas.MAX_SAVED_BYTES + 1)
    if len(data) > motion_canvas.MAX_SAVED_BYTES:
        raise ValueError('Stored animation exceeds 512000 bytes')
    return validate_scene(json.loads(data))


def export_html(scene: dict) -> str:
    """Offline, self-contained document; generated strings only enter as JSON data."""
    if motion_canvas.is_motion_scene(scene):
        scene = motion_canvas.validate_scene(scene)
        # The outer document is trusted, even for standalone exports. Generated
        # code always stays inside an opaque-origin sandboxed child frame.
        document = motion_canvas.player_document(scene)
        data = json.dumps({k: v for k, v in scene.items() if k not in ('source', 'compiled')}).replace('<', '\\u003c')
        static = Path(__file__).parent / 'gui' / 'static'
        script = (static / 'motion-canvas-player.js').read_text()
        css = (static / 'animations.css').read_text()
        return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; frame-src about:; '
            'base-uri \'none\'; form-action \'none\'">'
            f'<title>{html.escape(scene["title"])}</title><style>{css}</style>'
            '<body class="animation-document"><main id="animation"></main>'
            f'<script>{script}</script><script>BlackroseMotionPlayer.mount('
            f'document.getElementById("animation"),{data},null,'
            f'{json.dumps(document).replace("<", chr(92) + "u003c")},true);</script></body></html>')
    data = encode_scene(scene).decode().replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    static = Path(__file__).parent / 'gui' / 'static'
    script = (static / 'animations.js').read_text()
    css = (static / 'animations.css').read_text()
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'; '
            'base-uri \'none\'; form-action \'none\'">'
            f'<title>{html.escape(scene["title"])}</title><style>{css}</style>'
            '<body class="animation-document"><main id="animation"></main>'
            f'<script>{script}</script><script>BlackroseAnimations.mount('
            f'document.getElementById("animation"),{data});</script></body></html>')
