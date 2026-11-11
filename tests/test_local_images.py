"""Local routing, ComfyUI API, and GPU handoff regressions."""
import asyncio
import base64
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from blackrose.attachments.images import inspect_image
from blackrose.backend.router import RouterClient
from blackrose.config import Config
from blackrose.image_generation import GeneratedImage
from blackrose.local_images import ComfyService, GIB, LocalImages, MODEL
import test_gui_images as gui_images
from test_image_generation import GeneratingBackend

png = gui_images.png


def workflow(edit=False):
    graph = {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'qwen.safetensors'}},
        '4': {'class_type': 'TextEncodeQwenImage21', 'inputs': {'prompt': ['10', 0]}},
        '5': {'class_type': 'EmptyLatentImage', 'inputs': {'width': 1024, 'height': 1024}},
        '6': {'class_type': 'KSampler', 'inputs': {'seed': 0, 'steps': 25}},
        '8': {'class_type': 'SaveImage', 'inputs': {'filename_prefix': 'openwebui/qwen21'}},
        '10': {'class_type': 'LMStudioUnloadPassthrough', 'inputs': {'text': ''}},
    }
    if edit:
        graph['9'] = {'class_type': 'LoadImage', 'inputs': {'image': 'example.png'}}
        graph['4']['inputs']['images.image_1'] = ['9', 0]
    return graph


class Service:
    def __init__(self, events, owned=True):
        self.events = events
        self.owned = owned

    async def ensure_running(self):
        self.events.append('start')

    async def release(self):
        self.events.append('release')
        self.owned = False


class LocalProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name, editing in [('qwen21-t2i.api.json', False), ('qwen21-edit.api.json', True)]:
            (self.root / name).write_text(json.dumps(workflow(editing)))
        self.events = []
        self.requests = []
        self.prompt_id = None
        self.history_ready = asyncio.Event()
        self.block = False
        self.error = False
        self.busy = False
        self.fail_submit = False
        # The selected chat model and the router's view of it. Unloading and loading
        # update these rows, so each test can compare the state before and after a job.
        self.chat = 'gemma-4-e4b-it'
        self.rows = [{'id': self.chat, 'status': {'value': 'loaded'}}]
        # VRAM the kernel reports before the job, and after our chat model is released.
        self.free_vram = 16 * GIB
        self.free_after_release = 16 * GIB
        self.backend = AsyncMock()
        self.backend.models.side_effect = lambda: [dict(r) for r in self.rows]
        self.backend.unload.side_effect = self._unload
        self.backend.load.side_effect = self._load
        vram = patch('blackrose.local_images.available_vram', side_effect=lambda total: self.free_vram)
        vram.start()
        self.addCleanup(vram.stop)
        self.service = Service(self.events)
        self.provider = LocalImages('http://127.0.0.1:8189', self.root, self.backend,
            transport=httpx.MockTransport(self.handle), service=self.service,
            lock_path=self.root / 'lock', poll_interval=0.001, keep_chat_loaded=False,
            idle_release=0)  # Keep-warm has its own tests; these pin the per-job handoff.
        self.ram_free = 24 * GIB

    async def asyncTearDown(self):
        await self.provider.close()
        self.temp.cleanup()

    def _set_status(self, model, value):
        for row in self.rows:
            if row['id'] == model:
                row['status'] = {'value': value}

    def _unload(self, model):
        self.events.append('unload:' + model)
        self._set_status(model, 'unloaded')
        self.free_vram = self.free_after_release

    def _load(self, model):
        self.events.append('load:' + model)
        self._set_status(model, 'loaded')

    async def generate(self, prompt, **kwargs):
        """Run one job for the selected chat model, as the controller does."""
        kwargs.setdefault('chat_model', self.chat)
        return await self.provider.generate(prompt, **kwargs)

    async def handle(self, request):
        self.requests.append(request)
        self.assertEqual(request.url.host, '127.0.0.1')
        self.assertNotIn('authorization', request.headers)
        path = request.url.path
        if path == '/system_stats':
            return httpx.Response(200, json={'system': {'ram_free': self.ram_free},
                'devices': [{'vram_total': 16 * 1024**3}]})
        if path == '/queue' and request.method == 'GET':
            return httpx.Response(200, json={'queue_running': [[1, 'other-job']] if self.busy else [],
                                            'queue_pending': []})
        if path == '/prompt':
            body = json.loads(request.content)
            self.prompt_id = body['prompt_id']
            self.graph = body['prompt']
            if self.fail_submit:
                raise httpx.ReadTimeout('lost response', request=request)
            return httpx.Response(200, json={'prompt_id': self.prompt_id, 'node_errors': {}})
        if path.startswith('/history/'):
            self.history_ready.set()
            if self.block:
                await asyncio.Event().wait()
            return httpx.Response(200, json={self.prompt_id: {
                'status': {'status_str': 'error' if self.error else 'success'},
                'outputs': {'8': {'images': [{'filename': 'output.png', 'type': 'output'}]}}}})
        if path == '/view':
            return httpx.Response(200, content=png())
        if path == '/upload/image':
            self.assertIn(b'name="type"\r\n\r\ntemp', request.content)
            self.assertIn(png(), request.content)
            return httpx.Response(200, json={'name': 'reference.png', 'subfolder': ''})
        if path in ('/queue', '/interrupt'):
            return httpx.Response(200)
        self.fail(str(request.url))

    async def test_generation_uses_private_workflow_and_releases_before_restore(self):
        updates = []
        result = await self.generate('Exact prompt: "Hello"', on_progress=updates.append)
        self.assertEqual(result.model, MODEL)
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(result.data, png())
        self.assertEqual(self.events, ['start', 'unload:gemma-4-e4b-it', 'release', 'load:gemma-4-e4b-it'])
        self.assertEqual(self.graph['4']['inputs']['prompt'], 'Exact prompt: "Hello"')
        self.assertNotIn('10', self.graph)
        self.assertGreater(self.graph['5']['inputs']['width'], self.graph['5']['inputs']['height'])
        self.assertEqual(self.graph['6']['inputs']['steps'], 25)
        self.assertIn('10', json.loads((self.root / 'qwen21-t2i.api.json').read_text()))
        self.assertIn('Restoring Gemma 4 E4B IT…', updates)

    async def test_edits_upload_all_references_without_lmstudio_calls(self):
        reference = 'data:image/png;base64,' + base64.b64encode(png()).decode()
        await self.generate('Make it blue', references=[reference] * 4)
        self.assertEqual(sum(r.url.path == '/upload/image' for r in self.requests), 4)
        for index in range(1, 5):
            node = self.graph['4']['inputs'][f'images.image_{index}'][0]
            self.assertEqual(self.graph[node]['inputs']['image'], 'reference.png [temp]')
        self.assertNotIn('10', self.graph)

    async def test_generation_failure_restores_chat_model_after_cleanup(self):
        self.error = True
        with self.assertRaisesRegex(RuntimeError, 'Local image generation failed'):
            await self.generate('test')
        self.assertEqual(self.events[-2:], ['release', 'load:gemma-4-e4b-it'])

    async def test_stop_cancels_only_our_job_and_restores_chat_model(self):
        self.block = True
        task = asyncio.create_task(self.generate('test'))
        await self.history_ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events[-2:], ['release', 'load:gemma-4-e4b-it'])
        interrupt = next(r for r in self.requests if r.url.path == '/interrupt')
        self.assertEqual(json.loads(interrupt.content), {'prompt_id': self.prompt_id})

    async def test_lost_submission_response_still_cancels_our_known_uuid(self):
        self.fail_submit = True
        with self.assertRaisesRegex(RuntimeError, 'No paid request was sent'):
            await self.generate('test')
        interrupt = next(r for r in self.requests if r.url.path == '/interrupt')
        self.assertEqual(json.loads(interrupt.content)['prompt_id'], self.prompt_id)
        self.assertEqual(self.events[-2:], ['release', 'load:gemma-4-e4b-it'])

    async def test_busy_adopted_service_is_left_untouched(self):
        self.busy = True
        self.service.owned = False
        with self.assertRaisesRegex(RuntimeError, 'busy'):
            await self.generate('test')
        self.assertEqual(self.events, ['start'])
        self.assertFalse(any(r.method == 'POST' for r in self.requests))

    async def test_missing_workflow_fails_before_memory_handoff(self):
        self.provider.workflows = self.root / 'missing'
        with self.assertRaisesRegex(RuntimeError, 'workflows are missing'):
            await self.generate('test')
        self.assertEqual(self.events, [])

    async def test_failed_start_is_cleaned_up(self):
        self.service.ensure_running = AsyncMock(side_effect=RuntimeError('start failed'))
        with self.assertRaisesRegex(RuntimeError, 'start failed'):
            await self.generate('test')
        self.assertEqual(self.events, ['release'])

    async def test_sufficient_memory_keeps_gemma_loaded_and_skips_reload(self):
        self.provider.keep_chat_loaded = True
        self.free_vram = 10 * GIB
        result = await self.generate('test')
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(self.events, ['start', 'release'])
        self.backend.unload.assert_not_awaited()
        self.backend.load.assert_not_awaited()

    async def test_low_gpu_memory_falls_back_to_exclusive_generation(self):
        self.provider.keep_chat_loaded = True
        self.free_vram = 6 * GIB
        await self.generate('test')
        self.assertEqual(self.events, ['start', 'unload:gemma-4-e4b-it', 'release', 'load:gemma-4-e4b-it'])

    async def test_low_system_memory_also_uses_exclusive_path(self):
        self.provider.keep_chat_loaded = True
        self.free_vram = 10 * GIB
        self.ram_free = 12 * GIB
        await self.generate('test')
        self.backend.unload.assert_awaited_once()
        self.backend.load.assert_awaited_once()

    async def test_cancelling_shared_job_does_not_reload_gemma(self):
        self.provider.keep_chat_loaded = True
        self.free_vram = 10 * GIB
        self.block = True
        task = asyncio.create_task(self.generate('test'))
        await self.history_ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events, ['start', 'release'])
        self.backend.load.assert_not_awaited()

    async def test_edit_requires_more_headroom_than_generation(self):
        self.provider.keep_chat_loaded = True
        self.free_vram = 10 * GIB
        reference = 'data:image/png;base64,' + base64.b64encode(png()).decode()
        await self.generate('Edit this image', references=[reference])
        self.backend.unload.assert_awaited_once()
        self.backend.load.assert_awaited_once()

    async def test_edit_keeps_gemma_loaded_with_sufficient_headroom(self):
        self.provider.keep_chat_loaded = True
        self.free_vram = 11 * GIB
        reference = 'data:image/png;base64,' + base64.b64encode(png()).decode()
        await self.generate('Edit this image', references=[reference])
        self.backend.unload.assert_not_awaited()
        self.backend.load.assert_not_awaited()

    async def test_sleeping_chat_model_is_neither_unloaded_nor_woken(self):
        self._set_status(self.chat, 'sleeping')
        updates = []
        await self.generate('test', on_progress=updates.append)
        self.assertEqual(self.events, ['start', 'release'])
        self.backend.unload.assert_not_awaited()
        self.backend.load.assert_not_awaited()
        self.assertFalse(any(message.startswith('Restoring') for message in updates))

    async def test_unloaded_chat_model_stays_unloaded_after_failure(self):
        self._set_status(self.chat, 'unloaded')
        self.error = True
        with self.assertRaisesRegex(RuntimeError, 'Local image generation failed'):
            await self.generate('test')
        self.assertEqual(self.events, ['start', 'release'])
        self.backend.load.assert_not_awaited()
        self.assertEqual(self.rows[0]['status'], {'value': 'unloaded'})

    async def test_cancelling_sleeping_job_restores_nothing(self):
        self._set_status(self.chat, 'sleeping')
        self.block = True
        task = asyncio.create_task(self.generate('test'))
        await self.history_ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events, ['start', 'release'])
        self.backend.load.assert_not_awaited()

    async def test_cloud_chat_model_involves_no_router_memory(self):
        self.chat = 'opencode-go/gemma-4-e4b-it'
        result = await self.generate('test')
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(self.events, ['start', 'release'])
        self.backend.models.assert_not_awaited()
        self.backend.unload.assert_not_awaited()
        self.backend.load.assert_not_awaited()

    async def test_selected_model_is_restored_by_its_own_name(self):
        self.chat = 'qwen36-35b-a3b'
        self.rows = [{'id': self.chat, 'status': {'value': 'loaded'}}]
        self.provider.keep_chat_loaded = True
        updates = []
        await self.generate('test', on_progress=updates.append)
        self.assertEqual(self.events, ['start', 'unload:qwen36-35b-a3b', 'release', 'load:qwen36-35b-a3b'])
        self.assertIn('Restoring Qwen36 35B A3B…', updates)

    async def test_loading_chat_model_settles_before_the_handoff_decision(self):
        self._set_status(self.chat, 'loading')
        reads = []
        def settle():
            reads.append(1)
            if len(reads) > 1:
                self._set_status(self.chat, 'loaded')
            return [dict(r) for r in self.rows]
        self.backend.models.side_effect = settle
        await self.generate('test')
        self.assertGreater(len(reads), 1)
        self.assertEqual(self.events, ['start', 'unload:gemma-4-e4b-it', 'release', 'load:gemma-4-e4b-it'])

    async def test_foreign_model_is_never_evicted_to_make_room(self):
        self.rows.append({'id': 'other-client-model', 'status': {'value': 'loaded'}})
        self.free_vram = 3 * GIB
        self.free_after_release = 5 * GIB
        with self.assertRaisesRegex(RuntimeError, 'Another application is using GPU memory'):
            await self.generate('test')
        # Our model is released and restored; the other client's model is untouched.
        self.assertEqual(self.events, ['start', 'unload:gemma-4-e4b-it', 'release', 'load:gemma-4-e4b-it'])
        self.assertEqual([c.args[0] for c in self.backend.unload.await_args_list], [self.chat])
        self.assertEqual(self.rows[-1]['status'], {'value': 'loaded'})

    async def test_foreign_model_refusal_with_sleeping_chat_model_unloads_nothing(self):
        self._set_status(self.chat, 'sleeping')
        self.rows.append({'id': 'other-client-model', 'status': {'value': 'loaded'}})
        self.free_vram = 3 * GIB
        with self.assertRaisesRegex(RuntimeError, 'Another application is using GPU memory'):
            await self.generate('test')
        self.assertEqual(self.events, ['start', 'release'])
        self.backend.unload.assert_not_awaited()
        self.backend.load.assert_not_awaited()


class MemoryReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_acknowledgement_is_not_treated_as_memory_release(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == '/free':
                return httpx.Response(200)
            memory = 2**30 if calls.count('/system_stats') == 1 else 0
            return httpx.Response(200, json={'devices': [{'torch_vram_total': memory}]})
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8189',
                transport=httpx.MockTransport(handler)) as client:
            await ComfyService(client).release()
        self.assertEqual(calls, ['/free', '/system_stats', '/system_stats'])

    async def test_router_unload_waits_for_unloaded_status(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == '/models/unload':
                self.assertEqual(json.loads(request.content), {'model': 'gemma-4-e4b-it'})
                return httpx.Response(200, json={'success': True})
            return httpx.Response(200, json={'data': [{'id': 'gemma-4-e4b-it', 'status': {
                'value': 'loaded' if len(calls) == 1 else 'unloaded'}}]})
        client = httpx.AsyncClient(base_url='http://127.0.0.1:18080', transport=httpx.MockTransport(handler))
        router = RouterClient('http://127.0.0.1:18080', client=client)
        await router.unload('gemma-4-e4b-it')
        await router.aclose()
        self.assertEqual(calls, ['/v1/models', '/models/unload', '/v1/models'])


async def _record(order, item):
    order.append(item)


class TwoImageBackend(GeneratingBackend):
    """The first round asks for two images at once; the second answers."""

    async def stream(self, model, messages, tools):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            yield {'delta': {'tool_calls': [
                {'index': index, 'id': f'image-{index}', 'function': {
                    'name': 'image_gen', 'arguments': json.dumps({'prompt': f'Bird {index}'})}}
                for index in range(2)]}, 'finish_reason': 'tool_calls'}
        else:
            yield {'delta': {'content': 'Both birds are ready.'}, 'finish_reason': 'stop'}


class RecordingLocalImages:
    """Records the controller's handoff calls in order."""
    recovery_error = None

    def __init__(self):
        self.calls = []
        self.inside = False

    async def make_room_for_chat(self, model):
        self.calls.append(('room', model))

    @asynccontextmanager
    async def batch(self):
        self.calls.append('batch')
        self.inside = True
        try:
            yield
        finally:
            self.inside = False
            self.calls.append('batch-end')

    async def generate(self, prompt, aspect_ratio='16:9', references=(), *, on_progress=None,
                       chat_model=None):
        self.calls.append(('generate', self.inside))
        return GeneratedImage(png(), inspect_image(png()), MODEL, 0.0)

    async def close(self):
        self.calls.append('close')

    async def reap_orphan(self):
        self.calls.append('reap')

    def status(self):
        return {'state': 'loaded', 'seconds_left': 42}

    async def release_now(self):
        self.calls.append('release')
        self.status = lambda: {'state': 'not_loaded'}
        return self.status()

    def workflow_problem(self):
        return None


class ChatLocalImageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gui_images.ImageTests.asyncSetUp
    asyncTearDown = gui_images.ImageTests.asyncTearDown
    latest = gui_images.ImageTests.latest

    async def test_one_round_of_local_images_shares_one_handoff(self):
        c = self.runtime.controller
        c.model_name = 'gemma-4-e4b-it'
        c.backend = TwoImageBackend()
        c.local_image_generator = local = RecordingLocalImages()
        await self.session.submit('Draw two birds')
        await self.session._generation
        room = ('room', 'gemma-4-e4b-it')
        # The turn's model load, then each model round, check first; one batch holds both images.
        self.assertEqual(local.calls, [room, room, 'batch', ('generate', True),
                                       ('generate', True), 'batch-end', room])
        self.assertEqual(len((await c.current_messages())[-1].attachments), 2)

    async def test_single_local_image_needs_no_batch(self):
        c = self.runtime.controller
        c.model_name = 'gemma-4-e4b-it'
        c.backend = GeneratingBackend()
        c.local_image_generator = local = RecordingLocalImages()
        await self.session.submit('Draw a bird')
        await self.session._generation
        self.assertNotIn('batch', local.calls)
        self.assertIn(('generate', False), local.calls)

    async def test_picker_shows_and_releases_the_warm_image_model(self):
        c = self.runtime.controller
        c.local_image_generator = local = RecordingLocalImages()
        options = await self.session.image_providers()
        self.assertEqual(options['local_memory'], {'state': 'loaded', 'seconds_left': 42})
        self.assertEqual(local.calls, ['reap'], 'A status read reaps an orphaned record first')
        released = await self.session.release_image_memory()
        self.assertEqual(released['local_memory'], {'state': 'not_loaded'})
        self.assertIn('release', local.calls)

    async def test_picker_reports_a_failed_release(self):
        c = self.runtime.controller
        c.local_image_generator = local = RecordingLocalImages()

        async def fail():
            raise RuntimeError('free failed')

        local.release_now = fail
        self.assertEqual(await self.session.release_image_memory(),
                         {'error': 'Could not release the image model: free failed'})
        c.local_image_generator = None
        self.assertIn('error', await self.session.release_image_memory())
        self.assertIsNone((await self.session.image_providers())['local_memory'])

    async def test_chat_model_loads_make_room_first(self):
        c = self.runtime.controller
        c.local_image_generator = local = RecordingLocalImages()
        order = []
        original = c.backend.load

        async def load(model):
            order.append(('load', model))
            return await original(model)

        c.backend.load = load
        local.make_room_for_chat = lambda model: _record(order, ('room', model))
        await c.select_model('qwen36-35b-a3b')
        await c.load_model('gemma-4-e4b-it')
        self.assertEqual(order, [('room', 'qwen36-35b-a3b'), ('load', 'qwen36-35b-a3b'),
                                 ('room', 'gemma-4-e4b-it'), ('load', 'gemma-4-e4b-it')])

    async def test_local_route_needs_no_key_and_keeps_image_and_progress_in_chat(self):
        c = self.runtime.controller
        c.model_name = 'gemma-4-e4b-it'
        c.backend = GeneratingBackend()
        c.image_generator = AsyncMock()
        c.local_image_generator = AsyncMock()
        async def generate(*args, on_progress=None, chat_model=None):
            on_progress('Generating image locally…')
            return GeneratedImage(png(), inspect_image(png()), MODEL, 0.0)
        c.local_image_generator.generate.side_effect = generate
        await self.session.submit('Draw a small blue bird')
        await self.session._generation
        c.image_generator.generate.assert_not_called()
        c.local_image_generator.generate.assert_awaited_once()
        saved = (await c.current_messages())[-1]
        self.assertEqual(len(saved.attachments), 1)
        self.assertEqual(saved.state.value, 'complete')
        self.assertIn('Generating image locally…', [p.get('state') for k, p in self.events if k == 'status'])
        self.assertIn('locally', next(t['function']['description'] for t in c.tool_definitions()
                                      if t['function']['name'] == 'image_gen'))
        self.assertIn('no API charge', c.image_provider_label)

    async def test_local_error_never_falls_back_to_paid_provider(self):
        c = self.runtime.controller
        c.model_name = 'gemma-4-e4b-it'
        c.backend = GeneratingBackend()
        c.image_generator = AsyncMock()
        c.local_image_generator = AsyncMock()
        c.local_image_generator.generate.side_effect = RuntimeError('Local service unavailable')
        await self.session.submit('Draw a bird')
        await self.session._generation
        c.image_generator.generate.assert_not_called()
        tool = next(m for m in c.backend.requests[-1] if m['role'] == 'tool')
        self.assertIn('Do not retry or switch to paid', tool['content'])

    async def test_provider_choice_is_independent_of_the_chat_model(self):
        c = self.runtime.controller
        c.image_generator = AsyncMock()
        c.local_image_generator = AsyncMock()
        description = lambda: next(t['function']['description'] for t in c.tool_definitions()
                                   if t['function']['name'] == 'image_gen')
        for model in ('gemma-4-e4b-it', 'qwen36-35b-a3b'):
            await c.select_model(model)
            self.assertIs(c._image_provider(), c.local_image_generator, model)
            self.assertIn('locally', description())
        c.set_image_provider('openrouter')
        for model in ('gemma-4-e4b-it', 'qwen36-35b-a3b'):
            await c.select_model(model)
            self.assertIs(c._image_provider(), c.image_generator, model)
            self.assertIn('OpenRouter (paid)', description())
        c.set_image_provider('off')
        self.assertIsNone(c._image_provider())
        self.assertNotIn('image_gen', [t['function']['name'] for t in c.tool_definitions()])
        self.assertEqual(c.image_provider_label, 'Image generation off')


class ImageProviderChoiceTests(unittest.IsolatedAsyncioTestCase):
    """The saved provider decides routing; chat models and unavailable choices never reroute."""

    asyncSetUp = gui_images.ImageTests.asyncSetUp
    asyncTearDown = gui_images.ImageTests.asyncTearDown

    async def test_unset_choice_is_local_when_available_and_off_otherwise(self):
        c = self.runtime.controller
        c.image_generator = AsyncMock()
        c.local_image_generator = AsyncMock()
        self.assertEqual(c.image_selection().kind, 'local')
        c.local_image_generator = None
        selection = c.image_selection()
        self.assertEqual((selection.kind, selection.generator), ('off', None))
        self.assertNotIn('image_gen', [t['function']['name'] for t in c.tool_definitions()])

    async def test_unavailable_local_never_falls_back_to_paid(self):
        c = self.runtime.controller
        c.image_generator = AsyncMock()
        c.local_image_generator = None
        c.config.image_provider = 'local'
        self.assertEqual(c.image_selection().kind, 'off')
        self.assertIsNone(c._image_provider())
        self.assertEqual(c.image_provider_label, 'Local images unavailable')

    async def test_unavailable_openrouter_never_falls_back_to_local(self):
        c = self.runtime.controller
        c.image_generator = None
        c.local_image_generator = AsyncMock()
        c.config.image_provider = 'openrouter'
        self.assertEqual(c.image_selection().kind, 'off')
        self.assertIsNone(c._image_provider())

    async def test_paid_selection_names_the_configured_model(self):
        c = self.runtime.controller
        c.image_generator = AsyncMock()
        c.config.openrouter_image_models = ['bytedance-seed/seedream-5-0-pro', 'other/model']
        c.set_image_provider('openrouter', 'other/model')
        selection = c.image_selection()
        self.assertTrue(selection.paid)
        self.assertEqual(selection.model, 'other/model')
        self.assertEqual(c.config.image_model, 'other/model')
        with self.assertRaisesRegex(ValueError, 'configured OpenRouter'):
            c.set_image_provider('openrouter', 'not/listed')

    async def test_save_failure_rolls_back_and_surfaces_the_error(self):
        from blackrose.chat import PreferenceSaveError
        c = self.runtime.controller
        c.config.image_provider = 'local'
        with patch('blackrose.chat.save_preferences', side_effect=OSError('disk full')):
            with self.assertRaises(PreferenceSaveError):
                c.set_image_provider('off')
        self.assertEqual(c.config.image_provider, 'local')

    async def test_choice_cannot_change_while_generating(self):
        from blackrose.errors import ChatBusy
        c = self.runtime.controller
        c.is_generating = True
        try:
            with self.assertRaises(ChatBusy):
                c.set_image_provider('off')
        finally:
            c.is_generating = False
        self.assertEqual(c.config.image_provider, '')

    async def test_picker_explains_unavailable_options_and_refuses_them_server_side(self):
        c = self.runtime.controller
        c.image_generator = AsyncMock()
        c.local_image_generator = None
        with patch('blackrose.credentials.OpenRouterKeychain.has_key', return_value=False):
            state = await self.session.image_providers()
            options = {o['id']: o for o in state['options']}
            refused = await self.session.select_image_provider('openrouter')
            self.assertIn('error', refused)
        self.assertFalse(options['local']['available'])
        self.assertIn('not configured', options['local']['reason'])
        self.assertFalse(options['openrouter']['available'])
        self.assertIn('No OpenRouter API key', options['openrouter']['reason'])
        self.assertTrue(options['off']['available'])
        refused = await self.session.select_image_provider('local')
        self.assertIn('error', refused)
        self.assertEqual(c.config.image_provider, '')

    async def test_picker_reports_recovery_and_workflow_problems(self):
        c = self.runtime.controller
        c.local_image_generator = AsyncMock(recovery_error='Local image memory recovery failed: x')
        c.local_image_generator.workflow_problem = lambda: None
        c.image_generator = None
        state = await self.session.image_providers()
        self.assertEqual(state['options'][1]['reason'], 'Local image memory recovery failed: x')
        c.local_image_generator = AsyncMock(recovery_error=None)
        c.local_image_generator.workflow_problem = lambda: 'Local image workflows are missing'
        state = await self.session.image_providers()
        self.assertEqual(state['options'][1]['reason'], 'Local image workflows are missing')

    async def test_selection_persists_and_is_refused_while_a_reply_runs(self):
        c = self.runtime.controller
        c.local_image_generator = AsyncMock()
        c.image_generator = AsyncMock()
        with patch('blackrose.chat.save_preferences') as save:
            result = await self.session.select_image_provider('off')
        self.assertEqual(result['choice'], 'off')
        self.assertEqual(result['effective'], 'off')
        save.assert_called_once()
        c.is_generating = True
        try:
            busy = await self.session.select_image_provider('local')
        finally:
            c.is_generating = False
        self.assertEqual(busy, {'error': 'Wait for the reply to finish'})
        self.assertEqual(c.config.image_provider, 'off')

    def test_local_model_name_is_not_used_for_routing(self):
        source = (Path(__file__).resolve().parents[1] / 'src/blackrose/chat.py').read_text()
        self.assertNotIn('local_image_chat_model', source)


class ConfigTests(unittest.TestCase):
    def test_local_endpoint_cannot_be_remote(self):
        for endpoint in ('https://example.com', 'http://192.168.1.1:8189',
                         'http://secret@localhost:8189', 'http://localhost:8189/path'):
            with self.assertRaisesRegex(ValueError, 'loopback'):
                Config(local_image_endpoint=endpoint)


if __name__ == '__main__':
    unittest.main()

    def test_image_provider_and_model_are_validated(self):
        from blackrose.config import _valid_preference_value
        self.assertTrue(_valid_preference_value('image_provider', ''))
        self.assertTrue(_valid_preference_value('image_provider', 'openrouter'))
        self.assertFalse(_valid_preference_value('image_provider', 'paid'))
        self.assertFalse(_valid_preference_value('image_provider', None))
        with self.assertRaisesRegex(ValueError, 'image_provider'):
            Config(image_provider='paid')
        with self.assertRaisesRegex(ValueError, 'openrouter_image_models'):
            Config(openrouter_image_models=[])

    def test_image_model_outside_the_allow_list_falls_back(self):
        config = Config(image_model='not/listed', openrouter_image_models=['a/one', 'b/two'])
        self.assertEqual(config.image_model, 'a/one')
