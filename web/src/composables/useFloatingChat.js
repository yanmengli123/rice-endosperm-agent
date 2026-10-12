import { ref } from 'vue'
import { message } from 'ant-design-vue'
import { agentApi, threadApi } from '@/apis'
import { useAgentStore } from '@/stores/agent'
import { useChatThreadsStore } from '@/stores/chatThreads'
import { useChatUIStore } from '@/stores/chatUI'
import { useFloatingChatStore } from '@/stores/floatingChat'
import { handleChatError } from '@/utils/errorHandler'
import { MessageProcessor } from '@/utils/messageProcessor'
import { ReasoningVisibilityBuffer } from '@/utils/reasoningVisibility'
import { processRunSseResponse } from '@/composables/useAgentRunStream'
import { compareRunSeq, normalizeRunSeq } from '@/utils/runStreamResume'
import { deriveThreadTitle } from '@/utils/threadTitle'

/**
 * 悬浮球快捷问答引擎。
 *
 * 与主聊天（AgentChatComponent + useAgentRunStream）走同一条 AgentRun + SSE 链路，
 * 但不复用其流式状态机——那套深度耦合主组件的 chatState.threadStates 与平滑缓冲。
 * 这里维护单线程的极简消费：
 *   send → (懒建线程) → createAgentRun(幂等 request_id) → streamAgentRunEvents
 *   → processRunSseResponse(纯函数复用) → 累积可见文本 → 终态拉权威历史刷新。
 * 停止语义与主聊天一致：流式中调 cancelAgentRun；断开订阅不取消服务端任务。
 */

const RUN_TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])
const RUN_RECONNECT_BASE_DELAY_MS = 500
const RUN_RECONNECT_MAX_DELAY_MS = 15000
const RUN_RECONNECT_MAX_ATTEMPTS = 6
const ACTIVE_RUN_TTL_MS = 60 * 60 * 1000
// 终态后等事务提交再拉历史（与主聊天 finalizeRunStream 的 delay 对齐）
const HISTORY_REFRESH_DELAY_MS = 200
const BALL_THREAD_TITLE_PREFIX = '快问'

// 与 useAgentRunStream 同键格式：球线程与主会话线程天然不同 id，互不冲突
const activeRunStorageKey = (threadId) => `active_run:${threadId}`

const createClientRequestId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `req-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

const extractHumanText = (historyItem) => {
  if (Array.isArray(historyItem?.content)) {
    const textPart = historyItem.content.find((part) => part?.type === 'text')
    return textPart?.text || ''
  }
  return typeof historyItem?.content === 'string' ? historyItem.content : ''
}

export function useFloatingChat() {
  const agentStore = useAgentStore()
  const chatThreadsStore = useChatThreadsStore()
  const chatUIStore = useChatUIStore()
  const floatingStore = useFloatingChatStore()

  // 面板消息模型：{ id, role: 'user'|'assistant'|'notice', content, streaming?, error?, action? }
  const items = ref([])
  const isStreaming = ref(false)
  // run 创建请求进行中（发送按钮防重复）
  const isSending = ref(false)
  const statusText = ref('')
  const threadId = ref(floatingStore.ballThreadId || '')
  const threadAgentId = ref(floatingStore.ballThreadAgentId || '')
  const lastQuestion = ref('')

  let activeRunId = null
  let runLastSeq = '0-0'
  let runAbortController = null
  let reconnectAttempts = 0
  let lastRetryableJobTry = null
  let reasoningBuffer = null
  let refreshTimer = null

  const hasThread = () => Boolean(threadId.value)

  // ==================== 本地快照（跨刷新续流） ====================

  const saveActiveRunSnapshot = () => {
    if (!threadId.value || !activeRunId) return
    localStorage.setItem(
      activeRunStorageKey(threadId.value),
      JSON.stringify({
        run_id: activeRunId,
        last_seq: normalizeRunSeq(runLastSeq),
        created_at: Date.now(),
        client_id: 'web-floating-ball'
      })
    )
  }

  const loadActiveRunSnapshot = () => {
    const raw = localStorage.getItem(activeRunStorageKey(threadId.value))
    if (!raw) return null
    try {
      return JSON.parse(raw)
    } catch {
      return null
    }
  }

  const clearActiveRunSnapshot = () => {
    if (threadId.value) localStorage.removeItem(activeRunStorageKey(threadId.value))
  }

  // ==================== 消息项维护 ====================

  const appendAssistantText = (delta) => {
    const last = items.value[items.value.length - 1]
    if (last?.role === 'assistant' && last.streaming) {
      last.content += delta
      return
    }
    items.value.push({
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      content: delta,
      streaming: true
    })
  }

  const markLastAssistantDone = (error = false) => {
    for (let i = items.value.length - 1; i >= 0; i -= 1) {
      const item = items.value[i]
      if (item.role === 'assistant') {
        item.streaming = false
        item.error = error
        return
      }
      if (item.role === 'user') return
    }
  }

  const pushNotice = (content) => {
    const last = items.value[items.value.length - 1]
    if (last?.role === 'notice' && last.content === content) return
    items.value.push({ id: `notice-${Date.now()}`, role: 'notice', content, action: 'open-main' })
  }

  const rebuildFromHistory = (history) => {
    const next = []
    for (const entry of history || []) {
      if (entry?.type === 'human') {
        const text = extractHumanText(entry)
        if (text) next.push({ id: entry.id || `user-${next.length}`, role: 'user', content: text })
      } else if (entry?.type === 'ai') {
        const body = MessageProcessor.parseAssistantMessageBody(entry)
        if (body.content) {
          next.push({ id: entry.id || `assistant-${next.length}`, role: 'assistant', content: body.content })
        }
      }
    }
    items.value = next
  }

  const refreshHistory = async () => {
    const id = threadId.value
    if (!id) return
    const response = await agentApi.getAgentHistory(id)
    if (threadId.value !== id) return
    rebuildFromHistory(response?.history || [])
  }

  const scheduleRefreshHistory = (delay = HISTORY_REFRESH_DELAY_MS) => {
    clearTimeout(refreshTimer)
    refreshTimer = setTimeout(() => {
      refreshHistory().catch((error) => handleChatError(error, 'load'))
    }, delay)
  }

  // ==================== SSE 消费 ====================

  const stopSubscription = () => {
    if (runAbortController) {
      runAbortController.abort()
      runAbortController = null
    }
  }

  // run 被服务端挂起等待人工输入（审批/追问）：球内无法交互，提示转主界面。
  // 各操作幂等（chunk 中断与 end 事件可能先后触发；resume 恢复时无活跃流也会进入）。
  const handleInterrupted = (noticeText) => {
    stopSubscription()
    activeRunId = null
    runLastSeq = '0-0'
    isStreaming.value = false
    statusText.value = ''
    markLastAssistantDone(false)
    clearActiveRunSnapshot()
    pushNotice(noticeText || '本轮需要人工确认或补充信息，请到主界面继续')
    scheduleRefreshHistory()
  }

  const finalizeRun = (status) => {
    if (!activeRunId) return
    activeRunId = null
    runLastSeq = '0-0'
    reconnectAttempts = 0
    isStreaming.value = false
    statusText.value = ''
    clearActiveRunSnapshot()
    markLastAssistantDone(status === 'failed')
    scheduleRefreshHistory()
  }

  const consumeChunk = (chunk) => {
    if (!chunk || typeof chunk !== 'object') return
    switch (chunk.status) {
      case 'init':
        statusText.value = '已接收，等待处理…'
        return
      case 'loading': {
        const streamEvent = chunk.stream_event
        if (streamEvent && typeof streamEvent === 'object') {
          if (streamEvent.type === 'message_delta' && typeof streamEvent.content === 'string') {
            const visibleDelta = reasoningBuffer?.feed(streamEvent.content) || ''
            if (visibleDelta) {
              appendAssistantText(visibleDelta)
              statusText.value = ''
            }
          } else {
            // tool_call / tool_call_delta：球不渲染工具过程，仅提示进行中
            statusText.value = '正在调用工具…'
          }
          return
        }
        const msg = chunk.msg
        if (msg && typeof msg === 'object' && !msg.event && typeof msg.content === 'string') {
          const visibleDelta = reasoningBuffer?.feed(msg.content) || ''
          if (visibleDelta) {
            appendAssistantText(visibleDelta)
            statusText.value = ''
          }
        }
        return
      }
      case 'stream_event':
        if (chunk.event?.method === 'tools') statusText.value = '正在执行工具…'
        return
      case 'progress':
        statusText.value =
          typeof chunk.message === 'string' && chunk.message.trim()
            ? chunk.message.trim()
            : '服务端正在处理请求…'
        return
      case 'warning':
        if (chunk.message) message.warning(chunk.message)
        return
      case 'interrupted':
        handleInterrupted(
          chunk.message ? `${chunk.message}（可到主界面继续）` : '本轮被中断，可到主界面继续'
        )
        return
      case 'human_approval_required':
      case 'ask_user_question_required':
        handleInterrupted('该问题需要交互确认，请到主界面继续')
        return
      case 'error':
        handleChatError({ message: chunk.message || '回答失败' }, 'stream')
        return
      default:
        // finished / agent_state / citation_ready / followup_suggestions 等
        // 终态由 end 事件驱动，其余投影球内不渲染
        return
    }
  }

  const scheduleReconnect = (runId) => {
    if (activeRunId !== runId) return false
    reconnectAttempts += 1
    if (reconnectAttempts > RUN_RECONNECT_MAX_ATTEMPTS) return false
    const backoff = Math.min(
      RUN_RECONNECT_BASE_DELAY_MS * 2 ** (reconnectAttempts - 1),
      RUN_RECONNECT_MAX_DELAY_MS
    )
    statusText.value = '连接中断，正在重连…'
    setTimeout(() => {
      if (activeRunId === runId && !runAbortController) {
        void startStream(runId, runLastSeq)
      }
    }, backoff)
    return true
  }

  const startStream = async (runId, afterSeq = '0-0') => {
    if (!threadId.value || !runId) return
    stopSubscription()
    const controller = new AbortController()
    runAbortController = controller
    activeRunId = runId
    runLastSeq = normalizeRunSeq(afterSeq)
    reconnectAttempts = 0
    isStreaming.value = true
    saveActiveRunSnapshot()
    let sawTerminalEvent = false

    try {
      const response = await agentApi.streamAgentRunEvents(runId, runLastSeq, {
        signal: controller.signal
      })
      if (!response.ok) throw new Error(`SSE response not ok: ${response.status}`)

      await processRunSseResponse(response, (event, data, eventId) => {
        if (!data || activeRunId !== runId) return
        reconnectAttempts = 0

        if (eventId) {
          const incomingSeq = normalizeRunSeq(eventId)
          if (compareRunSeq(incomingSeq, runLastSeq) > 0) {
            runLastSeq = incomingSeq
            saveActiveRunSnapshot()
          }
        }

        const payload = data.payload || {}
        if (event === 'trace') return

        const isRetryableError =
          event === 'error' && (payload?.retryable === true || payload?.chunk?.retryable === true)
        if (isRetryableError) {
          const jobTry = Number.parseInt(payload?.chunk?.job_try, 10)
          if (jobTry === lastRetryableJobTry) return
          lastRetryableJobTry = jobTry
          statusText.value = '服务波动，正在自动重试…'
          return
        }

        const chunks = Array.isArray(payload.items)
          ? payload.items
          : payload.chunk
            ? [payload.chunk]
            : []
        chunks.forEach((chunk) => consumeChunk(chunk))

        if (event === 'end') {
          sawTerminalEvent = true
          const terminalStatus = payload.status
          if (terminalStatus === 'interrupted') {
            handleInterrupted()
          } else if (RUN_TERMINAL_STATUSES.has(terminalStatus)) {
            finalizeRun(terminalStatus)
          } else {
            finalizeRun('completed')
          }
        } else if (event === 'error') {
          sawTerminalEvent = true
          finalizeRun('failed')
        }
      })

      if (!sawTerminalEvent && !controller.signal.aborted && activeRunId === runId) {
        try {
          const runRes = await agentApi.getAgentRun(runId)
          const run = runRes?.run
          if (run?.status === 'interrupted') {
            handleInterrupted()
          } else if (run && RUN_TERMINAL_STATUSES.has(run.status)) {
            finalizeRun(run.status)
          } else if (!scheduleReconnect(runId)) {
            finalizeRun('failed')
          }
        } catch {
          if (!scheduleReconnect(runId)) finalizeRun('failed')
        }
      }
    } catch (error) {
      if (error?.name !== 'AbortError') {
        console.error('悬浮球 Run SSE stream error:', error)
        if (reconnectAttempts === 0) handleChatError(error, 'stream')
        if (!scheduleReconnect(runId)) {
          if (reconnectAttempts > 0) handleChatError(error, 'stream')
          finalizeRun('failed')
        }
      }
    } finally {
      if (runAbortController === controller) runAbortController = null
      if (!activeRunId) isStreaming.value = false
    }
  }

  // ==================== 发送 / 停止 / 换新线程 ====================

  const send = async (rawText) => {
    const text = String(rawText || '').trim()
    if (!text || isStreaming.value || isSending.value) return

    if (!threadId.value) {
      const agentId = agentStore.selectedAgentId
      if (!agentId) {
        message.warning('智能体加载中，请稍后再试')
        return
      }
      try {
        isSending.value = true
        const title = `${BALL_THREAD_TITLE_PREFIX}·${deriveThreadTitle(text) || '新的对话'}`
        const thread = await threadApi.createThread(agentId, title, { source: 'floating_ball' })
        if (!thread?.id) throw new Error('创建会话失败：缺少线程 id')
        threadId.value = thread.id
        threadAgentId.value = agentId
        floatingStore.setBallThread(thread.id, agentId)
        // 主界面会话列表即时可见（真实线程，透明可审计）
        chatThreadsStore.upsertThread(thread)
      } catch (error) {
        handleChatError(error, 'create')
        return
      } finally {
        isSending.value = false
      }
    }

    const requestId = createClientRequestId()
    lastQuestion.value = text
    items.value.push({ id: requestId, role: 'user', content: text })
    items.value.push({ id: `assistant-${requestId}`, role: 'assistant', content: '', streaming: true })
    reasoningBuffer = new ReasoningVisibilityBuffer()
    statusText.value = '思考中…'
    isStreaming.value = true

    try {
      isSending.value = true
      const runResp = await agentApi.createAgentRun({
        query: text,
        agent_slug: threadAgentId.value || agentStore.selectedAgentId,
        thread_id: threadId.value,
        meta: { request_id: requestId, client: 'web-floating-ball' },
        // 仅在用户手动选过模型时覆盖（与主聊天同口径），否则走智能体默认
        model_spec: chatUIStore.selectedModelSpec || null
      })
      const runId = runResp?.run_id
      if (!runId) throw new Error('创建 run 失败：缺少 run_id')
      await startStream(runId, '0-0')
    } catch (error) {
      isStreaming.value = false
      statusText.value = ''
      markLastAssistantDone(true)
      handleChatError(error, 'send')
    } finally {
      isSending.value = false
    }
  }

  const stop = async () => {
    if (!activeRunId) return
    try {
      await agentApi.cancelAgentRun(activeRunId)
      message.info('已发送取消请求')
    } catch (error) {
      handleChatError(error, 'stop')
    }
  }

  const retry = () => {
    if (isStreaming.value || isSending.value || !lastQuestion.value) return
    void send(lastQuestion.value)
  }

  const startNewThread = () => {
    // 断开订阅但保留服务端任务与本地快照：切回主界面仍可续上
    stopSubscription()
    activeRunId = null
    runLastSeq = '0-0'
    reconnectAttempts = 0
    isStreaming.value = false
    statusText.value = ''
    threadId.value = ''
    threadAgentId.value = ''
    lastQuestion.value = ''
    floatingStore.resetBallThread()
    items.value = []
  }

  // ==================== 恢复 / 释放 ====================

  // 挂载或 keep-alive 激活时调用：回放历史 + 续接仍在跑的 run。
  // 续流恒从 '0-0' 重放（球无平滑缓冲，全量事件重建答案文本）。
  // keep-alive 首次挂载时 onMounted/onActivated 会先后触发，用进行中标志去重。
  let resuming = false
  const resume = async () => {
    if (isStreaming.value || resuming) return
    resuming = true
    try {
      try {
        await refreshHistory()
      } catch (historyError) {
        if (historyError?.status === 404) {
          // 线程已在服务端被删除（如用户从主界面侧栏删掉快问会话）：重置球会话，下次提问重新建线程
          threadId.value = ''
          threadAgentId.value = ''
          floatingStore.resetBallThread()
          items.value = []
          lastQuestion.value = ''
          clearActiveRunSnapshot()
          return
        }
      }

      const id = threadId.value
      if (!id) return

      let runId = null
      const snapshot = loadActiveRunSnapshot()
      if (snapshot?.run_id) {
        if (Date.now() - Number(snapshot.created_at || 0) <= ACTIVE_RUN_TTL_MS) {
          runId = snapshot.run_id
        } else {
          clearActiveRunSnapshot()
        }
      }
      if (!runId) {
        try {
          const active = await agentApi.getThreadActiveRun(id)
          const run = active?.run
          if (run && !RUN_TERMINAL_STATUSES.has(run.status) && run.status !== 'interrupted') {
            runId = run.id
          }
        } catch {
          // 无活跃 run：正常态，忽略
        }
      }
      if (!runId) return

      try {
        const runRes = await agentApi.getAgentRun(runId)
        const status = runRes?.run?.status
        if (!status || RUN_TERMINAL_STATUSES.has(status)) {
          clearActiveRunSnapshot()
          return
        }
        if (status === 'interrupted') {
          clearActiveRunSnapshot()
          handleInterrupted()
          return
        }
        reasoningBuffer = new ReasoningVisibilityBuffer()
        items.value.push({
          id: `assistant-resume-${runId}`,
          role: 'assistant',
          content: '',
          streaming: true
        })
        statusText.value = '恢复进行中的回答…'
        await startStream(runId, '0-0')
      } catch {
        clearActiveRunSnapshot()
      }
    } finally {
      resuming = false
    }
  }

  // 卸载或 keep-alive 失活时调用：断开订阅，快照留在 localStorage 供 resume 续流。
  const dispose = () => {
    clearTimeout(refreshTimer)
    stopSubscription()
    activeRunId = null
    runLastSeq = '0-0'
    reconnectAttempts = 0
    isStreaming.value = false
    statusText.value = ''
  }

  return {
    items,
    isStreaming,
    isSending,
    statusText,
    threadId,
    lastQuestion,
    hasThread,
    send,
    stop,
    retry,
    startNewThread,
    resume,
    dispose,
    refreshHistory
  }
}
