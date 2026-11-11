import {build} from 'esbuild';
import {createHash} from 'node:crypto';
import {readFile,writeFile,readdir,mkdtemp,rm} from 'node:fs/promises';
import {dirname,resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {tmpdir} from 'node:os';
import {schema, profiles, instructions, examples} from './contract.mjs';

const root=dirname(fileURLToPath(import.meta.url));
const output=resolve(root,'../src/blackrose/gui/static');
const hash=value=>createHash('sha256').update(value).digest('hex');
const schemaHash=hash(JSON.stringify(schema));
const scratch=await mkdtemp(resolve(tmpdir(),'blackrose-ui-build-'));
try {
  await build({absWorkingDir:root,entryPoints:['library.jsx'],bundle:true,platform:'node',format:'esm',
    outfile:resolve(scratch,'registry.mjs'),define:{'process.env.NODE_ENV':'"production"'}});
  const {library,compile}=await import(pathToFileURL(resolve(scratch,'registry.mjs')));
  const artifact={protocol_version:1,library_version:'blackrose-semantic/1',schema_hash:schemaHash,
    packages:{'@openuidev/react-lang':'1.0.1','@openuidev/lang-core':'1.0.1',react:'19.2.0'},
    schema,renderer_schema:library.toJSONSchema(),prompts:Object.fromEntries(profiles.map(p=>[p,instructions(p)])),examples};
  artifact.source_hash=hash(Buffer.concat(await Promise.all(
    ['contract.mjs','library.jsx','host.jsx','build.mjs','package.json','package-lock.json'].map(file=>readFile(resolve(root,file))))));
  // Check every compiler fixture against the exact shipped parser/registry.
  const {createParser}=await import('@openuidev/lang-core');
  for(const example of Object.values(examples)) {
    const result=createParser(artifact.renderer_schema).parse(compile(example));
    if(!result.root || result.errors?.length)throw new Error(JSON.stringify(result.errors));
  }
  const result=await build({absWorkingDir:root,entryPoints:['host.jsx'],bundle:true,format:'iife',
    globalName:'BlackroseUI',target:'es2022',minify:true,legalComments:'eof',metafile:true,
    define:{'process.env.NODE_ENV':'"production"','__UI_SCHEMA_HASH__':JSON.stringify(schemaHash)},
    outfile:resolve(output,'ui-runtime.js')});
  const packages=new Map();
  for(const input of Object.keys(result.metafile.inputs)) {
    if(!input.startsWith('node_modules/'))continue;
    let folder=dirname(resolve(root,input));
    while(folder!==root) {
      try {
        const metadata=JSON.parse(await readFile(resolve(folder,'package.json'),'utf8'));
        if(metadata.name){packages.set(folder,metadata);break;}
      } catch(error) {if(error.code!=='ENOENT')throw error;}
      folder=dirname(folder);
    }
  }
  const notices=['Packages included in the locally bundled Blackrose interactive UI runtime.'];
  for(const [folder,meta] of [...packages].sort(([a],[b])=>a.localeCompare(b))) {
    const files=(await readdir(folder)).filter(f=>/^(licen[cs]e|copying|notice)([._-].*)?$/i.test(f));
    if(!files.length)throw new Error(`Missing license: ${meta.name}`);
    notices.push(`\n=== ${meta.name} ${meta.version} (${meta.license||'see notice'}) ===\n`);
    for(const file of files)notices.push(await readFile(resolve(folder,file),'utf8'));
  }
  artifact.bundle_hash=hash(await readFile(resolve(output,'ui-runtime.js')));
  artifact.artifact_hash=hash(JSON.stringify(artifact));
  await writeFile(resolve(output,'ui-contract.json'),JSON.stringify(artifact,null,2)+'\n');
  await writeFile(resolve(output,'ui-NOTICES.txt'),notices.join('\n'));
  console.log(`Built interactive UI: ${Object.keys(result.metafile.inputs).length} modules, ${Object.values(result.metafile.outputs)[0].bytes} bytes; schema ${schemaHash}`);
} finally {await rm(scratch,{recursive:true,force:true});}
