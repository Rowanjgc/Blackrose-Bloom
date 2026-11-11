"""Content leaves the window only through a confirmed, fully described hand-off.

The page renders model-written links and offers saved outputs, so nothing it
calls may start another application by itself. Every launcher here is a mock:
no test opens a browser, file manager or dialog.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from blackrose.gui import external
from blackrose.gui.bridge import Api
from blackrose.gui.render import to_html
from test_security_boundaries import BoundaryTest

SRC = Path(__file__).resolve().parents[1] / 'src'
CANARY = 'SYNTHETIC-CANARY-7f3a9c'
NATIVE_ASK = external.ask  # exercised below with a fake GTK only
_UNSET_WINDOW = object()  # default marker; callers pass None to test a detached window


def refuse_launch(*args, **kwargs):
    raise AssertionError('a test reached a real launcher')


def setUpModule():
    # Tests patch the resolver and dialog per call; a regression that bypasses
    # them must fail here instead of opening something on the tester's desktop.
    for target in ('webbrowser.open', 'webbrowser.open_new', 'webbrowser.open_new_tab',
                   'blackrose.gui.external.handler_for', 'blackrose.gui.external.ask'):
        patcher = patch(target, refuse_launch)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def handler(name='Example Browser', role='your default web browser', file_type=None):
    return external.Handler(name, role, file_type, SimpleNamespace(launch_uris=Mock(return_value=True)))


class Recorder:
    """Stands in for the native dialog and records exactly what it showed."""

    def __init__(self, answer):
        self.answer = answer
        self.shown = []

    def __call__(self, window, title, message, button, *, warning=False, detail='', detail_label='', note=''):
        # ``text`` is everything the sheet shows, in reading order.
        text = '\n'.join((title, message, detail_label, detail, note))
        self.shown.append(SimpleNamespace(title=title, message=message, button=button, warning=warning,
                                          detail=detail, detail_label=detail_label, note=note, text=text))
        return self.answer


class LinkTests(unittest.TestCase):
    def open(self, url, answer=True, app=None, window=_UNSET_WINDOW):
        if window is _UNSET_WINDOW:
            window = object()
        app = app or handler()
        ask = Recorder(answer)
        api = SimpleNamespace(_window=window)
        with patch.object(external, 'handler_for', return_value=app) as resolve, \
                patch.object(external, 'ask', ask):
            result = Api.open_link(api, url)
        return result, ask, app, resolve

    def test_declined_links_launch_nothing(self):
        url = f'https://collector.example/track?d={CANARY}'
        result, ask, app, _ = self.open(url, answer=False)
        self.assertFalse(result)
        self.assertEqual(len(ask.shown), 1)
        app.app.launch_uris.assert_not_called()

    def test_only_an_explicit_accept_launches_the_exact_address_with_the_named_handler(self):
        url = f'https://collector.example/track?d={CANARY}#part'
        for answer in (None, 'yes', 1):
            result, _, app, _ = self.open(url, answer=answer)
            self.assertFalse(result)
            app.app.launch_uris.assert_not_called()
        result, ask, app, _ = self.open(url)
        self.assertTrue(result)
        app.app.launch_uris.assert_called_once_with([url], None)
        self.assertEqual(ask.shown[0].button, 'Open in Example Browser')

    def test_dialog_shows_site_full_address_handler_and_trust_note(self):
        url = 'https://bücher.example:8443/' + 'a' * 90 + f'?d={CANARY}'
        _, ask, _, _ = self.open(url, answer=False)
        shown = ask.shown[0]
        self.assertEqual(shown.title, 'Open this link outside Blackrose?')
        self.assertEqual(shown.detail_label, 'Full address, all of it sent to xn--bcher-kva.example:8443')
        self.assertEqual(shown.detail, url)
        self.assertEqual(shown.message, 'Opens with: Example Browser (your default web browser)')
        self.assertIn('can differ from where it leads', shown.note)
        self.assertFalse(shown.warning)

    def test_email_links_name_recipients_and_say_nothing_is_sent(self):
        url = f'mailto:someone@example.com?body={CANARY}'
        _, ask, app, _ = self.open(url, answer=False, app=handler('Mail', 'your default email app'))
        shown = ask.shown[0]
        self.assertIn('To: someone@example.com', shown.message)
        self.assertEqual(shown.detail, url)
        self.assertIn('Opens with: Mail (your default email app)', shown.message)
        self.assertIn('Nothing is sent until you send the draft', shown.note)

    def test_unreviewable_or_unsafe_links_are_refused_before_any_dialog(self):
        for url in ('javascript:alert(1)', 'file:///etc/passwd', 'data:text/html,<script>1</script>',
                    'vbscript:x', '/relative', 'https://', 'https://trusted.example@other.example/',
                    'https://user:pw@example.com/', 'https://exa mple.com/', 'https://example.com/‮gpj.exe',
                    'https://example.com/\x00', 'https://example.com:99999/', 'mailto:',
                    'https://example.com/' + 'a' * external.MAX_ADDRESS, ''):
            with self.subTest(url=url):
                with patch.object(external, 'handler_for') as resolve, patch.object(external, 'ask') as ask:
                    with self.assertRaises(ValueError):
                        Api.open_link(SimpleNamespace(_window=object()), url)
                resolve.assert_not_called()
                ask.assert_not_called()

    def test_without_a_window_or_handler_nothing_is_opened(self):
        result, ask, app, resolve = self.open('https://example.com/', window=None)
        self.assertFalse(result)
        self.assertEqual(ask.shown, [])
        resolve.assert_not_called()
        with patch.object(external, 'handler_for', return_value=None), \
                patch.object(external, 'ask') as asked, self.assertRaisesRegex(ValueError, 'No application'):
            Api.open_link(SimpleNamespace(_window=object()), 'https://example.com/')
        asked.assert_not_called()

    def test_a_second_request_is_refused_while_one_waits_for_an_answer(self):
        waiting, release = threading.Event(), threading.Event()

        def ask(*args, **kwargs):
            waiting.set()
            release.wait(5)
            return False

        app = handler()
        with patch.object(external, 'handler_for', return_value=app), patch.object(external, 'ask', ask):
            first = threading.Thread(target=external.hand_off, args=(object(), external.link('https://a.example/')))
            first.start()
            self.assertTrue(waiting.wait(5))
            with self.assertRaisesRegex(ValueError, 'waiting for your answer'):
                external.hand_off(object(), external.link('https://b.example/'))
            release.set()
            first.join(5)
        app.app.launch_uris.assert_not_called()
        # The lock is released after an answer.
        with patch.object(external, 'handler_for', return_value=app), \
                patch.object(external, 'ask', Recorder(False)):
            self.assertFalse(external.hand_off(object(), external.link('https://c.example/')))

    def test_launch_failures_are_reported(self):
        app = handler()
        app.app.launch_uris.side_effect = OSError('no display')
        with patch.object(external, 'handler_for', return_value=app), \
                patch.object(external, 'ask', Recorder(True)), self.assertRaisesRegex(ValueError, 'could not open'):
            external.hand_off(object(), external.link('https://example.com/'))

    def test_logs_name_the_site_but_not_the_address(self):
        with self.assertLogs('blackrose.gui.external', 'INFO') as logs:
            self.open(f'https://collector.example/?d={CANARY}', answer=False)
            self.open(f'https://collector.example/?d={CANARY}')
        self.assertTrue(all('collector.example' in line for line in logs.output))
        self.assertNotIn(CANARY, '\n'.join(logs.output))


class NativeDialogTests(unittest.TestCase):
    class Gtk:
        class DialogFlags:
            MODAL, DESTROY_WITH_PARENT = 1, 2

        class MessageType:
            QUESTION, WARNING = 'question', 'warning'

        class ButtonsType:
            NONE = 'none'

        class ResponseType:
            CANCEL, ACCEPT, DELETE_EVENT = -6, -3, -4

    class Dialog:
        def __init__(self, response, **kwargs):
            self.response, self.kwargs = response, kwargs
            self.buttons, self.default, self.destroyed = [], None, False

        def set_title(self, title):
            self.title = title

        def format_secondary_text(self, text):
            self.detail = text

        def add_button(self, label, response):
            self.buttons.append((label, response))

        def set_default_response(self, response):
            self.default = response

        def run(self):
            return self.response

        def destroy(self):
            self.destroyed = True

    def ask(self, response, warning=False, threaded=False):
        dialogs = []
        gtk = SimpleNamespace(**{k: getattr(self.Gtk, k) for k in
                                 ('DialogFlags', 'MessageType', 'ButtonsType', 'ResponseType')})
        gtk.MessageDialog = lambda **kwargs: dialogs.append(self.Dialog(response, **kwargs)) or dialogs[-1]
        glib = SimpleNamespace(SOURCE_REMOVE=False, idle_add=lambda callback: callback())
        window = SimpleNamespace(native='native window')

        def call():
            return NATIVE_ASK(window, 'Title?', 'Message', 'Open in My_App', warning=warning, gtk=gtk, glib=glib)
        if threaded:
            result = []
            thread = threading.Thread(target=lambda: result.append(call()))
            thread.start()
            thread.join(5)
            return result[0], dialogs[0]
        return call(), dialogs[0]

    def test_cancel_is_the_default_and_only_accept_opens(self):
        for response, expected in ((self.Gtk.ResponseType.ACCEPT, True), (self.Gtk.ResponseType.CANCEL, False),
                                   (self.Gtk.ResponseType.DELETE_EVENT, False)):
            result, dialog = self.ask(response)
            self.assertIs(result, expected)
            self.assertEqual(dialog.default, self.Gtk.ResponseType.CANCEL)
            self.assertEqual(dialog.buttons, [('Cancel', self.Gtk.ResponseType.CANCEL),
                                              ('Open in My__App', self.Gtk.ResponseType.ACCEPT)])
            self.assertEqual(dialog.kwargs['transient_for'], 'native window')
            self.assertEqual((dialog.kwargs['text'], dialog.detail), ('Title?', 'Message'))
            self.assertTrue(dialog.destroyed)

    def test_active_pages_get_a_warning_dialog(self):
        self.assertEqual(self.ask(self.Gtk.ResponseType.CANCEL)[1].kwargs['message_type'], 'question')
        self.assertEqual(self.ask(self.Gtk.ResponseType.CANCEL, warning=True)[1].kwargs['message_type'], 'warning')

    def test_worker_threads_wait_for_the_gtk_thread_answer(self):
        result, dialog = self.ask(self.Gtk.ResponseType.ACCEPT, threaded=True)
        self.assertTrue(result)
        self.assertTrue(dialog.destroyed)


class NavigationPolicyTests(unittest.TestCase):
    """No frame navigates away, opens a window or downloads; nothing is launched."""

    def install(self):
        class NavigationPolicyDecision:
            def __init__(self, uri):
                self.uri, self.decided = uri, None

            def get_navigation_action(self):
                return SimpleNamespace(get_request=lambda: SimpleNamespace(get_uri=lambda: self.uri),
                                       get_frame_name=lambda: '_blank')

            def ignore(self):
                self.decided = 'ignore'

        class ResponsePolicyDecision:
            def __init__(self, supported):
                self.supported, self.decided = supported, None

            def is_mime_type_supported(self):
                return self.supported

            def use(self):
                self.decided = 'use'

            def ignore(self):
                self.decided = 'ignore'

            def download(self):
                self.decided = 'download'

        webkit = SimpleNamespace(
            NavigationPolicyDecision=NavigationPolicyDecision, ResponsePolicyDecision=ResponsePolicyDecision,
            PolicyDecisionType=SimpleNamespace(NAVIGATION_ACTION=0, NEW_WINDOW_ACTION=1, RESPONSE=2),
            UserScript=SimpleNamespace(new=lambda *args: None),
            UserContentInjectedFrames=SimpleNamespace(TOP_FRAME=0),
            UserScriptInjectionTime=SimpleNamespace(START=0))
        handlers, disconnected = {}, []
        webview = SimpleNamespace(connect=lambda signal, fn: handlers.setdefault(signal, fn),
                                  disconnect_by_func=disconnected.append)
        browser = SimpleNamespace(webview=webview, on_navigation=object(), on_js_bridge_call=object(),
                                  manager=SimpleNamespace(disconnect_by_func=lambda fn: None,
                                                          connect=lambda *a: None, add_script=lambda s: None))
        class Event(list):
            def __iadd__(self, handler):
                self.append(handler)
                return self

        page = 'file:///app/static/index.html'
        window = SimpleNamespace(uid='main', real_url=page, events=SimpleNamespace(before_show=Event()),
                                 gui=SimpleNamespace(BrowserView=SimpleNamespace(instances={'main': browser})))
        gi, repository = ModuleType('gi'), ModuleType('gi.repository')
        repository.WebKit2 = webkit
        with patch.dict(sys.modules, {'gi': gi, 'gi.repository': repository}):
            from blackrose.gui.frame_isolation import protect_desktop_bridge
            protect_desktop_bridge(window)
            for install in window.events.before_show:
                install()
        return handlers['decide-policy'], webkit, disconnected, browser, page

    def test_pywebview_browser_handoff_is_replaced(self):
        policy, webkit, disconnected, browser, page = self.install()
        self.assertEqual(disconnected, [browser.on_navigation])
        kinds = webkit.PolicyDecisionType
        with patch('webbrowser.open') as opened:
            for uri, kind, expected in (
                    ('https://collector.example/?d=1', kinds.NEW_WINDOW_ACTION, 'ignore'),
                    (page, kinds.NEW_WINDOW_ACTION, 'ignore'),
                    ('https://collector.example/', kinds.NAVIGATION_ACTION, 'ignore'),
                    ('file:///etc/passwd', kinds.NAVIGATION_ACTION, 'ignore'),
                    (page, kinds.NAVIGATION_ACTION, None),
                    ('about:srcdoc', kinds.NAVIGATION_ACTION, None),
                    ('file:///app/static/motion-canvas-host.html', kinds.NAVIGATION_ACTION, None)):
                decision = webkit.NavigationPolicyDecision(uri)
                handled = policy(None, decision, kind)
                self.assertEqual((decision.decided, handled), (expected, expected is not None), uri)
            for supported, expected in ((True, 'use'), (False, 'ignore')):
                decision = webkit.ResponsePolicyDecision(supported)
                self.assertTrue(policy(None, decision, kinds.RESPONSE))
                self.assertEqual(decision.decided, expected)
        opened.assert_not_called()


class ArtifactHandoffTests(BoundaryTest):
    """Saved outputs open elsewhere only after a dialog naming the path and app."""

    async def open(self, filename, answer=True, reveal=False, content='<script>fetch("https://x.example")</script>'):
        await self.runtime.controller.set_workspace(None)
        c = self.backend([[('document_create', {'path': filename, 'content': content})], 'Saved.'])
        await c.send('Save it')
        await c.cancel_title()
        attachment = (await c.current_messages())[-1].attachments[-1]
        loop = asyncio.get_running_loop()
        api = Api.__new__(Api)
        api._runtime = self.runtime
        api._loop = SimpleNamespace(run=lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(5))
        api._window = object()
        ask = Recorder(answer)
        app = handler('Example App', 'your default app for this type', 'Example type')
        with patch.object(external, 'handler_for', return_value=app) as resolve, patch.object(external, 'ask', ask):
            result = await asyncio.to_thread(api.open_artifact, attachment.id, reveal)
        return result, ask, app, resolve, Path(attachment.output_path)

    async def test_html_is_marked_active_and_opens_only_after_a_warning(self):
        result, ask, app, resolve, path = await self.open('page.html', answer=False)
        self.assertFalse(result)
        app.app.launch_uris.assert_not_called()
        shown = ask.shown[0]
        self.assertTrue(shown.warning)
        self.assertEqual(resolve.call_args.args[0].kind, 'page')
        self.assertEqual(shown.title, "Open this web page outside Blackrose's sandbox?")
        self.assertIn('active web page, not a document', shown.message)
        self.assertIn('run scripts', shown.message)
        self.assertEqual(shown.detail, str(path))
        self.assertIn('Opens with: Example App (your default app for this type)', shown.note)
        result, _, app, _, path = await self.open('accepted.html')
        self.assertTrue(result)
        app.app.launch_uris.assert_called_once_with([path.as_uri()], None)

    async def test_passive_documents_are_confirmed_without_the_active_warning(self):
        result, ask, app, _, path = await self.open('notes.md', content='# Notes')
        self.assertTrue(result)
        shown = ask.shown[0]
        self.assertFalse(shown.warning)
        self.assertEqual(shown.title, 'Open this file outside Blackrose?')
        self.assertNotIn('active web page', shown.text)
        self.assertEqual(shown.detail, str(path))
        self.assertEqual(shown.detail_label, 'File (Example type)')
        app.app.launch_uris.assert_called_once_with([path.as_uri()], None)

    async def test_show_folder_names_the_folder_and_launches_only_on_accept(self):
        result, ask, app, resolve, path = await self.open('notes.md', answer=False, reveal=True, content='# N')
        self.assertFalse(result)
        app.app.launch_uris.assert_not_called()
        self.assertEqual(resolve.call_args.args[0].kind, 'folder')
        self.assertEqual(ask.shown[0].detail, str(path.parent))
        self.assertEqual(ask.shown[0].button, 'Show in Example App')
        result, _, app, _, path = await self.open('other.md', reveal=True, content='# N')
        app.app.launch_uris.assert_called_once_with([path.parent.as_uri()], None)

    async def test_scripts_are_never_handed_to_another_application(self):
        with self.assertRaisesRegex(ValueError, 'source preview'):
            await self.open('tool.py', content='print(1)')

    async def test_sandboxed_html_preview_is_unchanged(self):
        await self.open('page.html', answer=False)
        c = self.runtime.controller
        attachment = (await c.current_messages())[-1].attachments[-1]
        loop = asyncio.get_running_loop()
        api = Api.__new__(Api)
        api._runtime = self.runtime
        api._loop = SimpleNamespace(run=lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(5))
        preview = await asyncio.to_thread(api.artifact_preview, attachment.id)
        self.assertEqual(set(preview), {'document'})
        page = (SRC / 'blackrose/gui/static/bloom.js').read_text()
        self.assertIn("frame.setAttribute('sandbox', '');", page)
        self.assertIn("default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'", page)


class RenderedDestinationTests(unittest.TestCase):
    def test_links_show_their_destination_not_the_model_title(self):
        html = to_html('[Your bank](https://bücher.example/login?d=1 "Official, verified")')
        self.assertIn('title="https://xn--bcher-kva.example/login?d=1"', html)
        self.assertNotIn('Official, verified', html)

    def test_remote_images_expose_the_full_address_before_loading(self):
        html = to_html(f'![Chart](https://bücher.example/c.png?d={CANARY})')
        self.assertIn('aria-label="Load image from xn--bcher-kva.example"', html)
        self.assertIn(f'title="Loads https://xn--bcher-kva.example/c.png?d={CANARY}"', html)
        self.assertNotIn('<img', html)

    def test_no_launcher_bypasses_the_confirmed_hand_off(self):
        launchers = ('webbrowser', 'xdg-open', 'launch_uris', 'launch_default_for_uri', 'show_uri', 'gio open')
        for path in (SRC / 'blackrose').rglob('*.py'):
            if path.name == 'external.py':
                continue
            text = path.read_text()
            for name in launchers:
                self.assertNotIn(name, text, f'{path} uses {name}')


if __name__ == '__main__':
    unittest.main()
