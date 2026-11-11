"""Rehearse stopped-application recovery using synthetic data only; no GUI/inference.

No live config or backup input is accepted. --output must name a new directory;
without it, all drill files are temporary. See docs/recovery.md for real backups.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from unittest.mock import patch

from blackrose.animation_recovery import MAX_REPAIRS
from blackrose.animations import MIME_TYPE
from blackrose.config import load_config, save_preferences, state_dir, write_json
from blackrose.gui.session import Session
from blackrose.illustrations import IllustrationDrafts
from blackrose.instance import desktop_instance
from blackrose.motion_canvas import compile_source, validate_scene
from blackrose.prompts import wire_message
from blackrose.runtime import build_runtime
from blackrose.storage.records import MessageState
from blackrose.storage.repository import NewAttachment
from blackrose.ui_documents import UIStream, contract
from local_knowledge.config import Config as KnowledgeConfig
from local_knowledge.database import SCHEMA_VERSION, connect, doctor
from local_knowledge.ingestion import import_file, writer_lock
from local_knowledge.retrieval import search


DOCUMENT = 'Recent evidence: the recovery sample retains every accepted character.\n'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1sAAAAASUVORK5CYII=')
SCENE_SOURCE = """import {makeScene2D, Circle} from '@motion-canvas/2d';
import {waitFor} from '@motion-canvas/core';
export default makeScene2D(function* (view) {
  view.add(<Circle size={80} fill={'#ef6461'} />);
  yield* waitFor(2);
});"""


class FixtureEmbeddings:
    """Fixed unit vectors for synthetic index rows; never loads a model."""
    def identity(self):
        return {'recovery_fixture': 1, 'dimensions': 128}

    def count_tokens(self, text):
        return len(text.split())

    def embed_documents(self, items):
        return [[1.] + [0.] * 127 for _ in items]

    def embed_query(self, query):
        raise AssertionError('Recovery verification must use keyword/citation reads, not inference')


@contextmanager
def isolated_xdg(root):
    with patch.dict(os.environ, {
        **{f'XDG_{name}_HOME': str(root / name.lower()) for name in ('CONFIG', 'DATA', 'STATE', 'CACHE')},
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
    }):
        yield


def inventory(root):
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Drill snapshot contains a symbolic link: {path}')
        if path.is_file() and path != root / 'manifest.json':
            data = path.read_bytes()
            files[path.relative_to(root).as_posix()] = {
                'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    return files


def verify_snapshot(root, expected):
    if inventory(root) != expected:
        raise ValueError('Snapshot files do not match the recorded inventory')


def startup(root):
    return root / 'custom-startup/bloom.toml'


def configure(root, *, restoring=False):
    knowledge_path = root / 'config/shared-retriever.toml'
    if restoring:
        knowledge = replace(KnowledgeConfig.load(knowledge_path),
                            data_dir=str(root / 'shared-index'),
                            database_path=str(root / 'shared-index/custom.sqlite3'),
                            cache_dir=str(root / 'cache/embeddings'))
    else:
        knowledge = KnowledgeConfig(data_dir=str(root / 'shared-index'),
                                    database_path=str(root / 'shared-index/custom.sqlite3'),
                                    cache_dir=str(root / 'cache/embeddings'),
                                    dimensions=128, chunk_tokens=32, chunk_overlap=4)
    knowledge.save(knowledge_path)
    if restoring:
        # Relocate configuration only. Retain indexed provenance paths and IDs.
        text = startup(root).read_text()
        old = load_config(startup(root)).knowledge_config
        startup(root).write_text(text.replace(json.dumps(old), json.dumps(str(knowledge_path))))
    else:
        startup(root).parent.mkdir(parents=True)
        startup(root).write_text(
            'default_model = "startup-default"\nrouter_autostart = false\n'
            'web_enabled = false\nimage_generation_enabled = false\nlocal_image_enabled = false\n'
            'interactive_ui_enabled = true\n'
            f'knowledge_config = {json.dumps(str(knowledge_path))}\n')
        config = load_config(startup(root))
        config.default_model = 'saved-preference'
        config.favorite_models = ['saved-preference']
        save_preferences(config)
        write_json(state_dir() / 'window.json', {'sidebar': 280, 'collapsed': False, 'wrap': True})
    return knowledge


def attachment(store, data, filename, kind='animation', mime=MIME_TYPE, **kwargs):
    blob = store.write(data, Path(filename).suffix)
    return NewAttachment(mime_type=mime, filename=filename, kind=kind,
                         storage_path=blob.relative_path, digest=blob.digest, size=blob.size, **kwargs)


async def seed(root, inputs):
    with isolated_xdg(root):
        knowledge = configure(root)
        runtime = build_runtime(load_config(startup(root)))
        await runtime.open()
        controller = runtime.controller
        repo, store = controller._repo, controller._store
        session = Session(controller, lambda *_: None)
        try:
            conversation = await repo.create_conversation('Recovery fixture')
            await controller.open_conversation(conversation.id)
            workspace = inputs / 'workspace'
            workspace.mkdir()
            await controller.set_workspace(str(workspace))
            document = controller.attach(DOCUMENT.encode(), 'evidence.txt')
            user = await repo.add_message(conversation.id, 'user', 'Keep this recent document and image.',
                attachments=controller._persist_attachments([document, controller.attach(PNG, 'sample.png')]))

            scene = validate_scene({'version': 2, 'engine': 'motion-canvas', 'title': 'Recovery sample',
                'description': 'A stationary circle for saved-byte verification.', 'duration': 2,
                'loop': False, 'source': SCENE_SOURCE, 'compiled': compile_source(SCENE_SOURCE)})
            animation = json.dumps(scene, separators=(',', ':')).encode()
            output = workspace / 'export.txt'
            output.write_text('External workspace output has its own backup lifetime.\n')
            assistant = await repo.add_message(conversation.id, 'assistant', 'Saved evidence and views.',
                model_used='fixture-only', reply_to=user.id, attachments=[
                    *[attachment(store, animation, f'animation-{i}.json') for i in range(4)],
                    attachment(store, output.read_bytes(), 'export.txt', 'artifact', 'text/plain',
                               output_path=str(output)),
                ])
            stream = UIStream(assistant)
            stream.begin('form')
            stream.append(json.dumps(contract()['examples']['form']))
            stream.end()
            await repo.save_blocks(assistant.id, stream.blocks)
            block = session._ui_payload(stream.blocks)[0]
            envelope = {'conversation_id': conversation.id, 'assistant_message_id': assistant.id,
                        'block_id': block['block_id'], 'source_revision': block['source_revision'],
                        'view_id': block['view_id']}
            result = await session.ui_state(envelope | {'state_sequence': 1, 'values': {'days': 3}})
            assert result['ok'], result
            event = 'recovery-fixture-event-0001'
            await repo.accept_ui_event(block['block_id'], 'submit', event, {'values': {'days': 3}})
            followup, reply = await repo.begin_turn(conversation.id, 'Use these reading preferences.',
                                                  model_used='fixture-only', ui_event_id=event)
            await repo.finish_message(reply.id, 'Most recent reply survived.', MessageState.COMPLETE)
            await repo.finish_ui_event(event, 'completed', followup.id)

            drafts = IllustrationDrafts(store.root / f'illustration-drafts/{conversation.id}/{assistant.id}')
            draft = drafts.save({'core_idea': 'Preserve evidence', 'facts': ['Two copies survive.'],
                                 'scene': 'Two envelopes on a white surface.',
                                 'action': 'Xiaohei carries one envelope.', 'labels': []})
            drafts.save({'action': 'Xiaohei places the envelope beside its copy.'},
                        draft['draft_id'], draft['revision'])
            animations = [a.id for a in assistant.attachments if a.kind == 'animation']
            recovery = controller.animation_recovery
            for old, new in zip(animations[:2], animations[1:3], strict=False):
                assert (await recovery.claim(conversation.id, old, 'Synthetic playback error'))['claimed']
                await recovery.finish(conversation.id, old, 'replaced', new)
            assert (await recovery.claim(conversation.id, animations[2], 'Synthetic limit'))['status'] == 'exhausted'
            await recovery.claim(conversation.id, animations[3], 'Synthetic interrupted repair')

            package = inputs / 'skill'
            package.mkdir()
            (package / 'SKILL.md').write_text('---\nname: Recovery sample\ndescription: Synthetic instructions\n---\nRead reference.txt.\n')
            (package / 'reference.txt').write_text('A retained skill resource.\n')
            skill = controller.skills.install(str(package), approved=True)
            controller.skills.set_enabled(skill['id'], False)

            provider = FixtureEmbeddings()
            original = inputs / 'original.md'
            original.write_text('# Recovery\n\nOriginal knowledge evidence survives in retained extraction.\n')
            original_id = import_file(knowledge, provider, original)['document_id']
            capture = controller.library_proposals.web_capture({
                'url': 'https://example.org/recovery-fixture', 'text': 'Immutable recovery capture evidence.',
                'retrieved_at': '2026-10-17T00:00:00+00:00'}, assistant)
            proposal = controller.library_proposals.propose(conversation.id, capture, 'Recovery capture')
            controller.knowledge._provider = provider
            captured = await controller.review_library_add(proposal['approval_id'], approve=True)
            passages = search(knowledge, provider, 'recovery', mode='keyword')['results']
            evidence = [{'citation': p['citation'], 'text': p['text'], 'chunk_id': p['chunk_id']} for p in passages]
            assert {p['document_id'] for p in passages} == {original_id, captured['document_id']}
            await repo.save_evidence(assistant.id, evidence)
            # Snapshot a crash after row deletion but before artifact cleanup.
            deleted = await repo.create_conversation('Pending cleanup fixture')
            discarded = attachment(store, b'Private deleted-conversation bytes.', 'discarded.txt',
                                   'artifact', 'text/plain')
            await repo.add_message(deleted.id, 'assistant', 'Deleted', attachments=[discarded])
            discarded_draft = f'illustration-drafts/{deleted.id}/1/draft-1.json'
            write_json(store.root / discarded_draft, {'revision': 1, 'fixture': 'deleted'})
            await recovery.claim(deleted.id, 999, 'Deleted-conversation recovery')
            await repo.delete_conversation(deleted.id)
            cleanup = {'conversation': deleted.id, 'blob_path': discarded.storage_path,
                       'draft_path': discarded_draft, 'jobs': len(await repo.cleanup_jobs())}
            return {'conversation': conversation.id, 'assistant': assistant.id, 'user': user.id,
                    'followup': followup.id, 'animations': animations, 'block_id': block['block_id'],
                    'event': event,
                    'evidence': evidence, 'documents': sorted([original_id, captured['document_id']]),
                    'repairs': await recovery.states(conversation.id), 'skill': skill['id'],
                    'extracted_text': document.text,
                    'workspace': controller.conversation.workspace, 'output_path': str(output),
                    'animation_sha256': hashlib.sha256(animation).hexdigest(),
                    'chat_schema': await runtime.database.schema_version(), 'cleanup': cleanup}
        finally:
            await session.close()
            await runtime.close()


async def reopen(root, expected):
    with isolated_xdg(root):
        knowledge = configure(root, restoring=True)
        config = load_config(startup(root))
        assert config.default_model == 'saved-preference' and config.favorite_models == ['saved-preference']
        assert config.knowledge_enabled and not config.web_enabled
        assert json.loads((state_dir() / 'window.json').read_text()) == {
            'sidebar': 280, 'collapsed': False, 'wrap': True}
        runtime = build_runtime(config)
        await runtime.open()
        controller = runtime.controller
        events = []
        session = Session(controller, lambda kind, payload: events.append((kind, payload)))
        try:
            assert not await controller._repo.cleanup_jobs()
            assert not controller._store.exists(expected['cleanup']['blob_path'])
            assert not (controller._store.root / expected['cleanup']['draft_path']).exists()
            assert not await controller.animation_recovery.states(expected['cleanup']['conversation'])
            await controller.open_conversation(expected['conversation'])
            assert controller.conversation.title == 'Recovery fixture'
            assert controller.conversation.workspace == expected['workspace']
            messages = await controller.current_messages()
            assert messages[-1].content == 'Most recent reply survived.'
            assert next(m for m in messages if m.id == expected['followup']).ui_context['submitted_values'] == {'days': 3}
            try:
                await controller._repo.accept_ui_event(expected['block_id'], 'submit', expected['event'], {})
            except ValueError:
                pass
            else:
                raise AssertionError('Restored completed action lost its event identity')
            user = next(m for m in messages if m.id == expected['user'])
            assert expected['extracted_text'] in wire_message(user, controller._store, True)['content'][0]['text']
            for message in messages:
                for item in message.attachments:
                    data = controller._store.read(item.storage_path)
                    assert data is not None and hashlib.sha256(data).hexdigest() == item.digest
                    if item.text_path:
                        assert controller._store.read(item.text_path).decode() == expected['extracted_text']
                    if item.kind == 'animation':
                        assert item.digest == expected['animation_sha256']
                        _, scene = await controller.read_animation(item.id)
                        assert scene['source'] == SCENE_SOURCE and scene['compiled']
                    if item.kind == 'artifact':
                        assert item.output_path == expected['output_path']
                        assert not Path(item.output_path).exists()
            assert await controller.message_evidence(expected['assistant']) == expected['evidence']
            await session.render_transcript()
            transcript = next(p for k, p in reversed(events) if k == 'transcript')['messages']
            assistant = next(m for m in transcript if m['id'] == expected['assistant'])
            block = assistant['blocks'][0]
            assert block['saved_state'] == {'days': 3} and block['state_sequence'] == 1
            envelope = {'conversation_id': expected['conversation'], 'assistant_message_id': expected['assistant'],
                        'block_id': block['block_id'], 'source_revision': block['source_revision'],
                        'view_id': block['view_id']}
            assert await session.ui_source(envelope) == block['source']
            preview = await session.image_preview(next(a.id for a in user.attachments if a.kind == 'image'))
            assert base64.b64decode(preview['src'].split(',', 1)[1]) == PNG
            assert all(not a.get('error') for a in assistant['attachments'])
            assert assistant['attachments'][3]['recovery']['status'] == 'interrupted'

            recovery = controller.animation_recovery
            assert await recovery.states(expected['conversation']) == expected['repairs']
            for item in expected['animations']:
                assert not (await recovery.claim(expected['conversation'], item, 'Reopen must not retry'))['claimed']
            assert expected['repairs'][expected['animations'][2]]['attempt'] == MAX_REPAIRS + 1
            drafts = IllustrationDrafts(controller._store.root / f'illustration-drafts/{expected["conversation"]}/{expected["assistant"]}')
            assert drafts.read('draft-1', 2)['plan']['action'].startswith('Xiaohei places')
            assert json.loads((drafts.root / 'draft-1-r1.json').read_text())['revision'] == 1
            assert not controller.skills.list()[0]['enabled']
            controller.skills.set_enabled(expected['skill'], True)
            assert controller.skills.read(expected['skill'], 'reference.txt')['text'] == 'A retained skill resource.\n'
            controller.skills.set_enabled(expected['skill'], False)

            documents = await controller.knowledge.call('list')
            assert sorted(d['id'] for d in documents['documents']) == expected['documents']
            captured = next(d for d in documents['documents']
                            if d['source_metadata'].get('document_type') == 'web_capture')
            capture_path = Path(knowledge.database_path).parent / 'captures' / Path(captured['path']).name
            assert capture_path.read_text() == 'Immutable recovery capture evidence.'
            for passage in expected['evidence']:
                result = await controller.knowledge.call('open', chunk_id=passage['chunk_id'], limit=1)
                assert result['results'][0]['citation'] == passage['citation']
                assert result['results'][0]['text'] == passage['text']
            assert len(search(knowledge, FixtureEmbeddings(), 'recovery', mode='keyword')['results']) == 2
            with closing(connect(knowledge, readonly=True)) as db:
                diagnostic = doctor(db, knowledge.dimensions)
                assert diagnostic['ok'], diagnostic
            assert await runtime.database.run(lambda db: db.execute('PRAGMA integrity_check').fetchone()[0]) == 'ok'
            assert not await runtime.database.run(lambda db: db.execute('PRAGMA foreign_key_check').fetchall())
            return len(messages), sum(len(m.attachments) for m in messages)
        finally:
            await session.close()
            await runtime.close()


async def drill(root):
    started = time.monotonic()
    source, backup, restored = (root / name for name in ('source', 'backup', 'restored'))
    inputs = root / 'external-inputs'
    inputs.mkdir()
    expected = await seed(source, inputs)
    # All application/retrieval connections are closed before any file is copied.
    with isolated_xdg(source), desktop_instance(), writer_lock(KnowledgeConfig.load(source / 'config/shared-retriever.toml')):
        files = inventory(source)
        shutil.copytree(source, root / 'backup.partial')
        verify_snapshot(root / 'backup.partial', files)
        verify_snapshot(source, files)
        (root / 'backup.partial').rename(backup)
    manifest = {'mode': 'stopped-application directory copy', 'files': files, 'fixture': expected,
                'application': version('blackrose-bloom'), 'chat_schema': expected['chat_schema'],
                'retriever_schema': SCHEMA_VERSION, 'recovery_schema': 'repairs table (unversioned)',
                'ui_schema_hash': contract()['schema_hash']}
    write_json(backup / 'manifest.json', manifest)

    # Detect incomplete/corrupt snapshots before attempting to reopen any database.
    shutil.copytree(backup, root / 'damaged')
    sample = next(p for p in files if p.endswith('.txt') and '/attachments/' in p)
    (root / 'damaged' / sample).unlink()
    for corrupt in (False, True):
        if corrupt:
            (root / 'damaged' / sample).write_text('Corrupt restored extraction')
        try:
            verify_snapshot(root / 'damaged', files)
        except ValueError:
            pass
        else:
            raise AssertionError('Incomplete/corrupt snapshot was accepted')

    # Remove the old locations from reach, so restore cannot read the originals.
    source.rename(root / 'source-unavailable')
    inputs.rename(root / 'external-inputs-unavailable')
    restore_started = time.monotonic()
    shutil.copytree(backup, restored)
    verify_snapshot(restored, manifest['files'])
    messages, attachments = await reopen(restored, expected)
    # Reopen twice using fresh runtime objects; citations/state must survive again.
    await reopen(restored, expected)
    verify_snapshot(backup, files)
    return {key: value for key, value in manifest.items() if key not in ('files', 'fixture')} | {
        'outcome': 'passed', 'recorded_at': datetime.now(UTC).isoformat(), 'python': platform.python_version(),
        'sqlite': sqlite3.sqlite_version, 'platform': platform.platform(),
        'node': subprocess.check_output(['node', '--version'], text=True).strip(),
        'files': len(files), 'bytes': sum(f['bytes'] for f in files.values()),
        'cleanup_jobs_recovered': expected['cleanup']['jobs'],
        'messages': messages, 'attachments': attachments, 'knowledge_documents': len(expected['documents']),
        'recovery_seconds': round(time.monotonic() - restore_started, 3),
        'total_seconds': round(time.monotonic() - started, 3),
        'checks': ['recent history', 'original and extracted attachment bytes', 'image preview',
                   'saved animation source/compiled bytes', 'interactive state and completed event',
                   'illustration revisions', 'repair chains/limits and interrupted status',
                   'durable artifact cleanup retry after restore',
                   'skills/preferences/layout', 'knowledge citations and capture snapshots',
                   'database integrity/foreign keys/FTS/vectors', 'missing/corrupt snapshot refusal',
                   'repeated reopen without original locations'],
        'limitations': ['synthetic scale', 'no graphical playback or screen-reader check',
                        'no real model weights, inference, services or keyring',
                        'indexed provenance paths retained; external sources/outputs backed up separately'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Retain the drill in this NEW directory')
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
    context = nullcontext(args.output.resolve()) if args.output else tempfile.TemporaryDirectory(prefix='blackrose-recovery-')
    with (context as directory,
          patch.object(socket.socket, 'connect', side_effect=AssertionError('Unexpected network access')),
          patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('Unexpected network access'))):
        root = Path(directory)
        report = asyncio.run(drill(root))
        report['source_revision'] = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[1], text=True).strip()
        report['source_tree_dirty'] = bool(subprocess.check_output(
            ['git', 'status', '--porcelain'], cwd=Path(__file__).resolve().parents[1], text=True).strip())
        report['drill_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        write_json(root / 'report.json', report)
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
