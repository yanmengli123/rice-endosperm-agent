<template>
  <div class="trace-stage-bar" aria-label="本轮执行阶段">
    <span class="trace-stage-bar__live" aria-live="polite">{{ liveAnnouncement }}</span>
    <div
      v-for="(stage, index) in stages"
      :key="stage.key"
      class="trace-stage-bar__item"
      :class="`is-${stage.status.toLowerCase()}`"
      :title="stage.detail || statusLabel(stage.status)"
    >
      <span class="trace-stage-bar__node">
        <Loader2 v-if="stage.status === 'RUNNING'" :size="12" class="is-spinning" />
        <CheckCircle2 v-else-if="stage.status === 'COMPLETED'" :size="12" />
        <XCircle v-else-if="stage.status === 'FAILED'" :size="12" />
        <TriangleAlert v-else-if="stage.status === 'DEGRADED'" :size="12" />
        <MinusCircle v-else :size="12" />
      </span>
      <span class="trace-stage-bar__label">{{ stage.label }}</span>
      <span v-if="stage.detail" class="trace-stage-bar__detail">{{ stage.detail }}</span>
      <span
        v-if="index < stages.length - 1"
        class="trace-stage-bar__connector"
        :class="{ 'is-reached': stages[index + 1].status !== 'PENDING' }"
      />
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { CheckCircle2, Loader2, MinusCircle, TriangleAlert, XCircle } from '@lucide/vue'
import { buildTraceStages } from '@/utils/traceProjection'

const props = defineProps({
  trace: { type: Object, default: null }
})

const stages = computed(() => buildTraceStages(props.trace || {}))

const STATUS_LABELS = {
  PENDING: '等待中',
  RUNNING: '进行中',
  COMPLETED: '已完成',
  FAILED: '失败',
  DEGRADED: '已降级',
  INTERRUPTED: '已中断',
  SKIPPED: '不适用'
}

const statusLabel = (status) => STATUS_LABELS[status] || status

// 屏幕阅读器播报：阶段推进时主动朗读（视觉状态对读屏器不可感知的补齐）
const liveAnnouncement = computed(() =>
  stages.value.map((stage) => `${stage.label} ${statusLabel(stage.status)}`).join('，')
)
</script>

<style scoped lang="less">
.trace-stage-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px 0;
  padding: 6px 4px;
}

.trace-stage-bar__live {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

.trace-stage-bar__item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--text-secondary, #8a8f99);

  &.is-running {
    color: var(--primary-color, #4c6ef5);
  }
  &.is-completed {
    color: var(--success-color, #2f9e44);
  }
  &.is-failed {
    color: var(--danger-color, #e03131);
  }
  &.is-degraded {
    color: var(--warning-color, #e8590c);
  }
  &.is-interrupted {
    color: var(--warning-color, #e8590c);
  }
}

.trace-stage-bar__node {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.trace-stage-bar__label {
  flex-shrink: 0;
}

.trace-stage-bar__detail {
  font-size: 11px;
  color: var(--text-tertiary, #adb5bd);
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-stage-bar__connector {
  width: 14px;
  height: 1px;
  background: var(--border-color, #dee2e6);
  margin: 0 4px;
  flex-shrink: 0;

  &.is-reached {
    background: var(--success-color, #2f9e44);
  }
}

.is-spinning {
  animation: trace-stage-spin 1s linear infinite;
}

@keyframes trace-stage-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
