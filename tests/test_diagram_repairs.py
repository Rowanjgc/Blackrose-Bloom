"""Source repairs are bounded, tool-free, durable, scoped and cancellable."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from blackrose.diagrams import replace_diagram, repaired_source
from blackrose.gui.session import Session
from blackrose.prompts import wire_message
from blackrose.storage.records import MessageState
from test_gui_images import make_runtime


BAD = 'flowchart LR\nA[Start] --> B[Verify]\\nB --> C[Close]\n'
GOOD = 'flowchart LR\nA[Start] --> B[Verify]\nB --> C[Close]\n'
ANSWER = 'Before.\n\n```mermaid\n' + BAD + '```\n\nAfter.'


class Backend:
    def __init__(self):
        self.requests = []
        self.answer = ANSWER
        self.hang = False
        self.started = asyncio.Event()

    async def load(self, model):
        pass

    async def models(self, provider=None):
        return [{'id': 'test-vision'}]

    async def aclose(self):
        pass

    async def stream(self, model, messages, tools, **options):
        self.requests.append((messages, tools, options))
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        yield {'delta': {'content': self.answer}, 'finish_reason': 'stop'}


class DiagramRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.config, self.runtime = make_runtime(Path(directory))
        self.backend = Backend()
        self.runtime.controller.backend = self.backend
        await self.runtime.open()
        self.addAsyncCleanup(self.runtime.close)
        self.events = []
        self.session = Session(self.runtime.controller, lambda kind, payload: self.events.append((kind, payload)))
        self.addAsyncCleanup(self.session.close)
        await self.session.boot()
        await self.session.submit('Explain a workflow.')
        await self.session._generation
        self.message = (await self.runtime.controller.current_messages())[-1]
        self.backend.requests.clear()
        self.backend.answer = GOOD
        self.backend.started.clear()

    async def drain(self):
        task = self.session._diagram_repair_task
        if task:
            await asyncio.wait_for(asyncio.shield(task), 5)

    async def report(self, source=BAD, block_id=''):
        return await self.session.diagram_failed(self.message.id, block_id, 0, source, 'Parse error')

    async def latest(self):
        return next(m for m in await self.runtime.controller.current_messages() if m.id == self.message.id)

    async def test_failure_repairs_only_source_preserves_draft_and_original_and_updates_history(self):
        self.session.update_draft('Keep this draft')
        self.assertEqual((await self.report())['status'], 'queued')
        await self.report()
        await self.drain()
        self.assertEqual(len(self.backend.requests), 1)
        messages, tools, options = self.backend.requests[0]
        self.assertEqual(tools, [])
        self.assertEqual(len(messages), 2)
        self.assertNotIn('Explain a workflow', str(messages))
        self.assertEqual(self.session._draft_text, 'Keep this draft')
        saved = await self.latest()
        self.assertEqual(saved.content, ANSWER)
        self.assertEqual(saved.blocks[0]['source'], ANSWER.replace(BAD, GOOD))
        self.assertEqual(len(await self.runtime.controller.current_messages()), 2)
        self.assertIn(GOOD, wire_message(saved, None, False)['content'])
        await self.session.render_transcript()
        payload = next(p for k, p in reversed(self.events) if k == 'transcript')['messages'][-1]
        self.assertNotIn('blocks', payload)
        self.assertIn('data-diagram-block="diagram-', payload['html'])
        self.assertNotIn('\\nB --', payload['html'])

    async def test_failed_replacement_or_reload_cannot_spend_again(self):
        self.backend.answer = BAD
        await self.report()
        await self.drain()
        self.assertEqual((await self.latest()).blocks[0]['diagram_repairs']['0']['status'], 'failed')
        reopened = Session(self.runtime.controller, lambda *args: None)
        self.addAsyncCleanup(reopened.close)
        await reopened.boot()
        await reopened.diagram_failed(self.message.id, f'diagram-{self.message.id}', 0, BAD, 'Still invalid')
        await asyncio.wait_for(reopened._diagram_repair_task, 5)
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual((await self.latest()).blocks[0]['diagram_repairs']['0']['status'], 'failed')

    async def test_disabled_stale_foreign_and_invalid_reports_do_not_call_model(self):
        self.config.diagram_repairs = 0
        self.assertEqual((await self.report())['status'], 'disabled')
        self.config.diagram_repairs = 1
        await self.report(source='Different source')
        await self.drain()
        self.assertEqual((await self.session.diagram_failed(True, '', 0, BAD, 'Bad'))['status'], 'unavailable')
        await self.runtime.controller.new_conversation()
        await self.report()
        await self.drain()
        self.assertFalse(self.backend.requests)

    async def test_repair_cannot_escape_fence_or_rewrite_prose(self):
        self.backend.answer = 'flowchart LR\nA-->B\n```\nInjected prose\n```mermaid\nA-->B'
        with self.assertLogs('blackrose.diagram_repairs', level='WARNING'):
            await self.report()
            await self.drain()
        saved = await self.latest()
        self.assertEqual(saved.content, ANSWER)
        self.assertEqual(saved.blocks[0]['source'], ANSWER)
        self.assertEqual(saved.blocks[0]['diagram_repairs']['0']['status'], 'failed')
        self.assertEqual(len(self.backend.requests), 1)

    async def test_interrupted_durable_claim_is_not_restarted(self):
        claim = await self.runtime.controller._repo.claim_diagram_repair(
            self.message.conversation_id, self.message.id, '', 0, BAD, 1)
        self.assertEqual(claim['status'], 'repairing')
        await self.report(BAD, claim['block_id'])
        await self.drain()
        self.assertFalse(self.backend.requests)
        self.assertEqual((await self.latest()).blocks[0]['diagram_repairs']['0']['status'], 'interrupted')

    async def test_original_citation_controls_survive_diagram_revision(self):
        from blackrose.citations import EvidenceRegistry, answer_units, validate_assessment
        from test_citations import assessment, RESULT
        registry = EvidenceRegistry(self.message.id)
        registry.capture('knowledge_open', RESULT)
        report = validate_assessment(assessment({'sources': registry.sources,
            'answer_units': answer_units(ANSWER)}), ANSWER, registry, 'balanced', 'fixture')
        await self.runtime.controller._repo.finish_message(self.message.id, ANSWER,
            MessageState.COMPLETE, audit=report)
        await self.report()
        await self.drain()
        saved = await self.latest()
        self.assertEqual(saved.audit, report)
        self.assertIn('Prior source-support assessment', wire_message(saved, None, False, include_audit=True)['content'])
        payload = self.session._message_payloads([saved], {})[0]
        self.assertEqual(payload['html'].count('class="claim-citation"'), 2)
        self.assertNotIn('\\nB --', payload['html'])

    async def test_saved_failure_waits_for_model_startup(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def load(model):
            started.set()
            await release.wait()

        self.backend.load = load
        reopened = Session(self.runtime.controller, lambda *args: None)
        self.addAsyncCleanup(reopened.close)
        boot = asyncio.create_task(reopened.boot())
        await asyncio.wait_for(started.wait(), 5)
        result = await reopened.diagram_failed(self.message.id, '', 0, BAD, 'Parse error')
        self.assertEqual(result['status'], 'queued')
        await asyncio.sleep(.01)
        self.assertFalse(self.backend.requests)
        release.set()
        await boot
        await asyncio.wait_for(reopened._diagram_repair_task, 5)
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual((await self.latest()).blocks[0]['source'], ANSWER.replace(BAD, GOOD))

    async def test_stop_cancels_active_repair_and_durably_consumes_attempt(self):
        self.backend.hang = True
        await self.report()
        task = self.session._diagram_repair_task
        await asyncio.wait_for(self.backend.started.wait(), 5)
        self.assertTrue(self.session.stop())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runtime.controller.is_generating)
        self.assertFalse(self.session._busy())
        self.assertEqual((await self.latest()).blocks[0]['diagram_repairs']['0']['status'], 'stopped')
        self.assertTrue(any(k == 'diagram_update' and p['repair']['status'] == 'stopped' for k, p in self.events))

    async def test_repair_waits_for_active_reply_and_drops_queued_work_on_stop(self):
        self.backend.hang = True
        await self.session.submit('Another question')
        task = self.session._generation
        await asyncio.wait_for(self.backend.started.wait(), 5)
        await self.report()
        await asyncio.sleep(.01)
        self.assertEqual(len(self.backend.requests), 1)
        repair = self.session._diagram_repair_task
        self.session.stop()
        await asyncio.gather(task, repair, return_exceptions=True)
        self.assertFalse((await self.latest()).blocks)

    async def test_navigation_during_repair_cannot_patch_other_chat(self):
        original = self.runtime.controller.selected_conversation
        await self.runtime.controller.new_conversation()
        other = self.runtime.controller.selected_conversation
        await self.runtime.controller.open_conversation(original.id)
        self.backend.hang = True
        await self.report()
        await asyncio.wait_for(self.backend.started.wait(), 5)
        # Navigation is allowed while a reply/repair owns the generation slot.
        await self.runtime.controller.open_conversation(other.id)
        self.session.stop()
        await asyncio.gather(self.session._diagram_repair_task, return_exceptions=True)
        self.assertFalse(any(k == 'diagram_update' for k, _ in self.events))
        saved = next(m for m in await self.runtime.controller.conversation_messages(original.id) if m.id == self.message.id)
        self.assertEqual(saved.blocks[0]['diagram_repairs']['0']['status'], 'stopped')

    def test_replacement_preserves_fences_containers_and_prose(self):
        for text in (ANSWER, '\n'.join('> ' + line for line in ANSWER.splitlines())):
            fixed = replace_diagram(text, 0, GOOD)
            self.assertIn('Before', fixed)
            self.assertIn('After', fixed)
            self.assertNotIn('\\nB -->', fixed)
        self.assertEqual(repaired_source('```mermaid\n' + GOOD + '```'), GOOD)
        with self.assertRaises(ValueError):
            repaired_source('```mermaid\n' + GOOD + '```\nExtra prose')
