"""Manual cloud-model animation/revision check with isolated chat storage.

Requires an explicit cloud model and configured credentials; running this sends
provider requests. Offline verification uses check_gui_motion_canvas.py instead.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile


from blackrose.animations import export_html
from blackrose.attachments import AttachmentStore
from blackrose.backend.router import RouterClient
from blackrose.backend.providers import ChatProviders, model_provider
from blackrose.chat import ChatController
from blackrose.config import Config
from blackrose.skills import SkillManager
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository


class UnusedService:
    async def close(self):
        pass


async def check_runtime_recovery(controller, output, on_tool):
    """Seed a known runtime error and exercise the actual session repair path."""
    from blackrose.animations import normalize_arguments
    from blackrose.gui.session import Session
    from blackrose.storage.records import MessageState

    source = (Path(__file__).resolve().parents[1] /
              'skills/explanatory-animations/references/gears.tsx').read_text()
    scene = {'title': 'Meshing gears', 'description': '12 teeth drive 24 teeth with a 2:1 speed reduction.',
             'duration': 8, 'source': source}
    await controller.ensure_conversation()
    _, assistant = await controller._repo.begin_turn(controller.conversation.id,
        'Animate how meshing gears reduce speed.', controller.model_name)
    result, _ = await controller._create_animation(assistant, normalize_arguments({'scene': scene, 'loop': True}))
    await controller._repo.finish_message(assistant.id, 'Created meshing gears.', MessageState.COMPLETE)

    def emit(kind, payload):
        if kind == 'tool_activity':
            on_tool(payload['name'], payload['phase'], payload)
        elif kind == 'animation_recovery':
            print(f"Recovery: {payload['status']}", flush=True)

    session = Session(controller, emit)
    try:
        await session.boot()
        await session.animation_failed(result['attachment_id'], 'unknown hex color: none')
        async with asyncio.timeout(420):
            await session._animation_repair_task
        state = (await controller.animation_recovery.states(controller.conversation.id))[result['attachment_id']]
        if state['status'] != 'replaced':
            raise AssertionError(f"Model repair failed: {state['status']}")
        _, repaired = await controller.read_animation(state['replacement_attachment_id'])
        (output / 'repaired.json').write_text(json.dumps(repaired, indent=2))
        (output / 'repaired.html').write_text(export_html(repaired))
        print(f'Live model recovery saved a replacement. Verify playback of {output / "repaired.html"}', flush=True)
    finally:
        await session.close()


async def check(args):
    if model_provider(args.model) == 'local':
        raise ValueError('Animation authoring requires an explicit cloud model.')
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bloom-animation-model-') as directory:
        root = Path(directory)
        os.environ['XDG_STATE_HOME'] = str(root / 'state')
        config = Config(endpoint=args.endpoint, default_model=args.model,
                        knowledge_enabled=False, web_enabled=False,
                        image_generation_enabled=False, router_autostart=False)
        backend = ChatProviders(RouterClient(config.endpoint))
        database = Database(root / 'chats.db')
        skills = SkillManager(root / 'skills')
        if args.scenario != 'implicit':
            skills.install(str(Path(__file__).resolve().parents[1] / 'skills/explanatory-animations'),
                           approved=True, origin='bundled')
        c = ChatController(backend, Repository(database), config, UnusedService(), skills,
                           UnusedService(), AttachmentStore(root / 'attachments'))
        events = []

        def on_tool(name, phase, result):
            if phase in ('start', 'error'):
                print(f'{name}: {phase}' + (f" · {result.get('error')}" if result else ''), flush=True)
            events.append({'tool': name, 'phase': phase, 'result': result})

        try:
            await database.connect()
            print(f'Using cloud model {args.model}…', flush=True)
            await c.start()
            if args.scenario == 'recovery':
                await check_runtime_recovery(c, args.output, on_tool)
                return
            scenes = []
            prompts = {'rag': [
                'Create a short animation explaining a RAG pipeline. Show the question, retrieval, '
                'selected documents, language model, and answer. Use clear sequencing and captions.',
                'Revise that animation: make the retrieval step slower and the total animation longer, keep the '
                'other steps, and show which two documents are selected. Use the existing source.',
            ], 'tcp': [
                'Create a short, looping animation that explains the TCP three-way handshake.',
                'Revise that same animation: make the SYN-ACK reply slower and the total animation longer. '
                'Keep looping enabled and keep the three handshake steps. Read the existing source.',
            ], 'layers': [
                'Create a short animation describing software layers',
                'Revise that animation: slow down the request moving through the layers '
                'and make the total animation longer. Read the existing source.',
            ], 'implicit': [
                'Create a short playable visual that shows a packet moving from client to server. '
                'It should play inline and repeat.',
                'Revise that same visual: make the packet move more slowly and the total duration longer. '
                'Keep it repeating. Read the existing source.',
            ], 'saddle': [
                '$explanatory-animations of a saddle stitch',
                'Revise that saddle stitching animation: slow down the binding step and '
                'make the total animation longer. Read the existing source.',
            ], 'gears': [
                'Create a short looping animation showing how two meshing gears transmit motion. '
                'Draw actual toothed gears rotating in opposite directions, with 12 and 24 teeth. '
                'Explain the 2:1 speed reduction.',
                'Revise that same animation: slow down the rotation and increase its total duration. '
                'Read the existing source. Keep the gears and looping.',
            ]}[args.scenario]
            for index, prompt in enumerate(prompts, start=1):
                async with asyncio.timeout(420):
                    turn = await c.send(prompt, on_tool=on_tool)
                attachments = [a for a in turn.assistant_message.attachments if a.kind == 'animation']
                if not attachments:
                    raise AssertionError(f'Turn {index} produced no animation: {turn.assistant_message.content}')
                _, scene = await c.read_animation(attachments[-1].id)
                scenes.append(scene)
                (args.output / f'turn-{index}.json').write_text(json.dumps(scene, indent=2), encoding='utf-8')
                (args.output / f'turn-{index}.html').write_text(export_html(scene), encoding='utf-8')
                if args.scenario in ('tcp', 'implicit') and not scene.get('loop'):
                    raise AssertionError('The requested TCP animation was not marked as looping')
                detail = f'{len(scene["source"])} source characters' if 'source' in scene else f'{len(scene["nodes"])} nodes'
                print(f'Turn {index}: {scene["title"]}, {scene["duration"]}s, {detail}', flush=True)
            reads = [e for e in events if e['tool'] == 'animation_read' and e['phase'] == 'complete']
            if args.scenario == 'saddle':
                unrelated = [e for e in events if e['tool'] == 'read_skill'
                             and e['phase'] == 'complete' and e['result'].get('path') in (
                                 'references/rag.animation.json',
                                 'references/tcp-handshake.animation.json')]
                if unrelated:
                    raise AssertionError('Saddle stitching loaded an unrelated RAG/TCP reference')
            if not reads:
                raise AssertionError('Model did not read saved source for the revision')
            if scenes[1]['duration'] <= scenes[0]['duration']:
                raise AssertionError('The revised animation did not become longer')
            print(f'Live generation and source-based revision passed. Artifacts: {args.output}', flush=True)
        finally:
            (args.output / 'tool-events.json').write_text(json.dumps(events, indent=2), encoding='utf-8')
            try:
                await c.shutdown()
            finally:
                await database.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:18080')
    parser.add_argument('--model', required=True, help='Explicit cloud model ID, e.g. opencode-go/<model>')
    parser.add_argument('--scenario', choices=['rag', 'tcp', 'layers', 'saddle', 'implicit', 'gears', 'recovery'], default='rag')
    parser.add_argument('--output', type=Path, default=Path('/tmp/blackrose-animation-model-check'))
    asyncio.run(check(parser.parse_args()))
