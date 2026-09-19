import { apiGet, apiPatch, apiPost, apiPut } from './base'

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
      full_graph = false
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
    const { kb_id, target_id, limit = 50 } = params || {}
    if (!kb_id) {
      throw new Error('kb_id is required')
    }
    const queryParams = new URLSearchParams({ kb_id, limit: String(limit) })
    if (target_id) {
      queryParams.set('target_id', target_id)
    }
    return await apiGet(`/api/graph/review/audit?${queryParams.toString()}`, {}, true)
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
  }
}

export const unifiedApi = graphApi
