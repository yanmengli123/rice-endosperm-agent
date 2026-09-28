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

/**
 * 图表锚点（ADR-0008 figure_refs[]）归一化：只接受后端签发条目
 * （ref + label 必填）；evidence_id / figure_index / suppressed_reason 为
 * 可选降级信息。前端不从芯片文本推断身份——身份只来自这份结构化载荷。
 */
export const normalizeFigureRefs = (refs) => {
  if (!Array.isArray(refs)) return []
  const allowedVisualStatuses = new Set([
    'VERIFIED_WITH_ASSET',
    'VERIFIED_WITH_TABLE',
    'VERIFIED_CAPTION_ONLY',
    'REFERENCE_ONLY'
  ])
  return refs
    .filter(
      (ref) =>
        ref &&
        typeof ref === 'object' &&
        /^F\d{1,3}$/.test(String(ref.ref || '')) &&
        String(ref.label || '').trim()
    )
    .map((ref) => {
      const status = String(ref.visual_status || '')
      // Missing status is a supported legacy shape. An unknown new value is
      // fail-closed to REFERENCE_ONLY so the UI never implies that an image or
      // table was verified merely because a future/invalid enum arrived.
      if (!status || allowedVisualStatuses.has(status)) return ref
      return { ...ref, visual_status: 'REFERENCE_ONLY' }
    })
}

/**
 * 表格卡片（ADR-0008 P2 citation_ready.tables[]）归一化：只接受后端受控解析
 * 的条目（table_id + kb_id + rows 数组）。rows 为纯文本 cell
 * （{text,rowspan,colspan,header}），前端永不渲染 HTML。
 */
export const normalizeVerifiedTables = (tables) => {
  if (!Array.isArray(tables)) return []
  return tables.filter(
    (table) =>
      table &&
      typeof table === 'object' &&
      String(table.table_id || '').trim() &&
      String(table.kb_id || '').trim() &&
      Array.isArray(table.rows)
  )
}

/**
 * 芯片点击 → 锚点行为裁决（纯函数，供事件处理器调用）：
 * - figure ref 有卡片（figure_index 指向 figures 下标）→ 滚动联动图卡；
 * - table ref 有卡片（table_index 指向 tables 下标）→ 滚动联动表格卡片；
 * - 无卡片但有 evidence_id（suppressed：跳原文）→ 经证据抽屉打开原文；
 * - 其余（orphan / 无血统）→ 无操作（绝不猜）。
 */
export const figureRefClickAction = (refId, refs, figures, tables) => {
  const entry = normalizeFigureRefs(refs).find((ref) => ref.ref === String(refId || ''))
  if (!entry) return { type: 'none' }
  if (String(entry.kind || '') === 'table') {
    // 注意先判空：Number(null) === 0 会让"无卡片"的 ref 误命中第 0 张表
    const tableIndex = entry.table_index == null ? NaN : Number(entry.table_index)
    if (
      Number.isInteger(tableIndex) &&
      tableIndex >= 0 &&
      Array.isArray(tables) &&
      tables[tableIndex]
    ) {
      return { type: 'scroll-table', entry, table: tables[tableIndex], tableIndex }
    }
  } else {
    // 注意先判空：Number(null) === 0 会让"无卡片"的 ref 误命中第 0 张卡
    const index = entry.figure_index == null ? NaN : Number(entry.figure_index)
    if (Number.isInteger(index) && index >= 0 && Array.isArray(figures) && figures[index]) {
      return { type: 'scroll', entry, figure: figures[index], figureIndex: index }
    }
  }
  const evidenceId = String(entry.evidence_id || '').trim()
  if (evidenceId) return { type: 'open-source', entry, evidenceId }
  return { type: 'none' }
}

/**
 * 消息级图表锚点数据源（与 inlineFiguresForMessage 同语义）：
 * 1. 已落库的 `extra_metadata.citation_ready.figure_refs`（历史/刷新）；
 * 2. `figureRefsByRun[run_id]`（流结束到历史回读之间的桥）。
 * 只挂该轮最后一条 AI 消息。
 */
export const figureRefsForMessage = (message, conv, figureRefsByRun) => {
  if (!isAssistantMessage(message)) return []
  const last = lastAssistantMessage(conv)
  if (!last || (last !== message && !(last.id && last.id === message.id))) return []
  const persisted = normalizeFigureRefs(message?.extra_metadata?.citation_ready?.figure_refs)
  if (persisted.length) return persisted
  const runId = String(message.run_id || message?.extra_metadata?.run_id || '')
  if (!runId || !figureRefsByRun || typeof figureRefsByRun !== 'object') return []
  return normalizeFigureRefs(figureRefsByRun[runId])
}

/**
 * 消息级表格卡片数据源（ADR-0008 P2，与图卡同语义）：
 * 落库 `citation_ready.tables` 优先 → `tablesByRun[run_id]` 桥接。
 */
export const tablesForMessage = (message, conv, tablesByRun) => {
  if (!isAssistantMessage(message)) return []
  const last = lastAssistantMessage(conv)
  if (!last || (last !== message && !(last.id && last.id === message.id))) return []
  const persisted = normalizeVerifiedTables(message?.extra_metadata?.citation_ready?.tables)
  if (persisted.length) return persisted
  const runId = String(message.run_id || message?.extra_metadata?.run_id || '')
  if (!runId || !tablesByRun || typeof tablesByRun !== 'object') return []
  return normalizeVerifiedTables(tablesByRun[runId])
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
 * @returns{{ citation: object|null, figures: object[], figureRefs: object[], tables: object[] } | null}
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
    return {
      citation,
      figures: normalizeVerifiedFigures(payload.figures),
      figureRefs: normalizeFigureRefs(payload.figure_refs),
      tables: normalizeVerifiedTables(payload.tables)
    }
  }
  return null
}
