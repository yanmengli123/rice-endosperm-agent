// Exercise the production prompt against Ant Design Vue's real ActionButton contract.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import process from 'node:process'
import { transform } from 'esbuild'
import * as Vue from 'vue'

const require = createRequire(import.meta.url)
const ActionButton = require('ant-design-vue/lib/_util/ActionButton').default
const Input = require('ant-design-vue/lib/input').default
const source = await readFile(new URL('../litDialogs.js', import.meta.url), 'utf8')
const renderer = Vue.createRenderer({
  createElement: (type) => ({
    type, tagName: type.toUpperCase(), nodeType: 1, children: [], props: {},
    addEventListener() {}, removeEventListener() {}
  }),
  createText: (text) => ({ text }), createComment: () => ({}),
  setText(node, text) { node.text = text },
  setElementText(node, text) { node.children = [{ text }] },
  patchProp(node, key, _old, value) { node.props[key] = value },
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
const all = (node, predicate) => [
  ...(predicate(node) ? [node] : []), ...(node.children || []).flatMap((child) => all(child, predicate))
]
const settle = async () => {
  for (let index = 0; index < 8; index++) { await Promise.resolve(); await Vue.nextTick() }
}

async function exercise(input) {
  let options, closed = false, settled = false, result
  const warnings = [], unhandled = [], root = { children: [] }
  const module = { exports: {} }
  vm.runInNewContext((await transform(input, { format: 'cjs' })).code, {
    module, exports: module.exports,
    require: (id) => {
      if (id === 'vue') return Vue
      if (id === 'ant-design-vue') return {
        Input,
        Modal: { confirm: (value) => { options = value } },
        message: { warning: (value) => warnings.push(value) }
      }
      throw Error('Unmocked prompt dependency: ' + id)
    }
  })
  const promise = module.exports.litPrompt({
    title: 'Synthetic required reason', required: true, requiredHint: 'Reason is required'
  }).then((value) => { settled = true; result = value })
  const open = Vue.ref(true)
  const close = () => {
    closed = true
    open.value = false
    options.afterClose?.()
  }
  const app = renderer.createApp({
    setup: () => () => open.value ? Vue.h('section', { role: 'dialog' }, [
      options.content(),
      Vue.h(ActionButton, { actionFn: options.onOk, close }, () => 'Save'),
      Vue.h(ActionButton, { actionFn: options.onCancel, close }, () => 'Cancel')
    ]) : null
  })
  const onUnhandled = (error) => unhandled.push(error)
  process.on('unhandledRejection', onUnhandled)
  try {
    app.mount(root)
    await settle()
    const buttons = () => all(root, (node) => node.type === 'button')
    assert.equal(buttons().length, 2, 'Mount the actual Ant Design buttons')
    buttons()[0].props.onClick({ preventDefault() {} })
    await settle()
    assert.equal(closed, false, 'Required empty prompt must remain open after actual Ant Design click')
    assert.equal(settled, false, 'Validation must not resolve as an accidental cancellation')
    buttons()[0].props.onClick({ preventDefault() {} })
    await settle()
    assert.equal(closed, false, 'Invalid input must allow another validation attempt')
    assert.deepEqual(warnings, ['Reason is required', 'Reason is required'])
    assert.equal(unhandled.length, 0, 'Validation must not reject a promise consumed by Ant Design')

    // Change the actual input vnode rather than setting the production ref directly.
    const inputNode = all(root, (node) => node.type === 'input')[0]
    assert.ok(inputNode)
    inputNode.props.onInput({ target: { value: '  Synthetic acceptance reason  ' } })
    await settle()
    buttons()[0].props.onClick({ preventDefault() {} })
    await promise
    await settle()
    assert.equal(result, 'Synthetic acceptance reason')
    assert.equal(closed, true, 'A valid required prompt must explicitly close the modal')
    assert.equal(all(root, (node) => node.props?.role === 'dialog').length, 0)
    assert.equal(unhandled.length, 0)

    const cancel = module.exports.litPrompt({ title: 'Cancel synthetic reason', required: true })
    open.value = true
    await settle()
    buttons()[1].props.onClick({ preventDefault() {} })
    assert.equal(await cancel, null, 'Cancel must still close and resolve null')
    await settle()
    assert.equal(all(root, (node) => node.props?.role === 'dialog').length, 0)

    const optional = module.exports.litPrompt({ title: 'Optional synthetic reason' })
    open.value = true
    await settle()
    buttons()[0].props.onClick({ preventDefault() {} })
    assert.equal(await optional, '', 'An optional prompt still accepts an empty value')
    await settle()
    assert.equal(all(root, (node) => node.props?.role === 'dialog').length, 0)
    assert.equal(unhandled.length, 0)
  } finally {
    app.unmount()
    process.removeListener('unhandledRejection', onUnhandled)
  }
}

await exercise(source)
console.log('Required literature prompt: actual Ant Design validation, continued input, close and promise safety passed')
