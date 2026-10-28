// Compiled Vue intake and real request/feed logic; all transport is local deferred fixtures.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import { normalizeTimeoutError } from '../apiError.js'

const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const deferred = () => {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const settle = async () => {
  for (let i = 0; i < 10; i++) { await Promise.resolve(); await Vue.nextTick() }
}
const renderer = Vue.createRenderer({
  createElement: () => ({ children: [] }), createText: () => ({}), createComment: () => ({}),
  setText() {}, setElementText() {}, patchProp() {},
  insert(node, parent) { parent.children ||= []; parent.children.push(node); node.parent = parent },
  remove() {}, parentNode: (node) => node.parent, nextSibling: () => null
})
async function evaluate(file, modules, globals = {}, sfc = false) {
  const source = await readFile(new URL(file, import.meta.url), 'utf8')
  const script = sfc ? compileScript(parse(source).descriptor, { id: 'intake-boundary' }).content : source
  const module = { exports: {} }
  vm.runInNewContext((await transform(script, { format: 'cjs' })).code, {
    module, exports: module.exports, AbortController, DOMException, Blob, FormData, URLSearchParams,
    console, require: (id) => id === 'vue' ? Vue : modules[id], ...globals
  })
  return module.exports
}
async function intake({ duplicate = false } = {}) {
  const user = Vue.reactive({ uid: 'owner-a', departmentId: 1, userRole: 'admin', isLoggedIn: true, isAdmin: true })
  const uploads = [], writes = [], confirmations = [], emitted = []
  const component = (await evaluate('../../components/literature/ImportPanel.vue', {
    '@/stores/user': { useUserStore: () => user },
    '@/composables/useLiteratureTaskFeed': { useLiteratureTaskFeed: () => ({ tasks: Vue.ref([]), refresh() {} }) },
    '@/composables/useLiteratureSettings': { parseOptionsPayload: () => ({}) },
    '@/composables/useTablistKeyboard': { useTablistKeyboard: () => ({ onTabKeydown() {} }) },
    '@/utils/ocr_engine': { resolveDefaultOcrEngine: () => 'mineru_official' },
    '@/composables/useLiteratureSessions': { confirmAction: () => { const item = deferred(); confirmations.push(item); return item.promise } },
    '@/apis/knowledge_api': {
      databaseApi: { getDatabases: async () => ({ databases: [{ kb_id: 'source', contract_key: 'pdf_evidence' }] }) },
      fileApi: { uploadFile: (file, kb, options) => { const item = { ...deferred(), file, kb, options }; uploads.push(item); return item.promise } },
      documentApi: { addDocuments: (kb, items, params, options) => { const item = { ...deferred(), kb, items, params, options }; writes.push(item); return item.promise } }
    },
    '@/apis/literature_api': { literatureWorkspaceApi: {} }
  }, {}, true)).default
  let state
  const setup = component.setup
  component.setup = (props, context) => { state = setup(props, context); return state }
  component.render = () => null
  const shown = Vue.ref(true)
  const app = renderer.createApp({ setup: () => () => Vue.h(Vue.KeepAlive, null, {
    default: () => shown.value ? Vue.h(component, { onImported: () => emitted.push(true) }) : Vue.h('div')
  }) })
  app.mount({})
  await settle()
  state.kbId.value = 'source'
  const start = (files = [{ name: 'one.pdf', size: 4 }, { name: 'two.pdf', size: 4 }]) =>
    state.importPdf({ target: { files, value: '' } })
  const acceptUpload = (index = 0) => uploads[index].resolve({ file_path: 'fixture/source.pdf', has_same_name: duplicate })
  return { app, state, user, shown, uploads, writes, confirmations, emitted, start, acceptUpload }
}

// An old upload that ignores cancellation must never become a parse submission under the new identity.
for (const boundary of ['owner', 'unmount', 'deactivate']) {
  const fixture = await intake()
  const pending = fixture.start()
  await settle()
  assert.equal(fixture.uploads.length, 1)
  if (boundary === 'owner') fixture.user.uid = 'owner-b'
  else if (boundary === 'unmount') fixture.app.unmount()
  else fixture.shown.value = false
  await settle()
  fixture.acceptUpload()
  await settle()
  assert.equal(fixture.writes.length, 0, `${boundary}: an old selection must not submit parsing`)
  assert.equal(fixture.uploads[0].options.signal.aborted, true)
  assert.equal(fixture.uploads.length, 1)
  await pending
  if (boundary !== 'unmount') fixture.app.unmount()
}
// Revocation while the duplicate dialog is open cannot grant an old confirmation to a new owner.
{
  const fixture = await intake({ duplicate: true })
  const pending = fixture.start()
  fixture.acceptUpload()
  await settle()
  assert.equal(fixture.confirmations.length, 1)
  fixture.user.departmentId = 2
  await settle()
  fixture.confirmations[0].resolve(true)
  await pending
  assert.equal(fixture.writes.length, 0)
  fixture.app.unmount()
}
// Explicit cancellation cannot retract a server-accepted request; it stops the remaining batch.
{
  const fixture = await intake()
  const pending = fixture.start()
  fixture.acceptUpload()
  await settle()
  assert.equal(fixture.writes.length, 1)
  fixture.state.cancelImport()
  assert.equal(fixture.writes[0].options.signal.aborted, true)
  fixture.writes[0].resolve({ result: { items: [{ parse_revision_id: 'already-accepted' }] } })
  await pending
  assert.equal(fixture.uploads.length, 1)
  assert.equal(fixture.state.busy.value, false)
  assert.match(fixture.state.notice.value, /核对/)
  fixture.app.unmount()
}
// A late completion from a cancelled batch must not release or overwrite a newer batch.
{
  const fixture = await intake()
  const oldBatch = fixture.start()
  fixture.state.cancelImport()
  const nextBatch = fixture.start([{ name: 'new.pdf', size: 4 }])
  fixture.acceptUpload(0)
  await oldBatch
  assert.equal(fixture.state.busy.value, true)
  assert.equal(fixture.writes.length, 0)
  fixture.acceptUpload(1)
  await settle()
  fixture.writes[0].resolve({ result: { items: [{ parse_revision_id: 'new-owner-ticket' }] } })
  await nextBatch
  assert.equal(fixture.state.results.value[0].name, 'new.pdf')
  assert.equal(fixture.state.results.value[0].ticket, 'new-owner-ticket')
  fixture.app.unmount()
}

// These adapters must forward the actual cancellation signal to fetch, not merely accept an ignored argument.
{
  const requests = [], controller = new AbortController()
  const modules = {
    './base': {
      apiAdminGet: (url, options) => { requests.push({ url, options }); return {} },
      apiAdminPost: (url, body, options) => { requests.push({ url, body, options }); return {} },
      apiPost: (url, body, options) => { requests.push({ url, body, options }); return {} }
    }
  }
  const knowledge = await evaluate('../../apis/knowledge_api.js', modules)
  await knowledge.databaseApi.getDatabases({ signal: controller.signal })
  await knowledge.fileApi.uploadFile(new Blob(['fixture']), 'source', { signal: controller.signal })
  await knowledge.documentApi.addDocuments('source', ['fixture/source.pdf'], {}, { signal: controller.signal })
  const literature = await evaluate('../../apis/literature_api.js', modules)
  await literature.literatureWorkspaceApi.importMarkdown({ markdown: 'fixture' }, { signal: controller.signal })
  await literature.literatureWorkspaceApi.importReferences(new Blob(['fixture']), 'auto', { signal: controller.signal })
  assert.equal(requests.length, 5)
  assert.ok(requests.every((request) => request.options.signal === controller.signal))
  assert.ok(requests[1].body instanceof FormData)
}

// Use the actual base helper and shared feed: an external cancellation signal must retain a deadline.
const timers = new Map(), intervals = new Map()
let timerId = 0, intervalId = 0, mode = 'success'
const fetches = []
const owner = Vue.reactive({ uid: 'owner-a', departmentId: 1, isLoggedIn: true, getAuthHeaders: () => ({ Authorization: 'fixture-only' }) })
const api = await evaluate('../../apis/base.js', {
  '@/stores/user': { useUserStore: () => owner, checkAdminPermission() {}, checkSuperAdminPermission() {} },
  'ant-design-vue': { message: { error() {} } },
  '@/utils/apiError': { normalizeTimeoutError }
}, {
  setTimeout: (callback, ms) => { const id = ++timerId; timers.set(id, { callback, ms }); return id },
  clearTimeout: (id) => timers.delete(id),
  fetch: (_url, options) => {
    fetches.push(options)
    if (mode === 'success') return Promise.resolve({ ok: true, headers: { get: () => 'application/json' }, json: async () => ({ tasks: [{ ref: 'current' }] }) })
    if (mode === 'blob-hang') return Promise.resolve({
      ok: true, headers: { get: () => 'application/pdf' },
      blob: () => new Promise((_resolve, reject) => {
        if (options.signal.aborted) reject(options.signal.reason)
        else options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true })
      })
    })
    return new Promise((_resolve, reject) => {
      const abort = () => reject(options.signal.reason)
      if (options.signal.aborted) abort()
      else options.signal.addEventListener('abort', abort, { once: true })
    })
  }
})
const feedModule = await evaluate('../../composables/useLiteratureTaskFeed.js', {
  '@/stores/user': { useUserStore: () => owner },
  '@/apis/literature_api': { listLiteratureTasks: (_limit, signal) => api.apiGet('/fixture/tasks', { signal }) }
}, {
  document: { hidden: false, addEventListener() {}, removeEventListener() {} },
  window: {
    setInterval: (callback) => { const id = ++intervalId; intervals.set(id, callback); return id },
    clearInterval: (id) => intervals.delete(id)
  }
})
let feed
const feedApp = renderer.createApp({ setup: () => { feed = feedModule.useLiteratureTaskFeed(); return () => null } })
feedApp.mount({})
await settle()
assert.equal(feed.tasks.value[0].ref, 'current')
mode = 'hang'
const failedRefresh = feed.refresh()
await settle()
const deadline = [...timers.values()].find((timer) => timer.ms === 30000)
assert.ok(deadline, 'Caller-provided signal still needs a 30 second deadline')
deadline.callback()
await failedRefresh
assert.match(feed.feedError.value, /过期/)
assert.equal(feed.tasks.value[0].ref, 'current', 'Timeout preserves the last known snapshot')
assert.equal(timers.size, 0, 'A settled request releases its deadline')
mode = 'success'
for (const callback of intervals.values()) callback()
await settle()
assert.equal(feed.feedError.value, '')
assert.equal(fetches.length, 3, 'The next tick recovers instead of remaining locked by a hung promise')
// Headers do not finish a binary download: keep caller cancellation and the 120s deadline until body consumption.
mode = 'blob-hang'
const binaryOwner = new AbortController()
const binary = await api.apiGet('/fixture/source.pdf', { signal: binaryOwner.signal }, true, 'blob')
const binaryRead = binary.blob()
binaryOwner.abort()
assert.equal(fetches.at(-1).signal.aborted, true, 'Account change must still stop the PDF body after headers')
await assert.rejects(binaryRead, { name: 'AbortError' })
assert.equal(timers.size, 0)
const slowBinary = await api.apiGet('/fixture/slow.pdf', {}, true, 'blob')
const slowRead = slowBinary.blob()
const slowDeadline = [...timers.values()].find((timer) => timer.ms === 120000)
assert.ok(slowDeadline, 'A pending binary body still has its deadline')
slowDeadline.callback()
await assert.rejects(slowRead, { name: 'TimeoutError' })
assert.equal(timers.size, 0)
// Intentional cancellation has its own AbortError and releases listeners/deadline too.
mode = 'hang'
const cancelSource = new AbortController()
const cancelled = api.apiGet('/fixture/cancel', { signal: cancelSource.signal })
cancelSource.abort()
await assert.rejects(cancelled, { name: 'AbortError' })
assert.equal(timers.size, 0)
feedApp.unmount()
console.log('Import owner/unmount/cancel boundaries and finite task polling recovery passed')
