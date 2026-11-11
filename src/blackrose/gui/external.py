"""Hand-offs from the window to other applications, after the user confirms.

Links, saved files and folders shown in a conversation may come from the
assistant, a web page or a file. Another application gives them more than the
window does: a browser sends the whole address to its site, and an HTML file
opened there can run scripts and reach the network, which the sandboxed preview
blocks. So each hand-off names its destination and the application that will
receive it in a native dialog, which page script cannot answer, and nothing
starts unless the user picks that application. Cancel is the default answer.

The page only passes a link or an attachment; the destination, the handler and
the wording are all decided here.
"""
from __future__ import annotations

import logging
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ..outbound import ascii_host, visible

log = logging.getLogger(__name__)

# Long enough for real links, short enough to read in full before opening.
MAX_ADDRESS = 2048
OPENABLE = frozenset({'.pdf', '.md', '.txt', '.html', '.htm', '.json', '.png', '.jpg', '.webp'})
# Pages that run scripts and load remote content in an ordinary browser.
ACTIVE = frozenset({'.html', '.htm'})
_SCHEMES = {'http': 'link', 'https': 'link', 'mailto': 'email'}
_HIDDEN = frozenset({'Cc', 'Cf', 'Zl', 'Zp', 'Zs'})
_NOUNS = {'link': 'web links', 'email': 'email links', 'file': 'this kind of file',
          'page': 'web pages', 'folder': 'folders'}
# One question at a time; a second request is refused rather than queued.
_asking = threading.Lock()


@dataclass(frozen=True)
class Handoff:
    kind: str         # 'link', 'email', 'file', 'page' (active HTML) or 'folder'
    uri: str          # exactly what the receiving application is given
    destination: str  # the full address or absolute path the user reviews
    host: str | None = None

    @property
    def active(self) -> bool:
        return self.kind == 'page'


@dataclass(frozen=True)
class Handler:
    name: str              # the application's display name
    role: str              # why the desktop chose it
    file_type: str | None  # a readable type for files, such as 'HTML document'
    app: Any               # the Gio.AppInfo that is launched


def link(url: str) -> Handoff:
    """An http(s) or mailto link the user can review in full, or ValueError."""
    if not isinstance(url, str) or not url or len(url) > MAX_ADDRESS:
        raise ValueError('This link is empty or too long to show for review. Nothing was opened.')
    if any(unicodedata.category(c) in _HIDDEN for c in url):
        raise ValueError('This link contains spaces or hidden characters. Nothing was opened.')
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
    except ValueError:
        raise ValueError('This link is not a valid address. Nothing was opened.') from None
    kind = _SCHEMES.get(scheme)
    if kind is None:
        raise ValueError('Only web (http, https) and email (mailto) links open outside Blackrose.')
    if kind == 'email':
        if not parts.path:
            raise ValueError('This email link has no recipient. Nothing was opened.')
        return Handoff('email', url, url)
    if not host:
        raise ValueError('This link names no site. Nothing was opened.')
    if parts.username is not None or parts.password is not None:
        # "https://trusted.example@other.example/" leads to other.example.
        raise ValueError('Links that carry a user name or password are not opened.')
    return Handoff('link', url, url, ascii_host(host) + (f':{port}' if port else ''))


def saved_file(path: Path) -> Handoff:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in OPENABLE:
        raise ValueError('Use source preview for scripts and structured text')
    if not path.is_absolute() or not path.is_file():
        raise ValueError('File is missing')
    return Handoff('page' if suffix in ACTIVE else 'file', path.as_uri(), str(path))


def folder(path: Path) -> Handoff:
    path = Path(path)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('Folder is missing')
    return Handoff('folder', path.as_uri(), str(path))


def handler_for(handoff: Handoff) -> Handler | None:
    """The application the desktop would choose; it is the one shown and launched."""
    from gi.repository import Gio

    file_type = None
    if handoff.kind in ('link', 'email'):
        app = Gio.AppInfo.get_default_for_uri_scheme(urlsplit(handoff.uri).scheme.lower())
        role = 'your default email app' if handoff.kind == 'email' else 'your default web browser'
    elif handoff.kind == 'folder':
        app = Gio.AppInfo.get_default_for_type('inode/directory', False)
        role = 'your default app for folders'
    else:
        # By name, like the extension check in saved_file.
        content_type, _ = Gio.content_type_guess(Path(handoff.destination).name, None)
        app = Gio.AppInfo.get_default_for_type(content_type, False)
        file_type = visible(Gio.content_type_get_description(content_type) or content_type)
        role = 'your default app for this type'
    if app is None:
        return None
    name = app.get_display_name() or app.get_name() or 'an unnamed application'
    return Handler(visible(name), role, file_type, app)


@dataclass(frozen=True)
class Confirmation:
    """What the approval sheet shows for a hand-off, most important first."""
    title: str
    message: str       # the warning, or which application opens it
    detail_label: str  # what the destination is
    detail: str        # the full address or path, hidden characters shown
    note: str          # when to open it
    button: str


def confirmation(handoff: Handoff, handler: Handler) -> Confirmation:
    """The question for one hand-off, split for display."""
    opens = f'Opens with: {handler.name} ({handler.role})'
    button = f'Open in {handler.name}'
    where = visible(handoff.destination)
    if handoff.kind == 'link':
        return Confirmation('Open this link outside Blackrose?', opens,
                            f'Full address, all of it sent to {handoff.host}', where,
                            'The assistant, a web page or a file may have written this link, and the text '
                            'you clicked can differ from where it leads. Open it only if you trust the site '
                            'and the address contains nothing private.', button)
    if handoff.kind == 'email':
        recipients = visible(unquote(urlsplit(handoff.uri).path))
        return Confirmation('Open this email link outside Blackrose?', f'To: {recipients}\n{opens}',
                            'Full link; your email app starts a draft from all of it', where,
                            'The assistant, a web page or a file may have written this link. Nothing is sent '
                            'until you send the draft; check its recipients and text first.', button)
    if handoff.kind == 'folder':
        return Confirmation('Show this folder outside Blackrose?', opens, 'Folder', where,
                            'Only the folder is shown; nothing in it is opened or run.', f'Show in {handler.name}')
    if handoff.active:
        return Confirmation("Open this web page outside Blackrose's sandbox?",
                            'Warning: this is an active web page, not a document. Outside Blackrose it can '
                            'run scripts, load content from the internet and send what it contains to other '
                            'sites. Preview in Blackrose blocks all of that.',
                            f'File ({handler.file_type})', where,
                            f'{opens}. The assistant may have written this page. Open it only if you have '
                            'read it and trust it; otherwise use Preview.', button)
    return Confirmation('Open this file outside Blackrose?', opens, f'File ({handler.file_type})', where,
                        'This file was saved in a conversation, so the assistant or an untrusted source may '
                        'have written it. Preview in Blackrose runs nothing and loads nothing from the '
                        'internet; another app may follow its links.', button)


def ask(window, title: str, message: str, button: str, *, warning: bool = False, wider: str | None = None,
        detail: str = '', detail_label: str = '', note: str = '', choices: list[str] | None = None,
        gtk: Any = None, glib: Any = None) -> bool | str | tuple[bool, ...]:
    """Ask natively, outside page script's reach; only an explicit accept returns True.

    The question appears in the window's approval sheet (``consent``). A modal
    dialog window is the fallback when the sheet is not installed. ``wider``
    labels a second accept, which returns ``consent.WIDER``. ``detail`` is exactly what
    is sent or opened, shown apart under ``detail_label``, and ``note`` closes the question.
    ``choices`` lists several exact payloads, each with a checkbox, checked at first;
    accepting returns one bool per choice, and the dialog fallback accepts them all.
    """
    from .consent import WIDER
    sheet = getattr(window, '_blackrose_consent', None)
    if sheet is not None and gtk is None:
        return sheet.ask(title, message, button, warning=warning, wider=wider,
                         detail=detail, detail_label=detail_label, note=note, choices=choices)
    if choices:
        detail = '\n\n'.join(choices)
        accepted = ask(window, title, message, button.replace('checked', 'all'), warning=warning,
                       detail=detail, detail_label=detail_label, note=note, gtk=gtk, glib=glib)
        return (True,) * len(choices) if accepted is True else False
    # The dialog shows one message, so the parts are joined in reading order.
    message = '\n\n'.join(part for part in (message, f'{detail_label}:\n{detail}' if detail_label else detail,
                                             note) if part)
    if gtk is None or glib is None:
        from gi.repository import GLib, Gtk
        gtk, glib = gtk or Gtk, glib or GLib
    answer: bool | str = False
    done = threading.Event()

    def show():
        nonlocal answer
        try:
            dialog = gtk.MessageDialog(
                transient_for=getattr(window, 'native', None),
                flags=gtk.DialogFlags.MODAL | gtk.DialogFlags.DESTROY_WITH_PARENT,
                message_type=gtk.MessageType.WARNING if warning else gtk.MessageType.QUESTION,
                buttons=gtk.ButtonsType.NONE,
                text=title,
            )
            dialog.set_title('Blackrose Bloom')
            dialog.format_secondary_text(message)
            dialog.add_button('Cancel', gtk.ResponseType.CANCEL)
            # Button labels read underscores as mnemonics.
            if wider:
                dialog.add_button(wider.replace('_', '__'), gtk.ResponseType.YES)
            dialog.add_button(button.replace('_', '__'), gtk.ResponseType.ACCEPT)
            # Enter and Escape both decline; opening takes a deliberate choice.
            dialog.set_default_response(gtk.ResponseType.CANCEL)
            try:
                response = dialog.run()
                answer = True if response == gtk.ResponseType.ACCEPT else (
                    WIDER if wider and response == gtk.ResponseType.YES else False)
            finally:
                dialog.destroy()
        finally:
            done.set()
        return glib.SOURCE_REMOVE

    if threading.current_thread() is threading.main_thread():
        show()
    else:
        glib.idle_add(show)
        done.wait()
    return answer


def launch(handler: Handler, handoff: Handoff) -> None:
    try:
        started = handler.app.launch_uris([handoff.uri], None)
    except Exception as error:  # GLib.Error from the launcher
        raise ValueError(f'{handler.name} could not open it: {error}') from None
    if not started:
        raise ValueError(f'{handler.name} could not open it.')


def hand_off(window, handoff: Handoff) -> bool:
    """Name the destination and handler, ask, and launch only if the user accepts."""
    if window is None:
        return False
    handler = handler_for(handoff)
    if handler is None:
        raise ValueError(f'No application is set to open {_NOUNS[handoff.kind]}. Nothing was opened.')
    if not _asking.acquire(blocking=False):
        raise ValueError('Another request to open something outside Blackrose is waiting for your answer.')
    try:
        shown = confirmation(handoff, handler)
        accepted = ask(window, shown.title, shown.message, shown.button, warning=handoff.active,
                       detail=shown.detail, detail_label=shown.detail_label, note=shown.note) is True
    finally:
        _asking.release()
    # Log the kind, site and application only; addresses and paths may be private.
    site = f' ({handoff.host})' if handoff.host else ''
    if not accepted:
        log.info('Declined opening a %s%s outside Blackrose', handoff.kind, site)
        return False
    launch(handler, handoff)
    log.info('Opened a %s%s with %s', handoff.kind, site, handler.name)
    return True
