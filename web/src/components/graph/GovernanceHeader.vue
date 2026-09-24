<template>
  <div class="governance-header">
    <div class="pipeline">
      <span class="pipeline-step" :class="{ active: buildActive }">
        <Database :size="13" /> 构建{{ buildLabel }}
      </span>
      <span class="pipeline-arrow">→</span>
      <span class="pipeline-step" :class="{ warn: gateCount > 0 }">规则门禁</span>
      <span class="pipeline-arrow">→</span>
      <span class="pipeline-step" :class="{ active: pendingReview > 0 }">人工审核</span>
      <span class="pipeline-arrow">→</span>
      <span class="pipeline-step" :class="{ warn: integrityCount > 0 }">质量检查</span>
      <span class="pipeline-arrow">→</span>
      <span class="pipeline-step" :class="{ active: Boolean(lastReleaseId) }">发布</span>
    </div>
    <div class="metrics">
      <button
        class="metric"
        type="button"
        title="待人工审核的候选关系与实体"
        @click="$emit('navigate', 'review')"
      >
        <span class="metric-value" :class="{ 'metric-value--hot': pendingReview > 0 }">{{
          pendingReview
        }}</span>
        <span class="metric-label">待人工审核</span>
      </button>
      <button
        class="metric"
        type="button"
        title="门禁送审待裁决"
        @click="$emit('navigate', 'review', 'gate')"
      >
        <span class="metric-value" :class="{ 'metric-value--warn': gateCount > 0 }">{{
          gateCount
        }}</span>
        <span class="metric-label">门禁待裁决</span>
      </button>
      <button
        class="metric"
        type="button"
        title="开放冲突"
        @click="$emit('navigate', 'review', 'conflict')"
      >
        <span class="metric-value" :class="{ 'metric-value--warn': conflictCount > 0 }">{{
          conflictCount
        }}</span>
        <span class="metric-label">开放冲突</span>
      </button>
      <button
        class="metric"
        type="button"
        title="完整性轻量计数（I3/I5/I6，不含引文重验；点击进入质量页做完整审计）"
        @click="$emit('navigate', 'quality')"
      >
        <span class="metric-value" :class="{ 'metric-value--bad': integrityCount > 0 }">{{
          integrityCount
        }}</span>
        <span class="metric-label">完整性违规</span>
      </button>
      <button
        class="metric"
        type="button"
        title="死信 Chunk / 过期缓存"
        @click="$emit('navigate', 'quality')"
      >
        <span class="metric-value" :class="{ 'metric-value--warn': deadChunks > 0 }">{{
          deadChunks
        }}</span>
        <span class="metric-label">死信 Chunk</span>
      </button>
      <button
        class="metric"
        type="button"
        title="当前生产检索策略"
        @click="$emit('navigate', 'build')"
      >
        <a-tag
          :color="reviewPolicy === 'approved_only' ? 'green' : 'default'"
          size="small"
          class="metric-tag"
        >
          {{ reviewPolicy === 'approved_only' ? '生产检索：仅已批准' : '生产检索：候选可见' }}
        </a-tag>
        <span class="metric-label">{{ lastReleaseText }}</span>
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { Database } from '@lucide/vue'

const props = defineProps({
  summary: { type: Object, default: null }
})

defineEmits(['navigate'])

const pendingReview = computed(
  () =>
    Number(props.summary?.counts?.triples?.CANDIDATE ?? 0) +
    Number(props.summary?.counts?.entities?.CANDIDATE ?? 0)
)
const gateCount = computed(() => Number(props.summary?.gates?.pending?._total ?? 0))
const conflictCount = computed(() => Number(props.summary?.conflicts?.open?._total ?? 0))
const integrityCount = computed(() => {
  const integrity = props.summary?.integrity || {}
  return Object.entries(integrity)
    .filter(([key, value]) => key.startsWith('I') && Number(value) > 0)
    .reduce((total, [, value]) => total + Number(value), 0)
})
const deadChunks = computed(() => Number(props.summary?.build?.dead_chunks ?? 0))
const reviewPolicy = computed(() => props.summary?.settings?.review_policy || 'candidates_visible')
const lastReleaseId = computed(() => props.summary?.release?.active_release_id || null)
const lastReleaseText = computed(() => {
  const last = props.summary?.release?.last_release
  if (!last) return '未发布'
  return `最后发布：${last.release_id?.slice(0, 10)}…（${last.status === 'ACTIVE' ? '生效中' : last.status}）`
})
const buildActive = computed(() => {
  const status = props.summary?.build?.build_task_status
  return status === 'pending' || status === 'running'
})
const buildLabel = computed(() => {
  const build = props.summary?.build
  if (!build?.configured) return '（未配置）'
  if (buildActive.value) return ` ${build.build_task_progress ?? 0}%`
  const pending = Number(build.pending_chunks ?? 0)
  return pending > 0 ? `（${pending} 待索引）` : '完成'
})
</script>

<style scoped lang="less">
.governance-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 12px;
  border-bottom: 1px solid var(--gray-200);
  background: var(--color-trans-light);
  font-size: 12px;
}

.pipeline {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--gray-500);
  flex-wrap: wrap;
}

.pipeline-step {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--gray-50);

  &.active {
    color: var(--main-color);
    background: var(--main-color-bg-hover, rgba(22, 119, 255, 0.1));
  }

  &.warn {
    color: var(--color-warning-600, #d97706);
    background: rgba(217, 119, 6, 0.1);
  }
}

.pipeline-arrow {
  color: var(--gray-300);
}

.metrics {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.metric {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
  font: inherit;

  &:hover {
    background: var(--gray-50);
  }
}

.metric-value {
  font-size: 15px;
  font-weight: 700;
  color: var(--gray-900);
  font-variant-numeric: tabular-nums;

  &--hot {
    color: var(--main-color);
  }

  &--warn {
    color: var(--color-warning-600, #d97706);
  }

  &--bad {
    color: var(--color-error, #cf1322);
  }
}

.metric-label {
  color: var(--gray-500);
}

.metric-tag {
  margin: 0;
}
</style>
