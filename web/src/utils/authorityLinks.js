/** Render authority chips from Markdown text tokens, never by rewriting source. */
export const authorityLinks = (md) => {
  md.core.ruler.push('authority_link_boundaries', (state) => {
    let tableDepth = 0
    for (const token of state.tokens) {
      if (token.type === 'table_open') tableDepth += 1
      if (token.type === 'table_close') tableDepth -= 1
      if (token.type !== 'inline') continue
      let linkDepth = 0
      let htmlDepth = 0
      for (const child of token.children || []) {
        if (child.type === 'link_open') linkDepth += 1
        if (child.type === 'link_close') linkDepth -= 1
        if (child.type === 'html_inline') {
          if (/^<\//.test(child.content)) htmlDepth = Math.max(0, htmlDepth - 1)
          else if (
            /^<[a-z]/i.test(child.content) &&
            !/\/>$/.test(child.content) &&
            !/^<(?:br|hr|img|input|wbr|source)\b/i.test(child.content)
          )
            htmlDepth += 1
        }
        if (child.type === 'text') {
          child.meta = {
            ...child.meta,
            authorityLinks: tableDepth === 0 && linkDepth === 0 && htmlDepth === 0
          }
        }
      }
    }
  })
  const original = md.renderer.rules.text
  md.renderer.rules.text = (tokens, index, options, env, self) => {
    const token = tokens[index]
    const flags = env.authorityLinks || {}
    if (!token.meta?.authorityLinks || (!flags.evidence && !flags.figures)) {
      return original
        ? original(tokens, index, options, env, self)
        : md.utils.escapeHtml(token.content)
    }
    return token.content
      .split(/(〔[^〕\n]*〕)/g)
      .map((part) => {
        const escaped = md.utils.escapeHtml(part)
        const figure = part.match(/^〔图表(F\d{1,3})｜[^〕]+〕$/)
        if (flags.figures && figure)
          return `<a class="figure-ref-chip-anchor" data-ref="${figure[1]}">${escaped}</a>`
        const evidence = part.match(/^〔证据(E\d{1,3})｜[^〕]+〕$/)
        if (flags.evidence && (evidence || /^〔引文定位｜[^〕]+〕$/.test(part))) {
          return `<a class="evidence-chip-anchor"${evidence ? ` data-evidence="${evidence[1]}"` : ''}>${escaped}</a>`
        }
        return escaped
      })
      .join('')
  }
}
