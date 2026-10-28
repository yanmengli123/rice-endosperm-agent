// Render the actual workbench template: failed refreshes must stay visible and recover.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'

const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const source = await readFile(
  new URL('../../components/literature/LibraryWorkbench.vue', import.meta.url),
  'utf8'
)
const { descriptor } = parse(source)
const script = compileScript(descriptor, { id: 'journal-feedback', inlineTemplate: true }).content
const module = { exports: {} }
let calls = 0
vm.runInNewContext((await transform(script, { format: 'cjs' })).code, {
  module,
  exports: module.exports,
  AbortController,
  setTimeout,
  clearTimeout,
  require: (id) => {
    // Form input DOM directives are outside this custom renderer's retry scope.
    if (id === 'vue') return { ...Vue, vModelText: {}, vModelSelect: {}, vModelCheckbox: {} }
    if (id === '@/apis/base') return { apiGet() {}, apiPost() {} }
    if (id === '@/composables/useLiteraturePollingScope')
      return { useLiteraturePollingScope: () => Vue.ref(true) }
    if (id === '@/composables/useLiteratureSessions') return { confirmAction: async () => false }
    if (id === '@/apis/literature_api')
      return {
        literatureWorkspaceApi: {
          journalsStatus: async () => {
            calls += 1
            if (calls !== 2) throw new Error('期刊状态服务暂时不可用')
            return { mapping_count: 3, journal_count: 2, if_covered_count: 1 }
          }
        }
      }
    if (id === './CardsPanel.vue')
      return { __esModule: true, default: { render: () => null } }
    throw new Error('Unmocked workbench dependency: ' + id)
  }
})

const renderer = Vue.createRenderer({
  createElement: (type) => ({ type, children: [], props: {} }),
  createText: (text) => ({ text }),
  createComment: () => ({}),
  setText: (node, text) => { node.text = text },
  setElementText: (node, text) => { node.children = [{ text }] },
  patchProp: (node, key, _old, value) => { node.props[key] = value },
  insert(node, parent, anchor = null) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(node)
    else parent.children.splice(index, 0, node)
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
  for (let index = 0; index < 6; index++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
const text = (node) => (node.text || '') + (node.children || []).map(text).join('')
const find = (node, predicate) =>
  predicate(node) ? node : (node.children || []).map((child) => find(child, predicate)).find(Boolean)
const root = { children: [] }
const app = renderer.createApp(module.exports.default)
app.mount(root)
await settle()

assert.equal(calls, 1)
assert.equal(
  text(find(root, (node) => node.props?.role === 'alert') || {}),
  '期刊状态服务暂时不可用',
  'Initial status failures must appear in the actual workbench template'
)
const refresh = () => find(root, (node) => node.type === 'button' && text(node) === '刷新指标状态')
assert.ok(refresh(), 'A visible retry control must remain available')
await refresh().props.onClick()
await settle()
assert.equal(calls, 2)
assert.equal(find(root, (node) => node.props?.role === 'alert'), undefined)
assert.match(text(root), /已导入映射 3 条/)
await refresh().props.onClick()
await settle()
assert.equal(calls, 3)
assert.equal(text(find(root, (node) => node.props?.role === 'alert') || {}), '期刊状态服务暂时不可用')
assert.match(text(root), /已导入映射 3 条/, 'A retry failure must preserve the last known status')
app.unmount()
console.log('Journal status refresh failure visibility, retry and recovery passed')
