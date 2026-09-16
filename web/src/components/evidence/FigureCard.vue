<template>
  <article
    class="figure-card"
    :class="`figure-card--${mediaStatus}`"
    :aria-label="`论文原图：${title}`"
  >
    <button
      type="button"
      class="figure-card__media"
      :disabled="mediaStatus !== 'ready'"
      :aria-label="mediaStatus === 'ready' ? '放大查看原图' : '原图暂不可用'"
      @click="openPreview"
    >
      <img v-if="mediaStatus === 'ready'" :src="state.url" :alt="title" loading="lazy" />
      <span
        v-else-if="mediaStatus === 'loading'"
        class="figure-card__skeleton"
        aria-hidden="true"
      ></span>
      <span v-else class="figure-card__fallback">原图暂不可用</span>
    </button>
    <div class="figure-card__body">
      <div class="figure-card__title" :title="title">{{ title }}</div>
      <div class="figure-card__meta">
        <span>第{{ figure.page }}页</span>
        <span v-if="figure.panel_match">· panel {{ figure.panel_match }}</span>
      </div>
      <button
        v-if="canOpenSource"
        type="button"
        class="figure-card__source"
        @click="$emit('open-source', figure)"
      >
        查看原文
      </button>
    </div>
    <Teleport to="body">
      <div
        v-if="previewVisible"
        class="figure-card-preview"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        @click="closePreview"
      >
        <img :src="state.url" :alt="title" @click.stop />
      </div>
    </Teleport>
  </article>
</template>

<script setup>
/**
 * 论文原图卡片（图卡 Phase 1 方案 A）：三态 = 加载骨架 / 原图 / 错误占位。
 *
 * 只展示后端 citation_ready.figures 的确定性投影：标题走实体题注 → 编号 → 中性文案，
 * 页码原样显示（页码权威在后端 Binding）。「查看原文」只在父级证据集中能找到
 * 同 evidence_id 的证据时出现——不绕开证据守卫自拼 fragments。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { figureCardTitle } from '@/utils/figureCard'

const props = defineProps({
  figure: { type: Object, required: true },
  // { status: 'loading' | 'ready' | 'error', url: string | null }，由 FigureCardGroup 的解析会话提供
  state: { type: Object, default: () => ({ status: 'loading', url: null }) },
  canOpenSource: { type: Boolean, default: false }
})
defineEmits(['open-source'])

const title = computed(() => figureCardTitle(props.figure))
const mediaStatus = computed(() => {
  const status = props.state?.status
  return status === 'ready' || status === 'loading' ? status : 'error'
})
const previewVisible = ref(false)

const onKeydown = (event) => {
  if (event.key === 'Escape') closePreview()
}
const openPreview = () => {
  if (mediaStatus.value !== 'ready') return
  previewVisible.value = true
  window.addEventListener('keydown', onKeydown)
}
const closePreview = () => {
  previewVisible.value = false
  window.removeEventListener('keydown', onKeydown)
}
// blob 被撤销 / 图集切换时同步关闭放大层，避免展示已 revoke 的 URL
watch(mediaStatus, (status) => {
  if (status !== 'ready') closePreview()
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<style scoped lang="less">
.figure-card {
  width: 236px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: linear-gradient(180deg, var(--gray-25) 0%, var(--gray-0) 100%);
  overflow: hidden;
  transition:
    border-color 0.18s ease,
    background 0.18s ease;

  &:hover {
    border-color: var(--main-200);
    background: var(--gray-0);
  }
}

.figure-card__media {
  display: block;
  width: 100%;
  aspect-ratio: 16 / 10;
  padding: 0;
  margin: 0;
  border: 0;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-50);
  cursor: zoom-in;
  overflow: hidden;

  &:disabled {
    cursor: default;
  }

  img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
    background: var(--gray-0);
  }
}

.figure-card__skeleton {
  display: block;
  width: 100%;
  height: 100%;
  background: linear-gradient(90deg, var(--gray-50) 0%, var(--gray-100) 50%, var(--gray-50) 100%);
  background-size: 200% 100%;
  animation: figure-card-shimmer 1.2s ease-in-out infinite;
}

@keyframes figure-card-shimmer {
  from {
    background-position: 200% 0;
  }
  to {
    background-position: -200% 0;
  }
}

.figure-card__fallback {
  display: flex;
  width: 100%;
  height: 100%;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--gray-500);
}

.figure-card__body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px 10px;
}

.figure-card__title {
  font-size: 12px;
  line-height: 1.45;
  color: var(--gray-900);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.figure-card__meta {
  font-size: 11px;
  color: var(--gray-600);
  display: flex;
  gap: 4px;
}

.figure-card__source {
  align-self: flex-start;
  padding: 0;
  border: 0;
  background: transparent;
  font-size: 12px;
  color: var(--main-600);
  cursor: pointer;

  &:hover {
    text-decoration: underline;
  }
}

.figure-card-preview {
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
