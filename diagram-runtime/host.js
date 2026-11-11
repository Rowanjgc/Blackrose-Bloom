/* Trusted renderer in an opaque-origin frame. No model-written script runs. */
import mermaid from 'mermaid';

const channel = 'blackrose-mermaid';
const settings = {
  startOnLoad: false, securityLevel: 'strict', htmlLabels: false,
  maxTextSize: 20000, maxEdges: 200, suppressErrorRendering: true,
  theme: 'base', fontFamily: 'system-ui, sans-serif',
  themeVariables: {
    background: '#0d0d0d', primaryColor: '#1a1a1a', primaryTextColor: '#e0e0e0',
    primaryBorderColor: '#b8506b', secondaryColor: '#222222', tertiaryColor: '#121212',
    lineColor: '#b3b3b3', textColor: '#e0e0e0', mainBkg: '#1a1a1a',
    nodeBorder: '#b8506b', clusterBkg: '#121212', clusterBorder: '#6e6e6e',
    edgeLabelBackground: '#0d0d0d', titleColor: '#e0e0e0',
  },
};
mermaid.initialize({...settings, secure: [...Object.keys(settings), 'secure']});
let busy = false;
window.addEventListener('message', async event => {
  const data = event.data;
  if (event.source !== parent || data?.channel !== channel || data.type !== 'render'
      || !Number.isSafeInteger(data.id) || typeof data.source !== 'string' || busy) return;
  busy = true;
  let repairable = false;
  try {
    if (data.source.length > settings.maxTextSize) throw new Error('Diagram exceeds 20,000 characters.');
    if (/%%\s*\{|^\s*---(?:\r?\n|$)/m.test(data.source)) {
      throw new Error('Diagram configuration is not supported. Use standard Mermaid syntax.');
    }
    repairable = true;
    await mermaid.parse(data.source);
    repairable = false;
    let {svg} = await mermaid.render('diagram-' + data.id, data.source);
    if (new TextEncoder().encode(svg).byteLength > 2 * 1024 * 1024) throw new Error('Rendered diagram is too large.');
    // Mermaid serializes link-bearing graphs with xlink:href but can omit its
    // namespace declaration. Make the SVG well-formed before removing links.
    if (!/\bxmlns:xlink=/.test(svg)) svg = svg.replace('<svg ', '<svg xmlns:xlink="http://www.w3.org/1999/xlink" ');
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml');
    if (doc.querySelector('parsererror')) throw new Error('Invalid diagram image.');
    const root = doc.documentElement;
    const box = root.getAttribute('viewBox')?.trim().split(/[\s,]+/).map(Number);
    if (box?.length === 4 && box.every(Number.isFinite) && box[2] > 0 && box[3] > 0) {
      root.setAttribute('width', String(box[2]));
      root.setAttribute('height', String(box[3]));
      root.style.removeProperty('max-width');
    }
    // Diagrams are passive images. Never bind Mermaid's optional interactions.
    // Remove active elements and external references before crossing the frame boundary.
    doc.querySelectorAll('script, foreignObject, image, a, animate, animateMotion, animateTransform, set')
      .forEach(node => {
        if (node.localName === 'a') node.replaceWith(...node.childNodes);
        else node.remove();
      });
    for (const node of doc.querySelectorAll('*')) {
      for (const attr of [...node.attributes]) {
        if (/^on/i.test(attr.name) || (/href$/i.test(attr.name) && !attr.value.startsWith('#'))) {
          node.removeAttributeNode(attr);
        }
      }
    }
    // SVG image documents cannot load external subresources; the frame CSP also
    // blocks such loads while Mermaid computes layout.
    const title = doc.querySelector('title')?.textContent || 'Mermaid diagram';
    const description = doc.querySelector('desc')?.textContent || '';
    parent.postMessage({channel, type: 'result', id: data.id,
      svg: new XMLSerializer().serializeToString(doc.documentElement),
      label: (title + (description ? '. ' + description : '')).slice(0, 2000)}, '*');
  } catch (error) {
    parent.postMessage({channel, type: 'result', id: data.id,
      error: String(error.message || error).slice(0, 500),
      repairable: repairable && !/^(?:Edge limit exceeded|Too many edges|Maximum text (?:size|length))/i.test(String(error.message || error))}, '*');
  } finally { busy = false; }
});
parent.postMessage({channel, type: 'ready'}, '*');
