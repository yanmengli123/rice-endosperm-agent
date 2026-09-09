<template>
  <div class="trace-timeline">
    <div v-if="summaryLine" class="trace-timeline-summary">{{ summaryLine }}</div>
    <div class="trace-timeline-list">
      <TraceSpanNode v-for="span in timeline" :key="span.span_id" :span="span" :depth="0" />
    </div>
    <div v-if="trace?.loading" class="trace-timeline-hint">正在加载执行轨迹…</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { buildTraceTimeline } from '@/utils/traceProjection'
import TraceSpanNode from './TraceSpanNode.vue'

const props = defineProps({
  trace: { type: Object, default: null }
})

const timeline = computed(() => buildTraceTimeline(props.trace || {}))

const summaryLine = computed(() => {
  const summary = props.trace?.summary
  if (!summary || summary.status === null) return ''
  const parts = []
  const statusLabel = {
    running: '进行中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '已中断'
  }[summary.status]
  if (statusLabel) parts.push(statusLabel)
  if (summary.duration_ms != null) parts.push(formatDuration(summary.duration_ms))
  if (summary.ttft_ms != null) parts.push(`首字 ${formatDuration(summary.ttft_ms)}`)
  if (summary.total_tokens) parts.push(`${summary.total_tokens.toLocaleString()} tokens`)
  const callCounts = [
    ['模型', summary.model_calls],
    ['工具', summary.tool_calls],
    ['MCP', summary.mcp_calls],
    ['知识', summary.knowledge_calls],
    ['子智能体', summary.subagent_calls]
  ].filter(([, count]) => count > 0)
  if (callCounts.length) {
    parts.push(callCounts.map(([label, count]) => `${label}×${count}`).join(' '))
  }
  return parts.join(' · ')
})

const formatDuration = (ms) => {
  const value = Number(ms) || 0
  if (value < 1000) return `${Math.round(value)}ms`
  if (value < 60000) return `${(value / 1000).toFixed(1)}s`
  return `${Math.floor(value / 60000)}m${Math.round((value % 60000) / 1000)}s`
}

defineExpose({ formatDuration })
</script>

<style scoped lang="less">
.trace-timeline {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.trace-timeline-summary {
  font-size: 12px;
  color: var(--text-secondary, #8a8f99);
  padding: 2px 4px;
}

.trace-timeline-list {
  display: flex;
  flex-direction: column;
}

.trace-timeline-hint {
  font-size: 12px;
  color: var(--text-secondary, #8a8f99);
  padding: 4px;
}
</style>
