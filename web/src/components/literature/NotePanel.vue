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
        {{ level.label
        }}<span v-if="statusOf(level.key)" class="level-status">{{ statusOf(level.key) }}</span>
      </button>
    </div>
    <ProductEditor
      :rid="rid"
      :kind="`note_${currentLevel}`"
      @editing-change="editingChanged"
      @saved="saved"
    />
    <div v-if="loadingNote" class="note-loading">笔记加载中…</div>
    <LiteratureMarkdown
      v-else-if="noteMarkdown"
      :content="noteMarkdown"
      @open-link="$emit('open-link', $event)"
    />
    <div v-else class="note-empty">
      {{ notice || '该级别尚未编译（价值分达阈值后自动升级，或手动触发编译）' }}
    </div>
  </div>
</template>

<script>
import { ref, watch, onUnmounted } from 'vue'
import { getLiteratureNote, fetchLiteratureAssetText } from '@/apis/literature_api'
import { confirmAction } from '@/composables/useLiteratureSessions'
import LiteratureMarkdown from './LiteratureMarkdown.vue'
import ProductEditor from './ProductEditor.vue'

export default {
  name: 'NotePanel',
  components: { LiteratureMarkdown, ProductEditor },
  emits: ['open-link', 'editing-change', 'saved'],
  props: {
    rid: { type: String, required: true },
    compileJobs: { type: Array, default: () => [] }
  },
  setup(props, { emit }) {
    const levels = [
      { key: 'L1', label: 'L1 笔记' },
      { key: 'L2', label: 'L2 深度' },
      { key: 'L3', label: 'L3 关系' }
    ]
    const currentLevel = ref('L1')
    const noteMarkdown = ref('')
    const loadingNote = ref(false)
    const editing = ref(false)
    const notice = ref('')
    const editingChanged = (value) => {
      editing.value = value
      emit('editing-change', value)
    }
    const saved = () => {
      editingChanged(false)
      loadNote()
      emit('saved')
    }
    let generation = 0

    const statusOf = (level) => {
      const job = (props.compileJobs || []).find((item) => item.level === level)
      return job ? job.status : ''
    }

    const loadNote = async () => {
      if (editing.value) return
      const ticket = ++generation
      const rid = props.rid
      const level = currentLevel.value
      if (!statusOf(currentLevel.value)) {
        noteMarkdown.value = ''
        loadingNote.value = false
        return
      }
      loadingNote.value = true
      notice.value = ''
      try {
        const meta = await getLiteratureNote(rid, level)
        const content = meta?.note_uri ? await fetchLiteratureAssetText(meta.note_uri) : ''
        if (ticket === generation) noteMarkdown.value = content
      } catch (error) {
        if (ticket === generation) {
          noteMarkdown.value = ''
          notice.value = error.message || '笔记读取失败'
        }
      } finally {
        if (ticket === generation) loadingNote.value = false
      }
    }
    const selectLevel = async (level) => {
      if (level === currentLevel.value) return
      if (editing.value) {
        if (!(await confirmAction('切换笔记会放弃未保存编辑，继续？'))) return
      }
      if (level === currentLevel.value) return
      editingChanged(false)
      currentLevel.value = level
      loadNote()
    }

    watch(() => [props.rid, props.compileJobs], loadNote, { immediate: true, deep: true })
    onUnmounted(() => {
      generation++
      emit('editing-change', false)
    })

    return {
      levels,
      currentLevel,
      noteMarkdown,
      loadingNote,
      statusOf,
      selectLevel,
      editingChanged,
      saved,
      notice
    }
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
  border: 1px solid var(--gray-300);
  background: var(--gray-100);
  color: var(--color-text-secondary);
  border-radius: 6px;
  padding: 4px 12px;
  cursor: pointer;
  &:hover {
    border-color: var(--main-color);
  }
  &.active {
    background: var(--main-color);
    border-color: var(--main-color);
    color: var(--gray-0);
    font-weight: 500;
  }
  &.disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
}
.level-status {
  margin-left: 6px;
  font-size: 11px;
  color: var(--color-text-tertiary);
}
.note-markdown {
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: 13px;
}
.note-empty,
.note-loading {
  padding: 20px 0;
  color: var(--color-text-tertiary);
}
</style>
