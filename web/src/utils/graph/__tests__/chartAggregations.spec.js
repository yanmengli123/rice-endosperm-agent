/**
 * 图谱洞察弦图聚合纯逻辑测试（裸 node 运行，与 reviewMeta.spec.js 同模式）：
 *   node src/utils/graph/__tests__/chartAggregations.spec.js
 */
import assert from 'node:assert/strict'
import process from 'node:process'

import {
  applyTypeLimit,
  buildChordData,
  DEFAULT_CHORD_MAX_TYPES,
  resolveDefaultMaxTypes
} from '../chartAggregations.js'
import { NODE_LABEL_COLORS, getPaletteColor } from '../graphPalette.js'
import { formatChordTooltip } from '../chartTooltips.js'

let passed = 0
let failed = 0

function test(name, fn) {
  try {
    fn()
    passed += 1
    console.log(`✓ ${name}`)
  } catch (error) {
    failed += 1
    console.error(`✗ ${name}\n  ${error?.stack || error}`)
  }
}

const node = (id, type) => ({ id, name: `${type}-${id}`, type })
const edge = (id, sourceId, targetId, type = 'REGULATES') => ({
  id,
  source_id: sourceId,
  target_id: targetId,
  type,
  properties: { triple_id: id }
})

// ── 基础聚合：无序对合并 + 方向明细保留 ──
test('A→B 与 B→A 合并为一根弦带，方向明细保留', () => {
  const data = buildChordData({
    nodes: [node('g1', 'Gene'), node('p1', 'Phenotype')],
    edges: [edge('e1', 'g1', 'p1'), edge('e2', 'g1', 'p1'), edge('e3', 'p1', 'g1')]
  })
  assert.equal(data.links.length, 1)
  assert.equal(data.links[0].value, 3)
  assert.deepEqual(
    data.links[0].directions,
    [
      { source: 'Gene', target: 'Phenotype', count: 2 },
      { source: 'Phenotype', target: 'Gene', count: 1 }
    ],
    '方向明细按计数降序'
  )
  const gene = data.nodes.find((n) => n.name === 'Gene')
  const phenotype = data.nodes.find((n) => n.name === 'Phenotype')
  assert.equal(gene.total, 3)
  assert.equal(phenotype.total, 3)
  assert.equal(data.empty, false)
  assert.equal(data.meta.relationEdges, 3)
  assert.equal(data.meta.entityNodes, 2)
})

// ── 弧段计量口径：节点不设 value（echarts 弦图弧段大小 = 弦带求和） ──
test('chord 节点不携带 value 字段，弧段大小交由弦带求和决定', () => {
  const data = buildChordData({
    nodes: [node('a', 'Gene'), node('b', 'RNA')],
    edges: [edge('e1', 'a', 'b')]
  })
  for (const n of data.nodes) {
    assert.ok(!('value' in n), `节点 ${n.name} 不应设 value`)
  }
})

// ── 自环：不进弦带、不产生弧段计量，单独计数供 tooltip ──
test('同类型自环不计入弦带，聚合进 selfLoops 与 meta.selfLoopEdges', () => {
  const data = buildChordData({
    nodes: [node('g1', 'Gene'), node('g2', 'Gene')],
    edges: [edge('e1', 'g1', 'g2'), edge('e2', 'g1', 'g1'), edge('e3', 'g2', 'g2')]
  })
  // g1/g2 同为 Gene：三条边在类型层面全是自环（含跨实体的 Gene→Gene）
  assert.equal(data.links.length, 0)
  assert.equal(data.nodes.length, 1, '仅有自环的类型仍显示弧段（tooltip 呈现内部关系）')
  assert.equal(data.nodes[0].name, 'Gene')
  assert.equal(data.nodes[0].selfLoops, 3)
  assert.equal(data.nodes[0].total, 0)
  assert.equal(data.meta.selfLoopEdges, 3)
  assert.equal(data.meta.relationEdges, 3)
  assert.equal(data.empty, false, '有实体关系边（含自环）不算空态')
})

// ── 画布口径对齐：Chunk 节点与 MENTIONS 边排除 ──
test('Chunk 节点与 MENTIONS 边不进入统计', () => {
  const data = buildChordData({
    nodes: [node('c1', 'Chunk'), node('g1', 'Gene'), node('p1', 'Phenotype')],
    edges: [
      { id: 'm1', source_id: 'c1', target_id: 'g1', type: 'MENTIONS' },
      edge('e1', 'g1', 'p1')
    ]
  })
  assert.equal(data.meta.entityNodes, 2)
  assert.equal(data.meta.relationEdges, 1)
  assert.equal(data.nodes.length, 2)
  assert.ok(!data.nodes.some((n) => n.name === 'Chunk'))
})

// ── 悬空边：端点类型未知时跳过并计数，不产生弦带 ──
test('端点不在节点表的悬空边跳过并计入 danglingEdges', () => {
  const data = buildChordData({
    nodes: [node('g1', 'Gene')],
    edges: [edge('e1', 'g1', 'ghost'), edge('e2', 'ghost', 'g1')]
  })
  assert.equal(data.links.length, 0)
  assert.equal(data.meta.danglingEdges, 2)
  assert.equal(data.empty, false)
})

// ── 空态：只有 Chunk/MENTIONS 或无数据 ──
test('无实体关系边时 empty=true', () => {
  assert.equal(buildChordData({ nodes: [], edges: [] }).empty, true, '空数据')
  assert.equal(
    buildChordData({
      nodes: [node('c1', 'Chunk'), node('g1', 'Gene')],
      edges: [{ id: 'm1', source_id: 'c1', target_id: 'g1', type: 'MENTIONS' }]
    }).empty,
    true,
    '仅 Chunk/MENTIONS'
  )
})

// ── Top-N 折叠：超上限类型不产生弧段，折叠信息如实上报 ──
test('类型数超过 maxTypes 时按关系强度保留 Top-N 并折叠其余', () => {
  // 构造 5 个类型：T1..T3 强（互相成对），T4/T5 弱（各 1 条边）
  const nodes = [
    node('a', 'T1'),
    node('b', 'T2'),
    node('c', 'T3'),
    node('d', 'T4'),
    node('e', 'T5')
  ]
  const edges = [
    edge('e1', 'a', 'b'),
    edge('e2', 'a', 'b'),
    edge('e3', 'a', 'c'),
    edge('e4', 'b', 'c'),
    edge('e5', 'd', 'e')
  ]
  const data = buildChordData({ nodes, edges }, { maxTypes: 3 })
  assert.equal(data.nodes.length, 3)
  assert.equal(data.meta.typeCount, 5)
  assert.equal(data.meta.shownTypes, 3)
  // T4/T5 被折叠：保留 Top3 = T1/T2/T3，被折叠的弦带权重如实上报
  assert.equal(data.meta.omittedTypes.length, 2)
  assert.ok(data.meta.omittedTypes.some((t) => t.name === 'T4'))
  assert.ok(data.meta.omittedTypes.some((t) => t.name === 'T5'))
  assert.equal(data.meta.omittedWeight, 1)
  assert.ok(data.links.every((l) => l.value >= 1))
  // 不限制：maxTypes=0 表示全部
  const all = buildChordData({ nodes, edges }, { maxTypes: 0 })
  assert.equal(all.nodes.length, 5)
  assert.equal(all.meta.omittedTypes.length, 0)
})

test('默认上限为 10 个类型', () => {
  assert.equal(DEFAULT_CHORD_MAX_TYPES, 10)
})

// ── 排序确定性：强类型在前、弦带按权重降序 ──
test('节点按 total 降序、弦带按 value 降序，平局按名称稳定', () => {
  const nodes = [node('a', 'Gene'), node('b', 'Phenotype'), node('c', 'RNA')]
  const edges = [edge('e1', 'a', 'b'), edge('e2', 'a', 'b'), edge('e3', 'a', 'c')]
  const data = buildChordData({ nodes, edges })
  assert.deepEqual(
    data.nodes.map((n) => n.name),
    ['Gene', 'Phenotype', 'RNA'],
    'Gene=3 第一，Phenotype 与 RNA 均=1 按名称排序'
  )
  assert.equal(data.links[0].source, 'Gene')
  assert.equal(data.links[0].value, 2)
  assert.equal(data.links[1].value, 1)
})

// ── 跨视图颜色一致：弧段颜色 = 画布同款哈希取色 ──
test('弧段颜色与画布节点取色一致', () => {
  const data = buildChordData({
    nodes: [node('g1', 'Gene'), node('p1', 'Phenotype')],
    edges: [edge('e1', 'g1', 'p1')]
  })
  for (const n of data.nodes) {
    assert.equal(n.itemStyle.color, getPaletteColor(n.name, NODE_LABEL_COLORS))
  }
  assert.equal(data.links[0].lineStyle.color, getPaletteColor('Gene', NODE_LABEL_COLORS))
  // 分层契约：聚合层只输出主题无关的身份色；透明度等主题渲染参数
  // 由 GraphInsightPanel 按明暗主题注入，不得混入纯函数
  for (const link of data.links) {
    assert.ok(!('opacity' in (link.lineStyle || {})), '聚合输出不得包含主题相关 opacity')
  }
})

// ── L3 语义完整：弦带不得静默丢失关系类型 ──
test('弦带按类型对聚合 relationTypes，跨方向合并并按条数降序', () => {
  const data = buildChordData({
    nodes: [node('g1', 'Gene'), node('p1', 'Phenotype')],
    edges: [
      edge('e1', 'g1', 'p1', 'REGULATES'),
      edge('e2', 'g1', 'p1', 'REGULATES'),
      edge('e3', 'g1', 'p1', 'affects'),
      edge('e4', 'p1', 'g1', 'INTERACTS')
    ]
  })
  assert.deepEqual(
    data.links[0].relationTypes,
    [
      { type: 'REGULATES', count: 2 },
      { type: 'affects', count: 1 },
      { type: 'INTERACTS', count: 1 }
    ],
    '两种方向、三种谓词合并到同一弦带，按 count 降序、平局按 localeCompare'
  )
  assert.equal(data.meta.crossRelationTypeCount, 3)
})

// ── L4 口径对账：摘要行三段数字必须自洽 ──
test('口径三段式：relationEdges = crossTypeEdges + selfLoopEdges + danglingEdges', () => {
  const data = buildChordData({
    nodes: [node('g1', 'Gene'), node('g2', 'Gene'), node('p1', 'Phenotype')],
    edges: [
      edge('e1', 'g1', 'g2'), // 自环
      edge('e2', 'g1', 'p1'), // 跨类型
      edge('e3', 'g1', 'ghost') // 悬空
    ]
  })
  const m = data.meta
  assert.equal(m.relationEdges, 3)
  assert.equal(m.selfLoopEdges, 1)
  assert.equal(m.danglingEdges, 1)
  assert.equal(m.crossTypeEdges, 1)
  assert.equal(m.relationEdges, m.crossTypeEdges + m.selfLoopEdges + m.danglingEdges)
})

test('drawnEdges 如实反映折叠损益：全量模式 == crossTypeEdges，Top 模式 == 全量 - 折叠权重', () => {
  const nodes = [
    node('a', 'T1'),
    node('b', 'T2'),
    node('c', 'T3'),
    node('d', 'T4'),
    node('e', 'T5')
  ]
  const edges = [
    edge('e1', 'a', 'b'),
    edge('e2', 'a', 'b'),
    edge('e3', 'a', 'c'),
    edge('e4', 'b', 'c'),
    edge('e5', 'd', 'e')
  ]
  const full = buildChordData({ nodes, edges }, { maxTypes: 0 })
  assert.equal(full.meta.drawnEdges, full.meta.crossTypeEdges, '全量模式无折叠损益')

  const top3 = buildChordData({ nodes, edges }, { maxTypes: 3 })
  assert.equal(
    top3.meta.drawnEdges,
    full.meta.crossTypeEdges - top3.meta.omittedWeight,
    'Top 模式绘制数 = 全量跨类型 - 折叠权重（可对账）'
  )
  assert.equal(top3.meta.drawnEdges, 4, 'T4-T5 的 1 条随折叠类型不展示')
})

// ── Top-N 默认档位数据自适应 ──
test('resolveDefaultMaxTypes：≤12 全部 / 13–30 Top15 / >30 Top10', () => {
  assert.equal(resolveDefaultMaxTypes(0), 0, '空数据默认全量')
  assert.equal(resolveDefaultMaxTypes(12), 0)
  assert.equal(resolveDefaultMaxTypes(13), 15)
  assert.equal(resolveDefaultMaxTypes(30), 15)
  assert.equal(resolveDefaultMaxTypes(31), DEFAULT_CHORD_MAX_TYPES)
  assert.equal(resolveDefaultMaxTypes(581), DEFAULT_CHORD_MAX_TYPES)
})

test('同一三元组的片段平行边只计一条关系', () => {
  const data = buildChordData({
    nodes: [node('a', 'Gene'), node('b', 'Phenotype')],
    edges: [edge('e1', 'a', 'b'), edge('e2', 'a', 'b')].map((e) => ({
      ...e,
      properties: { triple_id: 'same-fact' }
    }))
  })
  assert.equal(data.meta.relationEdges, 1)
  assert.equal(data.links[0].value, 1)
})

test('方向与谓词联合分布保留且类型名含箭头不会损坏方向', () => {
  const data = buildChordData({
    nodes: [node('a', 'A→B'), node('b', 'C')],
    edges: [edge('e1', 'a', 'b', 'ACTIVATES'), edge('e2', 'b', 'a', 'INHIBITS')]
  })
  assert.deepEqual(
    data.links[0].semantics.map((s) => [s.source, s.type, s.target, s.count]),
    [
      ['A→B', 'ACTIVATES', 'C', 1],
      ['C', 'INHIBITS', 'A→B', 1]
    ]
  )
})

test('类型对的方向表示与颜色不随输入顺序变化', () => {
  const graph = {
    nodes: [node('a', 'Gene'), node('b', 'Phenotype')],
    edges: [edge('e1', 'a', 'b'), edge('e2', 'b', 'a')]
  }
  const first = buildChordData(graph)
  const reversed = buildChordData({ ...graph, edges: [...graph.edges].reverse() })
  assert.deepEqual(first, reversed)
})

test('只有同类型关系时保留谓词和真实关系入口，不要求渲染空弦图', () => {
  const data = buildChordData({
    nodes: [node('a', 'Gene'), node('b', 'Gene')],
    edges: [edge('e1', 'a', 'b', 'ACTIVATES')]
  })
  assert.equal(data.hasRibbons, false)
  assert.equal(data.internalRelations[0].value, 1)
  assert.equal(data.internalRelations[0].semantics[0].type, 'ACTIVATES')
  assert.equal(data.internalRelations[0].semantics[0].edges[0].id, 'e1')
  assert.equal(data.meta.relationTypeCount, 1)
})

test('Top-N 类型内部关系折叠也登记，弧段区分总关联与已绘制关联', () => {
  const data = buildChordData(
    {
      nodes: [node('a', 'A'), node('b', 'B'), node('c', 'C'), node('d', 'D')],
      edges: [edge('e1', 'a', 'b'), edge('e2', 'a', 'c'), edge('e3', 'd', 'd')]
    },
    { maxTypes: 2 }
  )
  assert.equal(data.meta.omittedTypes.length, 2)
  const a = data.nodes.find((n) => n.name === 'A')
  assert.equal(a.total, 2)
  assert.equal(a.drawnTotal, 1)
  assert.equal(data.meta.drawnEdges + data.meta.omittedWeight, data.meta.crossTypeEdges)
})

test('tooltip 对节点类型、关系谓词和回退名称完整转义', () => {
  const attack = '<img src=x onerror=alert(1)>"&'
  const nodeTip = formatChordTooltip({
    dataType: 'node',
    data: { name: attack, total: 2, drawnTotal: 1 }
  })
  const edgeTip = formatChordTooltip({
    dataType: 'edge',
    data: {
      source: attack,
      target: attack,
      value: 1,
      semantics: [{ source: attack, target: attack, type: attack, count: 1 }]
    }
  })
  for (const tip of [nodeTip, edgeTip, formatChordTooltip({ name: attack })]) {
    assert.ok(!tip.includes('<img'))
    assert.ok(tip.includes('&lt;img'))
    assert.ok(tip.includes('&quot;'))
    assert.ok(tip.includes('&amp;'))
  }
})

// ── 有向视图（桑基/热力）：directedLinks 与弦带跨表示一致 ──
test('directedLinks 按方向拆分：A→B 与 B→A 各自成条，值与谓词分布正确', () => {
  const data = buildChordData({
    nodes: [node('g1', 'Gene'), node('p1', 'Phenotype')],
    edges: [
      edge('e1', 'g1', 'p1', 'REGULATES'),
      edge('e2', 'g1', 'p1', 'REGULATES'),
      edge('e3', 'p1', 'g1', 'affects')
    ]
  })
  const forward = data.directedLinks.find((l) => l.source === 'Gene' && l.target === 'Phenotype')
  const backward = data.directedLinks.find((l) => l.source === 'Phenotype' && l.target === 'Gene')
  assert.ok(forward && backward, '双向各有独立条目')
  assert.equal(forward.value, 2)
  assert.equal(backward.value, 1)
  assert.deepEqual(
    forward.relationTypes,
    [{ type: 'REGULATES', count: 2 }],
    '正向谓词分布来自该方向的 semantics 子集'
  )
  assert.deepEqual(backward.relationTypes, [{ type: 'affects', count: 1 }])
})

test('跨表示一致性：Σ directedLinks.value == meta.drawnRelationEdges（含内部关系）', () => {
  const nodes = [
    node('a', 'T1'),
    node('b', 'T2'),
    node('c', 'T3'),
    node('d', 'T4'),
    node('e', 'T5')
  ]
  const edges = [
    edge('e1', 'a', 'b'),
    edge('e2', 'b', 'a'),
    edge('e3', 'a', 'c'),
    edge('e4', 'b', 'c'),
    edge('e5', 'd', 'e'),
    edge('e6', 'e', 'd')
  ]
  for (const maxTypes of [0, 3]) {
    const data = buildChordData({ nodes, edges }, { maxTypes })
    const sum = data.directedLinks.reduce((s, l) => s + l.value, 0)
    assert.equal(sum, data.meta.drawnRelationEdges, `maxTypes=${maxTypes} 时两表示计数必须一致`)
    for (const l of data.directedLinks) {
      assert.ok(
        data.nodes.some((n) => n.name === l.source) && data.nodes.some((n) => n.name === l.target),
        `折叠后 directedLinks 不得含未保留类型（${l.source}→${l.target}）`
      )
    }
  }
})

test('directedLinks 包含类型内部关系，流向和矩阵共享真实事实', () => {
  const data = buildChordData({
    nodes: [node('g1', 'Gene'), node('g2', 'Gene'), node('p1', 'Phenotype')],
    edges: [edge('e1', 'g1', 'g2'), edge('e2', 'g1', 'p1')]
  })
  const internal = data.directedLinks.find((l) => l.source === l.target)
  assert.equal(internal.value, 1)
  assert.equal(internal.semantics[0].edges[0].id, 'e1')
  assert.equal(data.meta.selfLoopEdges, 1)
})

// ── applyTypeLimit：廉价折叠与内建折叠等价（单遍聚合 + 档位切换不重扫边） ──
test('applyTypeLimit 与 buildChordData 内建折叠逐字段等价', () => {
  const nodes = [
    node('a', 'T1'),
    node('b', 'T2'),
    node('c', 'T3'),
    node('d', 'T4'),
    node('e', 'T5')
  ]
  const edges = [
    edge('e1', 'a', 'b'),
    edge('e2', 'b', 'a'),
    edge('e3', 'a', 'c'),
    edge('e4', 'b', 'c'),
    edge('e5', 'd', 'e'),
    edge('e6', 'e', 'd'),
    edge('e7', 'c', 'c')
  ]
  const full = buildChordData({ nodes, edges }, { maxTypes: 0 })
  const builtin = buildChordData({ nodes, edges }, { maxTypes: 2 })
  const folded = applyTypeLimit(full, 2)
  assert.deepEqual(
    folded.nodes.map((n) => n.name),
    builtin.nodes.map((n) => n.name)
  )
  assert.deepEqual(
    folded.links.map((l) => [l.source, l.target, l.value]),
    builtin.links.map((l) => [l.source, l.target, l.value])
  )
  assert.deepEqual(
    folded.directedLinks.map((l) => [l.source, l.target, l.value]),
    builtin.directedLinks.map((l) => [l.source, l.target, l.value])
  )
  assert.equal(folded.meta.drawnEdges, builtin.meta.drawnEdges)
  assert.equal(folded.meta.omittedWeight, builtin.meta.omittedWeight)
  assert.deepEqual(
    folded.meta.omittedTypes.map((t) => t.name).sort(),
    builtin.meta.omittedTypes.map((t) => t.name).sort()
  )
  // 内部关系清单保持全量口径（模板声明「全部已加载类型」）
  assert.equal(folded.internalRelations.length, full.internalRelations.length)
  // meta 不可变字段不被折叠改写
  assert.equal(folded.meta.relationEdges, full.meta.relationEdges)
  assert.equal(folded.meta.typeCount, full.meta.typeCount)
})

test('applyTypeLimit 边界：limit=0/超界/Infinity 时原样返回', () => {
  const full = buildChordData(
    {
      nodes: [node('a', 'T1'), node('b', 'T2')],
      edges: [edge('e1', 'a', 'b')]
    },
    { maxTypes: 0 }
  )
  for (const limit of [0, -1, 2, 99]) {
    const out = applyTypeLimit(full, limit)
    assert.equal(out, full, `limit=${limit} 应原样返回同一对象`)
  }
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
