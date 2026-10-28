// Real component actions: authenticated assets, ownership fences and crop toggle.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import { cropRectangle } from '../literature_assistant.js'

const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const renderer = Vue.createRenderer({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null
})
const settle = async () => {
  for (let i = 0; i < 10; i++) { await Promise.resolve(); await Vue.nextTick() }
}
const pending = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const user = Vue.reactive({ uid: 'reader-a', departmentId: 1, userRole: 'user', isLoggedIn: true })
const created = [], revoked = [], downloads = [], requests = [], errors = []
class ObjectURL extends URL {
  static createObjectURL(blob) {
    const url = `blob:reader-action-${created.length}`
    created.push({ url, blob })
    return url
  }
  static revokeObjectURL(url) { revoked.push(url) }
}
const document = {
  createElement: () => ({ click() { downloads.push({ url: this.href, filename: this.download }) }, remove() {} }),
  body: { appendChild() {}, removeChild() {} }, querySelector: () => null
}
const attachment = { uri: 'kbasset://tenants/1/papers/one/file.pdf', text_uri: 'kbasset://tenants/1/papers/one/file.txt', filename: 'support.pdf' }
let listAttachments = async () => ({ attachments: [attachment] })
const asset = (uri, signal) => {
  const response = pending()
  requests.push({ uri, signal, ...response })
  return response.promise
}
const reviewData = { total: 1, items: [{ item_id: 'first', page: 1, render_digest: 'v1' }] }
const pageRequests = [], reviewRequests = []
const api = {
  apiGet: async (url, options = {}) => {
    reviewRequests.push(url)
    if (!url.includes('/page-image')) return structuredClone(reviewData)
    const response = pending()
    pageRequests.push({ url, signal: options.signal, ...response })
    return response.promise
  },
  apiPost: async () => ({})
}
async function mount(file, props, modules = {}, initialize = () => {}) {
  const source = await readFile(new URL(`../../components/literature/${file}.vue`, import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const script = descriptor.script?.content || compileScript(descriptor, { id: 'reader-buttons' }).content
  const module = { exports: {} }
  vm.runInNewContext((await transform(script, {
    format: 'cjs', define: { 'import.meta.url': '"file:///app/src/components/literature/Reader.vue"' }
  })).code, {
    module, exports: module.exports, console, AbortController, URL: ObjectURL, document,
    window: { devicePixelRatio: 1 }, Blob, File, setTimeout, clearTimeout,
    Worker: class { terminate() {} }, ResizeObserver: class { observe() {} disconnect() {} },
    require: (id) => {
      if (id === 'vue') return Vue
      if (modules[id]) return modules[id]
      if (id === '@/stores/user') return { useUserStore: () => user }
      if (id === '@/apis/base') return api
      if (id === '@/apis/literature_api') return {
        listLiteratureAttachments: (...args) => listAttachments(...args),
        uploadLiteratureAttachment: async () => ({}),
        fetchLiteratureAssetBlob: asset,
        buildLiteratureAssetUrl: (uri) => `/api/literature/assets?uri=${encodeURIComponent(uri)}`
      }
      if (id === '@/composables/useLiteratureSessions') return { confirmAction: async () => true }
      if (id === '@/utils/literature_assistant') return { cropRectangle }
      if (id === '@/utils/pdfLocator') return { pdfLocatorRequest: () => ({}) }
      if (id === '@/utils/pdfHighlight') return { physicalFragmentRects: () => [], pdfRectToScreen: () => null }
      if (id === '@/apis/workspace_api') return { resolveWorkspacePdfEvidence: async () => ({}) }
      if (id.endsWith('.vue')) return { default: { render: () => null } }
      throw Error(`Unmocked module ${id}`)
    }
  })
  const component = module.exports.default
  component.render = () => null
  const app = renderer.createApp(component, props)
  app.config.errorHandler = (error) => errors.push(error)
  const instance = app.mount({})
  const state = descriptor.scriptSetup ? instance.$.setupState : instance
  initialize(state)
  await settle()
  return { state, app, setProps: (changes) => Object.assign(instance.$.props, changes) }
}

const failures = []
async function check(name, run) {
  try { await run() } catch (error) { failures.push(`${name}: ${error.message}`) }
}
await check('Authenticated attachment actions', async () => {
  const { state, app, setProps } = await mount('AttachmentPanel', { rid: 'paper-a' })
  try {
    assert.equal(typeof state.downloadAttachment, 'function', 'Attachment links must request authenticated bytes')
    const first = state.downloadAttachment(attachment)
    await settle()
    assert.equal(requests.at(-1).uri, attachment.uri)
    assert.ok(requests.at(-1).signal instanceof AbortSignal)
    const bytes = new Blob(['%PDF-test'], { type: 'application/pdf' })
    requests.at(-1).resolve(bytes)
    await first
    assert.equal(created.at(-1).blob, bytes)
    assert.deepEqual(downloads.at(-1), { url: created.at(-1).url, filename: 'support.pdf' })
    assert.ok(revoked.includes(created.at(-1).url), 'Download object URL is released')

    const text = state.downloadAttachment(attachment, true)
    await settle()
    assert.equal(requests.at(-1).uri, attachment.text_uri)
    requests.at(-1).resolve(new Blob(['text'], { type: 'text/plain' }))
    await text
    assert.equal(downloads.at(-1).filename, 'support.pdf.txt')

    const canceled = state.downloadAttachment(attachment)
    await settle()
    const late = requests.at(-1), before = downloads.length
    user.uid = 'reader-b'
    await settle()
    assert.equal(late.signal.aborted, true)
    late.resolve(bytes)
    await canceled
    assert.equal(downloads.length, before, 'Old-account replies never trigger a new download')

    const failed = state.downloadAttachment(attachment)
    await settle()
    requests.at(-1).reject(Error('Source access revoked'))
    await failed
    assert.match(state.notice, /Source access revoked/)

    const oldList = pending()
    listAttachments = () => oldList.promise
    setProps({ rid: 'paper-b' })
    await settle()
    app.unmount()
    oldList.resolve({ attachments: [{ ...attachment, filename: 'late-private.pdf' }] })
    await settle()
    assert.equal(state.attachments.length, 0)
  } finally { app.unmount(); listAttachments = async () => ({ attachments: [attachment] }) }
})
await check('Unparsed catalog review controls', async () => {
  const before = reviewRequests.length
  const { state, app } = await mount('ReviewPanel', { rid: 'catalog-only', sourceRevisionId: '' })
  try {
    assert.equal(state.canReview, false, 'Catalog records cannot request OCR review without a source revision')
    await state.load()
    await state.triggerDual()
    assert.equal(reviewRequests.length, before)
    assert.match(state.reviewHint, /PDF\/Markdown/)
  } finally { app.unmount() }
})
await check('Authenticated review images', async () => {
  const { state, app, setProps } = await mount('ReviewPanel', { rid: 'paper-a', sourceRevisionId: 'revision-a' })
  try {
    const opening = state.togglePage(reviewData.items[0])
    await settle()
    const metadata = pageRequests.at(-1)
    assert.equal(metadata.url, '/api/literature/papers/paper-a/review/page-image?page=1')
    metadata.resolve({ uri: 'kbasset://tenants/1/papers/one/page-1.png', boxes: [] })
    await settle()
    assert.equal(requests.at(-1)?.uri, 'kbasset://tenants/1/papers/one/page-1.png', 'Page image bytes must use authenticated API')
    requests.at(-1).resolve(new Blob(['png'], { type: 'image/png' }))
    await opening
    assert.ok(state.pageImageUrl.startsWith('blob:'))
    const firstURL = state.pageImageUrl
    await state.togglePage(reviewData.items[0])
    assert.equal(state.pageImageUrl, '')
    assert.ok(revoked.includes(firstURL), 'Collapsing the review page releases its image')

    const lateOpen = state.togglePage(reviewData.items[0])
    await settle()
    const stale = pageRequests.at(-1)
    setProps({ rid: 'paper-b' })
    await settle()
    assert.equal(stale.signal.aborted, true)
    const count = created.length
    stale.resolve({ uri: 'kbasset://tenants/1/papers/one/old.png', boxes: [] })
    await lateOpen
    assert.equal(created.length, count)
    assert.equal(state.expanded, '')

    const dying = state.togglePage(reviewData.items[0])
    await settle()
    pageRequests.at(-1).resolve({ uri: 'kbasset://tenants/1/papers/two/page.png', boxes: [] })
    await settle()
    const lateBytes = requests.at(-1)
    app.unmount()
    assert.equal(lateBytes.signal.aborted, true)
    lateBytes.resolve(new Blob(['late'], { type: 'image/png' }))
    await dying
    assert.equal(created.length, count, 'Unmounted review pages cannot acquire a new Blob URL')
  } finally { app.unmount() }
})
await check('PDF cancel crop button', async () => {
  const pdfjs = {
    PDFWorker: class { destroy() {} },
    getDocument: () => ({ destroy: async () => {}, promise: Promise.resolve({ numPages: 2,
      getPage: async () => ({ getViewport: ({ scale }) => ({ width: 600 * scale, height: 800 * scale, scale }),
        render: () => ({ cancel() {}, promise: Promise.resolve() }), getTextContent: async () => ({ items: [] }) })
    }) }),
    TextLayer: class { cancel() {} async render() {} }
  }
  const { state, app } = await mount('LiteraturePdfReader', { url: 'blob:authorized' }, { 'pdfjs-dist': pdfjs }, (value) => {
    value.canvas = { style: {}, getContext: () => ({}) }
    value.textRoot = { replaceChildren() {} }
    value.scroll = { clientWidth: 720 }
  })
  try {
    assert.equal(state.rendered, true)
    state.toggleCrop()
    assert.equal(state.cropping, true)
    state.toggleCrop()
    assert.equal(state.cropping, false, 'Cancel crop button must leave crop mode')
    assert.equal(state.crop, null)
  } finally { app.unmount() }
})
await check('Reading render eligibility', async () => {
  let paper = { rid: 'catalog-only', source_revision_id: null, render: null }
  const triggered = []
  const { state, app, setProps } = await mount('PaperDetail', { rid: paper.rid }, {
    'ant-design-vue': { message: { error() {}, warning() {}, success() {}, info() {} } },
    '@/utils/litDialogs': { litConfirm: async () => false, litPrompt: async () => null },
    '@/composables/useTablistKeyboard': { useTablistKeyboard: () => ({ onTabKeydown() {} }) },
    '@/composables/useLiteraturePollingScope': { useLiteraturePollingScope: () => Vue.ref(true) },
    '@/apis/literature_api': {
      getLiteraturePaper: async () => structuredClone(paper),
      triggerLiteratureRender: async (rid) => { triggered.push(rid); return { status: 'SUCCEEDED', reused: true } },
      triggerLiteratureEnrich: async () => ({}),
      literatureWorkspaceApi: { sessions: async () => ({ items: [] }) }
    }
  })
  try {
    assert.equal(state.canRender, false)
    assert.match(state.renderHint, /PDF\/Markdown/)
    await state.doRender()
    assert.equal(triggered.length, 0, 'Disabled catalog rendering must not submit a known-invalid request')
    paper = { rid: 'markdown-ready', source_revision_id: 'markdown-revision', render: { status: 'SUCCEEDED' } }
    setProps({ rid: paper.rid })
    await settle()
    assert.equal(state.canRender, true, 'Existing Markdown reading products remain usable')
    await state.doRender()
    assert.deepEqual(triggered, ['markdown-ready'])
    paper = { rid: 'pdf-native', source_revision_id: 'pdf-revision', render: null }
    setProps({ rid: paper.rid })
    await settle()
    assert.equal(state.canRender, true, 'Native parsed PDF remains eligible')
  } finally { app.unmount() }
})
assert.deepEqual(failures, [])
assert.equal(errors.length, 0)
console.log('Reader buttons passed: authenticated attachments/review images, stale replies/Blob cleanup and cancel crop')
