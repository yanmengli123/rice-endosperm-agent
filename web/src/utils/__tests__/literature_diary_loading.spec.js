// Real diary template: date navigation cannot race the initial saved-entry read.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { transform } from 'esbuild'
import * as Vue from 'vue'
import * as calendar from '../literature_calendar.js'

const { parse, compileTemplate } = createRequire(import.meta.resolve('vue'))('@vue/compiler-sfc')
const source = await readFile(new URL('../../views/DiaryView.vue', import.meta.url), 'utf8')
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const renderer = Vue.createRenderer({
  createElement: (type) => ({ type, children: [], props: {} }),
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
const settle = async () => {
  for (let index = 0; index < 8; index++) { await Promise.resolve(); await Vue.nextTick() }
}
const text = (node) => (node.text || '') + (node.children || []).map(text).join('')
const find = (node, predicate) => predicate(node) ? node
  : (node.children || []).map((child) => find(child, predicate)).find(Boolean)
const all = (node, predicate) => [
  ...(predicate(node) ? [node] : []), ...(node.children || []).flatMap((child) => all(child, predicate))
]

async function exercise(input) {
  const { descriptor } = parse(input)
  const initialCalendar = deferred(), reads = []
  let calendarReads = 0, writes = 0
  const evaluate = async (script) => {
    const module = { exports: {} }
    vm.runInNewContext((await transform(script, { format: 'cjs' })).code, {
      module, exports: module.exports,
      require: (id) => {
        if (id === 'vue') return { ...Vue, vModelText: {} }
        if (id === '@/utils/literature_calendar') return calendar
        if (id === '@/composables/useConfirmAction') return { confirmAction: async () => true }
        if (id === '@/apis/literature_api') return { literatureWorkspaceApi: {} }
        if (id === '@/apis/base') return {
          apiGet: async (url) => {
            if (url.includes('/days')) return ++calendarReads === 1 ? initialCalendar.promise : { days: [] }
            const request = { url, ...deferred() }
            reads.push(request)
            return request.promise
          },
          apiPut: async () => { writes++; return {} }
        }
        throw Error('Unmocked diary dependency: ' + id)
      }
    })
    return module.exports
  }
  const component = (await evaluate(descriptor.script.content)).default
  const template = compileTemplate({ source: descriptor.template.content, id: 'diary-loading' })
  assert.equal(template.errors.length, 0)
  component.render = (await evaluate(template.code)).render
  const root = { children: [] }, app = renderer.createApp(component)
  const diary = app.mount(root)
  const button = (label) => find(root, (node) => node.type === 'button' && text(node).trim() === label)
  const control = (label) => find(root, (node) => node.props?.['aria-label'] === label)
  const assertNavigation = (disabled) => {
    for (const label of ['上月', '下月', '今天'])
      assert.equal(button(label).props.disabled, disabled, 'Date navigation must follow loading: ' + label)
    for (const label of ['日记月份', '日记日期'])
      assert.equal(control(label).props.disabled, disabled, 'Date navigation must follow loading: ' + label)
    for (const day of all(root, (node) => node.type === 'button' && /^\d{4}-/.test(node.props?.['aria-label'] || '')))
      assert.equal(day.props.disabled, disabled, 'Calendar days must follow loading')
  }
  try {
    await settle()
    assertNavigation(true)
    assert.equal(button('保存日记').props.disabled, true)
    initialCalendar.resolve({
      days: [diary.today],
      activities: [{ date: diary.today, imports: 1, kb: 0, papers: [] }]
    })
    await settle()
    assert.equal(reads.length, 1)
    assert.ok(control(`${diary.today} 有日记或文献活动`),
      'Activity-only dates must not be advertised as an existing personal diary')
    assert.ok(!text(root).includes('带圆点的日期已有日记'))
    assertNavigation(true)
    reads[0].reject(Error('Initial diary unavailable'))
    await settle()
    assertNavigation(false)
    assert.equal(button('保存日记').props.disabled, true, 'Failed initial read must keep save disabled')
    assert.equal(writes, 0)

    const retry = button('今天').props.onClick()
    await settle()
    assertNavigation(true)
    reads[1].resolve({ date: diary.today, content: '' })
    await retry
    await settle()
    assertNavigation(false)
    assert.equal(button('保存日记').props.disabled, false)
    assert.equal(diary.content, '', 'A date with only import activity can legitimately have no diary content')

    const select = control('日记日期').props.onChange({ target: { value: '1777-01-02' } })
    await settle()
    assertNavigation(true)
    assert.equal(reads[2].url, '/api/literature/diary/1777-01-02')
    reads[2].resolve({ content: '' })
    await select
    await settle()
    assert.equal(control('日记日期').props.value, '1777-01-02')
    assertNavigation(false)
    assert.equal(writes, 0, 'Navigation and retries do not modify any diary')
  } finally {
    app.unmount()
  }
}

const beforeNavigationFix = source
  .replaceAll(':disabled="saving || loading"', ':disabled="saving"')
  .replace(':disabled="!day || saving || loading"', ':disabled="!day || saving"')
await assert.rejects(() => exercise(beforeNavigationFix), /Date navigation must follow loading/)
const beforeActivityLabelFix = source.replaceAll('有日记或文献活动', '已有日记')
await assert.rejects(() => exercise(beforeActivityLabelFix), /Activity-only dates must not be advertised/)
await exercise(source)
console.log('Diary real controls passed: loading, retry, selected-date navigation and activity-only dates without writes')
