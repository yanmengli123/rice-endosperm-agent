// Exercise production stream/history closures and render the real inline alert.
// All transport is offline: no supplier calls, credentials or usage writes.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import { describeRunFailure } from '../runFailure.js'

const { parse, babelParse, compileTemplate } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const evaluate = async (source, modules) => {
  const module = { exports: {} }
  vm.runInNewContext((await transform(source, { format: 'cjs' })).code, {
    module, exports: module.exports, console,
    require: (id) => {
      if (id === 'vue') return Vue
      if (modules[id]) return modules[id]
      throw Error(`Unmocked dependency: ${id}`)
    }
  })
  return module.exports
}
const states = {}
const { useAgentThreadState } = await evaluate(
  await readFile(new URL('../../composables/useAgentThreadState.js', import.meta.url), 'utf8'), {}
)
const { getThreadState, resetOnGoingConv } = useAgentThreadState({
  chatState: { threadStates: states }, getCurrentThreadId: () => 'paper-thread'
})
const notices = []
const { useAgentStreamHandler } = await evaluate(
  await readFile(new URL('../../composables/useAgentStreamHandler.js', import.meta.url), 'utf8'), {
    'ant-design-vue': { message: {} },
    '@/utils/errorHandler': { handleChatError: (error) => notices.push(error.message) },
    '@/composables/useApproval': {}, '@/utils/reasoningVisibility': {},
    '@/utils/figureCard': {}, '@/utils/graphSnapshot': {}, '@/utils/runArtifacts': {},
    '@/utils/runFailure': { describeRunFailure }
  }
)
const { handleStreamChunk } = useAgentStreamHandler({ getThreadState })
handleStreamChunk({
  status: 'error', run_id: 'failed-run', error_type: 'worker_error',
  error_message: 'AuthenticationError: 401 invalid api key; synthetic-private-payload'
}, 'paper-thread')
assert.match(getThreadState('paper-thread').runFailure?.message || '', /鉴权失败/,
  'worker error_message must produce a durable, meaningful notice')
assert.equal(notices.length, 1)
assert.match(notices[0], /鉴权失败/)
assert.ok(!JSON.stringify(states).includes('synthetic-private-payload'))
resetOnGoingConv('paper-thread')
assert.equal(getThreadState('paper-thread').runFailure.runId, 'failed-run',
  'History finalization must not erase the error with temporary chunks')
assert.equal(getThreadState('other-thread').runFailure, null)
handleStreamChunk({ status: 'init', request_id: 'new-request' }, 'paper-thread')
assert.equal(getThreadState('paper-thread').runFailure, null, 'A new turn clears the old notice')

const { descriptor } = parse(await readFile(new URL('../../components/AgentChatComponent.vue', import.meta.url), 'utf8'))
const script = descriptor.scriptSetup.content
const ast = babelParse(script, { sourceType: 'module' }).program.body
const closure = (name) => {
  const node = ast.find((statement) => statement.type === 'VariableDeclaration' &&
    statement.declarations.some((decl) => decl.id.name === name))
  assert.ok(node, `Missing production closure ${name}`)
  return script.slice(node.start, node.end)
}
const failed = {
  status: 'failed', agent_run_id: 'failed-run', thread_id: 'paper-thread',
  error: { type: 'unexpected_error', message: '401 invalid api key' },
  run_context: { marker: 'server-context' }
}
let result = failed, resultRequest = null
const context = {
  chatState: { threadStates: states }, agentApi: {
    getAgentRunResult: async () => resultRequest || result,
    getAgentHistory: async () => ({ history: [{ type: 'human', content: 'synthetic question', run_id: 'failed-run' }] })
  },
  describeRunFailure, getThreadState, threadMessages: Vue.ref({}),
  findLatestTraceRunId: (history) => history.at(-1)?.run_id,
  resetRunTrace: (id, runId) => { states[id].trace = { runId } },
  loadRunTraceSnapshot: async () => {}, loadRunEvidence: async () => {},
  restoreCitationReadyFromHistory() {}, resetRunEvidence() {}, handleChatError() {}, console
}
vm.runInNewContext(`${closure('refreshRunContext')}\n${closure('fetchThreadMessages')}\nglobalThis.actions={refreshRunContext,fetchThreadMessages}`, context)
states['paper-thread'].trace = { runId: 'failed-run' }
await context.actions.fetchThreadMessages({ agentId: 'fixture', threadId: 'paper-thread' })
assert.match(states['paper-thread'].runFailure?.message || '', /鉴权失败/,
  'Reopening a failed thread must restore its authoritative run result')
assert.equal(context.threadMessages.value['paper-thread'][0].content, 'synthetic question')
assert.equal(states['paper-thread'].runContext.marker, 'server-context')
assert.equal(states['paper-thread'].onGoingConv.msgChunks['failed-run'], undefined,
  'Failure feedback must not fabricate an assistant answer')

let finishOld
resultRequest = new Promise((resolve) => { finishOld = resolve })
const pending = context.actions.refreshRunContext('paper-thread', 'failed-run')
states['paper-thread'].trace = { runId: 'new-run' }
states['paper-thread'].runFailure = null
finishOld(failed)
await pending
assert.equal(states['paper-thread'].runFailure, null, 'Late old results cannot overwrite a new turn')
resultRequest = null
result = { status: 'completed', agent_run_id: 'new-run', run_context: {} }
await context.actions.refreshRunContext('paper-thread', 'new-run')
assert.equal(states['paper-thread'].runFailure, null)

// Two requests for the same run may finish in reverse order during finalization.
states['paper-thread'].trace = { runId: 'failed-run' }
let finishRunning, finishFailure
resultRequest = new Promise((resolve) => { finishRunning = resolve })
const oldRunning = context.actions.refreshRunContext('paper-thread', 'failed-run')
resultRequest = new Promise((resolve) => { finishFailure = resolve })
const latestFailure = context.actions.refreshRunContext('paper-thread', 'failed-run')
finishFailure(failed)
await latestFailure
assert.match(states['paper-thread'].runFailure.message, /鉴权失败/)
finishRunning({ status: 'running', agent_run_id: 'failed-run', run_context: { marker: 'old-running' } })
await oldRunning
assert.match(states['paper-thread'].runFailure?.message || '', /鉴权失败/,
  'A late running snapshot for the same run must not erase the latest terminal failure')
assert.equal(states['paper-thread'].runContext.marker, 'server-context')

const marker = descriptor.template.content.indexOf('class="chat-run-failure"')
assert.ok(marker >= 0, 'The message area needs an inline failure alert')
const start = descriptor.template.content.lastIndexOf('<div', marker)
const end = descriptor.template.content.indexOf('</div>', marker) + '</div>'.length
const compiled = compileTemplate({ source: descriptor.template.content.slice(start, end), id: 'run-failure' })
assert.equal(compiled.errors.length, 0)
const { render } = await evaluate(compiled.code, {})
const renderer = Vue.createRenderer({
  createElement: (type) => ({ type, props: {}, children: [] }),
  createText: (text) => ({ text }), createComment: () => ({}),
  setText(node, text) { node.text = text },
  setElementText(node, text) { node.children = [{ text }] },
  patchProp(node, key, _old, value) { node.props[key] = value },
  insert(node, parent) { parent.children.push(node); node.parent = parent },
  remove(node) { node.parent?.children.splice(node.parent.children.indexOf(node), 1) },
  parentNode: (node) => node.parent, nextSibling: () => null
})
const thread = Vue.reactive({ runFailure: { runId: 'failed-run', message: describeRunFailure(failed.error) } })
const root = { children: [] }
const app = renderer.createApp({ setup: () => ({ currentThreadState: thread }), render })
app.mount(root)
assert.equal(root.children[0].props.role, 'alert')
const text = (node) => (node.text || '') + (node.children || []).map(text).join('')
assert.match(text(root), /鉴权失败/)
assert.match(text(root), /failed-run/)
thread.runFailure = null
await Vue.nextTick()
assert.ok(!text(root).includes('鉴权失败'))
app.unmount()
console.log('Run failures passed: worker error_message, history/reset/reopen, safe inline alert and stale-result fences')
