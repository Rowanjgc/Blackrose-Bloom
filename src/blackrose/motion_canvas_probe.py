"""Disposable GTK/WebKit worker. The parent owns the hard execution deadline.

No desktop bridge is installed. Generated code runs in an opaque sandboxed frame,
using the same trusted adapter as chat and exported animations.
"""
from __future__ import annotations

import json
import sys


def main():
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('WebKit2', '4.1')
    from gi.repository import GLib, Gtk, WebKit2

    payload = json.load(sys.stdin)
    if not Gtk.init_check()[0]:
        raise RuntimeError('WebKit playback validation needs a display (use xvfb-run in CI)')
    # A mapped, transparent window keeps WebKit RAF active without interrupting chat.
    window = Gtk.Window(title='Animation validation')
    window.set_default_size(960, 540)
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_accept_focus(False)
    window.set_focus_on_map(False)
    window.set_opacity(0)
    # Ephemeral storage: the worker is disposable, so WebKit keeps its caches, HSTS
    # list and storage salts in memory instead of the user's XDG cache and data dirs.
    context = WebKit2.WebContext.new_with_website_data_manager(
        WebKit2.WebsiteDataManager.new_ephemeral())
    view = WebKit2.WebView.new_with_context(context)
    window.add(view)
    result = None
    settings = json.dumps(payload, ensure_ascii=True).replace('<', '\\u003c')
    document = '''<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src about:; script-src 'unsafe-inline'; style-src 'unsafe-inline'">
<style>html,body{margin:0}iframe{border:0;width:960px;height:540px}</style>
<iframe sandbox="allow-scripts"></iframe><script>
const config = CONFIG;
const frame = document.querySelector('iframe');
window.probeResult = null;
let duration, targets, index = 0, samples = [];
const send = () => frame.contentWindow.postMessage({channel:'blackrose-motion-control',
  command:'inspect', value:{time:targets[index], id:index, capture:config.capture === true}}, '*');
window.addEventListener('message', event => {
  if (event.source !== frame.contentWindow || event.data?.channel !== 'blackrose-motion' || window.probeResult) return;
  const data = event.data;
  if (data.type === 'error') { window.probeResult = {error:String(data.message).slice(0,1000)}; return; }
  if (data.type === 'ready' && !targets) {
    duration = data.duration;
    if (!Number.isFinite(duration) || duration < .5 || duration > 120) {
      window.probeResult = {error:`Measured timeline must be 0.5..120 seconds; received ${duration}.`}; return;
    }
    if (config.times.some(time => time > duration + 1/30 + 1e-6)) {
      window.probeResult = {error:`Caption lies beyond measured timeline (${duration}s). Update source timing or captions.`}; return;
    }
    const end = Math.max(0, duration - 1/30);
    const requested = config.capture ? [0, end, ...config.times,
      ...Array.from({length: 8}, (_, i) => end * i / 7)] :
      [0, end/4, end/2, end*3/4, ...config.times, end, duration];
    targets = [...new Set(requested
      .map(time => Math.min(duration, Math.round(time*30)/30)))];
    if (config.capture) targets = targets.slice(0, 8);
    targets.sort((a,b)=>a-b);
    // Exercise backward seek through persistent state, reset, and replay to end.
    if (!config.capture) targets.push(targets[Math.floor(targets.length/2)], 0, end, 0);
    send();
  } else if (data.type === 'inspected' && data.id === index) {
    if (Math.abs(data.time - targets[index]) > 1/30 + 1e-6) {
      window.probeResult = {error:`Seek did not render requested frame ${targets[index]}s (received ${data.time}s).`}; return;
    }
    samples.push(data);
    if (++index < targets.length) send();
    else window.probeResult = {duration, samples};
  }
});
frame.srcdoc = config.document;
</script>'''.replace('CONFIG', settings, 1)

    def evaluated(webview, task):
        nonlocal result
        try:
            result = json.loads(webview.evaluate_javascript_finish(task).to_string())
        except Exception as error:
            result = {'error': f'WebKit inspection failed: {error}'}
        if result is not None:
            Gtk.main_quit()
        else:
            GLib.timeout_add(25, evaluate)

    def evaluate():
        view.evaluate_javascript('JSON.stringify(window.probeResult || null)', -1,
                                 None, None, None, evaluated)
        return False

    view.connect('load-changed', lambda v, e: evaluate() if e == WebKit2.LoadEvent.FINISHED else None)
    view.connect('web-process-terminated', lambda *args: (
        sys.stderr.write('WebKit validation process terminated\n'), Gtk.main_quit()))
    window.show_all()
    view.load_html(document, None)
    Gtk.main()
    window.destroy()
    if result is None:
        raise RuntimeError('WebKit validation ended without a result')
    print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
