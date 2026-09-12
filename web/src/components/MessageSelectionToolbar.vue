<template>
  <Teleport to="body">
    <div
      v-if="toolbarVisible"
      ref="toolbarRef"
      class="message-selection-toolbar"
      :style="{ left: `${toolbarPosition.left}px`, top: `${toolbarPosition.top}px` }"
      role="toolbar"
      aria-label="选中文本操作"
      @mousedown.prevent
    >
      <div class="selection-toolbar-actions">
        <button
          type="button"
          class="selection-toolbar-btn"
          :class="{ 'is-copied': isCopied }"
          title="复制选中文本"
          @click="handleCopy"
        >
          <Check v-if="isCopied" :size="14" />
          <Copy v-else :size="14" />
          <span>{{ isCopied ? '已复制' : '复制' }}</span>
        </button>
        <button
          type="button"
          class="selection-toolbar-btn primary"
          title="引用选中内容继续提问"
          @click="followUpSelection"
        >
          <Quote :size="14" />
          <span>追问</span>
        </button>
      </div>
      <p v-if="previewText" class="selection-toolbar-preview" :title="selectionText">
        {{ previewText }}
      </p>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { Check, Copy, Quote } from '@lucide/vue'
import { useMessageSelection } from '@/composables/useMessageSelection'
import { buildSelectionPreview } from '@/utils/selectionPopup'
import { copyTextToClipboard } from '@/utils/clipboard'

const props = defineProps({
  // 限定选区归属的聊天区域（返回 DOM 元素的函数），不传则退化为整个文档
  rootEl: {
    type: Function,
    default: () => null
  }
})

const emit = defineEmits(['follow-up'])

const isCopied = ref(false)
let copiedTimer = null

const { toolbarRef, toolbarVisible, toolbarPosition, selectionText, followUpSelection } =
  useMessageSelection({
    rootEl: () => props.rootEl(),
    onFollowUp: (text) => emit('follow-up', text)
  })

const previewText = computed(() => buildSelectionPreview(selectionText.value))

const handleCopy = async () => {
  if (!selectionText.value) return
  const copied = await copyTextToClipboard(selectionText.value)
  if (!copied) return
  isCopied.value = true
  if (copiedTimer) {
    clearTimeout(copiedTimer)
  }
  copiedTimer = setTimeout(() => {
    isCopied.value = false
    copiedTimer = null
  }, 2000)
}

onBeforeUnmount(() => {
  if (copiedTimer) {
    clearTimeout(copiedTimer)
  }
})
</script>

<style lang="less" scoped>
.message-selection-toolbar {
  position: fixed;
  z-index: 1500;
  min-width: 140px;
  max-width: 320px;
  padding: 4px;
  background: var(--gray-0);
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  box-shadow:
    0 8px 24px rgba(0, 0, 0, 0.12),
    0 2px 8px rgba(0, 0, 0, 0.06);
  user-select: none;
  animation: selection-toolbar-in 0.12s ease-out;
}

.selection-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.selection-toolbar-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 9px;
  border: none;
  border-radius: 6px;
  background: transparent;
  font-size: 13px;
  line-height: 1;
  color: var(--gray-700);
  cursor: pointer;
  transition:
    background-color 0.15s ease,
    color 0.15s ease;

  &:hover {
    background: var(--gray-50);
    color: var(--gray-900);
  }

  &.primary {
    color: var(--main-600);

    &:hover {
      background: var(--main-30);
      color: var(--main-700);
    }
  }

  &.is-copied {
    color: var(--color-success-500);
  }
}

.selection-toolbar-preview {
  margin: 2px 6px 4px;
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--gray-500);
  font-size: 12px;
  line-height: 1.5;
}

@keyframes selection-toolbar-in {
  from {
    opacity: 0;
    transform: translateY(2px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
