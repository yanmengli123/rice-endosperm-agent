// 底部信息面板的事件与用量模型（PaperAgent 手册 §3.3 的平台化移植）。
// 事件从文献任务投影（/api/literature/tasks，5s 轮询）的状态跃迁派生，
// token 读数来自平台用量接口（/api/user/usage）——只显示真实计量，
// 不做无价目表支撑的费用估算。纯函数与副作用分离，便于裸 node 测试。

export const LITERATURE_EVENT_LIMIT = 200

export const TASK_TYPE_LABELS = {
  reader_render: '阅读渲染',
  compile: '编译',
  translation: '翻译',
  ingest: '入库'
}

// 状态语义以后端投影下发的 is_active / is_failure 布尔位为**权威**
// （后端新增状态时前端零改动即正确）；下列集合仅作旧载荷（无布尔位）的兼容兜底。
const BAD_FALLBACK = new Set(['FAILED', 'DEAD', 'REJECTED'])
const ACTIVE_FALLBACK = new Set(['PENDING', 'RUNNING', 'WAITING_CACHE', 'BUILDING', 'INDEXING'])

function isActiveTask(task) {
  return typeof task.is_active === 'boolean' ? task.is_active : ACTIVE_FALLBACK.has(task.status)
}

function isFailureTask(task) {
  return typeof task.is_failure === 'boolean' ? task.is_failure : BAD_FALLBACK.has(task.status)
}

export function createLiteratureEventBuffer(limit = LITERATURE_EVENT_LIMIT, storage = []) {
  const events = storage
  return {
    events,
    record(level, source, message, ts = Date.now()) {
      const last = events[events.length - 1]
      // 连续完全相同的事件只保留一条（轮询抖动/重复错误不刷屏）
      if (last && last.level === level && last.source === source && last.message === message) {
        return last
      }
      events.push({ ts, level, source, message })
      if (events.length > limit) events.splice(0, events.length - limit)
      return events[events.length - 1]
    },
    clear() {
      events.splice(0, events.length)
    }
  }
}

export function taskKey(task) {
  return `${task.type}:${task.ref}`
}

function shortRef(ref) {
  const text = String(ref ?? '')
  return text.length > 28 ? `${text.slice(0, 28)}…` : text
}

/**
 * 任务投影 diff → 事件。
 * 首次快照（prevTasks 为 null）：只记录非初始态任务（面板中途打开时不回放全量历史）；
 * 后续 diff：状态或 attempt 变化即记录，失败终态附错误摘要。
 */
export function diffTasksToEvents(prevTasks, nextTasks, record, ts = Date.now()) {
  // 任务投影按 updated_at DESC 排序，同一身份（如 paper:level）可能同时存在
  // 历史失败行与新成功行——先按键归并（保留最新的一行），否则键碰撞会让
  // Map 覆盖顺序在轮询间翻转，每 5 秒重复记录虚假状态跃迁。
  const collapse = (tasks) => {
    const map = new Map()
    for (const task of tasks || []) {
      const key = taskKey(task)
      const held = map.get(key)
      // updated_at 为 ISO 字符串，字典序即时间序；无值视为最旧
      if (!held || String(task.updated_at || '') > String(held.updated_at || '')) {
        map.set(key, task)
      }
    }
    return map
  }
  const prev = collapse(prevTasks)
  for (const task of collapse(nextTasks).values()) {
    const label = `${TASK_TYPE_LABELS[task.type] || task.type} ${shortRef(task.ref)}`
    const before = prev.get(taskKey(task))
    if (!before) {
      if (task.status && !isActiveTask(task)) {
        // 首见失败也带原因摘要（面板中途打开时同样要能回答"为什么失败"）
        const detail =
          task.error && isFailureTask(task) ? `：${String(task.error).slice(0, 160)}` : ''
        record(
          isFailureTask(task) ? 'error' : 'info',
          '任务',
          `${label} → ${task.status}${detail}`,
          ts
        )
      }
      continue
    }
    if (before.status !== task.status || before.attempt !== task.attempt) {
      const level = isFailureTask(task) ? 'error' : 'info'
      const detail =
        task.error && isFailureTask(task) ? `：${String(task.error).slice(0, 160)}` : ''
      record(level, '任务', `${label} ${before.status}→${task.status}${detail}`, ts)
    }
  }
}

/**
 * 用量读数：会话 = 进入工作台以来的增量（"清零"即重置基线，与上游语义一致）；
 * 累计为本月真实计量；platform/byok 分域只在悬停明细展示。
 */
export function usageStats(baseline, current) {
  const cur = current || {}
  const base = baseline || {}
  return {
    session: Math.max(0, (cur.monthly_tokens || 0) - (base.monthly_tokens || 0)),
    monthly: cur.monthly_tokens || 0,
    platform: cur.monthly_platform_tokens || 0,
    byok: cur.monthly_byok_tokens || 0
  }
}

export function formatTokenCount(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n < 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(Math.round(n))
}
