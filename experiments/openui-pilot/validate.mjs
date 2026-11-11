import {isDeepStrictEqual} from 'node:util';
import {COMPONENTS} from './runtime.mjs';

// Convert a named-property JSON tree to the same component calls and gate used
// by the DSL arm. Unknown properties are errors, never silently discarded.
export function jsonProgram(text, schema) {
  const literal = (value, depth = 0) => {
    if (depth > 40) throw new Error('JSON tree exceeds 40 levels');
    if (Array.isArray(value)) return `[${value.map(v => literal(v, depth + 1)).join(', ')}]`;
    if (value && typeof value === 'object') {
      const {component, props} = value;
      if (!COMPONENTS.includes(component) || !props || Array.isArray(props) || typeof props !== 'object') {
        throw new Error('Every JSON object must be a supported {component, props} node');
      }
      if (Object.keys(value).some(key => !['component', 'props'].includes(key))) throw new Error('Unknown node field');
      const definition = schema.$defs[component];
      const keys = Object.keys(definition.properties);
      for (const key of Object.keys(props)) if (!keys.includes(key)) throw new Error(`${component}: unknown prop ${key}`);
      for (const key of definition.required || []) if (!(key in props) || props[key] === null) throw new Error(`${component}: missing ${key}`);
      const last = keys.reduce((n, key, i) => key in props ? i : n, -1);
      const args = keys.slice(0, last + 1).map(key => literal(props[key] ?? null, depth + 1));
      return `${component}(${args.join(', ')})`;
    }
    return JSON.stringify(value);
  };
  return `root = ${literal(JSON.parse(text))}`;
}

export function validate(text, arm, runtime, scenario) {
  const errors = [];
  let program = text;
  try {
    if (Buffer.byteLength(text) > 100_000) throw new Error('Output exceeds 100 KB');
    if (arm === 'json') program = jsonProgram(text, runtime.schema);
    else if (arm !== 'openui') throw new Error(`Unknown arm: ${arm}`);
    const parsed = runtime.parser.parse(program);
    const meta = parsed.meta || {};
    for (const error of meta.errors || []) errors.push(error.message || String(error));
    if (!parsed.root) errors.push('Missing root');
    if (meta.incomplete) errors.push('Incomplete program');
    for (const key of ['unresolved', 'orphaned']) {
      if (meta[key]?.length) errors.push(`${key}: ${meta[key].join(', ')}`);
    }
    if (Object.keys(parsed.stateDeclarations || {}).length || parsed.queryStatements?.length || parsed.mutationStatements?.length) {
      errors.push('State, queries and mutations are outside this pilot');
    }
    const nodes = [];
    const walk = (value, depth = 0) => {
      if (depth > 40) throw new Error('Tree exceeds 40 levels');
      if (Array.isArray(value)) return value.forEach(item => walk(item, depth + 1));
      if (!value || typeof value !== 'object') return;
      if ('k' in value || value.hasDynamicProps) errors.push('Dynamic expressions are outside this pilot');
      if (value.type === 'element') {
        nodes.push(value);
        if (!COMPONENTS.includes(value.typeName)) errors.push(`Unsupported component: ${value.typeName}`);
        if (value.partial) errors.push('Partial component');
        walk(value.props, depth + 1);
      } else Object.values(value).forEach(item => walk(item, depth + 1));
    };
    walk(parsed.root);
    if (parsed.root?.typeName !== 'Stack') errors.push('Root must be Stack');
    const meaningful = nodes.some(n =>
      (n.typeName === 'TextContent' && typeof n.props.text === 'string' && n.props.text.trim()) ||
      (n.typeName === 'Table' && n.props.columns?.some(c => Array.isArray(c.props.data) && c.props.data.length)) ||
      (['BarChart', 'LineChart'].includes(n.typeName) && n.props.labels?.length && n.props.series?.length));
    if (!meaningful) errors.push('Empty informational display');
    const structureOk = errors.length === 0;
    // This is evidence fidelity and requested coverage, not a visual-quality score.
    if (scenario) {
      for (const type of scenario.required) if (!nodes.some(n => n.typeName === type)) errors.push(`Missing requested ${type}`);
      const visible = nodes.filter(n => ['TextContent', 'CardHeader'].includes(n.typeName)).flatMap(n => Object.values(n.props).filter(v => typeof v === 'string')).join('\n');
      for (const text of scenario.texts) {
        const present = /^\d+$/.test(text) ? new RegExp(`(?<![\\d.])${text}(?![\\d.])`).test(visible) : visible.includes(text);
        if (!present) errors.push(`Missing visible text: ${text}`);
      }
      if (scenario.columns) {
        const expected = scenario.columns;
        const matches = nodes.some(n => n.typeName === 'Table' && isDeepStrictEqual(
          n.props.columns.map(c => ({label: c.props.label, data: c.props.data})), expected));
        if (!matches) errors.push('Table columns or values differ from evidence');
      }
      if (scenario.chart) {
        const {type, labels, series, xLabel, yLabel} = scenario.chart;
        const matches = nodes.some(n => n.typeName === type &&
          isDeepStrictEqual(n.props.labels, labels) && n.props.xLabel === xLabel && n.props.yLabel === yLabel &&
          isDeepStrictEqual(n.props.series.map(s => ({category: s.props.category, values: s.props.values})), series));
        if (!matches) errors.push('Chart labels, units or values differ from evidence');
      }
    }
    return {ok: errors.length === 0, structure_ok: structureOk, errors, program, root: parsed.root, meta};
  } catch (error) {
    return {ok: false, structure_ok: false, errors: [...errors, error.message], program};
  }
}
