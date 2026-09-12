// 划词追问纯函数：选区校验、弹层定位、引用块与预览文本构造

const DEFAULT_QUOTE_MAX_LENGTH = 2000
const VIEWPORT_MARGIN = 8

// 选区是否完整落在限定区域内的一条助手消息（.message-box.ai）里
export const isValidAssistantSelection = (selection, rootElement = null) => {
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return false
  const container = selection.getRangeAt(0).commonAncestorContainer
  const element =
    container && container.nodeType === 1 ? container : container?.parentElement || null
  if (!element) return false
  if (rootElement && !rootElement.contains(element)) return false
  return Boolean(element.closest('.message-box.ai'))
}

// 工具条定位：选区首行上方水平居中；上方空间不足翻转到末行下方；最终钳制进视口
export const computePopupPosition = (
  clientRects,
  viewport,
  popupSize,
  margin = VIEWPORT_MARGIN
) => {
  const rects = (clientRects || []).filter((rect) => rect && (rect.width > 0 || rect.height > 0))
  if (!rects.length || !viewport || !popupSize) return null

  const first = rects[0]
  const centerX = (first.left + first.right) / 2
  let top = first.top - popupSize.height - margin
  if (top < margin) {
    top = rects[rects.length - 1].bottom + margin
  }
  top = Math.max(margin, Math.min(top, viewport.height - popupSize.height - margin))

  const left = Math.max(
    margin,
    Math.min(centerX - popupSize.width / 2, viewport.width - popupSize.width - margin)
  )
  return { left, top }
}

// 选中文本 → Markdown 引用块；超长截断（与发送链路的 2000 字上限对齐）
export const buildFollowUpQuote = (rawText, maxLength = DEFAULT_QUOTE_MAX_LENGTH) => {
  const normalized = String(rawText || '')
    .replace(/\r\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
  if (!normalized) return ''
  const truncated =
    normalized.length > maxLength ? `${normalized.slice(0, maxLength)}…` : normalized
  return truncated
    .split('\n')
    .map((line) => `> ${line}`.trimEnd())
    .join('\n')
}

// 工具条内的单行预览文本
export const buildSelectionPreview = (rawText, maxLength = 60) => {
  const normalized = String(rawText || '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!normalized) return ''
  return normalized.length > maxLength ? `${normalized.slice(0, maxLength)}…` : normalized
}
