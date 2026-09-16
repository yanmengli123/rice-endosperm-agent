import { apiGet } from './base'

export const searchMentionFiles = (threadId, query, signal) => {
  const params = new URLSearchParams()
  if (threadId) params.set('thread_id', threadId)
  if (query) params.set('query', query)
  return apiGet(`/api/mention/search?${params.toString()}`, { signal })
}

/**
 * @doc 提及候选：用户可访问知识库范围内的文档（只含文档身份 + 图表就绪标记）。
 * @param {string[]} kbIds 当前智能体范围内的知识库 ID；为空时后端取用户可访问的全部
 */
export const searchMentionDocuments = (kbIds, query, signal) => {
  const params = new URLSearchParams()
  const scoped = (kbIds || []).filter(Boolean)
  if (scoped.length) params.set('kb_ids', scoped.join(','))
  if (query) params.set('query', query)
  return apiGet(`/api/mention/documents?${params.toString()}`, { signal })
}
