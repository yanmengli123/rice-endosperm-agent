import assert from 'node:assert/strict'

import {
  createLiteratureEventBuffer,
  diffTasksToEvents,
  formatTokenCount,
  usageStats
} from '../../composables/useLiteratureEvents.js'
import { normalizeLitChrome } from '../literature_chrome.js'

// ── 事件缓冲：环形上限与连续去重 ──
const buffer = createLiteratureEventBuffer(3)
buffer.record('info', '任务', 'a')
buffer.record('info', '任务', 'a')
assert.equal(buffer.events.length, 1, '连续相同事件去重')
buffer.record('info', '任务', 'b')
buffer.record('error', '任务', 'a')
buffer.record('info', '系统', 'c')
assert.equal(buffer.events.length, 3, '超出上限丢最旧')
assert.equal(buffer.events[0].message, 'b')
buffer.clear()
assert.equal(buffer.events.length, 0)

// ── 任务 diff → 事件 ──
const log = []
const record = (level, source, message) => log.push({ level, source, message })
const prev = [
  { type: 'compile', ref: '8:L1', status: 'RUNNING', attempt: 1 },
  { type: 'translation', ref: '9:zh', status: 'PENDING', attempt: 2 }
]
const next = [
  { type: 'compile', ref: '8:L1', status: 'SUCCEEDED', attempt: 1 },
  { type: 'translation', ref: '9:zh', status: 'DEAD', attempt: 3, error: 'ModelCallError: x' },
  { type: 'reader_render', ref: 'spr_new', status: 'RUNNING', attempt: 1 }
]
diffTasksToEvents(prev, next, record)
assert.equal(log.length, 2, '新出现的活跃任务不记事件，状态跃迁才记')
assert.ok(log[0].message.includes('编译 8:L1 RUNNING→SUCCEEDED'))
assert.equal(log[0].level, 'info')
assert.equal(log[1].level, 'error', 'DEAD/FAILED 标红')
assert.ok(log[1].message.includes('ModelCallError'))

// 首次快照：只记录非初始态
log.length = 0
diffTasksToEvents(null, [
  { type: 'compile', ref: 'x', status: 'PENDING', attempt: 0 },
  { type: 'compile', ref: 'y', status: 'SUCCEEDED', attempt: 1 }
], record)
assert.equal(log.length, 1)
assert.ok(log[0].message.includes('y'))

// ── 用量读数 ──
const stats = usageStats(
  { monthly_tokens: 1000, monthly_platform_tokens: 800, monthly_byok_tokens: 200 },
  { monthly_tokens: 4500, monthly_platform_tokens: 4000, monthly_byok_tokens: 500 }
)
assert.equal(stats.session, 3500)
assert.equal(stats.monthly, 4500)
assert.equal(stats.platform, 4000)
assert.equal(stats.byok, 500)
assert.deepEqual(usageStats(null, null), { session: 0, monthly: 0, platform: 0, byok: 0 })
assert.equal(usageStats({ monthly_tokens: 500 }, { monthly_tokens: 100 }).session, 0, '计量回退不允许负数')

assert.equal(formatTokenCount(999), '999')
assert.equal(formatTokenCount(1500), '1.5k')
assert.equal(formatTokenCount(2_400_000), '2.4M')
assert.equal(formatTokenCount('bad'), '0')

// ── 护眼偏好归一 ──
assert.equal(normalizeLitChrome({}).eyeCare, false, '缺省关闭')
assert.equal(normalizeLitChrome({ eyeCare: true }).eyeCare, true)
assert.equal(normalizeLitChrome({ eyeCare: 'yes' }).eyeCare, false, '非布尔拒绝')
assert.equal(normalizeLitChrome(null).readMode, false, '既有字段不受影响')

console.log('Literature statusbar events, usage stats and eye-care chrome passed')

// ── 同身份多行（历史失败行 + 新成功行）不得产生虚假跃迁 ──
{
  const log2 = []
  const rec2 = (level, source, message) => log2.push(message)
  const snapshot = (order) =>
    order === 0
      ? [{ type: 'compile', ref: '10:L1', status: 'SUCCEEDED', attempt: 1, updated_at: '2026-10-04T15:47:38' }, { type: 'compile', ref: '10:L1', status: 'DEAD', attempt: 3, updated_at: '2026-10-04T14:00:00' }]
      : [{ type: 'compile', ref: '10:L1', status: 'DEAD', attempt: 3, updated_at: '2026-10-04T14:00:00' }, { type: 'compile', ref: '10:L1', status: 'SUCCEEDED', attempt: 1, updated_at: '2026-10-04T15:47:38' }]
  // 连续两个轮询、两行顺序互换：归并后保持最新行（首行=updated_at 最新），无跃迁
  diffTasksToEvents(null, snapshot(0), rec2)
  const afterFirst = log2.length
  diffTasksToEvents(snapshot(0), snapshot(1), rec2)
  assert.equal(log2.length, afterFirst, '同键多行顺序翻转不产生虚假跃迁')
  assert.ok(log2[0].includes('SUCCEEDED'))
}
console.log('Duplicate task rows collapse passed')
