import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import { useTablistKeyboard } from '../../composables/useTablistKeyboard.js'

let options
async function evaluate(file) {
  const module = { exports: {} }
  vm.runInNewContext(
    (await transform(await readFile(new URL(file, import.meta.url), 'utf8'), { format: 'cjs' }))
      .code,
    {
      module,
      exports: module.exports,
      require: (id) =>
        id === 'vue'
          ? Vue
          : {
              Modal: {
                confirm: (config) => {
                  options = config
                }
              },
              Input: {},
              message: { warning() {} }
            }
    }
  )
  return module.exports
}
const { confirmAction } = await evaluate('../../composables/useConfirmAction.js')
let result = confirmAction('Discard?')
options.afterClose()
assert.equal(await result, false, 'Programmatic modal destruction cancels and unblocks the caller')
result = confirmAction('Proceed?')
options.onOk()
options.afterClose()
assert.equal(await result, true, 'afterClose cannot undo an already confirmed action')
const { litConfirm, litPrompt } = await evaluate('../litDialogs.js')
result = litConfirm({ title: 'Confirm' })
options.afterClose()
assert.equal(await result, false)
result = litPrompt({ title: 'Title' })
options.afterClose()
assert.equal(await result, null)

let orientation = 'horizontal',
  focused = 'a',
  preventions = 0,
  pendingResolve
const activations = []
const tabs = ['a', 'disabled', 'b', 'quoted"key'].map((key) => ({
  dataset: { tabKey: key },
  disabled: key === 'disabled',
  focus: () => {
    focused = key
  },
  getAttribute: () => null,
  closest: () => tablist
}))
const tablist = {
  isConnected: true,
  getAttribute: () => orientation,
  querySelectorAll: () => tabs
}
const event = (key, index = 0) => ({
  key,
  currentTarget: tabs[index],
  preventDefault: () => preventions++
})
const { onTabKeydown } = useTablistKeyboard(
  (key) => {
    activations.push(key)
    return new Promise((resolve) => {
      pendingResolve = resolve
    })
  },
  () => tabs.map((tab) => tab.dataset.tabKey)
)
let navigation = onTabKeydown(event('ArrowRight'))
assert.deepEqual(activations, ['b'], 'Disabled tabs are skipped')
assert.equal(focused, 'a', 'Focus waits for editing confirmation')
await onTabKeydown(event('ArrowRight'))
assert.equal(activations.length, 1, 'Held keys cannot open duplicate confirmations')
pendingResolve(false)
await navigation
assert.equal(focused, 'a', 'Cancel keeps selection and focus together')
const transientEvent = event('End')
navigation = onTabKeydown(transientEvent)
transientEvent.currentTarget = null
pendingResolve(true)
await navigation
assert.equal(
  focused,
  'quoted"key',
  'Native events can lose currentTarget; keys are not interpolated into selectors'
)
const previous = preventions
await onTabKeydown(event('ArrowDown'))
assert.equal(preventions, previous, 'Horizontal tabs must not steal vertical scrolling')
orientation = 'vertical'
navigation = onTabKeydown(event('ArrowDown'))
pendingResolve(true)
await navigation
assert.equal(focused, 'b')
navigation = onTabKeydown(event('Home', 2))
tablist.isConnected = false
pendingResolve(true)
await navigation
assert.equal(focused, 'b', 'Unmounted tablists cannot steal focus')
console.log(
  'Tab/dialog contracts passed: async cancellation, focus, orientation, disabled tabs, duplicate keys, destroy settlement'
)
