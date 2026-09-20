/**
 * 知识图谱治理前端纯逻辑：术语字典、批量准入判定、原因代码、审计 diff 摘要。
 *
 * 术语纪律（企业级可追责）：机器校验与人工决策严格分词——
 *  - 人工决策：人工批准 / 人工拒绝 / 规范层（托管导入，只读）
 *  - 机器校验：机器校验·触发词 / 机器校验·双模型复核 / 机器校验·多源佐证
 */

export const REVIEW_STATUS_META = Object.freeze({
  CANDIDATE: { label: 'AI 候选', short: '候选', color: 'default' },
  APPROVED: { label: '人工批准', short: '已批准', color: 'green' },
  REJECTED: { label: '人工拒绝', short: '已拒绝', color: 'red' },
  CANONICAL: { label: '规范层（托管导入）', short: '规范层', color: 'purple' }
})

export const TRUST_META = Object.freeze({
  VERIFIED_CORROBORATED: {
    label: '机器校验·多源佐证',
    color: 'cyan',
    hint: '触发词或双模型验证通过，且有 ≥2 篇文献佐证'
  },
  VERIFIED_SINGLE: {
    label: '机器校验·单源',
    color: 'blue',
    hint: '触发词或双模型验证通过，仅单一来源'
  },
  CANDIDATE: {
    label: '未机器验证',
    color: 'default',
    hint: '仅通过逐字校验，尚未经语义验证或人工审定'
  }
})

export const VERIFICATION_META = Object.freeze({
  OK: { label: '逐字命中', color: 'green' },
  DEGRADED: { label: '引文与当前原文不一致', color: 'orange' },
  MISSING: { label: '无引文', color: 'red' }
})

/** 标准原因代码（与后端 review_overlay.REASON_CODES 同闭集） */
export const REASON_CODES = Object.freeze([
  { value: 'DIRECTION_ERROR', label: '方向错误（因果颠倒）' },
  { value: 'NEGATION_MISREAD', label: '否定语义误读' },
  { value: 'OVER_GENERALIZATION', label: '过度泛化' },
  { value: 'SOURCE_UNTRUSTWORTHY', label: '来源不可信' },
  { value: 'WRONG_ENDPOINT', label: '端点实体错误' },
  { value: 'SCOPE_MISMATCH', label: '适用范围不符' },
  { value: 'DUPLICATE', label: '重复关系' },
  { value: 'INSUFFICIENT_EVIDENCE', label: '证据不足' },
  { value: 'OTHER', label: '其他' }
])

/** 组合原因代码与说明（与后端 compose_reason 同格式 `[CODE] 说明`） */
export function composeReason(code, note) {
  const normalized = String(code || '').trim().toUpperCase()
  const text = String(note || '').trim()
  const valid = REASON_CODES.some((item) => item.value === normalized)
  return valid && text ? `[${normalized}] ${text}` : text
}

export function reviewStatusLabel(status) {
  return REVIEW_STATUS_META[status]?.label || status || '未知'
}

export function reviewStatusColor(status) {
  return REVIEW_STATUS_META[status]?.color || 'default'
}

export function trustLabel(tier) {
  return TRUST_META[tier]?.label || tier || ''
}

export function trustColor(tier) {
  return TRUST_META[tier]?.color || 'default'
}

export function trustHint(tier) {
  return TRUST_META[tier]?.hint || ''
}

/**
 * 批量批准准入档位说明（严格程度与后端 evaluate_batch_admission 对齐）。
 */
export const BATCH_ADMISSION_META = Object.freeze({
  strict: {
    label: '严格（默认）',
    hint: '机器校验·多源佐证 + 引文逐字 OK 才允许批量批准'
  },
  standard: {
    label: '标准',
    hint: '任一机器校验通过 + 引文逐字 OK 即可批量批准'
  },
  relaxed: {
    label: '宽松',
    hint: '只要存在逐字 OK 引文即可批量批准（不建议生产使用）'
  }
})

/** 批量预检结果的可读原因（与后端 reason 代码对齐） */
export const PREVIEW_REASONS = Object.freeze({
  corroborated: '多源机器校验 + 引文 OK',
  single_source: '单源机器校验 + 引文 OK',
  ok_quote: '存在引文 OK（宽松档）',
  no_ok_quote: '无逐字 OK 引文，必须逐条人工',
  below_threshold: '机器校验等级低于准入档位',
  conflict_open: '冲突登记中，禁止批量',
  not_found: '对象不存在'
})

/**
 * 汇总批量预检结果 → 确认页文案（条数、准入/阻断、抽样预览）。
 * 返回 { admissible, blocked, sample: [{id, quote, chunkId}] }
 */
export function summarizeBatchPreview(preview) {
  const items = preview?.items || []
  const admissible = items.filter((item) => item.admissible)
  const blocked = items.filter((item) => !item.admissible)
  const sample = admissible.slice(0, 5).map((item) => ({
    id: item.target_id,
    quote: item.preview_quote || '（无引文）',
    chunkId: item.pinned_chunk_id || ''
  }))
  return { admissible, blocked, sample }
}

/** 审计动作的可读标签 */
export const AUDIT_ACTION_META = Object.freeze({
  APPROVE: '人工批准',
  REJECT: '人工拒绝',
  SUPERSEDE: '编辑（取代）',
  RENAME: '改名/别名',
  RETYPE: '改类型',
  ALIAS_PROMOTE: '别名晋升',
  ADD_RELATION: '手动补关系',
  REEXTRACT_CHUNK: '重抽段落',
  REJECT_CASCADE: '级联拒绝',
  GATE_PROMOTE: '门禁升格',
  GATE_DISCARD: '门禁废弃',
  CONFLICT_RESOLVE: '冲突裁决',
  GOVERNANCE_SETTINGS_UPDATE: '治理设置变更'
})

export function auditActionLabel(action) {
  return AUDIT_ACTION_META[action] || action
}

/** 边的审核状态（画布线型编码的数据来源） */
export function edgeReviewStatus(edge) {
  const properties = edge?.properties || {}
  if (properties.managed_projection) return 'CANONICAL'
  return properties.review_status || null
}

/** 画布边线型编码：AI 候选虚线 / 人工批准实线加粗 / 规范层长划线 / 冲突警示 */
export function edgeStyleByReviewStatus(edge, baseStyle = {}) {
  const status = edgeReviewStatus(edge)
  const conflict = edge?.properties?.conflict_status === 'CONTESTED'
  if (conflict) {
    return { ...baseStyle, lineDash: [2, 3], lineWidth: 1.6 }
  }
  switch (status) {
    case 'APPROVED':
      return { ...baseStyle, lineDash: undefined, lineWidth: 2 }
    case 'CANONICAL':
      return { ...baseStyle, lineDash: [12, 3], lineWidth: 2 }
    case 'REJECTED':
      return { ...baseStyle, lineDash: [4, 4], lineWidth: 1, opacity: 0.4 }
    case 'CANDIDATE':
    default:
      return { ...baseStyle, lineDash: [6, 4], lineWidth: 1.2 }
  }
}

export const EDGE_STATUS_LEGEND = Object.freeze([
  { key: 'CANDIDATE', label: 'AI 候选', pattern: '虚线' },
  { key: 'APPROVED', label: '人工批准', pattern: '实线（加粗）' },
  { key: 'CANONICAL', label: '规范层', pattern: '长划线' },
  { key: 'CONFLICT', label: '冲突中', pattern: '点线' }
])

// ---- 图谱构建状态规范化（getStatus 返回 → 统一展示模型） ----
// 后端权威字段：total_chunks / indexed_chunks / pending_chunks / dead_chunks /
// stale_cached_chunks / build_task_{status,progress,message,error}
// 前端展示只允许消费这里产出的归一化结构，不直接拼接原始字段。

export const BUILD_STATUS_LABELS = Object.freeze({
  pending: '排队中',
  running: '正在索引',
  success: '已完成',
  failed: '失败',
  canceled: '已取消'
})

const toSafeCount = (value) => {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
}

/**
 * 规范化 graphBuildApi.getStatus 的返回为展示模型。
 * @param {object|null} raw getStatus 响应
 * @returns {{total:number, indexed:number, pending:number, dead:number, stale:number,
 *   percent:number, taskStatus:string|null, taskProgress:number, taskMessage:string,
 *   taskError:string|null, active:boolean, configured:boolean}}
 */
export function normalizeBuildStatus(raw) {
  const s = raw || {}
  const total = toSafeCount(s.total_chunks)
  const indexed = toSafeCount(s.indexed_chunks)
  const pending = toSafeCount(s.pending_chunks)
  const taskStatus = typeof s.build_task_status === 'string' ? s.build_task_status : null
  return {
    total,
    indexed,
    pending,
    dead: toSafeCount(s.dead_chunks),
    stale: toSafeCount(s.stale_cached_chunks),
    percent: total > 0 ? Math.min(100, Math.round((indexed / total) * 100)) : 0,
    taskStatus,
    taskProgress: Math.min(100, Math.max(0, toSafeCount(s.build_task_progress))),
    taskMessage: typeof s.build_task_message === 'string' ? s.build_task_message : '',
    taskError: typeof s.build_task_error === 'string' && s.build_task_error ? s.build_task_error : null,
    active: taskStatus === 'pending' || taskStatus === 'running',
    configured: Boolean(s.configured)
  }
}

/** 顶部状态行：「已索引 1,234 / 共 5,678 段 · 待索引 4,444」 */
export function buildStatusSummary(model) {
  const fmt = (n) => n.toLocaleString('zh-CN')
  const parts = [`已索引 ${fmt(model.indexed)}`, `共 ${fmt(model.total)} 段`]
  if (model.pending > 0) parts.push(`待索引 ${fmt(model.pending)}`)
  if (model.dead > 0) parts.push(`死信 ${fmt(model.dead)}`)
  if (model.stale > 0) parts.push(`缓存过期 ${fmt(model.stale)}`)
  return parts.join(' · ')
}

/** 任务态描述：排队中/正在索引（带任务进度）/失败原因/已完成 */
export function buildTaskLabel(model) {
  if (!model.taskStatus) return ''
  if (model.active) {
    const base = BUILD_STATUS_LABELS[model.taskStatus] || model.taskStatus
    return model.taskProgress > 0 ? `${base} ${model.taskProgress}%` : base
  }
  if (model.taskStatus === 'failed') {
    return model.taskMessage || model.taskError || '索引失败'
  }
  return BUILD_STATUS_LABELS[model.taskStatus] || model.taskStatus
}
