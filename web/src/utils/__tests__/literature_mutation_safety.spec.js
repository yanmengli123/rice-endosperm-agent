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

  const workflow = await mount('../../components/literature/WorkflowPanel.vue', { rid: 'paper-a' })
  workflow.state.runId = 'old-run'
  workflow.state.state = { cancellable: true }
  operation = workflow.state.cancel()
  const cancelPrompt = confirmations.at(-1)
  await workflow.state.cancel()
  assert.equal(confirmations.at(-1), cancelPrompt)
  workflow.state.runId = 'new-run'
  cancelPrompt.resolve(true)
  await operation
  assert.equal(
    writes.filter((item) => item.path.endsWith('/cancel')).length,
    0,
    'Cancel confirmation cannot follow a newly selected run'
  )
  workflow.state.state = { cancellable: true }
  workflow.state.cancelReason = 'Operator reason'
  operation = workflow.state.cancel()
  confirmations.at(-1).resolve(true)
  await operation
  assert.equal(writes.at(-1).path, '/api/literature/workflow/new-run/cancel')
  assert.equal(writes.at(-1).body.reason, 'Operator reason')
  workflow.state.runId = 'run-before-switch'
  workflow.state.state = { cancellable: true }
  operation = workflow.state.cancel()
  owner.departmentId = 3
  confirmations.at(-1).resolve(true)
  await operation
  assert.equal(writes.filter((item) => item.path.endsWith('/cancel')).length, 1)
  assert.equal(workflow.state.runId, '', 'Tenant changes clear previous workflow details')
  delayWrites = true
  operation = workflow.state.start()
  const oldStart = waitingWrites.at(-1)
  await workflow.state.start()
  assert.equal(waitingWrites.at(-1), oldStart, 'Start is single-flight')
  workflow.props.rid = 'paper-b'
  await settle()
  const newStart = workflow.state.start()
  oldStart.resolve({ run_id: 'wrong-paper-run' })
  await operation
  assert.equal(workflow.state.runId, '')
  assert.equal(workflow.state.busy, true, 'Old finally cannot release the new paper operation')
  waitingWrites.at(-1).resolve({ run_id: 'correct-paper-run' })
  await newStart
  assert.equal(workflow.state.runId, 'correct-paper-run')
  console.log(
    'Mutation safety passed: fixed paper/card/run targets, owner fencing, digest preservation, single-flight operations, late result isolation'
  )
} finally {
  mounts.forEach((app) => app.unmount())
}
