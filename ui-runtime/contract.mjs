// The model contract, Python validation artifact and renderer share this registry.
const text = {type: 'string', minLength: 1, maxLength: 4000};
const short = {...text, maxLength: 160};
const number = {type: 'number', minimum: -1e9, maximum: 1e9};
const list = (items, maxItems = 40) => ({type: 'array', items, minItems: 1, maxItems});
const object = (properties, required = Object.keys(properties)) =>
  ({type: 'object', properties, required, additionalProperties: false});
const field = object({
  id: {type: 'string', pattern: '^[a-z][a-z0-9_]{0,39}$'}, label: short,
  kind: {enum: ['text', 'number', 'select', 'checkbox']},
  required: {type: 'boolean'}, options: list(short, 30),
  minimum: number, maximum: number,
  default: {type: ['string', 'number', 'boolean'], maxLength: 4000},
}, ['id', 'label', 'kind']);
const block = (type, props, required) => object({type: {const: type}, title: short, ...props}, ['type', 'title', ...required]);
export const variants = {
  text: block('text', {text}, ['text']),
  comparison: block('comparison', {
    columns: list(short, 8), rows: list({type: 'object', minProperties: 1, maxProperties: 8,
      additionalProperties: {type: ['string', 'number', 'boolean'], maxLength: 4000}}, 200),
    submit_label: short,
  }, ['columns', 'rows']),
  form: block('form', {fields: list(field, 20), submit_label: short}, ['fields', 'submit_label']),
  steps: block('steps', {items: list(object({title: short, text}), 30)}, ['items']),
  calculation: block('calculation', {
    items: list(object({label: short, quantity: {...number, minimum: 0, maximum: 1e6},
      unit_price: {...number, minimum: 0, maximum: 1e6}}), 100),
    unit: short, scale_label: short,
  }, ['items', 'unit', 'scale_label']),
  metrics: block('metrics', {data: list(object({label: short, value: number}), 100),
    unit: short, aggregate: {enum: ['sum', 'average', 'count']}}, ['data', 'unit', 'aggregate']),
};
export const schema = {$schema: 'https://json-schema.org/draft/2020-12/schema', oneOf: Object.values(variants)};
export const profiles = ['mixed', 'comparison', 'form', 'steps', 'calculation', 'metrics'];
export const examples = {
  comparison: {type:'comparison',title:'Retrieval approaches',columns:['Approach','Useful when','Main limit'],rows:[{'Approach':'Keyword','Useful when':'Exact names matter','Main limit':'Misses paraphrases'},{'Approach':'Semantic','Useful when':'Wording varies','Main limit':'Can drift from exact terms'}],submit_label:'Compare approaches'},
  form: {type:'form',title:'Reading preferences',fields:[{id:'days',label:'Days per week',kind:'number',minimum:1,maximum:7,required:true},{id:'format',label:'Preferred format',kind:'select',options:['Physical','Ebook','Either']}],submit_label:'Plan my reading'},
  steps: {type:'steps',title:'Review a source',items:[{title:'Read',text:'Read the source in context.'},{title:'Check',text:'Compare the claim with its evidence.'}]},
  calculation: {type:'calculation',title:'Illustrative project estimate',unit:'USD',scale_label:'Quantity multiplier',items:[{label:'Printing',quantity:10,unit_price:2.5}]},
  metrics: {type:'metrics',title:'Count of supplied distance observations',unit:'km',aggregate:'count',data:[{label:'North',value:10},{label:'South',value:15},{label:'East',value:0}]},
};
export function instructions(profile) {
  const names = profile === 'mixed' ? Object.keys(variants) : ['text', profile];
  return ['Interactive answer protocol blackrose-semantic/1. Read this contract as instructions for the NEXT generation segment only.',
    'Gather any evidence BEFORE activating interactive_ui. For the next response, emit ONLY newline-delimited JSON: one complete block per line, no code fences or surrounding prose. No more tools in that segment. Do not pretty-print across lines. Text belongs in a text block.',
    'Use Markdown for ordinary answers. Use these blocks only when controls help. Host code supplies layout, state, arithmetic and accessibility. Supplied numbers are not verified evidence. Never invent facts, units or user preferences.',
    'Group each answer by view: one steps block holds the whole sequence in items; one comparison block holds every row, and a row\'s keys are exactly the column names, one value per column; only select fields declare options; use text blocks for prose. Do not split one view across several blocks.',
    'Forms and optional comparison submit_label create a visible follow-up user request ONLY on a user click. submit_label is a top-level sibling of columns/rows/fields, never a key inside a row. Local edits/filter/sort/calculations make no model calls. Actions remain disabled until completion. No scripts, HTML, URLs, arbitrary formulas, queries or mutations.',
    ...(names.includes('form') ? [
      'Form fields collect the user\'s answers, not your guesses. Omit default unless the user supplied that field\'s value or explicitly asked you to choose defaults. Required means an intentional answer is needed, not that you should prefill one. Bounds and offered choices do not imply a default; never use a midpoint, first option, typical value, or an example value as a preference.',
      'For example, "ask for days per week (1–7)" leaves days blank (no default). "Use my 3 days per week as the initial value" permits default:3. "Collect English or Spanish" leaves language blank; "start with Spanish" permits default:"Spanish". Keep unknown fields blank even when another field has a supported default.',
    ] : []),
    ...(names.includes('metrics') ? [
      'Metrics: follow the requested aggregate exactly: sum adds values, average divides their sum by the number of observations, count counts observations INCLUDING zero-valued observations. unit describes each observation, never the aggregate count: the host automatically labels count as items. For North=10 km, South=15 km, East=0 km with count requested, use unit:"km", aggregate:"count", and all three original values; the host displays 3 items, not 25 km or 25 items.',
      'Preserve dataset membership, labels, signs, zero values and decimals exactly. Do not round source values or replace a zero row. Never insert a derived sum/average/count row into data: the host computes it from the original observations. For A=0.00001 mg, B=0.00002 mg, C=0 mg, keep those three rows with unit:"mg", aggregate:"sum"; do not append a Total row. One metrics block has one observation unit; use comparison or text for unlike units rather than relabeling or silently converting them.',
    ] : []),
    ...(names.includes('comparison') ? [
      'Comparisons: preserve supplied values and their per-row units. When conversion is not requested, do not normalize units or add derived conversion columns. For A=2 kg at 12 USD and B=500 g at 4 USD, keep 2 kg and 500 g with the original prices; do not add price per kg/g. Supplied figures are user-provided inputs, not independently verified measurements.',
      'Separate qualitative judgments from supplied evidence in the actual cells, not only in an introductory disclaimer. Include a Basis column when mixing opinions with observations. Label ordinary user-provided figures "Supplied input". Use "Supplied observation (one trial only)" ONLY when the user actually supplied a trial or measurement: never invent that provenance for a price, weight or other input. Label opinions "Qualitative expectation; depends on setup and use". If no measurements or sources were supplied, say measured evidence was not supplied.',
      'Keep qualitative comparisons brief and about concrete operations and configuration: reordering entries, assigning times, or displaying overlaps. Use conditional cells such as "May take more setup if you assign time slots" and "May help reveal conflicts when durations are entered". Limit the comparison to these observable operations; omit inferred emotional, cognitive, physiological or productivity effects and appeals to authority. A disclaimer does not justify unsupported claims elsewhere. A supplied single trial establishes only that trial, not general performance.',
    ] : []),
    ...(names.includes('calculation') ? [
      'Calculations: one block contains all supplied items and one combined total. The host provides an independent quantity input for EVERY item, including items initially at zero, plus an optional shared quantity multiplier. Preserve the supplied initial quantities and unit_price values; never precompute a total as an item or split items into separate calculators. Edits recalculate locally without a follow-up message.',
      'scale_label must be "Quantity multiplier": it is dimensionless, not Total Cost, Hours, USD or a single item count. unit names the subtotal/total unit; unit_price is the supplied value per quantity (including noncurrency values such as minutes per session). For a desk at 320 USD, chair at 210 USD and monitor at 180 USD, initially 1/2/0, emit all three items with quantities 1, 2, 0 and unit:"USD"; the user can change them to 1/1/1 for a combined 710 USD. Keep fractions and zero quantities/prices.',
    ] : []),
    'Limits: 12 blocks, 64 KiB total source. Invalid blocks show a readable fallback. Schema for each line:',
    JSON.stringify({oneOf: names.map(name => variants[name])}),
    'Examples (independent lines):', ...names.filter(name=>examples[name]).map(name=>JSON.stringify(examples[name])),
    ...(names.includes('comparison') ? [JSON.stringify({type:'comparison',title:'Illustrative qualitative comparison; no measurements supplied',columns:['Operation','Method A','Method B','Basis'],rows:[{'Operation':'Reordering entries','Method A':'May be simpler if entries are independent','Method B':'May require extra edits if entries are linked','Basis':'Qualitative expectation; depends on setup and use'}]})] : []),
    ...(names.includes('comparison') ? [JSON.stringify({type:'comparison',title:'Illustrative options: observation versus expectation',columns:['Aspect','Option A','Option B','Basis'],rows:[{'Aspect':'Setup time in one supplied trial','Option A':'12 seconds','Option B':'18 seconds','Basis':'Supplied observation (limited to this trial)'},{'Aspect':'Flexibility','Option A':'May be easier to rearrange if entries have no assigned times','Option B':'May make timing easier to see if entries have assigned times','Basis':'Qualitative expectation; depends on setup and use'}]})] : []),
    ...(names.includes('metrics') ? [
      'Final metrics check before emitting: compare every (label, value, observation unit) with the user\'s original dataset, including zeros; then check the requested aggregate separately. A count does NOT change the measured quantity or its unit. Copy the original measurement unit into unit even for count: km stays km, m stays m, mg stays mg. Only the host\'s aggregate output becomes items. Do not copy items into unit from the requested aggregate label.',
    ] : []),
  ].join('\n');
}
