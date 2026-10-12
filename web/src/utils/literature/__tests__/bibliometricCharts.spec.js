import assert from 'node:assert/strict'

import {
  buildDistributionOption,
  buildNetworkOption,
  buildTopBarsOption,
  buildYearlyTrendOption,
  kindLabel,
  languageLabel,
  truncateText
} from '../bibliometricCharts.js'

const theme = {
  colors: ['#265C96', '#009485', '#E8A035'],
  textColor: '#666',
  splitLineColor: '#eee',
  axisLineColor: '#ccc'
}

// 年度趋势：语言并集出 series，缺失桶补零，年份升序
const yearly = [
  { year: 2023, count: 1, by_language: { en: 1 } },
  { year: 2024, count: 0, by_language: {} },
  { year: 2025, count: 2, by_language: { en: 1, zh: 1 } }
]
const trend = buildYearlyTrendOption(yearly, theme)
assert.deepEqual(trend.xAxis.data, [2023, 2024, 2025])
assert.deepEqual(trend.series.map((series) => series.name).sort(), ['中文', '英文'])
const enSeries = trend.series.find((series) => series.name === '英文')
const zhSeries = trend.series.find((series) => series.name === '中文')
assert.deepEqual(enSeries.data, [1, 0, 1])
assert.deepEqual(zhSeries.data, [0, 0, 1])
assert.ok(trend.series.every((series) => series.stack === 'total'))

// Top 条形：倒序使榜首在顶部；长名截断
const bars = buildTopBarsOption(
  [
    { name: 'Rice Science', count: 9 },
    { name: 'Nature Food: 无机营养与水稻品质专刊综述合辑', count: 5 }
  ],
  theme
)
assert.deepEqual(bars.yAxis.data, ['Nature Food: 无机营养与水稻品质专刊综述合辑', 'Rice Science'])
assert.deepEqual(bars.series[0].data, [5, 9])
assert.ok(bars.yAxis.axisLabel.formatter(bars.yAxis.data[0]).endsWith('…'))

// 分布环形：按计数降序 + labelOf 映射
const donut = buildDistributionOption({ paper: 12, thesis: 3 }, theme, kindLabel)
assert.deepEqual(donut.series[0].data, [
  { name: '论文', value: 12 },
  { name: '学位论文', value: 3 }
])
assert.equal(languageLabel('en'), '英文')
assert.equal(languageLabel('unknown'), '未标注')

// 共现网络：节点大小按频次占比（rice 30/30→max，grain/yield 10/30→中间档），边宽随共现次数增长且有上限
const network = buildNetworkOption(
  {
    nodes: [
      { id: 'rice', label: 'rice', weight: 30 },
      { id: 'grain', label: 'grain number', weight: 10 },
      { id: 'yield', label: 'yield', weight: 10 }
    ],
    links: [
      { source: 'grain', target: 'rice', weight: 2 },
      { source: 'rice', target: 'yield', weight: 9 }
    ]
  },
  theme,
  { minSize: 8, maxSize: 44 }
)
const [riceNode, grainNode, yieldNode] = network.series[0].data
assert.equal(riceNode.symbolSize, 44)
assert.equal(grainNode.symbolSize, 20)
assert.equal(yieldNode.symbolSize, 20)
const [lightLink, heavyLink] = network.series[0].links
assert.ok(heavyLink.lineStyle.width > lightLink.lineStyle.width)
assert.ok(heavyLink.lineStyle.width <= 4)
// 官网清单字段可能携带 HTML：tooltip 必须走 canvas 富文本而不是 innerHTML。
for (const option of [trend, bars, donut, network]) {
  assert.equal(option.tooltip.renderMode, 'richText')
}
// 同输入同输出（确定性渲染）
assert.deepEqual(buildNetworkOption, buildNetworkOption)

// 截断工具
assert.equal(truncateText('abc', 5), 'abc')
assert.equal(truncateText('abcdef', 5), 'abcde…')

console.log('bibliometricCharts.spec.js: all assertions passed')
