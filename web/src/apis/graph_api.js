import { apiGet, apiPut } from './base'

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
  }
}

export const unifiedApi = graphApi
