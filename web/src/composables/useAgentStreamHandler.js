import { message } from 'ant-design-vue'
import { handleChatError } from '@/utils/errorHandler'
import { unref } from 'vue'
import { extractPendingInterrupt } from '@/composables/useApproval'
import { ReasoningVisibilityBuffer } from '@/utils/reasoningVisibility'
import { normalizeVerifiedFigures } from '@/utils/figureCard'
import { normalizeGraphSnapshot } from '@/utils/graphSnapshot'
import { artifactPathsFromChunk } from '@/utils/runArtifacts'

const reasoningVisibilityByMessage = new Map()

const safeStreamContent = (messageId, content) => {
  if (typeof content !== 'string' || !content) return ''
  if (!reasoningVisibilityByMessage.has(messageId)) {
    if (reasoningVisibilityByMessage.size >= 256) {
      reasoningVisibilityByMessage.delete(reasoningVisibilityByMessage.keys().next().value)
    }
    reasoningVisibilityByMessage.set(messageId, new ReasoningVisibilityBuffer())
  }
  return reasoningVisibilityByMessage.get(messageId).feed(content)
}

const serializeToolArgs = (args) => {
  if (typeof args === 'string') return args
  if (args === undefined || args === null) return ''
  return JSON.stringify(args)
}

const streamEventToMessageChunk = (streamEvent) => {
  if (!streamEvent || typeof streamEvent !== 'object') return null
  const messageId = streamEvent.message_id
  if (!messageId) return null

  if (streamEvent.type === 'message_delta') {
    const chunk = {
      id: messageId,
      type: 'AIMessageChunk',
      content: safeStreamContent(messageId, streamEvent.content)
    }
    if (
      streamEvent.reasoning_state === 'thinking' ||
      streamEvent.reasoning_content ||
      streamEvent.additional_reasoning_content
    ) {
      chunk.additional_kwargs = { reasoning_state: 'thinking' }
    }
    return chunk
  }

  if (streamEvent.type === 'tool_call' || streamEvent.type === 'tool_call_delta') {
    return {
      id: messageId,
      type: 'AIMessageChunk',
      content: '',
      tool_call_chunks: [
        {
          index: streamEvent.index || 0,
          id: streamEvent.tool_call_id,
          name: streamEvent.name,
          args:
            streamEvent.type === 'tool_call_delta'
              ? streamEvent.args_delta || ''
              : serializeToolArgs(streamEvent.args)
        }
      ]
    }
  }

  return null
}

const loadingMessageChunk = (chunk) => {
  const semanticChunk = streamEventToMessageChunk(chunk?.stream_event)
  if (semanticChunk) return semanticChunk

  const msg = chunk?.msg
  if (msg?.event) return null
  if (!msg || typeof msg !== 'object') return null

  const { reasoning_content: rawReasoning, ...safeMessage } = msg
  const additionalKwargs =
    msg.additional_kwargs && typeof msg.additional_kwargs === 'object'
      ? { ...msg.additional_kwargs }
      : {}
  const additionalReasoning = additionalKwargs.reasoning_content
  delete additionalKwargs.reasoning_content
  const messageId = msg.id || chunk?.request_id || 'legacy-message'
  safeMessage.content = safeStreamContent(messageId, msg.content)
  if (rawReasoning || additionalReasoning) {
    additionalKwargs.reasoning_state = 'thinking'
  }
  if (Object.keys(additionalKwargs).length) safeMessage.additional_kwargs = additionalKwargs
  else delete safeMessage.additional_kwargs
  return safeMessage
}

// 工具结果不走 messages 流，而是以 method=tools 的 stream_event 事件返回（tool-started/tool-finished）。
// 取出 tool-finished 的 output（一条 ToolMessage 字典），交给 msgChunks 与 AI 消息按 tool_call_id 关联。
const toolFinishedMessage = (chunk) => {
  const streamEvent = chunk?.event
  if (!streamEvent || streamEvent.method !== 'tools') return null

  const data = streamEvent.data
  if (!data || data.event !== 'tool-finished') return null

  const output = data.output
  if (!output || typeof output !== 'object') return null

  const id = output.id || output.tool_call_id || data.tool_call_id
  if (!id) return null
  return { ...output, type: 'tool', id }
}

export function useAgentStreamHandler({
  getThreadState,
  processApprovalInStream,
  currentAgentId,
  supportsFiles,
  streamSmoother
}) {
  const debugPrefix = '[AgentStateDebug]'
  /**
   * Process a single stream chunk based on its status
   * @param {Object} chunk - The parsed JSON chunk
   * @param {String} threadId - The current thread ID
   * @returns {Boolean} - Returns true if processing should stop (e.g. error, finished, interrupted)
   */
  const handleStreamChunk = (chunk, threadId) => {
    const { status, msg, request_id, message: chunkMessage } = chunk
    const threadState = getThreadState(threadId)

    if (!threadState) return false

    switch (status) {
      case 'init':
        {
          const resolvedRequestId = request_id || threadState.pendingRequestId
          if (resolvedRequestId) {
            threadState.pendingRequestId = resolvedRequestId
          }
          if (resolvedRequestId && msg && msg.type !== 'system') {
            const localHumanMessage = threadState.onGoingConv.msgChunks[resolvedRequestId]?.find(
              (item) => item?.type === 'human' || item?.role === 'user'
            )
            const initMessage = {
              ...msg,
              id: msg?.id || resolvedRequestId,
              extra_metadata: {
                ...(msg?.extra_metadata || {}),
                request_id: resolvedRequestId
              }
            }
            if (localHumanMessage?.image_content && !initMessage.image_content) {
              initMessage.message_type = localHumanMessage.message_type || initMessage.message_type
              initMessage.image_content = localHumanMessage.image_content
            }
            threadState.onGoingConv.msgChunks[resolvedRequestId] = [initMessage]
          }
          threadState.replyLoadingVisible = true
          threadState.replyLoadingMessage = ''
          threadState.contextCompressing = false
        }
        return false

      case 'loading':
        {
          threadState.replyLoadingMessage = ''
          const messageChunk = loadingMessageChunk(chunk)
          if (messageChunk?.id) {
            messageChunk.run_id = chunk.run_id || messageChunk.run_id
            messageChunk.thread_id = threadId || messageChunk.thread_id
            messageChunk.extra_metadata = {
              ...(messageChunk.extra_metadata || {}),
              ...(chunk.run_id ? { run_id: chunk.run_id } : {}),
              ...(chunk.request_id ? { request_id: chunk.request_id } : {}),
              ...(threadId ? { thread_id: threadId } : {})
            }
            if (streamSmoother) {
              streamSmoother.pushChunk(messageChunk, threadId)
            } else {
              if (!threadState.onGoingConv.msgChunks[messageChunk.id]) {
                threadState.onGoingConv.msgChunks[messageChunk.id] = []
              }
              threadState.onGoingConv.msgChunks[messageChunk.id].push(messageChunk)
            }
          }
        }
        return false

      case 'progress':
        threadState.replyLoadingVisible = true
        threadState.replyLoadingMessage =
          typeof chunkMessage === 'string' && chunkMessage.trim()
            ? chunkMessage.trim()
            : '服务端正在处理请求…'
        return false

      case 'stream_event':
        {
          // 工具结果需立即落地（不经平滑层），写入 msgChunks 后由 convertToolResultToMessages
          // 按 tool_call_id 关联到对应 AI 消息的 tool_call，驱动其完成态。
          const toolMessage = toolFinishedMessage(chunk)
          if (toolMessage) {
            if (!threadState.onGoingConv.msgChunks[toolMessage.id]) {
              threadState.onGoingConv.msgChunks[toolMessage.id] = []
            }
            threadState.onGoingConv.msgChunks[toolMessage.id].push(toolMessage)
          }
        }
        return false

      case 'error':
        streamSmoother?.flushThread(threadId)
        handleChatError({ message: chunkMessage }, 'stream')
        // Stop the loading indicator
        if (threadState) {
          threadState.isStreaming = false
          threadState.replyLoadingVisible = false
          threadState.replyLoadingMessage = ''
          threadState.pendingRequestId = null
          threadState.pendingInterrupt = null
          threadState.contextCompressing = false
        }
        return true

      case 'ask_user_question_required':
      case 'human_approval_required':
        streamSmoother?.flushThread(threadId)
        threadState.replyLoadingVisible = false
        threadState.replyLoadingMessage = ''
        console.log(`${debugPrefix}[approval_required]`, {
          threadId,
          currentAgentId: unref(currentAgentId)
        })
        // 使用审批 composable 处理审批请求
        return processApprovalInStream(chunk, threadId, unref(currentAgentId))

      case 'agent_state':
        console.log(`${debugPrefix}[agent_state_chunk]`, {
          threadId,
          supportsFiles: unref(supportsFiles),
          currentAgentId: unref(currentAgentId),
          hasAgentState: !!chunk.agent_state,
          todoCount: Array.isArray(chunk.agent_state?.todos) ? chunk.agent_state.todos.length : 0,
          uploadCount: Array.isArray(chunk.agent_state?.uploads)
            ? chunk.agent_state.uploads.length
            : 0
        })
        if (chunk.agent_state) {
          console.log(`${debugPrefix}[agent_state_apply]`, {
            threadId,
            todos: chunk.agent_state?.todos || [],
            uploads: chunk.agent_state?.uploads || []
          })
          threadState.agentState = chunk.agent_state
        } else {
          console.warn(`${debugPrefix}[agent_state_skip]`, {
            reason: 'empty_state',
            supportsFiles: unref(supportsFiles),
            hasAgentState: !!chunk.agent_state,
            currentAgentId: unref(currentAgentId),
            threadId
          })
        }
        return false

      case 'context_compression':
        if (chunk.compression) {
          threadState.contextCompressing = chunk.compression.status === 'started'
        }
        return false

      case 'citation_ready': {
        // Binding is already final and server-verified. Keep the structured
        // fact for diagnostics/UI consumers; never re-resolve it in-browser.
        threadState.verifiedCitation = chunk.citation || null
        // 图卡只接受后端确定性投影（figures 字段缺席 ⟺ 未发布，此时清空）
        const figures = normalizeVerifiedFigures(chunk.figures)
        threadState.verifiedFigures = figures
        // 答案气泡内图卡：按 run 暂存（新一轮 resetRunEvidence 不清），让上一条答案的
        // 图卡在历史回读前不消失
        const runId = String(chunk.run_id || threadState.activeRunId || '')
        if (runId && figures.length) {
          threadState.figuresByRun = { ...(threadState.figuresByRun || {}), [runId]: figures }
        }
        return false
      }

      case 'graph_snapshot_ready': {
        const snapshot = normalizeGraphSnapshot(chunk.graph_snapshot)
        threadState.verifiedGraphSnapshot = snapshot
        const runId = String(chunk.run_id || threadState.activeRunId || '')
        if (runId && snapshot) {
          threadState.graphsByRun = { ...(threadState.graphsByRun || {}), [runId]: snapshot }
        }
        return false
      }

      case 'locator_candidates':
        // 跨文献歧义：后端只给文档身份（file_id/kb_id/filename，无页码无图），前端渲染成
        // 可点选的候选文献，点选后以 @doc 提及重新提问（确定性硬约束）
        threadState.locatorCandidates = Array.isArray(chunk.candidates)
          ? chunk.candidates.filter((item) => item && typeof item === 'object' && item.file_id)
          : []
        return false

      case 'finished':
        streamSmoother?.flushThread(threadId)
        // 先标记流式结束，但保持消息显示直到历史记录加载完成
        if (threadState) {
          threadState.isStreaming = false
          threadState.replyLoadingVisible = false
          threadState.replyLoadingMessage = ''
          threadState.pendingRequestId = null
          threadState.pendingInterrupt = null
          threadState.contextCompressing = false
          // 产物按 run 快照（figuresByRun 同款桥接语义）：只信 finished chunk
          // 携带的 run 级权威清单（run_artifacts 投影，含空清单）；字段缺席
          // （旧服务端）不写快照——线程级 agentState.artifacts 是跨轮累积列表，
          // 绝不能当"本轮产物"快照（上一轮产物被钉到本轮的泄漏根因）
          const runId = String(chunk.run_id || threadState.activeRunId || '')
          const artifactPaths = artifactPathsFromChunk(chunk)
          if (runId && artifactPaths) {
            threadState.runArtifactsByRun = {
              ...(threadState.runArtifactsByRun || {}),
              [runId]: artifactPaths
            }
          }
          console.log(`${debugPrefix}[finished]`, {
            threadId,
            currentAgentId: unref(currentAgentId),
            hasThreadAgentState: !!threadState.agentState,
            supportsFiles: unref(supportsFiles)
          })
          if (unref(supportsFiles) && threadState.agentState) {
            console.log(
              `[AgentState|Final] ${new Date().toLocaleTimeString()}.${new Date().getMilliseconds()}`,
              {
                threadId,
                todos: threadState.agentState?.todos || [],
                uploads: threadState.agentState?.uploads || []
              }
            )
          }
        }
        return true

      case 'interrupted':
        streamSmoother?.flushThread(threadId)
        // 中断状态，刷新消息历史
        console.warn(`${debugPrefix}[interrupted]`, {
          threadId,
          message: chunkMessage,
          currentAgentId: unref(currentAgentId)
        })
        if (threadState) {
          threadState.isStreaming = false
          threadState.replyLoadingVisible = false
          threadState.replyLoadingMessage = ''
          threadState.pendingRequestId = null
          threadState.contextCompressing = false
          const pendingInterrupt = extractPendingInterrupt(chunk, threadId)
          if (pendingInterrupt) {
            threadState.pendingInterrupt = pendingInterrupt
          }
        }
        // 如果有 message 字段，显示提示（例如：敏感内容检测）
        if (chunkMessage) {
          message.info(chunkMessage)
        }
        return true

      case 'warning':
        if (chunkMessage) {
          message.warning(chunkMessage)
        }
        return false
    }

    return false
  }

  return {
    handleStreamChunk
  }
}
