<template>
  <section class="source-quote-card">
    <div class="source-quote-actions">
      <strong>原文 · 第 {{ sourceQuote.page_number }} 页</strong>
      <button type="button" @click="copyQuote">{{ copyState || '复制原文' }}</button>
      <button type="button" @click="emit('openSource', sourceQuote)">查看原文</button>
    </div>
    <blockquote>{{ sourceQuote.quote }}</blockquote>
    <p v-if="unresolved.length" class="source-quote-status" role="status">
      {{
        unresolved
          .map((target) => `${target.label}：${statusLabel(target.status, target.visual_status)}`)
          .join('；')
      }}
    </p>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import { copyTextToClipboard } from '@/utils/clipboard'

const props = defineProps({ sourceQuote: { type: Object, required: true } })
const emit = defineEmits(['openSource'])
const copyState = ref('')
const unresolved = computed(() =>
  (props.sourceQuote.targets || []).filter(
    (target) =>
      target.status !== 'RESOLVED' ||
      ['REFERENCE_ONLY', 'VERIFIED_CAPTION_ONLY'].includes(target.visual_status)
  )
)
const statusLabel = (status, visualStatus) => {
  if (status === 'RESOLVED')
    return visualStatus === 'VERIFIED_CAPTION_ONLY'
      ? '题注已核验，暂缺可展示图表'
      : '题注已匹配，图表暂未展示'
  return (
    {
      MISSING: '未找到可核验题注',
      AMBIGUOUS: '存在多个候选，未绑定',
      BUDGET_EXCEEDED: '超出本轮关联预算',
      UNAVAILABLE: '关联服务暂不可用'
    }[status] || '未完成核验'
  )
}
const copyQuote = async () => {
  copyState.value = (await copyTextToClipboard(props.sourceQuote.quote)) ? '已复制' : '复制失败'
}
</script>

<style scoped>
.source-quote-card {
  margin: 14px 0;
  padding: 12px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
}
.source-quote-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.source-quote-actions strong {
  flex: 1;
}
.source-quote-actions button {
  cursor: pointer;
  color: var(--primary-color);
  background: transparent;
  border: 0;
}
blockquote {
  margin: 12px 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.source-quote-status {
  margin: 10px 0 0;
  color: var(--text-secondary);
  font-size: 12px;
}
</style>
