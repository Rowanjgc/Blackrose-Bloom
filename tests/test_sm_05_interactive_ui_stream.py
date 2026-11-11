"""SM-05: interactive UI stream parser, block lifecycle and widget state sequence.

Pins the transitions documented in
``docs/state-machines/sm-05-interactive-ui-stream.md``: the per-round ``UIStream``
buffer and prose latch, how a round's ``pending`` bytes are flushed, the persisted
``UIBlock`` status/revision lifecycle (including one-shot repair and startup
reaping), repository immutability of finalised source, and the monotonic
``ui_state.sequence`` across a repair and a redraw.

``*_documented_follow_up`` tests characterise behaviour the code does not guard
yet. They assert today's behaviour so the gap is visible in the suite; invert
them when the proposal in the specification lands.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from blackrose import turns, ui_documents
from blackrose.interactive_answers import InteractiveAnswers
from blackrose.storage.db import Database
from blackrose.storage.records import MessageState
from blackrose.storage.repository import Repository
from blackrose.turns import TurnRunner
from blackrose.ui_documents import LIBRARY, UIEnvelope, UIStream, apply_repair, contract

# A line that starts like a semantic block and fails the contract: it is created
# as an interactive block with status 'invalid' and no document (ui_documents.py:394-399).
BAD = '{"type":"form","title":"Incomplete"}'

_FORM: str | None = None


def form_source() -> str:
    """The contract's own valid form document as one NDJSON line."""
    global _FORM
    if _FORM is None:
        _FORM = json.dumps(contract()['examples']['form'], separators=(',', ':'))
    return _FORM


def parser(callback=None) -> UIStream:
    return UIStream(SimpleNamespace(id=7, conversation_id=3), callback)


class StreamParserTests(unittest.TestCase):
    """The per-round parser: markdown, line buffering, latched prose, round end."""

    def setUp(self):
        self.form = form_source()

    def test_prose_latch_survives_valid_ui_lines_until_the_next_round(self):
        stream = parser()
        stream.begin('mixed')
        stream.append('Plain prose.\n')
        self.assertTrue(stream.prose)
        # A syntactically perfect block after prose is prose, not a block.
        stream.append(self.form + '\n')
        stream.end()
        self.assertEqual([b['kind'] for b in stream.blocks], ['markdown'])
        self.assertEqual(stream.blocks[0]['source'], 'Plain prose.\n' + self.form + '\n')
        self.assertEqual(stream.blocks[0]['status'], 'ready')
        # end() never clears the latch; begin() does, once per round.
        self.assertTrue(stream.prose)
        stream.begin('mixed')
        self.assertFalse(stream.prose)
        stream.append(self.form + '\n')
        stream.end()
        self.assertEqual([b['kind'] for b in stream.blocks], ['markdown', 'interactive_ui'])
        self.assertEqual(stream.blocks[-1]['document'], contract()['examples']['form'])
        self.assertEqual(stream.blocks[-1]['status'], 'ready')

    def test_blank_lines_are_dropped_before_prose_and_preserved_inside_it(self):
        stream = parser()
        stream.begin('mixed')
        stream.append('\n\n')
        stream.end()
        self.assertEqual(stream.blocks, [])
        stream.begin('mixed')
        stream.append('One\n\nTwo\n')
        stream.end()
        self.assertEqual([(b['kind'], b['source']) for b in stream.blocks],
                         [('markdown', 'One\n\nTwo\n')])

    def test_round_end_flushes_pending_for_every_status(self):
        stream = parser()
        stream.begin('mixed')
        stream.append(self.form[:-2])           # truncated JSON, no newline
        stream.end()
        self.assertEqual([b['status'] for b in stream.blocks], ['invalid'])
        self.assertIn('Incomplete or invalid UI JSON', stream.blocks[0]['error'])
        self.assertEqual(stream.pending, '')
        stream.begin('mixed')
        stream.append(self.form)                # complete JSON, no newline
        stream.end()
        self.assertEqual(stream.blocks[-1]['status'], 'ready')
        stream.begin('mixed')
        stream.append('   ')                    # whitespace-only pending
        stream.end()
        self.assertEqual(len(stream.blocks), 2)
        self.assertEqual(stream.pending, '')

    def test_end_applies_the_abort_status_only_to_streaming_blocks(self):
        stream = parser()
        stream.begin('form')
        stream.append(self.form + '\n' + BAD + '\n')
        self.assertEqual([b['status'] for b in stream.blocks], ['streaming', 'invalid'])
        stream.end('cancelled')
        self.assertEqual([b['status'] for b in stream.blocks], ['cancelled', 'invalid'])
        # A second end() (interrupted settlement after a closed round) is a no-op.
        stream.end('invalid')
        self.assertEqual([b['status'] for b in stream.blocks], ['cancelled', 'invalid'])

    def test_error_settlement_labels_valid_blocks_invalid(self):
        """H4: 'invalid' also means 'the reply failed', not only 'bad source'."""
        stream = parser()
        stream.begin('form')
        stream.append(self.form + '\n')
        stream.end('invalid')
        block = stream.blocks[0]
        self.assertEqual(block['status'], 'invalid')
        self.assertEqual(block['document'], contract()['examples']['form'])
        self.assertNotIn('error', block)
        self.assertTrue(block['fallback_text'])

    def test_over_limit_final_block_fails_a_ready_round_and_is_dropped_on_abort(self):
        stream = parser()
        stream.begin('mixed')
        stream.append((self.form + '\n') * 12)
        stream.append(self.form)               # a thirteenth block is still pending
        with self.assertRaisesRegex(ValueError, '12 blocks'):
            stream.end()
        # The failed ready-end left the round open; interrupted settlement closes it.
        stream.end('invalid')
        self.assertEqual(len(stream.blocks), 12)
        self.assertTrue(all(b['status'] == 'invalid' for b in stream.blocks))
        self.assertEqual(stream.pending, '')
        self.assertIsNone(stream.profile)

        aborted = parser()
        aborted.begin('mixed')
        aborted.append((self.form + '\n') * 12 + self.form)
        aborted.end('cancelled')               # no raise on the abnormal path
        self.assertEqual(len(aborted.blocks), 12)
        self.assertTrue(all(b['status'] == 'cancelled' for b in aborted.blocks))

    def test_markdown_rounds_create_no_semantic_blocks_and_never_publish(self):
        published = []
        stream = parser(published.append)
        stream.begin()
        stream.append(self.form + '\n')
        stream.end()
        self.assertFalse(stream.used)
        self.assertEqual([b['kind'] for b in stream.blocks], ['markdown'])
        self.assertEqual(stream.blocks[0]['source'], self.form + '\n')
        self.assertEqual(published, [])
        stream.begin('form')
        stream.append(self.form + '\n')
        stream.end()
        self.assertTrue(stream.used)
        self.assertEqual([b['kind'] for b in published[-1]], ['markdown', 'interactive_ui'])

    def test_block_positions_are_creation_order_across_rounds(self):
        stream = parser()
        stream.begin()
        stream.append('Intro.\n')
        stream.end()
        stream.begin('mixed')
        stream.append(self.form + '\n' + 'Closing prose.\n')
        stream.end()
        stream.begin()
        stream.append('More.\n')
        stream.end()
        self.assertEqual([(b['position'], b['kind']) for b in stream.blocks],
                         [(0, 'markdown'), (1, 'interactive_ui'), (2, 'markdown'),
                          (3, 'markdown')])


def stored_block(assistant, *, block_id='block-0', kind='interactive_ui', status='streaming',
                 source=None, position=0, revision=1, sequence=0, document=None):
    return {'block_id': block_id, 'message_id': assistant.id,
            'conversation_id': assistant.conversation_id, 'position': position, 'kind': kind,
            'protocol_version': 1, 'library_version': LIBRARY, 'source_revision': revision,
            'source_sequence': sequence, 'source': source if source is not None else form_source(),
            'source_hash': '', 'status': status, 'fallback_text': '',
            'document': document, 'schema_hash': ''}


class _RepositoryCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / 'chats.db')
        await self.db.connect()
        self.repo = Repository(self.db)
        self.conversation = await self.repo.create_conversation()
        self.request, self.assistant = await self.repo.begin_turn(
            self.conversation.id, 'Build a form', 'test-model')

    async def asyncTearDown(self):
        await self.db.close()
        self.temporary.cleanup()

    async def one_round(self, text, profile='form'):
        """Stream one round through the real parser and persist it as a round end does."""
        stream = UIStream(self.assistant)
        stream.begin(profile)
        stream.append(text)
        stream.end()
        await self.repo.save_blocks(self.assistant.id, stream.blocks)
        return stream

    async def stored_blocks(self):
        message = await self.repo.message(self.conversation.id, self.assistant.id)
        return message.blocks


class BlockLifecycleTests(_RepositoryCase):
    """The persisted UIBlock lifecycle: streaming, finalisation, repair, reaping."""

    async def test_finalized_source_is_immutable_including_the_source_sequence(self):
        block = stored_block(self.assistant, sequence=1)
        await self.repo.save_blocks(self.assistant.id, [block])
        block['source'] += ' '
        block['source_sequence'] = 2
        await self.repo.save_blocks(self.assistant.id, [block])       # still streaming
        block['status'] = 'ready'
        block['source_sequence'] = 3
        await self.repo.save_blocks(self.assistant.id, [block])       # finalise
        await self.repo.save_blocks(self.assistant.id, [dict(block)])  # idempotent re-save
        for change, error in (
                ({'source': 'tampered'}, 'Finalized UI source is immutable'),
                ({'source_sequence': 4}, 'Finalized UI source is immutable'),
                ({'fallback_text': 'rewritten'}, 'Finalized UI source is immutable'),
                ({'status': 'streaming'}, 'Finalized UI source is immutable'),
                ({'source_revision': 2}, 'UI block identity is immutable'),
                ({'position': 1}, 'UI block identity is immutable')):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, error):
                    await self.repo.save_blocks(self.assistant.id, [dict(block) | change])
        with self.assertRaisesRegex(ValueError, 'Stale UI source sequence'):
            await self.repo.save_blocks(self.assistant.id, [dict(block) | {'source_sequence': 2}])
        saved = await self.repo.ui_block(self.conversation.id, self.assistant.id, block['block_id'])
        self.assertEqual((saved['source'], saved['source_sequence'], saved['status']),
                         (block['source'], 3, 'ready'))

    async def test_save_blocks_is_atomic_and_checks_ownership(self):
        finalized = stored_block(self.assistant, block_id='a', status='ready', sequence=2)
        await self.repo.save_blocks(self.assistant.id, [finalized])
        pending = stored_block(self.assistant, block_id='b', position=1)
        with self.assertRaisesRegex(ValueError, 'Finalized UI source is immutable'):
            await self.repo.save_blocks(self.assistant.id,
                                        [pending, finalized | {'source': 'tampered'}])
        self.assertEqual([b['block_id'] for b in await self.stored_blocks()], ['a'])
        with self.assertRaisesRegex(ValueError, 'UI ownership mismatch'):
            await self.repo.save_blocks(self.assistant.id,
                                        [pending | {'conversation_id': 999}])
        with self.assertRaisesRegex(ValueError, 'UI owner must be an assistant message'):
            await self.repo.save_blocks(self.request.id, [pending | {'message_id': self.request.id}])
        await self.repo.save_blocks(self.assistant.id, [pending])
        self.assertEqual([(b['block_id'], b['position']) for b in await self.stored_blocks()],
                         [('a', 0), ('b', 1)])

    async def test_repair_is_one_shot_from_invalid_only(self):
        stream = await self.one_round(BAD + '\n')
        block = stream.blocks[0]
        self.assertEqual(block['status'], 'invalid')
        self.assertIsNone(block['document'])
        candidate = dict(block)
        apply_repair(candidate, form_source(), 'form')
        self.assertEqual((candidate['status'], candidate['error'],
                          candidate['source_revision'], candidate['source_sequence']),
                         ('ready', None, 2, 1))
        await self.repo.repair_block(self.assistant.id, candidate)
        saved = await self.repo.ui_block(self.conversation.id, self.assistant.id, block['block_id'])
        self.assertEqual((saved['status'], saved['source_revision'], saved['source_sequence']),
                         ('ready', 2, 1))
        self.assertEqual(saved['document'], contract()['examples']['form'])
        # Both layers refuse the second repair: staging by status, storage by identity.
        again = dict(candidate)
        with self.assertRaisesRegex(ValueError, 'Only an invalid UI block can be repaired'):
            apply_repair(again, form_source(), 'form')
        with self.assertRaisesRegex(ValueError, 'Only an invalid UI block can be repaired'):
            await self.repo.repair_block(self.assistant.id, again)

    async def test_repair_guards_revision_identity_and_cancelled_blocks(self):
        stream = await self.one_round(BAD + '\n')
        block = dict(stream.blocks[0])
        repaired = dict(block)
        apply_repair(repaired, form_source(), 'form')
        with self.assertRaisesRegex(ValueError, 'advance the source revision once'):
            await self.repo.repair_block(self.assistant.id, repaired | {'source_revision': 3})
        with self.assertRaisesRegex(ValueError, 'Only an invalid UI block'):
            await self.repo.repair_block(self.assistant.id, repaired | {'status': 'cancelled'})
        with self.assertRaisesRegex(ValueError, 'identity is immutable'):
            await self.repo.repair_block(self.assistant.id,
                                          repaired | {'position': block['position'] + 1})
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            await self.repo.repair_block(self.assistant.id, repaired | {'block_id': 'missing'})
        cancelled = stored_block(self.assistant, block_id='cancelled', position=1,
                                 status='cancelled', sequence=1)
        await self.repo.save_blocks(self.assistant.id, [cancelled])
        with self.assertRaisesRegex(ValueError, 'Only an invalid UI block'):
            await self.repo.repair_block(self.assistant.id,
                                          cancelled | {'status': 'ready', 'source_revision': 2})
        self.assertEqual([b['status'] for b in await self.stored_blocks()],
                         ['invalid', 'cancelled'])

    async def test_reap_cancels_streaming_blocks_once_and_leaves_terminal_blocks_alone(self):
        live = stored_block(self.assistant, block_id='live', sequence=2)
        done = stored_block(self.assistant, block_id='done', position=1, status='ready', sequence=1)
        bad = stored_block(self.assistant, block_id='bad', position=2, status='invalid', sequence=1)
        await self.repo.save_blocks(self.assistant.id, [live, done, bad])
        self.assertEqual(await self.repo.reap_pending(), 1)
        blocks = await self.stored_blocks()
        self.assertEqual({b['block_id']: b['status'] for b in blocks},
                         {'live': 'cancelled', 'done': 'ready', 'bad': 'invalid'})
        self.assertEqual((blocks[0]['source'], blocks[0]['source_sequence']), (form_source(), 2))
        self.assertEqual(await self.repo.reap_pending(), 0)
        self.assertEqual((await self.stored_blocks())[0]['status'], 'cancelled')
        self.assertEqual((await self.repo.message(self.conversation.id, self.assistant.id)).state,
                         MessageState.ERROR)

    async def test_ui_state_sequence_is_monotonic_across_repair_and_redraw(self):
        stream = await self.one_round(BAD + '\n')
        block_id = stream.blocks[0]['block_id']
        # Storage accepts state for any block; the policy layer is what refuses it for invalid.
        await self.repo.save_ui_state(block_id, 1, {'days': 3})
        candidate = dict(stream.blocks[0])
        apply_repair(candidate, form_source(), 'form')
        await self.repo.repair_block(self.assistant.id, candidate)
        for sequence in (0, 1, 3):
            with self.subTest(sequence=sequence), self.assertRaisesRegex(
                    ValueError, 'Stale UI state sequence'):
                await self.repo.save_ui_state(block_id, sequence, {'days': 4})
        self.assertEqual(await self.repo.save_ui_state(block_id, 2, {'days': 4}), 2)
        reopened = await self.repo.ui_block(self.conversation.id, self.assistant.id, block_id)
        self.assertEqual((reopened['state_sequence'], reopened['saved_state']), (2, {'days': 4}))
        self.assertEqual(reopened['source_revision'], 2)
        redrawn = (await self.stored_blocks())[0]
        self.assertEqual((redrawn['state_sequence'], redrawn['saved_state']), (2, {'days': 4}))
        self.assertEqual(await self.repo.save_ui_state(block_id, 3, {'days': 5}), 3)

    async def test_ui_state_cannot_orphan_a_missing_block(self):
        with self.assertRaises(sqlite3.IntegrityError):
            await self.repo.save_ui_state('missing-block', 1, {})

    async def test_the_repository_read_shape_is_not_accepted_again_documented_follow_up(self):
        """ui_block/_attach_batch add saved_state/state_sequence that save_blocks then rejects."""
        stream = await self.one_round(form_source() + '\n')
        block_id = stream.blocks[0]['block_id']
        await self.repo.save_ui_state(block_id, 1, {'days': 3})
        reopened = await self.repo.ui_block(self.conversation.id, self.assistant.id, block_id)
        self.assertEqual((reopened['state_sequence'], reopened['saved_state']), (1, {'days': 3}))
        with self.assertRaisesRegex(ValueError, 'Finalized UI source is immutable'):
            await self.repo.save_blocks(self.assistant.id, [reopened])

    async def test_invalid_blocks_are_refused_by_the_answers_policy_until_repaired(self):
        if not ui_documents.available():
            self.skipTest('interactive UI runtime bundle is not built')
        stream = await self.one_round(BAD + '\n')
        block = stream.blocks[0]
        answers = InteractiveAnswers(self.repo, SimpleNamespace(interactive_ui_actions_enabled=True))
        envelope = UIEnvelope(self.conversation.id, self.assistant.id, block['block_id'], 1, 'view')
        with self.assertRaisesRegex(ValueError, 'UI document is incomplete'):
            await answers.save_state(await self.repo.ui_block(
                self.conversation.id, self.assistant.id, block['block_id']),
                {'state_sequence': 1, 'values': {'days': 3}})
        with self.assertRaisesRegex(ValueError, 'incomplete, stale or unsupported'):
            await answers.owned_block(envelope, self.conversation.id, for_action=True)
        candidate = dict(block)
        apply_repair(candidate, form_source(), 'form')
        await self.repo.repair_block(self.assistant.id, candidate)
        repaired = await self.repo.ui_block(self.conversation.id, self.assistant.id, block['block_id'])
        self.assertEqual(repaired['state_sequence'], 0)
        saved = await answers.save_state(repaired, {'state_sequence': 1, 'values': {'days': 3}})
        self.assertEqual((saved.sequence, saved.values), (1, {'days': 3}))


class RepairInterruptionTests(_RepositoryCase):
    """Stop during the one-shot repair: the two cancellation windows differ."""

    def runner(self, repairs=1):
        chat = SimpleNamespace(_repo=self.repo,
                               config=SimpleNamespace(interactive_ui_repairs=repairs))
        runner = TurnRunner(chat)
        runner.assistant = self.assistant
        return runner

    async def test_stop_after_the_repair_commit_still_settles_the_reply(self):
        """Database._worker finishes the write, then raises CancelledError (db.py:20-35).

        H1 (fixed): memory follows the attempted commit, so the in-memory block
        agrees with the row ('ready' revision 2) and the interrupted settlement
        reaches finish_message instead of being refused as an immutable-identity
        change.  The reply settles CANCELLED rather than staying pending.
        """
        stream = await self.one_round(BAD + '\n')
        runner = self.runner()
        runner.ui_stream = stream
        original = self.repo.repair_block

        async def commit_then_cancel(message_id, block):
            await original(message_id, block)
            raise asyncio.CancelledError

        with patch.object(self.repo, 'repair_block', side_effect=commit_then_cancel), \
                patch.object(turns, 'repair_text', AsyncMock(return_value=form_source())):
            with self.assertRaises(asyncio.CancelledError):
                await runner._repair_ui(stream, self.assistant, 'form', None, None, None)
        committed = (await self.stored_blocks())[0]
        self.assertEqual((committed['status'], committed['source_revision']), ('ready', 2))
        self.assertEqual((stream.blocks[0]['status'], stream.blocks[0]['source_revision']),
                         ('ready', 2))
        await runner._save_interrupted(MessageState.CANCELLED, 'Stopped', 'off')
        self.assertEqual((await self.repo.message(self.conversation.id, self.assistant.id)).state,
                         MessageState.CANCELLED)

    async def test_stop_during_the_repair_request_commits_nothing_and_settles(self):
        stream = UIStream(self.assistant)
        stream.begin('form')
        stream.append(BAD + '\n' + form_source() + '\n')
        # Persist the round mid-flight, as the streaming save does (turns.py:643-644).
        await self.repo.save_blocks(self.assistant.id, stream.blocks)
        self.assertEqual([b['status'] for b in await self.stored_blocks()],
                         ['invalid', 'streaming'])
        runner = self.runner()
        runner.ui_stream = stream
        with patch.object(turns, 'repair_text', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await runner._repair_ui(stream, self.assistant, 'form', None, None, None)
        self.assertEqual([b['status'] for b in await self.stored_blocks()],
                         ['invalid', 'streaming'])
        # The other cancellation window settles cleanly.
        message = await runner._save_interrupted(MessageState.CANCELLED, 'Stopped', 'off')
        self.assertIsNone(message)
        stored = await self.repo.message(self.conversation.id, self.assistant.id)
        self.assertEqual(stored.state, MessageState.CANCELLED)
        self.assertEqual([b['status'] for b in stored.blocks], ['invalid', 'cancelled'])

    async def test_repair_budget_targets_only_invalid_interactive_blocks(self):
        stream = await self.one_round(form_source() + '\n' + BAD + '\n' + BAD + '\n')
        runner = self.runner(repairs=1)
        prompts = []

        async def fake_repair(chat, messages, options, builder, stream_budget):
            prompts.append(messages[1]['content'])
            return form_source()

        with patch.object(turns, 'repair_text', fake_repair):
            await runner._repair_ui(stream, self.assistant, 'form', None, None, None)
        self.assertEqual(len(prompts), 1)
        self.assertIn('Validation error', prompts[0])
        self.assertIn(BAD, prompts[0])
        self.assertEqual([b['status'] for b in stream.blocks], ['ready', 'ready', 'invalid'])
        self.assertEqual([b['source_revision'] for b in stream.blocks], [1, 2, 1])
        self.assertEqual([b['position'] for b in stream.blocks], [0, 1, 2])
        self.assertEqual([b['status'] for b in await self.stored_blocks()],
                         ['ready', 'ready', 'invalid'])


if __name__ == '__main__':
    unittest.main()
