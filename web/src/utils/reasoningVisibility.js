const TAG_PATTERN =
  /\\*(?:(<|&lt;|&#0*60;|&#x0*3c;)\s*(\/\s*)?think\s*(>|&gt;|&#0*62;|&#x0*3e;))/gi

const PARTIAL_OPEN_TAG_PATTERN =
  /(?:\\+|\\*(?:\<|&lt?|&#0*60?;?|&#x0*3c?;?))(?:\s*\/?\s*(?:t?h?i?n?k?)?\s*)?$/i

const holdPartialOpeningTag = (text) => {
  const match = PARTIAL_OPEN_TAG_PATTERN.exec(text)
  return match ? text.slice(0, match.index) : text
}

/**
 * Pure stateless stripper for a complete message: only complete tags are
 * removed, an unmatched opening tag hides the remainder (fail closed), and a
 * legit trailing fragment such as "<t" is preserved.  Live-stream holdback
 * lives on the server-side streaming buffer, so this client only ever sees
 * already-safe deltas.
 */
export const splitReasoningText = (value) => {
  const text = typeof value === 'string' ? value : ''
  if (!text) return { visible: '', hadReasoning: false, reasoningOpen: false }

  const visible = []
  let cursor = 0
  let depth = 0
  let hadReasoning = false
  let firstTagStart = null

  TAG_PATTERN.lastIndex = 0
  for (const match of text.matchAll(TAG_PATTERN)) {
    if (firstTagStart === null) firstTagStart = match.index
    if (!depth) visible.push(text.slice(cursor, match.index))
    const isClosing = Boolean(match[2])
    if (isClosing) depth = Math.max(0, depth - 1)
    else depth += 1
    hadReasoning = true
    cursor = match.index + match[0].length
  }
  if (!depth) visible.push(text.slice(cursor))

  let safeText = visible.join('')
  if (hadReasoning && firstTagStart !== null && !text.slice(0, firstTagStart).trim()) {
    safeText = safeText.trimStart()
  }
  return { visible: safeText, hadReasoning, reasoningOpen: depth > 0 }
}

export const sanitizeVisibleModelText = (value) => splitReasoningText(value).visible

/** Stateful compatibility filter for old servers that split a tag across SSE deltas. */
export class ReasoningVisibilityBuffer {
  raw = ''

  visible = ''

  feed(delta) {
    if (typeof delta !== 'string' || !delta) return ''
    this.raw += delta
    const nextVisible = holdPartialOpeningTag(splitReasoningText(this.raw).visible)
    if (!nextVisible.startsWith(this.visible)) {
      this.visible = nextVisible
      return ''
    }
    const visibleDelta = nextVisible.slice(this.visible.length)
    this.visible = nextVisible
    return visibleDelta
  }
}
