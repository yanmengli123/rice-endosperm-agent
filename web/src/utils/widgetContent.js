/** Split only top-level widget fences; ordinary code examples stay Markdown. */
export function splitWidgetContent(content) {
  const segments = []
  let text = ''
  let widget = null
  let fence = null
  const flushText = () => {
    if (text) segments.push({ type: 'markdown', content: text })
    text = ''
  }
  for (const line of String(content || '').match(/[^\n]*\n|[^\n]+$/g) || []) {
    const match = line.match(/^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$/)
    if (fence) {
      const closing = match && match[1][0] === fence.marker &&
        match[1].length >= fence.length && !match[2].trim()
      if (widget !== null) {
        if (closing) {
          segments.push({ type: 'widget', content: widget, closed: true })
          widget = null
        } else widget += line
      } else text += line
      if (closing) fence = null
      continue
    }
    if (match) {
      fence = { marker: match[1][0], length: match[1].length }
      if (match[2].trim() === 'widget') {
        flushText()
        widget = ''
        continue
      }
    }
    text += line
  }
  if (widget !== null) segments.push({ type: 'widget', content: widget, closed: false })
  flushText()
  return segments
}
