"""Validate a small animation plan and compile its host-owned timeline.

Model-authored visual code remains untrusted Motion Canvas source and is subject to
exactly the same compilation and isolated playback checks as direct authoring.
No plan string is interpolated as executable TypeScript.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math

import jsonschema

_ID = {'type': 'string', 'pattern': r'^[A-Za-z][A-Za-z0-9_-]{0,47}$'}
_TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 500}
_NUMBER = {'type': 'number', 'minimum': -10000, 'maximum': 10000}
_TRANSITION = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'target': _ID, 'property': {'enum': ['x', 'y', 'opacity', 'rotation', 'scale']},
                   'value': _NUMBER, 'duration': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 120}},
    'required': ['target', 'property', 'value', 'duration'],
}
PLAN_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'version': {'type': 'integer', 'const': 1}, 'objective': _TEXT, 'scene': _TEXT,
        'constraints': _TEXT,
        'elements': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'id': _ID, 'description': _TEXT, 'label': _TEXT,
                           'initial': {'type': 'object', 'additionalProperties': False,
                               'properties': {k: _NUMBER for k in ('x', 'y', 'opacity', 'rotation', 'scale')},
                               'required': ['x', 'y']},
                           'final': {'type': 'object', 'minProperties': 1, 'additionalProperties': False,
                               'properties': {k: _NUMBER for k in ('x', 'y', 'opacity', 'rotation', 'scale')}},
                           'relationship': _TEXT},
            'required': ['id', 'description', 'initial', 'final']}},
        'timeline': {'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': {
            'oneOf': [
                {'type': 'object', 'additionalProperties': False,
                 'properties': {'wait': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 120}},
                 'required': ['wait']},
                {'type': 'object', 'additionalProperties': False,
                 'properties': {'transition': _TRANSITION}, 'required': ['transition']},
                {'type': 'object', 'additionalProperties': False,
                 'properties': {'parallel': {'type': 'array', 'minItems': 2, 'maxItems': 32,
                                             'items': _TRANSITION}}, 'required': ['parallel']},
            ]}},
        'final_hold': {'type': 'number', 'minimum': 0, 'maximum': 120},
    },
    'required': ['version', 'objective', 'scene', 'constraints', 'elements', 'timeline', 'final_hold'],
}


def validate_plan(raw):
    """Return a detached canonical plan and its finite duration, or a diagnostic."""
    try:
        json.dumps(raw, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ValueError('Plan must be finite UTF-8 JSON') from error
    plan = deepcopy(raw)
    # An integer version is unambiguous; do not infer missing content or timing.
    if isinstance(plan, dict) and plan.get('version') == '1':
        plan['version'] = 1
    error = next(jsonschema.Draft202012Validator(PLAN_SCHEMA).iter_errors(plan), None)
    if error:
        path = '.'.join(map(str, error.absolute_path)) or 'plan'
        raise ValueError(f'Invalid plan {path}: {error.message[:300]}')
    ids = [element['id'] for element in plan['elements']]
    if len(set(ids)) != len(ids):
        raise ValueError('Plan element IDs must be unique')
    for element in plan['elements']:
        opacity = element['initial'].get('opacity', 1)
        if not 0 <= opacity <= 1:
            raise ValueError(f"element {element['id']}: initial opacity must be between 0 and 1")
    total = float(plan['final_hold'])
    states = {element['id']: dict(element['initial']) for element in plan['elements']}
    for index, step in enumerate(plan['timeline']):
        if 'wait' in step:
            total += step['wait']
            continue
        transitions = step.get('parallel', [step.get('transition')])
        targets = set()
        for transition in transitions:
            target = transition['target']
            prop = transition['property']
            if target not in ids:
                raise ValueError(f'timeline.{index}: unknown target {target!r}')
            if (target, prop) in targets:
                raise ValueError(f'timeline.{index}: concurrent transitions conflict on {target}.{prop}')
            targets.add((target, prop))
            if prop == 'opacity' and not 0 <= transition['value'] <= 1:
                raise ValueError(f'timeline.{index}: opacity must be between 0 and 1')
            states[target][prop] = transition['value']
        total += max(t['duration'] for t in transitions)
    for element in plan['elements']:
        for prop, value in element['final'].items():
            if states[element['id']].get(prop) != value:
                raise ValueError(f"element {element['id']}: final {prop} does not match the timeline")
    if not math.isfinite(total) or not 0.5 <= total <= 120:
        raise ValueError('Plan total duration must be between 0.5 and 120 seconds')
    return plan, total


_DEFAULT_PROPERTY = {'x': 0, 'y': 0, 'opacity': 1, 'rotation': 0, 'scale': 1}
_MOTION_EPSILON = {'x': 0.5, 'y': 0.5, 'opacity': 0.02, 'rotation': 0.5, 'scale': 0.01}


def _transitions(plan):
    for step in plan.get('timeline', []):
        if 'transition' in step:
            yield step['transition']
        elif 'parallel' in step:
            yield from step['parallel']


def _in_frame(observation):
    x = observation.get('absoluteX', observation.get('x'))
    y = observation.get('absoluteY', observation.get('y'))
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return True
    width = abs(observation.get('width') or 0)
    height = abs(observation.get('height') or 0)
    return (x + width / 2 >= -480 and x - width / 2 <= 480
            and y + height / 2 >= -270 and y - height / 2 <= 270)


def element_report(result, plan):
    """Check that planned elements appear and move in the rendered scene.

    The probe reports absolute opacity, position, size, rotation and scale for
    every node the host named from the plan. This is a presence-and-change check,
    not a geometry or occlusion verdict.
    """
    samples = result.get('samples') if isinstance(result, dict) else None
    observations = {}
    if isinstance(samples, list):
        for sample in samples:
            elements = sample.get('elements') if isinstance(sample, dict) else None
            if not isinstance(elements, dict):
                continue
            for element_id, raw in elements.items():
                if not isinstance(raw, dict):
                    continue
                values = {}
                for prop in (*_DEFAULT_PROPERTY, 'absoluteX', 'absoluteY', 'width', 'height'):
                    value = raw.get(prop)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) \
                            and math.isfinite(value):
                        values[prop] = float(value)
                if values:
                    observations.setdefault(element_id, []).append(values)
    if not observations:
        return {'assessment': 'unknown', 'issues': [], 'checked': 0}
    issues = []
    checked = 0
    for element in plan.get('elements', []):
        element_id = element['id']
        seen = observations.get(element_id)
        if not seen:
            issues.append(f"planned element '{element_id}' is missing from the runtime scene")
            continue
        checked += 1
        state = {**_DEFAULT_PROPERTY, **element.get('initial', {})}
        moving = set()
        should_be_visible = state['opacity'] > 0.01
        for transition in _transitions(plan):
            if transition.get('target') != element_id:
                continue
            prop, value = transition.get('property'), transition.get('value')
            if prop not in _DEFAULT_PROPERTY or not isinstance(value, (int, float)):
                continue
            if prop == 'opacity' and value > 0.01:
                should_be_visible = True
            if abs(value - state[prop]) > _MOTION_EPSILON[prop]:
                moving.add(prop)
            state[prop] = value
        if element.get('final', {}).get('opacity', state['opacity']) > 0.01:
            should_be_visible = True
        visible = any(o.get('opacity', 0) > 0.01 and _in_frame(o) for o in seen)
        if should_be_visible and not visible:
            issues.append(f"planned element '{element_id}' never becomes visible")
        for prop in sorted(moving):
            values = [o[prop] for o in seen if prop in o]
            if len(values) < 2 or max(values) - min(values) <= _MOTION_EPSILON[prop]:
                issues.append(f"planned element '{element_id}' never changes {prop}")
    return {'assessment': 'visible' if not issues else 'incomplete',
            'issues': issues[:12], 'checked': checked}


def assemble_source(plan, visual_source):
    """Append fixed scheduling code; plan values enter only as JSON literals."""
    plan, duration = validate_plan(plan)
    if not isinstance(visual_source, str) or not visual_source.strip():
        raise ValueError('Visual source is required')
    def transition(item):
        return (f'__br_nodes[{json.dumps(item["target"])}].{item["property"]}'
                f'({json.dumps(item["value"], allow_nan=False)}, {json.dumps(item["duration"], allow_nan=False)})')
    lines = []
    for step in plan['timeline']:
        if 'wait' in step:
            lines.append(f'yield* __br_wait({json.dumps(step["wait"], allow_nan=False)});')
        elif 'transition' in step:
            lines.append(f'yield* {transition(step["transition"])};')
        else:
            lines.append('yield* __br_all(' + ', '.join(map(transition, step['parallel'])) + ');')
    if plan['final_hold']:
        lines.append(f'yield* __br_wait({json.dumps(plan["final_hold"], allow_nan=False)});')
    keys = json.dumps([element['id'] for element in plan['elements']])
    initial = []
    for element in plan['elements']:
        for prop, value in element['initial'].items():
            initial.append(f'__br_nodes[{json.dumps(element["id"])}].{prop}({json.dumps(value, allow_nan=False)});')
    scaffold = '''
import {makeScene2D as __br_scene} from '@motion-canvas/2d';
import {all as __br_all, waitFor as __br_wait} from '@motion-canvas/core';
export default __br_scene(function* (__br_view) {
  const __br_nodes: Record<string, any> = build(__br_view);
  for (const __br_key of __BR_KEYS__) {
    if (!__br_nodes[__br_key]) throw new Error('Missing planned element: ' + __br_key);
  }
  // The probe reads this inventory to check planned visibility and motion.
  (globalThis as any).__blackrosePlanElements = __br_nodes;
  __BR_INITIAL__
  __BR_TIMELINE__
});
'''.replace('__BR_KEYS__', keys).replace('__BR_INITIAL__', '\n  '.join(initial)).replace('__BR_TIMELINE__', '\n  '.join(lines))
    return visual_source + '\n' + scaffold, duration
