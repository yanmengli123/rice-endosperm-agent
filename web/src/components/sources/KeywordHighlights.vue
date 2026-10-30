<template>
  <span class="keyword-highlights">
    <span v-for="(fragment, index) in fragments" :key="index" class="keyword-fragment">
      <template v-for="(part, partIndex) in fragment" :key="partIndex">
        <mark v-if="part.matched === true">{{ part.text }}</mark>
        <template v-else>{{ part.text }}</template>
      </template>
    </span>
  </span>
</template>

<script setup>
defineProps({ fragments: { type: Array, default: () => [] } })
</script>

<style scoped>
.keyword-fragment {
  display: block;
  white-space: pre-wrap;
}
mark {
  color: inherit;
  background: var(--main-light-3, #fff1b8);
  border-radius: 2px;
}
</style>
