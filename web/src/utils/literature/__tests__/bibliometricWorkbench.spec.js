import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import * as charts from '../bibliometricCharts.js'
import { useTablistKeyboard } from '../../../composables/useTablistKeyboard.js'
const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}
const user = Vue.reactive({ uid: 'a', departmentId: 1, isLoggedIn: true })
const lists = [],
  summaries = [],
  uploads = [],
  batchCalls = []
const api = {
  metricSources: async () => ({ items: [{ database: '', papers: 26 }] }),
  referenceBatches: async (offset = 0) => {
    batchCalls.push(offset)
    return {
      items: Array.from({ length: offset ? 2 : 50 }, (_, index) => ({
        batch_id: offset ? `old-${offset + index}` : index ? `batch-${index}` : 'batch-a',
        source: `history-${offset + index}.csv`,
        format: 'pubmed-csv',
        report: { papers_unique: 1 }
      })),
      has_more: !offset
    }
  },
  metricRecords: (filters, offset, limit) => {
    const request = { ...deferred(), filters, offset, limit }
    lists.push(request)
    return request.promise
  },
  bibliometrics: (_top, filters) => {
    const request = { ...deferred(), filters }
    summaries.push(request)
    return request.promise
  },
  metricNetwork: async () => {
    throw Error('Default records view must not compute the expensive network')
  },
  importReferences: (file) => {
    const request = { ...deferred(), file }
    uploads.push(request)
    return request.promise
  }
}
const descriptor = parse(
  await readFile(
    new URL('../../../components/literature/BibliometricsPanel.vue', import.meta.url),
    'utf8'
  )
).descriptor
const module = { exports: {} }
vm.runInNewContext(
  (await transform(compileScript(descriptor, { id: 'workbench' }).content, { format: 'cjs' })).code,
  {
    module,
    exports: module.exports,
    Function,
    document: { documentElement: {} },
    getComputedStyle: () => ({ getPropertyValue: () => '#123456' }),
    window: { addEventListener() {}, removeEventListener() {} },
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    require: (id) => {
      if (id === './TopicModelingPanel.vue') return {}
      if (id === 'vue') return Vue
      if (id === '@/apis/literature_api') return { literatureWorkspaceApi: api }
      if (id === '@/stores/user') return { useUserStore: () => user }
      if (id === '@/composables/useLiteraturePollingScope')
        return { useLiteraturePollingScope: () => Vue.ref(true) }
      if (id === '@/composables/useTablistKeyboard') return { useTablistKeyboard }
      if (id === '@/utils/literature/bibliometricCharts') return charts
      if (id === '@/utils/chartColors')
        return { getColorByIndex: () => '#123456', initColorPalette() {} }
      if (id === 'echarts')
        return {
          init: () => {
            throw Error('Do not initialize charts in the records view')
          }
        }
      throw Error('Unexpected dependency: ' + id)
    }
  }
)
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
const component = module.exports.default
component.render = () => null
const app = renderer.createApp(component)
const state = app.mount({}).$.setupState
const settle = async () => {
  for (let i = 0; i < 8; i++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
const summary = { overview: { papers: 1 }, yearly: [] }
const page = { items: [{ rid: 'paper', title: 'Rice' }], total: 26 }
try {
  await settle()
  assert.equal(state.view, 'records')
  assert.equal(
    lists[0].filters.only_imports,
    false,
    'Default corpus must include historical papers even if no upload batch exists'
  )
  lists[0].resolve(page)
  summaries[0].resolve(summary)
  await settle()
  assert.equal(state.records[0].title, 'Rice')
  await state.loadBatches(true)
  assert.equal(batchCalls.at(-1), 50)
  assert.equal(state.batches.length, 52, 'Older imports must remain individually selectable')
  await state.loadBatches()
  assert.equal(state.batches.length, 52, 'Refresh must not drop already loaded historical choices')
  state.queryDraft = 'old filter'
  state.yearFromDraft = 2024
  state.selectDatabase('cnki')
  assert.equal(state.scopeChoice, 'all')
  assert.equal(lists.at(-1).filters.database, 'cnki')
  assert.equal(lists.at(-1).filters.query, '')
  assert.equal(lists.at(-1).filters.year_from, null)
  const oldDatabase = lists.at(-1)
  state.selectDatabase('wanfang')
  oldDatabase.resolve({ items: [{ title: 'Wrong database' }], total: 100 })
  await settle()
  assert.equal(
    state.records.length,
    0,
    'Late source results cannot overwrite the selected database'
  )
  lists.at(-1).resolve(page)
  summaries.at(-1).resolve(summary)
  await settle()
  state.changePage(1)
  assert.equal(lists.at(-1).offset, 25)
  lists.at(-1).resolve({ items: [], total: 26 })
  await settle()
  state.scopeChoice = 'batch-a'
  state.selectScope()
  assert.equal(
    lists.at(-1).filters.database,
    '',
    'Selecting a file clears the previous database constraint'
  )
  assert.deepEqual(Array.from(lists.at(-1).filters.batch_ids), ['batch-a'])
  state.queryDraft = 'rice%'
  state.yearFromDraft = 2020
  state.yearToDraft = 2026
  state.applyFilters()
  assert.equal(lists.at(-1).offset, 0)
  assert.deepEqual(
    JSON.parse(JSON.stringify(lists.at(-1).filters)),
    JSON.parse(JSON.stringify(summaries.at(-1).filters))
  )
  assert.equal(lists.at(-1).filters.batch_ids[0], 'batch-a')
  assert.equal(lists.at(-1).filters.query, 'rice%')
  const staleList = lists.at(-1)
  state.scopeChoice = 'all'
  state.applyFilters()
  staleList.resolve({ items: [{ title: 'Wrong scope' }], total: 100 })
  await settle()
  assert.equal(state.records.length, 0, 'Late filter results cannot cross scopes')
  state.yearFromDraft = 2027
  const before = lists.length
  state.applyFilters()
  assert.equal(lists.length, before)
  assert.match(state.recordsError, /起始年份/)
  const files = [
    { name: 'CNKI.xls', size: 374624 },
    { name: 'WanFangdata.xlsx', size: 296937 }
  ]
  const event = { target: { files, value: 'selected' } }
  const importing = state.uploadReferences(event)
  assert.equal(event.target.value, '')
  uploads
    .at(-1)
    .resolve({ batch_id: 'batch-a', papers_written: 1, papers_skipped: 0, papers_unique: 1 })
  await settle()
  uploads.at(-1).reject(Error('Malformed workbook'))
  await importing
  assert.equal(state.importReports.length, 2)
  assert.equal(state.importReports[1].error, 'Malformed workbook')
  assert.equal(state.importing, false)
  assert.equal(state.view, 'records')
  assert.equal(lists.at(-1).filters.only_imports, false)
  assert.equal(lists.at(-1).filters.database, '')
  assert.equal(state.batches.length, 52, 'A new upload must not replace previous receipts')
  assert.equal(lists.at(-1).filters.query, '')
  const next = state.uploadReferences({ target: { files, value: 'selected' } })
  const pending = uploads.at(-1)
  user.uid = 'new-owner'
  pending.resolve({ source: 'Old private filename', papers_written: 1 })
  await next
  assert.equal(
    state.importReports.length,
    0,
    'Upload receipt from a former actor must not render under the new login'
  )
  assert.equal(state.records.length, 0)
  const count = uploads.length
  await state.uploadReferences({
    target: { files: [{ name: 'huge.csv', size: 65 * 1024 * 1024 }], value: '' }
  })
  assert.equal(uploads.length, count)
  console.log(
    'Bibliometric workbench passed: upload to list, partial failures, same-scope pagination/analysis, lazy charts, owner/filter fences, budgets'
  )
} finally {
  app.unmount()
}

const reopened = renderer.createApp(component)
const restored = reopened.mount({}).$.setupState
try {
  await settle()
  assert.equal(restored.scopeChoice, 'all')
  assert.equal(lists.at(-1).filters.only_imports, false)
  lists.at(-1).resolve({ items: [{ rid: 'history', title: 'Previously imported' }], total: 1 })
  summaries.at(-1).resolve(summary)
  await settle()
  assert.equal(
    restored.records[0].title,
    'Previously imported',
    'Reopening reloads server-owned records'
  )
} finally {
  reopened.unmount()
}
assert.match(
  descriptor.template.content,
  /@change="selectScope"/,
  'Batch selection applies immediately'
)
assert.match(descriptor.template.content, /@click="selectDatabase\(item.key\)"/)

// Exercise actual API serialization, not copied query-construction logic.
const urls = [],
  apiModule = { exports: {} }
vm.runInNewContext(
  (
    await transform(
      await readFile(new URL('../../../apis/literature_api.js', import.meta.url), 'utf8'),
      { format: 'cjs' }
    )
  ).code,
  {
    module: apiModule,
    exports: apiModule.exports,
    URLSearchParams,
    require: () => ({ apiGet: (url) => urls.push(url), apiPost() {}, apiPut() {}, apiDelete() {} })
  }
)
const filter = {
  only_imports: true,
  database: 'wos',
  batch_ids: ['a', 'b'],
  query: 'rice%_水稻',
  year_from: 2020,
  year_to: 2026
}
const client = apiModule.exports.literatureWorkspaceApi
client.metricRecords(filter, 25)
client.bibliometrics(20, filter)
client.metricNetwork('author', 50, 2, filter)
for (const url of urls) {
  const query = new URLSearchParams(url.split('?')[1])
  assert.deepEqual(query.getAll('batch_ids'), ['a', 'b'])
  assert.equal(query.get('database'), 'wos')
  assert.equal(query.get('query'), filter.query)
  assert.equal(query.get('only_imports'), 'true')
  assert.equal(query.get('year_from'), '2020')
}
