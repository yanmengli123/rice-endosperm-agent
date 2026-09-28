/**
 * followupSuggestions 纯逻辑 spec（裸 node 跑，web 容器无 vitest）：
 *   node src/utils/__tests__/followupSuggestions.spec.js
 */
import assert from 'node:assert/strict'

import {
  followupSuggestionsForMessage,
  normalizeFollowupSuggestions
} from '../followupSuggestions.js'

// normalizeFollowupSuggestions
assert.deepEqual(normalizeFollowupSuggestions(['  甲？ ', 42, '', null, '乙？']), ['甲？', '乙？'])
assert.deepEqual(normalizeFollowupSuggestions('不是数组'), [])
assert.deepEqual(normalizeFollowupSuggestions(null), [])
assert.deepEqual(normalizeFollowupSuggestions(['   ']), [])

// followupSuggestionsForMessage：非 AI 消息 / 非该轮最后一条 AI 消息恒为空
const aiMessage = (id, extra = {}) => ({ id, type: 'ai', run_id: 'run-1', extra_metadata: extra })
const conv = {
  messages: [
    { id: 'u1', type: 'human' },
    aiMessage('a1'),
    aiMessage('a2', { followup_suggestions: ['落库建议？'] })
  ]
}
assert.deepEqual(followupSuggestionsForMessage(conv.messages[0], conv, {}), [])
assert.deepEqual(followupSuggestionsForMessage(conv.messages[1], conv, {}), [])
// 落库载荷优先
assert.deepEqual(followupSuggestionsForMessage(conv.messages[2], conv, { 'run-1': ['流式建议？'] }), [
  '落库建议？'
])
// 无落库时走 ByRun 桥接；run_id 从 extra_metadata.run_id 兜底
const bridgeConv = {
  messages: [{ id: 'u1', type: 'human' }, { id: 'a1', type: 'ai', extra_metadata: { run_id: 'run-9' } }]
}
assert.deepEqual(
  followupSuggestionsForMessage(bridgeConv.messages[1], bridgeConv, { 'run-9': [' 桥接建议？ '] }),
  ['桥接建议？']
)
// 无 run_id 且无落库 → 空
assert.deepEqual(
  followupSuggestionsForMessage({ id: 'a1', type: 'ai' }, { messages: [{ type: 'ai', id: 'a1' }] }, {}),
  []
)

console.log('followupSuggestions.spec.js: all assertions passed')
