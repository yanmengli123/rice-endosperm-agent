/** Pure helpers for the deterministic conversation graph attachment. */

const isAssistantMessage = (message) =>
  Boolean(message) && (message.type === 'ai' || message.role === 'assistant')

const lastAssistantMessage = (conv) => {
  const messages = Array.isArray(conv?.messages) ? conv.messages : []
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (isAssistantMessage(messages[index])) return messages[index]
  }
  return null
}

export const normalizeGraphSnapshot = (value) => {
  if (!value || typeof value !== 'object' || value.schema !== 'graph_snapshot_v1') return null
  const nodes = Array.isArray(value.nodes)
    ? value.nodes.filter((node) => node && node.entity_id && node.kb_id && node.name)
    : []
  const nodeIds = new Set(nodes.map((node) => String(node.entity_id)))
  const edges = Array.isArray(value.edges)
    ? value.edges.filter(
        (edge) =>
          edge &&
          edge.triple_id &&
          edge.kb_id &&
          nodeIds.has(String(edge.source_entity_id)) &&
          nodeIds.has(String(edge.target_entity_id))
      )
    : []
  return { ...value, nodes, edges }
}

export const inlineGraphForMessage = (message, conv, graphsByRun) => {
  if (!isAssistantMessage(message)) return null
  const last = lastAssistantMessage(conv)
  if (!last || (last !== message && !(last.id && last.id === message.id))) return null
  const persisted = normalizeGraphSnapshot(message?.extra_metadata?.graph_snapshot)
  if (persisted) return persisted
  const runId = String(message.run_id || message?.extra_metadata?.run_id || '')
  return runId ? normalizeGraphSnapshot(graphsByRun?.[runId]) : null
}

export const extractGraphSnapshotFromHistory = (history) => {
  if (!Array.isArray(history)) return null
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const message = history[index]
    if (!isAssistantMessage(message)) continue
    return normalizeGraphSnapshot(message?.extra_metadata?.graph_snapshot)
  }
  return null
}

/** 成员策略放行的候选边数（已在卡上、以待审核样式渲染）。 */
export const pendingEdgeCount = (snapshot) =>
  (snapshot?.edges || []).filter(
    (edge) => String(edge.review_status || '').toUpperCase() === 'CANDIDATE'
  ).length

/** 被成员证据策略拦截、未上卡的候选边数（suppressed.review_policy）。 */
export const suppressedCandidateCount = (snapshot) =>
  Number(snapshot?.suppressed?.review_policy || 0)

/** relation_group → 展示标签（与 claim 通道的谓词归一桶一致）。 */
export const RELATION_GROUP_LABELS = Object.freeze({
  FUNCTIONAL_REGULATION: '功能调控',
  PERTURBATION_EVIDENCE: '扰动证据',
  ASSOCIATION_OR_CONTEXT: '关联 / 背景'
})

/**
 * 画布数据：大图只画支持度 Top 子集（快照边已按支持度排序），
 * 节点收缩到切片端点——画布负责概览，全量由分组列表承载。
 */
export const graphCanvasData = (snapshot, canvasEdgeLimit = 60) => {
  const normalized = normalizeGraphSnapshot(snapshot)
  if (!normalized) return { nodes: [], edges: [] }
  const edges = normalized.edges.slice(0, Math.max(1, canvasEdgeLimit))
  const kept = new Set(edges.flatMap((edge) => [String(edge.source_entity_id), String(edge.target_entity_id)]))
  const nodes = normalized.nodes.filter((node) => kept.has(String(node.entity_id)))
  return {
    nodes: nodes.map((node) => ({
      id: node.entity_id,
      name: node.name,
      type: node.label,
      properties: { ...node, entity_id: node.entity_id, label: node.label }
    })),
    edges: edges.map((edge) => ({
      id: edge.triple_id,
      source_id: edge.source_entity_id,
      target_id: edge.target_entity_id,
      type: edge.predicate,
      properties: { ...edge, triple_id: edge.triple_id, review_status: edge.review_status }
    }))
  }
}

/** 全量关系列表按 relation_group 分桶（画布之外的完整呈现）。 */
export const groupEdgesByRelationGroup = (snapshot) => {
  const normalized = normalizeGraphSnapshot(snapshot)
  const buckets = new Map()
  for (const edge of normalized?.edges || []) {
    const group = String(edge.relation_group || 'ASSOCIATION_OR_CONTEXT')
    if (!buckets.has(group)) buckets.set(group, [])
    buckets.get(group).push(edge)
  }
  return Array.from(buckets.entries()).map(([group, edges]) => ({
    group,
    label: RELATION_GROUP_LABELS[group] || group,
    edges
  }))
}

/** 逐轮导出源：该轮最后一条 AI 消息的发布快照 + run_id（下载端点的定位键）。 */
export const graphExportSourceForConversation = (conv) => {
  const messages = Array.isArray(conv?.messages) ? conv.messages : []
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message?.type !== 'ai') continue
    const snapshot = normalizeGraphSnapshot(message?.extra_metadata?.graph_snapshot)
    const runId = String(message.run_id || message?.extra_metadata?.run_id || '')
    return snapshot && runId ? { snapshot, runId } : null
  }
  return null
}

/** 服务端回放导出端点：字节由后端从发布载荷确定性生成，前端不做数据源。 */
export const exportGraphSnapshotUrl = (runId, format) =>
  `/api/agent/runs/${encodeURIComponent(String(runId || ''))}/graph-snapshot-export?format=${
    format === 'csv' ? 'csv' : 'json'
  }`

/** 图谱工作台深链：主库 + 种子实体，落地页以 tab=graph 直达图谱页签。 */
export const graphWorkbenchRoute = (snapshot) => {
  const normalized = normalizeGraphSnapshot(snapshot)
  if (!normalized) return null
  const kbId = String(normalized.nodes?.[0]?.kb_id || '').trim()
  if (!kbId) return null
  const seed = String(
    normalized.seed_display_name || (normalized.seed_names || [])[0] || ''
  ).trim()
  return {
    path: `/extensions/knowledgebase/${encodeURIComponent(kbId)}`,
    query: { tab: 'graph', ws: 'explorer', ...(seed ? { seed } : {}) }
  }
}

