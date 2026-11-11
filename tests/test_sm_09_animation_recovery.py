"""SM-09 invariants: the durable repair chain and the bounded WebKit probe.

Neither half needs GTK/WebKit or a display. The probe tests substitute a real
child process (plain Python) for the WebKit worker so the parent's deadline,
process-group kill and result guards are exercised end to end.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from blackrose import motion_canvas as motion
from blackrose.animation_recovery import AnimationRecovery, MAX_REPAIRS
from blackrose.motion_plan import element_report, validate_plan

CELLS = motion.MOTION_GRID_COLUMNS * motion.MOTION_GRID_ROWS


def grid(outside=0, caption=0):
    """One occupancy grid with a non-caption cell and a bottom-band cell."""
    cells = [0] * CELLS
    cells[4 * motion.MOTION_GRID_COLUMNS + 5] = outside
    cells[(motion.MOTION_GRID_ROWS - 1) * motion.MOTION_GRID_COLUMNS + 3] = caption
    return cells


def samples(times, grids, foreground=100):
    return {'duration': max(times) if times else 0,
            'samples': [{'time': at, 'foreground': foreground, 'grid': grid_value}
                        for at, grid_value in zip(times, grids, strict=True)]}


class RecoveryLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'animation-recovery.sqlite3'
        self.recovery = AnimationRecovery(self.path)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def reopen(self):
        """A second process opening the same ledger has no shared memory."""
        self.recovery = AnimationRecovery(self.path)
        return self.recovery

    async def test_the_recorded_replacement_link_continues_the_chain_after_reopen(self):
        first = await self.recovery.claim(7, 100, 'renderer failed')
        self.assertEqual((first['root_id'], first['attempt'], first['status'], first['claimed']),
                         (100, 1, 'repairing', True))
        # tool_execution._animation_create records this before the saved
        # attachment is published to the page (tool_execution.py:374-378).
        await self.recovery.finish(7, 100, 'replaced', 101)
        second = await self.reopen().claim(7, 101, 'replay failed')
        self.assertEqual((second['root_id'], second['attempt'], second['claimed']), (100, 2, True))
        await self.recovery.finish(7, 101, 'replaced', 102)
        third = await self.reopen().claim(7, 102, 'still failing')
        self.assertEqual((third['root_id'], third['attempt'], third['status'], third['claimed']),
                         (100, MAX_REPAIRS + 1, 'exhausted', False))
        repeat = await self.recovery.claim(7, 102, 'reported twice')
        self.assertEqual((repeat['attempt'], repeat['status'], repeat['claimed']),
                         (MAX_REPAIRS + 1, 'exhausted', False))
        states = await self.recovery.states(7)
        self.assertEqual(sorted(states), [100, 101, 102])
        self.assertIsNone(states[102]['replacement_attachment_id'])

    async def test_terminal_states_are_immutable_and_unknown_rows_are_ignored(self):
        await self.recovery.claim(7, 100, 'bad')
        await self.recovery.finish(7, 100, 'stopped')
        # A late link or a different outcome cannot rewrite a settled row.
        await self.recovery.finish(7, 100, 'replaced', 101)
        await self.recovery.finish(7, 100, 'failed')
        await self.recovery.finish(7, 999, 'failed')
        states = await self.recovery.states(7)
        self.assertEqual(sorted(states), [100])
        self.assertEqual((states[100]['status'], states[100]['replacement_attachment_id']),
                         ('stopped', None))

    async def test_a_replaced_row_without_a_recorded_link_starts_a_new_chain(self):
        # The store accepts 'replaced' with no replacement id; only the caller
        # knows the saved attachment id. This is the mechanism by which a
        # replacement whose link was never recorded becomes a new root.
        await self.recovery.claim(7, 100, 'bad')
        await self.recovery.finish(7, 100, 'replaced')
        state = (await self.recovery.states(7))[100]
        self.assertEqual((state['status'], state['replacement_attachment_id']), ('replaced', None))
        orphan = await self.reopen().claim(7, 101, 'replay failed')
        self.assertEqual((orphan['root_id'], orphan['attempt'], orphan['claimed']), (101, 1, True))

    async def test_a_restarted_ledger_never_settles_a_repairing_row(self):
        await self.recovery.claim(7, 100, 'bad')
        self.reopen()
        self.assertEqual((await self.recovery.states(7))[100]['status'], 'repairing')
        # Re-claiming reports the interrupted attempt without writing anything,
        # so the row keeps that status until the conversation is deleted.
        duplicate = await self.recovery.claim(7, 100, 'after restart')
        self.assertFalse(duplicate['claimed'])
        self.assertEqual((duplicate['attempt'], duplicate['status']), (1, 'repairing'))
        self.assertEqual((await self.recovery.states(7))[100]['status'], 'repairing')
        await self.recovery.delete(7)
        self.assertEqual(await self.recovery.states(7), {})

    async def test_untrusted_diagnostics_are_truncated_and_a_corrupt_ledger_reads_empty(self):
        await self.recovery.claim(7, 100, 'x' * 5000)
        self.assertEqual(len((await self.recovery.states(7))[100]['error']), 1000)
        self.path.write_bytes(b'not a sqlite database')
        with self.assertLogs('blackrose.animation_recovery', level='WARNING'):
            self.assertEqual(await self.recovery.states(7), {})

    async def test_a_cancelled_finish_still_records_the_replacement_link(self):
        await self.recovery.claim(7, 100, 'bad')
        # A concurrent render or artifact report holds the in-process lock. A
        # Stop delivered while the link write waits for it must not lose the
        # write, or the saved replacement would start a fresh chain.
        await self.recovery._lock.acquire()
        try:
            task = asyncio.create_task(self.recovery.finish(7, 100, 'replaced', 101))
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            self.recovery._lock.release()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = (await self.reopen().states(7))[100]
        self.assertEqual((state['status'], state['replacement_attachment_id']), ('replaced', 101))

    async def test_a_cancelled_claim_spends_no_attempt(self):
        await self.recovery._lock.acquire()
        try:
            task = asyncio.create_task(self.recovery.claim(7, 100, 'bad'))
            await asyncio.sleep(0.02)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            self.recovery._lock.release()
        self.assertFalse(await self.recovery.states(7))
        retry = await self.recovery.claim(7, 100, 'bad')
        self.assertEqual((retry['attempt'], retry['claimed']), (1, True))


class ProbeLifecycleTests(unittest.TestCase):
    """The parent owns the deadline and the process group on every exit path."""

    def setUp(self):
        self.pids = []
        self.scripts = []

    def tearDown(self):
        for pid in self.pids:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)

    def launch(self, scripts):
        """Run real child processes that stand in for the WebKit worker."""
        self.scripts = list(scripts)
        real = subprocess.Popen

        def popen(_argv, **kwargs):
            body = self.scripts.pop(0) if len(self.scripts) > 1 else self.scripts[0]
            child = real([sys.executable, '-c', body], **kwargs)
            self.pids.append(child.pid)
            return child

        return patch.object(motion.subprocess, 'Popen', popen)

    @staticmethod
    def emit(payload):
        # The payload is embedded as a JSON string literal, which is also a valid
        # Python literal, so the child prints exactly what the probe would.
        return f'import sys\nsys.stdin.read()\nprint({json.dumps(json.dumps(payload))})\n'

    def assert_group_is_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)

    def test_the_deadline_kills_the_process_group_and_raises(self):
        hang = 'import sys, time\nsys.stdin.read()\ntime.sleep(60)\n'
        with self.launch([hang]), patch.object(motion, 'PLAYBACK_TIMEOUT', 0.6):
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, 'exceeded 0.6 seconds'):
                motion.probe_document('<html data-case="deadline"></html>')
            self.assertLess(time.monotonic() - started, 10)
        self.assert_group_is_gone(self.pids[-1])

    def test_failed_workers_are_bounded_refusals(self):
        failing = 'import sys\nsys.stdin.read()\nsys.stderr.write("display unavailable")\nsys.exit(3)\n'
        junk = 'import sys\nsys.stdin.read()\nprint("not json")\n'
        cases = [
            (failing, 'unavailable: display unavailable'),
            (junk, 'returned no valid result'),
        ]
        for index, (script, message) in enumerate(cases):
            with self.subTest(message=message):
                with self.launch([script]):
                    with self.assertRaisesRegex(ValueError, message):
                        motion.probe_document(f'<html data-case="failed-{index}"></html>')
                self.assert_group_is_gone(self.pids[-1])

    def test_result_guards_reject_errors_empty_samples_and_bad_durations(self):
        cases = [
            ({'error': 'runtime fixture'}, 'validation failed: runtime fixture'),
            ({'duration': 8.0, 'samples': []}, 'rendered no samples'),
            ({'duration': 0.1, 'samples': [{'time': 0}]}, 'invalid duration'),
            ({'duration': '8', 'samples': [{'time': 0}]}, 'invalid duration'),
            ({'samples': [{'time': 0}]}, 'invalid duration'),
        ]
        for index, (payload, message) in enumerate(cases):
            with self.subTest(message=message):
                with self.launch([self.emit(payload)]):
                    with self.assertRaisesRegex(ValueError, message):
                        motion.probe_document(f'<html data-case="guard-{index}"></html>')

    def test_a_failed_probe_is_not_cached_so_a_retry_can_succeed(self):
        failing = 'import sys\nsys.stdin.read()\nsys.stderr.write("temporary")\nsys.exit(2)\n'
        success = self.emit({'duration': 8.0, 'samples': [{'time': 0}, {'time': 4}, {'time': 8}]})
        with self.launch([failing, success]):
            document = '<html data-case="retry"></html>'
            with self.assertRaisesRegex(ValueError, 'temporary'):
                motion.probe_document(document)
            self.assertEqual(motion.probe_document(document)['duration'], 8.0)
        self.assert_group_is_gone(self.pids[-1])

    def test_capture_probes_are_not_cached(self):
        scripts = [self.emit({'duration': 8.0, 'samples': [{'time': 0}]}),
                   self.emit({'duration': 9.0, 'samples': [{'time': 0}]})]
        with self.launch(scripts):
            document = '<html data-case="capture"></html>'
            first = motion.probe_document(document, capture=True)
            second = motion.probe_document(document, capture=True)
        self.assertEqual((first['duration'], second['duration']), (8.0, 9.0))


class MotionAssessmentTests(unittest.TestCase):
    def test_the_assessment_vocabulary_is_closed(self):
        cases = {
            'unknown': {'duration': 8, 'samples': []},
            'blank': samples((0, 2, 4, 6, 8), [grid(10), grid(11), grid(12), grid(13), grid(14)],
                             foreground=0),
            'static': samples((0, 2, 4, 6, 8), [grid(10)] * 5),
            'moving': samples((0, 2, 4, 6, 8), [grid(0), grid(1), grid(2), grid(3), grid(4)]),
            'limited': samples((0, 2, 4, 6, 8), [grid(10), grid(0), grid(0), grid(0), grid(0)]),
        }
        for expected, result in cases.items():
            with self.subTest(expected):
                report = motion.motion_report(result)
                self.assertEqual(report['assessment'], expected)
                self.assertIn(report['assessment'],
                              {'unknown', 'blank', 'static', 'limited', 'moving'})
                if expected == 'unknown':
                    self.assertIsNone(report['notice'])
                    self.assertNotIn('duration', report)
                else:
                    self.assertIsInstance(report['duration'], float)

    def test_repeated_seek_targets_compare_the_first_render_of_each_time(self):
        still = [{'time': at, 'foreground': 100, 'grid': grid(10)} for at in (0, 2, 4)]
        replayed = [*still, {'time': 4, 'foreground': 100, 'grid': grid(99)}]
        self.assertEqual(motion.motion_report({'duration': 4, 'samples': replayed})['assessment'],
                         'static')
        # 'blank' requires every sample to be empty, not just the first render.
        mixed = [{'time': 0, 'foreground': 5, 'grid': grid()},
                 {'time': 2, 'foreground': 0, 'grid': grid()},
                 {'time': 4, 'foreground': 0, 'grid': grid()}]
        self.assertEqual(motion.motion_report({'duration': 4, 'samples': mixed})['assessment'],
                         'static')
        repeated = [{'time': 0, 'foreground': 100, 'grid': grid(1)} for _ in range(5)]
        self.assertEqual(motion.motion_report({'duration': 4, 'samples': repeated})['assessment'],
                         'unknown')

    def test_limited_is_decided_against_the_supplied_measured_duration(self):
        result = samples((0, 2, 4, 6, 8), [grid(10), grid(0), grid(0), grid(0), grid(0)])
        self.assertEqual(motion.motion_report(result)['assessment'], 'limited')
        report = motion.motion_report(result, 3)
        self.assertEqual((report['assessment'], report['duration']), ('moving', 3))


class PrepareSceneCueTests(unittest.TestCase):
    def scene(self, cues, duration=10):
        return {'version': 2, 'engine': 'motion-canvas', 'title': 'Cues',
                'description': 'Cue clamping regression', 'duration': duration, 'loop': False,
                'source': 'export default makeScene2D(function* (view) {});',
                'cues': cues}

    def prepare(self, scene, measured=8.0):
        probe = {'duration': measured, 'samples': [
            {'time': at, 'foreground': 100, 'grid': grid()} for at in (0, 1, 2)]}
        with patch.object(motion, 'compile_source', return_value='compiled'), \
                patch.object(motion, 'probe_document', return_value=probe):
            return motion.prepare_scene(scene)

    def test_two_cues_inside_the_endpoint_tolerance_keep_the_last_one(self):
        # The probe accepts cue times up to one frame past the measured end
        # (motion_canvas_probe.py:56); both of these clamp onto the last frame,
        # where the player only ever displays the last one.
        prepared = self.prepare(self.scene([
            {'time': 2, 'text': 'Early'},
            {'time': 8.01, 'text': 'First'},
            {'time': 8.02, 'text': 'Last'}]))
        self.assertEqual(prepared['duration'], 8.0)
        self.assertEqual(prepared['cues'], [{'time': 2, 'text': 'Early'},
                                            {'time': 8.0, 'text': 'Last'}])
        motion.validate_scene(prepared)

    def test_cues_inside_the_measured_timeline_keep_their_times_and_order(self):
        prepared = self.prepare(self.scene([{'time': 2, 'text': 'A'},
                                            {'time': 4, 'text': 'B'}]))
        self.assertEqual([cue['time'] for cue in prepared['cues']], [2, 4])
        self.assertEqual(prepared['duration'], 8.0)


class ElementReportBoundTests(unittest.TestCase):
    def test_missing_elements_are_bounded_and_unknown_needs_an_inventory(self):
        elements = [{'id': f'e{index}', 'description': f'Element {index}',
                     'initial': {'x': 0, 'y': 0}, 'final': {'x': 0}} for index in range(13)]
        plan, duration = validate_plan({
            'version': 1, 'objective': 'Bound the issue list', 'scene': 'Thirteen elements',
            'constraints': 'None', 'elements': elements,
            'timeline': [{'wait': 1}], 'final_hold': 0})
        self.assertEqual(duration, 1)
        observed = {'samples': [{'time': 0, 'elements': {
            'unplanned': {'opacity': 1, 'x': 0, 'y': 0}}}]}
        report = element_report(observed, plan)
        self.assertEqual((report['assessment'], report['checked']), ('incomplete', 0))
        self.assertEqual(len(report['issues']), 12)
        self.assertIn("'e0'", report['issues'][0])
        self.assertEqual(element_report({'samples': [{'time': 0}]}, plan)['assessment'], 'unknown')


if __name__ == '__main__':
    unittest.main()
