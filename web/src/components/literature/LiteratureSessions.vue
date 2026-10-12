<template>
  <section class="sessions-workspace" aria-label="文献会话工作区">
    <header>
      <!-- 常驻工具行（两行以内）：新建入口 + 当前会话切换；搜索/列表/批量管理收进抽屉，
           把面板高度留给聊天区（原三排按钮+常驻列表占 42% 面板高度）。 -->
      <div class="actions">
        <button class="btn" :disabled="busy" @click="create('global-qa')">全库问答</button>
        <button class="btn" :disabled="busy" @click="create('lit')">AI 文献检索</button>
        <button class="btn" :disabled="busy" @click="create('manage')">管理会话</button>
        <button
          class="btn"
          :disabled="busy || !rid"
          :title="rid ? '针对当前选中文献开启专属问答' : '请先在左侧选择一篇文献'"
          @click="create('paper')"
        >
          本文问答
        </button>
        <button
          class="btn session-toggle"
          :aria-expanded="showSessions"
          :title="currentSessionLabel"
          @click="showSessions = !showSessions"
        >
          {{ currentSessionLabel }} {{ showSessions ? '▴' : '▾' }}
        </button>
        <!-- 面板级关闭：收起整个会话分栏，把宽度让给文献阅读（宿主切回阅读工作区） -->
        <button
          class="btn panel-close"
          aria-label="关闭会话面板"
          title="关闭会话面板，全宽阅读文献"
          @click="$emit('close')"
        >
          ×
        </button>
      </div>
      <div class="actions">
        <button class="btn" :disabled="busy || !selectedId" @click="rename">重命名</button>
        <button
          class="btn"
          :disabled="busy || !ready || chatStreaming"
          :title="
            chatStreaming
              ? '回答生成中，完成后可写回'
              : ready
                ? '将本会话最近一次完整回答写回 QA 卡片'
                : '会话载入后可写回'
          "
          @click="previewAnswer"
        >
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
        <button class="btn" :disabled="busy" @click="refresh">刷新会话</button>
      </div>
      <div v-if="notice" class="notice-bar" role="status">
        <span class="notice-text">{{ notice }}</span>
        <button class="notice-close" aria-label="关闭提示" @click="notice = ''">×</button>
      </div>
      <p v-if="selectedId && !ready">正在载入已绑定会话；载入前不可发送。</p>
      <details v-if="qnaPreview" open class="qna-preview">
        <summary>
          确认保存{{ qnaPreview.rid ? '本文' : '我的全局' }}问答卡片（派生笔记，不是原文证据）
        </summary>
        <MarkdownPreview :content="qnaPreview.content" />
        <button class="btn" :disabled="busy" @click="writeback">确认写回</button>
        <button class="btn" :disabled="busy" @click="qnaPreview = null">取消</button>
      </details>
      <div v-if="showSessions" class="session-drawer">
        <div class="actions">
          <label class="search-box">
            <input
              v-model="searchText"
              :disabled="busy"
              type="search"
              placeholder="搜索会话标题与消息内容"
              aria-label="搜索文献会话"
              @keyup.enter="runSearch"
            />
          </label>
          <button class="btn" :disabled="busy" @click="runSearch">搜索</button>
          <button v-if="searchText" class="btn" :disabled="busy" @click="clearSearch">
            清除搜索
          </button>
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
            清空当前已加载归档
          </button>
          <button v-if="hasMore" class="btn" :disabled="busy" @click="loadMore">
            加载更多（当前 {{ sessions.length }}）
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
            <div class="session-main">
              <button
                class="session-title"
                :class="{ active: session.id === selectedId }"
                :disabled="busy || archived"
                :title="session.excerpt || session.title"
                @click="selectSession(session.id)"
              >
                {{ session.title }} · {{ labels[session.session.kind] }}
              </button>
              <span v-if="session.excerpt" class="session-excerpt">{{ session.excerpt }}</span>
            </div>
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
          <span>{{
            searchText
              ? '没有匹配的会话，可更换关键词或清除搜索。'
              : archived
                ? '没有已归档会话'
                : '暂无会话：点击上方「全库问答」「AI 文献检索」，或先在左侧选中文献后点「本文问答」。'
          }}</span>
        </div>
      </div>
    </header>
    <AgentChatComponent
      v-if="selectedId && !archived"
      ref="chat"
      :single-mode="false"
      :send-disabled="!ready"
      :lock-thread-creation="true"
      :paper-reader-rid="selectedPaperRid"
      @thread-change="onThreadChange"
      @paper-evidence="$emit('reader-evidence', $event)"
    />
    <div v-else class="empty">
      <p>新建或选择一个文献会话。此处使用 Yuxi 原生聊天、模型配置、执行轨迹与审批。</p>
      <p class="empty-hint">会话与智能体问答完全互通：同一套流式、引用与配额。</p>
    </div>
  </section>
</template>

<script setup>
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Modal, message } from 'ant-design-vue'
import AgentChatComponent from '@/components/AgentChatComponent.vue'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { literatureWorkspaceApi as api } from '@/apis/literature_api'
import { confirmAction, useLiteratureSessions } from '@/composables/useLiteratureSessions'
const props = defineProps({
  rid: { type: String, default: '' },
  threadId: { type: String, default: '' },
  sessionState: { type: Object, default: null },
  canChangeThread: { type: Function, default: () => true },
  readerRequest: { type: Object, default: null }
})
const emit = defineEmits(['thread-selected', 'reader-evidence', 'reader-staged', 'close'])
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
  searchText,
  hasMore,
  allSelected,
  perform,
  toggleAll,
  toggleOne,
  removeOne,
  deleteSelected,
  clearArchived,
  restore,
  runSearch,
  clearSearch,
  loadMore
} = state
const ready = ref(false)
const chat = ref(null)
const selectedPaperRid = computed(() => {
  const binding = sessions.value.find((row) => row.id === selectedId.value)?.session
  return binding?.kind === 'paper' ? binding.rid : ''
})
// 会话列表折叠：默认收起把面板高度留给聊天区；无选中会话时自动展开引导首次使用
const showSessions = ref(false)
const currentSessionLabel = computed(() => {
  const row = sessions.value.find((item) => item.id === selectedId.value)
  if (!row) return '会话列表'
  return `${labels[row.session?.kind] || '会话'}：${row.title || row.id}`
})
let stagedRequest = ''
const stageReaderRequest = async () => {
  const request = props.readerRequest
  if (!request || disposed || request.id === stagedRequest) return
  if (!ready.value) return
  const stillCurrent = state.captureScope()
  if (request.thread_id !== selectedId.value) {
    // 目标会话与当前选中不一致（用户已切走）：显式失败并清掉挂起请求，
    // 不再静默丢弃——静默丢弃在用户侧表现为「点击阅读动作没有任何反应」。
    stagedRequest = request.id
    notice.value = '阅读内容的目标会话已切换，未写入草稿；请回到对应文献会话后重试。'
    emit('reader-staged', { id: request.id, success: false, error: '目标会话已切换' })
    return
  }
  stagedRequest = request.id
  try {
    if (selectedPaperRid.value !== request.rid)
      throw new Error('阅读内容与当前本文会话不匹配，未写入草稿')
    if (!chat.value?.stagePaperReaderDraft) throw new Error('阅读助手输入区尚未就绪')
    const staged = await chat.value.stagePaperReaderDraft(request)
    if (!disposed && stillCurrent() && props.readerRequest?.id === request.id) {
      if (selectedId.value !== request.thread_id || selectedPaperRid.value !== request.rid)
        throw new Error('目标会话已切换，阅读动作已结束')
      notice.value = staged?.sent
        ? '阅读内容已发送到本文会话，回答将出现在对话历史中。'
        : '阅读内容已进入本文会话草稿，请补充问题并确认发送。'
      emit('reader-staged', { id: request.id, success: true, sent: Boolean(staged?.sent) })
    }
  } catch (error) {
    if (!disposed && stillCurrent() && props.readerRequest?.id === request.id) {
      notice.value = error.message
      emit('reader-staged', { id: request.id, success: false, error: error.message })
    }
  }
}
const qnaPreview = ref(null)
// 嵌入式聊天暴露的 isProcessing 对应当前选中会话的流式状态；生成中禁止写回预览
const chatStreaming = computed(() => Boolean(chat.value?.isProcessing))
let generation = 0
let activationQueue = Promise.resolve()
let disposed = false
let refreshGeneration = 0
const refresh = async () => {
  const ticket = ++refreshGeneration
  const stillCurrent = state.captureScope()
  ready.value = false
  await state.refresh()
  if (disposed || !stillCurrent() || ticket !== refreshGeneration) return
  restoreReaderSession()
  if (!disposed && selectedId.value) await activate()
}
const restoreReaderSession = () => {
  const request = props.readerRequest,
    row = request?.threadRecord
  if (
    !request ||
    !row ||
    archived.value ||
    disposed ||
    row.id !== request.thread_id ||
    row.session?.kind !== 'paper' ||
    row.session.rid !== request.rid ||
    (selectedId.value && selectedId.value !== request.thread_id) ||
    props.threadId !== request.thread_id
  )
    return
  // A reused server-owned paper thread may live beyond the first page. Do not
  // infer its kind/rid from a client route or silently fall back to ordinary chat.
  if (!sessions.value.some((item) => item.id === row.id)) sessions.value = [row, ...sessions.value]
  selectedId.value = row.id
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
      let loaded = await chat.value?.selectThreadFromRoute(id)
      if (ticket !== generation || disposed) return
      // 会话存在但不在共享线程列表首页（文献会话可远超 100 条）时，
      // 用本地会话记录补进 store 后按同一路径激活。
      if (!loaded) {
        const row = sessions.value.find((item) => item.id === id)
        if (row && chat.value?.selectExternalThread)
          loaded = await chat.value.selectExternalThread(row)
      }
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
const selectSession = async (id) => {
  if (busy.value || id === selectedId.value) return
  const previous = selectedId.value
  const stillCurrent = state.captureScope()
  if (!(await props.canChangeThread())) return
  if (
    disposed ||
    !stillCurrent() ||
    busy.value ||
    selectedId.value !== previous ||
    archived.value ||
    !sessions.value.some((row) => row.id === id)
  )
    return
  selectedId.value = id
}
const create = async (kind) => {
  const rid = props.rid
  const previous = selectedId.value
  const currentOwner = state.captureScope()
  if (!(await props.canChangeThread())) return
  if (disposed || !currentOwner() || props.rid !== rid || selectedId.value !== previous) return
  return perform(async (stillCurrent) => {
    const data = await api.createSession(kind, kind === 'paper' ? rid : '')
    if (disposed || !stillCurrent() || props.rid !== rid || selectedId.value !== previous) return
    archived.value = false
    await refresh()
    if (disposed || !stillCurrent() || props.rid !== rid) return
    selectedId.value = data.thread.id
  })
}
const rename = () =>
  perform(async (stillCurrent) => {
    const id = selectedId.value
    const current = sessions.value.find((session) => session.id === id)
    if (!current) return
    let title = current?.title || ''
    const confirmed = await new Promise((resolve) => {
      Modal.confirm({
        title: '重命名会话',
        content: h('div', { style: { marginTop: '12px' } }, [
          h('input', {
            value: title,
            maxlength: 200,
            'aria-label': '会话标题',
            style: {
              width: '100%',
              padding: '4px 8px',
              border: '1px solid var(--gray-150)',
              background: 'var(--gray-0)',
              borderRadius: '4px'
            },
            onInput: (event) => {
              title = event.target.value
            }
          })
        ]),
        okText: '确认',
        cancelText: '取消',
        onOk: () => {
          if (!title.trim()) {
            message.warning('标题不能为空')
            return Promise.reject()
          }
          resolve(true)
        },
        onCancel: () => resolve(false),
        afterClose: () => resolve(false)
      })
    })
    if (!confirmed) return
    if (disposed || !stillCurrent() || selectedId.value !== id) return
    await api.updateSession(id, { title: title.trim() })
    if (disposed || !stillCurrent()) return
    await refresh()
  })
const archive = () =>
  perform(async (stillCurrent) => {
    const id = selectedId.value
    if (!id) return
    if (!(await confirmAction('归档当前会话？历史与用量记录会保留。'))) return
    if (disposed || !stillCurrent() || selectedId.value !== id) return
    await api.updateSession(id, { status: 'archived' })
    if (disposed || !stillCurrent()) return
    if (selectedId.value === id) selectedId.value = ''
    await refresh()
  })
const previewAnswer = () =>
  perform(async (stillCurrent) => {
    if (chatStreaming.value) {
      message.warning('回答生成中，完成后可写回')
      return
    }
    const id = selectedId.value
    const data = await api.qnaPreview(id)
    if (id === selectedId.value && !disposed && stillCurrent())
      qnaPreview.value = { ...data, thread_id: id }
  })
const writeback = () =>
  perform(async (stillCurrent) => {
    const preview = qnaPreview.value
    if (!preview || preview.thread_id !== selectedId.value) return
    const result = await api.qnaWriteback(preview.thread_id, {
      message_id: preview.message_id,
      expected_digest: preview.digest
    })
    if (
      disposed ||
      !stillCurrent() ||
      preview.thread_id !== selectedId.value ||
      qnaPreview.value !== preview
    )
      return
    qnaPreview.value = null
    notice.value = result.reused
      ? '该回答已写回，未重复创建。'
      : '问答卡片已写回；本文卡片可参与下次编译，全局卡片仅归当前用户。'
  })
const onThreadChange = (id) => {
  // thread-change fires before messages/configuration finish loading.
  if (!id || id !== selectedId.value) ready.value = false
}
watch(
  selectedId,
  () => {
    qnaPreview.value = null
    activate()
  },
  { flush: 'sync' }
)
watch(archived, activate, { flush: 'sync' })
watch(() => props.readerRequest, restoreReaderSession, { flush: 'post' })
watch(() => [props.readerRequest, ready.value, selectedPaperRid.value], stageReaderRequest, {
  flush: 'post'
})
watch(
  () => props.threadId,
  async (id) => {
    if (!id || id === selectedId.value) return
    archived.value = false
    await refresh()
    if (!disposed && props.threadId === id && sessions.value.some((session) => session.id === id))
      selectedId.value = id
  },
  { immediate: true }
)
onMounted(() => {
  showSessions.value = !selectedId.value
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
  /* 钉在滚动容器（.lit-main，顶部偏移 48px）内并占满视口剩余高度：
     右侧文献长文滚动时问答面板恒定完整可见。 */
  position: sticky;
  top: 8px;
  height: calc(100vh - 76px);
  max-height: calc(100vh - 76px);
  min-height: min(480px, calc(100vh - 76px));
}
@media (max-width: 1100px) {
  /* 窄屏单列堆叠布局取消钉驻，恢复自然流 */
  .sessions-workspace {
    position: static;
    height: auto;
    max-height: none;
    min-height: 600px;
  }
}
header {
  flex-shrink: 0;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 6px 0 0;
  align-items: center;
  &:first-child {
    margin-top: 0;
  }
}
.session-toggle {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
  font-weight: 600;
}
.panel-close {
  flex-shrink: 0;
  font-size: 15px;
  line-height: 1;
  padding: 4px 10px;
  &:hover {
    background: var(--color-bg-elevated);
  }
}
.session-drawer {
  border-top: 1px solid var(--gray-150);
  margin-top: 8px;
  padding-top: 8px;
}
.notice-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.notice-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.notice-close {
  flex-shrink: 0;
  border: 0;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  padding: 2px 8px;
  border-radius: 4px;
  line-height: 1;
  &:hover {
    background: var(--color-bg-elevated);
    color: var(--color-text-primary);
  }
}
.search-box {
  flex: 1;
  min-width: 0;
}
.search-box input {
  width: 100%;
  min-width: 0;
  color: var(--color-text-primary);
  background: var(--color-bg-container);
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  padding: 4px 10px;
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
  max-height: 40vh;
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
.session-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
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
.session-excerpt {
  color: var(--color-text-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.empty {
  margin: auto;
  color: var(--color-text-secondary);
  text-align: center;
}
.empty-hint {
  font-size: 12px;
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
/* 全局 .chat-main 把滚动条藏了（scrollbar-width:none）；文献分栏问答需要
   可见的上下滑动条：消息区滚动、输入框钉在面板底部，互不侵占。 */
:deep(.chat-main) {
  scrollbar-width: thin;
  scrollbar-color: var(--gray-300) transparent;
  &::-webkit-scrollbar {
    width: 8px;
  }
  &::-webkit-scrollbar-thumb {
    background: var(--gray-300);
    border-radius: 4px;
  }
  &::-webkit-scrollbar-thumb:hover {
    background: var(--gray-400);
  }
  &::-webkit-scrollbar-track {
    background: transparent;
  }
}
</style>
