// Hermetic Vue lifecycle tests. No browser, network, credentials or paid providers.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import * as preferences from '../literature_reader.js'
import * as calendar from '../literature_calendar.js'
import * as graph from '../literature_graph.js'
import * as chromeUtils from '../literature_chrome.js'
import { ref, computed } from 'vue'

const { code: sessionsModelCode } = await transform(
  await readFile(new URL('../../composables/useLiteratureSessions.js', import.meta.url), 'utf8'),
  { format: 'cjs' }
)

const chatUIStore = { sidebarCollapsed: false }
const storage = {
  getItem: () => null,
  setItem: () => {}
}

import * as d3 from 'd3'
import * as Three from 'three'
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js'
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js'
import { LineMaterial } from 'three/addons/lines/LineMaterial.js'
const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')

const renderer = Vue.createRenderer({
  createElement: () => ({}),
  createText: () => ({}),
  createComment: () => ({}),
  insert() {},
  remove() {},
  setText() {},
  setElementText() {},
  patchProp() {},
  parentNode: () => null,
  nextSibling: () => null
})
const settle = async () => {
  for (let i = 0; i < 8; i++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
const deferred = () => {
  let resolve
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve }
}
const mounts = []
async function mount(
  file,
  props = {},
  modules = {},
  window = {},
  globals = {},
  initialize = () => {}
) {
  const source = await readFile(new URL(file, import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const script =
    descriptor.script?.content || compileScript(descriptor, { id: 'hermetic-unit' }).content
  const { code } = await transform(script, { format: 'cjs' })
  const exports = {}
  const module = { exports }
  const require = (id) => {
    if (id === 'vue') return Vue
    if (id === 'd3') return d3
    if (id === '@/utils/literature_reader') return preferences
    if (id === '@/utils/literature_calendar') return calendar
    if (id === '@/utils/literature_graph') return graph
    if (id === '@/utils/literature_layout_runner')
      return {
        createLiteratureLayoutRunner: (update) => ({
          stop() {},
          start(nodes) {
            update({ positions: new Float32Array(nodes.length * 3), completed: 10, done: true })
          }
        })
      }
    if (id === '@/utils/literature_chrome') return chromeUtils
    if (id === '@/stores/chatUI') {
      return { useChatUIStore: () => chatUIStore }
    }
    if (id === '@/composables/useColumnResize') {
      return {
        useColumnResize: () => ({ isDragging: ref(false), startDrag: () => {}, stopDrag: () => {} })
      }
    }
    if (id === '@/composables/useLiteratureSessions') {
      const stateModule = { exports: {} }
      vm.runInNewContext(sessionsModelCode, {
        module: stateModule,
        exports: stateModule.exports,
        require,
        window
      })
      return stateModule.exports
    }
    if (id === '@lucide/vue') return new Proxy({}, { get: () => ({ render: () => null }) })
    if (modules[id]) return modules[id]
    if (id.endsWith('.vue')) return { default: { render: () => null } }
    throw Error(`Unmocked dependency: ${id}`)
  }
  vm.runInNewContext(
    code,
    {
      exports,
      module,
      require,
      Function,
      Float32Array,
      window,
      document: {},
      URL,
      alert: () => {
        throw Error('Unexpected alert')
      },
      setTimeout,
      clearTimeout,
      requestAnimationFrame: () => 1,
      cancelAnimationFrame() {},
      ...globals
    },
    { filename: file }
  )
  const component = module.exports.default
  component.render = () => null
  const app = renderer.createApp(component, props)
  const instance = app.mount({})
  mounts.push(app)
  initialize(instance.$.setupState)
  await settle()
  return {
    instance: descriptor.scriptSetup ? instance.$.setupState : instance,
    app,
    setProps: (changes) => Object.assign(instance.$.props, changes)
  }
}

try {
  const anchorScrolls = [],
    anchorSelectors = []
  const heading = (title) => ({
    tagName: 'H2',
    textContent: title,
    dataset: {},
    id: '',
    scrollIntoView: () => anchorScrolls.push(title)
  })
  const anchorNodes = [heading('摘要'), heading('Results'), heading('Results')]
  const markdown = await mount(
    '../../components/literature/LiteratureMarkdown.vue',
    { content: 'Markdown' },
    {},
    {},
    {
      document: { createTreeWalker: () => ({ nextNode: () => false }) },
      NodeFilter: { SHOW_TEXT: 1 },
      MutationObserver: class {
        observe() {}
        disconnect() {}
      }
    },
    (state) => {
      state.host = {
        querySelectorAll: (selector) => {
          anchorSelectors.push(selector)
          return anchorNodes
        },
        contains: () => true
      }
    }
  )
  markdown.instance.hydrate()
  assert.equal(markdown.instance.navigateAnchor('%E6%91%98%E8%A6%81'), true)
  assert.equal(markdown.instance.navigateAnchor('results-1'), true)
  assert.deepEqual(anchorScrolls, ['摘要', 'Results'])
  assert.equal(markdown.instance.navigateAnchor("');evil()"), false)
  assert.ok(
    anchorSelectors.every((selector) => !selector.includes('evil')),
    'User anchors never become selectors'
  )
  let prevented = false
  markdown.instance.handleLink({
    target: {
      closest: (kind) =>
        kind === 'a'
          ? {
              dataset: {},
              getAttribute: () => '#摘要'
            }
          : null
    },
    preventDefault: () => {
      prevented = true
    }
  })
  assert.equal(prevented, true)
  assert.equal(anchorScrolls.at(-1), '摘要')
  // Real Three.js scene math/geometry/materials, only GPU/DOM adapters mocked.
  let capturedScene,
    capturedCamera,
    capturedControls,
    gpuDisposed = false,
    pendingFrames = 0
  const style3d = Vue.reactive({
    ...graph.graphStyleDefaults(),
    showEdges: true,
    showEdgeDir: true,
    edgeWidth: 2
  })
  const view3d = await mount(
    '../../components/literature/LiteratureGraph3D.vue',
    {
      nodes: [
        { rid: 'a', title: 'Alpha' },
        { rid: 'b', title: 'Beta' }
      ],
      edges: [{ source: 'a', target: 'b' }],
      styleOptions: style3d,
      positions: new Float32Array([10, 20, 30, 100, 120, -50]),
      active: true
    },
    {
      three: {
        ...Three,
        Scene: class extends Three.Scene {
          constructor() {
            super()
            capturedScene = this
          }
        },
        PerspectiveCamera: class extends Three.PerspectiveCamera {
          constructor(...args) {
            super(...args)
            capturedCamera = this
          }
        },
        WebGLRenderer: class {
          domElement = { style: {}, remove() {} }
          setClearColor() {}
          setSize() {}
          setPixelRatio() {}
          render() {}
          dispose() {
            gpuDisposed = true
          }
        }
      },
      'three/addons/controls/OrbitControls.js': {
        OrbitControls: class {
          target = new Three.Vector3()
          events = {}
          constructor() {
            capturedControls = this
          }
          addEventListener(name, fn) {
            this.events[name] = fn
          }
          update() {}
          dispose() {}
        }
      },
      'three/addons/lines/LineSegments2.js': { LineSegments2 },
      'three/addons/lines/LineSegmentsGeometry.js': { LineSegmentsGeometry },
      'three/addons/lines/LineMaterial.js': { LineMaterial },
      '@/stores/theme': { useThemeStore: () => ({ isDark: false }) }
    },
    { devicePixelRatio: 1 },
    {
      ResizeObserver: class {
        observe() {}
        disconnect() {}
      },
      requestAnimationFrame: () => ++pendingFrames,
      cancelAnimationFrame: () => {
        pendingFrames = 0
      }
    },
    (state) => {
      state.host = { clientWidth: 900, clientHeight: 560, appendChild() {} }
    }
  )
  const meshes = capturedScene.children.filter((item) => item.isInstancedMesh)
  const line = capturedScene.children.find((item) => item.isLineSegments2)
  const transform3d = new Three.Matrix4()
  meshes[0].getMatrixAt(1, transform3d)
  assert.deepEqual(
    new Three.Vector3().setFromMatrixPosition(transform3d).toArray(),
    [100, 120, -50]
  )
  assert.deepEqual(
    Array.from(line.geometry.attributes.instanceStart.data.array),
    [10, 20, 30, 100, 120, -50]
  )
  assert.equal(meshes[1].visible, true, '3D citation arrows are actual geometry')
  assert.equal(line.material.linewidth, 2, '3D uses pixel-width lines, not ignored WebGL linewidth')
  capturedControls.events.start()
  capturedCamera.position.set(123, 456, 789)
  style3d.edgeWidth = 4
  style3d.showEdgeDir = false
  style3d.background = 'dark'
  await settle()
  assert.equal(line.material.linewidth, 4)
  assert.equal(meshes[1].visible, false)
  assert.deepEqual(
    capturedCamera.position.toArray(),
    [123, 456, 789],
    'Style changes preserve the camera'
  )
  assert.equal(
    capturedScene.children.filter((item) => item.isInstancedMesh)[0],
    meshes[0],
    'Style updates do not rebuild the scene'
  )
  assert.ok(pendingFrames > 0)
  view3d.setProps({ active: false })
  await settle()
  assert.equal(pendingFrames, 0, 'Hidden 3D view stops its frame loop')
  view3d.app.unmount()
  mounts.splice(mounts.indexOf(view3d.app), 1)
  assert.equal(gpuDisposed, true)

  const archiveSha = 'a'.repeat(64)
  let requestedSha, downloadName, revokedUrl
  const history = await mount(
    '../../components/literature/HistoryPanel.vue',
    {},
    {
      '@/apis/base': { apiGet: async () => ({ items: [{ id: 1 }], has_more: false }) },
      '@/apis/literature_api': {
        literatureWorkspaceApi: {
          historyArchives: async () => ({ items: [{ archive_sha256: archiveSha }] }),
          exportHistoryArchive: async (sha) => {
            requestedSha = sha
            return { blob: async () => new Blob(['verified-zip']) }
          }
        }
      }
    },
    {},
    {
      URL: {
        createObjectURL: () => 'blob:unit-archive',
        revokeObjectURL: (url) => {
          revokedUrl = url
        }
      },
      document: {
        createElement: () => ({
          click() {
            downloadName = this.download
          }
        })
      },
      setTimeout: (fn) => fn()
    }
  )
  assert.equal(history.instance.items.length, 1, 'Migration panel loads receipts on entry')
  assert.equal(history.instance.archives[0].archive_sha256, archiveSha)
  await history.instance.exportArchive(history.instance.archives[0])
  assert.equal(
    requestedSha,
    archiveSha,
    'Export uses the selected sealed archive, not current workspace state'
  )
  assert.equal(downloadName, `paperagent-history-${archiveSha.slice(0, 12)}.zip`)
  assert.equal(revokedUrl, 'blob:unit-archive')

  let confirm = false
  const workspaceReads = []
  const window = { confirm: () => confirm, addEventListener() {}, removeEventListener() {} }
  const { instance: workspace } = await mount(
    '../../views/LiteratureView.vue',
    {},
    {
      'vue-router': { useRoute: () => ({ query: {} }), onBeforeRouteLeave() {} },
      '@/stores/user': { useUserStore: () => ({}) },
      '@/apis/base': {
        apiGet: async (url) => {
          workspaceReads.push(url)
          return {}
        },
        apiPost: async () => ({})
      },
      '@/apis/literature_api': {
        listLiteraturePapers: async () => ({ papers: [], total: 0 }),
        listLiteratureTasks: async () => ({ tasks: [] }),
        literatureWorkspaceApi: {}
      }
    },
    window
  )
  workspace.openPaper({ rid: 'main' })
  for (let i = 1; i <= 5; i++) {
    workspace.comparisonChoice = `compare-${i}`
    workspace.addComparison()
  }
  assert.equal(workspace.comparisonRids.length, 4, 'Five-pane budget includes primary')
  workspace.setEditing('main', true)
  workspace.switchWorkspace('diary')
  assert.equal(workspace.viewMode, 'reader', 'Declined workspace switch preserves reader')
  workspace.openPaper({ rid: 'other' })
  assert.equal(workspace.selectedRid, 'main', 'Declined replacement preserves edited paper')
  workspace.setEditing('main', false)
  workspace.closeReader()
  assert.equal(workspace.selectedRid, 'compare-1', 'Close promotes existing comparison')
  assert.equal(workspace.comparisonRids.length, 3)
  workspace.switchWorkspace('concepts')
  await settle()
  assert.ok(
    workspaceReads.includes('/api/literature/concepts'),
    'Concept toolbar must load actual concept data'
  )
  workspace.switchWorkspace('reports')
  await settle()
  assert.ok(
    workspaceReads.includes('/api/literature/reports'),
    'Report toolbar must load the report list'
  )

  const requests = new Map()
  const { instance: diary } = await mount(
    '../../views/DiaryView.vue',
    {},
    {
      '@/apis/literature_api': { literatureWorkspaceApi: {} },
      '@/apis/base': {
        apiGet: async (url) => {
          if (url.includes('/days')) return { days: ['2024-02-29'] }
          const date = url.split('/').at(-1)
          return requests.has(date) ? requests.get(date).promise : { content: '' }
        },
        apiPut: async () => ({})
      }
    },
    window
  )
  diary.content = 'unsaved local note'
  await diary.changeMonth('2024-02')
  assert.notEqual(diary.month, '2024-02', 'Declined date switch retains selected month')
  assert.equal(diary.content, 'unsaved local note')
  confirm = true
  const older = deferred(),
    newer = deferred()
  requests.set('2024-02-01', older)
  requests.set('2024-02-29', newer)
  const a = diary.selectDay('2024-02-01'),
    b = diary.selectDay('2024-02-29')
  newer.resolve({ content: 'newer response' })
  await b
  older.resolve({ content: 'stale response' })
  await a
  assert.equal(diary.selectedDate, '2024-02-29')
  assert.equal(diary.month, '2024-02')
  assert.equal(diary.content, 'newer response', 'Stale response cannot replace current draft')
  assert.equal(diary.calendar.filter(Boolean).length, 29)

  let reads = 0
  const { instance: reader } = await mount(
    '../../components/literature/ReaderTabs.vue',
    { rid: 'paper', paper: {}, versionToken: 'v1' },
    {
      '@/stores/user': { useUserStore: () => ({ uid: '', getAuthHeaders: () => ({}) }) },
      '@/apis/base': {
        apiGet: async () => {
          reads++
          return { ready: false }
        }
      },
      '@/utils/asset_resolver': {
        collectAssetUris: () => [],
        createAssetResolverSession: () => ({ abortAll() {}, revokeAll() {} })
      }
    },
    window
  )
  reader.editing = true
  const before = reads
  await reader.refreshReader()
  assert.equal(reads, before, 'Refresh must not clear or fetch over active edit')
  assert.equal(reader.editing, true)
  assert.match(reader.editorNotice, /未清除本地编辑/)
  reader.zoomImage(100)
  assert.equal(reader.imageScale, 5)
  reader.zoomImage(-100)
  assert.equal(reader.imageScale, 0.25)
  let focused = -1
  reader.lightbox = {
    querySelectorAll: () =>
      [0, 1, 2].map((index) => ({
        focus: () => {
          focused = index
        }
      }))
  }
  reader.trapLightboxFocus({ shiftKey: true })
  assert.equal(focused, 2, 'Shift+Tab from dialog enters last enabled control')
  reader.editing = false
  await reader.refreshReader()
  assert.ok(reads > before)
  assert.deepEqual(
    Array.from(reader.visibleTabs, (tab) => tab.key),
    ['original']
  )
  const navigation = await mount(
    '../../components/literature/ReaderTabs.vue',
    {
      rid: 'paper',
      paper: {},
      readerTarget: { rid: 'paper', tab: 'noteL2', anchor: '核心概念', requestId: 1 }
    },
    {
      '@/stores/user': { useUserStore: () => ({ uid: '', getAuthHeaders: () => ({}) }) },
      '@/apis/base': { apiGet: async () => ({ ready: false }) },
      '@/utils/asset_resolver': {
        collectAssetUris: () => [],
        createAssetResolverSession: () => ({ abortAll() {}, revokeAll() {} })
      }
    },
    window
  )
  assert.equal(navigation.instance.current, 'noteL2', 'A product link selects its real reader tab')
  assert.ok(
    navigation.instance.visibleTabs.some((tab) => tab.key === 'noteL2'),
    'Explicit linked product remains visible even when not generated'
  )
  navigation.instance.productEditing = true
  confirm = false
  navigation.setProps({
    readerTarget: { rid: 'paper', tab: 'translation', variant: 'summary', requestId: 2 }
  })
  await settle()
  assert.equal(
    navigation.instance.current,
    'noteL2',
    'A declined link preserves unsaved product edits'
  )
  assert.equal(navigation.instance.navigationTarget.requestId, 1)
  confirm = true
  navigation.setProps({
    readerTarget: { rid: 'paper', tab: 'translation', variant: 'summary', requestId: 3 }
  })
  await settle()
  assert.equal(navigation.instance.current, 'translation')
  assert.equal(navigation.instance.variantKind, 'summary')
  assert.equal(navigation.instance.navigationTarget.requestId, 3)
  confirm = false

  let reviewData = {
    total: 2,
    render_digest: 'same',
    items: [
      { report_idx: 0, item_id: 'first', render_digest: 'same', decision: { choice: 'A' } },
      { report_idx: 1, item_id: 'second', render_digest: 'same' }
    ]
  }
  const { instance: review } = await mount(
    '../../components/literature/ReviewPanel.vue',
    { rid: 'paper' },
    {
      '@/apis/base': { apiGet: async () => structuredClone(reviewData), apiPost: async () => ({}) }
    },
    window
  )
  review.hideResolved = true
  assert.equal(review.visibleItems.length, 1)
  review.meta.items[1].suggestion = 'local suggestion'
  await review.load()
  assert.equal(review.meta.items[1].suggestion, 'local suggestion')
  reviewData.items[1].render_digest = 'different'
  await review.load()
  assert.equal(
    review.meta.items[1].suggestion,
    '',
    'Stale review suggestion cannot cross render identity'
  )

  const { instance: graphView } = await mount(
    '../../views/LiteratureGraphView.vue',
    {},
    {
      '@/stores/theme': { useThemeStore: () => ({ isDark: false }) },
      '@/apis/literature_api': {
        literatureWorkspaceApi: {
          graphNetwork: async () => ({
            nodes: [{ rid: 'click-node', title: 'Clickable paper' }],
            edges: []
          })
        }
      },
      '@/apis/base': {
        apiGet: async (url) =>
          url.includes('/network')
            ? { nodes: [{ rid: 'click-node', title: 'Clickable paper' }], edges: [] }
            : { rid: 'click-node', references: [], cited_by: [] },
        apiPost: async () => ({})
      }
    },
    window
  )
  graphView.canvas = {
    getBoundingClientRect: () => ({ left: 20, top: 30, width: 450, height: 280 })
  }
  const click = { clientX: 245, clientY: 170, button: 0, shiftKey: false, type: 'mouseup' }
  graphView.onMouseDown(click)
  await graphView.onMouseUp(click)
  assert.equal(
    graphView.selected?.rid,
    'click-node',
    'Node click is selection, not an unconditional drag'
  )
  assert.equal(graphView.neighbors?.rid, 'click-node')
  await graphView.load(true)
  assert.equal(
    graphView.selected?.rid,
    'click-node',
    'Count preview does not replace selection or canvas'
  )

  let allowSwitch = false,
    createCalls = 0
  const { instance: sessions } = await mount(
    '../../components/literature/LiteratureSessions.vue',
    {
      canChangeThread: () => allowSwitch
    },
    {
      '@/apis/literature_api': {
        literatureWorkspaceApi: {
          sessions: async () => ({
            items: [
              { id: 'thread-a', title: 'Paper chat', session: { kind: 'paper', rid: 'paper' } }
            ]
          }),
          createSession: async () => {
            createCalls++
            return { thread: { id: 'new-thread' } }
          }
        }
      }
    },
    window
  )
  sessions.selectSession('thread-a')
  await sessions.create('global-qa')
  assert.equal(sessions.selectedId, '', 'Unsaved-reader guard runs before native thread activation')
  assert.equal(createCalls, 0, 'Declined switch must not create a new bound conversation')
  allowSwitch = true
  sessions.selectSession('thread-a')
  await settle()
  assert.equal(sessions.selectedId, 'thread-a')
  assert.equal(sessions.ready, false, 'Sending stays disabled without a loaded native conversation')

  let metadataDirty = false
  const { instance: metadata } = await mount(
    '../../components/literature/MetadataPanel.vue',
    {
      rid: 'paper',
      onEditingChange: (value) => {
        metadataDirty = value
      }
    },
    {
      '@/apis/literature_api': {
        literatureWorkspaceApi: {
          metadata: async () => ({
            title: 'Original title',
            kind: 'paper',
            authors_json: [],
            keywords_json: [],
            metadata_token: 'token'
          })
        }
      }
    },
    window
  )
  assert.equal(metadataDirty, false)
  metadata.draft.title = 'Unsaved title'
  await settle()
  assert.equal(metadataDirty, true, 'Metadata edits participate in parent navigation guard')

  // --- 会话管理（对标 PaperAgent §6.4）：删除 / 批量 / 清空归档 ---
  confirm = true
  let sessionList = [
    { id: 'keep', title: 'Keep me', session: { kind: 'paper', rid: 'paper' } },
    { id: 'drop', title: 'Drop me', session: { kind: 'paper', rid: 'paper' } },
    { id: 'active', title: 'Active', session: { kind: 'paper', rid: 'paper' } }
  ]
  const deleted = []
  const { instance: managed } = await mount(
    '../../components/literature/LiteratureSessions.vue',
    {},
    {
      '@/apis/literature_api': {
        literatureWorkspaceApi: {
          sessions: async () => ({ items: structuredClone(sessionList) }),
          updateSession: async () => ({}),
          deleteSession: async (id) => {
            deleted.push(id)
            sessionList = sessionList.filter((item) => item.id !== id)
          }
        }
      }
    },
    window
  )
  await settle()
  await managed.clearArchived()
  assert.equal(deleted.length, 0, 'Archived purge cannot delete the active list')
  managed.toggleOne('not-listed', true)
  assert.equal(managed.selectedIds.length, 0, 'Selection must reference a visible session')
  managed.toggleAll(true)
  assert.equal(managed.selectedIds.length, 3)
  assert.equal(managed.allSelected, true)
  managed.toggleOne('drop', false)
  assert.deepEqual(managed.selectedIds, ['keep', 'active'], 'Deselecting one row narrows the batch')
  assert.equal(managed.allSelected, false)
  await managed.removeOne('drop')
  assert.deepEqual(deleted, ['drop'], 'Row delete issues exactly one request')
  assert.deepEqual(managed.selectedIds, ['keep', 'active'])
  managed.selectedId = 'active'
  await managed.removeOne('active')
  assert.equal(managed.selectedId, '', 'Deleting the active session clears the send target')
  managed.toggleAll(true)
  await managed.deleteSelected()
  assert.deepEqual(
    deleted,
    ['drop', 'active', 'keep'],
    'Batch delete removes every selected session'
  )

  const { instance: failing } = await mount(
    '../../components/literature/LiteratureSessions.vue',
    {},
    {
      '@/apis/literature_api': {
        literatureWorkspaceApi: {
          sessions: async () => ({
            items: [
              { id: 'ok', title: 'Ok', session: { kind: 'paper', rid: 'paper' } },
              { id: 'boom', title: 'Boom', session: { kind: 'paper', rid: 'paper' } }
            ]
          }),
          updateSession: async () => ({}),
          deleteSession: async (id) => {
            if (id === 'boom') throw Error('删除被拒绝')
          }
        }
      }
    },
    window
  )
  await settle()
  failing.toggleAll(true)
  await failing.deleteSelected()
  assert.match(
    failing.notice,
    /已删除 1 个会话；1 个失败/,
    'Partial batch failure reports successes and failures separately'
  )

  // --- 卡片问答：已有本文会话时复用，不重复创建 ---
  let askCreates = 0
  const { instance: askView } = await mount(
    '../../views/LiteratureView.vue',
    {},
    {
      'vue-router': { useRoute: () => ({ query: {} }), onBeforeRouteLeave() {} },
      '@/stores/user': { useUserStore: () => ({}) },
      '@/apis/base': { apiGet: async () => ({}), apiPost: async () => ({}) },
      '@/apis/literature_api': {
        listLiteraturePapers: async () => ({ items: [], total: 0 }),
        listLiteratureTasks: async () => ({ items: [] }),
        literatureWorkspaceApi: {
          sessions: async () => ({
            items: [{ id: 'existing', title: 'Existing', session: { kind: 'paper', rid: 'paper' } }]
          }),
          createSession: async () => {
            askCreates++
            return { thread: { id: 'fresh' } }
          }
        }
      }
    },
    window
  )
  await askView.askPaper({ rid: 'paper' })
  assert.equal(askCreates, 0, 'Existing per-paper session is reused, not duplicated')
  assert.equal(askView.viewMode, 'sessions')

  // --- Workbench chrome: outer sidebar collapse is declarative, not a store mutation ---
  const pref = { sidebarCollapsed: false }
  const deskCollapsed = computed(() =>
    chromeUtils.resolveSidebarCollapsed(pref.sidebarCollapsed, { compactShell: true })
  )
  assert.equal(
    deskCollapsed.value,
    true,
    'compactShell route collapses the shell without writing the preference'
  )
  assert.equal(
    pref.sidebarCollapsed,
    false,
    'The persisted preference is never mutated by route entry'
  )
  // Leaving the route (keepAlive: only onDeactivated fires) must restore by derivation.
  const otherCollapsed = computed(() =>
    chromeUtils.resolveSidebarCollapsed(pref.sidebarCollapsed, {})
  )
  assert.equal(otherCollapsed.value, false, 'Other routes fall back to the user preference')
  assert.equal(
    chromeUtils.resolveSidebarCollapsed(true, {}),
    true,
    'An explicitly collapsed preference still wins on normal routes'
  )

  // --- Workbench chrome internals ---
  const { instance: desk } = await mount(
    '../../views/LiteratureView.vue',
    {},
    {
      'vue-router': { useRoute: () => ({ query: {} }), onBeforeRouteLeave() {} },
      '@/stores/user': { useUserStore: () => ({ uid: 'alice' }) },
      '@/apis/base': { apiGet: async () => ({}), apiPost: async () => ({}) },
      '@/apis/literature_api': {
        listLiteraturePapers: async () => ({ items: [], total: 0 }),
        listLiteratureTasks: async () => ({ items: [] }),
        literatureWorkspaceApi: {}
      }
    },
    { confirm: () => true, addEventListener() {}, removeEventListener() {}, localStorage: storage }
  )
  assert.equal(desk.sidebarVisible, true, 'Domain deck is visible by default')
  assert.match(
    desk.workspaceStyle.gridTemplateColumns,
    /^320px 4px minmax\(0, 1fr\)$/,
    'Deck lays out sidebar | resizer | main'
  )
  desk.toggleReadMode()
  assert.equal(desk.readMode, true)
  assert.equal(desk.sidebarVisible, false, 'Reading mode hands the desktop to the content')
  assert.equal(desk.workspaceStyle.gridTemplateColumns, 'minmax(0, 1fr)')
  desk.toggleReadMode()
  desk.toggleSidebar()
  assert.equal(desk.sidebarVisible, false, 'Sidebar toggle really removes the column')
  desk.toggleSidebar()
  assert.equal(desk.sidebarVisible, true)
  desk.onSidebarResizeKeydown({ key: 'ArrowRight', shiftKey: true, preventDefault() {} })
  assert.equal(desk.litSidebarWidth, 352, 'Shift+Arrow widens by a coarse step')
  desk.onSidebarResizeKeydown({ key: 'Home', preventDefault() {} })
  assert.equal(desk.litSidebarWidth, 260, 'Home clamps to the minimum width')
  desk.onSidebarResizeKeydown({ key: 'End', preventDefault() {} })
  assert.equal(desk.litSidebarWidth, 560, 'End clamps to the maximum width')
  desk.onSidebarResizeKeydown({ key: 'Enter', preventDefault() {} })
  assert.equal(desk.litSidebarWidth, 320, 'Enter restores the default width')
  desk.selectDrawer('diary')
  assert.equal(desk.drawer, 'diary')
  desk.selectDrawer('nope')
  assert.equal(desk.drawer, 'diary', 'Unknown drawer keys are ignored')
  const sharedRows = [{ id: 'shared', title: 'Shared', session: { kind: 'global-qa' } }]
  desk.sessionState.sessions.value = sharedRows
  const { instance: sharedSessions } = await mount(
    '../../components/literature/LiteratureSessions.vue',
    { sessionState: desk.sessionState },
    {
      '@/apis/literature_api': {
        literatureWorkspaceApi: {
          sessions: async () => ({ items: sharedRows })
        }
      }
    },
    window
  )
  sharedSessions.selectSession('shared')
  assert.equal(
    desk.sessionState.selectedId.value,
    'shared',
    'Side list and native chat share one selected target'
  )
  sharedSessions.toggleAll(true)
  assert.deepEqual(
    desk.sessionState.selectedIds.value,
    ['shared'],
    'Batch selection is shared, not duplicated'
  )
  confirm = false
  workspace.setEditing('unsaved', true)
  const previousThread = workspace.activeThreadId
  workspace.selectSessionFromSidebar('declined-thread')
  assert.equal(
    workspace.activeThreadId,
    previousThread,
    'Declined sidebar navigation preserves native target'
  )
  assert.equal(
    workspace.sessionState.selectedId.value,
    '',
    'Declined navigation cannot pre-select a session'
  )

  console.log(
    'Literature component lifecycle tests passed: five readers, edit guards, diary races, safe refresh, review identity, session delete, card ask and workbench chrome'
  )
} finally {
  mounts.forEach((app) => app.unmount())
}
