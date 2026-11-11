/* Test double for react-dom: the SM-22 host harness never commits a real DOM.
 *
 * host.jsx imports flushSync from react-dom and createRoot from react-dom/client.
 * The state-machine tests drive the record objects that render() hands to React,
 * so a root that records the element tree is enough. Keeping this module CommonJS
 * makes every named import resolvable without inventing each export.
 */
exports.flushSync = (callback) => callback();
exports.createPortal = () => null;
exports.render = () => null;
exports.hydrate = () => null;
exports.unmountComponentAtNode = () => false;
exports.findDOMNode = () => null;
exports.version = '19.2.0-stub';
