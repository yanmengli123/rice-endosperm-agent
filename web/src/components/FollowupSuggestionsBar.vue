<template>
  <div class="followup-bar" role="group" aria-label="推荐的后续问题">
    <button
      v-for="question in suggestions"
      :key="question"
      type="button"
      class="followup-bar__chip"
      :title="question"
      @click="emit('select', question)"
    >
      {{ question }}
    </button>
  </div>
</template>

<script setup>
defineProps({
  suggestions: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits(['select'])
</script>

<style lang="less" scoped>
.followup-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 4px 0 12px 44px;
}

.followup-bar__chip {
  max-width: 100%;
  padding: 5px 14px;
  border-radius: 999px;
  border: 1px solid var(--main-100);
  background: var(--main-50);
  color: var(--main-700);
  font-size: 13px;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition:
    border-color 0.2s ease,
    background-color 0.2s ease;

  &:hover {
    border-color: var(--main-300);
    background: var(--main-100);
  }
}
</style>
