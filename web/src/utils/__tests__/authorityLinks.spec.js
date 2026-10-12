import assert from 'node:assert/strict'
import MarkdownIt from 'markdown-it'
import { authorityLinks } from '../authorityLinks.js'

const md = new MarkdownIt({ html: true, linkify: true }).use(authorityLinks)
const chip = '〔图表F1｜Figure 2〕'
const evidence = '〔证据E1｜paper.pdf，第5页〕'
const env = { authorityLinks: { evidence: true, figures: true } }
const html = md.render(`(${chip}) [${evidence}] **${chip}**`, env)
assert.equal((html.match(/figure-ref-chip-anchor/g) || []).length, 2)
assert.match(html, /data-evidence="E1"/)
for (const source of [
  `\`${chip}\``,
  `\`\`\`text\n${chip}\n\`\`\``,
  `    ${chip}`,
  `[${chip}](https://example.com)`,
  `[${chip}][doc]\n\n[doc]: https://example.com`,
  `<a href="https://example.com">${chip}</a>`,
  `<span>${chip}</span>`,
  `<div>\n${chip}\n</div>`,
  `| item |\n|---|\n| ${chip} |`
]) {
  assert.doesNotMatch(md.render(source, env), /figure-ref-chip-anchor/, source)
}
assert.doesNotMatch(md.render(chip), /figure-ref-chip-anchor/)
assert.doesNotMatch(
  md.render(evidence, { authorityLinks: { figures: true } }),
  /evidence-chip-anchor/
)
assert.doesNotMatch(md.render('〔图表F1｜<img src=x>〕', env), /figure-ref-chip-anchor/)
assert.match(md.render('〔图表F1｜&lt;img src=x&gt;〕', env), /&lt;img src=x&gt;/)
console.log('authorityLinks: Markdown boundaries and per-render switches passed')
