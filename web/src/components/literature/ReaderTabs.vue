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
        :disabled="loading || saving || editing"
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
        :disabled="loading || editing || assistantBusy"
        @click="sendAssistantAction({ action, surface: 'paper' })"
      >
        {{ label }}
      </button>
    </div>
    <p v-if="assistantNotice" role="status">{{ assistantNotice }}</p>
    <ReaderSelectionToolbar
      :root="selectionRoot"
      :disabled="loading || editing || assistantBusy || current === 'pdf'"
      :reset-key="`${rid}:${versionToken}:${current}`"
      @action="sendAssistantAction"
    />
    <section v-if="manageTabs" class="tab-manager" aria-label="阅读标签管理">
      <label
        ><input
          v-model="autoHideMissing"
          type="checkbox"
          :disabled="editing"
        />自动隐藏缺失产物</label
      >
      <p>拖拽排序，也可使用上移/下移按钮。原文状态入口始终保留；偏好按当前用户保存。</p>
      <div
        v-for="(tab, index) in orderedTabs"
        :key="tab.key"
        class="tab-option"
        :draggable="!editing"
        @dragstart="draggedTab = tab.key"
        @dragover.prevent
        @drop.prevent="reorderTab(draggedTab, tab.key)"
        @dragend="draggedTab = ''"
      >
        <label
          ><input
            type="checkbox"
            :checked="!hiddenTabs.includes(tab.key)"
            :disabled="tab.key === 'original' || editing"
            @change="setTabVisible(tab.key, $event.target.checked)"
          />{{ tab.label }}</label
        >
        <span>{{ availableTabs[tab.key] ? '已就绪' : '尚未生成 / 不可用' }}</span>
        <button
          class="btn"
          :disabled="index === 0 || editing"
          @click="reorderTab(tab.key, tabOrder[index - 1])"
        >
          上移
        </button>
        <button
          class="btn"
          :disabled="index === orderedTabs.length - 1 || editing"
          @click="reorderTab(tab.key, tabOrder[index + 1])"
        >
          下移
        </button>
      </div>
    </section>

    <div v-if="loading" class="reader-loading" role="status">
      {{ current === 'pdf' ? '源 PDF 载入中…' : '正文载入中…' }}
    </div>

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
          <p v-if="imagesLoading" role="status">正文已就绪，图片正在单独载入…</p>
          <button
            v-if="!editing && bundle.source !== 'scientific_parse'"
            class="sub-tab"
            @click="startEditing"
          >
            编辑正文
          </button>
          <template v-if="editing">
            <p>保存将冻结人工版本；自动渲染不会覆盖。</p>
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

      <div v-else-if="current === 'images'" class="pane">
        <p v-if="imagesLoading || galleryLoading" role="status">图片载入中，已完成的图片可先查看…</p>
        <p v-if="imageError" role="status">{{ imageError }}</p>
        <div v-if="!images.length && !imagesLoading && !galleryLoading" class="pane-empty">
          无图片（或渲染尚未完成）
        </div>
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
        <div v-if="!pdfUrl" class="pane-empty">{{ pdfError || '源 PDF 不可用' }}</div>
        <LiteraturePdfWorkspace
          v-else
          :original-url="pdfUrl"
          :rid="rid"
          :version-token="versionToken"
          :source="paper.source || {}"
          :evidence-target="evidenceTarget"
          :initial-page="pdfPage"
          v-model:scale="pdfScale"
          v-model:fit-width="pdfFitWidth"
          @page-changed="pdfPage = $event"
          @assistant-action="sendAssistantAction"
        />
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
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { apiGet, apiPut } from '@/apis/base'
import { confirmAction } from '@/composables/useLiteratureSessions'
import LiteratureMarkdown from './LiteratureMarkdown.vue'
import ReaderSelectionToolbar from './ReaderSelectionToolbar.vue'
import LiteraturePdfWorkspace from './LiteraturePdfWorkspace.vue'
import { readerCapture, isAutoSendAction } from '@/utils/literature_assistant'
import { useUserStore } from '@/stores/user'
import {
  moveReaderTab,
  readReaderPreferences,
  writeReaderPreferences
} from '@/utils/literature_reader'
import { collectAssetUris, createAssetResolverSession } from '@/utils/asset_resolver'

export default {
  name: 'ReaderTabs',
  components: { LiteratureMarkdown, ReaderSelectionToolbar, LiteraturePdfWorkspace },
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
      if (loading.value || editing.value) return
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
    const subTabKeys = ['original', 'pdf', 'images']
    const current = ref(subTabKeys.includes(preferences.tab) ? preferences.tab : 'original')
    const fontSize = ref(preferences.fontSize)
    const tabOrder = ref(preferences.tabOrder),
      hiddenTabs = ref(preferences.hiddenTabs)
    const autoHideMissing = ref(preferences.autoHideMissing),
      manageTabs = ref(false),
      draggedTab = ref('')
    const readerLoading = ref(true),
      pdfLoading = ref(true),
      imagesLoading = ref(false),
      galleryLoading = ref(false),
      pdfError = ref(''),
      imageError = ref(''),
      hasImages = ref(false)
    const loading = computed(() => current.value === 'pdf' ? pdfLoading.value : readerLoading.value)
    const bundle = ref(null)
    const navigationTarget = ref(null)
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
    let disposed = false
    let loadController = null,
      galleryGeneration = 0
    const subTabs = [
      { key: 'original', label: '原文' },
      { key: 'pdf', label: 'PDF 阅读' },
      { key: 'images', label: '图片' }
    ]
    const availableTabs = computed(() => ({
      original: !!bundle.value?.ready,
      pdf: pdfLoading.value || !!pdfUrl.value,
      images: readerLoading.value || hasImages.value
    }))
    // 存量偏好里可能残留已下线标签（译文/对照/笔记）的 key；map 后过滤掉，
    // 否则 find 返回 undefined 会在 visibleTabs 上直接崩溃。
    const orderedTabs = computed(() =>
      tabOrder.value
        .map((key) => subTabs.find((tab) => tab.key === key))
        .filter(Boolean)
    )
    const visibleTabs = computed(() =>
      orderedTabs.value.filter(
        (tab) =>
          tab.key === 'original' ||
          (navigationTarget.value?.rid === props.rid && navigationTarget.value?.tab === tab.key) ||
          (!hiddenTabs.value.includes(tab.key) &&
            (!autoHideMissing.value || availableTabs.value[tab.key] ||
              (tab.key === 'pdf' && current.value === 'pdf' && pdfError.value)))
      )
    )
    const selectTab = async (tab) => {
      if (tab === current.value) return
      if (editing.value) {
        if (!(await confirmAction('切换阅读标签会放弃未保存编辑，继续？'))) return
      }
      if (tab === current.value) return
      editing.value = false
      current.value = tab
    }
    const setTabVisible = (key, visible) => {
      hiddenTabs.value = hiddenTabs.value.filter((item) => item !== key)
      if (!visible && key !== 'original') hiddenTabs.value.push(key)
    }
    const reorderTab = (from, to) => {
      if (!editing.value) tabOrder.value = moveReaderTab(tabOrder.value, from, to)
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
    const scaledPdfUrl = computed(() =>
      pdfUrl.value
        ? `${pdfUrl.value}#zoom=${pdfFitWidth.value ? 'page-width' : Math.round(pdfScale.value * 100)}`
        : ''
    )
    const editing = ref(false)
    watch([editing], () => emit('editing-change', editing.value))
    watch([visibleTabs, loading], () => {
      if (
        !loading.value &&
        !editing.value &&
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
      const rid = props.rid,
        generation = loadGeneration
      try {
        const result = await apiGet(`/api/literature/papers/${enc(rid)}/document`, { signal: loadController?.signal })
        if (disposed || generation !== loadGeneration || rid !== props.rid) return
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
        if (disposed || generation !== loadGeneration || rid !== props.rid) return
        editorNotice.value = `正文加载失败：${err?.message || err}`
      }
    }
    const saveDocument = async () => {
      const rid = editRid.value,
        generation = loadGeneration
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
        if (disposed || generation !== loadGeneration || rid !== props.rid) return
        editing.value = false
        emit('document-saved')
        const refreshGeneration = loadGeneration + 1
        await load()
        if (disposed || refreshGeneration !== loadGeneration || rid !== props.rid) return
        editorNotice.value = '人工正文已冻结。'
      } catch (err) {
        if (disposed || generation !== loadGeneration || rid !== props.rid) return
        editorNotice.value = `未保存：${err?.message || err}（版本冲突时请重新打开编辑）`
      } finally {
        saving.value = false
      }
    }

    const images = computed(() => imageSources.value)

    const authenticatedObjectUrl = async (url, generation) => {
      if (disposed || generation !== loadGeneration) return null
      const response = await apiGet(url, { signal: loadController?.signal }, true, 'blob')
      const blob = await response.blob()
      if (disposed || generation !== loadGeneration) return null
      const objectUrl = URL.createObjectURL(blob)
      objectUrls.add(objectUrl)
      return objectUrl
    }

    const releaseObjectUrls = () => {
      loadController?.abort()
      nativeAssets.abortAll()
      nativeAssets.revokeAll()
      objectUrls.forEach((url) => URL.revokeObjectURL(url))
      objectUrls.clear()
      assetUrls = new Map()
      imageSources.value = []
      pdfUrl.value = ''
    }

    // Legacy asset URLs need a Bearer proxy; resolve only four at a time and
    // publish figures progressively without holding text or PDF readiness.
    const hydrateProtectedAssets = async (markdown, generation) => {
      const source = String(markdown || '')
      const urls = [...new Set(source.match(/\/api\/literature\/assets\?uri=[^)\s]+/g) || [])]
      if (!urls.length) return
      imagesLoading.value = true
      const queue = [...urls],
        resolved = new Map(),
        isCurrent = () => !disposed && generation === loadGeneration
      try {
        await Promise.all(Array.from({ length: Math.min(4, queue.length) }, async () => {
          while (queue.length && isCurrent()) {
            const url = queue.shift()
            if (!assetUrls.has(url))
              assetUrls.set(url, authenticatedObjectUrl(url, generation).catch(() => null))
            const objectUrl = await assetUrls.get(url)
            if (!isCurrent()) return
            if (objectUrl) {
              resolved.set(url, objectUrl)
              imageSources.value = [...new Set([...imageSources.value, objectUrl])]
            } else imageError.value = '部分图片暂不可用，可刷新阅读产物后重试。'
          }
        }))
        if (isCurrent())
          displayMarkdown.value = [...resolved].reduce((text, [url, blob]) => text.split(url).join(blob), source)
      } finally {
        if (isCurrent()) imagesLoading.value = false
      }
    }

    // MarkdownPreview already resolves native inline images with bounded
    // concurrency. The separate image gallery only fetches its bytes on demand.
    const loadNativeGallery = async () => {
      const generation = loadGeneration
      if (disposed || readerLoading.value || current.value !== 'images' || galleryGeneration === generation) return
      const uris = collectAssetUris(bundle.value?.markdown || '')
      if (!uris.length || !bundle.value?.knowledge_base_id) return
      galleryGeneration = generation
      galleryLoading.value = true
      try {
        const assets = await nativeAssets.resolveAssets(uris, { kbId: bundle.value.knowledge_base_id })
        if (disposed || generation !== loadGeneration) return
        imageSources.value = [...new Set([...imageSources.value, ...[...assets.values()].filter(Boolean)])]
        if ([...assets.values()].some((url) => !url))
          imageError.value = '部分图片暂不可用，可刷新阅读产物后重试。'
      } catch (error) {
        if (!disposed && generation === loadGeneration)
          imageError.value = error.message || '图片读取失败'
      } finally {
        if (!disposed && generation === loadGeneration) galleryLoading.value = false
      }
    }

    const load = async () => {
      assistantBusy.value = false
      assistantNotice.value = ''
      const generation = ++loadGeneration,
        rid = props.rid,
        isCurrent = () => !disposed && generation === loadGeneration && rid === props.rid
      releaseObjectUrls()
      loadController = new AbortController()
      const signal = loadController.signal
      readerLoading.value = pdfLoading.value = true
      imagesLoading.value = galleryLoading.value = hasImages.value = false
      pdfError.value = imageError.value = ''
      galleryGeneration = 0
      bundle.value = null
      displayMarkdown.value = ''
      closeImage()
      const textReady = (async () => {
        try {
          const result = await apiGet(`/api/literature/papers/${enc(rid)}/reader`, { signal })
          if (!isCurrent()) return
          bundle.value = result
          displayMarkdown.value = result?.ready ? String(result.markdown || '') : ''
          hasImages.value = /\/api\/literature\/assets\?uri=/.test(displayMarkdown.value) ||
            collectAssetUris(displayMarkdown.value).length > 0
          readerLoading.value = false
          if (result?.ready) {
            void hydrateProtectedAssets(displayMarkdown.value, generation)
            void loadNativeGallery()
          }
        } catch (error) {
          if (isCurrent()) bundle.value = { ready: false, reason: error.message || '阅读正文读取失败' }
        } finally {
          if (isCurrent()) readerLoading.value = false
        }
      })()
      const pdfReady = (async () => {
        try {
          const url = await authenticatedObjectUrl(`/api/literature/papers/${enc(rid)}/source-pdf`, generation)
          if (isCurrent()) pdfUrl.value = url || ''
        } catch (error) {
          if (isCurrent()) pdfError.value = error.message || '源 PDF 读取失败'
        } finally {
          if (isCurrent()) pdfLoading.value = false
        }
      })()
      await Promise.all([textReady, pdfReady])
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
      if (editing.value) {
        editorNotice.value = '产物版本已更新；未清除本地编辑，请先保存或取消，再刷新。'
        return
      }
      await load()
    }
    watch(current, () => { void loadNativeGallery() })
    watch(
      () => props.versionToken,
      (value, old) => {
        if (old && value !== old) refreshReader()
      }
    )

    watch(
      () => [props.rid, userStore.uid, userStore.departmentId, userStore.userRole],
      () => {
        navigationTarget.value = null
        const stored = readReaderPreferences(readerStorage, userStore.uid).tab
        current.value = subTabKeys.includes(stored) ? stored : 'original'
        editing.value = false
        editRid.value = editorNotice.value = ''
        pdfPage.value = 1
        load()
      },
      { immediate: true, flush: 'sync' }
    )
    onUnmounted(() => {
      disposed = true
      ++loadGeneration
      releaseObjectUrls()
      emit('editing-change', false)
    })
    watch(
      () => props.readerTarget,
      async (target) => {
        if (target?.rid !== props.rid) return
        const tab = target.tab || current.value
        if (!subTabs.some((item) => item.key === tab)) return
        if (editing.value) {
          if (!(await confirmAction('打开双链会放弃未保存编辑，继续？'))) return
        }
        editing.value = false
        navigationTarget.value = target
        selectTab(tab)
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
      imageScale,
      lightbox,
      closeImage,
      trapLightboxFocus,
      zoomImage,
      stepImage,
      fontSize,
      loading,
      readerLoading,
      pdfLoading,
      imagesLoading,
      galleryLoading,
      pdfError,
      imageError,
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
      bundle,
      displayMarkdown,
      previewSrc,
      subTabs,
      pdfUrl,
      images,
      openImage,
      load
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
</style>
