<template>
  <section class="source-annotations">
    <a-button size="small" :loading="loading" @click="toggle">
      {{ opened ? '收起来源阅读' : '来源阅读' }}
    </a-button>
    <div v-if="opened" class="reading-panel">
      <a-alert v-if="error" type="warning" :message="error" show-icon />
      <template v-if="annotations">
        <p class="hint">模型匹配仅表示原文支持关系，不代表新增事实核验。</p>
        <p v-if="annotations.hidden_count" class="hint">
          部分来源权限或文档版本已变化，已隐藏对应标注。
        </p>
        <p v-if="!citations.length" class="hint">未找到当前可展示的匹配来源。</p>
        <article v-for="(item, index) in citations" :key="index">
          <p class="answer-excerpt">{{ item.answer_excerpt }}</p>
          <details>
            <summary>来源 {{ index + 1 }} · {{ sourceName(item) }}</summary>
            <blockquote>{{ item.quote }}</blockquote>
            <button
              v-if="evidence(item)?.locator?.fragments?.length"
              type="button"
              @click="openSource(item)"
            >
              查看原文
            </button>
          </details>
        </article>
      </template>
      <template v-else-if="!loading">
        <p class="hint">按需使用独立模型匹配现有可信证据，会产生额外模型用量。</p>
        <a-button v-if="available" size="small" :loading="annotating" @click="annotate">{{
          error ? '重试标注' : '标注来源'
        }}</a-button>
        <p v-else class="hint">本回答没有当前可用的可信证据。</p>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { agentApi } from '@/apis/agent_api'

const props = defineProps({
  runId: { type: String, required: true },
  answer: { type: String, required: true }
})
const emit = defineEmits(['open-source'])
const opened = ref(false),
  loading = ref(false),
  annotating = ref(false)
const annotations = ref(null),
  available = ref(false),
  error = ref('')
let generation = 0
const citations = computed(() => annotations.value?.citations || [])
const source = (item) => annotations.value?.sources?.find((s) => s.id === item.source_id)
const evidence = (item) => source(item)?.evidence
const sourceName = (item) =>
  evidence(item)?.source?.filename || evidence(item)?.source?.file_id || '原文'

async function toggle() {
  opened.value = !opened.value
  if (!opened.value) return
  const ticket = generation
  loading.value = true
  error.value = ''
  try {
    const data = await agentApi.getSourceAnnotations(props.runId)
    if (ticket !== generation) return
    annotations.value = data.annotations
    available.value = data.available
  } catch (cause) {
    if (ticket === generation) error.value = cause.message || '读取来源失败'
  } finally {
    if (ticket === generation) loading.value = false
  }
}
async function annotate() {
  const ticket = generation
  annotating.value = true
  error.value = ''
  try {
    const data = await agentApi.annotateSources(props.runId)
    if (ticket === generation) annotations.value = data
  } catch (cause) {
    if (ticket === generation) error.value = cause.message || '标注失败，可重试'
  } finally {
    if (ticket === generation) annotating.value = false
  }
}
async function openSource(item) {
  const ticket = generation
  try {
    // Revalidate before emitting a locator cached in the reading panel.
    const data = await agentApi.getSourceAnnotations(props.runId)
    if (ticket !== generation) return
    annotations.value = data.annotations
    const current = evidence(item)
    if (current)
      emit('open-source', { ...current, source: { ...current.source, run_id: props.runId } })
    else error.value = '该来源已不可用，请刷新阅读面板'
  } catch (cause) {
    if (ticket === generation) error.value = cause.message || '打开来源失败'
  }
}
watch(
  () => [props.runId, props.answer],
  () => {
    generation++
    opened.value = false
    annotations.value = null
    error.value = ''
    available.value = loading.value = annotating.value = false
  }
)
</script>

<style scoped lang="less">
.source-annotations {
  margin: 12px 0;
}
.reading-panel {
  margin-top: 8px;
  padding: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
}
.hint {
  color: var(--gray-600);
  font-size: 12px;
}
article {
  margin: 12px 0;
  padding-top: 8px;
  border-top: 1px solid var(--gray-150);
}
.answer-excerpt,
blockquote {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.answer-excerpt {
  color: var(--gray-600);
  font-size: 13px;
}
summary,
button {
  cursor: pointer;
}
button {
  background: transparent;
  border: 0;
  color: var(--main-color);
}
</style>
