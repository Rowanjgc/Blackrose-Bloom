"""SM-07: citation audit phases, claim downgrades and answer/audit identity.

These tests pin the implicit state machine documented in
``docs/state-machines/sm-07-citation-audit.md``: the checkpoint statuses a
``CitationAudit`` may persist, the terminal statuses it returns, the latch on
revised answer text, the one-way claim downgrades applied by
``validate_assessment``/``display_report``, and the storage boundary that keeps
an audit bound to the bytes of its answer.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import tempfile
from pathlib import Path
import unittest

from blackrose.citations import (
    CitationAudit, EvidenceRegistry, answer_units, annotated_copy, audit_payload,
    digest, display_report, new_report, source_spans, unchecked_report, validate_assessment,
)
from blackrose.storage.db import Database
from blackrose.storage.records import MessageState
from blackrose.storage.repository import Repository

PASSAGE = 'The study enrolled 40 participants. No causal effect was established.\n\nCafé evidence.'
RESULT = {'results': [{'document_id': 'doc-a', 'chunk_id': 'chunk-a', 'title': 'Study',
                       'text': PASSAGE, 'citation': '[doc:doc-a, chunk:chunk-a]'}]}
MODEL = {'id': 'fixture', 'meta': {'n_ctx': 128000}}
KNOWLEDGE_OPEN = {'type': 'function', 'function': {
    'name': 'knowledge_open',
    'parameters': {'type': 'object', 'properties': {'document_id': {'type': 'string'}},
                   'required': ['document_id'], 'additionalProperties': False}}}
TRANSIENT = frozenset(('pending', 'checking', 'researching', 'revising'))
TERMINAL = frozenset(('complete', 'incomplete', 'not_applicable'))
STATUS_RANK = {'unchecked': 0, 'unsupported': 1, 'contradicted': 1,
               'partially_supported': 2, 'inference': 2, 'supported': 3}


def assessment(data, status='supported', research=(), claim_text=None):
    sources = data['sources']
    references = ([{'source_id': sources[0]['id'], 'span_ids': [p['id'] for p in sources[0]['spans']]}]
                  if sources else [])
    return {'source_based': True, 'research': list(research), 'units': [
        {'unit_id': unit['id'], 'claims': [{
            **({'claim_text': claim_text} if claim_text else {}),
            'status': status, 'references': deepcopy(references),
            'reason': 'Fixture assessment.', 'quality_issues': []}]}
        for unit in data['answer_units']]}


class Config:
    request_timeout = 5.0


class RecordingRepo:
    """Stands in for ``Repository.save_audit``; records every checkpoint."""

    def __init__(self):
        self.writes = []

    async def save_audit(self, message_id, report, *, content=None):
        self.writes.append((report['status'], report['body_hash'], content, deepcopy(report)))

    @property
    def statuses(self):
        return [write[0] for write in self.writes]


class ScriptedBackend:
    def __init__(self, steps):
        self.steps = iter(steps)

    async def stream(self, model, messages, tools, **options):
        step = next(self.steps)
        if callable(step):
            step = step(json.loads(messages[-1]['content']))
        if isinstance(step, BaseException):
            raise step
        yield {'delta': {'content': step if isinstance(step, str) else json.dumps(step)},
               'finish_reason': 'stop'}


class FixtureBuilder:
    model = MODEL

    def admit(self, prompt, tools):
        pass


class FixtureAssistant:
    id = 7


class FixtureChat:
    model_name = 'fixture'

    def __init__(self, steps, definitions=()):
        self.config = Config()
        self.backend = ScriptedBackend(steps)
        self._repo = RecordingRepo()
        self._definitions = list(definitions)
        self.tool_calls = []

    def tool_definitions(self, web):
        return self._definitions

    async def _call_tool(self, name, arguments, origin='assistant'):
        self.tool_calls.append((name, arguments, origin))
        return deepcopy(RESULT)


def audit_harness(steps, *, mode='balanced', attempted=True, sources=True, definitions=()):
    chat = FixtureChat(steps, definitions)
    registry = EvidenceRegistry(FixtureAssistant.id)
    if sources:
        registry.capture('knowledge_open', deepcopy(RESULT))
    registry.attempted = attempted or sources
    audit = CitationAudit(chat, FixtureAssistant(), FixtureBuilder(), {}, None, mode)
    audit.registry = registry
    return audit, chat


class AuditPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_balanced_sequence_is_checking_researching_revising_checking(self):
        research = [{'name': 'knowledge_open', 'arguments': {'document_id': 'doc-a'}}]
        audit, chat = audit_harness(
            [lambda data: assessment(data, 'unsupported', research=research),
             'The study enrolled 40 participants.',
             lambda data: assessment(data)],
            definitions=(KNOWLEDGE_OPEN,))
        audit.on_audit = lambda report: None
        text, report = await audit.run('Summarize the study.', '90 participants.')
        self.assertEqual(chat._repo.statuses, ['checking', 'researching', 'revising', 'checking'])
        self.assertEqual([call[0] for call in chat.tool_calls], ['knowledge_open'])
        self.assertEqual(text, 'The study enrolled 40 participants.')
        self.assertEqual((report['status'], report['coverage']), ('complete', {'supported': 1}))
        self.assertEqual(report['revision']['text'], '90 participants.')
        # Every checkpoint keeps its report bound to the draft it wrote.
        for status, body_hash, content, saved in chat._repo.writes:
            self.assertEqual(body_hash, digest(content))
            self.assertEqual(saved['body_hash'], digest(content))
            self.assertEqual(saved['status'], status)

    async def test_advisory_mode_never_enters_researching_or_revising(self):
        research = [{'name': 'knowledge_open', 'arguments': {'document_id': 'doc-a'}}]
        audit, chat = audit_harness(
            [lambda data: assessment(data, 'unsupported', research=research)],
            mode='advisory', definitions=(KNOWLEDGE_OPEN,))
        text, report = await audit.run('Summarize the study.', '90 participants.')
        self.assertEqual(chat._repo.statuses, ['checking'])
        self.assertEqual(chat.tool_calls, [])
        self.assertEqual(text, '90 participants.')
        self.assertEqual((report['status'], report['coverage']), ('complete', {'unsupported': 1}))
        self.assertNotIn('revision', report)

    async def test_run_returns_only_terminal_statuses(self):
        scripts = {
            'complete': [lambda data: assessment(data)],
            'malformed': ['{bad JSON'],
            'declined-but-attempted': [{'source_based': False}],
            'failed-final-assessment': [lambda data: assessment(data, 'unsupported'),
                                        'Revised answer.', '{bad JSON'],
        }
        for name, steps in scripts.items():
            with self.subTest(case=name):
                audit, _ = audit_harness(list(steps))
                text, report = await audit.run('Question.', 'A factual answer.')
                self.assertIn(report['status'], TERMINAL - {'not_applicable'})
                self.assertEqual(report['body_hash'], digest(text))

    async def test_audit_is_skipped_without_attempt_sources_or_leads(self):
        audit, chat = audit_harness([], mode='advisory', attempted=False, sources=False)
        self.assertEqual(await audit.run('Hello.', 'Hello there!'), ('Hello there!', None))
        self.assertEqual(chat._repo.writes, [])

    async def test_retrieval_attempt_forbids_not_applicable(self):
        # ``source_based=false`` cannot exempt a reply that already tried to retrieve.
        audit, _ = audit_harness([{'source_based': False}], mode='advisory')
        text, report = await audit.run('Summarize.', 'Hello there!')
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual([claim['status'] for claim in report['claims']], ['unchecked'])
        self.assertEqual(report['body_hash'], digest(text))

    async def test_inherited_evidence_alone_allows_a_declined_assessment(self):
        # Documented boundary: inheritance is not this reply's retrieval attempt.
        audit, _ = audit_harness([{'source_based': False}], mode='advisory',
                                 attempted=False, sources=False)
        audit.registry.inherit([{'id': 'm1s1', 'text': PASSAGE, 'sha256': digest(PASSAGE),
                                 'tool': 'knowledge_open', 'metadata': {},
                                 'spans': source_spans(PASSAGE)}])
        text, report = await audit.run('Hi.', 'Hello!')
        self.assertEqual(report['status'], 'not_applicable')
        self.assertEqual((report['claims'], report['coverage']), ([], {}))

    async def test_source_based_revision_cannot_become_unaudited(self):
        audit, _ = audit_harness([lambda data: assessment(data, 'unsupported'),
                                  'Revised answer.', {'source_based': False}],
                                 attempted=False, sources=False)
        audit.registry.inherit([{'id': 'm1s1', 'text': PASSAGE, 'sha256': digest(PASSAGE),
                                 'tool': 'knowledge_open', 'metadata': {},
                                 'spans': source_spans(PASSAGE)}])
        text, report = await audit.run('Summarize.', '90 participants.')
        self.assertEqual(text, 'Revised answer.')
        self.assertEqual(report['status'], 'incomplete')
        self.assertTrue(report['revision']['text'] == '90 participants.')
        self.assertTrue(any('cannot become unaudited' in note for note in report['limitations']))

    async def test_cancellation_mid_research_keeps_text_and_provisional_record(self):
        research = [{'name': 'knowledge_open', 'arguments': {'document_id': 'doc-a'}}]
        audit, chat = audit_harness(
            [lambda data: assessment(data, 'unsupported', research=research)],
            definitions=(KNOWLEDGE_OPEN,))

        async def cancelled(name, arguments, origin='assistant'):
            raise asyncio.CancelledError()

        chat._call_tool = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await audit.run('Summarize.', '90 participants.')
        report = audit.report
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['body_hash'], digest(audit.draft_text))
        self.assertEqual(report['revision']['text'], '90 participants.')
        self.assertEqual([claim['status'] for claim in report['claims']], ['unchecked'])
        # The last persisted status is transient; settlement or startup reaping
        # is what makes it terminal.
        self.assertIn(chat._repo.statuses[-1], TRANSIENT)

    async def test_timeout_mid_research_returns_an_incomplete_report(self):
        research = [{'name': 'knowledge_open', 'arguments': {'document_id': 'doc-a'}}]
        audit, chat = audit_harness(
            [lambda data: assessment(data, 'unsupported', research=research)],
            definitions=(KNOWLEDGE_OPEN,))
        chat.config.request_timeout = 0.01

        async def slow(name, arguments, origin='assistant'):
            await asyncio.sleep(0.5)

        chat._call_tool = slow
        text, report = await audit.run('Summarize.', '90 participants.')
        self.assertEqual(text, '90 participants.')
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['body_hash'], digest(text))
        self.assertEqual(report['revision']['text'], '90 participants.')

    async def test_revised_text_is_latched_when_the_final_assessment_fails(self):
        audit, _ = audit_harness([lambda data: assessment(data, 'unsupported'),
                                  'The study enrolled 40 participants.', '{bad JSON'])
        text, report = await audit.run('Summarize.', '90 participants.')
        self.assertEqual(text, 'The study enrolled 40 participants.')
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['body_hash'], digest(text))
        self.assertEqual(report['revision']['text'], '90 participants.')
        self.assertEqual(report['revision']['claims'][0]['status'], 'unsupported')

    async def test_empty_revision_cannot_replace_the_answer(self):
        audit, _ = audit_harness([lambda data: assessment(data, 'unsupported'), '   '])
        text, report = await audit.run('Summarize.', '90 participants.')
        self.assertEqual(text, '90 participants.')
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['body_hash'], digest(text))
        self.assertEqual(report['revision']['text'], '90 participants.')

    async def test_checkpoints_never_carry_the_provisional_revision(self):
        """Documents the crash window: only the in-memory report has ``revision``.

        Graceful cancellation and timeout attach it (see the two tests above);
        a checkpoint written at ``revising``/``checking`` does not, so startup
        reaping after a crash can only report unchecked claims.
        """
        research = [{'name': 'knowledge_open', 'arguments': {'document_id': 'doc-a'}}]
        audit, chat = audit_harness(
            [lambda data: assessment(data, 'unsupported', research=research),
             'The study enrolled 40 participants.',
             lambda data: assessment(data)],
            definitions=(KNOWLEDGE_OPEN,))
        text, report = await audit.run('Summarize.', '90 participants.')
        self.assertIn('revision', report)
        self.assertTrue(chat._repo.writes)
        self.assertFalse(any('revision' in saved for *_, saved in chat._repo.writes))


class ClaimDowngradeTests(unittest.TestCase):
    TEXT = 'The study enrolled 40 participants.'

    def report(self, payload, text=None, mode='balanced'):
        text = self.TEXT if text is None else text
        registry = EvidenceRegistry(7)
        registry.capture('knowledge_open', deepcopy(RESULT))
        return validate_assessment(payload, text, registry, mode, 'fixture'), registry

    def payload(self, status='supported', text=None):
        text = self.TEXT if text is None else text
        registry = EvidenceRegistry(7)
        registry.capture('knowledge_open', deepcopy(RESULT))
        return assessment({'sources': registry.sources, 'answer_units': answer_units(text)},
                          status), registry

    def test_claim_downgrades_are_monotonic_within_one_pass(self):
        def invalid_source(payload, registry):
            payload['units'][0]['claims'][0]['references'][0]['source_id'] = 'm99s1'

        def drop_references(payload, registry):
            payload['units'][0]['claims'][0]['references'] = []

        def extra_field(payload, registry):
            payload['units'][0]['claims'][0]['quote'] = 'Invented.'

        def blank_reason(payload, registry):
            payload['units'][0]['claims'][0]['reason'] = ' '

        def duplicate(payload, registry):
            payload['units'][0]['claims'] *= 2

        def overlap(payload, registry):
            payload['units'][0]['claims'].append(
                {**deepcopy(payload['units'][0]['claims'][0]), 'status': 'unsupported',
                 'claim_text': '40 participants and'})

        cases = {
            'untouched-supported': (None, 'supported', 'supported'),
            'invalid-source': (invalid_source, 'supported', 'unchecked'),
            'missing-reference': (drop_references, 'supported', 'unsupported'),
            'unsupported-field': (extra_field, 'supported', 'unchecked'),
            'blank-reason': (blank_reason, 'inference', 'unchecked'),
            'duplicate-single-selector': (duplicate, 'supported', 'unchecked'),
            'overlapping-selection': (overlap, 'supported', 'unchecked'),
        }
        for name, (mutate, declared, expected) in cases.items():
            with self.subTest(case=name):
                payload, registry = self.payload(declared)
                if mutate:
                    mutate(payload, registry)
                report, _ = self.report(payload)
                statuses = [claim['status'] for claim in report['claims']]
                self.assertTrue(statuses)
                for status in statuses:
                    self.assertLessEqual(STATUS_RANK[status], STATUS_RANK[declared])
                if name != 'overlapping-selection':
                    self.assertEqual(statuses, [expected])

    def test_invented_quotation_downgrades_support_to_unsupported(self):
        text = 'The paper states “This treatment cures everyone.”'
        payload, registry = self.payload('supported', text)
        report, _ = self.report(payload, text)
        self.assertEqual([claim['status'] for claim in report['claims']], ['unsupported'])
        self.assertIn('verbatim', report['claims'][0]['reason'])

    def test_contradicted_without_evidence_becomes_unchecked_not_supported(self):
        payload, _ = self.payload('contradicted')
        payload['units'][0]['claims'][0]['references'] = []
        report, _ = self.report(payload)
        self.assertEqual([claim['status'] for claim in report['claims']], ['unchecked'])

    def test_complete_status_never_coexists_with_an_unchecked_claim(self):
        payloads = []
        payloads.append(self.payload('supported')[0])
        payloads.append(self.payload('unsupported')[0])
        missing = self.payload('supported')[0]
        missing['units'] = []
        payloads.append(missing)
        gap = self.payload('supported')[0]
        gap['units'][0]['claims'][0]['claim_text'] = '40 participants'
        payloads.append(gap)
        for payload in payloads:
            with self.subTest(payload=json.dumps(payload['units'])[:80]):
                report, _ = self.report(payload)
                unchecked = any(claim['status'] == 'unchecked' for claim in report['claims'])
                self.assertEqual(report['status'] == 'complete', not unchecked)
                self.assertEqual(report['coverage'].get('unchecked', 0), sum(
                    claim['status'] == 'unchecked' for claim in report['claims']))

    def test_display_report_derives_status_instead_of_trusting_stored_labels(self):
        payload, _ = self.payload('supported')
        saved, _ = self.report(payload)
        saved['status'] = 'complete'
        saved['claims'][0]['status'] = 'unchecked'
        original = deepcopy(saved)
        self.assertEqual(display_report(self.TEXT, saved)['status'], 'incomplete')
        self.assertEqual(audit_payload(self.TEXT, saved)['status'], 'incomplete')
        self.assertEqual(saved, original)

    def test_display_report_collapses_overlap_to_unchecked(self):
        payload, _ = self.payload('supported')
        saved, _ = self.report(payload)
        saved['claims'].append({**deepcopy(saved['claims'][0]), 'id': 'c2', 'status': 'unsupported'})
        view = display_report(self.TEXT, saved)
        self.assertEqual([claim['status'] for claim in view['claims']], ['unchecked'])
        self.assertEqual(view['status'], 'incomplete')

    def test_body_hash_mismatch_fails_closed_on_every_read_path(self):
        payload, _ = self.payload('supported')
        saved, _ = self.report(payload)
        stale = deepcopy(saved)
        stale['body_hash'] = digest('A different answer.')
        self.assertEqual(display_report(self.TEXT, stale)['status'], 'incomplete')
        view = audit_payload(self.TEXT, stale)
        self.assertEqual(view['status'], 'incomplete')
        self.assertEqual({claim['status'] for claim in view['claims']}, {'unchecked'})
        self.assertIn('identity mismatch', annotated_copy(self.TEXT, stale))
        from blackrose.gui.render import audited_html
        self.assertNotIn('data-claim-id', audited_html(self.TEXT, stale))

    def test_unchecked_report_is_idempotent_and_preserves_provisional_history(self):
        registry = EvidenceRegistry(7)
        registry.capture('knowledge_open', deepcopy(RESULT))
        report = new_report('balanced', 'fixture', 'Draft answer.', registry)
        report['status'], report['attempted'] = 'complete', True
        report['revision'] = {'text': 'Draft answer.', 'body_hash': digest('Draft answer.'),
                              'claims': []}
        once = unchecked_report(report, 'Draft answer.', 'Audit interrupted before completion.')
        twice = unchecked_report(once, 'Draft answer.', 'Audit interrupted before completion.')
        self.assertEqual(once, twice)
        self.assertEqual(once['status'], 'incomplete')
        self.assertEqual(once['body_hash'], digest('Draft answer.'))
        self.assertEqual(once['revision'], report['revision'])
        self.assertEqual(once['sources'], report['sources'])
        self.assertEqual([claim['status'] for claim in once['claims']], ['unchecked'])


class ContradictionReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_can_only_keep_or_weaken_a_contradiction(self):
        mappings = {'insufficient': 'unsupported', 'unclear': 'unchecked',
                    'incompatible': 'contradicted'}
        for relation, expected in mappings.items():
            with self.subTest(relation=relation):
                # Advisory: the review runs, and no revision follows the verdict.
                audit, _ = audit_harness([
                    lambda data: assessment(data, 'contradicted'),
                    {'checks': [{'claim_id': 'c1', 'relation': relation,
                                 'reason': 'Fixture relation.'}]}], mode='advisory')
                _, report = await audit.run('Evaluate this claim.', 'Survival doubled.')
                self.assertEqual([claim['status'] for claim in report['claims']], [expected])
                self.assertEqual(report['status'] == 'complete', expected != 'unchecked')

    async def test_failed_review_leaves_the_claim_unchecked(self):
        for failure in ({'checks': []}, 'not json'):
            with self.subTest(failure=failure):
                audit, _ = audit_harness([lambda data: assessment(data, 'contradicted'), failure],
                                         mode='advisory')
                _, report = await audit.run('Evaluate this claim.', 'Survival doubled.')
                self.assertEqual([claim['status'] for claim in report['claims']], ['unchecked'])
                self.assertEqual(report['status'], 'incomplete')


class AuditStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / 'chats.db')
        await self.db.connect()
        self.repo = Repository(self.db)
        self.conversation = await self.repo.create_conversation()

    async def asyncTearDown(self):
        await self.db.close()
        self.temporary.cleanup()

    async def pending_reply(self, draft='Draft answer.'):
        _, reply = await self.repo.begin_turn(self.conversation.id, 'Question', 'fixture')
        registry = EvidenceRegistry(reply.id)
        registry.capture('knowledge_open', deepcopy(RESULT))
        report = new_report('balanced', 'fixture', draft, registry)
        report['status'] = 'checking'
        await self.repo.save_audit(reply.id, report, content=draft)
        return reply, report

    async def stored(self, reply):
        return next(message for message in await self.repo.messages(self.conversation.id)
                    if message.id == reply.id)

    async def test_save_audit_requires_a_pending_assistant_and_matching_bytes(self):
        user, reply = await self.repo.begin_turn(self.conversation.id, 'Question', 'fixture')
        registry = EvidenceRegistry(reply.id)
        original = new_report('balanced', 'fixture', 'Draft.', registry)
        await self.repo.save_audit(reply.id, original, content='Draft.')
        with self.assertRaises(ValueError):
            await self.repo.save_audit(user.id, new_report('balanced', 'fixture', 'Question', registry),
                                       content='Question')
        with self.assertRaises(ValueError):
            await self.repo.save_audit(reply.id, new_report('balanced', 'fixture', 'Other.', registry),
                                       content='Draft.')
        self.assertEqual((await self.stored(reply)).content, 'Draft.')
        self.assertEqual(await self.repo.audit_for(reply.id), original)
        await self.repo.finish_message(reply.id, 'Draft.', MessageState.COMPLETE)
        with self.assertRaises(ValueError):
            await self.repo.save_audit(reply.id, original, content='Draft.')

    async def test_finish_message_refuses_a_mismatched_audit_and_keeps_the_row_pending(self):
        reply, checkpoint = await self.pending_reply()
        with self.assertRaises(ValueError):
            await self.repo.finish_message(reply.id, 'A different answer.', MessageState.COMPLETE,
                                           audit=new_report('balanced', 'fixture', 'Draft answer.',
                                                            EvidenceRegistry(0)))
        message = await self.stored(reply)
        self.assertEqual((message.state, message.content), (MessageState.PENDING, 'Draft answer.'))
        self.assertEqual(await self.repo.audit_for(reply.id), checkpoint)
        final = new_report('balanced', 'fixture', 'Final answer.', EvidenceRegistry(0))
        final['status'] = 'incomplete'
        stored = await self.repo.finish_message(reply.id, 'Final answer.', MessageState.COMPLETE,
                                                audit=final)
        self.assertEqual(stored.audit, final)
        self.assertEqual(await self.repo.audit_for(reply.id), final)

    async def test_reap_converts_every_transient_checkpoint_to_incomplete(self):
        for transient in sorted(TRANSIENT):
            with self.subTest(status=transient):
                self.conversation = await self.repo.create_conversation(f'{transient} chat')
                reply, _ = await self.pending_reply()
                registry = EvidenceRegistry(reply.id)
                registry.capture('knowledge_open', deepcopy(RESULT))
                report = new_report('balanced', 'fixture', 'Draft answer.', registry)
                report['status'] = transient
                await self.repo.save_audit(reply.id, report, content='Draft answer.')
                self.assertEqual(await self.repo.reap_pending(), 1)
                reaped = await self.repo.audit_for(reply.id)
                self.assertEqual(reaped['status'], 'incomplete')
                self.assertEqual(reaped['body_hash'], digest('Draft answer.'))
                self.assertEqual(reaped['sources'], report['sources'])
                self.assertTrue(all(claim['status'] == 'unchecked' for claim in reaped['claims']))
                self.assertEqual((await self.stored(reply)).state, MessageState.ERROR)
                self.assertEqual(await self.repo.reap_pending(), 0)

    async def test_reap_keeps_a_checkpointed_provisional_revision(self):
        reply, _ = await self.pending_reply()
        registry = EvidenceRegistry(reply.id)
        report = new_report('balanced', 'fixture', 'Draft answer.', registry)
        report['status'] = 'checking'
        report['revision'] = {'text': 'Draft answer.', 'body_hash': digest('Draft answer.'),
                              'claims': []}
        await self.repo.save_audit(reply.id, report, content='Draft answer.')
        await self.repo.reap_pending()
        self.assertEqual((await self.repo.audit_for(reply.id))['revision'], report['revision'])

    async def test_reap_repairs_a_stale_body_hash_against_the_stored_answer(self):
        reply, report = await self.pending_reply()
        stale = {**report, 'body_hash': digest('Some earlier draft.')}
        await self.db.run(lambda conn: conn.execute(
            'UPDATE message_audits SET report=? WHERE message_id=?',
            (json.dumps(stale), reply.id)))
        self.assertEqual(await self.repo.reap_pending(), 1)
        reaped = await self.repo.audit_for(reply.id)
        self.assertEqual(reaped['body_hash'], digest((await self.stored(reply)).content))


if __name__ == '__main__':
    unittest.main()
