<template>
  <div class="note-panel">
    <div class="level-tabs">
      <button
        v-for="level in levels"
        :key="level.key"
        class="level-tab"
        :class="{ active: currentLevel === level.key, disabled: !statusOf(level.key) }"
        :disabled="!statusOf(level.key)"
        @click="selectLevel(level.key)"
      >
        {{ level.label }}<span v-if="statusOf(level.key)" class="level-status">{{ statusOf(level.key) }}</span>
      </button>
    </div>
    <div v-if="loadingNote" class="note-loading">笔记加载中…</div>
    <div v-else-if="noteMarkdown" class="note-markdown">{{ noteMarkdown }}</div>
    <div v-else class="note-empty">该级别尚未编译（价值分达阈值后自动升级，或手动触发编译）</div>
  </div>
</template>

<script>
import { ref, onUnmounted } from 'vue'
import { getLiteratureNote, fetchLiteratureAssetText } from '@/apis/literature_api'

export default {
  name: 'NotePanel',
  props: {
    rid: { type: String, required: true },
    compileJobs: { type: Array, default: () => [] }
  },
  setup(props) {
    const levels = [
      { key: 'L1', label: 'L1 笔记' },
      { key: 'L2', label: 'L2 深度' },
      { key: 'L3', label: 'L3 关系' }
    ]
    const currentLevel = ref('L1')
    const noteMarkdown = ref('')
    const loadingNote = ref(false)

    const statusOf = (level) => {
      const job = (props.compileJobs || []).find((item) => item.level === level)
      return job ? job.status : ''
    }

    const loadNote = async () => {
      if (!statusOf(currentLevel.value)) {
        noteMarkdown.value = ''
        return
      }
      loadingNote.value = true
      try {
        const meta = await getLiteratureNote(props.rid, currentLevel.value)
        noteMarkdown.value = meta?.note_uri ? await fetchLiteratureAssetText(meta.note_uri) : ''
      } catch (err) {
        noteMarkdown.value = ''
      } finally {
        loadingNote.value = false
      }
    }
    const selectLevel = (level) => {
      currentLevel.value = level
      loadNote()
    }

    loadNote()
    // compileJobs 变化时由父组件重载；此处按 rid 卸载清理（无定时器泄漏面）
    onUnmounted(() => {})

    return { levels, currentLevel, noteMarkdown, loadingNote, statusOf, selectLevel }
  }
}
</script>

<style lang="less" scoped>
.level-tabs {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}
.level-tab {
  border: 1px solid var(--border-color, #ddd);
  background: transparent;
  border-radius: 6px;
  padding: 4px 12px;
  cursor: pointer;
  &.active {
    background: var(--primary-color, #1677ff);
    color: #fff;
  }
  &.disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
}
.level-status {
  margin-left: 6px;
  font-size: 11px;
  opacity: 0.8;
}
.note-markdown {
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: 13px;
}
.note-empty,
.note-loading {
  padding: 20px 0;
  opacity: 0.6;
}
</style>
