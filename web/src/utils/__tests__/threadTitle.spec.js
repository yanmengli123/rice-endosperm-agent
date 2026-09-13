import assert from 'node:assert/strict'

import {
  MAX_TITLE_CHARS,
  deriveThreadTitle,
  extractProtectedSymbols,
  validateCatalogTitle
} from '../threadTitle.js'

const run = () => {
  // ---- 确定性命名：符号逐字保留（验收标准 1、5、6）----
  const wxTitle = deriveThreadTitle('通过 MCP 查 Wx')
  assert.ok(wxTitle.includes('Wx'), '标题必须逐字保留 Wx')
  assert.ok(wxTitle.includes('MCP'), '标题必须逐字保留 MCP')
  assert.ok(!wxTitle.includes('微信'), '不得把 Wx 扩写成微信')
  assert.equal(wxTitle, '通过 MCP 查 Wx')

  const osTitle = deriveThreadTitle('OsMYB73 这句在第几页')
  assert.ok(osTitle.includes('OsMYB73'))
  assert.ok(!osTitle.includes('水稻'), '原文没有的概念不得出现')
  assert.ok(!osTitle.includes('转录因子'))

  assert.ok(deriveThreadTitle('RAP-MSU 有哪些材料').includes('RAP-MSU'))

  // ---- 码点截断：长度上限与代理对安全 ----
  const longTitle = deriveThreadTitle(
    '这是一条特别特别特别长的中文消息，用来验证按码点截断是否正确执行'
  )
  assert.equal(Array.from(longTitle).length, MAX_TITLE_CHARS)

  const emojiTitle = deriveThreadTitle(
    '🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬🧬'
  )
  assert.equal(emojiTitle, '🧬'.repeat(MAX_TITLE_CHARS), '不得截断 emoji 代理对')
  assert.equal([...emojiTitle].length, MAX_TITLE_CHARS)

  // ---- 归一化：markdown 记号与空白折叠 ----
  assert.equal(deriveThreadTitle('**Wx** 是什么？'), 'Wx 是什么？')
  assert.equal(deriveThreadTitle('  查询\n\n 水稻\n 基因  '), '查询 水稻 基因')
  assert.equal(deriveThreadTitle('```\ncode only\n```'), 'code only')
  assert.equal(deriveThreadTitle('   '), '')
  assert.equal(deriveThreadTitle('***'), '')
  assert.equal(deriveThreadTitle(null), '')
  assert.equal(deriveThreadTitle(42), '')

  // ---- 确定性：同输入同输出（验收标准 5）----
  const raw = '通过 MCP 查 Wx 相关文献'
  assert.equal(deriveThreadTitle(raw), deriveThreadTitle(raw))

  // ---- 与桌面端对齐：纯文本消息等于原文前 28 个码点 ----
  const desktopEquivalent = '帮我整理水稻根系发育的文献综述框架和关键引用'
  assert.equal(deriveThreadTitle(desktopEquivalent), desktopEquivalent.slice(0, 28))

  // ---- 符号提取 ----
  assert.deepEqual(extractProtectedSymbols('通过 MCP 查 Wx 和 OsMYB73，还有 RAP-MSU 与 mRNA'), [
    'MCP',
    'Wx',
    'OsMYB73',
    'RAP-MSU',
    'mRNA'
  ])
  assert.deepEqual(extractProtectedSymbols('帮我看看水稻基因'), [])
  assert.deepEqual(extractProtectedSymbols('query the database'), [])
  assert.deepEqual(extractProtectedSymbols(null), [])

  // ---- 门禁：拦住幻觉（验收标准 2）----
  assert.equal(
    validateCatalogTitle('{"title":"微信使用说明"}', '通过 MCP 查 Wx'),
    null,
    '丢失 Wx/MCP 符号的标题必须被拒绝'
  )
  assert.equal(
    validateCatalogTitle('{"title":"水稻转录因子查询"}', 'OsMYB73 这句在第几页'),
    null,
    '引入原文没有的实体必须被拒绝'
  )

  // ---- 门禁：合规标题放行 ----
  assert.equal(
    validateCatalogTitle('{"title":"查询 Wx 与 MCP"}', '通过 MCP 查 Wx'),
    '查询 Wx 与 MCP'
  )
  assert.equal(
    validateCatalogTitle('{"title":"OsMYB73 页码定位"}', 'OsMYB73 这句在第几页'),
    'OsMYB73 页码定位'
  )

  // ---- 门禁：格式约束 ----
  assert.equal(
    validateCatalogTitle('{"title":"**Wx** 查询"}', '通过 MCP 查 Wx'),
    null,
    '拒绝 markdown'
  )
  assert.equal(validateCatalogTitle('{"title":"《Wx》查询"}', '通过 MCP 查 Wx'), null, '拒绝书名号')
  assert.equal(
    validateCatalogTitle('{"title":"关于 Wx 的查询"}', '通过 MCP 查 Wx'),
    null,
    '拒绝解释性前缀'
  )
  assert.equal(validateCatalogTitle('{"title":"查询"}', '通过 MCP 查 Wx'), null, '拒绝丢失符号')
  assert.equal(
    validateCatalogTitle(`{"title":"${'超长标题'.repeat(10)}"}`, '通过 MCP 查 Wx'),
    null,
    '拒绝超过码点上限'
  )
  const longOk = validateCatalogTitle(`{"title":"${'字'.repeat(MAX_TITLE_CHARS)}"}`, '帮我看看水稻')
  assert.equal(Array.from(longOk).length, MAX_TITLE_CHARS, '恰好达到上限的标题放行')

  // ---- 门禁：非契约输出一律拒绝 ----
  assert.equal(validateCatalogTitle('直接输出标题文字', '通过 MCP 查 Wx'), null, '拒绝裸文本')
  assert.equal(
    validateCatalogTitle('{"name":"查询 Wx"}', '通过 MCP 查 Wx'),
    null,
    '拒绝缺少 title 字段'
  )
  assert.equal(validateCatalogTitle('{"title":""}', '通过 MCP 查 Wx'), null, '拒绝空标题')
  assert.equal(validateCatalogTitle('["查询 Wx"]', '通过 MCP 查 Wx'), null, '拒绝数组')
  assert.equal(validateCatalogTitle(null, '通过 MCP 查 Wx'), null)
  // 已知边界：门禁是「符号保留性」校验而非全量实体校验。原文抽不出符号时
  // 不做实体约束（宁可放过、不可误拦），失败场景由确定性默认值兜底。
  assert.equal(validateCatalogTitle('{"title":"查询基因"}', '帮我看看水稻基因'), '查询基因')

  console.log('threadTitle.spec: all assertions passed')
}

run()
