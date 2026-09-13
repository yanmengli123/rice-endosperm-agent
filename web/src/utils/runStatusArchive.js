/**
 * 历史轮状态档案（纯函数）：状态面板「查看历史轮」的数据组装层。
 *
 * 背景：线程槽位（threadStates[threadId].trace / evidence*）只保留最新一轮，
 * 历史轮的执行轨迹与检索证据需要按 run_id 独立缓存。这里只做无副作用的
 * 组装与缓存簿记；网络拉取在 composables/useRunStatusArchive.js。
 */

import { applyTraceSnapshot, createTraceState } from './traceProjection.js'

/** 单线程会话内最多缓存的历史轮档案数，超出按 LRU 驱逐 */
export const RUN_STATUS_ARCHIVE_MAX_ENTRIES = 10

export const createArchiveEntry = (runId) => ({
  runId,
  loading: false,
  loaded: false,
  // 轨迹投影（与 live 槽位同一 createTraceState 形态，TraceTimelinePanel 直接可渲染）
  trace: null,
  traceExpired: false,
  traceError: false,
  // 证据字段名与线程槽位对齐，面板分支逻辑可对称复用
  evidence: [],
  evidenceSummary: null,
  evidenceRetrievals: [],
  evidenceIssues: [],
  evidenceRole: null,
  claimBindingStatus: null,
  evidenceProjectionStatus: null,
  sourceManifest: null,
  evidenceError: false,
  loadedAt: 0
})

/**
 * LRU 驱逐：entries 为 Map（迭代顺序即最近使用顺序，调用方在命中时重插）。
 * protectedKey 是当前查看焦点，永不驱逐；全部受保护时放弃驱逐。
 */
export const evictArchiveEntries = (entries, maxEntries, protectedKey = null) => {
  const limit = Number(maxEntries) || 0
  if (limit <= 0 || entries.size <= limit) return []
  const evicted = []
  for (const key of entries.keys()) {
    if (entries.size <= limit) break
    if (key === protectedKey) continue
    entries.delete(key)
    evicted.push(key)
  }
  return evicted
}

/** 用服务端轨迹快照离线重建投影（不订阅流、不碰线程槽位）。 */
export const assembleTraceProjection = (runId, snapshot) => {
  const state = createTraceState()
  state.runId = runId
  if (snapshot) applyTraceSnapshot(state, snapshot)
  return state
}

/** 把 /runs/{id}/evidence 响应归一为档案条目字段（与线程槽位同名同义）。 */
export const assembleEvidenceFields = (result) => {
  const source = result && typeof result === 'object' ? result : {}
  return {
    evidence: Array.isArray(source.evidence) ? source.evidence : [],
    evidenceSummary: source.summary || null,
    evidenceRetrievals: Array.isArray(source.retrievals) ? source.retrievals : [],
    evidenceIssues: Array.isArray(source.issues) ? source.issues : [],
    evidenceRole: source.evidence_role || null,
    claimBindingStatus: source.claim_binding_status || null,
    evidenceProjectionStatus: source.projection_status || null,
    retrievalCandidates: Array.isArray(source.retrieval_candidates) ? source.retrieval_candidates : [],
    locatorStatusReason: source.locator_status_reason || null,
    sourceManifest: source.source_manifest || null
  }
}
