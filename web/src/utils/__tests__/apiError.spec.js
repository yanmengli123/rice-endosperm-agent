import assert from 'node:assert/strict'
import { normalizeTimeoutError } from '../apiError.js'

const native = new DOMException('原始浏览器超时', 'TimeoutError')
const wrapped = normalizeTimeoutError(native)
assert.equal(wrapped.name, 'TimeoutError')
assert.equal(wrapped.message, '请求超时，请检查网络后重试')
assert.equal(wrapped.cause, native)
assert.equal(native.message, '原始浏览器超时')
const abort = new DOMException('关闭阅读器', 'AbortError')
assert.equal(normalizeTimeoutError(abort), abort)
console.log('apiError: browser read-only timeout and user cancellation passed')
