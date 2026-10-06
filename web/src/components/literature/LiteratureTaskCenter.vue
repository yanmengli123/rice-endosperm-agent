<template>
  <section class="lit-task-center" aria-label="文献库任务中心">
    <header class="tc-header">
      <h3>文献库任务中心</h3>
      <span class="tc-meta">
        {{ autoRefreshOn ? '每 5 秒自动刷新' : '暂停刷新' }}
        <template v-if="updatedAt">· 更新于 {{ formatTime(updatedAt) }}</template>
      </span>
      <button class="btn" @click="$emit('refresh')">手动刷新</button>
    </header>

    <div class="tc-filters">
      <div class="tc-segmented" role="tablist" aria-label="状态过滤">
        <button
          v-for="option in statusOptions"
          :key="option.value"
          class="tc-seg"
          :class="{ active: statusFilter === option.value }"
          role="tab"
          :aria-selected="statusFilter === option.value"
          @click="statusFilter = option.value"
        >
          {{ option.label }}
          <span class="tc-count">{{ option.count }}</span>
        </button>
      </div>
      <div class="tc-types" aria-label="类型过滤">
        <button
          v-for="option in typeOptions"
          :key="option.value"
          class="tc-type"
          :class="{ active: typeFilter === option.value }"
          @click="typeFilter = option.value"
        >
          {{ option.label }} <span class="tc-count">{{ option.count }}</span>
        </button>
      </div>
    </div>

    <div v-if="!filteredTasks.length" class="lit-empty">
      {{
        tasks.length ? '当前过滤条件下没有任务' : '暂无任务。导入 PDF、编译、翻译后在此跟踪进度。'
      }}
    </div>
    <ul v-else class="tc-list">
      <li
        v-for="(task, index) in filteredTasks"
        :key="taskKey(task, index)"
        class="tc-card"
        :class="[statusClass(task), { 'tc-highlight': highlightRef && task.ref === highlightRef }]"
      >
        <div class="tc-card-head">
          <span class="tc-status-dot" aria-hidden="true"></span>
          <strong class="tc-title" :title="task.detail || task.ref">
            {{ task.detail || task.ref }}
          </strong>
          <span class="tc-type-badge">{{ typeLabel(task.type) }}</span>
          <span class="tc-status-text">{{ statusLabel(task) }}</span>
        </div>

        <div
          class="tc-progress"
          role="progressbar"
          :aria-valuenow="task.progress || 0"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <div
            class="tc-progress-bar"
            :style="{ width: `${Math.max(2, task.progress || 0)}%` }"
          ></div>
          <span class="tc-progress-text">
            {{ task.stage || task.status }} · {{ task.progress || 0 }}%
          </span>
        </div>

        <div class="tc-card-meta">
          <span v-if="task.type === 'ingest'">单号 {{ shortRef(task.ref) }}</span>
          <span v-if="task.created_at">提交于 {{ formatTime(task.created_at) }}</span>
          <span v-else-if="task.updated_at">更新于 {{ formatTime(task.updated_at) }}</span>
          <template v-if="task.attempt > 0"
            ><span>已尝试 {{ task.attempt + 1 }} 次</span></template
          >
          <span v-if="task.processing_mode === 'parse_only'">仅解析（终点为草稿）</span>
        </div>

        <p v-if="task.error" class="tc-error">{{ task.error }}</p>
        <p v-if="task.type === 'ingest' && task.status === 'REJECTED'" class="tc-hint">
          质量门禁拒绝：不可重试，请检查文件内容（空白 / 乱码 / 非论文 PDF）。
        </p>
        <p v-else-if="task.type === 'ingest' && task.status === 'FAILED'" class="tc-hint">
          供应商解析失败：可重新导入同一文件（内容指纹复用，不产生重复任务）。
        </p>

        <div v-if="task.paper_rid" class="tc-actions">
          <button class="btn" @click="$emit('open-paper', { rid: task.paper_rid })">
            打开文献
          </button>
        </div>
      </li>
    </ul>
  </section>
</template>

<script setup>
/**
 * 文献库任务中心（对标平台 TaskCenterDrawer 的进度与标准，数据源为 PG 持久投影）：
 * - 状态/类型双维过滤 + 实时计数；
 * - 状态点 + 阶段权重进度条（后端权威布尔位 is_active/is_failure/is_terminal 驱动，
 *   前端零状态推断——后端新增状态时本组件零改动即正确）；
 * - 入库失败分类提示、域任务带「哪篇文献」与打开入口。
 * 进度口径：阶段权重（流水线走到哪一段），不是批内伪精度。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  tasks: { type: Array, default: () => [] },
  updatedAt: { type: Number, default: 0 },
  autoRefreshOn: { type: Boolean, default: true },
  highlightRef: { type: String, default: '' }
})
defineEmits(['refresh', 'open-paper'])

const statusFilter = ref('all')
const typeFilter = ref('all')

const TYPE_LABELS = {
  ingest: '入库',
  reader_render: '渲染',
  compile: '编译',
  translation: '翻译'
}

const isDone = (task) => Boolean(task.is_terminal) && !task.is_failure
const isFailed = (task) => Boolean(task.is_failure)

const statusOptions = computed(() => [
  { value: 'all', label: '全部', count: props.tasks.length },
  {
    value: 'active',
    label: '进行中',
    count: props.tasks.filter((task) => task.is_active).length
  },
  { value: 'done', label: '已完成', count: props.tasks.filter(isDone).length },
  { value: 'failed', label: '失败', count: props.tasks.filter(isFailed).length }
])

const typeOptions = computed(() => [
  { value: 'all', label: '全部', count: props.tasks.length },
  ...Object.entries(TYPE_LABELS).map(([value, label]) => ({
    value,
    label,
    count: props.tasks.filter((task) => task.type === value).length
  }))
])

const filteredTasks = computed(() =>
  props.tasks
    .filter((task) => (typeFilter.value === 'all' ? true : task.type === typeFilter.value))
    .filter((task) => {
      if (statusFilter.value === 'active') return task.is_active
      if (statusFilter.value === 'done') return isDone(task)
      if (statusFilter.value === 'failed') return isFailed(task)
      return true
    })
    .slice(0, 100)
)

const typeLabel = (type) => TYPE_LABELS[type] || type
// ref 标识文献/级别，多个不可变任务代次可以共享它。旧投影没有任务 ID，
// 用创建时间区分代次，并为缺失/重复时间戳加位置兜底；历史任务不能被折叠丢弃。
const taskKey = (task, index) =>
  JSON.stringify([task.type, task.task_id ?? task.ref, task.created_at || '', index])
const statusLabel = (task) =>
  task.is_failure ? '失败' : task.is_active ? '进行中' : task.is_terminal ? '已完成' : '进行中'
const statusClass = (task) => ({
  'tc-active': task.is_active,
  'tc-done': isDone(task),
  'tc-failed': isFailed(task)
})
const shortRef = (ref) => String(ref || '').slice(0, 20)
const formatTime = (value) => {
  if (!value) return '—'
  const raw =
    typeof value === 'number' ? new Date(value) : new Date(String(value).replace(' ', 'T'))
  if (Number.isNaN(raw.getTime())) return String(value).slice(0, 19)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(raw.getMonth() + 1)}-${pad(raw.getDate())} ${pad(raw.getHours())}:${pad(raw.getMinutes())}:${pad(raw.getSeconds())}`
}
</script>

<style scoped>
.lit-task-center {
  display: flex;
  flex-direction: column;
  gap: 12px;
  text-align: left;
}
.tc-header {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.tc-header h3 {
  margin: 0;
  font-size: 15px;
}
.tc-meta {
  color: var(--color-text-secondary);
  font-size: 12px;
}
.tc-filters {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.tc-segmented {
  display: inline-flex;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  overflow: hidden;
  width: fit-content;
}
.tc-seg {
  padding: 5px 14px;
  font-size: 13px;
  border: none;
  background: transparent;
  cursor: pointer;
  border-right: 1px solid var(--gray-150);
}
.tc-seg:last-child {
  border-right: none;
}
.tc-seg.active {
  background: var(--main-color, #4a7dbe);
  color: #fff;
}
.tc-count {
  opacity: 0.75;
  font-size: 12px;
  margin-left: 3px;
}
.tc-types {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.tc-type {
  padding: 2px 10px;
  font-size: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 999px;
  background: transparent;
  cursor: pointer;
}
.tc-type.active {
  border-color: var(--main-color, #4a7dbe);
  color: var(--main-color, #4a7dbe);
}
.tc-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tc-card {
  border: 1px solid var(--gray-150);
  border-left: 3px solid var(--gray-150, #bbb);
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tc-card.tc-active {
  border-left-color: var(--main-color, #4a7dbe);
}
.tc-card.tc-done {
  border-left-color: var(--color-success-600, #52c41a);
}
.tc-card.tc-failed {
  border-left-color: var(--color-error-700, #f5222d);
}
.tc-card.tc-highlight {
  outline: 2px solid var(--main-color, #4a7dbe);
  outline-offset: -2px;
}
.tc-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.tc-status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--gray-150, #bbb);
  flex-shrink: 0;
}
.tc-active .tc-status-dot {
  background: var(--main-color, #4a7dbe);
  animation: tc-pulse 1.6s ease-in-out infinite;
}
.tc-done .tc-status-dot {
  background: var(--color-success-600, #52c41a);
}
.tc-failed .tc-status-dot {
  background: var(--color-error-700, #f5222d);
}
@keyframes tc-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}
.tc-title {
  flex: 1 1 200px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
.tc-type-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--gray-150);
  color: var(--color-text-secondary);
}
.tc-status-text {
  font-size: 12px;
  color: var(--color-text-secondary);
}
.tc-active .tc-status-text {
  color: var(--main-color, #4a7dbe);
}
.tc-failed .tc-status-text {
  color: var(--color-error-700, #f5222d);
}
.tc-progress {
  position: relative;
  height: 16px;
  border-radius: 8px;
  background: var(--gray-100, #eee);
  overflow: hidden;
}
.tc-progress-bar {
  height: 100%;
  border-radius: 8px;
  background: var(--main-color, #4a7dbe);
  transition: width 0.4s ease;
  min-width: 2%;
}
.tc-done .tc-progress-bar {
  background: var(--color-success-600, #52c41a);
}
.tc-failed .tc-progress-bar {
  background: var(--color-error-700, #f5222d);
}
.tc-progress-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--color-text-primary);
  mix-blend-mode: difference;
  color: #fff;
}
.tc-card-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.tc-error {
  margin: 0;
  font-size: 12px;
  color: var(--color-error-700, #f5222d);
}
.tc-hint {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.tc-actions {
  display: flex;
  gap: 8px;
}
</style>
