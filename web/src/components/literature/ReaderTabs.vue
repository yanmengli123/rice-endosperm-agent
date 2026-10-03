<template>
  <div class="reader-tabs" :style="{ fontSize: `${fontSize}px` }">
    <div class="sub-tabs">
      <button
        v-for="tab in subTabs"
        :key="tab.key"
        class="sub-tab"
        :class="{ active: current === tab.key }"
        @click="current = tab.key"
      >
        {{ tab.label }}
      </button>
    </div>
    <label
      >阅读字号<select v-model.number="fontSize">
        <option v-for="size in [14, 16, 18, 20, 22, 24]" :key="size" :value="size">
          {{ size }}px
        </option>
      </select></label
    >

    <div v-if="loading" class="reader-loading">加载中…</div>

    <template v-else>
      <div v-if="current === 'original'" class="pane">
        <div v-if="!bundle?.ready" class="pane-empty">
          {{ bundle?.reason || '阅读视图尚未生成（请先触发渲染）' }}
        </div>
        <template v-else>
          <div v-if="bundle.resolved" class="resolved-badge">
            {{ bundle.frozen ? '人工修订已冻结' : '已应用人工裁决（修正版）' }}
          </div>
          <button v-if="!editing" class="sub-tab" @click="startEditing">编辑正文</button>
          <template v-if="editing">
            <p>保存将冻结人工版本；自动渲染不会覆盖，旧编译及译文会失效。</p>
            <div v-for="paragraph in editDocument.paragraphs" :key="paragraph.para_id">
              <label>{{ paragraph.para_id }}</label>
              <textarea v-model="paragraph.text_en" rows="4" style="width: 100%"></textarea>
            </div>
            <button :disabled="saving" @click="saveDocument">
              {{ saving ? '保存中…' : '保存并冻结' }}
            </button>
            <button :disabled="saving" @click="editing = false">取消</button>
          </template>
          <LiteratureMarkdown
            v-else
            :content="displayMarkdown"
            @open-link="$emit('open-link', $event)"
          />
          <p v-if="editorNotice" role="status">{{ editorNotice }}</p>
        </template>
      </div>

      <div v-else-if="current === 'translation'" class="pane">
        <ProductEditor :rid="rid" kind="variant_zh" @saved="load" />
        <div v-if="!variant?.ready" class="pane-empty">
          {{ variant?.reason || '译文尚未生成' }}
        </div>
        <template v-else>
          <LiteratureMarkdown :content="variant.content" @open-link="$emit('open-link', $event)" />
        </template>
      </div>

      <div v-else-if="current === 'parallel'" class="pane">
        <div v-if="!bundle?.ready || !variant?.ready" class="pane-empty">
          对照阅读需要原文与译文均已生成
        </div>
        <div v-else class="parallel-cols">
          <template v-for="row in parallelRows" :key="row.index">
            <div class="parallel-para en">{{ row.original || '（该段无原文）' }}</div>
            <div class="parallel-para zh">{{ row.translated || '（该段未译）' }}</div>
          </template>
        </div>
      </div>

      <div v-else-if="current === 'images'" class="pane">
        <div v-if="!images.length" class="pane-empty">无图片（或渲染尚未完成）</div>
        <div v-else class="image-grid">
          <figure v-for="src in images" :key="src" class="image-item">
            <img :src="src" loading="lazy" alt="文献图片" @click="openImage(src)" />
          </figure>
        </div>
      </div>

      <div v-else-if="current === 'pdf'" class="pane">
        <div v-if="!pdfUrl" class="pane-empty">源 PDF 不可用</div>
        <template v-else>
          <div class="pdf-controls">
            <button @click="changeZoom(-0.25)">缩小</button>
            <span>{{ Math.round(pdfScale * 100) }}%</span>
            <button @click="changeZoom(0.25)">放大</button>
            <button @click="pdfFitWidth = true">适合宽度</button>
          </div>
          <iframe :src="scaledPdfUrl" class="pdf-frame" title="源 PDF"></iframe>
        </template>
      </div>
      <div v-else-if="current.startsWith('noteL')" class="pane">
        <ProductEditor
          :rid="rid"
          :kind="`note_${current.slice(4)}`"
          @saved="loadNote(current.slice(4))"
        />
        <div v-if="noteLoading" class="reader-loading">加载笔记中…</div>
        <template v-else-if="noteMarkdown">
          <LiteratureMarkdown :content="noteMarkdown" @open-link="$emit('open-link', $event)" />
        </template>
        <div v-else class="pane-empty">{{ noteEmptyReason }}</div>
      </div>
    </template>

    <div v-if="previewSrc" class="image-lightbox" @click="previewSrc = ''">
      <img :src="previewSrc" alt="图片预览" @click.stop />
    </div>
  </div>
</template>

<script>
import { computed, onUnmounted, ref, watch } from 'vue'
import { apiGet, apiPut } from '@/apis/base'
import LiteratureMarkdown from './LiteratureMarkdown.vue'
import ProductEditor from './ProductEditor.vue'
import { useUserStore } from '@/stores/user'
import { readReaderPreferences, writeReaderPreferences } from '@/utils/literature_reader'

const splitParas = (text) =>
  String(text || '')
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter((paragraph) => paragraph && !paragraph.startsWith('#') && !paragraph.startsWith('---'))

export default {
  name: 'ReaderTabs',
  components: { LiteratureMarkdown, ProductEditor },
  props: {
    rid: { type: String, required: true }
  },
  emits: ['document-saved', 'open-link'],
  setup(props, { emit }) {
    const userStore = useUserStore()
    const readerStorage = {
      getItem: (key) => window.localStorage.getItem(key),
      setItem: (key, value) => window.localStorage.setItem(key, value)
    }
    const preferences = readReaderPreferences(readerStorage, userStore.uid)
    const current = ref(preferences.tab)
    const fontSize = ref(preferences.fontSize)
    const loading = ref(true)
    const bundle = ref(null)
    const variant = ref(null)
    const parallel = ref(null)
    const displayMarkdown = ref('')
    const imageSources = ref([])
    const pdfUrl = ref('')
    const previewSrc = ref('')
    const objectUrls = new Set()
    let loadGeneration = 0
    let noteGeneration = 0
    let disposed = false
    const subTabs = [
      { key: 'original', label: '原文' },
      { key: 'pdf', label: 'PDF' },
      { key: 'parallel', label: '双语对照' },
      { key: 'translation', label: '中文' },
      { key: 'images', label: '图片' },
      { key: 'noteL1', label: '笔记(L1)' },
      { key: 'noteL2', label: '深度(L2)' },
      { key: 'noteL3', label: '概念关系(L3)' }
    ]

    const enc = (value) => encodeURIComponent(value)
    const pdfScale = ref(preferences.pdfScale)
    const pdfFitWidth = ref(preferences.pdfFitWidth)
    watch([current, fontSize, pdfScale, pdfFitWidth], () =>
      writeReaderPreferences(readerStorage, userStore.uid, {
        tab: current.value,
        fontSize: fontSize.value,
        pdfScale: pdfScale.value,
        pdfFitWidth: pdfFitWidth.value
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
      const originals = splitParas(bundle.value?.markdown)
      const translations = splitParas(variant.value?.content)
      const total = Math.max(originals.length, translations.length)
      return Array.from({ length: total }, (_, index) => ({
        index,
        original: originals[index] || '',
        translated: translations[index] || ''
      }))
    })
    const images = computed(() => imageSources.value)

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
      objectUrls.forEach((url) => URL.revokeObjectURL(url))
      objectUrls.clear()
      imageSources.value = []
      pdfUrl.value = ''
    }

    const hydrateProtectedAssets = async (markdown, generation) => {
      const source = String(markdown || '')
      const assetPattern = /\/api\/literature\/assets\?uri=[^)\s]+/g
      const protectedUrls = [...new Set(source.match(assetPattern) || [])]
      const resolved = (
        await Promise.all(
          protectedUrls.map(async (url) => {
            try {
              const objectUrl = await authenticatedObjectUrl(url, generation)
              return objectUrl ? [url, objectUrl] : null
            } catch {
              return null
            }
          })
        )
      ).filter(Boolean)
      if (disposed || generation !== loadGeneration) return ''
      const lookup = new Map(resolved)
      imageSources.value = resolved.map(([, objectUrl]) => objectUrl)
      return protectedUrls.reduce(
        (content, url) => content.split(url).join(lookup.get(url) || url),
        source
      )
    }

    const load = async () => {
      const generation = ++loadGeneration
      const rid = props.rid
      const isCurrent = () => !disposed && generation === loadGeneration && rid === props.rid
      loading.value = true
      releaseObjectUrls()
      bundle.value = variant.value = parallel.value = null
      displayMarkdown.value = previewSrc.value = ''
      try {
        const [readerBundle, translatedVariant, parallelVariant] = await Promise.all([
          apiGet(`/api/literature/papers/${enc(rid)}/reader`).catch(() => null),
          apiGet(`/api/literature/papers/${enc(rid)}/variants/zh`).catch(() => null),
          apiGet(`/api/literature/papers/${enc(rid)}/parallel`).catch(() => null)
        ])
        if (!isCurrent()) return
        bundle.value = readerBundle
        variant.value = translatedVariant
        parallel.value = parallelVariant
        const markdown = readerBundle?.ready
          ? await hydrateProtectedAssets(readerBundle.markdown, generation)
          : ''
        if (!isCurrent()) return
        displayMarkdown.value = markdown
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

    const openImage = (src) => {
      previewSrc.value = src
    }

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
      releaseObjectUrls()
    })

    return {
      current,
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
      displayMarkdown,
      previewSrc,
      subTabs,
      pdfUrl,
      parallelRows,
      images,
      openImage,
      load,
      loadNote
    }
  }
}
</script>

<style lang="less" scoped>
.reader-tabs {
  min-height: 320px;
}
.sub-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--gray-150);
  margin-bottom: 10px;
}
.sub-tab {
  border: none;
  background: transparent;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
  &.active {
    border-bottom: 2px solid var(--main-color);
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
  background: color-mix(in srgb, #52c41a 14%, transparent);
  color: #389e0d;
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
  border-bottom: 1px dashed rgba(0, 0, 0, 0.06);
  white-space: pre-wrap;
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
  border: 1px solid var(--gray-150);
  border-radius: 4px;
  background: transparent;
  padding: 2px 10px;
  cursor: pointer;
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
  background: rgba(0, 0, 0, 0.75);
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
</style>
