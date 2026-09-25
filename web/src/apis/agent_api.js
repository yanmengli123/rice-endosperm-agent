import { apiGet, apiPost, apiDelete, apiPut, apiRequest } from './base'
import { useUserStore } from '@/stores/user'
import { buildCatalogPrompt } from '@/utils/threadTitle'

/**
 * 智能体API模块
 * 包含智能体管理、聊天、配置等功能
 * 权限要求: 任何已登录用户（普通用户、管理员、超级管理员）
 */

// =============================================================================
// === 智能体聊天分组 ===
// =============================================================================

export const agentApi = {
  /**
   * 简单聊天调用（非流式）
   * @param {string} query - 查询内容
   * @returns {Promise} - 聊天响应
   */
  simpleCall: (query) => apiPost('/api/chat/call', { query }),

  /**
   * 受限编目调用：为对话标题发起一次裸模型调用（低温度、限制输出 tokens）。
   * 返回模型原始输出（应为 {"title":"..."}），由调用方经 threadTitle 门禁校验后采用。
   * @param {string} query - 对话首条用户消息
   * @param {Object} modelSpec - 模型配置
   * @returns {Promise<string>} - 模型原始输出
   */
  generateTitle: async (query, modelSpec) => {
    const response = await apiPost('/api/chat/call', {
      query: buildCatalogPrompt(query),
      meta: {
        model_spec: modelSpec,
        temperature: 0,
        max_tokens: 64
      }
    })
    return response.response
  },

  /**
   * 获取智能体列表
   * @returns {Promise} - 智能体列表
   */
  getAgents: ({ includeSubagents = false } = {}) => {
    const params = new URLSearchParams()
    if (includeSubagents) params.set('include_subagents', 'true')
    const query = params.toString()
    return apiGet(query ? `/api/agent?${query}` : '/api/agent')
  },

  getAgentBackends: () => apiGet('/api/agent/backends'),

  /**
   * 获取单个智能体详情
   * @param {string} agentId - 智能体ID
   * @returns {Promise} - 智能体详情
   */
  getAgentDetail: (agentId) => apiGet(`/api/agent/${agentId}`),

  /**
   * 获取智能体历史消息
   * @param {string} agentId - 智能体ID
   * @param {string} threadId - 会话ID
   * @returns {Promise} - 历史消息
   */
  getAgentHistory: (threadId) => apiGet(`/api/chat/thread/${threadId}/history`),

  /**
   * 获取指定会话的 AgentState
   * @param {string} agentId - 智能体ID
   * @param {string} threadId - 会话ID
   * @returns {Promise} - AgentState
   */
  getAgentState: (threadId, { includeMessages = false } = {}) =>
    apiGet(`/api/chat/thread/${threadId}/state${includeMessages ? '?include_messages=true' : ''}`),

  /**
   * Submit feedback for a message
   * @param {number} messageId - Message ID
   * @param {string} rating - 'like' or 'dislike'
   * @param {string|null} reason - Optional reason for dislike
   * @returns {Promise} - Feedback response
   */
  submitMessageFeedback: (messageId, rating, reason = null) =>
    apiPost(`/api/chat/message/${messageId}/feedback`, { rating, reason }),

  /**
   * Get feedback status for a message
   * @param {number} messageId - Message ID
   * @returns {Promise} - Feedback status
   */
  getMessageFeedback: (messageId) => apiGet(`/api/chat/message/${messageId}/feedback`),

  createAgent: (payload) => apiPost('/api/agent', payload),

  updateAgent: (agentId, payload) => apiPut(`/api/agent/${agentId}`, payload),

  /**
   * 删除智能体。被主智能体引用的子智能体会返回 409（detail.code === 'subagent_referenced'），
   * superadmin 可传 force 强制删除。
   */
  deleteAgent: (agentId, { force = false } = {}) =>
    apiDelete(force ? `/api/agent/${agentId}?force=true` : `/api/agent/${agentId}`),

  /** 列出把该子智能体挂进协作白名单的主智能体（count 为真实总数，references 仅含可见项）。 */
  getAgentReferences: (agentId) => apiGet(`/api/agent/${agentId}/references`),

  /** 协作模式配方与模板（编排骨架 / 专家简报 / 调度决策表），供「从模式新建」预填。 */
  getCollaborationTemplates: () => apiGet('/api/agent/collaboration-templates'),

  /**
   * 创建异步运行任务（Run）
   * @param {Object} data - run 请求体
   * @returns {Promise<Object>}
   */
  createAgentRun: (data) =>
    apiPost('/api/agent/runs', {
      query: data.query,
      agent_slug: data.agent_slug,
      thread_id: data.thread_id,
      meta: data.meta || {},
      image_content: data.image_content || null,
      model_spec: data.model_spec || null,
      resume: data.resume ?? null,
      created_by_run_id: data.created_by_run_id || null,
      mention_protocol: data.mention_protocol || null,
      mentions: data.mentions || null
    }),

  /**
   * 获取协议能力快照（公开端点）：protocol_version + capabilities 位。
   * UI 能力驱动渲染据此门控（如 trace_stage_facets 缺失时显式隐藏阶段条），
   * 而不是靠数据字段缺席隐式兜底。
   */
  getAgentProtocol: () => apiGet('/api/agent/protocol'),

  /**
   * 获取 Run 状态
   * @param {string} runId - run ID
   * @returns {Promise<Object>}
   */
  getAgentRun: (runId) => apiGet(`/api/agent/runs/${runId}`),

  /**
   * 获取 Run 终态结果（status/output/run_context/run_artifacts）。
   * run_context 含冻结知识范围、终态执行计划与检索摘要——状态面板
   * 「知识范围」板块在终态后由此刷新。
   */
  getAgentRunResult: (runId) => apiGet(`/api/agent/runs/${runId}/result`),

  /** 获取 Run 的轻量知识检索审计记录。 */
  getAgentRunKnowledgeRetrievals: (runId) =>
    apiGet(`/api/agent/runs/${runId}/knowledge-retrievals`),

  /** 获取 Run 执行轨迹快照（summary + spans + snapshot_sequence）。 */
  getAgentRunTrace: (runId) => apiGet(`/api/agent/runs/${runId}/trace`),

  /** 执行轨迹缺口补拉：返回 sequence 严格大于 afterSequence 的事件。 */
  getAgentRunTraceEvents: (runId, { afterSequence = 0, limit = 500 } = {}) => {
    const params = new URLSearchParams({
      after_sequence: String(afterSequence),
      limit: String(limit)
    })
    return apiGet(`/api/agent/runs/${runId}/trace/events?${params.toString()}`)
  },

  /** 获取 Run 执行轨迹 span 列表。 */
  getAgentRunTraceSpans: (runId) => apiGet(`/api/agent/runs/${runId}/trace/spans`),

  /** 获取 Run 的科研检索证据候选 DTO（尚未等同于答案 Claim 引用）。 */
  getAgentRunEvidence: (runId) => apiGet(`/api/agent/runs/${runId}/evidence`),

  /** 原文查看审计：记录谁在何时查看了哪条证据的原文。 */
  recordEvidenceSourceView: (runId, evidenceId) =>
    apiPost(`/api/agent/runs/${runId}/evidence/${evidenceId}/view`, {}),

  /** 独立 Trace SSE；Last-Event-ID 是 PostgreSQL ledger sequence。 */
  streamAgentRunTrace: (runId, afterSequence = 0, options = {}) => {
    const { signal } = options
    const headers = { ...useUserStore().getAuthHeaders() }
    const cursor = Math.max(0, Number(afterSequence) || 0)
    if (cursor > 0) headers['Last-Event-ID'] = String(cursor)
    return fetch(`/api/agent/runs/${runId}/trace/stream`, {
      method: 'GET',
      headers,
      signal
    })
  },

  /**
   * 取消 Run
   * @param {string} runId - run ID
   * @returns {Promise<Object>}
   */
  cancelAgentRun: (runId) => apiPost(`/api/agent/runs/${runId}/cancel`, {}),

  /**
   * 获取线程活跃 Run
   * @param {string} threadId - 线程ID
   * @returns {Promise<Object>}
   */
  getThreadActiveRun: (threadId) => apiGet(`/api/agent/thread/${threadId}/active_run`),

  /**
   * 打开 Run 事件 SSE 连接（调用方负责关闭）
   * @param {string} runId - run ID
   * @param {string} afterSeq - 起始 seq/cursor
   * @param {Object} options - { signal, verbose }
   * @returns {Promise<Response>}
   */
  streamAgentRunEvents: (runId, afterSeq = '0-0', options = {}) => {
    const { signal, verbose = false } = options
    const headers = {
      ...useUserStore().getAuthHeaders()
    }
    const cursor = String(afterSeq || '0-0')
    if (cursor && cursor !== '0-0') {
      headers['Last-Event-ID'] = cursor
    }
    const params = new URLSearchParams({ verbose: String(verbose) })
    return fetch(`/api/agent/runs/${runId}/events?${params.toString()}`, {
      method: 'GET',
      headers,
      signal
    })
  }
}

// =============================================================================
// === 多模态图片支持分组 ===
// =============================================================================

export const multimodalApi = {
  /**
   * 上传图片并获取base64编码
   * @param {File} file - 图片文件
   * @returns {Promise} - 上传结果
   */
  uploadImage: (file) => {
    const formData = new FormData()
    formData.append('file', file)

    return apiRequest(
      '/api/chat/image/upload',
      {
        method: 'POST',
        body: formData
      },
      true
    )
  }
}

// =============================================================================
// === 对话线程分组 ===
// =============================================================================

export const threadApi = {
  /**
   * 获取对话线程列表
   * @param {string | null | undefined} agentId - 智能体ID，可选；不传时返回全部智能体对话
   * @param {number} limit - 返回数量限制，默认100
   * @param {number} offset - 偏移量，默认0
   * @returns {Promise} - 对话线程列表
   */
  getThreads: (agentId = null, limit = 100, offset = 0) => {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset)
    })
    if (agentId) {
      params.set('agent_id', agentId)
    }
    const url = `/api/chat/threads?${params.toString()}`
    return apiGet(url)
  },

  /**
   * 搜索历史对话
   * @param {string} query - 搜索关键词
   * @param {Object} options - 搜索选项
   * @param {string | null | undefined} options.agentId - 智能体ID，可选
   * @param {number} options.limit - 返回数量限制
   * @param {number} options.offset - 偏移量
   * @returns {Promise} - 搜索结果
   */
  searchThreads: (query, { agentId = null, limit = 20, offset = 0 } = {}) => {
    const params = new URLSearchParams({
      q: query,
      limit: String(limit),
      offset: String(offset)
    })
    if (agentId) {
      params.set('agent_id', agentId)
    }
    return apiGet(`/api/chat/threads/search?${params.toString()}`)
  },

  /**
   * 创建新对话线程
   * @param {string} agentId - 智能体ID
   * @param {string} title - 对话标题
   * @param {Object} metadata - 元数据
   * @returns {Promise} - 创建结果
   */
  createThread: (agentId, title, metadata) =>
    apiPost('/api/chat/thread', {
      agent_id: agentId,
      title: title || '新的对话',
      metadata: metadata || {}
    }),

  /**
   * 更新对话线程
   * @param {string} threadId - 对话线程ID
   * @param {string} title - 对话标题
   * @param {boolean} is_pinned - 是否置顶
   * @param {Object} metadata - 增量合并到 extra_metadata 的元数据（如 title_source）
   * @returns {Promise} - 更新结果
   */
  updateThread: (threadId, title, is_pinned, metadata) =>
    apiPut(`/api/chat/thread/${threadId}`, {
      title,
      is_pinned,
      metadata: metadata || {}
    }),

  /**
   * 删除对话线程
   * @param {string} threadId - 对话线程ID
   * @returns {Promise} - 删除结果
   */
  deleteThread: (threadId) => apiDelete(`/api/chat/thread/${threadId}`),

  /**
   * 获取线程附件列表
   * @param {string} threadId - 对话线程ID
   * @returns {Promise}
   */
  getThreadAttachments: (threadId) => apiGet(`/api/chat/thread/${threadId}/attachments`),

  /**
   * 列出线程文件（目录）
   * @param {string} threadId
   * @param {string} path
   * @param {boolean} recursive
   * @returns {Promise}
   */
  listThreadFiles: (threadId, path = '/home/gem/user-data', recursive = false) =>
    apiGet(
      `/api/chat/thread/${threadId}/files?path=${encodeURIComponent(path)}&recursive=${recursive}`
    ),

  /**
   * 读取线程文本文件内容（分页）
   * @param {string} threadId
   * @param {string} path
   * @param {number} offset
   * @param {number} limit
   * @returns {Promise}
   */
  readThreadFile: (threadId, path, offset = 0, limit = 2000) =>
    apiGet(
      `/api/chat/thread/${threadId}/files/content?path=${encodeURIComponent(path)}&offset=${offset}&limit=${limit}`
    ),

  /**
   * 获取线程文件下载/预览 URL
   * @param {string} threadId
   * @param {string} path
   * @param {boolean} download
   * @returns {string}
   */
  getThreadArtifactUrl: (threadId, path, download = false) => {
    const encodedPath = path
      .split('/')
      .filter(Boolean)
      .map((segment) => encodeURIComponent(segment))
      .join('/')
    const query = download ? '?download=true' : ''
    return `/api/chat/thread/${threadId}/artifacts/${encodedPath}${query}`
  },

  /**
   * 下载线程文件（带鉴权）
   * @param {string} threadId
   * @param {string} path
   * @returns {Promise<Response>}
   */
  downloadThreadArtifact: (threadId, path) =>
    apiGet(threadApi.getThreadArtifactUrl(threadId, path, true), {}, true, 'blob'),

  /**
   * 导出会话问答为自包含 HTML 文件（blob 响应，文件名取 Content-Disposition）
   * @param {string} threadId
   * @returns {Promise<Response>}
   */
  exportThreadHtml: (threadId) => apiGet(`/api/chat/thread/${threadId}/export`, {}, true, 'blob'),

  /**
   * 导出单条回答为自包含 HTML 文件（blob 响应，文件名取 Content-Disposition）
   * @param {string} threadId
   * @param {number|string} messageId
   * @returns {Promise<Response>}
   */
  exportMessageHtml: (threadId, messageId) =>
    apiGet(`/api/chat/thread/${threadId}/messages/${messageId}/export`, {}, true, 'blob'),

  /**
   * 保存交付物到 workspace/saved_artifacts
   * @param {string} threadId
   * @param {string} path
   * @returns {Promise}
   */
  saveThreadArtifactToWorkspace: (threadId, path) =>
    apiPost(`/api/chat/thread/${threadId}/artifacts/save`, { path }),

  /**
   * 上传临时附件
   * @param {File} file
   * @returns {Promise}
   */
  uploadTmpAttachment: (file) => {
    const formData = new FormData()
    formData.append('file', file)
    return apiRequest('/api/chat/attachments/tmp', {
      method: 'POST',
      body: formData
    })
  },

  /**
   * 解析临时附件
   * @param {Object} payload
   * @returns {Promise}
   */
  parseTmpAttachment: (payload) => apiPost('/api/chat/attachments/tmp/parse', payload),

  /**
   * 确认添加临时附件到线程
   * @param {string} threadId
   * @param {Array} attachments
   * @returns {Promise}
   */
  confirmTmpThreadAttachments: (threadId, attachments) =>
    apiPost(`/api/chat/thread/${threadId}/attachments/confirm`, { attachments }),

  /**
   * 上传附件
   * @param {string} threadId
   * @param {File} file
   * @returns {Promise}
   */
  uploadThreadAttachment: (threadId, file) => {
    const formData = new FormData()
    formData.append('file', file)
    return apiRequest(`/api/chat/thread/${threadId}/attachments`, {
      method: 'POST',
      body: formData
    })
  },

  /**
   * 删除附件
   * @param {string} threadId
   * @param {string} fileId
   * @returns {Promise}
   */
  deleteThreadAttachment: (threadId, fileId) =>
    apiDelete(`/api/chat/thread/${threadId}/attachments/${fileId}`)
}
