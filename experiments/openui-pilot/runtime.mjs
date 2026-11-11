import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

export const PIN = '78844d2d065e8ed3ea335d5e089c1e21fbd502e8';
export const COMPONENTS = ['Stack', 'Card', 'CardHeader', 'TextContent', 'Table', 'Col', 'BarChart', 'LineChart', 'Series'];
export const hash = value => createHash('sha256').update(value).digest('hex');

export async function loadRuntime(checkout) {
  if (!checkout) throw new Error('Supply --checkout PATH to the built OpenUI checkout; see README.md.');
  const root = resolve(checkout);
  const revision = execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], {encoding: 'utf8'}).trim();
  if (revision !== PIN) throw new Error(`OpenUI revision must be ${PIN}; found ${revision}`);
  const dirty = execFileSync('git', ['-C', root, 'status', '--porcelain', '--untracked-files=no'], {encoding: 'utf8'});
  if (dirty.trim()) throw new Error('OpenUI tracked source has changes; use a clean checkout at the pinned revision.');
  const files = ['packages/lang-core/dist/index.mjs', 'packages/react-ui/dist/genui-lib/index.mjs'];
  const [core, ui] = await Promise.all(files.map(file => import(pathToFileURL(resolve(root, file)))));
  const library = ui.openuiLibrary.extend({components: {
    remove: Object.keys(ui.openuiLibrary.components).filter(name => !COMPONENTS.includes(name)),
  }});
  const schema = library.toJSONSchema();
  const rules = [
    'Static informational display only. No state, queries, mutations, actions, functions, URLs, images or forms.',
    'Use only supplied evidence. Include its source label visibly. Preserve all requested numbers, units and labels.',
    'Root must be Stack. Every declared name must be reachable. Do not include markdown fences or commentary.',
  ];
  const openui = library.prompt({toolCalls: false, bindings: false, builtinFunctions: false, additionalRules: rules});
  const json = [
    'Return ONLY a JSON component tree. Each node is {"component":"Name","props":{...}}.',
    'Children and component-valued props contain nodes in the same format. Use named properties, never positional arguments.',
    'Example: {"component":"Stack","props":{"children":[{"component":"TextContent","props":{"text":"Hello"}}]}}',
    ...rules, 'Component property schemas:', JSON.stringify(schema.$defs),
  ].join('\n');
  return {
    parser: core.createParser(schema), fullParser: core.createParser(ui.openuiLibrary.toJSONSchema()),
    schema, prompts: {openui, json},
    metadata: {
      revision, components: COMPONENTS, schema_sha256: hash(JSON.stringify(schema)),
      modules: Object.fromEntries(await Promise.all(files.map(async file => [file, hash(await readFile(resolve(root, file)))]))),
      prompts: Object.fromEntries(Object.entries({openui, json}).map(([name, prompt]) => [name, {characters: prompt.length, sha256: hash(prompt)}])),
    },
  };
}
