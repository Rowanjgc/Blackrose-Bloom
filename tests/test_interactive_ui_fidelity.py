"""Task oracles must catch schema-valid numeric corruption without overclaiming."""
from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path
import unittest

from blackrose.ui_documents import validate_document


SCRIPT = Path(__file__).resolve().parents[1] / 'experiments/interactive-ui/fidelity.py'
spec = importlib.util.spec_from_file_location('interactive_ui_fidelity', SCRIPT)
fidelity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fidelity)


def reply(name='metrics-6'):
    # Independent task fixtures: a broken oracle must not rewrite its own inputs.
    unit, aggregate, data = {
        'metrics-6': ('km', 'count', [('North', 10), ('South', 15), ('East', 0)]),
        'tiny-metrics': ('mg', 'sum', [('A', 0.00001), ('B', 0.00002), ('C', 0)]),
        'metrics-3': ('items', 'average', [('A', -3), ('B', 0), ('C', 5)]),
        'metrics-5': ('hours', 'sum', [('Alpha', 1.5), ('Beta', 2.25)]),
        'count-distances-holdout': ('m', 'count', [('West', -2.5), ('Central', 0), ('Harbor', 7.25), ('Hill', 0)]),
    }[name]
    document = {'type': 'metrics', 'title': 'Supplied data', 'unit': unit,
                'aggregate': aggregate or 'sum',
                'data': [{'label': label, 'value': value} for label, value in data]}
    return {'name': name, 'schema_valid': True, 'blocks': [
        {'kind': 'interactive_ui', 'status': 'ready', 'document': document}]}


class FidelityTests(unittest.TestCase):
    def manifest(self):
        tasks = [['metrics-6', 'Count these distances', 'metrics']]
        return {'tasks': tasks, 'repeats': 2,
                'task_sha256': hashlib.sha256(json.dumps(tasks).encode()).hexdigest()}

    def cohort(self):
        return [reply() | {'repeat': repeat, 'task': 'Count these distances', 'profile': 'metrics'}
                for repeat in range(2)]

    def test_declared_cohort_requires_every_task_repeat_and_matching_prompt(self):
        manifest = self.manifest()
        rows = self.cohort()
        self.assertTrue(fidelity.cohort_status(rows, manifest)['complete'])
        for changed in (rows[:1], rows[:1] * 2, rows + rows[:1],
                        [rows[0], rows[1] | {'repeat': 9}],
                        [rows[0], rows[1] | {'repeat': True}],
                        [rows[0], rows[1] | {'repeat': None}],
                        [rows[0], rows[1] | {'task': 'A different task'}],
                        [rows[0], rows[1] | {'profile': 'comparison'}]):
            with self.subTest(rows=changed):
                self.assertFalse(fidelity.cohort_status(changed, manifest)['complete'])
                self.assertTrue(fidelity.evaluation_failed(fidelity.summarize(changed, manifest)))

    def test_missing_empty_or_tampered_manifests_never_pass(self):
        rows = self.cohort()
        for manifest in (None, {}, {'tasks': [], 'repeats': 2},
                         self.manifest() | {'task_sha256': 'changed'},
                         self.manifest() | {'repeats': True}):
            with self.subTest(manifest=manifest):
                self.assertTrue(fidelity.evaluation_failed(fidelity.summarize(rows, manifest)))
        self.assertTrue(fidelity.evaluation_failed(fidelity.summarize([], self.manifest())))

    def test_complete_cohort_still_fails_execution_schema_and_content_errors(self):
        manifest = self.manifest()
        rows = self.cohort()
        self.assertFalse(fidelity.evaluation_failed(fidelity.summarize(rows, manifest)))
        for row in (rows[1] | {'error': 'Timeout'}, rows[1] | {'schema_valid': False}):
            self.assertTrue(fidelity.evaluation_failed(fidelity.summarize([rows[0], row], manifest)))
        rows[1]['blocks'][0]['document']['unit'] = 'items'
        self.assertTrue(fidelity.evaluation_failed(fidelity.summarize(rows, manifest)))

    def test_count_unit_and_aggregate_are_independent_of_schema_validity(self):
        for unit, aggregate, failed in [('km', 'sum', 'requested_aggregate'),
                                        ('items', 'count', 'observation_unit')]:
            with self.subTest(unit=unit, aggregate=aggregate):
                row = reply()
                document = row['blocks'][0]['document']
                document.update(unit=unit, aggregate=aggregate)
                validate_document(json.dumps(document))  # Structurally valid, semantically wrong.
                result = fidelity.check_content(row)
                self.assertEqual(result['status'], 'fail')
                self.assertFalse(result['checks'][failed])
                self.assertTrue(result['checks']['dataset_membership_and_values'])
        self.assertEqual(fidelity.check_content(reply())['status'], 'pass')

    def test_zero_omission_summary_double_count_and_rounding_are_detected(self):
        source = reply('tiny-metrics')
        for change in ('drop_zero', 'append_total', 'replace_zero', 'round', 'duplicate', 'rename'):
            with self.subTest(change=change):
                row = deepcopy(source)
                data = row['blocks'][0]['document']['data']
                if change == 'drop_zero':
                    data.pop()
                elif change == 'append_total':
                    data.append({'label': 'Total', 'value': 0.00003})
                elif change == 'replace_zero':
                    data[-1] = {'label': 'Sum (A+B)', 'value': 0.00003}
                elif change == 'round':
                    data[0]['value'] = 0
                elif change == 'duplicate':
                    data.append(deepcopy(data[0]))
                else:
                    data[0]['label'] = 'Invented'
                validate_document(json.dumps(row['blocks'][0]['document']))
                self.assertEqual(fidelity.check_content(row)['status'], 'fail')
        self.assertEqual(fidelity.check_content(source)['status'], 'pass')

    def test_signs_fractions_order_and_unspecified_aggregate(self):
        for name in ('metrics-3', 'metrics-5', 'count-distances-holdout'):
            row = reply(name)
            data = row['blocks'][0]['document']['data']
            data.reverse()  # Reordering does not change membership or values.
            self.assertEqual(fidelity.check_content(row)['status'], 'pass')
            data[0]['value'] += 0.01
            self.assertEqual(fidelity.check_content(row)['status'], 'fail')

    def test_missing_duplicate_and_nonmetric_views_do_not_pass(self):
        row = reply()
        for blocks in ([], row['blocks'] * 2, [{'kind': 'markdown', 'status': 'ready'}],
                       [{'kind': 'interactive_ui', 'status': 'invalid', 'document': None}]):
            self.assertEqual(fidelity.check_content({**row, 'blocks': blocks})['status'], 'fail')
        self.assertEqual(fidelity.check_content({'name': 'comparison-6'})['status'], 'not_scored')

    def test_summary_reports_scope_and_never_treats_schemas_as_quality(self):
        row = reply()
        row['blocks'][0]['document']['unit'] = 'items'
        summary = fidelity.summarize([row, {'name': 'comparison-6', 'error': 'Timeout'}])
        self.assertEqual(summary['schema_valid'], 1)
        self.assertEqual(summary['content_fidelity']['fail'], 1)
        self.assertEqual(summary['content_fidelity']['not_scored'], 1)
        self.assertEqual(summary['execution_errors'], 1)
        timeout = fidelity.check_content({'name': 'metrics-6', 'error': 'Timeout'})
        self.assertEqual(timeout['status'], 'not_scored')
        self.assertIn('Execution failed', timeout['reason'])
        self.assertIn('Manual', summary['usefulness'])
        self.assertIn('Separate', summary['renderer_usability'])


if __name__ == '__main__':
    unittest.main()
