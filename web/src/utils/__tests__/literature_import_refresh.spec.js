// Compile the actual view template: child import completion must refresh the catalog.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import * as chrome from '../literature_chrome.js'

const { parse, compileTemplate, babelParse } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const { descriptor } = parse(await readFile(new URL('../../views/LiteratureView.vue', import.meta.url), 'utf8'))
const source = descriptor.script.content
const exported = babelParse(source, { sourceType: 'module' }).program.body.find(
  (node) => node.type === 'ExportDefaultDeclaration'
).declaration
const setup = exported.properties.find((node) => node.key?.name === 'setup')
const returned = setup.body.body.findLast((node) => node.type === 'ReturnStatement').argument
const loadBinding = returned.properties.find((node) => node.key?.name === 'load')
assert.ok(loadBinding, 'The view must expose its load handler to template event bindings')
assert.equal(source[loadBinding.end], ',')
const beforeFix = source.slice(0, loadBinding.start) + source.slice(loadBinding.end + 1)
const template = compileTemplate({
  source: descriptor.template.content, id: 'import-refresh', filename: 'LiteratureView.vue',
  compilerOptions: { hoistStatic: false }
})
assert.equal(template.errors.length, 0)
const runtime = { ...Vue, vModelText: {}, vModelSelect: {}, vModelCheckbox: {} }
const renderer = Vue.createRenderer({
  createElement: (type) => ({ type, children: [], props: {} }),
  createText: (text) => ({ text }), createComment: () => ({}),
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
  parentNode: (node) => node.parent, nextSibling: () => null
})
const settle = async () => {
  for (let index = 0; index < 8; index++) { await Promise.resolve(); await Vue.nextTick() }
}
const text = (node) => (node.text || '') + (node.children || []).map(text).join('')
const find = (node, predicate) =>
  predicate(node) ? node : (node.children || []).map((child) => find(child, predicate)).find(Boolean)

async function exercise(script) {
  const requests = [], warnings = []
  const placeholder = { inheritAttrs: false, render: () => null }
  const session = {
    sessions: Vue.ref([]), selectedId: Vue.ref(''), selectedIds: Vue.ref([]),
    allSelected: Vue.ref(false), archived: Vue.ref(false), busy: Vue.ref(false), notice: Vue.ref(''),
    refresh: async () => {}, dispose() {}
  }
  const require = (id) => {
    if (id === 'vue') return runtime
    if (id === 'vue-router') return { useRoute: () => ({ query: {} }), onBeforeRouteLeave() {} }
    if (id === '@lucide/vue') return new Proxy({}, { get: () => placeholder })
    if (id === 'ant-design-vue') return { message: { success() {}, error() {}, warning() {} } }
    if (id === '@/stores/user') return { useUserStore: () => ({ uid: 'fixture', isLoggedIn: true }) }
    if (id === '@/utils/literature_chrome') return chrome
    if (id === '@/utils/literature_assistant') return {}
    if (id === '@/composables/useColumnResize')
      return { useColumnResize: () => ({ isDragging: Vue.ref(false), startDrag() {} }) }
    if (id === '@/composables/useLiteratureSessions')
      return { confirmAction: async () => true, useLiteratureSessions: () => session }
    if (id === '@/composables/useLiteratureTaskFeed')
      return { useLiteratureTaskFeed: () => ({ tasks: Vue.ref([]), refresh() {} }) }
    if (id === '@/apis/base') return { apiGet: async () => ({}), apiPost: async () => ({}) }
    if (id === '@/apis/literature_api') return {
      literatureWorkspaceApi: {},
      listLiteraturePapers: async (params) => {
        requests.push(params)
        return { items: [{ rid: 'paper-' + requests.length, title: 'Catalog result ' + requests.length }], total: 1 }
      }
    }
    if (id.endsWith('/ImportPanel.vue') || id.endsWith('/BibliometricsPanel.vue')) return {
      __esModule: true, default: {
        inheritAttrs: false, emits: ['imported'],
        setup(_props, { emit }) {
          return () => Vue.h('button', { 'data-fixture-import': true, onClick: () => emit('imported') }, 'Complete import')
        }
      }
    }
    if (id.endsWith('.vue')) return { __esModule: true, default: placeholder }
    throw new Error('Unmocked view dependency: ' + id)
  }
  const evaluate = async (input) => {
    const module = { exports: {} }
    vm.runInNewContext((await transform(input, { format: 'cjs' })).code, {
      module, exports: module.exports, require, URLSearchParams,
      window: { localStorage: { getItem: () => null, setItem() {} }, addEventListener() {}, removeEventListener() {} }
    })
    return module.exports
  }
  const component = (await evaluate(script)).default
  component.render = (await evaluate(template.code)).render
  const app = renderer.createApp(component)
  app.config.warnHandler = (warning) => warnings.push(warning)
  const root = { children: [] }
  const view = app.mount(root)
  try {
    await settle()
    assert.equal(requests.length, 1)
    for (const [mode, name] of [['import', 'ImportPanel'], ['bibmetrics', 'BibliometricsPanel']]) {
      view.viewMode = mode
      await settle()
      const count = requests.length
      const button = find(root, (node) => node.props?.['data-fixture-import'])
      assert.ok(button, `${name} must be rendered by the real view template`)
      button.props.onClick()
      await settle()
      assert.equal(requests.length, count + 1, `${name} imported event must refresh the catalog`)
      assert.equal(view.papers[0].rid, 'paper-' + requests.length)
      await view.switchWorkspace('list')
      await settle()
      assert.match(text(root), new RegExp('Catalog result ' + requests.length))
    }
    assert.equal(warnings.some((warning) => warning.includes('Property "load"')), false)
  } finally {
    app.unmount()
  }
}

await assert.rejects(() => exercise(beforeFix), /ImportPanel imported event must refresh the catalog/)
await exercise(source)
console.log('In-memory pre-fix control fails; real import and bibliometrics completion refresh the rendered catalog')
