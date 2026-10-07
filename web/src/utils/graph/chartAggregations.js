/**
 * 当前已加载子图的类型聚合。计量单位为去重三元组，不是来源片段数。
 * 弦带是无序类型对；方向×谓词联合分布及原始边留作证据下钻。
 * 类型内部关系独立展示，不以伪造弦带权重补偿布局。
 */
import {
  NODE_LABEL_COLORS,
  getEdgeVisualLabel,
  getNodeVisualLabel,
  getPaletteColor,
  isEntityNode,
  isEntityRelationEdge
} from './graphPalette.js'

export const DEFAULT_CHORD_MAX_TYPES = 10

export function resolveDefaultMaxTypes(typeCount) {
  if (!Number.isFinite(typeCount) || typeCount <= 12) return 0
  if (typeCount <= 30) return 15
  return DEFAULT_CHORD_MAX_TYPES
}

const compareName = (a, b) => a.localeCompare(b) || (a < b ? -1 : a > b ? 1 : 0)
const byCount = (a, b) => b.count - a.count || compareName(a.source || a.type, b.source || b.type)

export function buildChordData(graphData, { maxTypes = DEFAULT_CHORD_MAX_TYPES } = {}) {
  const typeById = new Map()
  let entityNodes = 0
  for (const node of graphData?.nodes || []) {
    if (!isEntityNode(node)) continue
    entityNodes += 1
    typeById.set(String(node.id), getNodeVisualLabel(node))
  }

  const pairs = new Map()
  const seenFacts = new Set()
  const allRelationTypes = new Set()
  const crossRelationTypes = new Set()
  let relationEdges = 0
  let danglingEdges = 0
  let duplicateEdges = 0
  for (const edge of graphData?.edges || []) {
    if (!isEntityRelationEdge(edge)) continue
    const predicate = getEdgeVisualLabel(edge)
    const tripleId = edge.triple_id || edge.properties?.triple_id
    const factKey = JSON.stringify(
      tripleId
        ? ['triple', tripleId]
        : ['semantic', String(edge.source_id), predicate, String(edge.target_id)]
    )
    if (seenFacts.has(factKey)) {
      duplicateEdges += 1
      continue
    }
    seenFacts.add(factKey)
    relationEdges += 1
    const sourceType = typeById.get(String(edge.source_id))
    const targetType = typeById.get(String(edge.target_id))
    if (!sourceType || !targetType) {
      danglingEdges += 1
      continue
    }
    allRelationTypes.add(predicate)
    if (sourceType !== targetType) crossRelationTypes.add(predicate)
    const orderedTypes = [sourceType, targetType].sort(compareName)
    const key = JSON.stringify(orderedTypes)
    const pair = pairs.get(key) || {
      source: orderedTypes[0],
      target: orderedTypes[1],
      value: 0,
      directions: new Map(),
      relationTypes: new Map(),
      semantics: new Map()
    }
    pair.value += 1
    const directionKey = JSON.stringify([sourceType, targetType])
    const direction = pair.directions.get(directionKey) || {
      source: sourceType,
      target: targetType,
      count: 0
    }
    direction.count += 1
    pair.directions.set(directionKey, direction)
    pair.relationTypes.set(predicate, (pair.relationTypes.get(predicate) || 0) + 1)
    const semanticKey = JSON.stringify([sourceType, predicate, targetType])
    const semantic = pair.semantics.get(semanticKey) || {
      source: sourceType,
      type: predicate,
      target: targetType,
      count: 0,
      edges: []
    }
    semantic.count += 1
    semantic.edges.push(edge)
    pair.semantics.set(semanticKey, semantic)
    pairs.set(key, pair)
  }

  const entityCounts = new Map()
  for (const name of typeById.values()) entityCounts.set(name, (entityCounts.get(name) || 0) + 1)
  const totals = new Map([...entityCounts.keys()].map((name) => [name, 0]))
  const internalCounts = new Map()
  for (const pair of pairs.values()) {
    if (pair.source === pair.target) {
      internalCounts.set(pair.source, pair.value)
      if (!totals.has(pair.source)) totals.set(pair.source, 0)
    } else {
      totals.set(pair.source, (totals.get(pair.source) || 0) + pair.value)
      totals.set(pair.target, (totals.get(pair.target) || 0) + pair.value)
    }
  }
  const rankedTypes = [...totals.keys()].sort(
    (a, b) =>
      totals.get(b) +
        (internalCounts.get(b) || 0) -
        (totals.get(a) + (internalCounts.get(a) || 0)) || compareName(a, b)
  )
  const links = []
  const internalRelations = []
  for (const pair of pairs.values()) {
    const entry = {
      source: pair.source,
      target: pair.target,
      value: pair.value,
      directions: [...pair.directions.values()].sort(
        (a, b) => byCount(a, b) || compareName(a.target, b.target)
      ),
      relationTypes: [...pair.relationTypes]
        .map(([type, count]) => ({ type, count }))
        .sort(byCount),
      semantics: [...pair.semantics.values()].sort(
        (a, b) =>
          b.count - a.count ||
          compareName(a.source, b.source) ||
          compareName(a.type, b.type) ||
          compareName(a.target, b.target)
      )
    }
    for (const semantic of entry.semantics) {
      semantic.edges.sort((a, b) =>
        compareName(
          String(a.triple_id || a.properties?.triple_id || a.id),
          String(b.triple_id || b.properties?.triple_id || b.id)
        )
      )
    }
    if (pair.source === pair.target) {
      internalRelations.push(entry)
    } else {
      entry.lineStyle = { color: getPaletteColor(entry.source, NODE_LABEL_COLORS) }
      links.push(entry)
    }
  }
  const byPair = (a, b) =>
    b.value - a.value || compareName(a.source, b.source) || compareName(a.target, b.target)
  links.sort(byPair)
  internalRelations.sort(byPair)

  // 两种有向表示共享含内部关系的模型，方向下钻直接引用该方向的原始事实。
  const directedLinks = []
  const outgoing = new Map(),
    incoming = new Map()
  for (const link of [...links, ...internalRelations]) {
    for (const direction of link.directions) {
      const semantics = link.semantics.filter(
        (s) => s.source === direction.source && s.target === direction.target
      )
      const relationTypes = semantics.map((s) => ({ type: s.type, count: s.count })).sort(byCount)
      directedLinks.push({
        source: direction.source,
        target: direction.target,
        value: direction.count,
        relationTypes,
        semantics,
        directed: true
      })
      outgoing.set(direction.source, (outgoing.get(direction.source) || 0) + direction.count)
      incoming.set(direction.target, (incoming.get(direction.target) || 0) + direction.count)
    }
  }
  directedLinks.sort(byPair)

  const drawnTotals = new Map()
  for (const link of links) {
    drawnTotals.set(link.source, (drawnTotals.get(link.source) || 0) + link.value)
    drawnTotals.set(link.target, (drawnTotals.get(link.target) || 0) + link.value)
  }
  const nodes = rankedTypes.map((name) => ({
    name,
    itemStyle: { color: getPaletteColor(name, NODE_LABEL_COLORS) },
    total: totals.get(name),
    drawnTotal: drawnTotals.get(name) || 0,
    selfLoops: internalCounts.get(name) || 0,
    relationTotal: totals.get(name) + (internalCounts.get(name) || 0),
    entityCount: entityCounts.get(name),
    outgoing: outgoing.get(name) || 0,
    incoming: incoming.get(name) || 0
  }))
  const selfLoopEdges = [...internalCounts.values()].reduce((sum, count) => sum + count, 0)
  const full = {
    nodes,
    links,
    directedLinks,
    internalRelations,
    hasRibbons: links.length > 0,
    empty: relationEdges === 0,
    meta: {
      entityNodes,
      relationEdges,
      duplicateEdges,
      danglingEdges,
      selfLoopEdges,
      crossTypeEdges: relationEdges - selfLoopEdges - danglingEdges,
      drawnEdges: links.reduce((sum, link) => sum + link.value, 0),
      relationTypeCount: allRelationTypes.size,
      crossRelationTypeCount: crossRelationTypes.size,
      typeCount: rankedTypes.length,
      shownTypes: nodes.length,
      drawnInternalEdges: selfLoopEdges,
      drawnRelationEdges: relationEdges - danglingEdges,
      omittedInternalEdges: 0,
      omittedRelationEdges: 0,
      isolatedTypes: nodes.filter((n) => n.relationTotal === 0).length,
      omittedTypes: [],
      omittedWeight: 0,
      maxTypes: Infinity
    }
  }
  return applyTypeLimit(full, maxTypes)
}

/**
 * 对全量聚合结果做廉价的 Top-N 折叠（O(类型对)，不重扫边）。
 *
 * 面板聚合链使用「单遍 buildChordData(maxTypes=0) + applyTypeLimit(limit)」替代
 * 对同一数据跑两遍全量聚合：档位切换（Top-N ↔ 全部）只走本函数，口径与
 * buildChordData 内建折叠完全一致（由 spec 等价性用例锁定）。
 *
 * @param {object} full buildChordData(..., { maxTypes: 0 }) 的返回值
 * @param {number} limit 0/负数/≥类型数时原样返回
 */
export function applyTypeLimit(full, limit) {
  const typeCount = full?.meta?.typeCount || 0
  if (!full || !Number.isFinite(limit) || limit <= 0 || limit >= typeCount) {
    return full
  }
  const floor = Math.floor(limit)
  const kept = new Set(full.nodes.slice(0, floor).map((n) => n.name))
  const links = full.links.filter((l) => kept.has(l.source) && kept.has(l.target))
  const directedLinks = full.directedLinks.filter((l) => kept.has(l.source) && kept.has(l.target))
  const drawnTotals = new Map()
  for (const link of links) {
    drawnTotals.set(link.source, (drawnTotals.get(link.source) || 0) + link.value)
    drawnTotals.set(link.target, (drawnTotals.get(link.target) || 0) + link.value)
  }
  const nodes = full.nodes.slice(0, floor).map((n) => ({
    ...n,
    drawnTotal: drawnTotals.get(n.name) || 0
  }))
  // internalRelations 保持全量清单口径（模板声明「全部已加载类型」），不随档位折叠
  const omittedTypes = full.nodes.slice(floor).map((n) => ({
    name: n.name,
    total: n.total,
    internalRelations: n.selfLoops
  }))
  const omittedWeight = full.links
    .filter((l) => !(kept.has(l.source) && kept.has(l.target)))
    .reduce((sum, l) => sum + l.value, 0)
  const drawnInternalEdges = nodes.reduce((sum, node) => sum + node.selfLoops, 0)
  const drawnEdges = links.reduce((sum, link) => sum + link.value, 0)
  return {
    nodes,
    links,
    directedLinks,
    internalRelations: full.internalRelations,
    hasRibbons: links.length > 0,
    empty: full.empty,
    meta: {
      ...full.meta,
      shownTypes: nodes.length,
      omittedTypes,
      omittedWeight,
      drawnEdges,
      drawnInternalEdges,
      drawnRelationEdges: drawnEdges + drawnInternalEdges,
      omittedInternalEdges: full.meta.selfLoopEdges - drawnInternalEdges,
      omittedRelationEdges: omittedWeight + full.meta.selfLoopEdges - drawnInternalEdges,
      maxTypes: floor
    }
  }
}
