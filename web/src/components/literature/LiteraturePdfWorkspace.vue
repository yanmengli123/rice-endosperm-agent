<template>
  <section class="pdf-workspace" aria-label="原文 PDF">
    <p v-if="notice" role="status">{{ notice }}</p>
    <div class="pdf-panes">
      <div class="pdf-pane">
        <LiteraturePdfReader
          :url="originalUrl"
          :source="source"
          :evidence-target="evidenceTarget"
          :initial-page="page"
          :page-number="page"
          :scale="scale"
          :fit-width="fitWidth"
          @page-changed="changePage"
          @update:scale="$emit('update:scale', $event)"
          @update:fit-width="$emit('update:fitWidth', $event)"
          @assistant-action="$emit('assistant-action', $event)"
        />
      </div>
    </div>
  </section>
</template>

<script setup>
import { ref } from 'vue'
import LiteraturePdfReader from './LiteraturePdfReader.vue'

const props = defineProps({
  rid: { type: String, required: true },
  originalUrl: { type: String, required: true },
  versionToken: { type: String, default: '' },
  source: { type: Object, default: () => ({}) },
  evidenceTarget: { type: Object, default: null },
  initialPage: { type: Number, default: 1 },
  scale: { type: Number, default: 1 },
  fitWidth: { type: Boolean, default: true }
})
const emit = defineEmits(['assistant-action', 'page-changed', 'update:scale', 'update:fitWidth'])
const notice = ref('')
const page = ref(props.initialPage)
const changePage = (value) => {
  if (page.value === value) return
  page.value = value
  emit('page-changed', value)
}
</script>

<style scoped lang="less">
.pdf-workspace {
  min-width: 0;
}
.pdf-panes {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
}
.pdf-pane {
  min-width: 0;
}
</style>
