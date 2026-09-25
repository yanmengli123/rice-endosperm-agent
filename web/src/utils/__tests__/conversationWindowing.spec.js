import assert from 'node:assert/strict'

import {
  CONVERSATION_WINDOW_STEP,
  countHiddenConversations,
  windowConversationRows
} from '../conversationWindowing.js'

// ── windowConversationRows ───────────────────────────────────────────────────

const rows = Array.from({ length: 40 }, (_, i) => ({ key: `row-${i}` }))

// 不足窗口：原样返回同一引用（避免无谓拷贝）
const small = rows.slice(0, 10)
assert.strictEqual(windowConversationRows(small, 30), small)

// 超出窗口：只保留尾部窗口
const windowed = windowConversationRows(rows, 30)
assert.strictEqual(windowed.length, 30)
assert.strictEqual(windowed[0].key, 'row-10')
assert.strictEqual(windowed[29].key, 'row-39')

// 边界：恰好等于窗口不裁剪
assert.strictEqual(windowConversationRows(rows.slice(0, 30), 30).length, 30)

// 默认步长是 30（与 UI「显示更早」步进一致）
assert.strictEqual(CONVERSATION_WINDOW_STEP, 30)

// ── countHiddenConversations ─────────────────────────────────────────────────

assert.strictEqual(countHiddenConversations(40, 30), 10)
assert.strictEqual(countHiddenConversations(30, 30), 0)
assert.strictEqual(countHiddenConversations(5, 30), 0)
assert.strictEqual(countHiddenConversations(undefined, 30), 0)

console.log('conversationWindowing: all assertions passed')
