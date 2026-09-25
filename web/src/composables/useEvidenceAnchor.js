/**
 * 证据互锚（P1）：正文引用芯片〔证据E#〕点击 → 打开状态面板、展开证据区、
 * 高亮目标行；外加证据面板的事件驱动刷新（检索终态事件到达即重拉投影，
 * 消除「事件已到、清单仍等轮询」的双源窗口）。
 *
 * 依赖全部注入（面板开关/分区状态/线程与轨迹 getter/证据加载器），
 * 不 import 任何 '@' 模块——裸 node spec 可直接驱动。
 */
import { ref, watch } from 'vue'

const nextFrame = (callback) =>
  typeof requestAnimationFrame === 'function' ? requestAnimationFrame(callback) : callback()

// 浏览器与 node 同构（裸 node spec 无 window 对象）
const setTimeoutSafe = (callback, ms) => globalThis.setTimeout(callback, ms)
const clearTimeoutSafe = (timer) => globalThis.clearTimeout(timer)

export function useEvidenceAnchor({
  statePanelOpen,
  collapsedStateSections,
  openStatePanel,
  currentChatId,
  currentTrace,
  getThreadState,
  loadRunEvidence,
  refreshDelayMs = 600,
  highlightDurationMs = 4000
}) {
  const evidenceHighlightId = ref('')
  let evidenceHighlightTimer = null

  const handleLocateEvidence = ({ ref: evidenceRef } = {}) => {
    const target = String(evidenceRef || '').trim()
    if (!target) return
    if (!statePanelOpen.value) openStatePanel()
    if (collapsedStateSections.evidence) collapsedStateSections.evidence = false
    evidenceHighlightId.value = ''
    // 先置空再赋值，确保同一引用连续点击也能重新触发闪烁动画
    nextFrame(() => {
      evidenceHighlightId.value = target
      if (evidenceHighlightTimer) clearTimeoutSafe(evidenceHighlightTimer)
      evidenceHighlightTimer = setTimeoutSafe(() => {
        evidenceHighlightId.value = ''
      }, highlightDurationMs)
    })
  }

  // 事件驱动刷新：knowledge.search 终态事件到达即刷新证据投影；
  // 仅在已加载过证据或面板开启时拉取（避免为未打开的面板空转）。
  let evidenceEventRefreshTimer = null
  watch(
    () => {
      const knowledge = currentTrace.value?.knowledge || null
      return knowledge ? `${currentTrace.value?.runId || ''}:${knowledge.status}` : ''
    },
    (signature) => {
      if (!/:COMPLETED$/.test(signature) && !/:FAILED$/.test(signature)) return
      const threadId = currentChatId.value
      const runId = currentTrace.value?.runId
      if (!threadId || !runId) return
      const threadState = getThreadState(threadId)
      const evidenceLoaded =
        (threadState?.evidence?.length || 0) > 0 || Boolean(threadState?.evidenceRunId)
      if (!evidenceLoaded && !statePanelOpen.value) return
      if (evidenceEventRefreshTimer) clearTimeoutSafe(evidenceEventRefreshTimer)
      evidenceEventRefreshTimer = setTimeoutSafe(() => {
        loadRunEvidence(threadId, runId)
      }, refreshDelayMs)
    }
  )

  return { evidenceHighlightId, handleLocateEvidence }
}
