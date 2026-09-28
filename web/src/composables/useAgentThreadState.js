const createOnGoingConvState = () => ({
  msgChunks: {},
  currentRequestKey: null,
  currentAssistantKey: null,
  toolCallBuffers: {}
})

export function useAgentThreadState({
  chatState,
  getCurrentThreadId,
  onStopThread = null,
  onBeforeResetThread = null,
  onBeforeCleanupThread = null
}) {
  const resetThreadUiState = (threadState) => {
    if (!threadState) return
    threadState.replyLoadingVisible = false
    threadState.replyLoadingMessage = ''
    threadState.pendingRequestId = null
  }

  const getThreadState = (threadId) => {
    if (!threadId) return null
    if (!chatState.threadStates[threadId]) {
      chatState.threadStates[threadId] = {
        isStreaming: false,
        runStreamAbortController: null,
        activeRunId: null,
        runLastSeq: '0-0',
        lastRetryableJobTry: null,
        replyLoadingVisible: false,
        replyLoadingMessage: '',
        pendingRequestId: null,
        pendingInterrupt: null,
        onGoingConv: createOnGoingConvState(),
        agentState: null,
        contextCompressing: false,
        evidenceRunId: null,
        evidence: [],
        evidenceSummary: null,
        evidenceRetrievals: [],
        evidenceIssues: [],
        evidenceRole: null,
        claimBindingStatus: null,
        evidenceProjectionStatus: null,
        sourceManifest: null,
        verifiedCitation: null,
        // 本轮已发布的论文原图投影（citation_ready.figures）；字段缺席 ⟺ 未发布
        verifiedFigures: [],
        // 答案气泡内图卡的会话级暂存：run_id → figures（线程生命周期内不随新一轮重置，
        // 桥接"流结束 → 历史回读"之间；历史回读后以消息 extra_metadata.citation_ready 为准）
        figuresByRun: {},
        // 图表锚点（ADR-0008 citation_ready.figure_refs）：本轮签发的正文锚点绑定；
        // 字段缺席 ⟺ 未签发（开关关闭/无可解析提及）
        verifiedFigureRefs: [],
        // 锚点的会话级暂存：run_id → figure_refs（桥接语义同 figuresByRun）
        figureRefsByRun: {},
        // 表格卡片（ADR-0008 P2 citation_ready.tables）：受控解析的行列 JSON；
        // 字段缺席 ⟺ 未发布
        verifiedTables: [],
        // 表格卡片的会话级暂存：run_id → tables（桥接语义同 figuresByRun）
        tablesByRun: {},
        verifiedGraphSnapshot: null,
        // run_id → graph_snapshot_v1，桥接流末事件与历史消息回读。
        graphsByRun: {},
        // MCP 物化产物按轮快照：run_id → 虚拟路径列表（agent_state 整体替换会抹掉
        // 线程级 artifacts，finished 时快照进当前 run；历史回读后以 run_artifacts 投影为准）
        runArtifactsByRun: {},
        // 跨文献歧义时的候选文献（locator_candidates 事件；只含文档身份）
        locatorCandidates: [],
        // 当前/最近 run 的服务端上下文（run_context：知识范围冻结快照、终态
        // 计划与检索摘要）；创建响应先落一份，终态后由 result 端点刷新。
        runContext: null,
        // 上下文压缩持久标记（运行时内存态）：{requestId, at}，注入展示流
        // 成为「已压缩 N 条历史」分隔线；历史回读不重建（无服务端持久源）。
        compressionMarkers: []
      }
    }
    return chatState.threadStates[threadId]
  }

  const stopThreadStream = (threadId) => {
    if (!threadId) return
    if (typeof onStopThread === 'function') {
      onStopThread(threadId)
    }
  }

  const cleanupThreadState = (threadId) => {
    if (!threadId) return
    const threadState = chatState.threadStates[threadId]
    if (!threadState) return

    if (typeof onBeforeCleanupThread === 'function') {
      onBeforeCleanupThread(threadId)
    }

    if (threadState.runStreamAbortController) {
      threadState.runStreamAbortController.abort()
    }
    delete chatState.threadStates[threadId]
  }

  const resetOnGoingConv = (threadId = null) => {
    const targetThreadId =
      threadId || (typeof getCurrentThreadId === 'function' ? getCurrentThreadId() : null)

    if (targetThreadId) {
      const threadState = getThreadState(targetThreadId)
      if (!threadState) return

      if (typeof onBeforeResetThread === 'function') {
        onBeforeResetThread(targetThreadId)
      }

      if (threadState.runStreamAbortController) {
        threadState.runStreamAbortController.abort()
        threadState.runStreamAbortController = null
      }

      threadState.onGoingConv = createOnGoingConvState()
      resetThreadUiState(threadState)
      return
    }

    Object.keys(chatState.threadStates).forEach((id) => {
      cleanupThreadState(id)
    })
  }

  return {
    getThreadState,
    cleanupThreadState,
    resetOnGoingConv,
    stopThreadStream
  }
}
