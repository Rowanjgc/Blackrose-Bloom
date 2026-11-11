/* Test double for react-dom/client (see react-dom.cjs).
 *
 * createRoot records the most recent element tree instead of committing it. The
 * harness walks that tree to reach the Host provider value, which is the real
 * host.jsx object with its change/submit closures. Roots are published on
 * globalThis because the stub is bundled into the module under test.
 */
exports.createRoot = (container) => {
  const root = {
    container,
    tree: null,
    rendered: 0,
    unmounted: false,
    render(tree) { this.tree = tree; this.rendered += 1; },
    unmount() { this.tree = null; this.unmounted = true; },
  };
  (globalThis.__sm22UiRoots ||= []).push(root);
  return root;
};
exports.hydrateRoot = exports.createRoot;
