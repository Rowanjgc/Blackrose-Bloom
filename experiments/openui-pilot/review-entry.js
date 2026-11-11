import React from 'react';
import {createRoot} from 'react-dom/client';
import {Renderer} from '@openuidev/react-lang';
import {openuiLibrary} from '@openuidev/react-ui/genui-lib';

const components = __COMPONENTS__;
const library = openuiLibrary.extend({components: {
  remove: Object.keys(openuiLibrary.components).filter(name => !components.includes(name)),
}});
const h = React.createElement;
class Boundary extends React.Component {
  state = {error: null};
  static getDerivedStateFromError(error) { return {error: String(error)}; }
  render() { return this.state.error ? h('pre', {className: 'error'}, this.state.error) : this.props.children; }
}
function Review() {
  const [selected, setSelected] = React.useState(0);
  const record = window.pilotRecords[selected];
  const scenario = window.pilotScenarios.find(s => s.id === record.scenario);
  const attempt = record.attempts.at(-1);
  return h(React.Fragment, null,
    h('h1', null, 'Display pilot review'),
    h('p', null, 'Check readability, completeness, evidence fidelity and usefulness at both widths. A parser pass is not a quality pass.'),
    h('label', null, 'Trial ', h('select', {value: selected, onChange: event => setSelected(Number(event.target.value))},
      window.pilotRecords.map((r, i) => h('option', {value: i, key: r.file}, `${r.file} — ${r.final_ok ? 'gate passed' : 'failed'}`)))),
    h('p', null, `${(record.wall_ms / 1000).toFixed(1)} seconds; ${record.repairs} repairs. Quality: ${record.quality.status}.`),
    h('details', null, h('summary', null, 'Request and evidence'), h('pre', null, JSON.stringify(scenario, null, 2))),
    h('details', null, h('summary', null, 'All attempts'), h('pre', null, JSON.stringify(record.attempts.map(a => ({errors: a.errors, content: a.content})), null, 2))),
    ...(record.final_ok ? [480, 960].map(width => h('section', {key: `${selected}-${width}`},
      h('h2', null, `${width}px display`),
      h('div', {className: 'viewport'}, h('div', {className: 'display', style: {width}},
        h(Boundary, null, h(Renderer, {response: attempt.program, library})))))) : [h('pre', {key: 'errors', className: 'error'}, attempt.errors.join('\n'))]),
  );
}
createRoot(document.getElementById('review')).render(h(Review));
