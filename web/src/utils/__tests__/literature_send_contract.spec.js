// Execute production closures extracted by the SFC parser (not copied test logic).
// Transport/streaming are offline doubles: no model, upload or paid supplier calls.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import * as Vue from 'vue'
import * as assistant from '../literature_assistant.js'
const { parse, babelParse } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
async function script(file) {
  const { descriptor } = parse(await readFile(new URL(file, import.meta.url), 'utf8'))
  return descriptor.scriptSetup.content
}
const source = await script('../../components/AgentChatComponent.vue')
const ast = babelParse(source, { sourceType: 'module' })
const functions = [
  'readerOwnerKey',
  'stagePaperReaderDraft',
  'handleSendMessage',
  'handleSendOrStop'
]
  .map((name) => {
    const statement = ast.program.body.find(
      (node) =>
        node.type === 'VariableDeclaration' &&
        node.declarations.some((item) => item.id.name === name)
    )
    assert.ok(statement, `Missing production function: ${name}`)
    return source.slice(statement.start, statement.end)
  })
  .join('\n')
const inputSource = await script('../../components/AgentInputArea.vue')
const expose = babelParse(inputSource, { sourceType: 'module' }).program.body.find(
  (node) => node.type === 'ExpressionStatement' && node.expression.callee?.name === 'defineExpose'
).expression.arguments[0]
const submit = expose.properties.find((node) => node.key.name === 'submit').value
const submitSource = inputSource.slice(submit.start, submit.end)
const deferred = () => {
  let resolve, reject
  const promise = new Promise((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}
const settle = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve()
}
const capture = {
  thread_id: 'paper-thread',
  rid: 'paper',
  reading_version: 3,
  source_revision_id: 'rev',
  surface: 'original',
  action: 'explain',
  text: 'Selected methods'
}
function harness({ ordinary = false, tick = () => {} } = {}) {
  const state = Vue.reactive({ isStreaming: false })
  const requests = [],
    errors = [],
    streams = [],
    nativeEvents = []
  const creation = deferred(),
    stream = deferred()
  const context = {
    ...assistant,
    readerUser: Vue.reactive({ uid: 'owner', departmentId: 1, isLoggedIn: true }),
    props: {
      lockThreadCreation: !ordinary,
      paperReaderRid: ordinary ? '' : 'paper',
      sendDisabled: false
    },
    readerDisposed: false,
    userInput: Vue.ref(''),
    readerStaging: Vue.ref(false),
    stagedReader: Vue.ref(null),
    currentChatId: Vue.ref('paper-thread'),
    currentAgent: Vue.ref({ id: 'agent' }),
    currentAgentId: Vue.ref('agent'),
    selectedModelSpec: Vue.ref(null),
    sendCooldownActive: Vue.ref(false),
    isProcessing: Vue.computed(() => state.isStreaming),
    agentInputAreaRef: Vue.ref(null),
    currentPendingThreadAttachments: Vue.ref([]),
    mentionConfigWithDocuments: Vue.ref({}),
    approvalState: { threadId: '' },
    threadMessages: Vue.ref({ 'paper-thread': [] }),
    scrollController: { scrollToBottom() {} },
    nextTick: async () => {
      await Promise.resolve()
      tick(context)
    },
    startSendCooldown: () => {
      context.sendCooldownActive.value = true
    },
    message: { warning() {}, error() {}, info() {} },
    ensureActiveThread: async () => {
      throw Error('Reader may not create an unbound thread')
    },
    buildStructuredMentions: () => null,
    getThreadState: () => state,
    hideApprovalState() {},
    scheduleThreadNaming() {},
    resetOnGoingConv() {},
    createClientRequestId: () => 'request-id',
    markAttachmentsRequestId: () => [],
    insertOptimisticHumanMessage() {},
    rollbackAttachments() {},
    resetRunTrace() {},
    resetRunEvidence() {},
    loadRunTraceSnapshot: async () => {},
    startRunStream: async (...args) => {
      streams.push(args)
      await stream.promise
    },
    handleChatError: (error) => errors.push(error),
    agentApi: {
      createAgentRun: async (body) => {
        requests.push(body)
        return creation.promise
      },
      cancelAgentRun: async () => {
        throw Error('Auto-send must not cancel a run')
      }
    }
  }
  const inputContext = {
    props: { paperReaderEnabled: !ordinary },
    currentImage: Vue.ref(null),
    browserEnabled: Vue.ref(true),
    handleSend: () => nativeEvents.push('send')
  }
  vm.runInNewContext(`globalThis.submit = ${submitSource}`, inputContext)
  context.agentInputAreaRef.value = {
    focus() {},
    hasImage: () => Boolean(inputContext.currentImage.value),
    submit: inputContext.submit,
    restoreReaderImage() {}
  }
  vm.runInNewContext(
    `${functions}\nglobalThis.actions = {stagePaperReaderDraft, handleSendMessage, handleSendOrStop}`,
    context
  )
  return {
    ...context.actions,
    context,
    inputContext,
    state,
    requests,
    creation,
    stream,
    errors,
    streams,
    nativeEvents
  }
}

let h = harness()
let outcome
const send = h.stagePaperReaderDraft(capture).then((value) => {
  outcome = value
})
await settle()
assert.equal(h.requests.length, 1)
assert.equal(h.context.userInput.value, '')
assert.equal(outcome, undefined, 'Cleared input is NOT an acknowledgment of server acceptance')
assert.equal(h.requests[0].meta.literature_reader_context.rid, capture.rid)
assert.equal(
  h.requests[0].browser_enabled,
  true,
  'Native browser toggle remains part of the send payload'
)
h.creation.resolve({ run_id: 'run-created' })
await send
assert.equal(outcome.sent, true)
assert.equal(outcome.runId, 'run-created')
await settle()
assert.equal(h.streams.length, 1, 'Acknowledgment does not wait for the complete stream')
assert.equal(h.nativeEvents.length, 0, 'No fire-and-forget emit and no duplicate sends')
await assert.rejects(() => h.stagePaperReaderDraft(capture), /正在生成/)
assert.equal(h.requests.length, 1)
h.stream.resolve()
await settle()

h = harness()
const rejected = h.stagePaperReaderDraft(capture)
await settle()
h.creation.reject(Error('Stale version 409'))
assert.equal((await rejected).sent, false)
assert.ok(
  h.context.userInput.value.includes('Selected methods'),
  'Rejected create restores the original reader draft'
)
assert.equal(h.errors.length, 1)

h = harness()
h.context.sendCooldownActive.value = true
assert.equal((await h.stagePaperReaderDraft(capture)).sent, false)
assert.equal(h.requests.length, 0)
assert.ok(h.context.userInput.value, 'Cooldown preserves a draft for manual confirmation')
h = harness()
assert.equal((await h.stagePaperReaderDraft({ ...capture, action: 'ask' })).sent, false)
assert.equal(h.requests.length, 0)
h.context.userInput.value += ' user edit'
await assert.rejects(() => h.stagePaperReaderDraft(capture), /正在编辑/)
h = harness({
  tick: (ctx) => {
    ctx.currentChatId.value = 'other-thread'
    ctx.userInput.value = 'new user draft'
  }
})
await assert.rejects(() => h.stagePaperReaderDraft(capture), /会话已改变/)
assert.equal(h.requests.length, 0)
assert.equal(h.context.userInput.value, 'new user draft')

let tickCount = 0
h = harness({
  tick: (ctx) => {
    if (++tickCount === 2) ctx.readerUser.uid = 'new-owner'
  }
})
assert.equal((await h.stagePaperReaderDraft(capture)).sent, false)
assert.equal(
  h.requests.length,
  0,
  'Actor changes between staging and create cannot transmit the previous owner input'
)
h = harness()
const failedOldOwner = h.stagePaperReaderDraft(capture)
await settle()
h.context.readerUser.uid = 'new-owner'
h.creation.reject(Error('Previous owner request rejected'))
await failedOldOwner
assert.equal(
  h.context.userInput.value,
  '',
  'A rejected old-owner create cannot restore its private draft under the new login'
)

h = harness()
const acceptedOldOwner = h.stagePaperReaderDraft(capture)
await settle()
h.context.readerUser.uid = 'new-owner'
h.creation.resolve({ run_id: 'previous-owner-run' })
assert.equal(
  (await acceptedOldOwner).sent,
  true,
  'Accepted work is not replayed or cancelled when the UI owner changes'
)
await settle()
assert.equal(h.streams.length, 0, 'No subscription using a different actor credential')

h = harness({ ordinary: true })
h.context.userInput.value = 'Ordinary question'
const normal = h.handleSendMessage({ browserEnabled: false })
await settle()
assert.equal(h.requests.length, 1)
assert.equal('literature_reader_context' in h.requests[0].meta, false)
assert.equal(h.requests[0].query, 'Ordinary question')
h.creation.resolve({ run_id: 'normal-run' })
h.stream.resolve()
await normal
h.inputContext.submit(() => {
  throw Error('Ordinary submit must keep its native event path')
})
assert.deepEqual(h.nativeEvents, ['send'])
console.log(
  'Reader/native send contracts passed: server acknowledgment, rejected creates, cooldown, draft/identity fences, no duplicate/cancel, normal QA isolation'
)
