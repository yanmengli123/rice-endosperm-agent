<template>
  <section v-if="groups.length" class="figure-card-group" aria-label="已验证定位的论文原图">
    <div class="figure-card-group__header">
      <span class="figure-card-group__title">论文原图</span>
      <span class="figure-card-group__hint">来自已验证定位 · 页码由后端确定</span>
    </div>
    <div class="figure-card-group__list">
      <div v-for="group in groups" :key="group.key" class="figure-card-group__figure">
        <FigureCard
          :figure="group.primary"
          :state="stateFor(group.primary)"
          :can-open-source="canOpenSource(group.primary)"
          @open-source="$emit('open-source', $event)"
        />
        <div
          v-if="group.panels.length"
          class="figure-card-group__panels"
          role="list"
          :aria-label="`${group.panels.length} 个分图`"
        >
          <button
            v-for="panel in group.panels"
            :key="figureKey(panel)"
            type="button"
            role="listitem"
            class="figure-card-group__panel"
            :title="panelTitle(panel)"
            :disabled="stateFor(panel).status !== 'ready'"
            @click="openPanelPreview(panel)"
          >
            <img
              v-if="stateFor(panel).status === 'ready'"
              :src="stateFor(panel).url"
              :alt="panelTitle(panel)"
              loading="lazy"
            />
            <span v-else class="figure-card-group__panel-fallback" aria-hidden="true">
              {{ stateFor(panel).status === 'loading' ? '…' : '×' }}
            </span>
          </button>
        </div>
      </div>
    </div>
    <Teleport to="body">
      <div
        v-if="panelPreview.visible"
        class="figure-card-group__preview"
        role="dialog"
        aria-modal="true"
        :aria-label="panelPreview.title"
        @click="closePanelPreview"
      >
        <img :src="panelPreview.url" :alt="panelPreview.title" @click.stop />
      </div>
    </Teleport>
  </section>
</template>

<script setup>
/**
 * 图卡组：持有一个 kbasset 解析会话，为本轮 citation_ready.figures 取图。
 *
 * A2 图组：同一 binding 的多资产按 role 分为 primary（合成整图/最大块，大图卡）与
 * panel（缩略条，点开放大）；数据只来自后端确定性投影，前端不做任何拼图/裁剪。
 * 取图走鉴权资产端点（Bearer），MinIO URL / 预签名 URL 永不进前端；kb_id 缺失
 * 或 URI 非法的条目直接进入错误占位（buildKnowledgeAssetUrl 的 TypeError 由解析会话
 * 内部按条目吞掉，不会让卡组崩溃）。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import FigureCard from './FigureCard.vue'
import { createAssetResolverSession } from '@/utils/asset_resolver'
import { figureAssetUri } from '@/utils/figureCard'

const props = defineProps({
  figures: { type: Array, default: () => [] },
  // 可跳转原文的 evidence_id 集合（Set）；查不到即不显示「查看原文」，不绕开证据守卫
  evidenceIds: { type: Object, default: null }
})
defineEmits(['open-source'])

const LOADING = Object.freeze({ status: 'loading', url: null })
const FAILED = Object.freeze({ status: 'error', url: null })

const session = createAssetResolverSession()
const states = ref({})
let generation = 0

const figureKey = (figure) => `${figure?.binding_id || ''}:${figure?.asset_name || ''}`

const stateFor = (figure) => {
  const uri = figureAssetUri(figure)
  return (uri && states.value[uri]) || FAILED
}

const canOpenSource = (figure) => {
  const ids = props.evidenceIds
  const evidenceId = String(figure?.evidence_id || '')
  return Boolean(evidenceId && ids && typeof ids.has === 'function' && ids.has(evidenceId))
}

// 按 binding 分组：primary 优先（后端已排序，这里只做防御性归并），panel 按阅读序
const groups = computed(() => {
  const byBinding = new Map()
  ;(Array.isArray(props.figures) ? props.figures : []).forEach((figure) => {
    if (!figure || !figureAssetUri(figure)) return
    const key = String(figure.binding_id || figureKey(figure))
    if (!byBinding.has(key)) byBinding.set(key, [])
    byBinding.get(key).push(figure)
  })
  return [...byBinding.entries()].map(([key, members]) => {
    const primary = members.find((item) => item.role === 'primary') || members[0]
    const panels = members
      .filter((item) => item !== primary)
      .sort((a, b) => Number(a.group_index ?? 0) - Number(b.group_index ?? 0))
    return { key, primary, panels }
  })
})

const panelTitle = (panel) => {
  const label = String(panel?.panel_label || '').trim()
  if (label) return `分图 ${label}`
  const index = Number(panel?.group_index)
  return Number.isInteger(index) && index >= 0 ? `分图 ${index + 1}` : '分图'
}

const panelPreview = ref({ visible: false, url: '', title: '' })
const onKeydown = (event) => {
  if (event.key === 'Escape') closePanelPreview()
}
const openPanelPreview = (panel) => {
  const state = stateFor(panel)
  if (state.status !== 'ready') return
  panelPreview.value = { visible: true, url: state.url, title: panelTitle(panel) }
  window.addEventListener('keydown', onKeydown)
}
const closePanelPreview = () => {
  panelPreview.value = { visible: false, url: '', title: '' }
  window.removeEventListener('keydown', onKeydown)
}

const resolveAll = async (figures) => {
  // asset_resolver 生命周期契约：先 abort 在途、再 revoke 旧 blob、再解析新集合
  session.abortAll()
  session.revokeAll()
  closePanelPreview()
  generation += 1
  const current = generation

  const next = {}
  const byKb = new Map()
  for (const figure of figures) {
    const uri = figureAssetUri(figure)
    if (!uri) continue
    const kbId = String(figure.kb_id || '').trim()
    if (!kbId) {
      next[uri] = FAILED
      continue
    }
    next[uri] = LOADING
    if (!byKb.has(kbId)) byKb.set(kbId, [])
    byKb.get(kbId).push(uri)
  }
  states.value = next

  await Promise.all(
    [...byKb.entries()].map(async ([kbId, uris]) => {
      let resolved
      try {
        resolved = await session.resolveAssets(uris, { kbId })
      } catch (error) {
        // abort 属于正常的图集切换；其余失败按条目降级为占位
        if (current !== generation) return
        console.warn('图卡资产解析失败:', error)
        resolved = new Map(uris.map((uri) => [uri, null]))
      }
      if (current !== generation) return
      const merged = { ...states.value }
      for (const uri of uris) {
        const url = resolved.get(uri)
        merged[uri] = url ? { status: 'ready', url } : FAILED
      }
      states.value = merged
    })
  )
}

watch(
  () => props.figures,
  (figures) => {
    resolveAll(Array.isArray(figures) ? figures : [])
  },
  { immediate: true }
)

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  session.abortAll()
  session.revokeAll()
})
</script>

<style scoped lang="less">
.figure-card-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 8px 0 4px;
}

.figure-card-group__header {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.figure-card-group__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--gray-800);
}

.figure-card-group__hint {
  font-size: 11px;
  color: var(--gray-500);
}

.figure-card-group__list {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.figure-card-group__figure {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 236px;
}

.figure-card-group__panels {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.figure-card-group__panel {
  width: 44px;
  height: 44px;
  padding: 0;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-0);
  overflow: hidden;
  cursor: zoom-in;

  &:hover:not(:disabled) {
    border-color: var(--main-200);
  }

  &:disabled {
    cursor: default;
  }

  img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
}

.figure-card-group__panel-fallback {
  display: flex;
  width: 100%;
  height: 100%;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--gray-400);
}

.figure-card-group__preview {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  background: rgba(0, 0, 0, 0.72);
  cursor: zoom-out;

  img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    border-radius: 8px;
    background: var(--gray-0);
    cursor: default;
  }
}
</style>
