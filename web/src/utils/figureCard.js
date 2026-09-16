/**
 * 图卡（FigureCard）纯逻辑：kbasset URI 组装、标题回退链、SSE/历史载荷归一化。
 *
 * 数据只来自后端 `citation_ready` 载荷（`figures[]`，由 locator_resolution.figure_projection
 * 确定性投影而来）；前端不做任何二次裁决，也绝不从模型文本推断标题或页码。
 * 历史恢复只读 `extra_metadata.citation_ready`（实际发布过的载荷），不读审计用的
 * `citation_binding`——开关关闭的暗发布期，刷新页面不得从审计数据漏出图卡。
 *
 * 本模块不依赖 Vue/DOM，可用裸 node 跑 spec（web 容器无 vitest）：
 *   node src/utils/__tests__/figureCard.spec.js
 */

import { parseKbAssetUri } from './kbasset_contract.js'

/** `kbasset://{file_id}/{revision_id}/{asset_name}`；任一段缺失/非法 → null（不抛）。 */
export const figureAssetUri = (figure) => {
  const fileId = String(figure?.file_id || '').trim()
  const revisionId = String(figure?.revision_id || '').trim()
  const assetName = String(figure?.asset_name || '').trim()
  if (!fileId || !revisionId || !assetName) return null
  const uri = `kbasset://${fileId}/${revisionId}/${assetName}`
  return parseKbAssetUri(uri) ? uri : null
}

/** 标题回退链：实体题注 → 实体编号 → 中性文案（禁用任何模型/VLM 文本）。 */
export const figureCardTitle = (figure) => {
  const caption = String(figure?.caption || '').trim()
  if (caption) return caption
  const label = String(figure?.figure_label || '').trim()
  if (label) return label
  const page = Number(figure?.page)
  return Number.isInteger(page) && page >= 1 ? `图 · 第${page}页` : '图'
}

/** 只接受能组出合法 URI 且带 kb_id 的条目（取图三段式 + 鉴权路由的硬前置）。 */
export const normalizeVerifiedFigures = (figures) => {
  if (!Array.isArray(figures)) return []
  return figures.filter(
    (figure) =>
      figure &&
      typeof figure === 'object' &&
      String(figure.kb_id || '').trim() &&
      figureAssetUri(figure) !== null
  )
}

const isAssistantMessage = (message) =>
  Boolean(message) && (message.type === 'ai' || message.role === 'assistant')

const lastAssistantMessage = (conv) => {
  const messages = Array.isArray(conv?.messages) ? conv.messages : []
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (isAssistantMessage(messages[index])) return messages[index]
  }
  return null
}

/**
 * 答案气泡内的图卡数据源（消息级附件，不进 Markdown 正文）：
 * 1. 已落库的实际发布载荷 `extra_metadata.citation_ready.figures`（历史/刷新）；
 * 2. 本会话内按 run 暂存的实时载荷 `figuresByRun[run_id]`（流结束到历史回读之间的桥）。
 * 只挂在该轮最后一条 AI 消息上（工具调用中间消息不挂）；非 AI 消息恒为空。
 */
export const inlineFiguresForMessage = (message, conv, figuresByRun) => {
  if (!isAssistantMessage(message)) return []
  const last = lastAssistantMessage(conv)
  if (!last || (last !== message && !(last.id && last.id === message.id))) return []
  const persisted = normalizeVerifiedFigures(message?.extra_metadata?.citation_ready?.figures)
  if (persisted.length) return persisted
  const runId = String(message.run_id || message?.extra_metadata?.run_id || '')
  if (!runId || !figuresByRun || typeof figuresByRun !== 'object') return []
  return normalizeVerifiedFigures(figuresByRun[runId])
}

/**
 * 历史恢复：取最后一条 AI 消息的 `extra_metadata.citation_ready`。
 * 最后一条 AI 消息没有该载荷时返回 null（不回退到更早轮次——芯片/图卡表示的是最新一轮）。
 * @returns {{ citation: object|null, figures: object[] } | null}
 */
export const extractCitationReadyFromHistory = (history) => {
  if (!Array.isArray(history)) return null
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const message = history[index]
    if (!isAssistantMessage(message)) continue
    const payload = message?.extra_metadata?.citation_ready
    if (!payload || typeof payload !== 'object') return null
    const citation =
      payload.citation && typeof payload.citation === 'object' ? payload.citation : null
    return { citation, figures: normalizeVerifiedFigures(payload.figures) }
  }
  return null
}
