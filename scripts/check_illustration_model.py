"""Probe live draft authoring with isolated storage and no image-generation capability."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from blackrose.attachments import AttachmentStore
from blackrose.backend.router import RouterClient
from blackrose.chat import ChatController
from blackrose.config import Config
from blackrose.illustrations import SKILL_ID
from blackrose.skills import SkillManager
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository


class NoImages:
    async def generate(self, *args, **kwargs):
        raise AssertionError('This probe must never start an image job')

    async def close(self):
        pass


async def check(args):
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bloom-illustration-draft-') as folder:
        root = Path(folder)
        db = Database(root / 'chats.db')
        skills = SkillManager(root / 'skills')
        skills.install(str(Path(__file__).resolve().parents[1] / 'skills' / SKILL_ID),
                       approved=True, origin='bundled')
        config = Config(default_model=args.model, router_autostart=False,
                        knowledge_enabled=False, web_enabled=False)
        c = ChatController(RouterClient(args.endpoint), Repository(db), config,
                           NoImages(), skills, NoImages(), AttachmentStore(root / 'attachments'),
                           image_generator=NoImages(), local_image_generator=NoImages())
        events = []
        def on_tool(name, phase, result):
            events.append({'name': name, 'phase': phase, 'result': result})
            if phase in ('complete', 'error'):
                print(name, phase, json.dumps(result, ensure_ascii=False)[:500], flush=True)
        await db.connect()
        try:
            async with asyncio.timeout(240):
                turn = await c.send(
                    '$ian-xiaohei-illustrations: plan one illustration of performing a '
                    'leatherworking saddle stitch. Do not generate yet. Save a structurally valid '
                    'draft with illustration_draft, repair blocking issues if any, and clearly '
                    'distinguish structural checks from unverified factual and visual quality.',
                    on_tool=on_tool)
            drafts = list((c._store.root / 'illustration-drafts').rglob('draft-[1-4].json'))
            saved = [json.loads(p.read_text()) for p in drafts]
            valid = [d for d in saved if d['valid']]
            report = {'model': args.model, 'valid_drafts': len(valid), 'events': events,
                      'reply': turn.assistant_message.content, 'drafts': saved,
                      'image_jobs': sum(e['name'] == 'image_gen' and e['phase'] == 'start' for e in events)}
            (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            if not valid or report['image_jobs']:
                raise AssertionError('Expected a valid draft and zero image jobs')
            print('PASS: live model authored a validated draft; zero image jobs.', flush=True)
        finally:
            await c.shutdown()
            await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:18080')
    parser.add_argument('--model', default='gemma-4-e4b-it')
    parser.add_argument('--output', type=Path, default=Path('/tmp/bloom-illustration-model-check'))
    asyncio.run(check(parser.parse_args()))


if __name__ == '__main__':
    main()
