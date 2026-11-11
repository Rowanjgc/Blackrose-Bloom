"""Animation validation, chat tools, durable artifacts, revision and export boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock
from jsonschema import ValidationError

from blackrose.animations import (encode_scene, export_html, load_scene, validate_scene,
                                 normalize_arguments, repair_details)
from blackrose.attachments.images import inspect_image
from blackrose.image_generation import GeneratedImage
from blackrose.gui.render import to_html
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository
import test_gui_images as gui

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'skills/explanatory-animations/references/rag.animation.json'


def example():
    return json.loads(EXAMPLE.read_text())


def layer_scene(prop='opacity', value=1):
    return {'version': 1, 'title': 'Software layers', 'description': 'Highlight a software layer.',
            'duration': 2, 'nodes': [{'id': 'layer', 'kind': 'text', 'x': 100, 'y': 100}],
            'tracks': [{'node': 'layer', 'property': prop, 'keyframes': [
                {'time': 0, 'value': 0}, {'time': 2, 'value': value}]}]}


class ScriptedBackend(gui.Backend):
    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []
        self.definitions = []

    async def stream(self, model, messages, tools):
        self.requests.append(deepcopy(messages))
        self.definitions.append(deepcopy(tools))
        step = next(self.steps)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, str):
            yield {'delta': {'content': step}, 'finish_reason': 'stop'}
        else:
            name, args = step
            yield {'delta': {'tool_calls': [{'index': 0, 'id': f'call-{len(self.requests)}',
                'function': {'name': name, 'arguments': json.dumps(args)}}]}, 'finish_reason': 'tool_calls'}


class ValidationTests(unittest.TestCase):
    def test_track_alternatives_preserve_structure_for_constrained_generation(self):
        from jsonschema import Draft202012Validator
        from blackrose.animations import SCENE_INPUT_SCHEMA

        schema = SCENE_INPUT_SCHEMA
        # llama.cpp visits anyOf branches without their sibling constraints.
        # Validate each advertised branch alone, as its grammar will see it.
        for branch in schema['properties']['tracks']['items']['anyOf']:
            prop = branch['properties']['property']['enum'][0]
            valid = layer_scene(prop)['tracks'][0]
            validator = Draft202012Validator(branch)
            self.assertTrue(validator.is_valid(valid), prop)
            malformed = []
            for field in ('node', 'property', 'keyframes'):
                track = deepcopy(valid)
                del track[field]
                malformed.append(track)
            for field in ('time', 'value'):
                track = deepcopy(valid)
                del track['keyframes'][0][field]
                malformed.append(track)
            track = deepcopy(valid)
            track['keyframes'] = track['keyframes'][:1]
            malformed.append(track)
            track = deepcopy(valid)
            track['unexpected'] = True
            malformed.append(track)
            track = deepcopy(valid)
            track['keyframes'][0]['unexpected'] = True
            malformed.append(track)
            for track in malformed:
                with self.subTest(prop=prop, track=track):
                    self.assertFalse(validator.is_valid(track))

    def test_property_specific_ranges_match_tool_and_saved_scene_validation(self):
        from blackrose.tools import animation_tools, validate_call
        for prop, limit in [('x', 960), ('y', 540), ('opacity', 1), ('emphasis', 1)]:
            for value in (0, limit / 2, limit):
                with self.subTest(prop=prop, valid=value):
                    scene = layer_scene(prop, value)
                    validate_call(animation_tools([]), 'animation_create', {'scene': scene, 'loop': False})
                    self.assertEqual(validate_scene(scene), scene)
            for value in (-0.01, limit + 0.01):
                with self.subTest(prop=prop, invalid=value):
                    scene = layer_scene(prop, value)
                    with self.assertRaises(ValidationError):
                        validate_call(animation_tools([]), 'animation_create', {'scene': scene, 'loop': False})
                    with self.assertRaises(ValueError):
                        validate_scene(scene)

    def test_opacity_percentage_feedback_names_keyframe_and_fraction_range(self):
        arguments = {'scene': layer_scene(value=100), 'loop': False}
        before = deepcopy(arguments)
        details = repair_details(arguments)
        self.assertEqual(arguments, before)  # Never silently clamp or rescale model intent.
        self.assertEqual(len(details['validation_errors']), 1)
        issue = details['validation_errors'][0]
        self.assertEqual(issue['path'], 'scene.tracks.0.keyframes.1.value')
        self.assertIn('opacity values must be 0..1', issue['message'])
        self.assertIn('received 100', issue['message'])
        self.assertIn('not percentages', issue['message'])
        self.assertEqual(details['track_value_ranges']['opacity'], {'minimum': 0, 'maximum': 1})

    def test_semantic_repair_errors_include_the_failed_field(self):
        unknown = layer_scene()
        unknown['tracks'][0]['node'] = 'missing'
        duplicate = layer_scene()
        duplicate['nodes'].append(deepcopy(duplicate['nodes'][0]))
        timing = layer_scene()
        timing['tracks'][0]['keyframes'][-1]['time'] = 3
        position = layer_scene('x', 950)
        position['nodes'][0]['kind'] = 'box'
        for scene, path, message in [
                (unknown, 'scene.tracks.0.node', 'Unknown track node'),
                (duplicate, 'scene.nodes.1.id', 'Duplicate node ID'),
                (timing, 'scene.tracks.0.keyframes.1.time', 'fit within duration'),
                (position, 'scene.tracks.0.keyframes.1.value', 'inside the canvas')]:
            with self.subTest(path=path):
                issues = repair_details({'scene': scene, 'loop': False})['validation_errors']
                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0]['path'], path)
                self.assertIn(message, issues[0]['message'])

    def test_repair_details_remain_json_serializable_for_malformed_properties(self):
        for prop in (None, ['opacity'], {'value': 'opacity'}, 'unknown'):
            with self.subTest(prop=prop):
                details = repair_details({'scene': layer_scene(prop=prop), 'loop': False})
                self.assertTrue(details['validation_errors'])
                json.dumps(details, allow_nan=False)

    def test_repair_fallback_preserves_tool_envelope_error(self):
        from blackrose.tools import animation_tools, validate_call
        arguments = {'scene': layer_scene(), 'loop': False, 'revises_attachment_id': 999}
        try:
            validate_call(animation_tools([]), 'animation_create', arguments)
        except ValidationError as error:
            details = repair_details(arguments, error)
        else:
            self.fail('Unavailable revision argument should be rejected')
        self.assertTrue(details['validation_errors'])
        self.assertIn('revises_attachment_id', details['validation_errors'][0]['message'])
        json.dumps(details, allow_nan=False)

    def test_missing_input_version_defaults_but_saved_scenes_remain_strict(self):
        from blackrose.tools import animation_tools, validate_call
        scene = example()
        del scene['version']
        normalized = normalize_arguments({'scene': scene})
        self.assertNotIn('version', scene)
        self.assertEqual(normalized['scene']['version'], 1)
        validate_call(animation_tools([]), 'animation_create', normalized)
        self.assertEqual(validate_scene(normalized['scene']), example())
        with self.assertRaisesRegex(ValueError, 'version'):
            validate_scene(scene)
        schema = animation_tools([])[0]['function']['parameters']['properties']['scene']
        self.assertNotIn('version', schema['required'])
        for version in (2, None, '1'):
            with self.subTest(version=version), self.assertRaises(ValidationError):
                validate_call(animation_tools([]), 'animation_create',
                              normalize_arguments({'scene': dict(scene, version=version)}))

    def test_misplaced_scene_fields_are_moved_without_guessing_or_mutating(self):
        scene = example()
        arguments = {'scene': deepcopy(scene), 'loop': True}
        for key in ('version', 'title', 'description', 'duration', 'nodes', 'cues'):
            arguments[key] = arguments['scene'].pop(key)
        before = deepcopy(arguments)
        normalized = normalize_arguments(arguments)
        self.assertEqual(arguments, before)
        self.assertEqual(normalized, {'scene': dict(scene, loop=True), 'loop': True})
        self.assertEqual(validate_scene(normalized['scene']), dict(scene, loop=True))
        # Conflicting metadata and unknown fields must still be rejected.
        from blackrose.tools import animation_tools, validate_call
        for extra in ({'duration': 30}, {'script': 'alert(1)'}):
            with self.assertRaises(ValidationError):
                validate_call(animation_tools([]), 'animation_create',
                              normalize_arguments({'scene': scene, **extra}))

    def test_repair_reports_all_missing_metadata_in_one_result(self):
        scene = example()
        for key in ('version', 'title', 'description', 'duration'):
            del scene[key]
        result = repair_details({'scene': scene, 'loop': True})
        self.assertEqual(len(result['validation_errors']), 3)
        self.assertTrue(all(error['path'] == 'scene' for error in result['validation_errors']))
        self.assertEqual(result['required_shape']['scene']['version'], 1)

    def test_example_round_trips_and_exports_offline(self):
        self.assertEqual(json.loads(encode_scene(example())), example())
        exported = export_html(example())
        self.assertIn('BlackroseAnimations.mount', exported)
        self.assertIn('connect-src \'none\'', exported)
        self.assertNotIn('<script src=', exported)

    def test_untrusted_strings_stay_data_in_export_and_markdown(self):
        scene = example()
        attack = '</script><img src=x onerror="alert(1)"><script>alert(2)</script>'
        scene['title'] = attack
        scene['nodes'][0]['label'] = attack
        exported = export_html(scene)
        self.assertNotIn(attack, exported)
        self.assertNotIn('<img', exported)
        self.assertIn('\\u003c/script', exported)
        self.assertNotIn('<canvas', to_html('```animation\n' + json.dumps(scene) + '\n```'))

    def test_limits_references_and_unsupported_code_are_rejected(self):
        mutations = [
            lambda s: s.update(duration=float('nan')),
            lambda s: s.update(duration=float('inf')),
            lambda s: s.update(duration=121),
            lambda s: s.update(script='alert(1)'),
            lambda s: s['nodes'][0].update(onclick='alert(1)'),
            lambda s: s['nodes'][0].update(color='url(https://bad.example)'),
            lambda s: s['nodes'][0].update(kind='image', src='file:///etc/passwd'),
            lambda s: s['nodes'][0].pop('x2'),
            lambda s: s['nodes'][3].update(x=900),
            lambda s: s['tracks'][0]['keyframes'][-1].update(value=960),
            lambda s: s['nodes'].append(deepcopy(s['nodes'][0])),
            lambda s: s['tracks'][0].update(node='missing'),
            lambda s: s['tracks'].append(dict(deepcopy(s['tracks'][0]), easing='linear')),
            lambda s: s.update(loop='forever'),
            lambda s: s['tracks'][0]['keyframes'][1].update(time=0),
            lambda s: s['tracks'][0]['keyframes'][-1].update(time=13),
            lambda s: s['tracks'][1]['keyframes'][0].update(value=2),
            lambda s: s['cues'][1].update(time=0),
            lambda s: s.update(nodes=s['nodes'] * 6),
            lambda s: s.update(description='x' * 65_000),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                scene = example()
                mutate(scene)
                with self.assertRaises(ValueError):
                    validate_scene(scene)

    def test_javascript_interpolation_has_correct_boundaries(self):
        result = subprocess.run(['node', str(ROOT / 'tests/animation_player.cjs')],
                                capture_output=True, text=True, check=True)
        self.assertIn('passed', result.stdout)

    def test_compatible_duplicate_tracks_are_merged_without_mutating_input(self):
        scene = example()
        original = scene['tracks'][3]
        frames = original['keyframes']
        scene['tracks'][3:4] = [dict(original, keyframes=frames[2:]),
                               dict(original, keyframes=frames[:3]),
                               dict(original, keyframes=frames[:3])]
        before = deepcopy(scene)
        normalized = validate_scene(scene)
        self.assertEqual(scene, before)
        self.assertEqual(normalized['tracks'][3], original)
        self.assertEqual(json.loads(encode_scene(scene)), normalized)

    def test_disjoint_phases_merge_but_conflicting_overlaps_do_not(self):
        scene = example()
        track = deepcopy(scene['tracks'][3])
        track['keyframes'] = [{'time': 10, 'value': 0}, {'time': 12, 'value': 1}]
        scene['tracks'].append(track)
        normalized = validate_scene(scene)
        self.assertEqual(len(normalized['tracks']), len(scene['tracks']) - 1)
        self.assertEqual(normalized['tracks'][3]['keyframes'][-2:], track['keyframes'])
        track['keyframes'] = [{'time': 1, 'value': 1}, {'time': 4, 'value': 0}]
        with self.assertRaisesRegex(ValueError, 'Conflicting tracks for retrieval.emphasis'):
            validate_scene(scene)

    def test_repeated_highlights_with_overlapping_idle_padding_merge(self):
        scene = example()
        scene['tracks'] = [
            {'node': 'retrieval', 'property': 'emphasis', 'keyframes': [
                {'time': 0, 'value': 0}, {'time': 1, 'value': 1},
                {'time': 2, 'value': 0}, {'time': 12, 'value': 0}]},
            {'node': 'retrieval', 'property': 'emphasis', 'keyframes': [
                {'time': 0, 'value': 0}, {'time': 4, 'value': 0},
                {'time': 5, 'value': 1}, {'time': 6, 'value': 0}, {'time': 12, 'value': 0}]},
        ]
        normalized = validate_scene(scene)
        frames = normalized['tracks'][0]['keyframes']
        self.assertEqual([(f['time'], f['value']) for f in frames], [
            (0, 0), (1, 1), (2, 0), (4, 0), (5, 1), (6, 0), (12, 0)])
        # Both tracks may contain redundant idle points inside the other's pulse;
        # those must not introduce extra keyframes that distort its interpolation.
        scene['tracks'][1]['keyframes'].insert(1, {'time': .5, 'value': 0})
        self.assertEqual(validate_scene(scene), normalized)
        scene['tracks'][1]['keyframes'][2]['value'] = 1
        with self.assertRaisesRegex(ValueError, 'active highlight phases overlap'):
            validate_scene(scene)

    def test_duplicate_segments_cannot_evade_the_keyframe_limit(self):
        scene = example()
        scene['duration'] = 120
        scene['tracks'] = [{'node': 'packet', 'property': 'opacity', 'keyframes': [
            {'time': i, 'value': 0}, {'time': i + .5, 'value': 1}]} for i in range(17)]
        with self.assertRaisesRegex(ValueError, 'exceeds 32 keyframes'):
            validate_scene(scene)

    def test_looping_tcp_example_has_one_track_per_property(self):
        scene = load_scene(EXAMPLE.with_name('tcp-handshake.animation.json'))
        self.assertTrue(scene['loop'])
        self.assertEqual(len(scene['tracks']), len({(t['node'], t['property']) for t in scene['tracks']}))


class ChatTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gui.ImageTests.asyncSetUp
    asyncTearDown = gui.ImageTests.asyncTearDown
    latest = gui.ImageTests.latest

    async def send_steps(self, text, steps):
        self.runtime.controller.model_name = 'opencode-go/test-vision'
        self.runtime.controller.backend = ScriptedBackend(steps)
        await self.session.submit(text)
        await self.session._generation
        return self.runtime.controller

    async def create(self):
        return await self.send_steps('Animate how RAG works', [
            ('animation_create', {'scene': example()}), 'The animation is ready.'])

    async def test_invalid_opacity_can_be_repaired_and_saved_in_the_same_reply(self):
        c = await self.send_steps('Create a short animation describing software layers', [
            ('animation_create', {'scene': layer_scene(value=100), 'loop': False}),
            ('animation_create', {'scene': layer_scene(value=1), 'loop': False}), 'Ready.'])
        feedback = json.loads(next(m for m in c.backend.requests[1] if m['role'] == 'tool')['content'])
        self.assertIn('opacity values must be 0..1', feedback['error'])
        self.assertEqual(feedback['validation_errors'][0]['path'], 'scene.tracks.0.keyframes.1.value')
        self.assertIn('retry', feedback['notice'])
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, 'complete')
        self.assertEqual(len(saved.attachments), 1)
        self.assertEqual(load_scene(c.attachment_path(saved.attachments[0])), dict(layer_scene(value=1), loop=False))

    async def test_invalid_opacity_cannot_be_followed_by_a_false_success(self):
        c = await self.send_steps('Create an animation describing software layers', [
            ('animation_create', {'scene': layer_scene(value=100), 'loop': False}),
            'I have created the animation.'])
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, 'error')
        self.assertEqual(saved.attachments, [])
        self.assertIn('no animation was saved', saved.error)
        self.assertIn('scene.tracks.0.keyframes.1.value', saved.error)


    async def test_create_reopen_restart_and_revision_from_saved_source(self):
        c = await self.create()
        payload = self.latest('reply_images')['attachments'][0]
        self.assertEqual(payload['scene'], example())
        saved = (await c.current_messages())[-1].attachments[0]
        self.assertEqual(saved.kind, 'animation')
        initial_tools = {t['function']['name']: t['function'] for t in c.backend.definitions[0]}
        self.assertNotIn('animation_read', initial_tools)
        self.assertNotIn('revises_attachment_id', initial_tools['animation_create']['parameters']['properties'])
        next_tools = {t['function']['name']: t['function'] for t in c.backend.definitions[1]}
        self.assertEqual(next_tools['animation_read']['parameters']['properties']['attachment_id']['enum'], [saved.id])
        self.assertEqual(json.loads(c._store.read(saved.storage_path)), example())
        wire = c._wire_message((await c.current_messages())[-1])
        self.assertIsInstance(wire['content'], str)  # never sent as an image to vision models
        self.assertIn(str(saved.id), wire['content'])
        # Close the real database connection and instantiate a fresh repository.
        await self.runtime.database.close()
        # The fixture owns a named on-disk database; reuse that exact path.
        database = Database(self.runtime.database.path)
        await database.connect()
        c._repo = Repository(database)
        self.runtime.database = database
        await self.session.render_transcript()
        self.assertEqual(self.latest('transcript')['messages'][-1]['attachments'][0], payload)
        revised = example()
        revised['duration'] = 15
        revised['title'] = 'RAG with a slower retrieval step'
        for track in revised['tracks']:
            for frame in track['keyframes']:
                if frame['time'] >= 4:
                    frame['time'] += 3
        for cue in revised['cues']:
            if cue['time'] >= 4:
                cue['time'] += 3
        await self.send_steps('Slow down retrieval and show selected documents', [
            ('animation_read', {'attachment_id': saved.id}),
            ('animation_create', {'scene': revised, 'revises_attachment_id': saved.id}), 'Updated.'])
        tool_result = next(m for m in c.backend.requests[1] if m['role'] == 'tool')
        self.assertEqual(json.loads(tool_result['content'])['scene'], example())
        animations = [a for m in await c.current_messages() for a in m.attachments]
        self.assertEqual(len(animations), 2)
        self.assertEqual(load_scene(c.attachment_path(animations[0])), example())
        self.assertEqual(load_scene(c.attachment_path(animations[1])), revised)

    async def test_validation_failure_can_be_corrected_without_losing_chat(self):
        bad = example()
        bad['tracks'][0]['node'] = 'missing'
        c = await self.send_steps('Animate RAG', [('animation_create', {'scene': bad}),
            ('animation_create', {'scene': example()}), 'Ready.'])
        failed_result = next(m for m in c.backend.requests[1] if m['role'] == 'tool')
        self.assertIn('Unknown track node', json.loads(failed_result['content'])['error'])
        self.assertEqual(len((await c.current_messages())[-1].attachments), 1)
        self.assertEqual((await c.current_messages())[-1].state.value, 'complete')

    async def test_rejected_animation_cannot_finish_as_success_without_artifact(self):
        bad = example()
        for key in ('version', 'title', 'description', 'duration'):
            del bad[key]
        c = await self.send_steps('Create a looping animation', [
            ('animation_create', {'scene': bad, 'loop': True}),
            'I have successfully generated the animation.'])
        stored = (await c.current_messages())[-1]
        self.assertEqual(stored.state.value, 'error')
        self.assertEqual(stored.attachments, [])
        self.assertIn('no animation was saved', stored.error)
        self.assertIn('title', stored.error)
        result = json.loads(next(m for m in c.backend.requests[1] if m['role'] == 'tool')['content'])
        self.assertEqual(len(result['validation_errors']), 3)
        self.assertEqual(result['required_shape']['scene']['version'], 1)
        # A previous reply's saved animation does not satisfy a failed new request.
        await self.create()
        c = await self.send_steps('Create another animation', [
            ('animation_create', {'scene': bad, 'loop': True}), 'Done.'])
        self.assertEqual((await c.current_messages())[-1].state.value, 'error')

    async def test_misplaced_scene_metadata_recovers_in_real_tool_path(self):
        scene = example()
        arguments = {'scene': deepcopy(scene), 'loop': True}
        for key in ('version', 'title', 'description', 'duration', 'nodes', 'cues'):
            arguments[key] = arguments['scene'].pop(key)
        c = await self.send_steps('Create a looping animation', [
            ('animation_create', arguments), 'Ready.'])
        stored = (await c.current_messages())[-1]
        self.assertEqual(stored.state.value, 'complete')
        self.assertEqual(len(stored.attachments), 1)
        self.assertEqual((await c.read_animation(stored.attachments[0].id))[1], dict(scene, loop=True))

    async def test_missing_version_saves_on_first_attempt_without_tool_error(self):
        scene = example()
        del scene['version']
        c = await self.send_steps('Create an animation', [
            ('animation_create', {'scene': scene}), 'Ready.'])
        stored = (await c.current_messages())[-1]
        self.assertEqual(stored.state.value, 'complete')
        self.assertEqual(len(stored.attachments), 1)
        self.assertEqual((await c.read_animation(stored.attachments[0].id))[1], example())
        errors = [p for kind, p in self.events if kind == 'tool_activity' and p['phase'] == 'error']
        self.assertEqual(errors, [])

    async def test_split_highlights_are_saved_as_one_track_and_playable_immediately(self):
        scene = example()
        original = deepcopy(scene['tracks'][3])
        frames = original['keyframes']
        scene['tracks'][3:4] = [dict(original, keyframes=frames[:2]),
                               dict(original, keyframes=frames[2:])]
        c = await self.send_steps('Animate this process', [
            ('animation_create', {'scene': scene}), 'Ready.'])
        self.assertEqual(self.latest('reply_images')['attachments'][0]['scene'], example())
        self.assertEqual(len((await c.current_messages())[-1].attachments), 1)
        errors = [p for kind, p in self.events if kind == 'tool_activity' and p['phase'] == 'error']
        self.assertEqual(errors, [])

    async def test_explicit_tool_loop_flag_is_saved_and_reported(self):
        c = await self.send_steps('Make a looping animation', [
            ('animation_create', {'scene': example(), 'loop': True}), 'Ready.'])
        payload = self.latest('reply_images')['attachments'][0]
        self.assertTrue(payload['scene']['loop'])
        result = json.loads(next(m for m in c.backend.requests[1] if m['role'] == 'tool')['content'])
        self.assertTrue(result['loop'])
        self.assertEqual(next(t['function']['parameters']['required'] for t in c.backend.definitions[0]
                              if t['function']['name'] == 'animation_create'), ['scene', 'loop'])

    async def test_local_model_empty_skill_path_reads_the_animation_instructions(self):
        c = self.runtime.controller
        c.skills.install(str(EXAMPLE.parent.parent), approved=True, origin='bundled')
        await self.send_steps('Animate a TCP handshake', [
            ('read_skill', {'skill_id': 'explanatory-animations', 'path': ''}),
            ('animation_create', {'scene': example()}), 'Ready.'])
        result = json.loads(next(m for m in c.backend.requests[1] if m['role'] == 'tool')['content'])
        self.assertEqual(result['path'], 'SKILL.md')
        self.assertIn('Gothic Bloom', result['text'])
        self.assertEqual(len((await c.current_messages())[-1].attachments), 1)

    async def test_conflicting_highlights_return_specific_repair_instructions(self):
        scene = example()
        track = deepcopy(scene['tracks'][3])
        track['keyframes'][0]['value'] = 1
        scene['tracks'].append(track)
        c = await self.send_steps('Make a looping TCP animation', [
            ('animation_create', {'scene': scene}),
            ('animation_create', {'scene': example()}), 'Ready.'])
        result = json.loads(next(m for m in c.backend.requests[1] if m['role'] == 'tool')['content'])
        self.assertIn('retrieval.emphasis', result['error'])
        self.assertIn('retry', result['notice'])
        self.assertIn('loop=true', result['notice'])

    async def test_artifact_survives_failure_and_cancellation(self):
        for outcome in (RuntimeError('Continuation failed'), asyncio.CancelledError()):
            await self.runtime.controller.new_conversation()
            try:
                c = await self.send_steps('Animate RAG', [
                    ('animation_create', {'scene': example()}), outcome])
            except asyncio.CancelledError:
                c = self.runtime.controller
            await self.session.render_transcript()
            payload = self.latest('transcript')['messages'][-1]['attachments'][0]
            self.assertEqual(payload['scene'], example())
            self.assertEqual((await c.read_animation(payload['attachment_id']))[1], example())

    async def test_read_and_revision_are_scoped_to_current_conversation(self):
        c = await self.create()
        saved = (await c.current_messages())[-1].attachments[0]
        await c.new_conversation()
        with self.assertRaisesRegex(ValueError, 'this conversation'):
            await c.read_animation(saved.id)
        await self.send_steps('Revise another chat', [('animation_create', {
            'scene': example(), 'revises_attachment_id': saved.id}), 'Unavailable.'])
        self.assertEqual((await c.current_messages())[-1].attachments, [])
        result = json.loads(next(m for m in c.backend.requests[-1] if m['role'] == 'tool')['content'])
        self.assertIn('omit revises_attachment_id', result['notice'])

    async def test_corrupt_or_missing_scene_is_a_local_error(self):
        c = await self.create()
        saved = (await c.current_messages())[-1].attachments[0]
        path = c.attachment_path(saved)
        for data in (b'{', b'x' * 64_001):
            path.write_bytes(data)
            await self.session.render_transcript()
            payload = self.latest('transcript')['messages'][-1]['attachments'][0]
            self.assertIn('error', payload)
            self.assertNotIn('scene', payload)
        path.unlink()
        await self.session.render_transcript()
        self.assertIn('missing', self.latest('transcript')['messages'][-1]['attachments'][0]['error'])

    async def test_text_conversation_is_still_plain_and_animation_calls_are_bounded(self):
        c = await self.send_steps('Hello', ['Hello.'])
        self.assertEqual((await c.current_messages())[-1].content, 'Hello.')
        self.assertEqual((await c.current_messages())[-1].attachments, [])
        steps = [('animation_create', {'scene': example()})] * 5 + ['Done.']
        await self.send_steps('Make four animations', steps)
        self.assertEqual(len((await c.current_messages())[-1].attachments), 4)
        result = c.backend.requests[-1][-1]
        self.assertIn('At most four', json.loads(result['content'])['error'])

    async def test_images_and_animations_share_reply_without_replacing_each_other(self):
        c = self.runtime.controller
        c.config.image_provider = 'openrouter'
        c.image_generator = AsyncMock()
        c.image_generator.generate.return_value = GeneratedImage(gui.png(), inspect_image(gui.png()), 'test', 0)
        for order in (False, True):
            await c.new_conversation()
            steps = [('animation_create', {'scene': example()}), ('image_gen', {'prompt': 'A diagram'})]
            if order:
                steps.reverse()
            await self.send_steps('Make an animation and a static diagram', [*steps, 'Ready.'])
            attachments = self.latest('reply_images')['attachments']
            self.assertEqual({a['kind'] for a in attachments}, {'image', 'animation'})
            self.assertIn('scene', next(a for a in attachments if a['kind'] == 'animation'))
            self.assertIn('preview_id', next(a for a in attachments if a['kind'] == 'image'))

    async def test_deletion_keeps_shared_animation_until_last_reference_is_removed(self):
        c = await self.create()
        first = c.conversation.id
        path = c.attachment_path((await c.current_messages())[-1].attachments[0])
        await c.new_conversation()
        await self.create()
        self.assertEqual(c.attachment_path((await c.current_messages())[-1].attachments[0]), path)
        await c.delete_current_conversation()
        self.assertTrue(path.exists())
        await c.open_conversation(first)
        await c.delete_current_conversation()
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
