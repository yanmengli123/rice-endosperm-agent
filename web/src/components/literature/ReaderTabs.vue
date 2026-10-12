<template>
  <div ref="selectionRoot" class="reader-tabs" :style="{ fontSize: `${fontSize}px` }">
    <div class="sub-tabs">
      <button
        v-for="tab in visibleTabs"
        :key="tab.key"
        class="sub-tab"
        :class="{ active: current === tab.key }"
        :aria-pressed="current === tab.key"
        @click="selectTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>
    <div class="reader-tools">
      <button class="btn" :aria-expanded="manageTabs" @click="manageTabs = !manageTabs">
        阅读标签管理
      </button>
      <button
        class="btn"
        :disabled="loading || saving || editing || productEditing"
        @click="refreshReader"
      >
        刷新阅读产物
      </button>
      <label
        >阅读字号<select v-model.number="fontSize">
          <option v-for="size in [14, 16, 18, 20, 22, 24]" :key="size" :value="size">
            {{ size }}px
          </option>
        </select></label
      >
    </div>
    <p v-if="editorNotice" role="status">{{ editorNotice }}</p>
    <div class="reader-tools" aria-label="本文阅读助手">
      <span>本文助手</span>
      <button
        v-for="(label, action) in quickActions"
        :key="action"
        class="btn"
        :disabled="loading || editing || productEditing || assistantBusy"
        @click="sendAssistantAction({ action, surface: 'paper' })"
      >
        {{ label }}
      </button>
    </div>
    <p v-if="assistantNotice" role="status">{{ assistantNotice }}</p>
    <ReaderSelectionToolbar
      :root="selectionRoot"
      :disabled="loading || editing || productEditing || assistantBusy || current === 'pdf'"
      :reset-key="`${rid}:${versionToken}:${current}`"
      @action="sendAssistantAction"
    />
    <section v-if="manageTabs" class="tab-manager" aria-label="阅读标签管理">
      <label
        ><input
          v-model="autoHideMissing"
          type="checkbox"
          :disabled="editing || productEditing"
        />自动隐藏缺失产物</label
      >
      <p>拖拽排序，也可使用上移/下移按钮。原文状态入口始终保留；偏好按当前用户保存。</p>
      <div
        v-for="(tab, index) in orderedTabs"
        :key="tab.key"
        class="tab-option"
        :draggable="!editing && !productEditing"
        @dragstart="draggedTab = tab.key"
        @dragover.prevent
        @drop.prevent="reorderTab(draggedTab, tab.key)"
        @dragend="draggedTab = ''"
      >
        <label
          ><input
            type="checkbox"
            :checked="!hiddenTabs.includes(tab.key)"
            :disabled="tab.key === 'original' || editing || productEditing"
            @change="setTabVisible(tab.key, $event.target.checked)"
          />{{ tab.label }}</label
        >
        <span>{{ availableTabs[tab.key] ? '已就绪' : '尚未生成 / 不可用' }}</span>
        <button
          class="btn"
          :disabled="index === 0 || editing || productEditing"
          @click="reorderTab(tab.key, tabOrder[index - 1])"
        >
          上移
        </button>
        <button
          class="btn"
          :disabled="index === orderedTabs.length - 1 || editing || productEditing"
          @click="reorderTab(tab.key, tabOrder[index + 1])"
        >
          下移
        </button>
      </div>
    </section>

    <div v-if="loading" class="reader-loading">加载中…</div>

    <template v-else>
      <div v-if="current === 'original'" class="pane" data-reader-surface="original">
        <div v-if="!bundle?.ready" class="pane-empty">
          {{ bundle?.reason || '阅读视图尚未生成（请先触发渲染）' }}
        </div>
        <template v-else>
          <div v-if="bundle.resolved" class="resolved-badge">
            {{ bundle.frozen ? '人工修订已冻结' : '已应用人工裁决（修正版）' }}
          </div>
          <p v-if="bundle.notice" role="status">{{ bundle.notice }}</p>
          <button
            v-if="!editing && bundle.source !== 'scientific_parse'"
            class="sub-tab"
            @click="startEditing"
          >
            编辑正文
          </button>
          <template v-if="editing">
            <p>保存将冻结人工版本；自动渲染不会覆盖，旧编译及译文会失效。</p>
            <div v-for="paragraph in editDocument.paragraphs" :key="paragraph.para_id">
              <label>{{ paragraph.para_id }}</label>
              <textarea v-model="paragraph.text_en" rows="4" style="width: 100%"></textarea>
            </div>
            <button class="btn" :disabled="saving" @click="saveDocument">
              {{ saving ? '保存中…' : '保存并冻结' }}
            </button>
            <button class="btn" :disabled="saving" @click="editing = false">取消</button>
          </template>
          <LiteratureMarkdown
            v-else
            :content="displayMarkdown"
            :anchor="navigationTarget?.anchor || ''"
            :navigation-token="navigationTarget?.requestId || ''"
            :knowledge-base-id="bundle.knowledge_base_id || ''"
            @open-link="$emit('open-link', $event)"
            @open-image="openImage"
          />
        </template>
      </div>

      <div v-else-if="current === 'translation'" class="pane" data-reader-surface="translation">
        <label
          >译文类型<select v-model="variantKind" :disabled="productEditing">
            <option value="zh">中文全文</option>
            <option value="en_zh">双语全文</option>
            <option value="summary">总结</option>
          </select></label
        >
        <!-- 输出模板：切换只重排既有译文，不重新翻译（零 token） -->
        <label v-if="variantKind === 'en_zh'" class="template-picker"
          >输出模板<select v-model="templateKey" :disabled="productEditing || templateBusy">
            <option v-for="spec in templateOptions" :key="spec.key" :value="spec.key">
              {{ spec.label }}
            </option>
          </select></label
        >
        <p v-if="templateNotice" class="template-notice" role="status">{{ templateNotice }}</p>
        <ProductEditor
          :rid="rid"
          :kind="`variant_${variantKind}`"
          @editing-change="productEditing = $event"
          @saved="reloadVariant"
        />
        <div v-if="!selectedVariant?.ready" class="pane-empty">
          {{ selectedVariant?.reason || '该译文或总结尚未生成' }}
        </div>
        <template v-else>
          <LiteratureMarkdown
            :content="selectedVariant.content"
            :anchor="navigationTarget?.anchor || ''"
            :navigation-token="navigationTarget?.requestId || ''"
            @open-link="$emit('open-link', $event)"
            @open-image="openImage"
          />
        </template>
      </div>

      <div v-else-if="current === 'parallel'" class="pane" data-reader-surface="parallel">
        <p v-if="navigationTarget?.anchor && !parallelAnchor" role="status">
          当前对照原文未找到唯一章节或块锚点；未自动跳转。
        </p>
        <div v-if="!parallel?.ready && (!bundle?.ready || !variant?.ready)" class="pane-empty">
          {{ parallel?.reason || '对照阅读需要原文与译文均已生成' }}
        </div>
        <p v-if="parallel?.ready && parallel.alignment !== 'identity'" role="status">
          {{
            parallel.alignment === 'manual-positional'
              ? '当前显示人工冻结译文，按段落位置对照；编辑后不保证一一对应。'
              : '历史译文按段落位置对照，不保证一一对应。'
          }}
        </p>
        <div v-if="parallel?.ready || (bundle?.ready && variant?.ready)" class="parallel-cols">
          <template v-for="row in parallelRows" :key="row.index">
            <div class="parallel-para en">
              <LiteratureMarkdown
                :content="row.original || '（该段无原文）'"
                :anchor="parallelAnchor?.index === row.index ? parallelAnchor.anchor : ''"
                :navigation-token="navigationTarget?.requestId || ''"
                @open-link="$emit('open-link', $event)"
                @open-image="openImage"
              />
            </div>
            <div class="parallel-para zh">
              <LiteratureMarkdown
                :content="row.translated || '（该段未译）'"
                @open-link="$emit('open-link', $event)"
                @open-image="openImage"
              />
            </div>
          </template>
        </div>
      </div>

      <div v-else-if="current === 'images'" class="pane">
        <div v-if="!images.length" class="pane-empty">无图片（或渲染尚未完成）</div>
        <div v-else class="image-grid">
          <figure v-for="src in images" :key="src" class="image-item">
            <button class="image-open" @click="openImage(src)">
              <img :src="src" loading="lazy" alt="文献图片" />
            </button>
            <button class="btn" :disabled="assistantBusy" @click="explainImage(src)">
              讲解此图
            </button>
          </figure>
        </div>
      </div>

      <div v-else-if="current === 'pdf'" class="pane">
        <div v-if="!pdfUrl" class="pane-empty">源 PDF 不可用</div>
        <LiteraturePdfReader
          v-else
          :url="pdfUrl"
          :source="paper.source || {}"
          :evidence-target="evidenceTarget"
          :initial-page="pdfPage"
          v-model:scale="pdfScale"
          v-model:fit-width="pdfFitWidth"
          @page-changed="pdfPage = $event"
          @assistant-action="sendAssistantAction"
        />
      </div>
      <div v-else-if="current.startsWith('noteL')" class="pane" data-reader-surface="note">
        <ProductEditor
          :rid="rid"
          :kind="`note_${current.slice(4)}`"
          @saved="loadNote(current.slice(4))"
          @editing-change="productEditing = $event"
        />
        <div v-if="noteLoading" class="reader-loading">加载笔记中…</div>
        <template v-else-if="noteMarkdown">
          <LiteratureMarkdown
            :content="noteMarkdown"
            :anchor="navigationTarget?.anchor || ''"
            :navigation-token="navigationTarget?.requestId || ''"
            @open-link="$emit('open-link', $event)"
            @open-image="openImage"
          />
        </template>
        <div v-else class="pane-empty">{{ noteEmptyReason }}</div>
      </div>
    </template>

    <Teleport to="body"
      ><div
        v-if="previewSrc"
        ref="lightbox"
        class="image-lightbox"
        role="dialog"
        aria-modal="true"
        aria-label="文献图片预览"
        tabindex="-1"
        @keydown.esc.stop.prevent="closeImage"
        @keydown.left.prevent="stepImage(-1)"
        @keydown.right.prevent="stepImage(1)"
        @keydown.tab.prevent="trapLightboxFocus"
        @click.self="closeImage"
        @wheel.prevent="zoomImage($event.deltaY < 0 ? 0.1 : -0.1)"
      >
        <div class="lightbox-tools">
          <button class="btn" :disabled="images.length < 2" @click="stepImage(-1)">上一张</button>
          <button class="btn" @click="zoomImage(-0.25)">缩小</button
          ><span>{{ Math.round(imageScale * 100) }}%</span>
          <button class="btn" @click="zoomImage(0.25)">放大</button
          ><button class="btn" @click="imageScale = 1">复位</button>
          <button class="btn" :disabled="images.length < 2" @click="stepImage(1)">下一张</button>
          <button class="btn" :disabled="assistantBusy" @click="explainImage(previewSrc)">
            讲解此图
          </button>
          <button class="btn" @click="closeImage">关闭（Esc）</button>
        </div>
        <div class="lightbox-image">
          <img :src="previewSrc" alt="图片预览" :style="{ transform: `scale(${imageScale})` }" />
        </div></div
    ></Teleport>
  </div>
</template>

<script>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { apiGet, apiPut } from '@/apis/base'
import { confirmAction } from '@/composables/useLiteratureSessions'
import LiteratureMarkdown from './LiteratureMarkdown.vue'
import ProductEditor from './ProductEditor.vue'
import ReaderSelectionToolbar from './ReaderSelectionToolbar.vue'
import LiteraturePdfReader from './LiteraturePdfReader.vue'
import { readerCapture, isAutoSendAction } from '@/utils/literature_assistant'
import { useUserStore } from '@/stores/user'
import {
  locateParallelAnchor,
  moveReaderTab,
  readReaderPreferences,
  writeReaderPreferences
} from '@/utils/literature_reader'
import { collectAssetUris, createAssetResolverSession } from '@/utils/asset_resolver'

const splitParas = (text) =>
  String(text || '')
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter((paragraph) => paragraph && !paragraph.startsWith('#') && !paragraph.startsWith('---'))

export default {
  name: 'ReaderTabs',
  components: { LiteratureMarkdown, ProductEditor, ReaderSelectionToolbar, LiteraturePdfReader },
  props: {
    rid: { type: String, required: true },
    paper: { type: Object, default: () => ({}) },
    versionToken: { type: String, default: '' },
    readerTarget: { type: Object, default: null },
    evidenceTarget: { type: Object, default: null }
  },
  emits: ['document-saved', 'open-link', 'editing-change', 'assistant-action'],
  setup(props, { emit }) {
    const userStore = useUserStore()
    const selectionRoot = ref(null),
      assistantNotice = ref(''),
      assistantBusy = ref(false)
    const quickActions = {
      overview: '论文导读',
      methods: '研究方法',
      findings: '主要发现',
      limitations: '局限与启发'
    }
    const captureAction = (value) =>
      readerCapture({
        ...value,
        rid: props.rid,
        source_revision_id: props.paper.source_revision_id,
        reading_version: props.paper.reading_version
      })
    const sendAssistantAction = (value) => {
      if (loading.value || editing.value || productEditing.value) return
      try {
        const capture = captureAction(value)
        emit('assistant-action', { ...capture, imageFile: value.imageFile || null })
        assistantNotice.value = isAutoSendAction(value.action)
          ? '正在发送到本文会话，回答将进入对话历史。'
          : '正在交给本文会话；内容仅进入草稿，需确认发送。'
      } catch (error) {
        assistantNotice.value = error.message
      }
    }
    const explainImage = async (src) => {
      if (assistantBusy.value || !images.value.includes(src)) return
      const ticket = loadGeneration
      assistantBusy.value = true
      try {
        const capture = captureAction({ action: 'figure', surface: 'image' })
        // Only authenticated assets owned by this loaded reader may be attached.
        if (!src.startsWith('blob:')) throw new Error('图片尚未通过鉴权载入，不能发送')
        const response = await fetch(src),
          blob = await response.blob()
        if (!response.ok || !blob.type.startsWith('image/') || blob.size > 10 * 1024 * 1024)
          throw new Error('图片不可用或超过 10MB')
        if (disposed || ticket !== loadGeneration) return
        emit('assistant-action', {
          ...capture,
          imageFile: new File([blob], 'paper-figure', { type: blob.type })
        })
        assistantNotice.value = '图表已交给本文会话草稿，请确认发送。'
        closeImage()
      } catch (error) {
        if (!disposed && ticket === loadGeneration) assistantNotice.value = error.message
      } finally {
        if (!disposed && ticket === loadGeneration) assistantBusy.value = false
      }
    }
    const readerStorage = {
      getItem: (key) => window.localStorage.getItem(key),
      setItem: (key, value) => window.localStorage.setItem(key, value)
    }
    const preferences = readReaderPreferences(readerStorage, userStore.uid)
    const current = ref(preferences.tab)
    const fontSize = ref(preferences.fontSize)
    const tabOrder = ref(preferences.tabOrder),
      hiddenTabs = ref(preferences.hiddenTabs)
    const autoHideMissing = ref(preferences.autoHideMissing),
      manageTabs = ref(false),
      draggedTab = ref('')
    const productEditing = ref(false)
    const loading = ref(true)
    const bundle = ref(null)
    const variant = ref(null)
    const variantKind = ref('zh')
    const navigationTarget = ref(null)
    const otherVariant = ref(null)
    const selectedVariant = computed(() =>
      variantKind.value === 'zh' ? variant.value : otherVariant.value
    )
    const parallel = ref(null)
    const displayMarkdown = ref('')
    const imageSources = ref([])
    const pdfUrl = ref('')
    const pdfPage = ref(1)
    const previewSrc = ref('')
    const imageScale = ref(1),
      lightbox = ref(null)
    let previousFocus = null
    const objectUrls = new Set()
    let assetUrls = new Map()
    const nativeAssets = createAssetResolverSession()
    let loadGeneration = 0
    let noteGeneration = 0
    let variantGeneration = 0
    let disposed = false
    const subTabs = [
      { key: 'original', label: '原文' },
      { key: 'pdf', label: 'PDF' },
      { key: 'parallel', label: '双语对照' },
      { key: 'translation', label: '译文 / 总结' },
      { key: 'images', label: '图片' },
      { key: 'noteL1', label: '笔记(L1)' },
      { key: 'noteL2', label: '深度(L2)' },
      { key: 'noteL3', label: '概念关系(L3)' }
    ]
    const availableTabs = computed(() => ({
      original: !!bundle.value?.ready,
      pdf: !!pdfUrl.value,
      parallel: !!parallel.value?.ready || (!!bundle.value?.ready && !!variant.value?.ready),
      translation:
        !!variant.value?.ready ||
        !!otherVariant.value?.ready ||
        (props.paper.variants || []).some((item) =>
          ['zh', 'en_zh', 'summary'].includes(item.variant_kind)
        ),
      images: imageSources.value.length > 0,
      ...Object.fromEntries(
        ['L1', 'L2', 'L3'].map((level) => [
          `note${level}`,
          (props.paper.compile_jobs || []).some(
            (job) => job.level === level && job.status === 'SUCCEEDED'
          )
        ])
      )
    }))
    const orderedTabs = computed(() =>
      tabOrder.value.map((key) => subTabs.find((tab) => tab.key === key))
    )
    const visibleTabs = computed(() =>
      orderedTabs.value.filter(
        (tab) =>
          tab.key === 'original' ||
          (navigationTarget.value?.rid === props.rid && navigationTarget.value?.tab === tab.key) ||
          (!hiddenTabs.value.includes(tab.key) &&
            (!autoHideMissing.value || availableTabs.value[tab.key]))
      )
    )
    const selectTab = async (tab) => {
      if (tab === current.value) return
      if (editing.value || productEditing.value) {
        if (!(await confirmAction('切换阅读标签会放弃未保存编辑，继续？'))) return
      }
      if (tab === current.value) return
      editing.value = productEditing.value = false
      current.value = tab
    }
    const setTabVisible = (key, visible) => {
      hiddenTabs.value = hiddenTabs.value.filter((item) => item !== key)
      if (!visible && key !== 'original') hiddenTabs.value.push(key)
    }
    const reorderTab = (from, to) => {
      if (!editing.value && !productEditing.value)
        tabOrder.value = moveReaderTab(tabOrder.value, from, to)
    }

    const enc = (value) => encodeURIComponent(value)
    const pdfScale = ref(preferences.pdfScale)
    const pdfFitWidth = ref(preferences.pdfFitWidth)
    watch([current, fontSize, pdfScale, pdfFitWidth, tabOrder, hiddenTabs, autoHideMissing], () =>
      writeReaderPreferences(readerStorage, userStore.uid, {
        tab: current.value,
        fontSize: fontSize.value,
        pdfScale: pdfScale.value,
        pdfFitWidth: pdfFitWidth.value,
        tabOrder: tabOrder.value,
        hiddenTabs: hiddenTabs.value,
        autoHideMissing: autoHideMissing.value
      })
    )
    const changeZoom = (amount) => {
      pdfFitWidth.value = false
      pdfScale.value = Math.max(0.25, Math.min(4, pdfScale.value + amount))
    }
    const noteMarkdown = ref('')
    const noteLoading = ref(false)
    const noteEmptyReason = ref('该级别尚未编译')
    const scaledPdfUrl = computed(() =>
      pdfUrl.value
        ? `${pdfUrl.value}#zoom=${pdfFitWidth.value ? 'page-width' : Math.round(pdfScale.value * 100)}`
        : ''
    )
    const editing = ref(false)
    watch([editing, productEditing], () =>
      emit('editing-change', editing.value || productEditing.value)
    )
    watch([visibleTabs, loading], () => {
      if (
        !loading.value &&
        !editing.value &&
        !productEditing.value &&
        !visibleTabs.value.some((tab) => tab.key === current.value)
      )
        current.value = 'original'
    })
    const saving = ref(false)
    const editDocument = ref({ paragraphs: [] })
    const editRevision = ref(null)
    const editRid = ref('')
    const editInput = ref({})
    const editorNotice = ref('')
    const startEditing = async () => {
      const rid = props.rid
      try {
        const result = await apiGet(`/api/literature/papers/${enc(rid)}/document`)
        if (disposed || rid !== props.rid) return
        editRid.value = rid
        editDocument.value = result.document
        for (const paragraph of editDocument.value.paragraphs) {
          paragraph.text_en = paragraph.text_en || paragraph.text || ''
        }
        editRevision.value = result.expected_revision
        editInput.value = { source: result.source_revision_id, version: result.reading_version }
        editing.value = true
        editorNotice.value = ''
      } catch (err) {
        if (disposed || rid !== props.rid) return
        editorNotice.value = `正文加载失败：${err?.message || err}`
      }
    }
    const saveDocument = async () => {
      const rid = editRid.value
      if (!rid || rid !== props.rid || saving.value) return
      saving.value = true
      try {
        await apiPut(`/api/literature/papers/${enc(rid)}/document`, {
          document: editDocument.value,
          expected_revision: editRevision.value,
          expected_source_revision: editInput.value.source,
          expected_reading_version: editInput.value.version,
          freeze: true
        })
        if (disposed || rid !== props.rid) return
        editing.value = false
        emit('document-saved')
        await load()
        editorNotice.value = '人工正文已冻结；请重新生成编译笔记及译文。'
      } catch (err) {
        if (disposed || rid !== props.rid) return
        editorNotice.value = `未保存：${err?.message || err}（版本冲突时请重新打开编辑）`
      } finally {
        saving.value = false
      }
    }

    const loadNote = async (level) => {
      const generation = ++noteGeneration
      const rid = props.rid
      const isCurrent = () => !disposed && generation === noteGeneration && rid === props.rid
      noteLoading.value = true
      noteMarkdown.value = ''
      noteEmptyReason.value = '该级别尚未编译'
      try {
        const resp = await fetch(`/api/literature/papers/${enc(rid)}/notes/${level}`, {
          headers: useUserStore().getAuthHeaders()
        })
        if (!isCurrent()) return
        if (resp.ok) {
          const data = await resp.json()
          if (data.note_uri) {
            const assetResp = await fetch(
              `/api/literature/assets?uri=${encodeURIComponent(data.note_uri)}`,
              { headers: useUserStore().getAuthHeaders() }
            )
            if (assetResp.ok) {
              const text = await assetResp.text()
              if (isCurrent()) noteMarkdown.value = text
            } else if (isCurrent()) {
              noteEmptyReason.value = '笔记资源不可用或无权访问'
            }
          }
        } else if (resp.status === 404) {
          noteEmptyReason.value = '笔记不存在或尚未编译'
        } else {
          noteEmptyReason.value = '笔记加载失败或无权访问'
        }
      } catch {
        if (isCurrent()) noteEmptyReason.value = '加载失败'
      } finally {
        if (isCurrent()) noteLoading.value = false
      }
    }
    const parallelRows = computed(() => {
      if (parallel.value?.ready) {
        return (parallel.value.paragraphs || []).map((row, index) => ({ index, ...row }))
      }
      const originals = splitParas(displayMarkdown.value)
      const translations = splitParas(variant.value?.content)
      const total = Math.max(originals.length, translations.length)
      return Array.from({ length: total }, (_, index) => ({
        index,
        original: originals[index] || '',
        translated: translations[index] || ''
      }))
    })
    const images = computed(() => imageSources.value)
    const parallelAnchor = computed(() =>
      locateParallelAnchor(parallelRows.value, navigationTarget.value?.anchor)
    )

    const authenticatedObjectUrl = async (url, generation) => {
      const response = await apiGet(url, {}, true, 'blob')
      const objectUrl = URL.createObjectURL(await response.blob())
      if (disposed || generation !== loadGeneration) {
        URL.revokeObjectURL(objectUrl)
        return null
      }
      objectUrls.add(objectUrl)
      return objectUrl
    }

    const releaseObjectUrls = () => {
      nativeAssets.abortAll()
      nativeAssets.revokeAll()
      objectUrls.forEach((url) => URL.revokeObjectURL(url))
      objectUrls.clear()
      assetUrls = new Map()
      imageSources.value = []
      pdfUrl.value = ''
    }

    const hydrateProtectedAssets = async (markdown, generation, kbId = '') => {
      if (disposed || generation !== loadGeneration) return ''
      const source = String(markdown || '')
      const assetPattern = /\/api\/literature\/assets\?uri=[^)\s]+/g
      const protectedUrls = [...new Set(source.match(assetPattern) || [])]
      const resolved = (
        await Promise.all(
          protectedUrls.map(async (url) => {
            try {
              if (!assetUrls.has(url))
                assetUrls.set(
                  url,
                  authenticatedObjectUrl(url, generation).catch(() => null)
                )
              const objectUrl = await assetUrls.get(url)
              return objectUrl ? [url, objectUrl] : null
            } catch {
              return null
            }
          })
        )
      ).filter(Boolean)
      if (disposed || generation !== loadGeneration) return ''
      const lookup = new Map(resolved)
      const native = await nativeAssets.resolveAssets(collectAssetUris(source), { kbId })
      if (disposed || generation !== loadGeneration) return ''
      imageSources.value = [
        ...new Set([
          ...imageSources.value,
          ...resolved.map(([, objectUrl]) => objectUrl),
          ...[...native.values()].filter(Boolean)
        ])
      ]
      const hydrated = protectedUrls.reduce(
        (content, url) => content.split(url).join(lookup.get(url) || url),
        source
      )
      return [...native].reduce((text, [uri, url]) => text.split(uri).join(url || ''), hydrated)
    }

    const load = async () => {
      assistantBusy.value = false
      assistantNotice.value = ''
      const generation = ++loadGeneration
      const rid = props.rid
      const isCurrent = () => !disposed && generation === loadGeneration && rid === props.rid
      loading.value = true
      releaseObjectUrls()
      bundle.value = variant.value = parallel.value = null
      displayMarkdown.value = ''
      closeImage()
      try {
        const [readerBundle, translatedVariant, parallelVariant] = await Promise.all([
          apiGet(`/api/literature/papers/${enc(rid)}/reader`).catch((error) => ({
            ready: false,
            reason: error.message || '阅读正文读取失败'
          })),
          apiGet(`/api/literature/papers/${enc(rid)}/variants/zh`).catch((error) => ({
            ready: false,
            reason: error.message || '译文读取失败'
          })),
          apiGet(`/api/literature/papers/${enc(rid)}/parallel`).catch((error) => ({
            ready: false,
            reason: error.message || '对照阅读读取失败'
          }))
        ])
        if (!isCurrent()) return
        bundle.value = readerBundle
        variant.value = translatedVariant
        parallel.value = parallelVariant
        const markdown = readerBundle?.ready
          ? await hydrateProtectedAssets(
              readerBundle.markdown,
              generation,
              readerBundle.knowledge_base_id
            )
          : ''
        if (!isCurrent()) return
        displayMarkdown.value = markdown
        if (parallelVariant?.ready) {
          const rows = []
          for (const row of parallelVariant.paragraphs || []) {
            if (!isCurrent()) return
            rows.push({
              ...row,
              original: await hydrateProtectedAssets(
                row.original,
                generation,
                readerBundle?.knowledge_base_id || ''
              ),
              translated: await hydrateProtectedAssets(
                row.translated,
                generation,
                readerBundle?.knowledge_base_id || ''
              )
            })
          }
          if (!isCurrent()) return
          parallel.value = { ...parallelVariant, paragraphs: rows }
        }
        try {
          const url = await authenticatedObjectUrl(
            `/api/literature/papers/${enc(rid)}/source-pdf`,
            generation
          )
          if (isCurrent()) pdfUrl.value = url || ''
        } catch {
          if (isCurrent()) pdfUrl.value = ''
        }
      } finally {
        if (isCurrent()) loading.value = false
      }
    }

    const openImage = async (src) => {
      if (!src) return
      previousFocus = document.activeElement
      imageScale.value = 1
      previewSrc.value = src
      await nextTick()
      lightbox.value?.focus()
    }
    const closeImage = () => {
      const opened = !!previewSrc.value
      previewSrc.value = ''
      if (opened) previousFocus?.focus?.()
    }
    const trapLightboxFocus = (event) => {
      const buttons = [...(lightbox.value?.querySelectorAll('button:not([disabled])') || [])]
      if (!buttons.length) return
      const index = buttons.indexOf(document.activeElement)
      const target =
        index < 0
          ? event.shiftKey
            ? buttons.length - 1
            : 0
          : (index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length
      buttons[target].focus()
    }
    const zoomImage = (amount) =>
      (imageScale.value = Math.max(0.25, Math.min(5, imageScale.value + amount)))
    const stepImage = (step) => {
      if (!images.value.length) return
      const index = images.value.indexOf(previewSrc.value)
      previewSrc.value = images.value[(index + step + images.value.length) % images.value.length]
      imageScale.value = 1
    }
    const refreshReader = async () => {
      if (editing.value || productEditing.value) {
        editorNotice.value = '产物版本已更新；未清除本地编辑，请先保存或取消，再刷新。'
        return
      }
      await load()
      if (current.value.startsWith('noteL')) await loadNote(current.value.slice(4))
      if (variantKind.value !== 'zh') await loadOtherVariant()
    }
    watch(
      () => props.versionToken,
      (value, old) => {
        if (old && value !== old) refreshReader()
      }
    )
    const loadOtherVariant = async () => {
      const ticket = ++variantGeneration,
        rid = props.rid,
        kind = variantKind.value
      otherVariant.value = null
      if (kind === 'zh') return
      try {
        const result = await apiGet(`/api/literature/papers/${enc(rid)}/variants/${kind}`)
        if (!disposed && ticket === variantGeneration && rid === props.rid)
          otherVariant.value = result
      } catch (error) {
        if (!disposed && ticket === variantGeneration)
          otherVariant.value = { ready: false, reason: error.message || '读取失败' }
      }
    }
    const reloadVariant = () => (variantKind.value === 'zh' ? load() : loadOtherVariant())
    watch(() => [props.rid, variantKind.value], loadOtherVariant)

    // 输出模板（仅 en_zh 有意义）：切换走零成本重渲染，不重新翻译。
    const templateOptions = ref([])
    const templateKey = ref('obsidian_bilingual')
    const templateBusy = ref(false)
    const templateNotice = ref('')
    const loadTemplates = async () => {
      try {
        const result = await apiGet('/api/literature/templates')
        if (!disposed) templateOptions.value = result.templates || []
      } catch {
        // 模板清单不可用时保留默认项，不阻断阅读
        if (!disposed) templateOptions.value = []
      }
    }
    const applyTemplate = async (next) => {
      const previous = templateKey.value
      templateKey.value = previous
      if (!next || next === previous || variantKind.value !== 'en_zh') return
      templateBusy.value = true
      templateNotice.value = ''
      try {
        const result = await apiGet(
          `/api/literature/papers/${enc(props.rid)}/variants/en_zh/templated?template=${encodeURIComponent(next)}`
        )
        if (!result?.ready) {
          templateNotice.value = result?.reason || '该模板暂不可用'
          return
        }
        templateKey.value = next
        otherVariant.value = result
        templateNotice.value = '已按新模板重排（未重新翻译）'
      } catch (error) {
        templateNotice.value = `模板切换失败：${error?.message || error}`
      } finally {
        templateBusy.value = false
      }
    }
    watch(templateKey, applyTemplate)
    watch(
      () => props.rid,
      () => loadTemplates()
    )
    onMounted(loadTemplates)

    watch(current, (tab) => {
      ++noteGeneration
      if (tab === 'noteL1') loadNote('L1')
      else if (tab === 'noteL2') loadNote('L2')
      else if (tab === 'noteL3') loadNote('L3')
    })
    watch(
      () => props.rid,
      () => {
        ++noteGeneration
        navigationTarget.value = null
        current.value = readReaderPreferences(readerStorage, userStore.uid).tab
        editing.value = false
        editRid.value = editorNotice.value = noteMarkdown.value = ''
        load()
        if (current.value.startsWith('noteL')) loadNote(current.value.slice(4))
      },
      { immediate: true }
    )
    onUnmounted(() => {
      disposed = true
      ++loadGeneration
      ++noteGeneration
      ++variantGeneration
      releaseObjectUrls()
      emit('editing-change', false)
    })
    watch(
      () => props.readerTarget,
      async (target) => {
        if (target?.rid !== props.rid) return
        const tab = target.tab || current.value
        if (!subTabs.some((item) => item.key === tab)) return
        if (editing.value || productEditing.value) {
          if (!(await confirmAction('打开双链会放弃未保存编辑，继续？'))) return
        }
        editing.value = productEditing.value = false
        navigationTarget.value = target
        selectTab(tab)
        if (current.value !== tab) return
        if (target.variant && ['zh', 'summary', 'en_zh'].includes(target.variant))
          variantKind.value = target.variant
        if (tab.startsWith('noteL')) loadNote(tab.slice(4))
      },
      { immediate: true }
    )
    watch(
      () => props.evidenceTarget,
      (target) => {
        if (target?.rid === props.rid) selectTab('pdf')
      },
      { immediate: true }
    )
    watch(
      () => [props.rid, props.paper.source_revision_id],
      () => {
        pdfPage.value = 1
      }
    )

    return {
      selectionRoot,
      assistantNotice,
      assistantBusy,
      quickActions,
      sendAssistantAction,
      explainImage,
      pdfPage,
      navigationTarget,
      current,
      visibleTabs,
      orderedTabs,
      availableTabs,
      tabOrder,
      hiddenTabs,
      autoHideMissing,
      manageTabs,
      draggedTab,
      selectTab,
      setTabVisible,
      reorderTab,
      refreshReader,
      productEditing,
      imageScale,
      lightbox,
      closeImage,
      trapLightboxFocus,
      zoomImage,
      stepImage,
      fontSize,
      loading,
      pdfScale,
      pdfFitWidth,
      changeZoom,
      scaledPdfUrl,
      editing,
      saving,
      editDocument,
      editorNotice,
      startEditing,
      saveDocument,
      noteMarkdown,
      noteLoading,
      noteEmptyReason,
      bundle,
      variant,
      variantKind,
      selectedVariant,
      reloadVariant,
      displayMarkdown,
      previewSrc,
      subTabs,
      pdfUrl,
      parallelRows,
      parallelAnchor,
      images,
      openImage,
      load,
      loadNote
    }
  }
}
</script>

<style lang="less" scoped>
.template-picker {
  margin-left: 8px;
}
.template-notice {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
}
.reader-tabs {
  min-height: 320px;
}
.reader-tabs :deep(.yk-markdown-preview) {
  font-size: inherit;
}
.sub-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  border-bottom: 1px solid var(--gray-150);
  margin-bottom: 10px;
}
.sub-tab {
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
  transition: color 0.15s;
  &:hover {
    color: var(--main-color);
  }
  &.active {
    border-bottom: 2px solid var(--main-color);
    color: var(--main-color);
    font-weight: 600;
  }
}
.reader-loading,
.pane-empty {
  padding: 28px 0;
  color: var(--color-text-tertiary);
}
.resolved-badge {
  display: inline-block;
  font-size: 12px;
  border-radius: 4px;
  padding: 2px 8px;
  margin-bottom: 8px;
  background: color-mix(in srgb, var(--color-success-500) 14%, transparent);
  color: var(--color-success-700);
}
.parallel-cols {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 0 16px;
  font-size: 13px;
  line-height: 1.7;
}
.parallel-para {
  padding: 6px 0;
  border-bottom: 1px dashed var(--gray-200);
  &.zh {
    opacity: 0.95;
  }
}
.image-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px;
}
.image-item {
  margin: 0;
  img {
    width: 100%;
    border: 1px solid var(--gray-150);
    border-radius: 6px;
    cursor: zoom-in;
  }
}
.pdf-controls {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}
.ctrl-btn {
  border: 1px solid var(--gray-300);
  border-radius: 4px;
  background: var(--gray-100);
  color: var(--color-text);
  padding: 2px 10px;
  cursor: pointer;
  &:hover {
    border-color: var(--main-color);
  }
}
.ctrl-scale {
  font-size: 13px;
  min-width: 40px;
  text-align: center;
}
.pdf-wrap {
  overflow-x: auto;
  &.fit-width .pdf-frame {
    width: 100%;
  }
}
.note-md {
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: 13px;
  padding: 0 4px;
}
.pdf-frame {
  width: 100%;
  height: 560px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
}
.image-lightbox {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.85);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
  cursor: zoom-out;
  img {
    max-width: 92vw;
    max-height: 90vh;
  }
}
.reader-tools,
.tab-option,
.lightbox-tools {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin: 8px 0;
}
.tab-manager {
  padding: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
}
.tab-option {
  padding: 6px;
  background: var(--gray-25);
}
.tab-option label {
  min-width: 130px;
}
.image-open {
  padding: 0;
  border: 0;
  background: transparent;
  width: 100%;
  cursor: zoom-in;
}
.image-lightbox {
  display: flex;
  flex-direction: column;
  padding: 12px;
  box-sizing: border-box;
}
.lightbox-tools {
  z-index: 1;
  color: var(--color-text);
  background: var(--color-bg-container);
  padding: 8px;
  border-radius: 8px;
  cursor: default;
}
.lightbox-image {
  width: 100%;
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: grid;
  place-items: center;
  cursor: default;
}
.lightbox-image img {
  transform-origin: center;
}
@media (max-width: 700px) {
  .parallel-cols {
    grid-template-columns: 1fr;
  }
}
</style>
