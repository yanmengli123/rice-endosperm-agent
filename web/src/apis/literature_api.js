import { apiGet, apiPost } from './base'

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
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return apiGet(`/api/literature/papers${suffix}`, { signal })
}

/** 文献详情（元数据 + 渲染/编译/翻译/附件状态聚合） */
export function getLiteraturePaper(rid, signal = null) {
  return apiGet(`/api/literature/papers/${encodeURIComponent(rid)}`, { signal })
}

/** 读笔记级别（返回 kbasset URI，不返回正文） */
export function getLiteratureNote(rid, level, signal = null) {
  return apiGet(`/api/literature/papers/${encodeURIComponent(rid)}/notes/${encodeURIComponent(level)}`, {
    signal
  })
}

/** 触发阅读渲染（202 异步任务） */
export function triggerLiteratureRender(rid) {
  return apiPost(`/api/literature/papers/${encodeURIComponent(rid)}/render`)
}

/** 触发翻译（202 异步任务） */
export function triggerLiteratureTranslation(rid, variantKind = 'zh') {
  return apiPost(`/api/literature/papers/${encodeURIComponent(rid)}/translation?variant_kind=${encodeURIComponent(variantKind)}`)
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
  return fetch(buildLiteratureAssetUrl(uri), {
    signal,
    headers: { Accept: 'text/plain, text/markdown, application/json' }
  }).then(async (resp) => {
    if (!resp.ok) {
      throw new Error(`资产获取失败 (${resp.status})`)
    }
    return resp.text()
  })
}
