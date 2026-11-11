"""Manual qualitative probe for the bundled starter skills with a live model.

Runs representative requests through the real chat controller, once with the
starter skill selected (as a `$` chip would) and, with --baseline, once without
any skill installed. Storage, preferences and the Downloads/Blackrose output
folder are redirected into a temporary root. Web and library access are off;
research scenarios supply their sources in the request.

This sends inference requests to the configured endpoint. The default is the
local llama-router; check docs/shared-router.md before using a shared router,
and do not point it at a paid provider without deciding to accept that cost.
The script writes transcripts for human review. It asserts only structural
facts (a reply exists, tools ran without error); quality judgments are made by
reading the transcripts against each skill's references/examples.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from blackrose.attachments import AttachmentStore
from blackrose.backend.providers import ChatProviders
from blackrose.backend.router import RouterClient
from blackrose.backend.service import RouterKey
from blackrose.chat import ChatController
from blackrose.config import Config
from blackrose.skills import SkillManager
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository

ROOT = Path(__file__).resolve().parents[1]

SLEEP_SOURCES = '''Source A (fictional excerpt, "Cohort sleep study", 2021): In 4,100 adults
followed for 9 years, people sleeping under 6 hours had 1.3 times the risk of a
dementia diagnosis compared with 7 hours, adjusted for age, sex and education.
The authors note sleep was self-reported once at baseline.

Source B (fictional excerpt, "Sleep restriction trial", 2023): 48 healthy
students were randomized to 5 or 8 hours in bed for 7 nights. Restricted sleep
raised morning amyloid-beta in cerebrospinal fluid by 8%. Levels returned to
baseline after two nights of recovery sleep.

Source C (fictional excerpt, "Mendelian randomization analysis", 2024): Genetic
variants associated with shorter sleep showed no significant association with
Alzheimer's disease (odds ratio 1.02, 95% CI 0.94-1.10) in 300,000
participants.'''

HTTP_SOURCES = '''Source 1 (excerpt paraphrasing RFC 9000/9114 design): QUIC combines the
transport and TLS 1.3 handshakes, so a new connection usually needs one round
trip before application data, and resumed connections can send 0-RTT data.
Streams are independent, so a lost packet delays only the streams whose data it
carried.

Source 2 (fictional vendor blog, 2023): "Switching to HTTP/3 cut our median page
load time by 12%, with the largest gains for mobile users on lossy networks."

Source 3 (fictional measurement paper, 2022): On a wired, low-loss testbed,
HTTP/3 and HTTP/2 page load times differed by less than 2%; HTTP/3 used about 20%
more server CPU per request in the tested implementations.'''

SCENARIOS = {
    'thinking-partner': {
        'proposal': ['Help me find the weaknesses in this proposal: our 8-person team replaces '
                     'the weekly one-hour meeting with an async written update in a shared doc.'],
        'choice': ["I'm deciding whether to leave a stable job as an accountant to do a one-year "
                   "full-time master's in data science. Talk it through with me."],
        'technical-validation': ['I want to replace our Postgres full-text search with embeddings-only '
                                 'semantic search for our 2M-document support knowledge base. '
                                 'Is that sound, and how would I check before committing?'],
    },
    'learning-partner': {
        'explanation': ['Help me understand Fourier transforms well enough to use them.'],
        'misconception': ["Heavier objects fall faster, right? That's why a hammer beats a feather."],
        'guided-practice': ["I'm learning to calculate percentage discounts for my shop. Don't give me "
                            "the answers, give me one problem and let me try.",
                            'Is it 48 minus 15, so 33?'],
        'transfer': ['I understand recursion with factorials. Can I use it for anything real?'],
    },
    'writing-revision': {
        'notes-to-draft': ['Help me turn these scattered notes into a short essay:\n'
                           '- sold the car in March, cycling since\n- cheaper?? insurance, fuel gone\n'
                           '- neighbour hit by a van last year, still scared at junctions\n'
                           '- I notice the seasons now, smell of cut grass on the canal path\n'
                           '- the bad week in January, sleet, nearly gave up\n- bought panniers, game changer'],
        'restrained-edit': ['Light edit only please, keep my voice:\n\n'
                            "So I finally tried making sourdough and honestly? It was a disaster, but "
                            "like, a fun disaster. The starter which I named Gerald did not rise at all "
                            "for the first 3 days and I was convinced I'd killed him. Turns out my "
                            "kitchen is just freezing. Who knew. Anyway the loaf was flat as a pancake "
                            "but it tasted amazing so I'm counting it as a win."],
        'spec-file': ['Turn this chat thread into a short technical spec and save it as a Markdown file '
                      'named csv-export-spec.md.\n\n'
                      'Dana: users keep asking to export the orders table as CSV\n'
                      'Lee: only orders they can already see, respect filters\n'
                      'Dana: big accounts have 500k rows, browser will choke\n'
                      'Lee: generate server-side then email a link? or stream?\n'
                      'Sam: careful with Excel formula injection, cells starting with =\n'
                      'Dana: dates in ISO 8601. Currency as plain numbers plus a currency column\n'
                      "Lee: let's decide the async question next week"],
    },
    'research-synthesis': {
        'supplied-conflict': ['Using only these sources, how do they change our understanding of sleep '
                              'and dementia risk?\n\n' + SLEEP_SOURCES],
        'insufficient': ['What is the long-term effect of the new supplement Neurafin-X on memory in '
                         'adults? I have no sources; web search is off.'],
        'technical': ['Using these sources, does HTTP/3 actually reduce latency compared with HTTP/2?\n\n'
                      + HTTP_SOURCES],
    },
    'creative-workshop': {
        'ideation': ['Give me some directions for a short story about a lighthouse keeper.'],
        'development': ['Help me develop this premise without making it conventional: a city where every '
                        "resident can hear one other person's thoughts, assigned at birth."],
        'design-critique': ["I'm designing a habit app that doesn't feel like a nagging checklist. My idea: "
                            'a small garden where each habit is a plant that wilts if you skip it for 3 days. '
                            "What's not working, and what else could it be?"],
    },
}


class UnusedService:
    async def close(self):
        pass


async def run_scenario(args, skill, name, turns, with_skill, output):
    with tempfile.TemporaryDirectory(prefix='bloom-starter-skill-') as directory:
        root = Path(directory)
        downloads = root / 'Downloads'
        downloads.mkdir()
        (root / 'config').mkdir()
        (root / 'config/user-dirs.dirs').write_text(f'XDG_DOWNLOAD_DIR="{downloads}"\n')
        for variable, folder in (('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                                 ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache')):
            os.environ[variable] = str(root / folder)
        config = Config(endpoint=args.endpoint, default_model=args.model, knowledge_enabled=False,
                        web_enabled=False, image_generation_enabled=False, router_autostart=False,
                        memory_enabled=False)
        auth = RouterKey(args.key_file, owned=False) if args.key_file else None
        backend = ChatProviders(RouterClient(config.endpoint, auth=auth))
        database = Database(root / 'chats.db')
        skills = SkillManager(root / 'skills')
        if with_skill:
            skills.install(str(ROOT / 'skills' / skill), approved=True, origin='bundled')
        controller = ChatController(backend, Repository(database), config, UnusedService(), skills,
                                    UnusedService(), AttachmentStore(root / 'attachments'))
        events, record = [], {'skill': skill, 'scenario': name, 'with_skill': with_skill,
                              'model': args.model, 'turns': []}

        def on_tool(tool, phase, result):
            events.append({'tool': tool, 'phase': phase,
                           'path': (result or {}).get('path'), 'error': (result or {}).get('error')})

        try:
            await database.connect()
            await controller.start()
            for index, text in enumerate(turns):
                refs = ({'kind': 'skill', 'id': skill},) if with_skill and index == 0 else ()
                started = time.monotonic()
                async with asyncio.timeout(args.timeout):
                    turn = await controller.send(text, refs=refs, on_tool=on_tool)
                reply = turn.assistant_message.content
                if not reply.strip():
                    raise AssertionError(f'{skill}/{name}: empty reply')
                record['turns'].append({'request': text, 'reply': reply,
                                        'seconds': round(time.monotonic() - started, 1)})
            saved = sorted(p for p in downloads.rglob('*') if p.is_file())
            record['saved_files'] = [str(p.relative_to(downloads)) for p in saved]
            for path in saved:
                target = output / 'saved' / f'{skill}-{name}' / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        finally:
            record['tool_events'] = events
            try:
                await controller.shutdown()
            finally:
                await database.close()
    return record


def transcript(record):
    label = 'with skill' if record['with_skill'] else 'baseline (no skill)'
    lines = [f"## {record['skill']} / {record['scenario']} ({label})", '']
    tools = [f"{e['tool']}:{e['path'] or ''}" for e in record['tool_events'] if e['phase'] == 'complete']
    lines += [f"Tools: {', '.join(tools) or 'none'}",
              f"Saved files: {', '.join(record.get('saved_files') or []) or 'none'}", '']
    for turn in record['turns']:
        lines += ['**Request:**', '', '> ' + turn['request'].replace('\n', '\n> '), '',
                  f"**Reply** ({turn['seconds']} s):", '', turn['reply'], '']
    return '\n'.join(lines)


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    selected = args.skill or list(SCENARIOS)
    results = []
    for skill in selected:
        for name, turns in SCENARIOS[skill].items():
            for with_skill in ((True, False) if args.baseline else (True,)):
                print(f"{skill}/{name} {'skill' if with_skill else 'baseline'}…", flush=True)
                record = await run_scenario(args, skill, name, turns, with_skill, args.output)
                results.append(record)
                (args.output / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
                (args.output / 'transcripts.md').write_text(
                    '\n'.join(transcript(r) for r in results) + '\n', encoding='utf-8')
    print(f'Wrote {len(results)} transcripts to {args.output}. Review them against each '
          "skill's references/examples.md.", flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--endpoint', default='http://127.0.0.1:18080')
    parser.add_argument('--model', default=Config().default_model)
    parser.add_argument('--key-file', type=Path, help='Router API key file, e.g. the shared router key')
    parser.add_argument('--skill', action='append', choices=list(SCENARIOS))
    parser.add_argument('--baseline', action='store_true', help='Also run each request without any skill')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--output', type=Path, default=Path('artifacts/starter-skills'))
    arguments = parser.parse_args()
    if arguments.key_file:
        arguments.key_file = arguments.key_file.expanduser().resolve()
    asyncio.run(main(arguments))
