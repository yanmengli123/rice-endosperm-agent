<template>
  <section v-if="figures.length" class="figure-card-group" aria-label="已验证定位的论文原图">
    <div class="figure-card-group__header">
      <span class="figure-card-group__title">论文原图</span>
      <span class="figure-card-group__hint">来自已验证定位 · 页码由后端确定</span>
    </div>
    <div class="figure-card-group__list">
      <FigureCard
        v-for="figure in figures"
        :key="`${figure.binding_id}:${figure.asset_name}`"
        :figure="figure"
        :state="stateFor(figure)"
        :can-open-source="canOpenSource(figure)"
        @open-source="$emit('open-source', $event)"
      />
    </div>
  </section>
</template>

<script setup>
/**
 * 图卡组：持有一个 kbasset 解析会话，为本轮 citation_ready.figures 取图。
 *
 * 取图走鉴权资产端点（Bearer），MinIO URL / 预签名 URL 永不进前端；kb_id 缺失
 * 或 URI 非法的条目直接进入错误占位（buildKnowledgeAssetUrl 的 TypeError 由解析会话
 * 内部按条目吞掉，不会让卡组崩溃）。
 */
import { onBeforeUnmount, ref, watch } from 'vue'

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

const stateFor = (figure) => {
  const uri = figureAssetUri(figure)
  return (uri && states.value[uri]) || FAILED
}

const canOpenSource = (figure) => {
  const ids = props.evidenceIds
  const evidenceId = String(figure?.evidence_id || '')
  return Boolean(evidenceId && ids && typeof ids.has === 'function' && ids.has(evidenceId))
}

const resolveAll = async (figures) => {
  // asset_resolver 生命周期契约：先 abort 在途、再 revoke 旧 blob、再解析新集合
  session.abortAll()
  session.revokeAll()
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
</style>
