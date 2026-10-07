/**
 * 图谱视觉身份纯逻辑测试（裸 node 运行，与 reviewMeta.spec.js 同模式）：
 *   node src/utils/graph/__tests__/graphPalette.spec.js
 *
 * 当前覆盖：暗色主题自适应提亮（保色相、抬亮度下限、幂等）。
 */
import assert from 'node:assert/strict'
import process from 'node:process'

import {
  NODE_LABEL_COLORS,
  lightenForDarkTheme,
  isEntityNode,
  isEntityRelationEdge
} from '../graphPalette.js'

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

// HSL 亮度回算（与实现同公式，校验输出亮度达标）
function lightnessOf(hex) {
  const toInt = (s) => parseInt(hex.replace('#', '').slice(s, s + 2), 16) / 255
  const max = Math.max(toInt(0), toInt(2), toInt(4))
  const min = Math.min(toInt(0), toInt(2), toInt(4))
  return (max + min) / 2
}

test('调色盘最深的身份色被提亮到下限亮度，且函数幂等', () => {
  // #3996ae L≈0.45，是调色盘里最暗的一档
  const lightened = lightenForDarkTheme('#3996ae')
  assert.notEqual(lightened, '#3996ae')
  assert.ok(
    Math.abs(lightnessOf(lightened) - 0.62) < 0.02,
    `提亮后亮度应≈0.62，实际 ${lightnessOf(lightened).toFixed(3)}（${lightened}）`
  )
  assert.equal(lightenForDarkTheme(lightened), lightened, '已达下限的颜色再次提亮应原样返回')
})

test('已达亮度下限的颜色保持不变', () => {
  // #f27c7c L≈0.72 ≥ 0.62
  assert.equal(lightenForDarkTheme('#f27c7c'), '#f27c7c')
  assert.equal(lightenForDarkTheme('#e885ba'), '#e885ba')
})

test('提亮保持色相：输出与输入的色相角一致（±1°）', () => {
  const hueOf = (hex) => {
    const toInt = (s) => parseInt(hex.replace('#', '').slice(s, s + 2), 16) / 255
    const r = toInt(0)
    const g = toInt(2)
    const b = toInt(4)
    const max = Math.max(r, g, b)
    const min = Math.min(r, g, b)
    const d = max - min
    if (d === 0) return 0
    const h =
      max === r
        ? ((g - b) / d + (g < b ? 6 : 0)) * 60
        : max === g
          ? ((b - r) / d + 2) * 60
          : ((r - g) / d + 4) * 60
    return h
  }
  for (const color of NODE_LABEL_COLORS) {
    const out = lightenForDarkTheme(color)
    const delta = Math.abs(hueOf(out) - hueOf(color))
    assert.ok(delta < 1 || delta > 359, `色相漂移过大: ${color} → ${out}（Δ${delta.toFixed(2)}°）`)
  }
})

test('调色盘全部颜色提亮后仍互可区分（无两色输出相同）', () => {
  const outputs = NODE_LABEL_COLORS.map((c) => lightenForDarkTheme(c))
  assert.equal(new Set(outputs).size, outputs.length, '提亮不得让不同身份色坍缩为同色')
})

test('结构角色优先于开放类型名，Chunk/MENTIONS 不误删业务对象', () => {
  assert.equal(isEntityNode({ type: 'Chunk', node_kind: 'entity' }), true)
  assert.equal(isEntityNode({ type: 'Gene', node_kind: 'chunk' }), false)
  assert.equal(isEntityRelationEdge({ type: 'MENTIONS', edge_kind: 'relation' }), true)
  assert.equal(isEntityRelationEdge({ type: 'ACTIVATES', edge_kind: 'mention' }), false)
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) {
  process.exit(1)
}
