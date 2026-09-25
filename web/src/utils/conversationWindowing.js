/**
 * 会话窗口化纯函数（P2 性能）：默认只渲染最近 N 个会话行，长线程不必全量渲染。
 * Vue 接线在 composables/useConversationWindowing.js；纯函数层供裸 node spec。
 */

export const CONVERSATION_WINDOW_STEP = 30

/** 取窗口内会话行（不足窗口时原样返回同一引用，避免无谓拷贝）。 */
export const windowConversationRows = (rows, windowSize) => {
  if (!Array.isArray(rows) || rows.length <= windowSize) return rows
  return rows.slice(-windowSize)
}

/** 窗口外被隐藏的会话行数。 */
export const countHiddenConversations = (total, windowSize) =>
  Math.max(0, (Number(total) || 0) - (Number(windowSize) || 0))
