import assert from 'node:assert/strict'
import { publicSessionEvents } from '../publicSessionStream.js'

const encoded = new TextEncoder().encode(': heartbeat\r\nid: v2:s:4\r\ndata: {"text":"中文"}\r\n\r\nid: v2:s:5\ndata: {"next":\ndata: true}\n\n')
let index = 0
const body = new ReadableStream({
  pull(controller) {
    if (index === encoded.length) controller.close()
    else controller.enqueue(encoded.slice(index, ++index))
  }
})
const events = []
for await (const event of publicSessionEvents(body)) events.push(event)
assert.deepEqual(events, [{ id: 'v2:s:4', data: { text: '中文' } }, { id: 'v2:s:5', data: { next: true } }])
let cancelled = false
const open = new ReadableStream({
  start(controller) { controller.enqueue(new TextEncoder().encode('id: v2:s:6\ndata: {}\n\n')) },
  cancel() { cancelled = true }
})
for await (const event of publicSessionEvents(open)) { assert.equal(event.id, 'v2:s:6'); break }
assert.equal(cancelled, true)
assert.equal(open.locked, false)
console.log('Public Session SSE: UTF-8, CRLF, multiline, cursor and disconnect passed')
