<template>
  <section class="sessions-workspace" aria-label="文献会话工作区">
    <header>
      <div class="actions">
        <button class="btn" :disabled="busy" @click="create('global-qa')">全库问答</button>
        <button class="btn" :disabled="busy" @click="create('lit')">AI 文献检索</button>
        <button class="btn" :disabled="busy" @click="create('manage')">管理会话</button>
        <button class="btn" :disabled="busy || !rid" @click="create('paper')">本文问答</button>
      </div>
      <div class="actions">
        <button class="btn" @click="refresh">刷新会话</button>
        <button class="btn" :disabled="busy || !selectedId" @click="rename">重命名</button>
        <button class="btn" :disabled="busy || !ready" @click="previewAnswer">
          回答写回 QA 卡片
        </button>
        <button class="btn" :disabled="busy || !selectedId" @click="archive">归档当前</button>
        <label
          ><input
            :checked="archived"
            :disabled="busy"
            type="checkbox"
            @change="state.setArchived($event.target.checked)"
          />归档会话</label
        >
      </div>
      <div class="actions">
        <label
          ><input
            type="checkbox"
            :checked="allSelected"
            :disabled="busy || !sessions.length"
            aria-label="全选会话"
            @change="toggleAll($event.target.checked)"
          />全选</label
        >
        <span>已选 {{ selectedIds.length }}</span>
        <button class="btn" :disabled="busy || !selectedIds.length" @click="deleteSelected">
          批量删除
        </button>
        <button
          v-if="archived"
          class="btn"
          :disabled="busy || !sessions.length"
          @click="clearArchived"
        >
          清空已归档
        </button>
      </div>
      <ul v-if="sessions.length" class="session-rows">
        <li v-for="session in sessions" :key="session.id" class="session-row">
          <label
            ><input
              type="checkbox"
              :checked="selectedIds.includes(session.id)"
              :disabled="busy"
              :aria-label="`选择会话 ${session.title}`"
              @change="toggleOne(session.id, $event.target.checked)"
          /></label>
          <button
            class="session-title"
            :class="{ active: session.id === selectedId }"
            :disabled="busy || archived"
            @click="selectSession(session.id)"
          >
            {{ session.title }} · {{ labels[session.session.kind] }}
          </button>
          <button v-if="archived" class="btn" :disabled="busy" @click="restore(session.id)">
            恢复
          </button>
          <button
            class="btn"
            :disabled="busy"
            :aria-label="`删除会话 ${session.title}`"
            @click="removeOne(session.id)"
          >
            删除
          </button>
        </li>
      </ul>
      <div v-else class="actions">
        <span>{{ archived ? '没有已归档会话' : '暂无会话' }}</span>
      </div>
      <p v-if="notice" role="status">{{ notice }}</p>
      <p v-if="selectedId && !ready">正在载入已绑定会话；载入前不可发送。</p>
      <details v-if="qnaPreview" open class="qna-preview">
        <summary>
          确认保存{{ qnaPreview.rid ? '本文' : '我的全局' }}问答卡片（派生笔记，不是原文证据）
        </summary>
        <MarkdownPreview :content="qnaPreview.content" />
        <button class="btn" :disabled="busy" @click="writeback">确认写回</button>
        <button class="btn" :disabled="busy" @click="qnaPreview = null">取消</button>
      </details>
    </header>
    <AgentChatComponent
      v-if="selectedId && !archived"
      ref="chat"
      :single-mode="false"
      :send-disabled="!ready"
      @thread-change="onThreadChange"
    />
    <p v-else class="empty">
      新建或选择一个文献会话。此处使用 Yuxi 原生聊天、模型配置、执行轨迹与审批。
    </p>
  </section>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AgentChatComponent from '@/components/AgentChatComponent.vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { literatureWorkspaceApi as api } from '@/apis/literature_api'
import { useLiteratureSessions } from '@/composables/useLiteratureSessions'
const props = defineProps({
  rid: { type: String, default: '' },
  threadId: { type: String, default: '' },
  sessionState: { type: Object, default: null },
  canChangeThread: { type: Function, default: () => true }
})
const emit = defineEmits(['thread-selected'])
const labels = { paper: '本文', 'global-qa': '全库问答', manage: '管理', lit: '目录检索' }
// The desk owns list/selection state; standalone usage owns its own model.
const state = props.sessionState || useLiteratureSessions()
const {
  sessions,
  selectedId,
  selectedIds,
  archived,
  busy,
  notice,
  allSelected,
  perform,
  toggleAll,
  toggleOne,
  removeOne,
  deleteSelected,
  clearArchived,
  restore
} = state
const ready = ref(false)
const chat = ref(null)
const qnaPreview = ref(null)
let generation = 0
let activationQueue = Promise.resolve()
let disposed = false
const refresh = async () => {
  ready.value = false
  await state.refresh()
  if (!disposed && selectedId.value) await activate()
}
const activate = async () => {
  const ticket = ++generation
  ready.value = false
  qnaPreview.value = null
  const id = selectedId.value
  if (!id || archived.value) return
  // The native chat shares stores. Serialize selection so an older load cannot
  // finish after a newer selection and silently switch the send target back.
  activationQueue = activationQueue.then(async () => {
    if (ticket !== generation || disposed) return
    await nextTick()
    try {
      const loaded = await chat.value?.selectThreadFromRoute(id)
      if (ticket !== generation || disposed) return
      ready.value = !!loaded
      if (!loaded) notice.value = '会话未载入或已归档，请刷新列表。'
      else {
        const session = sessions.value.find((item) => item.id === id)?.session
        emit('thread-selected', { id, rid: session?.kind === 'paper' ? session.rid : '' })
      }
    } catch (error) {
      if (ticket === generation && !disposed) notice.value = error.message || '会话载入失败'
    }
  })
  await activationQueue
}
const selectSession = (id) => {
  if (busy.value || id === selectedId.value || !props.canChangeThread()) return
  selectedId.value = id
}
const create = (kind) => {
  if (!props.canChangeThread()) return
  return perform(async () => {
    const data = await api.createSession(kind, kind === 'paper' ? props.rid : '')
    if (disposed) return
    archived.value = false
    await refresh()
    selectedId.value = data.thread.id
  })
}
const rename = () =>
  perform(async () => {
    const current = sessions.value.find((session) => session.id === selectedId.value)
    const title = window.prompt('会话标题', current?.title || '')
    if (!title?.trim()) return
    await api.updateSession(selectedId.value, { title: title.trim() })
    await refresh()
  })
const archive = () =>
  perform(async () => {
    if (!window.confirm('归档当前会话？历史与用量记录会保留。')) return
    await api.updateSession(selectedId.value, { status: 'archived' })
    selectedId.value = ''
    await refresh()
  })
const previewAnswer = () =>
  perform(async () => {
    const id = selectedId.value
    const data = await api.qnaPreview(id)
    if (id === selectedId.value && !disposed) qnaPreview.value = { ...data, thread_id: id }
  })
const writeback = () =>
  perform(async () => {
    const preview = qnaPreview.value
    const result = await api.qnaWriteback(preview.thread_id, {
      message_id: preview.message_id,
      expected_digest: preview.digest
    })
    qnaPreview.value = null
    notice.value = result.reused
      ? '该回答已写回，未重复创建。'
      : '问答卡片已写回；本文卡片可参与下次编译，全局卡片仅归当前用户。'
  })
const onThreadChange = (id) => {
  // thread-change fires before messages/configuration finish loading.
  if (!id || id !== selectedId.value) ready.value = false
}
watch(selectedId, activate, { flush: 'sync' })
watch(archived, activate, { flush: 'sync' })
watch(
  () => props.threadId,
  async (id) => {
    if (!id || id === selectedId.value) return
    archived.value = false
    await refresh()
    if (sessions.value.some((session) => session.id === id)) selectedId.value = id
  },
  { immediate: true }
)
onMounted(() => {
  refresh()
})
onBeforeUnmount(() => {
  disposed = true
  generation++
  if (!props.sessionState) state.dispose()
})
</script>

<style lang="less" scoped>
.sessions-workspace {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 600px;
  height: 76vh;
}
header {
  flex-shrink: 0;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0;
  align-items: center;
}
select {
  flex: 1;
  min-width: 180px;
  max-width: 420px;
  color: var(--color-text-primary);
  background: var(--color-bg-container);
  padding: 6px;
}
.session-rows {
  list-style: none;
  padding: 0;
  margin: 0 0 8px;
  max-height: 30vh;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.session-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 6px;
  border-radius: 6px;
  &:hover {
    background: var(--color-bg-elevated);
  }
}
.session-title {
  flex: 1;
  min-width: 0;
  text-align: left;
  border: 1px solid var(--gray-150);
  background: var(--color-bg-container);
  color: var(--color-text-primary);
  border-radius: 6px;
  padding: 4px 10px;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  &.active {
    border-color: var(--main-color);
    font-weight: 600;
  }
  &:disabled {
    cursor: default;
  }
}
.empty {
  margin: auto;
  color: var(--color-text-secondary);
}
.qna-preview {
  max-height: 42vh;
  overflow: auto;
  border: 1px solid var(--gray-150);
  padding: 10px;
}
:deep(.chat-container) {
  flex: 1;
  min-height: 0;
  height: auto;
}
</style>
