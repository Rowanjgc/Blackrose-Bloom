import assert from 'node:assert/strict';
import test from 'node:test';
import {execFileSync} from 'node:child_process';
import {mkdtemp, readFile, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {loadRuntime} from './runtime.mjs';
import {generate, summarize, trial} from './run.mjs';
import {scenarios} from './scenarios.mjs';
import {jsonProgram, validate} from './validate.mjs';

const runtime = await loadRuntime(process.env.OPENUI_CHECKOUT);
const check = (text, arm = 'openui', scenario) => validate(text, arm, runtime, scenario);
const valid = 'root = Stack([TextContent("Hello")])';

test('static display accepted; unresolved, orphaned, incomplete and empty displays rejected', () => {
  assert.equal(check(valid).ok, true);
  for (const text of [
    'root = Stack([missingChart])',
    'root = Stack([TextContent("Hello")]',
    `${valid}\nunused = TextContent("Unused")`,
    'root = Stack([])',
    'root = Stack([TextContent("")])',
    'root = Stack([Button("Submit")])',
    '$name = "Hi"\nroot = Stack([TextContent($name)])',
  ]) assert.equal(check(text).ok, false, text);
});

test('named JSON and positional DSL use the same component gate', () => {
  const tree = {component: 'Stack', props: {children: [{component: 'TextContent', props: {text: 'Hello'}}]}};
  assert.equal(check(JSON.stringify(tree), 'json').ok, true);
  tree.props.typo = true;
  assert.throws(() => jsonProgram(JSON.stringify(tree), runtime.schema), /unknown prop/);
  assert.equal(check('{broken', 'json').ok, false);
  assert.equal(check('{"component":"Table","props":{}}', 'json').ok, false);
});

test('evidence checks reject missing content and changed table data', () => {
  const s = scenarios[0];
  const table = values => `root = Stack([TextContent(${JSON.stringify(s.source)}), Table([${values.map(c => `Col(${JSON.stringify(c.label)}, ${JSON.stringify(c.data)})`).join(', ')}])])`;
  assert.equal(check(table(s.columns), 'openui', s).ok, true);
  const wrong = structuredClone(s.columns);
  wrong[2].data[0] = 1;
  assert.equal(check(table(wrong), 'openui', s).ok, false);
  assert.equal(check(valid, 'openui', s).ok, false);
});

test('evidence checks reject mismatched chart values, units and source', () => {
  const s = scenarios[1];
  const chart = yLabel => `root = Stack([TextContent(${JSON.stringify(s.source)}), TextContent("104"), BarChart(${JSON.stringify(s.chart.labels)}, [Series("Revenue", ${JSON.stringify(s.chart.series[0].values)})], "grouped", "Month", ${JSON.stringify(yLabel)})])`;
  assert.equal(check(chart(s.chart.yLabel), 'openui', s).ok, true);
  assert.equal(check(chart('USD'), 'openui', s).ok, false);
  assert.equal(check(chart(s.chart.yLabel).replace('[12,15,14,18,21,24]', '[13,15,14,18,21,24]'), 'openui', s).ok, false);
  assert.equal(check(chart(s.chart.yLabel).replace(s.source, 'Unverified source'), 'openui', s).ok, false);
});

test('repairs stop at two and preserve evidence in the conversation', async () => {
  let calls = 0;
  const result = await trial(runtime, scenarios[0], 'openui', async messages => {
    calls++;
    assert.ok(messages[1].content.includes('98000'));
    if (calls > 1) assert.ok(messages.at(-1).content.includes('Validation failed'));
    return {content: 'root = Stack([missing])'};
  });
  assert.equal(calls, 3);
  assert.equal(result.repairs, 2);
  assert.equal(result.final_ok, false);
  assert.equal(result.quality.status, 'pending');
});

test('stream termination and request tuning are enforced', async () => {
  const fakeFetch = (done, finish) => async (_url, options) => {
    assert.deepEqual(Object.keys(JSON.parse(options.body)).sort(), ['messages', 'model', 'stream', 'stream_options']);
    return new Response(`data: ${JSON.stringify({choices: [{delta: {content: valid}, finish_reason: finish}]})}\n\n${done ? 'data: [DONE]\n\n' : ''}`);
  };
  const signal = AbortSignal.timeout(1000);
  assert.equal((await generate('http://localhost', [], signal, fakeFetch(true, 'stop'))).content, valid);
  await assert.rejects(generate('http://localhost', [], signal, fakeFetch(false, 'stop')), /Incomplete stream/);
  await assert.rejects(generate('http://localhost', [], signal, fakeFetch(true, 'length')), /Incomplete stream/);
});

test('transport failure stops repairs and is never a quality acceptance', async () => {
  let calls = 0;
  const record = await trial(runtime, scenarios[0], 'openui', async () => { calls++; throw new Error('timeout'); });
  assert.equal(calls, 1);
  assert.equal(record.final_ok, false);
  assert.equal(summarize([record]).openui.transport_failures, 1);
  assert.equal(summarize([record]).openui.integration_accepted, false);
});

test('total display deadline rejects an answer received after the budget', async () => {
  const record = await trial(runtime, scenarios[0], 'openui', async () => {
    await new Promise(resolve => setTimeout(resolve, 10));
    return {content: valid};
  }, 1);
  assert.equal(record.final_ok, false);
  assert.equal(record.attempts[0].transport_error, true);
});

test('review artifact builds from matching metadata without model requests', async () => {
  const out = await mkdtemp(resolve(tmpdir(), 'openui-pilot-review-'));
  await writeFile(resolve(out, 'metadata.json'), JSON.stringify({...runtime.metadata, scenarios}));
  const s = scenarios[0];
  const program = `root = Stack([TextContent(${JSON.stringify(s.source)}), Table([${s.columns.map(c => `Col(${JSON.stringify(c.label)}, ${JSON.stringify(c.data)})`).join(', ')}])])`;
  const record = {...await trial(runtime, s, 'openui', async () => ({content: program})), trial: 1};
  assert.equal(record.final_ok, true);
  await writeFile(resolve(out, 'simple-table-openui-1.json'), JSON.stringify(record));
  execFileSync(process.execPath, ['experiments/openui-pilot/review.mjs', '--checkout', process.env.OPENUI_CHECKOUT, '--results', out], {timeout: 60_000});
  assert.ok((await readFile(resolve(out, 'review/index.html'), 'utf8')).includes("connect-src 'none'"));
  assert.ok((await readFile(resolve(out, 'review/review.js'))).length > 1000);
  console.log(`Offline review fixture: ${out}/review`);
});
