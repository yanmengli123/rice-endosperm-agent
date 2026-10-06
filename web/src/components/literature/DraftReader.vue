<template>
  <section aria-label="仅解析草稿阅读器" class="draft-reader">
    <p>个人草稿：翻译只生成阅读派生物，不索引、不激活、不进入原文证据通道。</p>
    <div class="draft-actions">
      <button
        v-for="item in tabs"
        :key="item.key"
        class="btn"
        :aria-pressed="tab === item.key"
        @click="tab = item.key"
      >
        {{ item.label }}
      </button>
      <button class="btn" :disabled="busy || running" @click="translate">
        翻译草稿（使用我的翻译配置）
      </button>
      <button class="btn" :disabled="busy" @click="refresh">刷新翻译状态</button>
    </div>
    <p v-if="task">
      翻译 #{{ task.task_id }}：{{ task.status }} · {{ task.model_spec }}
      <span v-if="task.error" role="alert">{{ task.error }}</span>
    </p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <MarkdownPreview
      v-if="tab === 'original'"
      :content="document.markdown"
      :knowledge-base-id="document.kb_id"
    />
    <template v-else-if="tab === 'translation'">
      <MarkdownPreview v-if="variant" :content="variant.markdown" />
      <p v-else>译文尚未完成，请先提交翻译或刷新状态。</p>
    </template>
    <div v-else-if="tab === 'parallel'" class="parallel">
      <template v-for="row in variant?.alignment || []" :key="row.para_id">
        <MarkdownPreview :content="row.original" :knowledge-base-id="document.kb_id" />
        <MarkdownPreview :content="row.translated" />
      </template>
      <p v-if="!variant">双语对照在翻译完成后可用；按稳定段落 ID 对齐。</p>
    </div>
    <template v-else-if="tab === 'pdf'">
      <iframe v-if="pdfUrl" :src="pdfUrl" title="草稿源 PDF" />
      <button v-else class="btn" :disabled="busy" @click="loadPdf">加载源 PDF</button>
    </template>
  </section>
</template>

<script setup>
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { literatureTranslationApi as api } from '@/apis/literature_api'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { useLiteraturePollingScope } from '@/composables/useLiteraturePollingScope'

const props = defineProps({ document: { type: Object, required: true } })
const tabs = [
  { key: 'original', label: '原文' },
  { key: 'translation', label: '译文' },
  { key: 'parallel', label: '双语对照' },
  { key: 'pdf', label: 'PDF' }
]
const tab = ref('original'),
  task = ref(null),
  variant = ref(null),
  pdfUrl = ref('')
const busy = ref(false),
  notice = ref('')
const running = computed(() => ['PENDING', 'RUNNING'].includes(task.value?.status))
let disposed = false,
  generation = 0,
  controller = new AbortController(),
  timer = null
const isCurrent = (ticket) => !disposed && polling.value && ticket === generation
const refresh = async () => {
  if (disposed || !polling.value) return
  const ticket = generation
  const revision = props.document.revision_id
  busy.value = true
  window.clearTimeout(timer)
  try {
    const result = await api.draftTask(revision, controller.signal)
    if (!isCurrent(ticket)) return
    task.value = result.task
    if (task.value?.status === 'SUCCEEDED') {
      const content = await api.task(task.value.task_id, true, controller.signal)
      if (isCurrent(ticket)) variant.value = content
    }
    if (!isCurrent(ticket)) return
    if (running.value) timer = window.setTimeout(refresh, 5000)
    notice.value = ''
  } catch (error) {
    if (isCurrent(ticket) && error.name !== 'AbortError') {
      notice.value = error.message
      if ([401, 403, 404, 409, 422].includes(error.response?.status))
        task.value = variant.value = null
      else if (running.value) timer = window.setTimeout(refresh, 10000)
    }
  } finally {
    if (isCurrent(ticket)) busy.value = false
  }
}
const translate = async () => {
  if (!window.confirm('草稿翻译会向当前模型供应商发送论文内容并可能计费，允许本次翻译？')) return
  const ticket = generation
  busy.value = true
  try {
    const result = await api.translateDraft(props.document.revision_id)
    if (!isCurrent(ticket)) return
    task.value = result
    await refresh()
  } catch (error) {
    if (isCurrent(ticket)) notice.value = error.message
  } finally {
    if (isCurrent(ticket)) busy.value = false
  }
}
const loadPdf = async () => {
  if (disposed || !polling.value) return
  const ticket = generation
  busy.value = true
  try {
    const response = await api.draftPdf(props.document.revision_id, controller.signal)
    const url = URL.createObjectURL(await response.blob())
    if (!isCurrent(ticket)) {
      URL.revokeObjectURL(url)
      return
    }
    if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
    pdfUrl.value = url
  } catch (error) {
    if (isCurrent(ticket) && error.name !== 'AbortError') notice.value = error.message
  } finally {
    if (isCurrent(ticket)) busy.value = false
  }
}
const release = () => {
  generation++
  controller.abort()
  controller = new AbortController()
  window.clearTimeout(timer)
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
  pdfUrl.value = ''
  busy.value = false
}
const polling = useLiteraturePollingScope(release, () => {
  refresh()
  if (tab.value === 'pdf') loadPdf()
})
watch(
  () => props.document.revision_id,
  () => {
    release()
    task.value = variant.value = null
    tab.value = 'original'
    refresh()
  },
  { immediate: true }
)
onBeforeUnmount(() => {
  disposed = true
  release()
  controller.abort()
})
</script>

<style lang="less" scoped>
.draft-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.parallel {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
}
iframe {
  width: 100%;
  min-height: 70vh;
  border: 1px solid var(--gray-150);
}
p {
  font-size: 12px;
  color: var(--color-text-secondary);
}
</style>
