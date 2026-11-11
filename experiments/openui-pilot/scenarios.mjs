// Synthetic, fixed evidence: generation is presentation, not data invention.
const monthly = {
  labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
  series: [{category: 'Revenue', values: [12, 15, 14, 18, 21, 24]}],
  xLabel: 'Month', yLabel: 'Revenue (USD thousands)',
};
const employees = [
  {label: 'Name', data: ['Ada', 'Ben', 'Cleo', 'Dev', 'Eli']},
  {label: 'Department', data: ['Research', 'Sales', 'Engineering', 'Research', 'Support']},
  {label: 'Salary (USD)', data: [98000, 85000, 110000, 95000, 72000]},
];
const source = 'Source: Bloom pilot fixture v1 (synthetic)';

export const scenarios = [
  {
    id: 'simple-table',
    request: 'Show the five employees in a table. Keep the supplied column names and row order.',
    data: {columns: employees},
    required: ['Table'], columns: employees,
  },
  {
    id: 'chart-with-data',
    request: 'Show a Total Revenue metric and a BarChart of six months of revenue. The total is USD 104 thousand.',
    data: {...monthly, total: 104, totalUnit: 'USD thousands'},
    required: ['BarChart'], chart: {...monthly, type: 'BarChart'}, texts: ['104'],
  },
  {
    id: 'dashboard',
    request: 'Show a revenue dashboard with the total, a LineChart, and a table of monthly revenue. Total: USD 104 thousand.',
    data: {...monthly, total: 104, columns: [
      {label: 'Month', data: monthly.labels},
      {label: 'Revenue (USD thousands)', data: monthly.series[0].values},
    ]},
    required: ['LineChart', 'Table'], chart: {...monthly, type: 'LineChart'}, texts: ['104'],
    columns: [
      {label: 'Month', data: monthly.labels},
      {label: 'Revenue (USD thousands)', data: monthly.series[0].values},
    ],
  },
  {
    id: 'informational-display',
    request: 'Explain three synthetic telescope designs using metric cards, a mirror diameter BarChart, and an instrument table. Include the short description verbatim.',
    data: {
      description: 'These are fictional designs for testing display fidelity.',
      labels: ['Aster', 'Birch', 'Cedar'],
      series: [{category: 'Mirror diameter', values: [6.5, 2.4, 0.85]}],
      xLabel: 'Design', yLabel: 'Mirror diameter (m)',
      columns: [
        {label: 'Instrument', data: ['Camera', 'Spectrometer']},
        {label: 'Band', data: ['Near infrared', 'Mid infrared']},
      ],
    },
    required: ['BarChart', 'Table'], texts: ['These are fictional designs for testing display fidelity.'],
    chart: {
      type: 'BarChart', labels: ['Aster', 'Birch', 'Cedar'],
      series: [{category: 'Mirror diameter', values: [6.5, 2.4, 0.85]}],
      xLabel: 'Design', yLabel: 'Mirror diameter (m)',
    },
    columns: [
      {label: 'Instrument', data: ['Camera', 'Spectrometer']},
      {label: 'Band', data: ['Near infrared', 'Mid infrared']},
    ],
  },
].map(s => ({...s, source, texts: [...(s.texts || []), source]}));

export function userPrompt(scenario) {
  return `${scenario.request}\nUse only these supplied facts; do not invent values. ` +
    `Include this exact visible source label: ${scenario.source}\n` +
    `Evidence snapshot:\n${JSON.stringify(scenario.data, null, 2)}`;
}
