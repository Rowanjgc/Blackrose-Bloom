// Only parse/transform the supplied source. No generated modules are evaluated.
import {build} from 'esbuild';
import {readFile} from 'node:fs/promises';
import {checkSource, normalizeSource} from './diagnostics.mjs';

const allowed = new Set(['@motion-canvas/core', '@motion-canvas/2d',
  '@motion-canvas/2d/lib/jsx-runtime']);
const exportsByPackage = JSON.parse(await readFile(new URL('./exports.json', import.meta.url), 'utf8'));
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  if (Buffer.byteLength(input) > 64000) throw new Error('Scene source exceeds 64000 bytes');
}
try {
  const original = input;
  input = normalizeSource(input);
  const result = await build({
    stdin: {contents: input, loader: 'tsx', sourcefile: 'scene.tsx'},
    bundle: true, write: false, format: 'iife', globalName: 'BlackroseScene',
    platform: 'browser', target: 'es2022', logLevel: 'silent',
    jsx: 'automatic', jsxImportSource: '@motion-canvas/2d/lib',
    plugins: [{name: 'scene-imports', setup(builder) {
      builder.onResolve({filter: /.*/}, args => allowed.has(args.path)
        ? {path: args.path, namespace: 'motion-api'}
        : {errors: [{text: `Import unavailable: ${args.path}. Use @motion-canvas/core or @motion-canvas/2d.`}]});
      builder.onLoad({filter: /.*/, namespace: 'motion-api'}, args => {
        const {namespace, names} = exportsByPackage[args.path];
        return {contents: names.map(name =>
          `export const ${name} = BlackroseMotion.${namespace}.${name};`).join('\n'), loader: 'js'};
      });
    }}],
  });
  const diagnostics = checkSource(original);
  if (diagnostics.length) throw new Error(diagnostics.join('\n'));
  process.stdout.write(result.outputFiles[0].text);
} catch (error) {
  let message = error.errors?.map(e =>
    `${e.location ? `scene.tsx:${e.location.line}:${e.location.column}: ` : ''}${e.text}`).join('\n') || error.message;
  if (message.includes('for import "Group"')) message += '\nUse Node from @motion-canvas/2d for grouping.';
  if (message.includes('for import "easeInOut"')) message += '\nUse easeInOutCubic from @motion-canvas/core.';
  process.stderr.write(message.slice(0, 3000));
  process.exitCode = 1;
}
