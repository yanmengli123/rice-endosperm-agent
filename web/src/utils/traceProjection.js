/**
 * 执行轨迹前端投影（纯函数）：yuxi.run-trace.v1 事件 → span/summary 展示状态。
 *
 * 规则与服务端 projector（backend/package/yuxi/trace/projector.py）一致：
 * - 事件是不可变事实，span 状态由事件序列推导；
 * - `.started` 开 span、`.retrying` 计重试、`.completed/.failed/.interrupted` 闭合；
 * - run 级计数与 token/TTFT 汇总进 summary。
 */

const TERMINAL_SUFFIX_STATUS = {
  completed: 'COMPLETED',
  failed: 'FAILED',
  interrupted: 'INTERRUPTED',
  cancelled: 'INTERRUPTED',
  skipped: 'SKIPPED'
}

const CATEGORY_COUNT_FIELDS = {
  MODEL: 'model_calls',
  TOOL: 'tool_calls',
  MCP: 'mcp_calls',
  KNOWLEDGE: 'knowledge_calls',
  SUBAGENT: 'subagent_calls'
}

const parseOccurredAt = (value) => {
  if (!value) return null
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export const createTraceState = () => ({
  runId: null,
  loading: false,
  pendingEvents: [],
  lastAppliedSequence: 0,
  scannedThroughSequence: 0,
  snapshotSequence: 0,
  projectionSequence: 0,
  spans: {},
  summary: null
})

export const createTraceSummary = () => ({
  status: null,
  started_at: null,
  finished_at: null,
  duration_ms: null,
  ttft_ms: null,
  first_token_at: null,
  input_tokens: null,
  output_tokens: null,
  total_tokens: null,
  model_calls: 0,
  tool_calls: 0,
  mcp_calls: 0,
  knowledge_calls: 0,
  subagent_calls: 0,
  skill_count: 0,
  retry_count: 0,
  error_count: 0
})

/** 从服务端历史消息中找到最后一轮持久化 run，供页面重载后恢复权威快照。 */
export const findLatestTraceRunId = (history) => {
  if (!Array.isArray(history)) return null
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const message = history[index]
    const candidate = message?.run_id ?? message?.extra_metadata?.run_id
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim()
  }
  return null
}

/** 把一条 trace 事件应用到本地投影状态（原地变更）。 */
export const applyTraceEvent = (state, traceEvent) => {
  if (!traceEvent || typeof traceEvent !== 'object') return
  if (traceEvent.visibility === 'ADMIN') return
  const sequence = Number(traceEvent.sequence) || 0
  if (sequence <= 0 || sequence <= state.lastAppliedSequence) return

  const summary = (state.summary = state.summary || createTraceSummary())
  const occurredAt = parseOccurredAt(traceEvent.occurred_at)
  const attributes = traceEvent.attributes || {}
  const category = String(traceEvent.category || '')
  const eventType = String(traceEvent.event_type || '')
  const parts = eventType.split('.')
  const suffix = parts.length >= 3 ? parts[parts.length - 1] : ''

  state.lastAppliedSequence = sequence

  if (occurredAt) {
    if (!summary.started_at) summary.started_at = occurredAt
    summary.finished_at = occurredAt
    summary.duration_ms = occurredAt - summary.started_at
  }

  const countField = CATEGORY_COUNT_FIELDS[category]
  if (countField && suffix === 'started') summary[countField] += 1
  if (suffix === 'retrying') summary.retry_count += 1
  if (suffix === 'failed') summary.error_count += 1

  if (category === 'MODEL') {
    if (
      ['model.generation.first_token', 'model.generation.first_visible_token'].includes(
        eventType
      ) &&
      !summary.first_token_at
    ) {
      summary.first_token_at = occurredAt
      if (summary.started_at && occurredAt) {
        summary.ttft_ms = occurredAt - summary.started_at
      }
    }
  }
  if (category === 'SKILL' && eventType === 'skill.runtime.resolved') {
    const skills = attributes.prompt_skills
    summary.skill_count = Array.isArray(skills)
      ? skills.length
      : Number(attributes.skill_count) || 0
  }
  if (category === 'RUN') {
    if (eventType === 'run.execution.started') summary.status = 'running'
    else if (TERMINAL_SUFFIX_STATUS[suffix] && eventType.startsWith('run.execution.')) {
      summary.status = suffix
    }
  }

  const spanId = traceEvent.span_id
  if (!spanId) return
  let span = state.spans[spanId]
  if (!span) {
    span = state.spans[spanId] = {
      span_id: spanId,
      parent_span_id: traceEvent.parent_span_id || null,
      category,
      operation: traceEvent.operation || '',
      title: traceEvent.title || category,
      summary: traceEvent.summary || '',
      message_key: traceEvent.message_key || eventType,
      display_args: traceEvent.display_args || {},
      status: 'RUNNING',
      started_at: occurredAt,
      finished_at: null,
      duration_ms: null,
      error_type: null,
      retry_count: 0,
      attributes: {}
    }
  } else {
    if (traceEvent.title) span.title = traceEvent.title
    if (traceEvent.summary) span.summary = traceEvent.summary
    if (traceEvent.message_key) span.message_key = traceEvent.message_key
    if (traceEvent.display_args) span.display_args = traceEvent.display_args
    if (traceEvent.parent_span_id && !span.parent_span_id) {
      span.parent_span_id = traceEvent.parent_span_id
    }
  }
  if (!span.started_at && occurredAt) span.started_at = occurredAt
  if (suffix in TERMINAL_SUFFIX_STATUS) {
    span.status = TERMINAL_SUFFIX_STATUS[suffix]
    span.finished_at = occurredAt
    span.duration_ms =
      Number(traceEvent.duration_ms) ||
      (span.started_at && occurredAt ? occurredAt - span.started_at : 0)
    if (suffix === 'failed' || suffix === 'interrupted') {
      span.error_type = attributes.error_type || attributes['error.type'] || null
    }
  } else if (suffix === 'retrying') {
    span.retry_count += 1
    span.status = 'RUNNING'
  }
  Object.assign(span.attributes, attributes)
}

/** 用服务端快照（权威投影）重置本地状态。 */
export const applyTraceSnapshot = (state, snapshot) => {
  const snapshotRunId = snapshot?.run_id || null
  if (state.runId && snapshotRunId && state.runId !== snapshotRunId) return false
  const projectionSequence = Number(snapshot?.projection_sequence) || 0
  // 迟到的旧快照绝不能覆盖已消费的新事件，否则 spans 回退而游标仍前进。
  if (projectionSequence < state.lastAppliedSequence) return false
  const spans = {}
  const spanList = Array.isArray(snapshot?.spans) ? snapshot.spans : []
  spanList.forEach((span) => {
    if (!span?.span_id) return
    spans[span.span_id] = {
      ...span,
      started_at: parseOccurredAt(span.started_at),
      finished_at: parseOccurredAt(span.finished_at)
    }
  })
  state.spans = spans
  state.summary = snapshot?.summary
    ? {
        ...snapshot.summary,
        started_at: parseOccurredAt(snapshot.summary.started_at),
        finished_at: parseOccurredAt(snapshot.summary.finished_at),
        first_token_at: parseOccurredAt(snapshot.summary.first_token_at)
      }
    : null
  const snapshotSequence = Number(snapshot?.snapshot_sequence) || 0
  state.snapshotSequence = snapshotSequence
  state.projectionSequence = projectionSequence
  state.runId = snapshotRunId || state.runId
  state.lastAppliedSequence = projectionSequence
  state.scannedThroughSequence = Math.max(state.scannedThroughSequence, projectionSequence)
  return true
}

/** 时间线展示顺序：根 span 在前，同层按开始时间排序。 */
export const buildTraceTimeline = (state) => {
  const spans = Object.values(state?.spans || {})
  const sorted = spans
    .map((span) => ({ ...span, sort_time: span.started_at ? span.started_at.getTime() : 0 }))
    .sort((a, b) => a.sort_time - b.sort_time)
  const byId = new Map(sorted.map((span) => [span.span_id, span]))
  const childrenByParent = new Map()
  sorted.forEach((span) => {
    if (!span.parent_span_id || !byId.has(span.parent_span_id)) return
    const children = childrenByParent.get(span.parent_span_id) || []
    children.push(span)
    childrenByParent.set(span.parent_span_id, children)
  })
  const visited = new Set()
  const attach = (span, ancestry = new Set()) => {
    if (ancestry.has(span.span_id)) return { ...span, children: [] }
    visited.add(span.span_id)
    const nextAncestry = new Set(ancestry)
    nextAncestry.add(span.span_id)
    return {
      ...span,
      children: (childrenByParent.get(span.span_id) || []).map((child) =>
        attach(child, nextAncestry)
      )
    }
  }
  const roots = sorted.filter((span) => !span.parent_span_id || !byId.has(span.parent_span_id))
  const timeline = roots.map((span) => attach(span))
  // 损坏数据中的纯环没有根；作为独立根展示一次，不能递归爆栈。
  sorted.filter((span) => !visited.has(span.span_id)).forEach((span) => timeline.push(attach(span)))
  return timeline
}
