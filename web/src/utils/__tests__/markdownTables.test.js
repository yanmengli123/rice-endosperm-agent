import assert from 'node:assert/strict'
import test from 'node:test'

import { normalizeMarkdownTables } from '../markdown_tables.js'

test('repairs a mismatched scientific table', () => {
  const source = `证据类型与限制 | 关系 | 证据等级 | 关系组 | 证据类型 |
|—|—|—|—|
| OsbZIP58 → grain filling | E3 | ASSOCIATION_OR_CONTEXT | 文献观点/关联 | IND-2 热胁迫 |

- 结论需核验。`

  assert.equal(
    normalizeMarkdownTables(source),
    `| 证据类型与限制 | 关系 | 证据等级 | 关系组 | 证据类型 |
| --- | --- | --- | --- | --- |
| OsbZIP58 → grain filling | E3 | ASSOCIATION_OR_CONTEXT | 文献观点/关联 | IND-2 热胁迫 |

- 结论需核验。`
  )
})

test('does not alter code fences or ordinary pipe prose', () => {
  const source = `A | B 是普通文字。

\`\`\`markdown
A | B
— | —
1 | 2
\`\`\`
`
  assert.equal(normalizeMarkdownTables(source), source)
})

test('preserves escaped pipes and inline code', () => {
  const source = '| 表达式 | 说明 |\n| - | - |\n| `A | B` | A \\| B |'
  assert.equal(
    normalizeMarkdownTables(source),
    '| 表达式 | 说明 |\n| --- | --- |\n| `A | B` | A \\| B |'
  )
})
