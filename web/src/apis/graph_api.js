import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from './base'

export const graphApi = {
  getGraphs: async () => {
    return await apiGet('/api/graph/list', {}, true)
  },

  getSubgraph: async (params) => {
    const {
      kb_id,
      node_label = '*',
      max_depth = 2,
      max_nodes = 100,
      exclude_chunk = false,
      full_graph = false,
      review_policy
    } = params

    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({
      kb_id,
      node_label,
      max_depth: max_depth.toString(),
      max_nodes: max_nodes.toString(),
      exclude_chunk: exclude_chunk.toString()
    })
    if (full_graph) {
      queryParams.set('full_graph', 'true')
    }
    // 画布会话级显示过滤：仅影响本查询，绝不影响 Graph-RAG 检索（检索策略在治理设置）
    if (review_policy) {
      queryParams.set('review_policy', review_policy)
    }

    return await apiGet(`/api/graph/subgraph?${queryParams.toString()}`, {}, true)
  },

  getViewSettings: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/settings?${queryParams.toString()}`, {}, true)
  },

  updateViewSettings: async (kb_id, settings) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id })
    return await apiPut(`/api/graph/settings?${queryParams.toString()}`, settings, {}, true)
  },

  getStats: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/stats?${queryParams.toString()}`, {}, true)
  },

  getLabels: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/labels?${queryParams.toString()}`, {}, true)
  },

  // 「点开即见原文」：边/节点的全部逐字引文（PostgreSQL mention 表，显示时逐条重验）
  getTripleEvidence: async (kb_id, triple_id) => {
    if (!kb_id || !triple_id) {
      throw new Error('kb_id and triple_id are required')
    }

    const queryParams = new URLSearchParams({ kb_id, triple_id })
    return await apiGet(`/api/graph/evidence/triple?${queryParams.toString()}`, {}, true)
  },

  getEntityEvidence: async (kb_id, entity_id) => {
    if (!kb_id || !entity_id) {
      throw new Error('kb_id and entity_id are required')
    }

    const queryParams = new URLSearchParams({ kb_id, entity_id })
    return await apiGet(`/api/graph/evidence/entity?${queryParams.toString()}`, {}, true)
  },

  getIntegrity: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/integrity?${queryParams.toString()}`, {}, true)
  },

  // 人工审核闭环（决策叠加层）：approve 携带审核人看着的原文 chunk，reject 必填理由
  getVocabulary: async () => {
    return await apiGet('/api/graph/vocabulary', {}, true)
  },

  reviewApprove: async (payload) => {
    if (!payload?.kb_id || !payload?.target_id) {
      throw new Error('kb_id and target_id are required')
    }
    return await apiPost('/api/graph/review/approve', payload, {}, true)
  },

  reviewReject: async (payload) => {
    if (!payload?.kb_id || !payload?.target_id) {
      throw new Error('kb_id and target_id are required')
    }
    return await apiPost('/api/graph/review/reject', payload, {}, true)
  },

  reviewBatch: async (payload) => {
    if (!payload?.kb_id || !Array.isArray(payload?.targets)) {
      throw new Error('kb_id and targets are required')
    }
    return await apiPost('/api/graph/review/batch', payload, {}, true)
  },

  reviewEditTriple: async (payload) => {
    if (!payload?.kb_id || !payload?.triple_id) {
      throw new Error('kb_id and triple_id are required')
    }
    return await apiPatch('/api/graph/review/triple', payload, {}, true)
  },

  reviewEditEntity: async (payload) => {
    if (!payload?.kb_id || !payload?.entity_id) {
      throw new Error('kb_id and entity_id are required')
    }
    return await apiPatch('/api/graph/review/entity', payload, {}, true)
  },

  reviewAddTriple: async (payload) => {
    if (!payload?.kb_id || !payload?.chunk_id || !payload?.evidence_quote) {
      throw new Error('kb_id, chunk_id and evidence_quote are required')
    }
    return await apiPost('/api/graph/review/triple', payload, {}, true)
  },

  reviewReextract: async (payload) => {
    if (!payload?.kb_id || !payload?.chunk_id) {
      throw new Error('kb_id and chunk_id are required')
    }
    return await apiPost('/api/graph/review/reextract', payload, {}, true)
  },

  reviewQueue: async (params) => {
    const {
      kb_id,
      status = 'CANDIDATE',
      page = 1,
      page_size = 20,
      order = 'support_asc',
      file_id
    } = params || {}
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({
      kb_id,
      status,
      page: String(page),
      page_size: String(page_size),
      order
    })
    if (file_id) {
      queryParams.set('file_id', file_id)
    }
    return await apiGet(`/api/graph/review/queue?${queryParams.toString()}`, {}, true)
  },

  reviewAudit: async (params) => {
    const {
      kb_id,
      target_id,
      limit = 50,
      page = 1,
      page_size = 20,
      actor_uid,
      action,
      batch_id
    } = params || {}
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({
      kb_id,
      limit: String(limit),
      page: String(page),
      page_size: String(page_size)
    })
    if (target_id) {
      queryParams.set('target_id', target_id)
    }
    if (actor_uid) {
      queryParams.set('actor_uid', actor_uid)
    }
    if (action) {
      queryParams.set('action', action)
    }
    if (batch_id) {
      queryParams.set('batch_id', batch_id)
    }
    return await apiGet(`/api/graph/review/audit?${queryParams.toString()}`, {}, true)
  },

  reviewAuditExportUrl: (kb_id, filters = {}) => {
    const queryParams = new URLSearchParams({ kb_id })
    for (const key of ['target_id', 'actor_uid', 'action', 'batch_id']) {
      if (filters[key]) {
        queryParams.set(key, filters[key])
      }
    }
    return `/api/graph/review/audit/export?${queryParams.toString()}`
  },

  // 审计账本 CSV 导出（blob 下载；审计表 append-only，导出只读）
  reviewAuditExport: async (kb_id, filters = {}) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    return await apiGet(graphApi.reviewAuditExportUrl(kb_id, filters), {}, true, 'blob')
  },

  // 门禁送审队列（D4）：G7 strict 未过 / G9 否定矛盾的关系候选，人工裁决后闭环
  gateReviewQueue: async (params) => {
    const { kb_id, status = 'PENDING', gate_code, page = 1, page_size = 20 } = params || {}
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({
      kb_id,
      status,
      page: String(page),
      page_size: String(page_size)
    })
    if (gate_code) {
      queryParams.set('gate_code', gate_code)
    }
    return await apiGet(`/api/graph/gate-reviews?${queryParams.toString()}`, {}, true)
  },

  gateReviewResolve: async (payload) => {
    if (!payload?.kb_id || !payload?.review_id || !payload?.action) {
      throw new Error('kb_id, review_id and action are required')
    }
    return await apiPost('/api/graph/gate-reviews/resolve', payload, {}, true)
  },

  // 冲突队列（D6）：同条件极性矛盾（DIRECTION）与定义区间口径不一（DEFINITION）
  conflictQueue: async (params) => {
    const { kb_id, kind, status = 'OPEN', page = 1, page_size = 20 } = params || {}
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({
      kb_id,
      status,
      page: String(page),
      page_size: String(page_size)
    })
    if (kind) {
      queryParams.set('kind', kind)
    }
    return await apiGet(`/api/graph/conflicts?${queryParams.toString()}`, {}, true)
  },

  conflictResolve: async (payload) => {
    if (!payload?.kb_id || !payload?.conflict_id || !payload?.resolution) {
      throw new Error('kb_id, conflict_id and resolution are required')
    }
    return await apiPost('/api/graph/conflicts/resolve', payload, {}, true)
  },

  // ── 图谱治理（设置审计化 / 聚合总览 / 批量准入 / 协作 / 发布门禁）──

  governanceSettings: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/governance/settings?${queryParams.toString()}`, {}, true)
  },

  updateGovernanceSettings: async (payload) => {
    if (!payload?.kb_id) {
      throw new Error('kb_id is required')
    }
    return await apiPut('/api/graph/governance/settings', payload, {}, true)
  },

  governanceSummary: async (kb_id, { refresh = false } = {}) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({ kb_id })
    if (refresh) {
      queryParams.set('refresh', 'true')
    }
    return await apiGet(`/api/graph/governance/summary?${queryParams.toString()}`, {}, true)
  },

  governanceBatchPreview: async (payload) => {
    if (!payload?.kb_id || !Array.isArray(payload?.targets)) {
      throw new Error('kb_id and targets are required')
    }
    return await apiPost('/api/graph/governance/batch-preview', payload, {}, true)
  },

  governancePublishGates: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/governance/publish-gates?${queryParams.toString()}`, {}, true)
  },

  governanceMembers: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/governance/members?${queryParams.toString()}`, {}, true)
  },

  updateGovernanceMember: async (payload) => {
    if (!payload?.kb_id || !payload?.uid || !payload?.capability) {
      throw new Error('kb_id, uid and capability are required')
    }
    return await apiPut('/api/graph/governance/members', payload, {}, true)
  },

  deleteGovernanceMember: async (kb_id, uid) => {
    if (!kb_id || !uid) {
      throw new Error('kb_id and uid are required')
    }
    const queryParams = new URLSearchParams({ kb_id, uid })
    return await apiDelete(`/api/graph/governance/members?${queryParams.toString()}`, {}, true)
  },

  claimReviewTasks: async (payload) => {
    if (!payload?.kb_id || !Array.isArray(payload?.targets)) {
      throw new Error('kb_id and targets are required')
    }
    return await apiPost('/api/graph/governance/tasks/claim', payload, {}, true)
  },

  releaseReviewTask: async (payload) => {
    if (!payload?.kb_id || !payload?.target_id) {
      throw new Error('kb_id and target_id are required')
    }
    return await apiPost('/api/graph/governance/tasks/release', payload, {}, true)
  },

  // ── golden 抽检与可疑传递边（质量治理工作区）──

  goldenSamples: async (kb_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({ kb_id })
    return await apiGet(`/api/graph/golden-samples?${queryParams.toString()}`, {}, true)
  },

  registerGoldenSample: async (payload) => {
    if (!payload?.kb_id || !payload?.chunk_id) {
      throw new Error('kb_id and chunk_id are required')
    }
    return await apiPost('/api/graph/golden-samples', payload, {}, true)
  },

  evaluateGoldenSamples: async (kb_id, limit = 20) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    return await apiPost('/api/graph/golden-samples/evaluate', { kb_id, limit }, {}, true)
  },

  shortcutSuspects: async (kb_id, limit = 200) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({ kb_id, limit: String(limit) })
    return await apiGet(`/api/graph/shortcut-suspects?${queryParams.toString()}`, {}, true)
  }
}

export const unifiedApi = graphApi
