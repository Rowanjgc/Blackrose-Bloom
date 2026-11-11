import {build} from 'esbuild';
import {createHash} from 'node:crypto';
import {readFile, writeFile, readdir} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const output = resolve(root, '../src/blackrose/gui/static');
const sources = ['host.js', 'build.mjs', 'package.json', 'package-lock.json'];
const hash = value => createHash('sha256').update(value).digest('hex');
const result = await build({absWorkingDir: root, entryPoints: ['host.js'], bundle: true,
  format: 'iife', target: 'es2022', minify: true, legalComments: 'eof', metafile: true,
  write: false, define: {'process.env.NODE_ENV': '"production"'}});
const script = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const host = `<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<script>${script}</script>`;
const packages = new Map();
for (const input of Object.keys(result.metafile.inputs)) {
  if (!input.startsWith('node_modules/')) continue;
  let folder = dirname(resolve(root, input));
  while (folder !== root) {
    try {
      const metadata = JSON.parse(await readFile(resolve(folder, 'package.json'), 'utf8'));
      if (metadata.name) { packages.set(folder, metadata); break; }
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    folder = dirname(folder);
  }
}
const notices = ['Packages bundled in the local Blackrose Mermaid renderer.'];
for (const [folder, metadata] of [...packages].sort(([a], [b]) => a.localeCompare(b))) {
  const files = (await readdir(folder)).filter(file => /^(licen[cs]e|copying|notice)([._-].*)?$/i.test(file));
  if (!files.length) throw new Error('Missing license: ' + metadata.name);
  notices.push(`\n=== ${metadata.name} ${metadata.version} (${metadata.license || 'see notice'}) ===\n`);
  for (const file of files) notices.push(await readFile(resolve(folder, file), 'utf8'));
}
const manifest = {
  version: 'blackrose-mermaid/1', mermaid: '12.3.0',
  source_hash: hash(Buffer.concat(await Promise.all(sources.map(file => readFile(resolve(root, file)))))),
  host_hash: hash(host), notices_hash: hash(notices.join('\n')),
};
await writeFile(resolve(output, 'mermaid-host.html'), host);
await writeFile(resolve(output, 'mermaid-NOTICES.txt'), notices.join('\n'));
await writeFile(resolve(output, 'mermaid-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`Built full Mermaid ${manifest.mermaid}: ${Buffer.byteLength(host)} bytes, ${packages.size} packages.`);
