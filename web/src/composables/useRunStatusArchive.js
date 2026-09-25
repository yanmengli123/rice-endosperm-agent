import { reactive, ref } from 'vue'
import { agentApi } from '@/apis'
import {
  RUN_STATUS_ARCHIVE_MAX_ENTRIES,
  assembleEvidenceFields,
  assembleTraceProjection,
  createArchiveEntry,
  evictArchiveEntries
} from '@/utils/runStatusArchive'

const isNotFoundError = (error) => error?.response?.status === 404

/**
 * 历史轮状态档案：按 run_id 懒加载并缓存「执行轨迹 + 检索证据」。
 *
 * 与线程槽位（useRunTrace / loadRunEvidence，实时事件驱动）完全隔离：
 * 档案只通过服务端快照 API 离线重建，不订阅 SSE、不写入线程状态，
 * 因此查看历史轮不会干扰正在流式的最新一轮。
 */
export function useRunStatusArchive() {
  const focusedRunId = ref(null)
  const entries = reactive(new Map())

  const ensureEntry = (runId) => {
    let entry = entries.get(runId)
    if (!entry) {
      entry = reactive(createArchiveEntry(runId))
      entries.set(runId, entry)
    } else {
      // Map 迭代顺序即 LRU 最近使用顺序：命中时重插刷新新鲜度
      entries.delete(runId)
      entries.set(runId, entry)
    }
    return entry
  }

  const loadEntry = async (runId) => {
    const entry = ensureEntry(runId)
    if (entry.loading || entry.loaded) return
    entry.loading = true
    const [traceResult, evidenceResult, runResult] = await Promise.allSettled([
      agentApi.getAgentRunTrace(runId),
      agentApi.getAgentRunEvidence(runId),
      agentApi.getAgentRunResult(runId)
    ])
    // LRU 可能在请求期间驱逐了本条；写入脱离缓存的条目无害，重新聚焦会重建
    if (traceResult.status === 'fulfilled') {
      entry.trace = assembleTraceProjection(runId, traceResult.value)
    } else if (isNotFoundError(traceResult.reason)) {
      entry.traceExpired = true
    } else {
      entry.traceError = true
    }
    if (evidenceResult.status === 'fulfilled') {
      Object.assign(entry, assembleEvidenceFields(evidenceResult.value))
    } else {
      entry.evidenceError = true
    }
    if (runResult.status === 'fulfilled') {
      entry.runContext = runResult.value?.run_context || null
    }
    entry.loading = false
    entry.loaded = true
    entry.loadedAt = Date.now()
    evictArchiveEntries(entries, RUN_STATUS_ARCHIVE_MAX_ENTRIES, focusedRunId.value)
  }

  /**
   * 聚焦某一轮：seedTrace 允许用线程槽位的最新一轮做即时显示（服务端快照到达后替换）。
   * force 用于失败后的重试（重置条目重新拉取）。
   */
  const focusRun = (runId, { seedTrace = null, force = false } = {}) => {
    if (!runId) return
    if (force) entries.delete(runId)
    const entry = ensureEntry(runId)
    if (seedTrace && !entry.trace) entry.trace = seedTrace
    focusedRunId.value = runId
    void loadEntry(runId)
  }

  const clearFocus = () => {
    focusedRunId.value = null
  }

  const resetArchive = () => {
    entries.clear()
    focusedRunId.value = null
  }

  return { focusedRunId, entries, focusRun, clearFocus, resetArchive }
}
