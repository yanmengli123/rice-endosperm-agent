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
      <!-- 原文（P1 阅读视图 / 修正版优先） -->
      <div v-if="current === 'original'" class="pane">
        <div v-if="!bundle?.ready" class="pane-empty">
          {{ bundle?.reason || '阅读视图未生成（先在详情页触发渲染）' }}
        </div>
        <template v-else>
          <div v-if="bundle.resolved" class="resolved-badge">已应用人工裁决（修正版）</div>
          <MarkdownPreview :content="bundle.markdown" />
        </template>
      </div>

      <!-- 译文（P3 zh 变体） -->
      <div v-else-if="current === 'translation'" class="pane">
        <div v-if="!variant?.ready" class="pane-empty">{{ variant?.reason || '译文尚未生成' }}</div>
        <MarkdownPreview v-else :content="variant.content" />
      </div>

      <!-- 双语对照：按空行分段对齐 -->
      <div v-else-if="current === 'parallel'" class="pane">
        <div v-if="!bundle?.ready || !variant?.ready" class="pane-empty">
          对照阅读需原文与译文均已生成
        </div>
        <div v-else class="parallel-cols">
          <div class="parallel-col">
            <div v-for="(para, i) in originalParas" :key="'en-' + i" class="parallel-para en">{{ para }}</div>
          </div>
          <div class="parallel-col">
            <div v-for="(para, i) in translatedParas" :key="'zh-' + i" class="parallel-para zh">
              {{ para || '（该段未译）' }}
            </div>
          </div>
        </div>
      </div>

      <!-- 图片 -->
      <div v-else-if="current === 'images'" class="pane">
        <div v-if="!images.length" class="pane-empty">无图片（或渲染未完成）</div>
        <div v-else class="image-grid">
          <figure v-for="src in images" :key="src" class="image-item">
            <img :src="src" loading="lazy" @click="openImage(src)" />
          </figure>
        </div>
      </div>

      <!-- 源 PDF -->
      <div v-else-if="current === 'pdf'" class="pane">
        <iframe :src="pdfUrl" class="pdf-frame" title="源 PDF"></iframe>
      </div>
    </template>

    <div v-if="previewSrc" class="image-lightbox" @click="previewSrc = ''">
      <img :src="previewSrc" @click.stop />
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { apiGet } from '@/apis/base'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'

const splitParas = (text) =>
  String(text || '')
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter((p) => p && !p.startsWith('#') && !p.startsWith('---'))

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
    const previewSrc = ref('')
    const subTabs = [
      { key: 'original', label: '原文' },
      { key: 'translation', label: '译文' },
      { key: 'parallel', label: '双语对照' },
      { key: 'images', label: '图片' },
      { key: 'pdf', label: 'PDF' }
    ]

    const enc = (v) => encodeURIComponent(v)
    const pdfUrl = computed(() => `/api/literature/papers/${enc(props.rid)}/source-pdf`)

    const originalParas = computed(() => splitParas(bundle.value?.markdown))
    const translatedParas = computed(() => splitParas(variant.value?.content))
    const images = computed(() => {
      const md = String(bundle.value?.markdown || '')
      const out = []
      const re = /!\[[^\]]*\]\((\/api\/literature\/assets\?uri=[^)]+)\)/g
      let match
      while ((match = re.exec(md)) !== null) out.push(match[1])
      return out
    })

    const load = async () => {
      loading.value = true
      try {
        const [b, v] = await Promise.all([
          apiGet(`/api/literature/papers/${enc(props.rid)}/reader`).catch(() => null),
          apiGet(`/api/literature/papers/${enc(props.rid)}/variants/zh`).catch(() => null)
        ])
        bundle.value = b
        variant.value = v
      } finally {
        loading.value = false
      }
    }
    const openImage = (src) => {
      previewSrc.value = src
    }

    onMounted(load)

    return {
      current,
      loading,
      bundle,
      variant,
      previewSrc,
      subTabs,
      pdfUrl,
      originalParas,
      translatedParas,
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
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
  font-size: 13px;
  line-height: 1.7;
}
.parallel-para {
  padding: 6px 0;
  border-bottom: 1px dashed rgba(0, 0, 0, 0.06);
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
