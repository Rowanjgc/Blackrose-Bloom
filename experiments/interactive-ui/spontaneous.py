"""Spontaneous interactive-UI evaluation: no prompt names the feature.

Measures whether the disposition activates interactive UI on naturally
structured requests, whether the chosen profile matches the request, whether
blocks validate (including silent repair), and whether ordinary prose stays
Markdown. Conversation data is isolated in a scratch directory; the running
router is used unchanged and no model is switched.

Run from the checkout with .venv/bin/python.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time

from blackrose.config import load_config
from blackrose.runtime import build_runtime

PROMPTS = [
    ('comparison', "I'm picking a database for a small analytics side project \u2014 Postgres, SQLite, or DuckDB. What are the tradeoffs, and which would you lean toward for my case?"),
    ('form', "I want to build a realistic weekly reading habit. Ask me whatever you need about my schedule and preferences, then put together a plan."),
    ('steps', "I've never containerized anything. Walk me through getting a small Python app running in Docker, from nothing to a working container."),
    ('calculation', "I'm furnishing a small office: a desk at $320, a chair at $210, and a monitor at $180. I haven't decided quantities yet \u2014 help me see how the total changes if I buy one, two, or three of each."),
    ('metrics', "Here's my running mileage for the last six weeks: 12, 15, 9, 21, 18, 24 miles. Is my load trending up or down, and what's my average?"),
    ('mixed', "Help me plan a two-week trip to Japan: a day-by-day outline, a packing list, and a rough budget."),
]
CONTROLS = [
    "What's the capital of Australia?",
    "Explain how a refrigerator works.",
    "Write a haiku about autumn.",
]


def block_view(block):
    document = block.get('document') or {}
    return {'kind': block['kind'], 'status': block['status'], 'type': document.get('type'),
            'submit_label': document.get('submit_label'), 'error': block.get('error')}


def summarize(records):
    def first_view(row):
        ui = [b for b in row['blocks'] if b['kind'] == 'interactive_ui']
        return next((b['type'] for b in ui if b['type'] != 'text'), 'text' if ui else None)
    def stats(rows):
        ui = [[b for b in r['blocks'] if b['kind'] == 'interactive_ui'] for r in rows]
        return {'runs': len(rows),
                'activated': sum(bool(u) for u in ui),
                'valid': sum(bool(u) and all(b['status'] == 'ready' for b in u) for u in ui),
                'errors': sum(r['error'] is not None for r in rows)}
    out = {'positives': {}, 'controls': stats([r for r in records if r['kind'] == 'control'])}
    for kind, _ in PROMPTS:
        rows = [r for r in records if r['kind'] == kind]
        entry = stats(rows)
        chosen = [first_view(r) for r in rows]
        entry['chosen'] = chosen
        entry['profile_match'] = sum(c == kind or (kind == 'mixed' and c not in (None, 'text')) for c in chosen)
        entry['wrong_profile'] = sum(c not in (None, 'text', kind) for c in chosen)
        out['positives'][kind] = entry
    out['totals'] = stats(records)
    return out


async def run(args):
    config = replace(load_config(), default_model=args.model, router_autostart=False,
                     image_generation_enabled=False, local_image_enabled=False,
                     interactive_ui_enabled=True, interactive_ui_repairs=args.repairs)
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bloom-ui-spontaneous-') as scratch:
        os.environ['XDG_DATA_HOME'] = scratch
        os.environ['XDG_STATE_HOME'] = scratch
        runtime = build_runtime(config)
        await runtime.open()
        c = runtime.controller
        captured = []
        provider_stream = c.backend.stream

        async def stream(model, messages, offered=(), **options):
            captured.append({'model': model, 'tools': [t['function']['name'] for t in offered]})
            async for choice in provider_stream(model, messages, offered, **options):
                yield choice

        c.backend.stream = stream
        records = []
        try:
            queue = [(kind, p) for kind, p in PROMPTS] + [('control', p) for p in CONTROLS]
            if args.only:
                wanted = {name.strip() for name in args.only.split(',') if name.strip()}
                queue = [(kind, prompt) for kind, prompt in queue if kind in wanted]
            for kind, prompt in queue:
                for repeat in range(args.runs):
                    await c.new_conversation()
                    captured.clear()
                    started = time.perf_counter()
                    record = {'kind': kind, 'prompt': prompt, 'repeat': repeat, 'error': None, 'blocks': []}
                    try:
                        turn = await asyncio.wait_for(c.send(prompt), timeout=args.timeout)
                        record['blocks'] = [block_view(b) for b in turn.assistant_message.blocks]
                        record['content_length'] = len(turn.assistant_message.content or '')
                    except asyncio.TimeoutError:
                        record['error'] = 'TimeoutError: turn exceeded timeout'
                    except Exception as error:
                        record['error'] = f'{type(error).__name__}: {error}'
                    record['requests'] = len(captured)
                    record['tools_offered'] = captured[0]['tools'] if captured else []
                    record['total_seconds'] = time.perf_counter() - started
                    records.append(record)
                    ui = [b for b in record['blocks'] if b['kind'] == 'interactive_ui']
                    label = (f"{ui[0]['type']}/{ui[0]['status']}" if ui
                             else 'markdown' if not record['error'] else record['error'][:40])
                    print(f'{kind:11s} r{repeat + 1} {record["total_seconds"]:6.1f}s '
                          f'req={record["requests"]} -> {label}', flush=True)
                    (args.output / 'results.json').write_text(
                        json.dumps(records, indent=2, ensure_ascii=False))
        finally:
            await runtime.close()
        summary = summarize(records)
        (args.output / 'summary.json').write_text(json.dumps(summary, indent=2))
        print('\n' + json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='qwen36-35b-a3b')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--repairs', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=300.0)
    parser.add_argument('--only', help='Comma-separated prompt kinds to run (for example calculation,metrics,control)')
    parser.add_argument('--output', type=Path, default=Path('artifacts/interactive-ui-spontaneous'))
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error('--runs must be 1..10')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
