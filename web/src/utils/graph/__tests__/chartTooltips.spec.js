/**
 * 图谱洞察 tooltip 文案层测试（裸 node 运行）：
 *   node src/utils/graph/__tests__/chartTooltips.spec.js
 */
import assert from 'node:assert/strict'
import process from 'node:process'

import { encodeHTML, formatSankeyTooltip, makeHeatmapTooltip } from '../chartTooltips.js'

let passed = 0
let failed = 0

function test(name, fn) {
  try {
    fn()
    passed += 1
    console.log(`✓ ${name}`)
  } catch (error) {
    failed += 1
    console.error(`✗ ${name}\n  ${error?.stack || error}`)
  }
}

test('encodeHTML 覆盖五个危险字符', () => {
  assert.equal(
    encodeHTML(`<img src=x onerror="a('b')">&`),
    '&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;'
  )
  assert.equal(encodeHTML(null), '')
  assert.equal(encodeHTML(123), '123')
})

test('桑基 tooltip：节点/连线文案转义，谓词 Top3 + 等若干种', () => {
  const attack = `<img src=x>`
  const nodeTip = formatSankeyTooltip({
    dataType: 'node',
    data: { typeName: attack, side: 'source', total: 5, drawnTotal: 3 }
  })
  assert.ok(nodeTip.includes('&lt;img'), '节点名必须转义')
  assert.ok(nodeTip.includes('出向关系（含类型内部）：5 条（已绘制 3）'))

  const linkTip = formatSankeyTooltip({
    dataType: 'edge',
    data: {
      sourceType: 'A',
      targetType: attack,
      value: 9,
      relationTypes: [
        { type: '调控', count: 5 },
        { type: '参与', count: 3 },
        { type: '影响', count: 1 },
        { type: '其他', count: 0 }
      ]
    }
  })
  assert.ok(linkTip.includes('&lt;img'), '目标类型名必须转义')
  assert.ok(linkTip.includes('等 4 种谓词'), '超出 Top3 时给出总数')
})

test('热力图 tooltip：对角线标注自环语义，普通格标注方向', () => {
  const fmt = makeHeatmapTooltip(['Gene', 'Phenotype'])
  const diagonal = fmt({ value: [0, 0, 7] })
  assert.ok(diagonal.includes('类型内部关系：7 条'), '对角线按自环语义呈现')
  assert.ok(diagonal.includes('对角线'))
  const cell = fmt({ value: [1, 0, 12] })
  assert.ok(cell.includes('Gene → Phenotype'), '行=源、列=目标')
  assert.ok(cell.includes('12 条去重关系'))
  const outOfRange = fmt({ value: [9, 9, 1], name: 'x' })
  assert.equal(outOfRange, 'x', '越界坐标回退原名（防御路径）')
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
