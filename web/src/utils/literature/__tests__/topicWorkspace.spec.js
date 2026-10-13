import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import * as charts from '../topicCharts.js'
import { useTablistKeyboard } from '../../../composables/useTablistKeyboard.js'

const { parse, compileScript, compileTemplate } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const source = await readFile(new URL('../../../components/literature/TopicModelingPanel.vue', import.meta.url), 'utf8')
// 科学口径文案回归锁：这些措辞是防误读的最后防线（敏感性≠置信区间、权重≠词频、
// 桑基=构成≠迁移、年度分母含离群），只活在模板里，一次重构就会悄悄丢——锁进 spec。
const CALIBERS = [
  '敏感性比较，不是稳定性置信区间',
  '主题编号不能跨模型直接比较',
  '权重词云与原始词频不同',
  '年度分母包含离群记录',
  '表示构成，不代表文献迁移',
  '质量、参数与时间演化诊断',
  '离群 (-1)',
]
for (const caliber of CALIBERS) assert.ok(source.includes(caliber), `面板口径文案缺失: ${caliber}`)
const descriptor = parse(source).descriptor
assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: 'TopicModelingPanel.vue', id: 'topic-tests' }).errors, [])
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const user = Vue.reactive({ uid: 'alice', departmentId: 1, isLoggedIn: true })
const previewCalls = [], details = [], history = [], imports = []
const api = {
  topicCapabilities: async () => ({ worker_available: false, models: [] }),
  topicRuns: async (offset) => { history.push(offset); return { items: [], has_more: false, next_offset: offset + 25 } },
  previewTopics: () => { const call = deferred(); previewCalls.push(call); return call.promise },
  topicRun: (id) => { const call = { ...deferred(), id }; details.push(call); return call.promise },
  importTopics: () => { const call = deferred(); imports.push(call); return call.promise },
}
const module = { exports: {} }
vm.runInNewContext((await transform(compileScript(descriptor, { id: 'topic-tests' }).content, { format: 'cjs' })).code, {
  module, exports: module.exports, setTimeout, clearTimeout, console,
  require(id) {
    if (id === 'vue') return Vue
    if (id === '@/apis/literature_api') return { literatureWorkspaceApi: api }
    if (id === '@/stores/user') return { useUserStore: () => user }
    if (id === '@/stores/theme') return { useThemeStore: () => ({ isDark: false }) }
    if (id === '@/composables/useLiteraturePollingScope') return { useLiteraturePollingScope: () => Vue.ref(true) }
    if (id === '@/composables/useTablistKeyboard') return { useTablistKeyboard }
    if (id === '@/utils/literature/topicCharts') return charts
    if (id === '@/utils/chartColors') return { getColorByIndex: () => '#123456', initColorPalette() {} }
    if (id === 'echarts') return { init() { throw Error('Do not initialize a chart without data') } }
    throw Error('Unexpected dependency ' + id)
  }
})
const renderer = Vue.createRenderer({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), insert() {}, remove() {}, setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null })
const component = module.exports.default; component.render = () => null
const props = Vue.reactive({ filters: { database: 'pubmed' } })
const app = renderer.createApp(component, props), state = app.mount({}).$.setupState
const settle = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); await Vue.nextTick() } }
try {
  await settle()
  assert.equal(state.options.allow_remote, false, 'No paid calls by default')
  const pending = state.preview(); await settle()
  props.filters.database = 'wos'; await settle()
  previewCalls[0].resolve({ documents: 12 }); await pending; await settle()
  assert.equal(state.previewData, null, 'Late preview cannot authorize a changed corpus')
  const first = state.openRun('first'), second = state.openRun('second')
  details[0].resolve({ run_id: 'first', status: 'PENDING' }); await first
  assert.equal(state.run, null)
  details[1].resolve({ run_id: 'second', status: 'FAILED', progress: {} }); await second
  assert.equal(state.run.run_id, 'second')
  const late = state.openRun('private')
  user.uid = 'bob'; await settle()
  details.at(-1).resolve({ run_id: 'private', status: 'FAILED', progress: {} }); await late
  assert.equal(state.run, null, 'Account change must clear and fence all prior results')
  await state.loadHistory(true)
  assert.equal(history.at(-1), 25, 'Cursor advances even when ACL filtering returns no visible rows')
  assert.equal(imports.length, 0, 'No automatic import or supplier operation')
} finally { app.unmount() }
console.log('Topic workspace passed: no paid defaults, stale corpus/run fences, account isolation, cursor pagination, SFC template, caliber wording locks')
