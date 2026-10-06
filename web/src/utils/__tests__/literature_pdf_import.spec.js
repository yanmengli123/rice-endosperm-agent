import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import { resolveDefaultOcrEngine } from '../ocr_engine.js'

assert.equal(
  resolveDefaultOcrEngine({ contractKey: 'pdf_evidence', configuredEngine: 'rapid_ocr' }),
  'mineru_official'
)
assert.equal(resolveDefaultOcrEngine({ formatTemplate: 'pdf_literature' }), 'mineru_official')
assert.equal(resolveDefaultOcrEngine({ configuredEngine: 'deepseek_ocr' }), 'deepseek_ocr')

const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const apiSource = await readFile(new URL('../../apis/knowledge_api.js', import.meta.url), 'utf8')
const apiCode = (await transform(apiSource, { format: 'cjs' })).code
let uploadRequest
const apiModule = { exports: {} }
vm.runInNewContext(apiCode, {
  module: apiModule,
  exports: apiModule.exports,
  FormData,
  require: () => ({
    apiAdminPost: (...args) => {
      uploadRequest = args
      return {}
    }
  })
})
await apiModule.exports.fileApi.uploadFile(new Blob(['PDF']), 'kb_pdf')
assert.equal(uploadRequest[0], '/api/knowledge/files/upload?kb_id=kb_pdf')
assert.ok(uploadRequest[1] instanceof FormData)
assert.equal(uploadRequest[2], undefined, '浏览器生成 multipart boundary')
const multipartRequest = new Request('http://localhost/upload', {
  method: 'POST',
  body: uploadRequest[1]
})
assert.match(multipartRequest.headers.get('Content-Type'), /multipart\/form-data; boundary=/)
assert.equal((await multipartRequest.formData()).get('file').size, 3)

const source = await readFile(
  new URL('../../components/literature/ImportPanel.vue', import.meta.url),
  'utf8'
)
const { descriptor } = parse(source)
const code = (
  await transform(compileScript(descriptor, { id: 'pdf-import' }).content, { format: 'cjs' })
).code
const tasks = Vue.ref([])
let uploadFails = true
let submittedParams
let refreshes = 0
const modules = {
  vue: Vue,
  '@/utils/ocr_engine': { resolveDefaultOcrEngine },
  '@/apis/knowledge_api': {
    databaseApi: {
      getDatabases: async () => ({ databases: [{ kb_id: 'kb_pdf', contract_key: 'pdf_evidence' }] })
    },
    fileApi: {
      uploadFile: async () => {
        if (uploadFails) throw Error('Missing boundary in multipart')
        return { file_path: 'minio://test/a.pdf', content_hash: 'sha', size: 3 }
      }
    },
    documentApi: {
      addDocuments: async (_kb, _items, params) => {
        submittedParams = params
        return { result: { items: [{ parse_revision_id: 'rev1' }] } }
      }
    }
  },
  '@/apis/literature_api': { literatureWorkspaceApi: {} },
  '@/composables/useLiteratureSettings': { parseOptionsPayload: () => ({ language: 'auto' }) },
  '@/composables/useLiteratureTaskFeed': {
    useLiteratureTaskFeed: () => ({ tasks, refresh: async () => {} })
  },
  '@/apis/base': { apiGet: async () => ({ items: [] }) },
  '@/stores/user': { useUserStore: () => ({ isAdmin: true }) }
}
const componentModule = { exports: {} }
vm.runInNewContext(code, {
  module: componentModule,
  exports: componentModule.exports,
  Blob,
  window: { confirm: () => true },
  require: (id) => modules[id] || (id.endsWith('.vue') ? { default: {} } : null)
})
const component = componentModule.exports.default
let state
component.render = () => null
const renderer = Vue.createRenderer({
  createElement: () => ({}),
  createComment: () => ({}),
  insert() {},
  remove() {},
  parentNode: () => null,
  nextSibling: () => null
})
const setup = component.setup
component.setup = (props, context) => {
  state = setup(props, context)
  return state
}
const app = renderer.createApp(component, { onImported: () => refreshes++ })
app.mount({})
for (let i = 0; i < 8; i++) {
  await Promise.resolve()
  await Vue.nextTick()
}
state.kbId.value = 'kb_pdf'
const event = () => ({ target: { files: [{ name: 'a.pdf', size: 3 }], value: 'a.pdf' } })
await state.importPdf(event())
assert.match(state.notice.value, /没有 PDF/)
assert.match(state.results.value[0].message, /boundary/)
uploadFails = false
await state.importPdf(event())
assert.equal(submittedParams.auto_index, true)
assert.equal(submittedParams.ocr_engine, 'mineru_official')
assert.equal(submittedParams.content_hashes['minio://test/a.pdf'], 'sha')
assert.equal(submittedParams.file_sizes['minio://test/a.pdf'], 3)
assert.match(state.results.value[0].message, /上传成功/)
const beforeCompletion = refreshes
tasks.value = [
  { type: 'ingest', ref: 'rev1', is_terminal: true, is_failure: false, paper_rid: 'paper1' }
]
await Vue.nextTick()
assert.match(state.results.value[0].message, /入库成功/)
assert.ok(refreshes > beforeCompletion, '终态刷新文献目录')
await state.importPdf(event())
tasks.value = [
  { type: 'ingest', ref: 'rev1', is_terminal: true, is_failure: true, error: '质量门禁拒绝' }
]
await Vue.nextTick()
assert.match(state.results.value[0].message, /导入失败：质量门禁拒绝/)
await state.importPdf(event())
tasks.value = [
  {
    type: 'ingest',
    ref: 'rev1',
    is_terminal: true,
    is_failure: false,
    status: 'CANCELLED',
    paper_rid: 'paper-from-previous-run',
    stage: '已取消'
  }
]
await Vue.nextTick()
assert.equal(state.results.value[0].message, '已取消')
state.processingMode.value = 'parse_only'
await state.importPdf(event())
assert.equal(submittedParams.auto_index, false)
assert.equal(submittedParams.scientific_processing_mode, 'parse_only')
tasks.value = [
  { type: 'ingest', ref: 'rev1', is_terminal: true, is_failure: false, status: 'PARSED_ONLY' }
]
await Vue.nextTick()
assert.match(state.results.value[0].message, /草稿（未入库）/)
app.unmount()
console.log('PDF upload boundary, intake, completion, rejection and cancellation passed')
