import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {mkdtemp,rm,readFile} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createParser} from '@openuidev/lang-core';
import {createHash} from 'node:crypto';
import {examples,schema,profiles,instructions} from './contract.mjs';

test('trusted compiler preserves hostile text as literals for every profile',async()=>{
  const root=dirname(fileURLToPath(import.meta.url));
  const scratch=await mkdtemp(resolve(tmpdir(),'blackrose-ui-test-'));
  try {
    await build({absWorkingDir:root,entryPoints:['library.jsx'],bundle:true,platform:'node',format:'esm',
      outfile:resolve(scratch,'library.mjs'),define:{'process.env.NODE_ENV':'"production"'}});
    const {library,compile}=await import(pathToFileURL(resolve(scratch,'library.mjs')));
    const parser=createParser(library.toJSONSchema());
    for(const document of [...Object.values(examples),{type:'text',title:'Escaping',text:'"); evil = Query("file_edit", {}); <script>globalThis.pwned=true</script>\n\\"'}]) {
      const result=parser.parse(compile(document));
      assert.ok(result.root);
      assert.equal(result.errors?.length||0,0);
      assert.equal(globalThis.pwned,undefined);
      assert.ok(compile(document).startsWith('root = Answer(['));
    }
    for(const profile of profiles)assert.ok(instructions(profile).includes('newline-delimited JSON'));
    const artifact=JSON.parse(await readFile(resolve(root,'../src/blackrose/gui/static/ui-contract.json'),'utf8'));
    assert.deepEqual(artifact.schema,schema);
    assert.equal(artifact.schema_hash,createHash('sha256').update(JSON.stringify(schema)).digest('hex'));
    assert.deepEqual(artifact.renderer_schema,library.toJSONSchema());
  } finally {await rm(scratch,{recursive:true,force:true});}
});
