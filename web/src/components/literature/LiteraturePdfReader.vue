<template>
  <section class="literature-pdf-reader" aria-label="交互式文献 PDF 阅读器">
    <div class="pdf-tools">
      <button :disabled="busy || page <= 1" @click="go(page - 1)">上一页</button>
      <label
        >页码
        <input
          v-model.number="pageInput"
          type="number"
          :min="1"
          :max="total"
          @change="go(pageInput)"
          @keydown.enter.prevent="go(pageInput)"
      /></label>
      <span>/ {{ total }}</span>
      <button :disabled="busy || page >= total" @click="go(page + 1)">下一页</button>
      <button @click="zoom(-0.25)">−</button><span>{{ Math.round(scale * 100) }}%</span
      ><button @click="zoom(0.25)">+</button>
      <button @click="$emit('update:fitWidth', true)">适合宽度</button>
      <button :disabled="busy || !rendered" :aria-pressed="cropping" @click="toggleCrop">
        {{ cropping ? '取消框选' : '框选截图提问' }}
      </button>
      <button :disabled="busy || !rendered" @click="capturePage">本页截图提问</button>
    </div>
    <p class="pdf-hint">
      选中文字可解释、翻译或提问；截图将进入本文会话草稿，由你确认发送。扫描页没有文本层时请框选截图。
    </p>
    <p v-if="busy" role="status">加载 PDF 页面…</p>
    <p v-if="error" role="alert">{{ error }} <button @click="load">重试</button></p>
    <p v-if="locatorNotice" role="status">{{ locatorNotice }}</p>
    <p v-if="renderNotice" role="status">{{ renderNotice }}</p>
    <div ref="scroll" class="pdf-scroll" @keydown.esc="cancelCrop">
      <div ref="stage" class="pdf-stage" :style="stageStyle">
        <canvas ref="canvas" aria-label="PDF 当前页" />
        <div
          ref="textRoot"
          class="textLayer"
          data-reader-surface="pdf"
          :style="{ '--scale-factor': viewportScale }"
        ></div>
        <div
          v-for="(box, index) in boxes"
          :key="index"
          class="evidence-highlight"
          :style="rectStyle(box)"
        ></div>
        <div
          v-if="cropping"
          class="crop-overlay"
          tabindex="0"
          aria-label="拖动选择截图区域，Esc 取消"
          @pointerdown="cropStart"
          @pointermove="cropMove"
          @pointerup="cropEnd"
          @pointercancel="cancelCrop"
        >
          <div v-if="crop" class="crop-box" :style="rectStyle(crop)"></div>
        </div>
      </div>
    </div>
    <ReaderSelectionToolbar
      :root="textRoot"
      :disabled="busy || cropping"
      :reset-key="`${url}:${page}:${scale}`"
      @action="textAction"
    />
  </section>
</template>
<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as pdfjs from 'pdfjs-dist'
import ReaderSelectionToolbar from './ReaderSelectionToolbar.vue'
import { cropRectangle } from '@/utils/literature_assistant'
import { resolveWorkspacePdfEvidence } from '@/apis/workspace_api'
import { pdfLocatorRequest } from '@/utils/pdfLocator'
import { physicalFragmentRects, pdfRectToScreen } from '@/utils/pdfHighlight'

const props = defineProps({
  url: { type: String, required: true },
  source: { type: Object, default: () => ({}) },
  evidenceTarget: { type: Object, default: null },
  scale: { type: Number, default: 1 },
  initialPage: { type: Number, default: 1 },
  fitWidth: { type: Boolean, default: true }
})
const emit = defineEmits(['assistant-action', 'update:scale', 'update:fitWidth', 'page-changed'])
// Each reader owns one explicitly passed PDFWorker, reused across its pages.
// Never mutate the ordinary-chat drawer's global port: destroying one document
// on a shared implicit worker can otherwise interrupt another open reader.
const scroll = ref(null),
  stage = ref(null),
  canvas = ref(null),
  textRoot = ref(null)
const page = ref(1),
  pageInput = ref(1),
  total = ref(1),
  busy = ref(false),
  rendered = ref(false)
const error = ref(''),
  locatorNotice = ref(''),
  boxes = ref([]),
  width = ref(0),
  height = ref(0),
  viewportScale = ref(1)
const renderNotice = ref('')
const cropping = ref(false),
  crop = ref(null)
const stageStyle = computed(() => ({ width: `${width.value}px`, height: `${height.value}px` }))
const rectStyle = (rect) =>
  Object.fromEntries(['left', 'top', 'width', 'height'].map((key) => [key, `${rect[key]}px`]))
let pdf = null,
  pdfWorker = null,
  nativeWorker = null,
  loadingTask = null,
  renderTask = null,
  textLayer = null,
  observer = null
let generation = 0,
  renderGeneration = 0,
  locatorGeneration = 0,
  disposed = false,
  start = null,
  dragPointer = null,
  resolved = null
let rendering = Promise.resolve()
const cancelCrop = () => {
  cropping.value = false
  crop.value = null
  start = null
  dragPointer = null
}
const toggleCrop = () => {
  cancelCrop()
  cropping.value = true
}
const fragmentPage = (fragment) => fragment.page_number ?? fragment.page_index + 1
const highlight = (viewport) => {
  const fragments = (resolved?.locator?.fragments || []).filter(
    (item) => fragmentPage(item) === page.value
  )
  const physical = fragments.flatMap((item) => physicalFragmentRects(item, viewport))
  boxes.value = physical.length
    ? physical
    : fragments
        .map((item) =>
          pdfRectToScreen(item.bbox, {
            view: {
              x0: viewport.viewBox[0],
              y0: viewport.viewBox[1],
              x1: viewport.viewBox[2],
              y1: viewport.viewBox[3]
            },
            scale: viewport.scale,
            rotation: viewport.rotation,
            origin: item.origin
          })
        )
        .filter(Boolean)
}
const render = () => {
  const ticket = ++renderGeneration
  renderTask?.cancel()
  textLayer?.cancel()
  cancelCrop()
  rendered.value = false
  // Canvas cannot be reused until PDF.js has settled the previous render task.
  rendering = rendering
    .catch(() => {})
    .then(async () => {
      if (!pdf || disposed || ticket !== renderGeneration) return
      busy.value = true
      try {
        const pdfPage = await pdf.getPage(page.value)
        if (disposed || ticket !== renderGeneration) return
        const base = pdfPage.getViewport({ scale: 1 })
        const fit = Math.max(
          0.2,
          Math.min(4, ((scroll.value?.clientWidth || 720) - 24) / base.width)
        )
        const desired = props.fitWidth ? fit : props.scale
        const boundedScale = Math.min(
          desired,
          8192 / Math.max(base.width, base.height),
          Math.sqrt(8000000 / (base.width * base.height))
        )
        if (!Number.isFinite(boundedScale) || boundedScale <= 0) throw new Error('PDF 页面尺寸无效')
        renderNotice.value =
          boundedScale < desired ? '超大页面已按安全像素预算缩小显示，原文件未改动。' : ''
        const viewport = pdfPage.getViewport({ scale: boundedScale })
        const ratio = Math.min(
          window.devicePixelRatio || 1,
          2,
          Math.sqrt(16000000 / (viewport.width * viewport.height))
        )
        width.value = viewport.width
        height.value = viewport.height
        viewportScale.value = viewport.scale
        await nextTick()
        if (disposed || ticket !== renderGeneration || !canvas.value || !textRoot.value) return
        canvas.value.width = Math.floor(viewport.width * ratio)
        canvas.value.height = Math.floor(viewport.height * ratio)
        canvas.value.style.width = `${viewport.width}px`
        canvas.value.style.height = `${viewport.height}px`
        textRoot.value.replaceChildren()
        renderTask = pdfPage.render({
          canvasContext: canvas.value.getContext('2d'),
          viewport,
          transform: [ratio, 0, 0, ratio, 0, 0]
        })
        await renderTask.promise
        if (disposed || ticket !== renderGeneration) return
        const content = await pdfPage.getTextContent()
        if (disposed || ticket !== renderGeneration) return
        textLayer = new pdfjs.TextLayer({
          textContentSource: content,
          container: textRoot.value,
          viewport
        })
        await textLayer.render()
        if (disposed || ticket !== renderGeneration) return
        highlight(viewport)
        rendered.value = true
        error.value = ''
        if (boxes.value.length) {
          const first = boxes.value[0]
          scroll.value?.scrollTo({ top: Math.max(0, first.top - 80), behavior: 'smooth' })
        }
      } catch (err) {
        if (
          ticket === renderGeneration &&
          !disposed &&
          err.name !== 'RenderingCancelledException' &&
          err.name !== 'AbortException'
        )
          error.value = err.message || 'PDF 页面渲染失败'
      } finally {
        if (ticket === renderGeneration && !disposed) busy.value = false
      }
    })
  return rendering
}
const resolveTarget = async () => {
  const ticket = ++locatorGeneration,
    target = props.evidenceTarget
  resolved = null
  boxes.value = []
  locatorNotice.value = ''
  if (!target?.evidence || !pdf) return
  const source = target.evidence.source || {}
  if (
    !props.source.parse_revision_id ||
    ['kb_id', 'file_id', 'parse_revision_id'].some((key) => source[key] !== props.source[key])
  ) {
    locatorNotice.value = '引用与当前文献版本不一致，未自动定位。请刷新本文后重试。'
    return
  }
  try {
    const result = await resolveWorkspacePdfEvidence(pdfLocatorRequest(target.evidence))
    if (disposed || ticket !== locatorGeneration) return
    if (
      ['kb_id', 'file_id', 'parse_revision_id'].some(
        (key) => result.source?.[key] !== props.source[key]
      )
    )
      throw new Error('服务端返回的引用来源与本文不一致')
    const first = result.locator?.fragments?.[0],
      number = fragmentPage(first || {})
    if (!Number.isSafeInteger(number) || number < 1 || number > total.value)
      throw new Error('该引用没有可验证的 PDF 页码')
    resolved = result
    locatorNotice.value = `已定位服务端验证的原文，第 ${number} 页`
    await go(number)
  } catch (err) {
    if (!disposed && ticket === locatorGeneration)
      locatorNotice.value = err.message || '引用位置不可用，未猜测定位'
  }
}
const load = async () => {
  const ticket = ++generation
  ++locatorGeneration
  ++renderGeneration
  renderTask?.cancel()
  textLayer?.cancel()
  const oldTask = loadingTask
  loadingTask = null
  pdf = null
  rendered.value = false
  resolved = null
  boxes.value = []
  cancelCrop()
  busy.value = true
  error.value = ''
  locatorNotice.value = ''
  try {
    await oldTask?.destroy()
  } catch {
    /* A superseded PDF task may already be destroyed. */
  }
  if (disposed || ticket !== generation) return
  try {
    // ReaderTabs provides an authenticated, owned object URL only.
    if (!props.url.startsWith('blob:')) throw new Error('PDF 必须通过鉴权阅读接口载入')
    if (!pdfWorker) {
      nativeWorker = new Worker(new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url), {
        type: 'module'
      })
      pdfWorker = new pdfjs.PDFWorker({ port: nativeWorker })
    }
    loadingTask = pdfjs.getDocument({
      url: props.url,
      worker: pdfWorker,
      isEvalSupported: false,
      canvasMaxAreaInBytes: 64 * 1024 * 1024
    })
    const document = await loadingTask.promise
    if (disposed || ticket !== generation) return
    pdf = document
    total.value = pdf.numPages
    page.value = pageInput.value = Number.isSafeInteger(props.initialPage)
      ? Math.max(1, Math.min(total.value, props.initialPage))
      : 1
    await render()
    await resolveTarget()
  } catch (err) {
    if (!disposed && ticket === generation) error.value = err.message || 'PDF 加载失败'
  } finally {
    if (!disposed && ticket === generation) busy.value = false
  }
}
const go = async (number) => {
  if (!Number.isFinite(number) || !pdf) return
  page.value = pageInput.value = Math.max(1, Math.min(total.value, Math.trunc(number)))
  emit('page-changed', page.value)
  await render()
}
const zoom = (delta) => {
  emit('update:fitWidth', false)
  emit('update:scale', Math.max(0.5, Math.min(3, props.scale + delta)))
}
const textAction = (value) => emit('assistant-action', { ...value, page_hint: page.value })
const point = (event) => {
  const rect = stage.value.getBoundingClientRect()
  return { x: event.clientX - rect.left, y: event.clientY - rect.top }
}
const cropStart = (event) => {
  if (event.button !== 0 || !rendered.value) return
  start = point(event)
  dragPointer = event.pointerId
  crop.value = null
  event.currentTarget.setPointerCapture(event.pointerId)
}
const cropMove = (event) => {
  if (start && event.pointerId === dragPointer)
    crop.value = cropRectangle(start, point(event), width.value, height.value)
}
const capture = async (rect) => {
  const ticket = renderGeneration,
    number = page.value
  if (!rendered.value || !rect) return
  try {
    const output = document.createElement('canvas')
    const ratio = canvas.value.width / width.value
    // Bound image area, including HiDPI and large pages, before native upload.
    const factor = Math.min(ratio, 1800 / Math.max(rect.width, rect.height))
    output.width = Math.max(1, Math.round(rect.width * factor))
    output.height = Math.max(1, Math.round(rect.height * factor))
    output
      .getContext('2d')
      .drawImage(
        canvas.value,
        rect.left * ratio,
        rect.top * ratio,
        rect.width * ratio,
        rect.height * ratio,
        0,
        0,
        output.width,
        output.height
      )
    const blob = await new Promise((resolve) => output.toBlob(resolve, 'image/png'))
    if (!blob || disposed || ticket !== renderGeneration) return
    emit('assistant-action', {
      action: 'screenshot',
      surface: 'pdf',
      page_hint: number,
      imageFile: new File([blob], `paper-page-${number}.png`, { type: 'image/png' })
    })
    cancelCrop()
  } catch (err) {
    error.value = err.message || '截图生成失败'
  }
}
const cropEnd = (event) => {
  if (!start || event.pointerId !== dragPointer) return
  cropMove(event)
  start = null
  dragPointer = null
  if (!crop.value) {
    locatorNotice.value = '截图区域太小，请重新框选'
    return
  }
  capture(crop.value)
}
const capturePage = () => capture({ left: 0, top: 0, width: width.value, height: height.value })
watch(() => props.url, load)
watch(
  () => [props.scale, props.fitWidth],
  () => render()
)
watch(() => props.evidenceTarget, resolveTarget)
onMounted(() => {
  load()
  observer = new ResizeObserver(() => {
    if (props.fitWidth && pdf) render()
  })
  if (scroll.value) observer.observe(scroll.value)
})
onBeforeUnmount(() => {
  disposed = true
  ++generation
  ++renderGeneration
  ++locatorGeneration
  observer?.disconnect()
  renderTask?.cancel()
  textLayer?.cancel()
  Promise.resolve(loadingTask?.destroy())
    .catch(() => {})
    .finally(() => {
      pdfWorker?.destroy()
      nativeWorker?.terminate()
      pdfjs.TextLayer.cleanup?.()
    })
})
</script>
<style scoped>
.pdf-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.pdf-tools input {
  width: 58px;
  color: inherit;
  background: var(--gray-0, white);
}
button {
  cursor: pointer;
  color: inherit;
  background: var(--gray-0, white);
  border: 1px solid var(--gray-200, #ddd);
  border-radius: 5px;
  padding: 5px 8px;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.pdf-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
}
.pdf-scroll {
  height: 66vh;
  min-height: 360px;
  overflow: auto;
  background: var(--gray-100, #eee);
  padding: 12px;
}
.pdf-stage {
  position: relative;
  margin: auto;
  background: white;
  flex-shrink: 0;
}
canvas {
  display: block;
}
.textLayer {
  position: absolute;
  text-align: initial;
  inset: 0;
  overflow: clip;
  opacity: 1;
  line-height: 1;
  text-size-adjust: none;
  forced-color-adjust: none;
  transform-origin: 0 0;
  caret-color: CanvasText;
}
.textLayer :deep(:is(span, br)) {
  color: transparent;
  position: absolute;
  white-space: pre;
  cursor: text;
  transform-origin: 0% 0%;
}
.textLayer :deep(span.markedContent) {
  top: 0;
  height: 0;
}
.textLayer :deep(::selection) {
  background: #397bdf55;
}
.evidence-highlight {
  position: absolute;
  background: #ffcc0040;
  border: 1px solid #dca100;
  pointer-events: none;
}
.crop-overlay {
  position: absolute;
  inset: 0;
  cursor: crosshair;
  touch-action: none;
  background: #0001;
}
.crop-box {
  position: absolute;
  border: 2px solid #3678e5;
  background: #3678e522;
  pointer-events: none;
}
</style>
