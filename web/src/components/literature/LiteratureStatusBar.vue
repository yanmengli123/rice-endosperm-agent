<template>
  <footer
    class="lit-statusbar"
    :class="{ expanded }"
    role="contentinfo"
    aria-label="文献工作台信息面板"
  >
    <div
      class="statusbar-header"
      role="button"
      tabindex="0"
      :aria-expanded="expanded"
      @click="toggleExpanded"
      @keydown.enter.prevent="toggleExpanded"
      @keydown.space.prevent="toggleExpanded"
    >
      <span class="statusbar-title" aria-hidden="true">⚙ 信息面板</span>
      <span v-if="errorCount" class="statusbar-badge" aria-label="未读错误数">{{
        errorCount
      }}</span>
      <span class="statusbar-last" :title="lastEvent?.message || ''">{{
        lastEvent ? lastEvent.message : '—'
      }}</span>
      <span
        class="statusbar-stats"
        :title="`本月平台凭据 ${formatTokenCount(stats.platform)} · 自备凭据 ${formatTokenCount(stats.byok)} tokens（平台计量，不做费用估算）`"
      >
        <span class="stat-item"
          >本次 <span class="stat-num">{{ formatTokenCount(stats.session) }}</span></span
        >
        <span class="stat-item"
          >累计 <span class="stat-num">{{ formatTokenCount(stats.monthly) }}</span></span
        >
        <button
          class="stat-reset"
          type="button"
          title="清零：重置本次统计基线（不影响平台计量）"
          @click.stop="resetBaseline"
        >
          清零
        </button>
      </span>
      <select
        class="statusbar-filter"
        aria-label="事件过滤"
        :value="filter"
        @click.stop
        @change="onFilterChange"
      >
        <option value="all">全部</option>
        <option value="errors">仅错误</option>
        <option value="tasks">任务</option>
        <option value="system">系统</option>
      </select>
      <button
        class="statusbar-clear"
        type="button"
        title="清空信息面板日志（仅前端显示，不影响记录）"
        @click.stop="clearLog"
      >
        清空日志
      </button>
      <span class="statusbar-arrow" aria-hidden="true">{{ expanded ? '▼' : '▲' }}</span>
    </div>
    <div v-if="expanded" class="statusbar-body" role="log" aria-live="polite">
      <div v-if="!filteredEvents.length" class="statusbar-empty">暂无事件</div>
      <div
        v-for="(event, index) in filteredEvents"
        :key="`${event.ts}-${index}`"
        class="evt"
        :class="event.level"
      >
        <span class="evt-ts">{{ formatTs(event.ts) }}</span>
        <span class="evt-lv">{{ event.level }}</span>
        <span class="evt-src">{{ event.source }}</span>
        <span class="evt-msg">{{ event.message }}</span>
      </div>
    </div>
  </footer>
</template>

<script setup>
// PaperAgent §3.3 底部信息面板的平台化移植：事件由任务投影轮询派生，
// token 读数来自 /api/user/usage 真实计量。独立自持（无 props），
// 视图只需挂在网格下方。轮询失败记为系统 error 事件，不静默。
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import { apiGet } from '@/apis/base'
import { listLiteratureTasks } from '@/apis/literature_api'
import {
  LITERATURE_EVENT_LIMIT,
  createLiteratureEventBuffer,
  diffTasksToEvents,
  formatTokenCount,
  usageStats
} from '@/composables/useLiteratureEvents'

const TASK_POLL_MS = 5000
const USAGE_POLL_MS = 30000

// 存储必须用 reactive 数组：record 直接改数组，ref(裸数组) 拿不到变更触发
const events = reactive([])
const buffer = createLiteratureEventBuffer(LITERATURE_EVENT_LIMIT, events)
const expanded = ref(false)
const filter = ref('all')
const baseline = ref(null)
const currentUsage = ref(null)

const lastEvent = computed(() => (events.length ? events[events.length - 1] : null))
const errorCount = computed(() => {
  const total = events.filter((e) => e.level === 'error').length
  if (!total) return ''
  return total > 99 ? '99+' : String(total)
})
const filteredEvents = computed(() => {
  if (filter.value === 'errors') return events.filter((e) => e.level === 'error')
  if (filter.value === 'tasks') return events.filter((e) => e.source === '任务')
  if (filter.value === 'system') return events.filter((e) => e.source === '系统')
  return events.slice()
})
const stats = computed(() => usageStats(baseline.value, currentUsage.value))

const formatTs = (ts) => {
  const date = new Date(ts)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

const toggleExpanded = () => {
  expanded.value = !expanded.value
}
const onFilterChange = (event) => {
  filter.value = event.target.value
}
const clearLog = () => {
  buffer.clear()
}
const resetBaseline = () => {
  baseline.value = currentUsage.value ? { ...currentUsage.value } : null
}

let prevTasks = null
let taskTimer = null
let usageTimer = null
let generation = 0

const pollTasks = async () => {
  const run = generation
  try {
    const data = await listLiteratureTasks(60)
    if (run !== generation) return
    const tasks = data?.tasks || []
    diffTasksToEvents(prevTasks, tasks, buffer.record.bind(buffer))
    prevTasks = tasks
  } catch (error) {
    if (run !== generation) return
    buffer.record('error', '系统', `任务投影轮询失败：${error?.message || error}`)
  }
}

const pollUsage = async () => {
  const run = generation
  try {
    const data = await apiGet('/api/user/usage?days=1')
    if (run !== generation) return
    currentUsage.value = data || {}
    if (baseline.value === null) baseline.value = { ...currentUsage.value }
  } catch (error) {
    if (run !== generation) return
    buffer.record('error', '系统', `用量读取失败：${error?.message || error}`)
  }
}

onMounted(() => {
  buffer.record('info', '系统', '信息面板已启动（事件来自文献任务投影，每 5 秒刷新）')
  pollTasks()
  pollUsage()
  taskTimer = window.setInterval(pollTasks, TASK_POLL_MS)
  usageTimer = window.setInterval(pollUsage, USAGE_POLL_MS)
})

onBeforeUnmount(() => {
  generation += 1
  if (taskTimer) window.clearInterval(taskTimer)
  if (usageTimer) window.clearInterval(usageTimer)
})
</script>

<style lang="less" scoped>
@statusbar-h: 34px;

.lit-statusbar {
  flex-shrink: 0;
  border-top: 1px solid var(--gray-300, #d0d5dd);
  background: var(--color-bg-container);
  font-size: 11.5px;
  color: var(--color-text);
}

.statusbar-header {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: @statusbar-h;
  padding: 3px 12px;
  cursor: pointer;
  user-select: none;

  &:focus-visible {
    outline: 2px solid var(--main-color);
    outline-offset: -2px;
  }
}

.statusbar-title {
  font-weight: 600;
  white-space: nowrap;
}

.statusbar-badge {
  color: var(--color-error-700, #dc2626);
  font-weight: 700;
  font-size: 11px;
  white-space: nowrap;
}

.statusbar-last {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text-tertiary, #6b7280);
}

.statusbar-stats {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--color-text-tertiary, #6b7280);
  white-space: nowrap;
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;

  .stat-num {
    color: var(--color-text);
    font-weight: 600;
  }
}

.stat-reset,
.statusbar-clear {
  border: 1px solid var(--gray-300, #d0d5dd);
  border-radius: 5px;
  background: transparent;
  color: var(--color-text-tertiary, #6b7280);
  font-size: 11px;
  line-height: 1.4;
  padding: 1px 8px;
  cursor: pointer;

  &:hover {
    color: var(--main-color);
    border-color: var(--main-color);
  }
}

.statusbar-filter {
  font-size: 11px;
  border: 1px solid var(--gray-300, #d0d5dd);
  border-radius: 5px;
  padding: 1px 5px;
  background: var(--color-field-bg, #fff);
  color: var(--color-text);
  flex-shrink: 0;
}

.statusbar-arrow {
  color: var(--color-text-tertiary, #6b7280);
  flex-shrink: 0;
}

.statusbar-body {
  height: 160px;
  overflow-y: auto;
  border-top: 1px solid var(--gray-150, #e2e5ea);
  background: var(--color-bg-elevated);
}

.statusbar-empty {
  padding: 8px 14px;
  color: var(--color-text-tertiary, #6b7280);
}

.evt {
  display: flex;
  gap: 8px;
  padding: 3px 14px;
  border-bottom: 1px solid var(--gray-100, #f1f2f4);
  line-height: 1.5;
  word-break: break-all;
  overflow-wrap: anywhere;
  font-size: 11.5px;

  .evt-ts {
    color: var(--color-text-tertiary, #6b7280);
    white-space: nowrap;
    font-family: ui-monospace, Consolas, monospace;
  }

  .evt-lv {
    text-transform: uppercase;
    font-weight: 700;
    min-width: 34px;
    flex-shrink: 0;
    color: var(--color-text-tertiary, #6b7280);
  }

  .evt-src {
    min-width: 34px;
    font-weight: 600;
    color: var(--color-text-tertiary, #6b7280);
    flex-shrink: 0;
  }

  .evt-msg {
    flex: 1;
  }

  &.error {
    background: color-mix(in srgb, var(--color-error-700, #dc2626) 7%, transparent);

    .evt-lv {
      color: var(--color-error-700, #dc2626);
    }
  }

  &.warning .evt-lv {
    color: var(--color-warning-700, #d97706);
  }
}
</style>
