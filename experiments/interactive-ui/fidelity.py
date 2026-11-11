"""Exact supplied-dataset checks, separate from schema, rendering and human review.

These are bounded task oracles, not a general semantic judge. Comparison units,
evidence versus opinion, prose claims and usefulness still need source/rendered
review. Run against results.json from usefulness.py; no inference is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


# None means the task does not prescribe an aggregate; do not invent a failure.
METRICS = {
    'metrics-1': ('pages', 'sum', [('Monday', 12), ('Tuesday', 18), ('Wednesday', 10)]),
    'metrics-2': ('ms', 'average', [('A', 12), ('B', 18), ('C', 24)]),
    'metrics-3': ('items', None, [('A', -3), ('B', 0), ('C', 5)]),
    'metrics-4': ('', 'sum', [('draft', 3), ('review', 2), ('done', 5)]),
    'metrics-5': ('hours', None, [('Alpha', 1.5), ('Beta', 2.25)]),
    'metrics-6': ('km', 'count', [('North', 10), ('South', 15), ('East', 0)]),
    'natural-metrics': ('items', 'sum', [('A', -3), ('B', 0), ('C', 5)]),
    'tiny-metrics': ('mg', 'sum', [('A', 0.00001), ('B', 0.00002), ('C', 0)]),
    'count-distances-holdout': ('m', 'count', [('West', -2.5), ('Central', 0), ('Harbor', 7.25), ('Hill', 0)]),
}

REVIEW_CRITERIA = {
    'comparison-5': [
        'Each item retains its original weight and price: A 2 kg / 12 USD; B 500 g / 4 USD.',
        'No unrequested unit conversion or price-per-kg/g derivation in any table or prose block.',
    ],
    'comparison-6': [
        'Checklist and calendar tradeoffs are identified as qualitative judgments or expectations.',
        'No invented measurements, benchmarks, proven rankings or unsupported measured-fact labels.',
        'No invented supporting authority (expert consensus/research) or physiological mechanisms.',
        'Read all cells and prose in context; a phrase match cannot establish evidence fidelity.',
    ],
    'comparison-evidence-holdout': [
        'Retain checklist setup 2 minutes and calendar setup 5 minutes, scoped to one supplied trial.',
        'Distinguish the supplied measurements from a tentative qualitative view of flexibility.',
        'No general performance claim, invented source/consensus, or unprovided measurements.',
    ],
}


def cohort_status(rows, manifest):
    """A row count alone cannot prove that every declared task/repeat ran."""
    tasks = manifest.get('tasks', [])
    repeats = manifest.get('repeats')
    valid = (bool(tasks) and type(repeats) is int and 1 <= repeats <= 5
             and all(isinstance(task, (list, tuple)) and len(task) == 3
                     and isinstance(task[0], str) and isinstance(task[1], str)
                     and (task[2] is None or isinstance(task[2], str)) for task in tasks))
    if not valid:
        return {'complete': False, 'error': 'Invalid or empty declared cohort'}
    names = [task[0] for task in tasks]
    if len(set(names)) != len(names):
        return {'complete': False, 'error': 'Duplicate declared task names'}
    digest = hashlib.sha256(json.dumps(tasks).encode()).hexdigest()
    if manifest.get('task_sha256') != digest:
        return {'complete': False, 'error': 'Declared task hash mismatch'}
    expected = Counter((name, repeat) for name in names for repeat in range(repeats))
    invalid = [index for index, row in enumerate(rows)
               if not isinstance(row.get('name'), str) or type(row.get('repeat')) is not int]
    actual = Counter((row.get('name'), row.get('repeat')) for index, row in enumerate(rows)
                     if index not in invalid)
    definitions = {name: (prompt, profile) for name, prompt, profile in tasks}
    mismatched = [index for index, row in enumerate(rows)
                  if (row.get('task'), row.get('profile')) != definitions.get(row.get('name'))]
    missing = sorted((expected - actual).elements())
    unexpected = list((actual - expected).elements())
    return {'complete': not (missing or unexpected or mismatched or invalid),
            'expected_turns': sum(expected.values()), 'recorded_turns': len(rows),
            'missing': missing, 'unexpected_or_duplicate': unexpected,
            'task_mismatch_rows': mismatched, 'invalid_identity_rows': invalid}


def evaluation_failed(report):
    return (not report.get('cohort', {}).get('complete', False)
            or not report['turns'] or bool(report['execution_errors'])
            or report['schema_valid'] != report['turns']
            or bool(report['content_fidelity'].get('fail', 0)))


def check_content(row):
    """Check exact observations independently of aggregate and observation unit."""
    if 'error' in row:
        return {'status': 'not_scored', 'reason': 'Execution failed; no completed answer to assess.'}
    expected = METRICS.get(row['name'])
    if expected is None:
        return {'status': 'not_scored', 'reason': 'Task requires manual content review.',
                'review_criteria': REVIEW_CRITERIA.get(row['name'], ['Check the complete answer against the original task.'])}
    documents = [b.get('document') for b in row.get('blocks', [])
                 if b.get('kind') == 'interactive_ui' and (b.get('document') or {}).get('type') == 'metrics']
    checks = {'one_metrics_view': len(documents) == 1}
    if len(documents) == 1:
        document = documents[0]
        unit, aggregate, observations = expected
        data = document.get('data', [])
        numeric = all(type(item.get('value')) in (int, float) for item in data)
        actual = Counter((str(item.get('label', '')).strip().casefold(), item.get('value'))
                         for item in data) if numeric else Counter()
        supplied = Counter((label.casefold(), value) for label, value in observations)
        checks['dataset_membership_and_values'] = numeric and actual == supplied
        if unit:
            checks['observation_unit'] = document.get('unit', '').strip().casefold() == unit
        if aggregate:
            checks['requested_aggregate'] = document.get('aggregate') == aggregate
    return {'status': 'pass' if all(checks.values()) else 'fail', 'checks': checks,
            'scope': 'Exact task dataset, observation units and explicitly requested aggregate only; prose and usability require review.'}


def summarize(rows, manifest=None):
    outcomes = [check_content(row) for row in rows]
    return {
        'turns': len(rows), 'execution_errors': sum('error' in row for row in rows),
        'schema_valid': sum(row.get('schema_valid', False) for row in rows),
        'content_fidelity': {'scope': 'Bounded metrics oracles; all replies still require manual review.',
                             **dict(Counter(outcome['status'] for outcome in outcomes))},
        'usefulness': 'Manual task/control review required, independently of schema and content checks.',
        'renderer_usability': 'Separate real-page replay and interaction review required at both widths.',
        'promotion': 'No promotion decision is made by this report.',
        'cohort': cohort_status(rows, manifest) if manifest is not None else {
            'complete': False, 'error': 'No declared cohort supplied; completeness is unverified'},
        'cases': [{'name': row['name'], 'repeat': row.get('repeat', 0), **outcome}
                  for row, outcome in zip(rows, outcomes, strict=True)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--manifest', type=Path, help='Defaults to run.json beside results')
    args = parser.parse_args()
    manifest_path = args.manifest or args.results.with_name('run.json')
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    report = summarize(json.loads(args.results.read_text()), manifest)
    print(json.dumps(report, indent=2))
    if evaluation_failed(report):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
