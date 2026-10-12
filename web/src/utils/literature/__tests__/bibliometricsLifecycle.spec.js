// Compiled real SFC + Vue lifecycle; no browser or external API.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import * as options from '../bibliometricCharts.js'
import { useLiteraturePollingScope } from '../../../composables/useLiteraturePollingScope.js'
import { useTablistKeyboard } from '../../../composables/useTablistKeyboard.js'

const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const descriptor = parse(
  await readFile(
    new URL('../../../components/literature/BibliometricsPanel.vue', import.meta.url),
    'utf8'
  )
).descriptor
const code = (
  await transform(compileScript(descriptor, { id: 'bibliometrics-test' }).content, {
    format: 'cjs'
  })
).code
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}
const requests = [],
  networks = [],
  instances = [],
  observers = []
const user = Vue.reactive({ uid: 'a', departmentId: 1, isLoggedIn: true })
const module = { exports: {} }
vm.runInNewContext(code, {
  module,
  exports: module.exports,
  console,
  document: { documentElement: {} },
  getComputedStyle: () => ({ getPropertyValue: () => '#123456' }),
  window: { addEventListener() {}, removeEventListener() {} },
  ResizeObserver: class {
    constructor(fn) {
      this.fn = fn
      this.observed = new Set()
      observers.push(this)
    }
    observe(el) {
      this.observed.add(el)
    }
    disconnect() {
      this.observed.clear()
    }
  },
  require(id) {
    if (id === './TopicModelingPanel.vue') return {}
    if (id === 'vue') return Vue
    if (id === '@/stores/user') return { useUserStore: () => user }
    if (id === '@/composables/useLiteraturePollingScope') return { useLiteraturePollingScope }
    if (id === '@/composables/useTablistKeyboard') return { useTablistKeyboard }
    if (id === '@/utils/literature/bibliometricCharts') return options
    if (id === '@/utils/chartColors')
      return { initColorPalette() {}, getColorByIndex: () => '#123456' }
    if (id === '@/apis/literature_api')
      return {
        literatureWorkspaceApi: {
          metricSources: async () => ({ items: [] }),
          referenceBatches: async () => ({ items: [] }),
          metricRecords: async () => ({ items: [], total: 0 }),
          bibliometrics() {
            const request = deferred()
            requests.push(request)
            return request.promise
          },
          metricNetwork(type, top, minPair) {
            const request = { ...deferred(), type, top, minPair }
            networks.push(request)
            return request.promise
          }
        }
      }
    if (id === 'echarts')
      return {
        init() {
          const instance = {
            disposed: false,
            options: [],
            resized: 0,
            setOption(value) {
              assert.equal(this.disposed, false)
              this.options.push(value)
            },
            clear() {
              this.options = []
            },
            resize() {
              this.resized++
            },
            dispose() {
              this.disposed = true
            }
          }
          instances.push(instance)
          return instance
        }
      }
    throw new Error(`Unexpected dependency: ${id}`)
  }
})
const renderer = Vue.createRenderer({
  createElement: () => ({}),
  createText: () => ({}),
  createComment: () => ({}),
  setText() {},
  setElementText() {},
  patchProp() {},
  insert(node, parent) {
    node.parent = parent
  },
  remove(node) {
    node.parent = null
  },
  parentNode: (node) => node.parent,
  nextSibling: () => null
})
const settle = async () => {
  for (let i = 0; i < 8; i++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
const component = module.exports.default
const originalSetup = component.setup
let state
component.setup = (props, context) => {
  state = originalSetup(props, context)
  state.view.value = 'analysis'
  return state
}
component.render = () =>
  Vue.h(
    'section',
    ['trendEl', 'journalsEl', 'affiliationsEl', 'kindEl', 'languageEl', 'networkEl'].map((key) =>
      Vue.h('div', { ref: state[key] })
    )
  )
const shown = Vue.ref(true)
const app = renderer.createApp({
  setup: () => () =>
    Vue.h(Vue.KeepAlive, null, { default: () => (shown.value ? Vue.h(component) : Vue.h('div')) })
})
app.mount({})
await settle()
const data = {
  overview: { papers: 1, by_kind: {}, by_language: {} },
  yearly: [],
  journals_top: [],
  affiliations_top: []
}
requests[0].resolve(data)
await settle()
networks[0].resolve({ nodes: [], links: [], stats: { nodes: 0, links: 0 } })
await settle()
assert.equal(instances.length, 6)
observers[0].fn()
assert.ok(
  instances.every((chart) => chart.resized === 1),
  '侧栏调宽触发容器尺寸监听'
)

state.switchNetwork('author')
state.switchNetwork('keyword')
assert.equal(networks[1].type, 'author')
assert.equal(networks[2].type, 'keyword')
networks[2].resolve({ nodes: [], links: [], stats: { nodes: 2, links: 0 } })
await settle()
networks[1].resolve({ nodes: [], links: [], stats: { nodes: 99, links: 0 } })
await settle()
assert.equal(state.networkStats.value.nodes, 2, '迟到作者网络不能覆盖当前关键词网络')
state.minPair.value = 1
state.networkTop.value = 20
state.loadNetwork()
assert.equal(networks[3].minPair, 1)
assert.equal(networks[3].top, 20)
networks[3].reject(new Error('budget'))
await settle()
assert.equal(state.networkStats.value, null)
assert.equal(state.error.value, 'budget')
assert.equal(instances[5].options.length, 0, '失败不可继续展示旧网络')

state.loadAll()
const oldOwner = requests.at(-1)
user.uid = 'b'
assert.equal(state.overview.value, null, '切换账号立即清空旧用户统计')
oldOwner.resolve(data)
await settle()
assert.equal(state.overview.value, null)
const newOwner = requests.at(-1)
shown.value = false
await settle()
newOwner.resolve(data)
await settle()
assert.equal(state.overview.value, null, 'KeepAlive 停用阻止迟到响应')
shown.value = true
await settle()
requests.at(-1).resolve({ ...data, overview: { papers: 0 } })
await settle()
assert.equal(state.overview.value.papers, 0)
assert.ok(instances.every((chart) => chart.disposed))
const networkCount = networks.length
state.loadAll()
app.unmount()
requests.at(-1).resolve(data)
await settle()
assert.equal(instances.length, 6, '卸载后不再创建图表')
assert.equal(networks.length, networkCount)
assert.equal(observers[0].observed.size, 0)
console.log('bibliometricsLifecycle.spec.js: all assertions passed')
