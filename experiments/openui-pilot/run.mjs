import {mkdir, writeFile} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';
import {parseArgs} from 'node:util';
import {pathToFileURL} from 'node:url';
import {performance} from 'node:perf_hooks';
import {hash, loadRuntime} from './runtime.mjs';
import {scenarios, userPrompt} from './scenarios.mjs';
import {validate} from './validate.mjs';

export const MODEL = 'qwen36-35b-a3b';
const MAX_REPAIRS = 2;
const DEADLINE_MS = 60_000;

export async function resident(endpoint) {
  const response = await fetch(`${endpoint}/v1/models`, {signal: AbortSignal.timeout(5000)});
  if (!response.ok) throw new Error(`Model status HTTP ${response.status}`);
  const {data} = await response.json();
  const loaded = data.filter(row => row.status?.value === 'loaded');
  if (loaded.length !== 1 || loaded[0].id !== MODEL) {
    throw new Error(`Select ${MODEL} explicitly before running. Loaded: ${loaded.map(r => r.id).join(', ') || 'none'}. No load request was sent.`);
  }
}

export async function generate(endpoint, messages, signal, fetchImpl = fetch) {
  const started = performance.now();
  const response = await fetchImpl(`${endpoint}/v1/chat/completions`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, signal,
    // Model presets own all tuning. No grammar, sampling or token overrides.
    body: JSON.stringify({model: MODEL, stream: true, stream_options: {include_usage: true}, messages}),
  });
  if (!response.ok || !response.body) throw new Error(`Generation HTTP ${response.status}`);
  let buffer = '', content = '', usage = null, finish = null, done = false, firstContentMs = null;
  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  function line(raw) {
    if (!raw.startsWith('data:')) return;
    const value = raw.slice(5).trim();
    if (value === '[DONE]') { done = true; return; }
    const packet = JSON.parse(value);
    if (packet.error) throw new Error(`Server error: ${JSON.stringify(packet.error)}`);
    if (packet.usage) usage = packet.usage;
    const choice = packet.choices?.[0];
    if (choice?.finish_reason) finish = choice.finish_reason;
    if (choice?.delta?.content) {
      if (firstContentMs === null) firstContentMs = performance.now() - started;
      content += choice.delta.content;
      if (Buffer.byteLength(content) > 100_000) throw new Error('Output exceeds 100 KB');
    }
  }
  try {
    while (!done) {
      const chunk = await reader.read();
      buffer += chunk.done ? decoder.decode() : decoder.decode(chunk.value, {stream: true});
      const lines = buffer.split('\n');
      buffer = lines.pop();
      lines.forEach(raw => line(raw.trimEnd()));
      if (chunk.done) { if (buffer) line(buffer.trimEnd()); break; }
    }
    if (!done || finish !== 'stop') throw new Error(`Incomplete stream: done=${done}, finish=${finish}`);
    return {content, usage, firstContentMs};
  } finally {
    await reader.cancel();
  }
}

export async function trial(runtime, scenario, arm, call, deadlineMs = DEADLINE_MS) {
  const started = performance.now();
  const messages = [{role: 'system', content: runtime.prompts[arm]}, {role: 'user', content: userPrompt(scenario)}];
  const attempts = [];
  const signal = AbortSignal.timeout(deadlineMs);
  for (let i = 0; i <= MAX_REPAIRS; i++) {
    try {
      signal.throwIfAborted();
      const generated = await call(messages, signal);
      const result = validate(generated.content, arm, runtime, scenario);
      signal.throwIfAborted();
      if (performance.now() - started > deadlineMs) throw new Error('Total display deadline exceeded');
      attempts.push({...generated, ...result});
      if (result.ok) break;
      messages.push({role: 'assistant', content: generated.content}, {role: 'user', content:
        `Validation failed:\n${result.errors.slice(0, 20).join('\n')}\nRewrite the complete ${arm === 'json' ? 'JSON tree' : 'OpenUI program'}. Preserve the evidence, source label, requested components and values. Output only the requested format.`});
    } catch (error) {
      attempts.push({ok: false, errors: [error.message], transport_error: true});
      break;
    }
  }
  return {
    scenario: scenario.id, arm, first_ok: attempts[0].ok, final_ok: attempts.at(-1).ok,
    repairs: attempts.length - 1, wall_ms: performance.now() - started, attempts,
    quality: {status: 'pending', readable: null, complete: null, faithful: null, useful: null, notes: ''},
  };
}

const percentile = (values, p) => [...values].sort((a, b) => a - b)[Math.max(0, Math.ceil(p * values.length) - 1)];
export function summarize(records) {
  return Object.fromEntries(['openui', 'json'].map(arm => {
    const rows = records.filter(r => r.arm === arm);
    if (!rows.length) return [arm, null];
    const times = rows.map(r => r.wall_ms / 1000);
    const attempts = rows.flatMap(r => r.attempts);
    const tokens = name => attempts.every(a => Number.isFinite(a.usage?.[name]))
      ? attempts.reduce((sum, a) => sum + a.usage[name], 0) : null;
    return [arm, {
      n: rows.length, first_ok: rows.filter(r => r.first_ok).length, final_ok: rows.filter(r => r.final_ok).length,
      first_structure_ok: rows.filter(r => r.attempts[0].structure_ok).length,
      final_structure_ok: rows.filter(r => r.attempts.at(-1).structure_ok).length,
      median_seconds: percentile(times, 0.5), p95_seconds: percentile(times, 0.95), max_seconds: Math.max(...times),
      transport_failures: rows.filter(r => r.attempts.some(a => a.transport_error)).length,
      prompt_tokens: tokens('prompt_tokens'), completion_tokens: tokens('completion_tokens'),
      per_scenario: Object.fromEntries([...new Set(rows.map(r => r.scenario))].map(id => {
        const group = rows.filter(r => r.scenario === id);
        return [id, {n: group.length, final_ok: group.filter(r => r.final_ok).length}];
      })),
      quality_pending: rows.filter(r => r.quality.status === 'pending').length,
      // Never equate a structural/fidelity result with product acceptance.
      integration_accepted: false,
    }];
  }));
}

async function main() {
  const {values} = parseArgs({options: {
    checkout: {type: 'string'}, out: {type: 'string'}, endpoint: {type: 'string', default: 'http://127.0.0.1:18080'},
    trials: {type: 'string', default: '10'}, run: {type: 'boolean'}, help: {type: 'boolean'},
  }});
  if (values.help) {
    console.log('node experiments/openui-pilot/run.mjs --checkout PATH [--run --trials 10 --out DIR]\nWithout --run: local preflight only; no router requests. See README.md.');
    return;
  }
  const count = Number(values.trials);
  if (!Number.isInteger(count) || count < 1 || count > 100) throw new Error('--trials must be 1..100 per scenario per arm');
  const endpoint = values.endpoint.replace(/\/$/, '');
  const url = new URL(endpoint);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || url.pathname !== '/' || url.search || url.hash || url.username || url.password) {
    throw new Error('Use a loopback HTTP router origin, with no credentials, path, query or fragment');
  }
  const runtime = await loadRuntime(values.checkout);
  const sourceFiles = ['run.mjs', 'runtime.mjs', 'validate.mjs', 'scenarios.mjs'];
  const {readFile} = await import('node:fs/promises');
  const harnessHashes = Object.fromEntries(await Promise.all(sourceFiles.map(async file => [file, hash(await readFile(new URL(file, import.meta.url)))])));
  const metadata = {
    ...runtime.metadata, harness_sha256: harnessHashes, model: MODEL, endpoint,
    max_repairs: MAX_REPAIRS, display_deadline_ms: DEADLINE_MS, trials_per_scenario_per_arm: count,
    scenarios_sha256: hash(JSON.stringify(scenarios)),
  };
  console.log(JSON.stringify(metadata, null, 2));
  if (!values.run) { console.log('Preflight passed. No model requests sent.'); return; }
  await resident(endpoint);
  const out = resolve(values.out || `artifacts/openui-pilot/${new Date().toISOString().replaceAll(':', '-')}`);
  await mkdir(dirname(out), {recursive: true});
  await mkdir(out); // Existing results must never be overwritten or merged.
  await writeFile(resolve(out, 'metadata.json'), JSON.stringify({...metadata, generated_at: new Date().toISOString(), scenarios}, null, 2));
  for (const [arm, prompt] of Object.entries(runtime.prompts)) await writeFile(resolve(out, `${arm}-prompt.txt`), prompt);
  const records = [];
  // Alternate the first arm to reduce systematic warm-cache/order advantage.
  for (let n = 0; n < count; n++) for (const scenario of scenarios) {
    for (const arm of n % 2 ? ['json', 'openui'] : ['openui', 'json']) {
      await resident(endpoint);
      const record = {...await trial(runtime, scenario, arm, (messages, signal) => generate(endpoint, messages, signal)), trial: n + 1};
      records.push(record);
      const name = `${scenario.id}-${arm}-${n + 1}`;
      await writeFile(resolve(out, `${name}.json`), JSON.stringify(record, null, 2));
      const program = record.attempts.at(-1).program;
      if (program) await writeFile(resolve(out, `${name}.oui`), program);
      await writeFile(resolve(out, 'summary.json'), JSON.stringify(summarize(records), null, 2));
      console.log(`${name}: ${record.final_ok ? 'PASS (quality pending)' : 'FAIL'} ${(record.wall_ms / 1000).toFixed(1)}s`);
    }
  }
  console.log(`Saved ${records.length} trials to ${out}. Review quality before any integration decision.`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
