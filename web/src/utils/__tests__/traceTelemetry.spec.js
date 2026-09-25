import assert from 'node:assert/strict'

import {
  applyTraceEvent,
  applyTraceSnapshot,
  buildTraceTimeline,
  createTraceState
} from '../traceProjection.js'
import {
  reportTraceDegradation,
  resetTraceDegradationTelemetry,
  setTraceDegradationSink
} from '../traceTelemetry.js'

const events = []
setTraceDegradationSink((payload) => events.push(payload))
resetTraceDegradationTelemetry()

// ── 去重：同一 (runId, reason) 只送达一次；不同 runId/Reason 独立 ───────────────

assert.strictEqual(
  reportTraceDegradation({ runId: 'r1', reason: 'demo', detail: { a: 1 } }),
  true
)
assert.strictEqual(reportTraceDegradation({ runId: 'r1', reason: 'demo' }), false)
assert.strictEqual(reportTraceDegradation({ runId: 'r2', reason: 'demo' }), true)
assert.strictEqual(reportTraceDegradation({ runId: 'r1', reason: 'other' }), true)
assert.strictEqual(events.length, 3)
assert.strictEqual(events[0].runId, 'r1')
assert.strictEqual(events[0].reason, 'demo')
assert.deepStrictEqual(events[0].detail, { a: 1 })

// sink 抛异常不影响调用方
setTraceDegradationSink(() => {
  throw new Error('sink boom')
})
resetTraceDegradationTelemetry()
assert.doesNotThrow(() => reportTraceDegradation({ runId: 'r9', reason: 'boom' }))
setTraceDegradationSink((payload) => events.push(payload))

// ── 投影兜底触发遥测 ─────────────────────────────────────────────────────────

// 1) 无效 sequence 事件被拒并留痕
resetTraceDegradationTelemetry()
events.length = 0
const state1 = createTraceState()
state1.runId = 'run-a'
applyTraceEvent(state1, { sequence: 0, event_type: 'tool.execution.started', category: 'TOOL' })
applyTraceEvent(state1, { sequence: 'NaN', event_type: 'tool.execution.started', category: 'TOOL' })
assert.strictEqual(events.length, 1)
assert.strictEqual(events[0].reason, 'invalid_event_sequence')

// 2) 迟到旧快照被拒并留痕（带回退前后的游标）
events.length = 0
const state2 = createTraceState()
state2.runId = 'run-b'
state2.lastAppliedSequence = 10
assert.strictEqual(applyTraceSnapshot(state2, { run_id: 'run-b', projection_sequence: 5 }), false)
assert.strictEqual(events.length, 1)
assert.strictEqual(events[0].reason, 'stale_snapshot_rejected')
assert.strictEqual(events[0].detail.projectionSequence, 5)
assert.strictEqual(events[0].detail.lastAppliedSequence, 10)

// 3) run 不匹配的快照被拒并留痕
events.length = 0
const state3 = createTraceState()
state3.runId = 'run-c'
assert.strictEqual(applyTraceSnapshot(state3, { run_id: 'run-x', projection_sequence: 1 }), false)
assert.strictEqual(events[0].reason, 'snapshot_run_mismatch')

// 4) 空投影快照（无 spans 无 summary）留痕但不拦截
events.length = 0
const state4 = createTraceState()
assert.strictEqual(applyTraceSnapshot(state4, { run_id: 'run-d', foo: 1 }), true)
assert.strictEqual(events[0].reason, 'snapshot_missing_projection')

// 5) 纯环 span：时间线不爆栈并留痕
events.length = 0
const state5 = createTraceState()
state5.runId = 'run-e'
const t = '2026-09-25T00:00:00Z'
applyTraceEvent(state5, {
  sequence: 1,
  span_id: 'a',
  parent_span_id: 'b',
  category: 'TOOL',
  event_type: 'tool.execution.started',
  occurred_at: t
})
applyTraceEvent(state5, {
  sequence: 2,
  span_id: 'b',
  parent_span_id: 'a',
  category: 'TOOL',
  event_type: 'tool.execution.started',
  occurred_at: t
})
const timeline = buildTraceTimeline(state5)
assert.ok(Array.isArray(timeline) && timeline.length >= 2)
assert.strictEqual(events[0].reason, 'span_cycle_detected')
assert.strictEqual(events[0].detail.orphanCount, 2)

setTraceDegradationSink(null)
console.log('traceTelemetry: all assertions passed')
