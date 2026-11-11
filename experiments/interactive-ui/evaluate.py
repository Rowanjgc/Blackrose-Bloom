"""Explicit live-provider evaluation; paired semantic UI/Markdown, isolated data.

Run from the checkout with .venv/bin/python. No inference occurs without --model.
Cloud model IDs use the existing account and may incur normal provider charges.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, is_dataclass, replace
import json
import os
from pathlib import Path
import tempfile
import time

from blackrose.config import load_config
from blackrose.backend.stream_limits import StreamBudget
from blackrose.runtime import build_runtime
from blackrose.ui_documents import contract


TASKS = {
    'comparison': [
        'Compare keyword and semantic retrieval for exact names versus paraphrases.',
        'Compare these supplied plans: Basic costs 10 USD/month and includes 5 GB; Plus costs 20 USD/month and includes 20 GB. Let me choose one.',
        'Compare supplied study options: morning, 20 minutes, quiet; evening, 40 minutes, shared space.',
        'Show a filterable comparison of CSV (tabular, plain text), JSON (nested, plain text), SQLite (relational, database).',
        'Compare these supplied items without converting units: A 2 kg at 12 USD; B 500 g at 4 USD.',
        'Compare a checklist and a calendar as planning tools; distinguish qualitative tradeoffs from measured facts.',
    ],
    'form': [
        'Ask for days per week (1–7) and pages per day (1–100), then let me request a reading plan.',
        'Collect a project name and a choice of draft/review/done, then let me submit preferences.',
        'Collect a required checkbox confirming I reviewed my own notes, then let me request a summary.',
        'Collect a language choice of English or Spanish and an optional comment, then let me submit.',
        'Collect a weekly study budget between 0.5 and 12 hours, then let me request a schedule.',
        'Collect my preferred contact label as plain text. Treat punctuation and markup as text.',
    ],
    'steps': [
        'Walk me through reading a source, checking its evidence, and recording uncertainty, one step at a time.',
        'Show three steps for reviewing a pull request locally; do not run commands.',
        'Show a four-step checklist for planning a small writing project.',
        'Explain a two-step process: state a hypothesis, then find evidence that could disprove it.',
        'Give step navigation for checking labels, units and arithmetic in a supplied table.',
        'Give three accessible steps for preparing notes before a meeting.',
    ],
    'calculation': [
        'Let me scale an estimate: 10 printed pages at 0.20 USD each and 2 folders at 1.50 USD each.',
        'Let me scale 3 notebooks at 2.50 USD and 4 pencils at 0.75 USD.',
        'Make a local quantity multiplier for 2 sessions at 45 minutes and 3 sessions at 20 minutes; use minutes as the unit.',
        'Scale an illustrative estimate with one item: 7 units at 0 USD. Preserve zero.',
        'Scale an estimate: 0.5 hours at 20 USD/hour and 2 hours at 15 USD/hour.',
        'Scale a supplied budget: 2 tickets at 12.50 USD and 1 reservation at 5 USD.',
    ],
    'metrics': [
        'Chart supplied pages read: Monday 12, Tuesday 18, Wednesday 10. Compute the total from the same dataset.',
        'Show the average of supplied response times: A 12 ms, B 18 ms, C 24 ms.',
        'Show supplied changes: A -3 items, B 0 items, C 5 items. Preserve signs.',
        'Show supplied task counts: draft 3, review 2, done 5. Compute their sum.',
        'Chart supplied estimates: Alpha 1.5 hours, Beta 2.25 hours. Preserve units and decimals.',
        'Show count aggregation for supplied samples: North 10 km, South 15 km, East 0 km; label the aggregate as items.',
    ],
}


def capture_value(value):
    """Serialize metadata snapshots without replacing live provider arguments."""
    if isinstance(value, StreamBudget):
        return {'bounds': asdict(value.bounds), 'deadline': value.deadline,
                'response_bytes': value.response_bytes, 'reply_bytes': value.reply_bytes,
                'reasoning_bytes': value.reasoning_bytes}
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f'Unsupported capture metadata: {type(value).__name__}')


def capture_stream(provider_stream, captured):
    async def stream(model, messages, offered=(), **options):
        entry = json.loads(json.dumps({'model': model, 'messages': messages,
                                      'tools': offered, 'options': options}, default=capture_value))
        entry.update(usage={}, timings={}, first_text_seconds=None, first_reasoning_seconds=None)
        captured.append(entry)
        started = time.perf_counter()
        async for choice in provider_stream(model, messages, offered, **options):
            if choice.get('_usage'):
                entry['usage'] = choice['_usage']
            if choice.get('_timings'):
                entry['timings'] = choice['_timings']
            delta = choice.get('delta') or {}
            for field, key in (('content', 'first_text_seconds'), ('reasoning_content', 'first_reasoning_seconds')):
                if delta.get(field) and entry[key] is None:
                    entry[key] = time.perf_counter() - started
            yield choice
    return stream


def answer_checks(mode, blocks, content):
    ui = [b for b in blocks if b['kind'] == 'interactive_ui']
    return {'schema_valid': bool(ui) and all(b['status'] == 'ready' and b.get('document') for b in ui),
            'markdown_control_valid': (bool(content.strip()) and not ui and
                                       all(b['status'] == 'ready' for b in blocks)) if mode == 'markdown' else None}


def run_summary(records):
    errors = sum('error' in r for r in records)
    completed = len(records) - errors
    ui = [r for r in records if r['mode'] == 'ui']
    return {'turns': len(records), 'completed_turns': completed, 'errors': errors,
            'status': 'failed' if not completed else 'partial_errors' if errors else 'completed',
            'ui_schema_valid': sum(r.get('schema_valid', False) for r in ui),
            'ui_turns': len(ui),
            'markdown_controls_valid': sum(r.get('markdown_control_valid', False) for r in records
                                           if r['mode'] == 'markdown'),
            'notice': 'Schema validity does not establish content fidelity or usefulness.'}


async def run(args):
    config = replace(load_config(), default_model=args.model, router_autostart=args.start_router,
                     image_generation_enabled=False, local_image_enabled=False,
                     knowledge_enabled=False, web_enabled=False, memory_enabled=False, citation_rigor='off',
                     interactive_ui_actions_enabled=False)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bloom-ui-evaluation-') as scratch:
        # Retain configured provider credentials; isolate conversation/workspace data.
        os.environ['XDG_DATA_HOME'] = scratch
        os.environ['XDG_STATE_HOME'] = scratch
        runtime = build_runtime(config)
        await runtime.open()
        c = runtime.controller
        try:
            catalog = await c.backend.models()
        except BaseException:
            await runtime.close()
            raise
        (args.output / 'run.json').write_text(json.dumps({'model':args.model,'endpoint':config.endpoint,
            'runs':args.runs,'tasks_per_profile':args.tasks_per_profile,'catalog':catalog,
            'schema_hash':contract()['schema_hash'],'artifact_hash':contract()['artifact_hash'],
            'repairs':config.interactive_ui_repairs,'timeout_seconds':args.timeout,
            'sampling':'inherited unchanged from server preset',
            'cache_conditions':'not controlled; record provider timing/cache metrics where reported'},indent=2))
        captured = []
        c.backend.stream = capture_stream(c.backend.stream, captured)
        c.schedule_title = lambda *a, **kw: None
        definitions = c.tool_definitions
        def tools(*a, **kw):
            if not config.interactive_ui_enabled:
                return []
            found = [t for t in definitions(*a, **kw) if t['function']['name'] == 'activate_tools']
            for t in found:
                t['function']['parameters']['properties']['capability']['enum'] = ['interactive_ui']
            return found
        c.tool_definitions = tools
        records = []
        try:
            for profile, tasks in TASKS.items():
                for index, task in enumerate(tasks[:args.tasks_per_profile]):
                    for repeat in range(args.runs):
                        for mode in ('markdown', 'ui'):
                            await c.new_conversation()
                            config.interactive_ui_enabled = mode == 'ui'
                            text = task + (f' Use the interactive_ui {profile} profile.' if mode == 'ui' else ' Answer in plain Markdown.')
                            record = {'profile': profile, 'task': task, 'index': index, 'repeat': repeat, 'mode': mode,
                                      'model': args.model, 'metrics': [], 'first_accepted_block_seconds': None}
                            started = time.perf_counter()
                            captured.clear()
                            def ui(blocks):
                                if record['first_accepted_block_seconds'] is None and any(b.get('document') for b in blocks):
                                    record['first_accepted_block_seconds'] = time.perf_counter()-started
                            try:
                                turn = await asyncio.wait_for(c.send(text, on_ui=ui, on_metrics=record['metrics'].append), args.timeout)
                                record['blocks'] = turn.assistant_message.blocks
                                record['content'] = turn.assistant_message.content
                                record.update(answer_checks(mode, record['blocks'], record['content']))
                            except Exception as error:
                                record['error'] = f'{type(error).__name__}: {error}'
                            record['total_seconds'] = time.perf_counter()-started
                            record['requests'] = list(captured)
                            records.append(record)
                            (args.output / 'results.json').write_text(json.dumps(records, indent=2, ensure_ascii=False))
                            print(f'{profile} {index+1}/{args.tasks_per_profile} repeat {repeat+1} {mode}: {record["total_seconds"]:.2f}s', flush=True)
        finally:
            await runtime.close()
    summary = run_summary(records)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2), flush=True)
    return 1 if summary['errors'] or not summary['completed_turns'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', help='Explicit local preset or opencode-go model ID; enables live inference')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--start-router', action='store_true', help='Manage the local router for this run, including shutdown on completion')
    parser.add_argument('--tasks-per-profile', type=int, default=6, help='Use 1 for a five-task pilot; 6 for the 30-task suite')
    parser.add_argument('--timeout', type=float, default=120, help='Deadline in seconds for each turn')
    parser.add_argument('--output', type=Path, default=Path('artifacts/interactive-ui-evaluation'))
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error('--runs must be 1..10')
    if not 1 <= args.tasks_per_profile <= 6:
        parser.error('--tasks-per-profile must be 1..6')
    if not 0 < args.timeout < float('inf'):
        parser.error('--timeout must be positive and finite')
    if not args.model:
        print(json.dumps({'tasks': TASKS, 'profile_prompt_bytes': {
            p:len(value.encode()) for p,value in contract()['prompts'].items()},
            'notice': 'No inference run. Specify --model to run paired live trials.'}, indent=2))
        return
    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
