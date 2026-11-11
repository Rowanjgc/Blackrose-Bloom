"""Native approval sheet shown inside the main window, above the page.

Approvals must stay out of reach of page script: model output, Markdown and
generated frames all share the webview. A separate dialog window does that,
but tiling window managers place it as a new tile and pywebview's own dialog
is not even modal. This sheet is a GTK widget in a ``Gtk.Overlay`` over the
WebKit view, so it is in the same toplevel and still outside the DOM. While it
is open the webview is insensitive, so clicks and keys cannot reach the page.

Keyboard: Escape, D or N declines; A or Y accepts; S picks the wider accept
when a question offers one (allowing a whole site). A question about several
requests lists each with a checkbox, checked at first: 1 to 9 toggle one, Space
toggles the focused one, and accepting allows only those still checked. Enter never accepts, and
accepting stays disarmed for ``ARM_DELAY_MS`` after the sheet appears or the
window regains focus, so a keystroke meant for the composer, or a click aimed
at whatever was there before, cannot approve a request that just appeared.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

ARM_DELAY_MS = 400
# How long a worker waits for the queued show before checking for a closed window.
_CLOSE_POLL_SECONDS = 0.1
_DECLINE_KEYS = frozenset(('Escape', 'd', 'D', 'n', 'N'))
_ACCEPT_KEYS = frozenset(('a', 'A', 'y', 'Y'))
_WIDER_KEYS = frozenset(('s', 'S'))
WIDER = 'wider'  # the answer for a question's wider accept
_FOCUS_KEYS = frozenset(('Tab', 'ISO_Left_Tab', 'Left', 'Right', 'Up', 'Down'))

WIDTH = 600  # the sheet's widest; narrower windows keep a 16 px margin each side
WELL_MAX = 200  # the payload box's tallest before it scrolls
CHOICES_MAX = 320  # the same, for a list of requests
CHECK = 28  # a checkbox's indicator and spacing beside its text

# Gothic Bloom tokens (GOTHIC_BLOOM_DESIGN_LANGUAGE.md, gui/static/bloom.css); the app has one dark
# theme. GTK cannot use the page's bundled Libron, so the sheet takes it when installed, else a serif.
CSS = b'''
#blackrose-consent-scrim { background-color: rgba(0, 0, 0, 0.6); }
#blackrose-consent {
  font-family: Libron, "Noto Serif", Georgia, serif;
  background-color: #1a1a1a;
  color: #e0e0e0;
  border: 1px solid #333333;
  border-top: none;
  border-radius: 0 0 20px 20px;
  padding: 24px 24px 20px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6);
}
#blackrose-consent.warning { border-color: #b8506b; }
#blackrose-consent .consent-title { font-weight: bold; font-size: 1.3em; color: #e0e0e0; }
#blackrose-consent .consent-message { color: #b3b3b3; }
#blackrose-consent .consent-well {
  background-color: #0d0d0d;
  border: 1px solid #333333;
  border-radius: 10px;
  padding: 12px 16px;
}
#blackrose-consent .consent-caption { color: #b3b3b3; font-size: 0.875em; }
#blackrose-consent .consent-payload {
  font-family: "JetBrains Mono", "Cascadia Code", "Source Code Pro", monospace;
  font-size: 0.9em;
  color: #e0e0e0;
}
#blackrose-consent .consent-note { color: #b3b3b3; font-size: 0.875em; }
#blackrose-consent checkbutton { padding: 4px 0; outline-color: #e0e0e0; }
#blackrose-consent check {
  background-image: none; background-color: #0d0d0d; border: 1px solid #6e6e6e; border-radius: 4px;
  min-width: 14px; min-height: 14px; color: #e0e0e0;
}
#blackrose-consent check:checked { background-color: #ef6461; border-color: #ef6461; color: #0d0d0d; }
#blackrose-consent scrolledwindow, #blackrose-consent viewport { background-color: transparent; border: none; }
#blackrose-consent scrollbar { background-color: transparent; border: none; margin-left: 6px; }
#blackrose-consent scrollbar slider {
  background-color: #6e6e6e; border: none; border-radius: 4px; min-width: 6px; min-height: 24px;
}
#blackrose-consent scrollbar slider:hover { background-color: #b3b3b3; }
#blackrose-consent button {
  background-image: none;
  background-color: #121212;
  color: #e0e0e0;
  border: 1px solid #6e6e6e;
  border-radius: 10px;
  padding: 8px 16px;
  box-shadow: none;
  text-shadow: none;
  outline-color: #e0e0e0;
}
#blackrose-consent button:hover { background-color: #222222; }
#blackrose-consent button:focus { border-color: #e0e0e0; }
#blackrose-consent button.consent-accept { border-color: #ef6461; }
#blackrose-consent button:disabled, #blackrose-consent button.consent-accept:disabled {
  color: #9a9a9a; border-color: #333333;
}
#blackrose-consent .consent-key {
  color: #b3b3b3;
  font-size: 0.8em;
  border: 1px solid #6e6e6e;
  border-radius: 5px;
  padding: 0 6px;
}
#blackrose-consent button:disabled .consent-key { color: #9a9a9a; border-color: #333333; }
'''


class ConsentSheet:
    """One in-window question at a time; callers on other threads wait their turn."""

    def __init__(self, toplevel, webview, gtk: Any, glib: Any, gdk: Any, atk: Any = None):
        self._gtk, self._glib, self._gdk = gtk, glib, gdk
        self._toplevel, self._webview = toplevel, webview
        self._turn = threading.Lock()
        self._current: dict | None = None
        self._closed = False

        provider = gtk.CssProvider()
        provider.load_from_data(CSS)
        self._css = provider

        content = toplevel.get_child()
        toplevel.remove(content)
        overlay = gtk.Overlay()
        overlay.add(content)
        # The scrim dims the page so the question reads as the one thing to answer.
        self._scrim = gtk.Revealer()
        self._scrim.set_transition_type(gtk.RevealerTransitionType.CROSSFADE)
        self._scrim.set_transition_duration(140)
        self._scrim.set_no_show_all(True)
        scrim = gtk.EventBox()
        scrim.set_name('blackrose-consent-scrim')
        self._style(scrim)
        self._scrim.add(scrim)
        scrim.show()
        overlay.add_overlay(self._scrim)
        self._revealer = revealer = gtk.Revealer()
        revealer.set_transition_type(gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(140)
        revealer.set_halign(gtk.Align.CENTER)
        revealer.set_valign(gtk.Align.START)
        revealer.set_reveal_child(False)
        revealer.set_no_show_all(True)
        # Leave the way it came, then stop covering the page.
        revealer.connect('notify::child-revealed', self._settled)
        overlay.add_overlay(revealer)
        toplevel.add(overlay)
        overlay.show()
        content.show()

        def label(css_class, *, wrap=True):
            widget = gtk.Label(xalign=0)
            widget.set_line_wrap(wrap)
            widget.set_line_wrap_mode(2)  # Pango.WrapMode.WORD_CHAR: long URLs break anywhere
            # Kept narrow so the sheet's set width, not the text, decides the line length.
            widget.set_max_width_chars(30)
            widget.get_style_context().add_class(css_class)
            return widget

        self._sheet = sheet = gtk.Box(orientation=gtk.Orientation.VERTICAL, spacing=0)
        sheet.set_name('blackrose-consent')
        self._title = label('consent-title')
        self._message = label('consent-message')
        self._message.set_margin_top(8)
        # The exact payload sits apart from the prose, so it can be read in full.
        self._well = gtk.Box(orientation=gtk.Orientation.VERTICAL, spacing=4)
        self._well.get_style_context().add_class('consent-well')
        self._caption = label('consent-caption')
        self._payload = label('consent-payload')
        # The scroller's own width bounds these, so let them measure at it rather than at 30 characters.
        for text in (self._caption, self._payload):
            text.set_max_width_chars(400)
        _exact(self._payload)
        self._choices = gtk.Box(orientation=gtk.Orientation.VERTICAL, spacing=6)
        self._choices.set_no_show_all(True)
        self._well.pack_start(self._caption, False, False, 0)
        self._well.pack_start(self._payload, False, False, 0)
        self._well.pack_start(self._choices, False, False, 0)
        self._scroller = scroller = gtk.ScrolledWindow()
        scroller.set_policy(gtk.PolicyType.NEVER, gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        # A permanent scrollbar, so an address too long to show whole visibly continues.
        scroller.set_overlay_scrolling(False)
        scroller.set_margin_top(16)
        scroller.set_no_show_all(True)
        scroller.add(self._well)
        self._note = label('consent-note')
        self._note.set_margin_top(12)
        self._note.set_no_show_all(True)
        buttons = gtk.Box(orientation=gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_margin_top(20)
        self._decline, _ = self._button('Decline', 'Esc')
        self._accept, self._accept_label = self._button('Allow', 'A')
        self._accept.get_style_context().add_class('consent-accept')
        self._wider, self._wider_label = self._button('', 'S')
        self._wider.set_no_show_all(True)
        self._decline.connect('clicked', lambda _b: self._answer(False))
        self._accept.connect('clicked', lambda _b: self._answer(True))
        self._wider.connect('clicked', lambda _b: self._answer(WIDER))
        # Decline on its own at the start; the accepts together at the end, the narrow one last.
        buttons.pack_start(self._decline, False, False, 0)
        buttons.pack_end(self._accept, False, False, 0)
        buttons.pack_end(self._wider, False, False, 0)
        for widget in (self._title, self._message, scroller, self._note, buttons):
            sheet.pack_start(widget, False, False, 0)
        self._style(sheet)
        # Parts marked no-show-all appear only when a question uses them.
        sheet.show_all()
        scroller.get_child().show_all()
        revealer.add(sheet)
        if atk is not None:
            sheet.get_accessible().set_role(atk.Role.ALERT)

        toplevel.connect('key-press-event', self._key)
        toplevel.connect('focus-in-event', self._refocus)
        toplevel.connect('destroy', self._destroyed)

    def _button(self, text: str, key: str):
        """A button showing its key as a keycap; returns the button and its text label."""
        gtk = self._gtk
        button = gtk.Button()
        inner = gtk.Box(orientation=gtk.Orientation.HORIZONTAL, spacing=10)
        words = gtk.Label(label=text)
        cap = gtk.Label(label=key)
        cap.get_style_context().add_class('consent-key')
        inner.pack_start(words, False, False, 0)
        inner.pack_start(cap, False, False, 0)
        button.add(inner)
        inner.show_all()
        return button, words

    def _style(self, widget) -> None:
        context = widget.get_style_context()
        context.add_provider(self._css, self._gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if hasattr(widget, 'forall'):
            # forall includes internal children such as a scrolled window's scrollbars.
            widget.forall(self._style)

    # Called from any thread.
    def ask(self, title: str, message: str, button: str, *, warning: bool = False,
            wider: str | None = None, detail: str = '', detail_label: str = '', note: str = '',
            choices: list[str] | None = None) -> bool | str | tuple[bool, ...]:
        """Show the question; True only for an explicit accept, ``WIDER`` for the ``wider`` button.

        ``detail`` is exactly what would be sent or opened, shown apart under ``detail_label``;
        ``note`` is the closing guidance above the buttons. With ``choices``, each is
        a checkbox in place of ``detail``, and an accept returns one bool per choice.
        """
        shown = {'title': title, 'message': message, 'button': button, 'warning': warning, 'wider': wider,
                 'detail': detail, 'detail_label': detail_label, 'note': note, 'choices': choices}
        with self._turn:
            if self._closed:
                return False
            done = threading.Event()
            state = {'answer': False, 'done': done}

            def show():
                try:
                    self._show(state, **shown)
                except Exception:  # noqa: BLE001 - an unshown question is a decline
                    log.exception('Could not show the approval sheet')
                    done.set()
                return self._glib.SOURCE_REMOVE

            if threading.current_thread() is threading.main_thread():
                show()
                while not done.is_set():
                    self._gtk.main_iteration()
            else:
                try:
                    self._glib.idle_add(show)
                except Exception:  # noqa: BLE001 - an unshown question is a decline
                    log.exception('Could not schedule the approval sheet')
                    done.set()
                # The window can be destroyed after the closed check above but
                # before the queued show runs; _destroyed only completes a state
                # _show has already made current.
                while not done.wait(_CLOSE_POLL_SECONDS):
                    if self._closed:
                        done.set()
            answer = state['answer']
            if choices:
                return answer if isinstance(answer, tuple) and len(answer) == len(choices) else False
            return answer if answer is True or (wider and answer == WIDER) else False

    # GTK thread only below.
    def _show(self, state, title, message, button, warning, wider=None, detail='', detail_label='',
              note='', choices=None) -> None:
        if self._closed:
            state['done'].set()
            return
        self._current = state
        self._title.set_text(title)
        self._message.set_text(message)
        self._message.set_visible(bool(message))
        self._caption.set_text(detail_label)
        self._caption.set_visible(bool(detail_label))
        self._payload.set_text(detail)
        self._payload.set_visible(bool(detail))
        for old in self._choices.get_children():
            self._choices.remove(old)
        state['checks'] = checks = []
        for number, choice in enumerate(choices or (), 1):
            check = self._gtk.CheckButton()
            text = self._gtk.Label(label=f'{number}. {choice}' if number < 10 else choice, xalign=0)
            text.set_line_wrap(True)
            text.set_line_wrap_mode(2)
            text.set_max_width_chars(400)
            text.get_style_context().add_class('consent-payload')
            _exact(text)
            check.add(text)
            check.set_active(True)
            self._style(check)
            check.show_all()
            self._choices.pack_start(check, False, False, 0)
            checks.append(check)
        self._choices.set_visible(bool(checks))
        self._scroller.set_visible(bool(detail or checks))
        self._note.set_text(note)
        self._note.set_visible(bool(note))
        self._accept_label.set_text(button)
        state['wider'] = bool(wider)
        self._wider_label.set_text(wider or '')
        self._wider.set_visible(bool(wider))
        self._sheet.get_accessible().set_name(title)
        context = self._sheet.get_style_context()
        (context.add_class if warning else context.remove_class)('warning')
        width = min(WIDTH, max(320, self._toplevel.get_allocated_width() - 32))
        self._sheet.set_size_request(width, -1)
        self._fit_well(width - 48)
        self._webview.set_sensitive(False)
        self._scrim.show()
        self._scrim.set_reveal_child(True)
        self._revealer.show()
        self._sheet.show()
        self._revealer.set_reveal_child(True)
        self._decline.grab_focus()
        self._arm()

    def _fit_well(self, width: int) -> None:
        """Size the payload box to its wrapped text at ``width``, up to ``WELL_MAX``, then scroll.

        GTK 3 measures wrapped labels in a scrolled window at their natural width,
        which leaves a long address cut off with no sign that more follows.
        """
        from gi.repository import Pango  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI
        inner = width - 2 - 32  # the well's border and padding
        height = 2 + 24 + 4     # border, padding and spacing
        texts = [(text, inner) for text in (self._caption, self._payload)]
        checks = (self._current or {}).get('checks', [])
        texts += [(check.get_child(), inner - CHECK) for check in checks]
        for text, room in texts:
            if text.get_visible() and text.get_text():
                layout = text.create_pango_layout(text.get_text())
                layout.set_attributes(text.get_attributes())
                layout.set_width(room * Pango.SCALE)
                layout.set_wrap(Pango.WrapMode.WORD_CHAR)
                height += layout.get_pixel_size()[1]
        height += (8 + 6) * len(checks)  # each row's padding and spacing
        tallest = CHOICES_MAX if checks else WELL_MAX
        self._scroller.set_min_content_width(width)
        self._scroller.set_min_content_height(min(tallest, height))
        self._scroller.set_max_content_height(tallest)

    def _arm(self) -> None:
        state = self._current
        self._accept.set_sensitive(False)
        self._wider.set_sensitive(False)
        state['armed'] = False
        timer = state.get('timer')
        if timer:
            self._glib.source_remove(timer)

        def ready():
            if self._current is state:
                state['armed'] = True
                state['timer'] = None
                self._accept.set_sensitive(True)
                self._wider.set_sensitive(True)
            return self._glib.SOURCE_REMOVE
        state['timer'] = self._glib.timeout_add(ARM_DELAY_MS, ready)

    def _refocus(self, *_args):
        if self._current is not None:
            self._arm()
        return False

    def _key(self, _widget, event) -> bool:
        if self._current is None:
            return False
        name = self._gdk.keyval_name(event.keyval)
        checks = self._current.get('checks') or []
        if name in tuple('123456789') and int(name) <= len(checks):
            check = checks[int(name) - 1]
            check.set_active(not check.get_active())
            return True
        if name == 'space' and any(check.has_focus() for check in checks):
            return False  # The focused checkbox toggles; it never accepts.
        if name in _DECLINE_KEYS:
            self._answer(False)
        elif name in _ACCEPT_KEYS:
            self._answer(True)
        elif name in _WIDER_KEYS:
            if self._current.get('wider'):
                self._answer(WIDER)
        elif name in ('Return', 'KP_Enter', 'space'):
            # Activate only a focused decline; accepting takes A, Y or a click.
            if self._decline.has_focus():
                self._answer(False)
        elif name in _FOCUS_KEYS:
            return False
        return True  # Nothing else reaches the page while a question is open.

    def _answer(self, accepted: bool | str) -> None:
        state = self._current
        if state is None or (accepted and not state.get('armed')):
            return
        if accepted == WIDER and not state.get('wider'):
            return
        if accepted is True and state.get('checks'):
            accepted = tuple(check.get_active() for check in state['checks'])
        self._current = None
        if state.get('timer'):
            self._glib.source_remove(state['timer'])
        state['answer'] = accepted
        self._revealer.set_reveal_child(False)
        self._scrim.set_reveal_child(False)
        self._webview.set_sensitive(True)
        self._webview.grab_focus()
        state['done'].set()

    def _settled(self, revealer, _param) -> None:
        if not revealer.get_child_revealed() and self._current is None:
            revealer.hide()
            self._scrim.hide()

    def _destroyed(self, *_args) -> None:
        self._closed = True
        state, self._current = self._current, None
        if state is not None:
            state['done'].set()


def _exact(label) -> None:
    """Wrap without inserting hyphens, which would show a payload as something it is not."""
    from gi.repository import Pango  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI
    attributes = Pango.AttrList()
    attributes.insert(Pango.attr_insert_hyphens_new(False))
    label.set_attributes(attributes)


def install(window) -> None:
    """Add the sheet to pywebview's GTK window before it is first shown.

    pywebview runs ``before_show`` handlers synchronously on the GTK thread,
    after the WebKit view is in the window and before the page loads.
    """
    def attach():
        from gi.repository import Atk, Gdk, GLib, Gtk  # noqa: PLC0415 - desktop-only GTK/WebKit binding, absent in offline CI
        try:
            browser = window.gui.BrowserView.instances[window.uid]
            window._blackrose_consent = ConsentSheet(browser.window, browser.webview, Gtk, GLib, Gdk, Atk)
        except Exception:  # noqa: BLE001 - approvals fall back to a modal dialog window
            log.exception('Could not install the in-window approval sheet')
    window.events.before_show += attach
