// Build an offline, standalone review artifact. Never launches a browser.
import {createRequire} from 'node:module';
import {mkdir, readFile, readdir, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {parseArgs} from 'node:util';
import {COMPONENTS, hash, loadRuntime} from './runtime.mjs';

const {values} = parseArgs({options: {checkout: {type: 'string'}, results: {type: 'string'}}});
if (!values.results) throw new Error('Supply --results DIR from run.mjs');
const runtime = await loadRuntime(values.checkout);
const directory = resolve(values.results);
const metadata = JSON.parse(await readFile(resolve(directory, 'metadata.json')));
if (metadata.schema_sha256 !== runtime.metadata.schema_sha256) throw new Error('Review schema differs from generation schema');
for (const [file, digest] of Object.entries(metadata.modules)) {
  if (runtime.metadata.modules[file] !== digest) throw new Error(`Review module differs from generation: ${file}`);
}
const records = [];
for (const file of (await readdir(directory)).sort()) {
  if (!/^(simple-table|chart-with-data|dashboard|informational-display)-(openui|json)-\d+\.json$/.test(file)) continue;
  records.push({file, ...JSON.parse(await readFile(resolve(directory, file)))});
}
if (!records.length) throw new Error('No pilot trial records found');
const packageRoot = resolve(values.checkout, 'packages/browser-bundle');
const require = createRequire(resolve(packageRoot, 'package.json'));
const {build} = require('esbuild');
const entry = await readFile(new URL('review-entry.js', import.meta.url), 'utf8');
const built = await build({
  stdin: {contents: entry.replace('__COMPONENTS__', JSON.stringify(COMPONENTS)), resolveDir: packageRoot},
  bundle: true, write: false, format: 'iife', minify: true, target: 'es2020',
  define: {'process.env.NODE_ENV': '"production"'},
});
const out = resolve(directory, 'review');
await mkdir(out); // Preserve any previous review; choose a fresh result directory.
await writeFile(resolve(out, 'review.js'), built.outputFiles[0].contents);
const css = await readFile(resolve(packageRoot, 'dist/openui-styles.css'));
await writeFile(resolve(out, 'openui.css'), css);
await writeFile(resolve(out, 'records.js'), `window.pilotRecords=${JSON.stringify(records).replaceAll('<', '\\u003c')};window.pilotScenarios=${JSON.stringify(metadata.scenarios).replaceAll('<', '\\u003c')};`);
await writeFile(resolve(out, 'index.html'), `<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src data:; connect-src 'none'">
<title>OpenUI pilot review</title><link rel="stylesheet" href="openui.css">
<style>body{margin:24px;font:16px/1.5 system-ui;color:#222;background:#fff}h1{font-size:24px}select{font:inherit;max-width:100%}pre{white-space:pre-wrap;max-width:960px}.viewport{overflow:auto;margin:16px 0}.display{padding:16px;border:1px solid #aaa;box-sizing:border-box}summary{cursor:pointer}h2{font-size:18px}.error{color:#a00}</style>
<main id="review"></main><script src="records.js"></script><script src="review.js"></script></html>`);
await writeFile(resolve(out, 'build.json'), JSON.stringify({
  openui_revision: runtime.metadata.revision, schema_sha256: runtime.metadata.schema_sha256,
  javascript_sha256: hash(built.outputFiles[0].contents), css_sha256: hash(css),
  esbuild_version: require('esbuild').version,
}, null, 2));
console.log(`Review built: ${out}/index.html. Serve this directory on loopback; no browser was opened.`);
