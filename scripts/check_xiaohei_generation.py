"""Exercise topic-only planning and local Xiaohei generation with real Gemma.

Uses an isolated chat/skill store and the existing router preset, without paid
generation. Results and the actual model tool calls remain in a temporary folder.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import tempfile
from pathlib import Path

from blackrose.attachments import AttachmentStore
from blackrose.backend.router import RouterClient
from blackrose.backend.service import RouterService
from blackrose.chat import ChatController
from blackrose.config import Config
from blackrose.illustrations import SKILL_ID
from blackrose.local_images import LocalImages
from blackrose.skills import SkillManager
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompts-only', action='store_true',
                        help='Validate Gemma plans and provider prompts without rendering images.')
    options = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='blackrose-xiaohei-validation-'))
    os.environ['XDG_STATE_HOME'] = str(root / 'state')
    print(f'Validation artifacts: {root}', flush=True)
    config = Config(default_model='gemma-4-e4b-it', web_enabled=False, knowledge_enabled=False)
    backend = RouterClient(config.endpoint, service=RouterService(config))
    database = Database(root / 'chat.db')
    await database.connect()
    skills = SkillManager(root / 'skills')
    skills.install(str(Path(__file__).resolve().parents[1] / 'skills' / SKILL_ID),
                   approved=True, origin='bundled')
    images = LocalImages(config.local_image_endpoint, config.local_image_workflows,
                         backend)
    controller = ChatController(backend, Repository(database), config, None, skills, None,
                                AttachmentStore(root / 'attachments'), local_image_generator=images)
    original_stream = backend.stream
    calls = []

    async def stream(model, messages, tools=()):
        async for choice in original_stream(model, messages, tools):
            fragments = choice.get('delta', {}).get('tool_calls')
            if fragments:
                calls.append(fragments)
            yield choice
    backend.stream = stream
    original_generate = images.generate
    generated_prompts = []

    async def generate(prompt, *args, **kwargs):
        if len(generated_prompts) >= 2:
            raise RuntimeError('Validation limit of two local images reached.')
        generated_prompts.append(prompt)
        (root / f'prompt-{len(generated_prompts)}.txt').write_text(prompt)
        if options.prompts_only:
            raise RuntimeError('Prompt-only validation stopped before rendering. No image was generated.')
        return await original_generate(prompt, *args, **kwargs)
    images.generate = generate

    def on_tool(name, state, result):
        if state != 'start':
            summary = result
            if name == 'read_skill' and isinstance(result, dict):
                summary = {k: result[k] for k in ('skill_id', 'path') if k in result}
            print(json.dumps({'tool': name, 'state': state, 'result': summary}), flush=True)

    try:
        await controller.start()
        for index, request in enumerate([
            'Use the Xiaohei illustration skill to plan an illustration showing the water cycle. '
            'Do not generate yet.',
            'Use the Xiaohei illustration skill to show the water cycle. Generate one image.',
        ], 1):
            await controller.new_conversation()
            turn = await controller.send(request, on_delta=lambda text: print(text, end='', flush=True),
                                         on_tool=on_tool)
            (root / f'reply-{index}.md').write_text(turn.assistant_message.content)
            if index == 1 and generated_prompts:
                raise AssertionError('Plan-only request started an image job')
            print('\n', flush=True)
        if not generated_prompts:
            raise AssertionError('Topic-only generation did not reach the local image provider')
    finally:
        (root / 'tool-fragments.json').write_text(json.dumps(calls, indent=2))
        await images.close()
        await backend.aclose()
        await database.close()


if __name__ == '__main__':
    asyncio.run(main())
