<template>
  <a-drawer
    :open="visible"
    :width="drawerWidth"
    :body-style="{ padding: 0, display: 'flex', flexDirection: 'column', height: '100%' }"
    :header-style="{ padding: '10px 16px' }"
    title="证据原文"
    destroy-on-close
    @close="$emit('close')"
  >
    <div class="pdf-drawer-body">
      <aside class="pdf-evidence-rail">
        <button
          v-for="item in evidenceList"
          :key="item.evidence_id"
          type="button"
          class="pdf-evidence-rail-item"
          :class="{ 'is-active': item.evidence_id === evidence?.evidence_id }"
          :title="item.quote?.exact || ''"
          @click="$emit('select-evidence', item)"
        >
          <span class="rail-page">
            {{ pageNumber(item) }}
            <em v-if="item.locator?.highlight?.quote" class="rail-precise">句级</em>
          </span>
          <span class="rail-quote">{{ shortQuote(item) }}</span>
        </button>
        <div v-if="!evidenceList.length" class="pdf-evidence-rail-empty">无可切换证据</div>
      </aside>

      <div class="pdf-viewer-pane">
        <div v-if="error" class="pdf-viewer-error">
          <p>{{ error }}</p>
          <div class="pdf-viewer-error-actions">
            <a-button size="small" @click="$emit('close')">关闭</a-button>
            <a-button size="small" type="primary" @click="$emit('fallback', evidence)">
              新标签打开（仅跳页）
            </a-button>
          </div>
        </div>
        <div v-else-if="loading" class="pdf-viewer-loading">
          <a-spin :tip="loadingTip" />
          <div v-if="progress.total" class="pdf-progress-bar">
            <div class="pdf-progress-fill" :style="{ width: progressPercent + '%' }" />
          </div>
          <div v-if="progress.received" class="pdf-progress-text">{{ progressText }}</div>
        </div>
        <template v-else>
          <div class="pdf-toolbar">
            <a-button size="small" :disabled="page <= 1" @click="goPage(page - 1)">上一页</a-button>
            <span class="pdf-page-indicator">
              <a-input-number
                :value="page"
                size="small"
                :min="1"
                :max="totalPages"
                :controls="false"
                class="pdf-page-input"
                @change="onPageInputChange"
              />
              / {{ totalPages }}
            </span>
            <a-button size="small" :disabled="page >= totalPages" @click="goPage(page + 1)"
              >下一页</a-button
            >
            <span v-if="highlightNote" class="pdf-highlight-note">{{ highlightNote }}</span>
          </div>
          <div ref="scrollContainer" class="pdf-scroll">
            <div class="pdf-page-stage" :style="{ width: stageWidth + 'px' }">
              <canvas ref="canvasRef" class="pdf-canvas" />
              <div
                v-for="(box, index) in highlightBoxes"
                :key="index"
                class="pdf-highlight-box"
                :class="`pdf-highlight-box--${box.kind}`"
                :style="{
                  left: box.left + 'px',
                  top: box.top + 'px',
                  width: box.width + 'px',
                  height: box.height + 'px'
                }"
              />
            </div>
          </div>
        </template>
      </div>
    </div>
  </a-drawer>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import * as pdfjsLib from 'pdfjs-dist'
import { downloadWorkspaceKnowledgeFileWithProgress } from '@/apis/workspace_api'
import { evidenceHighlight, pdfRectToScreen, computeTextLayerHighlight } from '@/utils/pdfHighlight'

// Vite 官方 worker 方式:显式 Worker 实例交给 workerPort。
// 经实测 dev/生产下 `?url` + workerSrc 的 ESM worker 会静默失联
// (getDocument 的 promise 永不 resolve),必须用 new URL 构造。
pdfjsLib.GlobalWorkerOptions.workerPort = new Worker(
  new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url),
  { type: 'module' }
)

const props = defineProps({
  visible: { type: Boolean, default: false },
  evidence: { type: Object, default: null },
  evidenceList: { type: Array, default: () => [] },
  kbId: { type: String, default: null },
  fileId: { type: String, default: null }
})

defineEmits(['close', 'select-evidence', 'fallback'])

const drawerWidth = 'min(92vw, 980px)'
const loading = ref(false)
const error = ref('')
const page = ref(1)
const totalPages = ref(1)
const stageWidth = ref(720)
const scrollContainer = ref(null)
const canvasRef = ref(null)
// 下载进度（大文件经慢通道时给用户确定性反馈）；total=0 表示长度未知
const progress = ref({ received: 0, total: 0 })
// 当前页高亮盒：kind ∈ block（仅块级 bbox）| context（块级虚线上下文）| precise（句子级实心）
const highlightBoxes = ref([])
// 句子级定位结果：none（服务端未给精化句）| exact | partial | unmatched
const preciseState = ref('none')
let downloadAbort = null
let pdfDoc = null
let currentLoadKey = ''
let renderTask = null
const textContentCache = new Map()

const highlight = computed(() => evidenceHighlight(props.evidence))
const progressPercent = computed(() => {
  const { received, total } = progress.value
  if (!total || received > total) return received ? 100 : 0
  return Math.min(100, Math.round((received / total) * 100))
})
const progressText = computed(() => {
  const { received, total } = progress.value
  const mb = (bytes) => `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return total
    ? `${mb(received)} / ${mb(total)}（${progressPercent.value}%）`
    : `已下载 ${mb(received)}`
})
const loadingTip = computed(() =>
  progress.value.received ? '正在接收 PDF 原文…' : '正在请求 PDF 原文…'
)
const highlightNote = computed(() => {
  if (!highlight.value) return ''
  if (highlight.value.page !== page.value) return `引用原文在第 ${highlight.value.page} 页`
  const notes = {
    exact: '实心高亮为回应问题的句子，虚线框为所属段落',
    outside: '实心高亮为回应问题的句子（在段落框外续接，如图注或跨栏续行）',
    partial: '实心高亮为回应句起始位置，虚线框为所属段落',
    unmatched: '本页含引用段落（黄色高亮）；句子级定位未在文本层匹配到',
    none: '本页含引用原文（黄色高亮）'
  }
  return notes[preciseState.value] || notes.none
})

const renderMeta = { view: null, scale: 1, rotation: 0 }

const pageNumber = (item) => {
  const fragment = item?.locator?.fragments?.[0]
  return fragment ? `第 ${fragment.page_number ?? fragment.page_index + 1} 页` : '证据'
}
const shortQuote = (item) => {
  const quote = item?.locator?.highlight?.quote || item?.quote?.exact || ''
  return quote.length > 46 ? `${quote.slice(0, 46)}…` : quote || '（无原文摘录）'
}

const getTextContent = async (pdfPage, target) => {
  if (!textContentCache.has(target)) {
    textContentCache.set(target, await pdfPage.getTextContent())
  }
  return textContentCache.get(target)
}

const fragmentPage = (fragment) => Number(fragment.page_number) || Number(fragment.page_index) + 1

/**
 * 块级 bbox 始终可画；有服务端精化句时再在文本层里定位那一句。
 * 精化命中 → 句子实心 + 段落虚线；未命中 → 退回块级实心（与旧行为一致）。
 */
const refreshHighlightBoxes = async (pdfPage, viewport, target) => {
  const fragments = props.evidence?.locator?.fragments || []
  const blockRects = fragments
    .filter((fragment) => fragmentPage(fragment) === target)
    .map((fragment) => pdfRectToScreen(fragment.bbox, { ...renderMeta, origin: fragment.origin }))
    .filter(Boolean)
  const refinedQuote = props.evidence?.locator?.highlight?.quote
  let precise = null
  if (refinedQuote && blockRects.length) {
    try {
      const textContent = await getTextContent(pdfPage, target)
      precise = computeTextLayerHighlight({
        textContent,
        viewport,
        query: refinedQuote,
        constraintRect: blockRects[0]
      })
    } catch (textError) {
      console.warn('Failed to read PDF text layer:', textError)
    }
  }
  // 翻页/切证据竞态：文本层异步返回时页面已切换则丢弃本次结果
  if (page.value !== target) return
  if (precise) {
    highlightBoxes.value = [
      ...blockRects.map((rect) => ({ ...rect, kind: 'context' })),
      ...precise.rects.map((rect) => ({ ...rect, kind: 'precise' }))
    ]
    preciseState.value = precise.partial
      ? 'partial'
      : precise.withinConstraint
        ? 'exact'
        : 'outside'
  } else {
    highlightBoxes.value = blockRects.map((rect) => ({ ...rect, kind: 'block' }))
    preciseState.value = refinedQuote ? 'unmatched' : 'none'
  }
  scrollHighlightIntoView()
}

const renderPage = async (pageNumber) => {
  if (!pdfDoc) return
  const target = Math.min(Math.max(1, pageNumber), totalPages.value)
  const pdfPage = await pdfDoc.getPage(target)
  const containerWidth = (scrollContainer.value?.clientWidth || 720) - 32
  const base = pdfPage.getViewport({ scale: 1 })
  const scale = Math.min(Math.max(containerWidth / base.width, 0.4), 2.5)
  const viewport = pdfPage.getViewport({ scale })
  renderMeta.view = {
    x0: base.viewBox[0],
    y0: base.viewBox[1],
    x1: base.viewBox[2],
    y1: base.viewBox[3]
  }
  renderMeta.scale = scale
  renderMeta.rotation = viewport.rotation
  stageWidth.value = viewport.width
  page.value = target
  highlightBoxes.value = []
  const outputScale = Math.min(window.devicePixelRatio || 1, 2)
  canvasRef.value.width = Math.floor(viewport.width * outputScale)
  canvasRef.value.height = Math.floor(viewport.height * outputScale)
  // 竞态防护：快速翻页/切换证据时取消上一帧渲染，避免同一 canvas 并发 render 抛错
  if (renderTask) {
    try {
      renderTask.cancel()
    } catch (cancelError) {
      console.warn('Failed to cancel previous render:', cancelError)
    }
  }
  const task = pdfPage.render({
    canvasContext: canvasRef.value.getContext('2d'),
    viewport,
    transform: outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : undefined
  })
  renderTask = task
  try {
    await task.promise
  } finally {
    if (renderTask === task) renderTask = null
  }
  await refreshHighlightBoxes(pdfPage, viewport, target)
}

const goPage = async (target) => {
  try {
    await renderPage(target)
  } catch (renderError) {
    console.warn('Failed to render page:', renderError)
  }
}

// 手输页码提交（回车/失焦）后才渲染；与 v-model 双向绑定不同，这里以渲染结果为准
const onPageInputChange = (value) => {
  const target = Number(value)
  if (Number.isFinite(target) && target !== page.value) void goPage(target)
  // 非法输入（NaN/越界）保持当前页状态不变
}

const loadDocument = async () => {
  if (!props.visible || !props.kbId || !props.fileId) return
  const loadKey = `${props.kbId}:${props.fileId}`
  if (currentLoadKey === loadKey && pdfDoc) {
    await goPage(highlight.value?.page || 1)
    return
  }
  // 新文档：中断上一次仍在进行的下载（大文件慢通道下关闭/切换常见）
  downloadAbort?.abort()
  downloadAbort = new AbortController()
  const abortSignal = downloadAbort.signal
  loading.value = true
  error.value = ''
  progress.value = { received: 0, total: 0 }
  currentLoadKey = loadKey
  try {
    const buffered = await downloadWorkspaceKnowledgeFileWithProgress(props.kbId, props.fileId, {
      signal: abortSignal,
      onProgress: (received, total) => {
        if (currentLoadKey === loadKey) progress.value = { received, total }
      }
    })
    pdfDoc = await pdfjsLib.getDocument({ data: buffered }).promise
    textContentCache.clear()
    totalPages.value = pdfDoc.numPages
    loading.value = false
    await renderPage(highlight.value?.page || 1)
  } catch (loadError) {
    if (abortSignal.aborted) {
      // 用户主动关闭/切换触发的中断不是错误；保持 loading 复位即可
      loading.value = false
      return
    }
    loading.value = false
    error.value = loadError?.message || 'PDF 加载失败，请稍后重试'
  }
}

// 让句子级高亮（无则块级）进入视野：整句能装下就居中，装不下就对齐句子起点
const scrollHighlightIntoView = () => {
  requestAnimationFrame(() => {
    const container = scrollContainer.value
    if (!container) return
    const boxes = highlightBoxes.value
    const precise = boxes.filter((box) => box.kind === 'precise')
    const focus = precise.length ? precise : boxes.slice(0, 1)
    if (!focus.length) {
      container.scrollTo({ top: 0, behavior: 'smooth' })
      return
    }
    const unionTop = Math.min(...focus.map((box) => box.top))
    const unionBottom = Math.max(...focus.map((box) => box.top + box.height))
    const viewHeight = container.clientHeight
    const fits = unionBottom - unionTop <= viewHeight - 48
    const top = fits
      ? unionTop - (viewHeight - (unionBottom - unionTop)) / 2
      : focus[0].top - Math.max(80, viewHeight * 0.3)
    container.scrollTo({ top: Math.max(0, top), behavior: 'smooth' })
  })
}

watch(
  () => [props.visible, props.fileId],
  () => {
    if (props.visible) void loadDocument()
  },
  { immediate: true }
)

watch(
  () => props.evidence?.evidence_id,
  () => {
    if (props.visible && pdfDoc && highlight.value) void goPage(highlight.value.page)
  }
)

watch(
  () => props.visible,
  (visible) => {
    if (!visible) {
      downloadAbort?.abort()
      downloadAbort = null
      if (pdfDoc) {
        if (renderTask) {
          try {
            renderTask.cancel()
          } catch {
            /* 取消失败无需处理，随文档销毁 */
          }
          renderTask = null
        }
        pdfDoc.destroy()
        pdfDoc = null
        currentLoadKey = ''
        textContentCache.clear()
        highlightBoxes.value = []
        preciseState.value = 'none'
      }
    }
  }
)
</script>

<style scoped lang="less">
.pdf-drawer-body {
  display: flex;
  height: 100%;
  min-height: 0;
}

.pdf-evidence-rail {
  width: 220px;
  flex-shrink: 0;
  border-right: 1px solid var(--border-color, rgba(255, 255, 255, 0.08));
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pdf-evidence-rail-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  text-align: left;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-primary, #d7dade);

  &:hover {
    background: var(--bg-secondary, rgba(255, 255, 255, 0.05));
  }

  &.is-active {
    border-color: var(--accent, #4c8bf5);
    background: var(--bg-secondary, rgba(76, 139, 245, 0.1));
  }
}

.rail-page {
  font-weight: 600;
  color: var(--accent, #4c8bf5);
}

.rail-quote {
  color: var(--text-secondary, #8a8f99);
  word-break: break-all;
}

.pdf-evidence-rail-empty {
  font-size: 12px;
  color: var(--text-secondary, #8a8f99);
  padding: 8px;
}

.pdf-viewer-pane {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.pdf-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-color, rgba(255, 255, 255, 0.08));
}

.pdf-page-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary, #8a8f99);
}

.pdf-page-input {
  width: 56px;
}

.pdf-highlight-note {
  margin-left: auto;
  font-size: 12px;
  color: var(--warning, #e0a03a);
}

.pdf-scroll {
  flex: 1;
  overflow: auto;
  background: var(--bg-secondary, #20232a);
  padding: 16px;
}

.pdf-page-stage {
  position: relative;
  margin: 0 auto;
}

.pdf-canvas {
  display: block;
  width: 100%;
  height: auto;
}

.pdf-highlight-box {
  position: absolute;
  pointer-events: none;
  border-radius: 2px;

  // 仅有块级定位：整块实心（旧行为）
  &--block {
    background: rgba(255, 213, 79, 0.42);
    border: 1px solid rgba(255, 193, 7, 0.9);
    box-shadow: 0 0 0 2px rgba(255, 193, 7, 0.55);
  }

  // 句子级命中时，所属段落退为虚线上下文框
  &--context {
    background: transparent;
    border: 1px dashed rgba(255, 193, 7, 0.75);
  }

  // 回应问题的那一句：实心逐行高亮
  &--precise {
    background: rgba(255, 213, 79, 0.55);
    border: 1px solid rgba(255, 170, 0, 0.95);
    box-shadow: 0 0 0 2px rgba(255, 170, 0, 0.45);
  }
}

.rail-precise {
  margin-left: 6px;
  padding: 0 4px;
  border-radius: 3px;
  font-size: 10px;
  font-style: normal;
  font-weight: 500;
  color: var(--warning, #e0a03a);
  background: color-mix(in srgb, var(--warning, #e0a03a) 14%, transparent);
}

.pdf-viewer-loading,
.pdf-viewer-error {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary, #8a8f99);
}

.pdf-viewer-error-actions {
  display: flex;
  gap: 10px;
}

.pdf-progress-bar {
  width: 240px;
  height: 6px;
  border-radius: 3px;
  background: var(--fill-color-light, rgba(255, 255, 255, 0.08));
  overflow: hidden;
}

.pdf-progress-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--accent, #4c8bf5);
  transition: width 0.2s ease;
}

.pdf-progress-text {
  font-size: 12px;
  color: var(--text-secondary, #8a8f99);
  font-variant-numeric: tabular-nums;
}
</style>
