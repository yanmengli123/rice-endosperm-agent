import assert from 'node:assert/strict'
import { TOPIC_CHARTS, buildTopicChart } from '../topicCharts.js'

const theme = { colors: ['#123456', '#654321'], textColor: '#111111', splitLineColor: '#eeeeee' }
const data = {
  topics: [
    {
      topic: 0,
      label: '<script>Rice</script>',
      count: 2,
      keywords: [{ term: 'grain', weight: 0.3 }]
    },
    { topic: 1, label: 'Stress', count: 1, keywords: [{ term: 'gene', weight: 0.2 }] }
  ],
  assignments: [
    { document_key: 'a', topic: 0, x: 1, y: 2 },
    { document_key: 'b', topic: 0, x: 2, y: 1 },
    { document_key: 'c', topic: 1, x: -1, y: -2 },
    { document_key: 'd', topic: -1, x: 0, y: 0 }
  ],
  metadata: {
    a: { source: 'pubmed', title: 'Rice A' },
    b: { source: 'wos' },
    c: { source: 'cnki' },
    d: { source: 'cnki' }
  },
  yearly: [
    { year: 2024, topic: 0, count: 2, denominator: 4 },
    { year: 2024, topic: 1, count: 1, denominator: 4 },
    { year: 2024, topic: -1, count: 1, denominator: 4 }
  ],
  similarity: [{ a: 0, b: 1, cosine: 0.8 }],
  hierarchy: [[0, 1, 0.2, 3]],
  candidates: [
    { min_cluster_size: 10, coverage: 0.75, silhouette: null, npmi: 0.3, diversity: 0.8 }
  ]
}
assert.equal(TOPIC_CHARTS.length, 15)
for (const [kind] of TOPIC_CHARTS) {
  const original = JSON.stringify(data)
  const option = buildTopicChart(kind, data, theme, 0)
  assert.ok(option.series?.length || option.graphic?.length, kind)
  assert.equal(JSON.stringify(data), original, 'Chart builder must not mutate frozen input')
  assert.equal(option.tooltip.renderMode, 'richText', 'Source text cannot render as HTML')
}
const trends = buildTopicChart('trends', data, theme)
assert.deepEqual(
  trends.series.map((s) => s.data[0]),
  [50, 25, 25],
  'Yearly denominator includes outliers'
)
const pareto = buildTopicChart('pareto', data, theme)
assert.deepEqual(
  pareto.series[1].data,
  [50, 75],
  'Cumulative share must not silently exclude noise'
)
const groups = buildTopicChart('groups', data, theme)
const cnki = groups.xAxis.data.indexOf('cnki')
assert.equal(
  groups.series.reduce((n, s) => n + s.data[cnki], 0),
  100
)
const points = buildTopicChart('documents', data, theme)
assert.equal(points.series[0].data.length, 4, 'Do not silently sample away imported records')
assert.equal(
  buildTopicChart('quality', data, theme).series.find((s) => s.name === 'silhouette').data[0],
  null
)
const future = {
  ...data,
  analysis_year: 2024,
  yearly: [...data.yearly, { year: 2027, topic: 0, count: 1, denominator: 1 }]
}
assert.deepEqual(
  buildTopicChart('trends', future, theme).xAxis.data,
  [2024],
  'Future records stay out of the frozen trend window'
)
assert.equal(
  points.series[0].large,
  false,
  'Per-point colors and paper click navigation must remain available'
)
const countries = {
  ...data,
  metadata: {
    a: { countries: ['China', 'China', 'USA'] },
    b: { countries: ['China'] },
    c: { countries: [] }
  }
}
const countryGroups = buildTopicChart('groups', countries, theme, null, { groupBy: 'country' })
assert.deepEqual(countryGroups.xAxis.data, ['China', 'USA', 'unknown'])
for (const index of [0, 1, 2])
  assert.equal(
    countryGroups.series.reduce((sum, s) => sum + s.data[index], 0),
    100,
    'Within-group denominator includes noise; duplicate affiliations do not duplicate a record'
  )
assert.equal(countryGroups.series[0].data[0], 100)
const manyGroups = {
  ...data,
  metadata: { a: { affiliations: Array.from({ length: 25 }, (_, i) => `Institute${i}`) } }
}
const boundedGroups = buildTopicChart('groups', manyGroups, theme, null, { groupBy: 'affiliation' })
assert.ok(
  boundedGroups.xAxis.data.length <= 21 && boundedGroups.xAxis.data.includes('其他（Top20 之外）')
)
const weighted = { ...data, topics: [{ ...data.topics[0], word_frequencies: [['RISBZ1', 12]] }] }
const lexicalCloud = buildTopicChart('cloud', weighted, theme, 0, { wordWeight: 'frequency' })
assert.equal(
  lexicalCloud.graphic[0].children[0].style.text,
  'RISBZ1',
  'Lexical frequency is not c-TF-IDF'
)
assert.equal(
  buildTopicChart('cloud', weighted, theme, -1).graphic[0].children.length,
  0,
  'Noise must not silently display topic 0 words'
)
const reversed = { ...data, topics: [...data.topics].reverse() }
const tree = buildTopicChart('hierarchy', reversed, theme).series[0].data[0]
assert.deepEqual(
  tree.children.map((node) => node.topic),
  [0, 1],
  'Hierarchy leaf IDs are sorted independently of file order'
)
console.log(
  'Topic charts passed: 15 views, immutable input, exact denominators, full point coverage, HTML-safe tooltips'
)
