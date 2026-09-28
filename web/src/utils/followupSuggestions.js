/**
 * 追问建议（followup_suggestions）纯逻辑：归一化与消息级数据源。
 *
 * 数据只来自服务端（followup_suggestions SSE 载荷 / AI 消息
 * extra_metadata.followup_suggestions），前端不生成、不改写问题文本；
 * 建议文本按纯文本渲染，点击后原样作为用户消息发送。
 *
 * 本模块不依赖 Vue/DOM，可用裸 node 跑 spec（web 容器无 vitest）：
 *   node src/utils/__tests__/followupSuggestions.spec.js
 */

/** 只接受非空字符串条目（trim 后非空，保留原文不截断）。 */
export const normalizeFollowupSuggestions = (suggestions) => {
  if (!Array.isArray(suggestions)) return []
  return suggestions
    .filter((item) => typeof item === 'string' && item.trim())
    .map((item) => item.trim())
}

/**
 * 消息级追问建议数据源（与 figureRefsForMessage 同语义）：
 * 1. 已落库的 `extra_metadata.followup_suggestions`（历史/刷新）；
 * 2. `followupSuggestionsByRun[run_id]`（流结束到历史回读之间的桥）。
 * 只挂该轮最后一条 AI 消息。
 */
export const followupSuggestionsForMessage = (message, conv, followupSuggestionsByRun) => {
  if (!message || !(message.type === 'ai' || message.role === 'assistant')) return []
  const messages = Array.isArray(conv?.messages) ? conv.messages : []
  let last = null
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const item = messages[index]
    if (item && (item.type === 'ai' || item.role === 'assistant')) {
      last = item
      break
    }
  }
  if (!last || (last !== message && !(last.id && last.id === message.id))) return []
  const persisted = normalizeFollowupSuggestions(message?.extra_metadata?.followup_suggestions)
  if (persisted.length) return persisted
  const runId = String(message.run_id || message?.extra_metadata?.run_id || '')
  if (!runId || !followupSuggestionsByRun || typeof followupSuggestionsByRun !== 'object') return []
  return normalizeFollowupSuggestions(followupSuggestionsByRun[runId])
}
