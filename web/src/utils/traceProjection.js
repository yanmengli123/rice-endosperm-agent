/**
 * 执行轨迹前端投影（纯函数）：yuxi.run-trace.v1 事件 → span/summary 展示状态。
 *
 * 规则与服务端 projector（backend/package/yuxi/trace/projector.py）一致：
 * - 事件是不可变事实，span 状态由事件序列推导；
 * - `.started` 开 span、`.completed/.failed/.interrupted` 闭合；
 * - run 级计数与 token/TTFT 汇总进 summary；
 * - 静默兜底路径（无效序列/迟到快照/纯环）经 traceTelemetry 留痕，降级可计数。
 */
import { reportTraceDegradation } from './traceTelemetry.js'

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

/** 阶段条产物清单上限（与服务端 projector._ARTIFACT_FACET_MAX_ITEMS 同值） */
const ARTIFACT_FACET_MAX_ITEMS = 20

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
  summary: null,
  // 阶段条（P0「权威计划可见」）：run.plan.resolved / knowledge.search.* /
  // answer.source_guard.completed 的投影，buildTraceStages 据此推导四阶段状态。
  plan: null,
  knowledge: null,
  guard: null,
  render: null,
  artifacts: []
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
  error_count: 0,
  artifact_count: 0
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
  if (sequence <= 0) {
    reportTraceDegradation({
      runId: state?.runId,
      reason: 'invalid_event_sequence',
      detail: { sequence: traceEvent.sequence }
    })
    return
  }
  if (sequence <= state.lastAppliedSequence) return

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
  if (eventType === 'run.plan.resolved') {
    state.plan = {
      source_policy: attributes.source_policy || null,
      evidence_level: attributes.evidence_level || null,
      retrieval_required: Boolean(attributes.retrieval_required),
      requires_mcp: Boolean(attributes.requires_mcp),
      satisfiable: attributes.satisfiable !== false,
      required_server: attributes.required_server || null,
      required_server_missing: attributes.required_server_missing || null,
      error_code: attributes.error_code || null,
      at: occurredAt
    }
  }
  if (eventType === 'run.artifact.materialized') {
    // 快照 summary 可能来自旧服务端（无 artifact_count），必须 NaN 守卫。
    summary.artifact_count = (Number(summary.artifact_count) || 0) + 1
    if (state.artifacts.length < ARTIFACT_FACET_MAX_ITEMS) {
      state.artifacts.push({
        name: attributes.name || '',
        origin_source: attributes.origin_source || null,
        size_bytes: Number(attributes.size_bytes) || 0
      })
    }
  }
  if (eventType.startsWith('knowledge.search.')) {
    state.knowledge = {
      status: suffix === 'skipped' ? 'SKIPPED' : TERMINAL_SUFFIX_STATUS[suffix] || 'RUNNING',
      intent: attributes.intent || null,
      contract_status: attributes.contract_status || null,
      completeness_status: attributes.completeness_status || null,
      evidence_count:
        attributes.evidence_count === undefined ? null : Number(attributes.evidence_count) || 0,
      claim_count:
        attributes.claim_count === undefined ? null : Number(attributes.claim_count) || 0,
      verbatim_hit_count:
        attributes.verbatim_hit_count === undefined
          ? null
          : Number(attributes.verbatim_hit_count) || 0
    }
  }
  if (eventType === 'answer.source_guard.completed') {
    state.guard = {
      guard_status: attributes.guard_status || null,
      evidence_level: attributes.evidence_level || null
    }
  }
  if (eventType === 'answer.render.applied') {
    state.render = {
      boundary: attributes.boundary || null,
      applied: Boolean(attributes.applied),
      fallback_reason: attributes.fallback_reason || null
    }
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
  if (state.runId && snapshotRunId && state.runId !== snapshotRunId) {
    reportTraceDegradation({
      runId: state.runId,
      reason: 'snapshot_run_mismatch',
      detail: { snapshotRunId }
    })
    return false
  }
  const projectionSequence = Number(snapshot?.projection_sequence) || 0
  // 迟到的旧快照绝不能覆盖已消费的新事件，否则 spans 回退而游标仍前进。
  if (projectionSequence < state.lastAppliedSequence) {
    reportTraceDegradation({
      runId: state.runId || snapshotRunId,
      reason: 'stale_snapshot_rejected',
      detail: { projectionSequence, lastAppliedSequence: state.lastAppliedSequence }
    })
    return false
  }
  if (snapshot && !Array.isArray(snapshot.spans) && !snapshot.summary) {
    reportTraceDegradation({
      runId: snapshotRunId || state.runId,
      reason: 'snapshot_missing_projection',
      detail: { keys: Object.keys(snapshot).slice(0, 8) }
    })
  }
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
  // 阶段条 facets 水合：快照起步（历史轮档案 / 整页刷新）不重放事件，
  // 服务端投影 summary.attributes 是唯一真源；旧数据无 facets 时保持
  // null，让 buildTraceStages 走兜底推导而不是显示误导性状态。
  const summaryAttributes = snapshot?.summary?.attributes || {}
  state.plan = summaryAttributes.plan || null
  state.knowledge = summaryAttributes.knowledge || null
  state.guard = summaryAttributes.guard || null
  state.render = summaryAttributes.render || null
  state.artifacts = Array.isArray(summaryAttributes.artifacts)
    ? summaryAttributes.artifacts.slice(0, ARTIFACT_FACET_MAX_ITEMS)
    : []
  if (state.summary) {
    const snapshotArtifactCount = Number(summaryAttributes.artifact_count)
    state.summary.artifact_count = Number.isFinite(snapshotArtifactCount)
      ? snapshotArtifactCount
      : Number(state.summary.artifact_count) || 0
  }
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
  const cycleOnly = sorted.filter((span) => !visited.has(span.span_id))
  if (cycleOnly.length) {
    reportTraceDegradation({
      runId: state?.runId,
      reason: 'span_cycle_detected',
      detail: { orphanCount: cycleOnly.length }
    })
  }
  cycleOnly.forEach((span) => timeline.push(attach(span)))
  return timeline
}

/**
 * 阶段条推导（P0「权威计划可见」）：计划 → 检索 → 生成 → 守卫。
 *
 * 状态来源优先级：专项事件投影（plan/knowledge/guard）> summary 兜底推导。
 * 旧 run（无 run.plan.resolved 事件）自动落到兜底路径，不缺阶段。
 */
export const buildTraceStages = (state) => {
  const summary = state?.summary || {}
  const plan = state?.plan || null
  const knowledge = state?.knowledge || null
  const guard = state?.guard || null
  const runTerminal = ['completed', 'failed', 'cancelled', 'interrupted'].includes(summary.status)

  // 计划：有 run.plan.resolved 即已解析；不可满足标 FAILED；旧 run 无事件时
  // 用「已发生任何活动」推导为隐性完成，避免阶段条永远卡在第一位。
  let planStatus = 'PENDING'
  let planDetail = null
  if (plan) {
    planStatus = plan.satisfiable ? 'COMPLETED' : 'FAILED'
    const bits = [plan.source_policy, plan.evidence_level].filter(Boolean)
    if (plan.required_server) bits.push(`点名 ${plan.required_server}`)
    if (plan.required_server_missing) bits.push(`未绑定 ${plan.required_server_missing}`)
    if (!plan.satisfiable && plan.error_code) bits.push(plan.error_code)
    planDetail = bits.join('｜')
  } else if (
    summary.knowledge_calls > 0 ||
    summary.model_calls > 0 ||
    summary.tool_calls > 0 ||
    summary.mcp_calls > 0
  ) {
    planStatus = 'SKIPPED'
    planDetail = '旧版本运行（无计划事件）'
  }

  // 检索：knowledge.search.* 投影；计划明确判定无需检索时标 SKIPPED。
  let retrievalStatus = 'PENDING'
  let retrievalDetail = null
  if (knowledge) {
    retrievalStatus = knowledge.status
    if (knowledge.status === 'SKIPPED') {
      retrievalDetail = '本轮判定无需知识检索'
    } else {
      const bits = []
      if (knowledge.evidence_count !== null) bits.push(`${knowledge.evidence_count} 证据`)
      if (knowledge.claim_count !== null && knowledge.claim_count > 0)
        bits.push(`${knowledge.claim_count} Claim`)
      if (knowledge.completeness_status) bits.push(knowledge.completeness_status)
      retrievalDetail = bits.join('｜') || knowledge.intent
    }
  } else if (summary.knowledge_calls > 0) {
    retrievalStatus = runTerminal ? 'COMPLETED' : 'RUNNING'
  } else if (plan && !plan.retrieval_required && !plan.requires_mcp) {
    retrievalStatus = 'SKIPPED'
    retrievalDetail = '本轮判定无需知识检索'
  } else if (runTerminal) {
    retrievalStatus = 'SKIPPED'
  }

  // 生成：模型调用驱动；run 终态时收敛。
  let generationStatus = 'PENDING'
  if (summary.model_calls > 0) {
    generationStatus = runTerminal
      ? summary.status === 'completed'
        ? 'COMPLETED'
        : summary.status === 'failed'
          ? 'FAILED'
          : 'INTERRUPTED'
      : 'RUNNING'
  } else if (runTerminal) {
    generationStatus = 'SKIPPED'
  }

  // 守卫：answer.source_guard.completed 投影；REJECTED/DEGRADED 是用户必须
  // 看见的信号，标红/标黄而非隐藏。无守卫轮（E0/闲聊）在 run 完成后标 SKIPPED。
  let guardStatus = 'PENDING'
  let guardDetail = null
  if (guard) {
    if (guard.guard_status === 'PASSED') guardStatus = 'COMPLETED'
    else if (guard.guard_status === 'REJECTED') guardStatus = 'FAILED'
    else if (guard.guard_status === 'DEGRADED') guardStatus = 'DEGRADED'
    else guardStatus = 'COMPLETED'
    guardDetail = [guard.guard_status, guard.evidence_level].filter(Boolean).join('｜')
  } else if (runTerminal) {
    guardStatus = 'SKIPPED'
  }

  return [
    { key: 'plan', label: '计划', status: planStatus, detail: planDetail },
    { key: 'retrieval', label: '检索', status: retrievalStatus, detail: retrievalDetail },
    { key: 'generation', label: '生成', status: generationStatus, detail: null },
    { key: 'guard', label: '守卫', status: guardStatus, detail: guardDetail }
  ]
}
