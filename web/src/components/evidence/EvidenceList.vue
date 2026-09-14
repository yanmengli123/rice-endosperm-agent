<template>
  <div class="evidence-list">
    <div v-if="summaryLine" class="evidence-summary">{{ summaryLine }}</div>
    <div v-if="issueLine" class="evidence-warning">{{ issueLine }}</div>
    <div v-if="evidence.length === 0" class="evidence-empty">{{ emptyLine }}</div>
    <div class="evidence-items">
      <div
        v-for="item in evidence"
        :key="item.evidence_id"
        class="evidence-item"
        :class="`evidence-item--${statusClass(item)}`"
      >
        <div class="evidence-item-head">
          <span class="evidence-page">第 {{ pageNumber(item) }} 页</span>
          <span class="evidence-quality">{{ item.locator?.quality || 'UNKNOWN' }}</span>
          <span class="evidence-status" :class="`is-${statusClass(item)}`">{{
            statusLabel(item)
          }}</span>
          <button
            v-if="canOpenSource(item)"
            type="button"
            class="evidence-open"
            title="打开 PDF 原文页"
            @click="$emit('open-source', item)"
          >
            <ExternalLink :size="13" aria-hidden="true" />
            <span>原文</span>
          </button>
        </div>
        <p class="evidence-quote">{{ quoteText(item) }}</p>
        <div v-if="metaLine(item)" class="evidence-meta">{{ metaLine(item) }}</div>
      </div>
    </div>
    <div v-if="retrievalCandidates.length" class="evidence-retrieval">
      <div class="evidence-retrieval-head">
        <span class="evidence-retrieval-title">检索候选</span>
        <span class="evidence-retrieval-count">{{ retrievalCandidates.length }} 条未绑定定位</span>
      </div>
      <p class="evidence-retrieval-note">
        以下内容已检索到，但未形成可靠页码绑定；候选页码不发布给回答。
      </p>
      <div class="evidence-items">
        <div
          v-for="item in retrievalCandidates"
          :key="`rc-${item.evidence_id}`"
          class="evidence-item"
        >
          <div class="evidence-item-head">
            <span class="evidence-quality">{{ item.locator?.quality || 'UNKNOWN' }}</span>
            <span class="evidence-status is-degraded">检索候选</span>
          </div>
          <p class="evidence-quote">{{ quoteText(item) }}</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { ExternalLink } from '@lucide/vue'

defineEmits(['open-source'])

const props = defineProps({
  evidence: { type: Array, default: () => [] },
  summary: { type: Object, default: null },
  issues: { type: Array, default: () => [] },
  evidenceRole: { type: String, default: 'RETRIEVAL_CANDIDATE' },
  claimBindingStatus: { type: String, default: 'NOT_AVAILABLE' },
  projectionStatus: { type: String, default: null },
  retrievalCandidates: { type: Array, default: () => [] },
  locatorStatusReason: { type: String, default: null }
})

const summaryLine = computed(() => {
  const s = props.summary
  if (!s) return ''
  const noun = props.evidenceRole === 'ANSWER_CITATION' ? '回答引用证据' : '检索证据候选'
  const parts = [`共 ${s.total || 0} 条${noun}`]
  if (s.retrieval_candidate_count != null && s.retrieval_candidate_count !== s.total)
    parts.push(`${s.retrieval_candidate_count} 条检索候选`)
  if (s.verified) parts.push(`${s.verified} 条可精确定位`)
  if (s.degraded) parts.push(`${s.degraded} 条仅供核验`)
  if (s.rejected) parts.push(`${s.rejected} 条拒绝`)
  return parts.join(' · ')
})

const emptyLine = computed(() => {
  if (props.retrievalCandidates.length)
    return `检索到 ${props.retrievalCandidates.length} 条相关候选，但未形成可靠页码绑定；候选页码未展示。`
  if (props.projectionStatus === 'LOCATOR_FAILED') {
    if (props.locatorStatusReason === 'VISION_PROVIDER_UNAVAILABLE')
      return '图片定位需要视觉模型（当前未配置），且确定性指纹未命中；未输出任何页码。'
    return '已执行原文定位，但没有形成唯一且可验证的物理页码；候选页码未展示。'
  }
  if (props.projectionStatus === 'EVIDENCE_UNAVAILABLE')
    return '本轮要求了文献证据，但没有产生可出境的已验证证据。'
  return '本轮检索未产生可出境的定位证据。'
})

const issueLine = computed(() => {
  const issueCount = props.issues.length
  if (!issueCount) return ''
  const limited = props.issues.some((item) => String(item?.code || '').endsWith('LIMIT_REACHED'))
  return limited
    ? `证据投影已按安全上限截断，并记录 ${issueCount} 项完整性提示。`
    : `证据投影记录了 ${issueCount} 项完整性提示，请结合原文核验。`
})

const statusClass = (item) => {
  const status = item.verification?.status || 'FAILED'
  return status === 'OK' ? 'ok' : status === 'DEGRADED' ? 'degraded' : 'failed'
}

const statusLabel = (item) => {
  const map = { OK: '定位已验证', DEGRADED: '仅供核验', FAILED: '未通过' }
  return map[item.verification?.status] || '未知'
}

const pageNumber = (item) => item.locator?.fragments?.[0]?.page_number ?? item.locator?.page ?? '?'

const quoteText = (item) => {
  // 有句子级精化时优先展示回应问题的那一句，而不是段落开头
  const quote = item.locator?.highlight?.quote || item.quote?.exact || ''
  return quote.length > 160 ? `${quote.slice(0, 160)}…` : quote || '（无可引用原文）'
}

const canOpenSource = (item) =>
  Boolean(
    item.source?.kb_id &&
    item.source?.file_id &&
    item.locator?.locatable &&
    item.locator?.fragments?.length
  )

const metaLine = (item) => {
  const parts = [
    props.evidenceRole === 'ANSWER_CITATION' || item.evidence_role === 'ANSWER_CITATION'
      ? '已绑定本轮回答'
      : '检索候选，尚未绑定回答 Claim'
  ]
  if (item.locator?.highlight?.quote) parts.push('已定位回应句')
  if (item.source?.parse_revision_id) parts.push('解析版本已绑定')
  if (item.quote?.prefix !== undefined && item.quote?.start_char !== null)
    parts.push('文本位置已对齐')
  return parts.join(' · ')
}
</script>

<style scoped lang="less">
.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.evidence-summary {
  font-size: 12px;
  color: var(--text-secondary, #8a8f99);
  padding: 2px 4px;
}

.evidence-warning,
.evidence-empty {
  padding: 6px 8px;
  border-radius: 5px;
  font-size: 12px;
  line-height: 1.5;
}

.evidence-warning {
  color: var(--warning, #e0a03a);
  background: color-mix(in srgb, var(--warning, #e0a03a) 10%, transparent);
}

.evidence-empty {
  color: var(--text-secondary, #8a8f99);
  background: var(--fill-color-light, rgba(255, 255, 255, 0.03));
}

.evidence-items {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.evidence-item {
  padding: 8px 10px;
  border: 1px solid var(--border-color, rgba(255, 255, 255, 0.08));
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.evidence-item--degraded {
  border-color: var(--warning, #e0a03a);
}

.evidence-item--failed {
  border-color: var(--danger, #e5556a);
}

.evidence-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}

.evidence-page {
  color: var(--text-primary, #d7dade);
  font-weight: 600;
}

.evidence-quality {
  color: var(--text-secondary, #8a8f99);
}

.evidence-status {
  &.is-ok {
    color: var(--success, #34c77b);
  }

  &.is-degraded {
    color: var(--warning, #e0a03a);
  }

  &.is-failed {
    color: var(--danger, #e5556a);
  }
}

.evidence-open {
  margin-left: auto;
  padding: 2px 5px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary, #8a8f99);
  font: inherit;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  cursor: pointer;

  &:hover {
    color: var(--text-primary, #d7dade);
    background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  }
}

.evidence-retrieval {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px dashed var(--border-color, rgba(255, 255, 255, 0.12));
}

.evidence-retrieval-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}

.evidence-retrieval-title {
  color: var(--text-secondary, #8a8f99);
  font-weight: 600;
}

.evidence-retrieval-count {
  color: var(--warning, #e0a03a);
}

.evidence-retrieval-note {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-secondary, #6f747e);
}

.evidence-quote {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-primary, #c4c8cf);
}

.evidence-meta {
  font-size: 11px;
  color: var(--text-secondary, #6f747e);
}
</style>
