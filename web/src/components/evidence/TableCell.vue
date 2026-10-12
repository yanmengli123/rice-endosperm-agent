<template>
  <component
    :is="tag"
    :rowspan="safeRowspan > 1 ? safeRowspan : undefined"
    :colspan="safeColspan > 1 ? safeColspan : undefined"
  >
    <button
      v-if="canLocate && cell.source_selector"
      type="button"
      class="cell-source"
      title="查看单元格原文"
      @click="$emit('locate', cell.source_selector)"
    >
      {{ cell.text }}
    </button>
    <template v-else>{{ cell.text }}</template>
  </component>
</template>

<script setup>
import { computed } from 'vue'

// 受控单元格：纯文本插值 + 数值型跨度绑定（数据来自后端白名单解析，无标记语言）
const props = defineProps({
  canLocate: { type: Boolean, default: false },
  cell: {
    type: Object,
    required: true
  },
  tag: {
    type: String,
    default: 'td'
  }
})
defineEmits(['locate'])

// 历史消息可能由旧版本生成或被手工改写；展示边界再次 clamp，避免把异常跨度
// 直接交给浏览器表格布局。与服务端 _MAX_SPAN 保持一致。
const safeSpan = (value) => {
  const numeric = Number(value)
  return Number.isInteger(numeric) ? Math.max(1, Math.min(numeric, 50)) : 1
}
const safeRowspan = computed(() => safeSpan(props.cell?.rowspan))
const safeColspan = computed(() => safeSpan(props.cell?.colspan))
</script>

<style scoped>
.cell-source {
  border: 0;
  background: transparent;
  color: inherit;
  padding: 0;
  font: inherit;
  cursor: pointer;
  text-decoration: underline dotted;
}
</style>
