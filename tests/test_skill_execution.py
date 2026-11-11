"""Passive review and real confined fixtures; no provider, GPU or user-data access."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blackrose.gui.bridge import Api
from blackrose.skill_execution import (ScriptRunner, ScriptUnavailable, execution_review,
                                       package_snapshot, runtime_command, validate_artifact)
from blackrose.skills import SkillManager
from test_security_boundaries import BoundaryTest

ROOT = Path(__file__).resolve().parents[1]


class ExecutionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.manager = SkillManager(root / 'managed')
        self.source = root / 'source'
        self.source.mkdir()
        (self.source / 'SKILL.md').write_text('---\nname: calculator\n---\nUse calculate with JSON.')
        (self.source / 'execution.json').write_text(json.dumps(
            {'version': 1, 'entry_points': {'calculate': 'calculate.py'}}))
        (self.source / 'calculate.py').write_text('raise AssertionError("never run during review")')
        (self.source / 'data.txt').write_text('data')
        self.manager.install(str(self.source), approved=True)
        self.package = self.manager.root / 'calculator'

    def approve(self):
        review = self.manager.execution_review('calculator')
        self.manager.approve_execution('calculator', review['digest'])
        return review

    def test_passive_install_read_enable_and_instruction_approval_grant_no_execution(self):
        self.manager.read('calculator')
        self.manager.set_enabled('calculator', True)
        self.assertFalse(self.manager.has_execution_approval())
        with self.assertRaisesRegex(ValueError, 'Review this execution'):
            self.manager.prepare_execution('calculator', 'calculate')
        review = self.approve()
        self.assertIn('calculate.py', review['sources'])
        self.assertIn('data.txt', {r['path'] for r in review['resources']})
        self.assertTrue(self.manager.has_execution_approval())

    def test_every_package_change_invalidates_execution_while_resource_reads_remain_available(self):
        for name in ('calculate.py', 'data.txt', 'execution.json', 'added.txt'):
            with self.subTest(name=name):
                self.approve()
                target = self.package / name
                original = target.read_bytes() if target.exists() else None
                target.write_bytes((original or b'') + b' ')
                with self.assertRaises(ValueError):
                    self.manager.prepare_execution('calculator', 'calculate')
                with self.assertRaises(ValueError):
                    self.manager.approve_execution('calculator', '0' * 64)
                self.assertIn('Use calculate', self.manager.read('calculator')['text'])
                if original is None:
                    target.unlink()
                else:
                    target.write_bytes(original)

    def test_snapshot_runs_reviewed_bytes_after_live_file_changes(self):
        self.approve()
        files, entry = self.manager.prepare_execution('calculator', 'calculate')
        (self.package / entry).write_text('unreviewed replacement')
        self.assertIn(b'never run during review', files[entry])

    def test_review_exposes_hidden_source_characters_without_changing_executed_bytes(self):
        body = '# hidden \u202e directive\nprint("ok")\n'
        (self.package / 'calculate.py').write_text(body)
        review = self.approve()
        self.assertNotIn('\u202e', review['sources']['calculate.py'])
        self.assertIn('\\u202E', review['sources']['calculate.py'])
        files, _ = self.manager.prepare_execution('calculator', 'calculate')
        self.assertEqual(files['calculate.py'].decode(), body)

    def test_disable_revoke_and_unknown_entry_deny_execution(self):
        self.approve()
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.manager.prepare_execution('calculator', 'other')
        self.manager.set_enabled('calculator', False)
        with self.assertRaisesRegex(ValueError, 'disabled'):
            self.manager.prepare_execution('calculator', 'calculate')
        self.manager.set_enabled('calculator', True)
        self.manager.revoke_execution('calculator')
        with self.assertRaisesRegex(ValueError, 'Review'):
            self.manager.prepare_execution('calculator', 'calculate')

    def test_symlinks_special_files_and_unsupported_dependencies_fail_closed(self):
        (self.package / 'link').symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'links'):
            execution_review(self.package)
        (self.package / 'link').unlink()
        os.mkfifo(self.package / 'fifo')
        with self.assertRaisesRegex(ValueError, 'special'):
            package_snapshot(self.package)
        (self.package / 'fifo').unlink()
        (self.package / 'execution.json').write_text(json.dumps(
            {'version': 1, 'entry_points': {'calculate': 'calculate.py'}, 'pip': ['requests']}))
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            execution_review(self.package)

    def test_bridge_requires_native_approval_and_rechecks_after_dialog(self):
        class Loop:
            def run(self, operation):
                return asyncio.run(operation)

        api = Api.__new__(Api)
        api._loop = Loop()
        api._window = object()
        api._runtime = SimpleNamespace(controller=SimpleNamespace(skills=self.manager, is_generating=False))
        review = api.review_skill_execution('calculator')
        with patch('blackrose.gui.external.ask', return_value=False):
            self.assertFalse(api.approve_skill_execution('calculator', review['digest']))
        self.assertFalse(self.manager.has_execution_approval())
        with patch('blackrose.gui.external.ask', return_value=True):
            self.assertTrue(api.approve_skill_execution('calculator', review['digest']))
        self.manager.revoke_execution('calculator')

        def change(*args, **kwargs):
            (self.package / 'data.txt').write_text('changed while dialog open')
            return True

        with patch('blackrose.gui.external.ask', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'changed during review'):
                api.approve_skill_execution('calculator', review['digest'])
        self.assertFalse(self.manager.has_execution_approval())


class ArtifactValidationTests(unittest.TestCase):
    def test_only_bounded_plain_text_basename_is_accepted(self):
        value = {'filename': 'report.txt', 'content': 'mean: 2'}
        self.assertEqual(validate_artifact({'artifact': value}), value)
        for artifact in ({**value, 'filename': '../report.txt'}, {**value, 'filename': 'report.html'},
                         {**value, 'content': 'x' * 32001}, {**value, 'mime_type': 'text/html'}):
            with self.subTest(artifact=str(artifact)[:120]), self.assertRaises(ValueError):
                validate_artifact({'artifact': artifact})


class ConfinedExecutionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Offline environments may prohibit namespaces. A real probe records that
        # limitation; supported hosts run every fixture, never a mocked subprocess.
        try:
            command = runtime_command(Path('/tmp'), 'unused.py')
        except ScriptUnavailable as error:
            raise unittest.SkipTest(str(error)) from error
        command = command[:command.index('--')] + ['--', '/usr/bin/python3.13' if Path('/usr/bin/python3.13').exists()
                                                   else '/usr/bin/python3.12', '-I', '-S', '-c', 'print(1)']
        probe = subprocess.run(command, capture_output=True, timeout=10)
        if probe.returncode:
            raise unittest.SkipTest('Host forbids required namespaces: ' + probe.stderr.decode(errors='replace')[:200])

    async def asyncSetUp(self):
        self.runner = ScriptRunner()
        self.addAsyncCleanup(self.runner.close)

    async def run_source(self, source, payload=None):
        return await self.runner.run({'run.py': source.encode()}, 'run.py', payload)

    async def test_successful_computation_and_representative_packaged_report(self):
        root = ROOT / 'skills/numeric-summary'
        files, _, manifest = package_snapshot(root)
        result = await self.runner.run(files, manifest['entry_points']['summarize'], {'values': [1, 2, 6]})
        self.assertEqual(result['result']['mean'], 3)
        self.assertIn('Mean: 3', validate_artifact(result['result'])['content'])
        self.assertTrue(result['untrusted'])

    async def test_host_files_network_credentials_processes_and_limits_are_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            secret = Path(temporary) / 'secret'
            secret.write_text('PRIVATE')
            source = '''import json, os, socket, subprocess, resource
denied = {}
def check(name, fn):
    try: fn()
    except (OSError, RuntimeError, ValueError): denied[name] = True
    else: denied[name] = False
check('read', lambda: open(SECRET).read())
check('write', lambda: open(SECRET, 'w').write('changed'))
check('network', lambda: socket.socket())
check('fork', lambda: os.fork())
check('exec', lambda: os.execv('/usr/bin/python3.13', ['python', '-c', 'pass']))
check('spawn', lambda: subprocess.run(['/usr/bin/python3.13', '-c', 'pass']))
check('package_write', lambda: open('/skill/run.py', 'w'))
check('scratch', lambda: open('/tmp/large', 'wb').write(b'x' * 1100000))
check('raise_limit', lambda: resource.setrlimit(resource.RLIMIT_CPU, (100, 100)))
denied['credential'] = os.environ.get('BRA_TEST_SECRET') is None
denied['proc'] = not os.path.exists('/proc')
print(json.dumps(denied))
'''.replace('SECRET', repr(str(secret)), 2)
            with patch.dict(os.environ, {'BRA_TEST_SECRET': 'PRIVATE'}):
                result = await self.run_source(source)
            self.assertTrue(all(result['result'].values()), result)
            self.assertEqual(secret.read_text(), 'PRIVATE')

    async def test_missing_import_invalid_json_and_nonzero_exit_are_readable_failures(self):
        for source in ('import not_a_real_dependency', 'print("not JSON")',
                       'print("NaN")', 'print("1e999")', 'raise ValueError("fixture failure")'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                await self.run_source(source)

    async def test_memory_and_cpu_limits_terminate_excessive_work(self):
        with self.assertRaisesRegex(ValueError, 'MemoryError'):
            await self.run_source('data = bytearray(300 * 1024 * 1024)')
        with self.assertRaisesRegex(ValueError, 'exited unsuccessfully'):
            await self.run_source('while True: pass')

    async def test_output_and_input_are_bounded(self):
        with self.assertRaisesRegex(ValueError, 'input exceeds'):
            await self.run_source('pass', 'x' * 64001)
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream), self.assertRaisesRegex(ValueError, 'output exceeds'):
                await self.run_source(f'import sys\nwhile True: sys.{stream}.write("x" * 4096); sys.{stream}.flush()')

    async def test_wall_timeout_stop_and_shutdown_reap_processes(self):
        with patch('blackrose.skill_execution.WALL_SECONDS', .2):
            with self.assertRaisesRegex(ValueError, 'deadline'):
                await self.run_source('import time; time.sleep(60)')
        task = asyncio.create_task(self.run_source('import time; time.sleep(60)'))
        await asyncio.sleep(.15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runner.jobs)
        task = asyncio.create_task(self.run_source('import time; time.sleep(60)'))
        await asyncio.sleep(.15)
        await self.runner.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ScriptUnavailable):
            await self.run_source('print(1)')

    async def test_unavailable_confinement_never_falls_back_to_host_execution(self):
        with patch('blackrose.skill_execution.runtime_command', side_effect=ScriptUnavailable('Missing runtime')):
            with self.assertRaisesRegex(ScriptUnavailable, 'Missing runtime'):
                await self.run_source('raise AssertionError("must not execute")')
        self.assertFalse(self.runner.jobs)

    async def test_stop_during_launch_reaps_the_created_process(self):
        original = asyncio.create_subprocess_exec
        created = asyncio.Event()
        release = asyncio.Event()
        children = []

        async def launch(*args, **kwargs):
            process = await original(*args, **kwargs)
            children.append(process)
            created.set()
            await release.wait()
            return process

        with patch('blackrose.skill_execution.asyncio.create_subprocess_exec', side_effect=launch):
            task = asyncio.create_task(self.run_source('import time; time.sleep(60)'))
            await asyncio.wait_for(created.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(all(child.returncode is not None for child in children))


class ScriptTurnTests(BoundaryTest):
    @classmethod
    def setUpClass(cls):
        ConfinedExecutionTests.setUpClass()

    def install(self, approved=True):
        manager = self.runtime.controller.skills
        manager.install(str(ROOT / 'skills/numeric-summary'), approved=True)
        if approved:
            manager.approve_execution('numeric-summary', manager.execution_review('numeric-summary')['digest'])
        return manager

    async def test_explicit_tool_returns_useful_report_in_originating_conversation(self):
        self.install()
        c = self.backend([[('run_skill_script', {'skill_id': 'numeric-summary',
            'entry_point': 'summarize', 'input': {'values': [1, 2, 6]}})], 'Computed the summary.'])
        await c.send('Summarize these numbers')
        result = self.tool_results(c.backend)[0]
        self.assertEqual(result['result']['mean'], 3)
        self.assertTrue(result['untrusted'])
        messages = await c.current_messages()
        artifact = messages[-1].attachments[0]
        self.assertEqual(artifact.mime_type, 'text/plain')
        self.assertIn('Mean: 3', c._store.path_for(artifact.storage_path).read_text())
        self.assertTrue(Path(artifact.output_path).is_relative_to(self.downloads))

    async def test_instruction_approval_cannot_advertise_or_execute_script_tool(self):
        self.install(approved=False)
        c = self.backend([[('run_skill_script', {'skill_id': 'numeric-summary',
            'entry_point': 'summarize', 'input': {'values': [1, 2, 6]}})], 'Unavailable.'])
        await c.send('Summarize these numbers')
        self.assertNotIn('run_skill_script', [t['function']['name'] for t in c.backend.offered[0]])
        self.assertIn('unavailable', self.tool_results(c.backend)[0]['error'])
        self.assertFalse((await c.current_messages())[-1].attachments)

    async def test_script_output_keeps_later_web_requests_behind_outbound_approval(self):
        self.install()
        web = self.web()
        asked, approve = self.approver(False)
        c = self.backend([[('run_skill_script', {'skill_id': 'numeric-summary',
            'entry_point': 'summarize', 'input': {'values': [1, 2]}})],
            [('web_search', {'query': 'private computed result'})], 'Done.'])
        await c.send('Compute and search', approve_outbound=approve)
        self.assertEqual(self.tool_results(c.backend)[-1]['code'], 'approval_declined')
        self.assertEqual(len(asked), 1)
        self.assertEqual(web.calls, [])

    async def test_stop_during_report_publication_preserves_settled_attachment(self):
        from blackrose.workspace import WorkspaceFiles
        import threading
        self.install()
        c = self.backend([[('run_skill_script', {'skill_id': 'numeric-summary',
            'entry_point': 'summarize', 'input': {'values': [1, 2, 6]}})], 'Done.'])
        entered, release = threading.Event(), threading.Event()
        write = WorkspaceFiles.write

        def blocked(files, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Report publication fixture timed out')
            return write(files, *args, **kwargs)

        with patch.object(WorkspaceFiles, 'write', blocked):
            task = asyncio.create_task(c.send('Save the report'))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        attachments = (await c.current_messages())[-1].attachments
        self.assertEqual(len(attachments), 1)
        text = await asyncio.to_thread(Path(attachments[0].output_path).read_text)
        self.assertIn('Mean: 3', text)

    async def test_navigation_during_script_preserves_report_owner(self):
        self.install()
        c = self.runtime.controller
        origin = c.conversation.id
        await c.new_conversation()
        selected = c.conversation.id
        await c.open_conversation(origin)
        entered, release = asyncio.Event(), asyncio.Event()
        run = c.skill_scripts.run

        async def delayed(*args):
            entered.set()
            await release.wait()
            return await run(*args)

        c = self.backend([[('run_skill_script', {'skill_id': 'numeric-summary',
            'entry_point': 'summarize', 'input': {'values': [2, 4]}})], 'Done.'])
        with patch.object(c.skill_scripts, 'run', side_effect=delayed):
            task = asyncio.create_task(c.send('Compute a report'))
            await asyncio.wait_for(entered.wait(), 3)
            await c.open_conversation(selected)
            release.set()
            await task
        self.assertNotEqual(origin, selected)
        self.assertEqual(c.conversation.id, selected)
        self.assertFalse(await c.current_messages())
        messages = await c._repo.messages(origin)
        self.assertEqual(len(messages[-1].attachments), 1)


if __name__ == '__main__':
    unittest.main()
