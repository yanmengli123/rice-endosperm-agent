<template>
  <div class="query-chunk-preview">
    <div ref="body" class="chunk-body" :class="{ collapsed: !expanded }">
      <KeywordHighlights v-if="!expanded && highlights?.length" :fragments="highlights" />
      <template v-else>{{ content }}</template>
    </div>
    <button
      v-if="expanded || overflowing || highlights?.length"
      type="button"
      @click="expanded = !expanded"
    >
      {{ expanded ? '收起片段' : '展开完整片段' }}
    </button>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import KeywordHighlights from './KeywordHighlights.vue'
const props = defineProps({
  content: { type: String, default: '' },
  highlights: { type: Array, default: () => [] }
})
const body = ref(null),
  expanded = ref(false),
  overflowing = ref(false)
let observer
const measure = () => {
  if (!expanded.value && body.value)
    overflowing.value = body.value.scrollHeight > body.value.clientHeight + 1
}
onMounted(() => {
  observer = new ResizeObserver(measure)
  observer.observe(body.value)
  measure()
})
onBeforeUnmount(() => observer?.disconnect())
watch(
  () => props.content,
  async () => {
    expanded.value = false
    await nextTick()
    measure()
  }
)
</script>

<style scoped lang="less">
.chunk-body {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.6;
  font-size: 13px;
}
.collapsed {
  display: -webkit-box;
  -webkit-line-clamp: 6;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
button {
  margin-top: 6px;
  cursor: pointer;
  color: var(--gray-600);
  background: transparent;
  border: 0;
  padding: 0;
}
</style>
