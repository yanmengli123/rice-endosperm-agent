// Confirmation awaits must never change which paper/card/workflow is mutated.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { transform } from 'esbuild'
import * as Vue from 'vue'
const { parse, compileScript } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const deferred = () => {
  let resolve
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve }
}
const settle = async () => {
  for (let i = 0; i < 8; i++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
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
const owner = Vue.reactive({ uid: 'a', departmentId: 1, isLoggedIn: true })
const confirmations = [],
  writes = [],
  waitingWrites = []
let delayWrites = false
const card = {
  slug: 'qa-1',
  title: 'First',
  card_type: 'card-qa',
  content: 'Saved content',
  digest: 'original-digest'
}
const api = {
  apiGet: async (path) => (path.endsWith('/cards') ? { cards: [card] } : { status: 'SUCCEEDED' }),
  apiPost: async (path, body) => {
    writes.push({ path, body })
    if (delayWrites) {
      const d = deferred()
      waitingWrites.push(d)
      return d.promise
    }
    return { run_id: 'run-a', already_requested: false }
  }
}
const mounts = []
async function mount(file, props) {
  const { descriptor } = parse(await readFile(new URL(file, import.meta.url), 'utf8'))
  const source =
    descriptor.script?.content || compileScript(descriptor, { id: 'mutation-safety' }).content
  const module = { exports: {} }
  vm.runInNewContext((await transform(source, { format: 'cjs' })).code, {
    module,
    exports: module.exports,
    Function,
    AbortController,
    setTimeout: () => 1,
    clearTimeout() {},
    require: (id) => {
      if (id === 'vue') return Vue
      if (id === '@/apis/base') return api
      if (id === '@/stores/user') return { useUserStore: () => owner }
      if (id === '@/composables/useLiteraturePollingScope')
        return { useLiteraturePollingScope: () => Vue.ref(true) }
      if (id === '@/composables/useLiteratureSessions')
        return {
          confirmAction: () => {
            const d = deferred()
            confirmations.push(d)
            return d.promise
          }
        }
      throw Error('Unmocked dependency: ' + id)
    }
  })
  const component = module.exports.default
  component.render = () => null
  const app = renderer.createApp(component, props)
  const instance = app.mount({})
  mounts.push(app)
  await settle()
  return {
    app,
    state: descriptor.scriptSetup ? instance.$.setupState : instance,
    props: instance.$.props
  }
}
try {
  const cards = await mount('../../components/literature/CardsPanel.vue', { rid: 'paper-a' })
  await cards.state.edit(card)
  cards.state.content = 'Changed content'
  let operation = cards.state.save()
  const prompt = confirmations.at(-1)
  await cards.state.save()
  assert.equal(
    confirmations.at(-1),
    prompt,
    'Double clicks cannot open duplicate overwrite dialogs'
  )
  cards.props.rid = 'paper-b'
  await settle()
  prompt.resolve(true)
  await operation
  assert.equal(writes.length, 0, 'A card confirmation cannot retarget a newly selected paper')
  assert.equal(cards.state.editing, null)
  await cards.state.edit(card)
  cards.state.content = 'Changed B content'
  operation = cards.state.save()
  confirmations.at(-1).resolve(true)
  await operation
  assert.equal(writes[0].path, '/api/literature/papers/paper-b/cards')
  assert.equal(
    writes[0].body.expected_digest,
    'original-digest',
    'Optimistic concurrency remains server checked'
  )
  delayWrites = true
  cards.state.content = 'Old account pending save'
  operation = cards.state.save()
  await settle()
  owner.uid = 'b'
  await settle()
  cards.state.content = 'New owner draft'
  waitingWrites.at(-1).resolve({})
  await operation
  assert.equal(cards.state.content, 'New owner draft', 'Late save cannot clear a new owner draft')
  assert.equal(cards.state.saving, false)
  delayWrites = false

  console.log(
    'Mutation safety passed: fixed paper/card targets, owner fencing, digest preservation, single-flight operations, late result isolation'
  )
} finally {
  mounts.forEach((app) => app.unmount())
}
