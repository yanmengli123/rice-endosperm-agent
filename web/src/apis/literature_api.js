import { apiGet, apiPost, apiPut, apiDelete } from './base'

function bibliometricQuery(filters) {
  const query = new URLSearchParams()
  for (const key of ['only_imports', 'database', 'query', 'year_from', 'year_to']) {
    if (filters[key] !== undefined && filters[key] !== null && filters[key] !== '')
      query.set(key, String(filters[key]))
  }
  for (const id of filters.batch_ids || []) query.append('batch_ids', id)
  return query
}

/**
 * 文献域 API（P6c，ADR-0010）
 * 后端：/api/literature/*（server/routers/literature_router.py，薄层）
 * 约定：产物一律 kbasset:// 逻辑 URI；字节经 /api/literature/assets 鉴权代理。
 */

/**
 * 构建文献域资产的鉴权请求地址（物理布局不出后端）。
 * @param {string} uri kbasset://tenants/{t}/... 逻辑 URI
 * @returns {string} /api/literature/assets?uri=... （uri 经 encodeURIComponent）
 */
export function buildLiteratureAssetUrl(uri) {
  const value = String(uri || '')
  if (!value.startsWith('kbasset://')) {
    throw new TypeError('literature asset uri must start with kbasset://')
  }
  return `/api/literature/assets?uri=${encodeURIComponent(value)}`
}

/**
 * 文献列表（分页/类型筛选/检索）
 * @param {{query?: string, kind?: string, page?: number, page_size?: number}} params
 */
export function listLiteraturePapers(params = {}, signal = null) {
  const qs = new URLSearchParams()
  if (params.query) qs.set('query', params.query)
  if (params.kind) qs.set('kind', params.kind)
  if (params.page) qs.set('page', String(params.page))
  if (params.page_size) qs.set('page_size', String(params.page_size))
  for (const key of ['journal', 'quartile', 'min_if', 'year_min', 'year_max', 'source', 'sort']) {
    if (params[key] !== '' && params[key] != null) qs.set(key, String(params[key]))
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return apiGet(`/api/literature/papers${suffix}`, { signal })
}

/** 文献详情（元数据 + 渲染/附件状态聚合） */
export function getLiteraturePaper(rid, signal = null) {
  return apiGet(`/api/literature/papers/${encodeURIComponent(rid)}`, { signal })
}

/** 触发阅读渲染（202 异步任务） */
export function triggerLiteratureRender(rid) {
  return apiPost(`/api/literature/papers/${encodeURIComponent(rid)}/render`)
}

/** Enrich DOI and bibliographic metadata from the configured providers. */
export function triggerLiteratureEnrich(rid) {
  return apiPost(`/api/literature/papers/${encodeURIComponent(rid)}/enrich`)
}

/** 依附资料清单 */
export function listLiteratureAttachments(rid, signal = null) {
  return apiGet(`/api/literature/papers/${encodeURIComponent(rid)}/attachments`, { signal })
}

/** 上传依附资料（0 API 本地抽文本） */
export function uploadLiteratureAttachment(rid, file, kind = 'si') {
  const form = new FormData()
  form.append('file', file)
  return apiPost(
    `/api/literature/papers/${encodeURIComponent(rid)}/attachments?kind=${encodeURIComponent(kind)}`,
    form
  )
}

/** 文献域任务投影（渲染/导入；前端 3s 轮询） */
export function listLiteratureTasks(limit = 100, signal = null) {
  return apiGet(`/api/literature/tasks?limit=${encodeURIComponent(String(limit))}`, { signal })
}

/** 取资产字节（文本类返回 text，二进制由调用方处理 blob） */
export function fetchLiteratureAssetText(uri, signal = null) {
  return apiGet(
    buildLiteratureAssetUrl(uri),
    { signal, headers: { Accept: 'text/plain, text/markdown, application/json' } },
    true,
    'text'
  )
}

export async function fetchLiteratureAssetBlob(uri, signal = null) {
  const response = await apiGet(buildLiteratureAssetUrl(uri), { signal }, true, 'blob')
  return response.blob()
}

export const literatureWorkspaceApi = {
  exportBusinessBackup: () => apiGet('/api/literature/export/business-backup', {}, true, 'blob'),
  graphNetwork: (filters = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) {
      if (value !== '' && value != null)
        query.set(key, Array.isArray(value) ? value.join(',') : String(value))
    }
    return apiGet(`/api/literature/graph/network?${query}`)
  },
  metadata: (rid) => apiGet(`/api/literature/papers/${encodeURIComponent(rid)}/metadata`),
  saveMetadata: (rid, payload) =>
    apiPut(`/api/literature/papers/${encodeURIComponent(rid)}/metadata`, payload),
  runs: () => apiGet('/api/literature/runs'),
  /** 质量指标（拒绝率/降级率/修复成功率 + 任务/入库健康）。 */
  qualityMetrics: (days = 7) =>
    apiGet(`/api/literature/metrics/quality?days=${encodeURIComponent(String(days))}`),
  /** 死信人工恢复：FAILED/REJECTED 入库任务复位重跑（管理员）。 */
  retryIngest: (revisionId) =>
    apiPost(`/api/literature/ingest/${encodeURIComponent(revisionId)}/retry`),
  sessions: (archived = false, offset = 0, limit = 50) =>
    apiGet(`/api/literature/sessions?archived=${archived}&offset=${offset}&limit=${limit}`),
  /** 会话历史检索（标题 + 已完成消息全文；命中为导航，不进证据通道） */
  searchSessions: (query, { archived = false, offset = 0, limit = 20 } = {}) =>
    apiGet(
      `/api/literature/sessions/search?query=${encodeURIComponent(query)}&archived=${archived}&offset=${offset}&limit=${limit}`
    ),
  createSession: (kind, rid = '') => apiPost('/api/literature/sessions', { kind, rid }),
  qnaPreview: (id) => apiGet(`/api/literature/sessions/${encodeURIComponent(id)}/qna-preview`),
  qnaWriteback: (id, payload) =>
    apiPost(`/api/literature/sessions/${encodeURIComponent(id)}/qna-writeback`, payload),
  updateSession: (id, changes) =>
    apiPut(`/api/literature/sessions/${encodeURIComponent(id)}`, changes),
  /**
   * 删除会话（对标 PaperAgent §6.4 行内 `✕` 删除）。
   * 复用原生 thread 软删除端点：服务端按 uid 校验归属并置 status=deleted，
   * 历史与用量记录保留，不做物理删除。
   */
  deleteSession: (id) => apiDelete(`/api/chat/thread/${encodeURIComponent(id)}`),
  importMarkdown: (payload, options) => apiPost('/api/literature/papers/import/markdown', payload, options),
  /**
   * 文献清单导入（PubMed .nbib / WoS Tab-delimited / RIS / EndNote）。
   * 后端按内容嗅探格式，format='auto' 默认；202 + 导入报告。
   */
  importReferences: (file, format = 'auto', options) => {
    const form = new FormData()
    form.append('file', file)
    return apiPost(
      `/api/literature/papers/import/references?format=${encodeURIComponent(format)}`,
      form,
      options
    )
  },
  referenceBatches: (offset = 0) => apiGet(`/api/literature/reference-batches?offset=${offset}`),
  metricSources: () => apiGet('/api/literature/metrics/sources'),
  metricRecords: (filters = {}, offset = 0, limit = 25) =>
    apiGet(
      `/api/literature/metrics/records?${bibliometricQuery(filters)}&offset=${offset}&limit=${limit}`
    ),
  /** 清单、统计和网络使用同一范围，排除参考条目/回收站，50000 篇预算。 */
  bibliometrics: (journalsTop = 20, filters = {}) =>
    apiGet(
      `/api/literature/metrics/bibliometrics?journals_top=${encodeURIComponent(String(journalsTop))}&${bibliometricQuery(filters)}`
    ),
  /** 共现网络（type=keyword|author；节点=频次 TopN，边=同篇共现）。 */
  metricNetwork: (type = 'keyword', top = 50, minPair = 2, filters = {}) => {
    const query = bibliometricQuery(filters)
    query.set('type', type)
    query.set('top', String(top))
    query.set('min_pair', String(minPair))
    return apiGet(`/api/literature/metrics/networks?${query}`)
  },
  // 期刊指标（JCR/中科院）：预览只回摘要；导入是管理员写入；关联写回 IF/分区
  journalsStatus: () => apiGet('/api/literature/journals/status'),
  previewJournals: (file) => {
    const form = new FormData()
    form.append('file', file)
    return apiPost('/api/literature/journals/preview', form)
  },
  importJournals: (file) => {
    const form = new FormData()
    form.append('file', file)
    return apiPost('/api/literature/journals/import', form)
  },
  attachJournalMetrics: () => apiPost('/api/literature/library/journal-metrics'),
  exportDiary: (month) =>
    apiGet(`/api/literature/diary/export?month=${encodeURIComponent(month)}`, {}, true, 'blob')
}
