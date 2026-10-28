// Actual Vue reader setup: delayed bytes, independent panes and identity fences.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import * as preferences from '../literature_reader.js'
import * as assistant from '../literature_assistant.js'

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
const source = await readFile(new URL('../../components/literature/ReaderTabs.vue', import.meta.url), 'utf8')
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1]
const user = Vue.reactive({ uid: 'reader-a', departmentId: 1, userRole: 'user' })
const requests = [], created = [], revoked = [], galleryRequests = []
const native = {
  abortAll() {}, revokeAll() {},
  resolveAssets: (uris) => {
    const response = pending()
    galleryRequests.push({ uris, ...response })
    return response.promise
  }
}
const module = { exports: {} }
vm.runInNewContext((await transform(script, { format: 'cjs' })).code, {
  module, exports: module.exports, console, AbortController,
  window: { localStorage: { getItem: () => null, setItem() {} } }, document: {},
  URL: {
    createObjectURL: () => { const url = `blob:test-${created.length}`; created.push(url); return url },
    revokeObjectURL: (url) => revoked.push(url)
  },
  require: (id) => {
    if (id === 'vue') return Vue
    if (id === '@/stores/user') return { useUserStore: () => user }
    if (id === '@/utils/literature_reader') return preferences
    if (id === '@/utils/literature_assistant') return assistant
    if (id === '@/composables/useLiteratureSessions') return { confirmAction: async () => false }
    if (id === '@/utils/asset_resolver') return {
      collectAssetUris: (text) => [...String(text).matchAll(/kbasset:\/\/[^)\s]+/g)].map((match) => match[0]),
      createAssetResolverSession: () => native
    }
    if (id === '@/apis/base') return {
      apiGet: (url, options = {}) => {
        const response = pending()
        requests.push({ url, signal: options.signal, ...response })
        return response.promise
      }
    }
    if (id.endsWith('.vue')) return { default: { render: () => null } }
    throw Error('Unmocked dependency: ' + id)
  }
})
const component = module.exports.default
component.render = () => null
const app = renderer.createApp(component, { rid: 'paper-a', paper: {}, versionToken: 'v1' })
const errors = []
app.config.errorHandler = (error) => errors.push(error)
const reader = app.mount({})
await settle()
const find = (rid, suffix) => requests.find((request) => request.url === `/api/literature/papers/${rid}/${suffix}`)
assert.ok(find('paper-a', 'reader'))
assert.ok(find('paper-a', 'source-pdf'), 'PDF must start while the body request is still pending')

const markdown = '# Ready original\n\nOriginal text stays readable.\n' +
  Array.from({ length: 10 }, (_, index) => `![figure](/api/literature/assets?uri=figure-${index})`).join('\n') +
  '\n![native](kbasset://file/revision/image.png)'
find('paper-a', 'reader').resolve({ ready: true, markdown, knowledge_base_id: 'kb-a' })
await settle()
assert.equal(reader.readerLoading, false)
assert.equal(reader.loading, false, 'Text pane must not wait for any image or PDF')
assert.match(reader.displayMarkdown, /Original text stays readable/)
assert.equal(galleryRequests.length, 0, 'Native gallery bytes are lazy; inline Markdown owns its own resolver')
const assetRequests = () => requests.filter((request) => request.url.startsWith('/api/literature/assets'))
assert.equal(assetRequests().length, 4, 'Legacy image loading must have bounded concurrency')
reader.current = 'pdf'
await settle()
assert.equal(reader.loading, true)
find('paper-a', 'source-pdf').resolve({ blob: async () => ({}) })
await settle()
assert.equal(reader.pdfLoading, false)
assert.ok(reader.pdfUrl)
assert.equal(reader.loading, false, 'PDF becomes readable while image requests are still pending')
reader.current = 'images'
await settle()
assert.equal(galleryRequests.length, 1)
galleryRequests[0].resolve(new Map([['kbasset://file/revision/image.png', 'blob:native-figure']]))
await settle()
assert.ok(reader.images.includes('blob:native-figure'))
assetRequests()[0].resolve({ blob: async () => ({}) })
await settle()
assert.equal(assetRequests().length, 5)
assert.ok(reader.images.length >= 2, 'Resolved figures appear progressively')

const oldPdf = reader.pdfUrl
reader.$.props.rid = 'paper-b'
await settle()
assert.equal(reader.pdfUrl, '')
assert.ok(revoked.includes(oldPdf))
assert.equal(find('paper-a', 'reader').signal.aborted, true)
const imageCount = created.length
for (const request of assetRequests()) request.resolve({ blob: async () => ({}) })
await settle()
assert.equal(created.length, imageCount, 'Stale image bytes cannot acquire a new object URL')
assert.equal(reader.images.length, 0)
find('paper-b', 'reader').reject(Error('body unavailable'))
find('paper-b', 'source-pdf').resolve({ blob: async () => ({}) })
await settle()
assert.match(reader.bundle.reason, /body unavailable/)
reader.current = 'pdf'
await settle()
assert.ok(reader.pdfUrl, 'Body failure must not prevent the original PDF from opening')
assert.equal(reader.loading, false)

const ownerReload = reader.load()
await settle()
const oldOwnerBody = requests.filter((request) => request.url.endsWith('/reader')).at(-1)
const oldOwnerPdf = requests.filter((request) => request.url.endsWith('/source-pdf')).at(-1)
user.uid = 'reader-b'
await settle()
assert.equal(reader.pdfUrl, '', 'Account change synchronously clears old bytes')
assert.equal(oldOwnerBody.signal.aborted, true)
assert.equal(oldOwnerPdf.signal.aborted, true)
const urlsBeforeOldOwner = created.length
oldOwnerBody.resolve({ ready: true, markdown: '# Old owner late text' })
oldOwnerPdf.resolve({ blob: async () => ({}) })
await ownerReload
await settle()
assert.equal(created.length, urlsBeforeOldOwner)
assert.equal(reader.bundle, null)
const currentBody = requests.filter((request) => request.url.endsWith('/reader')).at(-1)
const currentPdf = requests.filter((request) => request.url.endsWith('/source-pdf')).at(-1)
currentBody.resolve({ ready: true, markdown: '# New owner text' })
reader.current = 'pdf'
currentPdf.reject(Error('PDF unavailable'))
await settle()
assert.equal(reader.readerLoading, false)
assert.match(reader.displayMarkdown, /New owner text/)
assert.match(reader.pdfError, /PDF unavailable/)
assert.equal(reader.pdfUrl, '')
assert.ok(reader.visibleTabs.some((tab) => tab.key === 'pdf'), 'Failed selected PDF pane keeps its own error visible')

reader.$.props.rid = 'paper-c'
await settle()
const lateBody = find('paper-c', 'reader'), latePdf = find('paper-c', 'source-pdf')
app.unmount()
assert.equal(lateBody.signal.aborted, true)
assert.equal(latePdf.signal.aborted, true)
const countBeforeUnmountReply = created.length
lateBody.resolve({ ready: true, markdown: '# Late' })
latePdf.resolve({ blob: async () => ({}) })
await settle()
assert.equal(created.length, countBeforeUnmountReply)
assert.equal(errors.length, 0)
console.log('Reader responsiveness passed: parallel text/PDF, bounded progressive images, lazy gallery, independent errors and identity/unmount fences')
