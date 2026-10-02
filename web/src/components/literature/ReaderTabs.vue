<template>
  <div class="reader-tabs">
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

    <div v-if="loading" class="reader-loading">加载中…</div>

    <template v-else>
      <div v-if="current === 'original'" class="pane">
        <div v-if="!bundle?.ready" class="pane-empty">
          {{ bundle?.reason || '阅读视图尚未生成（请先触发渲染）' }}
        </div>
        <template v-else>
          <div v-if="bundle.resolved" class="resolved-badge">已应用人工裁决（修正版）</div>
          <MarkdownPreview :content="displayMarkdown" />
        </template>
      </div>

      <div v-else-if="current === 'translation'" class="pane">
        <div v-if="!variant?.ready" class="pane-empty">
          {{ variant?.reason || '译文尚未生成' }}
        </div>
        <MarkdownPreview v-else :content="variant.content" />
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
        <iframe v-else :src="pdfUrl" class="pdf-frame" title="源 PDF"></iframe>
      </div>
    </template>

    <div v-if="previewSrc" class="image-lightbox" @click="previewSrc = ''">
      <img :src="previewSrc" alt="图片预览" @click.stop />
    </div>
  </div>
</template>

<script>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { apiGet } from '@/apis/base'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'

const splitParas = (text) =>
  String(text || '')
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter((paragraph) => paragraph && !paragraph.startsWith('#') && !paragraph.startsWith('---'))

export default {
  name: 'ReaderTabs',
  components: { MarkdownPreview },
  props: {
    rid: { type: String, required: true }
  },
  setup(props) {
    const current = ref('original')
    const loading = ref(true)
    const bundle = ref(null)
    const variant = ref(null)
    const parallel = ref(null)
    const displayMarkdown = ref('')
    const imageSources = ref([])
    const pdfUrl = ref('')
    const previewSrc = ref('')
    const objectUrls = new Set()
    const subTabs = [
      { key: 'original', label: '原文' },
      { key: 'translation', label: '译文' },
      { key: 'parallel', label: '双语对照' },
      { key: 'images', label: '图片' },
      { key: 'pdf', label: 'PDF' }
    ]

    const enc = (value) => encodeURIComponent(value)
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

    const authenticatedObjectUrl = async (url) => {
      const response = await apiGet(url, {}, true, 'blob')
      const objectUrl = URL.createObjectURL(await response.blob())
      objectUrls.add(objectUrl)
      return objectUrl
    }

    const releaseObjectUrls = () => {
      objectUrls.forEach((url) => URL.revokeObjectURL(url))
      objectUrls.clear()
      imageSources.value = []
      pdfUrl.value = ''
    }

    const hydrateProtectedAssets = async (markdown) => {
      const source = String(markdown || '')
      const assetPattern = /\/api\/literature\/assets\?uri=[^)\s]+/g
      const protectedUrls = [...new Set(source.match(assetPattern) || [])]
      const resolved = (
        await Promise.all(
          protectedUrls.map(async (url) => {
            try {
              return [url, await authenticatedObjectUrl(url)]
            } catch {
              return null
            }
          })
        )
      ).filter(Boolean)
      const lookup = new Map(resolved)
      imageSources.value = resolved.map(([, objectUrl]) => objectUrl)
      return protectedUrls.reduce(
        (content, url) => content.split(url).join(lookup.get(url)),
        source
      )
    }

    const load = async () => {
      loading.value = true
      releaseObjectUrls()
      try {
        const [readerBundle, translatedVariant, parallelVariant] = await Promise.all([
          apiGet(`/api/literature/papers/${enc(props.rid)}/reader`).catch(() => null),
          apiGet(`/api/literature/papers/${enc(props.rid)}/variants/zh`).catch(() => null),
          apiGet(`/api/literature/papers/${enc(props.rid)}/parallel`).catch(() => null)
        ])
        bundle.value = readerBundle
        variant.value = translatedVariant
        parallel.value = parallelVariant
        displayMarkdown.value = readerBundle?.ready
          ? await hydrateProtectedAssets(readerBundle.markdown)
          : ''
        try {
          pdfUrl.value = await authenticatedObjectUrl(
            `/api/literature/papers/${enc(props.rid)}/source-pdf`
          )
        } catch {
          pdfUrl.value = ''
        }
      } finally {
        loading.value = false
      }
    }

    const openImage = (src) => {
      previewSrc.value = src
    }

    onMounted(load)
    onUnmounted(releaseObjectUrls)

    return {
      current,
      loading,
      bundle,
      variant,
      displayMarkdown,
      previewSrc,
      subTabs,
      pdfUrl,
      parallelRows,
      images,
      openImage
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
  border-bottom: 1px solid var(--border-color, #eee);
  margin-bottom: 10px;
}
.sub-tab {
  border: none;
  background: transparent;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
  &.active {
    border-bottom: 2px solid var(--primary-color, #1677ff);
    font-weight: 600;
  }
}
.reader-loading,
.pane-empty {
  padding: 28px 0;
  opacity: 0.6;
}
.resolved-badge {
  display: inline-block;
  font-size: 12px;
  border-radius: 4px;
  padding: 2px 8px;
  margin-bottom: 8px;
  background: rgba(82, 196, 26, 0.12);
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
    border: 1px solid var(--border-color, #eee);
    border-radius: 6px;
    cursor: zoom-in;
  }
}
.pdf-frame {
  width: 100%;
  height: 560px;
  border: 1px solid var(--border-color, #eee);
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
