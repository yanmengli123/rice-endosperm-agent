import assert from 'node:assert/strict'

import { nextTick, ref } from 'vue'

import { useEvidenceAnchor } from '../useEvidenceAnchor.js'

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const makeDeps = () => {
  const statePanelOpen = ref(false)
  const collapsedStateSections = { evidence: true }
  const panelOpened = []
  const loaded = []
  const currentChatId = ref('t1')
  const traceState = ref({ runId: null, knowledge: null })
  const threadStates = {
    t1: { evidence: [], evidenceRunId: null }
  }
  return {
    statePanelOpen,
    collapsedStateSections,
    openStatePanel: () => {
      statePanelOpen.value = true
      panelOpened.push(true)
    },
    panelOpened,
    currentChatId,
    currentTrace: traceState,
    setTrace: (value) => {
      traceState.value = value
    },
    getThreadState: (threadId) => threadStates[threadId],
    loadRunEvidence: (threadId, runId) => {
      loaded.push({ threadId, runId })
    },
    loaded
  }
}

// ── handleLocateEvidence：开面板 + 展开证据区 + 高亮（node 无 rAF 时同步回退） ──

const deps = makeDeps()
const anchor = useEvidenceAnchor({ ...deps, highlightDurationMs: 10 })
anchor.handleLocateEvidence({ ref: 'E3' })
assert.strictEqual(deps.panelOpened.length, 1, '面板未开时应打开面板')
assert.strictEqual(deps.collapsedStateSections.evidence, false, '证据区折叠应被展开')
assert.strictEqual(anchor.evidenceHighlightId.value, 'E3')

// 空 ref：无操作（不误开面板）
const deps2 = makeDeps()
const anchor2 = useEvidenceAnchor(deps2)
anchor2.handleLocateEvidence({ ref: '' })
anchor2.handleLocateEvidence({})
assert.strictEqual(deps2.panelOpened.length, 0)
assert.strictEqual(anchor2.evidenceHighlightId.value, '')

// 高亮窗口到期自动清除
await delay(40)
assert.strictEqual(anchor.evidenceHighlightId.value, '', '高亮窗口到期应清除')

// ── 事件驱动刷新：检索终态事件触发拉取（防抖窗口后），面板开启是门槛之一 ──

const deps3 = makeDeps()
useEvidenceAnchor({ ...deps3, refreshDelayMs: 10 })
deps3.statePanelOpen.value = true
deps3.setTrace({ runId: 'run-1', knowledge: null })
await nextTick()
deps3.setTrace({ runId: 'run-1', knowledge: { status: 'COMPLETED' } })
await nextTick()
await delay(40)
assert.strictEqual(deps3.loaded.length, 1, 'COMPLETED 事件应触发一次证据刷新')
assert.strictEqual(deps3.loaded[0].runId, 'run-1')

// RUNNING 中间态不触发
deps3.setTrace({ runId: 'run-1', knowledge: { status: 'RUNNING' } })
await nextTick()
await delay(40)
assert.strictEqual(deps3.loaded.length, 1)

// 面板关闭且从未加载过证据 → 不拉取（避免空转）
const deps4 = makeDeps()
useEvidenceAnchor({ ...deps4, refreshDelayMs: 10 })
deps4.setTrace({ runId: 'run-2', knowledge: { status: 'FAILED' } })
await nextTick()
await delay(40)
assert.strictEqual(deps4.loaded.length, 0, '面板关闭且无已加载证据时不应拉取')

// 已加载过证据（evidenceRunId 存在）→ 即使面板关闭也刷新
deps4.getThreadState('t1').evidenceRunId = 'run-0'
deps4.setTrace({ runId: 'run-2', knowledge: { status: 'COMPLETED' } })
await nextTick()
await delay(40)
assert.strictEqual(deps4.loaded.length, 1)

console.log('useEvidenceAnchor: all assertions passed')
