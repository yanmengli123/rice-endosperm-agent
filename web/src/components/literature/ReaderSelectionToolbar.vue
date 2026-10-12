<template>
  <Teleport to="body">
    <div
      v-if="selected"
      class="reader-selection-tools"
      role="toolbar"
      aria-label="文献选区助手"
      :style="position"
      @pointerdown.prevent
      @keydown.esc.prevent="clear"
    >
      <button v-for="(label, action) in actions" :key="action" @click="choose(action)">
        {{ label }}
      </button>
      <button aria-label="关闭选区工具" @click="clear">×</button>
    </div>
  </Teleport>
</template>
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { scrollAffectsSelection, selectedReaderText } from '@/utils/literature_assistant'
const props = defineProps({
  root: { type: Object, default: null },
  disabled: Boolean,
  resetKey: { type: String, default: '' }
})
const emit = defineEmits(['action'])
const actions = { explain: '解释', translate: '翻译', summarize: '总结', ask: '提问' }
const selected = ref(null)
const position = computed(() => ({
  left: `${Math.max(8, Math.min(window.innerWidth - 292, selected.value?.rect.left || 8))}px`,
  top: `${Math.max(8, Math.min(window.innerHeight - 44, (selected.value?.rect.bottom || 0) + 8))}px`
}))
const clear = () => {
  selected.value = null
}
const update = () => {
  selected.value = props.disabled ? null : selectedReaderText(window.getSelection(), props.root)
}
const choose = (action) => {
  if (!selected.value || props.disabled) return
  emit('action', { action, text: selected.value.text, surface: selected.value.surface })
  clear()
}
const key = (event) => {
  if (event.key === 'Escape') clear()
  else if (event.key === 'Shift') update()
}
// 只有「会移动选区内容」的滚动才收起工具栏（scrollAffectsSelection：root 自身、
// 内部容器或包含 root 的祖先）。左右分栏中聊天面板的流式自动滚动等兄弟区域滚动
// 与选区无关，不得误杀——此前 document 级捕获把任何滚动都当消失信号，工具栏
// 频繁闪没（用户来不及点解释/翻译/总结/提问）。判定逻辑已抽为纯函数并加单测锁定。
const onScroll = (event) => {
  if (scrollAffectsSelection(event.target, props.root)) clear()
}
watch(() => [props.disabled, props.resetKey], clear)
onMounted(() => {
  document.addEventListener('pointerup', update)
  document.addEventListener('selectionchange', update)
  document.addEventListener('keyup', key)
  document.addEventListener('scroll', onScroll, true)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerup', update)
  document.removeEventListener('selectionchange', update)
  document.removeEventListener('keyup', key)
  document.removeEventListener('scroll', onScroll, true)
})
</script>
<style scoped>
.reader-selection-tools {
  position: fixed;
  z-index: 2100;
  display: flex;
  padding: 5px;
  gap: 3px;
  background: var(--gray-0, white);
  color: var(--color-text, #222);
  border: 1px solid var(--gray-200, #ddd);
  border-radius: 9px;
  box-shadow: 0 5px 24px #0002;
}
button {
  color: inherit;
  border: 0;
  background: transparent;
  border-radius: 5px;
  cursor: pointer;
  padding: 7px 10px;
}
button:hover,
button:focus-visible {
  background: var(--gray-100, #eef2ff);
}
</style>
