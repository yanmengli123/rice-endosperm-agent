import assert from 'node:assert/strict'
import { test } from 'node:test'
import { parseWidget, widgetFormMessage } from '../chatWidgets.js'
import { splitWidgetContent } from '../widgetContent.js'

test('only top-level closed widget fences become interactive components', () => {
  const content = 'before\n```widget\n{"type":"Card","children":[]}\n```\nafter\n'
  const segments = splitWidgetContent(content)
  assert.deepEqual(segments.map((item) => item.type), ['markdown', 'widget', 'markdown'])
  assert.equal(segments[1].closed, true)
  assert.equal(segments[2].content, 'after\n')
  const example = '````markdown\n```widget\n{}\n```\n````\n'
  assert.deepEqual(splitWidgetContent(example), [{ type: 'markdown', content: example }])
  assert.equal(splitWidgetContent('~~~widget\n{}')[0].closed, false)
  assert.equal(splitWidgetContent('```widget\n{}\n~~~~\n')[0].closed, false)
})

test('widget parser rejects scripts, arbitrary attributes, invalid URLs and excessive input', () => {
  assert.throws(() => parseWidget(JSON.stringify({ type: 'script' })))
  assert.throws(() => parseWidget(JSON.stringify({ type: 'Card', children: [], onclick: 'alert(1)' })))
  assert.throws(() => parseWidget(JSON.stringify({ type: 'Image', src: 'javascript:alert(1)', alt: 'x' })))
  assert.throws(() => parseWidget(JSON.stringify({ type: 'Image', path: '/workspace/../secret', alt: 'x' })))
  assert.throws(() => parseWidget(' '.repeat(24001)))
  assert.throws(() => parseWidget(JSON.stringify({ type: 'Card', children: Array.from({ length: 101 }, () => ({ type: 'Text', value: 'x' })) })))
})

test('validated form sends a plain message and refuses missing required values', () => {
  const form = { type: 'Form', onSubmitAction: { type: 'send_message', payload: { text: '检索文献' } }, children: [
    { type: 'Input', name: 'gene', label: '基因', required: true },
    { type: 'RadioGroup', name: 'scope', label: '范围', options: [{ value: 'rice', label: '水稻' }] },
    { type: 'Button', label: '提交', submit: true }
  ] }
  const node = parseWidget(JSON.stringify({ type: 'Card', children: [form] })).children[0]
  assert.equal(widgetFormMessage(node, [['gene', 'Wx'], ['scope', 'rice']]), '检索文献\n基因：Wx\n范围：水稻')
  assert.throws(() => widgetFormMessage(node, [['gene', '']]))
  assert.throws(() => widgetFormMessage(node, [['gene', 'Wx'], ['scope', 'unknown']]))
})
