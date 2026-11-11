"""Keep WebKit's native message handler unavailable to generated child frames.

An iframe sandbox restricts DOM access, but WebKit user-content message handlers
are also visible to subframes. Authenticate native messages with a capability
installed only in the trusted top frame, before any page script runs.

No frame may navigate away from the shipped pages, open a window or start a
download; content leaves the window only through the bridge's confirmed hand-offs.
"""
from __future__ import annotations

import json
import secrets
from urllib.parse import urljoin


def protect_desktop_bridge(window):
    """Register before webview.start; this application uses the GTK backend."""
    def install():
        from gi.repository import WebKit2

        browser = window.gui.BrowserView.instances[window.uid]
        manager = browser.manager
        capability = secrets.token_urlsafe(32)
        manager.disconnect_by_func(browser.on_js_bridge_call)

        def receive(content_manager, message):
            try:
                body = json.loads(message.get_js_value().to_string())
            except (ValueError, TypeError):
                return
            if not isinstance(body, dict) or body.get('_blackroseCapability') != capability:
                return
            browser.on_js_bridge_call(content_manager, message)

        manager.connect('script-message-received::jsBridge', receive)
        source = '''(() => {
          const capability = CAPABILITY;
          let bridge;
          Object.defineProperty(window, 'pywebview', {
            configurable: false,
            get() { return bridge; },
            set(value) {
              bridge = value;
              value._jsApiCallback = (funcName, params, id) => {
                window.webkit.messageHandlers.jsBridge.postMessage(JSON.stringify({
                  funcName, params, id, _blackroseCapability: capability
                }));
              };
            }
          });
        })();'''.replace('CAPABILITY', json.dumps(capability))
        manager.add_script(WebKit2.UserScript.new(source,
            WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.START, None, None))
        allowed_pages = {window.real_url, urljoin(window.real_url, 'motion-canvas-host.html'),
                         urljoin(window.real_url, 'mermaid-host.html'),
                         'about:blank', 'about:srcdoc'}

        def navigation(view, decision, kind):
            if isinstance(decision, WebKit2.NavigationPolicyDecision):
                # New-window requests included: links leave only through
                # Api.open_link, which names the destination and asks first.
                uri = decision.get_navigation_action().get_request().get_uri()
                if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION or uri not in allowed_pages:
                    decision.ignore()
                    return True
                return False
            if isinstance(decision, WebKit2.ResponsePolicyDecision):
                # Display what WebKit can; never start a download.
                if decision.is_mime_type_supported():
                    decision.use()
                else:
                    decision.ignore()
                return True
            return False

        # pywebview's handler runs first and hands '_blank' navigations to the
        # system browser before this one could refuse them; replace it.
        browser.webview.disconnect_by_func(browser.on_navigation)
        browser.webview.connect('decide-policy', navigation)

    window.events.before_show += install
