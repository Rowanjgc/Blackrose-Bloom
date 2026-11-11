"""Plan validation and host-owned Motion Canvas scheduling regressions."""
import unittest

from blackrose.motion_canvas import compile_source
from blackrose.motion_plan import assemble_source, element_report, validate_plan


def dns_plan():
    return {'version': 1, 'objective': 'Explain DNS resolution', 'scene': 'A query travels to a resolver',
            'constraints': 'Use readable labels within the canvas', 'elements': [
                {'id': 'client', 'description': 'Client', 'label': 'Browser', 'initial': {'x': -300, 'y': 0}, 'final': {'x': -300}},
                {'id': 'query', 'description': 'DNS query', 'label': 'example.org', 'initial': {'x': -200, 'y': 0}, 'final': {'x': 200, 'y': 80, 'opacity': 0.5}},
            ], 'timeline': [
                {'wait': 0.5},
                {'parallel': [{'target': 'query', 'property': 'x', 'value': 200, 'duration': 2},
                              {'target': 'query', 'property': 'opacity', 'value': 0.5, 'duration': 1}]},
                {'transition': {'target': 'query', 'property': 'y', 'value': 80, 'duration': 1}},
            ], 'final_hold': 1}


VISUAL = '''import {Node, Txt} from '@motion-canvas/2d';
export function build(view: any): Record<string, Node> {
 const client = new Txt({text: 'Browser'});
 const query = new Txt({text: 'example.org'});
 view.add(client); view.add(query);
 return {client, query};
}'''


class MotionPlanTests(unittest.TestCase):
    def test_duration_sequential_parallel_wait_and_hold(self):
        plan, duration = validate_plan(dns_plan())
        self.assertEqual(duration, 4.5)
        source, _ = assemble_source(plan, VISUAL)
        self.assertLess(source.index('yield* __br_wait(0.5)'), source.index('yield* __br_all('))
        self.assertLess(source.index('yield* __br_all('), source.index('yield* __br_nodes["query"].y'))
        self.assertIn('__br_nodes["query"].x(-200)', source)
        self.assertIn('yield* __br_wait(1)', source)

    def test_version_alias_does_not_mutate_input(self):
        plan = dns_plan()
        plan['version'] = '1'
        canonical, _ = validate_plan(plan)
        self.assertEqual(canonical['version'], 1)
        self.assertEqual(plan['version'], '1')

    def test_reject_invalid_references_conflicts_and_unsupported_motion(self):
        cases = []
        plan = dns_plan(); plan['timeline'][1]['parallel'][0]['target'] = 'missing'; cases.append(plan)
        plan = dns_plan(); plan['timeline'][1]['parallel'][1]['property'] = 'x'; cases.append(plan)
        plan = dns_plan(); plan['timeline'][1]['parallel'][0]['property'] = 'path'; cases.append(plan)
        plan = dns_plan(); del plan['elements'][0]['initial']['x']; cases.append(plan)
        plan = dns_plan(); plan['final_hold'] = 120; cases.append(plan)
        plan = dns_plan(); plan['elements'][0]['id'] = 'x\"]}; alert(1);//'; cases.append(plan)
        plan = dns_plan(); plan['timeline'][0]['wait'] = float('nan'); cases.append(plan)
        plan = dns_plan(); plan['elements'][1]['final']['x'] = 199; cases.append(plan)
        plan = dns_plan(); plan['timeline'][0] = {'wait': 1, 'transition': {}}; cases.append(plan)
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                validate_plan(plan)

    def test_source_compiles_with_real_motion_canvas_toolchain(self):
        source, _ = assemble_source(dns_plan(), VISUAL)
        self.assertTrue(compile_source(source))

    def test_blood_circulation_sequence_compiles(self):
        plan = dns_plan()
        plan.update(objective='Explain blood circulation', scene='Blood travels from heart to lungs')
        plan['elements'][0].update(description='Heart', label='Heart')
        plan['elements'][1].update(description='Blood cell', label='Blood')
        visual = VISUAL.replace('Browser', 'Heart').replace('example.org', 'Blood')
        source, duration = assemble_source(plan, visual)
        self.assertEqual(duration, 4.5)
        self.assertTrue(compile_source(source))

    def test_plan_is_detached(self):
        original = dns_plan()
        plan, _ = validate_plan(original)
        plan['elements'][0]['initial']['x'] = 0
        self.assertEqual(original, dns_plan())


class ElementReportTests(unittest.TestCase):
    def observations(self, query_before, query_after):
        client = {'opacity': 1, 'x': -300, 'y': 0, 'width': 80, 'height': 20}
        return {'duration': 4.5, 'samples': [
            {'time': 0, 'elements': {'client': client, 'query': query_before}},
            {'time': 2, 'elements': {'client': client, 'query': query_after}},
        ]}

    def test_visible_and_moving_elements_pass(self):
        plan, _ = validate_plan(dns_plan())
        report = element_report(self.observations(
            {'opacity': 1, 'x': -200, 'y': 0, 'width': 80, 'height': 20},
            {'opacity': 0.5, 'x': 200, 'y': 80, 'width': 80, 'height': 20}), plan)
        self.assertEqual(report['assessment'], 'visible')
        self.assertEqual(report['checked'], 2)
        self.assertEqual(report['issues'], [])

    def test_invisible_and_unmoving_elements_are_reported(self):
        plan, _ = validate_plan(dns_plan())
        still = {'opacity': 0, 'x': -200, 'y': 0, 'width': 80, 'height': 20}
        report = element_report(self.observations(still, still), plan)
        self.assertEqual(report['assessment'], 'incomplete')
        self.assertIn("planned element 'query' never becomes visible", report['issues'])
        self.assertIn("planned element 'query' never changes x", report['issues'])

    def test_missing_element_and_unknown_inventory_are_explicit(self):
        plan, _ = validate_plan(dns_plan())
        missing = {'duration': 4.5, 'samples': [
            {'time': 0, 'elements': {'client': {'opacity': 1, 'x': -300, 'y': 0}}},
            {'time': 2, 'elements': {'client': {'opacity': 1, 'x': -300, 'y': 0}}},
        ]}
        report = element_report(missing, plan)
        self.assertIn("planned element 'query' is missing from the runtime scene", report['issues'])
        self.assertEqual(element_report({'samples': [{'time': 0}]}, plan)['assessment'], 'unknown')


class MotionPlanToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pathlib import Path
        from types import SimpleNamespace
        from blackrose.tool_execution import ToolExecutor
        from blackrose.tools import animation_tools

        self.saved = []
        async def create(assistant, arguments, **kwargs):
            self.saved.append((arguments, kwargs))
            return {'attachment_id': 7}, []
        self.chat = SimpleNamespace(_store=SimpleNamespace(root=Path('/tmp')),
            tool_definitions=lambda *args, **kwargs: animation_tools([], plan_first=True),
            _create_animation=create, _sources=lambda *args: [])
        self.prompt = SimpleNamespace(animation_ids=set(), xiaohei=False, plan_only=False,
            animations_enabled=True, skills_available=False, library_write_enabled=False,
            ui_used=False, messages=[])
        self.executor = ToolExecutor(self.chat, SimpleNamespace(id=1, conversation_id=2),
                                     self.prompt, False, on_delta=lambda *args: None)

    async def call(self, name, args):
        import json
        await self.executor.execute({'id': 'a', 'function': {'name': name,
            'arguments': json.dumps(args)}}, {})
        return json.loads(self.prompt.messages[-1]['content'])

    async def test_plan_then_visuals_uses_existing_creation(self):
        result = await self.call('animation_plan', {'plan': dns_plan()})
        self.assertTrue(result['valid'])
        result = await self.call('animation_create_planned', {'scene': {
            'title': 'DNS', 'description': 'A DNS query', 'duration': 1, 'source': VISUAL}, 'loop': False})
        self.assertEqual(result['strategy'], 'plan-first')
        args, kwargs = self.saved[0]
        self.assertEqual(args['scene']['duration'], 4.5)
        self.assertEqual(kwargs['expected_duration'], 4.5)
        self.assertEqual(kwargs['plan'], self.executor.motion_plan)
        self.assertIn('yield* __br_all(', args['scene']['source'])
        self.assertIn('__blackrosePlanElements', args['scene']['source'])
        self.executor.require_successful_artifacts()

    async def test_invalid_plan_falls_back_once_to_direct_creation(self):
        bad = dns_plan()
        bad['timeline'][1]['parallel'][0]['target'] = 'missing'
        result = await self.call('animation_plan', {'plan': bad})
        self.assertIn('animation_create directly', result['notice'])
        self.assertEqual(self.executor.rejected_motion_plan, bad)
        self.assertIn('unknown target', self.executor.motion_plan_error)
        result = await self.call('animation_plan', {'plan': dns_plan()})
        self.assertIn('error', result)
        result = await self.call('animation_create', {'scene': {
            'title': 'DNS', 'description': 'Query', 'duration': 1, 'source': VISUAL}, 'loop': False})
        self.assertEqual(result['strategy'], 'direct')
        self.executor.require_successful_artifacts()

    async def test_creation_failure_falls_back_and_does_not_save(self):
        await self.call('animation_plan', {'plan': dns_plan()})
        async def fail(*args, **kwargs):
            raise ValueError('compilation failed')
        self.chat._create_animation = fail
        result = await self.call('animation_create_planned', {'scene': {
            'title': 'DNS', 'description': 'Query', 'duration': 1, 'source': VISUAL}, 'loop': False})
        self.assertIn('animation_create directly', result['notice'])
        self.assertFalse(self.executor.animation_created)
        self.assertEqual(self.saved, [])
