import assert from 'node:assert/strict'
import { installRuntimeErrorHandling } from '../runtimeErrors.js'

const listeners = new Map(),
  notices = [],
  logs = []
const target = {
  addEventListener: (name, fn) => listeners.set(name, fn),
  removeEventListener: (name, fn) => {
    if (listeners.get(name) === fn) listeners.delete(name)
  }
}
const app = { config: {} }
let time = 10000
const dispose = installRuntimeErrorHandling(app, {
  target,
  notify: (text) => notices.push(text),
  logger: { error: (...args) => logs.push(args) },
  now: () => time
})
const secret = 'Bearer private-token; prompt=patient-record; /api?password=private'
const error = new TypeError(secret)
app.config.errorHandler(error, null, 'render function')
assert.equal(target.__yuxiRecentErrors[0].category, 'TypeError')
assert.equal(target.__yuxiRecentErrors[0].phase, 'render function')
let prevented = 0
listeners.get('unhandledrejection')({ reason: { secret }, preventDefault: () => prevented++ })
const hostile = {
  get name() {
    throw error
  },
  toString() {
    throw error
  }
}
listeners.get('unhandledrejection')({ reason: hostile, preventDefault: () => prevented++ })
assert.equal(prevented, 2)
assert.equal(notices.length, 1, 'Repeated failures must not create a toast storm')
assert.equal(logs.length, 0, 'Production must not print runtime exceptions')
assert.ok(!JSON.stringify(target.__yuxiRecentErrors).includes('private'))
assert.equal(target.__yuxiRecentErrors[2].category, 'UnknownError')
const snapshot = target.__yuxiRecentErrors
snapshot.length = 0
assert.equal(target.__yuxiRecentErrors.length, 3, 'Diagnostics expose a copy, not the mutable ring')
assert.throws(() => {
  target.__yuxiRecentErrors[0].category = secret
})
time += 5000
for (let i = 0; i < 30; i++) app.config.errorHandler(error, null, secret)
assert.equal(notices.length, 2)
assert.equal(target.__yuxiRecentErrors.length, 20)
assert.equal(target.__yuxiRecentErrors.at(-1).phase, 'unknown')
dispose()
assert.equal(listeners.size, 0)
assert.equal(app.config.errorHandler, undefined)
assert.equal('__yuxiRecentErrors' in target, false)

const disposeDev = installRuntimeErrorHandling(app, {
  target,
  development: true,
  logger: { error: (...args) => logs.push(args) },
  notify: () => {
    throw error
  }
})
app.config.errorHandler(error, null, 'mounted hook')
assert.equal(logs.length, 1)
assert.ok(!JSON.stringify(logs).includes('private'), 'Development diagnostics are safe as well')
disposeDev()
console.log(
  'Runtime diagnostics passed: no secrets/raw stacks, bounded snapshots, rejection coverage, throttling, cleanup'
)
