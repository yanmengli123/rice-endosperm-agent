<template>
  <div
    class="trace-span"
    :class="{ 'trace-span--failed': span.status === 'FAILED' }"
    :style="{ '--trace-depth': depth }"
  >
    <button
      type="button"
      class="trace-span-row"
      :class="{ 'is-expandable': hasDetail }"
      @click="expanded = !expanded"
    >
      <span class="trace-span-status">
        <Loader2 v-if="span.status === 'RUNNING'" :size="14" class="trace-icon-running" />
        <CheckCircle2
          v-else-if="span.status === 'COMPLETED'"
          :size="14"
          class="trace-icon-completed"
        />
        <XCircle v-else-if="span.status === 'FAILED'" :size="14" class="trace-icon-failed" />
        <MinusCircle v-else :size="14" class="trace-icon-interrupted" />
      </span>
      <span class="trace-span-title">{{ span.title || span.category }}</span>
      <span v-if="span.retry_count" class="trace-span-badge">重试{{ span.retry_count }}</span>
      <span v-if="durationText" class="trace-span-duration">{{ durationText }}</span>
      <ChevronDown
        v-if="hasDetail"
        :size="13"
        class="trace-span-chevron"
        :class="{ 'is-expanded': expanded }"
      />
    </button>
    <div v-if="expanded && hasDetail" class="trace-span-detail">
      <div v-if="span.summary" class="trace-span-detail-line">{{ span.summary }}</div>
      <div v-if="span.error_type" class="trace-span-detail-line trace-span-error">
        错误：{{ span.error_type }}
      </div>
      <div v-for="(value, key) in detailAttributes" :key="key" class="trace-span-detail-line">
        <span class="trace-span-attr-key">{{ key }}</span>
        <span class="trace-span-attr-value">{{ formatAttributeValue(value) }}</span>
      </div>
    </div>
    <div v-if="span.children?.length" class="trace-span-children">
      <TraceSpanNode
        v-for="child in span.children"
        :key="child.span_id"
        :span="child"
        :depth="depth + 1"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { CheckCircle2, ChevronDown, Loader2, MinusCircle, XCircle } from '@lucide/vue'

const props = defineProps({
  span: { type: Object, required: true },
  depth: { type: Number, default: 0 }
})

const expanded = ref(false)

const durationText = computed(() => {
  const value = Number(props.span.duration_ms)
  if (!props.span.duration_ms && props.span.duration_ms !== 0) return ''
  if (Number.isNaN(value)) return ''
  if (value < 1000) return `${Math.round(value)}ms`
  if (value < 60000) return `${(value / 1000).toFixed(1)}s`
  return `${Math.floor(value / 60000)}m${Math.round((value % 60000) / 1000)}s`
})

// 详情只展示有信息量的属性；摘要计数类已进 summary 行
const DETAIL_ATTR_KEYS = [
  'tool',
  'mcp_server',
  'mcp_tool',
  'args_digest',
  'model_spec',
  'input_tokens',
  'output_tokens',
  'total_tokens',
  'skill',
  'prompt_skills',
  'claim_count',
  'evidence_count',
  'wiki_navigation_hit_count',
  'intent',
  'error_code',
  'error_type',
  'completeness_status',
  'knowledge_scope_version'
]

const detailAttributes = computed(() => {
  const source = props.span.attributes || {}
  const result = {}
  DETAIL_ATTR_KEYS.forEach((key) => {
    const value = source[key]
    if (value === null || value === undefined || value === '') return
    result[key] = value
  })
  return result
})

const hasDetail = computed(
  () =>
    Boolean(props.span.summary || props.span.error_type) ||
    Object.keys(detailAttributes.value).length > 0
)

const formatAttributeValue = (value) => {
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}
</script>

<style scoped lang="less">
.trace-span {
  display: flex;
  flex-direction: column;
}

.trace-span-row {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 3px 4px 3px calc(4px + var(--trace-depth, 0) * 12px);
  border: none;
  background: transparent;
  text-align: left;
  cursor: default;
  font-size: 12px;
  color: var(--text-primary, #d7dade);
  border-radius: 4px;

  &.is-expandable {
    cursor: pointer;

    &:hover {
      background: var(--bg-secondary, rgba(255, 255, 255, 0.04));
    }
  }
}

.trace-span-status {
  display: inline-flex;
  flex-shrink: 0;
}

.trace-icon-running {
  color: var(--accent, #4c8bf5);
  animation: trace-spin 1.2s linear infinite;
}

.trace-icon-completed {
  color: var(--success, #34c77b);
}

.trace-icon-failed {
  color: var(--danger, #e5556a);
}

.trace-icon-interrupted {
  color: var(--warning, #e0a03a);
}

.trace-span-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-span--failed .trace-span-title {
  color: var(--danger, #e5556a);
}

.trace-span-badge {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--warning, #e0a03a);
}

.trace-span-duration {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-secondary, #8a8f99);
  font-variant-numeric: tabular-nums;
}

.trace-span-chevron {
  flex-shrink: 0;
  color: var(--text-secondary, #8a8f99);
  transition: transform 0.15s ease;

  &.is-expanded {
    transform: rotate(180deg);
  }
}

.trace-span-detail {
  margin: 0 4px 4px calc(24px + var(--trace-depth, 0) * 12px);
  padding: 6px 8px;
  background: var(--bg-secondary, rgba(255, 255, 255, 0.03));
  border-radius: 4px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.trace-span-detail-line {
  font-size: 11px;
  color: var(--text-secondary, #8a8f99);
  line-height: 1.5;
  word-break: break-all;
}

.trace-span-error {
  color: var(--danger, #e5556a);
}

.trace-span-attr-key {
  color: var(--text-secondary, #6f747e);
  margin-right: 6px;

  &::after {
    content: ':';
  }
}

.trace-span-attr-value {
  color: var(--text-primary, #c4c8cf);
}

.trace-span-children {
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border-color, rgba(255, 255, 255, 0.08));
  margin-left: 10px;
}

@keyframes trace-spin {
  from {
    transform: rotate(0deg);
  }

  to {
    transform: rotate(360deg);
  }
}
</style>
