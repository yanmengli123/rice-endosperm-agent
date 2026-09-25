// run 级产物解析：唯一权威是"该 run 的产物清单"（服务端 run_artifacts 投影），
// 快照仅桥接"finished → 历史回读"窗口；绝不回退线程级累积列表——那是跨轮
// 泄漏的根因（上一轮产物被钉到新一轮名下，详见 docs/vibe/2026-09-22 设计文档）。

const toVirtualPath = (item) => (typeof item === 'string' ? item : item?.virtual_path)

// 保留服务端投影的完整条目（origin/size/media_type），供来源徽标与审计信息展示；
// 旧数据/字符串路径自动降级为无 origin 的最小条目。
export const toArtifactEntry = (item) => {
  if (typeof item === 'string')
    return { virtual_path: item, origin: null, name: null, size_bytes: null }
  if (!item || typeof item !== 'object') return null
  return {
    virtual_path: item.virtual_path || '',
    name: item.name || null,
    origin: item.origin && typeof item.origin === 'object' ? item.origin : null,
    size_bytes: item.size_bytes ?? null,
    media_type: item.media_type || null
  }
}

/**
 * 从 finished chunk 提取 run 级产物路径（快照桥接用，维持原契约）。
 * 字段缺席（旧服务端 / 本轮无产物）返回 null——调用方据此不写快照。
 * @param {Object} chunk finished chunk
 * @returns {string[] | null}
 */
export const artifactPathsFromChunk = (chunk) => {
  if (!Array.isArray(chunk?.artifacts)) return null
  return chunk.artifacts.map(toVirtualPath).filter(Boolean)
}

/**
 * 从 finished chunk 提取 run 级产物条目（含 origin，来源徽标用）。
 * @param {Object} chunk finished chunk
 * @returns {Array<Object>|null}
 */
export const artifactEntriesFromChunk = (chunk) => {
  if (!Array.isArray(chunk?.artifacts)) return null
  return chunk.artifacts.map(toArtifactEntry).filter((entry) => entry?.virtual_path)
}

/**
 * 解析一轮对话的产物：只看本轮**最后一条** AI 消息（最终回答）。
 * 1. message.run_artifacts 键存在（含空数组）= 服务端权威清单，空就是空；
 * 2. 键缺失 = 升级前的历史数据，回退该消息所属 run 的快照（runArtifactsByRun）；
 * 3. 都没有 → 空数组。
 * 绝不向前找更早的 AI 消息（同轮内重试/中断轮的产物不属于最终回答），
 * 也绝不读线程级 agentState.artifacts（跨轮累积）。
 * @param {Object} conv 单轮对话（messages 数组）
 * @param {Object} runArtifactsByRun run_id → 路径快照表
 * @returns {string[]}
 */
export const artifactsForConversation = (conv, runArtifactsByRun) => {
  const messages = Array.isArray(conv?.messages) ? conv.messages : []
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i]
    if (message?.type !== 'ai') continue
    if (Array.isArray(message?.run_artifacts)) {
      return message.run_artifacts.map(toVirtualPath).filter(Boolean)
    }
    const runId = message?.run_id || message?.extra_metadata?.run_id
    if (typeof runId === 'string' && runId.trim()) {
      const snapshot = runArtifactsByRun?.[runId.trim()]
      if (Array.isArray(snapshot)) return snapshot.filter(Boolean)
    }
    return []
  }
  return []
}

/**
 * 同 artifactsForConversation 的条目版（保留 origin 供来源徽标）。
 * 快照兜底路径只有虚拟路径（无 origin），自动降级为无来源条目。
 * @param {Object} conv 单轮对话（messages 数组）
 * @param {Object} runArtifactsByRun run_id → 路径快照表
 * @returns {Array<Object>}
 */
export const artifactEntriesForConversation = (conv, runArtifactsByRun) => {
  const messages = Array.isArray(conv?.messages) ? conv.messages : []
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i]
    if (message?.type !== 'ai') continue
    if (Array.isArray(message?.run_artifacts)) {
      return message.run_artifacts.map(toArtifactEntry).filter((entry) => entry?.virtual_path)
    }
    const runId = message?.run_id || message?.extra_metadata?.run_id
    if (typeof runId === 'string' && runId.trim()) {
      const snapshot = runArtifactsByRun?.[runId.trim()]
      if (Array.isArray(snapshot)) {
        return snapshot.map(toArtifactEntry).filter((entry) => entry?.virtual_path)
      }
    }
    return []
  }
  return []
}

/**
 * 从历史消息聚合"本会话产物"分组（按轮去重，键缺失的历史轮自动跳过）。
 * @param {Array} messages 服务端 history 消息数组
 * @returns {Array<{key: string, label: string, paths: string[]}>}
 */
export const sessionArtifactGroupsFromMessages = (messages) => {
  if (!Array.isArray(messages)) return []
  const groups = []
  const seen = new Set()
  let turnIndex = 0
  for (const message of messages) {
    if (message?.type === 'human') {
      turnIndex += 1
      continue
    }
    if (message?.type !== 'ai' || !Array.isArray(message?.run_artifacts)) continue
    const paths = message.run_artifacts.map(toVirtualPath).filter((path) => path && !seen.has(path))
    paths.forEach((path) => seen.add(path))
    if (!paths.length) continue
    groups.push({
      key: `turn-${message.run_id || turnIndex || groups.length + 1}`,
      label: turnIndex ? `第 ${turnIndex} 轮` : '更早',
      paths,
      entries: message.run_artifacts.map(toArtifactEntry).filter((entry) => entry?.virtual_path)
    })
  }
  return groups
}
