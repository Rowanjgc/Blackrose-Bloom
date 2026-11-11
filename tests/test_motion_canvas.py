"""Motion Canvas compilation boundaries and the real chat persistence path."""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from blackrose.animations import encode_scene, export_html, load_scene, normalize_arguments, repair_details
from blackrose.motion_canvas import (MOTION_GRID_COLUMNS, MOTION_GRID_ROWS, RUNTIME,
                                     compile_source, motion_report, prepare_scene, script_text,
                                     validate_scene)
from blackrose.tools import animation_tools, validate_call
from test_animations import ScriptedBackend
import test_gui_images as gui


def example():
    return {'title': 'Gears', 'description': 'Two meshing gears reverse rotation.', 'duration': 8,
            'source': (Path(__file__).resolve().parents[1] /
                       'skills/explanatory-animations/references/gears.tsx').read_text()}


HAS_COMPILER = shutil.which('node') and (RUNTIME / 'node_modules/esbuild').is_dir()
HAS_PLAYBACK = os.environ.get('BLACKROSE_TEST_WEBKIT') == '1' and HAS_COMPILER and importlib.util.find_spec('gi') and (
    os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def isolate_platform_caches():
    """Point XDG_CACHE_HOME at a scratch root for the rest of the calling module.

    The WebKit worker keeps its own storage in memory, but GStreamer and the GPU
    driver still cache their registry and shaders under XDG_CACHE_HOME, as in any
    GTK app. Tests must not write those into the developer's real cache.
    """
    root = tempfile.TemporaryDirectory(prefix='blackrose-platform-cache-')
    environment = patch.dict(os.environ, {'XDG_CACHE_HOME': root.name})
    environment.start()
    unittest.addModuleCleanup(root.cleanup)
    unittest.addModuleCleanup(environment.stop)


def setUpModule():
    isolate_platform_caches()


class MotionCanvasTests(unittest.TestCase):
    def test_new_authoring_contract_is_source_and_defaults_to_v2(self):
        arguments = normalize_arguments({'loop': True, 'scene': example()})
        validate_call(animation_tools([]), 'animation_create', arguments)
        scene = validate_scene(arguments['scene'])
        self.assertEqual((scene['engine'], scene['version'], scene['loop']), ('motion-canvas', 2, True))
        self.assertNotIn('tracks', animation_tools([])[0]['function']['parameters']['properties']['scene']['properties'])
        advertised = animation_tools([])[0]['function']['parameters']['properties']['scene']['properties']
        self.assertTrue({'source', 'title', 'description', 'duration'} <= advertised.keys())
        self.assertFalse({'version', 'engine', 'loop'} & advertised.keys())

    def test_bad_saved_scene_and_nonfinite_numbers_are_value_errors(self):
        scene = normalize_arguments({'scene': example()})['scene']
        for changes in ({'source': 12}, {'duration': float('nan')}, {'compiled': 'x' * 400001},
                        {'cues': [{'time': 9, 'text': 'Too late'}]}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                validate_scene(scene | changes)

    def test_script_escaping_preserves_regular_expression_literals(self):
        self.assertEqual(script_text('const x = /</g;'), 'const x = /</g;')
        self.assertNotIn('</script', script_text('"</ScRiPt><img>"').lower())

    def test_missing_source_repair_keeps_motion_canvas_contract(self):
        arguments = normalize_arguments({'scene': {'title': 'Example', 'description': 'Missing source', 'duration': 2}})
        result = repair_details(arguments, ValueError('Missing source'))
        self.assertIn('source', result['required_shape']['scene'])
        self.assertNotIn('nodes', result['required_shape']['scene'])

    @unittest.skipUnless(HAS_COMPILER, 'Run npm ci and npm run build in animation-runtime')
    def test_compiler_never_evaluates_generated_source_and_rejects_imports(self):
        self.assertIn('should not execute', compile_source('throw new Error("should not execute"); export default {};'))
        for path in ('node:fs', '/etc/passwd', '../private.js', 'https://example.com/a.js'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Import unavailable'):
                compile_source(f'import x from {json.dumps(path)}; export default x;')
        with self.assertRaisesRegex(ValueError, 'scene.tsx:'):
            compile_source('export default makeScene2D(function* (view) { <Circle size={ /> });')
        with self.assertRaisesRegex(ValueError, 'No matching export'):
            compile_source("import {Group} from '@motion-canvas/2d'; export default Group;")

    @unittest.skipUnless(HAS_PLAYBACK, 'WebKit and a display are required')
    def test_saved_source_round_trips_and_export_keeps_code_inside_sandbox(self):
        scene = normalize_arguments({'scene': example(), 'loop': False})['scene']
        data = encode_scene(scene)
        saved = json.loads(data)
        self.assertIn('BlackroseScene', saved['compiled'])
        self.assertEqual(saved['source'], scene['source'])
        exported = export_html(saved)
        self.assertIn("frame.setAttribute('sandbox', 'allow-scripts')", exported)
        self.assertNotIn('allow-same-origin', exported)
        self.assertIn('BlackroseMotionPlayer.mount', exported)


class MotionReportTests(unittest.TestCase):
    def grid(self, outside=0, caption=0):
        cells = [0] * (MOTION_GRID_COLUMNS * MOTION_GRID_ROWS)
        cells[4 * MOTION_GRID_COLUMNS + 5] = outside
        cells[(MOTION_GRID_ROWS - 1) * MOTION_GRID_COLUMNS + 3] = caption
        return cells

    def test_mechanism_motion_is_distinguished_from_caption_changes(self):
        moving = {'duration': 10, 'samples': [
            {'time': at, 'foreground': 100, 'grid': self.grid(10 + index, index)}
            for index, at in enumerate((0, 2, 4, 6, 8))]}
        self.assertEqual(motion_report(moving)['assessment'], 'moving')
        static = {'duration': 10, 'samples': [
            {'time': at, 'foreground': 100, 'grid': self.grid(10, index)}
            for index, at in enumerate((0, 2, 4, 6, 8))]}
        self.assertEqual(motion_report(static)['assessment'], 'static')
        limited = {'duration': 10, 'samples': [
            {'time': at, 'foreground': 100, 'grid': self.grid(10 if at < 2 else 0, index)}
            for index, at in enumerate((0, 2, 4, 6, 8))]}
        report = motion_report(limited)
        self.assertEqual(report['assessment'], 'limited')
        self.assertIn('after 2.00s of 10.00s', report['notice'])

    def test_blank_and_incomplete_reports_are_explicit(self):
        blank = {'duration': 4, 'samples': [
            {'time': at, 'foreground': 0, 'grid': self.grid()} for at in (0, 1, 2, 3)]}
        self.assertEqual(motion_report(blank)['assessment'], 'blank')
        self.assertEqual(motion_report({'duration': 4, 'samples': [
            {'time': 0, 'foreground': 1}]})['assessment'], 'unknown')

    def test_prepare_scene_merges_plan_element_report(self):
        from unittest.mock import patch
        from blackrose.motion_plan import validate_plan
        scene = normalize_arguments({'scene': example(), 'loop': False})['scene']
        plan, _ = validate_plan({
            'version': 1, 'objective': 'Move a dot', 'scene': 'A dot moves right',
            'constraints': 'Keep the dot in frame',
            'elements': [{'id': 'dot', 'description': 'Dot', 'initial': {'x': 0, 'y': 0},
                          'final': {'x': 100}}],
            'timeline': [{'transition': {'target': 'dot', 'property': 'x',
                                         'value': 100, 'duration': 1}}], 'final_hold': 1})
        dot = {'opacity': 1, 'x': 0, 'y': 0, 'width': 20, 'height': 20}
        moved = {**dot, 'x': 100}
        probe = {'duration': 8, 'samples': [
            {'time': 0, 'foreground': 100, 'grid': self.grid(10), 'elements': {'dot': dot}},
            {'time': 4, 'foreground': 100, 'grid': self.grid(11), 'elements': {'dot': moved}},
            {'time': 8, 'foreground': 100, 'grid': self.grid(11), 'elements': {'dot': moved}},
        ]}
        motion = {}
        with patch('blackrose.motion_canvas.compile_source', return_value='compiled'), \
                patch('blackrose.motion_canvas.probe_document', return_value=probe):
            prepared = prepare_scene(scene, motion=motion, plan=plan)
        self.assertEqual(prepared['duration'], 8)
        self.assertEqual(motion['elements']['assessment'], 'visible')
        self.assertNotIn('Element check', motion.get('notice') or '')


@unittest.skipUnless(HAS_PLAYBACK, 'WebKit and a display are required')
class MotionChatTests(unittest.IsolatedAsyncioTestCase):
    # Inherit the isolated controller/session fixture without rerunning its tests.
    async def asyncSetUp(self):
        await gui.ImageTests.asyncSetUp(self)
        self.runtime.controller.model_name = 'opencode-go/test-vision'
    asyncTearDown = gui.ImageTests.asyncTearDown

    async def test_runtime_rejection_retries_before_saving_and_reports_measured_duration(self):
        broken = example() | {'source': example()['source'].replace(
            'const angle = createSignal(0);', 'throw new Error("Preflight fixture"); const angle = createSignal(0);')}
        valid = example() | {'duration': 100}
        self.runtime.controller.backend = ScriptedBackend([
            ('animation_create', {'scene': broken, 'loop': False}),
            ('animation_create', {'scene': valid, 'loop': True}), 'Created.'])
        events = []
        turn = await self.runtime.controller.send('Animate gears',
            on_tool=lambda name, phase, result: events.append((name, phase, result)))
        self.assertEqual(len(turn.assistant_message.attachments), 1)
        result = next(result for name, phase, result in events if name == 'animation_create' and phase == 'complete')
        self.assertEqual(result['duration'], 8)
        self.assertEqual(result['validation']['playback'], 'passed')
        self.assertEqual(result['validation']['visual_quality'], 'unreviewed')
        self.assertIn(result['validation']['motion'], {'moving', 'limited', 'static', 'blank', 'unknown'})
        failure = next(result for name, phase, result in events if name == 'animation_create' and phase == 'error')
        self.assertIn('Preflight fixture', failure['error'])
        self.assertIn('TSX', failure['notice'])
        _, saved = await self.runtime.controller.read_animation(turn.assistant_message.attachments[0].id)
        self.assertEqual(saved['duration'], 8)

    async def test_source_creation_revision_and_compiler_repair(self):
        broken = example() | {'source': 'import x from "node:fs"; export default x;'}
        self.runtime.controller.backend = ScriptedBackend([
            ('animation_create', {'scene': broken, 'loop': False}),
            ('animation_create', {'scene': example(), 'loop': True}), 'Created.'])
        result = await self.runtime.controller.send('Animate gears')
        attachment = result.assistant_message.attachments[0]
        _, scene = await self.runtime.controller.read_animation(attachment.id)
        self.assertTrue(scene['loop'])
        self.assertEqual(scene['version'], 2)
        self.assertIn('compiled', scene)
        self.assertEqual(load_scene(self.runtime.controller.attachment_path(attachment)), scene)
        payload = self.session._attachment_payload(attachment)
        self.assertIn('player_document', payload)
        self.assertNotIn('compiled', payload['scene'])
        read = await self.runtime.controller._call_tool('animation_read', {'attachment_id': attachment.id})
        self.assertNotIn('compiled', read['scene'])
        self.assertEqual(read['scene']['source'], example()['source'])
        revision = example() | {'title': 'Slower gears', 'duration': 10,
            'source': example()['source'].replace('angle(720, 6, linear)', 'angle(720, 8, linear)')}
        self.runtime.controller.backend = ScriptedBackend([
            ('animation_create', {'scene': revision, 'loop': False, 'revises_attachment_id': attachment.id}), 'Revised.'])
        result = await self.runtime.controller.send('Slow down that animation')
        _, updated = await self.runtime.controller.read_animation(result.assistant_message.attachments[0].id)
        self.assertEqual(updated['duration'], 10)
        self.assertFalse(updated['loop'])
        self.assertEqual((await self.runtime.controller.read_animation(attachment.id))[1], scene)
