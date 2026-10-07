// 执行实际 SFC setup/watch；只替换网络和绘图，避免用静态文本断言验证权限生命周期。
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import process from 'node:process'
import { setImmediate } from 'node:timers/promises'
import { compileScript, parse } from 'vue/compiler-sfc'
import * as vue from 'vue'
import * as aggregation from '../chartAggregations.js'
import * as options from '../chartOptions.js'
import * as tooltips from '../chartTooltips.js'
import * as palette from '../graphPalette.js'
import * as access from '../graphAccess.js'
import * as reviewMeta from '../reviewMeta.js'
import { useGraph } from '../../../composables/useGraph.js'

const renderer = vue.createRenderer({
  createElement: () => ({ children: [] }),
  createText: () => ({}),
  createComment: () => ({}),
  insert: (node, parent) => parent.children.push(node),
  remove() {},
  setText() {},
  setElementText() {},
  parentNode: () => null,
  nextSibling: () => null,
  patchProp() {}
})
const base = {
  nodes: [{ id: 'private', type: 'Gene', node_kind: 'entity' }],
  edges: [],
  meta: { review_policy: 'approved_only', generated_at: '2026-10-07T00:00:00Z' }
}
const response = { success: true, data: base }
const mount = (api, workbench = false) => {
  const detail = workbench === 'detail'
  const name = detail ? 'GraphDetailPanel' : workbench ? 'ExplorerWorkbench' : 'GraphInsightPanel'
  const file = new URL(`../../../components/${detail ? '' : 'graph/'}${name}.vue`, import.meta.url)
  const { descriptor } = parse(fs.readFileSync(file, 'utf8'))
  const compiled = compileScript(descriptor, {
    id: 'graph-access-test',
    genDefaultAs: '__component'
  })
  const user = vue.reactive({
    uid: 'owner',
    token: 'session-a',
    userRole: 'admin',
    departmentId: 1
  })
  const bindings = {
    ...vue,
    ...aggregation,
    ...options,
    ...tooltips,
    ...palette,
    ...access,
    ...reviewMeta,
    useGraph,
    unifiedApi: api,
    useDatabaseStore: () => ({ kbId: 'private-kb', database: {} }),
    message: { error() {}, warning() {}, success() {} },
    useThemeStore: () => ({ isDark: false }),
    useUserStore: () => user,
    graphApi: api,
    echarts: {},
    RotateCw: {},
    X: {}
  }
  for (const name of Object.keys(compiled.imports)) bindings[name] ??= {}
  const context = vm.createContext({
    ...bindings,
    console: { ...console, error() {} },
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Date,
    Map,
    Set,
    ResizeObserver: class {},
    document: { documentElement: {} },
    getComputedStyle: () => ({ getPropertyValue: () => '' })
  })
  const code = compiled.content.replace(/^import[\s\S]*?from\s+(['"])[^'"]+\1\s*;?/gm, '')
  vm.runInContext(`${code}\nthis.testComponent = __component`, context)
  context.testComponent.render = () => null
  const props = vue.reactive(
    detail
      ? { visible: false, kbId: 'private-kb', type: 'edge', item: null }
      : workbench
        ? { active: false }
        : { open: false, kbId: 'private-kb', graphData: base }
  )
  const app = renderer.createApp({ render: () => vue.h(context.testComponent, { ...props }) })
  app.mount({ children: [] })
  return {
    state: app._instance.subTree.component.setupState,
    props,
    user,
    unmount: () => app.unmount()
  }
}

let passed = 0
let failed = 0
const test = async (name, run) => {
  try {
    await run()
    passed++
    console.log(`✓ ${name}`)
  } catch (error) {
    failed++
    console.error(`✗ ${name}: ${error.message}`)
  }
}
for (const status of [401, 403, 404]) {
  await test(`HTTP ${status} 清空私有快照及详情`, async () => {
    const api = { getSubgraph: async () => response }
    const denied = mount(api)
    try {
      await denied.state.ensureFullData(true)
      assert.equal(denied.state.fullData.nodes.length, 1)
      denied.state.selectedPair = { source: 'Gene', target: 'Gene' }
      api.getSubgraph = async () => {
        const e = new Error('denied')
        e.response = { status }
        throw e
      }
      await denied.state.ensureFullData(true)
      assert.equal(denied.state.fullData, null, `HTTP ${status} 必须清空私有快照`)
      assert.equal(denied.state.selectedPair, null)
    } finally {
      denied.unmount()
    }
  })
}

await test('切换登录会话使旧快照失效', async () => {
  const fixture = mount({ getSubgraph: async () => response })
  try {
    await fixture.state.ensureFullData(true)
    fixture.user.token = 'session-b'
    await vue.nextTick()
    assert.equal(fixture.state.fullData, null)
  } finally {
    fixture.unmount()
  }
})

await test('重新进入全图重新授权，等待中不展示旧全图', async () => {
  let calls = 0
  let finish
  const fixture = mount({
    getSubgraph: () => {
      calls++
      return calls === 1
        ? Promise.resolve(response)
        : new Promise((resolve) => {
            finish = resolve
          })
    }
  })
  try {
    fixture.state.dataScope = 'full'
    await fixture.state.ensureFullData(true)
    assert.equal(fixture.state.fullData.nodes.length, 1)
    fixture.state.onDataScopeChange({ target: { value: 'canvas' } })
    fixture.state.onDataScopeChange({ target: { value: 'full' } })
    assert.equal(calls, 2)
    assert.equal(fixture.state.fullData, null)
    finish(response)
    await vue.nextTick()
  } finally {
    fixture.unmount()
  }
})

await test('画布权限失败同步清空节点、边与详情', async () => {
  const fixture = mount(
    {
      getViewSettings: async () => ({ success: true, data: {} }),
      getSubgraph: async () => {
        const e = new Error('denied')
        e.response = { status: 404 }
        throw e
      }
    },
    true
  )
  try {
    fixture.state.graph.updateGraphData(base.nodes, base.edges, base.meta)
    fixture.state.graph.handleNodeClick(base.nodes[0])
    await fixture.state.loadGraph()
    assert.equal(fixture.state.graph.graphData.nodes.length, 0)
    assert.equal(fixture.state.graph.selectedItem, null)
    assert.equal(fixture.state.graph.showDetailDrawer, false)
  } finally {
    fixture.unmount()
  }
})

await test('等待设置时卸载，不再发起图谱查询', async () => {
  let finishSettings
  let graphQueries = 0
  const fixture = mount(
    {
      getViewSettings: () =>
        new Promise((resolve) => {
          finishSettings = resolve
        }),
      getSubgraph: async () => {
        graphQueries++
        return response
      }
    },
    true
  )
  const pending = fixture.state.loadGraph()
  await vue.nextTick()
  fixture.unmount()
  finishSettings({ success: true, data: {} })
  await pending
  assert.equal(graphQueries, 0)
})

await test('关系详情的证据与历史不会被旧事实响应覆盖', async () => {
  const pendingEvidence = new Map(),
    pendingHistory = new Map()
  const fixture = mount(
    {
      getTripleEvidence: (_kb, id) => new Promise((resolve) => pendingEvidence.set(id, resolve)),
      reviewAudit: ({ target_id }) =>
        new Promise((resolve) => pendingHistory.set(target_id, resolve))
    },
    'detail'
  )
  const select = async (id) => {
    fixture.props.visible = true
    fixture.props.item = { data: { original: { properties: { triple_id: id } } } }
    await vue.nextTick()
  }
  try {
    await select('fact-A')
    await select('fact-B')
    pendingEvidence.get('fact-B')({
      success: true,
      data: { triple_id: 'fact-B', mentions: [{ chunk_id: 'B', quote: 'B' }] }
    })
    pendingHistory.get('fact-B')({ success: true, data: { items: [{ target_id: 'fact-B' }] } })
    await setImmediate()
    assert.equal(fixture.state.evidence.triple_id, 'fact-B')
    pendingEvidence.get('fact-A')({
      success: true,
      data: { triple_id: 'fact-A', mentions: [{ chunk_id: 'A', quote: 'A' }] }
    })
    pendingHistory.get('fact-A')({ success: true, data: { items: [{ target_id: 'fact-A' }] } })
    await setImmediate()
    assert.equal(fixture.state.evidence.triple_id, 'fact-B')
    assert.equal(fixture.state.selectedPinnedChunkId, 'B')
    assert.equal(fixture.state.history[0].target_id, 'fact-B')
  } finally {
    fixture.unmount()
  }
})

await test('详情权限拒绝通知父画布清空', async () => {
  const fixture = mount(
    {
      getTripleEvidence: async () => {
        const error = new Error('denied')
        error.response = { status: 404 }
        throw error
      },
      reviewAudit: async () => ({ success: true, data: { items: [] } })
    },
    'detail'
  )
  let denied = 0
  try {
    fixture.props.onAccessDenied = () => denied++
    fixture.props.item = { data: { original: { properties: { triple_id: 'private-fact' } } } }
    fixture.props.visible = true
    await vue.nextTick()
    await setImmediate()
    assert.equal(denied, 1)
    assert.equal(fixture.state.evidence, null)
  } finally {
    fixture.unmount()
  }
})

await test('设置端点发现撤权后不再查询图谱，并清空原数据', async () => {
  let graphQueries = 0
  const fixture = mount(
    {
      getViewSettings: async () => {
        const error = new Error('denied')
        error.response = { status: 404 }
        throw error
      },
      getSubgraph: async () => {
        graphQueries++
        return response
      }
    },
    true
  )
  try {
    fixture.state.graph.updateGraphData(base.nodes, base.edges, base.meta)
    await fixture.state.loadGraph()
    assert.equal(graphQueries, 0)
    assert.equal(fixture.state.graph.graphData.nodes.length, 0)
  } finally {
    fixture.unmount()
  }
})

console.log(`${passed} passed, ${failed} failed`)
if (failed) process.exit(1)
