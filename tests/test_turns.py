"""Admission, durable turn handoff and host-owned effect budgets."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from blackrose.attachments import TooManyAttachments
from blackrose.errors import InputRejected, RecoveryRequired
from blackrose.image_generation import GeneratedImage
from blackrose.attachments.images import inspect_image
from blackrose.prompts import PromptBuilder
from blackrose.storage.records import MessageState
from blackrose.turns import TurnRunner
import test_gui_images as fixtures


class Backend(fixtures.Backend):
    def __init__(self, steps=('Done.',), context=128000):
        self.steps = iter(steps)
        self.context = context
        self.requests = []
        self.offered = []
        self.load = AsyncMock()

    async def models(self, provider=None):
        return [{'id': name, 'architecture': {'input_modalities': ['text', 'image']},
                 'meta': {'n_ctx': self.context}}
                for name in ('test-vision', 'opencode-go/test-vision')]

    async def stream(self, model, messages, tools):
        self.requests.append(deepcopy(messages))
        self.offered.append(deepcopy(tools))
        step = next(self.steps)
        if isinstance(step, str):
            yield {'delta': {'content': step}, 'finish_reason': 'stop'}
        else:
            yield {'delta': {'tool_calls': [
                {'index': index, 'id': f'call-{len(self.requests)}-{index}', 'function': {
                    'name': name, 'arguments': json.dumps(args)}}
                for index, (name, args) in enumerate(step)]}, 'finish_reason': 'tool_calls'}


async def block_final_save(database, *, fail=False):
    """Hold the complete-reply write inside SQLite until released.

    Returns ``(entered, release)`` threading events. With ``fail`` the
    released write raises, so nothing it wrote commits.
    """
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        if not release.wait(5):
            raise TimeoutError('final save was not released')
        if fail:
            raise RuntimeError('injected final save failure')
        return 0
    def install(conn):
        conn.create_function('block_final_save', 0, block)
        conn.executescript('''
            CREATE TEMP TRIGGER block_final_save BEFORE UPDATE OF state ON messages
            WHEN NEW.role = 'assistant' AND NEW.state = 'complete'
            BEGIN SELECT block_final_save(); END;''')
    await database.run(install)
    return entered, release


async def stop_during_final_save(test, task, entered, release, stop=None):
    """Stop twice while the final save is held, then let the write finish."""
    stop = stop or task.cancel
    try:
        test.assertTrue(await asyncio.to_thread(entered.wait, 2))
        stop()
        await asyncio.sleep(0)
        stop()
        await asyncio.sleep(0)
        test.assertFalse(task.done())
    finally:
        release.set()
    with test.assertRaises(asyncio.CancelledError):
        await task


class TurnTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ImageTests.asyncSetUp
    asyncTearDown = fixtures.ImageTests.asyncTearDown

    def backend(self, steps=('Done.',), context=128000):
        c = self.runtime.controller
        c.backend = Backend(steps, context)
        return c

    def generator(self, **kwargs):
        image = GeneratedImage(fixtures.png(), inspect_image(fixtures.png()), 'test-image', 0.0)
        return AsyncMock(generate=AsyncMock(return_value=replace(image, **kwargs)))

    async def test_accepted_document_tail_reaches_model(self):
        c = self.backend()
        text = 'a' * 28000 + '\nTAIL_EVIDENCE'
        pending = c.attach(text.encode(), 'evidence.txt')
        self.assertFalse(pending.document.truncated)
        await c.send('Summarize the final section', attachments=[pending])
        self.assertIn('TAIL_EVIDENCE', c.backend.requests[0][-1]['content'])

    async def test_turn_reads_history_attachments_once_across_both_builds(self):
        c = self.backend(steps=('First answer.', 'Second answer.'))
        await c.send('Read these', attachments=[c.attach(fixtures.png(), 'image.png'),
                                              c.attach(b'Document evidence', 'notes.txt')])
        history = await c.current_messages()
        paths = [a.storage_path if a.kind == 'image' else a.text_path
                 for a in history[0].attachments]
        with patch.object(c._store, 'read', wraps=c._store.read) as read:
            await c.send('Follow up')
        self.assertCountEqual([call.args[0] for call in read.call_args_list], paths)
        sent = json.dumps(c.backend.requests[-1])
        self.assertIn('Document evidence', sent)
        self.assertIn('data:image/png;base64,', sent)
        self.assertIn('Local filesystem:', sent)

    async def test_oversized_actual_prompt_is_rejected_before_turn_persistence(self):
        c = self.backend(context=4096)
        pending = c.attach(b'a' * 28000, 'evidence.txt')
        with self.assertRaises(InputRejected):
            await c.send('Summarize', attachments=[pending])
        self.assertFalse(c.last_request_stored)
        self.assertEqual(await c.current_messages(), [])
        self.assertEqual(c.backend.requests, [])

    async def test_context_is_rechecked_after_tool_evidence(self):
        c = self.backend([[('knowledge_search', {'query': 'needle'})], 'Wrongly continued.'], context=32000)
        c.config.knowledge_enabled = True
        c.knowledge.call = AsyncMock(return_value={'results': [], 'text': 'evidence ' * 6000})
        with self.assertRaises(InputRejected) as rejected:
            await c.send('Find evidence')
        self.assertIn('conservative host estimate', str(rejected.exception))
        self.assertIn('Tool results and any repair history', str(rejected.exception))
        self.assertIn('configured model context: 32,000', str(rejected.exception))
        self.assertEqual(len(c.backend.requests), 1)
        self.assertEqual((await c.current_messages())[-1].state.value, 'error')
        self.assertFalse(c.is_generating)

    def install_skill(self, body='INSTRUCTION_BODY', reference='SELECTED_REFERENCE'):
        source = self.root / 'source-skill'
        source.mkdir()
        (source / 'SKILL.md').write_text(
            '---\nname: focused-review\ndescription: Review proposals.\n---\n' + body)
        (source / 'references').mkdir()
        (source / 'references/needed.md').write_text(reference)
        (source / 'references/other.md').write_text('UNNEEDED_REFERENCE')
        self.runtime.controller.skills.install(str(source), approved=True)

    async def test_skill_instructions_and_selected_resources_are_disclosed_in_stages(self):
        c = self.backend([
            [('read_skill', {'skill_id': 'focused-review', 'path': ''})],
            [('read_skill', {'skill_id': 'focused-review', 'path': 'references'})],
            [('read_skill', {'skill_id': 'focused-review', 'path': 'references/needed.md'})],
            'Reviewed.', 'Next answer.'])
        self.install_skill()
        with patch.object(c.skills, 'read', wraps=c.skills.read) as read:
            await c.send('Review this proposal using the relevant skill.')
            self.assertEqual([call.kwargs.get('path', 'SKILL.md') for call in read.call_args_list],
                             ['SKILL.md', 'references', 'references/needed.md'])
            initial, instructions, directory, reference = map(json.dumps, c.backend.requests)
            self.assertNotIn('INSTRUCTION_BODY', initial)
            self.assertNotIn('SELECTED_REFERENCE', initial)
            self.assertIn('INSTRUCTION_BODY', instructions)
            self.assertNotIn('SELECTED_REFERENCE', instructions)
            self.assertIn('references/needed.md', directory)
            self.assertNotIn('SELECTED_REFERENCE', directory)
            self.assertIn('SELECTED_REFERENCE', reference)
            self.assertNotIn('UNNEEDED_REFERENCE', reference)
            messages = await c.current_messages()
            self.assertEqual(messages[-1].content, 'Reviewed.')
            self.assertEqual(await c._repo.evidence_for(messages[-1].id), [])
            await c.send('An unrelated follow-up.')
            self.assertEqual(read.call_count, 3)
            self.assertNotIn('INSTRUCTION_BODY', json.dumps(c.backend.requests[-1]))
            self.assertNotIn('SELECTED_REFERENCE', json.dumps(c.backend.requests[-1]))

    async def test_skill_content_is_admitted_after_loading_before_next_inference(self):
        c = self.backend([[('read_skill', {'skill_id': 'focused-review'})], 'Wrongly continued.'],
                         context=32000)
        self.install_skill(body='INSTRUCTION_BODY ' * 3000)
        with self.assertRaises(InputRejected):
            await c.send('Review this proposal.')
        self.assertEqual(len(c.backend.requests), 1)
        self.assertEqual((await c.current_messages())[-1].state.value, 'error')

    async def test_disabled_skill_read_returns_error_without_disclosing_content(self):
        c = self.backend([[('read_skill', {'skill_id': 'focused-review'})], 'Unavailable.'])
        self.install_skill()
        c.skills.set_enabled('focused-review', False)
        await c.send('Review a proposal.')
        self.assertNotIn('focused-review', c.backend.requests[0][0]['content'])
        self.assertNotIn('INSTRUCTION_BODY', json.dumps(c.backend.requests[-1]))
        result = json.loads(c.backend.requests[-1][-1]['content'])
        self.assertIn('unavailable', result['error'])
        self.assertNotIn('read_skill', [t['function']['name'] for t in c.backend.offered[0]])

    async def test_spent_tool_budget_asks_for_the_answer(self):
        c = self.backend([[('activate_tools', {'capability': 'shell'})], 'Answer.'])
        c.config.max_tool_rounds = 1
        await c.send('Research this.')
        self.assertEqual(c.backend.offered[1], [])
        self.assertEqual(c.backend.requests[1][-1]['role'], 'user')
        self.assertIn('Tool budget spent', c.backend.requests[1][-1]['content'])
        self.assertNotIn('Tool budget spent', json.dumps(c.backend.requests[0]))
        self.assertEqual((await c.current_messages())[-1].state.value, 'complete')

    async def test_text_tool_call_after_spent_budget_is_not_a_finished_answer(self):
        leaked = ('<tool_call>\n<function=fetch_content>\n<parameter=url>\n'
                  'https://example.com\n</parameter>\n</function>\n</tool_call>')
        c = self.backend([[('activate_tools', {'capability': 'shell'})], leaked])
        c.config.max_tool_rounds = 1
        with self.assertRaisesRegex(RuntimeError, 'Tool limit reached without a final answer'):
            await c.send('Research this.')
        self.assertEqual((await c.current_messages())[-1].state.value, 'error')

    async def test_send_revalidates_final_attachment_aggregate(self):
        c = self.backend()
        pending = [c.attach(fixtures.png(), f'{index}.png') for index in range(5)]
        with self.assertRaises(TooManyAttachments):
            await c.send('Describe these', attachments=pending)
        self.assertEqual(await c.current_messages(), [])
        c.backend.load.assert_not_awaited()

    async def test_cancel_after_commit_receives_identity_then_finalizes_reply(self):
        c = self.backend()
        committed, release = asyncio.Event(), asyncio.Event()
        begin = c._repo.begin_turn
        async def blocked(*args, **kwargs):
            result = await begin(*args, **kwargs)
            committed.set()
            await release.wait()
            return result
        c._repo.begin_turn = blocked
        task = asyncio.create_task(c.send('Keep this request'))
        await asyncio.wait_for(committed.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        messages = await c.current_messages()
        self.assertEqual([m.state.value for m in messages], ['complete', 'cancelled'])
        self.assertTrue(c.last_request_stored)
        self.assertFalse(c.is_generating)

    async def test_cancel_during_prompt_build_stores_nothing_and_releases_the_turn(self):
        c = self.backend(['Next answer.'])
        entered, release = threading.Event(), threading.Event()
        build = PromptBuilder.build
        def blocked(builder, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('prompt build was not released')
            return build(builder, *args, **kwargs)
        with patch.object(PromptBuilder, 'build', blocked):
            task = asyncio.create_task(c.send('Unsent question'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await c.current_messages(), [])
        self.assertEqual(c.backend.requests, [])
        self.assertFalse(c.last_request_stored)
        self.assertFalse(c.is_generating)
        self.assertIsNone(c.outbound)
        await c.send('Next question')
        self.assertEqual([m.content for m in await c.current_messages()],
                         ['Next question', 'Next answer.'])

    async def test_stop_during_final_save_keeps_the_committed_reply(self):
        # The committed complete reply stands; Stop still propagates.
        c = self.backend(['Complete answer.'])
        c.schedule_title = Mock()
        c.schedule_memory_sync = Mock()
        c.config.memory_enabled = True
        c.memory = Mock(recall=AsyncMock(return_value=[]), aclose=AsyncMock())
        entered, release = await block_final_save(self.runtime.database)
        await stop_during_final_save(self, asyncio.create_task(c.send('Question')), entered, release)
        request, reply = await c.current_messages()
        self.assertEqual(reply.state, MessageState.COMPLETE)
        self.assertEqual(reply.content, 'Complete answer.')
        self.assertIsNone(reply.error)
        self.assertEqual(c.last_completed_turn.assistant_message.id, reply.id)
        self.assertEqual(c.last_completed_turn.assistant_message.state, MessageState.COMPLETE)
        self.assertFalse(c.is_generating)
        self.assertIsNone(c.outbound)
        # Follow-ups belong to the stored reply, as when Stop arrives after the turn returns.
        title_request, title_answer = c.schedule_title.call_args.args[1:3]
        self.assertEqual((title_request.id, title_answer.id), (request.id, reply.id))
        c.schedule_memory_sync.assert_called_once_with()
        # The memory job queued with the complete write stays deliverable.
        self.assertEqual((await c._repo.memory_turn(reply.id))[1].id, reply.id)

    async def test_stop_during_a_failed_final_save_settles_the_reply_as_cancelled(self):
        c = self.backend(['Complete answer.'])
        c.schedule_title = Mock()
        entered, release = await block_final_save(self.runtime.database, fail=True)
        with self.assertLogs('blackrose.turns', level='WARNING') as logs:
            await stop_during_final_save(self, asyncio.create_task(c.send('Question')), entered, release)
        self.assertIn('failed during Stop', logs.output[0])
        reply = (await c.current_messages())[-1]
        self.assertEqual(reply.state, MessageState.CANCELLED)
        self.assertEqual(reply.content, 'Complete answer.')
        self.assertEqual(reply.error, 'Stopped')
        self.assertIsNone(c.last_completed_turn)
        self.assertFalse(c.is_generating)
        c.schedule_title.assert_not_called()

    async def test_stop_before_the_final_save_starts_settles_the_reply_as_cancelled(self):
        c = self.runtime.controller
        c.schedule_title = Mock()
        checkpointing, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        checkpoint = c._repo.checkpoint_content
        drain = TurnRunner._stop_checkpoints

        async def held_checkpoint(identity, content):
            checkpointing.set()
            await release.wait()
            return await checkpoint(identity, content)

        async def signalled_drain(runner):
            draining.set()
            await drain(runner)

        class FinishingBackend(Backend):
            async def stream(self, model, messages, tools):
                yield {'delta': {'content': 'Accepted answer.'}}
                await checkpointing.wait()
                yield {'delta': {}, 'finish_reason': 'stop'}

        c.backend = FinishingBackend()
        # Draining the held checkpoint is the last step before the complete write.
        with patch('blackrose.turns.CONTENT_CHECKPOINT_SECONDS', 0.01), \
                patch.object(TurnRunner, '_stop_checkpoints', signalled_drain), \
                patch.object(c._repo, 'checkpoint_content', side_effect=held_checkpoint), \
                patch.object(c._repo, 'finish_message', wraps=c._repo.finish_message) as finish:
            task = asyncio.create_task(c.send('Question'))
            try:
                await asyncio.wait_for(draining.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([call.args[2] for call in finish.call_args_list], [MessageState.CANCELLED])
        reply = (await c.current_messages())[-1]
        self.assertEqual(reply.state, MessageState.CANCELLED)
        self.assertEqual(reply.content, 'Accepted answer.')
        self.assertEqual(reply.error, 'Stopped')
        self.assertIsNone(c.last_completed_turn)
        c.schedule_title.assert_not_called()

    async def test_more_than_twelve_parallel_calls_execute_nothing(self):
        c = self.backend([[('image_gen', {'prompt': f'Bird {i}'}) for i in range(13)], 'Unreached.'])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator()
        with self.assertRaisesRegex(RuntimeError, 'exceeded the tool-call limit'):
            await c.send('Draw many birds')
        c.image_generator.generate.assert_not_awaited()
        self.assertEqual(len(c.backend.requests), 1)
        self.assertEqual((await c.current_messages())[-1].state, MessageState.ERROR)

    async def test_tool_calls_after_the_budget_is_spent_execute_nothing(self):
        c = self.backend([[('activate_tools', {'capability': 'shell'})],
                          [('image_gen', {'prompt': 'A bird'})]])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator()
        c.config.max_tool_rounds = 1
        with self.assertRaisesRegex(RuntimeError, 'exceeded the tool-call limit'):
            await c.send('Draw a bird')
        self.assertEqual(c.backend.offered[1], [])
        c.image_generator.generate.assert_not_awaited()
        self.assertEqual((await c.current_messages())[-1].state, MessageState.ERROR)

    async def test_generated_images_are_shown_to_a_vision_model_next_round(self):
        c = self.backend([[('image_gen', {'prompt': 'A bird'})], 'Checked.'])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator()
        await c.send('Draw a bird')
        follow_up = c.backend.requests[1][-1]
        self.assertEqual(follow_up['role'], 'user')
        self.assertIn('Generated image results', follow_up['content'][0]['text'])
        self.assertTrue(follow_up['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertEqual((await c.current_messages())[-1].state, MessageState.COMPLETE)

    async def test_controller_reuse_initializes_conversation_before_reading_history(self):
        c = self.backend(['First answer.', 'Second answer.'])
        await c.send('First question')
        c.conversation = None
        await c.send('Second question')
        self.assertEqual([m['content'] for m in c.backend.requests[-1][1:]],
                         ['First question', 'First answer.', 'Second question'])

    async def test_execution_failure_blocks_model_retries_and_marks_reply_failed(self):
        call = [('image_gen', {'prompt': 'A bird'})]
        c = self.backend([call, call, 'Successfully generated.'])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator()
        c.image_generator.generate.side_effect = RuntimeError('Image process unavailable')
        with self.assertRaisesRegex(RuntimeError, 'Image generation failed'):
            await c.send('Draw a bird')
        c.image_generator.generate.assert_awaited_once()
        self.assertNotIn('image_gen', [t['function']['name'] for t in c.backend.offered[1]])
        self.assertEqual((await c.current_messages())[-1].state.value, 'error')

    async def test_validation_repair_does_not_consume_an_image_attempt(self):
        c = self.backend([[('image_gen', {})], [('image_gen', {'prompt': 'A bird'})], 'Ready.'])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator()
        await c.send('Draw a bird')
        c.image_generator.generate.assert_awaited_once()
        self.assertEqual((await c.current_messages())[-1].state.value, 'complete')

    async def test_reference_validation_repair_does_not_consume_an_image_attempt(self):
        c = self.backend([
            [('image_gen', {'prompt': 'A bird', 'reference_attachment_ids': [9999]})],
            [('image_gen', {'prompt': 'A bird'})], 'Ready.'])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator()
        await c.send('Draw a bird')
        c.image_generator.generate.assert_awaited_once()
        self.assertIn('image_gen', [t['function']['name'] for t in c.backend.offered[1]])
        self.assertEqual((await c.current_messages())[-1].state.value, 'complete')

    async def test_image_effect_budget_is_enforced_within_a_parallel_batch(self):
        c = self.backend([[('image_gen', {'prompt': f'Bird {i}'}) for i in range(5)], 'Four images.'])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator()
        await c.send('Draw several birds')
        self.assertEqual(c.image_generator.generate.await_count, 4)
        self.assertEqual(len((await c.current_messages())[-1].attachments), 4)

    async def test_completed_image_is_persisted_before_recovery_failure_aborts_chat(self):
        c = self.backend([[('image_gen', {'prompt': 'A bird'})], 'Must not continue.'])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator(cleanup_warning='Memory release failed')
        images = []
        with self.assertRaisesRegex(RecoveryRequired, 'Memory release failed'):
            await c.send('Draw a bird', on_images=lambda saved: images.extend(saved))
        self.assertEqual(len(images), 1)
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, 'error')
        self.assertTrue(c.attachment_path(saved.attachments[0]).is_file())
        self.assertEqual(len(c.backend.requests), 1)

    async def test_post_download_cancellation_keeps_the_image(self):
        c = self.backend([[('image_gen', {'prompt': 'A bird'})]])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator(cancelled=True)
        images = []
        with self.assertRaises(asyncio.CancelledError):
            await c.send('Draw a bird', on_images=lambda saved: images.extend(saved))
        self.assertEqual(len(images), 1)
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, 'cancelled')
        self.assertEqual(len(saved.attachments), 1)

    async def test_stop_during_completed_image_persistence_settles_the_artifact(self):
        c = self.backend([[('image_gen', {'prompt': 'A bird'})]])
        c.config.image_provider = 'openrouter'
        c.image_generator = self.generator()
        entered, release = threading.Event(), threading.Event()
        persist = c._persist_attachments
        def blocked(attachments):
            if attachments:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('artifact writer was not released')
            return persist(attachments)
        c._persist_attachments = blocked
        images = []
        task = asyncio.create_task(c.send('Draw a bird', on_images=lambda saved: images.extend(saved)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(images), 1)
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, 'cancelled')
        self.assertTrue(c.attachment_path(saved.attachments[0]).is_file())

    async def test_unresolved_gpu_recovery_prevents_chat_model_loading(self):
        c = self.backend()
        c.local_image_generator = AsyncMock(recovery_error='Local image memory recovery failed')
        with self.assertRaises(RecoveryRequired):
            await c.send('Hello')
        c.backend.load.assert_not_awaited()
        self.assertEqual(await c.current_messages(), [])

    async def test_unresolved_gpu_recovery_also_prevents_model_selection(self):
        c = self.backend()
        c.local_image_generator = AsyncMock(recovery_error='Local image memory recovery failed')
        with self.assertRaises(RecoveryRequired):
            await c.select_model('another-model')
        c.backend.load.assert_not_awaited()
        self.assertEqual(c.model_name, 'test-vision')
