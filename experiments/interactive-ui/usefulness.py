"""Bounded local usefulness audit; synthetic tasks, scratch conversations, no paid APIs.

Run with --model to enable inference. Results capture full documents for a separate
rendered/content review: schema_valid is deliberately not a usefulness score.
The live router is never started, stopped or reconfigured. Preset tuning is retained.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from blackrose.config import load_config
from blackrose.runtime import build_runtime
from blackrose.ui_documents import contract
from evaluate import TASKS, answer_checks, capture_stream
from fidelity import check_content, evaluation_failed, summarize


EXTRA = [
    ('natural-comparison', 'Compare these supplied plans in a filterable table and let me choose one: Basic costs 10 USD/month and includes 5 GB; Plus costs 20 USD/month and includes 20 GB.', None),
    ('natural-form', 'Ask for my weekly study budget between 0.5 and 12 hours and my preferred language (English or Spanish), then let me submit those choices for a schedule. Do not assume my preferences.', None),
    ('provided-form-defaults', 'Collect days per week (1–7), pages per day (1–100), and language (English or Spanish) for a reading plan. Use my 3 days per week and Spanish as the initial values. I have not chosen pages per day; require me to answer that field. Let me submit the choices.', 'form'),
    ('requested-form-defaults', 'Collect days per week (1–7) and pages per day (1–100) for a reading plan. Please choose sensible initial defaults for both fields and let me change them before submitting.', 'form'),
    ('natural-steps', 'Walk me through reading a source, checking its evidence, and recording uncertainty, one step at a time.', None),
    ('natural-calculation', 'Let me scale one combined estimate: 10 printed pages at 0.20 USD each and 2 folders at 1.50 USD each. I need a shared multiplier and combined total.', None),
    ('natural-metrics', 'Chart supplied changes: A -3 items, B 0 items, C 5 items. Preserve signs and compute the total.', None),
    ('control-fact', 'What is 2 plus 2? Answer in one sentence.', None),
    ('control-prose', 'Explain how a refrigerator works in one short paragraph.', None),
    ('control-poem', 'Write a haiku about autumn.', None),
    ('mixed-budget', 'Use only these supplied facts: Basic 10 USD/month for 5 GB; Plus 20 USD/month for 20 GB. Give a filterable comparison with a choose-plan button, one combined cost calculator for 3 notebooks at 2.50 USD and 4 pencils at 0.75 USD, and a short explanation distinguishing the plan prices from the stationery total.', 'mixed'),
    ('independent-quantities', 'A desk costs 320 USD, a chair 210 USD, a monitor 180 USD. Let me set each quantity independently: for example 1 desk, 2 chairs, 0 monitors. Show the live combined total, without sending a follow-up.', 'calculation'),
    ('tiny-metrics', 'Chart supplied measurements A 0.00001 mg, B 0.00002 mg, C 0 mg. Preserve nonzero values and compute their sum without presenting them as zero.', 'metrics'),
    ('count-distances-holdout', 'Chart these four observations and count the observations, including zeros, with the aggregate labeled items: West -2.5 m, Central 0 m, Harbor 7.25 m, Hill 0 m. Keep the measurement units and original values.', 'metrics'),
    ('comparison-evidence-holdout', 'Compare a paper checklist and a calendar. Supplied observation: in one trial, checklist setup took 2 minutes and calendar setup took 5 minutes. Distinguish those limited measurements from your qualitative view of flexibility.', 'comparison'),
    ('negative-steps', 'Explain how to compare two hypothetical approaches in exactly three sequential steps. Keep them together in a single navigable sequence.', 'steps'),
]


def tasks():
    return [(f'{profile}-{i+1}', task, profile)
            for profile, group in TASKS.items() for i, task in enumerate(group)] + EXTRA


async def run(args):
    if args.output.exists():
        raise ValueError('Use a new output directory; existing runs must not be overwritten')
    selected = tasks()
    if args.only:
        wanted = {name.strip() for name in args.only.split(',')}
        unknown = wanted - {name for name, _, _ in selected}
        if unknown:
            raise ValueError(f'Unknown task names: {sorted(unknown)}')
        selected = [task for task in selected if task[0] in wanted]
    base = load_config()
    if not base.endpoint.startswith(('http://127.0.0.1:', 'http://localhost:')):
        raise ValueError('This audit requires the configured loopback local provider')
    config = replace(base, default_model=args.model, router_autostart=False,
                     image_generation_enabled=False, local_image_enabled=False,
                     knowledge_enabled=False, web_enabled=False, memory_enabled=False,
                     interactive_ui_enabled=True, interactive_ui_actions_enabled=False,
                     interactive_ui_repairs=args.repairs, citation_rigor='off')
    args.output.mkdir(parents=True)
    records = []
    with tempfile.TemporaryDirectory(prefix='bloom-ui-usefulness-') as scratch:
        os.environ['XDG_DATA_HOME'] = scratch
        os.environ['XDG_STATE_HOME'] = scratch
        runtime = build_runtime(config)
        await runtime.open()
        c = runtime.controller
        c.schedule_title = lambda *a, **kw: None
        offered = c.tool_definitions

        def definitions(*a, **kw):
            definitions = [t for t in offered(*a, **kw) if t['function']['name'] == 'activate_tools']
            for tool in definitions:
                tool['function']['parameters']['properties']['capability']['enum'] = ['interactive_ui']
            return definitions

        c.tool_definitions = definitions
        captured = []
        c.backend.stream = capture_stream(c.backend.stream, captured)
        try:
            catalog = await c.backend.models()
            ids = {m['id'] for m in catalog} if isinstance(catalog, list) else set()
            if args.model not in ids:
                raise ValueError(f'Explicit model is absent from local catalog: {args.model}')
            artifact = contract()
            manifest = {
                'date': datetime.now(UTC).isoformat(), 'model': args.model, 'endpoint': config.endpoint,
                'schema_hash': artifact['schema_hash'], 'artifact_hash': artifact['artifact_hash'],
                'bundle_hash': artifact['bundle_hash'], 'repairs': args.repairs,
                'timeout_seconds': args.timeout, 'repeats': args.runs,
                'reasoning_effort': config.reasoning_effort.get(args.model, 'default'),
                'tasks': selected,
                'task_sha256': hashlib.sha256(json.dumps(selected).encode()).hexdigest(),
                'limits': 'Activation-only catalog, supplied synthetic evidence, no tools/actions/retrieval. No full-catalog or cloud claim. Preset unchanged; cache/contention uncontrolled.',
            }
            (args.output / 'run.json').write_text(json.dumps(manifest, indent=2))
            for name, prompt, profile in selected:
                for repeat in range(args.runs):
                    if contract()['artifact_hash'] != artifact['artifact_hash']:
                        raise RuntimeError('Interactive UI artifact changed during the declared cohort')
                    await c.new_conversation()
                    captured.clear()
                    text = prompt + (f' Use the interactive_ui {profile} profile.' if profile else '')
                    row = {'name': name, 'profile': profile, 'task': prompt, 'repeat': repeat,
                           'mode': 'ui', 'first_accepted_block_seconds': None}
                    start = time.perf_counter()

                    def on_ui(blocks, row=row, start=start):
                        if row['first_accepted_block_seconds'] is None and any(b.get('document') for b in blocks):
                            row['first_accepted_block_seconds'] = time.perf_counter() - start

                    try:
                        turn = await asyncio.wait_for(c.send(text, on_ui=on_ui), args.timeout)
                        row['blocks'] = turn.assistant_message.blocks
                        row['content'] = turn.assistant_message.content
                        row.update(answer_checks('ui', row['blocks'], row['content']))
                    except Exception as error:
                        row['error'] = f'{type(error).__name__}: {error}'
                    row['total_seconds'] = time.perf_counter() - start
                    row['requests'] = list(captured)
                    row['content_fidelity'] = check_content(row)
                    records.append(row)
                    (args.output / 'results.json').write_text(json.dumps(records, indent=2, ensure_ascii=False))
                    summary = summarize(records, manifest)
                    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2))
                    kinds = [(b.get('document') or {}).get('type', b['status']) for b in row.get('blocks', []) if b.get('document') or b['kind'] == 'interactive_ui']
                    print(f'{name} r{repeat+1}: {row["total_seconds"]:.2f}s {kinds} {row.get("error", "")}', flush=True)
        finally:
            await runtime.close()
    if evaluation_failed(summary):
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model')
    p.add_argument('--repairs', type=int, choices=(0, 1, 2), default=0)
    p.add_argument('--runs', type=int, choices=range(1, 6), default=1)
    p.add_argument('--timeout', type=float, default=120)
    p.add_argument('--only', help='Comma-separated task names')
    p.add_argument('--output', type=Path, default=Path('artifacts/interactive-ui-usefulness-20261103'))
    args = p.parse_args()
    if args.model:
        asyncio.run(run(args))
    else:
        print(json.dumps(tasks(), indent=2))


if __name__ == '__main__':
    main()
