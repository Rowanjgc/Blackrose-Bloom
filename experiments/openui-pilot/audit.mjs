// Recheck original recorded successes with the full historical component set.
// This is graph/completeness validation only, not the new pilot's evidence gate.
import {readFile, writeFile} from 'node:fs/promises';
import {parseArgs} from 'node:util';
import {hash, loadRuntime} from './runtime.mjs';

const {values} = parseArgs({options: {
  checkout: {type: 'string'}, results: {type: 'string'}, out: {type: 'string'},
}});
if (!values.results) throw new Error('Supply --results PATH to original repair-results.json');
const runtime = await loadRuntime(values.checkout);
const raw = await readFile(values.results);
const source = JSON.parse(raw);
const models = {};
for (const [model, entry] of Object.entries(source.models)) {
  const flagged = [];
  let total = 0, recordedPasses = 0;
  for (const [scenario, {trials}] of Object.entries(entry.scenarios)) for (const [i, trial] of trials.entries()) {
    total++;
    if (!trial.finalOk) continue;
    recordedPasses++;
    const parsed = runtime.fullParser.parse(trial.attempts.at(-1).program);
    const meta = parsed.meta;
    if (!parsed.root || meta.errors.length || meta.incomplete || meta.unresolved.length || meta.orphaned.length) {
      flagged.push({scenario, trial: i + 1, incomplete: meta.incomplete, unresolved: meta.unresolved, orphaned: meta.orphaned, errors: meta.errors});
    }
  }
  models[model] = {total, recorded_passes: recordedPasses, passes_after_graph_check: recordedPasses - flagged.length, flagged};
}
const result = {
  description: 'Recheck of recorded final programs; no model inference and no semantic/visual assessment. Stricter re-generation may recover these cases.',
  openui_revision: runtime.metadata.revision, modules: runtime.metadata.modules,
  source_sha256: hash(raw), source_generated_at: source.generated_at, models,
};
const output = JSON.stringify(result, null, 2) + '\n';
if (values.out) await writeFile(values.out, output, {flag: 'wx'});
console.log(output);
