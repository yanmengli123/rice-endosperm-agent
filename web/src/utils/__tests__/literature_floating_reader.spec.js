// Real parent and floating-window templates: docking and draft guards must stay connected.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import * as chrome from '../literature_chrome.js'

const { parse, compileScript, compileTemplate } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const { descriptor: viewSource } = parse(await readFile(new URL('../../views/LiteratureView.vue', import.meta.url), 'utf8'))
const { descriptor: floatSource } = parse(await readFile(new URL('../../components/literature/FloatingReaderWindow.vue', import.meta.url), 'utf8'))
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
  for (let index = 0; index < 10; index++) { await Promise.resolve(); await Vue.nextTick() }
}
const text = (node) => (node.text || '') + (node.children || []).map(text).join('')
const find = (node, predicate) =>
  predicate(node) ? node : (node.children || []).map((child) => find(child, predicate)).find(Boolean)

async function mount(templateSource = viewSource.template.content, size = { width: 1280, height: 900 }) {
  let accepted = true
  const prompts = []
  const listeners = new Map()
  const browserWindow = {
    innerWidth: size.width, innerHeight: size.height,
    localStorage: { getItem: () => null, setItem() {} },
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set())
      listeners.get(name).add(callback)
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback) }
  }
  const placeholder = { inheritAttrs: false, render: () => null }
  const session = {
    sessions: Vue.ref([]), selectedId: Vue.ref(''), selectedIds: Vue.ref([]),
    allSelected: Vue.ref(false), archived: Vue.ref(false), busy: Vue.ref(false), notice: Vue.ref(''),
    refresh: async () => {}, dispose() {}
  }
  // Editors emit the same dirty/unmount events as the real children, without fetching assets.
  const editor = (label) => ({
    inheritAttrs: false, props: ['rid'], emits: ['editing-change'],
    setup(props, { emit }) {
      Vue.onBeforeUnmount(() => emit('editing-change', false))
      return () => Vue.h('button', {
        [`data-${label}-edit`]: props.rid, onClick: () => emit('editing-change', true)
      }, `Unsaved ${label} ${props.rid}`)
    }
  })
  let floating
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
    if (id === '@/composables/useLiteratureSessions') return {
      useLiteratureSessions: () => session,
      confirmAction: async (message) => { prompts.push(message); return accepted }
    }
    if (id === '@/composables/useLiteratureTaskFeed')
      return { useLiteratureTaskFeed: () => ({ tasks: Vue.ref([]), refresh() {} }) }
    if (id === '@/apis/base') return { apiGet: async () => ({}), apiPost: async () => ({}) }
    if (id === '@/apis/literature_api') return {
      literatureWorkspaceApi: {}, listLiteraturePapers: async () => ({ items: [], total: 0 }),
      getLiteraturePaper: async (rid) => ({ rid, title: `Paper ${rid}` })
    }
    if (id.endsWith('/FloatingReaderWindow.vue')) return { __esModule: true, default: floating }
    if (id.endsWith('/ReaderTabs.vue')) return { __esModule: true, default: editor('floating') }
    if (id.endsWith('/PaperDetail.vue')) return { __esModule: true, default: editor('main') }
    if (id.endsWith('.vue')) return { __esModule: true, default: placeholder }
    throw new Error('Unmocked view dependency: ' + id)
  }
  const evaluate = async (input) => {
    const module = { exports: {} }
    vm.runInNewContext((await transform(input, { format: 'cjs' })).code, {
      module, exports: module.exports, require, URLSearchParams, setTimeout, clearTimeout,
      window: browserWindow
    })
    return module.exports
  }
  floating = (await evaluate(compileScript(floatSource, { id: 'floating-window', inlineTemplate: true }).content)).default
  const template = compileTemplate({
    source: templateSource, id: 'floating-parent', filename: 'LiteratureView.vue',
    compilerOptions: { hoistStatic: false }
  })
  assert.equal(template.errors.length, 0)
  const component = (await evaluate(viewSource.script.content)).default
  component.render = (await evaluate(template.code)).render
  const app = renderer.createApp(component)
  app.component('router-link', placeholder)
  const warnings = []
  app.config.warnHandler = (warning) => warnings.push(warning)
  const root = { children: [] }
  const view = app.mount(root)
  await settle()
  return {
    view, root, prompts, warnings,
    resizeListeners: () => listeners.get('resize')?.size || 0,
    async resize(width, height) {
      browserWindow.innerWidth = width
      browserWindow.innerHeight = height
      for (const callback of listeners.get('resize') || []) callback()
      await settle()
    },
    accept: (value) => { accepted = value },
    async click(predicate) {
      const node = find(root, predicate)
      assert.ok(node, 'Expected control must be rendered')
      node.props.onClick({ stopPropagation() {} })
      await settle()
    },
    unmount: () => app.unmount()
  }
}
const dock = (node) => node.type === 'button' && text(node).trim() === '停靠'
const closeFloat = (node) => node.type === 'button' && node.props?.['aria-label'] === '关闭 阅读 · Paper a'

async function dockCurrent(template) {
  const h = await mount(template)
  try {
    await h.view.openPaper({ rid: 'a' })
    await h.view.popoutReader('a')
    await settle()
    await h.click(dock)
    assert.equal(h.view.floatingReaders.length, 0, 'Dock button must remove its floating reader')
    assert.equal(h.view.selectedRid, 'a')
    assert.equal(h.view.viewMode, 'reader')
    assert.equal(h.view.comparisonRids.length, 0)
    assert.deepEqual(h.warnings, [])
  } finally { h.unmount() }
}

const unboundDock = viewSource.template.content.replace(/\s*@dock="dockFloatingReader\(win.rid\)"/, '')
assert.notEqual(unboundDock, viewSource.template.content)
await assert.rejects(() => dockCurrent(unboundDock), /Dock button must remove its floating reader/)
await dockCurrent(viewSource.template.content)

const h = await mount()
try {
  await h.view.openPaper({ rid: 'a' })
  await h.view.popoutReader('a')
  await h.view.openPaper({ rid: 'b' })
  await settle()
  await h.click(dock)
  assert.equal(h.view.selectedRid, 'b', 'Docking another reader must preserve the primary paper')
  assert.deepEqual([...h.view.comparisonRids], ['a'])

  await h.view.popoutReader('a')
  h.view.comparisonRids = ['c', 'd', 'e', 'f']
  await settle()
  await h.click(dock)
  assert.equal(h.view.floatingReaders.length, 1, 'Full comparison grid must keep the floating reader')
  assert.deepEqual([...h.view.comparisonRids], ['c', 'd', 'e', 'f'])
  assert.match(h.view.searchNotice, /最多 5 篇/)
  h.view.comparisonRids = ['a', 'c', 'd', 'e']
  await settle()
  await h.click(dock)
  assert.equal(h.view.floatingReaders.length, 0, 'Already displayed paper needs no extra slot')
  assert.equal(h.view.comparisonRids.length, 4)

  h.view.selectedRid = 'a'
  h.view.comparisonRids = []
  await h.view.popoutReader('a')
  await settle()
  await h.click((node) => node.props?.['data-main-edit'] === 'a')
  await h.click((node) => node.props?.['data-floating-edit'] === 'a')
  h.accept(false)
  await h.click(closeFloat)
  assert.equal(h.view.floatingReaders.length, 1, 'Cancelled close must preserve floating draft')
  await h.click(dock)
  assert.equal(h.view.floatingReaders.length, 1, 'Cancelled dock must preserve floating draft')
  h.accept(true)
  await h.click(dock)
  assert.equal(h.view.floatingReaders.length, 0)
  h.accept(false)
  assert.equal(await h.view.confirmLeave(), false, 'Floating unmount must not clear same-paper main draft')

  await h.view.popoutReader('a')
  await settle()
  const promptCount = h.prompts.length
  await h.click(closeFloat)
  assert.equal(h.view.floatingReaders.length, 0, 'Clean floating reader can close without discarding main draft')
  assert.equal(h.prompts.length, promptCount)
  assert.equal(await h.view.confirmLeave(), false)

  h.view.setEditing('a', false)
  await h.view.popoutReader('a')
  await settle()
  await h.click((node) => node.props?.['data-floating-edit'] === 'a')
  assert.equal(await h.view.confirmLeave(), false, 'Floating edits must participate in route-leave protection')
  h.accept(true)
  await h.click(closeFloat)
  assert.equal(h.view.floatingReaders.length, 0)
  h.accept(false)
  assert.equal(await h.view.confirmLeave(), true, 'Closed floating reader must release only its dirty key')

  await h.view.popoutReader('a')
  h.view.selectedRid = ''
  h.view.viewMode = 'list'
  await settle()
  await h.click(dock)
  assert.equal(h.view.selectedRid, 'a')
  assert.equal(h.view.viewMode, 'reader')
  assert.equal(h.view.floatingReaders.length, 0)
  assert.deepEqual(h.warnings, [])
} finally { h.unmount() }

const narrow = await mount(viewSource.template.content, { width: 491, height: 871 })
const windowGeometry = () => {
  const window = find(narrow.root, (node) => node.props?.['aria-label'] === '阅读 · Paper a')
  assert.ok(window)
  const values = window.props.style
  return Object.fromEntries(['left', 'top', 'width', 'height'].map((key) => [key, parseFloat(values[key])]))
}
const fits = (width, height) => {
  const rect = windowGeometry()
  assert.ok(rect.left >= 0 && rect.top >= 0, 'Floating window must not start outside the viewport')
  assert.ok(rect.left + rect.width <= width, 'Dock and close controls must stay inside the viewport width')
  assert.ok(rect.top + rect.height <= height, 'Entire floating window must fit the viewport height')
}
let narrowUnmounted = false
try {
  await narrow.view.openPaper({ rid: 'a' })
  await narrow.view.popoutReader('a')
  await settle()
  fits(491, 871)
  assert.equal(narrow.resizeListeners(), 1, 'Floating window must react to viewport resize')
  await narrow.resize(360, 300)
  fits(360, 300)
  await narrow.resize(240, 160)
  fits(240, 160)
  await narrow.resize(1280, 900)
  fits(1280, 900)
  await narrow.resize(491, 871)

  const pointerListeners = new Map()
  let captured = false
  const target = {
    setPointerCapture() { captured = true }, hasPointerCapture: () => captured,
    releasePointerCapture() { captured = false },
    addEventListener(name, callback) { pointerListeners.set(name, callback) },
    removeEventListener(name, callback) {
      if (pointerListeners.get(name) === callback) pointerListeners.delete(name)
    }
  }
  const header = find(narrow.root, (node) => node.type === 'header' && node.props?.class === 'float-title')
  header.props.onPointerdown({
    button: 0, pointerId: 1, clientX: 100, clientY: 100,
    target: { closest: () => null }, currentTarget: target
  })
  pointerListeners.get('pointermove')({ clientX: 10000, clientY: 10000 })
  await settle()
  fits(491, 871)
  assert.equal(captured, true)
  narrow.unmount()
  narrowUnmounted = true
  assert.equal(narrow.resizeListeners(), 0, 'Unmount must remove the resize listener')
  assert.equal(pointerListeners.size, 0, 'Unmount must remove active drag listeners')
  assert.equal(captured, false, 'Unmount must release active pointer capture')
} finally { if (!narrowUnmounted) narrow.unmount() }

console.log('Floating reader dock/close, draft guards, viewport fitting and resize/drag cleanup passed')
