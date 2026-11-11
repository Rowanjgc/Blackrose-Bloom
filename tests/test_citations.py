"""Citation integrity and full-turn behavior with offline, adversarial providers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from blackrose.citations import (
    EvidenceRegistry, answer_units, audit_payload, digest, new_report,
    supporting_passages, uncovered_units, validate_assessment,
)
from blackrose.config import Config, load_config, save_citation_rigor
from blackrose.gui.render import audited_html
import test_gui_images as gui
from test_turns import block_final_save, stop_during_final_save

PASSAGE = 'The study enrolled 40 participants. No causal effect was established.\n\nCafé 🐈 evidence.'
RESULT = {'results': [{'document_id': 'doc-a', 'chunk_id': 'chunk-a', 'title': 'Study',
                       'text': PASSAGE, 'citation': '[doc:doc-a, chunk:chunk-a]',
                       'revision': 'revision-a', 'source_metadata': {'project': 'Trial A'}}]}


def assessment(data, status='supported', research=()):
    sources = data['sources']
    references = [{'source_id': sources[0]['id'], 'span_ids': [p['id'] for p in sources[0]['spans']]}] if sources else []
    return {'source_based': True, 'research': list(research), 'units': [
        {'unit_id': u['id'], 'claims': [{'status': status,
         'references': deepcopy(references), 'reason': 'Fixture assessment.', 'quality_issues': []}]}
        for u in data['answer_units']]}


class EvidenceTests(unittest.TestCase):
    def registry(self):
        registry = EvidenceRegistry(12)
        registry.capture('knowledge_open', deepcopy(RESULT))
        return registry

    def payload(self, text='The study enrolled 40 participants.'):
        registry = self.registry()
        return text, registry, assessment({'sources': registry.sources, 'answer_units': answer_units(text)})

    def test_supporting_text_is_a_direct_slice_of_the_captured_source(self):
        text, registry, payload = self.payload()
        raw = registry.sources[0]['text']
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        saved = supporting_passages(report, report['claims'][0])
        self.assertEqual(''.join(saved[0]['excerpts']), raw)
        RESULT_COPY = deepcopy(RESULT)
        registry.capture('knowledge_open', RESULT_COPY)
        RESULT_COPY['results'][0]['text'] = 'A changed source.'
        self.assertEqual(registry.sources[0]['text'], PASSAGE)
        self.assertEqual(registry.sources[0]['sha256'], digest(PASSAGE))

    def test_model_supplied_quotation_or_metadata_cannot_be_saved_as_evidence(self):
        for key in ('quote', 'text', 'excerpt', 'title', 'url', 'metadata'):
            with self.subTest(key=key):
                text, registry, payload = self.payload()
                payload['units'][0]['claims'][0]['references'][0][key] = 'Invented evidence.'
                report = validate_assessment(payload, text, registry, 'strict', 'fixture')
                self.assertEqual(report['claims'][0]['status'], 'unchecked')
                self.assertEqual(report['claims'][0]['references'], [])
                self.assertNotIn('Invented evidence.', json.dumps(report))

    def test_fabricated_source_span_and_cross_message_ids_fail_closed(self):
        for changes in ({'source_id': 'm99s1'}, {'span_ids': ['p999']}, {'span_ids': ['p1', 'p1']}):
            text, registry, payload = self.payload()
            payload['units'][0]['claims'][0]['references'][0].update(changes)
            report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
            self.assertEqual(report['claims'][0]['status'], 'unchecked')

    def test_invented_direct_quote_is_unsupported_even_with_a_real_citation(self):
        text, registry, payload = self.payload('The paper states “This treatment cures everyone.”')
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        self.assertEqual(report['claims'][0]['status'], 'unsupported')
        self.assertIn('verbatim', report['claims'][0]['reason'])

    def test_search_snippets_and_failed_results_never_become_support(self):
        registry = EvidenceRegistry(1)
        registry.capture('web_search', {'results': [{'url': 'https://example.org', 'text': 'A fake snippet.'}]})
        registry.capture('fetch_content', {'error': 'Unavailable', 'text': 'Must not be used'})
        self.assertEqual(registry.sources, [])
        self.assertTrue(registry.attempted)
        text = 'A factual assertion.'
        report = validate_assessment(assessment({'sources': [], 'answer_units': answer_units(text)}),
                                     text, registry, 'advisory', 'fixture')
        self.assertEqual(report['claims'][0]['status'], 'unsupported')

    def test_integrity_hash_and_boundaries_are_checked_before_display_or_reuse(self):
        text, registry, payload = self.payload()
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        report['sources'][0]['text'] = 'Tampered saved text.'
        rendered = audit_payload(text, report)
        self.assertEqual(rendered['status'], 'incomplete')
        self.assertEqual(rendered['claims'][0]['passages'], [])
        self.assertNotIn('Tampered saved text.', rendered['copy_text'])

    def test_unicode_repeated_passages_are_selected_by_position(self):
        registry = EvidenceRegistry(1)
        registry.capture('fetch_content', {'url': 'https://example.org', 'text': 'Café 🐈. Café 🐈.\nLast.'})
        source = registry.sources[0]
        self.assertEqual(registry.excerpt({'source_id': source['id'], 'span_ids': ['p2']}), ['Café 🐈.\n'])

    def test_missing_claim_coverage_becomes_unchecked(self):
        text, registry, payload = self.payload('First fact.\nSecond fact.')
        payload['units'] = payload['units'][:1]
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['coverage']['unchecked'], 1)

    def test_uncovered_units_detects_absent_and_empty_entries(self):
        text = 'First fact. Second fact. Third fact.'
        ids = [unit['id'] for unit in answer_units(text)]
        payload = {'units': [
            {'unit_id': ids[0], 'claims': [{'claim_text': 'First fact.'}], 'exclusions': []},
            {'unit_id': ids[1], 'claims': [], 'exclusions': []}]}
        self.assertEqual([unit['id'] for unit in uncovered_units(payload, text)], [ids[1], ids[2]])
        self.assertEqual(uncovered_units({'units': 'invalid'}, text), [])
        self.assertEqual(uncovered_units(None, text), [])

    def test_strict_cannot_exclude_factual_background_as_out_of_scope(self):
        text, registry, payload = self.payload()
        payload['units'][0] = {'unit_id': 'u1', 'claims': [], 'exclusions': [
            {'kind': 'out_of_scope', 'reason': 'Background.'}]}
        self.assertEqual(validate_assessment(payload, text, registry, 'advisory', 'fixture')['status'], 'complete')
        # Strict rejects the exclusion but keeps going: the text stays an unchecked
        # gap instead of raising away every verdict in the batch.
        strict = validate_assessment(payload, text, registry, 'strict', 'fixture')
        self.assertEqual(strict['status'], 'incomplete')
        self.assertEqual(strict['claims'][0]['status'], 'unchecked')
        self.assertTrue(any('exclusion' in note for note in strict['limitations']))

    def test_seeded_integrity_evaluation_never_endorses_fabricated_support(self):
        path = Path(__file__).resolve().parents[1]
        suite = json.loads((path / 'tests/fixtures/citation-evaluation.json').read_text())
        spec = importlib.util.spec_from_file_location('evaluate_citations', path / 'scripts/evaluate_citations.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        results = module.host_gate(suite['cases'])
        self.assertEqual(len(results), 5)
        self.assertTrue(all(row['passed'] for row in results))

    def test_model_html_remains_escaped_when_host_controls_are_inserted(self):
        text, registry, payload = self.payload('<script>alert(1)</script> A statement.')
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        html = audited_html(text, report)
        self.assertNotIn('<script>', html)
        self.assertIn('data-claim-id="c1"', html)
        self.assertNotIn('data-claim-id', audited_html(text + ' changed', report))

    def test_contiguous_quote_spans_join_but_omitted_text_is_not_invented(self):
        text, registry, payload = self.payload('The paper states “The study enrolled 40 participants. No causal effect was established.”')
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        self.assertEqual(report['claims'][0]['status'], 'supported')
        registry = EvidenceRegistry(1)
        registry.capture('fetch_content', {'url': 'https://example.org', 'text': 'First. Omitted. Last.'})
        source = registry.sources[0]
        self.assertEqual(registry.excerpt({'source_id': source['id'], 'span_ids': ['p1', 'p3']}), ['First. ', 'Last.'])

    def test_explicit_quote_omissions_cannot_invent_reorder_or_stitch_sources(self):
        from blackrose.citations import quotation_matches
        registry = EvidenceRegistry(1)
        registry.capture('fetch_content', {'url': 'https://example.org', 'text': 'First. Omitted. Last.'})
        ref = {'source_id': registry.sources[0]['id'], 'span_ids': ['p1', 'p3']}
        self.assertTrue(quotation_matches('First. […] Last.', registry, [ref]))
        self.assertFalse(quotation_matches('Last. […] First.', registry, [ref]))
        self.assertFalse(quotation_matches('First. […] Invented.', registry, [ref]))
        registry.capture('fetch_content', {'url': 'https://another.example.org', 'text': 'Invented.'})
        other = {'source_id': registry.sources[1]['id'], 'span_ids': ['p1']}
        self.assertFalse(quotation_matches('First. […] Invented.', registry, [ref, other]))

    def test_invented_single_quoted_text_is_flagged(self):
        for quote in ("'A fictional quotation.'", '‘A fictional quotation.’'):
            text, registry, payload = self.payload('The paper says ' + quote)
            report = validate_assessment(payload, text, registry, 'strict', 'fixture')
            self.assertEqual(report['claims'][0]['status'], 'unsupported')

    def test_same_text_with_changed_provenance_gets_a_distinct_snapshot(self):
        registry = self.registry()
        changed = deepcopy(RESULT)
        changed['results'][0]['source_metadata']['version'] = '2.0'
        registry.capture('knowledge_open', changed)
        self.assertEqual(len(registry.sources), 2)
        self.assertNotEqual(registry.sources[0]['id'], registry.sources[1]['id'])

    def test_claim_positions_are_derived_from_exact_draft_text(self):
        text, registry, payload = self.payload('The study enrolled 40 participants; survival was not measured.')
        payload['units'][0]['claims'][0]['claim_text'] = 'survival was not measured'
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        claim = report['claims'][0]
        self.assertEqual(text[claim['start']:claim['end']], 'survival was not measured')
        payload['units'][0]['claims'][0]['claim_text'] = 'Invented draft text'
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        self.assertTrue(all(c['status'] == 'unchecked' for c in report['claims']))
        payload['units'][0]['claims'][0].pop('claim_text')
        payload['units'][0]['claims'][0]['end'] = 10
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        self.assertTrue(all(c['status'] == 'unchecked' for c in report['claims']))

    def test_one_invalid_item_does_not_discard_other_verdicts(self):
        text, registry, payload = self.payload('The study enrolled 40 participants and doubled survival.')
        source = registry.sources[0]
        payload['units'][0]['claims'] = [
            {'claim_text': 'The study enrolled 40 participants', 'status': 'supported',
             'references': [{'source_id': source['id'], 'span_ids': [source['spans'][0]['id']]}],
             'reason': 'Fixture assessment.', 'quality_issues': []},
            {'claim_text': 'and doubled survival.', 'status': 'not-a-status',
             'references': [], 'reason': 'Fixture assessment.', 'quality_issues': []}]
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        statuses = {text[c['start']:c['end']]: c['status'] for c in report['claims']}
        self.assertEqual(statuses['The study enrolled 40 participants'], 'supported')
        self.assertEqual(statuses['and doubled survival.'], 'unchecked')

    def test_nested_list_facts_are_audited_while_code_is_excluded(self):
        text = '- Parent fact\n    - Nested factual claim\n\n```python\nprint("code")\n```\n'
        units = answer_units(text)
        self.assertTrue(any('Nested factual claim' in u['text'] for u in units))
        self.assertFalse(any('print' in u['text'] for u in units))

    def test_sentence_units_keep_offsets_quotes_versions_and_abbreviations(self):
        text = 'Dr. Lin tested version 2.2. The report says “First. Second.” Both tests finished.'
        units = answer_units(text)
        self.assertEqual([u['text'] for u in units], [
            'Dr. Lin tested version 2.2.', 'The report says “First. Second.”', 'Both tests finished.'])
        for unit in units:
            self.assertEqual(text[unit['start']:unit['start'] + len(unit['text'])], unit['text'])

    def test_partial_and_generated_sources_always_expose_quality_caveats(self):
        text, registry, payload = self.payload()
        registry.sources[0]['metadata'].update(text_truncated=True, source_metadata={'generator_model': 'fixture-model'})
        report = validate_assessment(payload, text, registry, 'strict', 'fixture')
        self.assertEqual(len(report['claims'][0]['quality_issues']), 2)

    def test_compound_claim_has_separate_verdicts_and_exact_passages(self):
        text = 'The trial enrolled 40 participants and doubled survival.'
        registry = EvidenceRegistry(1)
        registry.capture('knowledge_open', {'results': [{'text':
            'The trial enrolled 40 participants. Survival was not measured.'}]})
        claims = [{'claim_text': phrase, 'status': status, 'reason': 'Fixture relation.',
                   'references': [{'source_id': 'm1s1', 'span_ids': [span]}]}
                  for phrase, status, span in (
                      ('The trial enrolled 40 participants', 'supported', 'p1'),
                      ('and doubled survival.', 'unsupported', 'p2'))]
        report = validate_assessment({'source_based': True, 'units': [
            {'unit_id': 'u1', 'claims': claims}]}, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['status'], 'complete')
        self.assertEqual([c['status'] for c in report['claims']], ['supported', 'unsupported'])
        self.assertNotIn('doubled', audit_payload(text, report)['claims'][0]['text'])
        self.assertEqual(supporting_passages(report, report['claims'][1])[0]['excerpts'],
                         ['Survival was not measured.'])

    def test_overlapping_verdicts_never_leave_a_supported_badge(self):
        text, registry, payload = self.payload('The study enrolled 40 participants and doubled survival.')
        raw = payload['units'][0]['claims'][0]
        raw['claim_text'] = text
        payload['units'][0]['claims'].append({**deepcopy(raw), 'status': 'unsupported',
                                            'claim_text': 'and doubled survival.'})
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(len(report['claims']), 1)
        self.assertEqual(report['claims'][0]['status'], 'unchecked')
        self.assertEqual(report['claims'][0]['references'], [])
        self.assertIn('Overlapping', report['claims'][0]['reason'])

    def test_multiple_claims_cannot_default_to_the_whole_unit(self):
        text, registry, payload = self.payload()
        payload['units'][0]['claims'] *= 2
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['claims'][0]['status'], 'unchecked')
        self.assertEqual(report['status'], 'incomplete')

    def test_identical_explicit_claims_collapse_to_one_badge(self):
        text, registry, payload = self.payload()
        payload['units'][0]['claims'][0]['claim_text'] = text
        payload['units'][0]['claims'] *= 2
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(len(report['claims']), 1)
        self.assertEqual(report['coverage'], {'supported': 1})

    def test_excluded_and_supported_text_becomes_unchecked(self):
        text, registry, payload = self.payload()
        payload['units'][0]['exclusions'] = [{'kind': 'nonfactual', 'reason': 'Incorrect exclusion.'}]
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['claims'][0]['status'], 'unchecked')
        self.assertEqual(report['exclusions'], [])
        self.assertEqual(report['coverage'], {'unchecked': 1})

    def test_unclassified_gaps_do_not_swallow_a_valid_claim_between_them(self):
        text, registry, payload = self.payload('Unknown prefix; 40 participants; unknown suffix.')
        payload['units'][0]['claims'][0]['claim_text'] = '40 participants'
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        view = audit_payload(text, report)
        self.assertEqual(sum(c['status'] == 'supported' for c in view['claims']), 1)
        self.assertEqual(sum(c['status'] == 'unchecked' for c in view['claims']), 2)

    def test_only_and_between_assessed_clauses_is_automatic_scaffolding(self):
        for connector in ('and', 'but', 'not', 'therefore'):
            text, registry, payload = self.payload(f'40 participants {connector} survival doubled')
            first = payload['units'][0]['claims'][0]
            first['claim_text'] = '40 participants'
            payload['units'][0]['claims'].append({**deepcopy(first), 'claim_text': 'survival doubled',
                                                'status': 'unsupported'})
            report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
            if connector in ('and', 'but'):
                self.assertEqual(report['status'], 'complete')
                self.assertEqual(report['exclusions'][0]['kind'], 'nonfactual')
            else:
                self.assertEqual(report['status'], 'incomplete')
                self.assertTrue(any(c['status'] == 'unchecked' for c in report['claims']))

    def test_reference_marker_gaps_are_not_factual_claims(self):
        text, registry, payload = self.payload('40 participants [1][2] doubled survival.')
        payload['units'][0]['claims'][0]['claim_text'] = '40 participants'
        payload['units'][0]['claims'].append({**deepcopy(payload['units'][0]['claims'][0]),
                                              'claim_text': 'doubled survival'})
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertTrue(any(e['kind'] == 'nonfactual' for e in report['exclusions']))
        self.assertFalse(any('1][2' in text[c['start']:c['end']] for c in report['claims']))

    def test_two_source_quotations_use_their_own_claim_evidence(self):
        first, second = 'The study enrolled 40 participants.', 'No causal effect was established.'
        text, registry, payload = self.payload(f'“{first}”; “{second}”')
        payload['units'][0]['claims'] = [
            {'claim_text': f'“{quote}”', 'status': 'supported', 'reason': 'Exact source quotation.',
             'references': [{'source_id': registry.sources[0]['id'], 'span_ids': [span]}]}
            for quote, span in ((first, 'p1'), (second, 'p2'))]
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['coverage'], {'supported': 2})
        self.assertEqual(report['status'], 'complete')
        payload['units'][0]['claims'][0]['references'][0]['span_ids'] = ['p2']
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual([c['status'] for c in report['claims']], ['unsupported', 'supported'])

    def test_repeated_known_unit_entries_receive_the_same_overlap_checks(self):
        text, registry, payload = self.payload('“The study enrolled 40 participants.”; “No causal effect was established.”')
        first = payload['units'][0]['claims'][0]
        first['claim_text'] = '“The study enrolled 40 participants.”'
        second = {**deepcopy(first), 'claim_text': '“No causal effect was established.”'}
        payload['units'].append({'unit_id': 'u1', 'claims': [second]})
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['coverage'], {'supported': 2})
        payload['units'][1]['claims'][0]['claim_text'] = text
        payload['units'][1]['claims'][0]['status'] = 'unsupported'
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['coverage'], {'unchecked': 1})

    def test_one_quote_can_have_multiple_claims_from_one_real_source(self):
        text, registry, payload = self.payload('“The study enrolled 40 participants. No causal effect was established.”')
        payload['units'][0]['claims'] = [
            {'claim_text': phrase, 'status': 'supported', 'reason': 'Real source text.',
             'references': [{'source_id': registry.sources[0]['id'], 'span_ids': [span]}]}
            for phrase, span in (('“The study enrolled 40 participants.', 'p1'),
                                 ('No causal effect was established.”', 'p2'))]
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['coverage'], {'supported': 2})
        self.assertEqual(supporting_passages(report, report['claims'][0])[0]['excerpts'],
                         ['The study enrolled 40 participants. '])
        # Two different sources containing the individual sentences cannot be
        # laundered into a single invented composite direct quotation.
        registry = EvidenceRegistry(1)
        for part in ('The study enrolled 40 participants.', 'No causal effect was established.'):
            registry.capture('knowledge_open', {'results': [{'text': part}]})
        for index, claim in enumerate(payload['units'][0]['claims']):
            claim['references'] = [{'source_id': registry.sources[index]['id'], 'span_ids': ['p1']}]
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['coverage'], {'unsupported': 2})

    def test_disputed_assertion_is_not_misrepresented_as_a_source_quotation(self):
        original = 'The record reads “Café 🐈 counted 400 entries.”'
        text, registry, payload = self.payload(
            'The claim that the record reads "Café 🐈 counted 400 entries" is contradicted by the source.')
        registry = EvidenceRegistry(12)
        actual = 'The record reads: Café 🐈 counted 40 entries.'
        registry.capture('knowledge_open', {'results': [{'text': actual}]})
        payload = assessment({'sources': registry.sources, 'answer_units': answer_units(text)})
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture', assertions=(original,))
        self.assertEqual(report['claims'][0]['status'], 'supported')
        self.assertEqual(supporting_passages(report, report['claims'][0])[0]['excerpts'], [actual])
        self.assertNotIn('400', supporting_passages(report, report['claims'][0])[0]['excerpts'][0])
        # No model-provided role or missing anchor can waive quotation fidelity.
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['claims'][0]['status'], 'unsupported')
        for positive in (original, 'The paper states “Café 🐈 counted 400 entries.”',
                         'The claim “Café 🐈 counted 400 entries” is supported by the source.',
                         'The claim “Café 🐈 counted 400 entries” is not contradicted by the source.'):
            text = positive
            payload = assessment({'sources': registry.sources, 'answer_units': answer_units(text)})
            report = validate_assessment(payload, text, registry, 'balanced', 'fixture', assertions=(original,))
            self.assertEqual(report['claims'][0]['status'], 'unsupported')

    def test_model_rationale_cannot_pass_off_an_invented_quote_as_source_text(self):
        text, registry, payload = self.payload()
        fake = 'Future research should randomize participants and measure clinical sleep disorders.'
        payload['units'][0]['claims'][0]['reason'] = f"The source states, '{fake}'"
        report = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        self.assertEqual(report['claims'][0]['status'], 'unchecked')
        view = audit_payload(text, report)
        self.assertNotIn(fake, view['copy_text'])
        self.assertEqual(view['claims'][0]['passages'][0]['excerpts'], [PASSAGE])
        payload['units'][0]['claims'][0]['reason'] = "The source states, 'The study enrolled 40 participants.'"
        self.assertEqual(validate_assessment(payload, text, registry, 'balanced', 'fixture')['claims'][0]['status'], 'supported')
        payload['units'][0]['claims'][0]['reason'] = "The claim '90 participants' is not established by this source."
        payload['units'][0]['claims'][0]['status'] = 'unsupported'
        self.assertEqual(validate_assessment(payload, text, registry, 'balanced', 'fixture')['claims'][0]['status'], 'unsupported')

    def test_legacy_explanations_and_caveats_are_checked_without_rewriting_records(self):
        text, registry, payload = self.payload()
        saved = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        fake = 'All participants were cured.'
        saved['claims'][0]['reason'] = f'According to the source, "{fake}"'
        saved['revision'] = {'text': text, 'body_hash': digest(text), 'claims': deepcopy(saved['claims'])}
        original = deepcopy(saved)
        view = audit_payload(text, saved)
        self.assertNotIn(fake, view['copy_text'])
        self.assertEqual(view['claims'][0]['status'], 'unchecked')
        self.assertEqual(view['revision']['claims'][0]['status'], 'unchecked')
        self.assertEqual(saved, original)
        saved['claims'][0]['reason'] = 'A neutral assessment.'
        saved['claims'][0]['quality_issues'] = [f'The report states "{fake}"']
        self.assertEqual(audit_payload(text, saved)['claims'][0]['status'], 'unchecked')

    def test_historical_overlaps_are_guarded_in_html_copy_and_provisional_view(self):
        text, registry, payload = self.payload()
        saved = validate_assessment(payload, text, registry, 'balanced', 'fixture')
        saved['claims'].append({**deepcopy(saved['claims'][0]), 'id': 'c2', 'status': 'unsupported'})
        saved['revision'] = {'text': text, 'body_hash': digest(text), 'claims': deepcopy(saved['claims'])}
        original = deepcopy(saved)
        view = audit_payload(text, saved)
        self.assertEqual(view['claims'][0]['status'], 'unchecked')
        self.assertEqual(view['revision']['claims'][0]['status'], 'unchecked')
        self.assertNotIn('supported:', view['copy_text'])
        self.assertEqual(audited_html(text, saved).count('class="claim-citation"'), 1)
        self.assertEqual(saved, original)

    def test_false_support_metric_catches_even_one_bad_badge(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/evaluate_citations.py'
        spec = importlib.util.spec_from_file_location('evaluate_citations', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        case = {'problems': ['doubled survival'], 'supported': ['40 participants']}
        text = '40 participants and doubled survival'
        report = {'claims': [{'start': 0, 'end': len(text), 'status': 'supported'},
                             {'start': 20, 'end': len(text), 'status': 'unsupported'}]}
        self.assertEqual(module.measure(case, report, text)['false_support'], ['doubled survival'])
        self.assertEqual(module.measure(case, report, 'Corrected answer.')['false_support'], [])


class ConfigTests(unittest.TestCase):
    def test_default_off_and_comment_preserving_custom_config(self):
        self.assertEqual(Config().citation_rigor, 'off')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'custom.toml'
            path.write_text('# Keep this\ncitation_rigor = "off" # Inline\nweb_enabled = false\n')
            with patch('blackrose.config.config_dir', return_value=Path(temporary)):
                config = load_config(path)
                save_citation_rigor(config, 'strict')
                self.assertEqual(load_config(path).citation_rigor, 'strict')
            self.assertIn('# Keep this', path.read_text())
            self.assertIn('# Inline', path.read_text())
            self.assertIn('web_enabled = false', path.read_text())

    def test_failed_save_and_invalid_mode_leave_previous_selection(self):
        config = Config()
        with tempfile.TemporaryDirectory() as temporary:
            config._config_path = Path(temporary) / 'config.toml'
            with patch('blackrose.config.os.replace', side_effect=PermissionError('Read only')):
                with self.assertRaises(PermissionError):
                    save_citation_rigor(config, 'balanced')
            self.assertEqual(config.citation_rigor, 'off')
            self.assertFalse(config._config_path.exists())
        for mode in ('unknown', None, [], 1):
            with self.assertRaises(ValueError):
                Config(citation_rigor=mode)


class Backend(gui.Backend):
    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []
        self.offered = []

    async def models(self, provider=None):
        return [{'id': 'test-vision', 'meta': {'n_ctx': 128000}}]

    async def stream(self, model, messages, tools):
        self.requests.append(deepcopy(messages))
        self.offered.append(deepcopy(tools))
        step = next(self.steps)
        if callable(step):
            step = step(json.loads(messages[-1]['content']))
        if isinstance(step, str):
            yield {'delta': {'content': step}, 'finish_reason': 'stop'}
        elif isinstance(step, dict):
            yield {'delta': {'content': json.dumps(step)}, 'finish_reason': 'stop'}
        else:
            yield {'delta': {'tool_calls': [{'index': i, 'id': f'call-{i}', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}} for i, (name, arguments) in enumerate(step)]},
                'finish_reason': 'tool_calls'}


class TurnTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gui.ImageTests.asyncSetUp
    asyncTearDown = gui.ImageTests.asyncTearDown

    def setup_backend(self, steps, mode='advisory'):
        c = self.runtime.controller
        c.backend = Backend(steps)
        c.schedule_title = Mock()
        c.config.citation_rigor = mode
        c.config.knowledge_enabled = True
        c.knowledge.call = AsyncMock(return_value=deepcopy(RESULT))
        return c

    async def test_off_has_no_audit_request_record_or_extra_instructions(self):
        c = self.setup_backend([[('knowledge_search', {'query': 'study'})], '40 participants.'], 'off')
        turn = await c.send('Summarize the study.')
        self.assertIsNone(turn.audit)
        self.assertIsNone(await c._repo.audit_for(turn.assistant_message.id))
        self.assertEqual(len(c.backend.requests), 2)
        self.assertNotIn('Citation rigor is enabled', c.backend.requests[0][0]['content'])

    async def test_snapshots_and_annotations_survive_reopen_and_source_changes(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '40 participants.', assessment])
        turn = await c.send('Summarize the study.')
        self.assertEqual(turn.audit['status'], 'complete')
        c.knowledge.call.return_value['results'][0]['text'] = 'Changed source.'
        await self.runtime.database.close()
        await self.runtime.database.connect()
        report = await c._repo.audit_for(turn.assistant_message.id)
        self.assertEqual(report['sources'][0]['text'], PASSAGE)
        await self.session.render_transcript()
        payload = self.latest('transcript')['messages'][-1]
        self.assertIn('data-claim-id=', payload['html'])
        self.assertEqual(''.join(payload['audit']['claims'][0]['passages'][0]['excerpts']), PASSAGE)

    latest = gui.ImageTests.latest

    async def test_coverage_retry_reassesses_only_uncovered_units(self):
        def partial(data):
            unit, source = data['answer_units'][0], data['sources'][0]
            return {'source_based': True, 'units': [{'unit_id': unit['id'], 'claims': [{
                'claim_text': unit['text'], 'status': 'supported',
                'references': [{'source_id': source['id'], 'span_ids': [source['spans'][0]['id']]}],
                'reason': 'Fixture assessment.', 'quality_issues': []}]}]}
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})],
                                'First fact. Second fact.', partial, assessment], 'advisory')
        turn = await c.send('Summarize.')
        self.assertEqual(turn.audit['status'], 'complete')
        self.assertEqual(turn.audit['coverage'], {'supported': 2})
        retry_data = json.loads(c.backend.requests[-1][-1]['content'])
        self.assertEqual([u['id'] for u in retry_data['answer_units']], ['u2'])
        self.assertEqual(len(c.backend.requests), 4)

    async def test_failed_coverage_retry_keeps_partial_audit(self):
        def partial(data):
            unit = data['answer_units'][0]
            return {'source_based': True, 'units': [{'unit_id': unit['id'], 'claims': [{
                'claim_text': unit['text'], 'status': 'unsupported', 'references': [],
                'reason': 'Fixture gap.', 'quality_issues': []}]}]}
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})],
                                'First fact. Second fact.', partial, '{bad JSON'], 'advisory')
        turn = await c.send('Summarize.')
        self.assertEqual(turn.audit['status'], 'incomplete')
        self.assertEqual(turn.audit['coverage'], {'unsupported': 1, 'unchecked': 1})
        self.assertTrue(any('coverage retry' in s.lower() for s in turn.audit['limitations']))
        self.assertEqual(len(c.backend.requests), 4)

    async def test_structurally_invalid_assessment_gets_one_coverage_recovery(self):
        invalid = {'source_based': True, 'units': [{'unit_id': 'invented', 'claims': []}]}
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})],
                                'First fact. Second fact.', invalid, assessment], 'strict')
        turn = await c.send('Summarize.')
        self.assertEqual(turn.audit['status'], 'complete')
        self.assertEqual(turn.audit['coverage'], {'supported': 2})
        self.assertEqual(len(c.backend.requests), 4)

    async def test_repeated_invalid_assessment_stays_unchecked_without_loop(self):
        invalid = {'source_based': True, 'units': [{'unit_id': 'invented', 'claims': []}]}
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})],
                                'First fact.', invalid, invalid], 'advisory')
        turn = await c.send('Summarize.')
        self.assertEqual(turn.audit['status'], 'incomplete')
        self.assertEqual(turn.audit['coverage'], {'unchecked': 1})
        self.assertEqual(len(c.backend.requests), 4)

    async def test_balanced_and_strict_obey_research_budget_and_reaudit_revision(self):
        requests = [{'name': 'knowledge_open', 'arguments': {'document_id': 'doc-a'}} for _ in range(12)]
        for mode, limit in (('balanced', 2), ('strict', 6)):
            with self.subTest(mode=mode):
                await self.runtime.controller.new_conversation()
                c = self.setup_backend([[('knowledge_search', {'query': 'study'})], '40 participants.',
                    lambda data: assessment(data, research=requests), 'The study enrolled 40 participants.', assessment], mode)
                turn = await c.send('Summarize.')
                self.assertEqual(c.knowledge.call.await_count, limit + 1)
                self.assertEqual(turn.assistant_message.content, 'The study enrolled 40 participants.')
                self.assertEqual(turn.audit['body_hash'], digest(turn.assistant_message.content))
                self.assertEqual(turn.audit['revision']['text'], '40 participants.')
                self.assertEqual(len(c.backend.requests), 5)
                self.assertTrue(all(not offered for offered in c.backend.offered[2:]))

    async def test_malformed_audit_preserves_answer_as_unchecked(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], 'A useful answer.', '{bad JSON'])
        turn = await c.send('Explain.')
        self.assertEqual(turn.assistant_message.content, 'A useful answer.')
        self.assertEqual(turn.audit['status'], 'incomplete')
        self.assertEqual(turn.audit['claims'][0]['status'], 'unchecked')

    async def test_failed_final_assessment_keeps_revision_and_original_claim_history(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '90 participants.',
            lambda data: assessment(data, 'unsupported'), 'The study enrolled 40 participants.', '{bad JSON'], 'balanced')
        turn = await c.send('Summarize the study.')
        self.assertEqual(turn.assistant_message.content, 'The study enrolled 40 participants.')
        self.assertEqual(turn.audit['status'], 'incomplete')
        self.assertEqual(turn.audit['body_hash'], digest(turn.assistant_message.content))
        self.assertEqual(turn.audit['revision']['text'], '90 participants.')
        saved = await c._repo.audit_for(turn.assistant_message.id)
        self.assertEqual(saved, turn.audit)

    async def test_initial_format_failure_still_allows_one_bounded_revision(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '90 participants.',
            '{bad JSON', 'The study enrolled 40 participants.', assessment], 'balanced')
        turn = await c.send('Summarize the study.')
        self.assertEqual(turn.assistant_message.content, 'The study enrolled 40 participants.')
        self.assertEqual(turn.audit['status'], 'complete')
        self.assertEqual(turn.audit['revision']['claims'][0]['status'], 'unchecked')
        self.assertEqual(len(c.backend.requests), 5)

    async def test_contradiction_review_distinguishes_absence_from_negation(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], 'Survival doubled.',
            lambda data: assessment(data, 'contradicted'), {'checks': [
                {'claim_id': 'c1', 'relation': 'insufficient',
                 'reason': 'Unmeasured survival leaves the improvement unknown, rather than disproven.'}]}])
        c.knowledge.call.return_value = {'results': [{'text': 'Survival was not measured.'}]}
        turn = await c.send('Evaluate this claim.')
        self.assertEqual(turn.audit['claims'][0]['status'], 'unsupported')
        self.assertIn('unknown', turn.audit['claims'][0]['reason'])
        self.assertEqual(len(c.backend.requests), 4)

    async def test_invalid_contradiction_review_cannot_confirm_a_contradiction(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], 'A claim.',
            lambda data: assessment(data, 'contradicted'), {'checks': [
                {'claim_id': 'invented', 'relation': 'incompatible', 'reason': 'Invented claim ID.'}]}])
        turn = await c.send('Evaluate this claim.')
        self.assertEqual(turn.audit['claims'][0]['status'], 'unchecked')
        self.assertEqual(turn.audit['status'], 'incomplete')

    async def test_fresh_followup_reuses_captured_evidence_and_unrelated_reply_is_exempt(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '40 participants.', assessment,
                                'Forty participants.', assessment, 'Hello!', {'source_based': False}])
        first = await c.send('Summarize the study.')
        second = await c.send('Say that another way.')
        self.assertEqual(second.audit['sources'][0]['id'], first.audit['sources'][0]['id'])
        self.assertEqual(c.knowledge.call.await_count, 1)
        third = await c.send('Hi!')
        self.assertEqual(third.audit['status'], 'not_applicable')

    async def test_empty_and_oversized_retrieval_do_not_create_invented_snapshots(self):
        c = self.setup_backend([[('knowledge_search', {'query': 'study'})], 'A claim.', assessment])
        c.knowledge.call.return_value = {'results': [dict(RESULT['results'][0], text='a' * 70000)]}
        turn = await c.send('Summarize.')
        self.assertEqual(turn.audit['sources'], [])
        self.assertEqual(turn.audit['claims'][0]['status'], 'unsupported')

    async def test_crash_reaps_pending_audit_and_preserves_captured_text(self):
        c = self.setup_backend([])
        await c.ensure_conversation()
        _, pending = await c._repo.begin_turn(c.conversation.id, 'Question', c.model_name)
        registry = EvidenceRegistry(pending.id)
        registry.capture('knowledge_open', deepcopy(RESULT))
        report = new_report('strict', c.model_name, 'Partial answer.', registry)
        await c._repo.save_audit(pending.id, report, content='Partial answer.')
        await c._repo.reap_pending()
        saved = await c._repo.audit_for(pending.id)
        self.assertEqual(saved['status'], 'incomplete')
        self.assertEqual(saved['sources'][0]['text'], PASSAGE)
        with self.assertRaises(ValueError):
            await c._repo.save_audit(pending.id, report)

    async def test_cancellation_in_audit_retains_unchecked_answer(self):
        started = asyncio.Event()
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], 'Partial answer.'])
        original = c.backend.stream
        async def stream(model, messages, tools):
            if 'assess source support' in messages[0]['content']:
                started.set()
                await asyncio.Event().wait()
            async for choice in original(model, messages, tools):
                yield choice
        c.backend.stream = stream
        task = asyncio.create_task(c.send('Explain.'))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, 'cancelled')
        self.assertEqual(saved.content, 'Partial answer.')
        self.assertEqual((await c._repo.audit_for(saved.id))['status'], 'incomplete')

    async def test_stop_during_final_save_keeps_the_completed_audit(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '40 participants.', assessment])
        entered, release = await block_final_save(self.runtime.database)
        await stop_during_final_save(self, asyncio.create_task(c.send('Summarize the study.')),
                                     entered, release)
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.state.value, 'complete')
        self.assertEqual(saved.content, '40 participants.')
        report = await c._repo.audit_for(saved.id)
        self.assertEqual(report, c.last_completed_turn.audit)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['coverage'], {'supported': 1})
        self.assertEqual(report['body_hash'], digest(saved.content))
        self.assertNotIn('Reply or audit interrupted before completion.', report['limitations'])
        self.assertEqual(report['sources'][0]['text'], PASSAGE)

    async def test_cancellation_after_revision_keeps_corrected_text_in_storage_and_gui(self):
        started = asyncio.Event()
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '90 participants.',
            lambda data: assessment(data, 'unsupported'), 'The study enrolled 40 participants.'], 'balanced')
        await c.ensure_conversation()
        original = c.backend.stream

        async def stream(model, messages, tools):
            if ('assess source support' in messages[0]['content'] and
                    json.loads(messages[-1]['content'])['answer_units'][0]['text'] == 'The study enrolled 40 participants.'):
                started.set()
                await asyncio.Event().wait()
            async for choice in original(model, messages, tools):
                yield choice

        c.backend.stream = stream
        task = asyncio.create_task(self.session._send('Summarize the study.'))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        saved = (await c.current_messages())[-1]
        self.assertEqual(saved.content, 'The study enrolled 40 participants.')
        report = await c._repo.audit_for(saved.id)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['revision']['text'], '90 participants.')
        emitted = self.latest('reply_end')
        self.assertIn('40 participants', emitted['html'])
        self.assertNotIn('90 participants', emitted['html'])
        self.assertEqual(emitted['audit']['body_hash'], digest(saved.content))

    async def test_audit_timeout_and_disabled_research_never_silently_downgrade(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], 'Partial answer.'])
        c.config.request_timeout = .02
        original = c.backend.stream
        async def stream(model, messages, tools):
            if 'assess source support' in messages[0]['content']:
                await asyncio.sleep(1)
            async for choice in original(model, messages, tools):
                yield choice
        c.backend.stream = stream
        turn = await c.send('Explain.')
        self.assertEqual(turn.audit['mode'], 'advisory')
        self.assertEqual(turn.audit['status'], 'incomplete')
        self.assertEqual(turn.assistant_message.content, 'Partial answer.')

    async def test_effectful_or_disabled_research_tools_cannot_run(self):
        requests = [{'name': 'document_create', 'arguments': {'path': 'unwanted.md', 'content': 'No.'}},
                    {'name': 'fetch_content', 'arguments': {'url': 'https://example.org'}}]
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '40 participants.',
            lambda data: assessment(data, research=requests), '40 participants.', assessment], 'strict')
        c.file_tool = AsyncMock()
        c.web.fetch = AsyncMock()
        turn = await c.send('Summarize.')
        c.file_tool.assert_not_awaited()
        c.web.fetch.assert_not_awaited()
        self.assertTrue(any('disabled' in s for s in turn.audit['limitations']))

    async def test_extra_web_search_reads_a_real_page_within_the_same_budget(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '40 participants.',
            lambda data: assessment(data, research=[{'name': 'web_search', 'arguments': {'query': 'synthetic trial'}}]),
            '40 participants.', assessment], 'balanced')
        c.config.web_enabled = True
        c.web.search = AsyncMock(return_value={'results': [{'url': 'https://primary.example/study',
            'title': 'Trial record', 'snippet': 'A search provider summary, not evidence.'}]})
        c.web.fetch = AsyncMock(return_value={'url': 'https://primary.example/study', 'text': 'The trial enrolled 40 participants.'})
        asked = []
        async def approve(request):
            asked.append(request)
            return True
        turn = await c.send('Summarize.', approve_outbound=approve)
        # The reply read library passages, so the audit's own search needs approval;
        # the page it then reads came from that search and does not.
        self.assertEqual([(r.kind, r.target, r.origin) for r in asked],
                         [('search', 'synthetic trial', 'citation check')])
        c.web.search.assert_awaited_once()
        c.web.fetch.assert_awaited_once_with(url='https://primary.example/study')
        self.assertEqual(len(turn.audit['sources']), 2)
        self.assertTrue(any(s['text'] == 'The trial enrolled 40 participants.' for s in turn.audit['sources']))
        self.assertFalse(any('search provider summary' in s['text'] for s in turn.audit['sources']))
        self.assertTrue(any(s.get('url') == 'https://primary.example/study' for s in turn.sources))

    async def test_research_search_after_library_reads_is_not_sent_without_approval(self):
        for approve in (None, AsyncMock(return_value=False)):
            with self.subTest(approver=approve is not None):
                c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '40 participants.',
                    lambda data: assessment(data, research=[{'name': 'web_search', 'arguments': {'query': 'synthetic trial'}}]),
                    '40 participants.', assessment], 'balanced')
                c.config.web_enabled = True
                c.web.search = AsyncMock()
                c.web.fetch = AsyncMock()
                turn = await c.send('Summarize.', approve_outbound=approve)
                c.web.search.assert_not_awaited()
                c.web.fetch.assert_not_awaited()
                self.assertTrue(any('Nothing was sent' in s for s in turn.audit['limitations']))

    async def test_mode_change_is_blocked_during_a_reply_and_command_persists(self):
        c = self.setup_backend([], 'off')
        c.config._config_path = self.root / 'config.toml'
        c.is_generating = True
        result = await self.session.set_citation_rigor('strict')
        self.assertIn('Wait', result['error'])
        self.assertEqual(c.config.citation_rigor, 'off')
        c.is_generating = False
        await self.session.run_command('/rigor strict')
        self.assertEqual(c.config.citation_rigor, 'strict')
        self.assertIn('citation_rigor = "strict"', c.config._config_path.read_text())

    async def test_conversation_deletion_removes_audit_and_evidence_snapshots(self):
        c = self.setup_backend([[('knowledge_open', {'document_id': 'doc-a'})], '40 participants.', assessment])
        turn = await c.send('Summarize.')
        await c.delete_current_conversation()
        self.assertIsNone(await c._repo.audit_for(turn.assistant_message.id))

    async def test_final_answer_and_audit_roll_back_together(self):
        from blackrose.storage.records import MessageState
        c = self.setup_backend([])
        await c.ensure_conversation()
        _, pending = await c._repo.begin_turn(c.conversation.id, 'Question', c.model_name)
        registry = EvidenceRegistry(pending.id)
        original = new_report('strict', c.model_name, 'Draft.', registry)
        await c._repo.save_audit(pending.id, original, content='Draft.')
        await self.runtime.database.run(lambda conn: conn.executescript('''
            CREATE TRIGGER reject_final BEFORE UPDATE ON messages WHEN NEW.state='complete'
            BEGIN SELECT RAISE(ABORT, 'injected terminal failure'); END;'''))
        revised = new_report('strict', c.model_name, 'Revised.', registry)
        with self.assertRaisesRegex(Exception, 'injected terminal failure'):
            await c._repo.finish_message(pending.id, 'Revised.', MessageState.COMPLETE, audit=revised)
        self.assertEqual((await c.current_messages())[-1].content, 'Draft.')
        self.assertEqual(await c._repo.audit_for(pending.id), original)


if __name__ == '__main__':
    unittest.main()
