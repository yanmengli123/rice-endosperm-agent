// 执行轨迹前端投影单测：事件 → span/summary 状态机、快照起步、时间线树。
// 运行方式与 messageProcessor.spec.js 相同：node src/utils/__tests__/traceProjection.spec.js
import assert from 'node:assert/strict'
import {
  applyTraceEvent,
  applyTraceSnapshot,
  buildTraceTimeline,
  createTraceState,
  createTraceSummary,
  findLatestTraceRunId
} from '../traceProjection.js'

const baseEvent = (overrides = {}) => ({
  schema_version: 'yuxi.run-trace.v1',
  event_id: 'evt_x',
  sequence: 1,
  run_id: 'run-1',
  thread_id: 'thread-1',
  category: 'TOOL',
  operation: 'execution',
  event_type: 'tool.execution.started',
  span_id: 'tool-1',
  parent_span_id: null,
  occurred_at: '2026-09-08T06:42:00.000Z',
  duration_ms: null,
  title: 'Gene Lookup',
  summary: null,
  attributes: {},
  resource_refs: [],
  visibility: 'USER',
  ...overrides
})

// 1. span 生命周期：started → retrying → completed
{
  const state = createTraceState()
  applyTraceEvent(state, baseEvent())
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 2,
      event_type: 'tool.execution.retrying',
      occurred_at: '2026-09-08T06:42:01.000Z'
    })
  )
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 3,
      event_type: 'tool.execution.completed',
      occurred_at: '2026-09-08T06:42:03.000Z',
      duration_ms: 3000,
      summary: '命中 18 条'
    })
  )
  const span = state.spans['tool-1']
  assert.equal(span.status, 'COMPLETED')
  assert.equal(span.retry_count, 1)
  assert.equal(span.duration_ms, 3000)
  assert.equal(state.summary.tool_calls, 1)
  assert.equal(state.summary.retry_count, 1)
  assert.equal(state.lastAppliedSequence, 3)
}

// 2. 幂等：重复/旧序号事件直接丢弃
{
  const state = createTraceState()
  applyTraceEvent(state, baseEvent({ sequence: 5 }))
  applyTraceEvent(state, baseEvent({ sequence: 5 }))
  applyTraceEvent(state, baseEvent({ sequence: 4 }))
  assert.equal(state.lastAppliedSequence, 5)
  assert.equal(state.summary.tool_calls, 1)
}

// 3. MODEL 计数与 TTFT/token 汇总
{
  const state = createTraceState()
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 1,
      category: 'RUN',
      event_type: 'run.execution.started',
      span_id: 'run',
      title: '本轮执行'
    })
  )
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 2,
      category: 'MODEL',
      event_type: 'model.generation.started',
      span_id: 'model-1',
      title: '模型生成'
    })
  )
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 3,
      category: 'MODEL',
      event_type: 'model.generation.first_token',
      span_id: null,
      occurred_at: '2026-09-08T06:42:00.841Z'
    })
  )
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 4,
      category: 'MODEL',
      event_type: 'model.generation.completed',
      span_id: 'model-1',
      duration_ms: 3800,
      attributes: { input_tokens: 1000, output_tokens: 500, total_tokens: 1500 }
    })
  )
  assert.equal(state.summary.model_calls, 1)
  assert.equal(state.summary.ttft_ms, 841)
  assert.equal(state.summary.total_tokens, null) // 计费值只由服务端 UsageLedger 快照注入
  assert.equal(state.summary.status, 'running') // run 未终态
  assert.equal(state.spans['model-1'].status, 'COMPLETED')
}

// 4. 快照重置：snapshot_sequence 抬高去重水位
{
  const state = createTraceState()
  state.lastAppliedSequence = 3
  applyTraceSnapshot(state, {
    snapshot_sequence: 10,
    projection_sequence: 10,
    summary: { status: 'completed', model_calls: 2 },
    spans: [{ span_id: 'run', category: 'RUN', status: 'COMPLETED', title: '本轮执行' }]
  })
  assert.equal(state.lastAppliedSequence, 10)
  assert.equal(state.snapshotSequence, 10)
  assert.equal(state.spans.run.status, 'COMPLETED')
  applyTraceEvent(state, baseEvent({ sequence: 7 })) // 旧事件被水位拦截
  assert.equal(state.summary.tool_calls, undefined)
}

// 5. 上一轮迟到的快照不能覆盖当前 run
{
  const state = createTraceState()
  state.runId = 'run-new'
  state.lastAppliedSequence = 2
  const applied = applyTraceSnapshot(state, {
    run_id: 'run-old',
    snapshot_sequence: 9,
    projection_sequence: 9,
    summary: { status: 'completed' },
    spans: []
  })
  assert.equal(applied, false)
  assert.equal(state.runId, 'run-new')
  assert.equal(state.lastAppliedSequence, 2)
}

// 6. 时间线树：parent/child 组装
{
  const state = createTraceState()
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 1,
      category: 'RUN',
      event_type: 'run.execution.started',
      span_id: 'run',
      title: '本轮执行'
    })
  )
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 2,
      category: 'KNOWLEDGE',
      event_type: 'knowledge.search.started',
      span_id: 'kr_1',
      parent_span_id: 'run',
      title: '知识检索'
    })
  )
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 3,
      category: 'TOOL',
      event_type: 'tool.execution.started',
      span_id: 'tool-1',
      parent_span_id: 'kr_1',
      title: 'Vector Search'
    })
  )
  const timeline = buildTraceTimeline(state)
  assert.equal(timeline.length, 1)
  assert.equal(timeline[0].span_id, 'run')
  assert.equal(timeline[0].children.length, 1)
  assert.equal(timeline[0].children[0].span_id, 'kr_1')
  assert.equal(timeline[0].children[0].children[0].span_id, 'tool-1')
}

// 7. failed span 携带 error_type
{
  const state = createTraceState()
  applyTraceEvent(state, baseEvent({ sequence: 1 }))
  applyTraceEvent(
    state,
    baseEvent({
      sequence: 2,
      event_type: 'tool.execution.failed',
      duration_ms: 1200,
      attributes: { error_type: 'TimeoutError' }
    })
  )
  assert.equal(state.spans['tool-1'].status, 'FAILED')
  assert.equal(state.spans['tool-1'].error_type, 'TimeoutError')
  assert.equal(state.summary.error_count, 1)
}

// 8. 历史消息按最后一条持久化 run_id 恢复，兼容 metadata 旧形态
{
  assert.equal(
    findLatestTraceRunId([
      { type: 'human', run_id: 'run-1' },
      { type: 'ai', extra_metadata: { run_id: 'run-2' } }
    ]),
    'run-2'
  )
  assert.equal(findLatestTraceRunId([{ type: 'human' }]), null)
}

// 9. SKILL 解析事件计入 skill_count
{
  const state = createTraceSummary()
  const trace = { spans: {}, summary: state, lastAppliedSequence: 0 }
  applyTraceEvent(
    trace,
    baseEvent({
      sequence: 1,
      category: 'SKILL',
      event_type: 'skill.runtime.resolved',
      span_id: null,
      attributes: { prompt_skills: ['rice-gene-analysis', 'lit-review'] }
    })
  )
  assert.equal(trace.summary.skill_count, 2)
}

// 10. 迟到快照不覆盖新事件；纯环数据也不会递归爆栈
{
  const state = createTraceState()
  applyTraceEvent(state, baseEvent({ sequence: 12 }))
  const applied = applyTraceSnapshot(state, {
    snapshot_sequence: 8,
    projection_sequence: 8,
    summary: { status: 'running' },
    spans: []
  })
  assert.equal(applied, false)
  assert.ok(state.spans['tool-1'])

  state.spans.a = { span_id: 'a', parent_span_id: 'b', started_at: null }
  state.spans.b = { span_id: 'b', parent_span_id: 'a', started_at: null }
  assert.doesNotThrow(() => buildTraceTimeline(state))
}

console.log('traceProjection.spec.js: all tests passed')
