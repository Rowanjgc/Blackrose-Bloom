"""Observed authoring failures and real, supervised runtime regression cases."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from blackrose.animations import normalize_arguments
from blackrose import motion_canvas as motion
from test_motion_canvas import HAS_COMPILER, HAS_PLAYBACK, example, isolate_platform_caches


def setUpModule():
    isolate_platform_caches()


def scene(body, imports=''):
    return normalize_arguments({'scene': {
        'title': 'Probe', 'description': 'Validation regression fixture', 'duration': 8,
        'source': "import {makeScene2D, Circle, Path, Line} from '@motion-canvas/2d';\n"
                  "import {waitFor} from '@motion-canvas/core';\n" + imports + '\n'
                  'export default makeScene2D(function* (view) {\n' + body + '\n});',
    }})['scene']


@unittest.skipUnless(HAS_COMPILER, 'Build animation-runtime first')
class SourceDiagnosticsTests(unittest.TestCase):
    def test_observed_authoring_errors_are_rejected_with_locations(self):
        cases = [
            ("export const scene = {};", 'Export default'),
            ("export default MissingThing;", 'Cannot find name'),
            (scene('view.add(<Path points={[[0,0],[100,100]]}/>);')['source'], 'points'),
            (scene('const ring = new Circle(); ring.nonexistent(12);')['source'], 'nonexistent'),
            ('/// <reference path="/etc/passwd"/>\nexport default {};', 'Reference directives'),
            ("import type {Secret} from '../private'; export default {} as Secret;", 'Cannot find module'),
        ]
        for source, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, 'scene.tsx:') as raised:
                motion.compile_source(source)
            self.assertIn(message, str(raised.exception))

    def test_aliases_helpers_and_reactive_signals_remain_supported(self):
        source = """import {makeScene2D as scene, Circle as Ring} from '@motion-canvas/2d';
import {createSignal, linear} from '@motion-canvas/core';
const animation = scene(function* (view) {
  const progress = createSignal(0);
  const add = () => view.add(<Ring size={40} fill={null} x={() => progress() * 100}/>);
  add();
  yield* progress(1, 2, linear);
});
export {animation as default};"""
        self.assertIn('BlackroseScene', motion.compile_source(source))

    def test_unscheduled_generator_calls_are_rejected_but_spawn_is_allowed(self):
        source = scene('''const ring = createRef<Circle>();
view.add(<Circle ref={ring} size={40} fill="#ef6461"/>);
ring().opacity(1, 0.2);
all(ring().x(10, 1));
waitFor(1);
spawn(function* () { yield* waitFor(1); });
yield* ring().opacity(1, 0.2);
yield* waitFor(1);''', "import {createRef, all, spawn} from '@motion-canvas/core';")['source']
        with self.assertRaisesRegex(ValueError, 'not scheduled') as raised:
            motion.compile_source(source)
        text = str(raised.exception)
        self.assertEqual(text.count('not scheduled'), 3)
        self.assertIn('scene.tsx:', text)

    def test_evolving_empty_arrays_support_incremental_point_geometry(self):
        source = scene('''const points = [];
points.push(new Vector2(-180, 40));
points.push(new Vector2(0, -20));
points.push(new Vector2(180, 40));
view.add(<Line points={points} stroke="#ef6461" lineWidth={4}/>);
yield* waitFor(1);''', "import {Vector2} from '@motion-canvas/core';")['source']
        self.assertIn('BlackroseScene', motion.compile_source(source))


@unittest.skipUnless(HAS_PLAYBACK, 'WebKit and a display are required')
class PlaybackValidationTests(unittest.TestCase):
    def test_common_unpainted_and_transparent_color_literals_are_normalized(self):
        original = scene('''view.add(new Circle({x:-60, size:70, fill:'none', stroke:'#ef6461', lineWidth:5}));
view.add(<Circle x={60} size={70} fill="transparent" stroke="#e4bd74" lineWidth={5}/>);
view.add(<Circle y={100} size={24} fill="null" stroke="none"/>);
yield* waitFor(1);''')
        prepared = motion.prepare_scene(original)
        self.assertEqual(prepared['source'], original['source'])
        self.assertEqual(prepared['duration'], 1)

    def test_measured_duration_replaces_metadata_and_cues_use_actual_timeline(self):
        short = scene('view.add(<Circle size={60} fill="#ef6461"/>); yield* waitFor(2);')
        measured = motion.prepare_scene(short)['duration']
        self.assertAlmostEqual(measured, 2, delta=1/30 + 1e-6)
        endpoint = motion.prepare_scene(short | {'cues': [{'time': 2, 'text': 'Finished'}]})
        self.assertEqual(endpoint['cues'][0]['time'], endpoint['duration'])
        short['cues'] = [{'time': 6, 'text': 'Never reached'}]
        with self.assertRaisesRegex(ValueError, 'Caption lies beyond measured timeline'):
            motion.prepare_scene(short)

    def test_runtime_errors_and_invalid_scene_objects_are_rejected(self):
        cases = [
            (scene('throw new Error("runtime fixture");'), 'runtime fixture'),
            (scene('view.add(<Circle size={50} fill={String.fromCharCode(110,111,110,101)}/>); yield* waitFor(1);'), 'none'),
            (scene('yield* waitFor(121);'), '0.5..120'),
            (scene('yield* waitFor(.1);'), '0.5..120'),
            (scene('') | {'source': 'export default {};'}, 'Export default makeScene2D'),
        ]
        for spec, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                motion.prepare_scene(spec)

    def test_render_error_after_startup_is_rejected(self):
        late = scene('''const progress = createSignal(0);
view.add(<Circle size={60} fill={() => {
  if (progress() > 2) throw new Error('late render failure');
  return '#ef6461';
}}/>); yield* progress(5, 5);''', "import {createSignal} from '@motion-canvas/core';")
        with self.assertRaisesRegex(ValueError, 'late render failure'):
            motion.prepare_scene(late)

    def test_supervisor_terminates_nonterminating_sources(self):
        # Test both a blocking generator and one that yields an endless timeline.
        for body in ('while (true) {}', 'while (true) yield* waitFor(1);'):
            compiled = scene(body)
            compiled['compiled'] = motion.compile_source(compiled['source'])
            started = time.monotonic()
            with patch.object(motion, 'PLAYBACK_TIMEOUT', 3), self.assertRaisesRegex(ValueError, 'exceeded 3 seconds'):
                motion.probe_document(motion.player_document(compiled))
            self.assertLess(time.monotonic() - started, 6)
        # The worker and shared display are still usable after termination.
        self.assertEqual(motion.prepare_scene(example() | {'version': 2, 'engine': 'motion-canvas'})['duration'], 8)

    def test_all_references_render_and_backward_seek_returns_to_first_frame(self):
        for path in sorted((motion.RUNTIME.parent / 'skills/explanatory-animations/references').glob('*.tsx')):
            with self.subTest(reference=path.name):
                prepared = motion.prepare_scene(scene('') | {'source': path.read_text(), 'duration': 120})
                result = motion.probe_document(motion.player_document(prepared))
                self.assertGreaterEqual(len(result['samples']), 6)
                self.assertEqual(result['samples'][0]['time'], 0)
                self.assertEqual(result['samples'][-1]['time'], 0)
                self.assertEqual(result['samples'][0]['hash'], result['samples'][-1]['hash'])
                self.assertTrue(any(sample['foreground'] for sample in result['samples']))
                self.assertGreater(len({sample['hash'] for sample in result['samples']}), 1)
