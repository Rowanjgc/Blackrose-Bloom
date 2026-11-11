"""Image previews in the desktop package used by the installed launcher."""
from __future__ import annotations

import base64
import os
import threading
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from blackrose.attachments import AttachmentStore
from blackrose.chat import ChatController
from blackrose.config import Config
from blackrose.gui.render import to_html
from blackrose.gui.session import Session
from blackrose.runtime import Runtime
from blackrose.skills import SkillManager
from blackrose.storage.db import Database
from blackrose.storage.repository import Repository

# A real PNG, with dimensions large enough to see in a thumbnail.
def png(width=320, height=200):
    import struct
    import zlib
    def chunk(tag, payload):
        return struct.pack('>I', len(payload)) + tag + payload + struct.pack('>I', zlib.crc32(tag + payload) & 0xffffffff)
    rows = b''.join(b'\0' + bytes([y % 256]) * width for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')

class Backend:
    async def load(self, model):
        pass
    async def models(self, provider=None):
        return [{'id': 'test-vision', 'architecture': {'input_modalities': ['text', 'image']}}]
    async def stream(self, model, messages, tools):
        yield {'delta': {'content': 'A framed image is visible.'}, 'finish_reason': 'stop'}
    async def aclose(self):
        pass

class Service:
    async def close(self):
        pass

def make_runtime(root):
    config = Config(default_model='test-vision', knowledge_enabled=False, web_enabled=False)
    database = Database(root / 'test.db')
    controller = ChatController(Backend(), Repository(database), config, Service(),
        SkillManager(root / 'skills'), Service(), AttachmentStore(root / 'attachments'))
    return config, Runtime(controller, database)

class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Preference saves go to config_dir(); keep them out of the developer's real settings.
        self.environment = patch.dict(os.environ, {
            name: str(self.root / child) for name, child in (
                ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state'))})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config, self.runtime = make_runtime(self.root)
        await self.runtime.open()
        self.events = []
        self.session = Session(self.runtime.controller, lambda kind, payload: self.events.append((kind, payload)))
        await self.session.boot()
    async def asyncTearDown(self):
        await self.session.close()
        await self.runtime.close()
        self.temp.cleanup()
    def latest(self, kind):
        return next(payload for event, payload in reversed(self.events) if event == kind)
    async def test_attachment_shows_a_preview_before_send_and_after_reopen(self):
        image = png()
        self.assertTrue(await self.session.attach_encoded('garden.png', base64.b64encode(image).decode()))
        preview = self.latest('attachments')['items'][0]
        self.assertTrue(preview['src'].startswith('data:image/png;base64,'))
        self.assertEqual((preview['width'], preview['height']), (320, 200))
        self.assertFalse((self.root / 'attachments').exists())
        self.assertEqual(await self.session.submit('Describe this image'), {'clear': True})
        await self.session._generation
        self.assertEqual(self.latest('user_message')['attachments'][0]['src'], preview['src'])
        await self.session.render_transcript()
        saved = self.latest('transcript')['messages'][0]['attachments'][0]
        self.assertIn('preview_id', saved)
        self.assertNotIn('src', saved)
        self.assertEqual((await self.session.image_preview(saved['preview_id']))['src'], preview['src'])
    async def test_file_attachment_uses_the_same_preview(self):
        path = self.root / 'sample.png'
        path.write_bytes(png())
        self.assertTrue(await self.session.attach_path(str(path)))
        self.assertIn('src', self.latest('attachments')['items'][0])
    async def test_path_probes_run_off_the_session_loop(self):
        loop_thread = threading.get_ident()
        probe_threads = []

        def absent(_path):
            probe_threads.append(threading.get_ident())
            return False

        with patch.object(Path, 'is_file', absent), patch.object(Path, 'is_dir', absent), \
             patch.object(self.session, 'run_command', new_callable=AsyncMock):
            self.assertFalse(await self.session.attach_path('missing.png', gesture=True))
            self.assertEqual(await self.session.submit('/missing-command'), {'clear': True})
        self.assertEqual(len(probe_threads), 2)
        self.assertTrue(all(thread != loop_thread for thread in probe_threads))
    async def test_unknown_and_removed_previews_are_unavailable(self):
        self.assertIsNone(await self.session.image_preview(999))
        await self.session.attach_bytes(png(), 'garden.png')
        await self.session.submit('Describe')
        await self.session._generation
        await self.session.render_transcript()
        attachment = (await self.runtime.controller.current_messages())[0].attachments[0]
        self.runtime.controller._store.delete(attachment.storage_path)
        self.assertIsNone(await self.session.image_preview(attachment.id))
    async def test_preview_paths_cannot_escape_the_attachment_store(self):
        await self.session.attach_bytes(png(), 'garden.png')
        await self.session.submit('Describe')
        await self.session._generation
        attachment = (await self.runtime.controller.current_messages())[0].attachments[0]
        private = self.root / 'private.png'
        private.write_bytes(png())
        for path in ('../private.png', str(private)):
            self.assertNotIn('preview_id', self.session._attachment_payload(replace(attachment, storage_path=path)))
    async def test_document_attachments_stay_documents(self):
        self.assertTrue(await self.session.attach_bytes(b'A short document.', 'notes.txt'))
        attachment = self.latest('attachments')['items'][0]
        self.assertEqual(attachment['kind'], 'document')
        self.assertNotIn('src', attachment)

class MarkdownTests(unittest.TestCase):
    def test_remote_images_require_a_click(self):
        html = to_html('![Garden](https://images.example.com/garden.png)')
        self.assertIn('class="image-frame"', html)
        self.assertIn('class="image-load"', html)
        self.assertNotIn('<img', html)
    def test_local_paths_are_not_model_readable(self):
        for source in ('file:///etc/passwd', '../private.png', 'javascript:alert(1)'):
            self.assertNotIn('data-image-src=', to_html(f'![Image]({source})'))
    def test_alt_text_cannot_inject_markup(self):
        self.assertNotIn('<img', to_html('![<img src=x onerror=alert(1)>](https://example.com/image.png)'))

if __name__ == '__main__':
    unittest.main()
