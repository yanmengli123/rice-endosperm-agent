// The workbench binds only v-model:visible; its settings close button must honor that contract.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'

const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const source = await readFile(new URL('../../components/SettingsModal.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const script = compileScript(descriptor, { id: 'settings-close', inlineTemplate: true }).content
const module = { exports: {} }
const Placeholder = { render: () => null }
vm.runInNewContext((await transform(script, { format: 'cjs' })).code, {
  module,
  exports: module.exports,
  require: (id) => {
    if (id === 'vue') return { ...Vue, vShow: {} }
    if (id === '@/stores/user')
      return { useUserStore: () => ({ isLoggedIn: true, isAdmin: false, isSuperAdmin: false }) }
    if (id === '@lucide/vue') return new Proxy({}, { get: () => Placeholder })
    if (id.endsWith('.vue')) return { __esModule: true, default: Placeholder }
    throw new Error('Unmocked settings dependency: ' + id)
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
let cancelModal
const Modal = {
  props: ['open'],
  emits: ['cancel', 'update:open'],
  setup(props, { emit, slots }) {
    cancelModal = () => emit('cancel')
    return () => props.open ? Vue.h('section', { role: 'dialog' }, slots.default?.()) : null
  }
}
const shown = Vue.ref(true)
let legacyCloseEvents = 0
const app = renderer.createApp({
  setup: () => () => Vue.h(module.exports.default, {
    visible: shown.value,
    'onUpdate:visible': (value) => { shown.value = value },
    onClose: () => { legacyCloseEvents += 1 }
  })
})
app.component('AModal', Modal)
const root = { children: [] }
const find = (node, predicate) =>
  predicate(node) ? node : (node.children || []).map((child) => find(child, predicate)).find(Boolean)
const closeButton = () => find(root, (node) => node.props?.['aria-label'] === '关闭设置')
const dialog = () => find(root, (node) => node.props?.role === 'dialog')
app.mount(root)
assert.ok(dialog() && closeButton(), 'The real settings template must render its close button')
closeButton().props.onClick()
await Vue.nextTick()
assert.equal(shown.value, false, 'Close must update v-model even when the parent has no close handler')
assert.equal(dialog(), undefined, 'The settings overlay must disappear after clicking close')
assert.equal(legacyCloseEvents, 1, 'Existing consumers must still receive the close event')

shown.value = true
await Vue.nextTick()
assert.ok(dialog(), 'The modal must open again after a successful close')
closeButton().props.onClick()
await Vue.nextTick()
assert.equal(shown.value, false)
assert.equal(dialog(), undefined)
assert.equal(legacyCloseEvents, 2)

shown.value = true
await Vue.nextTick()
cancelModal()
await Vue.nextTick()
assert.equal(shown.value, false, 'Modal cancel must obey the same v-model close contract')
assert.equal(dialog(), undefined)
assert.equal(legacyCloseEvents, 3)
app.unmount()
console.log('Settings modal actual close button, reopen, cancel and legacy close compatibility passed')
