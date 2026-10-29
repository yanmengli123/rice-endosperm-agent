<template>
  <div class="chat-message-content">
    <template v-for="(segment, index) in segments" :key="`${index}:${segment.type}`">
      <WidgetRenderer
        v-if="segment.type === 'widget'"
        :source="segment.content"
        :closed="segment.closed"
        :disabled="disabled || !actionHandler"
        :action-handler="actionHandler"
        :image-loader="imageLoader"
      />
      <MarkdownPreview
        v-else
        :content="segment.content"
        code-copy
        evidence-links
        figure-ref-links
        @locate-evidence="emit('locate-evidence', $event)"
        @figure-ref-click="emit('figure-ref-click', $event)"
      />
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import WidgetRenderer from './WidgetRenderer.vue'
import { splitWidgetContent } from '@/utils/widgetContent'

const props = defineProps({
  content: { type: String, default: '' },
  disabled: Boolean,
  actionHandler: { type: Function, default: null },
  imageLoader: { type: Function, default: null }
})
const emit = defineEmits(['locate-evidence', 'figure-ref-click'])
const segments = computed(() => splitWidgetContent(props.content))
</script>
