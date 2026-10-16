<template>
  <section class="pdf-workspace" aria-label="原文与保版式译文 PDF">
    <div class="pdf-mode-tools">
      <button
        v-for="item in modes"
        :key="item.key"
        class="btn"
        :aria-pressed="mode === item.key"
        :disabled="item.key !== 'original' && !translatedUrl"
        @click="mode = item.key"
      >
        {{ item.label }}
      </button>
      <span v-if="bundle?.status" role="status"
        >{{ statusLabel }} · {{ Math.round((bundle.progress || 0) * 100) }}%</span
      >
      <button
        v-if="bundle?.cancelable"
        class="btn"
        :disabled="cancelling || bundle.cancel_requested"
        @click="cancel"
      >
        {{ bundle.cancel_requested ? '取消处理中' : '取消翻译' }}
      </button>
      <button v-if="bundle?.outputs?.pdf_zh" class="btn" @click="download('pdf_zh')">
        下载中文 PDF
      </button>
      <button v-if="bundle?.outputs?.pdf_dual" class="btn" @click="download('pdf_dual')">
        下载双语 PDF
      </button>
      <button v-if="bundle?.outputs?.pdf_candidate" class="btn" @click="download('pdf_candidate')">
        下载候选译文（未通过 QA）
      </button>
      <button v-if="bundle?.outputs?.qa" class="btn" @click="showQa = !showQa">质量报告</button>
      <button class="btn" @click="downloadSource">翻译引擎源码（AGPL）</button>
    </div>
    <p class="pdf-derived-notice">
      保版式译文基于原始
      PDF，未应用阅读正文的人工修订。译文为阅读辅助，事实引用请回原文核验；双语下载按原文页、译文页交替排列。
    </p>
    <p v-if="notice || bundle?.reason" role="status">{{ notice || bundle.reason }}</p>
    <div v-if="showQa && qa" class="qa-report">
      <p>
        自动质量检查：{{ qa.status === 'passed' ? '通过' : '未通过' }} · {{ qa.error_count }} 个错误
        · {{ qa.warning_count }} 个提示
      </p>
      <ul v-if="qa.issues?.length">
        <li v-for="(issue, index) in qa.issues" :key="index">
          {{ issue.page ? `第 ${issue.page} 页` : '文本块检查' }} · {{ issue.message }}
        </li>
      </ul>
      <p v-else>自动检查未发现问题；学术语义仍需结合原文核验。</p>
      <p>检查范围：版式与内容完整性；新版引擎增加数字和基因符号一致性。学术语义需要人工核验。</p>
      <p v-if="qa.engine_warning_count">
        引擎另外报告 {{ qa.engine_warning_count }} 个提示，请查看完整 QA。
      </p>
      <button class="btn" @click="download('qa')">下载完整 QA</button>
    </div>
    <div class="pdf-panes" :class="{ split: mode === 'split' }">
      <div v-if="mode !== 'translated'" class="pdf-pane">
        <p v-if="mode === 'split'">原文</p>
        <LiteraturePdfReader
          :url="originalUrl"
          :source="source"
          :evidence-target="evidenceTarget"
          :initial-page="page"
          :page-number="page"
          :scale="scale"
          :fit-width="fitWidth"
          @page-changed="changePage"
          @update:scale="$emit('update:scale', $event)"
          @update:fit-width="$emit('update:fitWidth', $event)"
          @assistant-action="$emit('assistant-action', $event)"
        />
      </div>
      <div v-if="mode !== 'original' && translatedUrl" class="pdf-pane">
        <p>中文译文 · {{ bundle.quality === 'passed' ? 'QA 已通过' : '请核验质量报告' }}</p>
        <LiteraturePdfReader
          :url="translatedUrl"
          :initial-page="page"
          :page-number="page"
          :allow-evidence="false"
          surface="translation"
          :scale="scale"
          :fit-width="fitWidth"
          @page-changed="changePage"
          @update:scale="$emit('update:scale', $event)"
          @update:fit-width="$emit('update:fitWidth', $event)"
          @assistant-action="$emit('assistant-action', $event)"
        />
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { literaturePdfApi, fetchLiteratureAssetBlob } from '@/apis/literature_api'
import LiteraturePdfReader from './LiteraturePdfReader.vue'
import { useUserStore } from '@/stores/user'
import { useLiteraturePollingScope } from '@/composables/useLiteraturePollingScope'

const props = defineProps({
  rid: { type: String, required: true },
  originalUrl: { type: String, required: true },
  versionToken: { type: String, default: '' },
  source: { type: Object, default: () => ({}) },
  evidenceTarget: { type: Object, default: null },
  initialPage: { type: Number, default: 1 },
  scale: { type: Number, default: 1 },
  fitWidth: { type: Boolean, default: true }
})
const emit = defineEmits(['assistant-action', 'page-changed', 'update:scale', 'update:fitWidth'])
const modes = [
  { key: 'original', label: '原文 PDF' },
  { key: 'translated', label: '中文译文 PDF' },
  { key: 'split', label: '左右对照' }
]
const mode = ref('original'),
  bundle = ref(null),
  translatedUrl = ref(''),
  qa = ref(null),
  showQa = ref(false)
const notice = ref(''),
  cancelling = ref(false),
  page = ref(props.initialPage)
let controller = null,
  timer = null,
  generation = 0,
  disposed = false,
  translatedUri = ''
const user = useUserStore(),
  actions = new Set(),
  owner = () => JSON.stringify([user.uid, user.departmentId, user.userRole]),
  identity = () => JSON.stringify([owner(), props.rid, props.versionToken])
const pause = () => {
  ++generation
  clearTimeout(timer)
  controller?.abort()
  for (const request of actions) request.abort()
  actions.clear()
  cancelling.value = false
  release()
}
const polling = useLiteraturePollingScope(pause, () => load())
const action = () => {
  const request = new AbortController(), key = identity()
  actions.add(request)
  return {
    request,
    current: () => !disposed && polling.value && !request.signal.aborted && key === identity()
  }
}
const statusLabel = computed(() =>
  bundle.value?.cancel_requested ? '取消处理中' : bundle.value?.stage || bundle.value?.status || ''
)
const changePage = (value) => {
  if (page.value === value) return
  page.value = value
  emit('page-changed', value)
}
const release = () => {
  if (translatedUrl.value) URL.revokeObjectURL(translatedUrl.value)
  translatedUrl.value = ''
  translatedUri = ''
}
const load = async () => {
  if (disposed || !polling.value || !user.uid) return
  clearTimeout(timer)
  controller?.abort()
  controller = new AbortController()
  const request = controller,
    ticket = ++generation,
    rid = props.rid,
    key = identity()
  const current = () =>
    !disposed && polling.value && ticket === generation && key === identity() && !request.signal.aborted
  try {
    const result = await literaturePdfApi.bundle(rid, request.signal)
    if (!current()) return
    bundle.value = result
    const uri = result.ready ? result.outputs?.pdf_zh?.uri : ''
    if (uri && uri !== translatedUri) {
      const blob = await fetchLiteratureAssetBlob(uri, request.signal)
      if (!current()) return
      release()
      translatedUrl.value = URL.createObjectURL(blob)
      translatedUri = uri
    } else if (!uri) {
      release()
      mode.value = 'original'
    }
    if (result.outputs?.qa?.uri) {
      const blob = await fetchLiteratureAssetBlob(result.outputs.qa.uri, request.signal)
      const report = JSON.parse(await blob.text())
      if (!current()) return
      qa.value = report
    } else qa.value = null
    notice.value = ''
    if (['PENDING', 'RUNNING'].includes(result.status)) timer = setTimeout(load, 3000)
  } catch (error) {
    if (current()) notice.value = error.message || 'PDF 翻译状态读取失败'
  }
}
const cancel = async () => {
  if (cancelling.value || !bundle.value?.job_id || !polling.value) return
  const { request, current } = action(), rid = props.rid, jobId = bundle.value.job_id
  cancelling.value = true
  try {
    await literaturePdfApi.cancel(rid, jobId, request.signal)
    if (current()) await load()
  } catch (error) {
    if (current()) notice.value = error.message || '取消请求失败'
  } finally {
    actions.delete(request)
    if (current()) cancelling.value = false
  }
}
const saveBlob = (blob, filename) => {
  const url = URL.createObjectURL(blob),
    link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
const download = async (kind) => {
  const uri = bundle.value?.outputs?.[kind]?.uri
  if (!uri || !polling.value) return
  const { request, current } = action(), rid = props.rid
  try {
    const blob = await fetchLiteratureAssetBlob(uri, request.signal)
    if (current()) saveBlob(blob, `${rid}-${kind}.${kind === 'qa' ? 'json' : 'pdf'}`)
  } catch (error) {
    if (current()) notice.value = error.message || '下载失败'
  } finally {
    actions.delete(request)
  }
}
const downloadSource = async () => {
  const { request, current } = action()
  try {
    const response = await literaturePdfApi.source(request.signal)
    const blob = await response.blob()
    if (current()) saveBlob(blob, 'supertranslate-corresponding-source.tar.gz')
  } catch (error) {
    if (current()) notice.value = error.message || '引擎源码暂不可用'
  } finally {
    actions.delete(request)
  }
}
watch(identity, () => {
  pause()
  bundle.value = null
  qa.value = null
  notice.value = ''
  mode.value = 'original'
  load()
}, { flush: 'sync' })
watch(
  () => props.evidenceTarget,
  () => {
    mode.value = 'original'
  }
)
onMounted(load)
onBeforeUnmount(() => {
  disposed = true
  pause()
})
</script>

<style scoped lang="less">
.pdf-mode-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.pdf-derived-notice {
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.pdf-panes {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
}
.pdf-panes.split {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.pdf-pane {
  min-width: 0;
}
.qa-report {
  padding: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  margin-bottom: 12px;
}
@media (max-width: 760px) {
  .pdf-panes.split {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
