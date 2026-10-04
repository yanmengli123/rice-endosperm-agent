import { apiGet, apiPost, apiPut, apiDelete } from './base'

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
  for (const key of [
    'journal',
    'quartile',
    'min_if',
    'year_min',
    'year_max',
    'compiled_level',
    'source',
    'sort'
  ]) {
    if (params[key] !== '' && params[key] != null) qs.set(key, String(params[key]))
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return apiGet(`/api/literature/papers${suffix}`, { signal })
}

/** 文献详情（元数据 + 渲染/编译/翻译/附件状态聚合） */
export function getLiteraturePaper(rid, signal = null) {
  return apiGet(`/api/literature/papers/${encodeURIComponent(rid)}`, { signal })
}

/** 读笔记级别（返回 kbasset URI，不返回正文） */
export function getLiteratureNote(rid, level, signal = null) {
  return apiGet(
    `/api/literature/papers/${encodeURIComponent(rid)}/notes/${encodeURIComponent(level)}`,
    {
      signal
    }
  )
}

/** 触发阅读渲染（202 异步任务） */
export function triggerLiteratureRender(rid) {
  return apiPost(`/api/literature/papers/${encodeURIComponent(rid)}/render`)
}

/** 触发翻译（202 异步任务） */
export function triggerLiteratureTranslation(rid, variantKind = 'zh') {
  return apiPost(
    `/api/literature/papers/${encodeURIComponent(rid)}/translation?variant_kind=${encodeURIComponent(variantKind)}`
  )
}

/** Start an asynchronous L1/L2/L3 note compilation. */
export function triggerLiteratureCompile(rid, level = 'L1', force = false) {
  const query = new URLSearchParams({ level, force: String(force) })
  return apiPost(`/api/literature/papers/${encodeURIComponent(rid)}/compile?${query}`)
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

/** 文献域任务投影（渲染/编译/翻译；前端 3s 轮询） */
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

export const literatureWorkspaceApi = {
  exportBusinessBackup: () => apiGet('/api/literature/export/business-backup', {}, true, 'blob'),
  historyArchives: () => apiGet('/api/literature/history/archives'),
  exportHistoryArchive: (sha256) =>
    apiGet(
      `/api/literature/history/archives/${encodeURIComponent(sha256)}/download`,
      {},
      true,
      'blob'
    ),
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
  compileBatch: (payload) => apiPost('/api/literature/library/compile', payload),
  runs: () => apiGet('/api/literature/runs'),
  sessions: (archived = false) => apiGet(`/api/literature/sessions?archived=${archived}`),
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
  importMarkdown: (payload) => apiPost('/api/literature/papers/import/markdown', payload),
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
