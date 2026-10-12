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
  for (let i = 0; i < 10; i++) {
    await Promise.resolve()
    await Vue.nextTick()
  }
}
const owner = Vue.reactive({ uid: 'owner-a', departmentId: 1, isLoggedIn: true })
const dialogs = [],
  deletes = [],
  updates = [],
  listWaits = []
let rows = [
  { id: 'a', title: 'A', session: { kind: 'paper', rid: 'paper-a' } },
  { id: 'b', title: 'B', session: { kind: 'paper', rid: 'paper-b' } }
]
let waitLists = false
const api = {
  sessions: async () => {
    if (waitLists) {
      const d = deferred()
      listWaits.push(d)
      return d.promise
    }
    return { items: rows, has_more: true }
  },
  deleteSession: async (id) => deletes.push(id),
  updateSession: async (id, body) => updates.push({ id, body })
}
const modules = {
  vue: Vue,
  'ant-design-vue': {
    Modal: { confirm: (options) => dialogs.push(options) },
    message: { warning() {} }
  },
  '@/apis/literature_api': { literatureWorkspaceApi: api },
  '@/stores/user': { useUserStore: () => owner }
}
async function evaluate(file, sfc = false) {
  const source = await readFile(new URL(file, import.meta.url), 'utf8')
  const script = sfc
    ? compileScript(parse(source).descriptor, { id: 'session-safety' }).content
    : source
  const module = { exports: {} }
  vm.runInNewContext((await transform(script, { format: 'cjs' })).code, {
    module,
    exports: module.exports,
    Function,
    require: (id) => {
      if (modules[id]) return modules[id]
      if (id.endsWith('.vue')) return { default: { render: () => null } }
      throw Error('Unmocked dependency: ' + id)
    }
  })
  return module.exports
}
modules['./useConfirmAction'] = await evaluate('../../composables/useConfirmAction.js')
modules['@/composables/useLiteratureSessions'] = await evaluate(
  '../../composables/useLiteratureSessions.js'
)
const { useLiteratureSessions } = modules['@/composables/useLiteratureSessions']
const scope = Vue.effectScope()
const state = scope.run(() => useLiteratureSessions())
await state.refresh()
state.selectedId.value = 'a'
state.selectedIds.value = ['a']
waitLists = true
const oldRefresh = state.refresh()
owner.uid = 'owner-b'
assert.equal(
  state.sessions.value.length,
  0,
  'Account changes clear private cached rows synchronously'
)
assert.equal(state.selectedId.value, '')
assert.equal(state.selectedIds.value.length, 0)
listWaits.at(-1).resolve({ items: rows })
await oldRefresh
assert.equal(
  state.sessions.value.length,
  0,
  'Old account responses cannot rehydrate the new account'
)
waitLists = false
await state.refresh()
const deleting = state.removeOne('a')
assert.equal(state.busy.value, true)
owner.departmentId = 2
dialogs.at(-1).onOk()
await deleting
assert.equal(
  deletes.length,
  0,
  'Confirmation acquired under one tenant cannot act using another tenant auth'
)
assert.equal(state.busy.value, false)
await state.refresh()
const oldWork = deferred(),
  newWork = deferred()
const oldAction = state.perform(async () => oldWork.promise)
owner.uid = 'owner-c'
const newAction = state.perform(async () => newWork.promise)
oldWork.resolve()
await oldAction
assert.equal(state.busy.value, true, 'Old finally must not unlock a new owner operation')
newWork.resolve()
await newAction
assert.equal(state.busy.value, false)
await state.setArchived(true)
const clearing = state.clearArchived()
assert.match(dialogs.at(-1).content, /当前已加载.*尚未加载的会话不会删除/)
dialogs.at(-1).afterClose()
await clearing
assert.equal(deletes.length, 0)
state.archived.value = false

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
const component = (await evaluate('../../components/literature/LiteratureSessions.vue', true))
  .default
component.render = () => null
const app = renderer.createApp(component, { sessionState: state })
const instance = app.mount({})
const sessions = instance.$.setupState
sessions.chat = { selectThreadFromRoute: async () => true }
await settle()
state.selectedId.value = 'a'
await settle()
let operation = sessions.rename()
state.selectedId.value = 'b'
dialogs.at(-1).onOk()
await operation
assert.equal(
  updates.length,
  0,
  'Changing selection during rename must not rename the new selection'
)
state.selectedId.value = 'a'
await settle()
operation = sessions.archive()
state.selectedId.value = 'b'
dialogs.at(-1).onOk()
await operation
assert.equal(updates.length, 0, 'Changing selection during archive must not archive another thread')
assert.equal(state.selectedId.value, 'b')
operation = sessions.rename()
dialogs.at(-1).onOk()
await operation
assert.equal(updates[0].id, 'b', 'Valid confirmation operates on its captured id')
assert.equal(updates[0].body.title, 'B')
const previewWait = deferred()
api.qnaPreview = async () => previewWait.promise
state.selectedId.value = 'a'
await settle()
operation = sessions.previewAnswer()
state.selectedId.value = 'b'
previewWait.resolve({ content: 'private old answer', digest: 'digest' })
await operation
assert.equal(sessions.qnaPreview, null, 'Late answer previews cannot cross thread selections')
operation = sessions.archive()
app.unmount()
dialogs.at(-1).onOk()
await operation
assert.equal(updates.length, 1, 'An unmounted owner cannot mutate via an orphaned modal')
state.dispose()
scope.stop()
console.log(
  'Session safety passed: owner/tenant fences, late responses, dialog identity, orphan modals, preview scope, honest pagination'
)
