// Actual Vue KeepAlive and compiled upload component; no browser or external services.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import { resolveDefaultOcrEngine } from '../ocr_engine.js'

const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const renderer = Vue.createRenderer({
  createElement: () => ({ children: [] }),
  createText: (text) => ({ text }),
  createComment: () => ({}),
  setText() {},
  setElementText() {},
  patchProp() {},
  insert(node, parent) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    parent.children ||= []
    parent.children.push(node)
    node.parent = parent
  },
  remove(node) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    node.parent = null
  },
  parentNode: (node) => node.parent,
  nextSibling: () => null
})
const settle = async () => {
  for (let i = 0; i < 6; i++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
async function evaluate(file, modules, globals = {}, sfc = false) {
  const source = await readFile(new URL(file, import.meta.url), 'utf8')
  const descriptor = sfc ? parse(source).descriptor : null
  const script = descriptor?.scriptSetup
    ? compileScript(descriptor, { id: 'lifecycle-regression' }).content
    : descriptor?.script?.content || source
  const module = { exports: {} }
  vm.runInNewContext((await transform(script, { format: 'cjs' })).code, {
    module,
    exports: module.exports,
    AbortController,
    console,
    require: (id) => {
      if (id === 'vue') return Vue
      if (id === 'ant-design-vue')
        return {
          message: { success() {}, error() {}, warning() {}, info() {} },
          Modal: { confirm() {} }
        }
      if (id === '@/utils/litDialogs')
        return { litConfirm: async () => false, litPrompt: async () => null }
      if (id === '@/composables/useLiteratureSessions')
        return { confirmAction: async () => false }
      if (id === '@/composables/useTablistKeyboard')
        return { useTablistKeyboard: () => ({ onTabKeydown: () => {} }) }
      if (id === '@/utils/literature_catalog') return { duplicateCounts: () => new Map() }
      if (modules[id]) return modules[id]
      if (id.endsWith('.vue')) return { default: { render: () => null } }
      throw Error('Unmocked dependency: ' + id)
    },
    ...globals
  })
  return module.exports
}

// Two consumers share one request, but deactivation/hiding/account changes fence late responses.
const intervals = new Map(),
  visibility = new Set(),
  requests = []
let intervalId = 0
const document = {
  hidden: false,
  addEventListener: (_name, fn) => visibility.add(fn),
  removeEventListener: (_name, fn) => visibility.delete(fn)
}
const pollingScope = await evaluate(
  '../../composables/useLiteraturePollingScope.js',
  {},
  { document }
)
const user = Vue.reactive({ uid: 'owner-a', departmentId: 1, isLoggedIn: true })
const feedModule = await evaluate(
  '../../composables/useLiteratureTaskFeed.js',
  {
    '@/stores/user': { useUserStore: () => user },
    '@/apis/literature_api': {
      listLiteratureTasks: (_limit, signal) =>
        new Promise((resolve, reject) => {
          requests.push({ signal, resolve, reject })
        })
    }
  },
  {
    document,
    window: {
      setInterval: (fn) => {
        intervals.set(++intervalId, fn)
        return intervalId
      },
      clearInterval: (id) => intervals.delete(id)
    }
  }
)
let feed
const Consumer = {
  setup() {
    feed = feedModule.useLiteratureTaskFeed()
    return () => null
  }
}
const Route = { setup: () => () => Vue.h('div', [Vue.h(Consumer), Vue.h(Consumer)]) }
const shown = Vue.ref(true)
const app = renderer.createApp({
  setup: () => () =>
    Vue.h(Vue.KeepAlive, null, {
      default: () => (shown.value ? Vue.h(Route) : Vue.h('div'))
    })
})
app.mount({})
await settle()
assert.equal(requests.length, 1)
assert.equal(intervals.size, 1)
shown.value = false
await settle()
assert.equal(intervals.size, 0, '离开缓存路由必须暂停全局轮询')
assert.equal(requests[0].signal.aborted, true)
requests[0].resolve({ tasks: [{ ref: 'late-a' }] })
await settle()
assert.equal(feed.tasks.value.length, 0)
shown.value = true
await settle()
assert.equal(requests.length, 2, '返回缓存路由立即刷新且只有一份请求')
requests[1].resolve({ tasks: [{ ref: 'a' }] })
await settle()
assert.equal(feed.tasks.value[0].ref, 'a')
const outstandingRefresh = feed.refresh()
const oldOwner = requests.at(-1)
user.uid = 'owner-b'
await settle()
assert.equal(oldOwner.signal.aborted, true)
assert.equal(feed.tasks.value.length, 0, '账号切换立即清空旧快照')
const newOwner = requests.at(-1)
oldOwner.resolve({ tasks: [{ ref: 'late-a' }] })
await outstandingRefresh
await settle()
assert.equal(feed.tasks.value.length, 0)
newOwner.resolve({ tasks: [{ ref: 'b' }] })
await settle()
assert.equal(feed.tasks.value[0].ref, 'b')
document.hidden = true
for (const callback of visibility) callback()
assert.equal(intervals.size, 0)
document.hidden = false
for (const callback of visibility) callback()
assert.equal(intervals.size, 1)
const hiddenRequest = requests.at(-1)
user.isLoggedIn = false
await settle()
assert.equal(hiddenRequest.signal.aborted, true)
assert.equal(feed.tasks.value.length, 0)
assert.equal(feed.updatedAt.value, 0)
assert.equal(intervals.size, 0)
app.unmount()
assert.equal(visibility.size, 0, '最后一个订阅释放监听器')

// A cached detail must stop its own 3s timer and reject stale loads independently of the feed.
const paperUser = Vue.reactive({ uid: 'owner-a', isLoggedIn: true })
const paperReads = [],
  timeouts = new Map()
let timeoutId = 0,
  detailState
const detail = (
  await evaluate(
    '../../components/literature/PaperDetail.vue',
    {
      'vue-router': { useRouter: () => ({ push() {} }) },
      '@/composables/useLiteraturePollingScope': pollingScope,
      '@/stores/user': { useUserStore: () => paperUser },
      '@/apis/base': { apiPost: async () => ({}) },
      '@/apis/literature_api': {
        literaturePdfApi: {
          capabilities: async () => ({ enabled: false, reason: 'Test runtime disabled' })
        },
        getLiteraturePaper: (rid, signal) =>
          new Promise((resolve) => paperReads.push({ rid, signal, resolve }))
      }
    },
    {
      setTimeout: (fn) => {
        timeouts.set(++timeoutId, fn)
        return timeoutId
      },
      clearTimeout: (id) => timeouts.delete(id),
      alert: () => {
        throw Error('Unexpected alert')
      }
    },
    true
  )
).default
const detailSetup = detail.setup
detail.setup = (props, context) => {
  detailState = detailSetup(props, context)
  return detailState
}
detail.render = () => null
const showDetail = Vue.ref(true)
const detailApp = renderer.createApp({
  setup: () => () =>
    Vue.h(Vue.KeepAlive, null, {
      default: () => (showDetail.value ? Vue.h(detail, { rid: 'paper-a' }) : Vue.h('div'))
    })
})
detailApp.mount({})
await settle()
assert.equal(paperReads.length, 1)
paperReads[0].resolve({ rid: 'paper-a', render: { status: 'RUNNING' } })
await settle()
assert.equal(timeouts.size, 1)
const refreshDetail = detailState.load()
const staleDetail = paperReads.at(-1)
showDetail.value = false
await settle()
assert.equal(timeouts.size, 0)
assert.equal(staleDetail.signal.aborted, true)
staleDetail.resolve({ rid: 'stale-paper', render: { status: 'RUNNING' } })
await refreshDetail
assert.equal(detailState.paper.value.rid, 'paper-a')
assert.equal(timeouts.size, 0, '迟到结果不能重新启动轮询')
showDetail.value = true
await settle()
assert.equal(paperReads.length, 3)
const oldDetailOwner = paperReads.at(-1)
paperUser.uid = 'owner-b'
await settle()
assert.equal(oldDetailOwner.signal.aborted, true)
assert.equal(detailState.paper.value, null)
oldDetailOwner.resolve({ rid: 'stale-owner', render: { status: 'RUNNING' } })
paperReads.at(-1).resolve({ rid: 'paper-b', render: { status: 'RUNNING' } })
await settle()
assert.equal(detailState.paper.value.rid, 'paper-b')
assert.equal(timeouts.size, 1)
const beforeLogout = paperReads.length
paperUser.isLoggedIn = false
await detailState.load()
assert.equal(paperReads.length, beforeLogout, '退出登录不能重新拉取详情')
assert.equal(timeouts.size, 0)
assert.equal(detailState.paper.value, null)
detailApp.unmount()

// Every remaining autonomous network poller must use the same visibility/KeepAlive boundary.
const eventModel = await evaluate('../../composables/useLiteratureEvents.js', {})
for (const name of [
  'WorkflowPanel',
  'LibraryWorkbench',
  'DraftReader',
  'TranslationSettings',
  'LiteratureStatusBar'
]) {
  const reads = []
  const read = (signal) => new Promise((resolve) => reads.push({ signal, resolve }))
  const usageOwner = Vue.ref('owner-a')
  const component = (
    await evaluate(
      `../../components/literature/${name}.vue`,
      {
        '@/composables/useLiteraturePollingScope': pollingScope,
        '@/composables/useLiteratureSettings': {
          useLiteratureSettings: () => ({ reviewPolicy: 'wait' })
        },
        '@/composables/useLiteratureEvents': eventModel,
        '@/composables/useLiteratureTaskFeed': {
          useLiteratureTaskFeed: () => ({
            tasks: Vue.ref([]),
            feedError: Vue.ref(''),
            ownerKey: usageOwner
          })
        },
        '@/stores/user': { useUserStore: () => ({ userId: 1 }) },
        '@/apis/base': { apiGet: (_url, opts) => read(opts.signal), apiPost: async () => ({}) },
        '@/apis/literature_api': {
          literatureWorkspaceApi: { journalsStatus: async () => ({}) },
          literatureTranslationApi: {
            settings: async () => ({ profiles: [], models: [] }),
            probeSources: async () => ({ items: [] }),
            task: (_id, _content, signal) => read(signal),
            draftTask: (_revision, signal) => read(signal)
          }
        }
      },
      {
        setTimeout: (fn) => {
          timeouts.set(++timeoutId, fn)
          return timeoutId
        },
        clearTimeout: (id) => timeouts.delete(id),
        window: {
          setTimeout: (fn) => {
            timeouts.set(++timeoutId, fn)
            return timeoutId
          },
          clearTimeout: (id) => timeouts.delete(id),
          setInterval: (fn) => {
            intervals.set(++intervalId, fn)
            return intervalId
          },
          clearInterval: (id) => intervals.delete(id),
          localStorage: { getItem: () => '88' }
        },
        URL: { revokeObjectURL() {} }
      },
      true
    )
  ).default
  let state
  const setup = component.setup
  component.setup = (props, context) => {
    state = setup(props, context)
    return state
  }
  component.render = () => null
  const visible = Vue.ref(true)
  const props =
    name === 'DraftReader' ? { document: { revision_id: 'draft-a' } } : { rid: 'paper-a' }
  const pollApp = renderer.createApp({
    setup: () => () =>
      Vue.h(Vue.KeepAlive, null, {
        default: () => (visible.value ? Vue.h(component, props) : Vue.h('div'))
      })
  })
  pollApp.mount({})
  await settle()
  if (name === 'WorkflowPanel') {
    state.runId.value = 'run-a'
    void state.refresh()
  }
  if (name === 'LibraryWorkbench') {
    state.runId.value = 'run-a'
    state.indexState.value = { index_id: 'index-a' }
    void state.refreshLibrary()
    void state.refreshIndex()
  }
  assert.ok(reads.length, name + ' actually requested initial state')
  const previousReads = reads.slice()
  visible.value = false
  await settle()
  for (const request of previousReads) assert.equal(request.signal.aborted, true, name)
  assert.equal(timeouts.size + intervals.size, 0, name + ' pauses all scheduled requests')
  const result = { status: 'RUNNING', task: { status: 'RUNNING' }, total_tokens: 999 }
  for (const request of previousReads) request.resolve(result)
  await settle()
  assert.equal(timeouts.size + intervals.size, 0, name + ' rejects late rescheduling')
  const snapshot = state.state || state.libraryState || state.task || state.currentUsage
  assert.equal(snapshot.value, null, name + ' rejects late publication')
  visible.value = true
  await settle()
  assert.ok(reads.length > previousReads.length, name + ' resumes read-only polling')
  const afterResume = reads.slice(previousReads.length)
  document.hidden = true
  for (const callback of visibility) callback()
  for (const request of afterResume) assert.equal(request.signal.aborted, true, name)
  assert.equal(timeouts.size + intervals.size, 0, name + ' pauses in hidden tab')
  document.hidden = false
  for (const callback of visibility) callback()
  await settle()
  if (name === 'LiteratureStatusBar') {
    const oldUsage = reads.at(-1)
    usageOwner.value = 'owner-b'
    assert.equal(oldUsage.signal.aborted, true)
    oldUsage.resolve({ total_tokens: 999 })
    await settle()
    assert.equal(state.currentUsage.value, null, '旧账号用量不可发布')
    usageOwner.value = ''
    assert.equal(intervals.size, 0, '退出登录暂停用量轮询')
  }
  pollApp.unmount()
  assert.equal(visibility.size, 0, name + ' removes visibility listener')
  assert.equal(timeouts.size + intervals.size, 0, name + ' releases all timers')
}

// Knowledge-source intake must send the same explicit modes as literature intake.
const store = Vue.reactive({
  kbId: 'kb-pdf',
  database: { contract_key: 'pdf_evidence' },
  state: {}
})
const messages = []
const upload = (
  await evaluate(
    '../../components/FileUploadModal.vue',
    {
      '@/stores/database': { useDatabaseStore: () => store },
      '@/stores/config': { useConfigStore: () => ({ config: {} }) },
      '@/stores/user': { useUserStore: () => ({ getAuthHeaders: () => ({}) }) },
      '@/utils/ocr_engine': { resolveDefaultOcrEngine },
      '@/utils/chunkUtils': { buildChunkParamsPayload: () => ({}) },
      '@/apis/system_api': { ocrApi: {} },
      '@/apis/knowledge_api': {
        fileApi: { getSupportedFileTypes: async () => ({ file_types: ['.pdf'] }) },
        documentApi: {}
      },
      '@/apis/workspace_api': { getWorkspaceTree: async () => ({}) },
      'ant-design-vue': {
        message: new Proxy({}, { get: () => (msg) => messages.push(msg) }),
        Upload: {},
        Modal: {}
      },
      '@ant-design/icons-vue': { ReloadOutlined: {} },
      '@lucide/vue': new Proxy({}, { get: () => ({}) })
    },
    {},
    true
  )
).default
let uploadState
const originalSetup = upload.setup
upload.setup = (props, context) => {
  uploadState = originalSetup(props, context)
  return uploadState
}
upload.render = () => null
const uploadApp = renderer.createApp(upload, { visible: false })
const uploadInstance = uploadApp.mount({})
await settle()
for (const contract of ['pdf_evidence', 'legacy']) {
  store.database = contract === 'legacy' ? {} : { contract_key: contract }
  uploadInstance.$.props.formatTemplate = contract === 'legacy' ? 'pdf_literature' : ''
  for (const autoIndex of [false, true]) {
    uploadState.autoIndex.value = autoIndex
    uploadState.processingParams.value.ocr_engine = 'deepseek_ocr'
    let submitted
    store.addFiles = async (request) => {
      submitted = request
      return true
    }
    uploadState.fileList.value = [
      { name: 'rice.pdf', status: 'done', response: { file_path: 'minio://fixture/rice.pdf' } }
    ]
    await uploadState.chunkData()
    assert.ok(submitted, JSON.stringify(messages))
    assert.equal(submitted.params.scientific_processing_mode, autoIndex ? 'activate' : 'parse_only')
    assert.equal(submitted.params.auto_index, autoIndex)
    assert.equal(submitted.params.pdf_evidence_pipeline, true)
    assert.equal(
      submitted.params.ocr_engine,
      contract === 'legacy' ? 'mineru_official' : 'deepseek_ocr'
    )
  }
}
store.database = { contract_key: 'plain_file' }
uploadState.autoIndex.value = false
assert.equal(
  uploadState.withEvidencePipelineParams({}).scientific_processing_mode,
  undefined,
  '真实非 PDF 契约优先于历史模板，不能污染普通库行为'
)
uploadApp.unmount()

// The operations panel must pass pause/resume callbacks to the real shared scope.
document.hidden = false
const operationsUser = Vue.reactive({ uid: 'admin-a', departmentId: 1, userRole: 'superadmin', isAdmin: true })
const diagnostics = [], operationErrors = []
const operations = (await evaluate(
  '../../components/literature/LiteraturePdfOperations.vue',
  {
    '@/stores/user': { useUserStore: () => operationsUser },
    '@/composables/useLiteraturePollingScope': pollingScope,
    '@/apis/literature_api': { literaturePdfApi: {
      operations: (signal) => new Promise((resolve) => diagnostics.push({ signal, resolve })),
      unresolvedUsage: async () => ({ items: [] })
    } }
  }, {}, true
)).default
let operationsState
const operationsSetup = operations.setup
operations.setup = (props, context) => {
  operationsState = operationsSetup(props, context)
  return operationsState
}
operations.render = () => null
const operationsShown = Vue.ref(true)
const operationsApp = renderer.createApp({
  setup: () => () => Vue.h(Vue.KeepAlive, null, {
    default: () => operationsShown.value ? Vue.h(operations) : Vue.h('div')
  })
})
operationsApp.config.errorHandler = (error) => operationErrors.push(error)
operationsApp.mount({})
await settle()
assert.equal(diagnostics.length, 1)
operationsShown.value = false
await settle()
assert.equal(diagnostics[0].signal.aborted, true)
assert.equal(operationsState.busy.value, false)
diagnostics[0].resolve({ owner: 'late-admin-a' })
await settle()
assert.equal(operationsState.snapshot.value, null)
operationsShown.value = true
await settle()
assert.equal(diagnostics.length, 2)
operationsUser.uid = 'admin-b'
await settle()
assert.equal(diagnostics[1].signal.aborted, true)
diagnostics[1].resolve({ owner: 'late-admin-a' })
diagnostics[2].resolve({ owner: 'admin-b' })
await settle()
assert.equal(operationsState.snapshot.value.owner, 'admin-b')
document.hidden = true
for (const callback of visibility) callback()
await settle()
assert.equal(operationsState.busy.value, false)
document.hidden = false
for (const callback of visibility) callback()
await settle()
assert.equal(diagnostics.length, 4)
operationsApp.unmount()
assert.equal(diagnostics[3].signal.aborted, true)
assert.equal(operationErrors.length, 0, '诊断面板的停用/可见性/恢复不得抛出回调异常')
// Actual PDF component: hidden pages, owner changes, blob/download completion and cancellation.
document.hidden = false
const pdfUser = Vue.reactive({ uid: 'reader-a', departmentId: 1, userRole: 'user' })
const pdfRequests = [], blobRequests = [], cancelRequests = [], pdfTimers = new Map()
const revoked = [], created = [], saved = [], pdfErrors = []
let timerSequence = 0
const pdfDocument = {
  ...document,
  createElement: () => ({ click: () => saved.push(true) })
}
const pdf = (await evaluate(
  '../../components/literature/LiteraturePdfWorkspace.vue',
  {
    '@/stores/user': { useUserStore: () => pdfUser },
    '@/composables/useLiteraturePollingScope': pollingScope,
    '@/apis/literature_api': {
      literaturePdfApi: {
        bundle: (rid, signal) => new Promise((resolve) => pdfRequests.push({ rid, signal, resolve })),
        cancel: (rid, jobId, signal) => new Promise((resolve) => cancelRequests.push({ rid, jobId, signal, resolve }))
      },
      fetchLiteratureAssetBlob: (uri, signal) => new Promise((resolve) => blobRequests.push({ uri, signal, resolve }))
    }
  }, {
    document: pdfDocument,
    URL: {
      createObjectURL: () => { const url = `blob:fixture-${created.length}`; created.push(url); return url },
      revokeObjectURL: (url) => revoked.push(url)
    },
    setTimeout: (callback) => { const id = ++timerSequence; pdfTimers.set(id, callback); return id },
    clearTimeout: (id) => pdfTimers.delete(id)
  }, true
)).default
let pdfState
const pdfSetup = pdf.setup
pdf.setup = (props, context) => {
  pdfState = pdfSetup(props, context)
  return pdfState
}
pdf.render = () => null
const pdfShown = Vue.ref(true)
const pdfApp = renderer.createApp({ setup: () => () => Vue.h(Vue.KeepAlive, null, {
  default: () => pdfShown.value ? Vue.h(pdf, { rid: 'paper-a', originalUrl: 'blob:original' }) : Vue.h('div')
}) })
pdfApp.config.errorHandler = (error) => pdfErrors.push(error)
pdfApp.mount({})
await settle()
assert.equal(pdfRequests.length, 1)
pdfShown.value = false
await settle()
assert.equal(pdfRequests[0].signal.aborted, true)
pdfRequests[0].resolve({ ready: false, owner: 'late-reader-a' })
await settle()
assert.equal(pdfState.bundle.value, null)
pdfShown.value = true
await settle()
const completed = { ready: true, status: 'SUCCEEDED', job_id: 32, outputs: { pdf_zh: { uri: 'kbasset://fixture' } } }
pdfRequests[1].resolve(completed)
await settle()
assert.equal(blobRequests.length, 1)
pdfUser.uid = 'reader-b'
await settle()
assert.equal(blobRequests[0].signal.aborted, true)
blobRequests[0].resolve({})
await settle()
assert.equal(created.length, 0, '上个账号的 PDF 不得进入 Blob URL')
pdfRequests[2].resolve(completed)
await settle()
blobRequests[1].resolve({})
await settle()
assert.equal(created.length, 1)
const pendingDownload = pdfState.download('pdf_zh')
const pendingCancel = pdfState.cancel()
await settle()
pdfUser.uid = 'reader-c'
await settle()
assert.equal(pdfState.bundle.value, null)
assert.equal(pdfState.translatedUrl.value, '')
assert.equal(revoked[0], created[0])
assert.equal(blobRequests[2].signal.aborted, true)
assert.equal(cancelRequests[0].signal.aborted, true)
blobRequests[2].resolve({})
cancelRequests[0].resolve({})
await pendingDownload
await pendingCancel
await settle()
assert.equal(saved.length, 0, '账号切换后的旧下载不得触发保存')
assert.equal(pdfRequests.length, 4, '旧取消请求不得刷新新账号')
pdfRequests[3].resolve({ ready: false, status: 'RUNNING', job_id: 32 })
await settle()
assert.equal(pdfTimers.size, 1)
document.hidden = true
for (const callback of visibility) callback()
await settle()
assert.equal(pdfTimers.size, 0)
pdfApp.unmount()
assert.equal(pdfErrors.length, 0)
console.log('KeepAlive, owner/visibility/late-response fencing, PDF download cleanup and intake modes passed')
